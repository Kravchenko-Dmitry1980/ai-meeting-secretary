"""Free deterministic routes and bound voice confirmations, without transports."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from secretary.application.bot_commands import BotCommandService
from secretary.domain.bot import BotAction, BotError, BotEvent, DeterministicReply
from secretary.domain.team import AcceptanceReceipt, CommandReceipt, ExecutionState, TaskCommand, TaskSnapshot, TeamMember


def case():
    member = TeamMember(id=str(uuid4()), display_name='Synthetic owner', max_user_id='12',
                        vikunja_user_id='23', project_ids=('7',), revision=2)
    team, bot, voice = Mock(), Mock(), Mock()
    team.get_member.return_value = member
    team.list_projections.return_value = ()
    bot.read_context.return_value = None
    bot.store_button.return_value = 'opaque-local-nonce'
    voice.enqueue.return_value = DeterministicReply('Обрабатываю. Изменения потребуют подтверждения.')
    service = BotCommandService(team, bot, public_origin='https://team.example.test', voice=voice)
    event = BotEvent(event_id=str(uuid4()), dedup_key='synthetic-message', bot_id='42',
        kind='message_created', timestamp_ms=1791090000000, user_id='12', actor_id=member.id,
        actor_revision=2, project_ids=('7',), message_id='synthetic-mid', text='Мои задачи')
    return SimpleNamespace(member=member, team=team, bot=bot, voice=voice, service=service, event=event)


@pytest.mark.parametrize('text', ['Мои задачи', 'Сегодня', 'Просрочено', 'Открыть доску',
                                 'Открыть матрицу', 'Помощь', 'Вход на компьютере'])
def test_builtin_remains_free_with_unavailable_voice_provider(text):
    c = case()
    c.voice.enqueue.side_effect = AssertionError('paid route reached by free button')
    result = c.service.handle(BotEvent(**{**c.event.__dict__, 'text': text}))
    assert isinstance(result, DeterministicReply)
    c.voice.enqueue.assert_not_called()


@pytest.mark.parametrize('media,text', [
    ((), 'Создай задачу: проверить смету'),
    (({'type': 'audio', 'payload': {'url': 'https://cdn.example.test/private?token=synthetic'}},), None),
])
def test_authorized_audio_or_free_text_only_enqueues_local_job(media, text):
    c = case()
    event = BotEvent(**{**c.event.__dict__, 'text': text, 'media': media})
    result = c.service.handle(event)
    assert 'Обрабатываю' in result.text
    c.voice.enqueue.assert_called_once_with(event, text=text)
    c.team.accept_command.assert_not_called()
    c.bot.save_proposal.assert_not_called()


def test_unknown_actor_never_enqueues_audio():
    c = case()
    event = BotEvent(**{**c.event.__dict__, 'actor_id': None, 'media': ({'type': 'audio'},)})
    with pytest.raises(BotError):
        c.service.handle(event)
    c.voice.enqueue.assert_not_called()


def test_intent_confirmation_uses_bound_revision_operation_and_no_new_analysis():
    c = case()
    proposal, operation = str(uuid4()), str(uuid4())
    c.bot.resolve_button.return_value = BotAction('confirm_intent:' + proposal + ':3', operation_id=operation)
    receipt = CommandReceipt(acceptance_receipt=AcceptanceReceipt(operation_id=operation,
        payload_hash='a' * 64, decision='accepted', decided_at=datetime(2026, 10, 4, tzinfo=timezone.utc)),
        execution_state=ExecutionState(state='queued'))
    c.voice.confirm_intent.return_value = receipt
    event = BotEvent(**{**c.event.__dict__, 'kind': 'message_callback', 'callback_id': 'synthetic-callback',
                        'callback_payload': 'opaque-local-nonce', 'text': None})
    result = c.service.handle(event)
    assert result.receipt == receipt
    c.voice.confirm_intent.assert_called_once_with(c.member.id, proposal, expected_revision=3,
                                                 operation_id=operation)
    c.voice.enqueue.assert_not_called()
    c.team.accept_command.assert_not_called()


@pytest.mark.parametrize('action', ['confirm_intent', 'confirm_intent:bad:0',
    'confirm_intent:' + str(uuid4()) + ':-1', 'confirm_intent:' + str(uuid4()) + ':true',
    'confirm_intent:' + str(uuid4()) + ':0:extra'])
def test_malformed_intent_confirmation_never_reaches_acceptance(action):
    c = case()
    c.bot.resolve_button.return_value = BotAction(action, operation_id=str(uuid4()))
    event = BotEvent(**{**c.event.__dict__, 'kind': 'message_callback', 'callback_id': 'synthetic-callback',
                        'callback_payload': 'opaque-local-nonce', 'text': None})
    with pytest.raises(BotError):
        c.service.handle(event)
    c.voice.confirm_intent.assert_not_called()


def test_disabled_voice_retains_clear_configuration_response():
    c = case()
    c.service.voice = None
    event = BotEvent(**{**c.event.__dict__, 'media': ({'type': 'audio'},), 'text': None})
    assert 'пока не подключена' in c.service.handle(event).text
    c.voice.enqueue.assert_not_called()


@pytest.mark.parametrize('purpose,paid', [('due', True), ('result', False)])
def test_task_context_only_routes_due_to_analysis_and_result_stays_free(purpose, paid):
    c = case()
    task = TaskSnapshot(task_id='50', project_id='7', revision=4, remote_fingerprint='b' * 64,
        title='Synthetic task', assignee_id=c.member.id, important=False,
        urgent=False, classification_confirmed=True)
    c.team.get_projection.return_value = task
    context = BotAction(purpose, task.task_id, TaskCommand(operation_id=str(uuid4()),
        action='link', project_id='7', task_id='50', expected_revision=4,
        expected_fingerprint='b' * 64, values={}))
    c.bot.read_context.return_value = context
    text = 'к пятнице в 16:00' if paid else 'Смета проверена, замечания устранены'
    event = BotEvent(**{**c.event.__dict__, 'text': text})
    result = c.service.handle(event)
    if paid:
        c.voice.enqueue.assert_called_once_with(event, text=text, context=context)
        assert 'Обрабатываю' in result.text
    else:
        c.voice.enqueue.assert_not_called()
        assert result.command.values.result == text
        assert result.command.values.bucket == 'done'


def test_stale_due_context_cannot_queue_paid_request():
    c = case()
    c.team.get_projection.return_value = TaskSnapshot(task_id='50', project_id='7', revision=5,
        remote_fingerprint='c' * 64, title='Changed task', assignee_id=c.member.id)
    c.bot.read_context.return_value = BotAction('due', '50', TaskCommand(operation_id=str(uuid4()),
        action='link', project_id='7', task_id='50', expected_revision=4,
        expected_fingerprint='b' * 64, values={}))
    with pytest.raises(BotError):
        c.service.handle(BotEvent(**{**c.event.__dict__, 'text': 'к пятнице в 16:00'}))
    c.voice.enqueue.assert_not_called()
