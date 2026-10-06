"""MAX outbound transport tests use MockTransport and synthetic data only."""
from __future__ import annotations

from importlib import import_module
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest


TOKEN = 'SYNTHETIC_MAX_TOKEN_NOT_A_CREDENTIAL'
BOT_ID = '99'
USER_ID = '9007199254740993'
BASE_URL = 'https://platform-api2.max.ru'


def api():
    return import_module('secretary.infrastructure.max_bot')


def domain():
    return import_module('secretary.domain.bot')


def send(**updates):
    fields = {'operation_id': str(uuid4()), 'user_id': USER_ID, 'text': 'Synthetic task <literal>', 'buttons': (), 'sensitive': False}
    return domain().BotSend(**{**fields, **updates})


def success(request, **updates):
    body = json.loads(request.content)
    result = {'message': {'sender': {'user_id': int(BOT_ID)},
        'recipient': {'user_id': int(USER_ID), 'chat_type': 'dialog', 'chat_id': 1},
        'timestamp': 1791061200000, 'body': {'mid': 'synthetic-mid', 'seq': 1, 'text': body['text']}}}
    result['message'].update(updates)
    return httpx.Response(200, json=result)


def client(handler, **options):
    settings = {'trust_env': False, 'follow_redirects': False, 'timeout': httpx.Timeout(5),
        'transport': httpx.MockTransport(handler), **options}
    return api().MaxClient(TOKEN, bot_id=BOT_ID, client=httpx.Client(**settings))


def desired():
    return domain().MaxSubscription(url='https://team.example.invalid/hooks/max',
        update_types=('message_created', 'message_callback', 'bot_started'), secret='SYNTHETIC_WEBHOOK_SECRET')


def subscription_response(url=None, types=None, **extra):
    return {'url': url or desired().url, 'time': 1791061200000,
        'update_types': list(types or desired().update_types), **extra}


def test_message_uses_fixed_https_raw_token_exact_query_and_only_documented_body():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'POST' and str(request.url).startswith(BASE_URL + '/messages?')
        assert dict(request.url.params) == {'user_id': USER_ID, 'disable_link_preview': 'true'}
        assert request.headers['Authorization'] == TOKEN
        assert 'Idempotency-Key' not in request.headers and 'access_token' not in request.url.params
        body = json.loads(request.content)
        assert body == {'text': 'Synthetic task <literal>', 'notify': True}
        return success(request)
    transport = client(handler)
    command = send()
    result = transport.send_message(command)
    assert result.operation_id == command.operation_id and result.state == 'sent' and result.message_id == 'synthetic-mid'
    assert len(calls) == 1 and TOKEN not in repr(result)
    transport.close()
    assert transport.client.is_closed


@pytest.mark.parametrize('timestamp,expected', [
    (0, '1970-01-01T00:00:00.000+00:00'),
    (1791061200123, '2026-10-03T21:00:00.123+00:00'),
    (253402300799999, '9999-12-31T23:59:59.999+00:00'),
])
def test_message_receipt_preserves_only_exact_max_acceptance_timestamp(timestamp, expected):
    transport = client(lambda request: success(request, timestamp=timestamp))
    try:
        result = transport.send_message(send())
        assert result.state == 'sent' and result.accepted_at == expected
    finally:
        transport.close()


@pytest.mark.parametrize('timestamp', [None, True, False, -1, 1791061200123.0,
    '1791061200123', 253402300800000, 2 ** 63 - 1, 2 ** 63, 10 ** 400])
def test_invalid_max_acceptance_timestamp_is_uncertain_without_local_time(timestamp):
    calls = []
    transport = client(lambda request: calls.append(request.method) or success(request, timestamp=timestamp))
    try:
        result = transport.send_message(send())
        assert result.state == 'uncertain' and result.error_code == 'max_response_invalid'
        assert result.accepted_at is None and calls == ['POST']
    finally:
        transport.close()


def test_absent_max_acceptance_timestamp_is_uncertain_without_local_time():
    def handler(request):
        value = json.loads(success(request).content)
        value['message'].pop('timestamp')
        return httpx.Response(200, json=value)
    transport = client(handler)
    try:
        result = transport.send_message(send())
        assert result.state == 'uncertain' and result.error_code == 'max_response_invalid'
        assert result.accepted_at is None
    finally:
        transport.close()


