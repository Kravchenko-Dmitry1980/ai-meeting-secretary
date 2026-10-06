"""Independent public task API. No Secretary runtime, secrets or capture imports.

The HTTPS reverse proxy preserves the configured Host. ASGI forwarding headers
are deliberately not trusted; the launcher binds loopback with proxy_headers=False.
No provider or worker is started by this factory.
"""
from __future__ import annotations

import ipaddress
import asyncio
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyCookie, APIKeyHeader
from starlette.concurrency import run_in_threadpool
from pydantic import Field, SecretStr, StrictStr, ValidationError, field_validator

from secretary.domain.team import (Bucket, CommandReceipt, DecimalID, Revision, TaskCommand,
    TaskSnapshot, TeamConflict, TeamDTO, TeamForbidden, UUIDString)
from secretary.domain.team_auth import AuthError
from secretary.domain.bot import BotError
from secretary.domain.team_reads import HistoryCursor, MemberPage, TaskHistoryPage, TeamStatusView
from secretary.domain.team_dashboard import DashboardCursor, OwnerDashboardView
from secretary.domain.team_due_resolution import (
    DueResolutionCandidate, DueResolutionConfirmation, DueResolutionPreview, DueResolutionRequest,
)
from secretary.interface.team_auth import validate_init_data


COOKIE = '__Host-secretary-team'
BODY_LIMIT = 256 * 1024
BODY_TIMEOUT_SECONDS = 10
_PUBLIC_PATH = re.compile(r'^/api/team/v1/(?:session/(?:max|code)|session|me|members|status|dashboard|tasks(?:/[1-9][0-9]{0,127}(?:/(?:history|due-resolution(?:/previews)?))?)?|due-resolutions/[0-9a-fA-F-]{36}/confirm|commands(?:/[0-9a-fA-F-]{36})?)$')
_SESSION_COOKIE = APIKeyCookie(name=COOKIE, auto_error=False)
_WEBHOOK_HEADER = APIKeyHeader(name='X-Max-Bot-Api-Secret', auto_error=False)


class TeamGatewaySettings(TeamDTO):
    public_origin: StrictStr
    bot_id: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    bot_token: SecretStr
    webhook_secret: SecretStr | None = None

    @field_validator('webhook_secret')
    @classmethod
    def secret_valid(cls, value):
        if value is not None and not re.fullmatch(r'[A-Za-z0-9_-]{5,256}', value.get_secret_value()):
            raise ValueError('max_webhook_secret_invalid')
        return value

    @field_validator('public_origin')
    @classmethod
    def https_origin(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or value != 'https://' + parsed.netloc or not value.isascii()
                or '%' in parsed.netloc or '\\' in parsed.netloc or parsed.netloc.endswith(':')
                or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
            raise ValueError('team_https_origin_required')
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError('team_origin_port_invalid')
        # Browsers lowercase DNS hosts and omit the default HTTPS port in Origin
        # and Host. Preserve exact matching against that canonical form.
        host = parsed.hostname.lower()
        authority = '[' + host + ']' if ':' in host else host
        if parsed.port is not None and parsed.port != 443:
            authority += ':' + str(parsed.port)
        return 'https://' + authority

    @field_validator('bot_token')
    @classmethod
    def token_required(cls, value):
        if not value.get_secret_value() or len(value.get_secret_value()) > 4096:
            raise ValueError('max_bot_token_required')
        return value

    @field_validator('bot_id')
    @classmethod
    def bot_identity_required(cls, value):
        if not re.fullmatch(r'[1-9][0-9]{0,18}', value) or int(value) > 2**63-1:
            raise ValueError('max_bot_identity_required')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ValueError('max_bot_identity_required') from None
        return value


@dataclass(frozen=True)
class TeamGatewayClients:
    auth: object
    bot_intake: object | None = None
    reads: object | None = None
    dashboard: object | None = None
    due_resolution: object | None = None


class Actor(TeamDTO):
    id: UUIDString
    display_name: str
    role: Literal['owner', 'member']
    project_ids: tuple[DecimalID, ...]
    revision: Revision


class SessionView(TeamDTO):
    actor: Actor
    csrf: str


class MaxLogin(TeamDTO):
    init_data: Annotated[str, Field(strict=True, min_length=1, max_length=16384)]


class CodeLogin(TeamDTO):
    value: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]


class TaskPage(TeamDTO):
    items: tuple[TaskSnapshot, ...]
    next_cursor: DecimalID | None = None


class TeamAPIError(TeamDTO):
    error_code: str


def _error(status, code):
    return JSONResponse({'error_code': code}, status_code=status)


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = item
    return value


