"""Durable short-command processing, separate from the Polza billing database.

A before-submission checkpoint is deliberately conservative: if its response is
lost, no new POST is authorized. A billing reservation and this transaction are
not atomic. Paid requests, source text, proposals and receipts are immutable.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import ValidationError

from secretary.domain.bot import BotError, BotEvent, DeterministicReply, SendReceipt, text_value, uuid_value
from secretary.domain.team import CommandReceipt, TeamForbidden, canonical, content_hash
from secretary.domain.voice_commands import IntentProposal, VoiceClaim, VoiceError
from secretary.infrastructure.bot_repository import BotDelivery, _event_json, _plain, _reply_json


VOICE_MIGRATION = (
    '''CREATE TABLE voice_jobs(id TEXT PRIMARY KEY NOT NULL,event_id TEXT NOT NULL UNIQUE REFERENCES bot_events(id),
        bot_id TEXT NOT NULL,user_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
        payload TEXT NOT NULL CHECK(json_valid(payload)),created_at REAL NOT NULL)''',
    '''CREATE TABLE voice_job_state(id TEXT PRIMARY KEY NOT NULL REFERENCES voice_jobs(id),
        state TEXT NOT NULL CHECK(state IN('queued','processing','complete','paused_budget','paused_config','uncertain','rejected')),
        stage TEXT NOT NULL CHECK(stage IN('queued','stt','intent','repair','complete')),
        worker_id TEXT,fence INTEGER NOT NULL DEFAULT 0 CHECK(fence>=0),lease_until REAL,error_code TEXT)''',
    '''CREATE TABLE voice_requests(operation_id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL REFERENCES voice_jobs(id),
        stage TEXT NOT NULL CHECK(stage IN('stt','intent')),attempt INTEGER NOT NULL CHECK(attempt IN(0,1)),
        payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),
        UNIQUE(job_id,stage,attempt),CHECK(stage!='stt' OR attempt=0))''',
    '''CREATE TABLE voice_responses(operation_id TEXT PRIMARY KEY NOT NULL REFERENCES voice_requests(operation_id),
        payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE voice_transcripts(job_id TEXT PRIMARY KEY NOT NULL REFERENCES voice_jobs(id),
        payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE voice_contexts(job_id TEXT PRIMARY KEY NOT NULL REFERENCES voice_jobs(id),
        payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE voice_proposal_batches(job_id TEXT PRIMARY KEY NOT NULL REFERENCES voice_jobs(id),
        payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE voice_proposals(id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL REFERENCES voice_jobs(id),
        actor_id TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE,payload_hash TEXT NOT NULL,
        payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE voice_confirmations(proposal_id TEXT PRIMARY KEY NOT NULL REFERENCES voice_proposals(id),
        operation_id TEXT NOT NULL UNIQUE REFERENCES team_commands(operation_id),
        payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE voice_notices(id TEXT PRIMARY KEY NOT NULL,event_id TEXT NOT NULL REFERENCES bot_events(id),
        item_key TEXT NOT NULL,payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),
        UNIQUE(event_id,item_key))''',
    '''CREATE TABLE voice_notice_state(id TEXT PRIMARY KEY NOT NULL REFERENCES voice_notices(id),
        state TEXT NOT NULL CHECK(state IN('pending','sending','sent','uncertain','retryable','rejected')),
        worker_id TEXT,fence INTEGER NOT NULL DEFAULT 0 CHECK(fence>=0),lease_until REAL,
        attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3),not_before REAL NOT NULL DEFAULT 0,
        receipt TEXT CHECK(receipt IS NULL OR json_valid(receipt)))''',
    'CREATE INDEX voice_job_queue ON voice_job_state(state)',
    'CREATE INDEX voice_notice_queue ON voice_notice_state(state,not_before)',
    *tuple(f'''CREATE TRIGGER {table}_immutable_{verb.lower()} BEFORE {verb} ON {table}
        BEGIN SELECT RAISE(ABORT,'Immutable voice evidence'); END'''
        for table in ('voice_jobs', 'voice_requests', 'voice_responses', 'voice_transcripts', 'voice_contexts',
                      'voice_proposal_batches', 'voice_proposals', 'voice_confirmations', 'voice_notices')
        for verb in ('UPDATE', 'DELETE')),
    *tuple(f'''CREATE TRIGGER {table}_replace_guard BEFORE INSERT ON {table}
        WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition})
        BEGIN SELECT RAISE(ABORT,'Immutable voice evidence'); END'''
        for table, condition in (
            ('voice_jobs', 'id=NEW.id OR event_id=NEW.event_id'),
            ('voice_requests', 'operation_id=NEW.operation_id OR (job_id=NEW.job_id AND stage=NEW.stage AND attempt=NEW.attempt)'),
            ('voice_responses', 'operation_id=NEW.operation_id'),
            ('voice_transcripts', 'job_id=NEW.job_id'),
            ('voice_contexts', 'job_id=NEW.job_id'),
            ('voice_proposal_batches', 'job_id=NEW.job_id'),
            ('voice_proposals', 'id=NEW.id OR operation_id=NEW.operation_id'),
            ('voice_confirmations', 'proposal_id=NEW.proposal_id OR operation_id=NEW.operation_id'),
            ('voice_notices', 'id=NEW.id OR (event_id=NEW.event_id AND item_key=NEW.item_key)'))),
    *tuple(f'''CREATE TRIGGER {table}_replace_guard BEFORE INSERT ON {table}
        WHEN EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'Voice execution cannot be replaced'); END'''
        for table in ('voice_job_state', 'voice_notice_state')),
    *tuple(f'''CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT,'Voice execution cannot be deleted'); END'''
        for table in ('voice_job_state', 'voice_notice_state')),
    '''CREATE TRIGGER voice_job_state_binding BEFORE UPDATE ON voice_job_state
        WHEN NEW.id IS NOT OLD.id OR NEW.fence<OLD.fence
          OR (OLD.state IN('complete','paused_budget','paused_config','uncertain','rejected') AND NEW.state IS NOT OLD.state)
        BEGIN SELECT RAISE(ABORT,'Voice execution cannot regress'); END''',
    '''CREATE TRIGGER voice_notice_state_binding BEFORE UPDATE ON voice_notice_state
        WHEN NEW.id IS NOT OLD.id OR NEW.fence<OLD.fence OR NEW.attempt<OLD.attempt
          OR (OLD.state IN('sent','uncertain','retryable','rejected') AND NEW.state IS NOT OLD.state)
        BEGIN SELECT RAISE(ABORT,'Voice delivery cannot regress'); END''',
)


# A local pre-dispatch disposition is neither a provider response nor a retry.
VOICE_ABORT_MIGRATION = (
    '''CREATE TABLE voice_request_aborts(operation_id TEXT PRIMARY KEY NOT NULL REFERENCES voice_requests(operation_id),
        payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    *tuple(f'''CREATE TRIGGER voice_request_aborts_immutable_{verb.lower()} BEFORE {verb} ON voice_request_aborts
        BEGIN SELECT RAISE(ABORT,'Immutable voice evidence'); END''' for verb in ('UPDATE','DELETE')),
    '''CREATE TRIGGER voice_request_aborts_replace_guard BEFORE INSERT ON voice_request_aborts
        WHEN EXISTS(SELECT 1 FROM voice_request_aborts WHERE operation_id=NEW.operation_id)
        BEGIN SELECT RAISE(ABORT,'Immutable voice evidence'); END''',
    '''CREATE TRIGGER response_abort_guard BEFORE INSERT ON voice_responses
        WHEN EXISTS(SELECT 1 FROM voice_request_aborts WHERE operation_id=NEW.operation_id)
        BEGIN SELECT RAISE(ABORT,'Conflicting voice disposition'); END''',
    '''CREATE TRIGGER abort_response_guard BEFORE INSERT ON voice_request_aborts
        WHEN EXISTS(SELECT 1 FROM voice_responses WHERE operation_id=NEW.operation_id)
        BEGIN SELECT RAISE(ABORT,'Conflicting voice disposition'); END''',
)

_ABORT_CODES = frozenset({'monthly_budget_exhausted','monthly_budget_scope_exhausted',
    'monthly_budget_configuration','monthly_budget_stale','monthly_budget_key_changed',
    'monthly_budget_period_changed'})


def _encoded(value, maximum=131072):
    try:
        payload = canonical(_plain(value))
        if len(payload.encode('utf-8')) > maximum:
            raise ValueError
        return payload
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise VoiceError('voice_payload_invalid') from None


def _checked(row):
    try:
        data = json.loads(row['payload'])
        if content_hash(data) != row['payload_hash']:
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError):
        raise VoiceError('voice_storage_corrupt') from None


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_:-]{1,128}', value):
        raise VoiceError('voice_key_invalid')
    return value


def _uuid(value):
    try:
        uuid_value(value)
    except BotError:
        raise VoiceError('voice_identity_invalid') from None


class VoiceRepository:
    def __init__(self, db, team, bot, clock=None):
        if db.path != team.db.path or db.path != bot.db.path:
            raise VoiceError('voice_database_mismatch')
        self.db, self.team, self.bot = db, team, bot
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise VoiceError('voice_clock_invalid')
        return value.astimezone(timezone.utc)

    @staticmethod
    def _lease(worker, seconds):
        try:
            text_value(worker, 128)
        except BotError:
            raise VoiceError('voice_lease_invalid') from None
        if type(seconds) is not int or not 1 <= seconds <= 3600:
            raise VoiceError('voice_lease_invalid')

    def _bound(self, conn, event):
        try:
            actual = self.bot._bound(conn, event)
        except BotError:
            raise VoiceError('voice_actor_changed') from None
        if actual.actor_id is None or actual.kind != 'message_created':
            raise VoiceError('voice_actor_unavailable')
        return actual

    def _job(self, conn, identifier):
        row = conn.execute('SELECT * FROM voice_jobs WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise VoiceError('voice_job_unavailable')
        value = _checked(row)
        try:
            event = _event_json(canonical(value['event']))
            if event.event_id != row['event_id'] or event.bot_id != row['bot_id'] or event.user_id != row['user_id']:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise VoiceError('voice_storage_corrupt') from None
        return row, event, value['text']

    @staticmethod
    def _pending(conn, identifier):
        aborted = VoiceRepository._aborts(conn, identifier)
        if not aborted:
            return conn.execute('''SELECT 1 FROM voice_requests r LEFT JOIN voice_responses a USING(operation_id)
                WHERE r.job_id=? AND a.operation_id IS NULL LIMIT 1''', (identifier,)).fetchone() is not None
        return conn.execute('''SELECT 1 FROM voice_requests r LEFT JOIN voice_responses a USING(operation_id)
            WHERE r.job_id=? AND a.operation_id IS NULL AND NOT EXISTS(
                SELECT 1 FROM voice_request_aborts b WHERE b.operation_id=r.operation_id) LIMIT 1''', (identifier,)).fetchone() is not None

    @staticmethod
    def _aborts(conn, identifier):
        # Raw restart preflight/checkpoint may still see genuine schema 7/8.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_request_aborts'").fetchone():
            if conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0]>=9:
                raise VoiceError('voice_storage_corrupt')
            return ()
        result=[]
        for row in conn.execute('''SELECT a.* FROM voice_request_aborts a JOIN voice_requests r USING(operation_id)
                WHERE r.job_id=? ORDER BY r.rowid''',(identifier,)):
            value=_checked(row)
            VoiceRepository._abort_info(value)
            request_row=conn.execute('SELECT * FROM voice_requests WHERE operation_id=?',(row['operation_id'],)).fetchone()
            request=_checked(request_row)
            VoiceRepository._request_info(request)
            if (value['operation_id']!=row['operation_id'] or request['stage']!=request_row['stage']
                    or request['attempt']!=request_row['attempt'] or any(value[k]!=v or type(value[k]) is not type(v)
                    for k,v in request.items()) or conn.execute('SELECT 1 FROM voice_responses WHERE operation_id=?',(row['operation_id'],)).fetchone()):
                raise VoiceError('voice_storage_corrupt')
            result.append(value)
        return tuple(result)

    @staticmethod
    def recovery_outcome(conn, identifier):
        """Trusted recovery read; absent old-schema abort evidence stays unknown."""
        if VoiceRepository._pending(conn, identifier):
            return 'uncertain','voice_request_uncertain'
        aborted=VoiceRepository._aborts(conn, identifier)
        if aborted:
            code=aborted[-1]['error_code']
            return ('paused_budget' if code in {'monthly_budget_exhausted','monthly_budget_scope_exhausted'}
                    else 'paused_config'),code
        return 'queued',None

    def _claim(self, conn, claim, *, complete=False, authorize=True):
        if not isinstance(claim, VoiceClaim) or type(claim.fence) is not int:
            raise VoiceError('voice_claim_invalid')
        row = conn.execute('SELECT * FROM voice_job_state WHERE id=?', (claim.job_id,)).fetchone()
        if (not row or row['worker_id'] != claim.worker_id or row['fence'] != claim.fence
                or row['state'] not in ({'processing', 'complete'} if complete else {'processing'})
                or row['state'] != 'complete' and row['lease_until'] <= self._now().timestamp()):
            raise VoiceError('voice_claim_lost')
        _, event, _ = self._job(conn, claim.job_id)
        if _encoded(event) != _encoded(claim.event):
            raise VoiceError('voice_claim_invalid')
        if authorize:
            self._bound(conn, event)
        return row, event

    def enqueue(self, event, *, text=None):
        if not isinstance(event, BotEvent):
            raise VoiceError('voice_event_invalid')
        if text is not None and (not isinstance(text, str) or not text.strip() or text != event.text or len(text) > 16000):
            raise VoiceError('voice_source_invalid')
        if text is None and not event.media:
            raise VoiceError('voice_source_missing')
        with self.db.transaction() as conn:
            event = self._bound(conn, event)
            payload = _encoded({'event': event, 'text': text})
            old = conn.execute('SELECT * FROM voice_jobs WHERE event_id=?', (event.event_id,)).fetchone()
            if old:
                _checked(old)
                if old['payload'] != payload:
                    raise VoiceError('voice_job_conflict')
                return old['id']
            identifier = str(uuid4())
            conn.execute('INSERT INTO voice_jobs VALUES(?,?,?,?,?,?,?)', (identifier, event.event_id,
                event.bot_id, event.user_id, content_hash(json.loads(payload)), payload, self._now().timestamp()))
            conn.execute("INSERT INTO voice_job_state(id,state,stage) VALUES(?,'queued',?)", (identifier, 'intent' if text else 'stt'))
            if text is not None:
                proof = _encoded({'text': text, 'receipts': []})
                conn.execute('INSERT INTO voice_transcripts VALUES(?,?,?)', (identifier, content_hash(json.loads(proof)), proof))
            return identifier

    def _recover(self, conn):
        stamp = self._now().timestamp()
        rows = conn.execute("SELECT id FROM voice_job_state WHERE state='processing' AND lease_until<=?", (stamp,)).fetchall()
        for row in rows:
            state,error = self.recovery_outcome(conn, row['id'])
            conn.execute('UPDATE voice_job_state SET state=?,worker_id=NULL,lease_until=NULL,error_code=? WHERE id=?',
                (state, error, row['id']))
        conn.execute("UPDATE voice_notice_state SET state='uncertain',worker_id=NULL,lease_until=NULL WHERE state='sending' AND lease_until<=?", (stamp,))

    def recover_expired(self):
        with self.db.transaction() as conn:
            self._recover(conn)

    def claim_job(self, worker_id, lease_seconds=60):
        self._lease(worker_id, lease_seconds)
        with self.db.transaction() as conn:
            self._recover(conn)
            rows = conn.execute('''SELECT s.* FROM voice_job_state s JOIN voice_jobs j ON j.id=s.id
                WHERE s.state='queued' AND NOT EXISTS(SELECT 1 FROM voice_job_state earlier
                JOIN voice_jobs e ON e.id=earlier.id WHERE e.bot_id=j.bot_id AND e.user_id=j.user_id
                AND e.rowid<j.rowid AND earlier.state IN('queued','processing')) ORDER BY j.rowid''').fetchall()
            for row in rows:
                _, event, _ = self._job(conn, row['id'])
                try:
                    self._bound(conn, event)
                except VoiceError:
                    conn.execute("UPDATE voice_job_state SET state='rejected',error_code='voice_actor_changed' WHERE id=?", (row['id'],))
                    continue
                until, fence = self._now() + timedelta(seconds=lease_seconds), row['fence'] + 1
                conn.execute("UPDATE voice_job_state SET state='processing',worker_id=?,fence=?,lease_until=? WHERE id=?",
                             (worker_id, fence, until.timestamp(), row['id']))
                return VoiceClaim(row['id'], event, worker_id, fence, until, row['stage'])
            return None

    def authorize_job(self, claim):
        with self.db.transaction() as conn:
            return self._claim(conn, claim)[1]

    def pending_request(self, claim):
        """Fenced worker cleanup only; exposes no source after actor revocation."""
        with self.db.transaction() as conn:
            self._claim(conn, claim, authorize=False)
            return self._pending(conn, claim.job_id)

    def renew_job(self, claim, lease_seconds=60):
        self._lease(claim.worker_id, lease_seconds)
        with self.db.transaction() as conn:
            row, _ = self._claim(conn, claim)
            until = max(datetime.fromtimestamp(row['lease_until'], timezone.utc), self._now() + timedelta(seconds=lease_seconds))
            conn.execute('UPDATE voice_job_state SET lease_until=? WHERE id=?', (until.timestamp(), claim.job_id))
            return replace(claim, lease_until=until, stage=row['stage'])

    def get_job(self, job_id):
        """Trusted internal status read. No unauthenticated interface exposes it."""
        _uuid(job_id)
        with self.db.transaction() as conn:
            _, event, text = self._job(conn, job_id)
            self._bound(conn, event)
            state = conn.execute('SELECT * FROM voice_job_state WHERE id=?', (job_id,)).fetchone()
            return {'job_id': job_id, 'event': event, 'text': text, 'state': state['state'], 'stage': state['stage'],
                    'error_code': state['error_code'], 'pending_request': self._pending(conn, job_id),
                    'transcript': self._transcript(conn, job_id), 'proposals': self._proposals(conn, job_id)}

    @staticmethod
    def _request_info(info):
        if not isinstance(info, dict) or set(info) != {'operation_id', 'category', 'command_id', 'stage', 'attempt', 'request_hash'}:
            raise VoiceError('voice_request_invalid')
        _uuid(info['operation_id'])
        _uuid(info['command_id'])
        if (info['stage'] not in {'stt', 'intent'} or info['category'] != 'voice_' + info['stage']
                or type(info['attempt']) is not int or info['attempt'] not in (0, 1)
                or info['stage'] == 'stt' and info['attempt'] != 0
                or not isinstance(info['request_hash'], str) or not re.fullmatch(r'[0-9a-f]{64}', info['request_hash'])):
            raise VoiceError('voice_request_invalid')
        return _encoded(info, 4096)

    def begin_request(self, claim, info):
        payload = self._request_info(info)
        with self.db.transaction() as conn:
            self._claim(conn, claim)
            if info['command_id'] != claim.event.event_id:
                raise VoiceError('voice_request_scope')
            if conn.execute('SELECT 1 FROM voice_requests WHERE operation_id=? OR (job_id=? AND stage=? AND attempt=?)',
                    (info['operation_id'], claim.job_id, info['stage'], info['attempt'])).fetchone():
                raise VoiceError('voice_request_already_started')
            if self._pending(conn, claim.job_id):
                raise VoiceError('voice_request_uncertain')
            if self._aborts(conn, claim.job_id):
                raise VoiceError('voice_request_not_submitted')
            source = self._transcript(conn, claim.job_id)
            if (info['stage'] == 'stt' and source is not None or info['stage'] == 'intent' and source is None
                    or info['stage'] == 'intent' and not conn.execute('SELECT 1 FROM voice_contexts WHERE job_id=?', (claim.job_id,)).fetchone()
                    or conn.execute('SELECT 1 FROM voice_proposal_batches WHERE job_id=?', (claim.job_id,)).fetchone()):
                raise VoiceError('voice_request_order')
            if info['attempt'] == 1:
                previous = conn.execute('''SELECT a.* FROM voice_requests r JOIN voice_responses a USING(operation_id)
                    WHERE r.job_id=? AND r.stage='intent' AND r.attempt=0''', (claim.job_id,)).fetchone()
                if not previous or _checked(previous)['status'] != 200:
                    raise VoiceError('voice_repair_unavailable')
            conn.execute('INSERT INTO voice_requests VALUES(?,?,?,?,?,?)', (info['operation_id'], claim.job_id,
                info['stage'], info['attempt'], content_hash(json.loads(payload)), payload))
            conn.execute('UPDATE voice_job_state SET stage=? WHERE id=?',
                         ('repair' if info['attempt'] == 1 else info['stage'], claim.job_id))

    def record_response(self, claim, info):
        if not isinstance(info, dict) or set(info) != {'operation_id', 'stage', 'attempt', 'request_hash', 'status', 'provider_response', 'usage_receipt'}:
            raise VoiceError('voice_response_invalid')
        if type(info['status']) is not int or not 100 <= info['status'] <= 599 or not isinstance(info['usage_receipt'], dict):
            raise VoiceError('voice_response_invalid')
        maximum = 4 * 1024 * 1024 if info['stage'] == 'stt' else 64 * 1024
        _encoded(info['provider_response'], maximum)
        _encoded(info['usage_receipt'], 8192)
        payload = _encoded(info, maximum + 12288)
        with self.db.transaction() as conn:
            self._claim(conn, claim)
            row = conn.execute('SELECT * FROM voice_requests WHERE operation_id=? AND job_id=?',
                               (info['operation_id'], claim.job_id)).fetchone()
            if not row:
                raise VoiceError('voice_response_unbound')
            request = _checked(row)
            if any(info[key] != request[key] or type(info[key]) is not type(request[key])
                   for key in ('operation_id', 'stage', 'attempt', 'request_hash')):
                raise VoiceError('voice_response_unbound')
            if conn.execute('SELECT 1 FROM voice_request_aborts WHERE operation_id=?',(info['operation_id'],)).fetchone():
                raise VoiceError('voice_response_conflict')
            old = conn.execute('SELECT * FROM voice_responses WHERE operation_id=?', (info['operation_id'],)).fetchone()
            if old:
                _checked(old)
                if old['payload'] != payload:
                    raise VoiceError('voice_response_conflict')
                return
            conn.execute('INSERT INTO voice_responses VALUES(?,?,?)',
                         (info['operation_id'], content_hash(json.loads(payload)), payload))

    @staticmethod
    def _abort_info(info):
        if not isinstance(info,dict) or set(info)!={'operation_id','category','command_id','stage','attempt','request_hash','error_code'}:
            raise VoiceError('voice_abort_invalid')
        VoiceRepository._request_info({k:v for k,v in info.items() if k!='error_code'})
        if not isinstance(info['error_code'],str) or info['error_code'] not in _ABORT_CODES:
            raise VoiceError('voice_abort_invalid')
        return _encoded(info,4096)

    def record_not_submitted(self, claim, info):
        """Trusted Polza hook only: release_unsubmitted committed, no dispatch ran."""
        payload=self._abort_info(info)
        with self.db.transaction() as conn:
            self._claim(conn,claim,authorize=False)
            row=conn.execute('SELECT * FROM voice_requests WHERE operation_id=? AND job_id=?',
                (info['operation_id'],claim.job_id)).fetchone()
            if not row:
                raise VoiceError('voice_abort_unbound')
            request=_checked(row)
            self._request_info(request)
            if (info['command_id']!=claim.event.event_id or any(info[k]!=v or type(info[k]) is not type(v) for k,v in request.items())):
                raise VoiceError('voice_abort_unbound')
            if conn.execute('SELECT 1 FROM voice_responses WHERE operation_id=?',(info['operation_id'],)).fetchone():
                raise VoiceError('voice_abort_conflict')
            old=conn.execute('SELECT * FROM voice_request_aborts WHERE operation_id=?',(info['operation_id'],)).fetchone()
            if old:
                _checked(old)
                if old['payload']!=payload:
                    raise VoiceError('voice_abort_conflict')
                return
            conn.execute('INSERT INTO voice_request_aborts VALUES(?,?,?)',
                (info['operation_id'],content_hash(json.loads(payload)),payload))

    def read_responses(self, claim):
        with self.db.transaction() as conn:
            self._claim(conn, claim)
            rows = conn.execute('''SELECT a.* FROM voice_requests r JOIN voice_responses a USING(operation_id)
                WHERE r.job_id=? ORDER BY r.rowid''', (claim.job_id,)).fetchall()
            return tuple(_checked(row) for row in rows)

    @staticmethod
    def _transcript(conn, job_id):
        row = conn.execute('SELECT * FROM voice_transcripts WHERE job_id=?', (job_id,)).fetchone()
        return _checked(row)['text'] if row else None

    def transcript(self, claim):
        with self.db.transaction() as conn:
            self._claim(conn, claim)
            return self._transcript(conn, claim.job_id)

    def checkpoint_transcript(self, claim, text, receipts=()):
        if not isinstance(text, str) or not text.strip() or len(text) > 16000 or not isinstance(receipts, (tuple, list)):
            raise VoiceError('voice_transcript_invalid')
        payload = _encoded({'text': text, 'receipts': receipts})
        with self.db.transaction() as conn:
            self._claim(conn, claim)
            old = conn.execute('SELECT * FROM voice_transcripts WHERE job_id=?', (claim.job_id,)).fetchone()
            if old:
                _checked(old)
                if old['payload'] != payload:
                    raise VoiceError('voice_transcript_conflict')
                return
            response = conn.execute('''SELECT a.* FROM voice_requests r JOIN voice_responses a USING(operation_id)
                WHERE r.job_id=? AND r.stage='stt' AND r.attempt=0''', (claim.job_id,)).fetchone()
            if not response or _checked(response)['status'] != 200:
                raise VoiceError('voice_transcript_unbound')
            conn.execute('INSERT INTO voice_transcripts VALUES(?,?,?)', (claim.job_id, content_hash(json.loads(payload)), payload))
            conn.execute("UPDATE voice_job_state SET stage='intent' WHERE id=?", (claim.job_id,))

    def checkpoint_context(self, claim, context):
        """Freeze allowed identities and revisions before the first intent POST."""
        if not isinstance(context, dict):
            raise VoiceError('voice_context_invalid')
        payload = _encoded(context, 32768)
        with self.db.transaction() as conn:
            self._claim(conn, claim)
            if self._transcript(conn, claim.job_id) is None:
                raise VoiceError('voice_source_unavailable')
            old = conn.execute('SELECT * FROM voice_contexts WHERE job_id=?', (claim.job_id,)).fetchone()
            if old:
                _checked(old)
                if old['payload'] != payload:
                    raise VoiceError('voice_context_conflict')
                return json.loads(payload)
            if conn.execute("SELECT 1 FROM voice_requests WHERE job_id=? AND stage='intent'", (claim.job_id,)).fetchone():
                raise VoiceError('voice_context_too_late')
            conn.execute('INSERT INTO voice_contexts VALUES(?,?,?)', (claim.job_id, content_hash(json.loads(payload)), payload))
            return json.loads(payload)

    def read_context(self, claim):
        with self.db.transaction() as conn:
            self._claim(conn, claim)
            row = conn.execute('SELECT * FROM voice_contexts WHERE job_id=?', (claim.job_id,)).fetchone()
            return _checked(row) if row else None

    @staticmethod
    def _proposal(row):
        try:
            value = IntentProposal.model_validate(_checked(row))
            if value.id != row['id'] or value.operation_id != row['operation_id'] or value.actor_id != row['actor_id']:
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError):
            raise VoiceError('voice_storage_corrupt') from None

    def _proposals(self, conn, job_id):
        return tuple(self._proposal(row) for row in conn.execute('SELECT * FROM voice_proposals WHERE job_id=? ORDER BY rowid', (job_id,)))

    def store_proposals(self, claim, proposals):
        if not isinstance(proposals, tuple) or len(proposals) > 5:
            raise VoiceError('voice_proposals_invalid')
        try:
            values = tuple(IntentProposal.model_validate(value.model_dump(exclude_unset=True)) for value in proposals)
        except (ValueError, TypeError, AttributeError):
            raise VoiceError('voice_proposals_invalid') from None
        if len({value.id for value in values}) != len(values) or len({value.operation_id for value in values}) != len(values):
            raise VoiceError('voice_proposals_invalid')
        payload = _encoded(values)
        with self.db.transaction() as conn:
            _, event = self._claim(conn, claim)
            source = self._transcript(conn, claim.job_id)
            if source is None or self._pending(conn, claim.job_id):
                raise VoiceError('voice_source_unavailable')
            digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
            for value in values:
                if (value.actor_id != event.actor_id or value.actor_revision != event.actor_revision
                        or value.project_ids != event.project_ids or value.command_id != event.event_id
                        or value.source_hash != digest or not value.evidence_quote.strip()
                        or value.evidence_quote not in source):
                    raise VoiceError('voice_proposal_source_invalid')
            old = conn.execute('SELECT * FROM voice_proposal_batches WHERE job_id=?', (claim.job_id,)).fetchone()
            if old:
                _checked(old)
                if old['payload'] != payload:
                    raise VoiceError('voice_proposals_conflict')
                return self._proposals(conn, claim.job_id)
            if any(value.expires_at <= self._now() for value in values):
                raise VoiceError('voice_proposal_expired')
            try:
                conn.execute('INSERT INTO voice_proposal_batches VALUES(?,?,?)',
                             (claim.job_id, content_hash(json.loads(payload)), payload))
                for value in values:
                    encoded = _encoded(value)
                    conn.execute('INSERT INTO voice_proposals VALUES(?,?,?,?,?,?)', (value.id, claim.job_id,
                        value.actor_id, value.operation_id, content_hash(json.loads(encoded)), encoded))
            except sqlite3.IntegrityError:
                raise VoiceError('voice_proposals_conflict') from None
            conn.execute("UPDATE voice_job_state SET stage='complete' WHERE id=?", (claim.job_id,))
            return values

    def _owned_proposal(self, conn, actor, identifier):
        row = conn.execute('SELECT * FROM voice_proposals WHERE id=?', (identifier,)).fetchone()
        if not row or row['actor_id'] != actor:
            raise VoiceError('voice_proposal_unavailable')
        value = self._proposal(row)
        _, event, _ = self._job(conn, row['job_id'])
        self._bound(conn, event)
        if value.actor_id != event.actor_id or value.actor_revision != event.actor_revision or value.project_ids != event.project_ids:
            raise VoiceError('voice_proposal_unavailable')
        return value

    def get_proposal(self, actor, identifier):
        _uuid(actor)
        _uuid(identifier)
        with self.db.transaction() as conn:
            return self._owned_proposal(conn, actor, identifier)

    def confirm_intent(self, actor, proposal_id, *, expected_revision, operation_id):
        _uuid(actor)
        _uuid(proposal_id)
        _uuid(operation_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise VoiceError('voice_proposal_revision_invalid')
        with self.db.transaction() as conn:
            proposal = self._owned_proposal(conn, actor, proposal_id)
            if proposal.revision != expected_revision or proposal.operation_id != operation_id:
                raise VoiceError('voice_proposal_conflict')
            old = conn.execute('SELECT * FROM voice_confirmations WHERE proposal_id=?', (proposal_id,)).fetchone()
            if old:
                acceptance = _checked(old)
                if old['operation_id'] != operation_id:
                    raise VoiceError('voice_storage_corrupt')
            else:
                if proposal.expires_at <= self._now():
                    raise VoiceError('voice_proposal_expired')
                if proposal.command is None or proposal.unresolved_fields:
                    raise VoiceError('voice_proposal_unresolved')
            receipt = self.team.accept_command_in_transaction(conn, actor, proposal.command,
                                                               expected_actor_revision=proposal.actor_revision)
            encoded = _encoded(receipt.acceptance_receipt)
            if old:
                if acceptance != json.loads(encoded):
                    raise VoiceError('voice_storage_corrupt')
            else:
                conn.execute('INSERT INTO voice_confirmations VALUES(?,?,?,?)',
                             (proposal_id, operation_id, content_hash(json.loads(encoded)), encoded))
            return receipt

    def complete_job(self, claim):
        with self.db.transaction() as conn:
            self._claim(conn, claim, complete=True)
            if self._pending(conn, claim.job_id) or self._aborts(conn,claim.job_id) or not conn.execute('SELECT 1 FROM voice_proposal_batches WHERE job_id=?', (claim.job_id,)).fetchone():
                raise VoiceError('voice_job_incomplete')
            conn.execute("UPDATE voice_job_state SET state='complete',stage='complete' WHERE id=?", (claim.job_id,))

    def fail_job(self, claim, state, error_code):
        if state not in {'paused_budget', 'paused_config', 'uncertain', 'rejected'}:
            raise VoiceError('voice_job_state_invalid')
        _key(error_code)
        with self.db.transaction() as conn:
            self._claim(conn, claim, authorize=False)
            if state != 'uncertain' and self._pending(conn, claim.job_id):
                raise VoiceError('voice_request_uncertain')
            conn.execute('UPDATE voice_job_state SET state=?,error_code=? WHERE id=?', (state, error_code, claim.job_id))

    def enqueue_notice(self, event, key, reply):
        _key(key)
        if not isinstance(reply, DeterministicReply) or reply.sensitive_action:
            raise VoiceError('voice_notice_invalid')
        # Commands remain in the proposal/button ledger, not the transport body.
        value = DeterministicReply(reply.text, reply.buttons, receipt=reply.receipt)
        payload = _encoded(value)
        if re.search(r'[0-9a-f]{32}\.[A-Z2-7]{10}', value.text):
            raise VoiceError('voice_notice_sensitive')
        def media_urls(item):
            if isinstance(item, dict):
                for name, child in item.items():
                    if name == 'url' and isinstance(child, str):
                        yield child
                    else:
                        yield from media_urls(child)
            elif isinstance(item, (tuple, list)):
                for child in item:
                    yield from media_urls(child)
        if any(url and url in payload for url in media_urls(event.media)):
            raise VoiceError('voice_notice_sensitive')
        with self.db.transaction() as conn:
            self._bound(conn, event)
            if not conn.execute('SELECT 1 FROM voice_jobs WHERE event_id=?', (event.event_id,)).fetchone():
                raise VoiceError('voice_job_unavailable')
            old = conn.execute('SELECT rowid,* FROM voice_notices WHERE event_id=? AND item_key=?', (event.event_id, key)).fetchone()
            if old:
                saved = _checked(old)
                self._required_notices(conn, old, saved)
                saved.pop('required_notice_ids', None)
                if canonical(saved) != payload:
                    raise VoiceError('voice_notice_conflict')
                return old['id']
            if value.buttons:
                # Bind the complete preview to this immutable confirmation.
                # Progress is optional; no failed detail is evidence of review.
                required = self._notice_predecessors(conn, event.event_id)
                payload = _encoded({**_plain(value), 'required_notice_ids': required})
            identifier = str(uuid4())
            conn.execute('INSERT INTO voice_notices VALUES(?,?,?,?,?)',
                         (identifier, event.event_id, key, content_hash(json.loads(payload)), payload))
            conn.execute("INSERT INTO voice_notice_state(id,state) VALUES(?,'pending')", (identifier,))
            return identifier

    @staticmethod
    def _notice_predecessors(conn, event_id, before=None):
        progress = {str(uuid5(NAMESPACE_URL, f'secretary:voice:{event_id}:progress:{stage}'))
                    for stage in ('stt', 'intent')}
        rows = conn.execute('''SELECT rowid,* FROM voice_notices WHERE event_id=?
            AND (? IS NULL OR rowid<?) ORDER BY rowid''', (event_id, before, before)).fetchall()
        required = []
        for row in rows:
            _checked(row)
            if row['item_key'] not in progress:
                required.append(row['id'])
        return required

    def _required_notices(self, conn, evidence, payload):
        required = self._notice_predecessors(conn, evidence['event_id'], evidence['rowid']) if payload.get('buttons') else []
        if 'required_notice_ids' in payload and payload['required_notice_ids'] != required:
            raise VoiceError('voice_storage_corrupt')
        # Pre-fix immutable notices lack this metadata. Derive the same
        # conservative dependency without rewriting their historical payload.
        return required

    def _notice_ready(self, conn, identifier):
        evidence = conn.execute('SELECT rowid,* FROM voice_notices WHERE id=?', (identifier,)).fetchone()
        if not evidence:
            raise VoiceError('voice_storage_corrupt')
        required = self._required_notices(conn, evidence, _checked(evidence))
        for predecessor in required:
            state = conn.execute('SELECT state,receipt FROM voice_notice_state WHERE id=?', (predecessor,)).fetchone()
            if not state or state['state'] != 'sent' or state['receipt'] is None:
                return False
            try:
                receipt = SendReceipt(**json.loads(state['receipt']))
            except (BotError, ValueError, TypeError):
                return False
            if receipt.operation_id != predecessor or receipt.state != 'sent':
                return False
        return True

    def _delivery(self, conn, row):
        evidence = conn.execute('SELECT * FROM voice_notices WHERE id=?', (row['id'],)).fetchone()
        if not evidence:
            raise VoiceError('voice_storage_corrupt')
        payload = _checked(evidence)
        payload.pop('required_notice_ids', None)
        try:
            event = self.bot._load_event(conn, evidence['event_id'])
            reply = _reply_json(canonical(payload))
        except (BotError, ValueError, TypeError):
            raise VoiceError('voice_storage_corrupt') from None
        return BotDelivery(row['id'], event, reply, row['worker_id'], row['fence'],
                           datetime.fromtimestamp(row['lease_until'], timezone.utc), row['state'], row['attempt'])

    def claim_reply(self, worker_id, lease_seconds=60):
        self._lease(worker_id, lease_seconds)
        with self.db.transaction() as conn:
            self._recover(conn)
            rows = conn.execute('''SELECT s.* FROM voice_notice_state s JOIN voice_notices n ON n.id=s.id
                WHERE s.state='pending' AND s.not_before<=?
                AND EXISTS(SELECT 1 FROM bot_event_state e JOIN bot_replies b ON b.event_id=e.id
                    JOIN bot_reply_state r ON r.id=b.id WHERE e.id=n.event_id AND e.state='done'
                    AND r.state NOT IN('pending','sending'))
                AND NOT EXISTS(SELECT 1 FROM voice_notices older JOIN voice_notice_state os ON os.id=older.id
                    WHERE older.event_id=n.event_id AND older.rowid<n.rowid AND os.state IN('pending','sending'))
                ORDER BY n.rowid''', (self._now().timestamp(),)).fetchall()
            for row in rows:
                evidence = conn.execute('SELECT event_id FROM voice_notices WHERE id=?', (row['id'],)).fetchone()
                try:
                    self._bound(conn, self.bot._load_event(conn, evidence['event_id']))
                except (VoiceError, BotError):
                    conn.execute("UPDATE voice_notice_state SET state='rejected' WHERE id=?", (row['id'],))
                    continue
                if not self._notice_ready(conn, row['id']):
                    continue
                until = self._now() + timedelta(seconds=lease_seconds)
                conn.execute("UPDATE voice_notice_state SET state='sending',worker_id=?,fence=fence+1,attempt=attempt+1,lease_until=? WHERE id=?",
                             (worker_id, until.timestamp(), row['id']))
                return self._delivery(conn, conn.execute('SELECT * FROM voice_notice_state WHERE id=?', (row['id'],)).fetchone())
            return None

    def _reply_claim(self, conn, delivery):
        if not isinstance(delivery, BotDelivery) or type(delivery.fence) is not int:
            raise VoiceError('voice_claim_invalid')
        row = conn.execute('SELECT * FROM voice_notice_state WHERE id=?', (delivery.id,)).fetchone()
        if (not row or row['state'] != 'sending' or row['worker_id'] != delivery.worker_id or row['fence'] != delivery.fence
                or row['lease_until'] <= self._now().timestamp()):
            raise VoiceError('voice_claim_lost')
        actual = self._delivery(conn, row)
        if actual.event != delivery.event or actual.reply != delivery.reply or actual.attempt != delivery.attempt:
            raise VoiceError('voice_claim_invalid')
        return actual

    def authorize_reply(self, delivery):
        with self.db.transaction() as conn:
            actual = self._reply_claim(conn, delivery)
            self._bound(conn, actual.event)
            if not self._notice_ready(conn, actual.id):
                raise VoiceError('voice_preview_not_delivered')
            return actual

    def renew_reply(self, delivery, lease_seconds=60):
        self._lease(delivery.worker_id, lease_seconds)
        with self.db.transaction() as conn:
            actual = self._reply_claim(conn, delivery)
            self._bound(conn, actual.event)
            until = max(actual.lease_until, self._now() + timedelta(seconds=lease_seconds))
            conn.execute('UPDATE voice_notice_state SET lease_until=? WHERE id=?', (until.timestamp(), actual.id))
            return replace(actual, lease_until=until)

    def finish_reply(self, delivery, receipt):
        if not isinstance(receipt, SendReceipt):
            raise VoiceError('voice_receipt_invalid')
        try:
            receipt = SendReceipt(**asdict(receipt))
        except BotError:
            raise VoiceError('voice_receipt_invalid') from None
        if receipt.operation_id != delivery.id:
            raise VoiceError('voice_receipt_invalid')
        if receipt.error_code is not None:
            _key(receipt.error_code)
        with self.db.transaction() as conn:
            actual = self._reply_claim(conn, delivery)
            state, after = receipt.state, self._now().timestamp()
            if state == 'retryable':
                if receipt.error_code != 'max_rate_limited':
                    state = 'uncertain'
                elif actual.attempt < 3:
                    state, after = 'pending', after + (receipt.retry_after if receipt.retry_after is not None else 1)
            conn.execute('UPDATE voice_notice_state SET state=?,not_before=?,receipt=? WHERE id=?',
                         (state, after, _encoded(receipt), delivery.id))
