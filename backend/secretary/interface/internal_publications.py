"""Standalone internal Team gateway, never mounted in Secretary's public app.

Host integration MUST use uvicorn proxy_headers=False: this boundary checks
the actual ASGI peer, and cannot undo peer rewriting by upstream middleware.
No worker is started here. POST acknowledges acceptance; only worker verified
readback can later produce an applied receipt through GET.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from secretary.application.publication_commands import PublicationCommandError, publication_task_command
from secretary.domain.task_delivery import GatewayPublication
from secretary.domain.team import TeamConflict, TeamForbidden


SERVICE_SECRET_HEADER = b'x-secretary-service-secret'
MAX_BODY_BYTES = 256 * 1024
_NONTERMINAL = frozenset({'queued', 'running', 'reconciling'})


def _error(status, code):
    return JSONResponse({'error_code': code}, status_code=status)


def _host_loopback(raw):
    try:
        value = raw.decode('ascii')
        parsed = urlsplit('http://' + value)
        if (not value or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment or parsed.hostname is None):
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        return parsed.hostname.lower() == 'localhost' or ipaddress.ip_address(parsed.hostname).is_loopback
    except (UnicodeError, ValueError):
        return False


class _LoopbackBoundary:
    def __init__(self, app, *, secret):
        self.app, self.secret = app, secret.encode('ascii')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        client = scope.get('client')
        try:
            peer_allowed = bool(client and ipaddress.ip_address(client[0]).is_loopback)
        except (ValueError, TypeError):
            peer_allowed = False
        headers = scope.get('headers', ())
        hosts = [value for key, value in headers if key.lower() == b'host']
        secrets = [value for key, value in headers if key.lower() == SERVICE_SECRET_HEADER]
        if (not peer_allowed or len(hosts) != 1 or not _host_loopback(hosts[0])
                or len(secrets) != 1 or not hmac.compare_digest(secrets[0], self.secret)):
            return await _error(403, 'publication_gateway_forbidden')(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            if message['type'] != 'http.request':
                return await _error(400, 'publication_request_invalid')(scope, receive, send)
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                return await _error(413, 'publication_payload_too_large')(scope, receive, send)
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        captured = bytes(body)
        delivered = False
        async def captured_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': captured, 'more_body': False}
            return await receive()
        return await self.app(scope, captured_receive, send)


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = item
    return value


def create_publication_gateway(repository, secret: str, allowed_actor_id: str) -> FastAPI:
    if not isinstance(secret, str) or not secret or not secret.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in secret):
        raise ValueError('publication_gateway_secret_invalid')
    try:
        if not isinstance(allowed_actor_id, str) or str(UUID(allowed_actor_id)) != allowed_actor_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('publication_gateway_actor_invalid') from None
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_LoopbackBoundary, secret=secret)

    def owner(project_id=None):
        member = repository.get_member(allowed_actor_id)
        if (member is None or not member.enabled or member.role != 'owner'
                or project_id is not None and project_id not in member.project_ids):
            raise TeamForbidden('owner_required')

    def response(receipt):
        return JSONResponse(receipt.model_dump(mode='json'),
                            status_code=202 if receipt.execution_state.state in _NONTERMINAL else 200)

    @app.post('/internal/v1/task-publications')
    async def submit(request: Request):
        try:
            owner()
        except TeamForbidden:
            return _error(403, 'publication_gateway_forbidden')
        except Exception:
            return _error(503, 'publication_gateway_unavailable')
        try:
            payload = json.loads(await request.body(), object_pairs_hook=_unique_json,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite_json')))
            envelope = GatewayPublication.model_validate(payload)
            if envelope.actor_id != allowed_actor_id:
                return _error(403, 'publication_gateway_forbidden')
            owner(envelope.scope.destination_project_id)
            command = publication_task_command(envelope)
            receipt = repository.accept_command(allowed_actor_id, command)
            return response(receipt)
        except TeamForbidden:
            return _error(403, 'publication_gateway_forbidden')
        except TeamConflict:
            return _error(409, 'publication_command_conflict')
        except (ValidationError, PublicationCommandError, ValueError, TypeError, RecursionError):
            return _error(422, 'publication_payload_invalid')
        except Exception:
            return _error(503, 'publication_gateway_unavailable')

    @app.get('/internal/v1/task-publications/{operation_id}')
    def read(operation_id: str):
        try:
            owner()
            if str(UUID(operation_id)) != operation_id:
                return _error(422, 'publication_operation_invalid')
            receipt = repository.get_receipt(allowed_actor_id, operation_id)
            return response(receipt)
        except TeamForbidden as exc:
            return _error(404 if str(exc) == 'command_unavailable' else 403,
                          'publication_receipt_unavailable')
        except (ValueError, TypeError):
            return _error(422, 'publication_operation_invalid')
        except Exception:
            return _error(503, 'publication_gateway_unavailable')

    return app
