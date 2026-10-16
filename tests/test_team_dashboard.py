"""Owner dashboard: bounded local evidence only, isolated databases, no network."""
from datetime import timedelta
import importlib
import importlib.util
import json
import http.cookiejar
import time as stdlib_time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from secretary.domain.bot import BotEvent, DeterministicReply, SendReceipt
from secretary.domain.team import TaskCommand, TeamForbidden
from secretary.infrastructure.bot_repository import BotRepository
from secretary.infrastructure.team_auth_repository import AuthRepository
from secretary.infrastructure.voice_repository import VoiceRepository
from secretary.interface.team_gateway import TeamGatewayClients, TeamGatewaySettings, create_team_app
from test_team_reads import case as read_case, command
from test_notification_outbox import case as notification_case, queued as queued_notification, sending as sending_notification


def test_owner_dashboard_components_exist():
    for module in ('secretary.domain.team_dashboard', 'secretary.infrastructure.team_dashboard_repository'):
        assert importlib.util.find_spec(module) is not None, f'T10 owner dashboard component absent: {module}'


@pytest.fixture
def case(read_case, monkeypatch):
    c = read_case
    monkeypatch.setattr(http.cookiejar, 'time', SimpleNamespace(
        time=lambda: c.clock[0].timestamp(), localtime=stdlib_time.localtime))
    module = 'secretary.infrastructure.team_dashboard_repository'
    assert importlib.util.find_spec(module) is not None, 'T10 owner dashboard repository absent'
    c.dashboard_type = importlib.import_module(module).TeamDashboardRepository
    c.auth = AuthRepository(c.db, secret=b'synthetic-dashboard-secret-32bytes', clock=lambda: c.clock[0])
    c.bot = BotRepository(c.db, c.team, c.auth, clock=lambda: c.clock[0])
    c.voices = VoiceRepository(c.db, c.team, c.bot, clock=lambda: c.clock[0])
    c.dashboard = c.dashboard_type(c.team, reads=c.reads)
    return c


def _view(c, **options):
    return c.dashboard.dashboard(c.owner.id, '7', expected_actor_revision=c.team.get_member(c.owner.id).revision, **options)


def _state_command(c, state, *, project='7'):
    snapshot = c.task.model_copy(update={'task_id': str(1000 + len(c.team.list_projections(c.owner.id, '7', limit=100))),
                                         'project_id': project, 'title': 'PRIVATE command task title'})
    c.team.save_projection(snapshot, expected_revision=None)
    cmd = TaskCommand(operation_id=str(uuid4()), project_id=project, task_id=snapshot.task_id,
        expected_revision=0, expected_fingerprint=snapshot.remote_fingerprint,
        action='comment', values={'comment': 'PRIVATE literal comment not dashboard metadata'})
    c.team.accept_command(c.owner.id, cmd)
    if state == 'queued':
        return cmd
    claim = c.team.claim_command('synthetic-dashboard-worker')
    assert claim is not None and claim.command.operation_id == cmd.operation_id
    if state == 'reconciling':
        c.team.record_remote_result(claim, state='uncertain', error_code='operation_uncertain')
        claim = c.team.claim_reconciliation(cmd.operation_id, 'synthetic-dashboard-reconciler')
        assert claim is not None and claim.command.operation_id == cmd.operation_id
    elif state != 'running':
        fields = {'snapshot': snapshot.model_copy(update={'revision': 1, 'remote_fingerprint': 'b' * 64})} if state == 'applied' else {}
        c.team.record_remote_result(claim, state=state, error_code=None if state == 'applied' else 'operation_uncertain', **fields)
    c.clock[0] += timedelta(seconds=1)
    return cmd


def _reply(c, state='sent', *, actor=None, receipt=None, accepted_at=None):
    actor = actor or c.owner
    identifier = str(uuid4())
    event = BotEvent(event_id=identifier, dedup_key=identifier, bot_id='42', kind='message_created',
        timestamp_ms=int(c.clock[0].timestamp() * 1000), user_id=actor.max_user_id,
        chat_id=actor.max_user_id, message_id=identifier, text='PRIVATE raw webhook source')
    accepted = c.bot.intake(event)
    claim = c.bot.claim_event('synthetic-dashboard-inbox')
    assert claim is not None and claim.event.event_id == accepted.event_id
    c.bot.finish_event(claim, DeterministicReply('PRIVATE outbound message', receipt=receipt))
    outbound = c.bot.claim_reply('synthetic-dashboard-outbox')
    assert outbound is not None
    c.bot.finish_reply(outbound, SendReceipt(outbound.id, state,
        message_id='PRIVATE remote message identifier' if state == 'sent' else None,
        error_code=None if state == 'sent' else 'bot_send_uncertain', accepted_at=accepted_at))
    return c.bot.get_event(event.event_id)


