"""Closed native GET diagnostics; neither this transport nor its inputs are proof.

The public lane owns its HTTP transport but accepts diagnostic binding claims.
It does not establish listener/process ownership, SourceEvidence, consent, token
ID/scopes/expiry, or activation authority. The private mock lane is permanently
synthetic. Separate GET responses do not constitute one database snapshot, and
direct project users do not include team shares. This module performs no launch,
credential issuance, source/store writes, or restore-hold transitions.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re

import httpx
from pydantic import SecretStr


_MAX_ID = (1 << 63) - 1
_MAX_BODY = 64 * 1024
_MAX_AGGREGATE = 1024 * 1024
_MAX_PAGES = 20
_MAX_USERS = 1000
_PER_PAGE = 50
_MAX_DEPTH = 32
_REQUEST_TIMEOUT = 5.0
_TOTAL_TIMEOUT = 30.0
_ERRORS = frozenset({
    'native_binding_invalid', 'native_credential_invalid', 'native_transport_unavailable',
    'native_http_unavailable', 'native_body_invalid', 'native_body_limit',
    'native_identity_mismatch', 'native_project_invalid', 'native_membership_invalid',
    'native_pagination_invalid', 'native_timeout', 'native_observation_unavailable',
})


class NativeObservationError(ValueError):
    """Stable public codes only; never include a server body or secret argument."""

    def __init__(self, code='native_observation_unavailable'):
        safe = code if type(code) is str and code in _ERRORS else 'native_observation_unavailable'
        super().__init__(safe)


def _integer(value, *, minimum=1, maximum=_MAX_ID):
    return type(value) is int and minimum <= value <= maximum


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode('utf-8')).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeGetBinding:
    port: int
    project_id: int
    principal_id: int
    human_owner_id: int
    credential_sha256: str

    def __post_init__(self):
        if (not _integer(self.port, minimum=1024, maximum=65535) or self.port == 8765
                or not all(_integer(value) for value in
                           (self.project_id, self.principal_id, self.human_owner_id))
                or self.principal_id == self.human_owner_id
                or type(self.credential_sha256) is not str
                or re.fullmatch(r'[0-9a-f]{64}', self.credential_sha256) is None):
            raise NativeObservationError('native_binding_invalid')


@dataclass(frozen=True, slots=True)
class NativeDirectMember:
    id: int
    bot_owner_id: int
    permission: int


@dataclass(frozen=True, slots=True)
class NativeDiagnosticResult:
    evidence_kind: str
    binding_sha256: str
    credential_sha256: str
    resource_sha256: str
    principal_id: int
    human_owner_id: int
    project_id: int
    project_owner_id: int
    project_max_permission: int
    project_updated_at: str
    direct_members: tuple[NativeDirectMember, ...]
    pages: int
    started_at: str
    finished_at: str
    method: str = field(default='GET', init=False)
    diagnostic_only: bool = field(default=True, init=False)
    activation_supported: bool = field(default=False, init=False)
    outbound_enabled: bool = field(default=False, init=False)

    def __post_init__(self):
        if self.evidence_kind not in ('diagnostic_get', 'synthetic_get'):
            raise NativeObservationError()


def _validated_token(binding, token):
    if type(binding) is not NativeGetBinding:
        raise NativeObservationError('native_binding_invalid')
    # Revalidate the current values; frozen caller claims do not become authority.
    NativeGetBinding(**asdict(binding))
    if not isinstance(token, SecretStr):
        raise NativeObservationError('native_credential_invalid')
    raw = token.get_secret_value()
    if (type(raw) is not str or not 3 < len(raw) <= 4096 or not raw.startswith('tk_')
            or any(not 33 <= ord(character) <= 126 for character in raw)):
        raise NativeObservationError('native_credential_invalid')
    digest = hashlib.sha256(raw.encode('ascii')).hexdigest()
    if not hmac.compare_digest(digest, binding.credential_sha256):
        raise NativeObservationError('native_credential_invalid')
    return raw


def _client_options(binding, raw):
    return dict(base_url=f'http://127.0.0.1:{binding.port}/api/v2/',
                headers={'Authorization': f'Bearer {raw}', 'Accept': 'application/json',
                         'Accept-Encoding': 'identity'},
                trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(5, connect=2, pool=2))


@asynccontextmanager
async def _direct_client(binding, token):
    raw = _validated_token(binding, token)
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    async with httpx.AsyncClient(transport=transport, **_client_options(binding, raw)) as client:
        yield client


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise NativeObservationError('native_body_invalid')
        result[key] = value
    return result


def _nonfinite(value):
    raise NativeObservationError('native_body_invalid')


def _json_body(body):
    try:
        value = json.loads(body.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (UnicodeError, ValueError, RecursionError):
        raise NativeObservationError('native_body_invalid') from None
    if type(value) is not dict:
        raise NativeObservationError('native_body_invalid')
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_DEPTH:
            raise NativeObservationError('native_body_invalid')
        if type(current) is dict:
            stack.extend((child, depth + 1) for child in current.values())
        elif type(current) is list:
            stack.extend((child, depth + 1) for child in current)
        elif type(current) is float and not math.isfinite(current):
            raise NativeObservationError('native_body_invalid')
    return value


class _Reader:
    """Private closed route map and one aggregate body budget for the whole scan."""

    def __init__(self, client, binding):
        self.client, self.binding = client, binding
        self.total_bytes = 0

    async def get(self, operation, *, page=None):
        if operation == 'identity' and page is None:
            path, params = 'user', None
        elif operation == 'project' and page is None:
            path, params = f'projects/{self.binding.project_id}', None
        elif operation == 'direct_users' and _integer(page, maximum=_MAX_PAGES):
            path = f'projects/{self.binding.project_id}/users'
            params = {'page': page, 'per_page': _PER_PAGE}
        else:
            raise NativeObservationError('native_observation_unavailable')
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                async with self.client.stream('GET', path, params=params) as response:
                    if response.status_code != 200:
                        raise NativeObservationError('native_http_unavailable')
                    media = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                    encoding = response.headers.get('Content-Encoding', '').strip().lower()
                    if media != 'application/json' or encoding not in ('', 'identity'):
                        raise NativeObservationError('native_body_invalid')
                    declared = response.headers.get('Content-Length')
                    if declared is not None:
                        if re.fullmatch(r'[0-9]{1,10}', declared) is None:
                            raise NativeObservationError('native_body_invalid')
                        declared = int(declared)
                        if declared > _MAX_BODY:
                            raise NativeObservationError('native_body_limit')
                    chunks, count = [], 0
                    # Mock/preloaded responses can already be consumed. The same
                    # bounds apply; live responses are read incrementally as raw bytes.
                    if response.is_stream_consumed:
                        buffered = response.content
                        count = len(buffered)
                        self.total_bytes += count
                        if count > _MAX_BODY or self.total_bytes > _MAX_AGGREGATE:
                            raise NativeObservationError('native_body_limit')
                        chunks.append(buffered)
                    else:
                        async for chunk in response.aiter_raw():
                            count += len(chunk)
                            self.total_bytes += len(chunk)
                            if count > _MAX_BODY or self.total_bytes > _MAX_AGGREGATE:
                                raise NativeObservationError('native_body_limit')
                            chunks.append(chunk)
                    if declared is not None and count != declared:
                        raise NativeObservationError('native_body_invalid')
                    return _json_body(b''.join(chunks))
        except TimeoutError:
            raise NativeObservationError('native_timeout') from None
        except httpx.HTTPError:
            raise NativeObservationError('native_transport_unavailable') from None


def _identity(body, binding):
    identifier, owner = body.get('id'), body.get('bot_owner_id')
    if (not _integer(identifier) or not _integer(owner)
            or identifier != binding.principal_id or owner != binding.human_owner_id):
        raise NativeObservationError('native_identity_mismatch')


def _project(body, binding):
    owner = body.get('owner')
    permission, updated = body.get('max_permission'), body.get('updated')
    if (not _integer(body.get('id')) or body['id'] != binding.project_id
            or body.get('is_archived') is not False
            or not _integer(permission, maximum=2)
            or type(owner) is not dict or not _integer(owner.get('id'))
            or ('bot_owner_id' in owner and not _integer(owner['bot_owner_id'], minimum=0))
            or type(updated) is not str or len(updated) > 128
            or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})',
                            updated) is None):
        raise NativeObservationError('native_project_invalid')
    try:
        version = datetime.fromisoformat(updated)
        if version.tzinfo is None or version.utcoffset() is None:
            raise ValueError()
    except ValueError:
        raise NativeObservationError('native_project_invalid') from None
    return owner['id'], permission, updated


def _page(body, requested):
    number, size = body.get('page'), body.get('per_page')
    total, pages = body.get('total'), body.get('total_pages')
    if (not _integer(number, maximum=_MAX_PAGES) or number != requested
            or not _integer(size, maximum=_PER_PAGE)
            or not _integer(total, minimum=0, maximum=_MAX_USERS)
            or not _integer(pages, minimum=0, maximum=_MAX_PAGES)
            or pages != (total + size - 1) // size
            or (pages and requested > pages) or (not pages and requested != 1)):
        raise NativeObservationError('native_pagination_invalid')
    items = body.get('items')
    if items is None and total == 0:
        items = []
    if (type(items) is not list
            or len(items) != min(size, max(0, total - (requested - 1) * size))):
        raise NativeObservationError('native_pagination_invalid')
    members = []
    for item in items:
        # Vikunja omits a zero BotOwnerID on the wire. The bound bot still
        # requires its positive owner in _observe below.
        if (type(item) is not dict or not _integer(item.get('id'))
                or not _integer(item.get('bot_owner_id', 0), minimum=0)
                or not _integer(item.get('permission'), minimum=0, maximum=2)):
            raise NativeObservationError('native_membership_invalid')
        members.append(NativeDirectMember(item['id'], item.get('bot_owner_id', 0), item['permission']))
    return (total, size, pages), members


async def _observe(client, binding, evidence_kind):
    started = datetime.now(timezone.utc)
    reader = _Reader(client, binding)
    try:
        async with asyncio.timeout(_TOTAL_TIMEOUT):
            _identity(await reader.get('identity'), binding)
            owner, permission, updated = _project(await reader.get('project'), binding)
            metadata = None
            members, seen = [], set()
            number = 1
            while True:
                actual, current = _page(await reader.get('direct_users', page=number), number)
                if metadata is None:
                    metadata = actual
                elif actual != metadata:
                    raise NativeObservationError('native_pagination_invalid')
                for member in current:
                    if member.id in seen:
                        raise NativeObservationError('native_pagination_invalid')
                    seen.add(member.id)
                    members.append(member)
                if number >= metadata[2]:
                    break
                number += 1
            if len(members) != metadata[0]:
                raise NativeObservationError('native_pagination_invalid')
            principal = [member for member in members if member.id == binding.principal_id]
            if (len(principal) != 1 or principal[0].bot_owner_id != binding.human_owner_id
                    or principal[0].permission not in (1, 2)):
                raise NativeObservationError('native_membership_invalid')
    except TimeoutError:
        raise NativeObservationError('native_timeout') from None
    resource = dict(project_id=binding.project_id, project_owner_id=owner,
                    project_max_permission=permission, project_updated_at=updated,
                    direct_members=[asdict(member) for member in members])
    finished = datetime.now(timezone.utc)
    if finished < started:
        raise NativeObservationError()
    return NativeDiagnosticResult(evidence_kind=evidence_kind,
        binding_sha256=_sha(asdict(binding)), credential_sha256=binding.credential_sha256,
        resource_sha256=_sha(resource), principal_id=binding.principal_id,
        human_owner_id=binding.human_owner_id, project_id=binding.project_id,
        project_owner_id=owner, project_max_permission=permission, project_updated_at=updated,
        direct_members=tuple(members), pages=number, started_at=started.isoformat(),
        finished_at=finished.isoformat())


async def observe_native_diagnostic(binding, *, token):
    """Transport-only diagnostic; caller claims never qualify production activation."""
    async with _direct_client(binding, token) as client:
        return await _observe(client, binding, 'diagnostic_get')


async def _test_observe_native(binding, *, token, transport):
    """Private MockTransport seam; cannot mint a production-labelled observation."""
    raw = _validated_token(binding, token)
    if type(transport) is not httpx.MockTransport:
        raise NativeObservationError('native_transport_unavailable')
    async with httpx.AsyncClient(transport=transport, **_client_options(binding, raw)) as client:
        return await _observe(client, binding, 'synthetic_get')
