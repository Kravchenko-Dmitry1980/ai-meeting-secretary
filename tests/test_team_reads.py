"""Public T9 read contracts against temporary real Team storage."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import importlib
import importlib.util
import http.cookiejar as cookiejar
import time as stdlib_time

import httpx
import pytest
from fastapi.testclient import TestClient

from secretary.domain.cloud_budget import BudgetSnapshot
from secretary.domain.team import TaskCommand, TaskSnapshot, TeamForbidden, TeamMember
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository
from secretary.infrastructure.team_auth_repository import AuthRepository
from secretary.interface.team_gateway import TeamGatewayClients, TeamGatewaySettings, create_team_app
from test_team_sync import case as sync_case


def uid():
    return str(uuid4())


def test_scoped_read_component_exists():
    assert importlib.util.find_spec('secretary.infrastructure.team_read_repository'), 'T9 read component missing'


@pytest.fixture
def case(tmp_path):
    name = 'secretary.infrastructure.team_read_repository'
    assert importlib.util.find_spec(name), 'T9 scoped read repository missing'
    cls = importlib.import_module(name).TeamReadRepository
    clock = [datetime(2026, 10, 4, 9, tzinfo=timezone.utc)]
    db = TeamDatabase(tmp_path / 'team.sqlite3')
    team = TeamRepository(db, clock=lambda: clock[0])
    owner = TeamMember(id=uid(), display_name='Owner', role='owner', max_user_id='11',
                       vikunja_user_id='21', project_ids=('7',))
    member = TeamMember(id=uid(), display_name='Member', max_user_id='12',
                        vikunja_user_id='22', project_ids=('7',))
    other = TeamMember(id=uid(), display_name='Colleague', max_user_id='13',
                       vikunja_user_id='23', project_ids=('7',))
    foreign = TeamMember(id=uid(), display_name='Foreign secret', max_user_id='14',
                         vikunja_user_id='24', project_ids=('8',))
    for value in (owner, member, other, foreign):
        team.upsert_member(value, expected_revision=None)
    task = TaskSnapshot(task_id='9007199254740997', project_id='7', revision=0,
        remote_fingerprint='a' * 64, title='Task', assignee_id=member.id,
        important=False, urgent=False, classification_confirmed=True)
    team.save_projection(task, expected_revision=None)
    reads = cls(team)
    return SimpleNamespace(cls=cls, clock=clock, db=db, team=team, reads=reads,
        owner=owner, member=member, other=other, foreign=foreign, task=task)


def command(c, actor=None, **changes):
    data = dict(operation_id=uid(), project_id='7', task_id=c.task.task_id,
        expected_revision=c.task.revision, expected_fingerprint=c.task.remote_fingerprint,
        action='set_state', values={'bucket': 'doing'})
    data.update(changes)
    result = TaskCommand(**data)
    c.team.accept_command((actor or c.member).id, result)
    return result


def test_directory_is_bounded_safe_and_scoped(case):
    c = case
    first = c.reads.members(c.owner.id, '7', limit=2, expected_actor_revision=0)
    second = c.reads.members(c.owner.id, '7', limit=2, after=first.next_cursor)
    assert len(first.items) == 2 and len(second.items) == 1
    assert second.next_cursor is None
    assert {v.id for v in first.items + second.items} == {c.owner.id, c.member.id, c.other.id}
    assert set(first.items[0].model_dump()) == {'id', 'display_name', 'revision'}
    assert c.foreign.display_name not in first.model_dump_json() + second.model_dump_json()


@pytest.mark.parametrize('actor,project', [('member', '7'), ('owner', '8'), ('foreign', '7')])
def test_directory_rechecks_owner_and_project(case, actor, project):
    with pytest.raises(TeamForbidden):
        case.reads.members(getattr(case, actor).id, project)


@pytest.mark.parametrize('method', ['members', 'status', 'history'])
def test_reads_reject_changed_actor_revision(case, method):
    c = case
    c.team.upsert_member(c.owner.model_copy(update={'revision': 1}), expected_revision=0)
    arg = c.task.task_id if method == 'history' else '7'
    with pytest.raises(TeamForbidden):
        getattr(c.reads, method)(c.owner.id, arg, expected_actor_revision=0)


def test_history_hides_others_attempts_and_exposes_verified_action(case):
    c = case
    private = command(c, c.owner)
    own = command(c, c.member)
    assert [x.operation_id for x in c.reads.history(c.member.id, c.task.task_id).items] == [own.operation_id]
    assert c.reads.history(c.other.id, c.task.task_id).items == ()
    claim = c.team.claim_command('worker')
    c.clock[0] += timedelta(seconds=5)
    applied = c.task.model_copy(update={'revision': 1, 'remote_fingerprint': 'b' * 64, 'bucket': 'doing'})
    c.team.record_remote_result(claim, state='applied', snapshot=applied)
    history = c.reads.history(c.other.id, c.task.task_id).items
    assert len(history) == 1 and history[0].event == 'applied'
    assert history[0].operation_id == private.operation_id
    assert history[0].actor_id == c.owner.id and history[0].actor_display_name == 'Owner'
    assert history[0].verified_at == c.clock[0]
    assert history[0].remote_occurred_at is None


def test_history_uses_current_label_but_keeps_immutable_actor_and_time(case):
    c = case
    cmd = command(c)
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'display_name': 'Renamed'}), expected_revision=0)
    c.clock[0] += timedelta(days=1)
    item = c.reads.history(c.owner.id, c.task.task_id).items[0]
    assert item.actor_id == c.member.id and item.actor_display_name == 'Renamed'
    assert item.recorded_at == datetime(2026, 10, 4, 9, tzinfo=timezone.utc)
    assert item.operation_id == cmd.operation_id and item.verified_at is None


def test_history_pagination_is_stable_and_cursor_bound_to_task(case):
    c = case
    for _ in range(4):
        command(c)
    first = c.reads.history(c.owner.id, c.task.task_id, limit=2)
    second = c.reads.history(c.owner.id, c.task.task_id, limit=2, after=first.next_cursor)
    assert len(first.items) == len(second.items) == 2
    assert len({x.id for x in first.items + second.items}) == 4 and second.next_cursor is None
    another = c.task.model_copy(update={'task_id': '55'})
    c.team.save_projection(another, expected_revision=None)
    with pytest.raises(ValueError):
        c.reads.history(c.owner.id, '55', after=first.next_cursor)


def test_history_orders_same_timestamp_by_durable_sequence(case):
    c = case
    commands = [command(c) for _ in range(12)]
    page = c.reads.history(c.owner.id, c.task.task_id)
    assert [item.operation_id for item in page.items] == [item.operation_id for item in reversed(commands)]


def test_history_created_task_uses_verified_execution_task_id(case):
    c = case
    cmd = command(c, c.owner, task_id=None, expected_revision=None, expected_fingerprint=None,
                  action='create', expected_assignee_revision=0,
                  values={'title': 'New task', 'assignee_id': c.member.id})
    claim = c.team.claim_command('worker')
    created = c.task.model_copy(update={'task_id': '777', 'title': 'New task'})
    c.team.record_remote_result(claim, state='applied', snapshot=created)
    items = c.reads.history(c.owner.id, '777').items
    assert {x.operation_id for x in items} == {cmd.operation_id}
    assert any(x.event == 'applied' for x in items)


def test_history_never_returns_raw_remote_plan_or_trusted_source_payload(case):
    c = case
    cmd = command(c)
    with c.db.transaction() as conn:
        c.team._journal(conn, cmd.operation_id, 'remote_step_started',
            {'before_image': {'private_remote_id': 'secret-remote-value'}, 'worker_id': 'private-worker'})
    text = c.reads.history(c.owner.id, c.task.task_id).model_dump_json()
    assert 'secret-remote-value' not in text and 'private-worker' not in text
    assert 'remote_step_started' not in text


def test_history_preserves_literal_submitted_values_without_default_fields(case):
    c = case
    literal = '<!-- unfinished <script> is literal text'
    cmd = command(c, action='comment', values={'comment': literal})
    page = c.reads.history(c.owner.id, c.task.task_id).model_dump(mode='json')
    assert page['items'][0]['values'] == {'comment': literal}
    assert page['items'][0]['operation_id'] == cmd.operation_id
    command(c, c.owner, action='set_due', values={'due_at': None, 'due_confirmed': True,
                                                'reason': 'Explicitly cleared'})
    newest = c.reads.history(c.owner.id, c.task.task_id).model_dump(mode='json')['items'][0]
    assert newest['values'] == {'due_at': None, 'due_confirmed': True, 'reason': 'Explicitly cleared'}


def test_cross_project_history_denied_before_any_events_are_returned(case):
    command(case)
    with pytest.raises(TeamForbidden):
        case.reads.history(case.foreign.id, case.task.task_id)


def test_disabled_members_are_excluded_without_mutating_directory(case):
    c = case
    c.team.upsert_member(c.other.model_copy(update={'revision': 1, 'enabled': False}), expected_revision=0)
    assert {m.id for m in c.reads.members(c.owner.id, '7').items} == {c.owner.id, c.member.id}
    assert c.team.get_member(c.other.id).enabled is False


def test_budget_read_revocation_does_not_release_owner_amounts(case):
    c = case
    def revoke_during_read():
        c.team.upsert_member(c.owner.model_copy(update={'revision': 1, 'role': 'member'}), expected_revision=0)
        return budget()
    with pytest.raises(TeamForbidden):
        c.cls(c.team, budget_reader=revoke_during_read).status(c.owner.id, '7')


def test_status_defaults_are_honest_and_command_verification_is_separate(case):
    c = case
    command(c)
    c.team.record_remote_result(c.team.claim_command('worker'), state='applied',
        snapshot=c.task.model_copy(update={'revision': 1, 'remote_fingerprint': 'b' * 64}))
    status = c.reads.status(c.owner.id, '7')
    assert status.sync.state == 'not_configured'
    assert status.sync.last_successful_sync_at is None
    assert status.sync.last_command_verified_at == c.clock[0]
    assert status.cloud.state == 'not_configured' and status.cloud.remaining_rub is None


def budget(paused=None, remaining=123456789):
    return BudgetSnapshot('2026-10', 3000000000, 3000000000, 1234567, 9876543, remaining, paused)


def test_budget_exact_decimal_aggregates_owner_only(case):
    c = case
    reads = c.cls(c.team, budget_reader=lambda: budget())
    own = reads.status(c.owner.id, '7').cloud
    assert own.state == 'ready' and own.spend_rub == '1.234567'
    assert own.reserved_rub == '9.876543' and own.remaining_rub == '123.456789'
    assert own.effective_budget_rub == '3000.000000'
    member = reads.status(c.member.id, '7').cloud
    assert member.state == 'ready'
    assert member.spend_rub is member.reserved_rub is member.remaining_rub is member.effective_budget_rub is None


@pytest.mark.parametrize('pause,remaining,want', [(None, 0, 'monthly_budget_exhausted'),
    ('monthly_budget_stale', 0, 'monthly_budget_stale'), ('secret-provider-message', 1, 'monthly_budget_unavailable')])
def test_budget_pause_codes_are_truthful_and_sanitized(case, pause, remaining, want):
    view = case.cls(case.team, budget_reader=lambda: budget(pause, remaining)).status(case.owner.id, '7')
    assert view.cloud.state == 'paused' and view.cloud.paused_reason == want
    assert 'secret-provider-message' not in view.model_dump_json()


def test_budget_reader_failure_does_not_expose_exception_or_claim_ready(case):
    def broken():
        raise RuntimeError('provider-token-secret')
    view = case.cls(case.team, budget_reader=broken).status(case.owner.id, '7')
    assert view.cloud.state == 'unavailable' and view.cloud.spend_rub is None
    assert 'provider-token-secret' not in view.model_dump_json()


@pytest.fixture
def http(case, monkeypatch):
    c = case
    # Keep the browser cookie clock aligned with the frozen auth clock. Secure
    # cookie expiry remains enabled without depending on the machine's date.
    monkeypatch.setattr(cookiejar, 'time', SimpleNamespace(
        time=lambda: c.clock[0].timestamp(), localtime=stdlib_time.localtime))
    auth = AuthRepository(c.db, secret=b'synthetic-reader-secret-32bytes!!', clock=lambda: c.clock[0])
    settings = TeamGatewaySettings(public_origin='https://team.example', bot_id='42', bot_token='synthetic')
    app = create_team_app(settings, c.team, TeamGatewayClients(auth=auth, reads=c.reads))
    with TestClient(app, base_url='https://team.example', client=('203.0.113.8', 43123)) as client:
        yield c, auth, client


def login(http, member):
    _, auth, client = http
    code = auth.issue_code(member.max_user_id)
    response = client.post('/api/team/v1/session/code', json={'value': code.value},
                           headers={'Origin': 'https://team.example'})
    assert response.status_code == 200


def test_http_read_routes_auth_scope_and_safe_models(http):
    c, _, client = http
    paths = ['/api/team/v1/members?project_id=7', '/api/team/v1/status?project_id=7',
             '/api/team/v1/tasks/' + c.task.task_id + '/history']
    for path in paths:
        assert client.get(path).status_code == 401
    login(http, c.owner)
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        assert 'max_user_id' not in response.text and 'vikunja_user_id' not in response.text
    login(http, c.foreign)
    for path in paths:
        assert client.get(path).status_code == 403


def test_http_rejects_unbounded_or_invalid_cursors_without_raw_echo(http):
    c, _, client = http
    login(http, c.owner)
    for path in ['/api/team/v1/members?project_id=7&limit=101',
        '/api/team/v1/members?project_id=7&after=secret',
        '/api/team/v1/tasks/' + c.task.task_id + '/history?after=secret']:
        response = client.get(path)
        assert response.status_code == 422
        assert 'secret' not in response.text


def test_real_provider_poll_is_public_history_without_invented_author(sync_case):
    from secretary.infrastructure.team_read_repository import TeamReadRepository
    c = sync_case
    c.remote['55']['title'] = 'Actual remote observation'
    assert c.service.sync_once().state == 'ready'
    reads = TeamReadRepository(c.team, sync=c.repository)
    item = reads.history(c.member.id, '55').items[0]
    assert item.kind == 'external_change' and item.event == 'remote_change_observed'
    assert item.changed_fields == ('title',)
    assert item.actor_id is item.actor_display_name is item.values is item.remote_occurred_at is None
    assert item.recorded_at == c.now[0]
    assert item.before_fingerprint != item.after_fingerprint
    status = reads.status(c.member.id, '7')
    assert status.sync.last_successful_sync_at == c.now[0]


def test_deleted_provider_task_has_public_evidence_and_retains_previous_sync(sync_case):
    from secretary.infrastructure.team_read_repository import TeamReadRepository
    c = sync_case
    c.service.sync_once()
    previous = c.now[0]
    c.now[0] += timedelta(minutes=1)
    del c.remote['56']
    assert c.service.sync_once().state == 'degraded'
    reads = TeamReadRepository(c.team, sync=c.repository)
    item = reads.history(c.member.id, '56').items[0]
    assert item.event == 'remote_task_missing' and item.error_code == 'sync_task_missing'
    assert item.after_fingerprint is None and item.actor_id is None
    assert reads.status(c.member.id, '7').sync.last_successful_sync_at == previous
    assert c.team.get_projection(c.member.id, '56').bucket == 'inbox'


def test_repeated_unchanged_provider_polls_do_not_claim_external_changes(sync_case):
    from secretary.infrastructure.team_read_repository import TeamReadRepository
    c = sync_case
    reads = TeamReadRepository(c.team, sync=c.repository)
    for _ in range(3):
        assert c.service.sync_once().state == 'ready'
        assert reads.history(c.member.id, '55').items == ()
        assert reads.status(c.member.id, '7').sync.last_successful_sync_at == c.now[0]
        c.now[0] += timedelta(seconds=30)


@pytest.mark.parametrize('state', ['running', 'uncertain'])
def test_held_unchanged_task_keeps_diagnostic_evidence_without_fake_history(sync_case, state):
    from secretary.infrastructure.team_read_repository import TeamReadRepository
    c = sync_case
    cmd = TaskCommand(operation_id=uid(), project_id='7', task_id='55', expected_revision=0,
        expected_fingerprint=c.snapshots['55'].remote_fingerprint, action='set_state', values={'bucket': 'doing'})
    c.team.accept_command(c.member.id, cmd)
    claim = c.team.claim_command('history-test-worker')
    if state == 'uncertain':
        c.team.record_remote_result(claim, state='uncertain', error_code='operation_uncertain')
    assert c.service.sync_once().error_code == 'sync_task_busy'
    with c.db.connection() as conn:
        before = tuple(tuple(row) for row in conn.execute('SELECT * FROM team_sync_observations ORDER BY id'))
    assert before
    page = TeamReadRepository(c.team, sync=c.repository).history(c.member.id, '55')
    assert all(item.kind == 'command' for item in page.items)
    with c.db.connection() as conn:
        assert tuple(tuple(row) for row in conn.execute('SELECT * FROM team_sync_observations ORDER BY id')) == before


def test_failed_task_get_is_diagnostic_not_proof_of_remote_change(sync_case):
    from secretary.application.team_sync import TeamSyncService
    from secretary.infrastructure.team_read_repository import TeamReadRepository
    from secretary.infrastructure.vikunja import VikunjaClient
    c = sync_case
    original_transport = c.client._http._transport
    def remote(request):
        if request.url.path == '/api/v2/tasks/55':
            return httpx.Response(503, json={'message': 'Synthetic failed GET'})
        return original_transport.handle_request(request)
    with httpx.Client(base_url='http://127.0.0.1:34891/api/v2', headers={'Authorization': 'Bearer SYNTHETIC'},
        trust_env=False, follow_redirects=False, timeout=5, transport=httpx.MockTransport(remote)) as http:
        client = VikunjaClient(http, binding=c.client.binding, members=c.client.members)
        assert TeamSyncService(c.repository, client).sync_once().state == 'degraded'
    with c.db.connection() as conn:
        row = conn.execute("SELECT * FROM team_sync_observations WHERE task_id='55'").fetchone()
        assert row and row['error_code'] and row['after_fingerprint'] is None
    assert TeamReadRepository(c.team, sync=c.repository).history(c.member.id, '55').items == ()


def test_unconfirmed_external_due_difference_remains_visible_history(sync_case):
    from secretary.infrastructure.team_read_repository import TeamReadRepository
    c = sync_case
    c.remote['55']['due_date'] = '2026-10-05T10:00:00Z'
    assert c.service.sync_once().state == 'degraded'
    item = TeamReadRepository(c.team, sync=c.repository).history(c.member.id, '55').items[0]
    assert item.event == 'remote_observation_rejected' and item.error_code == 'remote_due_unconfirmed'
    assert item.before_fingerprint and item.after_fingerprint
    assert item.before_fingerprint != item.after_fingerprint
    assert item.actor_id is item.remote_occurred_at is None


def test_first_provider_observation_is_not_a_known_external_change(sync_case):
    from secretary.infrastructure.team_read_repository import TeamReadRepository
    c = sync_case
    c.remote['57'] = {**c.remote['55'], 'id': 57, 'title': 'First observed task'}
    assert c.service.sync_once().state == 'ready'
    assert c.team.get_projection(c.member.id, '57').title == 'First observed task'
    assert TeamReadRepository(c.team, sync=c.repository).history(c.member.id, '57').items == ()
