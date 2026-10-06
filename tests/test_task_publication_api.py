"""HTTP contract/localhost security; fixtures cannot access live providers."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from secretary.api import create_app
from secretary.domain.task_delivery import (DeliveryReceipt, PublicationContext, PublicationItemReceipt,
    PublicationPreviewResult, PublishPreview)
from secretary.domain.team import AcceptanceReceipt, ExecutionState, TeamConflict, TeamForbidden
from secretary.settings import Settings
from test_backend import FakeCapture, auth
from test_task_publications import case as publication_case, source_case


def ident():
    return str(uuid4())


@pytest.fixture
def api_case(tmp_path):
    stamp = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    scope = dict(meeting_id='synthetic', transcript_version=1, summary_version=1, destination_project_id='7')
    watermarks = dict(assignment_revision=1, roster_revision=1, attribution_revision=1, context_hash='a' * 64)
    item = dict(action_id='action', title='Подготовить отчёт', assignee_id=ident(),
        source_segment_ids=['s'], evidence_quote='Я подготовлю отчёт', due_confirmed=True)
    preview = PublishPreview(preview_id=ident(), scope=scope, watermarks=watermarks, items=[item],
        actor_id=ident(), expires_at=stamp + timedelta(minutes=15))
    operation = ident()
    receipt = DeliveryReceipt(operation_id=operation, scope=scope,
        acceptance_receipt=AcceptanceReceipt(operation_id=operation, payload_hash='b' * 64,
            decision='accepted', decided_at=stamp), items=[PublicationItemReceipt(publication_id=ident(),
            delivery_operation_id=ident(), action_id='action', intent='publish', execution_state=ExecutionState(state='queued'))])
    calls = []

    class Service:
        def context(self, meeting):
            calls.append(('context', meeting))
            return PublicationContext(scope=scope, watermarks=watermarks, project_name='Команда', members=[], candidates=[])

        def preview(self, meeting, request):
            calls.append(('preview', meeting, request))
            return PublicationPreviewResult(candidates=[], preview=preview)

        def confirm(self, meeting, command):
            calls.append(('confirm', meeting, command))
            return receipt

        def read(self, meeting, operation_id):
            calls.append(('read', meeting, operation_id))
            return receipt

        def list(self, meeting):
            calls.append(('list', meeting))
            return [receipt]

    service = Service()
    settings = Settings(_env_file=None, data_dir=tmp_path / 'data', project_dir=tmp_path,
        cloud_enabled=False, polza_api_key='', local_cost_limits_enabled=False)
    app = create_app(settings, capture=FakeCapture(), enrollment_capture=FakeCapture(), run_worker=False,
        provider_factory=lambda *_: pytest.fail('No provider'), task_publications_factory=lambda db: service)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        yield SimpleNamespace(client=client, service=service, calls=calls, receipt=receipt, preview=preview,
            operation=operation, scope=scope, settings=settings)


def test_routes_keep_preview_and_confirmation_separate(api_case):
    c = api_case
    root = '/api/v1/meetings/synthetic/task-publications'
    assert c.client.get(root + '/context').status_code == 200
    request = {'scope': c.scope, 'selections': [{'action_id': 'action', 'due_resolution': 'none'}]}
    assert c.client.post(root + '/preview', json=request).status_code == 403
    assert [x[0] for x in c.calls] == ['context']
    headers = auth(c.client)
    prepared = c.client.post(root + '/preview', json=request, headers=headers)
    assert prepared.status_code == 200
    assert [x[0] for x in c.calls] == ['context', 'preview']
    command = {**prepared.json()['preview'], 'operation_id': c.operation}
    accepted = c.client.post(root, json=command, headers=headers)
    assert accepted.status_code == 202
    assert accepted.json()['items'][0]['execution_state']['state'] == 'queued'
    assert c.client.get(root + '/' + c.operation).json() == accepted.json()
    assert c.client.get(root).json() == [accepted.json()]
    assert [x[0] for x in c.calls] == ['context', 'preview', 'confirm', 'read', 'list']


@pytest.mark.parametrize('exception,status', [(TeamConflict('source_changed'), 409), (TeamForbidden('owner_required'), 403)])
def test_routes_preserve_conflict_and_current_acl_failure(api_case, exception, status):
    def denied(*args):
        raise exception
    api_case.service.context = denied
    response = api_case.client.get('/api/v1/meetings/synthetic/task-publications/context')
    assert response.status_code == status
    assert response.json() == {'detail': str(exception)}


def test_unconfigured_publication_never_returns_demo_success(api_case):
    api_case.client.app.state.task_publications = None
    response = api_case.client.get('/api/v1/meetings/synthetic/task-publications/context')
    assert response.status_code == 503
    assert response.json()['detail'] == 'team_publication_not_configured'


@pytest.mark.parametrize('headers', [{'Host': 'evil.example'}, {'Origin': 'https://evil.example'}, {'Origin': 'http://127.0.0.1:9876'}])
def test_publication_routes_inherit_localhost_boundary(api_case, headers):
    response = api_case.client.get('/api/v1/meetings/synthetic/task-publications/context', headers=headers)
    assert response.status_code == 403
    assert api_case.calls == []


def test_applied_publication_cannot_use_an_unverified_gateway_response(api_case):
    value = api_case.receipt.items[0].model_dump()
    value['execution_state'] = {'state': 'applied', 'task_id': '99', 'verified_at': datetime.now(timezone.utc)}
    with pytest.raises(ValidationError, match='publication_requires_verified_gateway_receipt'):
        PublicationItemReceipt.model_validate(value)


def test_invalid_operation_or_selection_never_dispatches(api_case):
    root = '/api/v1/meetings/synthetic/task-publications'
    assert api_case.client.get(root + '/not-a-uuid').status_code == 422
    response = api_case.client.post(root + '/preview', json={'scope': api_case.scope, 'selections': []},
        headers=auth(api_case.client))
    assert response.status_code == 422
    assert api_case.calls == []


@pytest.fixture
def real_api_case(publication_case, tmp_path):
    """Use the real service and both repositories at the actual HTTP boundary."""
    from secretary.application.task_publications import TaskPublicationService
    from secretary.infrastructure.task_publication_repository import TaskPublicationRepository
    c = publication_case
    settings = Settings(_env_file=None, data_dir=tmp_path / 'http-data', project_dir=tmp_path,
        cloud_enabled=False, polza_api_key='', local_cost_limits_enabled=False)
    settings.data_dir.mkdir()
    with c.db.connection() as source, sqlite3.connect(settings.data_dir / 'secretary.sqlite3') as target:
        source.backup(target)
    def factory(db):
        return TaskPublicationService(db, TaskPublicationRepository(db, clock=lambda: c.time[0]), c.team,
            actor_id=c.owner.id, project_id='7', clock=lambda: c.time[0])
    app = create_app(settings, capture=FakeCapture(), enrollment_capture=FakeCapture(), run_worker=False,
        provider_factory=lambda *_: pytest.fail('No provider'), task_publications_factory=factory)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        yield SimpleNamespace(source=c, client=client, db=app.state.db, service=app.state.task_publications,
            root=f'/api/v1/meetings/{c.meeting}/task-publications')


def test_real_http_preview_confirm_history_and_late_source_change(real_api_case):
    c = real_api_case
    headers = auth(c.client)
    context = c.client.get(c.root + '/context')
    assert context.status_code == 200
    assert context.json()['candidates'][0]['eligible'] is True
    request = {'scope': context.json()['scope'], 'selections': [
        {'action_id': c.source.action_id, 'due_resolution': 'none'}]}
    preview = c.client.post(c.root + '/preview', json=request, headers=headers)
    assert preview.status_code == 200
    item = preview.json()['preview']['items'][0]
    assert item['assignee_id'] == c.source.member.id
    assert item['evidence_quote'] == c.source.source.text
    assert c.service.repository.claim_next('no-auto-publish') is None
    command = {**preview.json()['preview'], 'operation_id': ident()}
    accepted = c.client.post(c.root, json=command, headers=headers)
    assert accepted.status_code == 202
    replay = c.client.post(c.root, json=command, headers=headers)
    assert replay.json() == accepted.json()
    assert c.client.get(c.root + '/' + command['operation_id']).json() == accepted.json()
    assert len(c.client.get(c.root).json()) == 1
    c.db.execute("UPDATE segments SET text='Изменённый синтетический источник' WHERE id=?", (c.source.source.id,))
    stale = c.client.get(c.root + '/' + command['operation_id'])
    assert stale.status_code == 200
    assert stale.json()['items'][0]['source_stale'] is True
    assert stale.json()['items'][0]['correction_required'] is False
    assert stale.json()['items'][0]['execution_state']['state'] == 'queued'


def test_real_http_tampered_preview_cannot_queue_a_task(real_api_case):
    c = real_api_case
    headers = auth(c.client)
    prepared = c.client.post(c.root + '/preview', json={'scope': c.source.scope.model_dump(),
        'selections': [{'action_id': c.source.action_id, 'due_resolution': 'none'}]}, headers=headers).json()
    command = {**prepared['preview'], 'operation_id': ident()}
    command['items'][0]['title'] = 'Неподтверждённый новый текст'
    response = c.client.post(c.root, json=command, headers=headers)
    assert response.status_code == 409
    assert c.service.repository.claim_next('sender') is None
    assert c.client.get(c.root).json() == []


def test_real_http_unknown_actor_role_revocation_blocks_current_context(real_api_case):
    c = real_api_case
    owner = c.source.owner
    c.source.team.upsert_member(owner.model_copy(update={'role': 'member', 'revision': 1}), expected_revision=0)
    response = c.client.get(c.root + '/context')
    assert response.status_code == 403
    assert response.json()['detail'] == 'owner_required'


def test_real_http_missing_operation_is_404_but_revoked_owner_is_403(real_api_case):
    c = real_api_case
    response = c.client.get(c.root + '/' + ident())
    assert response.status_code == 404
    assert response.json()['detail'] == 'publication_command_unavailable'
    owner = c.source.owner
    c.source.team.upsert_member(owner.model_copy(update={'role': 'member', 'revision': 1}), expected_revision=0)
    assert c.client.get(c.root + '/' + ident()).status_code == 403
