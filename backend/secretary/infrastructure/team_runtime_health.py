"""Durable, redacted own-subscription/callback observations in the control DB.

No constructor or method performs HTTP. A caller may record a callback only
after authenticating its header and committing normalized BotRepository intake.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
from uuid import UUID

from secretary.domain.team import TeamForbidden, uuid_string
from secretary.domain.team_dashboard import DashboardWebhookStatus


def _identifier(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise ValueError('runtime_health_identity_invalid') from None


def _url(value):
    try:
        url = urlsplit(value)
        if (not isinstance(value, str) or not value.isascii() or url.scheme != 'https'
                or not url.hostname or url.port is not None or url.username or url.password
                or url.path != '/hooks/max' or url.query or url.fragment
                or any(ord(char) <= 32 or ord(char) >= 127 for char in value)):
            raise ValueError
        return hashlib.sha256(value.encode('ascii')).hexdigest()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('runtime_health_subscription_invalid') from None


def _types(values):
    if type(values) is not tuple or not values or len(set(values)) != len(values) or not set(values) <= {'message_created', 'message_callback', 'bot_started'}:
        raise ValueError('runtime_health_subscription_invalid')
    return json.dumps(sorted(values), separators=(',', ':'))


class RuntimeHealthRepository:
    def __init__(self, control_db_path, deployment_id, *, team=None, clock=None):
        self.path, self.deployment_id = Path(control_db_path).resolve(), _identifier(deployment_id)
        self.team, self.clock = team, clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'maintenance_state' not in tables and 'runtime_health_config' not in tables:
                raise ValueError('runtime_health_database_invalid')
            for statement in (
                '''CREATE TABLE IF NOT EXISTS runtime_health_config(deployment_id TEXT PRIMARY KEY,
                    url_hash TEXT NOT NULL,update_types TEXT NOT NULL,configured_at TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS runtime_health_observations(id INTEGER PRIMARY KEY,
                    deployment_id TEXT NOT NULL,state TEXT NOT NULL,observed_at TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS runtime_health_attempts(deployment_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT,started_at TEXT NOT NULL,
                    submitted_at TEXT,completed_at TEXT,PRIMARY KEY(deployment_id,operation_id))''',
                '''CREATE TABLE IF NOT EXISTS runtime_health_callbacks(deployment_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,kind TEXT NOT NULL,accepted_at TEXT NOT NULL,
                    PRIMARY KEY(deployment_id,event_id))''',
                '''CREATE TRIGGER IF NOT EXISTS runtime_health_config_immutable BEFORE UPDATE ON runtime_health_config
                    BEGIN SELECT RAISE(ABORT,'runtime_health_immutable'); END''',
                '''CREATE TRIGGER IF NOT EXISTS runtime_health_config_no_delete BEFORE DELETE ON runtime_health_config
                    BEGIN SELECT RAISE(ABORT,'runtime_health_immutable'); END''',
                '''CREATE TRIGGER IF NOT EXISTS runtime_health_attempt_terminal BEFORE UPDATE ON runtime_health_attempts
                    WHEN OLD.state='completed' OR NEW.deployment_id!=OLD.deployment_id OR NEW.operation_id!=OLD.operation_id
                    OR NEW.started_at!=OLD.started_at BEGIN SELECT RAISE(ABORT,'runtime_health_immutable'); END''',
            ):
                conn.execute(statement)

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('runtime_health_clock_invalid')
        return value.astimezone(timezone.utc).isoformat()

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self.path, timeout=.2, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=200')
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def configure_subscription(self, own_url, update_types):
        digest, types = _url(own_url), _types(update_types)
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM runtime_health_config WHERE deployment_id=?', (self.deployment_id,)).fetchone()
            if row:
                if (row['url_hash'], row['update_types']) != (digest, types):
                    raise ValueError('runtime_health_subscription_conflict')
                return
            conn.execute('INSERT INTO runtime_health_config VALUES(?,?,?,?)', (self.deployment_id, digest, types, self._now()))

    def _config(self, conn):
        row = conn.execute('SELECT * FROM runtime_health_config WHERE deployment_id=?', (self.deployment_id,)).fetchone()
        if row is None:
            raise ValueError('runtime_health_not_configured')
        return row

    def record_subscription(self, state, *, observed_url, update_types=()):
        if state not in {'present', 'missing', 'unavailable'}:
            raise ValueError('runtime_health_state_invalid')
        digest = _url(observed_url)
        with self._transaction() as conn:
            config = self._config(conn)
            if digest != config['url_hash'] or state == 'present' and _types(update_types) != config['update_types']:
                raise ValueError('runtime_health_subscription_conflict')
            conn.execute('INSERT INTO runtime_health_observations(deployment_id,state,observed_at) VALUES(?,?,?)',
                (self.deployment_id, state, self._now()))

    def _active(self, conn):
        return conn.execute('''SELECT * FROM runtime_health_attempts WHERE deployment_id=? AND
            (state!='completed' OR receipt='uncertain') ORDER BY rowid DESC LIMIT 1''', (self.deployment_id,)).fetchone()

    def can_begin_subscription(self):
        with self._transaction() as conn:
            self._config(conn)
            return self._active(conn) is None

    def prepared_attempt(self):
        with self._transaction() as conn:
            row = self._active(conn)
            return row['operation_id'] if row is not None and row['state'] == 'prepared' else None

    def recover_subscription_attempt(self, operation_id):
        """Trusted dead-run recovery for one exact owned outbound ticket UUID."""
        identifier = _identifier(operation_id)
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM runtime_health_attempts WHERE deployment_id=? AND operation_id=?',
                (self.deployment_id, identifier)).fetchone()
            if row is None:
                return {'operation_id': identifier, 'state': 'absent', 'receipt': None}
            if row['state'] == 'submitted':
                stamp = self._now()
                conn.execute("UPDATE runtime_health_attempts SET state='completed',receipt='uncertain',completed_at=? WHERE deployment_id=? AND operation_id=?",
                    (stamp, self.deployment_id, identifier))
                conn.execute('INSERT INTO runtime_health_observations(deployment_id,state,observed_at) VALUES(?,?,?)',
                    (self.deployment_id, 'uncertain', stamp))
                return {'operation_id': identifier, 'state': 'completed', 'receipt': 'uncertain'}
            return {'operation_id': identifier, 'state': row['state'], 'receipt': row['receipt']}

    def begin_subscription_attempt(self, operation_id):
        identifier = _identifier(operation_id)
        with self._transaction() as conn:
            self._config(conn)
            row = conn.execute('SELECT * FROM runtime_health_attempts WHERE deployment_id=? AND operation_id=?',
                (self.deployment_id, identifier)).fetchone()
            if row:
                return
            if self._active(conn) is not None:
                raise ValueError('runtime_health_registration_uncertain')
            conn.execute("INSERT INTO runtime_health_attempts VALUES(?,?,'prepared',NULL,?,NULL,NULL)",
                (self.deployment_id, identifier, self._now()))

    def mark_subscription_submitted(self, operation_id):
        identifier = _identifier(operation_id)
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM runtime_health_attempts WHERE deployment_id=? AND operation_id=?',
                (self.deployment_id, identifier)).fetchone()
            if row is None or row['state'] != 'prepared':
                raise ValueError('runtime_health_registration_uncertain')
            conn.execute("UPDATE runtime_health_attempts SET state='submitted',submitted_at=? WHERE deployment_id=? AND operation_id=?",
                (self._now(), self.deployment_id, identifier))

    def complete_subscription_attempt(self, operation_id, state):
        identifier = _identifier(operation_id)
        if state not in {'verified', 'rejected', 'uncertain'}:
            raise ValueError('runtime_health_state_invalid')
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM runtime_health_attempts WHERE deployment_id=? AND operation_id=?',
                (self.deployment_id, identifier)).fetchone()
            if row is None:
                raise ValueError('runtime_health_registration_uncertain')
            if row['state'] == 'completed':
                if row['receipt'] != state:
                    raise ValueError('runtime_health_receipt_conflict')
                return
            if row['state'] != 'submitted':
                raise ValueError('runtime_health_registration_uncertain')
            stamp = self._now()
            conn.execute("UPDATE runtime_health_attempts SET state='completed',receipt=?,completed_at=? WHERE deployment_id=? AND operation_id=?",
                (state, stamp, self.deployment_id, identifier))
            conn.execute('INSERT INTO runtime_health_observations(deployment_id,state,observed_at) VALUES(?,?,?)',
                (self.deployment_id, state, stamp))

    def record_callback(self, event_id, kind):
        identifier = _identifier(event_id)
        if kind not in {'message_created', 'message_callback', 'bot_started'}:
            raise ValueError('runtime_health_callback_invalid')
        with self._transaction() as conn:
            old = conn.execute('SELECT kind FROM runtime_health_callbacks WHERE deployment_id=? AND event_id=?',
                (self.deployment_id, identifier)).fetchone()
            if old:
                if old['kind'] != kind:
                    raise ValueError('runtime_health_callback_conflict')
                return
            conn.execute('INSERT INTO runtime_health_callbacks VALUES(?,?,?,?)',
                (self.deployment_id, identifier, kind, self._now()))

    def read_status(self):
        with self._transaction() as conn:
            configured = conn.execute('SELECT 1 FROM runtime_health_config WHERE deployment_id=?', (self.deployment_id,)).fetchone() is not None
            observation = conn.execute('SELECT state,observed_at FROM runtime_health_observations WHERE deployment_id=? ORDER BY id DESC LIMIT 1',
                (self.deployment_id,)).fetchone()
            callback = conn.execute('SELECT accepted_at FROM runtime_health_callbacks WHERE deployment_id=? ORDER BY accepted_at DESC LIMIT 1',
                (self.deployment_id,)).fetchone()
            attempt = conn.execute('SELECT state,receipt FROM runtime_health_attempts WHERE deployment_id=? ORDER BY rowid DESC LIMIT 1',
                (self.deployment_id,)).fetchone()
        state = observation['state'] if observation else None
        result = {'state': 'issues' if state in {'missing', 'unavailable', 'rejected', 'uncertain'} else
                      'observed' if observation or callback else 'unknown' if configured else 'not_configured',
            'configured_subscription': configured, 'subscription_state': state,
            'last_subscription_observed_at': observation['observed_at'] if observation else None,
            'last_trusted_callback_at': callback['accepted_at'] if callback else None,
            'last_authenticated_accept_at': callback['accepted_at'] if callback else None,
            'phone_delivery': 'unknown', 'registration_state': 'none' if attempt is None else
                'uncertain' if attempt['receipt'] == 'uncertain' else attempt['state']}
        return result

    def read_webhook(self, actor, project_id, *, expected_actor_revision=None):
        if self.team is None:
            raise TeamForbidden('owner_required')
        def authorize(revision):
            with self.team.db.connection() as conn:
                member = self.team._scope(conn, uuid_string(actor), project_id)
                self.team._actor_revision(member, revision)
                if member.role != 'owner':
                    raise TeamForbidden('owner_required')
                return member.revision
        revision = authorize(expected_actor_revision)
        value = self.read_status()
        value.pop('registration_state')
        result = DashboardWebhookStatus.model_validate(value)
        authorize(revision)
        return result
