"""Owner-local invitation routes: synthetic repositories, no MAX/network/data."""
from __future__ import annotations

import importlib
import importlib.util
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.domain.team import TeamConflict, TeamForbidden, TeamMember
from secretary.domain.team_auth import AuthError
from secretary.infrastructure.team_auth_repository import AuthRepository
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository
from secretary.settings import Settings
from test_backend import FakeCapture, auth


ROOT = '/api/v1/team'


def uid():
    return str(uuid4())


def routes():
    name = 'secretary.interface.local_team_routes'
    assert importlib.util.find_spec(name) is not None, 'local team router is not implemented'
    return importlib.import_module(name)


class SyntheticAuthRepository:
    def __init__(self):
        self.owner_id = uid()
        self.invitation_id = uid()
        self.now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        self.enabled = True
        self.calls = []
        self.member = TeamMember(id=uid(), display_name='Synthetic member', role='member',
            max_user_id='9007199254740993', vikunja_user_id='9007199254740995', project_ids=('7',))

    def call(self, method, owner, *args):
        assert owner == self.owner_id
        if not self.enabled:
            raise TeamForbidden('private owner-state details must never be returned')
        self.calls.append((method, owner, *args))

    def create_invitation(self, owner, projects):
        self.call('create', owner, projects)
        return {'id': self.invitation_id, 'value': 'synthetic-invitation-value-not-a-real-secret',
                'expires_at': self.now + timedelta(minutes=15)}

    def read_invitation(self, owner, invitation_id):
        self.call('read', owner, invitation_id)
        return {'id': self.invitation_id, 'project_ids': ('7',), 'candidate_user_id': self.member.max_user_id,
                'expires_at': self.now + timedelta(minutes=15), 'confirmed_member_id': None, 'revoked': False}

    def confirm_invitation(self, owner, invitation_id, member):
        self.call('confirm', owner, invitation_id, member)
        return member

    def revoke_invitation(self, owner, invitation_id):
        self.call('revoke_invitation', owner, invitation_id)

    def revoke_member(self, owner, target, expected_revision):
        self.call('revoke_member', owner, target, expected_revision)
        return self.member.model_copy(update={'enabled': False, 'revision': expected_revision + 1})


@pytest.fixture
def repository():
    return SyntheticAuthRepository()


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path / 'data', project_dir=tmp_path,
                    cloud_enabled=False, polza_api_key='', local_cost_limits_enabled=False)


def make_app(settings, factory=None, *, omitted=False, enforce_single_instance=True):
    assert 'team_auth_factory' in inspect.signature(create_app).parameters, 'owner factory integration missing'
    kwargs = {} if omitted else {'team_auth_factory': factory}
    return create_app(settings, capture=FakeCapture(), enrollment_capture=FakeCapture(), run_worker=False,
        provider_factory=lambda *_: pytest.fail('No provider is allowed'),
        enforce_single_instance=enforce_single_instance, **kwargs)


@pytest.fixture
def case(settings, repository):
    factories = []
    def factory(db):
        factories.append(db)
        return repository, repository.owner_id
    app = make_app(settings, factory)
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 43123)) as client:
        yield SimpleNamespace(client=client, repository=repository, factories=factories, app=app)


def test_owner_factory_is_called_once_and_routes_use_only_its_actor(case):
    c = case
    assert c.factories == [c.app.state.db]
    headers = auth(c.client)
    created = c.client.post(ROOT + '/invitations', json={'project_ids': ['7']}, headers=headers)
    assert created.status_code == 201
    invitation = created.json()
    assert set(invitation) == {'id', 'value', 'expires_at'}
    assert invitation['id'] == c.repository.invitation_id
    read = c.client.get(ROOT + '/invitations/' + invitation['id'])
    assert read.status_code == 200 and 'value' not in read.json()
    assert read.json()['candidate_user_id'] == '9007199254740993'
    member = c.repository.member.model_dump(mode='json')
    confirmed = c.client.post(ROOT + '/invitations/' + invitation['id'] + '/confirm', json={'member': member}, headers=headers)
    assert confirmed.status_code == 200 and confirmed.json() == member
    assert [call[0] for call in c.repository.calls] == ['create', 'read', 'confirm']
    assert all(call[1] == c.repository.owner_id for call in c.repository.calls)
    assert c.repository.calls[0][2] == ('7',)
    assert isinstance(c.repository.calls[-1][3], TeamMember)
    assert created.headers['Cache-Control'] == 'no-store'