def test_unwired_dashboard_does_not_invent_green_health_zero_counts_or_delivery_time(case):
    view = _view(case)
    assert view.project_id == '7' and view.commands.items == () and view.commands.next_cursor is None
    assert view.status.sync.state == view.status.cloud.state == 'not_configured'
    assert view.deliveries.state == view.webhook.state == view.notifications.state == 'not_configured'
    assert view.deliveries.pending_count is None and view.notifications.pending_count is None
    assert view.deliveries.last_max_api_accepted_at is view.webhook.last_authenticated_accept_at is None
    assert view.deliveries.human_read_confirmed is view.notifications.human_read_confirmed is None


@pytest.mark.parametrize('actor,project', [('member', '7'), ('other', '7'), ('owner', '8'), ('foreign', '7')])
def test_only_current_project_owner_can_read_dashboard(case, actor, project):
    with pytest.raises(TeamForbidden):
        case.dashboard.dashboard(getattr(case, actor).id, project)


def test_command_page_is_bounded_scoped_and_omits_terminal_private_payloads(case):
    c = case
    wanted = [_state_command(c, state) for state in ('running', 'reconciling', 'uncertain')]
    _state_command(c, 'applied')
    wanted.append(_state_command(c, 'queued'))
    first = _view(c, limit=2)
    second = _view(c, limit=2, after=first.commands.next_cursor)
    items = first.commands.items + second.commands.items
    assert {item.operation_id for item in items} == {cmd.operation_id for cmd in wanted}
    assert {item.state for item in items} == {'queued', 'running', 'reconciling', 'uncertain'}
    assert len(first.commands.items) == len(second.commands.items) == 2 and second.commands.next_cursor is None
    assert all(item.accepted_at.tzinfo is not None for item in items)
    assert all(item.last_state_at is None or item.last_state_at.tzinfo is not None for item in items)
    assert 'PRIVATE' not in first.model_dump_json() + second.model_dump_json()
    assert all('actor_id' not in item.model_dump() and 'values' not in item.model_dump() for item in items)


@pytest.mark.parametrize('options', [{'limit': 101}, {'limit': True}, {'after': 'PRIVATE malformed'}, {'after': 'x' * 513}])
def test_invalid_dashboard_pagination_refused_without_raw_echo(case, options):
    with pytest.raises(ValueError) as caught:
        _view(case, **options)
    assert 'PRIVATE' not in str(caught.value)


def test_dashboard_cursor_cannot_be_reused_in_another_project(case):
    c = case
    for _ in range(3):
        command(c)
    cursor = _view(c, limit=1).commands.next_cursor
    c.team.upsert_member(c.owner.model_copy(update={'revision': 1, 'project_ids': ('7', '8')}), expected_revision=0)
    with pytest.raises(ValueError):
        c.dashboard.dashboard(c.owner.id, '8', after=cursor, expected_actor_revision=1)


def test_api_accepted_bot_receipt_is_not_human_read_or_webhook_health(case):
    c = case
    _reply(c)
    c.dashboard = c.dashboard_type(c.team, reads=c.reads, bot=c.bot)
    view = _view(c)
    assert view.deliveries.state == 'observed'
    assert view.deliveries.max_api_accepted_count == 1 and view.deliveries.pending_count == 0
    assert view.deliveries.last_max_api_accepted_at is view.deliveries.human_read_confirmed is None
    assert view.webhook.state == 'not_configured' and view.webhook.last_authenticated_accept_at is None
    assert 'PRIVATE' not in view.model_dump_json() and 'max_user_id' not in view.model_dump_json()


def test_delivery_issues_and_foreign_project_counts_are_isolated(case):
    c = case
    _reply(c, state='uncertain')
    _reply(c, state='sent', actor=c.foreign)
    c.dashboard = c.dashboard_type(c.team, reads=c.reads, bot=c.bot)
    view = _view(c)
    assert view.deliveries.state == 'issues' and view.deliveries.uncertain_count == 1
    assert view.deliveries.max_api_accepted_count == 0


