"""T8 application contracts: synthetic commands, no cloud or capture."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import importlib
import importlib.util
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock

import pytest

from secretary.domain.bot import BotAction, BotEvent
from secretary.domain.team import TaskCommand, TeamMember, TaskSnapshot
from secretary.domain.voice_commands import VoiceClaim, VoiceError
from test_max_media import media_case

UTC = timezone.utc


class VoiceMemory:
    def __init__(self, text):
        self.text, self.responses, self.proposals, self.notices = text, [], (), []
        self.requests = []
        self.state = None
        self.authorizations = 0
        self.context = None

    def authorize_job(self, claim):
        self.authorizations += 1
        return claim.event

    def transcript(self, claim):
        return self.text

    def read_responses(self, claim):
        return tuple(self.responses)

    def read_context(self, claim):
        return self.context

    def checkpoint_context(self, claim, context):
        if self.context is None:
            self.context = context
        return self.context

    def get_job(self, job_id):
        return {'pending_request': False, 'proposals': self.proposals}

    def pending_request(self, claim):
        return False

    def begin_request(self, claim, info):
        self.requests.append(info)

    def record_response(self, claim, info):
        self.responses.append(info)

    def checkpoint_transcript(self, claim, text, receipts=()):
        self.text = text

    def store_proposals(self, claim, proposals):
        self.proposals = proposals
        return proposals

    def enqueue_notice(self, event, key, reply):
        self.notices.append((key, reply))
        return str(uuid4())

    def complete_job(self, claim):
        self.state = 'complete'

    def fail_job(self, claim, *, state, error_code):
        self.state, self.error_code = state, error_code


def raw(**values):
    result = dict(action='create', project_id='7', task_id=None, member_id=None,
        person_mention=None, title='Подготовить отчёт', text=None, bucket=None,
        important=None, urgent=None, due_phrase=None, proposed_due_at=None,
        evidence_quote='Подготовить отчёт', utterance_kind='command', unresolved_fields=[])
    result.update(values)
    return {'proposals': [result]}


@pytest.fixture
def case():
    assert importlib.util.find_spec('secretary.application.voice_commands') is not None, 'T8 application implementation absent'
    module = importlib.import_module('secretary.application.voice_commands')
    owner = TeamMember(id=str(uuid4()), display_name='Дмитрий', role='owner',
        max_user_id='11', vikunja_user_id='11', project_ids=('7',), revision=2)
    person = TeamMember(id=str(uuid4()), display_name='Алексей', role='member',
        max_user_id='12', vikunja_user_id='12', project_ids=('7',), revision=3)
    task = TaskSnapshot(task_id='50', project_id='7', revision=4,
        remote_fingerprint='a'*64, title='Отчёт', assignee_id=person.id,
        important=False, urgent=False, classification_confirmed=True)
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    event_time = datetime(2026, 10, 8, 22, 30, tzinfo=UTC)  # already Friday in Moscow
    event = BotEvent(event_id=str(uuid4()), dedup_key='message:synthetic', bot_id='42',
        kind='message_created', timestamp_ms=int(event_time.timestamp()*1000),
        actor_id=owner.id, actor_revision=owner.revision, project_ids=owner.project_ids,
        user_id=owner.max_user_id, chat_id=owner.max_user_id, message_id='synthetic')
    team = Mock()
    team.get_member.side_effect = lambda identifier: {owner.id: owner, person.id: person}.get(identifier)
    team.list_members.return_value = (owner, person)
    team.list_projections.return_value = (task,)
    bot = Mock()
    bot.store_button.return_value = 'b_synthetic'
    bot.read_context.return_value = None
    voices = VoiceMemory('Алексей, Подготовить отчёт')
    media = Mock()
    polza = SimpleNamespace(parse_calls=[], stt_calls=[])

    async def parse(command_id, text, allowed_context, **hooks):
        polza.parse_calls.append((command_id, text, allowed_context, hooks))
        return polza.reply

    polza.parse_task_intent = parse
    polza.reply = raw(member_id=person.id, person_mention='Алексей')
    service = module.VoiceCommandService(team, bot, voices, media, polza, clock=lambda: now)
    claim = VoiceClaim(str(uuid4()), event, 'worker', 1, now+timedelta(seconds=60))
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'Не ставь задачу Алексею: Подготовить отчёт',
    'Мы обсуждали, что Алексей мог бы Подготовить отчёт',
    'Алексей говорил, что нужно Подготовить отчёт',
    'Нужно ли Алексею Подготовить отчёт?',
])
async def test_negation_and_reported_speech_do_not_create_task(case, text):
    c = case
    c.voices.text = text
    await c.service.execute(c.claim)
    assert not c.voices.proposals
    c.bot.store_button.assert_not_called()
    assert c.voices.state == 'complete'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['person', 'date', 'time'])
async def test_ambiguous_person_or_date_requires_review(case, kind):
    c = case
    if kind == 'person':
        c.polza.reply = raw(member_id=None, person_mention='Саша')
    else:
        c.voices.text += ', ' + ('когда-нибудь в пятницу' if kind == 'date' else 'в пятницу')
        c.polza.reply = raw(member_id=c.person.id, person_mention='Алексей',
            due_phrase='когда-нибудь в пятницу' if kind == 'date' else 'в пятницу',
            proposed_due_at='2026-10-09T15:00:00Z')
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert p.command is None
    assert set(p.unresolved_fields) & {'member', 'due', 'due_time'}
    c.bot.store_button.assert_not_called()
    assert 'Уточните' in c.voices.notices[-1][1].text


@pytest.mark.asyncio
async def test_friday_preview_uses_event_timezone_not_server_date(case):
    c = case
    c.voices.text += ', в пятницу в 14:00'
    c.polza.reply = raw(member_id=c.person.id, person_mention='Алексей',
        due_phrase='в пятницу в 14:00', proposed_due_at='2026-10-09T11:00:00Z')
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert p.proposed_due_at == datetime(2026, 10, 9, 11, tzinfo=UTC)
    assert p.command is not None
    assert p.command.values.due_at == p.proposed_due_at
    context = c.polza.parse_calls[0][2]
    assert context['event_datetime'].startswith('2026-10-09T01:30:00')
    assert context['timezone'] == 'Europe/Moscow'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('project_id','999'),('task_id','999'),('member_id',str(uuid4()))])
async def test_prompt_injection_cannot_select_foreign_project(case, field, value):
    c = case
    c.polza.reply = raw(**{field:value})
    await c.service.execute(c.claim)
    assert c.voices.state == 'rejected'
    assert c.voices.error_code == 'voice_scope_invalid'
    assert c.voices.proposals == ()
    assert len(c.polza.parse_calls) == 1
    c.bot.store_button.assert_not_called()


@pytest.mark.asyncio
async def test_literal_evidence_and_person_mapping_fail_closed(case):
    c = case
    c.polza.reply = raw(member_id=c.person.id, person_mention='Дмитрий', evidence_quote='Выдуманная цитата')
    await c.service.execute(c.claim)
    assert c.voices.state == 'rejected'
    assert not c.voices.proposals


@pytest.mark.asyncio
async def test_stt_checkpoint_precedes_intent_and_resume_uses_raw_response(case):
    c = case
    c.voices.text = None
    c.voices.responses = [dict(stage='stt', attempt=0, status=200, provider_response={'text':'Алексей, Подготовить отчёт'})]
    audio = SimpleNamespace(path='synthetic', command_id=c.event.event_id)
    c.media.download_voice.return_value = audio

    async def stt(command_id, got_audio, **hooks):
        c.polza.stt_calls.append(hooks)
        assert hooks['checkpointed_responses'] == {0:{'text':'Алексей, Подготовить отчёт'}}
        return {'segments':[{'text':'Алексей, Подготовить отчёт'}], 'usage':{'confirmed_rub':1}}

    old = c.polza.parse_task_intent
    async def parse(command_id, text, context, **hooks):
        assert c.voices.text == text
        return await old(command_id, text, context, **hooks)
    c.polza.transcribe_voice = stt
    c.polza.parse_task_intent = parse
    await c.service.execute(c.claim)
    assert c.voices.text == 'Алексей, Подготовить отчёт'
    assert c.voices.state == 'complete'
    c.media.download_voice.assert_not_called()  # response replay does not need source bytes


@pytest.mark.asyncio
async def test_fresh_actor_authorized_before_each_paid_submission(case):
    c = case
    async def parse(command_id, text, context, **hooks):
        info = dict(operation_id=str(uuid4()), category='voice_intent', command_id=command_id,
                    stage='intent', attempt=0, request_hash='a'*64)
        hooks['before_submission'](info)
        assert c.voices.requests[-1] == info
        return c.polza.reply
    c.polza.parse_task_intent = parse
    await c.service.execute(c.claim)
    assert c.voices.authorizations >= 2


@pytest.mark.asyncio
async def test_immutable_ids_and_confirmation_do_not_repeat_ai(case):
    c = case
    await c.service.execute(c.claim)
    first = c.voices.proposals
    await c.service.execute(c.claim)
    assert first == c.voices.proposals
    p, = first
    assert p.expires_at == c.now + timedelta(hours=24)
    c.voices.confirm_intent = Mock(return_value='receipt')
    assert c.service.confirm_intent(c.owner.id, p.id, 0, p.operation_id) == 'receipt'
    assert len(c.polza.parse_calls) == 1
    assert c.bot.store_button.call_args.kwargs['action'] == f'confirm_intent:{p.id}:{p.revision}'
    assert c.bot.store_button.call_args.kwargs['expires_seconds'] == 86400


@pytest.mark.asyncio
@pytest.mark.parametrize('important,confirmed,expected', [(False,True,'done'),(True,True,'review'),(None,False,'review')])
async def test_done_preserves_canonical_review_policy(case, important, confirmed, expected):
    c = case
    c.task = c.task.model_copy(update={'important':important,'classification_confirmed':confirmed})
    c.team.list_projections.return_value = (c.task,)
    c.voices.text = 'Задачу 50 готово, отчёт отправлен'
    c.polza.reply = raw(action='set_state',task_id='50',title=None,bucket='done',
        text='отчёт отправлен',evidence_quote=c.voices.text)
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert p.command.values.bucket == expected
    assert p.command.values.result == 'отчёт отправлен'


@pytest.mark.asyncio
async def test_cancelled_before_processing_never_calls_media_or_provider(case):
    c = case
    await c.service.execute(c.claim, cancelled=lambda: True)
    assert c.voices.error_code == 'voice_cancelled'
    assert not c.polza.parse_calls
    c.media.download_voice.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'Это цитата для протокола: «Алексей, Подготовить отчёт».',
    'Если решим обновить сервер, тогда Алексей должен Подготовить отчёт.',
    'Алексей, Подготовить отчёт. Нет, отменяю, ничего не меняй.',
])
async def test_hypothetical_quoted_and_retracted_commands_are_not_actionable(case, text):
    c = case
    c.voices.text = text
    await c.service.execute(c.claim)
    assert not c.voices.proposals
    c.bot.store_button.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('phrase,proposed,unresolved', [
    ('9 октября 2026 года, 14:00 по Москве', '2026-10-09T11:00:00Z', False),
    ('пятницу, 5 октября 2026 года, 14:00 по Москве', '2026-10-05T11:00:00Z', True),
    ('2026-10-09T14:00+03:00', '2026-10-09T11:00:00Z', False),
])
async def test_explicit_russian_dates_and_contradictory_weekday(case, phrase, proposed, unresolved):
    c = case
    c.voices.text += ', '+phrase
    c.polza.reply = raw(member_id=c.person.id, person_mention='Алексей',
                       due_phrase=phrase, proposed_due_at=proposed)
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert (p.command is None) is unresolved
    if unresolved:
        assert 'due' in p.unresolved_fields


@pytest.mark.asyncio
async def test_bound_due_context_resolves_date_without_model_task_id(case):
    c = case
    c.voices.text = 'завтра в 10:00, нужен дополнительный день для проверки'
    c.event = replace(c.event,text=c.voices.text)
    c.claim = replace(c.claim,event=c.event)
    link = TaskCommand(operation_id=str(uuid4()),project_id='7',task_id='50',
        expected_revision=4,expected_fingerprint='a'*64,action='link',values={})
    context = BotAction('due','50',link,link.operation_id)
    c.bot.read_context.return_value = context
    c.team.get_projection.return_value = c.task
    c.voices.enqueue = Mock(return_value=c.claim.job_id)
    c.service.enqueue(c.event,text=c.voices.text,context=context)
    c.polza.reply = raw(action='propose_due',project_id=None,title=None,
        due_phrase='завтра в 10:00',proposed_due_at='2026-10-10T07:00:00Z',
        evidence_quote=c.voices.text)
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert p.command.action == 'propose_due'
    assert p.command.task_id == '50' and p.command.expected_revision == 4
    assert c.polza.parse_calls[0][2]['tasks'] == [{
        'id':'50','project_id':'7','revision':4,'remote_fingerprint':'a'*64,
        'title':'Отчёт','assignee_id':c.person.id,'bucket':'inbox',
        'important':False,'classification_confirmed':True}]


@pytest.mark.asyncio
async def test_intent_replay_keeps_original_context_revisions(case):
    c = case
    c.polza.reply = raw(action='set_state',task_id='50',title=None,bucket='doing',
                       evidence_quote=c.voices.text)
    c.voices.context = c.service._context(c.event)
    c.team.list_projections.return_value = (c.task.model_copy(update={'revision':99,'remote_fingerprint':'b'*64}),)
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert p.command.expected_revision == 4 and p.command.expected_fingerprint == 'a'*64


@pytest.mark.asyncio
async def test_preview_contains_classification_values_and_is_bounded(case):
    c = case
    c.voices.text = 'Задача 50 важная, но не срочная'
    c.polza.reply = raw(action='classify',task_id='50',title=None,important=True,urgent=False,
                       evidence_quote=c.voices.text)
    reply = await c.service.execute(c.claim)
    assert 'Важность: важная' in reply.text and 'Срочность: не срочная' in reply.text
    assert len(reply.text) <= 4000


@pytest.mark.asyncio
async def test_progress_stages_are_stable_and_skip_cached_provider_work(case):
    c = case
    await c.service.execute(c.claim)
    assert any(reply.text == 'Разбираю поручение.' for _,reply in c.voices.notices)
    count = len(c.voices.notices)
    await c.service.execute(c.claim)
    assert all(reply.text != 'Разбираю поручение.' for _,reply in c.voices.notices[count:])


@pytest.mark.asyncio
async def test_preview_replay_uses_original_member_display_names(case):
    c = case
    await c.service.execute(c.claim)
    original = c.voices.notices[-1][1]
    renamed = c.person.model_copy(update={'display_name':'Новое имя','revision':4})
    c.team.get_member.side_effect = lambda identifier: {c.owner.id:c.owner,c.person.id:renamed}.get(identifier)
    replay = await c.service.execute(c.claim)
    assert replay == original


@pytest.mark.asyncio
async def test_revoked_actor_persists_failure_even_when_authorized_status_read_fails(case):
    c = case
    c.team.get_member.return_value = None
    c.team.get_member.side_effect = None
    c.voices.get_job = Mock(side_effect=VoiceError('voice_actor_changed'))
    await c.service.execute(c.claim)
    assert c.voices.state == 'rejected' and c.voices.error_code == 'voice_actor_changed'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,state',[(201,'uncertain'),(202,'uncertain'),(500,'uncertain'),(400,'rejected'),(429,'rejected')])
async def test_cached_non_200_never_becomes_success_or_new_paid_request(case,status,state):
    c = case
    c.voices.context = c.service._context(c.event)
    c.voices.responses = [dict(stage='intent',attempt=0,status=status,
        provider_response={'choices':[{'finish_reason':'stop','message':{'content':'{"proposals":[]}'}}]})]
    await c.service.execute(c.claim)
    assert c.voices.state == state
    assert not c.polza.parse_calls and not c.voices.requests


@pytest.mark.asyncio
async def test_missing_task_and_project_evidence_requires_review(case):
    c = case
    c.voices.text = 'Переведи в работу'
    c.polza.reply = raw(action='set_state',task_id='50',title=None,bucket='doing',evidence_quote=c.voices.text)
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert p.command is None and 'task' in p.unresolved_fields


@pytest.mark.asyncio
async def test_create_title_cannot_be_fabricated(case):
    c = case
    c.polza.reply = raw(member_id=c.person.id,person_mention='Алексей',title='Удалить секретные файлы')
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert p.command is None and 'evidence' in p.unresolved_fields


@pytest.mark.asyncio
async def test_long_five_previews_split_full_values_before_confirmation(case):
    c = case
    name,title,description = 'А'*2300,'Б'*3000,'В'*2500
    person = c.person.model_copy(update={'display_name':name})
    c.team.list_members.return_value = (c.owner,person)
    c.voices.text = f'{name}, создай задачу {title}. Описание: {description}'
    item = raw(member_id=person.id,person_mention=name,title=title,text=description,evidence_quote=title)['proposals'][0]
    c.polza.reply = {'proposals':[item]*5}
    reply = await c.service.execute(c.claim)
    assert c.voices.state == 'complete' and len(reply.buttons)==5
    assert all(len(notice.text)<=4000 for _,notice in c.voices.notices)
    details = ''.join(notice.text for _,notice in c.voices.notices[:-1])
    assert title in details and description in details and name in details
    assert all(not notice.buttons for _,notice in c.voices.notices[:-1])


@pytest.mark.asyncio
@pytest.mark.parametrize('mention',['мне','меня','я'])
async def test_explicit_self_pronoun_maps_only_bound_sender(case,mention):
    c = case
    c.voices.text = f'{mention}, Подготовить отчёт'
    c.polza.reply = raw(member_id=c.owner.id,person_mention=mention)
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert p.command.values.assignee_id == c.owner.id
    assert p.command.expected_assignee_revision == c.owner.revision


@pytest.mark.asyncio
async def test_missing_person_mention_does_not_default_to_sender(case):
    c = case
    c.polza.reply = raw(member_id=c.owner.id,person_mention=None)
    await c.service.execute(c.claim)
    p, = c.voices.proposals
    assert p.command is None and 'member' in p.unresolved_fields


@pytest.mark.asyncio
async def test_self_pronoun_cannot_select_other_actor(case):
    c = case
    c.voices.text = 'Поставь мне задачу Подготовить отчёт'
    c.polza.reply = raw(member_id=c.person.id,person_mention='мне')
    await c.service.execute(c.claim)
    assert c.voices.state == 'rejected' and c.voices.error_code == 'voice_member_evidence_invalid'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,expected_state,word',[
    ('configuration','paused_config','MAX'),('duration','rejected','120'),
    ('codec','rejected','аудиофайл'),('process','rejected','аудиофайл')])
async def test_real_media_failures_have_actionable_message_without_paid_work(case,media_case,kind,expected_state,word):
    c,m = case,media_case
    c.voices.text = None
    c.service.media = m.adapter
    c.event = replace(c.event,media=m.event.media)
    c.claim = replace(c.claim,event=c.event)
    if kind=='configuration':
        m.adapter.allowlisted_hosts = ()
    elif kind=='duration':
        m.probe[0]['format']['duration'] = '120.001'
    elif kind=='codec':
        m.probe[0]['streams'][0]['codec_name'] = 'not_supported'
    else:
        m.adapter.process_runner = lambda *args,**kwargs: m.a.MediaProcessResult(1,b'')
    reply = await c.service.execute(c.claim)
    assert c.voices.state == expected_state
    assert word in reply.text and 'SYNTHETIC_SIGNED_SECRET' not in reply.text
    assert not c.polza.parse_calls and not c.voices.requests
    if kind=='configuration':
        assert not m.calls and not m.processes


@pytest.mark.asyncio
@pytest.mark.parametrize('code,state,word',[
    ('authentication','paused_config','настройки Polza'),
    ('access_denied','paused_config','настройки Polza'),
    ('insufficient_funds','paused_budget','недостаточно средств'),
    ('unsupported_response_format','paused_config','настройки Polza'),
    ('invalid_request','paused_config','настройки Polza')])
async def test_provider_rejections_distinguish_configuration_balance_and_monthly_cap(case,code,state,word):
    c = case
    async def reject(*args,**kwargs):
        raise VoiceError(code)
    c.polza.parse_task_intent = reject
    reply = await c.service.execute(c.claim)
    assert c.voices.state == state and word in reply.text
    assert not c.voices.requests
    if code=='insufficient_funds':
        assert 'общий бюджет Polza исчерпан' not in reply.text
