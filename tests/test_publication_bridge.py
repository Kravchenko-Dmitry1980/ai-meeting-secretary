"""T5 internal bridge: isolated Team/Secretary roles, ASGI and MockTransport only."""
from __future__ import annotations

import asyncio
import hashlib
from importlib import import_module
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from secretary.domain.task_delivery import GatewayPublication, PublicationScope, PublicationTask, delivery_uuid, publication_uuid
from secretary.domain.team import TeamMember, TaskSnapshot, content_hash
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository


SECRET = 'synthetic-publication-service-secret'
BASE_URL = 'http://127.0.0.1:8766'
HEADER = 'X-Secretary-Service-Secret'


def commands():
    return import_module('secretary.application.publication_commands')


def bridge_api():
    return import_module('secretary.infrastructure.publication_bridge')


def gateway_api():
    return import_module('secretary.interface.internal_publications')


@pytest.fixture
def bridge_case(tmp_path):
    source_path = tmp_path / 'secretary.sqlite3'
    with sqlite3.connect(source_path) as conn:
        conn.execute('CREATE TABLE meetings(id TEXT PRIMARY KEY, text TEXT)')
        conn.execute('INSERT INTO meetings VALUES(?,?)', ('meeting', 'Synthetic original source'))
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    repository = TeamRepository(TeamDatabase(tmp_path / 'team.sqlite3'))
    owner = TeamMember(id=str(uuid4()), display_name='Owner', role='owner', max_user_id='11',
                       vikunja_user_id='21', project_ids=('7',))
    member = TeamMember(id=str(uuid4()), display_name='Member', role='member', max_user_id='12',
                        vikunja_user_id='22', project_ids=('7',))
    repository.upsert_member(owner, expected_revision=None)
    repository.upsert_member(member, expected_revision=None)
    snapshot = TaskSnapshot(task_id='9007199254740997', project_id='7', revision=0,
        remote_fingerprint='a' * 64, title='Existing task', assignee_id=member.id)
    repository.save_projection(snapshot, expected_revision=None)
    scope = PublicationScope(meeting_id='meeting', transcript_version=3, summary_version=4,
                             destination_project_id='7')
    item = PublicationTask(action_id='action', title='Prepare report', assignee_id=member.id,
        source_segment_ids=('s1', 's2'), evidence_quote='Я подготовлю отчёт <буквально>.',
        due_at='2026-10-09T18:00:00+03:00', due_phrase='к пятнице', due_confirmed=True,
        source_fingerprint='assignment-evidence-v1:' + 'f' * 64,
        publication_id=publication_uuid(scope, 'action'))
    return SimpleNamespace(repository=repository, owner=owner, member=member, scope=scope,
        item=item, snapshot=snapshot, source_path=source_path, source_hash=source_hash)


def envelope(c, **updates):
    item = c.item.model_copy(update=updates)
    if item.intent == 'create_separate':
        item = item.model_copy(update={'publication_id': publication_uuid(c.scope, item.action_id, item.separate_id)})
    return GatewayPublication(actor_id=c.owner.id, scope=c.scope, item=item)


def target_envelope(c, intent):
    return envelope(c, intent=intent, target_publication_id=str(uuid4()), target_task_id=c.snapshot.task_id,
        expected_task_revision=c.snapshot.revision, expected_task_fingerprint=c.snapshot.remote_fingerprint,
        target_snapshot=c.snapshot)


def app_client(c, *, client=('127.0.0.1', 43123)):
    app = gateway_api().create_publication_gateway(c.repository, SECRET, c.owner.id)
    return TestClient(app, base_url=BASE_URL, client=client)


def receipt(c, env, *, applied=False):
    value = c.repository.accept_command(c.owner.id, commands().publication_task_command(env))
    if applied:
        claim = c.repository.claim_command('synthetic-worker')
        snapshot = c.snapshot.model_copy(update={'task_id': '9007199254740999', 'revision': 0,
                                                'title': env.item.title})
        value = c.repository.record_remote_result(claim, state='applied', snapshot=snapshot)
    return value.model_dump(mode='json')