def test_multiscope_general_reply_is_omitted_but_linked_command_receipt_is_scoped(case):
    c = case
    c.team.upsert_member(c.owner.model_copy(update={'revision': 1, 'project_ids': ('7', '8')}), expected_revision=0)
    _reply(c)
    cmd = command(c, c.owner)
    _reply(c, receipt=c.team.get_receipt(c.owner.id, cmd.operation_id))
    c.dashboard = c.dashboard_type(c.team, reads=c.reads, bot=c.bot)
    assert _view(c).deliveries.max_api_accepted_count == 1


def test_owner_revocation_after_injected_notification_read_prevents_partial_data_release(case):
    c = case
    class Notifications:
        def summary(self, actor, project, *, expected_actor_revision=None):
            assert actor == c.owner.id and project == '7' and expected_actor_revision == 0
            c.team.upsert_member(c.owner.model_copy(update={'revision': 1, 'role': 'member'}), expected_revision=0)
            return {'state': 'observed', 'pending_count': 0, 'sending_count': 0, 'uncertain_count': 0,
                'failed_count': 0, 'cancelled_count': 0, 'last_max_api_accepted_at': None}
    c.dashboard = c.dashboard_type(c.team, reads=c.reads, notifications=Notifications())
    with pytest.raises(TeamForbidden):
        _view(c)


def test_unknown_notification_and_webhook_reader_failure_are_sanitized_unavailable(case):
    c = case
    class Broken:
        def summary(self, *args, **kwargs):
            raise RuntimeError('PRIVATE_TOKEN_REMOTE_BODY')
    def broken_webhook(*args, **kwargs):
        raise RuntimeError('PRIVATE_RAW_WEBHOOK')
    c.dashboard = c.dashboard_type(c.team, reads=c.reads, notifications=Broken(), webhook_reader=broken_webhook)
    view = _view(c)
    assert view.notifications.state == view.webhook.state == 'unavailable'
    assert view.notifications.pending_count is None and 'PRIVATE' not in view.model_dump_json()


def test_dashboard_http_owner_boundary_and_no_store(case):
    c = case
    app = create_team_app(TeamGatewaySettings(public_origin='https://team.example', bot_id='42', bot_token='synthetic'),
        c.team, TeamGatewayClients(auth=c.auth, reads=c.reads, dashboard=c.dashboard))
    path = '/api/team/v1/dashboard?project_id=7'
    with TestClient(app, base_url='https://team.example', client=('203.0.113.12', 12000)) as client:
        assert client.get(path).status_code == 401
        for member, status in ((c.member, 403), (c.owner, 200), (c.foreign, 403)):
            code = c.auth.issue_code(member.max_user_id)
            assert client.post('/api/team/v1/session/code', json={'value': code.value}, headers={'Origin': 'https://team.example'}).status_code == 200
            response = client.get(path)
            assert response.status_code == status
            assert response.headers['cache-control'] == 'no-store'
            assert 'max_user_id' not in response.text and 'PRIVATE' not in response.text
        code = c.auth.issue_code(c.owner.max_user_id)
        client.post('/api/team/v1/session/code', json={'value': code.value}, headers={'Origin': 'https://team.example'})
        response = client.get(path + '&limit=101')
        assert response.status_code == 422


def test_only_actual_max_receipt_timestamp_survives_repository_reopen(case):
    c = case
    from secretary.infrastructure.team_dashboard_repository import TeamDashboardRepository
    from secretary.infrastructure.team_repository import TeamRepository
    from secretary.infrastructure.team_database import TeamDatabase
    instant = '2026-10-04T08:01:02.345+00:00'
    _reply(c, accepted_at=instant)
    c.clock[0] += timedelta(days=5)
    _reply(c)  # The newer legacy send has no provider timestamp.
    reopened = TeamRepository(TeamDatabase(c.db.path), clock=lambda: c.clock[0])
    c.dashboard = TeamDashboardRepository(reopened, bot=c.bot)
    view = _view(c)
    assert view.deliveries.max_api_accepted_count == 2
    assert view.deliveries.last_max_api_accepted_at.isoformat() == '2026-10-04T08:01:02.345000+00:00'
    assert view.deliveries.human_read_confirmed is None and view.webhook.last_authenticated_accept_at is None


