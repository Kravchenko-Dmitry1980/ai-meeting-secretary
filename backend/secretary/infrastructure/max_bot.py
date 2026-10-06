"""Pinned MAX outbound boundary: one POST, no implicit retries or redirects.

Sent means API acceptance, not delivery/read. An ambiguous outcome must remain
uncertain in the caller's durable outbox. HTTP 429 retryability is local policy
for the documented rate-limit status; MAX promises no native idempotency key.
Subscription verification proves accepted POST and URL/types readback only;
GET does not expose the secret, and real webhook delivery is a separate gate.
The injected client is owned. Only direct verified HTTPTransport with retries=0
or a trusted offline MockTransport is supported; pinned HTTPX internals provide
the routing/TLS/retry inspection that its public client API does not expose.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import math
import re
import ssl
from uuid import UUID

import httpcore
import httpx

from secretary.domain.bot import BotButton, BotSend, MaxSubscription, SendReceipt, SubscriptionReceipt


BASE_URL = 'https://platform-api2.max.ru'
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 10
_ERROR_CODES = frozenset({
    'max_transport_unsafe', 'max_configuration_invalid', 'max_request_invalid',
    'max_response_invalid', 'max_http_rejected', 'max_transport_uncertain',
    'max_rate_limited', 'max_subscription_uncertain', 'max_subscription_rejected',
})


class MaxError(ValueError):
    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else 'max_response_invalid'
        super().__init__(self.code)


def _id(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', value)
            or int(value) > 2 ** 63 - 1):
        raise MaxError('max_request_invalid')
    return value


def _remote_id(value):
    if type(value) is not int or not 1 <= value <= 2 ** 63 - 1:
        raise MaxError('max_response_invalid')
    return str(value)


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MaxError('max_request_invalid') from None
    return value


def _text(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or '\x00' in value
            or not empty and not value.strip()):
        raise MaxError('max_request_invalid')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise MaxError('max_request_invalid') from None
    return value


def _https_url(value, *, subscription=False, redact=False):
    try:
        _text(value, 2048)
        url = httpx.URL(value)
        if (url.scheme != 'https' or not url.host or url.userinfo or url.fragment
                or any(character.isspace() for character in value)
                or subscription and (url.port is not None or url.query or url.path != '/hooks/max')):
            raise ValueError
        if url.host.lower() == 'localhost' or url.host.endswith('.localhost'):
            raise ValueError
        try:
            address = ipaddress.ip_address(url.host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if redact:
            url = url.copy_with(query=None, fragment=None)
        return str(url)
    except (ValueError, TypeError, httpx.InvalidURL):
        raise MaxError('max_request_invalid') from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class MaxClient:
    def __init__(self, token: str, *, bot_id: str, client: httpx.Client):
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or any(not 33 <= ord(character) <= 126 for character in token)):
            raise MaxError('max_configuration_invalid')
        self.bot_id, self.client, self._token = _id(bot_id), client, token
        self._check_client()

    def close(self):
        self.client.close()

    def read_bot_identity(self) -> dict[str, str | None]:
        """Explicit GET /me identity read; no subscription or delivery proof.

        The provider's required user_id is integer<int64>; username is nullable.
        Only public identity crosses this port, after checking the configured bot.
        """
        status, value, _ = self._request('GET', '/me')
        if status != 200:
            raise MaxError('max_http_rejected' if 400 <= status < 500 else 'max_transport_uncertain')
        identifier = _remote_id(value.get('user_id'))
        if value.get('is_bot') is not True or 'username' not in value:
            raise MaxError('max_response_invalid')
        username = value['username']
        if username is not None:
            if (type(username) is not str or len(username) > 256
                    or any(ord(character) < 32 or ord(character) == 127 for character in username)):
                raise MaxError('max_response_invalid')
            try:
                username.encode('utf-8')
            except UnicodeError:
                raise MaxError('max_response_invalid') from None
        if identifier != self.bot_id:
            raise MaxError('max_configuration_invalid')
        return {'id': identifier, 'username': username}

    def _check_client(self):
        client = self.client
        if type(client) is not httpx.Client:
            raise MaxError('max_transport_unsafe')
        timeout = client.timeout
        mounts = getattr(client, '_mounts', None)
        if (client.is_closed or client.trust_env or client.follow_redirects or client.auth is not None
                or client.params or len(client.cookies) or any(client.event_hooks.values())
                or mounts is None or any(value is not None for value in mounts.values())
                or str(client.base_url) not in ('', BASE_URL, BASE_URL + '/')
                or any(header.lower() not in ('accept', 'accept-encoding', 'connection', 'user-agent')
                       for header in client.headers)
                or any(type(value) not in (int, float) or not 0 < value <= MAX_TIMEOUT_SECONDS
                       or not math.isfinite(value)
                       for value in (timeout.connect, timeout.read, timeout.write, timeout.pool))):
            raise MaxError('max_transport_unsafe')
        transport = getattr(client, '_transport', None)
        if type(transport) is httpx.MockTransport:
            return  # Synthetic fixture contract; never implies live TLS proof.
        if type(transport) is not httpx.HTTPTransport:
            raise MaxError('max_transport_unsafe')
        pool = getattr(transport, '_pool', None)
        context = getattr(pool, '_ssl_context', None)
        if (type(pool) is not httpcore.ConnectionPool or getattr(pool, '_retries', None) != 0
                or not isinstance(context, ssl.SSLContext) or context.verify_mode != ssl.CERT_REQUIRED
                or not context.check_hostname):
            raise MaxError('max_transport_unsafe')

    def _request(self, method, path, *, payload=None, params=None):
        self._check_client()
        try:
            with self.client.stream(method, BASE_URL + path, json=payload, params=params,
                    headers={'Authorization': self._token, 'Accept-Encoding': 'identity'}, follow_redirects=False) as response:
                status = response.status_code
                if status != 200:
                    return status, None, response.headers.get('Retry-After')
                if response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
                    raise MaxError('max_response_invalid')
                if response.headers.get('content-encoding', '').strip().lower() not in ('', 'identity'):
                    # HTTPX decoders may expand one small network chunk before
                    # iter_bytes yields it. Refuse compression before reading,
                    # so the 2MiB limit also bounds the transport's body input.
                    raise MaxError('max_response_invalid')
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > MAX_BODY_BYTES:
                        raise MaxError('max_response_invalid')
                    data.extend(chunk)
                value = json.loads(data, object_pairs_hook=_pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(value, dict):
                    raise MaxError('max_response_invalid')
                return status, value, None
        except MaxError:
            raise
        except (ValueError, UnicodeError, RecursionError):
            raise MaxError('max_response_invalid') from None
        except Exception:
            raise MaxError('max_transport_uncertain') from None

    @staticmethod
    def _retry_after(value):
        if isinstance(value, str) and re.fullmatch(r'[0-9]{1,10}', value):
            return max(1, min(3600, int(value)))
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                return max(1, min(3600, math.ceil((date - datetime.now(timezone.utc)).total_seconds())))
        except (TypeError, ValueError, OverflowError, AttributeError):
            pass
        return 1

    def _button(self, button):
        if not isinstance(button, BotButton):
            raise MaxError('max_request_invalid')
        result = {'type': button.kind, 'text': _text(button.text, 128)}
        if button.kind == 'callback':
            if button.url is not None or button.web_app is not None or button.contact_id is not None:
                raise MaxError('max_request_invalid')
            result['payload'] = _text(button.payload, 1024)
        elif button.kind == 'link':
            if button.payload is not None or button.web_app is not None or button.contact_id is not None:
                raise MaxError('max_request_invalid')
            result['url'] = _https_url(button.url)
        elif button.kind == 'open_app':
            if button.url is not None:
                raise MaxError('max_request_invalid')
            result['web_app'] = _text(button.web_app, 128)
            if not re.fullmatch(r'[A-Za-z0-9_-]+', result['web_app']):
                raise MaxError('max_request_invalid')
            if button.contact_id is not None:
                result['contact_id'] = int(_id(button.contact_id))
            if button.payload is not None:
                if not isinstance(button.payload, str) or not re.fullmatch(r'[A-Za-z0-9_-]{0,512}', button.payload):
                    raise MaxError('max_request_invalid')
                result['payload'] = button.payload
        else:
            raise MaxError('max_request_invalid')
        return result

    def send_message(self, command: BotSend) -> SendReceipt:
        self._check_client()
        if not isinstance(command, BotSend):
            raise MaxError('max_request_invalid')
        operation_id, user_id, text = _uuid(command.operation_id), _id(command.user_id), _text(command.text, 4000)
        if type(command.sensitive) is not bool or type(command.buttons) is not tuple or len(command.buttons) > 30:
            raise MaxError('max_request_invalid')
        payload = {'text': text, 'notify': not command.sensitive}
        rows = []
        for row in command.buttons:
            if type(row) is not tuple or not row or len(row) > 7:
                raise MaxError('max_request_invalid')
            buttons = [self._button(button) for button in row]
            if any(button['type'] in ('link', 'open_app') for button in buttons) and len(buttons) > 3:
                raise MaxError('max_request_invalid')
            rows.append(buttons)
        if rows:
            payload['attachments'] = [{'type': 'inline_keyboard', 'payload': {'buttons': rows}}]
        try:
            status, value, retry = self._request('POST', '/messages', payload=payload,
                params={'user_id': user_id, 'disable_link_preview': 'true'})
            if status == 429:
                return SendReceipt(operation_id, 'retryable', error_code='max_rate_limited', retry_after=self._retry_after(retry))
            if 400 <= status < 500:
                return SendReceipt(operation_id, 'rejected', error_code='max_http_rejected')
            if status != 200:
                return SendReceipt(operation_id, 'uncertain', error_code='max_transport_uncertain')
            message = value.get('message')
            if not isinstance(message, dict):
                raise MaxError('max_response_invalid')
            recipient, body = message.get('recipient'), message.get('body')
            if (not isinstance(recipient, dict) or _remote_id(recipient.get('user_id')) != user_id
                    or recipient.get('chat_type') != 'dialog' or not isinstance(body, dict)
                    or body.get('text') != text):
                raise MaxError('max_response_invalid')
            try:
                message_id = _text(body.get('mid'), 1024)
            except MaxError:
                raise MaxError('max_response_invalid') from None
            if message.get('sender') is not None and (not isinstance(message['sender'], dict)
                    or _remote_id(message['sender'].get('user_id')) != self.bot_id):
                raise MaxError('max_response_invalid')
            if type(message.get('timestamp')) is not int or not 0 <= message['timestamp'] <= 2 ** 63 - 1:
                raise MaxError('max_response_invalid')
            seconds, milliseconds = divmod(message['timestamp'], 1000)
            try:
                accepted_at = (datetime(1970, 1, 1, tzinfo=timezone.utc)
                    + timedelta(seconds=seconds, milliseconds=milliseconds)).isoformat(timespec='milliseconds')
            except (ValueError, OverflowError):
                raise MaxError('max_response_invalid') from None
            return SendReceipt(operation_id, 'sent', message_id=message_id, accepted_at=accepted_at)
        except MaxError as exc:
            if exc.code == 'max_transport_unsafe':
                raise
            return SendReceipt(operation_id, 'uncertain', error_code=exc.code)

    def _read_subscriptions(self, *, redact):
        status, value, _ = self._request('GET', '/subscriptions')
        if status != 200:
            raise MaxError('max_http_rejected' if 400 <= status < 500 else 'max_subscription_uncertain')
        records = value.get('subscriptions')
        if not isinstance(records, list) or len(records) > 1000:
            raise MaxError('max_response_invalid')
        result, urls = [], set()
        for record in records:
            try:
                if not isinstance(record, dict):
                    raise ValueError
                url = _https_url(record.get('url'), redact=redact)
                timestamp, types = record.get('time'), record.get('update_types')
                if (url in urls or type(timestamp) is not int or not 0 <= timestamp <= 2 ** 63 - 1
                        or not isinstance(types, list) or len(types) > 100
                        or any(not isinstance(kind, str) or not re.fullmatch(r'[a-z_]{1,128}', kind) for kind in types)
                        or len(set(types)) != len(types)):
                    raise ValueError
            except (MaxError, ValueError, TypeError):
                raise MaxError('max_response_invalid') from None
            urls.add(url)
            result.append({'url': url, 'time': timestamp, 'update_types': list(types)})
        return tuple(result)

    def read_subscription(self) -> tuple[dict, ...]:
        return self._read_subscriptions(redact=True)

    def register_own_subscription(self, desired: MaxSubscription) -> SubscriptionReceipt:
        self._check_client()
        if not isinstance(desired, MaxSubscription):
            raise MaxError('max_request_invalid')
        url = _https_url(desired.url, subscription=True)
        if (type(desired.update_types) is not tuple or not desired.update_types or len(desired.update_types) > 100
                or any(not isinstance(kind, str) or not re.fullmatch(r'[a-z_]{1,128}', kind) for kind in desired.update_types)
                or not set(desired.update_types) <= {'message_created', 'message_callback', 'bot_started'}
                or len(set(desired.update_types)) != len(desired.update_types)
                or not isinstance(desired.secret, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{5,256}', desired.secret)):
            raise MaxError('max_request_invalid')
        types = tuple(desired.update_types)
        try:
            before = self._read_subscriptions(redact=False)
            foreign_before = {item['url']: item for item in before if item['url'] != url}
        except MaxError as exc:
            return SubscriptionReceipt('uncertain', error_code=exc.code)
        try:
            status, value, _ = self._request('POST', '/subscriptions',
                payload={'url': url, 'update_types': list(types), 'secret': desired.secret})
            if 400 <= status < 500:
                return SubscriptionReceipt('rejected', error_code='max_subscription_rejected')
            if status != 200 or type(value.get('success')) is not bool:
                raise MaxError('max_subscription_uncertain')
            if value['success'] is False:
                return SubscriptionReceipt('rejected', error_code='max_subscription_rejected')
            after = self._read_subscriptions(redact=False)
            own = [item for item in after if item['url'] == url]
            foreign_after = {item['url']: item for item in after if item['url'] != url}
            if (len(own) != 1 or set(own[0]['update_types']) != set(types)
                    or any(foreign_after.get(key) != item for key, item in foreign_before.items())):
                raise MaxError('max_subscription_uncertain')
            return SubscriptionReceipt('verified', verified_at=datetime.now(timezone.utc))
        except MaxError as exc:
            return SubscriptionReceipt('uncertain', error_code=exc.code)
