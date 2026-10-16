"""Short MAX commands: durable checkpoints, conservative previews, explicit consent.

The model only suggests data. This service never executes a task, downloads an
unbound attachment, reserves money itself, or retries an ambiguous paid request.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from secretary.domain.bot import BotAction, BotButton, BotError, DeterministicReply
from secretary.domain.team import TaskCommand, TaskOrigin
from secretary.domain.voice_commands import IntentProposal, RawIntentReply, VoiceError


_MOSCOW = timezone(timedelta(hours=3), 'Europe/Moscow')
_WEEKDAYS = ('понедельник', 'вторник', 'сред', 'четверг', 'пятниц', 'суббот', 'воскресень')
_MONTHS = ('января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря')
_NEGATED = re.compile(r'\b(?:не\s+(?:став\w*|созда\w*|назнач\w*|дела\w*|завод\w*|меня\w*|перенос\w*|нуж\w*|надо|хочу)|не\s+нужно|отмени\s+поручение)\b', re.I)
_REPORTED = re.compile(r'\b(?:обсуждал\w*|говорил\w*|сказал\w*|мог\w*\s+бы|если|нужно\s+ли|можно\s+ли|что\s+если|цитата\s+для\s+протокола)\b', re.I)
_RETRACTED = re.compile(r'\b(?:отменяю|ничего\s+не\s+меняй|не\s+выполняй\s+(?:это|поручение)|отмени\s+(?:это\s+)?поручение)\b', re.I)
_NAMES = {'member':'исполнителя', 'project':'проект', 'task':'задачу', 'due':'дату',
          'due_time':'время', 'classification':'важность и срочность', 'result':'результат',
          'evidence':'цитату', 'action':'действие'}
_ACTIONS = {'create':'Создать задачу', 'set_state':'Изменить статус', 'assign':'Назначить исполнителя',
            'set_due':'Изменить срок', 'propose_due':'Предложить срок', 'classify':'Изменить классификацию',
            'comment':'Добавить комментарий', 'rename':'Изменить название'}
_BUCKETS = {'inbox':'Новая','accepted':'Принята','doing':'В работе','blocked':'Заблокирована',
            'review':'На проверке','done':'Готово','cancelled':'Отменена'}


def _identity(event_id, kind):
    return str(uuid5(NAMESPACE_URL, f'secretary:voice:{event_id}:{kind}'))


def _source_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _short(text, limit=240):
    return text if len(text) <= limit else text[:limit-1] + '…'


def _noncommand(text, quote):
    """Check containing clauses, including a prefix omitted by model evidence.

    Repeated evidence is accepted only when all its occurrences are commands;
    choosing the affirmative occurrence of a repeated reported quote is unsafe.
    """
    if _RETRACTED.search(text):
        return True
    start = 0
    found = False
    while (index := text.find(quote, start)) >= 0:
        found = True
        left = max((text.rfind(mark, 0, index) for mark in '.!?\n;'), default=-1) + 1
        endings = [end for mark in '.!?\n;' if (end := text.find(mark, index+len(quote))) >= 0]
        right = min(endings) + 1 if endings else len(text)
        clause = text[left:right]
        if '?' in clause or _NEGATED.search(clause) or _REPORTED.search(clause):
            return True
        start = index + len(quote)
    return not found


def _due(phrase, proposed, text, event_time):
    """Resolve only explicit local calendar forms, never model-invented times."""
    if not phrase:
        return None, ('due',) if proposed is not None else ()
    if phrase not in text:
        raise VoiceError('voice_evidence_invalid')
    lowered = phrase.casefold()
    if re.search(r'когда-нибудь|примерно|возможно|может|\bили\b|недел', lowered):
        return None, ('due',)
    date = None
    explicit = re.search(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', phrase)
    russian = re.search(r'\b(\d{1,2})\s+('+ '|'.join(_MONTHS)+r')\s+(\d{4})\b', lowered)
    days = [index for index, word in enumerate(_WEEKDAYS) if re.search(r'\b'+word+r'\w*\b', lowered)]
    if explicit:
        try:
            date = datetime.strptime(explicit.group(1), '%Y-%m-%d').date()
        except ValueError:
            return None, ('due',)
    elif russian:
        try:
            date = datetime(int(russian.group(3)),_MONTHS.index(russian.group(2))+1,int(russian.group(1))).date()
        except ValueError:
            return None, ('due',)
    else:
        today = event_time.astimezone(_MOSCOW).date()
        if re.search(r'\bпослезавтра\b', lowered):
            date = today + timedelta(days=2)
        elif re.search(r'\bзавтра\b', lowered):
            date = today + timedelta(days=1)
        elif re.search(r'\bсегодня\b', lowered):
            date = today
        else:
            if len(days) == 1:
                delta = (days[0] - today.weekday()) % 7
                if 'следующ' in lowered and delta == 0:
                    delta = 7
                date = today + timedelta(days=delta)
    if date is None:
        return None, ('due',)
    if days and (len(days)!=1 or days[0]!=date.weekday()):
        return None, ('due',)
    iso = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})', phrase)
    if iso:
        try:
            local = datetime.fromisoformat(iso.group(0).replace('Z', '+00:00'))
        except ValueError:
            return None, ('due',)
        due = local.astimezone(timezone.utc)
        return (due,()) if proposed == due else (None,('due',))
    times = re.findall(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)', phrase)
    if len(times) != 1:
        return None, ('due_time',)
    hour, minute = map(int, times[0])
    local = datetime(date.year, date.month, date.day, hour, minute, tzinfo=_MOSCOW)
    due = local.astimezone(timezone.utc)
    if proposed is None or proposed != due:
        return None, ('due',)
    return due, ()


class VoiceCommandService:
    def __init__(self, team, bot, voices, media, polza, *, clock=None):
        self.team, self.bot, self.voices, self.media, self.polza = team, bot, voices, media, polza
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise VoiceError('voice_clock_invalid')
        return now.astimezone(timezone.utc)

    def _actor(self, event):
        if (event.quarantine_reason or not event.actor_id or event.actor_revision is None or not event.user_id
                or len(set(event.project_ids)) != len(event.project_ids)):
            raise VoiceError('voice_actor_unavailable')
        member = self.team.get_member(event.actor_id)
        if (member is None or not member.enabled or member.id != event.actor_id
                or member.max_user_id != event.user_id or member.revision != event.actor_revision
                or tuple(sorted(event.project_ids)) != member.project_ids):
            raise VoiceError('voice_actor_changed')
        return member

    def enqueue(self, event, text=None, *, context=None):
        self._actor(event)
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 16000):
            raise VoiceError('voice_text_invalid')
        if context is not None:
            if context != self.bot.read_context(event):
                raise VoiceError('voice_context_invalid')
            self._due_binding(event,context)
        self.voices.enqueue(event, text=text)
        source = 'текстовую команду' if text is not None else 'аудиосообщение'
        return DeterministicReply(f'Обрабатываю {source}. Результат появится отдельным сообщением; изменение задачи потребует подтверждения.')

    def _authorize(self, claim, cancelled):
        if cancelled and cancelled():
            raise VoiceError('voice_cancelled')
        event = self.voices.authorize_job(claim)
        if event != claim.event:
            raise VoiceError('voice_actor_changed')
        return self._actor(event)

    def _due_binding(self, event, binding):
        if binding is None:
            return None
        if (not isinstance(binding,BotAction) or binding.action!='due' or binding.command is None
                or binding.command.action!='link' or binding.task_id!=binding.command.task_id):
            raise VoiceError('voice_context_invalid')
        task = self.team.get_projection(event.actor_id,binding.task_id,expected_actor_revision=event.actor_revision)
        command = binding.command
        if (task.project_id not in event.project_ids or task.project_id!=command.project_id
                or task.revision!=command.expected_revision or task.remote_fingerprint!=command.expected_fingerprint):
            raise VoiceError('voice_context_stale')
        return task

    def _context(self, event, binding=None):
        actor = self._actor(event)
        if not event.project_ids or len(event.project_ids) > 10:
            raise VoiceError('voice_context_unavailable')
        members = {actor.id: actor}
        tasks = {}
        bound_task = self._due_binding(event,binding)
        if bound_task:
            tasks[bound_task.task_id] = bound_task
        for project in event.project_ids:
            if actor.role == 'owner':
                for member in self.team.list_members(actor.id, project):
                    if member.enabled and project in member.project_ids:
                        members[member.id] = member
            if len(members) > 10:
                raise VoiceError('voice_context_too_large')
            if not bound_task and len(tasks) < 100:
                page = self.team.list_projections(actor.id, project, limit=100-len(tasks),
                    expected_actor_revision=actor.revision)
                for task in page[:100-len(tasks)]:
                    if task.project_id != project:
                        raise VoiceError('voice_scope_invalid')
                    tasks[task.task_id] = task
        try:
            event_time = datetime.fromtimestamp(event.timestamp_ms/1000, tz=_MOSCOW)
        except (ValueError, OverflowError, OSError):
            raise VoiceError('voice_event_time_invalid') from None
        context = dict(members=[{'id':m.id, 'display_name':m.display_name,
                        'project_ids':[p for p in m.project_ids if p in event.project_ids], 'revision':m.revision}
                        for m in sorted(members.values(), key=lambda m:m.id)],
            project_ids=list(event.project_ids), tasks=[{'id':t.task_id, 'project_id':t.project_id,
                'revision':t.revision, 'remote_fingerprint':t.remote_fingerprint, 'title':t.title,
                'assignee_id':t.assignee_id, 'bucket':t.bucket, 'important':t.important,
                'classification_confirmed':t.classification_confirmed} for t in tasks.values()],
            event_datetime=event_time.isoformat(), event_timestamp_ms=event.timestamp_ms,
            timezone='Europe/Moscow', actor_id=actor.id, actor_revision=actor.revision,
            default_project_id=event.project_ids[0] if len(event.project_ids)==1 else None)
        if len(json.dumps(context, ensure_ascii=False).encode('utf-8')) > 32768:
            raise VoiceError('voice_context_too_large')
        return context

    @staticmethod
    def _replay(responses, stage):
        cached = {}
        for item in responses:
            if item['stage']!=stage:
                continue
            status = item.get('status')
            if type(status) is not int or status!=200:
                if type(status) is int and 400<=status<500:
                    raise VoiceError('voice_provider_response_rejected')
                raise VoiceError('voice_provider_response_uncertain',uncertain=True)
            cached[item['attempt']] = item['provider_response']
        return cached

    def _hooks(self, claim, cancelled):
        def before(info):
            self._authorize(claim, cancelled)
            self.voices.begin_request(claim, info)
        def response(info):
            # Receipt persistence must finish even when cancellation was requested.
            self.voices.record_response(claim, info)
        def not_submitted(info):
            self.voices.record_not_submitted(claim, info)
        return {'before_submission':before, 'response_checkpoint':response,
                'not_submitted_checkpoint':not_submitted}

    async def _download(self, event, cancelled):
        task = asyncio.create_task(asyncio.to_thread(self.media.download_voice, event, cancelled=cancelled))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Drain the owned thread: it must finish cleanup before the job ends.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise

    def _proposals(self, event, text, raw, context, binding=None):
        try:
            reply = RawIntentReply.model_validate(raw)
        except ValidationError:
            raise VoiceError('voice_intent_invalid') from None
        projects = set(context['project_ids'])
        members = {m['id']:m for m in context['members']}
        tasks = {t['id']:t for t in context['tasks']}
        source_hash = _source_hash(text)
        now = self._now()
        event_time = datetime.fromisoformat(context['event_datetime'])
        proposals = []
        for index, item in enumerate(reply.proposals):
            if binding is not None:
                if (item.action!='propose_due' or item.task_id not in (None,binding.task_id)
                        or item.project_id not in (None,binding.command.project_id)):
                    raise VoiceError('voice_context_invalid')
                item = item.model_copy(update={'task_id':binding.task_id,'project_id':binding.command.project_id})
            if (item.project_id is not None and item.project_id not in projects
                    or item.task_id is not None and item.task_id not in tasks
                    or item.member_id is not None and item.member_id not in members):
                raise VoiceError('voice_scope_invalid')
            if item.evidence_quote not in text:
                raise VoiceError('voice_evidence_invalid')
            if item.utterance_kind != 'command' or _noncommand(text, item.evidence_quote):
                continue
            unresolved = set(item.unresolved_fields)
            task = tasks.get(item.task_id)
            project = item.project_id or (task['project_id'] if task else context['default_project_id'])
            if project is None:
                unresolved.add('project')
            if task and task['project_id'] != project:
                raise VoiceError('voice_scope_invalid')
            if item.action != 'create' and task is None:
                unresolved.add('task')
            elif task and binding is None and not (re.search(r'(?<!\d)'+re.escape(task['id'])+r'(?!\d)',text)
                    or task['title'].casefold() in text.casefold()):
                unresolved.add('task')
            if not task and len(projects)>1 and project is not None and not re.search(
                    r'\bпроект\w*\s*(?:№|#)?\s*'+re.escape(project)+r'(?!\d)',text,re.I):
                unresolved.add('project')
            member = members.get(item.member_id)
            if item.action in {'create','assign'}:
                mention = item.person_mention
                if mention is not None and mention.casefold() in {'мне','меня','я'}:
                    exact = [members[context['actor_id']]] if re.search(r'\b'+re.escape(mention)+r'\b',text,re.I) else []
                else:
                    exact = [m for m in members.values() if mention is not None
                        and (mention.casefold()==m['display_name'].casefold() or mention==m['id'])]
                if mention is None or mention not in text or len(exact)!=1:
                    unresolved.add('member')
                elif member and exact[0]['id'] != member['id']:
                    raise VoiceError('voice_member_evidence_invalid')
                else:
                    member = exact[0]
                if member is None or project not in member['project_ids']:
                    unresolved.add('member')
            due, date_unresolved = _due(item.due_phrase,item.proposed_due_at,text,event_time)
            unresolved.update(date_unresolved)
            if item.action in {'set_due','propose_due'} and due is None:
                unresolved.add('due')
            values = {}
            if item.action == 'create':
                if not item.title:
                    unresolved.add('action')
                else:
                    values['title'] = item.title
                    if item.title.casefold() not in text.casefold():
                        unresolved.add('evidence')
                if member:
                    values['assignee_id'] = member['id']
                if item.text:
                    if item.text not in text:
                        unresolved.add('evidence')
                    else:
                        values['description'] = item.text
                if due:
                    values.update(due_at=due,due_phrase=item.due_phrase,due_confirmed=True)
                if item.important is not None and item.urgent is not None:
                    values.update(important=item.important,urgent=item.urgent,classification_confirmed=True)
            elif item.action == 'assign':
                if member:
                    values['assignee_id'] = member['id']
            elif item.action == 'set_state':
                if not item.bucket:
                    unresolved.add('action')
                else:
                    bucket = item.bucket
                    if bucket == 'done':
                        if not item.text or item.text not in text:
                            unresolved.add('result')
                        else:
                            values['result'] = item.text
                        if task and (task['important'] is not False or not task['classification_confirmed']):
                            bucket = 'review'
                    values['bucket'] = bucket
            elif item.action in {'set_due','propose_due'}:
                values.update(due_at=due,due_phrase=item.due_phrase,due_confirmed=True,
                              reason=item.evidence_quote)
            elif item.action == 'classify':
                if item.important is None or item.urgent is None:
                    unresolved.add('classification')
                else:
                    values.update(important=item.important,urgent=item.urgent,classification_confirmed=True)
            elif item.action == 'comment':
                if not item.text or item.text not in text:
                    unresolved.add('evidence')
                else:
                    values['comment'] = item.text
            elif item.action == 'rename':
                if not item.title or item.title not in text:
                    unresolved.add('evidence')
                else:
                    values['title'] = item.title
            identifier = _identity(event.event_id, f'proposal:{index}:{source_hash}')
            operation_id = _identity(event.event_id, f'operation:{identifier}')
            assignee_revision = member['revision'] if member and item.action in {'create','assign'} else None
            command = None
            if not unresolved:
                try:
                    command = TaskCommand(operation_id=operation_id, project_id=project,
                        task_id=item.task_id, action=item.action, values=values,
                        expected_revision=task['revision'] if task else None,
                        expected_fingerprint=task['remote_fingerprint'] if task else None,
                        expected_assignee_revision=assignee_revision, origin=TaskOrigin(source_kind='max'))
                except ValidationError:
                    unresolved.add('action')
            proposals.append(IntentProposal(id=identifier,command_id=event.event_id,
                actor_id=event.actor_id,actor_revision=event.actor_revision,project_ids=event.project_ids,
                action=item.action,project_id=project,task_id=item.task_id,
                expected_revision=task['revision'] if task else None,
                expected_fingerprint=task['remote_fingerprint'] if task else None,
                expected_assignee_revision=assignee_revision,evidence_quote=item.evidence_quote,
                unresolved_fields=tuple(sorted(unresolved)),proposed_due_at=due,due_phrase=item.due_phrase,
                expires_at=now+timedelta(hours=24),source_hash=source_hash,operation_id=operation_id,command=command))
        return tuple(proposals)

    def _finish(self, claim, proposals):
        context = self.voices.read_context(claim)
        member_names = {m['id']:m['display_name'] for m in context['members']} if context else {}
        lines = ['Предложения по команде. Изменения ещё не применены.']
        summary = ['Предложения по команде. Полный текст отправлен перед этим сообщением. Изменения ещё не применены.']
        buttons = []
        for index, proposal in enumerate(proposals, 1):
            lines.append(f'\n{index}. {_ACTIONS[proposal.action]} · проект {proposal.project_id or "не выбран"}'
                         + (f' · задача #{proposal.task_id}' if proposal.task_id else ''))
            summary.append(f'\n{index}. {_ACTIONS[proposal.action]} · проект {_short(proposal.project_id or "не выбран",24)}'
                           + (f' · задача #{_short(proposal.task_id,24)}' if proposal.task_id else ''))
            lines.append(f'Цитата: «{proposal.evidence_quote}»')
            if proposal.proposed_due_at:
                lines.append('Срок: '+proposal.proposed_due_at.astimezone(_MOSCOW).strftime('%d.%m.%Y %H:%M')+' МСК')
            if proposal.unresolved_fields:
                explanation = 'Уточните: '+', '.join(_NAMES[x] for x in proposal.unresolved_fields)+'. Пришлите новую команду.'
                lines.append(explanation)
                summary.append(explanation)
            elif proposal.command is not None:
                command = proposal.command
                if command.values.title:
                    lines.append('Название: '+command.values.title)
                if command.values.assignee_id:
                    lines.append('Исполнитель: '+member_names.get(command.values.assignee_id,'недоступен'))
                if command.values.bucket:
                    lines.append('Статус: '+_BUCKETS[command.values.bucket])
                if command.values.result:
                    lines.append('Результат: '+command.values.result)
                if command.values.comment:
                    lines.append('Комментарий: '+command.values.comment)
                if command.values.description:
                    lines.append('Описание: '+command.values.description)
                if command.values.reason:
                    lines.append('Причина: '+command.values.reason)
                if command.values.important is not None:
                    lines.append('Важность: '+('важная' if command.values.important else 'не важная'))
                if command.values.urgent is not None:
                    lines.append('Срочность: '+('срочная' if command.values.urgent else 'не срочная'))
                payload = self.bot.store_button(claim.event,key=proposal.id,
                    action=f'confirm_intent:{proposal.id}:{proposal.revision}',operation_id=proposal.operation_id,expires_seconds=86400)
                buttons.append((BotButton(f'Подтвердить {index}',payload=payload),))
        if not proposals:
            lines = ['Явных поручений не найдено. Задачи не изменены. Отправьте отдельную команду, если нужно создать поручение.']
        text = '\n'.join(lines)
        if len(text)>4000:
            # Full immutable values precede the confirmation buttons in the
            # repository's per-event FIFO outbox. A restart repeats the same
            # chunks/keys, rather than publishing an unseen truncated command.
            for index,start in enumerate(range(0,len(text),3500)):
                self.voices.enqueue_notice(claim.event,_identity(claim.event.event_id,f'detail:{index}'),
                                           DeterministicReply(text[start:start+3500]))
            text = '\n'.join(summary)
        reply = DeterministicReply(text,tuple(buttons))
        self.voices.enqueue_notice(claim.event,_identity(claim.event.event_id,'result'),reply)
        self.voices.complete_job(claim)
        return reply

    def _pending(self, claim):
        return self.voices.pending_request(claim)

    def _progress(self, claim, stage):
        text = 'Расшифровываю аудио.' if stage=='stt' else 'Разбираю поручение.'
        self.voices.enqueue_notice(claim.event,_identity(claim.event.event_id,f'progress:{stage}'),DeterministicReply(text))

    def _failure(self, claim, error, *, cancelled=False):
        code = 'voice_cancelled' if cancelled else getattr(error,'code','voice_processing_failed')
        if not isinstance(code,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,100}',code):
            code = 'voice_processing_failed'
        pending = self._pending(claim)
        if pending or getattr(error,'uncertain',False):
            state = 'uncertain'
            message = 'Результат оплачиваемого запроса пока не определён. Повторная отправка остановлена; требуется сверка.'
        elif code in {'monthly_budget_exhausted','monthly_budget_scope_exhausted'}:
            state = 'paused_budget'
            message = 'Обработка приостановлена: общий бюджет Polza исчерпан. Встроенные команды и кнопки продолжают работать.'
        elif code == 'insufficient_funds':
            state = 'paused_budget'
            message = 'Обработка приостановлена: недостаточно средств в Polza. Владелец должен проверить баланс. Встроенные команды и кнопки продолжают работать.'
        elif code in {'missing_key','invalid_base_url','monthly_budget_required','monthly_budget_configuration',
                      'monthly_budget_stale','monthly_budget_key_changed','monthly_budget_estimate_unavailable',
                      'monthly_budget_period_changed',
                      'authentication','access_denied','unsupported_response_format','invalid_request'}:
            state = 'paused_config'
            message = 'Обработка приостановлена: владелец должен проверить настройки Polza и общего бюджета.'
        elif code in {'max_media_configuration_required','max_media_configuration_invalid','max_media_transport_unsafe'}:
            state = 'paused_config'
            message = 'Загрузка голосовых сообщений ещё не настроена. Владелец должен проверить подключение MAX.'
        elif code in {'max_media_duration_exceeded','max_media_too_large'}:
            state = 'rejected'
            message = 'Пришлите аудио длительностью не более 120 секунд и размером не более 10 MiB. Задачи не изменены.'
        elif code in {'max_media_invalid','max_media_encoding_invalid','max_media_process_failed','max_media_process_output_invalid'}:
            state = 'rejected'
            message = 'Аудио не удалось прочитать или формат не поддерживается. Пришлите другой аудиофайл либо команду текстом.'
        else:
            state = 'rejected'
            message = ('Обработка отменена. Задачи не изменены.' if cancelled or code=='voice_cancelled' else
                       'Команда требует уточнения или недоступна. Задачи не изменены. Пришлите аудиофайл или отдельную текстовую команду.')
        self.voices.fail_job(claim,state=state,error_code=code)
        reply = DeterministicReply(message)
        try:
            self.voices.enqueue_notice(claim.event,_identity(claim.event.event_id,f'error:{state}:{code}'),reply)
        except BotError as notice_error:
            if notice_error.code not in {'voice_actor_changed','bot_actor_changed','bot_actor_unknown','voice_actor_unavailable'}:
                raise
        return reply

    async def execute(self, claim, *, cancelled=None):
        try:
            self._authorize(claim,cancelled)
            saved = self.voices.get_job(claim.job_id)
            proposals = saved.get('proposals',())
            if proposals or saved.get('stage')=='complete':
                return self._finish(claim,proposals)
            text = self.voices.transcript(claim)
            hooks = self._hooks(claim,cancelled)
            responses = self.voices.read_responses(claim)
            if text is None:
                replay = self._replay(responses,'stt')
                audio = None
                if not replay:
                    self._authorize(claim,cancelled)
                    audio = await self._download(claim.event,cancelled)
                    self._progress(claim,'stt')
                result = await self.polza.transcribe_voice(claim.event.event_id,audio,
                    **hooks,checkpointed_responses=replay)
                segments = result.get('segments')
                if not isinstance(segments,list) or not segments or any(not isinstance(s,dict) or not isinstance(s.get('text'),str) for s in segments):
                    raise VoiceError('voice_transcript_invalid')
                text = ' '.join(s['text'] for s in segments)
                if not text.strip() or len(text)>16000 or '\x00' in text:
                    raise VoiceError('voice_transcript_invalid')
                self.voices.checkpoint_transcript(claim,text,receipts=(result['usage'],) if result.get('usage') else ())
            self._authorize(claim,cancelled)
            binding = self.bot.read_context(claim.event) if claim.event.text else None
            context = self.voices.read_context(claim)
            if context is None:
                context = self.voices.checkpoint_context(claim,self._context(claim.event,binding))
            responses = self.voices.read_responses(claim)
            if not self._replay(responses,'intent'):
                self._progress(claim,'intent')
            raw = await self.polza.parse_task_intent(claim.event.event_id,text,context,
                **hooks,checkpointed_responses=self._replay(responses,'intent'))
            self._authorize(claim,cancelled)
            proposals = self._proposals(claim.event,text,raw,context,binding)
            proposals = self.voices.store_proposals(claim,proposals)
            return self._finish(claim,proposals)
        except asyncio.CancelledError as error:
            self._failure(claim,error,cancelled=True)
            raise
        except Exception as error:
            return self._failure(claim,error)

    def confirm_intent(self, actor, proposal_id, expected_revision, operation_id):
        return self.voices.confirm_intent(actor,proposal_id,expected_revision=expected_revision,operation_id=operation_id)
