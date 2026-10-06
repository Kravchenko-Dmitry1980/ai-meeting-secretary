"""T8 paid boundaries: isolated real repositories and synthetic HTTP only."""
from datetime import datetime, timedelta, timezone
from importlib import import_module, util
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4
import wave

import httpx
import pytest

from secretary.application.bot_commands import BotCommandService
from secretary.application.monthly_budget import MonthlyBudget
from secretary.domain.bot import BotEvent, CommandPreview
from secretary.domain.cloud_budget import AccountUsage, CloudCharge
from secretary.domain.team import TaskSnapshot, TeamMember
from secretary.infrastructure.bot_repository import BotRepository
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.infrastructure.polza import PolzaClient
from secretary.infrastructure.team_auth_repository import AuthRepository
from secretary.infrastructure.team_database import SCHEMA_VERSION, TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository
from secretary.settings import Settings


KEY = 'synthetic-voice-budget-not-real'
KEY_TAG = hashlib.sha256(KEY.encode()).hexdigest()
CAP = 3000_000000
LITERAL = 'Не создавай задачу, это проверка.'


def _voice_type(module, symbol):
    assert util.find_spec(module) is not None, f'T8 implementation absent: {module}'
    return getattr(import_module(module), symbol)


class _Prices:
    async def quote(self, model):
        assert model in {'openai/whisper-large-v3-turbo', 'openai/gpt-4.1-mini'}
        return {'prompt_per_million': 1, 'completion_per_million': 1, 'stt_per_minute': .1}


class _NoMedia:
    async def download_voice(self, event, *, cancelled=None):
        pytest.fail('A literal text command must not download or inspect audio')


def _case(tmp_path, monkeypatch, handler, *, usage=0):
    Repository = _voice_type('secretary.infrastructure.voice_repository', 'VoiceRepository')
    # Never load .env, inherit a credential, or construct an owner account reader.
    for field_name, field in Settings.model_fields.items():
        monkeypatch.delenv(field_name, raising=False)
        alias = field.validation_alias
        if isinstance(alias, str):
            monkeypatch.delenv(alias, raising=False)
        else:
            for choice in getattr(alias, 'choices', ()):
                if isinstance(choice, str):
                    monkeypatch.delenv(choice, raising=False)
    clock = [datetime(2026, 10, 4, 10, tzinfo=timezone.utc)]
    db = TeamDatabase(tmp_path / 'team.sqlite3')
    with db.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == SCHEMA_VERSION
    team = TeamRepository(db, clock=lambda: clock[0])
    actor = TeamMember(id=str(uuid4()), display_name='Synthetic budget actor', role='owner',
        max_user_id='9007199254740993', vikunja_user_id='11', project_ids=('7',))
    team.upsert_member(actor, expected_revision=None)
    task = TaskSnapshot(task_id='9007199254740995', project_id='7', revision=0,
        remote_fingerprint='a' * 64, title='Synthetic budget task', assignee_id=actor.id,
        important=False, urgent=False, classification_confirmed=True)
    team.save_projection(task, expected_revision=None)
    auth = AuthRepository(db, secret=b'SYNTHETIC_VOICE_BUDGET_AUTH_SECRET_32', clock=lambda: clock[0])
    bot = BotRepository(db, team, auth, clock=lambda: clock[0])
    voices = Repository(db, team, bot, clock=lambda: clock[0])
    budget_path = tmp_path / 'billing.sqlite3'
    budget = MonthlyBudget(BudgetRepository(budget_path), clock=lambda: clock[0])
    budget.refresh_account(AccountUsage(key_tag=KEY_TAG, limit_micro=CAP,
        remaining_micro=CAP - usage, usage_micro=usage, reset='monthly', observed_at=clock[0]))
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / 'data',
        polza_api_key=KEY, cloud_enabled=True, local_cost_limits_enabled=False,
        request_timeout_seconds=10)
    client = PolzaClient(settings, httpx.MockTransport(handler), budget=budget,
        charge_context={'key_tag': KEY_TAG}, price_reader=_Prices())
    return SimpleNamespace(Repository=Repository, clock=clock, db=db, team=team, actor=actor,
        task=task, bot=bot, auth=auth, voices=voices, budget=budget, budget_path=budget_path,
        client=client, settings=settings, path=tmp_path)