def test_explicit_revocations_forward_exact_expected_revision(case):
    c = case
    headers = auth(c.client)
    invitation = c.repository.invitation_id
    assert c.client.delete(ROOT + '/invitations/' + invitation, headers=headers).status_code == 204
    member = c.client.request('DELETE', ROOT + '/members/' + c.repository.member.id,
        json={'expected_revision': 0}, headers=headers)
    assert member.status_code == 200 and member.json()['enabled'] is False and member.json()['revision'] == 1
    assert c.repository.calls[-1] == ('revoke_member', c.repository.owner_id, c.repository.member.id, 0)


@pytest.mark.parametrize('method,path,payload', [
    ('POST', '/invitations', {'project_ids': ['7']}),
    ('POST', '/invitations/{id}/confirm', {'member': {}}),
    ('DELETE', '/invitations/{id}', None),
    ('DELETE', '/members/{id}', {'expected_revision': 0}),
])
def test_mutations_require_existing_secretary_csrf_before_repository_access(case, method, path, payload):
    c = case
    response = c.client.request(method, ROOT + path.format(id=c.repository.invitation_id), json=payload)
    assert response.status_code == 403 and c.repository.calls == []


@pytest.mark.parametrize('path,method,payload', [
    ('/invitations', 'POST', {'project_ids': ['7']}),
    ('/invitations/{id}', 'GET', None),
    ('/invitations/{id}/confirm', 'POST', {'member': {}}),
    ('/invitations/{id}', 'DELETE', None),
    ('/members/{id}', 'DELETE', {'expected_revision': 0}),
])
def test_default_feature_is_disabled_without_constructing_an_auth_repository(settings, path, method, payload):
    app = make_app(settings, omitted=True)
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 43123)) as client:
        response = client.request(method, ROOT + path.format(id=uid()), json=payload, headers=auth(client))
        assert response.status_code == 503
        assert response.json() == {'detail': 'local_team_not_configured'}


@pytest.mark.parametrize('peer,headers', [
    ('192.0.2.8', {}), ('192.0.2.8', {'X-Forwarded-For': '127.0.0.1', 'X-Real-IP': '127.0.0.1'}),
    ('192.0.2.8', {'Forwarded': 'for=127.0.0.1;host=127.0.0.1'}),
    ('testclient', {}), ('localhost', {}),
])
def test_actual_peer_cannot_be_replaced_by_forwarded_headers(settings, repository, peer, headers):
    app = make_app(settings, lambda db: (repository, repository.owner_id))
    with TestClient(app, base_url='http://127.0.0.1:8765', client=(peer, 43123)) as client:
        response = client.get(ROOT + '/invitations/' + repository.invitation_id, headers=headers)
        assert response.status_code == 403 and repository.calls == []
        assert response.json() == {'detail': 'local_team_loopback_required'}


@pytest.mark.parametrize('host', ['localhost', 'example.test', '127.0.0.1.evil.test', '127.0.0.1/',
                                 '127.0.0.1@evil.test', '127.0.0.1:invalid', '[::1%scope]:8765',
                                 '[::1]evil.example', '[::1]junk:8765'])
def test_router_itself_requires_literal_loopback_host_without_dns(repository, host):
    app = FastAPI()
    app.include_router(routes().create_local_team_router(repository, actor_id=repository.owner_id))
    with TestClient(app, client=('127.0.0.1', 43123)) as client:
        response = client.get(ROOT + '/invitations/' + repository.invitation_id, headers={'Host': host})
        assert response.status_code == 403 and repository.calls == []


def test_duplicate_host_is_not_treated_as_one_trusted_host(repository):
    app = FastAPI()
    app.include_router(routes().create_local_team_router(repository, actor_id=repository.owner_id))
    with TestClient(app, client=('127.0.0.1', 43123)) as client:
        response = client.get(ROOT + '/invitations/' + repository.invitation_id,
                              headers=[('Host', '127.0.0.1'), ('Host', 'example.test')])
        assert response.status_code == 403 and repository.calls == []


def test_ipv6_literal_loopback_is_supported(repository):
    app = FastAPI()
    app.include_router(routes().create_local_team_router(repository, actor_id=repository.owner_id))
    with TestClient(app, base_url='http://[::1]:8765', client=('::1', 43123)) as client:
        assert client.get(ROOT + '/invitations/' + repository.invitation_id).status_code == 200


@pytest.mark.parametrize('payload', [{'project_ids': []}, {'project_ids': ['7', '7']}, {'project_ids': [7]},
    {'project_ids': ['07']}, {'project_ids': ['7'], 'actor_id': 'client-selected-actor-secret'},
    {'project_ids': ['7'], 'secret': 'NEVER_ECHO_THIS_VALUE'}])
