"""Synthetic, durable T13 cloud/bot phase shared by two guarded children.

There is deliberately no remote idempotency or logical-operation uniqueness.
The journal commits actual side effects before a simulated lost acknowledgement.
No socket, native media process, real credential, or owner file is used.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import wave

import httpx

from secretary.application.bot_commands import BotCommandService
from secretary.application.monthly_budget import MonthlyBudget
from secretary.application.voice_commands import VoiceCommandService
from secretary.domain.voice_commands import VoiceError
from secretary.infrastructure.bot_repository import BotRepository
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.infrastructure.max_bot import MaxClient
from secretary.infrastructure.max_media import ValidatedAudio
from secretary.infrastructure.polza import PolzaClient
from secretary.infrastructure.team_auth_repository import AuthRepository
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository
from secretary.infrastructure.voice_repository import VoiceRepository
from secretary.interface.max_webhook import normalize_max_event
from secretary.orchestration.bot_worker import BotInboxWorker, BotOutboxWorker
from secretary.settings import Settings


NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
KEY = 'synthetic-team-load-polza-not-real'
KEY_TAG = hashlib.sha256(KEY.encode()).hexdigest()
AUTH_SECRET = b'SYNTHETIC_TEAM_LOAD_AUTH_SECRET_32'
BOT_ID = '99'
RESERVE_MICRO = 1800_000000


@contextmanager
def _remote(directory):
    conn = sqlite3.connect(Path(directory) / 'remote.sqlite3', timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=15000')
    try:
        yield conn
    finally:
        conn.close()


def init_cloud(directory: Path):
    """Parent calls once after its remote and shared billing stores are seeded."""
    directory = Path(directory)
    with _remote(directory) as conn:
        conn.executescript('''
          CREATE TABLE load_cloud_ready(phase TEXT NOT NULL,child INTEGER NOT NULL,
            pid INTEGER NOT NULL,PRIMARY KEY(phase,child));
          CREATE TABLE load_cloud_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,child INTEGER NOT NULL,slot INTEGER NOT NULL,
            operation_id TEXT NOT NULL,request_hash TEXT NOT NULL,
            started_ns INTEGER NOT NULL,finished_ns INTEGER NOT NULL,outcome TEXT NOT NULL,
            budget_status TEXT,budget_reserved INTEGER);
          CREATE TABLE load_cloud_effects(id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt INTEGER NOT NULL,provider TEXT NOT NULL,operation_id TEXT NOT NULL);
        ''')


def _barrier(directory, phase, index, *, seconds=40):
    # Never wait while holding a SQLite transaction or a worker claim lock.
    with _remote(directory) as conn:
        conn.execute('INSERT INTO load_cloud_ready VALUES(?,?,?)', (phase, index, os.getpid()))
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        with _remote(directory) as conn:
            rows = conn.execute('SELECT child FROM load_cloud_ready WHERE phase=?', (phase,)).fetchall()
        if {row['child'] for row in rows} == {0, 1}:
            return
        time.sleep(.01)
    raise AssertionError('synthetic_cloud_barrier_timeout:' + phase)


class _Prices:
    async def quote(self, model):
        assert model == 'openai/whisper-large-v3-turbo'
        # A deliberately inflated synthetic quote makes two short commands
        # compete for the real shared 3000 RUB monthly reservation boundary.
        return {'stt_per_minute': 1800}


class _SyntheticMedia:
    def __init__(self, directory, index):
        self.directory, self.index = Path(directory), index

    def download_voice(self, event, *, cancelled=None):
        assert event.media and (cancelled is None or not cancelled())
        path = self.directory / ('synthetic-cloud-' + str(self.index) + '.wav')
        with wave.open(str(path), 'wb') as writer:
            writer.setparams((1, 2, 16000, 0, 'NONE', 'none'))
            writer.writeframes(b'\0\0' * 16000)
        raw = path.read_bytes()
        return ValidatedAudio(path.resolve(), 1000, hashlib.sha256(raw).hexdigest(), len(raw), event.event_id)


class _CloudRemote:
    def __init__(self, directory, index, members):
        self.directory, self.index = Path(directory), index
        self.slots = {str(member['max_user_id']): slot for slot, member in enumerate(members)}
        self.active_voice_event = None

    def _journal(self, provider, slot, operation, request_hash, outcome, effect, *, budget=None):
        started = time.perf_counter_ns()
        with _remote(self.directory) as conn:
            conn.execute('BEGIN IMMEDIATE')
            cursor = conn.execute('''INSERT INTO load_cloud_attempts
                (provider,child,slot,operation_id,request_hash,started_ns,finished_ns,outcome,
                 budget_status,budget_reserved) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (provider, self.index, slot, operation, request_hash, started, time.perf_counter_ns(),
                 outcome, budget['status'] if budget else None, budget['reserved_micro'] if budget else None))
            identifier = cursor.lastrowid
            effect_id = None
            if effect:
                effect_id = conn.execute('INSERT INTO load_cloud_effects(attempt,provider,operation_id) VALUES(?,?,?)',
                    (identifier, provider, operation)).lastrowid
            conn.commit()
        return identifier, effect_id

    def _finish(self, attempt):
        # Hold an in-flight window outside the SQL transaction so the second
        # real process can claim/send another reply at the same time.
        time.sleep(.02)
        with _remote(self.directory) as conn:
            conn.execute('UPDATE load_cloud_attempts SET finished_ns=? WHERE id=?',
                (time.perf_counter_ns(), attempt))

    def polza(self, request):
        assert request.method == 'POST' and request.url.path.endswith('/audio/transcriptions')
        assert request.headers['Authorization'] == 'Bearer ' + KEY
        assert self.active_voice_event is not None
        request_hash = hashlib.sha256(request.content).hexdigest()
        with sqlite3.connect(self.directory / 'billing.sqlite3') as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM billing_charges WHERE command_id=? AND status=\'submitted\'',
                (self.active_voice_event,)).fetchall()
        assert len(rows) == 1 and rows[0]['reserved_micro'] == RESERVE_MICRO
        assert rows[0]['request_hash'] == request_hash and rows[0]['key_tag'] == KEY_TAG
        attempt, _ = self._journal('polza', self.index, rows[0]['operation_id'], request_hash,
            'committed_timeout', True, budget=rows[0])
        self._finish(attempt)
        # The remote accepted and charged the operation before its reply was lost.
        raise httpx.ReadTimeout('synthetic-redacted-timeout', request=request)

    def max(self, request):
        assert request.method == 'POST' and request.url.path == '/messages'
        assert 'Idempotency-Key' not in request.headers
        user_id = request.url.params['user_id']
        slot = self.slots[user_id]
        with sqlite3.connect(self.directory / 'team.sqlite3') as conn:
            rows = conn.execute('''SELECT r.id FROM bot_replies r JOIN bot_reply_state s USING(id)
                JOIN bot_events e ON e.id=r.event_id WHERE s.state='sending'
                AND json_extract(e.payload,'$.user_id')=?''', (user_id,)).fetchall()
        assert len(rows) == 1
        operation = rows[0][0]
        with _remote(self.directory) as conn:
            count = conn.execute('SELECT COUNT(*) FROM load_cloud_attempts WHERE provider=\'max\' AND slot=?',
                (slot,)).fetchone()[0]
        limited = slot == 0 or slot == 1 and count == 0
        outcome = '429' if limited else 'committed_503' if slot == 2 else 'committed_timeout' if slot == 3 else '200'
        attempt, effect_id = self._journal('max', slot, operation, hashlib.sha256(request.content).hexdigest(),
            outcome, not limited)
        self._finish(attempt)
        if limited:
            return httpx.Response(429, headers={'Retry-After': '2'}, json={'error': 'synthetic-limit'})
        if slot == 2:
            return httpx.Response(503, json={'error': 'synthetic-redacted-unavailable'})
        if slot == 3:
            raise httpx.ReadTimeout('synthetic-redacted-timeout', request=request)
        body = json.loads(request.content)
        return httpx.Response(200, json={'message': {
            'sender': {'user_id': int(BOT_ID)},
            'recipient': {'user_id': int(user_id), 'chat_type': 'dialog', 'chat_id': int(user_id)},
            'timestamp': int(NOW.timestamp() * 1000),
            'body': {'mid': 'synthetic-max-' + str(effect_id), 'seq': effect_id, 'text': body['text']}}})


def _event(member, slot):
    body = {'mid': 'synthetic-load-event-' + str(slot), 'text': 'Помощь', 'attachments': []}
    if slot < 2:
        body['text'] = None
        body['attachments'] = [{'type': 'audio', 'payload': {'url': 'https://synthetic.invalid/voice.wav'}}]
    return normalize_max_event({'update_type': 'message_created', 'timestamp': int(NOW.timestamp() * 1000),
        'message': {'sender': {'user_id': int(member['max_user_id']), 'is_bot': False},
            'recipient': {'chat_id': int(member['max_user_id'])}, 'body': body}}, bot_id=BOT_ID)


async def _drain(worker):
    results = []
    for _ in range(30):
        result = await worker.run_once()
        if result is None:
            return results
        results.append(result)
    raise AssertionError('synthetic_cloud_worker_did_not_quiesce')


def run_cloud_phase(directory: Path, index: int, shared_fixture=None) -> dict:
    """Called concurrently by the two real owned Python children, no listener."""
    assert index in (0, 1)
    directory = Path(directory).resolve()
    members = json.loads((directory / 'members.json').read_text('utf-8'))
    assert len(members) == 10 and len({item['max_user_id'] for item in members}) == 10
    started = time.perf_counter_ns()
    clock = [NOW]
    db = TeamDatabase(directory / 'team.sqlite3')
    team = TeamRepository(db, clock=lambda: clock[0])
    auth = AuthRepository(db, secret=AUTH_SECRET, clock=lambda: clock[0])
    bot = BotRepository(db, team, auth, clock=lambda: clock[0])
    voices = VoiceRepository(db, team, bot, clock=lambda: NOW)
    budget = MonthlyBudget(BudgetRepository(directory / 'billing.sqlite3'), clock=lambda: NOW)
    assert budget.snapshot().approved_limit_micro == 3000_000000
    remote = _CloudRemote(directory, index, members)
    settings = Settings(_env_file=None, project_dir=directory, data_dir=directory / 'scratch-data',
        polza_api_key=KEY, cloud_enabled=True, local_cost_limits_enabled=False, request_timeout_seconds=10)
    polza = PolzaClient(settings, httpx.MockTransport(remote.polza), budget=budget,
        charge_context={'key_tag': KEY_TAG}, price_reader=_Prices())
    voice = VoiceCommandService(team, bot, voices, _SyntheticMedia(directory, index), polza, clock=lambda: NOW)
    service = BotCommandService(team, bot, public_origin='https://synthetic.invalid', voice=voice, clock=lambda: NOW)
    inbox = BotInboxWorker(bot, service, worker_id='load-inbox-' + str(index))
    intakes = Counter()
    _barrier(directory, 'cloud_start', index)
    for _ in range(3):
        for slot, member in enumerate(members):
            intakes[bot.intake(_event(member, slot)).state] += 1
    _barrier(directory, 'intake_done', index)
    inbox_count = len(asyncio.run(_drain(inbox)))
    _barrier(directory, 'inbox_done', index)

    client = MaxClient('SYNTHETIC_TEAM_LOAD_MAX_TOKEN', bot_id=BOT_ID,
        client=httpx.Client(transport=httpx.MockTransport(remote.max), timeout=5, trust_env=False, follow_redirects=False))
    outbox = BotOutboxWorker(bot, client, auth, worker_id='load-outbox-' + str(index))
    outgoing = Counter()
    try:
        for phase, seconds in enumerate((0, 2, 4)):
            clock[0] = NOW + timedelta(seconds=seconds)
            outgoing.update(result.state for result in asyncio.run(_drain(outbox)))
            _barrier(directory, 'outbox_' + str(phase), index)
            if phase < 2:
                clock[0] = NOW + timedelta(seconds=seconds + 1)
                assert asyncio.run(outbox.run_once()) is None  # Retry-After must be respected.
                _barrier(directory, 'outbox_early_' + str(phase), index)
        clock[0] = NOW + timedelta(seconds=120)
        reopened = BotRepository(TeamDatabase(db.path), team, auth, clock=lambda: clock[0])
        reopened.recover_expired()
        replacement = BotOutboxWorker(reopened, client, auth, worker_id='load-reopened-outbox-' + str(index))
        assert asyncio.run(replacement.run_once()) is None  # Unknown POST is never replayed.
    finally:
        client.close()
    _barrier(directory, 'max_done', index)

    # Exactly two durable voice jobs now compete from separate real processes.
    clock[0] = NOW
    claim = voices.claim_job('load-voice-' + str(index), lease_seconds=60)
    assert claim is not None
    remote.active_voice_event = claim.event.event_id
    _barrier(directory, 'voice_claimed', index)
    asyncio.run(voice.execute(claim))
    paid_state = voices.get_job(claim.job_id)['state']
    assert paid_state in {'uncertain', 'paused_budget'}
    _barrier(directory, 'voice_done', index)
    assert voices.enqueue(claim.event, text=None) == claim.job_id
    recovered = VoiceRepository(TeamDatabase(db.path), team, bot, clock=lambda: NOW + timedelta(seconds=120))
    recovered.recover_expired()
    assert recovered.claim_job('load-reopened-voice-' + str(index)) is None
    repeat_blocked = False
    with db.connection() as conn:
        row = conn.execute('SELECT payload FROM voice_requests WHERE job_id=?', (claim.job_id,)).fetchone()
    if row is not None:
        try:
            voices.begin_request(claim, json.loads(row[0]))
        except VoiceError as error:
            assert error.code in {'voice_claim_lost', 'voice_request_already_started', 'voice_request_uncertain'}
            repeat_blocked = True
        else:
            raise AssertionError('persisted_paid_admission_reopened')
    _barrier(directory, 'cloud_done', index)
    return {'index': index, 'intakes': dict(intakes), 'inbox_processed': inbox_count,
        'max_receipts': dict(outgoing), 'voice_state': paid_state, 'persisted_repeat_blocked': repeat_blocked,
        'elapsed_ms': (time.perf_counter_ns() - started) / 1_000_000}


def validate_cloud(directory: Path, results) -> dict:
    """Parent verifies durable facts, rather than trusting child success strings."""
    directory = Path(directory)
    metrics = [item.get('cloud', item) for item in results]
    assert {item['index'] for item in metrics} == {0, 1}
    assert sum(sum(item['intakes'].values()) for item in metrics) == 60
    assert sum(item['intakes'].get('queued', 0) for item in metrics) == 10
    assert sum(item['intakes'].get('duplicate', 0) for item in metrics) == 50
    assert sum(item['inbox_processed'] for item in metrics) == 10
    assert sorted(item['voice_state'] for item in metrics) == ['paused_budget', 'uncertain']
    assert sum(item['persisted_repeat_blocked'] for item in metrics) == 1
    with sqlite3.connect(directory / 'team.sqlite3') as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute('SELECT COUNT(*) FROM bot_events').fetchone()[0] == 10
        assert conn.execute('SELECT COUNT(*) FROM bot_replies').fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM bot_event_state WHERE state='done'").fetchone()[0] == 10
        states = Counter(row['state'] for row in conn.execute('SELECT state FROM bot_reply_state'))
        assert states == {'sent': 7, 'uncertain': 2, 'retryable': 1}
        assert conn.execute('SELECT MAX(attempt) FROM bot_reply_state').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(*) FROM voice_jobs').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM voice_requests').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM voice_responses').fetchone()[0] == 0
        assert Counter(row[0] for row in conn.execute('SELECT state FROM voice_job_state')) == {
            'uncertain': 1, 'paused_budget': 1}
    with _remote(directory) as conn:
        attempts = conn.execute('SELECT * FROM load_cloud_attempts').fetchall()
        effects = conn.execute('SELECT * FROM load_cloud_effects').fetchall()
        assert Counter(row['provider'] for row in attempts) == {'max': 13, 'polza': 1}
        assert Counter(row['provider'] for row in effects) == {'max': 9, 'polza': 1}
        max_attempts = Counter(row['slot'] for row in attempts if row['provider'] == 'max')
        assert max_attempts == {0: 3, 1: 2, **{slot: 1 for slot in range(2, 10)}}
        assert len({row['operation_id'] for row in attempts if row['provider'] == 'max'}) == 10
        assert max(Counter((row['provider'], row['operation_id']) for row in effects).values()) == 1
        sends = [row for row in attempts if row['provider'] == 'max']
        assert {row['child'] for row in sends} == {0, 1}
        overlap = sum(a['child'] != b['child'] and a['started_ns'] < b['finished_ns']
            and b['started_ns'] < a['finished_ns'] for i, a in enumerate(sends) for b in sends[i + 1:])
        assert overlap > 0, 'MAX synthetic HTTP calls must overlap in the two real processes'
        paid = next(row for row in attempts if row['provider'] == 'polza')
        assert paid['budget_status'] == 'submitted' and paid['budget_reserved'] == RESERVE_MICRO
        assert paid['outcome'] == 'committed_timeout'
        participants = conn.execute("SELECT pid FROM load_cloud_ready WHERE phase='voice_claimed'").fetchall()
        assert len({row['pid'] for row in participants}) == 2
    budget = MonthlyBudget(BudgetRepository(directory / 'billing.sqlite3'), clock=lambda: NOW)
    snapshot = budget.snapshot()
    assert snapshot.confirmed_micro == 0 and snapshot.reserved_micro == RESERVE_MICRO
    assert snapshot.remaining_micro == 1200_000000 and snapshot.paused_code is None
    with budget.repository.transaction() as conn:
        charges = conn.execute('SELECT * FROM billing_charges').fetchall()
        assert len(charges) == 1 and charges[0]['status'] == 'uncertain'
        assert charges[0]['operation_id'] == paid['operation_id']
        assert charges[0]['request_hash'] == paid['request_hash']
    return {'webhook_deliveries': 60, 'unique_events': 10, 'duplicate_deliveries': 50,
        'unique_replies': 10, 'max_attempts': 13, 'max_effects': 9,
        'max_states': dict(states), 'paid_attempts': 1, 'paid_effects': 1,
        'unknown_reserved_rub': 1800, 'remaining_rub': 1200,
        'duplicate_effects': 0, 'budget_bypass': 0, 'unknown_replays': 0,
        'max_inflight_overlap_pairs': overlap,
        'synthetic_remote_latency_ms': _latencies(attempts),
        'synthetic_elapsed_ms': [item['elapsed_ms'] for item in metrics]}


def _latencies(attempts):
    values = sorted((row['finished_ns'] - row['started_ns']) / 1_000_000 for row in attempts)
    return {'count': len(values), 'p50': round(values[(len(values) - 1) // 2], 3),
        'p95': round(values[max(0, (len(values) * 95 + 99) // 100 - 1)], 3),
        'max': round(values[-1], 3)}
