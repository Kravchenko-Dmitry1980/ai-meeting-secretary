"""T6 auth storage: synthetic secrets and isolated Team SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from importlib import import_module
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from uuid import uuid4
from urllib.parse import quote

import pytest


def uid():
    return str(uuid4())


def api():
    assert (Path(__file__).resolve().parents[1] / 'backend/secretary/infrastructure/team_auth_repository.py').is_file(), 'T6 auth repository absent'
    values = {}
    for name in ('domain.team', 'domain.team_auth', 'infrastructure.team_database',
                 'infrastructure.team_repository', 'infrastructure.team_auth_repository'):
        values.update(vars(import_module('secretary.' + name)))
    return SimpleNamespace(**values)


@pytest.fixture
def case(tmp_path):
    a = api()
    db = a.TeamDatabase(tmp_path / 'team.sqlite3')
    clock = [datetime(2026, 10, 3, 12, tzinfo=timezone.utc)]
    team = a.TeamRepository(db, clock=lambda: clock[0])
    owner = a.TeamMember(id=uid(), display_name='Synthetic owner', role='owner', max_user_id='111',
                         vikunja_user_id='11', project_ids=('7',))
    member = a.TeamMember(id=uid(), display_name='Synthetic member', max_user_id='222',
                          vikunja_user_id='22', project_ids=('7',))
    for person in (owner, member):
        team.upsert_member(person, expected_revision=None)
    auth = a.AuthRepository(db, secret=b'SYNTHETIC_STORAGE_KEY_32_BYTES_ONLY', clock=lambda: clock[0])
    return SimpleNamespace(a=a, db=db, team=team, auth=auth, clock=clock, owner=owner, member=member)


def identity(c, user_id=None, replay='b' * 64):
    return c.a.MaxIdentity(user_id=user_id or c.member.max_user_id,
        auth_date=int(c.clock[0].timestamp()), signature_hex='a' * 64, replay_key=replay)


def invited_member(c, max_id='333', **edits):
    return c.a.TeamMember(**{'id': uid(), 'display_name': 'Invited synthetic person', 'max_user_id': max_id,
        'vikunja_user_id': '33', 'project_ids': ('7',), **edits})


def test_fresh_team_database_migrates_through_three_and_reopens(case):
    c = case
    with c.db.connection() as conn:
        assert [row[0] for row in conn.execute('SELECT version FROM team_schema ORDER BY version')] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    c.a.TeamDatabase(c.db.path)


def test_max_login_replay_is_atomic_and_does_not_issue_second_session(case):
    c = case
    value = identity(c)
    def login(_):
        try:
            return c.auth.login_max(value)
        except c.a.AuthError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(login, range(2)))
    grants = [result for result in results if isinstance(result, c.a.SessionGrant)]
    assert len(grants) == 1 and results.count('auth_replay') == 1
    grant = grants[0]
    assert len(grant.token) >= 43 and grant.member == c.member
    assert grant.expires_at == c.clock[0] + timedelta(hours=12)
    assert grant.token not in repr(grant) and grant.csrf not in repr(grant)
    assert c.auth.authenticate(grant.token) == c.member
    assert c.auth.csrf_token(grant.token) == grant.csrf


def test_verified_max_replay_stays_consumed_after_session_secret_rotation(case):
    from secretary.interface.team_auth import validate_init_data

    c = case
    token = 'SYNTHETIC_MAX_TOKEN'
    fields = {'auth_date': str(int(c.clock[0].timestamp())),
        'user': json.dumps({'id': int(c.member.max_user_id)}, separators=(',', ':'))}
    data = '\n'.join(key + '=' + fields[key] for key in sorted(fields))
    key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    raw = '&'.join(quote(key, safe='') + '=' + quote(value, safe='') for key, value in fields.items())
    verified = validate_init_data(raw, bot_token=token, bot_id='42', now=c.clock[0])
    first = c.auth.login_max(verified)
    restarted = c.a.AuthRepository(c.a.TeamDatabase(c.db.path),
        secret=b'ROTATED_SYNTHETIC_AUTH_STORAGE_KEY_32BYTES', clock=lambda: c.clock[0])
    with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
        restarted.authenticate(first.token)
    same = validate_init_data(raw, bot_token=token, bot_id='42', now=c.clock[0])
    assert same.replay_key == verified.replay_key
    with pytest.raises(c.a.AuthError, match='auth_replay'):
        restarted.login_max(same)
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_auth_sessions').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM team_auth_replays').fetchone()[0] == 1


def test_unknown_or_disabled_max_mapping_never_creates_session(case):
    c = case
    with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
        c.auth.login_max(identity(c, '999'))
    c.team.upsert_member(c.member.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
        c.auth.login_max(identity(c, replay='c' * 64))


def test_session_idle_slides_only_after_valid_auth_and_absolute_expiry_never_moves(case):
    c = case
    grant = c.auth.login_max(identity(c))
    for _ in range(23):
        c.clock[0] += timedelta(minutes=29)
        assert c.auth.authenticate(grant.token) == c.member
    c.clock[0] = grant.expires_at
    with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
        c.auth.authenticate(grant.token)


def test_invalid_csrf_does_not_slide_idle_expiry_and_logout_requires_atomic_csrf(case):
    c = case
    grant = c.auth.login_max(identity(c))
    c.clock[0] += timedelta(minutes=29)
    for call in (lambda: c.auth.authenticate(grant.token, csrf='bad', require_csrf=True),
                 lambda: c.auth.logout(grant.token, 'bad')):
        with pytest.raises(c.a.AuthError, match='auth_csrf_invalid'):
            call()
    c.clock[0] += timedelta(minutes=2)
    with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
        c.auth.authenticate(grant.token)


def test_logout_revokes_exact_session_and_member_revision_change_revokes_other_session(case):
    c = case
    first = c.auth.login_max(identity(c))
    second = c.auth.login_max(identity(c, replay='c' * 64))
    c.auth.logout(first.token, first.csrf)
    with pytest.raises(c.a.AuthError):
        c.auth.authenticate(first.token)
    assert c.auth.authenticate(second.token) == c.member
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'display_name': 'Renamed'}), expected_revision=0)
    with pytest.raises(c.a.AuthError):
        c.auth.authenticate(second.token)


def test_code_is_copyable_bound_to_one_challenge_and_consumed_once_under_race(case):
    c = case
    code = c.auth.issue_code(c.member.max_user_id)
    assert re.fullmatch(r'[0-9a-f]{32}\.[A-Z2-7]{10}', code.value)
    assert code.expires_at == c.clock[0] + timedelta(minutes=5)
    def consume(_):
        try:
            return c.auth.consume_code(code.value)
        except c.a.AuthError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, range(2)))
    assert sum(isinstance(value, c.a.SessionGrant) for value in results) == 1
    assert results.count('auth_invalid_credentials') == 1


def test_malformed_suffix_consumes_attempts_and_five_failures_stay_locked_after_restart(case):
    c = case
    code = c.auth.issue_code(c.member.max_user_id)
    challenge = code.value.split('.')[0]
    for value in (challenge, challenge + '.!', challenge + '.TOO_LONG_NOT_CODE', challenge + '.123', challenge + '.a' * 10):
        with pytest.raises(c.a.AuthError):
            c.auth.consume_code(value)
    restarted = c.a.AuthRepository(c.a.TeamDatabase(c.db.path), secret=b'SYNTHETIC_STORAGE_KEY_32_BYTES_ONLY', clock=lambda: c.clock[0])
    with pytest.raises(c.a.AuthError):
        restarted.consume_code(code.value)
    with c.db.connection() as conn:
        assert conn.execute('SELECT attempts FROM team_auth_codes WHERE id=?', (challenge,)).fetchone()[0] == 5


def test_new_code_revokes_old_and_expiry_is_exact(case):
    c = case
    old = c.auth.issue_code(c.member.max_user_id)
    new = c.auth.issue_code(c.member.max_user_id)
    with pytest.raises(c.a.AuthError):
        c.auth.consume_code(old.value)
    c.clock[0] = new.expires_at
    with pytest.raises(c.a.AuthError):
        c.auth.consume_code(new.value)


def test_invitation_pins_candidate_without_granting_membership_and_owner_can_confirm_owner(case):
    c = case
    invite = c.auth.create_invitation(c.owner.id, ('7',))
    assert len(invite.value) >= 43 and invite.expires_at == c.clock[0] + timedelta(hours=24)
    view = c.auth.claim_invitation(invite.value, '333')
    assert view.candidate_user_id == '333' and view.confirmed_member_id is None
    assert c.auth.claim_invitation(invite.value, '333') == view
    with pytest.raises(c.a.AuthError, match='auth_forbidden'):
        c.auth.claim_invitation(invite.value, '444')
    assert c.team.resolve_member('333') is None
    person = invited_member(c, role='owner')
    assert c.auth.confirm_invitation(c.owner.id, invite.id, person) == person
    assert c.auth.read_invitation(c.owner.id, invite.id).confirmed_member_id == person.id
    assert c.auth.login_max(identity(c, '333')).member == person


@pytest.mark.parametrize('change', ['max', 'projects', 'revision', 'existing-id', 'vikunja-id', 'disabled'])
def test_confirmation_cannot_change_bound_identity_scope_or_reuse_existing_member(case, change):
    c = case
    invite = c.auth.create_invitation(c.owner.id, ('7',))
    c.auth.claim_invitation(invite.value, '333')
    edits = {'max': {'max_user_id': '444'}, 'projects': {'project_ids': ('7', '8')},
        'revision': {'revision': 1}, 'existing-id': {'id': c.member.id},
        'vikunja-id': {'vikunja_user_id': c.member.vikunja_user_id}, 'disabled': {'enabled': False}}[change]
    with pytest.raises(c.a.AuthError):
        c.auth.confirm_invitation(c.owner.id, invite.id, invited_member(c, **edits))
    assert c.team.resolve_member('333') is None


def test_current_creator_revision_and_scope_are_checked_on_claim_and_admin(case):
    c = case
    invite = c.auth.create_invitation(c.owner.id, ('7',))
    c.team.upsert_member(c.owner.model_copy(update={'revision': 1, 'project_ids': ('8',)}), expected_revision=0)
    for call in (lambda: c.auth.claim_invitation(invite.value, '333'),
                 lambda: c.auth.read_invitation(c.owner.id, invite.id),
                 lambda: c.auth.confirm_invitation(c.owner.id, invite.id, invited_member(c)),
                 lambda: c.auth.revoke_invitation(c.owner.id, invite.id)):
        with pytest.raises(c.a.AuthError, match='auth_forbidden'):
            call()


def test_nonowner_or_crossowner_or_foreign_project_cannot_manage_invitation(case):
    c = case
    for actor, projects in ((c.member.id, ('7',)), (c.owner.id, ('8',))):
        with pytest.raises(c.a.AuthError, match='auth_forbidden'):
            c.auth.create_invitation(actor, projects)
    invite = c.auth.create_invitation(c.owner.id, ('7',))
    other = invited_member(c, max_id='444', role='owner')
    c.team.upsert_member(other, expected_revision=None)
    with pytest.raises(c.a.AuthError, match='auth_forbidden'):
        c.auth.read_invitation(other.id, invite.id)
    c.auth.revoke_invitation(c.owner.id, invite.id)
    assert c.auth.read_invitation(c.owner.id, invite.id).revoked
    with pytest.raises(c.a.AuthError):
        c.auth.claim_invitation(invite.value, '333')


def test_revocation_is_fenced_and_revokes_sessions_and_codes_in_same_transaction(case):
    c = case
    grant = c.auth.login_max(identity(c))
    code = c.auth.issue_code(c.member.max_user_id)
    revoked = c.auth.revoke_member(c.owner.id, c.member.id, 0)
    assert not revoked.enabled and revoked.revision == 1
    for call in (lambda: c.auth.authenticate(grant.token), lambda: c.auth.consume_code(code.value),
                 lambda: c.auth.revoke_member(c.owner.id, c.member.id, 0)):
        with pytest.raises(c.a.AuthError):
            call()


def test_rate_denials_are_durable_after_exception_and_restart_and_windows_are_independent(case):
    c = case
    for _ in range(2):
        c.auth.check_rate('login', '127.0.0.1', limit=2, window_seconds=60)
    for _ in range(2):
        with pytest.raises(c.a.AuthError, match='auth_rate_limited'):
            c.auth.check_rate('login', '127.0.0.1', limit=2, window_seconds=60)
    restarted = c.a.AuthRepository(c.a.TeamDatabase(c.db.path), secret=b'SYNTHETIC_STORAGE_KEY_32_BYTES_ONLY', clock=lambda: c.clock[0])
    with pytest.raises(c.a.AuthError, match='auth_rate_limited'):
        restarted.check_rate('login', '127.0.0.1', limit=2, window_seconds=60)
    restarted.check_rate('login', '127.0.0.2', limit=2, window_seconds=60)
    c.clock[0] += timedelta(seconds=60)
    restarted.check_rate('login', '127.0.0.1', limit=2, window_seconds=60)


def test_database_and_auth_journal_never_store_raw_credentials_or_signatures(case):
    c = case
    grant = c.auth.login_max(identity(c))
    code = c.auth.issue_code(c.member.max_user_id)
    invite = c.auth.create_invitation(c.owner.id, ('7',))
    with c.db.connection() as conn:
        serialized = '\n'.join(str(tuple(row)) for table in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'team_auth_%'")
            for row in conn.execute('SELECT * FROM ' + table['name']))
    for secret in (grant.token, grant.csrf, code.value, code.value.split('.')[1], invite.value, 'a' * 64, 'b' * 64):
        assert secret not in serialized


@pytest.mark.parametrize('version', [1, 2])
def test_real_previous_schema_upgrade_preserves_members_and_replace_guards(tmp_path, version):
    a = api()
    schema = import_module('secretary.infrastructure.team_database')
    path = tmp_path / ('legacy-' + str(version) + '.sqlite3')
    member = a.TeamMember(id=uid(), display_name='Legacy synthetic member', max_user_id='111',
                          vikunja_user_id='11', project_ids=('7',))
    with sqlite3.connect(path) as conn:
        for sql in schema.SCHEMA:
            conn.execute(sql)
        conn.execute('INSERT INTO team_schema VALUES(1)')
        if version == 2:
            for sql in schema.INSERT_GUARDS:
                conn.execute(sql)
            conn.execute('INSERT INTO team_schema VALUES(2)')
        conn.execute('INSERT INTO team_members VALUES(?,?,?,?,?)', (member.id, member.max_user_id,
            member.vikunja_user_id, member.revision, a.canonical(member.model_dump(mode='json'))))
    db = a.TeamDatabase(path)
    assert a.TeamRepository(db).resolve_member('111') == member
    with db.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 9
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        guards = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert {'team_commands_immutable_insert', 'team_messages_immutable_insert',
                'team_auth_journal_replace_guard', 'team_auth_invitation_binding'} <= guards
    a.TeamDatabase(path)


def test_auth_migration_failure_rolls_back_all_statements_and_keeps_previous_version(tmp_path, monkeypatch):
    a = api()
    schema = import_module('secretary.infrastructure.team_database')
    auth = import_module('secretary.infrastructure.team_auth_repository')
    path = tmp_path / 'legacy-rollback.sqlite3'
    with sqlite3.connect(path) as conn:
        for sql in (*schema.SCHEMA, *schema.INSERT_GUARDS):
            conn.execute(sql)
        conn.executemany('INSERT INTO team_schema VALUES(?)', [(1,), (2,)])
    with monkeypatch.context() as patch:
        patch.setattr(auth, 'AUTH_MIGRATION', (*auth.AUTH_MIGRATION, 'INVALID MIGRATION STATEMENT'))
        with pytest.raises(sqlite3.OperationalError):
            a.TeamDatabase(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 2
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'team_auth_%'").fetchall() == []
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='team_commands_immutable_insert'").fetchone()
    a.TeamDatabase(path)


def test_concurrent_wrong_codes_cannot_reset_or_exceed_five_attempts(case):
    c = case
    code = c.auth.issue_code(c.member.max_user_id)
    bad = code.value.split('.')[0] + '.!'
    def consume(_):
        with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
            c.auth.consume_code(bad)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(consume, range(12)))
    with c.db.connection() as conn:
        row = conn.execute('SELECT attempts,revoked FROM team_auth_codes').fetchone()
        assert tuple(row) == (5, 1)
        assert conn.execute('SELECT COUNT(*) FROM team_auth_sessions').fetchone()[0] == 0
    with pytest.raises(c.a.AuthError):
        c.auth.consume_code(code.value)


def test_concurrent_aggregate_rate_limit_has_exactly_limit_successes(case):
    c = case
    def check(_):
        try:
            c.auth.check_rate('login_max', 'all', limit=3, window_seconds=60)
            return True
        except c.a.AuthError as error:
            assert error.code == 'auth_rate_limited'
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(check, range(9))).count(True) == 3
    with c.db.connection() as conn:
        assert conn.execute('SELECT attempts FROM team_auth_rates').fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM team_auth_journal WHERE event='rate_denied'").fetchone()[0] == 6


def test_confirmation_race_is_idempotent_and_reserves_disabled_identity(case):
    c = case
    invitation = c.auth.create_invitation(c.owner.id, ('7',))
    c.auth.claim_invitation(invitation.value, '333')
    member = invited_member(c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: c.auth.confirm_invitation(c.owner.id, invitation.id, member), range(2)))
    assert results == [member, member]
    with c.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM team_auth_journal WHERE event='invite_confirmed'").fetchone()[0] == 1
    c.auth.revoke_member(c.owner.id, member.id, 0)
    second = c.auth.create_invitation(c.owner.id, ('7',))
    c.auth.claim_invitation(second.value, '333')
    with pytest.raises(c.a.AuthError, match='auth_conflict'):
        c.auth.confirm_invitation(c.owner.id, second.id, invited_member(c, vikunja_user_id='44'))


def test_expired_invitation_cannot_claim_or_confirm_and_claim_does_not_create_session(case):
    c = case
    invitation = c.auth.create_invitation(c.owner.id, ('7',))
    c.auth.claim_invitation(invitation.value, '333')
    c.clock[0] = invitation.expires_at
    for call in (lambda: c.auth.claim_invitation(invitation.value, '333'),
                 lambda: c.auth.confirm_invitation(c.owner.id, invitation.id, invited_member(c))):
        with pytest.raises(c.a.AuthError, match='auth_forbidden'):
            call()
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_auth_sessions').fetchone()[0] == 0


def test_member_payload_corruption_is_fail_closed_for_session_and_code(case):
    c = case
    grant = c.auth.login_max(identity(c))
    code = c.auth.issue_code(c.member.max_user_id)
    with c.db.connection() as conn:
        conn.execute('UPDATE team_members SET revision=9 WHERE id=?', (c.member.id,))
    for call in (lambda: c.auth.authenticate(grant.token), lambda: c.auth.consume_code(code.value)):
        with pytest.raises(c.a.AuthError, match='auth_unavailable'):
            call()
    with c.db.connection() as conn:
        assert conn.execute('SELECT attempts FROM team_auth_codes').fetchone()[0] == 1
    value = identity(c, replay='c' * 64)
    with pytest.raises(c.a.AuthError, match='auth_unavailable'):
        c.auth.login_max(value)
    with pytest.raises(c.a.AuthError, match='auth_replay'):
        c.auth.login_max(value)


@pytest.mark.parametrize('peer', [None, 123, True, b'127.0.0.1', 'X-Forwarded-For: 127.0.0.1'])
def test_rate_requires_actual_string_ip_or_explicit_aggregate_peer(case, peer):
    with pytest.raises(ValueError, match='auth_rate_requires_actual_ip'):
        case.auth.check_rate('login', peer, limit=2, window_seconds=60)


def test_auth_sql_bindings_and_replay_journal_cannot_be_replaced_or_rewound(case):
    c = case
    grant = c.auth.login_max(identity(c))
    code = c.auth.issue_code(c.member.max_user_id)
    invitation = c.auth.create_invitation(c.owner.id, ('7',))
    c.auth.claim_invitation(invitation.value, '333')
    c.auth.logout(grant.token, grant.csrf)
    with pytest.raises(c.a.AuthError):
        c.auth.consume_code(code.value.split('.')[0] + '.!')
    with c.db.connection() as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        for table in ('team_auth_replays', 'team_auth_sessions', 'team_auth_codes',
                      'team_auth_invitations', 'team_auth_journal'):
            row = tuple(conn.execute('SELECT * FROM ' + table).fetchone())
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('INSERT OR REPLACE INTO ' + table + ' VALUES(' + ','.join('?' for _ in row) + ')', row)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute('DELETE FROM ' + table)
        for sql in ("UPDATE team_auth_replays SET user_id='999'",
                    "UPDATE team_auth_journal SET event='login_denied'",
                    'UPDATE team_auth_sessions SET revoked=0',
                    'UPDATE team_auth_sessions SET absolute_until=absolute_until+1',
                    'UPDATE team_auth_codes SET attempts=0',
                    "UPDATE team_auth_invitations SET candidate_user_id='999'",
                    'UPDATE team_auth_invitations SET owner_revision=owner_revision+1'):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
