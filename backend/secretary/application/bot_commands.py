"""Deterministic MAX commands over local, durable ports; no transport or AI calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from secretary.domain.bot import BotAction, BotButton, BotError, BotEvent, CommandPreview, DeterministicReply, uuid_value
from secretary.domain.team import TaskCommand


_PAGE_SIZE = 5
# Operational task dates use Moscow's current UTC+03:00 policy. This avoids an
# undeclared tzdata dependency on Windows; this is not a historic DST converter.
_MOSCOW = timezone(timedelta(hours=3), 'Europe/Moscow')
_DECIMAL = re.compile(r'[1-9][0-9]{0,127}\Z')
_LIST_NAMES = {'mine': 'Мои задачи', 'today': 'Сегодня', 'overdue': 'Просрочено'}
_STATE_NAMES = {'inbox': 'Новая', 'accepted': 'Принята', 'doing': 'В работе',
                'blocked': 'Заблокирована', 'review': 'На проверке', 'done': 'Готово', 'cancelled': 'Отменена'}
_PENDING_INVITATION = 'Запрос на доступ отправлен владельцу. Дождитесь подтверждения.'


def _short(value, limit=180):
    """Only presentation is shortened. Stored commands/proposals keep literal input."""
    return value if len(value) <= limit else value[:limit - 1] + '…'


class BotCommandService:
    def __init__(self, team, bot, *, public_origin, bot_username=None, clock=None, voice=None):
        try:
            parsed = urlsplit(public_origin)
            valid = (parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment and parsed.path in ('', '/')
                     and '?' not in public_origin and '#' not in public_origin
                     and not any(ord(char) <= 32 for char in public_origin))
            parsed.port  # Reject malformed ports even though no connection is made.
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise BotError('bot_public_origin_invalid')
        if bot_username is not None and (not isinstance(bot_username, str)
                or not re.fullmatch(r'[A-Za-z0-9_]{1,128}', bot_username)):
            raise BotError('bot_username_invalid')
        self.team, self.bot = team, bot
        self.voice = voice
        self.public_origin = public_origin.rstrip('/')
        self.bot_username = bot_username
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BotError('bot_clock_invalid')
        return value.astimezone(timezone.utc)

    def _actor(self, event):
        if (event.quarantine_reason or not event.actor_id or event.actor_revision is None
                or not event.user_id or not isinstance(event.project_ids, tuple)
                or any(not isinstance(value, str) or not _DECIMAL.fullmatch(value) for value in event.project_ids)
                or len(set(event.project_ids)) != len(event.project_ids)):
            raise BotError('bot_actor_unavailable')
        member = self.team.get_member(event.actor_id)
        if (member is None or member.id != event.actor_id or not member.enabled
                or member.max_user_id != event.user_id or member.revision != event.actor_revision
                or tuple(sorted(event.project_ids)) != member.project_ids):
            raise BotError('bot_actor_changed')
        return member

    def handle(self, event: BotEvent) -> CommandPreview | DeterministicReply:
        if (event.actor_id is None and event.invitation_id and not event.quarantine_reason
                and event.kind == 'invite_bot_started'):
            return DeterministicReply(_PENDING_INVITATION)
        self._actor(event)
        if event.kind == 'message_callback':
            if not event.callback_id or not event.callback_payload:
                raise BotError('bot_callback_invalid')
            return self._callback(event, self.bot.resolve_button(event))
        if event.kind not in {'message_created', 'bot_started'}:
            raise BotError('bot_event_unsupported')
        if event.media:
            if self.voice is not None:
                return self.voice.enqueue(event, text=None)
            return DeterministicReply('Обработка аудио и вложений пока не подключена. Отправьте команду текстом.')
        text = (event.text or '').strip()
        if event.kind == 'bot_started' or text.casefold() in {'помощь', '/help', '/start'}:
            return self._help()
        for mode, name in _LIST_NAMES.items():
            if text.casefold() == name.casefold():
                if not event.project_ids:
                    return DeterministicReply('Нет доступных проектов. Обратитесь к владельцу.')
                return self._list(event, mode, sorted(event.project_ids, key=lambda item: (len(item), item))[0])
        if text.casefold() in {'открыть доску', 'открыть матрицу'}:
            return self._open('board' if text.casefold() == 'открыть доску' else 'matrix')
        if text.casefold() == 'вход на компьютере':
            return DeterministicReply('Запрошен одноразовый вход на компьютере. Код будет выдан отдельно для формы входа.',
                                      sensitive_action='desktop_code')
        if not text:
            return DeterministicReply('Отправьте команду или предложение текстом. Обработка вложений пока не подключена.')
        context = self.bot.read_context(event)
        if context is not None:
            return self._context_text(event, context)
        if self.voice is not None:
            return self.voice.enqueue(event, text=event.text)
        proposal = self.bot.save_proposal(event, event.text, purpose='intent')
        return DeterministicReply(f'Предложение сохранено: {proposal}. Задача не создана. '
                                  'Разбор свободного текста пока не подключён; доступны команды из «Помощь».')

    @staticmethod
    def _help():
        return DeterministicReply('Доступные команды:\nМои задачи\nСегодня\nПросрочено\nОткрыть доску\n'
            'Открыть матрицу\nВход на компьютере\nПомощь\n\nВ карточках: Принять, В работе, Готово, Предложить срок. '
            'Изменение задачи требует отдельного подтверждения. Для завершения нужен результат; '
            'важные и неклассифицированные задачи направляются на проверку. Свободный текст сохраняется как предложение.')

    def _open(self, view):
        name = 'Открыть доску' if view == 'board' else 'Открыть матрицу'
        if self.bot_username:
            return DeterministicReply(name + '.', ((BotButton(name, kind='open_app',
                web_app=self.bot_username, payload=view),),))
        return DeterministicReply('Открытие Mini App в MAX не настроено. Доступна HTTPS-ссылка на командный интерфейс.',
            ((BotButton(name, kind='link', url=f'{self.public_origin}/team/?view={view}'),),))

    def _button(self, event, key, label, action, **bindings):
        value = self.bot.store_button(event, key=key, action=action, **bindings)
        return BotButton(label, payload=value)

    def _list(self, event, mode, project, after=None):
        if mode not in _LIST_NAMES or project not in event.project_ids or (after is not None and not _DECIMAL.fullmatch(after)):
            raise BotError('bot_action_invalid')
        page = self.team.list_projections(event.actor_id, project, limit=_PAGE_SIZE, after=after,
                                          mine=True, expected_actor_revision=event.actor_revision)
        tasks = page[:_PAGE_SIZE]
        now = self._now()
        today = now.astimezone(_MOSCOW).date()
        lines = [f'{_LIST_NAMES[mode]} · проект {project}', 'Сроки показаны по Москве.']
        buttons = []
        for task in tasks:
            if task.project_id != project or task.assignee_id != event.actor_id:
                raise BotError('bot_task_unavailable')
            if task.bucket in {'done', 'cancelled'}:
                continue
            if mode != 'mine':
                if not task.due_confirmed or task.due_at is None:
                    continue
                if mode == 'today' and task.due_at.astimezone(_MOSCOW).date() != today:
                    continue
                if mode == 'overdue' and task.due_at >= now:
                    continue
            due = task.due_at.astimezone(_MOSCOW).strftime('%d.%m.%Y %H:%M') if task.due_at and task.due_confirmed else 'не задан'
            lines.append(f'\n#{task.task_id} {_short(task.title)}\n{_STATE_NAMES[task.bucket]} · срок: {due}')
            item_key = str(uuid5(NAMESPACE_URL, 'secretary:max:task:' + task.task_id))
            buttons.append(tuple(self._button(event, f'{action}:{item_key}', label, action, task_id=task.task_id)
                for action, label in (('accept', 'Принять'), ('doing', 'В работе'), ('done', 'Готово'), ('due', 'Предложить срок'))))
        if not buttons:
            lines.append('\nНа этой странице подходящих задач нет.')
        if len(page) > _PAGE_SIZE:
            action = f'list:{mode}:{project}:{tasks[-1].task_id}'
        else:
            projects = sorted(event.project_ids, key=lambda item: (len(item), item))
            index = projects.index(project)
            action = f'list:{mode}:{projects[index + 1]}:0' if index + 1 < len(projects) else None
        if action:
            buttons.append((self._button(event, 'next', 'Следующая страница', action),))
            lines.append('\nЕсть следующие страницы.')
        return DeterministicReply('\n'.join(lines), tuple(buttons))

    def _task(self, event, identifier):
        if not isinstance(identifier, str) or not _DECIMAL.fullmatch(identifier):
            raise BotError('bot_action_invalid')
        task = self.team.get_projection(event.actor_id, identifier, expected_actor_revision=event.actor_revision)
        if (task.task_id != identifier or task.project_id not in event.project_ids
                or task.assignee_id != event.actor_id or task.bucket in {'done', 'cancelled'}):
            raise BotError('bot_task_unavailable')
        return task

    def _callback(self, event, action):
        if not isinstance(action, BotAction):
            raise BotError('bot_action_invalid')
        if action.action.startswith('list:'):
            parts = action.action.split(':')
            if len(parts) != 4:
                raise BotError('bot_action_invalid')
            return self._list(event, parts[1], parts[2], None if parts[3] == '0' else parts[3])
        if action.action == 'confirm':
            # The repository atomically consumes the bound nonce and accepts its
            # immutable command. Rebuilding a command here would break replay.
            return self._receipt(event, self.bot.confirm_button(event))
        if action.action.startswith('confirm_intent:'):
            parts = action.action.split(':')
            if (self.voice is None or len(parts) != 3 or not re.fullmatch(r'0|[1-9][0-9]{0,9}', parts[2])
                    or action.operation_id is None or action.command is not None):
                raise BotError('bot_action_invalid')
            uuid_value(parts[1])
            uuid_value(action.operation_id)
            return self._receipt(event, self.voice.confirm_intent(event.actor_id, parts[1],
                expected_revision=int(parts[2]), operation_id=action.operation_id))
        if action.action == 'status' and action.operation_id:
            return self._receipt(event, self.team.get_receipt(event.actor_id, action.operation_id,
                expected_actor_revision=event.actor_revision))
        if action.action not in {'accept', 'doing', 'done', 'due'}:
            raise BotError('bot_action_invalid')
        task = self._task(event, action.task_id)
        if (type(action.expected_revision) is not int or action.expected_revision != task.revision
                or action.expected_fingerprint != task.remote_fingerprint):
            raise BotError('bot_button_stale')
        if action.action in {'done', 'due'}:
            purpose = 'result' if action.action == 'done' else 'due'
            self.bot.save_context(event, task.task_id, purpose)
            if purpose == 'result':
                return DeterministicReply('Пришлите следующим текстовым сообщением результат по задаче '
                    f'#{task.task_id} «{_short(task.title)}». Затем подтвердите изменение.')
            if self.voice is not None:
                return DeterministicReply('Пришлите следующим текстовым сообщением предлагаемый срок и причину для задачи '
                    f'#{task.task_id}. Распознанный срок потребуется подтвердить.')
            return DeterministicReply('Пришлите следующим текстовым сообщением предлагаемый срок и причину для задачи '
                f'#{task.task_id}. Текст сохранится как предложение; разбор даты пока не подключён.')
        return self._preview(event, task, 'accepted' if action.action == 'accept' else 'doing')

    def _context_text(self, event, context):
        if (not isinstance(context, BotAction) or context.action not in {'result', 'due'}
                or context.command is None or context.command.action != 'link'):
            raise BotError('bot_context_invalid')
        task = self._task(event, context.task_id)
        command = context.command
        if (command.task_id != task.task_id or command.project_id != task.project_id
                or command.expected_revision != task.revision or command.expected_fingerprint != task.remote_fingerprint):
            raise BotError('bot_context_stale')
        if context.action == 'due':
            if self.voice is not None:
                return self.voice.enqueue(event, text=event.text, context=context)
            proposal = self.bot.save_proposal(event, event.text, task_id=task.task_id, purpose='due')
            return DeterministicReply(f'Предложение срока сохранено: {proposal}. Срок задачи не изменён. '
                                      'Разбор даты пока не подключён.')
        target = 'done' if task.important is False and task.classification_confirmed else 'review'
        return self._preview(event, task, target, result=event.text)

    def _preview(self, event, task, bucket, *, result=None):
        operation = str(uuid5(NAMESPACE_URL, f'secretary:max:{event.bot_id}:{event.event_id}:state:{bucket}:{task.task_id}'))
        values = {'bucket': bucket}
        if result is not None:
            values['result'] = result
        command = TaskCommand(operation_id=operation, project_id=task.project_id, task_id=task.task_id,
            expected_revision=task.revision, expected_fingerprint=task.remote_fingerprint,
            action='set_state', values=values)
        text = f'Подтвердите: #{task.task_id} «{_short(task.title)}» → {_STATE_NAMES[bucket]}.'
        if result is not None:
            text += '\nРезультат: ' + _short(result, 1600)
            if len(result) > 1600:
                text += '\nВ подтверждении сохранён полный текст вашего сообщения.'
        text += '\nИзменение ещё не принято.'
        button = self._button(event, 'confirm', 'Подтвердить', 'confirm', command=command)
        return CommandPreview(text, ((button,),), command=command)

    def _receipt(self, event, receipt):
        texts = {
            'queued': 'Команда принята в очередь. Выполнение ещё не подтверждено.',
            'running': 'Команда выполняется. Результат ещё не подтвержден.',
            'reconciling': 'Идёт сверка результата с доской. Повторное изменение не отправляется.',
            'applied': 'Изменение подтверждено проверкой карточки.',
            'conflict': 'Конфликт: карточка изменилась. Откройте свежий список задач.',
            'uncertain': 'Результат неизвестен. Идёт проверка; повторно отправлять изменение не нужно.',
            'rejected': 'Команда отклонена. Откройте свежий список задач или обратитесь к владельцу.',
        }
        state = receipt.execution_state.state
        button = self._button(event, 'status', 'Проверить статус', 'status',
                              operation_id=receipt.acceptance_receipt.operation_id)
        return DeterministicReply(texts[state], ((button,),), receipt=receipt)