def _event(c, *, text=None, callback_payload=None):
    identifier = str(uuid4())
    event = BotEvent(event_id=identifier, dedup_key=identifier, bot_id='42',
        kind='message_callback' if callback_payload else 'message_created',
        timestamp_ms=int(c.clock[0].timestamp() * 1000), user_id=c.actor.max_user_id,
        chat_id=c.actor.max_user_id, message_id=identifier, text=text,
        callback_id=identifier if callback_payload else None, callback_payload=callback_payload,
        media=() if text is not None or callback_payload else
            ({'type': 'voice', 'payload': {'url': 'https://synthetic.invalid/audio'}},))
    accepted = c.bot.intake(event)
    return c.bot.get_event(accepted.event_id)


def _job(c, *, text=LITERAL):
    event = _event(c, text=text)
    identifier = c.voices.enqueue(event, text=text)
    claim = c.voices.claim_job('synthetic-budget-worker', lease_seconds=10)
    assert claim is not None and claim.job_id == identifier
    return event, identifier, claim


def _count(c, table):
    assert table in {'voice_requests', 'voice_responses', 'voice_proposals', 'team_commands'}
    with c.db.connection() as conn:
        return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]


def _billing_rows(c):
    with c.budget.repository.transaction() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM billing_charges')]


def _context(c, event):
    date = datetime.fromtimestamp(event.timestamp_ms / 1000, timezone.utc)
    return {'members': ({'id': c.actor.id, 'display_name': c.actor.display_name,
        'project_ids': c.actor.project_ids, 'revision': c.actor.revision},),
        'project_ids': c.actor.project_ids, 'tasks': (),
        'event_datetime': date.astimezone(timezone(timedelta(hours=3))).isoformat(),
        'event_timestamp_ms': event.timestamp_ms, 'timezone': 'Europe/Moscow',
        'actor_id': c.actor.id, 'actor_revision': c.actor.revision, 'default_project_id': '7'}


def _restart(c):
    c.clock[0] += timedelta(seconds=11)
    db = TeamDatabase(c.db.path)
    voices = c.Repository(db, c.team, c.bot, clock=lambda: c.clock[0])
    voices.recover_expired()
    return voices


@pytest.mark.asyncio
async def test_exhausted_budget_keeps_text_buttons_working(tmp_path, monkeypatch):
    Service = _voice_type('secretary.application.voice_commands', 'VoiceCommandService')
    calls = []
    c = _case(tmp_path, monkeypatch, lambda request: calls.append(request) or
        pytest.fail('Exhausted monthly budget must prevent every paid HTTP request'), usage=CAP)
    _, identifier, claim = _job(c)
    service = Service(c.team, c.bot, c.voices, _NoMedia(), c.client, clock=lambda: c.clock[0])
    await service.execute(claim)
    assert c.voices.get_job(identifier)['state'] == 'paused_budget'
    assert c.voices.get_job(identifier)['error_code'] == 'monthly_budget_exhausted'
    assert calls == [] and _count(c, 'voice_requests') == 0 and _billing_rows(c) == []

    deterministic = BotCommandService(c.team, c.bot, public_origin='https://synthetic.invalid',
        clock=lambda: c.clock[0], voice=service)
    listing = deterministic.handle(_event(c, text='Мои задачи'))
    assert c.task.title in listing.text
    doing = next(button for row in listing.buttons for button in row if button.text == 'В работе')
    preview = deterministic.handle(_event(c, callback_payload=doing.payload))
    assert isinstance(preview, CommandPreview)
    assert preview.command.values.bucket == 'doing'
    assert _count(c, 'team_commands') == 0  # A button still requires separate confirmation.
    confirm = next(button for row in preview.buttons for button in row if button.text == 'Подтвердить')
    accepted = deterministic.handle(_event(c, callback_payload=confirm.payload))
    assert accepted.receipt.execution_state.state == 'queued'
    assert _count(c, 'team_commands') == 1
    assert calls == [] and c.budget.snapshot().remaining_micro == 0