def test_invalid_or_client_actor_inputs_are_sanitized_and_never_forwarded(case, payload):
    response = case.client.post(ROOT + '/invitations', json=payload, headers=auth(case.client))
    assert response.status_code == 422 and case.repository.calls == []
    assert response.json() == {'detail': 'local_team_request_invalid'}


@pytest.mark.parametrize('revision', [-1, True, 0.5, '0'])
def test_member_revision_is_a_strict_nonnegative_integer(case, revision):
    response = case.client.request('DELETE', ROOT + '/members/' + case.repository.member.id,
        json={'expected_revision': revision}, headers=auth(case.client))
    assert response.status_code == 422 and case.repository.calls == []


@pytest.mark.parametrize('exception,status,code', [
    (TeamForbidden('secret authority details'), 403, 'local_team_forbidden'),
    (TeamConflict('secret invitation conflict details'), 409, 'local_team_conflict'),
    (RuntimeError('secret underlying storage failure'), 503, 'local_team_unavailable'),
])
def test_repository_failures_never_expose_exception_text(case, exception, status, code):
    def fail(*args):
        raise exception
    case.repository.read_invitation = fail
    response = case.client.get(ROOT + '/invitations/' + case.repository.invitation_id)
    assert response.status_code == status and response.json() == {'detail': code}


def test_owner_status_is_rechecked_on_each_repository_call(case):
    path = ROOT + '/invitations/' + case.repository.invitation_id
    assert case.client.get(path).status_code == 200
    case.repository.enabled = False
    response = case.client.get(path)
    assert response.status_code == 403 and response.json() == {'detail': 'local_team_forbidden'}


def test_no_claim_or_login_code_endpoint_is_exposed(case):
    paths = case.client.get('/openapi.json').json()['paths']
    team_paths = {path for path in paths if path.startswith(ROOT)}
    assert team_paths == {ROOT + '/invitations', ROOT + '/invitations/{invitation_id}',
        ROOT + '/invitations/{invitation_id}/confirm', ROOT + '/members/{member_id}'}
    for path in ('/claim', '/issue-code', '/invitations/claim', '/login-code'):
        assert case.client.post(ROOT + path, json={}, headers=auth(case.client)).status_code in {404, 405}


@pytest.mark.parametrize('code,status', [
    ('auth_forbidden', 403), ('auth_invalid_credentials', 403), ('auth_csrf_invalid', 403),
    ('auth_conflict', 409), ('auth_replay', 409), ('auth_rate_limited', 429),
    ('auth_unavailable', 503), ('auth_dto_invalid', 422),
])
def test_known_auth_errors_have_explicit_sanitized_http_mapping(case, code, status):
    def fail(*args):
        raise AuthError(code)
    case.repository.read_invitation = fail
    response = case.client.get(ROOT + '/invitations/' + case.repository.invitation_id)
    assert response.status_code == status and response.json() == {'detail': code}


def test_unexpected_auth_subsystem_error_is_not_exposed_as_local_capability(case):
    def fail(*args):
        raise AuthError('max_signature_invalid')
    case.repository.read_invitation = fail
    response = case.client.get(ROOT + '/invitations/' + case.repository.invitation_id)
    assert response.status_code == 503 and response.json() == {'detail': 'local_team_unavailable'}


def test_peer_check_happens_before_parsing_malformed_body(repository):
    app = FastAPI()
    app.include_router(routes().create_local_team_router(repository, actor_id=repository.owner_id))
    with TestClient(app, base_url='http://127.0.0.1', client=('192.0.2.8', 43123)) as client:
        response = client.post(ROOT + '/invitations', content='{invalid private input',
            headers={'Content-Type': 'application/json', 'X-Forwarded-For': '127.0.0.1'})
        assert response.status_code == 403 and repository.calls == []


def test_disabled_feature_is_checked_before_parsing_malformed_body(settings):
    app = make_app(settings, omitted=True)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 43123)) as client:
        response = client.post(ROOT + '/invitations', content='{invalid private input',
            headers={**auth(client), 'Content-Type': 'application/json'})
        assert response.status_code == 503 and response.json() == {'detail': 'local_team_not_configured'}


def test_invalid_path_uuid_is_sanitized(case):
    response = case.client.get(ROOT + '/invitations/private-invalid-identifier')
    assert response.status_code == 422 and case.repository.calls == []
    assert response.json() == {'detail': 'local_team_request_invalid'}