@pytest.mark.parametrize('intent', ['publish', 'create_separate'])
def test_create_command_preserves_literal_source_and_confirmed_date(bridge_case, intent):
    c = bridge_case
    env = envelope(c, intent=intent, separate_id=str(uuid4()) if intent == 'create_separate' else None)
    command = commands().publication_task_command(env)
    assert command.action == 'create' and command.operation_id == delivery_uuid(c.scope, env.item)
    assert command.values.title == c.item.title and command.values.assignee_id == c.member.id
    assert command.expected_assignee_revision == c.member.revision
    assert command.values.due_at.isoformat() == '2026-10-09T15:00:00+00:00'
    assert command.values.due_phrase == 'к пятнице' and command.values.due_confirmed is True
    assert command.values.due_timezone == 'Europe/Moscow'
    assert c.item.evidence_quote in command.values.description
    assert all(segment in command.values.description for segment in c.item.source_segment_ids)
    assert command.origin.publication_id == env.item.publication_id
    assert command.origin.meeting_id == c.scope.meeting_id and command.origin.transcript_version == 3
    assert command.origin.summary_version == 4 and command.origin.action_id == c.item.action_id
    assert len(command.values.description.encode('utf-8')) <= 16 * 1024
    expected = content_hash({'actor_id': c.owner.id, 'command': command.model_dump(mode='json', exclude_unset=True)})
    assert commands().gateway_payload_hash(env) == expected


@pytest.mark.parametrize('intent,action', [('link', 'link'), ('propose_update', 'comment')])
def test_existing_target_commands_use_frozen_target_and_never_mutate_assignment(bridge_case, intent, action):
    c = bridge_case
    env = target_envelope(c, intent)
    command = commands().publication_task_command(env)
    assert command.action == action and command.task_id == c.snapshot.task_id
    assert command.expected_revision == c.snapshot.revision and command.expected_fingerprint == 'a' * 64
    assert command.expected_assignee_revision is None
    assert command.values.assignee_id is None and command.values.due_at is None
    if intent == 'link':
        assert command.values.model_fields_set == set()
    else:
        assert command.values.model_fields_set == {'comment'}
        assert all(value in command.values.comment for value in (c.item.title, c.member.id, c.item.publication_id))
        assert len(command.values.comment) <= 4000
        assert command.operation_id != env.item.publication_id


def test_formatter_refuses_oversized_literal_payload_without_silent_truncation(bridge_case):
    c = bridge_case
    env = envelope(c, source_segment_ids=('x' * 4000, 'y' * 4000, 'z' * 4000, 'w' * 4000, 'v' * 4000))
    with pytest.raises(commands().PublicationCommandError) as caught:
        commands().publication_task_command(env)
    assert str(caught.value) == 'publication_description_too_large'
    assert 'xxxx' not in str(caught.value)


def test_gateway_queues_once_replays_acceptance_and_only_worker_produces_applied(bridge_case):
    c = bridge_case
    env = envelope(c)
    with app_client(c) as client:
        first = client.post('/internal/v1/task-publications', json=env.model_dump(mode='json'), headers={HEADER: SECRET})
        assert first.status_code == 202 and first.json()['execution_state']['state'] == 'queued'
        replay = client.post('/internal/v1/task-publications', json=env.model_dump(mode='json'), headers={HEADER: SECRET})
        assert replay.json()['acceptance_receipt'] == first.json()['acceptance_receipt']
        with c.repository.db.connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 1
        operation_id = delivery_uuid(c.scope, env.item)
        queued = client.get('/internal/v1/task-publications/' + operation_id, headers={HEADER: SECRET})
        assert queued.status_code == 202
        claim = c.repository.claim_command('synthetic-worker')
        c.repository.record_remote_result(claim, state='applied', snapshot=c.snapshot.model_copy(
            update={'task_id': '9007199254740999', 'title': c.item.title}))
        applied = client.get('/internal/v1/task-publications/' + operation_id, headers={HEADER: SECRET})
        assert applied.status_code == 200 and applied.json()['execution_state']['state'] == 'applied'
    assert hashlib.sha256(c.source_path.read_bytes()).hexdigest() == c.source_hash


