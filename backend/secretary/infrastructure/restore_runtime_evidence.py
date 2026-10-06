"""Read-only proof that the explicit original managed runtime has stopped.

This is a trusted same-user CLI boundary, not a host-wide writer census. It
neither creates a maintenance registry nor repairs/retires tickets or processes.
The caller binds these exact evidence paths outside its archive and target.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote
from uuid import UUID

from secretary.infrastructure.team_process_identity import DeadProcessProof, verify_dead_identity


class RuntimeEvidenceError(ValueError):
    def __init__(self, code='restore_runtime_evidence_invalid'):
        self.code = code
        super().__init__(code)


_SHA = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_ROLES = {'secretary', 'team', 'billing', 'vikunja'}
_IDENTITY = {'pid', 'creation_time', 'executable_sha256', 'argv_sha256'}
_TABLES = {
    'maintenance_state': ('id', 'deployment_id', 'mode', 'fence', 'barrier_id', 'owner_id', 'native_evidence'),
    'maintenance_participants': ('participant_id', 'run_id', 'identity', 'guard_version'),
    'maintenance_sources': ('role', 'path'),
    'maintenance_tickets': ('id', 'participant_id', 'run_id', 'kind', 'operation_id', 'fence', 'state', 'created_at'),
    'maintenance_events': ('id', 'kind', 'barrier_id', 'fence', 'created_at'),
    'maintenance_recovery_checkpoints': ('id', 'payload', 'recovery_ticket_id', 'dead_proof', 'created_at'),
    'maintenance_native_containment': ('participant_id', 'run_id', 'identity', 'containment_version', 'recorded_at'),
}
_FILE_LIMIT = 64 * 1024 * 1024
_JSON_LIMIT = 128 * 1024
_TOTAL_LIMIT = 16 * 1024 * 1024


def _fail(code='restore_runtime_evidence_invalid'):
    raise RuntimeEvidenceError(code)


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        _fail()
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail()
        result[key] = value
    return result


def _json(value):
    if not isinstance(value, (str, bytes)) or len(value) > _JSON_LIMIT:
        _fail()
    result = json.loads(value, object_pairs_hook=_pairs,
                        parse_constant=lambda _: _fail())
    if type(result) is not dict:
        _fail()
    return result


def _path(value, root=None, *, file=False):
    raw = os.fspath(value)
    if (type(raw) is not str or not Path(raw).is_absolute() or raw.startswith(('\\\\', '//'))
            or any(ord(c) < 32 for c in raw) or '..' in Path(raw).parts
            or any(':' in part for part in Path(raw).parts[1:])):
        _fail('restore_runtime_path_invalid')
    path = Path(os.path.abspath(raw))
    if root is not None and not path.is_relative_to(root):
        _fail('restore_runtime_path_invalid')
    for candidate in (path, *path.parents):
        if candidate.exists():
            info = candidate.lstat()
            if candidate.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                _fail('restore_runtime_path_invalid')
    if file:
        info = path.stat()
        if not path.is_file() or info.st_nlink != 1:
            _fail('restore_runtime_path_invalid')
    return path


@contextmanager
def _pin(path):
    """Retain a read-only, no-write/no-delete Windows file handle."""
    if os.name != 'nt':
        # Only _verify's explicit synthetic seam can run on another platform.
        with path.open('rb'):
            yield
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        _fail('restore_runtime_custody_unavailable')
    try:
        _path(path, file=True)
        yield
    finally:
        kernel.CloseHandle(handle)


def _read_bytes(path, limit):
    if path.stat().st_size > limit:
        _fail('restore_runtime_evidence_bounded')
    with path.open('rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        _fail('restore_runtime_evidence_bounded')
    return value


def _file_digest(path):
    if path.stat().st_size > _FILE_LIMIT:
        _fail('restore_runtime_evidence_bounded')
    digest, total = hashlib.sha256(), 0
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > _FILE_LIMIT:
                _fail('restore_runtime_evidence_bounded')
            digest.update(block)
    return digest.hexdigest()


def _files(control, lifecycle):
    values = {'maintenance': _file_digest(control), 'lifecycle': _file_digest(lifecycle)}
    for suffix in ('-wal', '-journal'):
        path = Path(str(control) + suffix)
        values[suffix] = _file_digest(path) if path.exists() else None
    # Reader lock bookkeeping is not database content, but presence changes
    # must not silently turn an inactive immutable read into a live-WAL read.
    values['-shm'] = Path(str(control) + '-shm').exists()
    return values


def _read_control(path, *, immutable):
    # Immutable is ONLY for a pinned main file with all sidecars absent, checked
    # before and after. An existing WAL always gets SQLite's normal WAL reader.
    uri = 'file:' + quote(path.as_posix(), safe='/:') + '?mode=ro' + ('&immutable=1' if immutable else '')
    deadline = time.monotonic() + 2
    with closing(sqlite3.connect(uri, uri=True, timeout=.2, isolation_level=None)) as conn:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _TOTAL_LIMIT)
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA busy_timeout=200')
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute('BEGIN')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != set(_TABLES):
            _fail('restore_runtime_registry_invalid')
        result, size = {}, 0
        for table, columns in _TABLES.items():
            if tuple(row[1] for row in conn.execute('PRAGMA table_info(' + table + ')')) != columns:
                _fail('restore_runtime_registry_invalid')
            rows = []
            for row in conn.execute('SELECT * FROM ' + table + ' ORDER BY ' + ','.join(columns)):
                if len(rows) >= 10000:
                    _fail('restore_runtime_evidence_bounded')
                value = dict(zip(columns, row))
                encoded = _canonical(value)
                size += len(encoded)
                if len(encoded) > _JSON_LIMIT or size > _TOTAL_LIMIT or time.monotonic() >= deadline:
                    _fail('restore_runtime_evidence_bounded')
                rows.append(value)
            result[table] = rows
        return result


def _identity(value):
    if (type(value) is not dict or set(value) != _IDENTITY
            or type(value['pid']) is not int or not 1 <= value['pid'] < 2**32
            or type(value['creation_time']) is not str
            or not re.fullmatch(r'[1-9][0-9]{0,19}', value['creation_time'])
            or any(type(value[key]) is not str or not _SHA.fullmatch(value[key])
                   for key in ('executable_sha256', 'argv_sha256'))):
        _fail('restore_runtime_identity_invalid')
    return value


def _proof(value, identity):
    if type(value) is not DeadProcessProof:
        _fail('restore_runtime_process_unverified')
    fields = asdict(value)
    if {key: fields[key] for key in _IDENTITY} != identity or value.state not in ('exited', 'pid_reused'):
        _fail('restore_runtime_process_unverified')
    at = datetime.fromisoformat(value.observed_at)
    if at.tzinfo is None or at.utcoffset() is None or not -2 <= (datetime.now(timezone.utc) - at).total_seconds() <= 30:
        _fail('restore_runtime_process_unverified')
    return _hash(_canonical(identity))


def _record(record, run):
    required = {'pid', 'creation_time', 'executable', 'executable_sha256', 'argv_hash', 'run_id', 'account'}
    if type(record) is not dict or not required.issubset(record) or record['run_id'] != run:
        _fail('restore_runtime_identity_invalid')
    _uuid(record['run_id'])
    _path(record['executable'])  # Evidence only: no opening a recorded executable.
    if type(record['account']) is not str or not 1 <= len(record['account']) <= 512 or any(ord(c) < 32 for c in record['account']):
        _fail('restore_runtime_identity_invalid')
    return _identity({key: record[key] for key in ('pid', 'creation_time', 'executable_sha256')}
                     | {'argv_sha256': record['argv_hash']})


def _verify_supervisor(record):
    """No construction/start/stop/config loading; absence alone is insufficient."""
    try:
        from scripts.team.supervisor import process_absent
    except ModuleNotFoundError as error:
        if error.name not in ('scripts', 'scripts.team', 'scripts.team.supervisor'):
            raise
        # The guarded runner puts the fixed project/scripts directory on
        # sys.path. Neither branch accepts an executable/config from evidence.
        from team.supervisor import process_absent
    identity = _record(record, record['run_id'])
    if process_absent(record) is not True:
        _fail('restore_runtime_process_alive')
    # process_absent also compares argv/exe: a live same-FILETIME mismatch is
    # NOT stopped. This independent lifetime proof closes that ambiguity.
    return verify_dead_identity(identity)


def _verify(*, project_root, archive_manifest, maintenance_path, lifecycle_path,
            dead_verifier, supervisor_verifier, protected_scope):
    """Internal test seam; callbacks must return exact typed, fresh proofs."""
    try:
        root = _path(project_root)
        if not root.is_dir():
            _fail('restore_runtime_path_invalid')
        control = _path(maintenance_path, root, file=True)
        lifecycle_path = _path(lifecycle_path, root, file=True)
        if control == lifecycle_path:
            _fail('restore_runtime_path_invalid')
        if type(archive_manifest) is not dict or len(_canonical(archive_manifest)) > 1024 * 1024:
            _fail()
        archive_hash = _hash(_canonical(archive_manifest))
        deployment = _uuid(archive_manifest['deployment_id'])
        required = archive_manifest['required_participants']
        if (archive_manifest.get('schema_version') != 1 or archive_manifest.get('state') != 'complete'
                or type(required) is not list or not 1 <= len(required) <= 128
                or any(type(v) is not str or not _NAME.fullmatch(v) for v in required)
                or len(required) != len(set(required))):
            _fail('restore_runtime_archive_invalid')
        with ExitStack() as stack:
            for parent in sorted({control.parent, lifecycle_path.parent}):
                stack.enter_context(protected_scope(parent))
            pins = [control, lifecycle_path]
            if Path(str(control) + '-journal').exists():
                _fail('restore_runtime_recovery_required')
            for suffix in ('-wal', '-shm'):
                path = Path(str(control) + suffix)
                if path.exists():
                    pins.append(_path(path, root, file=True))
            for path in pins:
                stack.enter_context(_pin(path))
            before = _files(control, lifecycle_path)
            immutable = before['-wal'] is None and before['-shm'] is False
            data = _read_control(control, immutable=immutable)
            lifecycle = _json(_read_bytes(lifecycle_path, _JSON_LIMIT))
            states = data['maintenance_state']
            if len(states) != 1:
                _fail('restore_runtime_registry_invalid')
            state = states[0]
            if (state['id'] != 1 or state['deployment_id'] != deployment
                    or state['mode'] not in ('draining', 'frozen', 'restore_blocked')
                    or type(state['fence']) is not int or state['fence'] < 1):
                _fail('restore_runtime_admission_open')
            if any(row['state'] != 'finished' for row in data['maintenance_tickets']):
                _fail('restore_runtime_tickets_active')
            sources = data['maintenance_sources']
            if len(sources) != 4 or {row['role'] for row in sources} != _ROLES:
                _fail('restore_runtime_bindings_invalid')
            bindings = {row['role']: os.path.normcase(str(_path(row['path'], root))) for row in sources}
            if len(set(bindings.values())) != 4:
                _fail('restore_runtime_bindings_invalid')
            participants = data['maintenance_participants']
            names = [row['participant_id'] for row in participants]
            if (not 1 <= len(participants) <= 128 or len(set(names)) != len(names)
                    or not set(required).issubset(names)):
                _fail('restore_runtime_participants_invalid')
            proofs = []
            for participant in participants:
                if (type(participant['participant_id']) is not str or not _NAME.fullmatch(participant['participant_id'])
                        or participant['guard_version'] != 1):
                    _fail('restore_runtime_participants_invalid')
                _uuid(participant['run_id'])
                value = _identity(_json(participant['identity']))
                proofs.append(_proof(dead_verifier(value), value))
            run = _uuid(lifecycle['run_id'])
            if (type(lifecycle['schema_version']) is not int or lifecycle['schema_version'] != 1
                    or lifecycle['deployment_id'] != deployment or lifecycle['desired'] != 'stopped'
                    or type(lifecycle['components']) is not dict
                    or set(lifecycle['components']) != {'vikunja', 'gateway', 'proxy'}):
                _fail('restore_runtime_lifecycle_invalid')
            for record in [lifecycle['supervisor'], *lifecycle['components'].values()]:
                value = _record(record, run)
                proofs.append(_proof(supervisor_verifier(record), value))
            gateway_identity = _record(lifecycle['components']['gateway'], run)
            if not any(row['run_id'] == run and _json(row['identity']) == gateway_identity for row in participants):
                _fail('restore_runtime_participants_invalid')
            archive_native = archive_manifest['native_quiescence']
            if (type(archive_native) is not dict or archive_native['deployment_id'] != deployment
                    or archive_native['component'] != 'vikunja'):
                _fail('restore_runtime_archive_invalid')
            _uuid(archive_native['run_id'])
            value = _identity({key: archive_native[key] for key in ('pid', 'creation_time', 'executable_sha256')}
                              | {'argv_sha256': archive_native['argv_hash']})
            proofs.append(_proof(dead_verifier(value), value))
            # New transaction sees concurrent changes instead of reusing an old
            # SQLite snapshot. Retained pins prohibit replace/write on Windows.
            if (_read_control(control, immutable=immutable) != data or _files(control, lifecycle_path) != before
                    or _hash(_canonical(archive_manifest)) != archive_hash):
                _fail('restore_runtime_evidence_changed')
            result = dict(schema_version=1, deployment_id=deployment, archive_manifest_sha256=archive_hash,
                control_sha256=_hash(_canonical(data)), physical_files_sha256=_hash(_canonical(before)),
                lifecycle_sha256=before['lifecycle'], source_bindings_sha256=_hash(_canonical(bindings)),
                process_identities_sha256=_hash(_canonical(sorted(proofs))), participant_count=len(participants),
                lifecycle_process_count=4, active_ticket_count=0)
            result['evidence_sha256'] = _hash(_canonical(result))
            return result
    except RuntimeEvidenceError:
        raise
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError, RecursionError, ImportError):
        raise RuntimeEvidenceError() from None


def verify_stopped_runtime(*, project_root, archive_manifest: dict, maintenance_path: Path, lifecycle_path: Path) -> dict:
    """Windows-only fresh verification; no fake evidence or writable fallback."""
    if os.name != 'nt':
        _fail('restore_runtime_windows_required')
    from secretary.infrastructure.restore_operator import protected_scope
    return _verify(project_root=project_root, archive_manifest=archive_manifest,
                   maintenance_path=maintenance_path, lifecycle_path=lifecycle_path,
                   dead_verifier=verify_dead_identity, supervisor_verifier=_verify_supervisor,
                   protected_scope=protected_scope)