class _PublicBoundary:
    def __init__(self, app, *, origin, webhook_secret=None, auth=None):
        self.app, self.origin = app, origin.encode('ascii')
        self.auth = auth
        self.host = urlsplit(origin).netloc.encode('ascii').lower()
        self.webhook_secret = webhook_secret.encode('ascii') if webhook_secret is not None else None

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            # There is no public websocket or any delegated native app surface.
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 1008})
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                headers = [(key, value) for key, value in message.get('headers', ())
                           if key.lower() not in {b'cache-control', b'x-content-type-options', b'referrer-policy'}]
                headers.extend(((b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                                (b'referrer-policy', b'no-referrer')))
                message = {**message, 'headers': headers}
            await send(message)

        path = scope['path']
        hook = path == '/hooks/max'
        allowed_method = ('POST' if hook or path in {'/api/team/v1/session/max', '/api/team/v1/session/code',
            '/api/team/v1/commands'} or path.endswith('/due-resolution/previews')
            or path.startswith('/api/team/v1/due-resolutions/') and path.endswith('/confirm')
            else 'DELETE' if path == '/api/team/v1/session' else 'GET')
        if (not path.isascii() or scope.get('raw_path',path.encode('ascii')) != path.encode('ascii')
                or scope['method'] != allowed_method or (not hook and not _PUBLIC_PATH.fullmatch(path))):
            return await _error(404, 'team_route_unavailable')(scope, receive, secure_send)
        headers = scope.get('headers', ())
        hosts = [v for k, v in headers if k.lower() == b'host']
        if len(hosts) != 1 or hosts[0].lower() != self.host:
            return await _error(403, 'team_host_forbidden')(scope, receive, secure_send)
        limit = BODY_LIMIT
        if hook:
            if scope['method'] != 'POST' or scope.get('raw_path', b'/hooks/max') != b'/hooks/max':
                return await _error(404, 'team_route_unavailable')(scope, receive, secure_send)
            if self.webhook_secret is None:
                return await _error(503, 'max_webhook_unavailable')(scope, receive, secure_send)
            secrets = [v for k, v in headers if k.lower() == b'x-max-bot-api-secret']
            if len(secrets) != 1 or not hmac.compare_digest(secrets[0], self.webhook_secret):
                return await _error(401, 'max_webhook_authentication_required')(scope, receive, secure_send)
            limit = 1024 * 1024
        origins = [v for k, v in headers if k.lower() == b'origin']
        mutation = scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}
        if not hook and (origins and (len(origins) != 1 or origins[0] != self.origin)
                or mutation and origins != [self.origin]):
            return await _error(403, 'team_origin_forbidden')(scope, receive, secure_send)
        try:
            peer = ipaddress.ip_address(scope['client'][0])
            effective_peer = str(peer)
            forwarded = [v for k,v in headers if k.lower() == b'x-team-client-ip']
            if peer.is_loopback and forwarded:
                if len(forwarded) != 1 or b'%' in forwarded[0]:
                    raise ValueError('invalid_proxy_identity')
                effective_peer = str(ipaddress.ip_address(forwarded[0].decode('ascii')))
        except (KeyError,IndexError,TypeError,UnicodeError,ValueError):
            return await _error(400, 'team_proxy_identity_invalid')(scope, receive, secure_send)
        scope.setdefault('state',{})['team_client_ip'] = effective_peer
        limiter = getattr(self.auth,'check_rate',None)
        if not callable(limiter):
            return await _error(503, 'team_ingress_unavailable')(scope, receive, secure_send)
        # The actual socket peer is unchanged. A single proxy-owned identity is
        # used only for bounded counters; Forwarded/XFF never grant authority.
        group, global_limit, peer_limit = ('ingress_hook',600,120) if hook else ('ingress_api',1200,240)
        try:
            await run_in_threadpool(limiter,group,'all',limit=global_limit,window_seconds=60)
            await run_in_threadpool(limiter,group,effective_peer,limit=peer_limit,window_seconds=60)
            if path in {'/api/team/v1/session/max','/api/team/v1/session/code'}:
                endpoint = 'session_max' if path.endswith('/max') else 'session_code'
                await run_in_threadpool(limiter,endpoint,'all',limit=120,window_seconds=60)
                await run_in_threadpool(limiter,endpoint,effective_peer,limit=20,window_seconds=60)
        except AuthError as exc:
            limited = 'team_login_rate_limited' if path in {'/api/team/v1/session/max','/api/team/v1/session/code'} else 'team_ingress_rate_limited'
            status, code = (429,limited) if exc.code == 'auth_rate_limited' else (503,'team_ingress_unavailable')
            return await _error(status,code)(scope,receive,secure_send)
        except (sqlite3.Error,TypeError,AttributeError):
            return await _error(503,'team_ingress_unavailable')(scope,receive,secure_send)
        lengths = [v for k, v in headers if k.lower() == b'content-length']
        try:
            if len(lengths) > 1 or lengths and (not re.fullmatch(rb'[0-9]+', lengths[0]) or int(lengths[0]) > limit):
                return await _error(413, 'team_payload_too_large')(scope, receive, secure_send)
        except ValueError:
            return await _error(400, 'team_request_invalid')(scope, receive, secure_send)
        body = bytearray()
        try:
            async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    if message['type'] != 'http.request':
                        return await _error(400, 'team_request_invalid')(scope, receive, secure_send)
                    chunk = message.get('body', b'')
                    if len(body) + len(chunk) > limit:
                        return await _error(413, 'team_payload_too_large')(scope, receive, secure_send)
                    body.extend(chunk)
                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            return await _error(408,'team_request_timeout')(scope,receive,secure_send)
        if hook and not body:
            return await _error(400, 'team_json_invalid')(scope, receive, secure_send)
        if body:
            try:
                json.loads(body, object_pairs_hook=_unique_json,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_number')))
            except (ValueError, UnicodeError, RecursionError):
                return await _error(400, 'team_json_invalid')(scope, receive, secure_send)
        captured, delivered = bytes(body), False

        async def captured_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': captured, 'more_body': False}
            return await receive()

        return await self.app(scope, captured_receive, secure_send)


def _cookie(request):
    values = [v for k, v in request.scope.get('headers', ()) if k.lower() == b'cookie']
    if len(values) != 1:
        raise AuthError('auth_invalid_credentials')
    try:
        pairs = [pair.strip().partition('=') for pair in values[0].decode('ascii').split(';')]
        found = [value for key, separator, value in pairs if key == COOKIE and separator]
    except UnicodeError:
        raise AuthError('auth_invalid_credentials') from None
    if len(found) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', found[0]):
        raise AuthError('auth_invalid_credentials')
    return found[0]


def require_actor(request: Request, _schema_cookie: Annotated[str | None, Security(_SESSION_COOKIE)] = None) -> Actor:
    token = _cookie(request)
    mutation = request.method not in {'GET', 'HEAD', 'OPTIONS'}
    csrf_values = [v for k, v in request.scope.get('headers', ()) if k.lower() == b'x-csrf-token']
    csrf = None
    if mutation:
        if len(csrf_values) != 1:
            raise AuthError('auth_csrf_invalid')
        try:
            csrf = csrf_values[0].decode('ascii')
        except UnicodeError:
            raise AuthError('auth_csrf_invalid') from None
    member = request.app.state.team_auth.authenticate(token, csrf=csrf, require_csrf=mutation)
    request.state.team_token = token
    return Actor(**{key: getattr(member, key) for key in Actor.model_fields})


def _session_view(member, csrf):
    return SessionView(actor=Actor(**{key: getattr(member, key) for key in Actor.model_fields}), csrf=csrf)


def create_team_app(settings: TeamGatewaySettings, repository, clients: TeamGatewayClients) -> FastAPI:
    settings = TeamGatewaySettings.model_validate(settings.model_dump())
    app = FastAPI(title='Secretary Team Gateway', version='1.0.0', docs_url=None,
                  redoc_url=None, openapi_url=None, responses={status: {'model': TeamAPIError}
                  for status in (400, 401, 403, 408, 409, 413, 422, 429, 503)})
    app.state.team_auth = clients.auth
    # Constructor only binds ports. No DB read, runtime startup or provider call.
    # Lazy default permits schema export with an inert repository object.
    def read_repository():
        if clients.reads is not None:
            return clients.reads
        from secretary.infrastructure.team_read_repository import TeamReadRepository
        return TeamReadRepository(repository)
    def dashboard_repository():
        if clients.dashboard is not None:
            return clients.dashboard
        from secretary.infrastructure.team_dashboard_repository import TeamDashboardRepository
        return TeamDashboardRepository(repository, reads=read_repository())
    app.add_middleware(_PublicBoundary, origin=settings.public_origin, auth=clients.auth,
        webhook_secret=settings.webhook_secret.get_secret_value() if settings.webhook_secret else None)

    @app.post('/hooks/max', operation_id='receiveMaxWebhook')
    async def receive_max_webhook(request: Request, _header: Annotated[str | None, Security(_WEBHOOK_HEADER)]):
        # Middleware authenticated before reading/parsing. No handler, media,
        # Polza or remote send work happens on this acknowledgment path.
        if clients.bot_intake is None:
            return _error(503, 'max_webhook_unavailable')
        from secretary.interface.max_webhook import normalize_max_event
        event = normalize_max_event(json.loads(await request.body()), bot_id=settings.bot_id)
        await run_in_threadpool(clients.bot_intake.intake, event)
        return {'ok': True}

    @app.exception_handler(BotError)
    async def bot_error(request, exc):
        return _error(503, 'max_webhook_storage_unavailable')

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        if exc.code in {'auth_unavailable', 'auth_dto_invalid', 'max_auth_configuration_invalid'}:
            return _error(503, 'team_auth_unavailable')
        if exc.code == 'auth_csrf_invalid':
            return _error(403, 'team_csrf_forbidden')
        if exc.code == 'auth_rate_limited':
            return _error(429, 'team_login_rate_limited')
        # Invalid signature, unknown/disabled mapping, replay and code failures do
        # not reveal which account, challenge or invitation exists.
        return _error(401, 'team_authentication_required')

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        return _error(503, 'team_storage_unavailable')

    @app.exception_handler(ValidationError)
    async def stored_contract_error(request, exc):
        # RequestValidationError has its separate 422 handler. Corrupt trusted
        # storage must not echo Pydantic's input in a traceback or HTTP response.
        return _error(503, 'team_storage_unavailable')

    @app.exception_handler(TeamForbidden)
    async def forbidden(request, exc):
        return _error(403, 'team_scope_forbidden')

    @app.exception_handler(TeamConflict)
    async def conflict(request, exc):
        return _error(409, 'team_command_conflict')

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        # FastAPI's default includes raw input, which could contain a login secret.
        return _error(422, 'team_request_invalid')

    def grant_response(grant):
        response = JSONResponse(_session_view(grant.member, grant.csrf).model_dump(mode='json'))
        response.set_cookie(COOKIE, grant.token, secure=True, httponly=True, samesite='lax',
                            path='/', expires=grant.expires_at)
        return response

    @app.post('/api/team/v1/session/max', response_model=SessionView)
    def max_session(request: Request, body: MaxLogin):
        identity = validate_init_data(body.init_data, bot_token=settings.bot_token.get_secret_value(),
                                      bot_id=settings.bot_id, now=clients.auth.clock())
        return grant_response(clients.auth.login_max(identity))

    @app.post('/api/team/v1/session/code', response_model=SessionView)
    def code_session(request: Request, body: CodeLogin):
        return grant_response(clients.auth.consume_code(body.value))

    @app.delete('/api/team/v1/session', status_code=204)
    def logout(request: Request, actor: Annotated[Actor, Depends(require_actor)]):
        clients.auth.logout(request.state.team_token, request.headers['x-csrf-token'])
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    @app.get('/api/team/v1/me', response_model=SessionView)
    def me(request: Request, actor: Annotated[Actor, Depends(require_actor)]):
        return SessionView(actor=actor, csrf=clients.auth.csrf_token(request.state.team_token))

    @app.get('/api/team/v1/tasks', response_model=TaskPage)
    def tasks(actor: Annotated[Actor, Depends(require_actor)], project_id: DecimalID,
              limit: Annotated[int, Query(ge=1, le=100)] = 50, after: DecimalID | None = None,
              mine: bool = False, bucket: Bucket | None = None):
        items = repository.list_projections(actor.id, project_id, limit=limit, after=after,
            mine=mine, bucket=bucket, expected_actor_revision=actor.revision)
        return TaskPage(items=items[:limit], next_cursor=items[limit - 1].task_id if len(items) > limit else None)

    @app.get('/api/team/v1/members', response_model=MemberPage)
    def members(actor: Annotated[Actor, Depends(require_actor)], project_id: DecimalID,
                limit: Annotated[int, Query(ge=1, le=100)] = 50, after: UUIDString | None = None):
        return read_repository().members(actor.id, project_id, limit=limit, after=after,
                                         expected_actor_revision=actor.revision)

    @app.get('/api/team/v1/tasks/{task_id}/history', response_model=TaskHistoryPage)
    def history(task_id: DecimalID, actor: Annotated[Actor, Depends(require_actor)],
                limit: Annotated[int, Query(ge=1, le=100)] = 50, after: HistoryCursor | None = None):
        try:
            return read_repository().history(actor.id, task_id, limit=limit, after=after,
                                             expected_actor_revision=actor.revision)
        except (TeamForbidden, TeamConflict, ValidationError):
            raise
        except ValueError:
            return _error(422, 'team_request_invalid')

    @app.get('/api/team/v1/status', response_model=TeamStatusView)
    def team_status(actor: Annotated[Actor, Depends(require_actor)], project_id: DecimalID):
        return read_repository().status(actor.id, project_id, expected_actor_revision=actor.revision)

    @app.get('/api/team/v1/dashboard', response_model=OwnerDashboardView)
    def dashboard(actor: Annotated[Actor, Depends(require_actor)], project_id: DecimalID,
                  limit: Annotated[int, Query(ge=1, le=100)] = 50, after: DashboardCursor | None = None):
        try:
            return dashboard_repository().dashboard(actor.id, project_id, limit=limit, after=after,
                                                    expected_actor_revision=actor.revision)
        except (TeamForbidden, TeamConflict, ValidationError):
            raise
        except ValueError:
            return _error(422, 'team_request_invalid')

    @app.get('/api/team/v1/tasks/{task_id}', response_model=TaskSnapshot)
    def task(task_id: DecimalID, actor: Annotated[Actor, Depends(require_actor)]):
        return repository.get_projection(actor.id, task_id, expected_actor_revision=actor.revision)

    @app.get('/api/team/v1/tasks/{task_id}/due-resolution', response_model=DueResolutionCandidate)
    def due_candidate(task_id: DecimalID, actor: Annotated[Actor, Depends(require_actor)]):
        if actor.role != 'owner':
            raise TeamForbidden('owner_required')
        if clients.due_resolution is None:
            return _error(503, 'team_due_resolution_unavailable')
        from secretary.infrastructure.vikunja import VikunjaError
        try:
            return clients.due_resolution.candidate(actor.id, task_id, expected_actor_revision=actor.revision)
        except VikunjaError:
            return _error(503, 'team_due_resolution_remote_unavailable')

    @app.post('/api/team/v1/tasks/{task_id}/due-resolution/previews', response_model=DueResolutionPreview,
              status_code=201)
    def due_preview(task_id: DecimalID, body: DueResolutionRequest, actor: Annotated[Actor, Depends(require_actor)]):
        if actor.role != 'owner':
            raise TeamForbidden('owner_required')
        if clients.due_resolution is None:
            return _error(503, 'team_due_resolution_unavailable')
        from secretary.infrastructure.vikunja import VikunjaError
        try:
            return clients.due_resolution.preview(actor.id, task_id, body, expected_actor_revision=actor.revision)
        except VikunjaError:
            return _error(503, 'team_due_resolution_remote_unavailable')

    @app.post('/api/team/v1/due-resolutions/{preview_id}/confirm', response_model=CommandReceipt, status_code=202)
    def due_confirm(preview_id: UUIDString, body: DueResolutionConfirmation,
                    actor: Annotated[Actor, Depends(require_actor)]):
        if actor.role != 'owner':
            raise TeamForbidden('owner_required')
        if clients.due_resolution is None:
            return _error(503, 'team_due_resolution_unavailable')
        from secretary.infrastructure.vikunja import VikunjaError
        try:
            return clients.due_resolution.confirm(actor.id, preview_id, body, expected_actor_revision=actor.revision)
        except VikunjaError:
            return _error(503, 'team_due_resolution_remote_unavailable')

    @app.post('/api/team/v1/commands', response_model=CommandReceipt, status_code=202)
    def commands(request: Request, body: TaskCommand, actor: Annotated[Actor, Depends(require_actor)]):
        if body.action == 'resolve_due':
            return _error(422, 'team_due_resolution_confirmation_required')
        if body.origin is not None or body.action == 'link':
            return _error(422, 'team_trusted_origin_required')
        if body.action in {'create', 'assign'} and body.expected_assignee_revision is None:
            return _error(422, 'team_assignee_revision_required')
        return repository.accept_command(actor.id, body, expected_actor_revision=actor.revision)

    @app.get('/api/team/v1/commands/{operation_id}', response_model=CommandReceipt)
    def receipt(operation_id: UUIDString, actor: Annotated[Actor, Depends(require_actor)]):
        return repository.get_receipt(actor.id, operation_id, expected_actor_revision=actor.revision)

    return app


def gateway_server_config(app, *, host='127.0.0.1', port=8766):
    """The T12 supervisor must use this explicit safe native launch contract."""
    import uvicorn
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError('gateway_bind_must_be_loopback')
    except (ValueError, TypeError):
        raise ValueError('gateway_bind_must_be_loopback') from None
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('gateway_port_invalid')
    return uvicorn.Config(app, host=host, port=port, proxy_headers=False, access_log=False)