@pytest.mark.asyncio
async def test_paid_timeout_remains_uncertain(tmp_path, monkeypatch):
    Service = _voice_type('secretary.application.voice_commands', 'VoiceCommandService')
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('PRIVATE_SYNTHETIC_TIMEOUT', request=request)
    c = _case(tmp_path, monkeypatch, timeout)
    _, identifier, claim = _job(c)
    service = Service(c.team, c.bot, c.voices, _NoMedia(), c.client, clock=lambda: c.clock[0])
    await service.execute(claim)
    state = c.voices.get_job(identifier)
    assert state['state'] == 'uncertain' and state['error_code'] == 'request_uncertain'
    assert 'PRIVATE_SYNTHETIC_TIMEOUT' not in json.dumps(state, default=str)
    charges = _billing_rows(c)
    assert len(calls) == len(charges) == _count(c, 'voice_requests') == 1
    assert charges[0]['status'] == 'uncertain' and charges[0]['category'] == 'voice_intent'
    assert charges[0]['command_id'] == claim.event.event_id
    assert charges[0]['reserved_micro'] > 0 and c.budget.snapshot().reserved_micro > 0
    assert _count(c, 'voice_responses') == _count(c, 'team_commands') == 0
    restarted = _restart(c)
    assert restarted.get_job(identifier)['state'] == 'uncertain'
    assert restarted.claim_job('replacement') is None
    reopened_budget = MonthlyBudget(BudgetRepository(c.budget_path), clock=lambda: c.clock[0])
    assert reopened_budget.reservation(charges[0]['operation_id']).status == 'uncertain'
    assert reopened_budget.snapshot().reserved_micro == charges[0]['reserved_micro']
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_paid_intent_checkpoint_replay_does_not_charge_again(tmp_path, monkeypatch):
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'gen_synthetic_voice_intent',
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
                'content': json.dumps({'proposals': []})}}], 'usage': {'cost_rub': .02}})
    c = _case(tmp_path, monkeypatch, response)
    event, identifier, claim = _job(c)
    context = _context(c, event)
    c.voices.checkpoint_context(claim, context)
    parsed = await c.client.parse_task_intent(event.event_id, LITERAL, context,
        before_submission=lambda info: c.voices.begin_request(claim, info),
        response_checkpoint=lambda info: c.voices.record_response(claim, info))
    assert parsed == {'proposals': []}
    assert len(calls) == _count(c, 'voice_responses') == 1
    assert c.budget.snapshot().confirmed_micro == 20_000

    # Simulate a crash after paid response persistence but before publishing proposals.
    restarted = _restart(c)
    resumed = restarted.claim_job('replacement')
    assert resumed is not None and resumed.job_id == identifier and resumed.fence > claim.fence
    assert restarted.read_context(resumed) == json.loads(json.dumps(context))
    cached = {item['attempt']: item['provider_response'] for item in restarted.read_responses(resumed)}
    no_http = PolzaClient(c.settings, httpx.MockTransport(lambda _: pytest.fail('Cached response must not POST')),
        budget=c.budget, charge_context={'key_tag': KEY_TAG}, price_reader=_Prices())
    replay = await no_http.parse_task_intent(event.event_id, LITERAL, _context(c, event),
        before_submission=lambda _: pytest.fail('Cached response must not reserve a second request'),
        response_checkpoint=lambda _: pytest.fail('Cached response is already durable'),
        checkpointed_responses=cached)
    assert replay == parsed and len(calls) == 1
    charges = _billing_rows(c)
    assert len(charges) == 1 and charges[0]['confirmed_micro'] == 20_000
    assert charges[0]['status'] == 'confirmed' and c.budget.snapshot().reserved_micro == 0