def test_real_voice_and_bot_sources_combine_only_current_project_receipts(case):
    c = case
    source = _reply(c, accepted_at='2026-10-04T08:01:02Z')
    c.voices.enqueue(source, text=source.text)
    c.voices.enqueue_notice(source, 'synthetic-preview', DeterministicReply('PRIVATE voice notice text'))
    delivery = c.voices.claim_reply('synthetic-voice-sender')
    assert delivery is not None
    c.voices.finish_reply(delivery, SendReceipt(delivery.id, 'sent', message_id='PRIVATE_voice_message',
        accepted_at='2026-10-04T08:03:00Z'))
    c.dashboard = c.dashboard_type(c.team, bot=c.bot, voices=c.voices)
    view = _view(c)
    assert view.deliveries.sources == ('bot', 'voice') and view.deliveries.max_api_accepted_count == 2
    assert view.deliveries.last_max_api_accepted_at.isoformat() == '2026-10-04T08:03:00+00:00'
    assert view.deliveries.human_read_confirmed is None and 'PRIVATE' not in view.model_dump_json()


def test_stale_disabled_owner_and_foreign_source_ports_do_not_reveal_aggregates(case, tmp_path):
    c = case
    class ForeignPort:
        db = type('DB', (), {'path': tmp_path / 'foreign-private.sqlite'})()
    c.dashboard = c.dashboard_type(c.team, bot=ForeignPort())
    view = _view(c)
    assert view.deliveries.state == 'unavailable' and view.deliveries.max_api_accepted_count is None
    c.team.upsert_member(c.owner.model_copy(update={'revision': 1, 'enabled': False}), expected_revision=0)
    with pytest.raises(TeamForbidden):
        c.dashboard.dashboard(c.owner.id, '7', expected_actor_revision=0)


def test_webhook_reader_is_explicit_evidence_with_unknown_current_health(case):
    c = case
    def reader(actor, project, *, expected_actor_revision):
        assert (actor, project, expected_actor_revision) == (c.owner.id, '7', 0)
        return {'state': 'unknown', 'last_authenticated_accept_at': '2026-10-04T08:00:00Z'}
    c.dashboard = c.dashboard_type(c.team, webhook_reader=reader)
    view = _view(c)
    assert view.webhook.state == 'unknown' and view.webhook.last_authenticated_accept_at is not None
    assert view.deliveries.state == 'not_configured'


@pytest.mark.parametrize('summary', [
    {'state': 'ready', 'pending_count': 0},
    {'state': 'observed', 'pending_count': True, 'sending_count': 0, 'uncertain_count': 0, 'failed_count': 0, 'cancelled_count': 0},
    {'state': 'observed', 'pending_count': 0},
    {'state': 'not_configured', 'pending_count': 0},
    {'state': 'unavailable', 'last_max_api_accepted_at': '2026-10-04T08:00:00Z'},
    {'state': 'observed', 'pending_count': 0, 'sending_count': 0, 'uncertain_count': 1, 'failed_count': 0, 'cancelled_count': 0},
    {'state': 'issues', 'pending_count': 0, 'sending_count': 0, 'uncertain_count': 0, 'failed_count': 0, 'cancelled_count': 0},
])
def test_malformed_notification_snapshot_cannot_invent_zero_counts_or_health(case, summary):
    c = case
    class Notifications:
        def summary(self, *args, **kwargs):
            return summary
    c.dashboard = c.dashboard_type(c.team, notifications=Notifications())
    result = _view(c).notifications
    assert result.state == 'unavailable' and result.pending_count is None and result.last_max_api_accepted_at is None


@pytest.mark.parametrize('change', [{'operation_id': '00000000-0000-4000-8000-000000000001'},
    {'accepted_at': '2026-02-30T08:00:00Z'}, {'state': 'uncertain'}, {'PRIVATE_TOKEN': 'PRIVATE'}])
def test_corrupt_persisted_sent_receipt_fails_closed_without_false_time_or_count(case, change):
    c = case
    _reply(c, accepted_at='2026-10-04T08:01:02Z')
    with c.db.transaction() as conn:
        row = conn.execute('SELECT id,receipt FROM bot_reply_state').fetchone()
        receipt = json.loads(row['receipt']) | change
        conn.execute('UPDATE bot_reply_state SET receipt=? WHERE id=?', (json.dumps(receipt), row['id']))
    c.dashboard = c.dashboard_type(c.team, bot=c.bot)
    value = _view(c).deliveries
    assert value.state == 'unavailable' and value.max_api_accepted_count is None and value.last_max_api_accepted_at is None
    assert 'PRIVATE' not in value.model_dump_json()


