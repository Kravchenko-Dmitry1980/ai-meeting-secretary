"""T8 durable boundaries in isolated SQLite. No model, transport or owner data."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest


def uid():
    return str(uuid4())


@pytest.fixture
def c(tmp_path):
    api = {}
    for name in ('domain.bot', 'domain.team', 'domain.voice_commands', 'infrastructure.team_database',
                 'infrastructure.team_repository', 'infrastructure.team_auth_repository',
                 'infrastructure.bot_repository', 'infrastructure.voice_repository'):
        api.update(vars(import_module('secretary.' + name)))
    a = SimpleNamespace(**api)
    clock = [datetime(2026, 10, 4, 10, tzinfo=timezone.utc)]
    db = a.TeamDatabase(tmp_path / 'voice.sqlite3')
    team = a.TeamRepository(db, clock=lambda: clock[0])
    actor = a.TeamMember(id=uid(), display_name='Synthetic actor', role='owner', max_user_id='9007199254740993',
                         vikunja_user_id='11', project_ids=('7',))
    team.upsert_member(actor, expected_revision=None)
    snapshot = a.TaskSnapshot(task_id='9007199254740995', project_id='7', revision=0,
        remote_fingerprint='a' * 64, title='Synthetic task', assignee_id=actor.id,
        important=False, urgent=False, classification_confirmed=True)
    team.save_projection(snapshot, expected_revision=None)
    auth = a.AuthRepository(db, secret=b'SYNTHETIC_VOICE_REPOSITORY_SECRET_32', clock=lambda: clock[0])
    bot = a.BotRepository(db, team, auth, clock=lambda: clock[0])
    repo = a.VoiceRepository(db, team, bot, clock=lambda: clock[0])
    return SimpleNamespace(a=a, clock=clock, db=db, team=team, actor=actor, snapshot=snapshot, auth=auth, bot=bot, repo=repo)


def event(c, *, text=None, user_id=None):
    identifier = uid()
    incoming = c.a.BotEvent(event_id=identifier, dedup_key=identifier, bot_id='42', kind='message_created',
        timestamp_ms=int(c.clock[0].timestamp() * 1000), user_id=user_id or c.actor.max_user_id,
        chat_id=user_id or c.actor.max_user_id, message_id=identifier, text=text,
        media=() if text is not None else ({'type': 'voice', 'payload': {'url': 'https://synthetic.invalid/audio'}},))
    accepted = c.bot.intake(incoming)
    return c.bot.get_event(accepted.event_id)


def job(c, *, text=None):
    source = event(c, text=text)
    identifier = c.repo.enqueue(source, text=text)
    claim = c.repo.claim_job('worker', lease_seconds=10)
    if text is not None:
        c.repo.checkpoint_context(claim, {'actor_id': source.actor_id, 'project_ids': list(source.project_ids)})
    return source, identifier, claim


def request(c, claim, stage='stt', attempt=0, **changes):
    return {'operation_id': uid(), 'category': 'voice_' + stage, 'command_id': claim.event.event_id,
            'stage': stage, 'attempt': attempt, 'request_hash': ('a' if stage == 'stt' else 'b') * 64, **changes}


def response(info, **changes):
    return {key: info[key] for key in ('operation_id', 'stage', 'attempt', 'request_hash')} | {
        'status': 200, 'provider_response': {'text': 'Сделай задачу', 'id': 'synthetic-provider-id'},
        'usage_receipt': {'confirmed_rub': '0.25', 'provider_request_id': 'synthetic-provider-id', 'kind': 'transcription'}, **changes}


def transcript(c, claim, text='Сделай задачу'):
    info = request(c, claim)
    c.repo.begin_request(claim, info)
    answer = response(info)
    c.repo.record_response(claim, answer)
    c.repo.checkpoint_transcript(claim, text, receipts=(answer['usage_receipt'],))
    return info, answer


def proposal(c, claim, **changes):
    operation_id = uid()
    command = c.a.TaskCommand(operation_id=operation_id, project_id='7', task_id=c.snapshot.task_id,
        expected_revision=0, expected_fingerprint='a' * 64, action='set_state', values={'bucket': 'doing'})
    text = c.repo.transcript(claim)
    return c.a.IntentProposal(**{'id': uid(), 'command_id': claim.event.event_id, 'actor_id': c.actor.id,
        'actor_revision': c.actor.revision, 'project_ids': c.actor.project_ids, 'action': 'set_state',
        'project_id': '7', 'task_id': c.snapshot.task_id, 'expected_revision': 0, 'expected_fingerprint': 'a' * 64,
        'evidence_quote': text, 'expires_at': c.clock[0] + timedelta(minutes=15),
        'source_hash': hashlib.sha256(text.encode()).hexdigest(), 'operation_id': operation_id, 'command': command,
        **changes})


def count(c, table):
    assert table in {'voice_jobs', 'voice_requests', 'voice_responses', 'voice_proposals', 'team_commands', 'voice_notices'}
    with c.db.connection() as conn:
        return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]


def acknowledge_initial_reply(c):
    c.bot.finish_event(c.bot.claim_event('bot-worker'), c.a.DeterministicReply('Обрабатываю'))
    delivery = c.bot.claim_reply('bot-sender')
    c.bot.finish_reply(delivery, c.a.SendReceipt(delivery.id, 'sent', message_id='synthetic-initial'))


def test_enqueue_replay_is_same_immutable_job_and_changed_text_conflicts(c):
    source = event(c, text='Сделай задачу')
    identifier = c.repo.enqueue(source, text=source.text)
    assert c.repo.enqueue(source, text=source.text) == identifier
    with pytest.raises(c.a.VoiceError):
        c.repo.enqueue(source, text='Другой текст')
    assert count(c, 'voice_jobs') == 1


@pytest.mark.parametrize('change', ['unknown', 'revoked', 'changed_event'])
def test_unbound_or_revoked_event_cannot_enqueue_or_start_paid_work(c, change):
    source = event(c, text='Сделай задачу', user_id='555' if change == 'unknown' else None)
    if change == 'revoked':
        c.team.upsert_member(c.actor.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    elif change == 'changed_event':
        source = replace(source, actor_revision=9)
    with pytest.raises(c.a.VoiceError):
        c.repo.enqueue(source, text=source.text)
    assert count(c, 'voice_jobs') == count(c, 'voice_requests') == 0


def test_local_crash_before_paid_stage_requeues_with_new_fence(c):
    source, identifier, first = job(c)
    c.clock[0] += timedelta(seconds=10)
    c.repo.recover_expired()
    second = c.repo.claim_job('replacement')
    assert second.job_id == identifier and second.fence > first.fence
    assert c.repo.authorize_job(second) == source
    with pytest.raises(c.a.VoiceError):
        c.repo.checkpoint_transcript(first, 'stale')


@pytest.mark.parametrize('stage', ['stt', 'intent'])
def test_paid_request_without_response_becomes_uncertain_and_is_never_reclaimed(c, stage):
    _, identifier, claim = job(c, text='Сделай задачу' if stage == 'intent' else None)
    c.repo.begin_request(claim, request(c, claim, stage))
    c.clock[0] += timedelta(seconds=10)
    c.repo.recover_expired()
    assert c.repo.get_job(identifier)['state'] == 'uncertain'
    assert c.repo.claim_job('replacement') is None


def test_raw_stt_response_survives_crash_before_transcript_parser(c):
    _, identifier, claim = job(c)
    info = request(c, claim)
    c.repo.begin_request(claim, info)
    answer = response(info)
    c.repo.record_response(claim, answer)
    c.clock[0] += timedelta(seconds=10)
    restarted = c.a.VoiceRepository(c.a.TeamDatabase(c.db.path), c.team, c.bot, clock=lambda: c.clock[0])
    resumed = restarted.claim_job('replacement')
    assert resumed.job_id == identifier and restarted.transcript(resumed) is None
    assert restarted.read_responses(resumed) == (answer,)
    with pytest.raises(c.a.VoiceError):
        restarted.begin_request(resumed, request(c, resumed))
    restarted.checkpoint_transcript(resumed, 'Сделай задачу', receipts=(answer['usage_receipt'],))
    assert restarted.transcript(resumed) == 'Сделай задачу'


def test_transcript_checkpoint_is_literal_immutable_and_skips_stt_after_restart(c):
    _, _, claim = job(c)
    literal = '  Сделай задачу.\nНе меняй текст.  '
    transcript(c, claim, literal)
    c.repo.checkpoint_transcript(claim, literal, receipts=({'confirmed_rub': '0.25', 'provider_request_id': 'synthetic-provider-id', 'kind': 'transcription'},))
    with pytest.raises(c.a.VoiceError):
        c.repo.checkpoint_transcript(claim, literal.strip())
    c.clock[0] += timedelta(seconds=10)
    resumed = c.repo.claim_job('replacement')
    assert resumed.stage == 'intent' and c.repo.transcript(resumed) == literal
    with pytest.raises(c.a.VoiceError):
        c.repo.begin_request(resumed, request(c, resumed))


def test_known_malformed_intent_response_allows_one_repair_without_initial_post(c):
    _, _, claim = job(c, text='Сделай задачу')
    first = request(c, claim, 'intent')
    c.repo.begin_request(claim, first)
    answer = response(first, provider_response={'choices': [{'message': {'content': '{bad json'}}]},
                      usage_receipt={'confirmed_rub': '0.1', 'kind': 'intent', 'provider_request_id': 'intent-1'})
    c.repo.record_response(claim, answer)
    c.clock[0] += timedelta(seconds=10)
    resumed = c.repo.claim_job('replacement')
    assert c.repo.read_responses(resumed) == (answer,)
    with pytest.raises(c.a.VoiceError):
        c.repo.begin_request(resumed, request(c, resumed, 'intent'))
    repair = request(c, resumed, 'intent', 1)
    c.repo.begin_request(resumed, repair)
    c.repo.record_response(resumed, response(repair))
    with pytest.raises(c.a.VoiceError):
        c.repo.begin_request(resumed, request(c, resumed, 'intent', 2))
    assert count(c, 'voice_requests') == 2


@pytest.mark.parametrize('stage,attempt,changes', [
    ('stt', 1, {}), ('intent', 1, {}), ('intent', 0, {}),
    ('stt', 0, {'category': 'meeting_stt'}), ('stt', 0, {'command_id': uid()}),
    ('stt', 0, {'request_hash': 'not-a-digest'}), ('stt', True, {}),
])
def test_invalid_or_out_of_order_paid_start_has_no_write(c, stage, attempt, changes):
    _, _, claim = job(c)
    with pytest.raises(c.a.VoiceError):
        c.repo.begin_request(claim, request(c, claim, stage, attempt, **changes))
    assert count(c, 'voice_requests') == 0


def test_request_and_response_are_exact_bound_and_replays_do_not_overwrite(c):
    _, _, claim = job(c)
    info = request(c, claim)
    c.repo.begin_request(claim, info)
    answer = response(info)
    for edits in ({'operation_id': uid()}, {'request_hash': 'c' * 64}, {'attempt': 1}, {'stage': 'intent'}):
        with pytest.raises(c.a.VoiceError):
            c.repo.record_response(claim, answer | edits)
    assert count(c, 'voice_responses') == 0
    c.repo.record_response(claim, answer)
    c.repo.record_response(claim, answer)
    with pytest.raises(c.a.VoiceError):
        c.repo.record_response(claim, answer | {'provider_response': {'text': 'different'}})
    assert c.repo.read_responses(claim) == (answer,)


def test_two_workers_cannot_claim_same_user_jobs_but_other_user_progresses(c):
    first, _ = event(c, text='Первое'), event(c, text='Второе')
    second = event(c, text='Третье')
    for item in (first, second):
        c.repo.enqueue(item, text=item.text)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda worker: c.repo.claim_job(worker), ['a', 'b']))
    assert sum(value is not None for value in claims) == 1
    person = c.actor.model_copy(update={'id': uid(), 'max_user_id': '777', 'vikunja_user_id': '22'})
    c.team.upsert_member(person, expected_revision=None)
    foreign = event(c, text='Другой участник', user_id='777')
    c.repo.enqueue(foreign, text=foreign.text)
    assert c.repo.claim_job('other').event.actor_id == person.id


@pytest.mark.parametrize('state', ['paused_budget', 'paused_config', 'rejected'])
def test_definite_pre_submission_pause_is_terminal_but_cannot_hide_pending_paid_request(c, state):
    _, identifier, claim = job(c)
    c.repo.fail_job(claim, state, 'voice_configuration_required')
    assert c.repo.get_job(identifier)['state'] == state
    assert c.repo.claim_job('other') is None
    _, identifier2, claim2 = job(c)
    c.repo.begin_request(claim2, request(c, claim2))
    with pytest.raises(c.a.VoiceError):
        c.repo.fail_job(claim2, state, 'voice_configuration_required')
    c.repo.fail_job(claim2, 'uncertain', 'voice_request_uncertain')
    assert c.repo.get_job(identifier2)['state'] == 'uncertain'


def test_revocation_after_claim_blocks_checkpoint_and_paid_start(c):
    _, _, claim = job(c, text='Сделай задачу')
    c.team.upsert_member(c.actor.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    for action in (lambda: c.repo.authorize_job(claim), lambda: c.repo.renew_job(claim),
                   lambda: c.repo.begin_request(claim, request(c, claim, 'intent'))):
        with pytest.raises(c.a.VoiceError):
            action()
    assert count(c, 'voice_requests') == 0


def test_proposals_confirm_once_and_replay_reads_current_execution_after_expiry(c):
    _, _, claim = job(c, text='Сделай задачу')
    value = proposal(c, claim)
    assert c.repo.store_proposals(claim, (value,)) == (value,)
    assert c.repo.store_proposals(claim, (value,)) == (value,)
    first = c.repo.confirm_intent(c.actor.id, value.id, expected_revision=value.revision, operation_id=value.operation_id)
    paid = c.team.claim_command('task-worker')
    snapshot = c.snapshot.model_copy(update={'revision': 1, 'bucket': 'doing', 'remote_fingerprint': 'b' * 64})
    c.team.record_remote_result(paid, state='applied', snapshot=snapshot)
    c.clock[0] += timedelta(minutes=16)
    replay = c.repo.confirm_intent(c.actor.id, value.id, expected_revision=value.revision, operation_id=value.operation_id)
    assert replay.acceptance_receipt == first.acceptance_receipt and replay.execution_state.state == 'applied'
    assert count(c, 'team_commands') == 1 and count(c, 'voice_requests') == 0


@pytest.mark.parametrize('change', ['source', 'quote', 'actor', 'event', 'projects'])
def test_proposal_must_match_literal_source_and_current_event_scope(c, change):
    _, _, claim = job(c, text='Сделай задачу')
    value = proposal(c, claim)
    edits = {'source': {'source_hash': 'f' * 64}, 'quote': {'evidence_quote': 'Новая цитата'},
             'actor': {'actor_id': uid()}, 'event': {'command_id': uid()}, 'projects': {'project_ids': ('7', '8')}}[change]
    with pytest.raises(c.a.VoiceError):
        c.repo.store_proposals(claim, (value.model_copy(update=edits),))
    assert count(c, 'voice_proposals') == 0


@pytest.mark.parametrize('change', ['expiry', 'revision', 'operation', 'actor', 'unresolved', 'revoked'])
def test_invalid_proposal_confirmation_does_not_accept_any_command(c, change):
    _, _, claim = job(c, text='Сделай задачу')
    value = proposal(c, claim)
    if change == 'unresolved':
        value = value.model_copy(update={'command': None, 'unresolved_fields': ('task',)})
    c.repo.store_proposals(claim, (value,))
    if change == 'expiry':
        c.clock[0] += timedelta(minutes=15)
    if change == 'revoked':
        c.team.upsert_member(c.actor.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    with pytest.raises(c.a.VoiceError):
        c.repo.confirm_intent(uid() if change == 'actor' else c.actor.id, value.id,
            expected_revision=1 if change == 'revision' else value.revision, operation_id=uid() if change == 'operation' else value.operation_id)
    assert count(c, 'team_commands') == 0


def test_confirm_race_reuses_one_canonical_team_acceptance(c):
    _, _, claim = job(c, text='Сделай задачу')
    value = proposal(c, claim)
    c.repo.store_proposals(claim, (value,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: c.repo.confirm_intent(c.actor.id, value.id, expected_revision=0, operation_id=value.operation_id), range(2)))
    assert results[0].acceptance_receipt == results[1].acceptance_receipt
    assert count(c, 'team_commands') == 1


def test_completed_proposal_batch_is_immutable_and_local_completion_is_idempotent(c):
    _, identifier, claim = job(c, text='Сделай задачу')
    value = proposal(c, claim)
    c.repo.store_proposals(claim, (value,))
    with pytest.raises(c.a.VoiceError):
        c.repo.store_proposals(claim, ())
    c.repo.complete_job(claim)
    c.repo.complete_job(claim)
    assert c.repo.get_job(identifier)['state'] == 'complete' and c.repo.claim_job('other') is None


def test_notice_is_separate_from_initial_bot_reply_and_preserves_outbox_fencing(c):
    source, _, claim = job(c, text='Сделай задачу')
    acknowledge_initial_reply(c)
    reply = c.a.DeterministicReply('Проверьте предложение')
    notice = c.repo.enqueue_notice(source, 'preview', reply)
    assert c.repo.enqueue_notice(source, 'preview', reply) == notice
    with pytest.raises(c.a.VoiceError):
        c.repo.enqueue_notice(source, 'preview', c.a.DeterministicReply('Другой ответ'))
    delivery = c.repo.claim_reply('sender', lease_seconds=10)
    assert delivery.reply == reply and delivery.event == source
    c.clock[0] += timedelta(seconds=10)
    c.repo.recover_expired()
    assert c.repo.claim_reply('replacement') is None
    with pytest.raises(c.a.VoiceError):
        c.repo.finish_reply(delivery, c.a.SendReceipt(delivery.id, 'sent', message_id='late'))


def test_notice_rate_limit_only_retries_three_times_other_failures_are_uncertain(c):
    source, _, _ = job(c, text='Сделай задачу')
    acknowledge_initial_reply(c)
    c.repo.enqueue_notice(source, 'preview', c.a.DeterministicReply('Предложение'))
    for attempt in range(1, 4):
        delivery = c.repo.claim_reply('sender')
        assert delivery.attempt == attempt
        c.repo.finish_reply(delivery, c.a.SendReceipt(delivery.id, 'retryable', error_code='max_rate_limited', retry_after=2))
        assert c.repo.claim_reply('early') is None
        c.clock[0] += timedelta(seconds=2)
    assert c.repo.claim_reply('again') is None
    c.repo.enqueue_notice(source, 'second', c.a.DeterministicReply('Ещё предложение'))
    delivery = c.repo.claim_reply('sender')
    c.repo.finish_reply(delivery, c.a.SendReceipt(delivery.id, 'retryable', error_code='unknown_network'))
    assert c.repo.claim_reply('again') is None


def test_notice_refuses_desktop_code_instruction_and_revoked_actor(c):
    source, _, _ = job(c, text='Сделай задачу')
    acknowledge_initial_reply(c)
    with pytest.raises(c.a.VoiceError):
        c.repo.enqueue_notice(source, 'code', c.a.DeterministicReply('Код', sensitive_action='desktop_code'))
    c.repo.enqueue_notice(source, 'preview', c.a.DeterministicReply('Предложение'))
    delivery = c.repo.claim_reply('sender')
    c.team.upsert_member(c.actor.model_copy(update={'revision': 1, 'enabled': False}), expected_revision=0)
    with pytest.raises(c.a.VoiceError):
        c.repo.authorize_reply(delivery)


def test_voice_evidence_cannot_be_replaced_even_with_recursive_triggers_off(c):
    source, _, claim = job(c)
    transcript(c, claim)
    c.repo.checkpoint_context(claim, {})
    value = proposal(c, claim)
    c.repo.store_proposals(claim, (value,))
    c.repo.confirm_intent(c.actor.id, value.id, expected_revision=0, operation_id=value.operation_id)
    c.repo.enqueue_notice(source, 'preview', c.a.DeterministicReply('Предложение'))
    with c.db.connection() as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        for table in ('voice_jobs', 'voice_requests', 'voice_responses', 'voice_transcripts', 'voice_contexts',
                      'voice_proposal_batches', 'voice_proposals', 'voice_confirmations', 'voice_notices'):
            row = tuple(conn.execute('SELECT * FROM ' + table).fetchone())
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('INSERT OR REPLACE INTO ' + table + ' VALUES(' + ','.join('?' for _ in row) + ')', row)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('DELETE FROM ' + table)
            column = conn.execute('PRAGMA table_info(' + table + ')').fetchone()['name']
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('UPDATE ' + table + ' SET ' + column + '=' + column)


def test_context_is_bounded_immutable_checkpoint_before_intent_and_survives_restart(c):
    _, _, claim = job(c)
    transcript(c, claim)
    assert c.repo.read_context(claim) is None
    with pytest.raises(c.a.VoiceError):
        c.repo.begin_request(claim, request(c, claim, 'intent'))
    context = {'tasks': [{'task_id': c.snapshot.task_id, 'revision': 0, 'fingerprint': 'a' * 64}], 'members': []}
    assert c.repo.checkpoint_context(claim, context) == context
    assert c.repo.checkpoint_context(claim, context) == context
    with pytest.raises(c.a.VoiceError):
        c.repo.checkpoint_context(claim, {'tasks': []})
    with pytest.raises(c.a.VoiceError):
        c.repo.checkpoint_context(claim, {'oversized': 'Ж' * 32768})
    c.clock[0] += timedelta(seconds=10)
    resumed = c.repo.claim_job('replacement')
    assert c.repo.read_context(resumed) == context
    c.repo.begin_request(resumed, request(c, resumed, 'intent'))


def test_context_cannot_be_checkpointed_before_literal_source(c):
    _, _, claim = job(c)
    with pytest.raises(c.a.VoiceError):
        c.repo.checkpoint_context(claim, {})


def version4_database(c, path):
    from secretary.infrastructure.team_database import SCHEMA, INSERT_GUARDS
    from secretary.infrastructure.team_auth_repository import AUTH_MIGRATION
    from secretary.infrastructure.bot_repository import BOT_MIGRATION
    with sqlite3.connect(path) as conn:
        for sql in (*SCHEMA, *INSERT_GUARDS, *AUTH_MIGRATION, *BOT_MIGRATION):
            conn.execute(sql)
        conn.executemany('INSERT INTO team_schema VALUES(?)', [(1,), (2,), (3,), (4,)])
    old_db = object.__new__(c.a.TeamDatabase)
    old_db.path = path.resolve()
    old_db.maintenance, old_db.participant_id = None, None
    team = c.a.TeamRepository(old_db, clock=lambda: c.clock[0])
    team.upsert_member(c.actor, expected_revision=None)
    auth = c.a.AuthRepository(old_db, secret=b'SYNTHETIC_VOICE_MIGRATION_SECRET_32', clock=lambda: c.clock[0])
    bot = c.a.BotRepository(old_db, team, auth, clock=lambda: c.clock[0])
    source = event(c, text='Исходное событие')
    assert bot.intake(replace(source, actor_id=None, actor_revision=None, project_ids=())).state == 'queued'
    return old_db


def test_real_version4_upgrade_preserves_existing_bot_payloads_and_is_idempotent(c, tmp_path):
    old = version4_database(c, tmp_path / 'v4.sqlite3')
    with old.connection() as conn:
        before = tuple(tuple(row) for row in conn.execute('SELECT * FROM bot_events'))
    upgraded = c.a.TeamDatabase(old.path)
    c.a.TeamDatabase(old.path)
    with upgraded.connection() as conn:
        assert tuple(row[0] for row in conn.execute('SELECT version FROM team_schema ORDER BY version')) == (1, 2, 3, 4, 5, 6, 7, 8, 9)
        assert tuple(tuple(row) for row in conn.execute('SELECT * FROM bot_events')) == before
        assert conn.execute('SELECT COUNT(*) FROM voice_jobs').fetchone()[0] == 0
        assert conn.execute('PRAGMA foreign_key_check').fetchone() is None


def test_failed_voice_migration_rolls_back_all_new_tables_and_schema_marker(c, tmp_path, monkeypatch):
    module = import_module('secretary.infrastructure.voice_repository')
    old = version4_database(c, tmp_path / 'v4-failure.sqlite3')
    with old.connection() as conn:
        before = tuple(tuple(row) for row in conn.execute('SELECT * FROM bot_events'))
    monkeypatch.setattr(module, 'VOICE_MIGRATION', module.VOICE_MIGRATION + ('CREATE TABLE deliberate_error(',))
    with pytest.raises(sqlite3.OperationalError):
        c.a.TeamDatabase(old.path)
    with old.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 4
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'voice_%'").fetchone()
        assert tuple(tuple(row) for row in conn.execute('SELECT * FROM bot_events')) == before


def test_confirmation_checkpoint_failure_rolls_back_canonical_acceptance(c):
    _, _, claim = job(c, text='Сделай задачу')
    value = proposal(c, claim)
    c.repo.store_proposals(claim, (value,))
    with c.db.connection() as conn:
        conn.execute("CREATE TRIGGER synthetic_confirmation_failure BEFORE INSERT ON voice_confirmations BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(sqlite3.IntegrityError):
        c.repo.confirm_intent(c.actor.id, value.id, expected_revision=0, operation_id=value.operation_id)
    assert count(c, 'team_commands') == 0


def test_late_response_cannot_cross_expired_fence_or_make_uncertain_replayable(c):
    _, identifier, claim = job(c)
    info = request(c, claim)
    c.repo.begin_request(claim, info)
    c.clock[0] += timedelta(seconds=10)
    c.repo.recover_expired()
    with pytest.raises(c.a.VoiceError):
        c.repo.record_response(claim, response(info))
    assert c.repo.get_job(identifier)['state'] == 'uncertain'
    assert count(c, 'voice_responses') == 0


def test_response_body_bound_is_bytes_not_characters(c):
    _, _, claim = job(c, text='Сделай задачу')
    info = request(c, claim, 'intent')
    c.repo.begin_request(claim, info)
    with pytest.raises(c.a.VoiceError):
        c.repo.record_response(claim, response(info, provider_response={'text': 'Ж' * 32768}))
    assert count(c, 'voice_responses') == 0


def test_media_download_url_never_enters_notice(c):
    source, _, _ = job(c)
    with pytest.raises(c.a.VoiceError):
        c.repo.enqueue_notice(source, 'bad', c.a.DeterministicReply('https://synthetic.invalid/audio'))
    assert count(c, 'voice_notices') == 0


def test_voice_notices_wait_for_initial_bot_reply_and_follow_per_event_fifo(c):
    source, _, _ = job(c, text='Сделай задачу')
    c.repo.enqueue_notice(source, 'stage', c.a.DeterministicReply('Текст распознан'))
    c.repo.enqueue_notice(source, 'result', c.a.DeterministicReply('Предложения готовы'))
    assert c.repo.claim_reply('too-early') is None
    c.bot.finish_event(c.bot.claim_event('inbox'), c.a.DeterministicReply('Обрабатываю'))
    assert c.repo.claim_reply('still-early') is None
    first = c.bot.claim_reply('sender')
    assert c.repo.claim_reply('sending-initial') is None
    c.bot.finish_reply(first, c.a.SendReceipt(first.id, 'sent', message_id='initial'))
    stage = c.repo.claim_reply('sender')
    assert stage.reply.text == 'Текст распознан'
    assert c.repo.claim_reply('parallel') is None
    c.repo.finish_reply(stage, c.a.SendReceipt(stage.id, 'sent', message_id='stage'))
    result = c.repo.claim_reply('sender')
    assert result.reply.text == 'Предложения готовы'


@pytest.mark.parametrize('paid', [False, True])
def test_revocation_allows_fenced_pending_boolean_and_terminalizes_safely(c, paid):
    _, identifier, claim = job(c)
    if paid:
        c.repo.begin_request(claim, request(c, claim))
    c.team.upsert_member(c.actor.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    assert c.repo.pending_request(claim) is paid
    c.repo.fail_job(claim, 'uncertain' if paid else 'rejected', 'voice_actor_changed')
    with c.db.connection() as conn:
        assert conn.execute('SELECT state FROM voice_job_state WHERE id=?', (identifier,)).fetchone()[0] == ('uncertain' if paid else 'rejected')
    with pytest.raises(c.a.VoiceError):
        c.repo.pending_request(claim)


@pytest.mark.parametrize('status', [201, 202, 204, 400, 500])
@pytest.mark.parametrize('stage', ['stt', 'intent'])
def test_only_http_200_allows_transcript_or_intent_repair(c, status, stage):
    _, _, claim = job(c, text='Сделай задачу' if stage == 'intent' else None)
    info = request(c, claim, stage)
    c.repo.begin_request(claim, info)
    c.repo.record_response(claim, response(info, status=status))
    if stage == 'stt':
        with pytest.raises(c.a.VoiceError):
            c.repo.checkpoint_transcript(claim, 'Сделай задачу')
        assert c.repo.transcript(claim) is None
    else:
        with pytest.raises(c.a.VoiceError):
            c.repo.begin_request(claim, request(c, claim, 'intent', 1))
    assert c.repo.read_responses(claim)[0]['status'] == status
    assert count(c, 'voice_requests') == 1