@pytest.mark.asyncio
async def test_paid_stt_checkpoint_replay_needs_no_media_or_second_charge(tmp_path, monkeypatch):
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'gen_synthetic_voice_stt', 'text': LITERAL,
            'duration': 1, 'usage': {'cost_rub': .02}})
    c = _case(tmp_path, monkeypatch, response)
    event, identifier, claim = _job(c, text=None)
    path = tmp_path / 'synthetic.wav'
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, 16000, 0, 'NONE', 'none'))
        writer.writeframes(b'\0\0' * 16000)
    audio = SimpleNamespace(path=path, duration_ms=1000, command_id=event.event_id)
    parsed = await c.client.transcribe_voice(event.event_id, audio,
        before_submission=lambda info: c.voices.begin_request(claim, info),
        response_checkpoint=lambda info: c.voices.record_response(claim, info))
    assert parsed['segments'][0]['text'] == LITERAL and len(calls) == 1
    path.unlink()  # Restart must consume the paid checkpoint, not fetch/inspect transient media.
    restarted = _restart(c)
    resumed = restarted.claim_job('replacement')
    assert resumed is not None and resumed.job_id == identifier
    cached = {item['attempt']: item['provider_response'] for item in restarted.read_responses(resumed)}
    no_http = PolzaClient(c.settings, httpx.MockTransport(lambda _: pytest.fail('Cached STT must not POST')),
        budget=c.budget, charge_context={'key_tag': KEY_TAG}, price_reader=_Prices())
    replay = await no_http.transcribe_voice(event.event_id, None, checkpointed_responses=cached,
        before_submission=lambda _: pytest.fail('Cached STT must not reserve a second request'))
    assert replay['segments'] == parsed['segments'] and replay['duration_ms'] == parsed['duration_ms']
    assert len(_billing_rows(c)) == 1 and c.budget.snapshot().confirmed_micro == 20_000
    assert c.budget.snapshot().reserved_micro == 0 and _count(c, 'team_commands') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected_state', [
    (201, 'uncertain'), (202, 'uncertain'), (400, 'rejected'),
    (429, 'rejected'), (500, 'uncertain'),
])
async def test_cached_non_200_intent_is_not_parsed_or_resubmitted(
        tmp_path, monkeypatch, status, expected_state):
    Service = _voice_type('secretary.application.voice_commands', 'VoiceCommandService')
    c = _case(tmp_path, monkeypatch,
        lambda _: pytest.fail('A recorded non-200 response must never dispatch another HTTP request'))
    event, identifier, claim = _job(c)
    c.voices.checkpoint_context(claim, _context(c, event))
    operation = str(uuid4())
    request_hash = 'b' * 64
    info = {'operation_id': operation, 'category': 'voice_intent',
        'command_id': event.event_id, 'stage': 'intent', 'attempt': 0,
        'request_hash': request_hash}
    c.budget.reserve(CloudCharge(operation, 'voice_intent', request_hash, 20_000, 20_000,
        command_id=event.event_id), key_tag=KEY_TAG)
    c.voices.begin_request(claim, info)
    c.budget.mark_submitted(operation)
    provider_id = 'gen_synthetic_non_200_receipt'
    receipt = {'confirmed_rub': '0.02', 'provider_request_id': provider_id, 'kind': 'intent'}
    # A valid model-shaped body cannot override its actual HTTP status.
    provider_response = {'id': provider_id, 'choices': [{'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': json.dumps({'proposals': []})}}],
        'usage': {'cost_rub': .02}}
    saved_response = {key: info[key] for key in ('operation_id', 'stage', 'attempt', 'request_hash')}
    saved_response.update(status=status, provider_response=provider_response, usage_receipt=receipt)
    c.budget.settle(operation, receipt)
    c.voices.record_response(claim, saved_response)
    assert c.voices.get_job(identifier)['pending_request'] is False
    original_charges = _billing_rows(c)
    assert len(original_charges) == 1 and original_charges[0]['confirmed_micro'] == 20_000

    restarted = _restart(c)
    resumed = restarted.claim_job('replacement')
    assert resumed is not None and resumed.job_id == identifier
    assert restarted.read_responses(resumed) == (saved_response,)
    parse_calls = []
    real_parse = c.client.parse_task_intent
    async def tracked_parse(*args, **kwargs):
        parse_calls.append((args, kwargs))
        return await real_parse(*args, **kwargs)
    monkeypatch.setattr(c.client, 'parse_task_intent', tracked_parse)
    service = Service(c.team, c.bot, restarted, _NoMedia(), c.client, clock=lambda: c.clock[0])
    await service.execute(resumed)
    state = restarted.get_job(identifier)
    assert state['state'] == expected_state
    assert state['stage'] != 'complete' and state['pending_request'] is False
    assert parse_calls == []
    assert _count(c, 'voice_requests') == _count(c, 'voice_responses') == 1
    assert _count(c, 'voice_proposals') == _count(c, 'team_commands') == 0
    assert _billing_rows(c) == original_charges
    assert c.budget.snapshot().confirmed_micro == 20_000
    with c.db.connection() as conn:
        persisted = json.loads(conn.execute('SELECT payload FROM voice_responses WHERE operation_id=?',
            (operation,)).fetchone()[0])
    assert persisted == saved_response
