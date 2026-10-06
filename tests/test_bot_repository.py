"""Synthetic T7 inbox, durable buttons and fenced outbound delivery contracts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest


def uid():
    return str(uuid4())


@pytest.fixture
def botcase(tmp_path):
    assert (Path(__file__).resolve().parents[1] / 'backend/secretary/infrastructure/bot_repository.py').exists(), 'T7 repository absent'
    values = {}
    for name in ('domain.team', 'domain.bot', 'infrastructure.team_database', 'infrastructure.team_repository',
                 'infrastructure.team_auth_repository', 'infrastructure.bot_repository'):
        values.update(vars(import_module('secretary.' + name)))
    a = SimpleNamespace(**values)
    now = [datetime(2026, 10, 4, 1, tzinfo=timezone.utc)]
    db = a.TeamDatabase(tmp_path / 'bot-team.sqlite3')
    team = a.TeamRepository(db, clock=lambda: now[0])
    owner = a.TeamMember(id=uid(), display_name='Synthetic owner', role='owner', max_user_id='111',
                         vikunja_user_id='11', project_ids=('7',))
    member = a.TeamMember(id=uid(), display_name='Synthetic member', max_user_id='222',
                          vikunja_user_id='22', project_ids=('7',))
    for person in (owner, member):
        team.upsert_member(person, expected_revision=None)
    snapshot = a.TaskSnapshot(task_id='55', project_id='7', revision=0, remote_fingerprint='a' * 64,
        title='Synthetic task', assignee_id=member.id, important=False, urgent=False, classification_confirmed=True)
    team.save_projection(snapshot, expected_revision=None)
    auth = a.AuthRepository(db, secret=b'SYNTHETIC_BOT_AUTH_STORAGE_SECRET_32', clock=lambda: now[0])
    repo = a.BotRepository(db, team, auth, clock=lambda: now[0])
    return SimpleNamespace(a=a, db=db, team=team, auth=auth, repo=repo, owner=owner, member=member,
                           snapshot=snapshot, time=now)


def event(c, **edits):
    identifier = uid()
    return c.a.BotEvent(**{'event_id': identifier, 'dedup_key': 'message:' + identifier, 'bot_id': '42',
        'kind': 'message_created', 'timestamp_ms': int(c.time[0].timestamp() * 1000),
        'user_id': c.member.max_user_id, 'chat_id': c.member.max_user_id,
        'message_id': identifier, 'text': 'Мои задачи', **edits})


def stored(c, **edits):
    value = event(c, **edits)
    receipt = c.repo.intake(value)
    assert receipt.state == 'queued'
    return c.repo.get_event(receipt.event_id)


def command(c):
    return c.a.TaskCommand(operation_id=uid(), project_id='7', task_id='55', expected_revision=0,
        expected_fingerprint='a' * 64, action='set_state', values={'bucket': 'doing'})


def callback(c, payload, **edits):
    return stored(c, kind='message_callback', callback_id=uid(), callback_payload=payload, text=None, **edits)


def test_claim_fence_recovery_and_current_actor_revision(botcase):
    c = botcase
    value = stored(c)
    first = c.repo.claim_event('worker', lease_seconds=10)
    assert first.event == value and c.repo.authorize_event(first) == value
    c.time[0] += timedelta(seconds=10)
    c.repo.recover_expired()
    second = c.repo.claim_event('other')
    assert second.fence > first.fence
    with pytest.raises(c.a.BotError):
        c.repo.finish_event(first, c.a.DeterministicReply('old'))
    c.team.upsert_member(c.member.model_copy(update={'revision': 1}), expected_revision=0)
    with pytest.raises(c.a.BotError):
        c.repo.authorize_event(second)
    c.repo.fail_event(second, 'bot_actor_changed')
    assert c.repo.claim_reply('sender') is None


def test_button_payload_is_stable_for_exact_intent_and_conflicts_for_changed_intent(botcase):
    c = botcase
    value = stored(c)
    cmd = command(c)
    payload = c.repo.store_button(value, key='confirm', action='confirm', command=cmd)
    assert payload == c.repo.store_button(value, key='confirm', action='confirm', command=cmd)
    assert len(payload) <= 1024 and '55' not in payload[:3]
    with pytest.raises(c.a.BotError, match='bot_button_conflict'):
        c.repo.store_button(value, key='confirm', action='confirm', command=command(c))
    action = c.repo.resolve_button(callback(c, payload))
    assert action.command == cmd


def test_button_rejects_foreign_identity_expiry_and_stale_projection(botcase):
    c = botcase
    value = stored(c)
    payload = c.repo.store_button(value, key='task', action='task', task_id='55', expires_seconds=10)
    with pytest.raises(c.a.BotError):
        c.repo.resolve_button(callback(c, payload, user_id=c.owner.max_user_id))
    stale = callback(c, payload)
    c.team.save_projection(c.snapshot.model_copy(update={'revision': 1, 'remote_fingerprint': 'b' * 64}), expected_revision=0)
    with pytest.raises(c.a.BotError, match='bot_button_stale'):
        c.repo.resolve_button(stale)
    c.time[0] += timedelta(seconds=10)
    with pytest.raises(c.a.BotError):
        c.repo.resolve_button(stale)


def test_confirm_callback_concurrency_creates_one_command_and_replay_ignores_new_projection(botcase):
    c = botcase
    cmd = command(c)
    payload = c.repo.store_button(stored(c), key='confirm', action='confirm', command=cmd)
    callbacks = [callback(c, payload), callback(c, payload)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(c.repo.confirm_button, callbacks))
    assert receipts[0].acceptance_receipt == receipts[1].acceptance_receipt
    assert receipts[0].acceptance_receipt.decision == 'accepted'
    c.team.save_projection(c.snapshot.model_copy(update={'revision': 1, 'remote_fingerprint': 'b' * 64}), expected_revision=0)
    assert c.repo.confirm_button(callback(c, payload)).acceptance_receipt == receipts[0].acceptance_receipt
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 1


def test_finish_processing_enqueues_one_reply_and_sender_crash_never_requeues(botcase):
    c = botcase
    stored(c)
    claim = c.repo.claim_event('worker')
    reply = c.a.DeterministicReply('Сохранено')
    c.repo.finish_event(claim, reply)
    c.repo.finish_event(claim, reply)
    delivery = c.repo.claim_reply('sender', lease_seconds=10)
    assert delivery.reply == reply and delivery.state == 'sending' and delivery.attempt == 1
    assert c.repo.authorize_reply(delivery).id == delivery.id
    c.time[0] += timedelta(seconds=10)
    restarted = c.a.BotRepository(c.a.TeamDatabase(c.db.path), c.team, c.auth, clock=lambda: c.time[0])
    restarted.recover_expired()
    assert restarted.claim_reply('sender2') is None
    with pytest.raises(c.a.BotError):
        restarted.finish_reply(delivery, c.a.SendReceipt(delivery.id, 'sent', message_id='late'))


def test_reply_retryable_is_bounded_delayed_and_sensitive_code_never_automatically_retries(botcase):
    c = botcase
    stored(c)
    c.repo.finish_event(c.repo.claim_event('worker'), c.a.DeterministicReply('Обычный ответ'))
    for attempt in range(1, 4):
        delivery = c.repo.claim_reply('sender')
        assert delivery.attempt == attempt
        c.repo.finish_reply(delivery, c.a.SendReceipt(delivery.id, 'retryable', error_code='max_rate_limited', retry_after=10))
        assert c.repo.claim_reply('early') is None
        c.time[0] += timedelta(seconds=10)
    assert c.repo.claim_reply('again') is None
    stored(c)
    c.repo.finish_event(c.repo.claim_event('worker'), c.a.DeterministicReply('Код для входа', sensitive_action='desktop_code'))
    sensitive = c.repo.claim_reply('sender')
    c.repo.finish_reply(sensitive, c.a.SendReceipt(sensitive.id, 'retryable', error_code='max_rate_limited', retry_after=1))
    c.time[0] += timedelta(seconds=10)
    assert c.repo.claim_reply('again') is None


def test_outbound_heartbeat_and_auth_revocation_are_fenced(botcase):
    c = botcase
    stored(c)
    c.repo.finish_event(c.repo.claim_event('worker'), c.a.DeterministicReply('Ответ'))
    delivery = c.repo.claim_reply('sender', lease_seconds=10)
    c.time[0] += timedelta(seconds=5)
    renewed = c.repo.renew_reply(delivery, lease_seconds=60)
    assert renewed.lease_until > delivery.lease_until
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'enabled': False}), expected_revision=0)
    with pytest.raises(c.a.BotError):
        c.repo.authorize_reply(renewed)


def test_proposal_idempotency_requires_exact_text_task_and_purpose(botcase):
    c = botcase
    value = stored(c)
    proposal = c.repo.save_proposal(value, 'Обсудить срок', task_id='55', purpose='intent')
    assert c.repo.save_proposal(value, 'Обсудить срок', task_id='55', purpose='intent') == proposal
    with pytest.raises(c.a.BotError, match='bot_proposal_conflict'):
        c.repo.save_proposal(value, 'Другой срок', task_id='55', purpose='intent')


def test_bot_evidence_tables_refuse_replace_update_and_delete(botcase):
    c = botcase
    value = stored(c)
    c.repo.store_button(value, key='task', action='task', task_id='55')
    c.repo.save_proposal(value, 'Предложение')
    c.repo.finish_event(c.repo.claim_event('worker'), c.a.DeterministicReply('Ответ'))
    with c.db.connection() as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        for table in ('bot_events', 'bot_buttons', 'bot_proposals', 'bot_replies'):
            row = tuple(conn.execute('SELECT * FROM ' + table).fetchone())
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('INSERT OR REPLACE INTO ' + table + ' VALUES(' + ','.join('?' for _ in row) + ')', row)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('DELETE FROM ' + table)


def test_intake_source_identity_ignores_only_derived_fields_and_bot_started_timestamp(botcase):
    c = botcase
    value = event(c)
    first = c.repo.intake(value)
    assert c.repo.intake(replace(value, actor_id=c.owner.id, actor_revision=99, project_ids=('99',))).state == 'duplicate'
    with pytest.raises(c.a.BotError, match='bot_event_conflict'):
        c.repo.intake(replace(value, text='Изменённый текст'))
    assert c.repo.get_event(first.event_id).text == value.text
    invite = c.auth.create_invitation(c.owner.id, ('7',))
    start = event(c, kind='bot_started', user_id='333', chat_id='333', invitation_value=invite.value, text=None)
    accepted = c.repo.intake(start)
    assert c.repo.intake(replace(start, timestamp_ms=start.timestamp_ms + 1000)).event_id == accepted.event_id
    with pytest.raises(c.a.BotError, match='bot_event_conflict'):
        c.repo.intake(replace(start, invitation_value='a' * 43))


def test_context_is_bound_to_one_text_event_without_resurrecting_older_context(botcase):
    c = botcase
    c.repo.save_context(stored(c), '55', 'due')
    c.repo.save_context(stored(c), '55', 'result')
    value = stored(c, text='Сделано')
    action = c.repo.read_context(value)
    assert action.action == 'result' and action.command.action == 'link'
    assert action.command.expected_revision == 0 and action.command.expected_fingerprint == 'a' * 64
    assert c.repo.read_context(value) == action
    assert c.repo.read_context(stored(c, text='Несвязанный следующий текст')) is None


def test_atomic_confirmation_failure_rolls_back_acceptance_and_retry_reuses_stored_command(botcase):
    c = botcase
    cmd = command(c)
    payload = c.repo.store_button(stored(c), key='confirm', action='confirm', command=cmd)
    value = callback(c, payload)
    with c.db.connection() as conn:
        conn.execute("CREATE TRIGGER synthetic_fail_accept BEFORE INSERT ON team_journal BEGIN SELECT RAISE(ABORT,'synthetic crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        c.repo.confirm_button(value)
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM team_execution').fetchone()[0] == 0
        conn.execute('DROP TRIGGER synthetic_fail_accept')
    assert c.repo.confirm_button(value).acceptance_receipt.operation_id == cmd.operation_id


def test_finish_event_is_atomic_with_reply_outbox_and_cannot_persist_login_code(botcase):
    c = botcase
    value = stored(c)
    claim = c.repo.claim_event('worker')
    with c.db.connection() as conn:
        conn.execute("CREATE TRIGGER synthetic_fail_reply BEFORE INSERT ON bot_reply_state BEGIN SELECT RAISE(ABORT,'synthetic crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        c.repo.finish_event(claim, c.a.DeterministicReply('Ответ'))
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bot_replies').fetchone()[0] == 0
        assert conn.execute('SELECT state FROM bot_event_state WHERE id=?', (value.event_id,)).fetchone()[0] == 'processing'
        conn.execute('DROP TRIGGER synthetic_fail_reply')
    with pytest.raises(c.a.BotError, match='bot_sensitive_reply_forbidden'):
        c.repo.finish_event(claim, c.a.DeterministicReply('a' * 32 + '.ABCDEFGHIJ', sensitive_action='desktop_code'))
    c.repo.finish_event(claim, c.a.DeterministicReply('Ответ'))


def test_unverified_retryable_receipt_never_becomes_pending_and_receipt_id_is_exact(botcase):
    c = botcase
    stored(c)
    c.repo.finish_event(c.repo.claim_event('worker'), c.a.DeterministicReply('Ответ'))
    delivery = c.repo.claim_reply('sender')
    with pytest.raises(c.a.BotError, match='bot_receipt_invalid'):
        c.repo.finish_reply(delivery, c.a.SendReceipt(uid(), 'sent', message_id='different'))
    c.repo.finish_reply(delivery, c.a.SendReceipt(delivery.id, 'retryable', error_code='max_timeout'))
    c.time[0] += timedelta(seconds=10)
    assert c.repo.claim_reply('again') is None


def test_context_absence_is_durable_and_later_context_cannot_change_old_text_meaning(botcase):
    c = botcase
    before = stored(c, text='Свободный текст до запроса результата')
    assert c.repo.read_context(before) is None
    c.repo.save_context(stored(c), '55', 'result')
    assert c.repo.read_context(before) is None
    older_timestamp = int(c.time[0].timestamp() * 1000) - 1
    delayed = stored(c, text='Задержанное сообщение', timestamp_ms=older_timestamp)
    assert c.repo.read_context(delayed) is None
    assert c.repo.read_context(stored(c, text='Текущий результат')).action == 'result'


def test_command_preview_stores_only_safe_base_reply_instruction(botcase):
    c = botcase
    stored(c)
    preview = c.a.CommandPreview('Подтвердите действие', command=command(c))
    c.repo.finish_event(c.repo.claim_event('worker'), preview)
    delivered = c.repo.claim_reply('sender')
    assert delivered.reply.text == preview.text
    assert type(delivered.reply) is c.a.DeterministicReply


@pytest.mark.parametrize('callback_chat', ['222', None])
def test_group_origin_button_accepts_private_or_missing_message_callback_for_same_user(botcase, callback_chat):
    c = botcase
    payload = c.repo.store_button(stored(c, chat_id='-777'), key='doing', action='doing', task_id='55')
    action = c.repo.resolve_button(callback(c, payload, chat_id=callback_chat))
    assert action.action == 'doing'
    assert action.command is None
    assert action.expected_revision == 0 and action.expected_fingerprint == 'a' * 64
    with pytest.raises(c.a.BotError, match='bot_button_forbidden'):
        c.repo.resolve_button(callback(c, payload, chat_id=callback_chat, user_id=c.owner.max_user_id))


def test_result_context_from_nullable_callback_is_bound_to_user_not_optional_chat(botcase):
    c = botcase
    request = stored(c, kind='message_callback', chat_id=None, text=None, callback_id=uid())
    c.repo.save_context(request, '55', 'result')
    assert c.repo.read_context(stored(c, chat_id='222', text='Результат')).action == 'result'


def test_nested_login_code_and_sensitive_buttons_are_never_durable(botcase):
    c = botcase
    stored(c)
    claim = c.repo.claim_event('worker')
    code = 'a' * 32 + '.ABCDEFGHIJ'
    button = c.a.BotButton('Кнопка', payload=code)
    for reply in (c.a.DeterministicReply('Ответ', ((button,),)),
                  c.a.DeterministicReply('Код', ((c.a.BotButton('Кнопка', payload='opaque'),),), sensitive_action='desktop_code')):
        with pytest.raises(c.a.BotError, match='bot_sensitive_reply_forbidden'):
            c.repo.finish_event(claim, reply)
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bot_replies').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['message_created', 'message_callback'])
def test_provider_redelivery_timestamp_does_not_change_stored_time_or_allow_changed_content(botcase, kind):
    c = botcase
    value = event(c, kind=kind, callback_id=uid() if kind == 'message_callback' else None,
                  callback_payload='opaque' if kind == 'message_callback' else None)
    accepted = c.repo.intake(value)
    replay = c.repo.intake(replace(value, event_id=uid(), timestamp_ms=value.timestamp_ms + 1000))
    assert replay.state == 'duplicate' and replay.event_id == accepted.event_id
    assert c.repo.get_event(accepted.event_id).timestamp_ms == value.timestamp_ms
    for changed in (replace(value, text='Другой текст'), replace(value, user_id='111'),
                    replace(value, callback_payload='changed')):
        with pytest.raises(c.a.BotError, match='bot_event_conflict'):
            c.repo.intake(changed)


def test_accepted_exact_callback_recovers_after_expiry_but_new_callback_and_revoked_actor_cannot(botcase):
    c = botcase
    cmd = command(c)
    payload = c.repo.store_button(stored(c), key='confirm', action='confirm', command=cmd, expires_seconds=10)
    value = callback(c, payload)
    accepted = c.repo.confirm_button(value)
    c.time[0] += timedelta(seconds=11)
    c.team.save_projection(c.snapshot.model_copy(update={'revision': 1, 'remote_fingerprint': 'b' * 64}), expected_revision=0)
    assert c.repo.resolve_button(value).command == cmd
    assert c.repo.confirm_button(value).acceptance_receipt == accepted.acceptance_receipt
    with pytest.raises(c.a.BotError, match='bot_button_expired'):
        c.repo.confirm_button(callback(c, payload))
    with pytest.raises(c.a.BotError, match='bot_event_changed'):
        c.repo.confirm_button(replace(value, user_id=c.owner.max_user_id))
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'enabled': False}), expected_revision=0)
    with pytest.raises(c.a.BotError, match='bot_actor_changed'):
        c.repo.confirm_button(value)
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 1


def test_confirmation_checkpoint_failure_rolls_back_command_acceptance(botcase):
    c = botcase
    payload = c.repo.store_button(stored(c), key='confirm', action='confirm', command=command(c))
    value = callback(c, payload)
    with c.db.connection() as conn:
        conn.execute("CREATE TRIGGER synthetic_confirm_failure BEFORE INSERT ON bot_button_confirmations BEGIN SELECT RAISE(ABORT,'synthetic crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        c.repo.confirm_button(value)
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM bot_button_confirmations').fetchone()[0] == 0
        conn.execute('DROP TRIGGER synthetic_confirm_failure')
    assert c.repo.confirm_button(value).acceptance_receipt.decision == 'accepted'


def test_inbox_claims_preserve_per_user_order_while_other_users_can_progress(botcase):
    c = botcase
    first = stored(c)
    second = stored(c, text='Результат следующего сообщения')
    other = stored(c, user_id=c.owner.max_user_id)
    first_claim = c.repo.claim_event('worker-a')
    other_claim = c.repo.claim_event('worker-b')
    assert first_claim.event == first and other_claim.event == other
    assert c.repo.claim_event('worker-c') is None
    c.repo.finish_event(first_claim, c.a.DeterministicReply('Запрос результата'))
    assert c.repo.claim_event('worker-c').event == second


@pytest.mark.parametrize('user_id', ['222', '999'])
def test_large_normalized_media_is_durably_quarantined_without_private_fields(botcase, user_id):
    c = botcase
    media = tuple({'type': 'audio', 'url': 'https://example.invalid/' + 'x' * 8100,
                   'token': 'SYNTHETIC_LARGE_TOKEN_' + 'y' * 4000, 'file_id': 'z' * 4000} for _ in range(10))
    supplied = event(c, user_id=user_id, media=media)
    receipt = c.repo.intake(supplied)
    assert receipt.state == 'quarantined'
    assert c.repo.intake(supplied).state == 'duplicate'
    saved = c.repo.get_event(receipt.event_id)
    assert saved.text is None and saved.media == ()
    with c.db.connection() as conn:
        assert 'SYNTHETIC_LARGE_TOKEN_' not in '\n'.join(conn.iterdump())
