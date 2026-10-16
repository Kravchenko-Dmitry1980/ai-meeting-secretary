"""Owner-local team setup, mounted behind Secretary Host/Origin/CSRF middleware.

The HTTP server must disable proxy-header peer rewriting. This router checks the
ASGI peer and literal Host before parsing input; forwarding headers grant nothing.
Only an explicitly supplied repository and trusted actor enable these routes.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field, field_validator

from secretary.domain.team import DecimalID, Revision, TeamConflict, TeamDTO, TeamForbidden, TeamMember, uuid_string
from secretary.domain.team_auth import AuthError, InvitationGrant, InvitationView


class CreateInvitation(TeamDTO):
    project_ids: tuple[DecimalID, ...] = Field(min_length=1, max_length=100)

    @field_validator('project_ids')
    @classmethod
    def unique_projects(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('duplicate_project')
        return value


class ConfirmInvitation(TeamDTO):
    member: TeamMember


class RevokeMember(TeamDTO):
    expected_revision: Revision


class LocalTeamError(TeamDTO):
    detail: str


_AUTH_STATUS = {
    'auth_forbidden': 403,
    'auth_invalid_credentials': 403,
    'auth_csrf_invalid': 403,
    'auth_conflict': 409,
    'auth_replay': 409,
    'auth_rate_limited': 429,
    'auth_unavailable': 503,
    'auth_dto_invalid': 422,
}


def _error(status, code):
    return JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store'})


def _loopback_peer_and_host(request):
    client = request.scope.get('client')
    try:
        if not client or '%' in client[0] or not ipaddress.ip_address(client[0]).is_loopback:
            return False
        hosts = [value for key, value in request.scope.get('headers', ()) if key.lower() == b'host']
        if len(hosts) != 1:
            return False
        value = hosts[0].decode('ascii')
        if not value or '%' in value or any(ord(char) <= 32 for char in value) or value.endswith(':'):
            return False
        parsed = urlsplit('http://' + value)
        if (parsed.username is not None or parsed.password is not None or parsed.path
                or parsed.query or parsed.fragment or parsed.hostname is None):
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError, UnicodeError):
        return False


def create_local_team_router(repository, *, actor_id: str) -> APIRouter:
    """Create five local administration operations; repository checks fresh owner ACL."""
    actor = uuid_string(actor_id) if repository is not None else None

    class OwnerLocalRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def guarded(request: Request):
                if not _loopback_peer_and_host(request):
                    return _error(403, 'local_team_loopback_required')
                if repository is None:
                    return _error(503, 'local_team_not_configured')
                try:
                    response = await original(request)
                except RequestValidationError:
                    return _error(422, 'local_team_request_invalid')
                except TeamForbidden:
                    return _error(403, 'local_team_forbidden')
                except TeamConflict:
                    return _error(409, 'local_team_conflict')
                except AuthError as exc:
                    status = _AUTH_STATUS.get(exc.code)
                    return _error(status, exc.code) if status else _error(503, 'local_team_unavailable')
                except Exception:
                    return _error(503, 'local_team_unavailable')
                response.headers['Cache-Control'] = 'no-store'
                return response

            return guarded

    errors = {status: {'model': LocalTeamError} for status in (403, 404, 409, 422, 429, 503)}
    router = APIRouter(prefix='/api/v1/team', route_class=OwnerLocalRoute, responses=errors)

    @router.post('/invitations', status_code=201, response_model=InvitationGrant)
    def create(body: CreateInvitation):
        return repository.create_invitation(actor, body.project_ids)

    @router.get('/invitations/{invitation_id}', response_model=InvitationView)
    def read(invitation_id: UUID):
        return repository.read_invitation(actor, str(invitation_id))

    @router.post('/invitations/{invitation_id}/confirm', response_model=TeamMember)
    def confirm(invitation_id: UUID, body: ConfirmInvitation):
        return repository.confirm_invitation(actor, str(invitation_id), body.member)

    @router.delete('/invitations/{invitation_id}', status_code=204, response_class=Response)
    def revoke_invitation(invitation_id: UUID):
        repository.revoke_invitation(actor, str(invitation_id))
        return Response(status_code=204)

    @router.delete('/members/{member_id}', response_model=TeamMember)
    def revoke_member(member_id: UUID, body: RevokeMember):
        return repository.revoke_member(actor, str(member_id), body.expected_revision)

    return router