def test_keyboard_maps_callback_link_and_native_open_app_without_url_alias():
    buttons = ((domain().BotButton(text='Done', kind='callback', payload='opaque_confirm'),
        domain().BotButton(text='Desktop', kind='link', url='https://team.example.invalid/team'),
        domain().BotButton(text='Board', kind='open_app', web_app='SyntheticSecretaryBot', contact_id=BOT_ID, payload='board')),)
    def handler(request):
        body = json.loads(request.content)
        assert body['attachments'] == [{'type': 'inline_keyboard', 'payload': {'buttons': [[
            {'type': 'callback', 'text': 'Done', 'payload': 'opaque_confirm'},
            {'type': 'link', 'text': 'Desktop', 'url': 'https://team.example.invalid/team'},
            {'type': 'open_app', 'text': 'Board', 'web_app': 'SyntheticSecretaryBot', 'contact_id': 99, 'payload': 'board'},
        ]]}}]
        return success(request)
    transport = client(handler)
    assert transport.send_message(send(buttons=buttons)).state == 'sent'
    transport.close()


def test_sensitive_message_disables_push_without_returning_source_text():
    def handler(request):
        assert json.loads(request.content)['notify'] is False
        return success(request)
    transport = client(handler)
    result = transport.send_message(send(text='PRIVATE_SYNTHETIC_CODE', sensitive=True))
    assert result.state == 'sent' and 'PRIVATE' not in repr(result)
    transport.close()


@pytest.mark.parametrize('status,expected', [(400, 'rejected'), (401, 'rejected'), (403, 'rejected'),
    (404, 'rejected'), (429, 'retryable'), (301, 'uncertain'), (307, 'uncertain'), (500, 'uncertain'), (503, 'uncertain')])
def test_429_and_ambiguous_send_have_distinct_retry_policy(status, expected):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(status, headers={'Location': 'https://evil.example', 'Retry-After': '7'},
            content=b'PRIVATE_SYNTHETIC_BODY_AND_SIGNED_URL')
    transport = client(handler)
    result = transport.send_message(send())
    assert result.state == expected and calls == ['POST']
    assert result.accepted_at is None
    assert 'PRIVATE' not in repr(result) and TOKEN not in repr(result)
    if status == 429: assert result.retry_after == 7 and result.error_code == 'max_rate_limited'
    transport.close()


@pytest.mark.parametrize('fault', ['timeout', 'read_error', 'unexpected'])
def test_ambiguous_transport_failure_never_reposts(fault):
    calls = []
    def handler(request):
        calls.append(request.method)
        error = {'timeout': httpx.ReadTimeout, 'read_error': httpx.ReadError, 'unexpected': TypeError}[fault]
        raise error('PRIVATE_SYNTHETIC_BODY')
    transport = client(handler)
    result = transport.send_message(send())
    assert result.state == 'uncertain' and calls == ['POST'] and 'PRIVATE' not in repr(result)
    assert result.accepted_at is None
    assert result.error_code == 'max_transport_uncertain'
    transport.close()


@pytest.mark.parametrize('fault', ['missing_mid', 'wrong_recipient', 'numeric_string', 'bool_id', 'wrong_sender',
    'wrong_text', 'malformed', 'duplicate', 'nonfinite', 'oversized', 'wrong_content_type'])
def test_200_without_valid_acceptance_evidence_is_uncertain(fault):
    calls = []
    def handler(request):
        calls.append(request.method)
        value = json.loads(success(request).content)
        message = value['message']
        if fault == 'missing_mid': message['body'].pop('mid')
        if fault == 'wrong_recipient': message['recipient']['user_id'] = 42
        if fault == 'numeric_string': message['recipient']['user_id'] = USER_ID
        if fault == 'bool_id': message['recipient']['user_id'] = True
        if fault == 'wrong_sender': message['sender']['user_id'] = 42
        if fault == 'wrong_text': message['body']['text'] = 'PRIVATE_UNEXPECTED_TEXT'
        if fault == 'malformed': return httpx.Response(200, content=b'PRIVATE', headers={'Content-Type': 'application/json'})
        if fault == 'duplicate': return httpx.Response(200, content=b'{"message":{},"message":{}}', headers={'Content-Type': 'application/json'})
        if fault == 'nonfinite': return httpx.Response(200, content=b'{"message":NaN}', headers={'Content-Type': 'application/json'})
        if fault == 'oversized': return httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1), headers={'Content-Type': 'application/json'})
        if fault == 'wrong_content_type': return httpx.Response(200, content=json.dumps(value).encode(), headers={'Content-Type': 'text/html'})
        return httpx.Response(200, json=value)
    transport = client(handler)
    result = transport.send_message(send())
    assert result.state == 'uncertain' and calls == ['POST'] and 'PRIVATE' not in repr(result)
    assert result.error_code == 'max_response_invalid'
    transport.close()


