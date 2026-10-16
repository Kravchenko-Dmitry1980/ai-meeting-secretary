"""Explicit offline first-owner bootstrap; never open an existing Team database.

The CLI's trusted root is this checkout. Tests inject a separate synthetic root
through the Python port; there is no command-line root override or env lookup.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager, ExitStack
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_CONFIG_BYTES = 32768
OWNER_FIELDS = frozenset(('id', 'display_name', 'role', 'enabled', 'max_user_id',
    'vikunja_user_id', 'person_profile_id', 'project_ids', 'revision'))
SIDECARS = ('-wal', '-shm', '-journal')


class BootstrapError(ValueError):
    """Only stable sanitized codes cross the CLI boundary."""


def _fail(code):
    raise BootstrapError(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('bootstrap_config_invalid')
        result[key] = value
    return result


def _absolute_local(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(c) < 32 for c in value) or value.startswith(('\\\\', '//'))):
        _fail('bootstrap_path_unsafe')
    pieces = re.split(r'[\\/]', value)
    if any(p in ('.', '..') or (p and p.rstrip(' .') != p) for p in pieces):
        _fail('bootstrap_path_unsafe')
    if any(':' in p for p in pieces[1:]) or (os.name != 'nt' and ':' in value):
        _fail('bootstrap_path_unsafe')
    path = Path(value)
    if not path.is_absolute():
        _fail('bootstrap_path_unsafe')
    return path


def _unsafe_entry(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _directory_chain(path):
    """Inspect lexically, before any resolve/open that could follow a junction."""
    existing = []
    for parent in (*reversed(path.parents), path):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if _unsafe_entry(info) or not stat.S_ISDIR(info.st_mode):
            _fail('bootstrap_path_unsafe')
        existing.append(parent)
    return existing


def _target_absent(target):
    for item in (target, *(target.with_name(target.name + suffix) for suffix in SIDECARS)):
        if os.path.lexists(item):
            _fail('bootstrap_target_exists')


def _owner(value):
    if not isinstance(value, dict) or set(value) != OWNER_FIELDS:
        _fail('bootstrap_config_invalid')
    if (value['role'] != 'owner' or value['enabled'] is not True
            or type(value['revision']) is not int or value['revision'] != 0
            or not isinstance(value['project_ids'], list) or not value['project_ids']):
        _fail('bootstrap_config_invalid')
    for identifier in (value['max_user_id'], value['vikunja_user_id'], *value['project_ids']):
        if (not isinstance(identifier, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', identifier)
                or int(identifier) > 2**63 - 1):
            _fail('bootstrap_config_invalid')
    if len(set(value['project_ids'])) != len(value['project_ids']):
        _fail('bootstrap_config_invalid')
    if str(PROJECT_ROOT / 'backend') not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / 'backend'))
    from secretary.domain.team import TeamMember
    try:
        return TeamMember.model_validate(value)
    except ValueError:
        _fail('bootstrap_config_invalid')


def _plan(raw_json, project_root):
    try:
        raw = raw_json.encode('utf-8') if isinstance(raw_json, str) else raw_json
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CONFIG_BYTES:
            _fail('bootstrap_config_invalid')
        config = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
            parse_constant=lambda value: _fail('bootstrap_config_invalid'))
    except (UnicodeError, ValueError, TypeError):
        _fail('bootstrap_config_invalid')
    if (not isinstance(config, dict) or set(config) != {'schema', 'project_root', 'target_team_database', 'owner'}
            or type(config['schema']) is not int or config['schema'] != 1):
        _fail('bootstrap_config_invalid')
    root = _absolute_local(str(project_root))
    supplied_root = _absolute_local(config['project_root'])
    if os.path.normcase(str(root)) != os.path.normcase(str(supplied_root)):
        _fail('bootstrap_project_root_mismatch')
    if not root.is_dir():
        _fail('bootstrap_path_unsafe')
    target = _absolute_local(config['target_team_database'])
    if os.path.normcase(str(target.parent)) != os.path.normcase(str(root / '.runtime/team')):
        _fail('bootstrap_path_unsafe')
    stem = target.stem
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.sqlite3?', target.name)
            or re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', stem)):
        _fail('bootstrap_path_unsafe')
    _directory_chain(target.parent)
    _target_absent(target)
    if target.parent.is_dir():
        _assert_private_directory(target.parent)
    owner = _owner(config['owner'])
    normalized = dict(schema=1, project_root=str(root), target_team_database=str(target),
        owner=owner.model_dump(mode='json'))
    digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return root, target, owner, digest


@contextmanager
def _pin_directory(path, *, leaf=False):
    """Hold Windows ancestors without FILE_SHARE_DELETE through publication."""
    _directory_chain(path)
    if os.name != 'nt':
        before = path.stat()
        yield
        after = path.lstat()
        if _unsafe_entry(after) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            _fail('bootstrap_path_unsafe')
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Include FILE_LIST_DIRECTORY: metadata-only READ_ATTRIBUTES handles do
    # not participate in Windows rename sharing checks for the directory.
    # Ancestors have pinned descendants; the staging leaf additionally needs
    # LIST_DIRECTORY so its own rename cannot detach the SQLite path.
    access = 0x81 if leaf else 0x80
    handle = kernel.CreateFileW(str(path), access, 1, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        _fail('bootstrap_path_unsafe')
    try:
        # OPEN_REPARSE_POINT above prevents opening the referent of a race.
        _directory_chain(path)
        yield
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def _windows_private_security():
    """Current process token only; never discover an account or load a key."""
    from ctypes import wintypes
    class SecurityAttributes(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', wintypes.BOOL)]
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    token, sid, descriptor = wintypes.HANDLE(), wintypes.LPWSTR(), ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            _fail('bootstrap_private_path_failed')
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            _fail('bootstrap_private_path_failed')
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            _fail('bootstrap_private_path_failed')
        user_sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(user_sid, ctypes.byref(sid)):
            _fail('bootstrap_private_path_failed')
        sddl = f'O:{sid.value}D:P(A;OICI;FA;;;{sid.value})'
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            _fail('bootstrap_private_path_failed')
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        yield advapi, kernel, user_sid, attributes
    finally:
        if descriptor: kernel.LocalFree(descriptor)
        if sid: kernel.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
        if token: kernel.CloseHandle(token)


def _create_private_directory(path):
    """New paths only: protected current-user ACL, no process/ACL repair port."""
    if os.name != 'nt':
        path.mkdir(mode=0o700)
        return
    with _windows_private_security() as (_, kernel, _, attributes):
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            _fail('bootstrap_private_path_failed')


def _assert_private_directory(path):
    """Do not place future SQLite sidecars in a shared or foreign-owned folder."""
    _directory_chain(path)
    if os.name != 'nt':
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            _fail('bootstrap_private_path_failed')
        return
    from ctypes import wintypes
    class ACL(ctypes.Structure):
        _fields_ = [('revision', ctypes.c_ubyte), ('sbz1', ctypes.c_ubyte), ('size', wintypes.WORD),
            ('ace_count', wintypes.WORD), ('sbz2', wintypes.WORD)]
    class AccessAllowedACE(ctypes.Structure):
        _fields_ = [('type', ctypes.c_ubyte), ('flags', ctypes.c_ubyte), ('size', wintypes.WORD),
            ('mask', wintypes.DWORD)]
    with _windows_private_security() as (advapi, kernel, user_sid, _):
        advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD)]
        advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
        advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        advapi.GetAce.restype = wintypes.BOOL
        advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        advapi.EqualSid.restype = wintypes.BOOL
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            if advapi.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None,
                    ctypes.byref(dacl), None, ctypes.byref(descriptor)):
                _fail('bootstrap_private_path_failed')
            control, revision, ace = wintypes.WORD(), wintypes.DWORD(), ctypes.c_void_p()
            if (not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
                    or not control.value & 0x1000 or not dacl or not advapi.EqualSid(owner, user_sid)
                    or ctypes.cast(dacl, ctypes.POINTER(ACL)).contents.ace_count != 1
                    or not advapi.GetAce(dacl, 0, ctypes.byref(ace))):
                _fail('bootstrap_private_path_failed')
            grant = ctypes.cast(ace, ctypes.POINTER(AccessAllowedACE)).contents
            if (grant.type != 0 or grant.flags & 3 != 3 or grant.flags & 8
                    or grant.mask != 0x001F01FF or not advapi.EqualSid(ctypes.c_void_p(ace.value + 8), user_sid)):
                _fail('bootstrap_private_path_failed')
        finally:
            if descriptor: kernel.LocalFree(descriptor)


def _create_database(path, owner):
    from secretary.infrastructure.team_database import TeamDatabase
    from secretary.infrastructure.team_repository import TeamRepository
    repository = TeamRepository(TeamDatabase(path))
    repository.upsert_member(owner, expected_revision=None)
    if repository.get_member(owner.id) != owner:
        _fail('bootstrap_database_invalid')


def _verify_database(path, owner):
    with closing(sqlite3.connect(path)) as conn:
        if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
            _fail('bootstrap_database_invalid')
        if conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
            _fail('bootstrap_database_invalid')
        _inspect_database(conn, owner)
    with path.open('rb+') as durable:
        os.fsync(durable.fileno())


def _inspect_database(conn, owner):
    from secretary.infrastructure.team_database import SCHEMA_VERSION
    if (conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
            or conn.execute('PRAGMA foreign_key_check').fetchall()
            or conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] != SCHEMA_VERSION):
        _fail('bootstrap_database_invalid')
    members = conn.execute('SELECT payload FROM team_members').fetchall()
    if len(members) != 1 or json.loads(members[0][0]) != owner.model_dump(mode='json'):
        _fail('bootstrap_database_invalid')
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    for (table,) in tables:
        if table not in {'team_schema', 'team_members', 'sqlite_sequence'}:
            if conn.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]:
                _fail('bootstrap_database_invalid')


def _verify_readonly(path, owner):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        _inspect_database(conn, owner)


def _publish_no_overwrite(source, target):
    try:
        # Atomic same-volume publication, refusing an existing name. A link
        # creates no second DB content and retains the protected file ACL.
        # Keep all directory pins; unlink the owned staging alias in cleanup.
        os.link(source, target)
    except FileExistsError:
        _fail('bootstrap_target_exists')


@contextmanager
def _pin_file(path, identity, *, delete=False):
    """Hold the exact completed leaf against write/delete through publication."""
    if os.name != 'nt':
        _fail('bootstrap_platform_unsupported')
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    access = 0x80010000 if delete else 0x80000000
    handle = kernel.CreateFileW(str(path), access, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        _fail('bootstrap_file_pin_failed')
    fd = None
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        info = os.fstat(fd)
        if (_unsafe_entry(info) or not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != identity):
            _fail('bootstrap_cleanup_refused')
        yield fd
    finally:
        if fd is not None: os.close(fd)
        else: kernel.CloseHandle(handle)


def _delete_pinned_file(fd):
    """Delete the opened owned alias by handle, never by a rescanned filename."""
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
        ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    disposition = wintypes.BOOL(True)
    if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(fd), 4,
            ctypes.byref(disposition), ctypes.sizeof(disposition)):
        _fail('bootstrap_cleanup_refused')


def _cleanup_owned(stage, identity, *, published_target=None, owned_files=None):
    if not stage.exists():
        return
    info = stage.lstat()
    if _unsafe_entry(info) or (info.st_dev, info.st_ino) != identity:
        _fail('bootstrap_cleanup_refused')
    with _pin_directory(stage, leaf=True):
        info = stage.lstat()
        if (info.st_dev, info.st_ino) != identity:
            _fail('bootstrap_cleanup_refused')
        allowed = owned_files or {}
        entries = list(stage.iterdir())
        for entry in entries:
            info = entry.lstat()
            published_alias = False
            if published_target is not None and entry.name == 'team.sqlite3' and info.st_nlink == 2:
                target_info = published_target.lstat()
                published_alias = (not _unsafe_entry(target_info) and target_info.st_nlink == 2
                    and (info.st_dev, info.st_ino) == (target_info.st_dev, target_info.st_ino))
            if (entry.name not in allowed or _unsafe_entry(info) or not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != allowed[entry.name]
                    or (info.st_nlink != 1 and not published_alias)):
                _fail('bootstrap_cleanup_refused')
        for entry in entries:
            with _pin_file(entry, allowed[entry.name], delete=True) as fd:
                _delete_pinned_file(fd)
    info = stage.lstat()
    if _unsafe_entry(info) or (info.st_dev, info.st_ino) != identity:
        _fail('bootstrap_cleanup_refused')
    stage.rmdir()


def bootstrap_owner(raw_json, *, project_root, apply=False):
    """Explicit trusted root port; no settings, discovery, HTTP or runtime start."""
    if type(apply) is not bool:
        _fail('bootstrap_config_invalid')
    root, target, owner, digest = _plan(raw_json, project_root)
    result = dict(state='preview', code='owner_bootstrap_ready', owner_uuid=owner.id, config_digest=digest)
    if not apply:
        return result
    try:
        with ExitStack() as pinned:
            for path in _directory_chain(target.parent):
                pinned.enter_context(_pin_directory(path))
            for path in (root / '.runtime', target.parent):
                with _pin_directory(path.parent, leaf=True):
                    if not os.path.lexists(path):
                        _create_private_directory(path)
                    # Inspect and pin concurrent entries before descendants.
                    _directory_chain(path)
                    pinned.enter_context(_pin_directory(path))
            _assert_private_directory(target.parent)
            _target_absent(target)
            stage = target.parent / ('.owner-bootstrap-' + uuid4().hex)
            stage_pins = ExitStack()
            with _pin_directory(target.parent, leaf=True):
                _create_private_directory(stage)
                info = stage.lstat()
                identity = (info.st_dev, info.st_ino)
                if _unsafe_entry(info) or not stat.S_ISDIR(info.st_mode):
                    _fail('bootstrap_path_unsafe')
                stage_pins.enter_context(_pin_directory(stage, leaf=True))
            try:
                published = False
                owned_files = {}
                try:
                    _assert_private_directory(stage)
                    path = stage / 'team.sqlite3'
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
                    fd = os.open(path, flags, 0o600)
                    os.close(fd)
                    before = path.lstat()
                    owned_files[path.name] = (before.st_dev, before.st_ino)
                    _create_database(path, owner)
                    _verify_database(path, owner)
                    after = path.lstat()
                    if (_unsafe_entry(after) or not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
                            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                            or any(os.path.lexists(path.with_name(path.name + suffix)) for suffix in SIDECARS)):
                        _fail('bootstrap_database_invalid')
                    with _pin_file(path, owned_files[path.name]) as fd:
                        if os.fstat(fd).st_nlink != 1:
                            _fail('bootstrap_database_invalid')
                        _verify_readonly(path, owner)
                        _directory_chain(target.parent)
                        _target_absent(target)
                        _publish_no_overwrite(path, target)
                        published = True
                        result.update(state='created', code='owner_bootstrap_created')
                finally:
                    # The leaf stage protects all ancestors during SQL/link.
                    # Re-pin the parent strongly before releasing that leaf.
                    with _pin_directory(target.parent, leaf=True):
                        stage_pins.close()
                        try:
                            _cleanup_owned(stage, identity, published_target=target if published else None,
                                owned_files=owned_files)
                        except (BootstrapError, OSError):
                            if not published:
                                raise
                            result['code'] = 'owner_bootstrap_created_cleanup_required'
            finally:
                stage_pins.close()
    except BootstrapError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        _fail('bootstrap_apply_failed')
    return result


def _read_config(path, root):
    path = _absolute_local(str(path))
    if not path.is_relative_to(root) or path.suffix.lower() != '.json':
        _fail('bootstrap_config_path_invalid')
    _directory_chain(path.parent)
    info = path.lstat()
    if _unsafe_entry(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_CONFIG_BYTES:
        _fail('bootstrap_config_path_invalid')
    with path.open('rb') as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            _fail('bootstrap_config_path_invalid')
        return source.read(MAX_CONFIG_BYTES + 1)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        _fail('bootstrap_arguments_invalid')


def main(arguments=None, *, project_root=None):
    parser = _SafeParser(description='Preview a new offline first-owner Team DB; no secrets in JSON.')
    parser.add_argument('--config', required=True, help='Explicit absolute project-local nonsecret schema1 JSON file')
    parser.add_argument('--apply', action='store_true', help='Create only a fresh nonexistent Team DB')
    try:
        args = parser.parse_args(arguments)
        root = _absolute_local(str(project_root if project_root is not None else PROJECT_ROOT))
        raw = _read_config(args.config, root)
        result = bootstrap_owner(raw, project_root=root, apply=args.apply)
    except BootstrapError as error:
        print(json.dumps({'state': 'rejected', 'code': str(error)}, separators=(',', ':')))
        return 2
    except (OSError, ValueError):
        print(json.dumps({'state': 'rejected', 'code': 'bootstrap_input_unavailable'}, separators=(',', ':')))
        return 2
    print(json.dumps(result, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
