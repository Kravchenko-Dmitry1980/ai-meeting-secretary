"""T13 offline E2E: actual repositories, services, ASGI and HTTP adapters.

Only native/Polza/MAX wire responses and a local normalized WAV boundary are
synthetic. No success receipt is injected into a repository by these tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from secretary.application.publication_delivery import PublicationDispatcher
from secretary.application.reminders import ReminderScheduler
from secretary.application.team_sync import TeamSyncService
from secretary.application.voice_commands import VoiceCommandService
from secretary.domain.team import TaskCommand, TeamMember
from secretary.infrastructure.max_media import ValidatedAudio
from secretary.infrastructure.notification_repository import NotificationRepository
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository
from secretary.infrastructure.team_sync_repository import TeamSyncRepository
from secretary.application.notification_sender import NotificationSender
from secretary.orchestration.notification_worker import NotificationWorker
from secretary.orchestration.publication_worker import PublicationWorker
from secretary.orchestration.team_worker import TeamWorker
from secretary.orchestration.voice_worker import VoiceWorker

from test_backend import auth
from test_task_publication_api import real_api_case, publication_case, source_case
from test_voice_budget import _case, _billing_rows, _count
from team_e2e_support import (ORIGIN, WEBHOOK_SECRET, callback_wire, internal_bridge,
    make_bot, make_max, make_native, message_wire, outbox, seed_task)


@pytest.fixture
def team_case(tmp_path):
    now = [datetime(2026, 10, 5, 6, 59, 30, tzinfo=timezone.utc)]
    team = TeamRepository(TeamDatabase(tmp_path / 'team.sqlite3'), clock=lambda: now[0])
    owner = TeamMember(id=str(uuid4()), display_name='Синтетический владелец', role='owner',
        max_user_id='9007199254740993', vikunja_user_id='21', project_ids=('7',))
    member = TeamMember(id=str(uuid4()), display_name='Синтетический исполнитель',
        max_user_id='9007199254740995', vikunja_user_id='22', project_ids=('7',))
    for value in (owner, member):
        team.upsert_member(value, expected_revision=None)
    return SimpleNamespace(team=team, now=now, owner=owner, member=member)


def _post_webhook(client, payload):
    response = client.post('/hooks/max', json=payload,
        headers={'X-Max-Bot-Api-Secret': WEBHOOK_SECRET})
    assert response.status_code == 200 and response.json() == {'ok': True}


async def _max_digest(team, owner, member, task, native, clock, maximum):
    """The source result enters the actual current scheduler and private outbox."""
    sync = TeamSyncService(TeamSyncRepository(team, clock=clock), native.client)
    assert sync.sync_once().state == 'ready'
    repository = NotificationRepository(team, clock=clock)
    snapshot = repository.planning_snapshot(('7',), now=clock(), resumed=True)
    planned = ReminderScheduler().plan(clock(), snapshot)
    intent, = repository.commit_plan(snapshot, tuple(planned), planned_at=clock())
    assert intent.rule == 'daily_digest' and intent.recipient_id == member.id
    assert [ref.task_id for ref in intent.task_refs] == [task.task_id]
    worker = NotificationWorker(repository, NotificationSender(team, {'7': native.client}, maximum.client,
        clock=clock), worker_id='t13-source-notification')
    sent = await worker.run_once()
    assert sent.state == 'sent' and sent.accepted_at is not None
    assert sent.accepted_at != clock().isoformat()
    assert repository.read(intent.notification_id)['receipt']['accepted_at'] == sent.accepted_at
    assert maximum.control.accepted[-1][0] == member.max_user_id
    assert task.title in maximum.control.accepted[-1][1]['text']
    assert repository.summary(owner.id, '7')['human_read_confirmed'] is None
    assert await worker.run_once() is None and len(maximum.control.accepted) == 1
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_gateway_ack', [False, True])
async def test_source_http_preview_confirm_publication_native_final_get_and_max_receipt(real_api_case, lost_gateway_ack):
    c = real_api_case
    clock = lambda: c.source.time[0]
    native = make_native(c.source.team, clock)
    maximum = make_max(clock)
    try:
        headers = auth(c.client)
        context = c.client.get(c.root + '/context')
        assert context.status_code == 200
        prepared = c.client.post(c.root + '/preview', headers=headers, json={
            'scope': context.json()['scope'], 'selections': [
                {'action_id': c.source.action_id, 'due_resolution': 'none'}]})
        assert prepared.status_code == 200 and prepared.json()['preview']
        assert c.service.repository.claim_next('no-auto-publication') is None
        assert native.simulator.tasks == {}
        command = {**prepared.json()['preview'], 'operation_id': str(uuid4())}
        accepted = c.client.post(c.root, headers=headers, json=command)
        assert accepted.status_code == 202
        assert accepted.json()['items'][0]['execution_state']['state'] == 'queued'
        assert c.client.post(c.root, headers=headers, json=command).json() == accepted.json()
        assert native.simulator.tasks == {}
        with internal_bridge(c.source.team, c.source.owner) as wire:
            wire.control.lose_ack = lost_gateway_ack
            dispatcher = PublicationDispatcher(c.service.repository, wire.bridge,
                validate_dispatch=c.service.validate_dispatch)
            publication_worker = PublicationWorker(dispatcher, worker_id='t13-publication')
            submitted = await publication_worker.run_once()
            assert submitted.execution_state.state == ('uncertain' if lost_gateway_ack else 'queued')
            delivery_id = accepted.json()['items'][0]['delivery_operation_id']
            assert c.source.team.get_receipt(c.source.owner.id, delivery_id).execution_state.state == 'queued'
            # Execute the actual queued command, including every native primitive
            # and postcondition GET; no manual record_remote_result shortcut.
            applied = await native.worker.run_once()
            assert applied.execution_state.state == 'applied', applied.model_dump()
            assert applied.execution_state.verified_at is not None and applied.current is not None
            if lost_gateway_ack:
                # A fresh source repository/dispatcher must read the existing ID.
                from secretary.infrastructure.task_publication_repository import TaskPublicationRepository
                repository = TaskPublicationRepository(c.db, clock=clock)
                publication_worker = PublicationWorker(PublicationDispatcher(repository, wire.bridge,
                    validate_dispatch=c.service.validate_dispatch), worker_id='t13-publication-restart')
            await publication_worker.run_once()
            delivered = c.client.get(c.root + '/' + command['operation_id'])
            assert delivered.status_code == 200
            item = delivered.json()['items'][0]
            assert item['execution_state']['state'] == 'applied'
            assert item['gateway_receipt']['execution_state']['state'] == 'applied'
            assert item['execution_state']['task_id'] == applied.current.task_id
            assert applied.current.title == prepared.json()['preview']['items'][0]['title']
            assert applied.current.assignee_id == c.source.member.id and applied.current.due_confirmed
            assert c.source.source.text in applied.current.description
            assert native.simulator.requests[-1].method == 'GET'
            assert len([r for r in wire.control.requests if r.method == 'POST']) == 1
            assert len([m for m in native.simulator.mutations if m == ('POST', '/api/v2/projects/7/tasks')]) == 1
            assert len(native.simulator.tasks) == 1
            assert await native.worker.run_once() is None
            await _max_digest(c.source.team, c.source.owner, c.source.member, applied.current, native, clock, maximum)
            assert c.client.post(c.root, headers=headers, json=command).json()['items'][0]['execution_state']['state'] == 'applied'
            assert len(native.simulator.tasks) == 1
    finally:
        native.http.close()
        maximum.http.close()


class _LocalWave:
    """Already-normalized synthetic audio; no CDN/download/FFmpeg qualification."""
    def __init__(self, path):
        self.path, self.calls = path, []

    def download_voice(self, event, *, cancelled=None):
        assert cancelled is None or not cancelled()
        with wave.open(str(self.path), 'rb') as audio:
            assert audio.getparams()[:3] == (1, 2, 16000)
            duration = audio.getnframes() * 1000 // audio.getframerate()
        self.calls.append(event.event_id)
        payload = self.path.read_bytes()
        return ValidatedAudio(self.path, duration, hashlib.sha256(payload).hexdigest(), len(payload), event.event_id)


@pytest.mark.asyncio
async def test_synthetic_wav_real_paid_stt_intent_preview_confirm_native_and_private_max(tmp_path, monkeypatch):
    literal, title = 'Создай задачу проверить смету и назначь мне', 'проверить смету'
    paid, c = [], None
    def provider(request):
        stage = 'stt' if request.url.path.endswith('/audio/transcriptions') else 'intent'
        assert request.method == 'POST'
        with c.db.connection() as conn:
            pending = conn.execute('SELECT operation_id FROM voice_requests WHERE stage=?', (stage,)).fetchone()
        assert pending is not None and c.budget.reservation(pending[0]).status == 'submitted'
        paid.append((stage, pending[0]))
        if stage == 'stt':
            assert json.loads(request.content)['file'].startswith('data:audio/wav;base64,')
            return httpx.Response(200, json={'id': 'gen_t13_stt', 'text': literal,
                'duration': 1, 'usage': {'cost_rub': .02}})
        proposal = {'action': 'create', 'project_id': '7', 'task_id': None,
            'member_id': c.actor.id, 'person_mention': 'мне', 'title': title, 'text': None,
            'bucket': None, 'important': None, 'urgent': None, 'due_phrase': None,
            'proposed_due_at': None, 'evidence_quote': literal, 'utterance_kind': 'command', 'unresolved_fields': []}
        captured = json.loads(json.loads(request.content)['messages'][1]['content'])
        assert captured['literal_text'] == literal and captured['allowed_context']['actor_id'] == c.actor.id
        return httpx.Response(200, json={'id': 'gen_t13_intent', 'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': json.dumps({'proposals': [proposal]}, ensure_ascii=False)}}],
            'usage': {'cost_rub': .02}})
    c = _case(tmp_path, monkeypatch, provider)
    clock = lambda: c.clock[0]
    path = tmp_path / 't13-command.wav'
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 16000)
    media = _LocalWave(path)
    voice = VoiceCommandService(c.team, c.bot, c.voices, media, c.client, clock=clock)
    bot = make_bot(c.team, clock, voice=voice, auth=c.auth, repository=c.bot)
    maximum = make_max(clock)
    native = make_native(c.team, clock)
    processing = VoiceWorker(c.voices, voice, worker_id='t13-voice')
    try:
        with TestClient(bot.app, base_url=ORIGIN, client=('203.0.113.42', 45110)) as public:
            incoming = message_wire(c.actor, clock, identifier='t13-native-voice', media=[
                {'type': 'audio', 'payload': {'url': 'https://synthetic.invalid/t13.wav'}}])
            _post_webhook(public, incoming)
            _post_webhook(public, incoming)
            initial = await bot.inbox.run_once()
            assert initial is not None and 'Обрабатываю' in initial.text
            assert paid == [] and _count(c, 'team_commands') == 0
            preview = await processing.run_once()
            assert preview is not None and title in preview.text
            assert _count(c, 'team_commands') == 0 and native.simulator.mutations == []
            assert [stage for stage, _ in paid] == ['stt', 'intent']
            assert len({operation for _, operation in paid}) == 2
            assert _count(c, 'voice_requests') == _count(c, 'voice_responses') == 2
            charges = _billing_rows(c)
            assert len(charges) == 2 and all(row['status'] == 'confirmed' for row in charges)
            assert {row['operation_id'] for row in charges} == {operation for _, operation in paid}
            assert {row['category'] for row in charges} == {'voice_stt', 'voice_intent'}
            assert c.budget.snapshot().confirmed_micro == 40_000 and c.budget.snapshot().reserved_micro == 0
            initial_sender = outbox(bot, maximum.client)
            voice_sender = outbox(bot, maximum.client, voices=c.voices)
            assert await voice_sender.run_once() is None
            assert (await initial_sender.run_once()).state == 'sent'
            for _ in range(3):
                assert (await voice_sender.run_once()).state == 'sent'
            preview_wire_body = maximum.control.accepted[-1][1]
            assert preview_wire_body['text'] == preview.text
            keyboard, = [attachment for attachment in preview_wire_body['attachments']
                if attachment['type'] == 'inline_keyboard']
            confirm = next(button for row in keyboard['payload']['buttons']
                for button in row if button['text'] == 'Подтвердить 1')
            expected_nonce = next(button.payload for row in preview.buttons for button in row
                if button.text == 'Подтвердить 1')
            assert confirm['type'] == 'callback'
            if confirm['payload'] != expected_nonce:
                pytest.fail('Delivered MAX confirmation nonce does not match durable preview')
            with c.db.connection() as conn:
                notices = conn.execute('SELECT payload FROM voice_notices').fetchall()
            assert len(notices) == len(maximum.control.accepted) - 1 == 3
            assert any(preview.text == json.loads(row[0])['text'] for row in notices)
            accepted = []
            before_budget = c.budget.snapshot()
            for index in range(2):
                _post_webhook(public, callback_wire(c.actor, clock,
                    identifier='t13-confirm-' + str(index), payload=confirm['payload']))
                reply = await bot.inbox.run_once()
                assert reply.receipt.execution_state.state == 'queued'
                accepted.append(reply.receipt)
                assert (await initial_sender.run_once()).state == 'sent'
            assert accepted[0].acceptance_receipt == accepted[1].acceptance_receipt
            assert _count(c, 'team_commands') == 1 and c.budget.snapshot() == before_budget
            applied = await native.worker.run_once()
            assert applied.execution_state.state == 'applied', applied.model_dump()
            assert applied.current.title == title and applied.current.assignee_id == c.actor.id
            assert applied.current.task_id != c.task.task_id and c.team.get_projection(c.actor.id, c.task.task_id) == c.task
            assert len(native.simulator.tasks) == 1
            assert len([m for m in native.simulator.mutations if m == ('POST', '/api/v2/projects/7/tasks')]) == 1
            assert len([m for m in native.simulator.mutations if m == ('POST', '/api/v2/tasks/' + applied.current.task_id + '/assignees')]) == 1
            _post_webhook(public, callback_wire(c.actor, clock, identifier='t13-confirm-after-applied', payload=confirm['payload']))
            replay = await bot.inbox.run_once()
            assert replay.receipt.execution_state.state == 'applied'
            assert (await initial_sender.run_once()).state == 'sent'
            assert await native.worker.run_once() is None and len(paid) == 2
            assert c.budget.snapshot() == before_budget and len(media.calls) == 1
            assert all(user == c.actor.max_user_id for user, _ in maximum.control.accepted)
            assert 'Изменение подтверждено проверкой карточки' in maximum.control.accepted[-1][1]['text']
    finally:
        native.http.close()
        maximum.http.close()


def _reminders(c, native):
    repository = NotificationRepository(c.team, clock=lambda: c.now[0])
    sync = TeamSyncService(TeamSyncRepository(c.team, clock=lambda: c.now[0]), native.client)
    assert sync.sync_once().state == 'ready'
    first = repository.planning_snapshot(('7',), now=c.now[0])
    repository.commit_plan(first, (), planned_at=c.now[0])
    c.now[0] += timedelta(seconds=30)
    assert sync.sync_once().state == 'ready'
    def enqueue():
        snapshot = repository.planning_snapshot(('7',), now=c.now[0])
        due = tuple(intent for intent in ReminderScheduler().plan(c.now[0], snapshot) if intent.rule == 'due_24h')
        assert len(due) == 1
        return repository.commit_plan(snapshot, due, planned_at=c.now[0])[0]
    return repository, sync, enqueue


@pytest.mark.asyncio
async def test_actual_owner_due_command_cancels_old_reminder_and_sends_new_generation(team_case):
    c = team_case
    clock = lambda: c.now[0]
    native, maximum = make_native(c.team, clock), make_max(clock)
    try:
        task = seed_task(native, c.team, c.owner, c.member,
            due_at=c.now[0] + timedelta(hours=24, seconds=30))
        reminders, sync, enqueue = _reminders(c, native)
        old = enqueue()
        old_generation = reminders.due_revision(task.task_id)
        new_due = task.due_at + timedelta(seconds=60)
        command = TaskCommand(operation_id=str(uuid4()), action='set_due', project_id='7', task_id=task.task_id,
            expected_revision=task.revision, expected_fingerprint=task.remote_fingerprint,
            values={'due_at': new_due, 'due_confirmed': True, 'reason': 'Владелец согласовал новое время'})
        queued = c.team.accept_command(c.owner.id, command, expected_actor_revision=c.owner.revision)
        assert queued.execution_state.state == 'queued'
        assert reminders.read(old.notification_id)['state'] == 'pending'
        applied = await native.worker.run_once()
        assert applied.execution_state.state == 'applied', applied.model_dump()
        assert applied.current.due_at == new_due and applied.current.due_confirmed
        assert reminders.due_revision(task.task_id) == old_generation + 1
        assert reminders.read(old.notification_id)['state'] == 'cancelled'
        assert len(native.simulator.comments[task.task_id]) == 1
        assert 'Владелец согласовал новое время' in native.simulator.comments[task.task_id][0]['comment']
        worker = NotificationWorker(reminders, NotificationSender(c.team, {'7': native.client}, maximum.client,
            clock=clock), worker_id='t13-notification')
        assert await worker.run_once() is None and maximum.control.requests == []
        c.now[0] += timedelta(seconds=60)
        assert sync.sync_once().state == 'ready'
        new = enqueue()
        assert new.notification_id != old.notification_id and new.task_refs[0].due_revision == old_generation + 1
        assert (await worker.run_once()).state == 'sent'
        assert reminders.read(new.notification_id)['state'] == 'sent'
        assert len(maximum.control.accepted) == 1 and maximum.control.accepted[0][0] == c.member.max_user_id
        assert reminders.read(old.notification_id)['state'] == 'cancelled'
        assert c.team.get_projection(c.owner.id, task.task_id) == applied.current
    finally:
        native.http.close()
        maximum.http.close()


@pytest.mark.asyncio
async def test_accepted_native_create_lost_ack_restart_retains_uncertainty_without_second_post(team_case):
    c = team_case
    clock = lambda: c.now[0]
    lost = []
    def lose_once(request, response):
        if request.method == 'POST' and request.url.path == '/api/v2/projects/7/tasks' and not lost:
            assert response.status_code == 201
            lost.append(request)
            raise httpx.ReadTimeout('Synthetic accepted native create ACK lost', request=request)
        return response
    native = make_native(c.team, clock, transport_hook=lose_once)
    try:
        command = TaskCommand(operation_id=str(uuid4()), action='create', project_id='7',
            expected_assignee_revision=c.member.revision, values={'title': 'Принята без ACK', 'assignee_id': c.member.id})
        c.team.accept_command(c.owner.id, command)
        uncertain = await native.worker.run_once()
        assert uncertain.execution_state.state == 'uncertain'
        assert len(native.simulator.tasks) == 1 and len(lost) == 1
        with c.team.db.connection() as conn:
            steps = conn.execute("SELECT event FROM team_journal WHERE operation_id=? AND event='remote_step_uncertain'",
                (command.operation_id,)).fetchall()
        assert len(steps) == 1
        reopened = TeamRepository(TeamDatabase(c.team.db.path), clock=clock)
        from secretary.application.team_tasks import TeamTaskService
        replacement = TeamWorker(reopened, TeamTaskService(reopened, native.client), worker_id='t13-native-restart')
        assert await replacement.run_once() is None
        # Explicit reconciliation is GET-only; a partial create never authorizes
        # the previously unstarted assignee POST.
        mutation_count = len(native.simulator.mutations)
        checked = await replacement.reconcile_once(command.operation_id)
        assert checked.execution_state.state == 'uncertain'
        assert len(native.simulator.mutations) == mutation_count == 1
        assert len(native.simulator.tasks) == 1
        assert list(native.simulator.tasks.values())[0]['assignees'] == []
        assert reopened.get_receipt(c.owner.id, command.operation_id).execution_state.state == 'uncertain'
    finally:
        native.http.close()


@pytest.mark.asyncio
async def test_accepted_max_reminder_lost_ack_restart_has_one_attempt_and_no_second_post(team_case):
    c = team_case
    clock = lambda: c.now[0]
    native, maximum = make_native(c.team, clock), make_max(clock)
    try:
        task = seed_task(native, c.team, c.owner, c.member,
            due_at=c.now[0] + timedelta(hours=24, seconds=30))
        reminders, _, enqueue = _reminders(c, native)
        intent = enqueue()
        maximum.control.mode = 'accepted_lost_ack'
        sender = NotificationSender(c.team, {'7': native.client}, maximum.client, clock=clock)
        receipt = await NotificationWorker(reminders, sender, worker_id='t13-notification').run_once()
        assert receipt.state == 'uncertain'
        assert reminders.read(intent.notification_id)['state'] == 'uncertain'
        assert len(maximum.control.accepted) == 1
        c.now[0] += timedelta(minutes=3)
        reopened = TeamRepository(TeamDatabase(c.team.db.path), clock=clock)
        saved = NotificationRepository(reopened, clock=clock)
        maximum.control.mode = 'sent'
        replacement = NotificationWorker(saved, NotificationSender(reopened, {'7': native.client},
            maximum.client, clock=clock), worker_id='t13-notification-restart')
        for _ in range(3):
            assert await replacement.run_once() is None
        assert saved.read(intent.notification_id)['state'] == 'uncertain'
        assert len(maximum.control.requests) == len(maximum.control.accepted) == 1
        with reopened.db.connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM notification_attempts WHERE notification_id=?', (intent.notification_id,)).fetchone()[0] == 1
            recorded = json.loads(conn.execute('SELECT receipt FROM notification_receipts WHERE notification_id=?', (intent.notification_id,)).fetchone()[0])
        assert recorded['state'] == 'uncertain' and recorded['accepted_at'] is None
        assert reopened.get_projection(c.owner.id, task.task_id) == task
    finally:
        native.http.close()
        maximum.http.close()