@pytest.mark.parametrize('options', [{'trust_env': True}, {'follow_redirects': True}, {'timeout': None}, {'timeout': 11},
    {'headers': {'Authorization': 'wrong'}}, {'headers': {'Host': 'evil.example'}}, {'params': {'token': 'PRIVATE'}},
    {'cookies': {'private': 'value'}}, {'auth': ('private', 'password')}, {'event_hooks': {'request': [lambda request: None]}},
    {'base_url': 'https://evil.example'}, {'base_url': BASE_URL + '/private'}])
def test_unsafe_injected_client_is_rejected_before_http(options):
    http = httpx.Client(trust_env=False, follow_redirects=False, timeout=5,
        transport=httpx.MockTransport(lambda request: pytest.fail('No HTTP expected')),
        **{key: value for key, value in options.items() if key not in ('trust_env', 'follow_redirects', 'timeout')})
    for name in ('trust_env', 'follow_redirects', 'timeout'):
        if name in options:
            if name == 'trust_env': http._trust_env = options[name]
            else: setattr(http, name, options[name])
    try:
        with pytest.raises(api().MaxError): api().MaxClient(TOKEN, bot_id=BOT_ID, client=http)
    finally:
        http.close()


@pytest.mark.parametrize('options', [{'verify': False}, {'transport': httpx.HTTPTransport(retries=1)}, {'proxy': 'http://192.0.2.1:3128'}])
def test_tls_proxy_and_hidden_retry_bypasses_are_rejected_without_network(options):
    http = httpx.Client(trust_env=False, follow_redirects=False, timeout=5, **options)
    try:
        with pytest.raises(api().MaxError): api().MaxClient(TOKEN, bot_id=BOT_ID, client=http)
    finally:
        http.close()


def test_client_safety_is_rechecked_before_each_request():
    calls = []
    transport = client(lambda request: calls.append(request) or success(request))
    transport.client.event_hooks['request'].append(lambda request: None)
    with pytest.raises(api().MaxError): transport.send_message(send())
    assert calls == []
    transport.close()


def test_subscription_read_only_exposes_url_types_time_without_provider_secrets():
    def handler(request):
        assert request.method == 'GET' and request.url.path == '/subscriptions'
        return httpx.Response(200, json={'subscriptions': [subscription_response(secret='PRIVATE', token='PRIVATE',
            url='https://other.example.invalid/hooks?signature=PRIVATE')]})
    transport = client(handler)
    result = transport.read_subscription()
    assert result == ({'url': 'https://other.example.invalid/hooks', 'time': 1791061200000,
                       'update_types': list(desired().update_types)},)
    assert 'PRIVATE' not in repr(result)
    transport.close()


def test_subscription_recovery_touches_only_owned_subscription():
    calls = []
    foreign = subscription_response(url='https://other.example.invalid/hooks', types=('message_created',))
    def handler(request):
        calls.append(request.method)
        assert request.headers['Authorization'] == TOKEN
        if request.method == 'POST':
            assert json.loads(request.content) == {'url': desired().url, 'update_types': list(desired().update_types), 'secret': desired().secret}
            return httpx.Response(200, json={'success': True})
        return httpx.Response(200, json={'subscriptions': [foreign] + ([subscription_response()] if len(calls) == 3 else [])})
    transport = client(handler)
    result = transport.register_own_subscription(desired())
    assert result.state == 'verified' and result.verified_at is not None and calls == ['GET', 'POST', 'GET']
    assert 'PRIVATE' not in repr(result) and desired().secret not in repr(desired())
    transport.close()


def test_existing_matching_subscription_still_posts_to_establish_secret_provenance():
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={'success': True} if request.method == 'POST' else {'subscriptions': [subscription_response()]})
    transport = client(handler)
    assert transport.register_own_subscription(desired()).state == 'verified'
    assert calls == ['GET', 'POST', 'GET']
    transport.close()