@pytest.mark.parametrize('peer,host,secret', [
    (('192.0.2.1', 43123), '127.0.0.1:8766', SECRET),
    (('127.0.0.1', 43123), 'evil.example', SECRET),
    (('127.0.0.1', 43123), '127.0.0.1:8766', 'wrong'),
])
def test_gateway_refuses_peer_host_and_secret_before_source_parsing(bridge_case, peer, host, secret):
    c = bridge_case
    with app_client(c, client=peer) as client:
        response = client.post('/internal/v1/task-publications', content=b'PRIVATE_SYNTHETIC_SOURCE malformed',
            headers={'Host': host, HEADER: secret, 'X-Forwarded-For': '127.0.0.1', 'Forwarded': 'for=127.0.0.1'})
    assert response.status_code == 403 and 'PRIVATE_SYNTHETIC_SOURCE' not in response.text
    assert c.repository.claim_command('check') is None


@pytest.mark.parametrize('change', ['actor', 'disabled', 'demoted', 'project'])
def test_gateway_requires_exact_current_owner_and_project_access(bridge_case, change):
    c = bridge_case
    env = envelope(c)
    if change == 'actor':
        env = env.model_copy(update={'actor_id': c.member.id})
    else:
        fields = {'disabled': {'enabled': False}, 'demoted': {'role': 'member'}, 'project': {'project_ids': ('8',)}}[change]
        c.repository.upsert_member(c.owner.model_copy(update={**fields, 'revision': 1}), expected_revision=0)
    with app_client(c) as client:
        response = client.post('/internal/v1/task-publications', json=env.model_dump(mode='json'), headers={HEADER: SECRET})
    assert response.status_code == 403 and c.item.evidence_quote not in response.text


def test_gateway_revocation_also_blocks_receipt_read(bridge_case):
    c = bridge_case
    env = envelope(c)
    operation_id = delivery_uuid(c.scope, env.item)
    with app_client(c) as client:
        assert client.post('/internal/v1/task-publications', json=env.model_dump(mode='json'), headers={HEADER: SECRET}).status_code == 202
        c.repository.upsert_member(c.owner.model_copy(update={'role': 'member', 'revision': 1}), expected_revision=0)
        response = client.get('/internal/v1/task-publications/' + operation_id, headers={HEADER: SECRET})
        assert response.status_code == 403 and c.item.evidence_quote not in response.text


def test_gateway_has_no_docs_or_other_application_routes_and_sanitizes_invalid_input(bridge_case):
    c = bridge_case
    with app_client(c) as client:
        for path in ('/docs', '/openapi.json', '/api/v1/config'):
            assert client.get(path, headers={HEADER: SECRET}).status_code == 404
        invalid = client.post('/internal/v1/task-publications', json={'quote': 'PRIVATE_SYNTHETIC_SOURCE'}, headers={HEADER: SECRET})
        assert invalid.status_code == 422 and 'PRIVATE_SYNTHETIC_SOURCE' not in invalid.text
        missing = client.get('/internal/v1/task-publications/' + str(uuid4()), headers={HEADER: SECRET})
        assert missing.status_code == 404


@pytest.mark.parametrize('headers', [
    [(b'host', b'127.0.0.1:8766'), (b'host', b'evil.example'), (HEADER.lower().encode(), SECRET.encode())],
    [(b'host', b'127.0.0.1:8766'), (HEADER.lower().encode(), SECRET.encode()), (HEADER.lower().encode(), b'wrong')],
])
def test_gateway_rejects_duplicate_security_headers(bridge_case, headers):
    app = gateway_api().create_publication_gateway(bridge_case.repository, SECRET, bridge_case.owner.id)
    assert asyncio.run(asgi_request(app, headers, [b'{}']))[0] == 403


async def asgi_request(app, headers, chunks):
    messages = []
    pending = list(chunks)
    async def receive():
        body = pending.pop(0)
        return {'type': 'http.request', 'body': body, 'more_body': bool(pending)}
    async def send(message):
        messages.append(message)
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST',
        'scheme': 'http', 'path': '/internal/v1/task-publications', 'raw_path': b'/internal/v1/task-publications',
        'query_string': b'', 'headers': headers, 'client': ('127.0.0.1', 43123), 'server': ('127.0.0.1', 8766)}
    await app(scope, receive, send)
    status = next(message['status'] for message in messages if message['type'] == 'http.response.start')
    body = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
    return status, body


