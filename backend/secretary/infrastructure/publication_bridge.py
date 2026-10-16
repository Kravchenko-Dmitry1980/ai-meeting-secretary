"""Owned synchronous HTTP client for a standalone, literal-loopback gateway.

The host supplies a trusted transport without mutation retries, proxy mounts or
event hooks. The bridge owns and closes this client; it never retries a POST.
The gateway must run with uvicorn proxy_headers=False. Read after restart also
requires the durable publisher to compare its stored envelope/hash to receipt.
"""
from __future__ import annotations

import ipaddress
import json
from uuid import UUID

import httpx
from pydantic import ValidationError

from secretary.application.publication_commands import (
    PublicationCommandError, gateway_payload_hash, publication_task_command,
)
from secretary.domain.task_delivery import GatewayPublication, delivery_uuid
from secretary.domain.team import CommandReceipt, canonical


SERVICE_SECRET_HEADER = 'X-Secretary-Service-Secret'
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_NONTERMINAL = frozenset({'queued', 'running', 'reconciling'})
_ERROR_CODES = frozenset({
    'bridge_timeout', 'bridge_unavailable', 'bridge_http_uncertain', 'bridge_redirect',
    'bridge_receipt_invalid', 'bridge_receipt_mismatch', 'bridge_read_missing',
    'bridge_response_too_large', 'bridge_actor_mismatch', 'bridge_payload_invalid',
})


class BridgeUncertain(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else 'bridge_http_uncertain'
        super().__init__(self.code)


def _uuid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('bridge_operation_requires_uuid_string')
    try:
        normalized = str(UUID(value))
    except ValueError:
        raise ValueError('bridge_operation_requires_uuid_string') from None
    if value != normalized:
        raise ValueError('bridge_operation_requires_canonical_uuid')
    return value


def _base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('bridge_requires_literal_http_loopback')
    try:
        url = httpx.URL(value)
        address = ipaddress.ip_address(url.host)
        valid = (url.scheme == 'http' and address.is_loopback and url.port is not None
                 and not url.userinfo and url.path in ('', '/') and not url.query and not url.fragment)
    except (ValueError, httpx.InvalidURL):
        valid = False
    if not valid:
        raise ValueError('bridge_requires_literal_http_loopback')
    return str(url).rstrip('/')


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = item
    return value


class LocalPublicationBridge:
    def __init__(self, client: httpx.Client, *, base_url: str, secret: str, actor_id: str):
        self.base_url = _base_url(base_url)
        self.actor_id = _uuid(actor_id)
        if not isinstance(secret, str) or not secret or not secret.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in secret):
            raise ValueError('bridge_secret_invalid')
        if (not isinstance(client, httpx.Client) or client.is_closed or client.trust_env
                or client.follow_redirects or client.auth is not None or client.params
                or len(client.cookies) or any(client.event_hooks.values())):
            raise ValueError('bridge_client_unsafe')
        # httpx exposes no public routing/mount inventory. This narrow check of
        # its pinned client implementation rejects explicit proxy= and mounts=;
        # the primary injected transport remains the host's trusted contract.
        mounts = getattr(client, '_mounts', None)
        if mounts is None or any(transport is not None for transport in mounts.values()):
            raise ValueError('bridge_client_routing_unsafe')
        if str(client.base_url) and _base_url(str(client.base_url)) != self.base_url:
            raise ValueError('bridge_client_base_url_mismatch')
        if any(header in client.headers for header in ('host', 'authorization', 'cookie', SERVICE_SECRET_HEADER)):
            raise ValueError('bridge_client_headers_unsafe')
        self.client = client
        self._secret = secret
        self._expected = {}

    def close(self):
        self.client.close()

    def submit(self, envelope: GatewayPublication) -> CommandReceipt:
        try:
            envelope = GatewayPublication.model_validate_json(canonical(envelope.model_dump(mode='json')))
            command = publication_task_command(envelope)
            expected_hash = gateway_payload_hash(envelope)
        except (PublicationCommandError, ValidationError, TypeError, ValueError, AttributeError):
            raise BridgeUncertain('bridge_payload_invalid') from None
        if envelope.actor_id != self.actor_id:
            raise BridgeUncertain('bridge_actor_mismatch')
        operation_id = delivery_uuid(envelope.scope, envelope.item)
        expected = (expected_hash, envelope.scope.destination_project_id, command.task_id)
        old = self._expected.get(operation_id)
        if old is not None and old != expected:
            raise BridgeUncertain('bridge_receipt_mismatch')
        # Remember identity before sending: a lost ACK does not permit another
        # POST and subsequent GET must still match the original payload.
        self._expected[operation_id] = expected
        return self._request('POST', '/internal/v1/task-publications', operation_id,
                             body=envelope.model_dump(mode='json'))

    def read(self, operation_id: str) -> CommandReceipt:
        operation_id = _uuid(operation_id)
        return self._request('GET', '/internal/v1/task-publications/' + operation_id, operation_id)

    def _request(self, method, path, operation_id, *, body=None):
        try:
            with self.client.stream(method, self.base_url + path, json=body,
                    headers={SERVICE_SECRET_HEADER: self._secret}, follow_redirects=False) as response:
                if method == 'GET' and response.status_code == 404:
                    raise BridgeUncertain('bridge_read_missing')
                if 300 <= response.status_code < 400:
                    raise BridgeUncertain('bridge_redirect')
                if response.status_code not in (200, 202):
                    raise BridgeUncertain('bridge_http_uncertain')
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > MAX_RECEIPT_BYTES:
                        raise BridgeUncertain('bridge_response_too_large')
                    data.extend(chunk)
                try:
                    payload = json.loads(data, object_pairs_hook=_unique_json,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite_json')))
                    receipt = CommandReceipt.model_validate(payload)
                except (ValidationError, ValueError, TypeError, RecursionError):
                    raise BridgeUncertain('bridge_receipt_invalid') from None
                self._validate_receipt(receipt, operation_id, response.status_code)
                return receipt
        except BridgeUncertain:
            raise
        except httpx.TimeoutException:
            raise BridgeUncertain('bridge_timeout') from None
        except Exception:
            # A custom transport can fail outside HTTPX's exception hierarchy.
            # Its text is never a public error, and delivery is still uncertain.
            raise BridgeUncertain('bridge_unavailable') from None

    def _validate_receipt(self, receipt, operation_id, status):
        acceptance, execution, current = receipt.acceptance_receipt, receipt.execution_state, receipt.current
        expected = self._expected.get(operation_id)
        if acceptance.operation_id != operation_id:
            raise BridgeUncertain('bridge_receipt_mismatch')
        if (execution.state in _NONTERMINAL) != (status == 202):
            raise BridgeUncertain('bridge_receipt_invalid')
        if acceptance.decision != 'accepted' and execution.state not in ('rejected', 'conflict'):
            raise BridgeUncertain('bridge_receipt_invalid')
        if expected:
            digest, project_id, target_id = expected
            if (acceptance.payload_hash != digest or current and current.project_id != project_id
                    or target_id is not None and current and current.task_id != target_id
                    or target_id is not None and execution.task_id is not None and execution.task_id != target_id):
                raise BridgeUncertain('bridge_receipt_mismatch')
        if execution.state == 'applied':
            if (acceptance.decision != 'accepted' or execution.verified_at is None
                    or execution.task_id is None or current is None or current.task_id != execution.task_id):
                raise BridgeUncertain('bridge_receipt_invalid')
        elif execution.verified_at is not None:
            raise BridgeUncertain('bridge_receipt_invalid')
