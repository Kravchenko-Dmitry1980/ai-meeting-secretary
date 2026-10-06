"""Deterministic publication-to-command mapping; no repository or HTTP access."""
from __future__ import annotations

from pydantic import ValidationError

from secretary.domain.task_delivery import GatewayPublication, delivery_uuid
from secretary.domain.team import TaskChange, TaskCommand, TaskOrigin, canonical, content_hash


MAX_PUBLICATION_BYTES = 256 * 1024
MAX_DESCRIPTION_BYTES = 16 * 1024
MAX_COMMENT_CHARACTERS = 4000
_ERROR_CODES = frozenset({
    'publication_payload_invalid', 'publication_payload_too_large',
    'publication_description_too_large', 'publication_comment_too_large',
})


class PublicationCommandError(ValueError):
    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else 'publication_payload_invalid'
        super().__init__(self.code)


def _captured(envelope: GatewayPublication) -> GatewayPublication:
    try:
        value = envelope.model_dump(mode='json')
        encoded = canonical(value).encode('utf-8')
        if len(encoded) > MAX_PUBLICATION_BYTES:
            raise PublicationCommandError('publication_payload_too_large')
        # model_copy/model_construct can bypass validation. Capture/revalidate
        # once so the exact source used by all formatting and hashing is stable.
        return GatewayPublication.model_validate_json(encoded)
    except PublicationCommandError:
        raise
    except (ValidationError, ValueError, TypeError, AttributeError, OverflowError):
        raise PublicationCommandError('publication_payload_invalid') from None


def publication_task_command(envelope: GatewayPublication) -> TaskCommand:
    envelope = _captured(envelope)
    scope, item = envelope.scope, envelope.item
    origin = TaskOrigin(source_kind='meeting', publication_id=item.publication_id,
        meeting_id=scope.meeting_id, transcript_version=scope.transcript_version,
        summary_version=scope.summary_version, action_id=item.action_id)
    common = dict(operation_id=delivery_uuid(scope, item), project_id=scope.destination_project_id, origin=origin)
    if item.intent in ('publish', 'create_separate'):
        description = '\n'.join((
            'Источник: совещание Secretary',
            f'Совещание: {scope.meeting_id}',
            f'Версия расшифровки: {scope.transcript_version}',
            f'Версия итогов: {scope.summary_version}',
            f'Проект: {scope.destination_project_id}',
            f'Действие: {item.action_id}',
            f'Публикация: {item.publication_id}',
            'Фрагменты: ' + ', '.join(item.source_segment_ids),
            'Дословная цитата:', item.evidence_quote,
        ))
        if len(description.encode('utf-8')) > MAX_DESCRIPTION_BYTES:
            raise PublicationCommandError('publication_description_too_large')
        return TaskCommand(**common, action='create', expected_assignee_revision=item.member_revision,
            values=TaskChange(title=item.title, description=description, assignee_id=item.assignee_id,
                due_at=item.due_at, due_phrase=item.due_phrase, due_timezone=item.due_timezone,
                due_confirmed=item.due_confirmed))
    target = dict(task_id=item.target_task_id, expected_revision=item.expected_task_revision,
                  expected_fingerprint=item.expected_task_fingerprint)
    if item.intent == 'link':
        return TaskCommand(**common, **target, action='link', values=TaskChange())
    date = item.due_at.isoformat() if item.due_at is not None else 'без срока'
    comment = '\n'.join((
        'Предложение из совещания Secretary (поля задачи не изменены)',
        f'Задача: {item.title}', f'Исполнитель UUID: {item.assignee_id}',
        f'Подтверждённый срок: {date}', f'Часовой пояс: {item.due_timezone}',
        f'Фраза срока: {item.due_phrase or "без срока"}',
        f'Публикация источника: {item.publication_id}',
        f'Совещание: {scope.meeting_id}; расшифровка {scope.transcript_version}; итоги {scope.summary_version}',
        f'Действие: {item.action_id}',
    ))
    if len(comment) > MAX_COMMENT_CHARACTERS:
        raise PublicationCommandError('publication_comment_too_large')
    return TaskCommand(**common, **target, action='comment', values=TaskChange(comment=comment))


def gateway_payload_hash(envelope: GatewayPublication) -> str:
    # Use the same capture for actor identity and command; never hash the DTO
    # separately from the command that TeamRepository accepts.
    envelope = _captured(envelope)
    command = publication_task_command(envelope)
    return content_hash({'actor_id': envelope.actor_id,
                         'command': command.model_dump(mode='json', exclude_unset=True)})