@pytest.mark.parametrize('fault', ['false', 'timeout', 'missing', 'wrong_types', 'foreign_removed', 'malformed'])
def test_subscription_failure_never_returns_verified_or_reposts(fault):
    calls = []
    foreign = subscription_response(url='https://other.example.invalid/hooks', types=('message_created',))
    def handler(request):
        calls.append(request.method)
        if request.method == 'POST':
            if fault == 'timeout': raise httpx.ReadTimeout('PRIVATE_SECRET')
            if fault == 'malformed': return httpx.Response(200, json={'success': 'true', 'secret': 'PRIVATE'})
            return httpx.Response(200, json={'success': fault != 'false', 'message': 'PRIVATE'})
        if len(calls) == 1: return httpx.Response(200, json={'subscriptions': [foreign]})
        values = [] if fault == 'missing' else [subscription_response(types=('bot_started',) if fault == 'wrong_types' else None)]
        if fault != 'foreign_removed': values.append(foreign)
        return httpx.Response(200, json={'subscriptions': values})
    transport = client(handler)
    result = transport.register_own_subscription(desired())
    assert result.state in ('rejected', 'uncertain') and result.verified_at is None
    assert calls.count('POST') == 1 and 'PRIVATE' not in repr(result)
    transport.close()


def test_frozen_contract_extension_marks_documented_only_and_real_unknowns():
    contract = json.loads((Path(__file__).resolve().parents[1] / 'docs/contracts/max-transport-contract.json').read_text(encoding='utf-8'))
    assert contract['qualification']['live_authenticated_verified'] is False
    assert contract['messages']['response_message_id'] == 'message.body.mid'
    assert contract['buttons']['open_app']['web_app_semantics'] == 'public_bot_name'
    assert contract['subscriptions']['secret_readback_supported'] is False
    assert contract['transport']['automatic_post_retries'] == 0


@pytest.mark.parametrize('identifier', [1, True, '01', '0', str(2 ** 63)])
def test_transport_revalidates_even_constructed_invalid_recipient_before_http(identifier):
    calls = []
    transport = client(lambda request: calls.append(request) or success(request))
    command = send()
    object.__setattr__(command, 'user_id', identifier)
    try:
        with pytest.raises(api().MaxError) as caught: transport.send_message(command)
        assert str(caught.value) == 'max_request_invalid' and calls == []
    finally:
        transport.close()


@pytest.mark.parametrize('url', ['http://public.example.invalid/hooks/max', 'https://localhost/hooks/max',
    'https://127.0.0.1/hooks/max', 'https://user:PRIVATE@team.example.invalid/hooks/max',
    'https://team.example.invalid:8443/hooks/max', 'https://team.example.invalid/hooks/max?token=PRIVATE',
    'https://team.example.invalid/other'])
def test_unsafe_subscription_is_rejected_before_read_or_write(url):
    calls = []
    transport = client(lambda request: calls.append(request) or httpx.Response(200))
    value = desired()
    object.__setattr__(value, 'url', url)
    try:
        with pytest.raises(api().MaxError) as caught: transport.register_own_subscription(value)
        assert str(caught.value) == 'max_request_invalid' and calls == []
    finally:
        transport.close()


@pytest.mark.parametrize('payload', [
    {'subscriptions': [{}]},
    {'subscriptions': [subscription_response(time=True)]},
    {'subscriptions': [subscription_response(update_types=['message_created', 'message_created'])]},
    {'subscriptions': [subscription_response(), subscription_response()]},
    {'subscriptions': None},
])
def test_subscription_schema_errors_are_sanitized_without_partial_records(payload):
    calls = []
    transport = client(lambda request: calls.append(request.method) or httpx.Response(200, json=payload))
    try:
        with pytest.raises(api().MaxError) as caught: transport.read_subscription()
        assert caught.value.code == 'max_response_invalid' and calls == ['GET'] and 'PRIVATE' not in str(caught.value)
    finally:
        transport.close()


def test_preflight_subscription_read_failure_does_not_authorize_post():
    calls = []
    transport = client(lambda request: calls.append(request.method) or httpx.Response(500, content=b'PRIVATE'))
    try:
        result = transport.register_own_subscription(desired())
        assert result.state == 'uncertain' and calls == ['GET'] and 'PRIVATE' not in repr(result)
    finally:
        transport.close()