def test_command_error_code_is_sanitized_and_same_timestamp_pages_do_not_repeat(case):
    c = case
    first = _state_command(c, 'running')
    # Corrupt only a temporary fixture's mutable error payload; no source text
    # may be echoed through the owner dashboard.
    with c.db.transaction() as conn:
        row = conn.execute('SELECT payload FROM team_execution WHERE operation_id=?', (first.operation_id,)).fetchone()
        payload = json.loads(row['payload'])
        payload['error_code'] = 'PRIVATE_RAW_PROVIDER_MESSAGE'
        conn.execute('UPDATE team_execution SET payload=? WHERE operation_id=?', (json.dumps(payload), first.operation_id))
    c.clock[0] -= timedelta(seconds=1)
    created = [command(c, c.owner) for _ in range(8)]
    result, after = [], None
    while True:
        page = _view(c, limit=2, after=after)
        result.extend(page.commands.items)
        after = page.commands.next_cursor
        if after is None:
            break
    assert {item.operation_id for item in result} == {first.operation_id, *(cmd.operation_id for cmd in created)}
    assert len(result) == 9
    assert next(item for item in result if item.operation_id == first.operation_id).error_code == 'task_operation_failed'
    assert 'PRIVATE' not in ''.join(item.model_dump_json() for item in result)


def test_actual_notification_repository_summary_preserves_project_and_provider_time(notification_case):
    from secretary.infrastructure.team_dashboard_repository import TeamDashboardRepository
    c = notification_case
    _, intent = queued_notification(c)
    dashboard = TeamDashboardRepository(c.team, notifications=c.repo)
    initial = dashboard.dashboard(c.owner.id, '7', expected_actor_revision=0)
    assert initial.notifications.state == 'observed' and initial.notifications.pending_count == 1
    started, _ = sending_notification(c)
    assert dashboard.dashboard(c.owner.id, '7').notifications.sending_count == 1
    c.repo.finish(started, SendReceipt(intent.notification_id, 'sent', message_id='PRIVATE_notification_mid',
        accepted_at='2026-10-05T06:59:01.234Z'))
    result = dashboard.dashboard(c.owner.id, '7')
    assert result.notifications.pending_count == result.notifications.sending_count == 0
    assert result.notifications.last_max_api_accepted_at.isoformat() == '2026-10-05T06:59:01.234000+00:00'
    assert result.notifications.human_read_confirmed is None
    assert result.deliveries.state == result.webhook.state == 'not_configured'
    assert 'PRIVATE' not in result.model_dump_json()
    c.team.upsert_member(c.owner.model_copy(update={'revision': 1, 'project_ids': ('7', '8')}), expected_revision=0)
    foreign = dashboard.dashboard(c.owner.id, '8', expected_actor_revision=1)
    assert foreign.notifications.pending_count == 0 and foreign.notifications.last_max_api_accepted_at is None
    with pytest.raises(TeamForbidden):
        dashboard.dashboard(c.member.id, '7')


def test_injected_status_project_mismatch_is_storage_unavailable_not_bad_user_request(case):
    c = case
    class Reads:
        def status(self, actor, project, *, expected_actor_revision=None):
            return c.reads.status(actor, project, expected_actor_revision=expected_actor_revision).model_copy(update={'project_id': '8'})
    dashboard = c.dashboard_type(c.team, reads=Reads())
    app = create_team_app(TeamGatewaySettings(public_origin='https://team.example', bot_id='42', bot_token='synthetic'),
        c.team, TeamGatewayClients(auth=c.auth, dashboard=dashboard))
    with TestClient(app, base_url='https://team.example', client=('203.0.113.12', 12000)) as client:
        code = c.auth.issue_code(c.owner.max_user_id)
        assert client.post('/api/team/v1/session/code', json={'value': code.value}, headers={'Origin': 'https://team.example'}).status_code == 200
        result = client.get('/api/team/v1/dashboard?project_id=7')
    assert result.status_code == 503 and result.json() == {'error_code': 'team_storage_unavailable'}