@pytest.mark.parametrize('content_length', [None, b'1', b'300000'])
def test_gateway_counts_real_stream_bytes_before_parse(bridge_case, content_length):
    c = bridge_case
    headers = [(b'host', b'127.0.0.1:8766'), (HEADER.lower().encode(), SECRET.encode())]
    if content_length is not None: headers.append((b'content-length', content_length))
    app = gateway_api().create_publication_gateway(c.repository, SECRET, c.owner.id)
    status, body = asyncio.run(asgi_request(app, headers, [b'x' * 150000, b'y' * 150000]))
    assert status == 413 and b'yyyy' not in body
    assert c.repository.claim_command('check') is None


def local_bridge(c, handler, **client_options):
    client = httpx.Client(base_url=BASE_URL, trust_env=False, follow_redirects=False,
                          transport=httpx.MockTransport(handler), **client_options)
    return bridge_api().LocalPublicationBridge(client, base_url=BASE_URL, secret=SECRET, actor_id=c.owner.id)


def test_bridge_validates_queue_identity_hash_and_owned_client_lifecycle(bridge_case):
    c = bridge_case
    env = envelope(c)
    result = receipt(c, env)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers[HEADER] == SECRET and request.url.host == '127.0.0.1'
        assert json.loads(request.content) == env.model_dump(mode='json')
        return httpx.Response(202, json=result)
    bridge = local_bridge(c, handler)
    assert bridge.submit(env).acceptance_receipt.operation_id == delivery_uuid(c.scope, env.item)
    assert len(calls) == 1
    bridge.close()
    assert bridge.client.is_closed


def test_lost_post_ack_uses_get_without_resubmitting_or_treating_404_as_absence(bridge_case):
    c = bridge_case
    env = envelope(c)
    accepted = receipt(c, env)
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == 'POST': raise httpx.ReadTimeout('PRIVATE_SYNTHETIC_RESPONSE', request=request)
        return httpx.Response(202, json=accepted)
    bridge = local_bridge(c, handler)
    with pytest.raises(bridge_api().BridgeUncertain) as caught:
        bridge.submit(env)
    assert 'PRIVATE_SYNTHETIC_RESPONSE' not in str(caught.value)
    assert bridge.read(delivery_uuid(c.scope, env.item)).execution_state.state == 'queued'
    assert calls == ['POST', 'GET']
    absent = local_bridge(c, lambda request: httpx.Response(404, json={'detail': 'PRIVATE_SYNTHETIC_RESPONSE'}))
    with pytest.raises(bridge_api().BridgeUncertain) as caught:
        absent.read(delivery_uuid(c.scope, env.item))
    assert str(caught.value) == 'bridge_read_missing'
    bridge.close()
    absent.close()


@pytest.mark.parametrize('fault', ['operation', 'hash', 'rejected_applied', 'missing_verified', 'wrong_task', 'wrong_project', 'missing_current'])
def test_bridge_rejects_forged_applied_receipts_without_exposing_payload(bridge_case, fault):
    c = bridge_case
    env = envelope(c)
    value = receipt(c, env, applied=True)
    if fault == 'operation': value['acceptance_receipt']['operation_id'] = str(uuid4())
    if fault == 'hash': value['acceptance_receipt']['payload_hash'] = '0' * 64
    if fault == 'rejected_applied': value['acceptance_receipt']['decision'] = 'rejected'
    if fault == 'missing_verified': value['execution_state']['verified_at'] = None
    if fault == 'wrong_task': value['execution_state']['task_id'] = '55'
    if fault == 'wrong_project': value['current']['project_id'] = '8'
    if fault == 'missing_current': value['current'] = None
    bridge = local_bridge(c, lambda request: httpx.Response(200, json=value))
    with pytest.raises(bridge_api().BridgeUncertain) as caught:
        bridge.submit(env)
    assert c.item.evidence_quote not in str(caught.value)
    bridge.close()