@pytest.mark.parametrize('retry', ['bogus PRIVATE', '-1', '9999999999', '0'])
def test_rate_limit_wait_hint_is_bounded_without_auto_retry(retry):
    calls = []
    transport = client(lambda request: calls.append(request.method) or httpx.Response(429, headers={'Retry-After': retry}))
    try:
        result = transport.send_message(send())
        assert result.state == 'retryable' and 1 <= result.retry_after <= 3600 and calls == ['POST']
        assert 'PRIVATE' not in repr(result)
    finally:
        transport.close()


def test_unmodified_direct_verified_http_transport_is_allowed_without_requests():
    http = httpx.Client(trust_env=False, follow_redirects=False, timeout=5, verify=True)
    transport = api().MaxClient(TOKEN, bot_id=BOT_ID, client=http)
    transport.close()


def test_compressed_response_is_refused_before_decoder_can_expand_its_body():
    class MustNotRead(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail('Compressed body must be refused before reading')
    def handler(request):
        return httpx.Response(200, stream=MustNotRead(), headers={
            'Content-Type': 'application/json', 'Content-Encoding': 'gzip'})
    transport = client(handler)
    try:
        result = transport.send_message(send())
        assert result.state == 'uncertain' and result.error_code == 'max_response_invalid'
    finally:
        transport.close()


def test_every_request_explicitly_requests_uncompressed_json():
    def handler(request):
        assert request.headers['Accept-Encoding'] == 'identity'
        return success(request)
    transport = client(handler)
    try:
        assert transport.send_message(send()).state == 'sent'
    finally:
        transport.close()


def test_nul_in_provider_message_id_returns_uncertain_instead_of_leaking_dto_exception():
    def handler(request):
        result = success(request)
        value = json.loads(result.content)
        value['message']['body']['mid'] = '\x00'
        return httpx.Response(200, json=value)
    transport = client(handler)
    try:
        result = transport.send_message(send())
        assert result.state == 'uncertain' and result.error_code == 'max_response_invalid'
    finally:
        transport.close()


@pytest.mark.parametrize('headers', [
    {'Content-Type': 'text/plain'}, {'Content-Length': '99999'},
    {'Content-Encoding': 'gzip'}, {'Transfer-Encoding': 'chunked'},
    {'X-Secretary-Token': 'PRIVATE_OTHER_APPLICATION_TOKEN'},
])
def test_client_entity_headers_and_foreign_application_headers_are_rejected(headers):
    http = httpx.Client(trust_env=False, follow_redirects=False, timeout=5, headers=headers,
        transport=httpx.MockTransport(lambda request: pytest.fail('No HTTP expected')))
    try:
        with pytest.raises(api().MaxError) as caught: api().MaxClient(TOKEN, bot_id=BOT_ID, client=http)
        assert str(caught.value) == 'max_transport_unsafe'
    finally:
        http.close()


def test_overridden_client_send_cannot_hide_an_automatic_post_retry():
    class RetryingClient(httpx.Client):
        def send(self, request, **kwargs):
            super().send(request, **kwargs).close()
            return super().send(request, **kwargs)
    http = RetryingClient(trust_env=False, follow_redirects=False, timeout=5,
        transport=httpx.MockTransport(success))
    try:
        with pytest.raises(api().MaxError): api().MaxClient(TOKEN, bot_id=BOT_ID, client=http)
    finally:
        http.close()


def test_constructed_subscription_cannot_broaden_supported_event_scope():
    calls = []
    transport = client(lambda request: calls.append(request) or httpx.Response(200))
    value = desired()
    object.__setattr__(value, 'update_types', ('message_created', 'user_added'))
    try:
        with pytest.raises(api().MaxError) as caught: transport.register_own_subscription(value)
        assert caught.value.code == 'max_request_invalid' and calls == []
    finally:
        transport.close()


@pytest.mark.parametrize('timeout', ['PRIVATE_INVALID_TIMEOUT', True, 10 ** 400])
def test_malformed_timeout_configuration_is_code_only_failure(timeout):
    http = httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout,
        transport=httpx.MockTransport(lambda request: pytest.fail('No HTTP expected')))
    try:
        with pytest.raises(api().MaxError) as caught: api().MaxClient(TOKEN, bot_id=BOT_ID, client=http)
        assert caught.value.code == 'max_transport_unsafe' and 'PRIVATE' not in str(caught.value)
    finally:
        http.close()
