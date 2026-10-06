"""T10 synthetic end-to-end notification evidence through real adapters.

Only HTTPX MockTransport and an injected clock replace external boundaries.
The Team database, sync mapper, scheduler, outbox and worker are real. These
tests qualify offline contracts; they do not qualify a phone or live delivery.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from secretary.application.team_sync import TeamSyncService
from secretary.domain.cloud_budget import APPROVED_MONTHLY_MICRO, BudgetSnapshot
from secretary.domain.team import TaskSnapshot, TeamMember
from secretary.infrastructure.max_bot import MaxClient
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_dashboard_repository import TeamDashboardRepository
from secretary.infrastructure.team_repository import RepositoryMemberDirectory, TeamRepository
from secretary.infrastructure.team_sync_repository import TeamSyncRepository
from secretary.infrastructure.vikunja import ProjectBinding, TaskReadContext, VikunjaClient

UTC = timezone.utc
OWNER = '10000000-0000-4000-8000-000000000001'
MEMBER = '10000000-0000-4000-8000-000000000002'
TASK_ID = '9007199254740997'
MAX_USER_ID = '9007199254740993'
BOT_ID = '99'


def implementation():
    names = ('secretary.infrastructure.notification_repository',
             'secretary.application.notification_sender',
             'secretary.orchestration.notification_worker',
             'secretary.application.reminders')
    for name in names:
        assert importlib.util.find_spec(name) is not None, f'T10 implementation absent: {name}'
    return SimpleNamespace(**dict(zip(('storage', 'sender', 'worker', 'planner'),
                                     (importlib.import_module(name) for name in names))))


def rfc3339(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


@pytest.fixture
def case(tmp_path, request):
    modules = implementation()
    options = getattr(request, 'param', {})
    target = options.get('target', datetime(2026, 10, 5, 7, tzinfo=UTC))
    now = [target-options.get('gap', timedelta(seconds=30))]
    db = TeamDatabase(tmp_path / 'synthetic-notifications.sqlite3')
    team = TeamRepository(db, clock=lambda: now[0])
    owner = TeamMember(id=OWNER, display_name='Synthetic owner', role='owner',
                       max_user_id='9007199254740995', vikunja_user_id='21', project_ids=('7',))
    member = TeamMember(id=MEMBER, display_name='Synthetic member',
                        max_user_id=MAX_USER_ID, vikunja_user_id='22', project_ids=('7',))
    for row in (owner, member):
        team.upsert_member(row, expected_revision=None)
    binding = ProjectBinding('7', '17', '18',
        dict(zip(('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'),
                 map(str, range(31, 38)))), '41', '42', '43')
    remote = {TASK_ID: dict(id=int(TASK_ID), project_id=7,
        title='Original synthetic task', description='<p>Synthetic detail</p>',
        done=False, due_date=rfc3339(target + timedelta(hours=24)),
        assignees=[{'id': 22}], labels=[], buckets=[{'id': 32, 'project_view_id': 17}],
        repeat_after=0, repeat_mode=0)}
    control = SimpleNamespace(vikunja_requests=[], max_requests=[], max_mode='sent',
        accepted_messages=[], task_read_hook=None, expected_max_user_id=MAX_USER_ID,
        native_timestamp=int((target - timedelta(seconds=17)).timestamp() * 1000))

    def vikunja_transport(request):
        control.vikunja_requests.append(request)
        assert request.method == 'GET', 'Notifications must never mutate native tasks'
        path = request.url.path.removeprefix('/api/v2')
        view = dict(id=17, project_id=7, view_kind='kanban',
                    bucket_configuration_mode='manual', done_bucket_id=0, default_bucket_id=31)
        if path == '/user':
            return httpx.Response(200, json={'id': 18, 'bot_owner_id': 19})
        if path == '/projects/7/views/17':
            return httpx.Response(200, json=view)
        if path == '/projects/7/views':
            return httpx.Response(200, json=dict(items=[view], page=1, per_page=50, total=1, total_pages=1))
        if path == '/projects/7/views/17/buckets':
            return httpx.Response(200, json={'items': [{'id': n, 'project_view_id': 17} for n in range(31, 38)]})
        if path.startswith('/labels/'):
            identifier = int(path.rsplit('/', 1)[1])
            return httpx.Response(200, json={'id': identifier, 'created_by': {'id': 18},
                'title': {41: 'secretary:important', 42: 'secretary:urgent', 43: 'secretary:cancelled'}[identifier]})
        if path == '/projects/7/tasks':
            return httpx.Response(200, json=dict(items=list(remote.values()), page=1,
                per_page=50, total=len(remote), total_pages=1))
        if path.startswith('/tasks/') and path.endswith('/comments'):
            return httpx.Response(200, json=dict(items=[], page=1, per_page=50, total=0, total_pages=0))
        if path.startswith('/tasks/'):
            identifier = path.split('/')[2]
            if control.task_read_hook is not None:
                hook, control.task_read_hook = control.task_read_hook, None
                hook()
            return httpx.Response(200, json=remote[identifier])
        pytest.fail(f'Unexpected synthetic Vikunja endpoint: {request.method} {path}')

    def max_transport(request):
        assert request.method == 'POST' and request.url.path == '/messages'
        assert str(request.url).startswith('https://platform-api2.max.ru/messages?')
        assert request.url.params['user_id'] == control.expected_max_user_id
        assert request.url.params['disable_link_preview'] == 'true'
        body = json.loads(request.content)
        control.max_requests.append(request)
        if control.max_mode == 'rate_limited':
            return httpx.Response(429, headers={'Retry-After': '2'})
        control.accepted_messages.append(body)
        if control.max_mode == 'accepted_lost_response':
            raise httpx.ReadTimeout('Synthetic accepted response lost', request=request)
        return httpx.Response(200, json={'message': {'sender': {'user_id': int(BOT_ID)},
            'recipient': {'user_id': int(control.expected_max_user_id), 'chat_type': 'dialog', 'chat_id': 1},
            'timestamp': control.native_timestamp,
            'body': {'mid': f'synthetic-mid-{len(control.accepted_messages)}', 'seq': 1,
                     'text': body['text']}}})

    native_http = httpx.Client(base_url='http://127.0.0.1:34891/api/v2',
        headers={'Authorization': 'Bearer SYNTHETIC_VIKUNJA_FIXTURE'},
        trust_env=False, follow_redirects=False, timeout=5,
        transport=httpx.MockTransport(vikunja_transport))
    max_http = httpx.Client(trust_env=False, follow_redirects=False, timeout=5,
                           transport=httpx.MockTransport(max_transport))
    native = VikunjaClient(native_http, binding=binding, members=RepositoryMemberDirectory(team))
    max_client = MaxClient('SYNTHETIC_MAX_FIXTURE_NOT_A_CREDENTIAL', bot_id=BOT_ID, client=max_http)
    observed = native.observe_task(TASK_ID)
    initial = TaskSnapshot(task_id=TASK_ID, project_id='7', revision=0,
        remote_fingerprint=observed.remote_fingerprint, title=observed.title,
        description=observed.description, assignee_id=MEMBER, bucket='accepted',
        important=False, urgent=False, classification_confirmed=True,
        due_at=observed.due_at, due_confirmed=True)
    team.save_projection(native.get_task(TASK_ID,
        context=TaskReadContext(0, baseline=initial)), expected_revision=None)
    c = SimpleNamespace(modules=modules, now=now, db=db, team=team, owner=owner,
        member=member, remote=remote, control=control, native=native, max_client=max_client)
    reopen_runtime(c)
    assert c.sync_service.sync_once().state == 'ready'
    snapshot = c.repository.planning_snapshot(('7',), now=now[0])
    previous = (tuple(c.modules.planner.ReminderScheduler().plan(now[0], snapshot))
                if options.get('backlog') else ())
    c.backlog = c.repository.commit_plan(snapshot, previous, planned_at=now[0])
    now[0] = target
    assert c.sync_service.sync_once().state == 'ready'
    control.vikunja_requests.clear()
    yield c
    native_http.close()
    max_http.close()


def reopen_runtime(c):
    # Reopen the actual file and construct new outbox/worker objects, preserving
    # only synthetic providers and clock. No lifecycle helpers are involved.
    c.db = TeamDatabase(c.db.path)
    c.team = TeamRepository(c.db, clock=lambda: c.now[0])
    c.native.members = RepositoryMemberDirectory(c.team)
    c.sync = TeamSyncRepository(c.team, clock=lambda: c.now[0])
    c.sync_service = TeamSyncService(c.sync, c.native)
    c.repository = c.modules.storage.NotificationRepository(c.team, clock=lambda: c.now[0])
    c.sender = c.modules.sender.NotificationSender(c.team, {'7': c.native}, c.max_client,
                                                   clock=lambda: c.now[0])
    c.worker = c.modules.worker.NotificationWorker(c.repository, c.sender,
                                                    worker_id='synthetic-notifications')


def current(c):
    return c.team.get_projection(OWNER, TASK_ID)


def record_current_native(c, **confirmed_changes):
    previous = current(c)
    baseline = previous.model_copy(update=confirmed_changes)
    mapped = c.native.get_task(TASK_ID,
        context=TaskReadContext(previous.revision + 1, baseline=baseline))
    c.team.save_projection(mapped, expected_revision=previous.revision)
    assert c.sync_service.sync_once().state == 'ready'
    return mapped


def enqueue(c, rule='due_24h', *, resumed=False):
    at = c.now[0]
    snapshot = c.repository.planning_snapshot(('7',), now=at, resumed=resumed)
    planned = c.modules.planner.ReminderScheduler().plan(at, snapshot)
    selected = tuple(item for item in planned if item.rule == rule)
    assert selected, f'Expected actual scheduler candidate {rule}'
    stored = c.repository.commit_plan(snapshot, selected, planned_at=at)
    return stored


def body(c, index=0):
    return json.loads(c.control.max_requests[index].content)


def persisted(c, table, notification_id):
    assert table in {'notification_attempts', 'notification_receipts'}
    with c.db.connection() as conn:
        return tuple(dict(row) for row in conn.execute(
            f'SELECT * FROM {table} WHERE notification_id=? ORDER BY attempt', (notification_id,)))


def assert_no_task_mutation(c, snapshot):
    assert current(c) == snapshot
    assert all(request.method == 'GET' for request in c.control.vikunja_requests)
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 0


def test_cross_boundary_implementation_exists():
    implementation()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', [{'gap': timedelta(days=5), 'backlog': True}], indirect=True)
async def test_restart_sends_one_actual_current_digest_without_backlog(case):
    c = case
    assert len(c.backlog) == 1 and c.backlog[0].rule == 'daily_digest'
    old = c.backlog[0]
    reopen_runtime(c)
    intents = enqueue(c, 'daily_digest', resumed=True)
    assert len(intents) == 1
    assert intents[0].scheduled_at.date() == c.now[0].date()
    assert old.task_refs[0].due_revision == intents[0].task_refs[0].due_revision
    snapshot = current(c)
    for _ in range(3):
        await c.worker.run_once()
    assert len(c.control.max_requests) == 1
    assert snapshot.title in body(c)['text']
    assert len(c.control.accepted_messages) == 1
    assert c.repository.read(old.notification_id)['state'] == 'cancelled'
    assert c.repository.read(intents[0].notification_id)['state'] == 'sent'
    assert enqueue(c, 'daily_digest', resumed=True)[0].notification_id == intents[0].notification_id
    await c.worker.run_once()
    assert len(c.control.max_requests) == 1
    assert_no_task_mutation(c, snapshot)


@pytest.mark.asyncio
async def test_actual_post_uses_current_renamed_title_without_new_due_generation(case):
    c = case
    intent, = enqueue(c)
    generation = c.repository.due_revision(TASK_ID)
    old_title = current(c).title
    c.remote[TASK_ID]['title'] = 'Current renamed synthetic title <literal>'
    assert c.sync_service.sync_once().state == 'ready'
    snapshot = current(c)
    assert c.repository.due_revision(TASK_ID) == generation
    await c.worker.run_once()
    assert len(c.control.max_requests) == 1
    assert snapshot.title in body(c)['text'] and old_title not in body(c)['text']
    assert body(c)['notify'] is True
    state = c.repository.read(intent.notification_id)
    assert state['state'] == 'sent' and state['receipt']['message_id'] == 'synthetic-mid-1'
    attempt, = persisted(c, 'notification_attempts', intent.notification_id)
    sent = json.loads(attempt['send_payload'])
    assert sent['text'] == body(c)['text'] and sent['text'] != intent.text
    assert sent['operation_id'] == intent.notification_id and sent['user_id'] == MAX_USER_ID
    receipt, = persisted(c, 'notification_receipts', intent.notification_id)
    assert json.loads(receipt['receipt'])['message_id'] == 'synthetic-mid-1'
    assert_no_task_mutation(c, snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['closed', 'due', 'assignee', 'revoked'])
async def test_changed_task_or_revoked_recipient_after_plan_never_posts(case, change):
    c = case
    intent, = enqueue(c)
    if change == 'closed':
        c.remote[TASK_ID].update(done=True, buckets=[{'id': 36, 'project_view_id': 17}])
        assert c.sync_service.sync_once().state == 'ready'
    elif change == 'due':
        new_due = current(c).due_at + timedelta(days=1)
        c.remote[TASK_ID]['due_date'] = rfc3339(new_due)
        record_current_native(c, due_at=new_due, due_confirmed=True)
    elif change == 'assignee':
        c.remote[TASK_ID]['assignees'] = [{'id': 21}]
        assert c.sync_service.sync_once().state == 'ready'
    else:
        previous = c.team.get_member(MEMBER)
        c.team.upsert_member(previous.model_copy(update={'revision': previous.revision+1,
                            'enabled': False}), expected_revision=previous.revision)
    snapshot = current(c)
    await c.worker.run_once()
    assert c.control.max_requests == [] and c.control.accepted_messages == []
    assert c.repository.read(intent.notification_id)['state'] == 'cancelled'
    assert_no_task_mutation(c, snapshot)


@pytest.mark.asyncio
async def test_fresh_native_get_mismatch_defers_without_post_or_projection_overwrite(case):
    c = case
    intent, = enqueue(c)
    baseline = current(c)
    c.remote[TASK_ID]['title'] = 'Native rename after the last canonical sync'
    c.control.vikunja_requests.clear()
    await c.worker.run_once()
    assert any(request.url.path == f'/api/v2/tasks/{TASK_ID}' for request in c.control.vikunja_requests)
    assert not c.control.max_requests
    state = c.repository.read(intent.notification_id)
    assert state['state'] == 'pending' and state['attempt'] == 0 and state['receipt'] is None
    assert_no_task_mutation(c, baseline)


@pytest.mark.asyncio
async def test_known_429_has_exactly_three_bounded_posts_and_does_not_mutate_tasks(case):
    c = case
    intent, = enqueue(c)
    c.control.max_mode = 'rate_limited'
    baseline = current(c)
    for attempt in range(1, 4):
        await c.worker.run_once()
        assert len(c.control.max_requests) == attempt
        await c.worker.run_once()  # Retry-After has not elapsed yet.
        assert len(c.control.max_requests) == attempt
        c.now[0] += timedelta(seconds=2)
        assert c.sync_service.sync_once().state == 'ready'
    for _ in range(2):
        await c.worker.run_once()
    state = c.repository.read(intent.notification_id)
    assert state['attempt'] == 3 and state['state'] == 'failed'
    assert len(c.control.max_requests) == 3 and c.control.accepted_messages == []
    assert len(persisted(c, 'notification_attempts', intent.notification_id)) == 3
    assert len(persisted(c, 'notification_receipts', intent.notification_id)) == 3
    assert all(json.loads(request.content) == body(c) for request in c.control.max_requests)
    assert_no_task_mutation(c, baseline)


@pytest.mark.asyncio
async def test_uncertain_send_does_not_loop(case):
    c = case
    intent, = enqueue(c)
    c.control.max_mode = 'accepted_lost_response'
    baseline = current(c)
    await c.worker.run_once()
    assert len(c.control.accepted_messages) == len(c.control.max_requests) == 1
    assert c.repository.read(intent.notification_id)['state'] == 'uncertain'
    c.now[0] += timedelta(seconds=120)
    c.control.max_mode = 'sent'
    reopen_runtime(c)
    assert c.sync_service.sync_once().state == 'ready'
    for _ in range(3):
        await c.worker.run_once()
    assert len(c.control.max_requests) == 1
    assert c.repository.read(intent.notification_id)['state'] == 'uncertain'
    attempt, = persisted(c, 'notification_attempts', intent.notification_id)
    receipt, = persisted(c, 'notification_receipts', intent.notification_id)
    assert json.loads(attempt['send_payload'])['text'] == body(c)['text']
    stored = json.loads(receipt['receipt'])
    assert stored['state'] == 'uncertain' and stored['accepted_at'] is None
    assert_no_task_mutation(c, baseline)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', [{'target': datetime(2026, 10, 5, 17, 59, 59, tzinfo=UTC)}], indirect=True)
async def test_packet_delayed_into_quiet_hours_merges_into_one_current_morning_digest(case):
    c = case
    individual, = enqueue(c)
    c.now[0] += timedelta(seconds=1)  # 21:00 Moscow, after planning.
    assert c.sync_service.sync_once().state == 'ready'
    await c.worker.run_once()
    assert c.control.max_requests == []
    assert c.repository.read(individual.notification_id)['attempt'] == 0
    c.now[0] = datetime(2026, 10, 6, 6, tzinfo=UTC)  # Next 09:00 Moscow.
    assert c.sync_service.sync_once().state == 'ready'
    digest, = enqueue(c, 'daily_digest', resumed=True)
    baseline = current(c)
    for _ in range(3):
        await c.worker.run_once()
    assert c.repository.read(individual.notification_id)['state'] == 'cancelled'
    assert c.repository.read(digest.notification_id)['state'] == 'sent'
    assert len(c.control.max_requests) == 1 and baseline.title in body(c)['text']
    assert_no_task_mutation(c, baseline)


@pytest.mark.asyncio
async def test_missing_polza_key_does_not_interfere_with_actual_notification_post(case, monkeypatch):
    monkeypatch.delenv('POLZA_API_KEY', raising=False)
    c = case
    intent, = enqueue(c)
    baseline = current(c)
    await c.worker.run_once()
    assert len(c.control.max_requests) == 1
    assert c.repository.read(intent.notification_id)['state'] == 'sent'
    assert baseline.title in body(c)['text']
    assert_no_task_mutation(c, baseline)


@pytest.mark.asyncio
async def test_due_away_and_back_between_plans_increments_generation_and_cancels_old_packet(case):
    c = case
    old, = enqueue(c)
    due = current(c).due_at
    generation = c.repository.due_revision(TASK_ID)
    c.remote[TASK_ID]['due_date'] = rfc3339(due + timedelta(days=1))
    record_current_native(c, due_at=due+timedelta(days=1), due_confirmed=True)
    c.remote[TASK_ID]['due_date'] = rfc3339(due)
    record_current_native(c, due_at=due, due_confirmed=True)
    assert c.repository.due_revision(TASK_ID) == generation+2
    assert current(c).due_at == due
    await c.worker.run_once()
    assert c.repository.read(old.notification_id)['state'] == 'cancelled'
    assert c.control.max_requests == []
    c.now[0] += timedelta(days=1)
    assert c.sync_service.sync_once().state == 'ready'
    new, = enqueue(c, 'daily_digest', resumed=True)
    assert new.notification_id != old.notification_id
    assert new.task_refs[0].due_revision == generation+2
    baseline = current(c)
    await c.worker.run_once()
    assert len(c.control.max_requests) == 1 and baseline.title in body(c)['text']
    assert c.repository.read(new.notification_id)['state'] == 'sent'
    assert_no_task_mutation(c, baseline)


@pytest.mark.asyncio
async def test_native_max_timestamp_is_persisted_as_sent_dashboard_evidence(case):
    c = case
    intent, = enqueue(c)
    await c.worker.run_once()
    state = c.repository.read(intent.notification_id)
    assert state['state'] == 'sent'
    native_time = datetime.fromtimestamp(c.control.native_timestamp/1000, UTC)
    assert native_time != c.now[0]
    assert datetime.fromisoformat(state['receipt']['accepted_at']) == native_time
    receipt, = persisted(c, 'notification_receipts', intent.notification_id)
    assert datetime.fromisoformat(receipt['accepted_at']) == native_time
    assert datetime.fromisoformat(receipt['received_at']) == c.now[0]
    summary = c.repository.summary(OWNER, '7')
    assert summary['state'] == 'observed' and summary['last_max_api_accepted_at'] == native_time
    assert summary['human_read_confirmed'] is None
    assert summary['pending_count'] == summary['sending_count'] == summary['uncertain_count'] == 0
    assert not any(key in summary for key in ('read_at', 'delivered_at', 'human_read'))
    requests_before_dashboard = len(c.control.vikunja_requests), len(c.control.max_requests)
    dashboard = TeamDashboardRepository(c.team, notifications=c.repository).dashboard(OWNER, '7')
    assert dashboard.notifications.state == 'observed'
    assert dashboard.notifications.last_max_api_accepted_at == native_time
    assert dashboard.notifications.pending_count == dashboard.notifications.sending_count == 0
    assert dashboard.notifications.human_read_confirmed is None
    assert 'synthetic-mid-1' not in dashboard.model_dump_json()
    assert requests_before_dashboard == (len(c.control.vikunja_requests), len(c.control.max_requests))
    assert len(c.control.max_requests) == 1


@pytest.mark.asyncio
async def test_budget_threshold_released_before_post_retains_same_key_for_later_confirmed_crossing(case):
    c = case
    cap, threshold = APPROVED_MONTHLY_MICRO, APPROVED_MONTHLY_MICRO//2
    budget = [BudgetSnapshot('2026-10', cap, cap, 0, threshold, cap-threshold, None)]
    # This local snapshot-reader port reports synthetic ledger observations;
    # notification lifecycle is real, and no cloud key or paid call is involved.
    c.sender.budget_reader = lambda: budget[0]
    c.control.expected_max_user_id = c.owner.max_user_id

    def plan_threshold():
        snapshot = c.repository.planning_snapshot(('7',), now=c.now[0], budget=budget[0])
        planned = c.modules.planner.ReminderScheduler().plan(c.now[0], snapshot)
        selected = tuple(item for item in planned if item.rule == 'budget_50')
        assert len(selected) == 1 and selected[0].recipient_id == OWNER
        return c.repository.commit_plan(snapshot, selected, planned_at=c.now[0])[0]

    intent = plan_threshold()
    baseline = current(c)
    budget[0] = BudgetSnapshot('2026-10', cap, cap, 0, 0, cap, None)
    await c.worker.run_once()
    assert c.control.max_requests == []
    state = c.repository.read(intent.notification_id)
    assert state['state'] == 'pending' and state['attempt'] == 0 and state['receipt'] is None
    budget[0] = BudgetSnapshot('2026-10', cap, cap, threshold, 0, cap-threshold, None)
    c.now[0] += timedelta(seconds=61)
    assert c.sync_service.sync_once().state == 'ready'
    renewed = plan_threshold()
    assert renewed.notification_id == intent.notification_id and renewed.dedup_key == intent.dedup_key
    await c.worker.run_once()
    assert len(c.control.max_requests) == 1 and '50%' in body(c)['text'] and '2026-10' in body(c)['text']
    assert c.repository.read(intent.notification_id)['state'] == 'sent'
    assert plan_threshold().notification_id == intent.notification_id
    await c.worker.run_once()
    assert len(c.control.max_requests) == 1
    assert len(persisted(c, 'notification_receipts', intent.notification_id)) == 1
    assert_no_task_mutation(c, baseline)
