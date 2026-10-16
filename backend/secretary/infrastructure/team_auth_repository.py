"""Local Team authentication state; trusted MAX verification happens upstream.

No bearer, one-time code, invitation value, initData or MAX signature is stored.
Expected failures which consume attempts/replays/rate allowance are raised only
after their transaction commits. This module performs no HTTP or setup actions.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
from uuid import uuid4

from pydantic import ValidationError

from secretary.domain.team import TeamMember, canonical, content_hash, uuid_string
from secretary.domain.team_auth import (
    AuthError, DesktopCode, InvitationGrant, InvitationView, MaxIdentity, SessionGrant,
)


AUTH_MIGRATION = (
    '''CREATE TABLE team_auth_replays(replay_hash TEXT PRIMARY KEY NOT NULL CHECK(length(replay_hash)=64),
        user_id TEXT NOT NULL,used_at REAL NOT NULL)''',
    '''CREATE TABLE team_auth_sessions(id TEXT PRIMARY KEY NOT NULL,token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64),
        member_id TEXT NOT NULL REFERENCES team_members(id),member_revision INTEGER NOT NULL CHECK(typeof(member_revision)='integer' AND member_revision>=0),
        created_at REAL NOT NULL,absolute_until REAL NOT NULL,idle_until REAL NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN(0,1)),
        CHECK(absolute_until>created_at AND idle_until<=absolute_until))''',
    '''CREATE TABLE team_auth_codes(id TEXT PRIMARY KEY NOT NULL,member_id TEXT NOT NULL REFERENCES team_members(id),
        member_revision INTEGER NOT NULL CHECK(typeof(member_revision)='integer' AND member_revision>=0),
        code_hash TEXT NOT NULL CHECK(length(code_hash)=64),created_at REAL NOT NULL,expires_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(typeof(attempts)='integer' AND attempts BETWEEN 0 AND 5),
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN(0,1)),used_at REAL,
        CHECK(expires_at>created_at),CHECK(used_at IS NULL OR attempts>=1))''',
    '''CREATE TABLE team_auth_invitations(id TEXT PRIMARY KEY NOT NULL,value_hash TEXT NOT NULL UNIQUE CHECK(length(value_hash)=64),
        owner_id TEXT NOT NULL REFERENCES team_members(id),owner_revision INTEGER NOT NULL CHECK(typeof(owner_revision)='integer' AND owner_revision>=0),
        project_ids TEXT NOT NULL CHECK(json_valid(project_ids) AND json_type(project_ids)='array' AND json_array_length(project_ids)>0),
        created_at REAL NOT NULL,expires_at REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN(0,1)),
        candidate_user_id TEXT,confirmed_member_id TEXT REFERENCES team_members(id),confirmation_hash TEXT,
        CHECK(expires_at>created_at),
        CHECK((confirmed_member_id IS NULL)=(confirmation_hash IS NULL)),
        CHECK(confirmed_member_id IS NULL OR candidate_user_id IS NOT NULL))''',
    '''CREATE TABLE team_auth_rates(endpoint TEXT NOT NULL,peer_hash TEXT NOT NULL CHECK(length(peer_hash)=64),
        window_start INTEGER NOT NULL,window_seconds INTEGER NOT NULL CHECK(typeof(window_seconds)='integer' AND window_seconds>0),
        attempts INTEGER NOT NULL CHECK(typeof(attempts)='integer' AND attempts>0),
        PRIMARY KEY(endpoint,peer_hash,window_start,window_seconds))''',
    '''CREATE TABLE team_auth_journal(id TEXT PRIMARY KEY NOT NULL,event TEXT NOT NULL,
        actor_id TEXT,subject_id TEXT,code TEXT,created_at REAL NOT NULL,
        CHECK(event IN ('max_login','max_replay','login_denied','session_created','session_expired','session_revoked',
        'csrf_denied','code_issued','code_denied','code_consumed','invite_created','invite_claimed',
        'invite_confirmed','invite_revoked','member_revoked','rate_denied')),
        CHECK(code IS NULL OR code IN ('auth_invalid_credentials','auth_replay','auth_csrf_invalid',
        'auth_forbidden','auth_conflict','auth_rate_limited','auth_unavailable')))''',
    'CREATE INDEX team_auth_session_member ON team_auth_sessions(member_id,revoked)',
    'CREATE INDEX team_auth_code_member ON team_auth_codes(member_id,revoked)',
    *tuple(f'''CREATE TRIGGER {table}_immutable_{verb.lower()} BEFORE {verb} ON {table}
        BEGIN SELECT RAISE(ABORT,'Immutable auth evidence'); END'''
        for table in ('team_auth_replays', 'team_auth_journal') for verb in ('UPDATE', 'DELETE')),
    *tuple(f'''CREATE TRIGGER {table}_replace_guard BEFORE INSERT ON {table}
        WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition})
        BEGIN SELECT RAISE(ABORT,'Auth identity cannot be replaced'); END'''
        for table, condition in (
            ('team_auth_replays', 'replay_hash=NEW.replay_hash'),
            ('team_auth_journal', 'id=NEW.id'),
            ('team_auth_sessions', 'id=NEW.id OR token_hash=NEW.token_hash'),
            ('team_auth_codes', 'id=NEW.id'),
            ('team_auth_invitations', 'id=NEW.id OR value_hash=NEW.value_hash'))),
    *tuple(f'''CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT,'Auth identity cannot be deleted'); END'''
        for table in ('team_auth_sessions', 'team_auth_codes', 'team_auth_invitations')),
    '''CREATE TRIGGER team_auth_session_binding BEFORE UPDATE ON team_auth_sessions
        WHEN NEW.id IS NOT OLD.id OR NEW.token_hash IS NOT OLD.token_hash OR NEW.member_id IS NOT OLD.member_id
          OR NEW.member_revision!=OLD.member_revision OR NEW.created_at!=OLD.created_at
          OR NEW.absolute_until!=OLD.absolute_until OR NEW.idle_until<OLD.idle_until OR NEW.revoked<OLD.revoked
        BEGIN SELECT RAISE(ABORT,'Session binding is immutable'); END''',
    '''CREATE TRIGGER team_auth_code_binding BEFORE UPDATE ON team_auth_codes
        WHEN NEW.id IS NOT OLD.id OR NEW.member_id IS NOT OLD.member_id OR NEW.member_revision!=OLD.member_revision
          OR NEW.code_hash IS NOT OLD.code_hash OR NEW.created_at!=OLD.created_at OR NEW.expires_at!=OLD.expires_at
          OR NEW.attempts<OLD.attempts OR NEW.revoked<OLD.revoked OR (OLD.used_at IS NOT NULL AND NEW.used_at IS NOT OLD.used_at)
        BEGIN SELECT RAISE(ABORT,'Challenge binding is immutable'); END''',
    '''CREATE TRIGGER team_auth_invitation_binding BEFORE UPDATE ON team_auth_invitations
        WHEN NEW.id IS NOT OLD.id OR NEW.value_hash IS NOT OLD.value_hash OR NEW.owner_id IS NOT OLD.owner_id
          OR NEW.owner_revision!=OLD.owner_revision OR NEW.project_ids IS NOT OLD.project_ids
          OR NEW.created_at!=OLD.created_at OR NEW.expires_at!=OLD.expires_at OR NEW.revoked<OLD.revoked
          OR (OLD.candidate_user_id IS NOT NULL AND NEW.candidate_user_id IS NOT OLD.candidate_user_id)
          OR (OLD.confirmed_member_id IS NOT NULL AND NEW.confirmed_member_id IS NOT OLD.confirmed_member_id)
          OR (OLD.confirmation_hash IS NOT NULL AND NEW.confirmation_hash IS NOT OLD.confirmation_hash)
        BEGIN SELECT RAISE(ABORT,'Invitation binding is immutable'); END''',
)


def _id(value):
    try:
        return uuid_string(value)
    except ValueError:
        raise AuthError('auth_forbidden') from None


def _max_id(value):
    if (not isinstance(value, str) or not re.fullmatch('[1-9][0-9]{0,18}', value)
            or int(value) > 2 ** 63 - 1):
        raise AuthError('auth_invalid_credentials')
    return value


def _projects(value):
    if (not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 100
            or any(not isinstance(item, str) or not re.fullmatch('[1-9][0-9]{0,127}', item) for item in value)
            or len(set(value)) != len(value)):
        raise AuthError('auth_forbidden')
    return tuple(sorted(value))


class AuthRepository:
    def __init__(self, db, *, secret: bytes, clock=None):
        if type(secret) is not bytes or len(secret) < 32:
            raise ValueError('auth_secret_requires_32_bytes')
        self.db, self._secret = db, secret
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('clock_requires_timezone')
        return value.astimezone(timezone.utc)

    def _hash(self, kind, value):
        return hmac.new(self._secret, (kind + ':' + value).encode('utf-8'), hashlib.sha256).hexdigest()

    @staticmethod
    def _token(value):
        if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]{43}', value):
            raise AuthError('auth_invalid_credentials')
        return value

    @staticmethod
    def _member(conn, *, identifier=None, max_id=None):
        row = conn.execute('SELECT * FROM team_members WHERE id=?' if identifier is not None else
            'SELECT * FROM team_members WHERE max_user_id=?', (identifier if identifier is not None else max_id,)).fetchone()
        if row is None:
            return None
        try:
            member = TeamMember.model_validate_json(row['payload'])
        except (ValueError, ValidationError):
            raise AuthError('auth_unavailable') from None
        if (member.id, member.max_user_id, member.vikunja_user_id, member.revision) != (
                row['id'], row['max_user_id'], row['vikunja_user_id'], row['revision']):
            raise AuthError('auth_unavailable')
        return member

    def _owner(self, conn, owner_id, projects):
        owner = self._member(conn, identifier=owner_id)
        if not owner or not owner.enabled or owner.role != 'owner' or not set(projects) <= set(owner.project_ids):
            raise AuthError('auth_forbidden')
        return owner

    def _journal(self, conn, event, *, actor=None, subject=None, code=None):
        conn.execute('INSERT INTO team_auth_journal VALUES(?,?,?,?,?,?)',
            (str(uuid4()), event, actor, subject, code, self._now().timestamp()))

    def csrf_token(self, token):
        """Internal derivation; public callers must authenticate before returning it."""
        return self._hash('csrf', self._token(token))

    def _mint(self, conn, member):
        stamp, identifier, token = self._now(), str(uuid4()), secrets.token_urlsafe(32)
        expires = stamp + timedelta(hours=12)
        conn.execute('INSERT INTO team_auth_sessions VALUES(?,?,?,?,?,?,?,0)',
            (identifier, self._hash('session', token), member.id, member.revision, stamp.timestamp(),
             expires.timestamp(), (stamp + timedelta(minutes=30)).timestamp()))
        self._journal(conn, 'session_created', actor=member.id, subject=identifier)
        return SessionGrant(token=token, csrf=self.csrf_token(token), expires_at=expires, member=member)

    def login_max(self, identity: MaxIdentity):
        """Trusted port: only the verified initData result belongs here."""
        if not isinstance(identity, MaxIdentity):
            raise AuthError('auth_invalid_credentials')
        identity = MaxIdentity(identity.user_id, identity.auth_date, identity.signature_hex, identity.replay_key)
        # Canonical MAX replay identity must survive session-secret rotation.
        # Store only a digest, independent of the local session HMAC secret.
        replay = hashlib.sha256(identity.replay_key.encode('ascii')).hexdigest()
        result, error = None, None
        with self.db.transaction() as conn:
            if conn.execute('SELECT 1 FROM team_auth_replays WHERE replay_hash=?', (replay,)).fetchone():
                error = 'auth_replay'
                self._journal(conn, 'max_replay', subject=identity.user_id, code=error)
            else:
                conn.execute('INSERT INTO team_auth_replays VALUES(?,?,?)', (replay, identity.user_id, self._now().timestamp()))
                try:
                    member = self._member(conn, max_id=identity.user_id)
                except AuthError as failure:
                    # Corrupt identity data must deny login without rolling back
                    # the already observed cryptographic replay key.
                    member, error = None, failure.code
                if not member or not member.enabled:
                    error = error or 'auth_invalid_credentials'
                    self._journal(conn, 'login_denied', subject=identity.user_id, code=error)
                else:
                    result = self._mint(conn, member)
                    self._journal(conn, 'max_login', actor=member.id)
        if error:
            raise AuthError(error)
        return result

    def _session(self, conn, token, csrf, require_csrf, touch):
        row = conn.execute('SELECT * FROM team_auth_sessions WHERE token_hash=?', (self._hash('session', token),)).fetchone()
        if row is None or row['revoked']:
            return None, row, 'auth_invalid_credentials'
        stamp = self._now().timestamp()
        member = self._member(conn, identifier=row['member_id'])
        if (not member or not member.enabled or member.revision != row['member_revision']
                or stamp >= row['idle_until'] or stamp >= row['absolute_until']):
            conn.execute('UPDATE team_auth_sessions SET revoked=1 WHERE id=?', (row['id'],))
            self._journal(conn, 'session_expired', actor=row['member_id'], subject=row['id'], code='auth_invalid_credentials')
            return None, row, 'auth_invalid_credentials'
        if require_csrf and (not isinstance(csrf, str) or not re.fullmatch('[0-9a-f]{64}', csrf)
                             or not hmac.compare_digest(csrf, self.csrf_token(token))):
            self._journal(conn, 'csrf_denied', actor=member.id, subject=row['id'], code='auth_csrf_invalid')
            return None, row, 'auth_csrf_invalid'
        if touch:
            idle = max(row['idle_until'], min(stamp + 30 * 60, row['absolute_until']))
            conn.execute('UPDATE team_auth_sessions SET idle_until=? WHERE id=?', (idle, row['id']))
        return member, row, None

    def authenticate(self, token, *, csrf=None, require_csrf=False):
        token = self._token(token)
        if type(require_csrf) is not bool:
            raise AuthError('auth_invalid_credentials')
        with self.db.transaction() as conn:
            member, _, error = self._session(conn, token, csrf, require_csrf, True)
        if error:
            raise AuthError(error)
        return member

    def logout(self, token, csrf):
        token = self._token(token)
        with self.db.transaction() as conn:
            member, row, error = self._session(conn, token, csrf, True, False)
            if error is None:
                conn.execute('UPDATE team_auth_sessions SET revoked=1 WHERE id=?', (row['id'],))
                self._journal(conn, 'session_revoked', actor=member.id, subject=row['id'])
        if error:
            raise AuthError(error)

    def issue_code(self, max_user_id):
        """Trusted future authenticated MAX-event port, never a public API action."""
        max_user_id = _max_id(max_user_id)
        with self.db.transaction() as conn:
            member = self._member(conn, max_id=max_user_id)
            if not member or not member.enabled:
                raise AuthError('auth_invalid_credentials')
            stamp = self._now()
            identifier = secrets.token_hex(16)
            code = base64.b32encode(secrets.token_bytes(7)).decode('ascii')[:10]
            conn.execute('UPDATE team_auth_codes SET revoked=1 WHERE member_id=? AND revoked=0', (member.id,))
            conn.execute('''INSERT INTO team_auth_codes(id,member_id,member_revision,code_hash,created_at,expires_at)
                VALUES(?,?,?,?,?,?)''', (identifier, member.id, member.revision,
                self._hash('code', identifier + ':' + code), stamp.timestamp(), (stamp + timedelta(minutes=5)).timestamp()))
            self._journal(conn, 'code_issued', actor=member.id, subject=identifier)
            return DesktopCode(identifier + '.' + code, stamp + timedelta(minutes=5))

    def consume_code(self, value):
        # Parsing intentionally permits an existing challenge ID with ANY suffix
        # to reach its attempt counter. The code secret itself has a strict form.
        if not isinstance(value, str):
            raise AuthError('auth_invalid_credentials')
        identifier, _, code = value.partition('.')
        if not re.fullmatch('[0-9a-f]{32}', identifier):
            raise AuthError('auth_invalid_credentials')
        result, error = None, 'auth_invalid_credentials'
        with self.db.transaction() as conn:
            row = conn.execute('SELECT * FROM team_auth_codes WHERE id=?', (identifier,)).fetchone()
            if row and not row['revoked'] and row['used_at'] is None and row['attempts'] < 5:
                attempts = row['attempts'] + 1
                conn.execute('UPDATE team_auth_codes SET attempts=? WHERE id=?', (attempts, identifier))
                try:
                    member = self._member(conn, identifier=row['member_id'])
                except AuthError as failure:
                    # The attempted challenge stays consumed even when the
                    # member record is corrupt. No session is minted.
                    member, error = None, failure.code
                valid = (self._now().timestamp() < row['expires_at'] and member is not None and member.enabled
                         and member.revision == row['member_revision'] and len(code) == 10
                         and re.fullmatch('[A-Z2-7]{10}', code) is not None
                         and hmac.compare_digest(row['code_hash'], self._hash('code', identifier + ':' + code)))
                if valid:
                    conn.execute('UPDATE team_auth_codes SET used_at=? WHERE id=?', (self._now().timestamp(), identifier))
                    result, error = self._mint(conn, member), None
                    self._journal(conn, 'code_consumed', actor=member.id, subject=identifier)
                elif attempts == 5 or self._now().timestamp() >= row['expires_at'] or not member or not member.enabled or member.revision != row['member_revision']:
                    conn.execute('UPDATE team_auth_codes SET revoked=1 WHERE id=?', (identifier,))
            if error:
                self._journal(conn, 'code_denied', subject=identifier, code=error)
        if error:
            raise AuthError(error)
        return result

    def create_invitation(self, owner_id, project_ids):
        owner_id, projects = _id(owner_id), _projects(project_ids)
        with self.db.transaction() as conn:
            owner = self._owner(conn, owner_id, projects)
            identifier, value, stamp = str(uuid4()), secrets.token_urlsafe(32), self._now()
            expires = stamp + timedelta(hours=24)
            conn.execute('''INSERT INTO team_auth_invitations(id,value_hash,owner_id,owner_revision,project_ids,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?)''', (identifier, self._hash('invitation', value), owner.id,
                owner.revision, canonical(projects), stamp.timestamp(), expires.timestamp()))
            self._journal(conn, 'invite_created', actor=owner.id, subject=identifier)
            return InvitationGrant(identifier, value, expires)

    def _invitation(self, conn, invitation_id=None, value=None, owner_id=None):
        row = conn.execute('SELECT * FROM team_auth_invitations WHERE id=?' if invitation_id else
            'SELECT * FROM team_auth_invitations WHERE value_hash=?',
            (invitation_id if invitation_id else self._hash('invitation', value),)).fetchone()
        if row is None or owner_id is not None and row['owner_id'] != owner_id:
            raise AuthError('auth_forbidden')
        try:
            projects = _projects(json.loads(row['project_ids']))
        except (ValueError, TypeError):
            raise AuthError('auth_unavailable') from None
        owner = self._owner(conn, row['owner_id'], projects)
        if owner.revision != row['owner_revision']:
            raise AuthError('auth_forbidden')
        return row, projects

    @staticmethod
    def _view(row, projects):
        return InvitationView(id=row['id'], project_ids=projects, candidate_user_id=row['candidate_user_id'],
            expires_at=datetime.fromtimestamp(row['expires_at'], timezone.utc),
            confirmed_member_id=row['confirmed_member_id'], revoked=bool(row['revoked']))

    def claim_invitation(self, value, max_user_id):
        """Trusted first-start MAX event pins identity; this never creates access."""
        with self.db.transaction() as conn:
            return self.claim_invitation_in_transaction(conn, value, max_user_id)

    def claim_invitation_in_transaction(self, conn, value, max_user_id):
        """Trusted webhook intake atomically pins candidate and sanitized inbox event."""
        if not conn.in_transaction or conn.execute('PRAGMA database_list').fetchone()[2] != str(self.db.path):
            raise AuthError('auth_conflict')
        value, max_user_id = self._token(value), _max_id(max_user_id)
        row, projects = self._invitation(conn, value=value)
        if row['revoked'] or self._now().timestamp() >= row['expires_at']:
            raise AuthError('auth_forbidden')
        if row['candidate_user_id'] is not None and row['candidate_user_id'] != max_user_id:
            raise AuthError('auth_forbidden')
        if row['candidate_user_id'] is None:
            conn.execute('UPDATE team_auth_invitations SET candidate_user_id=? WHERE id=?', (max_user_id, row['id']))
            self._journal(conn, 'invite_claimed', subject=row['id'])
            row = conn.execute('SELECT * FROM team_auth_invitations WHERE id=?', (row['id'],)).fetchone()
        return self._view(row, projects)

    def read_invitation(self, owner_id, invitation_id):
        owner_id, invitation_id = _id(owner_id), _id(invitation_id)
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            row, projects = self._invitation(conn, invitation_id=invitation_id, owner_id=owner_id)
            return self._view(row, projects)

    def confirm_invitation(self, owner_id, invitation_id, member: TeamMember):
        owner_id, invitation_id = _id(owner_id), _id(invitation_id)
        try:
            member = TeamMember.model_validate(member.model_dump(mode='json'))
        except (ValueError, AttributeError):
            raise AuthError('auth_conflict') from None
        digest = content_hash(member.model_dump(mode='json'))
        with self.db.transaction() as conn:
            row, projects = self._invitation(conn, invitation_id=invitation_id, owner_id=owner_id)
            if row['revoked']:
                raise AuthError('auth_forbidden')
            if (member.revision != 0 or not member.enabled or member.max_user_id != row['candidate_user_id']
                    or member.project_ids != projects):
                raise AuthError('auth_conflict')
            if row['confirmed_member_id'] is not None:
                stored = self._member(conn, identifier=row['confirmed_member_id'])
                if row['confirmed_member_id'] != member.id or row['confirmation_hash'] != digest or stored != member:
                    raise AuthError('auth_conflict')
                return stored
            if self._now().timestamp() >= row['expires_at']:
                raise AuthError('auth_forbidden')
            # No upsert: an existing UUID, MAX ID or Vikunja ID (even disabled)
            # remains reserved. This insert shares the invitation transaction.
            try:
                conn.execute('INSERT INTO team_members VALUES(?,?,?,?,?)', (member.id, member.max_user_id,
                    member.vikunja_user_id, member.revision, canonical(member.model_dump(mode='json'))))
            except sqlite3.IntegrityError:
                raise AuthError('auth_conflict') from None
            conn.execute('UPDATE team_auth_invitations SET confirmed_member_id=?,confirmation_hash=? WHERE id=?',
                         (member.id, digest, invitation_id))
            self._journal(conn, 'invite_confirmed', actor=owner_id, subject=member.id)
            return member

    def revoke_invitation(self, owner_id, invitation_id):
        owner_id, invitation_id = _id(owner_id), _id(invitation_id)
        with self.db.transaction() as conn:
            row, _ = self._invitation(conn, invitation_id=invitation_id, owner_id=owner_id)
            if not row['revoked']:
                conn.execute('UPDATE team_auth_invitations SET revoked=1 WHERE id=?', (invitation_id,))
                self._journal(conn, 'invite_revoked', actor=owner_id, subject=invitation_id)

    def revoke_member(self, owner_id, target_id, expected_revision):
        owner_id, target_id = _id(owner_id), _id(target_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise AuthError('auth_conflict')
        with self.db.transaction() as conn:
            target = self._member(conn, identifier=target_id)
            if not target or not target.project_ids:
                raise AuthError('auth_forbidden')
            self._owner(conn, owner_id, target.project_ids)
            if target.revision != expected_revision:
                raise AuthError('auth_conflict')
            updated = target.model_copy(update={'enabled': False, 'revision': target.revision + 1})
            conn.execute('UPDATE team_members SET revision=?,payload=? WHERE id=?',
                         (updated.revision, canonical(updated.model_dump(mode='json')), target_id))
            conn.execute('UPDATE team_auth_sessions SET revoked=1 WHERE member_id=?', (target_id,))
            conn.execute('UPDATE team_auth_codes SET revoked=1 WHERE member_id=?', (target_id,))
            self._journal(conn, 'member_revoked', actor=owner_id, subject=target_id)
            return updated

    def check_rate(self, endpoint, peer, *, limit, window_seconds):
        if (not isinstance(endpoint, str) or not re.fullmatch('[a-zA-Z0-9_/-]{1,64}', endpoint)
                or type(limit) is not int or not 1 <= limit <= 10000
                or type(window_seconds) is not int or not 1 <= window_seconds <= 86400):
            raise ValueError('invalid_auth_rate_configuration')
        try:
            if not isinstance(peer, str):
                raise ValueError('peer_type')
            peer = 'all' if peer == 'all' else ipaddress.ip_address(peer).compressed
        except ValueError:
            raise ValueError('auth_rate_requires_actual_ip') from None
        stamp = int(self._now().timestamp())
        window = stamp // window_seconds * window_seconds
        digest, denied = self._hash('rate-peer', peer), False
        with self.db.transaction() as conn:
            row = conn.execute('SELECT attempts FROM team_auth_rates WHERE endpoint=? AND peer_hash=? AND window_start=? AND window_seconds=?',
                               (endpoint, digest, window, window_seconds)).fetchone()
            count = row['attempts'] + 1 if row else 1
            conn.execute('''INSERT INTO team_auth_rates VALUES(?,?,?,?,?) ON CONFLICT(endpoint,peer_hash,window_start,window_seconds)
                DO UPDATE SET attempts=excluded.attempts''', (endpoint, digest, window, window_seconds, count))
            denied = count > limit
            if denied:
                self._journal(conn, 'rate_denied', code='auth_rate_limited')
        if denied:
            raise AuthError('auth_rate_limited')