def test_confirmation_can_explicitly_request_an_owner_member(case):
    member = case.repository.member.model_copy(update={'role': 'owner'})
    response = case.client.post(ROOT + '/invitations/' + case.repository.invitation_id + '/confirm',
        json={'member': member.model_dump(mode='json')}, headers=auth(case.client))
    assert response.status_code == 200 and response.json()['role'] == 'owner'
    assert case.repository.calls[-1][3].role == 'owner'


@pytest.fixture
def real_case(settings, tmp_path):
    """Real storage and actual Secretary routes, entirely inside guarded scratch."""
    clock = [datetime(2026, 10, 3, 12, tzinfo=timezone.utc)]
    db = TeamDatabase(tmp_path / 'local-auth-team.sqlite3')
    team = TeamRepository(db, clock=lambda: clock[0])
    owner = TeamMember(id=uid(), display_name='Synthetic local owner', role='owner',
        max_user_id='101', vikunja_user_id='201', project_ids=('7',))
    member = TeamMember(id=uid(), display_name='Synthetic existing member',
        max_user_id='102', vikunja_user_id='202', project_ids=('7',))
    for person in (owner, member):
        team.upsert_member(person, expected_revision=None)
    repository = AuthRepository(db, secret=b'SYNTHETIC_LOCAL_AUTH_TEST_KEY_32_BYTES', clock=lambda: clock[0])
    app = make_app(settings, lambda source_db: (repository, owner.id))
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 43123)) as client:
        yield SimpleNamespace(client=client, db=db, team=team, repository=repository, owner=owner,
            member=member, clock=clock, settings=settings, headers=auth(client))


def auth_state(c):
    """Compare mutation state without serializing ephemeral invitation/session values."""
    names = ('team_members', 'team_auth_invitations', 'team_auth_sessions', 'team_auth_codes', 'team_auth_journal')
    with c.db.connection() as conn:
        return {name: tuple(tuple(row) for row in conn.execute(f'SELECT * FROM {name} ORDER BY rowid')) for name in names}


def real_invite(c, *, claim=True):
    response = c.client.post(ROOT + '/invitations', json={'project_ids': ['7']}, headers=c.headers)
    assert response.status_code == 201
    invitation = response.json()
    candidate = TeamMember(id=uid(), display_name='Synthetic invited owner', role='owner',
        max_user_id='9007199254740993', vikunja_user_id='9007199254740995', project_ids=('7',))
    if claim:
        # Simulates only the future trusted MAX-event port, never an HTTP claim endpoint.
        view = c.repository.claim_invitation(invitation['value'], candidate.max_user_id)
        assert view.candidate_user_id == candidate.max_user_id
    return invitation, candidate


def real_confirm(c, invitation, candidate):
    return c.client.post(ROOT + '/invitations/' + invitation['id'] + '/confirm',
        json={'member': candidate.model_dump(mode='json')}, headers=c.headers)


def test_real_http_confirm_preserves_exact_candidate_max_id_and_explicit_owner_role(real_case):
    c = real_case
    invitation, candidate = real_invite(c)
    assert c.team.get_member(candidate.id) is None
    read = c.client.get(ROOT + '/invitations/' + invitation['id'])
    assert read.status_code == 200 and read.json()['candidate_user_id'] == '9007199254740993'
    assert read.json()['confirmed_member_id'] is None and 'value' not in read.json()
    confirmed = real_confirm(c, invitation, candidate)
    assert confirmed.status_code == 200 and confirmed.json() == candidate.model_dump(mode='json')
    assert c.team.get_member(candidate.id) == candidate and candidate.role == 'owner'
    assert c.client.get(ROOT + '/invitations/' + invitation['id']).json()['confirmed_member_id'] == candidate.id
    before = auth_state(c)
    assert real_confirm(c, invitation, candidate).status_code == 200
    assert auth_state(c) == before


@pytest.mark.parametrize('change', ['max_id', 'project_scope', 'reserved_vikunja_id'])
def test_real_http_auth_conflict_is_409_and_confirmation_transaction_rolls_back(real_case, change):
    c = real_case
    invitation, candidate = real_invite(c)
    edits = {'max_id': {'max_user_id': '9007199254740994'}, 'project_scope': {'project_ids': ('7', '8')},
             'reserved_vikunja_id': {'vikunja_user_id': c.member.vikunja_user_id}}[change]
    before = auth_state(c)
    denied = real_confirm(c, invitation, candidate.model_copy(update=edits))
    assert denied.status_code == 409 and denied.json() == {'detail': 'auth_conflict'}
    assert auth_state(c) == before and c.team.get_member(candidate.id) is None


