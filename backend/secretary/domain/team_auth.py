"""Strict immutable authentication results; no HTTP, persistence or credentials."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from uuid import UUID

from secretary.domain.team import TeamMember


_ERROR_CODES = frozenset({
    'auth_dto_invalid', 'max_auth_configuration_invalid', 'max_init_data_invalid',
    'max_init_data_too_large', 'max_signature_invalid', 'max_identity_invalid',
    'max_auth_date_invalid', 'max_init_data_expired', 'max_init_data_future',
    'auth_invalid_credentials', 'auth_replay', 'auth_csrf_invalid',
    'auth_forbidden', 'auth_conflict', 'auth_rate_limited', 'auth_unavailable',
})


class AuthError(ValueError):
    """Only stable codes, never raw initData, tokens or profile text."""
    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else 'auth_dto_invalid'
        super().__init__(self.code)


def _opaque(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(character in value for character in ('\r', '\n', '\x00'))):
        raise AuthError('auth_dto_invalid')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise AuthError('auth_dto_invalid') from None


def _decimal(value, *, int64=False):
    if (not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,127}', value)
            or int64 and (len(value) > 19 or int(value) > 2 ** 63 - 1)):
        raise AuthError('auth_dto_invalid')


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise AuthError('auth_dto_invalid') from None


def _expires(instance):
    value = instance.expires_at
    try:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        object.__setattr__(instance, 'expires_at', value.astimezone(timezone.utc))
    except (ValueError, TypeError, OverflowError):
        raise AuthError('auth_dto_invalid') from None


@dataclass(frozen=True)
class MaxIdentity:
    user_id: str
    auth_date: int
    signature_hex: str = field(repr=False)
    replay_key: str = field(repr=False)

    def __post_init__(self):
        _decimal(self.user_id, int64=True)
        if type(self.auth_date) is not int or not 0 <= self.auth_date <= 2 ** 63 - 1:
            raise AuthError('auth_dto_invalid')
        for value in (self.signature_hex, self.replay_key):
            if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
                raise AuthError('auth_dto_invalid')


@dataclass(frozen=True)
class SessionGrant:
    token: str = field(repr=False)
    csrf: str = field(repr=False)
    expires_at: datetime
    member: TeamMember

    def __post_init__(self):
        _opaque(self.token)
        _opaque(self.csrf)
        _expires(self)
        try:
            if not isinstance(self.member, TeamMember):
                raise ValueError
            object.__setattr__(self, 'member', TeamMember.model_validate(self.member.model_dump(mode='json')))
        except ValueError:
            raise AuthError('auth_dto_invalid') from None


@dataclass(frozen=True)
class InvitationGrant:
    id: str
    value: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self):
        _uuid(self.id)
        _opaque(self.value)
        _expires(self)


@dataclass(frozen=True)
class DesktopCode:
    # Opaque copyable challenge+code, not an input parser. Malformed code input
    # must reach the repository's atomic attempt counter rather than this DTO.
    value: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self):
        _opaque(self.value)
        _expires(self)


@dataclass(frozen=True)
class InvitationView:
    id: str
    project_ids: tuple[str, ...]
    candidate_user_id: str | None
    expires_at: datetime
    confirmed_member_id: str | None
    revoked: bool

    def __post_init__(self):
        _uuid(self.id)
        _expires(self)
        if type(self.project_ids) is not tuple or not self.project_ids:
            raise AuthError('auth_dto_invalid')
        for value in self.project_ids:
            _decimal(value)
        if len(set(self.project_ids)) != len(self.project_ids) or type(self.revoked) is not bool:
            raise AuthError('auth_dto_invalid')
        if self.candidate_user_id is not None:
            _decimal(self.candidate_user_id, int64=True)
        if self.confirmed_member_id is not None:
            _uuid(self.confirmed_member_id)
