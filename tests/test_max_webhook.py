"""T7 webhook proofs use an isolated ASGI app and never a network listener."""
import asyncio
from dataclasses import replace
from importlib import import_module
import json
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from secretary.domain.bot import IntakeReceipt
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_auth_repository import AuthRepository

_AUTH = None


@pytest.fixture(autouse=True)
def isolated_rate_ledger(tmp_path, monkeypatch):
    auth = AuthRepository(TeamDatabase(tmp_path/'webhook-rate.sqlite3'),
        secret=b'synthetic-webhook-rates-secret-32-bytes')
    monkeypatch.setattr(sys.modules[__name__], '_AUTH', auth)


def wire():
    return {'update_type': 'message_created', 'timestamp': 1791061200000,
        'message': {'sender': {'user_id': 9007199254740993, 'is_bot': False},
            'recipient': {'chat_id': -9007199254740993, 'chat_type': 'chat', 'user_id': None},
            'body': {'mid': 'synthetic-mid', 'seq': 1, 'text': 'Мои задачи'}}}


def normal(value):
    return import_module('secretary.interface.max_webhook').normalize_max_event(value, bot_id='99')


def app_case(intake=None, secret='synthetic_webhook_secret'):
    gateway = import_module('secretary.interface.team_gateway')
    saved = []
    def accept(event):
        saved.append(event)
        return IntakeReceipt(event.event_id, 'queued')
    clients = gateway.TeamGatewayClients(auth=_AUTH, bot_intake=intake or SimpleNamespace(intake=accept))
    settings = gateway.TeamGatewaySettings(public_origin='https://team.example', bot_id='99',
        bot_token='synthetic-token', webhook_secret=secret)
    return gateway.create_team_app(settings, object(), clients), saved


def test_webhook_ack_precedes_slow_provider_work():
    app, events = app_case()
    with TestClient(app, base_url='https://team.example', client=('203.0.113.7',40001)) as client:
        # No worker/HTTP/provider port is constructed by create_team_app.
        response = client.post('/hooks/max', json=wire(), headers={'X-Max-Bot-Api-Secret': 'synthetic_webhook_secret'})
        assert response.status_code == 200 and response.json() == {'ok': True}
        assert len(events) == 1
        assert response.headers['cache-control'] == 'no-store'


def test_secret_is_checked_before_reading_body():
    boundary = import_module('secretary.interface.team_gateway')._PublicBoundary
    seen = []
    async def downstream(scope, receive, send):
        pytest.fail('untrusted request reached parser or dispatch')
    async def receive():
        pytest.fail('untrusted request body read')
    async def send(message):
        seen.append(message)
    scope = {'type': 'http', 'method': 'POST', 'path': '/hooks/max',
             'headers': [(b'host', b'team.example')], 'query_string': b''}
    asyncio.run(boundary(downstream, origin='https://team.example', webhook_secret='synthetic_webhook_secret')(scope, receive, send))
    assert seen[0]['status'] == 401


@pytest.mark.parametrize('headers', [[], [('X-Max-Bot-Api-Secret', 'wrong')],
    [('X-Max-Bot-Api-Secret', 'synthetic_webhook_secret'), ('X-Max-Bot-Api-Secret', 'synthetic_webhook_secret')]])
def test_invalid_or_duplicate_secret_never_parses(headers):
    app, saved = app_case()
    with TestClient(app, base_url='https://team.example', client=('203.0.113.7',40001)) as client:
        response = client.post('/hooks/max', content=b'{not-json', headers=headers)
        assert response.status_code == 401 and not saved


def test_db_failure_does_not_acknowledge_event():
    def fail(event):
        raise sqlite3.OperationalError('SYNTHETIC_PRIVATE_DB_DETAIL')
    app, _ = app_case(SimpleNamespace(intake=fail))
    with TestClient(app, base_url='https://team.example', client=('203.0.113.7',40001)) as client:
        r = client.post('/hooks/max', json=wire(), headers={'X-Max-Bot-Api-Secret': 'synthetic_webhook_secret'})
        assert r.status_code == 503 and 'PRIVATE_DB' not in r.text


def test_unconfigured_intake_is_disabled():
    g = import_module('secretary.interface.team_gateway')
    settings = g.TeamGatewaySettings(public_origin='https://team.example', bot_id='99', bot_token='synthetic')
    app = g.create_team_app(settings, object(), g.TeamGatewayClients(auth=object()))
    with TestClient(app, base_url='https://team.example', client=('203.0.113.7',40001)) as client:
        assert client.post('/hooks/max', content=b'{bad').status_code == 503


@pytest.mark.parametrize('payload,status', [(b'{"update_type":"a","update_type":"b"}', 400),
    (b'{}' + b' ' * (1024 * 1024), 413)], ids=['duplicate-json', 'large-body'])