def test_real_http_expired_invitation_cannot_create_a_member(real_case):
    c = real_case
    invitation, candidate = real_invite(c)
    c.clock[0] = datetime.fromisoformat(invitation['expires_at'])
    before = auth_state(c)
    denied = real_confirm(c, invitation, candidate)
    assert denied.status_code == 403 and denied.json() == {'detail': 'auth_forbidden'}
    assert auth_state(c) == before and c.team.get_member(candidate.id) is None


def test_real_http_revoked_invitation_stays_revoked_without_duplicate_writes(real_case):
    c = real_case
    invitation, candidate = real_invite(c)
    path = ROOT + '/invitations/' + invitation['id']
    assert c.client.delete(path, headers=c.headers).status_code == 204
    read = c.client.get(path)
    assert read.status_code == 200 and read.json()['revoked'] is True
    before = auth_state(c)
    assert c.client.delete(path, headers=c.headers).status_code == 204
    denied = real_confirm(c, invitation, candidate)
    assert denied.status_code == 403 and denied.json() == {'detail': 'auth_forbidden'}
    assert auth_state(c) == before and c.team.get_member(candidate.id) is None


@pytest.mark.parametrize('change', [{'role': 'member'}, {'project_ids': ('8',)}, {'enabled': False}])
def test_real_http_admin_operations_recheck_current_owner_role_and_scope_without_writes(real_case, change):
    c = real_case
    invitation, candidate = real_invite(c)
    c.team.upsert_member(c.owner.model_copy(update={**change, 'revision': 1}), expected_revision=0)
    before = auth_state(c)
    path = ROOT + '/invitations/' + invitation['id']
    responses = (
        c.client.post(ROOT + '/invitations', json={'project_ids': ['7']}, headers=c.headers),
        c.client.get(path), real_confirm(c, invitation, candidate), c.client.delete(path, headers=c.headers),
        c.client.request('DELETE', ROOT + '/members/' + c.member.id, json={'expected_revision': 0}, headers=c.headers),
    )
    assert all(response.status_code == 403 and response.json() == {'detail': 'auth_forbidden'} for response in responses)
    assert auth_state(c) == before


def test_real_http_cannot_create_invitation_for_ungranted_project(real_case):
    c = real_case
    before = auth_state(c)
    response = c.client.post(ROOT + '/invitations', json={'project_ids': ['8']}, headers=c.headers)
    assert response.status_code == 403 and response.json() == {'detail': 'auth_forbidden'}
    assert auth_state(c) == before


def test_real_http_member_revoke_checks_cas_then_invalidates_session_and_code(real_case):
    c = real_case
    session = c.repository.consume_code(c.repository.issue_code(c.member.max_user_id).value)
    pending_code = c.repository.issue_code(c.member.max_user_id)
    path = ROOT + '/members/' + c.member.id
    before = auth_state(c)
    conflict = c.client.request('DELETE', path, json={'expected_revision': 1}, headers=c.headers)
    assert conflict.status_code == 409 and conflict.json() == {'detail': 'auth_conflict'}
    assert auth_state(c) == before
    response = c.client.request('DELETE', path, json={'expected_revision': 0}, headers=c.headers)
    assert response.status_code == 200 and response.json()['enabled'] is False and response.json()['revision'] == 1
    assert c.team.get_member(c.member.id).enabled is False
    with pytest.raises(AuthError, match='auth_invalid_credentials'):
        c.repository.authenticate(session.token)
    with pytest.raises(AuthError, match='auth_invalid_credentials'):
        c.repository.consume_code(pending_code.value)


def test_real_http_another_configured_owner_cannot_read_or_confirm_foreign_invitation(real_case):
    c = real_case
    invitation, candidate = real_invite(c)
    other = TeamMember(id=uid(), display_name='Synthetic other owner', role='owner', max_user_id='103',
        vikunja_user_id='203', project_ids=('7',))
    c.team.upsert_member(other, expected_revision=None)
    other_app = make_app(c.settings, lambda source_db: (c.repository, other.id),
                         enforce_single_instance=False)
    before = auth_state(c)
    with TestClient(other_app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 43123)) as client:
        path = ROOT + '/invitations/' + invitation['id']
        read = client.get(path)
        confirm = client.post(path + '/confirm', json={'member': candidate.model_dump(mode='json')}, headers=auth(client))
        assert read.status_code == confirm.status_code == 403
        assert read.json() == confirm.json() == {'detail': 'auth_forbidden'}
    assert auth_state(c) == before
