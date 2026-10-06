"""Bounded native protocol diagnostics: synthetic HTTP only, never a permit."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import inspect
import json
import re
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest


TOKEN = 'tk_SYNTHETIC-NATIVE-CREDENTIAL-ONLY-not-an-issued-key'
PRIVATE = 'DO-NOT-RETURN-SERVER-PRIVATE-TEXT'
TOKEN_SHA = hashlib.sha256(TOKEN.encode('ascii')).hexdigest()
UPDATED = '2026-10-05T03:00:00Z'


def implementation():
    name = 'secretary.infrastructure.restore_native_observations'
    assert importlib.util.find_spec(name), 'bounded native GET diagnostic module is absent'
    return importlib.import_module(name)


def member(identifier=19, *, owner=11, permission=1):
    return dict(id=identifier, bot_owner_id=owner, permission=permission,
                username=PRIVATE, email=PRIVATE, name=PRIVATE)


def page(items, *, number=1, size=50, total=None, pages=None):
    total = len(items or []) if total is None else total
    return dict(items=items, page=number, per_page=size, total=total,
                total_pages=(total + size - 1) // size if pages is None else pages)


def case(*, override=None, binding_changes=None, user=None, project=None, pages=None,
         token=TOKEN):
    target = implementation()
    values = dict(port=3456, project_id=7, principal_id=19, human_owner_id=11,
                  credential_sha256=TOKEN_SHA)
    values.update(binding_changes or {})
    binding = target.NativeGetBinding(**values)
    calls = []
    identity = user if user is not None else dict(id=19, bot_owner_id=11,
        username=PRIVATE, email=PRIVATE, settings={'opaque': PRIVATE})
    resource = project if project is not None else dict(id=7, is_archived=False,
        max_permission=1, owner=dict(id=23, bot_owner_id=0, username=PRIVATE),
        updated=UPDATED, title=PRIVATE, description=PRIVATE)
    membership = pages if pages is not None else [page([member()])]

    def handle(request):
        calls.append(request)
        assert request.method == 'GET', 'diagnostic attempted a non-GET operation'
        assert request.url.scheme == 'http' and request.url.host == '127.0.0.1'
        assert request.url.port == 3456
        assert request.headers['authorization'] == f'Bearer {TOKEN}'
        assert request.headers['accept-encoding'] == 'identity'
        custom = override(request) if override is not None else None
        if custom is not None:
            return custom
        if request.url.path == '/api/v2/user':
            assert not request.url.query
            return httpx.Response(200, json=identity)
        if request.url.path == '/api/v2/projects/7':
            assert not request.url.query
            return httpx.Response(200, json=resource)
        if request.url.path == '/api/v2/projects/7/users':
            assert set(request.url.params) == {'page', 'per_page'}
            assert request.url.params['per_page'] == '50'
            number = int(request.url.params['page'])
            assert 1 <= number <= len(membership)
            return httpx.Response(200, json=membership[number - 1])
        raise AssertionError('unexpected synthetic route')

    return SimpleNamespace(target=target, binding=binding, calls=calls,
        transport=httpx.MockTransport(handle), token=SecretStr(token))


async def read(c):
    return await c.target._test_observe_native(c.binding, token=c.token,
                                              transport=c.transport)


def assert_sanitized(error):
    text = str(error)
    assert re.fullmatch(r'native_[a-z_]+', text)
    assert TOKEN not in text and PRIVATE not in text
    assert TOKEN not in repr(error) and PRIVATE not in repr(error)


def test_public_entrypoint_cannot_accept_proof_url_transport_clock_or_evidence_kind():
    target = implementation()
    assert set(inspect.signature(target.observe_native_diagnostic).parameters) == {'binding', 'token'}
    assert set(inspect.signature(target._test_observe_native).parameters) == {'binding', 'token', 'transport'}
    assert target.__name__ != 'secretary.infrastructure.vikunja'


@pytest.mark.asyncio
async def test_owned_public_client_ignores_environment_and_has_no_redirect_retry_or_hooks(monkeypatch):
    c = case()
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY', 'http://127.0.0.1:1')
    async with c.target._direct_client(c.binding, c.token) as client:
        assert type(client._transport) is httpx.AsyncHTTPTransport
        assert client.trust_env is False and client.follow_redirects is False
        assert client._transport._pool._retries == 0
        assert not client._mounts
        assert client.event_hooks == {'request': [], 'response': []}
        assert client.timeout.connect <= 2 and client.timeout.pool <= 2
        assert client.timeout.read <= 5 and client.timeout.write <= 5
    assert c.calls == []


@pytest.mark.asyncio
async def test_current_identity_exact_project_and_direct_membership_are_separate_diagnostic_facts():
    c = case()
    result = await read(c)
    facts = asdict(result)
    assert result.evidence_kind == 'synthetic_get'
    assert result.diagnostic_only is True
    assert result.activation_supported is False and result.outbound_enabled is False
    assert (result.principal_id, result.human_owner_id, result.project_id) == (19, 11, 7)
    assert result.project_owner_id == 23  # Project owner is not inferred from bot ownership.
    assert result.project_max_permission == 1 and result.project_updated_at == UPDATED
    assert [asdict(row) for row in result.direct_members] == [dict(id=19, bot_owner_id=11, permission=1)]
    assert result.pages == 1 and result.credential_sha256 == TOKEN_SHA
    assert re.fullmatch('[0-9a-f]{64}', result.binding_sha256)
    assert datetime.fromisoformat(result.started_at).tzinfo is not None
    assert datetime.fromisoformat(result.finished_at) >= datetime.fromisoformat(result.started_at)
    serialized = json.dumps(facts) + repr(result)
    assert TOKEN not in serialized and PRIVATE not in serialized
    assert not {'token_id', 'token', 'source_evidence', 'activation_permit', 'username', 'email'} & set(facts)
    assert [request.url.path for request in c.calls] == [
        '/api/v2/user', '/api/v2/projects/7', '/api/v2/projects/7/users']


@pytest.mark.parametrize('changes', [
    {'port': 0}, {'port': 80}, {'port': 443}, {'port': 1023}, {'port': 65536},
    {'port': True}, {'port': '3456'}, {'project_id': 0}, {'project_id': -1},
    {'project_id': True}, {'project_id': 2**63}, {'principal_id': 0},
    {'principal_id': '19'}, {'human_owner_id': 0}, {'human_owner_id': None},
    {'human_owner_id': True}, {'human_owner_id': 2**63}, {'human_owner_id': 19},
    {'credential_sha256': 'A' * 64}, {'credential_sha256': 'g' * 64},
    {'credential_sha256': 'a' * 63}, {'credential_sha256': None},
])
def test_closed_binding_rejects_bad_route_identity_or_owner_claims(changes):
    target = implementation()
    with pytest.raises(target.NativeObservationError) as caught:
        case(binding_changes=changes)
    assert_sanitized(caught.value)


def test_closed_binding_cannot_add_an_arbitrary_url_or_path():
    target = implementation()
    for extra in ({'base_url': 'http://example.invalid/api/v2'}, {'path': '/token/test'},
                  {'method': 'POST'}, {'proxy': 'http://127.0.0.1:1'}):
        with pytest.raises(TypeError):
            target.NativeGetBinding(port=3456, project_id=7, principal_id=19,
                human_owner_id=11, credential_sha256=TOKEN_SHA, **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize('token', ['', 'tk_', 'ey.JWT.not-supported', 'tk_with\nnewline',
                                  'tk_неASCII', 'tk_' + 'a' * 4094])
async def test_invalid_preissued_credential_never_sends_a_request(token):
    c = case(token=token)
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert c.calls == []
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_digest_mismatch_never_sends_a_request():
    c = case(binding_changes={'credential_sha256': 'a' * 64})
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert c.calls == []
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_secretary_port_is_rejected_before_io():
    target = implementation()
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 19, 'bot_owner_id': 11})
    with pytest.raises(target.NativeObservationError) as caught:
        binding = target.NativeGetBinding(port=8765, project_id=7, principal_id=19,
            human_owner_id=11, credential_sha256=TOKEN_SHA)
        await target._test_observe_native(binding, token=SecretStr(TOKEN),
                                          transport=httpx.MockTransport(response))
    assert calls == []
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_clock_rewind_is_rejected_without_fabricated_interval(monkeypatch):
    c = case()
    actual = iter([datetime(2026, 10, 5, 3, 1, 0, tzinfo=timezone.utc),
                   datetime(2026, 10, 5, 3, 0, 59, tzinfo=timezone.utc)])
    class ReversedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return next(actual)
    monkeypatch.setattr(c.target, 'datetime', ReversedClock)
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 3
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [
    {'id': 20, 'bot_owner_id': 11}, {'id': 19, 'bot_owner_id': 12},
    {'id': 19, 'bot_owner_id': 0}, {'id': 19}, {'id': True, 'bot_owner_id': 11},
    {'id': '19', 'bot_owner_id': 11}, {'id': 19, 'bot_owner_id': '11'},
    {'id': 2**63, 'bot_owner_id': 11},
])
async def test_current_owner_identity_cannot_be_inferred_from_matching_historical_membership(identity):
    c = case(user=identity)  # Default direct member still matches the configured bot.
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert [request.url.path for request in c.calls] == ['/api/v2/user']
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 404, 429, 500])
async def test_failed_identity_scope_revocation_expiry_or_redirect_never_uses_fallback(status):
    def response(request):
        return httpx.Response(status, json={'message': PRIVATE, 'code': 11},
                              headers={'Location': 'http://127.0.0.1:1/private'})
    c = case(override=response)
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 1
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'id': 8}, {'id': True}, {'is_archived': True}, {'is_archived': 0},
    {'max_permission': 0}, {'max_permission': None}, {'max_permission': True},
    {'max_permission': 3}, {'owner': {'id': 0}}, {'owner': {'id': '23'}},
    {'owner': None}, {'updated': PRIVATE}, {'updated': 'x' * 129},
])
async def test_project_identity_usable_state_permission_owner_and_version_are_required(changes):
    resource = dict(id=7, is_archived=False, max_permission=1,
                    owner=dict(id=23, bot_owner_id=0), updated=UPDATED)
    resource.update(changes)
    c = case(project=resource)
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 2
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['id', 'is_archived', 'max_permission', 'owner', 'updated'])
async def test_project_required_observation_facts_cannot_be_omitted(missing):
    resource = dict(id=7, is_archived=False, max_permission=1,
                    owner=dict(id=23, bot_owner_id=0), updated=UPDATED)
    del resource[missing]
    c = case(project=resource)
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 2
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('members', [[], [member(20)], [member(owner=12)],
    [member(permission=0)], [member(permission=True)], [member(permission=3)],
    [member(permission=None)], [{'id': 19, 'bot_owner_id': 11}],
    [member(), member()], [member(identifier=True)], [member(identifier=2**63)]])
async def test_direct_principal_must_be_present_exactly_once_and_have_current_write_permission(members):
    c = case(pages=[page(members)])
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 3
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_complete_server_capped_pagination_records_only_current_id_permission_facts():
    c = case(pages=[page([member(), member(20, owner=0, permission=0)], size=2, total=3),
                    page([member(21, owner=0, permission=2)], size=2, total=3, number=2)])
    result = await read(c)
    assert result.pages == 2
    assert [row.id for row in result.direct_members] == [19, 20, 21]
    assert [request.url.params['page'] for request in c.calls[2:]] == ['1', '2']


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'page': 0}, {'page': 2}, {'page': True}, {'per_page': 0}, {'per_page': 51},
    {'per_page': True}, {'total': -1}, {'total': 1001}, {'total': True},
    {'total_pages': 21}, {'total_pages': 0}, {'total_pages': 2},
    {'items': None}, {'items': {}}, {'items': [member(), member(20)]},
])
async def test_invalid_first_page_is_not_reported_as_complete(changes):
    body = page([member()])
    body.update(changes)
    c = case(pages=[body])
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 3
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('second', [
    page([member(20)], size=1, total=3, number=2),
    page([member(20)], size=2, total=2, number=2, pages=2),
    page([member(20)], size=1, total=2, number=1),
    page([member()], size=1, total=2, number=2),
    page([], size=1, total=2, number=2),
    page([member(20), member(21)], size=1, total=2, number=2),
])
async def test_changed_repeated_duplicate_missing_or_overfull_later_page_cannot_qualify(second):
    c = case(pages=[page([member()], size=1, total=2), second])
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 4
    assert_sanitized(caught.value)


class FragmentedBody(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks, self.delay = chunks, delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize('body,headers', [
    (b'{"id":19,"id":19,"bot_owner_id":11}', {}),
    (b'{"id":19,"bot_owner_id":11,"x":NaN}', {}),
    (b'{"id":19,"bot_owner_id":11,"x":Infinity}', {}),
    (b'[]', {}), (b'not-json-' + PRIVATE.encode(), {}),
    (b'{"id":19,"bot_owner_id":11}', {'Content-Type': 'text/html'}),
    (b'{"id":19,"bot_owner_id":11}', {'Content-Encoding': 'gzip'}),
    (b'{"id":19,"bot_owner_id":11}', {'Content-Length': '65537'}),
    (b'{"id":19,"bot_owner_id":11}', {'Content-Length': 'not-a-number'}),
    (b'{"id":19,"bot_owner_id":11,"x":' + b'[' * 65 + b'0' + b']' * 65 + b'}', {}),
])
async def test_ambiguous_encoded_malformed_or_unbounded_json_is_sanitized(body, headers):
    response_headers = {'Content-Type': 'application/json', **headers}
    c = case(override=lambda request: httpx.Response(200, content=body, headers=response_headers))
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 1
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_fragmented_body_without_declared_length_is_bounded_and_closed():
    stream = FragmentedBody([b' ' * 32768, b' ' * 32768, b' '])
    c = case(override=lambda request: httpx.Response(200, stream=stream,
                     headers={'Content-Type': 'application/json'}))
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 1 and stream.closed
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_individually_bounded_complete_pages_still_have_an_aggregate_byte_budget():
    def response(request):
        if request.url.path != '/api/v2/projects/7/users':
            return None
        number = int(request.url.params['page'])
        row = member() if number == 1 else member(100 + number, owner=0)
        encoded = json.dumps(page([row], number=number, size=1, total=20)).encode()
        body = encoded + b' ' * (64000 - len(encoded))
        return httpx.Response(200, content=body, headers={'Content-Type': 'application/json'})
    c = case(override=response)
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 19  # Identity + project + 17th individually valid page.
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_slow_body_hits_cooperative_request_bound_before_next_get(monkeypatch):
    stream = FragmentedBody([b'{"id":19,"bot_owner_id":11}'], delay=0.04)
    c = case(override=lambda request: httpx.Response(200, stream=stream,
                     headers={'Content-Type': 'application/json'}))
    monkeypatch.setattr(c.target, '_REQUEST_TIMEOUT', 0.01)
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 1 and stream.closed
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_overall_bound_covers_multiple_otherwise_fast_gets(monkeypatch):
    streams = []
    second_entered, release_second = asyncio.Event(), asyncio.Event()
    bodies = [dict(id=19, bot_owner_id=11),
              dict(id=7, is_archived=False, max_permission=1,
                   owner=dict(id=23, bot_owner_id=0), updated=UPDATED)]
    class PendingSecondBody(FragmentedBody):
        async def __aiter__(self):
            second_entered.set()
            await release_second.wait()
            for chunk in self.chunks:
                yield chunk
    def response(request):
        kind = FragmentedBody if not streams else PendingSecondBody
        stream = kind([json.dumps(bodies[len(streams)]).encode()])
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={'Content-Type': 'application/json'})
    c = case(override=response)
    monkeypatch.setattr(c.target, '_TOTAL_TIMEOUT', 0.5)
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert second_entered.is_set() and not release_second.is_set()
    assert len(c.calls) == 2 and all(stream.closed for stream in streams)
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_transport_exception_cannot_leak_credentials_or_server_text():
    def response(request):
        raise httpx.ReadError(TOKEN + PRIVATE, request=request)
    c = case(override=response)
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert len(c.calls) == 1
    assert_sanitized(caught.value)
