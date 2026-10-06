"""Durable auth recovery regressions promoted from independent T6 review."""
from datetime import timedelta

import pytest

from test_team_auth_repository import case, identity, invited_member


def reopen(c):
    return c.a.AuthRepository(c.a.TeamDatabase(c.db.path),
        secret=b'SYNTHETIC_STORAGE_KEY_32_BYTES_ONLY', clock=lambda: c.clock[0])


def test_unknown_identity_replay_survives_restart_and_later_membership(case):
    c = case
    signed = identity(c, '333')
    with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
        c.auth.login_max(signed)
    c.team.upsert_member(invited_member(c), expected_revision=None)
    c.auth = reopen(c)
    with pytest.raises(c.a.AuthError, match='auth_replay'):
        c.auth.login_max(signed)
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_auth_sessions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM team_auth_replays').fetchone()[0] == 1


def test_failed_csrf_and_clock_rewind_do_not_reactivate_expired_session(case):
    c = case
    grant = c.auth.login_max(identity(c))
    c.clock[0] += timedelta(minutes=29)
    with pytest.raises(c.a.AuthError, match='auth_csrf_invalid'):
        c.auth.authenticate(grant.token, csrf='0' * 64, require_csrf=True)
    c.clock[0] += timedelta(minutes=1)
    with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
        c.auth.authenticate(grant.token)
    c.clock[0] -= timedelta(minutes=20)
    c.auth = reopen(c)
    with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
        c.auth.authenticate(grant.token, csrf=grant.csrf, require_csrf=True)


def test_repairing_mapping_after_restart_does_not_unlock_consumed_code(case):
    c = case
    code = c.auth.issue_code(c.member.max_user_id)
    with c.db.connection() as conn:
        conn.execute('UPDATE team_members SET revision=9 WHERE id=?', (c.member.id,))
    with pytest.raises(c.a.AuthError, match='auth_unavailable'):
        c.auth.consume_code(code.value)
    with c.db.connection() as conn:
        assert tuple(conn.execute('SELECT attempts,revoked FROM team_auth_codes').fetchone()) == (1, 1)
        conn.execute('UPDATE team_members SET revision=0 WHERE id=?', (c.member.id,))
    c.auth = reopen(c)
    with pytest.raises(c.a.AuthError, match='auth_invalid_credentials'):
        c.auth.consume_code(code.value)
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_auth_sessions').fetchone()[0] == 0
