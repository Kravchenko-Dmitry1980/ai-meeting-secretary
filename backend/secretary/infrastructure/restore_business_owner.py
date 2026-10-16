"""Explicit current local business-owner policy and purpose-specific consent.

The human enrollment ceremony is this deployment's business-role trust root.
It does not infer ownership from the Windows operator, archived Team members,
sessions, MAX/Vikunja accounts, settings or a caller's asserted role. The human
must verify the identity mapping shown in the ceremony. CurrentUser DPAPI and
private Windows ACLs protect against other accounts, not hostile same-user code.

This issuer signs consent to an exact commitment; provider-observation digests
are bindings, not semantic provider proof. No activation, source write, provider
call or nonce consumption is performed. A future independent decision writer
must authenticate again and atomically consume the nonce for its exact decision.
Imports and construction create no stores. No existing policy is reset/adopted.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
from uuid import UUID, uuid4

from .restore_operator import (
    LIMIT, OperatorError, _WindowsAdapter, _canonical, _directories, _hash,
    _json, _local, _unsafe, _win32_path,
)

CONSENT_SECONDS = 300
_SHA = re.compile(r'[0-9a-f]{64}')
_DECIMAL = re.compile(r'[1-9][0-9]{0,18}')
_CAPABILITIES = frozenset(('fresh_work', 'fresh_auth', 'sql_write', 'recovery',
    'native_runtime', 'polza_dispatch', 'vikunja_dispatch', 'max_dispatch'))
_COMMITMENT_FIELDS = frozenset(('protocol', 'purpose', 'restore_id', 'epoch_id',
    'preparation_id', 'source_snapshot_sha256', 'binding_sha256', 'scope_sha256',
    'provider_observations_sha256', 'resource_scope_sha256', 'capabilities',
    'owner_policy_sha256'))
_POLICY_FIELDS = frozenset(('protocol', 'member_id', 'max_user_id',
    'vikunja_user_id', 'project_ids'))
_CODES = frozenset('business_owner_' + suffix for suffix in (
    'platform_unsupported', 'custody_invalid', 'not_enrolled', 'already_enrolled',
    'policy_invalid', 'consent_required', 'commitment_invalid', 'consent_invalid'))


class BusinessOwnerError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _CODES else 'business_owner_custody_invalid'
        super().__init__(self.code)


def _fail(code='business_owner_custody_invalid'):
    raise BusinessOwnerError(code) from None


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, TypeError, AttributeError):
        return False


def _identifier(value):
    return type(value) is str and bool(_DECIMAL.fullmatch(value)) and int(value) <= 2**63 - 1


def _sha(value):
    return type(value) is str and bool(_SHA.fullmatch(value))


def _raw(value):
    return _canonical(value)


def _io(path):
    return Path(_win32_path(path))


class _OwnerBodyError(Exception):
    def __init__(self, error):
        self.error = error


@contextmanager
def _owner_scope(manager):
    """Preserve our sanitized domain errors across legacy ValueError mapping.

    Actual custody errors from the Windows scope are still handled as failures.
    Only our already-sanitized body exception gets this non-ValueError wrapper.
    """
    try:
        with manager:
            try:
                yield
            except BusinessOwnerError as error:
                raise _OwnerBodyError(error) from None
    except _OwnerBodyError as wrapped:
        raise wrapped.error from None


@dataclass(frozen=True)
class OwnerPolicy:
    protocol: int
    member_id: str
    max_user_id: str
    vikunja_user_id: str
    project_ids: tuple[str, ...]

    def __post_init__(self):
        if (type(self.protocol) is not int or self.protocol != 1 or not _uuid(self.member_id)
                or not _identifier(self.max_user_id) or not _identifier(self.vikunja_user_id)
                or type(self.project_ids) is not tuple or not 1 <= len(self.project_ids) <= 10
                or any(not _identifier(item) for item in self.project_ids)
                or tuple(sorted(set(self.project_ids))) != self.project_ids):
            _fail('business_owner_policy_invalid')


def _policy(value):
    if type(value) is not OwnerPolicy:
        _fail('business_owner_policy_invalid')
    # Revalidate frozen DTO fields; object.__setattr__ is not an authority port.
    checked = OwnerPolicy(value.protocol, value.member_id, value.max_user_id,
        value.vikunja_user_id, value.project_ids)
    return dict(protocol=checked.protocol, member_id=checked.member_id,
        max_user_id=checked.max_user_id, vikunja_user_id=checked.vikunja_user_id,
        project_ids=list(checked.project_ids))


def _stored_policy(value):
    if (type(value) is not dict or set(value) != _POLICY_FIELDS
            or type(value['project_ids']) is not list):
        _fail()
    try:
        return _policy(OwnerPolicy(value['protocol'], value['member_id'], value['max_user_id'],
            value['vikunja_user_id'], tuple(value['project_ids'])))
    except BusinessOwnerError:
        _fail()


def _commitment(value, policy_sha):
    if (type(value) is not dict or set(value) != _COMMITMENT_FIELDS
            or type(value['protocol']) is not int or value['protocol'] != 1
            or type(value['purpose']) is not str or value['purpose'] != 'restore_activation_consent'
            or any(not _uuid(value[key]) for key in ('restore_id', 'epoch_id', 'preparation_id'))
            or any(not _sha(value[key]) for key in ('source_snapshot_sha256', 'binding_sha256',
                'scope_sha256', 'provider_observations_sha256', 'resource_scope_sha256', 'owner_policy_sha256'))
            or value['owner_policy_sha256'] != policy_sha
            or type(value['capabilities']) is not list or not 1 <= len(value['capabilities']) <= len(_CAPABILITIES)
            or any(type(item) is not str or item not in _CAPABILITIES for item in value['capabilities'])
            or sorted(set(value['capabilities'])) != value['capabilities']):
        _fail('business_owner_commitment_invalid')
    # Return owned canonical primitives, never a mutable caller binding.
    return {**value, 'capabilities': list(value['capabilities'])}


@dataclass(frozen=True)
class VerifiedBusinessOwnerConsent:
    """Authentication result only; constructing this DTO grants nothing."""
    owner_digest: str
    owner_policy_sha256: str
    consent_digest: str
    nonce_hash: str
    expires_at: int

    def __post_init__(self):
        if (any(not _sha(getattr(self, name)) for name in ('owner_digest',
                'owner_policy_sha256', 'consent_digest', 'nonce_hash'))
                or type(self.expires_at) is not int or not 0 < self.expires_at < 2**63):
            _fail('business_owner_consent_invalid')


class BusinessOwnerAuthority:
    def __init__(self, project_root: Path):
        try:
            self._initialize(project_root, _WindowsAdapter(), lambda: datetime.now(timezone.utc))
        except BusinessOwnerError:
            raise
        except OperatorError as error:
            _fail('business_owner_platform_unsupported' if error.code == 'operator_platform_unsupported'
                else 'business_owner_custody_invalid')
        except (OSError, ValueError, TypeError, UnicodeError):
            _fail()

    def _initialize(self, root, adapter, clock):
        self._root, self._adapter, self._clock = _local(root), adapter, clock
        _directories(self.root)
        info = _io(self.root).lstat()
        self._root_identity = (info.st_dev, info.st_ino)
        adapter.identity()
        self._anchor = self.root / '.runtime/team-business-owner'

    @property
    def root(self):
        return self._root

    @property
    def anchor(self):
        return self._anchor

    def _ensure_root(self):
        _directories(self.root)
        info = _io(self.root).lstat()
        if (info.st_dev, info.st_ino) != self._root_identity:
            _fail()

    def _scope_digest(self):
        self._ensure_root()
        facts = [[os.path.normcase(str(path)), _io(path).stat().st_dev,
            _io(path).stat().st_ino] for path in (self.root, self.anchor)]
        return _hash(_raw(['business-owner-scope-v1', facts]))

    def _now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            _fail()
        stamp = int(value.astimezone(timezone.utc).timestamp())
        if not 0 <= stamp < 2**63 - CONSENT_SECONDS:
            _fail()
        return stamp

    def _load(self):
        self._ensure_root()
        if not os.path.lexists(_io(self.anchor)):
            _fail('business_owner_not_enrolled')
        _directories(self.anchor)
        path = self.anchor / 'owner.json'
        with _owner_scope(self._adapter.scope(self.anchor)):
            info = _io(path).lstat()
            if _unsafe(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= LIMIT:
                _fail()
            with self._adapter.file(path) as stream:
                held = os.fstat(stream.fileno())
                if (held.st_dev, held.st_ino, held.st_nlink) != (info.st_dev, info.st_ino, 1):
                    _fail()
                outer = _json(stream.read(LIMIT + 1))
                if set(outer) != {'schema', 'protected'} or type(outer['schema']) is not int or outer['schema'] != 1 or type(outer['protected']) is not str:
                    _fail()
                encrypted = base64.b64decode(outer['protected'], validate=True)
                inner = _json(self._adapter.unprotect(encrypted))
                if (set(inner) != {'schema', 'enrollment_id', 'principal_digest', 'scope_digest',
                        'policy', 'policy_sha256', 'key'} or type(inner['schema']) is not int or inner['schema'] != 1
                        or not _uuid(inner['enrollment_id'])
                        or any(not _sha(inner[name]) for name in ('principal_digest', 'scope_digest', 'policy_sha256', 'key'))
                        or inner['principal_digest'] != _hash(self._adapter.identity().encode('utf8'))
                        or inner['scope_digest'] != self._scope_digest()):
                    _fail()
                policy = _stored_policy(inner['policy'])
                if inner['policy_sha256'] != _hash(_raw(policy)):
                    _fail()
                after = _io(path).lstat()
                if (after.st_dev, after.st_ino, after.st_nlink) != (held.st_dev, held.st_ino, 1):
                    _fail()
                key = bytes.fromhex(inner['key'])
                identity = {name: value for name, value in inner.items() if name != 'key'}
                identity['key_tag'] = _hash(b'business-owner-key-binding-v1\0' + key)
                return _hash(_raw(identity)), policy, inner['scope_digest'], key

    def _read(self):
        try:
            return self._load()
        except BusinessOwnerError:
            raise
        except (OperatorError, OSError, ValueError, TypeError, UnicodeError):
            _fail()

    @property
    def owner_digest(self):
        return self._read()[0]

    @property
    def owner_policy_sha256(self):
        return _hash(_raw(self._read()[1]))

    @staticmethod
    def _consent(digest, statement, *, reader, writer):
        challenge = secrets.token_hex(32)
        try:
            writer('digest ' + digest)
            writer('challenge ' + challenge)
            answer = reader('Type ' + statement + ' challenge digest: ')
        except (EOFError, OSError, KeyboardInterrupt):
            _fail('business_owner_consent_required')
        if type(answer) is not str or not hmac.compare_digest(answer, statement + ' ' + challenge + ' ' + digest):
            _fail('business_owner_consent_required')
        return challenge

    def enroll(self, policy, *, reader=input, writer=print):
        try:
            policy_value = _policy(policy)
            self._ensure_root()
            if os.path.lexists(_io(self.anchor)):
                _fail('business_owner_already_enrolled')
            principal = _hash(self._adapter.identity().encode('utf8'))
            enrollment = str(uuid4())
            plan = dict(protocol=1, purpose='enroll_current_business_owner', project_root=str(self.root),
                root_file_id=list(self._root_identity), principal_digest=principal,
                enrollment_id=enrollment, policy=policy_value)
            plan_sha = _hash(_raw(plan))
            with _owner_scope(self._adapter.root_scope(self.root)):
                self._ensure_root()
                writer('business-owner policy ' + _raw(policy_value).decode('utf8'))
                writer('enrollment scope ' + _raw({name: value for name, value in plan.items() if name != 'policy'}).decode('utf8'))
                self._consent(plan_sha, 'ENROLL BUSINESS OWNER', reader=reader, writer=writer)
                if principal != _hash(self._adapter.identity().encode('utf8')) or _policy(policy) != policy_value:
                    _fail()
                self._ensure_root()
                runtime = self.anchor.parent
                if not os.path.lexists(_io(runtime)):
                    self._adapter.mkdir(runtime)
                _directories(runtime)
                with _owner_scope(self._adapter.root_scope(runtime)):
                    self._adapter.mkdir(self.anchor)
                    with _owner_scope(self._adapter.scope(self.anchor)):
                        payload = dict(schema=1, enrollment_id=enrollment, principal_digest=principal,
                            scope_digest=self._scope_digest(), policy=policy_value,
                            policy_sha256=_hash(_raw(policy_value)), key=secrets.token_hex(32))
                        outer = dict(schema=1, protected=base64.b64encode(self._adapter.protect(_raw(payload))).decode('ascii'))
                        self._adapter.write_new(self.anchor / 'owner.json', _raw(outer))
                    return self.owner_digest
        except BusinessOwnerError:
            raise
        except (OperatorError, OSError, ValueError, TypeError, UnicodeError):
            _fail()

    def issue(self, commitment: dict, *, reader=input, writer=print):
        try:
            owner, policy, scope, _ = self._read()
            policy_sha = _hash(_raw(policy))
            value = _commitment(commitment, policy_sha)
            digest = _hash(_raw(value))
            writer('business-owner policy ' + _raw(policy).decode('utf8'))
            writer('purpose commitment ' + _raw(value).decode('utf8'))
            nonce = self._consent(digest, 'CONSENT restore_activation_consent', reader=reader, writer=writer)
            current_owner, current_policy, current_scope, key = self._read()
            if (current_owner != owner or current_policy != policy or current_scope != scope
                    or _commitment(commitment, policy_sha) != value):
                _fail('business_owner_consent_invalid')
            now = self._now()
            receipt = dict(schema=1, purpose='restore_activation_consent', owner_digest=owner,
                owner_policy_sha256=policy_sha, scope_digest=scope, commitment_digest=digest,
                nonce=nonce, created_at=now, expires_at=now + CONSENT_SECONDS)
            receipt['signature'] = hmac.new(key, b'business-owner-purpose-consent-v1\0' + _raw(receipt), hashlib.sha256).hexdigest()
            return receipt
        except BusinessOwnerError:
            raise
        except (OperatorError, OSError, ValueError, TypeError, UnicodeError):
            _fail('business_owner_consent_invalid')

    def authenticate(self, commitment: dict, receipt: dict):
        try:
            owner, policy, scope, key = self._read()
            policy_sha = _hash(_raw(policy))
            value = _commitment(commitment, policy_sha)
            fields = {'schema', 'purpose', 'owner_digest', 'owner_policy_sha256', 'scope_digest',
                'commitment_digest', 'nonce', 'created_at', 'expires_at', 'signature'}
            if (type(receipt) is not dict or set(receipt) != fields
                    or type(receipt['schema']) is not int or receipt['schema'] != 1
                    or type(receipt['purpose']) is not str or receipt['purpose'] != 'restore_activation_consent'
                    or any(not _sha(receipt[name]) for name in ('owner_digest', 'owner_policy_sha256',
                        'scope_digest', 'commitment_digest', 'nonce', 'signature'))
                    or any(type(receipt[name]) is not int for name in ('created_at', 'expires_at'))
                    or receipt['expires_at'] - receipt['created_at'] != CONSENT_SECONDS
                    or not 0 <= receipt['created_at'] <= self._now() < receipt['expires_at']
                    or receipt['owner_digest'] != owner or receipt['owner_policy_sha256'] != policy_sha
                    or receipt['scope_digest'] != scope or receipt['commitment_digest'] != _hash(_raw(value))):
                _fail('business_owner_consent_invalid')
            body = {name: value for name, value in receipt.items() if name != 'signature'}
            signature = hmac.new(key, b'business-owner-purpose-consent-v1\0' + _raw(body), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, receipt['signature']):
                _fail('business_owner_consent_invalid')
            return VerifiedBusinessOwnerConsent(owner, policy_sha, _hash(_raw(receipt)),
                _hash(receipt['nonce'].encode('ascii')), receipt['expires_at'])
        except BusinessOwnerError as error:
            if error.code in ('business_owner_commitment_invalid', 'business_owner_policy_invalid'):
                _fail('business_owner_consent_invalid')
            raise
        except (OperatorError, OSError, ValueError, TypeError, UnicodeError):
            _fail('business_owner_consent_invalid')


def _test_authority(project_root, *, adapter, clock):
    """Explicit private offline seam; not available from the production constructor."""
    authority = BusinessOwnerAuthority.__new__(BusinessOwnerAuthority)
    authority._initialize(project_root, adapter, clock)
    return authority