@pytest.mark.parametrize('status', [301, 307, 500, 503])
def test_bridge_does_not_retry_or_follow_mutation_redirects(bridge_case, status):
    c = bridge_case
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(status, headers={'location': 'https://evil.example'}, content=b'PRIVATE_SYNTHETIC_RESPONSE')
    bridge = local_bridge(c, handler)
    with pytest.raises(bridge_api().BridgeUncertain) as caught:
        bridge.submit(envelope(c))
    assert calls == ['POST'] and 'PRIVATE_SYNTHETIC_RESPONSE' not in str(caught.value)
    bridge.close()


@pytest.mark.parametrize('url', ['https://127.0.0.1:8766', 'http://localhost:8766', 'http://127.0.0.1:8766@evil.example',
                              'http://127.0.0.1:8766/internal', 'http://127.0.0.1:8766?token=x'])
def test_bridge_refuses_nonliteral_or_noncanonical_loopback_url(bridge_case, url):
    client = httpx.Client(trust_env=False, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    with pytest.raises(ValueError):
        bridge_api().LocalPublicationBridge(client, base_url=url, secret=SECRET, actor_id=bridge_case.owner.id)
    client.close()


@pytest.mark.parametrize('options', [{'trust_env': True}, {'follow_redirects': True}])
def test_bridge_refuses_unsafe_injected_client(bridge_case, options):
    settings = {'trust_env': False, 'follow_redirects': False, **options}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)), **settings)
    with pytest.raises(ValueError):
        bridge_api().LocalPublicationBridge(client, base_url=BASE_URL, secret=SECRET, actor_id=bridge_case.owner.id)
    client.close()


@pytest.mark.parametrize('identifier', [9007199254740993, '1', '00000000-0000-0000-0000-00000000000g'])
def test_bridge_read_refuses_nonuuid_ids_without_request(bridge_case, identifier):
    calls = []
    bridge = local_bridge(bridge_case, lambda request: calls.append(request) or httpx.Response(200))
    with pytest.raises(ValueError): bridge.read(identifier)
    assert calls == []
    bridge.close()


@pytest.mark.parametrize('intent,code', [
    ('propose_update', 'publication_comment_too_large'),
    ('publish', 'publication_payload_too_large'),
])
def test_formatter_rejects_other_payload_bounds_before_transport(bridge_case, intent, code):
    c = bridge_case
    env = (target_envelope(c, intent).model_copy(update={'item': target_envelope(c, intent).item.model_copy(
        update={'title': 'x' * 4000})}) if intent == 'propose_update'
        else envelope(c, due_phrase='x' * (256 * 1024)))
    with pytest.raises(commands().PublicationCommandError) as caught:
        commands().publication_task_command(env)
    assert caught.value.code == code and str(caught.value) == code
    calls = []
    bridge = local_bridge(c, lambda request: calls.append(request) or httpx.Response(200))
    with pytest.raises(bridge_api().BridgeUncertain):
        bridge.submit(env)
    assert calls == []
    bridge.close()


@pytest.mark.parametrize('options', [
    {'auth': ('synthetic-user', 'synthetic-password')},
    {'params': {'leak': 'synthetic'}},
    {'cookies': {'synthetic': 'cookie'}},
    {'headers': {'Authorization': 'synthetic-token'}},
    {'headers': {'Host': 'evil.example'}},
    {'event_hooks': {'request': [lambda request: None]}},
])
def test_bridge_refuses_side_effectful_client_defaults(bridge_case, options):
    client = httpx.Client(trust_env=False, follow_redirects=False,
        transport=httpx.MockTransport(lambda request: httpx.Response(200)), **options)
    with pytest.raises(ValueError):
        bridge_api().LocalPublicationBridge(client, base_url=BASE_URL, secret=SECRET, actor_id=bridge_case.owner.id)
    client.close()