def test_trusted_invalid_or_large_body_is_rejected(payload, status):
    app, saved = app_case()
    with TestClient(app, base_url='https://team.example', client=('203.0.113.7',40001)) as client:
        r = client.post('/hooks/max', content=payload, headers={'X-Max-Bot-Api-Secret': 'synthetic_webhook_secret'})
        assert r.status_code == status and not saved


def test_message_exact_identity_and_provider_stable_key():
    one = normal(wire())
    assert one.user_id == '9007199254740993' and one.chat_id == '-9007199254740993'
    assert not one.quarantine_reason and one.actor_id is None
    changed = wire()
    changed['timestamp'] += 1000
    assert normal(changed).dedup_key == one.dedup_key
    changed['message']['body']['mid'] = 'different-mid'
    assert normal(changed).dedup_key != one.dedup_key


def test_callback_can_have_null_message_but_not_missing_identity():
    value = {'update_type': 'message_callback', 'timestamp': 1791061200000,
        'callback': {'callback_id': 'cb-synthetic', 'user': {'user_id': 12, 'is_bot': False}, 'payload': 'opaque'}, 'message': None}
    e = normal(value)
    assert e.callback_id == 'cb-synthetic' and e.user_id == '12' and not e.quarantine_reason
    value['callback']['user']['user_id'] = None
    assert normal(value).quarantine_reason


def test_missing_message_is_quarantined_without_charge():
    event = normal({'update_type': 'message_created', 'timestamp': 1791061200000, 'message': None})
    assert event.quarantine_reason and event.text is None and not event.media
    app, events = app_case()
    with TestClient(app, base_url='https://team.example', client=('203.0.113.7',40001)) as client:
        r = client.post('/hooks/max', json={'update_type': 'message_created', 'timestamp': 1791061200000},
            headers={'X-Max-Bot-Api-Secret': 'synthetic_webhook_secret'})
        assert r.status_code == 200 and events[0].quarantine_reason


@pytest.mark.parametrize('bad', [True, 12.0, '12', 0, 2**63, -3])
def test_provider_id_is_an_exact_positive_int64(bad):
    value = wire()
    value['message']['sender']['user_id'] = bad
    assert normal(value).quarantine_reason


def test_invitation_remains_transient_and_is_not_in_repr():
    value = {'update_type': 'bot_started', 'timestamp': 1791061200000, 'chat_id': 13,
        'user': {'user_id': 12, 'is_bot': False}, 'payload': 'synthetic_invitation_secret'}
    e = normal(value)
    assert e.invitation_value == value['payload'] and value['payload'] not in repr(e)
    assert normal({**value, 'timestamp': value['timestamp'] + 100}).dedup_key == e.dedup_key


def test_quarantined_event_drops_sensitive_payload():
    value = wire()
    value['message']['body']['mid'] = None
    value['message']['body']['text'] = 'PRIVATE_SYNTHETIC_TEXT'
    value['message']['body']['attachments'] = [{'type': 'audio', 'payload': {'url': 'https://example.invalid/private-token'}}]
    e = normal(value)
    assert e.quarantine_reason and e.text is None and e.media == () and e.callback_payload is None


def test_unknown_update_is_a_quarantine_and_not_a_body_hash_identity():
    e = normal({'update_type': 'synthetic_unknown', 'timestamp': 1791061200000})
    assert e.quarantine_reason and e.dedup_key.startswith('quarantine:')


@pytest.mark.parametrize('kind', ['unknown\x00kind', '\ud800', '', 33, 'UPPERCASE'])
def test_unsafe_update_kind_is_sanitized_before_quarantine(kind):
    e = normal({'update_type': kind, 'timestamp': 1791061200000})
    assert e.kind == 'invalid_update' and e.quarantine_reason


def test_empty_trusted_body_is_a_sanitized_bad_request():
    app, events = app_case()
    with TestClient(app, base_url='https://team.example', client=('203.0.113.7',40001), raise_server_exceptions=False) as client:
        response = client.post('/hooks/max', content=b'', headers={'X-Max-Bot-Api-Secret': 'synthetic_webhook_secret'})
        assert response.status_code == 400 and not events
        assert response.json()['error_code'] == 'team_json_invalid'


@pytest.mark.parametrize('bot_id', ['synthetic', '00', '-1', str(2**63)])
def test_gateway_requires_positive_decimal_int64_bot_id(bot_id):
    gateway = import_module('secretary.interface.team_gateway')
    with pytest.raises(ValueError):
        gateway.TeamGatewaySettings(public_origin='https://team.example', bot_id=bot_id,
            bot_token='synthetic-token', webhook_secret='synthetic_webhook_secret')
