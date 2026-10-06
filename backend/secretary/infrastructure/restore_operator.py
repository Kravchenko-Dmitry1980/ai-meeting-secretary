"""Independent Windows local-operator consent, never restored owner authority.

CurrentUser DPAPI/private ACL custody is trusted against other accounts, not
malicious processes already running as the same user. Explicit local enrollment
is a new operator policy; it does not prove a Team business-owner identity.
Authentication does not consume a nonce or authorize activation. R3's independent
control transaction owns consumption. Imports open no stores or native handles.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager, ExitStack
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from uuid import UUID, uuid4

LIMIT = 128 * 1024
APPROVAL_SECONDS = 300
_SHA = re.compile('[0-9a-f]{64}')


class OperatorError(ValueError):
    def __init__(self, code):
        self.code = code if code in {
            'operator_platform_unsupported', 'operator_custody_invalid', 'operator_not_enrolled',
            'operator_already_enrolled', 'operator_consent_required', 'operator_commitment_invalid',
            'operator_approval_invalid',
        } else 'operator_custody_invalid'
        super().__init__(self.code)


def _fail(code='operator_custody_invalid'):
    raise OperatorError(code) from None


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value, *, code='operator_commitment_invalid'):
    remaining = 4096
    def visit(child, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 24:
            _fail(code)
        if type(child) is dict:
            for key, nested in child.items():
                if type(key) is not str:
                    _fail(code)
                visit(key, depth + 1)
                visit(nested, depth + 1)
        elif type(child) is list:
            for nested in child:
                visit(nested, depth + 1)
        elif type(child) is str and len(child) > LIMIT:
            _fail(code)
        elif type(child) is float and not math.isfinite(child):
            _fail(code)
        elif child is not None and type(child) not in (str, int, float, bool):
            _fail(code)
    try:
        visit(value, 0)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(raw) > LIMIT:
            _fail(code)
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail(code)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= LIMIT:
            _fail()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=lambda _: _fail())
        if type(value) is not dict:
            _fail()
        _canonical(value, code='operator_custody_invalid')
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _unsafe(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _local(path):
    raw = str(path)
    if (not raw or len(raw) > 4096 or raw.startswith(('\\\\', '//')) or any(ord(c) < 32 for c in raw)
            or any(p in ('.', '..') or p.rstrip(' .') != p for p in re.split(r'[\\/]', raw) if p)
            or any(':' in p for p in re.split(r'[\\/]', raw)[1:])):
        _fail()
    result = Path(path)
    if not result.is_absolute():
        _fail()
    return result


def _win32_path(path):
    """Internal Win32 spelling after public local-path validation only.

    Extended/device/UNC spellings remain forbidden public inputs. Converting a
    validated drive-local Path here supports MAX_PATH without host policy or
    registry changes and leaves all public paths/commitments unchanged.
    """
    local = _local(path)
    raw = str(local)
    if (len(local.drive) != 2 or local.drive[1] != ':'
            or raw.replace('/', '\\').startswith('\\\\')):
        _fail()
    return '\\\\?\\' + raw


def _directories(path):
    for item in (*reversed(path.parents), path):
        info = Path(_win32_path(item)).lstat()
        if _unsafe(info) or not stat.S_ISDIR(info.st_mode):
            _fail()


@contextmanager
def _security(*, directory=True):
    """Current TokenUser and explicit security descriptor; never default owner."""
    if os.name != 'nt':
        _fail('operator_platform_unsupported')
    from ctypes import wintypes as w
    class Attributes(ctypes.Structure):
        _fields_ = [('length', w.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', w.BOOL)]
    advapi, kernel = ctypes.WinDLL('advapi32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    token, sid, descriptor = w.HANDLE(), w.LPWSTR(), ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            _fail()
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            _fail()
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            _fail()
        user = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(user, ctypes.byref(sid)):
            _fail()
        flags = 'OICI' if directory else ''
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f'O:{sid.value}D:P(A;{flags};FA;;;{sid.value})', 1, ctypes.byref(descriptor), None):
            _fail()
        yield advapi, kernel, user, sid.value, Attributes(ctypes.sizeof(Attributes), descriptor, False)
    finally:
        if descriptor: kernel.LocalFree(descriptor)
        if sid: kernel.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
        if token: kernel.CloseHandle(token)


def _private_handle(handle, *, allow_default_owner=False):
    """Return private/protected facts by held handle, never reparsed pathname."""
    from ctypes import wintypes as w
    class ACL(ctypes.Structure):
        _fields_ = [('revision', ctypes.c_ubyte), ('sbz', ctypes.c_ubyte), ('size', w.WORD), ('count', w.WORD), ('sbz2', w.WORD)]
    class ACE(ctypes.Structure):
        _fields_ = [('kind', ctypes.c_ubyte), ('flags', ctypes.c_ubyte), ('size', w.WORD), ('mask', w.DWORD)]
    with _security() as (advapi, kernel, user, _, _):
        advapi.GetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)]
        advapi.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            if advapi.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)):
                _fail()
            control, revision, ace = w.WORD(), w.DWORD(), ctypes.c_void_p()
            if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                _fail()
            owner_matches = bool(advapi.EqualSid(owner, user))
            if not owner_matches and allow_default_owner:
                # Only exact TokenOwner, never arbitrary group membership. The
                # caller requires a pinned protected TokenUser-private ancestor.
                token, size = w.HANDLE(), w.DWORD()
                try:
                    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
                        _fail()
                    advapi.GetTokenInformation(token, 4, None, 0, ctypes.byref(size))
                    if not 0 < size.value <= 65536:
                        _fail()
                    buffer = ctypes.create_string_buffer(size.value)
                    if not advapi.GetTokenInformation(token, 4, buffer, size, ctypes.byref(size)):
                        _fail()
                    default_owner = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
                    owner_matches = bool(advapi.EqualSid(owner, default_owner))
                finally:
                    if token: kernel.CloseHandle(token)
            if (not dacl or not owner_matches or ctypes.cast(dacl, ctypes.POINTER(ACL)).contents.count != 1
                    or not advapi.GetAce(dacl, 0, ctypes.byref(ace))):
                return False, False
            grant = ctypes.cast(ace, ctypes.POINTER(ACE)).contents
            private = (grant.kind == 0 and not grant.flags & 8 and grant.mask == 0x001F01FF
                       and advapi.EqualSid(ctypes.c_void_p(ace.value + 8), user))
            return bool(private), bool(control.value & 0x1000)
        finally:
            if descriptor: kernel.LocalFree(descriptor)


@contextmanager
def _directory_handle(path, *, leaf):
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateFileW(_win32_path(path), 0x20081 if leaf else 0x20080, 1, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        _fail()
    try:
        yield handle
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def _directory_pins(path):
    if os.name != 'nt':
        _fail('operator_platform_unsupported')
    try:
        path = _local(path)
        _directories(path)
        with ExitStack() as stack:
            handles = [(item, stack.enter_context(_directory_handle(item, leaf=item == path)))
                       for item in (*reversed(path.parents), path)]
            _directories(path)
            yield handles
            _directories(path)
    except OperatorError:
        raise
    except (OSError, ValueError, TypeError):
        _fail()


@contextmanager
def _protected_scope(path, *, strict=False):
    path = _local(path)
    with _directory_pins(path) as handles:
        def check():
            protected_ancestor = False
            for item, handle in handles:
                private, protected = _private_handle(handle, allow_default_owner=item == path and not strict and protected_ancestor)
                if item == path and (not private or strict and not protected or not protected and not protected_ancestor):
                    _fail()
                protected_ancestor = protected_ancestor or private and protected
        check()
        yield
        check()


def protected_scope(path):
    """Pin private custody; inherited sole-TokenUser descendants are supported."""
    return _protected_scope(path)


def create_private_directory(path):
    """Explicit new directory only; never repair/adopt an existing ACL."""
    path = _local(path)
    with _directory_pins(path.parent):
        with _security() as (_, kernel, _, _, attributes):
            from ctypes import wintypes as w
            kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.c_void_p]
            if not kernel.CreateDirectoryW(_win32_path(path), ctypes.byref(attributes)):
                _fail()
        with _protected_scope(path, strict=True):
            pass


class _WindowsAdapter:
    def identity(self):
        with _security() as (_, _, _, sid, _):
            return sid

    def mkdir(self, path):
        create_private_directory(path)

    def scope(self, path):
        return _protected_scope(path, strict=True)

    def root_scope(self, path):
        return _directory_pins(path)

    def crypt(self, value, *, decrypt):
        if type(value) is not bytes or not 0 < len(value) <= LIMIT:
            _fail()
        from ctypes import wintypes as w
        class Blob(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        incoming, outgoing = Blob(len(value), buffer), Blob()
        crypt, kernel = ctypes.WinDLL('crypt32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.POINTER(Blob)]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            _fail()
        try:
            if not 0 < outgoing.size <= LIMIT:
                _fail()
            return ctypes.string_at(outgoing.data, outgoing.size)
        finally:
            kernel.LocalFree(outgoing.data)

    def protect(self, value):
        return self.crypt(value, decrypt=False)

    def unprotect(self, value):
        return self.crypt(value, decrypt=True)

    def write_new(self, path, data):
        import msvcrt
        from ctypes import wintypes as w
        with _security(directory=False) as (_, kernel, _, _, attributes):
            kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
            kernel.CreateFileW.restype = w.HANDLE
            handle = kernel.CreateFileW(_win32_path(path), 0xC0020000, 0, ctypes.byref(attributes), 1, 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                _fail('operator_already_enrolled')
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            except Exception:
                kernel.CloseHandle(handle)
                _fail()
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())

    @contextmanager
    def file(self, path):
        import msvcrt
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.CreateFileW(_win32_path(path), 0x80020000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            _fail()
        try:
            if _private_handle(handle) != (True, True):
                _fail()
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(fd, 'rb') as stream:
            yield stream
            if _private_handle(handle) != (True, True):
                _fail()


@dataclass(frozen=True)
class VerifiedOperatorApproval:
    operator_digest: str
    approval_digest: str
    nonce_hash: str
    expires_at: int

    def __post_init__(self):
        if (any(type(value) is not str or not _SHA.fullmatch(value) for value in
                (self.operator_digest, self.approval_digest, self.nonce_hash))
                or type(self.expires_at) is not int or not 0 < self.expires_at < 2**63):
            _fail('operator_approval_invalid')


class OperatorAuthority:
    def __init__(self, project_root: Path):
        try:
            self._initialize(project_root, _WindowsAdapter(), lambda: datetime.now(timezone.utc))
        except OperatorError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            _fail()

    def _initialize(self, root, adapter, clock):
        self._root, self._adapter, self._clock = _local(root), adapter, clock
        _directories(self.root)
        info = self.root.lstat()
        self._root_identity = (info.st_dev, info.st_ino)
        adapter.identity()
        self._anchor = self.root / '.runtime/team-operator'

    @property
    def root(self):
        return self._root

    @property
    def anchor(self):
        return self._anchor

    def _scope_digest(self):
        self._ensure_root()
        roots = [[os.path.normcase(str(path)), path.stat().st_dev, path.stat().st_ino] for path in (self.root, self.anchor)]
        return _hash(_canonical(['restore-operator-scope-v1', roots]))

    def _ensure_root(self):
        _directories(self.root)
        info = self.root.lstat()
        if (info.st_dev, info.st_ino) != self._root_identity:
            _fail()

    def _now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            _fail()
        return int(value.astimezone(timezone.utc).timestamp())

    def _load(self):
        self._ensure_root()
        if not self.anchor.exists():
            _fail('operator_not_enrolled')
        _directories(self.anchor)
        path = self.anchor / 'operator.json'
        with self._adapter.scope(self.anchor):
            info = path.lstat()
            if _unsafe(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= LIMIT:
                _fail()
            with self._adapter.file(path) as stream:
                held = os.fstat(stream.fileno())
                if (held.st_dev, held.st_ino, held.st_nlink) != (info.st_dev, info.st_ino, 1):
                    _fail()
                outer = _json(stream.read(LIMIT + 1))
                if set(outer) != {'schema', 'protected'} or type(outer['schema']) is not int or outer['schema'] != 1 or type(outer['protected']) is not str:
                    _fail()
                try:
                    encrypted = base64.b64decode(outer['protected'], validate=True)
                except (ValueError, TypeError):
                    _fail()
                inner = _json(self._adapter.unprotect(encrypted))
                if (set(inner) != {'schema', 'enrollment_id', 'principal_digest', 'scope_digest', 'key'}
                        or type(inner['schema']) is not int or inner['schema'] != 1
                        or type(inner['enrollment_id']) is not str or str(UUID(inner['enrollment_id'])) != inner['enrollment_id']
                        or any(type(inner[k]) is not str or not _SHA.fullmatch(inner[k]) for k in ('principal_digest', 'scope_digest', 'key'))
                        or inner['principal_digest'] != _hash(self._adapter.identity().encode('utf-8'))
                        or inner['scope_digest'] != self._scope_digest()):
                    _fail()
                after = path.lstat()
                if (after.st_dev, after.st_ino, after.st_nlink) != (held.st_dev, held.st_ino, 1):
                    _fail()
                identity = {k: value for k, value in inner.items() if k != 'key'}
                return _hash(_canonical(identity)), bytes.fromhex(inner['key'])

    @property
    def operator_digest(self):
        try:
            return self._load()[0]
        except OperatorError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            _fail()

    @staticmethod
    def _consent(digest, *, reader, writer):
        challenge = secrets.token_hex(32)
        try:
            writer('digest ' + digest)
            writer('challenge ' + challenge)
            answer = reader('Type challenge digest: ')
        except (EOFError, OSError, KeyboardInterrupt):
            _fail('operator_consent_required')
        if type(answer) is not str or not hmac.compare_digest(answer, challenge + ' ' + digest):
            _fail('operator_consent_required')
        return challenge

    def enroll(self, *, reader=input, writer=print):
        try:
            self._ensure_root()
            if os.path.lexists(self.anchor):
                _fail('operator_already_enrolled')
            principal = _hash(self._adapter.identity().encode('utf-8'))
            enrollment = str(uuid4())
            plan = _hash(_canonical(['restore-operator-enroll-v1', str(self.root), principal, enrollment]))
            self._consent(plan, reader=reader, writer=writer)
            if principal != _hash(self._adapter.identity().encode('utf-8')):
                _fail()
            self._ensure_root()
            with self._adapter.root_scope(self.root):
                self._ensure_root()
                runtime = self.anchor.parent
                if not runtime.exists():
                    self._adapter.mkdir(runtime)
                _directories(runtime)
                self._adapter.mkdir(self.anchor)
                with self._adapter.scope(self.anchor):
                    payload = dict(schema=1, enrollment_id=enrollment, principal_digest=principal,
                                   scope_digest=self._scope_digest(), key=secrets.token_hex(32))
                    outer = dict(schema=1, protected=base64.b64encode(self._adapter.protect(_canonical(payload))).decode('ascii'))
                    self._adapter.write_new(self.anchor / 'operator.json', _canonical(outer))
            return self.operator_digest
        except OperatorError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            _fail()

    def issue(self, commitment: dict, *, reader=input, writer=print):
        try:
            operator, _ = self._load()
            if type(commitment) is not dict or not commitment:
                _fail('operator_commitment_invalid')
            digest = _hash(_canonical(commitment))
            nonce = self._consent(digest, reader=reader, writer=writer)
            operator_after, key = self._load()
            if operator_after != operator or digest != _hash(_canonical(commitment)):
                _fail('operator_approval_invalid')
            now = self._now()
            receipt = dict(schema=1, operator_digest=operator, commitment_digest=digest, nonce=nonce,
                           created_at=now, expires_at=now + APPROVAL_SECONDS)
            receipt['signature'] = hmac.new(key, b'restore-operator-approval-v1\0' + _canonical(receipt), hashlib.sha256).hexdigest()
            return receipt
        except OperatorError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            _fail()

    def authenticate(self, commitment: dict, receipt: dict):
        try:
            operator, key = self._load()
            fields = {'schema', 'operator_digest', 'commitment_digest', 'nonce', 'created_at', 'expires_at', 'signature'}
            if (type(commitment) is not dict or not commitment or type(receipt) is not dict or set(receipt) != fields
                    or type(receipt['schema']) is not int or receipt['schema'] != 1
                    or any(type(receipt[k]) is not str or not _SHA.fullmatch(receipt[k]) for k in ('operator_digest', 'commitment_digest', 'nonce', 'signature'))
                    or any(type(receipt[k]) is not int for k in ('created_at', 'expires_at'))
                    or receipt['expires_at'] - receipt['created_at'] != APPROVAL_SECONDS
                    or not 0 <= receipt['created_at'] <= self._now() < receipt['expires_at']
                    or receipt['operator_digest'] != operator
                    or receipt['commitment_digest'] != _hash(_canonical(commitment))):
                _fail('operator_approval_invalid')
            body = {k: v for k, v in receipt.items() if k != 'signature'}
            signature = hmac.new(key, b'restore-operator-approval-v1\0' + _canonical(body), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, receipt['signature']):
                _fail('operator_approval_invalid')
            return VerifiedOperatorApproval(operator, _hash(_canonical(receipt)), _hash(receipt['nonce'].encode('ascii')), receipt['expires_at'])
        except OperatorError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            _fail('operator_approval_invalid')


def _test_authority(project_root, *, adapter, clock):
    """Internal synthetic fixture seam. Production constructor exposes no bypass."""
    value = object.__new__(OperatorAuthority)
    value._initialize(project_root, adapter, clock)
    return value
