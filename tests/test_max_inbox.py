"""T7 inbox contracts use synthetic identities and an isolated Team SQLite."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from importlib import import_module, util
import sqlite3
from threading import Event, Thread
from time import perf_counter
from uuid import uuid4

import pytest

from test_bot_repository import botcase, event


def test_bot_storage_port_is_available():
    name = 'secretary.infrastructure.bot_repository'
    assert util.find_spec(name) is not None, 'T7 bot repository absent'
    assert callable(import_module(name).BotRepository)


def database_dump(c):
    with c.db.connection() as conn:
        return '\n'.join(conn.iterdump())


def test_concurrent_same_kind_key_has_one_immutable_event(botcase):
    c = botcase
    original = event(c)
    values = [replace(original, event_id=str(uuid4())) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(c.repo.intake, values))
    assert [value.state for value in receipts].count('queued') == 1
    assert [value.state for value in receipts].count('duplicate') == 3
    assert len({value.event_id for value in receipts}) == 1
    saved = c.repo.get_event(receipts[0].event_id)
    assert saved.actor_id == c.member.id and saved.actor_revision == c.member.revision
    assert saved.project_ids == c.member.project_ids
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bot_events').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM bot_event_state').fetchone()[0] == 1


def test_deduplication_is_namespaced_by_bot_and_event_kind(botcase):
    c = botcase
    original = event(c)
    values = (original, replace(original, event_id=str(uuid4()), bot_id='43'),
        replace(original, event_id=str(uuid4()), kind='message_callback',
            callback_id='synthetic-callback', callback_payload='opaque', text=None))
    receipts = [c.repo.intake(value) for value in values]
    assert all(value.state == 'queued' for value in receipts)
    assert len({value.event_id for value in receipts}) == 3


def test_duplicate_preserves_original_actor_snapshot_after_revision_change(botcase):
    c = botcase
    original = event(c)
    accepted = c.repo.intake(original)
    before = c.repo.get_event(accepted.event_id)
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'project_ids': ('7', '8')}),
        expected_revision=0)
    duplicate = c.repo.intake(replace(original, event_id=str(uuid4())))
    assert duplicate.state == 'duplicate' and duplicate.event_id == accepted.event_id
    assert c.repo.get_event(accepted.event_id) == before
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bot_events').fetchone()[0] == 1


def test_actor_binding_is_resolved_from_current_exact_max_id(botcase):
    c = botcase
    current = c.member.model_copy(update={'revision': 1, 'project_ids': ('7', '8')})
    c.team.upsert_member(current, expected_revision=0)
    supplied = event(c, actor_id=c.owner.id, actor_revision=99, project_ids=('999',))
    receipt = c.repo.intake(supplied)
    saved = c.repo.get_event(receipt.event_id)
    assert receipt.state == 'queued'
    assert (saved.actor_id, saved.actor_revision, saved.project_ids) == (
        current.id, current.revision, current.project_ids)


@pytest.mark.parametrize('disabled', [False, True])
def test_unmapped_or_disabled_actor_is_quarantined_without_private_payload(botcase, disabled):
    c = botcase
    if disabled:
        c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'enabled': False}), expected_revision=0)
    marker = 'SYNTHETIC_PRIVATE_UNKNOWN_MESSAGE'
    supplied = event(c, user_id=c.member.max_user_id if disabled else '999', text=marker,
        callback_payload='SYNTHETIC_PRIVATE_CALLBACK', media=({'url': 'https://example.invalid/private-media'},),
        actor_id=c.owner.id, actor_revision=0, project_ids=('7',))
    receipt = c.repo.intake(supplied)
    assert receipt.state == 'quarantined'
    saved = c.repo.get_event(receipt.event_id)
    assert saved.actor_id is None and saved.actor_revision is None and saved.project_ids == ()
    assert saved.text is None and saved.callback_payload is None and saved.media == ()
    assert c.repo.claim_event('worker') is None
    dump = database_dump(c)
    assert marker not in dump and 'SYNTHETIC_PRIVATE_CALLBACK' not in dump and 'private-media' not in dump


def test_normalized_malformed_event_stays_quarantined_even_for_mapped_actor(botcase):
    c = botcase
    receipt = c.repo.intake(event(c, quarantine_reason='bot_event_invalid', text='SYNTHETIC_BAD_EVENT_TEXT',
        callback_payload='SYNTHETIC_BAD_EVENT_CALLBACK', media=({'url': 'https://example.invalid/bad-event'},)))
    assert receipt.state == 'quarantined'
    saved = c.repo.get_event(receipt.event_id)
    assert saved.quarantine_reason and saved.text is None and saved.callback_payload is None and saved.media == ()
    assert c.repo.claim_event('worker') is None
    dump = database_dump(c)
    assert 'SYNTHETIC_BAD_EVENT_' not in dump and '/bad-event' not in dump


def test_valid_invitation_is_claimed_atomically_but_does_not_create_membership(botcase):
    c = botcase
    invitation = c.auth.create_invitation(c.owner.id, ('7',))
    supplied = event(c, kind='bot_started', user_id='333', chat_id='333', message_id=None,
        text=None, invitation_value=invitation.value)
    receipt = c.repo.intake(supplied)
    assert receipt.state == 'queued'
    saved = c.repo.get_event(receipt.event_id)
    assert saved.invitation_id == invitation.id and saved.invitation_value is None
    assert saved.actor_id is None and saved.actor_revision is None and saved.project_ids == ()
    view = c.auth.read_invitation(c.owner.id, invitation.id)
    assert view.candidate_user_id == '333' and view.confirmed_member_id is None
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_members WHERE max_user_id=?', ('333',)).fetchone()[0] == 0
    assert invitation.value not in database_dump(c)
    duplicate = c.repo.intake(replace(supplied, event_id=str(uuid4())))
    assert duplicate.state == 'duplicate' and duplicate.event_id == receipt.event_id
    with c.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM team_auth_journal WHERE event='invite_claimed'").fetchone()[0] == 1


def test_inbox_insert_failure_rolls_back_invitation_candidate_claim(botcase):
    c = botcase
    invitation = c.auth.create_invitation(c.owner.id, ('7',))
    supplied = event(c, kind='bot_started', user_id='333', chat_id='333', message_id=None,
        text=None, invitation_value=invitation.value)
    with c.db.connection() as conn:
        conn.execute("CREATE TRIGGER synthetic_reject_inbox BEFORE INSERT ON bot_events BEGIN SELECT RAISE(ABORT,'synthetic reject'); END")
    with pytest.raises((c.a.BotError, sqlite3.IntegrityError)):
        c.repo.intake(supplied)
    view = c.auth.read_invitation(c.owner.id, invitation.id)
    assert view.candidate_user_id is None
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bot_events').fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM team_auth_journal WHERE event='invite_claimed'").fetchone()[0] == 0
    assert invitation.value not in database_dump(c)


def test_invalid_invitation_is_sanitized_without_pinning_identity(botcase):
    c = botcase
    token = 'SYNTHETIC_INVALID_INVITATION_1234567890abcdefg'
    supplied = event(c, kind='bot_started', user_id='333', chat_id='333', message_id=None,
        text=None, invitation_value=token)
    receipt = c.repo.intake(supplied)
    assert receipt.state == 'quarantined'
    saved = c.repo.get_event(receipt.event_id)
    assert saved.invitation_value is None and saved.invitation_id is None and saved.actor_id is None
    assert token not in database_dump(c)
    assert c.repo.claim_event('worker') is None


def test_intake_writer_contention_is_bounded_and_never_acknowledged(botcase):
    c = botcase
    ready, release = Event(), Event()
    errors = []

    def hold_writer():
        try:
            with sqlite3.connect(c.db.path, isolation_level=None) as conn:
                conn.execute('BEGIN IMMEDIATE')
                ready.set()
                release.wait(1)
                conn.rollback()
        except BaseException as error:
            errors.append(error)
            ready.set()

    locker = Thread(target=hold_writer)
    locker.start()
    try:
        assert ready.wait(1) and not errors
        started = perf_counter()
        with pytest.raises(c.a.BotError, match='^bot_storage_busy$'):
            c.repo.intake(event(c))
        elapsed = perf_counter() - started
    finally:
        release.set()
        locker.join(2)
    assert not locker.is_alive() and not errors
    assert elapsed < 0.6, 'Intake must use the 200 ms SQLite busy bound, not the general 15 s timeout'
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bot_events').fetchone()[0] == 0


def legacy_three(path):
    schema = import_module('secretary.infrastructure.team_database')
    auth = import_module('secretary.infrastructure.team_auth_repository')
    with sqlite3.connect(path) as conn:
        for sql in (*schema.SCHEMA, *schema.INSERT_GUARDS, *auth.AUTH_MIGRATION):
            conn.execute(sql)
        conn.executemany('INSERT INTO team_schema VALUES(?)', [(1,), (2,), (3,)])
        conn.execute('INSERT INTO team_auth_replays VALUES(?,?,?)', ('a' * 64, '123', 1000.0))
    return schema


def test_genuine_v3_database_migrates_to_four_without_losing_replay_evidence(tmp_path):
    path = tmp_path / 'legacy-three.sqlite3'
    schema = legacy_three(path)
    db = schema.TeamDatabase(path)
    with db.connection() as conn:
        assert [row[0] for row in conn.execute('SELECT version FROM team_schema ORDER BY version')] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        assert tuple(conn.execute('SELECT * FROM team_auth_replays').fetchone()) == ('a' * 64, '123', 1000.0)
        assert conn.execute('SELECT COUNT(*) FROM bot_events').fetchone()[0] == 0
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    schema.TeamDatabase(path)


def test_bot_migration_failure_rolls_back_ddl_and_preserves_version_three(tmp_path, monkeypatch):
    path = tmp_path / 'legacy-three-rollback.sqlite3'
    schema = legacy_three(path)
    bot = import_module('secretary.infrastructure.bot_repository')
    with monkeypatch.context() as patch:
        patch.setattr(bot, 'BOT_MIGRATION', (*bot.BOT_MIGRATION, 'INVALID SYNTHETIC MIGRATION'))
        with pytest.raises(sqlite3.OperationalError):
            schema.TeamDatabase(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 3
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'bot_%'").fetchall() == []
        assert tuple(conn.execute('SELECT * FROM team_auth_replays').fetchone()) == ('a' * 64, '123', 1000.0)
    schema.TeamDatabase(path)
