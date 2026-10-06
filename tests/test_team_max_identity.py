"""Explicit MAX GET /me startup identity; synthetic transport only.

Primary contract: https://dev.max.ru/docs-api/methods/GET/me (2026-10-04):
user_id integer<int64>, username string|null, is_bot boolean.
"""
import socket

import httpx
import pytest

from secretary.infrastructure.max_bot import MaxClient, MaxError


TOKEN = 'SYNTHETIC-ONLY-BOT-TOKEN'
BOT_ID = '9007199254740997'


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('MAX identity tests prohibit real provider/network access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture
def client():
    state = {'requests': [], 'status': 200, 'payload': {
        'user_id': int(BOT_ID), 'username': 'synthetic_bot', 'is_bot': True,
        'first_name': 'Synthetic', 'last_activity_time': 1728000000000,
        'description': 'PRIVATE-DESCRIPTION-NOT-IN-IDENTITY',
        'avatar_url': 'https://private.example/signed?secret=PRIVATE-AVATAR'}}
    def handle(request):
        state['requests'].append(request)
        assert request.method == 'GET' and request.url.path == '/me'
        assert request.url.host == 'platform-api2.max.ru'
        assert not request.url.query and not request.content
        assert request.headers['Authorization'] == TOKEN
        if 'error' in state:
            raise state['error']
        if 'raw' in state:
            return httpx.Response(state['status'], content=state['raw'], headers={'Content-Type': 'application/json'})
        return httpx.Response(state['status'], json=state['payload'])
    transport = httpx.MockTransport(handle)
    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False, timeout=5) as http:
        yield MaxClient(TOKEN, bot_id=BOT_ID, client=http), state


def read(port):
    method = getattr(port, 'read_bot_identity', None)
    assert callable(method), 'T12 explicit GET /me identity port missing'
    return method()


def test_constructor_performs_no_identity_or_subscription_get(client):
    _, state = client
    assert state['requests'] == []


def test_get_me_returns_exact_int64_decimal_id_and_only_public_identity(client):
    port, state = client
    assert read(port) == {'id': BOT_ID, 'username': 'synthetic_bot'}
    assert len(state['requests']) == 1
    assert 'PRIVATE' not in str(read(port))
    assert len(state['requests']) == 2  # Every explicit call owns exactly one GET.


def test_nullable_username_is_preserved(client):
    port, state = client
    state['payload']['username'] = None
    assert read(port) == {'id': BOT_ID, 'username': None}


def test_token_for_different_bot_cannot_validate_configuration(client):
    port, state = client
    state['payload']['user_id'] = 123
    with pytest.raises(MaxError) as caught:
        read(port)
    assert caught.value.code == 'max_configuration_invalid'
    assert TOKEN not in str(caught.value)
    assert len(state['requests']) == 1


@pytest.mark.parametrize('identifier', [True, False, 0, -1, 2**63, 9007199254740997.0,
    BOT_ID, None, [], {}])
def test_provider_user_id_must_be_actual_positive_int64_without_coercion(client, identifier):
    port, state = client
    state['payload']['user_id'] = identifier
    with pytest.raises(MaxError) as caught:
        read(port)
    assert caught.value.code == 'max_response_invalid'
    assert len(state['requests']) == 1


@pytest.mark.parametrize('change', [
    {'is_bot': False}, {'is_bot': 1}, {'is_bot': 'true'},
    {'username': 1}, {'username': []}, {'username': 'unsafe\r\nTOKEN'},
    {'username': 'unsafe\x00'}, {'username': '\ud800'},
])
def test_bot_flag_and_nullable_public_username_are_validated(client, change):
    port, state = client
    state['payload'].update(change)
    if change.get('username') == '\ud800':
        # Keep valid ASCII JSON containing an escaped invalid Unicode surrogate.
        state['raw'] = b'{"user_id":9007199254740997,"is_bot":true,"username":"\\ud800"}'
    with pytest.raises(MaxError) as caught:
        read(port)
    assert caught.value.code == 'max_response_invalid'
    assert TOKEN not in str(caught.value)
    assert len(state['requests']) == 1


@pytest.mark.parametrize('field', ['user_id', 'username', 'is_bot'])
def test_required_provider_identity_fields_cannot_be_invented(client, field):
    port, state = client
    del state['payload'][field]
    with pytest.raises(MaxError) as caught:
        read(port)
    assert caught.value.code == 'max_response_invalid'
    assert len(state['requests']) == 1


@pytest.mark.parametrize('status', [201, 302, 401, 403, 429, 500, 503])
def test_http_failure_never_returns_identity_retries_or_posts(client, status):
    port, state = client
    state['status'] = status
    state['payload'] = {'error': TOKEN + '-PRIVATE-RAW-BODY'}
    with pytest.raises(MaxError) as caught:
        read(port)
    assert caught.value.code == ('max_http_rejected' if 400 <= status < 500 else 'max_transport_uncertain')
    assert TOKEN not in str(caught.value) and 'PRIVATE' not in str(caught.value)
    assert len(state['requests']) == 1


def test_timeout_is_safe_and_has_one_get_without_retry(client):
    port, state = client
    state['error'] = httpx.ReadTimeout(TOKEN + '-PRIVATE-ERROR')
    with pytest.raises(MaxError) as caught:
        read(port)
    assert caught.value.code == 'max_transport_uncertain'
    assert TOKEN not in str(caught.value)
    assert len(state['requests']) == 1


@pytest.mark.parametrize('raw', [b'{"user_id":1,"user_id":9007199254740997,"is_bot":true,"username":null}',
    b'not-json', b'[]'])
def test_malformed_or_duplicate_identity_json_is_not_accepted(client, raw):
    port, state = client
    state['raw'] = raw
    with pytest.raises(MaxError) as caught:
        read(port)
    assert caught.value.code == 'max_response_invalid'
    assert len(state['requests']) == 1
