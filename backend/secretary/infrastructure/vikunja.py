"""Pinned Vikunja v2 boundary. No retries, credentials in DTOs, or DB side effects.

Mutations are single HTTP steps, not task transactions. The caller owns durable
authorization/leases, step evidence and the unconditional final verification.
An accepted-looking HTTP response alone must never become an applied receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
import hashlib
import hmac
import ipaddress
import json
import math
import re
from types import MappingProxyType
from typing import Mapping, Protocol

import httpx
from pydantic import ValidationError

from secretary.domain.team import TaskCommand, TaskSnapshot, TeamMember, content_hash
from secretary.infrastructure.integration_contracts import ExternalContractError


STATES = ('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled')
ZERO_DATE = '0001-01-01T00:00:00Z'
MAX_ID = 2 ** 63 - 1
MAX_BODY = 2 * 1024 * 1024
MAX_PAGES = 1000
MAX_ITEMS = 50_000
_DECIMAL = re.compile(r'[1-9][0-9]{0,18}\Z')
_MARKER = re.compile(r'secretary-origin:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
_COMMAND_MARKER = re.compile(r'secretary-command:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:(?:0|[1-9][0-9]{0,5})\Z')


class VikunjaError(ExternalContractError):
    """Sanitized failure. No provider detail, request body or token is exposed."""


class VikunjaMutationUncertain(VikunjaError):
    """A mutation may have reached the server. Reconcile by reading; do not resend."""


def _local_id(value) -> str:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value) or int(value) > MAX_ID:
        raise VikunjaError('invalid_decimal_id')
    return value


def _remote_id(value) -> str:
    if type(value) is not int or not 0 < value <= MAX_ID:
        raise VikunjaError('invalid_provider_id')
    return str(value)


def _integer(value, *, minimum=0, maximum=MAX_ID):
    if type(value) is not int or not minimum <= value <= maximum:
        raise VikunjaError('invalid_provider_integer')
    return value


def _text(value, *, nonempty=False, limit=MAX_BODY):
    if not isinstance(value, str):
        raise VikunjaError('invalid_provider_text')
    try:
        if len(value.encode('utf-8')) > limit or (nonempty and not value.strip()):
            raise VikunjaError('invalid_provider_text')
    except UnicodeError:
        raise VikunjaError('invalid_provider_text') from None
    return value


def _title(value):
    # Domain Text counts Unicode characters, not UTF-8 bytes. Keep the same
    # policy before create/PATCH and after GET so our own writes remain readable.
    value = _text(value, nonempty=True, limit=16_000)
    if len(value) > 4000:
        raise VikunjaError('invalid_provider_text')
    return value


def _records(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise VikunjaError('invalid_collection')
    return value


def _date(value):
    # This is the Go zero date observed in ordinary task responses, not a claim
    # that a live due-clear mutation has been qualified by this module.
    if value == ZERO_DATE or value == '0001-01-01T00:00:00+00:00':
        return None
    if not isinstance(value, str):
        raise VikunjaError('invalid_due_date')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise VikunjaError('invalid_due_date') from None


def _date_json(value):
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class ProjectBinding:
    project_id: str
    manual_view_id: str
    bot_user_id: str
    bucket_ids: Mapping[str, str]
    important_label_id: str
    urgent_label_id: str
    cancelled_label_id: str

    def __post_init__(self):
        for value in (self.project_id, self.manual_view_id, self.bot_user_id,
                      self.important_label_id, self.urgent_label_id, self.cancelled_label_id):
            _local_id(value)
        if set(self.bucket_ids) != set(STATES):
            raise VikunjaError('seven_state_buckets_required')
        copied = {state: _local_id(self.bucket_ids[state]) for state in STATES}
        if len(set(copied.values())) != len(STATES):
            raise VikunjaError('distinct_state_buckets_required')
        if len({self.important_label_id, self.urgent_label_id, self.cancelled_label_id}) != 3:
            raise VikunjaError('distinct_managed_labels_required')
        object.__setattr__(self, 'bucket_ids', MappingProxyType(copied))


@dataclass(frozen=True)
class ProvisionedTokenBinding:
    """Trusted provisioning evidence, kept outside task/browser DTOs.

    Only a successful token-creation receipt from the owner-controlled setup
    flow may establish this binding. Persist its IDs/digest in protected local
    configuration; never construct it from a caller's claimed owner or a
    project membership response. It stores no raw token.
    """
    owner_id: str
    token_id: str
    token_sha256: str

    def __post_init__(self):
        _local_id(self.owner_id)
        _local_id(self.token_id)
        if not isinstance(self.token_sha256, str) or not re.fullmatch('[0-9a-f]{64}', self.token_sha256):
            raise VikunjaError('invalid_token_binding_digest')

    @classmethod
    def from_creation_receipt(cls, response: Mapping, expected_bot_id: str):
        """Consume the trusted POST /tokens 201 object without retaining its secret."""
        expected_bot_id = _local_id(expected_bot_id)
        if not isinstance(response, Mapping):
            raise VikunjaError('invalid_token_creation_receipt')
        owner_id, token_id = _remote_id(response.get('owner_id')), _remote_id(response.get('id'))
        if owner_id != expected_bot_id:
            raise VikunjaError('token_creation_owner_mismatch')
        token = response.get('token')
        if (not isinstance(token, str) or not token.startswith('tk_') or not 3 < len(token) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in token)):
            raise VikunjaError('invalid_token_creation_receipt')
        return cls(owner_id, token_id, hashlib.sha256(token.encode('ascii')).hexdigest())


class MemberDirectory(Protocol):
    def get_by_vikunja_id(self, identifier: str) -> TeamMember | None: ...
    def get_by_id(self, identifier: str) -> TeamMember | None: ...


class MappingMemberDirectory:
    """Immutable snapshot of explicit identities; no fuzzy display-name lookup."""

    def __init__(self, members: tuple[TeamMember, ...]):
        normalized = tuple(TeamMember.model_validate(member.model_dump()) for member in members)
        self._remote = {member.vikunja_user_id: member for member in normalized}
        self._local = {member.id: member for member in normalized}
        if len(self._local) != len(normalized) or len(self._remote) != len(normalized):
            raise VikunjaError('duplicate_member_identity')

    def get_by_vikunja_id(self, identifier):
        return self._remote.get(_local_id(identifier))

    def get_by_id(self, identifier):
        return self._local.get(identifier)


@dataclass(frozen=True)
class TaskReadContext:
    revision: int
    baseline: TaskSnapshot | None = None
    verifying_command: TaskCommand | None = None

    def __post_init__(self):
        _integer(self.revision)
        if self.baseline is not None:
            TaskSnapshot.model_validate(self.baseline.model_dump())
            if self.revision not in (self.baseline.revision, self.baseline.revision + 1):
                raise VikunjaError('invalid_projection_revision')
        elif self.revision != 0:
            raise VikunjaError('initial_projection_revision_must_be_zero')
        if self.verifying_command is not None:
            command = TaskCommand.model_validate(self.verifying_command.model_dump(exclude_unset=True))
            if command.action == 'create':
                if self.baseline is not None or self.revision != 0:
                    raise VikunjaError('invalid_create_projection_context')
            elif (self.baseline is None or self.revision != self.baseline.revision + 1
                  or command.expected_revision != self.baseline.revision
                  or command.expected_fingerprint != self.baseline.remote_fingerprint):
                raise VikunjaError('invalid_command_projection_context')


@dataclass(frozen=True)
class CommentObservation:
    id: str
    comment: str
    author_id: str


@dataclass(frozen=True)
class TaskObservation:
    """Provider facts; deliberately carries neither gateway revision nor confirmations."""
    task_id: str
    project_id: str
    title: str
    description: str
    done: bool
    due_at: datetime | None
    assignee_ids: tuple[str, ...]
    label_ids: tuple[str, ...]
    bucket_id: str
    buckets: tuple[tuple[str, str], ...]
    comments: tuple[CommentObservation, ...]
    repeat_after: int
    repeat_mode: int
    remote_fingerprint: str
    etag: str | None


@dataclass(frozen=True)
class TaskPage:
    """Internal provider records, not a browser-facing task DTO."""
    items: tuple[dict, ...]
    page: int
    per_page: int
    total: int
    total_pages: int
    next_page: int | None


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag in {'p', 'div', 'br', 'li', 'pre'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {'p', 'div', 'li', 'pre'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def visible_text(value: str) -> str:
    """Bounded HTML-to-visible-text projection; does not modify stored HTML."""
    parser = _VisibleText()
    parser.feed(_text(value))
    parser.close()
    return ''.join(parser.parts)


def has_visible_marker(value: str, marker: str) -> bool:
    """Match a canonical origin/command marker on its own visible line.

    Author identity, expected comment text and preservation of prior comments
    remain mandatory application postconditions for command completion.
    """
    if not isinstance(marker, str) or not (_MARKER.fullmatch(marker) or _COMMAND_MARKER.fullmatch(marker)):
        raise VikunjaError('invalid_visible_marker')
    # A standalone visible line prevents matches in link attributes, quoted
    # suffixes and another UUID. The marker is never interpreted as HTML.
    return any(line.strip() == marker for line in visible_text(value).splitlines())


def _literal_html(value):
    # Secretary/MAX source text is literal, never interpreted as Markdown or
    # user HTML. Normalize only line endings; escape before adding our markup.
    value = _text(value).replace('\r\n', '\n').replace('\r', '\n')
    return '<p>' + escape(value).replace('\n', '<br>') + '</p>'


def _marker(marker):
    if not isinstance(marker, str) or not _MARKER.fullmatch(marker):
        raise VikunjaError('invalid_origin_marker')
    return marker


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _check_depth(value):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError('json_depth_exceeded')
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


class VikunjaClient:
    def __init__(self, http_client: httpx.Client, *, binding: ProjectBinding, members: MemberDirectory,
                 credential_binding: ProvisionedTokenBinding | None = None):
        self._http = http_client
        self.binding = binding
        self.members = members
        if credential_binding is not None and not isinstance(credential_binding, ProvisionedTokenBinding):
            raise VikunjaError('invalid_credential_binding')
        self.credential_binding = credential_binding
        self._authorization_hash: str | None = None
        self._uses_api_token = False
        self._binding_validated = False
        self._etags: dict[str, str] = {}
        self._validate_transport()

    def _validate_transport(self):
        url = self._http.base_url
        try:
            local = ipaddress.ip_address(url.host).is_loopback
        except ValueError:
            local = False
        auth = self._http.headers.get('Authorization', '')
        timeout = self._http.timeout
        if (url.scheme != 'http' or not local or not url.port or url.path.rstrip('/') != '/api/v2'
                or url.query or url.fragment or url.username or url.password or self._http.params
                or self._http.trust_env or self._http.follow_redirects or self._http.auth is not None
                or any(self._http.event_hooks.values())
                or any(value is not None for value in self._http._mounts.values())
                or not auth.startswith('Bearer ') or not auth[7:].strip()
                or any(c.isspace() for c in auth[7:])
                or any(value is None or not math.isfinite(value) or not 0 < value <= 30
                       for value in (timeout.connect, timeout.read, timeout.write, timeout.pool))):
            raise VikunjaError('unsafe_vikunja_transport')
        token = auth[7:]
        if len(token) > 4096 or any(not 33 <= ord(char) <= 126 for char in token):
            raise VikunjaError('unsafe_vikunja_transport')
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        if self._authorization_hash is not None and not hmac.compare_digest(self._authorization_hash, digest):
            raise VikunjaError('vikunja_credential_changed')
        uses_api_token = token.startswith('tk_')
        if uses_api_token:
            evidence = self.credential_binding
            if evidence is None:
                raise VikunjaError('provisioned_token_binding_required')
            if (evidence.owner_id != self.binding.bot_user_id
                    or not hmac.compare_digest(evidence.token_sha256, digest)):
                raise VikunjaError('provisioned_token_binding_mismatch')
        elif self.credential_binding is not None:
            raise VikunjaError('provisioned_binding_requires_api_token')
        self._authorization_hash, self._uses_api_token = digest, uses_api_token

    @staticmethod
    def _bad(code, mutation, status=None):
        error = VikunjaMutationUncertain if mutation else VikunjaError
        raise error(code, status_code=status) from None

    def _request(self, method, path, expected, *, payload=None, params=None, headers=None, allow_304=False):
        self._validate_transport()
        mutation = method != 'GET'
        if allow_304 and method not in ('GET', 'PATCH'):
            raise VikunjaError('304_not_supported_for_method')
        if mutation and not self._binding_validated:
            raise VikunjaError('binding_not_validated')
        if payload is not None:
            try:
                encoded = json.dumps(payload, allow_nan=False).encode('utf-8')
            except (ValueError, TypeError):
                raise VikunjaError('invalid_mutation_payload') from None
            if len(encoded) > MAX_BODY:
                raise VikunjaError('mutation_payload_too_large')
        try:
            with self._http.stream(method, path.lstrip('/'), json=payload, params=params,
                                   headers=headers, follow_redirects=False) as response:
                status = response.status_code
                if 400 <= status < 500:
                    raise VikunjaError('vikunja_http_rejected', status_code=status)
                if status != expected and not (allow_304 and status == 304):
                    self._bad('unexpected_http_status', mutation, status)
                data = bytearray()
                for part in response.iter_bytes():
                    data.extend(part)
                    if len(data) > MAX_BODY:
                        self._bad('response_too_large', mutation, status)
                if status in (204, 304):
                    if data:
                        self._bad('unexpected_empty_response_body', mutation, status)
                    return None, response.headers, status
                if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
                    self._bad('invalid_response_content_type', mutation, status)
                try:
                    parsed = json.loads(data, object_pairs_hook=_pairs,
                                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(parsed, dict):
                        raise ValueError
                    _check_depth(parsed)
                except (ValueError, UnicodeError, RecursionError):
                    self._bad('malformed_success_response', mutation, status)
                return parsed, response.headers, status
        except httpx.HTTPError:
            self._bad('vikunja_transport_failure', mutation)

    @staticmethod
    def _page(data, requested):
        try:
            items = _records(data['items'])
            number = _integer(data['page'], minimum=1, maximum=MAX_PAGES)
            size = _integer(data['per_page'], minimum=1, maximum=MAX_ITEMS)
            total = _integer(data['total'], maximum=MAX_ITEMS)
            pages = _integer(data['total_pages'], maximum=MAX_PAGES)
            if number != requested or len(items) > size or len(items) > total:
                raise VikunjaError('invalid_pagination')
            if (total == 0 and (pages != 0 or items)) or (total > 0 and pages == 0):
                raise VikunjaError('invalid_pagination')
            if pages >= number and total > 0 and not items:
                raise VikunjaError('incomplete_page')
            return TaskPage(tuple(items), number, size, total, pages, number + 1 if number < pages else None)
        except (KeyError, TypeError):
            raise VikunjaError('invalid_collection') from None

    def _read_pages(self, path, *, params=None):
        number, seen, values, expected_total = 1, set(), [], None
        while True:
            data = self._request('GET', path, 200, params={**(params or {}), 'page': number, 'per_page': 50})[0]
            current = self._page(data, number)
            if expected_total is not None and current.total != expected_total:
                raise VikunjaError('collection_changed_during_scan')
            expected_total = current.total
            for item in current.items:
                identifier = _remote_id(item.get('id'))
                if identifier in seen:
                    raise VikunjaError('duplicate_collection_id')
                seen.add(identifier)
                values.append(item)
            if current.next_page is None:
                if len(values) != expected_total:
                    raise VikunjaError('incomplete_collection')
                return values
            number = current.next_page

    def validate_binding(self):
        self._binding_validated = False
        b = self.binding
        if self._uses_api_token:
            # /user does not accept scoped API tokens in pinned Vikunja 2.7.
            # Receipt hash+owner is the token attestation. This independent
            # all-page direct-membership read proves project access only.
            users = self._read_pages(f'/projects/{b.project_id}/users')
            matching = [item for item in users if _remote_id(item.get('id')) == b.bot_user_id]
            if len(matching) != 1:
                raise VikunjaError('bot_direct_membership_required')
            user = matching[0]
            _remote_id(user.get('bot_owner_id'))
            _integer(user.get('permission'), minimum=1, maximum=2)
        else:
            user = self._request('GET', '/user', 200)[0]
            if _remote_id(user.get('id')) != b.bot_user_id:
                raise VikunjaError('bot_identity_mismatch')
            _remote_id(user.get('bot_owner_id'))
        view = self._request('GET', f'/projects/{b.project_id}/views/{b.manual_view_id}', 200)[0]
        if (_remote_id(view.get('id')) != b.manual_view_id
                or _remote_id(view.get('project_id')) != b.project_id
                or view.get('view_kind') != 'kanban' or view.get('bucket_configuration_mode') != 'manual'
                or type(view.get('done_bucket_id')) is not int or view['done_bucket_id'] != 0
                or _remote_id(view.get('default_bucket_id')) != b.bucket_ids['inbox']):
            raise VikunjaError('manual_view_configuration_mismatch')
        # Native done changes also affect OTHER Kanban views. Provisioning
        # must disable their done coupling too; keep every view in fingerprints
        # rather than silently ignoring an automatic, unrelated bucket change.
        views = self._read_pages(f'/projects/{b.project_id}/views')
        matching = []
        for candidate in views:
            identifier = _remote_id(candidate.get('id'))
            if (_remote_id(candidate.get('project_id')) != b.project_id
                    or candidate.get('view_kind') not in ('list', 'gantt', 'table', 'kanban')):
                raise VikunjaError('project_view_scope_mismatch')
            if candidate['view_kind'] == 'kanban' and (
                    type(candidate.get('done_bucket_id')) is not int or candidate['done_bucket_id'] != 0):
                raise VikunjaError('project_kanban_done_automation_enabled')
            if identifier == b.manual_view_id:
                matching.append(candidate)
        configured_keys = ('id', 'project_id', 'view_kind', 'bucket_configuration_mode',
                           'done_bucket_id', 'default_bucket_id')
        if len(matching) != 1 or any(matching[0].get(key) != view.get(key) for key in configured_keys):
            raise VikunjaError('manual_view_collection_mismatch')
        data = self._request('GET', f'/projects/{b.project_id}/views/{b.manual_view_id}/buckets', 200)[0]
        if 'items' not in data:
            raise VikunjaError('invalid_collection')
        buckets = _records(data['items'])
        ids = [_remote_id(bucket.get('id')) for bucket in buckets]
        if (len(ids) != 7 or set(ids) != set(b.bucket_ids.values())
                or any(_remote_id(bucket.get('project_view_id')) != b.manual_view_id for bucket in buckets)):
            raise VikunjaError('bucket_mapping_mismatch')
        for identifier, name in ((b.important_label_id, 'secretary:important'),
                                 (b.urgent_label_id, 'secretary:urgent'),
                                 (b.cancelled_label_id, 'secretary:cancelled')):
            label = self._request('GET', f'/labels/{identifier}', 200)[0]
            creator = label.get('created_by')
            if (_remote_id(label.get('id')) != identifier or label.get('title') != name
                    or not isinstance(creator, dict) or _remote_id(creator.get('id')) != b.bot_user_id):
                raise VikunjaError('managed_label_identity_mismatch')
        self._binding_validated = True

    def _task_identity(self, data, task_id=None):
        identifier = _remote_id(data.get('id'))
        if (task_id is not None and identifier != task_id
                or _remote_id(data.get('project_id')) != self.binding.project_id):
            raise VikunjaError('task_scope_mismatch')
        return identifier

    def observe_task(self, task_id, *, conditional=False):
        task_id = _local_id(task_id)
        params = {'expand': 'buckets', 'format': 'html'}
        headers = {'If-None-Match': self._etags[task_id]} if conditional and task_id in self._etags else {}
        data, response_headers, status = self._request('GET', f'/tasks/{task_id}', 200,
            params=params, headers=headers, allow_304=True)
        if status == 304:
            if not headers:
                raise VikunjaError('304_without_cached_representation')
            # T1 only proved ETags for ordinary task changes, not relation/bucket
            # invalidation. Until that is qualified, 304 cannot skip a full read.
            data, response_headers, _ = self._request('GET', f'/tasks/{task_id}', 200, params=params)
        self._task_identity(data, task_id)
        try:
            title = _title(data['title'])
            description = _text(data['description'])
            done = data['done']
            if type(done) is not bool:
                raise VikunjaError('invalid_done_state')
            due = _date(data['due_date'])
            assignees = tuple(sorted(_remote_id(row.get('id')) for row in _records(data['assignees'])))
            labels = tuple(sorted(_remote_id(row.get('id')) for row in _records(data['labels'])))
            buckets = tuple(sorted((_remote_id(row.get('project_view_id')), _remote_id(row.get('id')))
                                   for row in _records(data['buckets'])))
            if len(set(assignees)) != len(assignees) or len(set(labels)) != len(labels) or len(set(buckets)) != len(buckets):
                raise VikunjaError('duplicate_remote_identity')
            selected = [bucket for view, bucket in buckets if view == self.binding.manual_view_id]
            if len(selected) != 1 or selected[0] not in self.binding.bucket_ids.values():
                raise VikunjaError('task_bucket_mapping_mismatch')
            repeat_after = _integer(data['repeat_after'])
            repeat_mode = _integer(data['repeat_mode'])
        except (KeyError, TypeError):
            raise VikunjaError('invalid_task_response') from None
        comments = []
        for item in self._read_pages(f'/tasks/{task_id}/comments', params={'format': 'html'}):
            try:
                comments.append(CommentObservation(_remote_id(item['id']), _text(item['comment']),
                                                   _remote_id(item['author']['id'])))
            except (KeyError, TypeError):
                raise VikunjaError('invalid_comment_response') from None
        comments.sort(key=lambda item: item.id)
        fingerprint = content_hash({'schema': 1, 'task_id': task_id, 'project_id': self.binding.project_id,
            'title': title, 'description': description, 'done': done, 'due_at': _date_json(due),
            'assignee_ids': assignees, 'label_ids': labels, 'buckets': buckets,
            'repeat_after': repeat_after, 'repeat_mode': repeat_mode,
            'comments': [{'id': c.id, 'comment': c.comment, 'author_id': c.author_id} for c in comments]})
        etag = response_headers.get('ETag')
        if etag and (len(etag) > 1024 or any(ord(char) < 32 for char in etag)):
            raise VikunjaError('invalid_etag')
        if etag:
            self._etags[task_id] = etag
        return TaskObservation(task_id, self.binding.project_id, title, description, done, due,
                               assignees, labels, selected[0], buckets, tuple(comments),
                               repeat_after, repeat_mode, fingerprint, etag)

    def _member(self, local_id):
        member = self.members.get_by_id(local_id)
        if not member or member.id != local_id or not member.enabled or self.binding.project_id not in member.project_ids:
            raise VikunjaError('assignee_unavailable')
        _local_id(member.vikunja_user_id)
        return member

    def get_task(self, task_id, *, context: TaskReadContext, conditional=False) -> TaskSnapshot:
        """Map final provider facts using trusted gateway metadata.

        A verifying command may be supplied only after the application proves
        every durable step, including comments and preservation of prior data.
        This mapping alone is not an applied-command receipt.
        """
        observed = self.observe_task(task_id, conditional=conditional)
        baseline, command = context.baseline, context.verifying_command
        if baseline and (baseline.task_id != observed.task_id or baseline.project_id != observed.project_id):
            raise VikunjaError('projection_context_scope_mismatch')
        if observed.repeat_after or observed.repeat_mode:
            raise VikunjaError('repeating_task_not_supported')
        bucket = next(state for state, identifier in self.binding.bucket_ids.items() if identifier == observed.bucket_id)
        cancelled = self.binding.cancelled_label_id in observed.label_ids
        if observed.done != (bucket in ('done', 'cancelled')) or cancelled != (bucket == 'cancelled'):
            raise VikunjaError('inconsistent_remote_task_state')
        if len(observed.assignee_ids) > 1:
            raise VikunjaError('multiple_assignees_not_supported')
        assignee = None
        if observed.assignee_ids:
            member = self.members.get_by_vikunja_id(observed.assignee_ids[0])
            if (member is None or member.vikunja_user_id != observed.assignee_ids[0]
                    or self.binding.project_id not in member.project_ids):
                raise VikunjaError('unknown_remote_assignee')
            assignee = member.id
        important = self.binding.important_label_id in observed.label_ids
        urgent = self.binding.urgent_label_id in observed.label_ids
        classified = bool(baseline and baseline.classification_confirmed
                          and (baseline.important, baseline.urgent) == (important, urgent))
        same_due = bool(baseline and baseline.due_at == observed.due_at)
        due_confirmed = bool(same_due and baseline.due_confirmed)
        due_phrase = baseline.due_phrase if same_due else None
        origin = baseline.origin if baseline else None
        if command:
            if command.project_id != observed.project_id or command.task_id not in (None, observed.task_id):
                raise VikunjaError('command_context_scope_mismatch')
            values = command.values
            expected = {}
            if command.action in ('create', 'rename'):
                expected['title'] = values.title
            if command.action in ('create', 'assign'):
                expected['assignee'] = values.assignee_id
            if command.action == 'create':
                expected['bucket'] = 'inbox'
            if command.action == 'set_state':
                expected['bucket'] = values.bucket
            if command.action in ('create', 'classify') and values.classification_confirmed is True:
                expected.update(important=values.important, urgent=values.urgent)
            if command.action in ('create', 'set_due', 'resolve_due') and 'due_at' in values.model_fields_set:
                expected['due_at'] = values.due_at
            actual = {'title': observed.title, 'assignee': assignee, 'bucket': bucket,
                      'important': important, 'urgent': urgent, 'due_at': observed.due_at}
            if any(actual[key] != value for key, value in expected.items()):
                raise VikunjaError('task_postcondition_mismatch')
            if command.action == 'create':
                expected_marker = 'secretary-origin:' + (command.origin.publication_id
                    if command.origin and command.origin.publication_id else command.operation_id)
                if not has_visible_marker(observed.description, expected_marker):
                    raise VikunjaError('origin_postcondition_mismatch')
                origin = command.origin
            if command.action in ('create', 'classify') and values.classification_confirmed is True:
                classified = True
            if command.action in ('create', 'set_due', 'resolve_due') and 'due_at' in values.model_fields_set:
                due_confirmed, due_phrase = values.due_confirmed is True, values.due_phrase
        confirms_due = bool(command and command.action in ('create', 'set_due', 'resolve_due')
                            and 'due_at' in command.values.model_fields_set)
        if (observed.due_at is not None and not due_confirmed
                or baseline and not same_due and not confirms_due):
            raise VikunjaError('remote_due_unconfirmed')
        try:
            return TaskSnapshot(task_id=observed.task_id, project_id=observed.project_id,
                revision=context.revision, remote_fingerprint=observed.remote_fingerprint,
                title=observed.title, description=observed.description, assignee_id=assignee,
                bucket=bucket, important=important, urgent=urgent, classification_confirmed=classified,
                due_at=observed.due_at, due_phrase=due_phrase, due_confirmed=due_confirmed, origin=origin)
        except ValidationError:
            raise VikunjaError('invalid_task_projection') from None

    def list_project_tasks(self, *, page=1, per_page=50):
        _integer(page, minimum=1, maximum=MAX_PAGES)
        _integer(per_page, minimum=1, maximum=1000)
        data = self._request('GET', f'/projects/{self.binding.project_id}/tasks', 200,
            params={'page': page, 'per_page': per_page, 'format': 'html', 'sort_by': 'id', 'order_by': 'asc'})[0]
        result = self._page(data, page)
        seen = set()
        for item in result.items:
            identifier = self._task_identity(item)
            if identifier in seen:
                raise VikunjaError('duplicate_collection_id')
            seen.add(identifier)
            _text(item.get('description'))
        return result

    def find_origin(self, project_id, marker):
        if _local_id(project_id) != self.binding.project_id:
            raise VikunjaError('project_scope_mismatch')
        marker = _marker(marker)
        number, seen, matches, total = 1, set(), [], None
        while True:
            current = self.list_project_tasks(page=number)
            if total is not None and current.total != total:
                raise VikunjaError('collection_changed_during_scan')
            total = current.total
            for item in current.items:
                identifier = self._task_identity(item)
                if identifier in seen:
                    raise VikunjaError('duplicate_collection_id')
                seen.add(identifier)
                if has_visible_marker(item['description'], marker):
                    matches.append(identifier)
            if current.next_page is None:
                if len(seen) != total:
                    raise VikunjaError('incomplete_collection')
                # Empty is only an observation, never permission to retry POST.
                return tuple(matches)
            number = current.next_page

    def _mutation_result(self, data, validator):
        try:
            validator(data)
        except (VikunjaError, KeyError, TypeError, ValueError):
            raise VikunjaMutationUncertain('mutation_response_unverified') from None

    def create_task(self, draft: TaskCommand, marker):
        """Create literal-text description with a recoverable visible origin marker."""
        draft = TaskCommand.model_validate(draft.model_dump(exclude_unset=True))
        marker = _marker(marker)
        if draft.action != 'create' or draft.project_id != self.binding.project_id:
            raise VikunjaError('invalid_create_scope')
        expected = draft.origin.publication_id if draft.origin and draft.origin.publication_id else draft.operation_id
        if marker != 'secretary-origin:' + expected:
            raise VikunjaError('origin_marker_scope_mismatch')
        self._member(draft.values.assignee_id)
        description = draft.values.description or ''
        if 'secretary-origin:' in description:
            raise VikunjaError('description_contains_origin_marker')
        payload = {'title': _title(draft.values.title), 'description': _literal_html(description) + _literal_html(marker)}
        if draft.values.due_at is not None:
            payload['due_date'] = draft.values.due_at.isoformat()
        data = self._request('POST', f'/projects/{self.binding.project_id}/tasks', 201,
                             payload=payload, params={'format': 'html'}, headers={'X-Vikunja-Format': 'html'})[0]
        self._mutation_result(data, self._task_identity)
        return str(data['id'])

    def apply_task_change(self, task_id, patch: Mapping, expected_etag=None):
        task_id = _local_id(task_id)
        if not isinstance(patch, Mapping) or not patch or set(patch) - {'title', 'due_date', 'done'}:
            raise VikunjaError('invalid_task_patch_fields')
        payload = dict(patch)
        if 'title' in payload:
            _title(payload['title'])
        if 'due_date' in payload:
            _date(payload['due_date'])
        if 'done' in payload and type(payload['done']) is not bool:
            raise VikunjaError('invalid_done_state')
        headers = {'Content-Type': 'application/merge-patch+json', 'X-Vikunja-Format': 'html'}
        if expected_etag is not None:
            _text(expected_etag, nonempty=True, limit=1024)
            if any(ord(char) < 32 for char in expected_etag):
                raise VikunjaError('invalid_etag')
            headers['If-Match'] = expected_etag  # T1 proved this is NOT write CAS.
        data, _, status = self._request('PATCH', f'/tasks/{task_id}', 200,
                                       payload=payload, headers=headers, allow_304=True)
        # Pinned runtime returns empty 304 for an unchanged PATCH, even without
        # conditional headers. This is not postcondition proof: the application
        # must still observe and verify the requested target before applying.
        if status != 304:
            self._mutation_result(data, lambda row: self._task_identity(row, task_id))

    def add_assignee(self, task_id, member_id):
        task_id = _local_id(task_id)
        provider_id = self._member(member_id).vikunja_user_id
        data = self._request('POST', f'/tasks/{task_id}/assignees', 201,
                             payload={'user_id': int(provider_id)})[0]
        def check(row):
            if _remote_id(row.get('user_id')) != provider_id:
                raise VikunjaError('assignee_response_mismatch')
        self._mutation_result(data, check)

    def remove_assignee(self, task_id, provider_user_id):
        self._request('DELETE', f'/tasks/{_local_id(task_id)}/assignees/{_local_id(provider_user_id)}', 204)

    def _label(self, label_id):
        label_id = _local_id(label_id)
        if label_id not in (self.binding.important_label_id, self.binding.urgent_label_id, self.binding.cancelled_label_id):
            raise VikunjaError('unmanaged_label')
        return label_id

    def add_label(self, task_id, label_id):
        task_id, label_id = _local_id(task_id), self._label(label_id)
        data = self._request('POST', f'/tasks/{task_id}/labels', 201, payload={'label_id': int(label_id)})[0]
        def check(row):
            if _remote_id(row.get('label_id')) != label_id:
                raise VikunjaError('label_response_mismatch')
        self._mutation_result(data, check)

    def remove_label(self, task_id, label_id):
        self._request('DELETE', f'/tasks/{_local_id(task_id)}/labels/{self._label(label_id)}', 204)

    def move_task(self, task_id, bucket):
        task_id = _local_id(task_id)
        if bucket not in self.binding.bucket_ids:
            raise VikunjaError('unknown_state_bucket')
        b = self.binding
        bucket_id = b.bucket_ids[bucket]
        data = self._request('PUT', f'/projects/{b.project_id}/views/{b.manual_view_id}/buckets/{bucket_id}/tasks',
                             200, payload={'task_id': int(task_id)})[0]
        def check(row):
            if (_remote_id(row.get('task_id')) != task_id or _remote_id(row.get('bucket_id')) != bucket_id
                    or _remote_id(row.get('project_view_id')) != b.manual_view_id):
                raise VikunjaError('bucket_response_mismatch')
        self._mutation_result(data, check)

    def add_comment(self, task_id, text):
        """Append literal source text, preserving a caller-supplied visible step marker."""
        task_id = _local_id(task_id)
        text = _text(text, nonempty=True, limit=32_000)
        if len(text) > 5000:
            raise VikunjaError('invalid_comment_text')
        data = self._request('POST', f'/tasks/{task_id}/comments', 201,
                             payload={'comment': _literal_html(text)}, params={'format': 'html'},
                             headers={'X-Vikunja-Format': 'html'})[0]
        def check(row):
            _remote_id(row.get('id'))
            _text(row.get('comment'), nonempty=True)
            if not isinstance(row.get('author'), dict) or _remote_id(row['author'].get('id')) != self.binding.bot_user_id:
                raise VikunjaError('comment_author_mismatch')
        self._mutation_result(data, check)
        return str(data['id'])

    def delete_task(self, task_id):
        self._request('DELETE', f'/tasks/{_local_id(task_id)}', 204)