@pytest.mark.parametrize('kind', ['malformed', 'duplicate_key', 'nonfinite', 'oversized', 'queued_200', 'applied_202'])
def test_bridge_refuses_malformed_receipt_or_http_state_without_retry(bridge_case, kind):
    c = bridge_case
    env = envelope(c)
    value = receipt(c, env, applied=kind == 'applied_202')
    status = 200 if kind == 'queued_200' else 202
    data = json.dumps(value).encode()
    if kind == 'malformed': data = b'PRIVATE_SYNTHETIC_SOURCE not json'
    if kind == 'duplicate_key': data = b'{"acceptance_receipt":{},"acceptance_receipt":{}}'
    if kind == 'nonfinite': data = b'{"acceptance_receipt":NaN}'
    if kind == 'oversized': data = b' ' * (2 * 1024 * 1024 + 1)
    calls = []
    bridge = local_bridge(c, lambda request: calls.append(request.method) or httpx.Response(status, content=data))
    with pytest.raises(bridge_api().BridgeUncertain) as caught:
        bridge.submit(env)
    assert calls == ['POST'] and 'PRIVATE_SYNTHETIC_SOURCE' not in str(caught.value)
    bridge.close()


def test_lost_ack_read_still_verifies_original_payload_hash(bridge_case):
    c = bridge_case
    env = envelope(c)
    value = receipt(c, env)
    value['acceptance_receipt']['payload_hash'] = '0' * 64
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == 'POST': raise httpx.ReadTimeout('private', request=request)
        return httpx.Response(202, json=value)
    bridge = local_bridge(c, handler)
    with pytest.raises(bridge_api().BridgeUncertain): bridge.submit(env)
    with pytest.raises(bridge_api().BridgeUncertain) as caught:
        bridge.read(delivery_uuid(c.scope, env.item))
    assert str(caught.value) == 'bridge_receipt_mismatch' and calls == ['POST', 'GET']
    bridge.close()


@pytest.mark.parametrize('content', [b'{"actor_id":"PRIVATE","actor_id":"PRIVATE2"}', b'{"value":NaN}'])
def test_gateway_refuses_ambiguous_json_without_source_echo(bridge_case, content):
    with app_client(bridge_case) as client:
        response = client.post('/internal/v1/task-publications', content=content, headers={HEADER: SECRET})
    assert response.status_code == 422 and 'PRIVATE' not in response.text
    assert bridge_case.repository.claim_command('check') is None


def test_bridge_refuses_explicit_proxy_mount_without_network(bridge_case):
    client = httpx.Client(trust_env=False, follow_redirects=False,
        proxy='http://192.0.2.1:3128')
    try:
        with pytest.raises(ValueError):
            bridge_api().LocalPublicationBridge(client, base_url=BASE_URL, secret=SECRET,
                                                actor_id=bridge_case.owner.id)
    finally:
        client.close()


def test_bridge_sanitizes_unexpected_transport_failure_without_retry(bridge_case):
    calls = []
    def handler(request):
        calls.append(request.method)
        raise TypeError('PRIVATE_SYNTHETIC_RESPONSE')
    bridge = local_bridge(bridge_case, handler)
    try:
        with pytest.raises(bridge_api().BridgeUncertain) as caught:
            bridge.submit(envelope(bridge_case))
        assert str(caught.value) == 'bridge_unavailable' and calls == ['POST']
    finally:
        bridge.close()


@pytest.mark.parametrize('method', ['replay', 'read'])
def test_gateway_owner_revocation_during_repository_boundary_is_atomic(bridge_case, monkeypatch, method):
    c = bridge_case
    env = envelope(c)
    command = commands().publication_task_command(env)
    c.repository.accept_command(c.owner.id, command)
    operation_id = command.operation_id
    target = 'accept_command' if method == 'replay' else 'get_receipt'
    original = getattr(c.repository, target)
    def demote_before_call(*args, **kwargs):
        c.repository.upsert_member(c.owner.model_copy(update={'role': 'member', 'revision': 1}), expected_revision=0)
        return original(*args, **kwargs)
    monkeypatch.setattr(c.repository, target, demote_before_call)
    with app_client(c) as client:
        result = (client.post('/internal/v1/task-publications', json=env.model_dump(mode='json'), headers={HEADER: SECRET})
            if method == 'replay' else client.get('/internal/v1/task-publications/' + operation_id, headers={HEADER: SECRET}))
    assert result.status_code == 403
    assert c.item.evidence_quote not in result.text
