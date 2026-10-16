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
from .team_maintenance import (
    _EVENT_IMMUTABLE_TRIGGERS,
    _MAINTENANCE_REGISTRY_TABLES_V1,
    _MAINTENANCE_REGISTRY_TABLES_V2,
    _TICKET_COMMITMENT_TRIGGERS,
)


class RuntimeEvidenceError(ValueError):
    def __init__(self, code='restore_runtime_evidence_invalid'):
        self.code = code
        super().__init__(code)


_SHA = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_ROLES = {'secretary', 'team', 'billing', 'vikunja'}
_IDENTITY = {'pid', 'creation_time', 'executable_sha256', 'argv_sha256'}
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
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version in (0, 1) and tables == set(_MAINTENANCE_REGISTRY_TABLES_V1):
            registry_version, registry_tables = 1, _MAINTENANCE_REGISTRY_TABLES_V1
        elif version == 2 and tables == set(_MAINTENANCE_REGISTRY_TABLES_V2):
            registry_version, registry_tables = 2, _MAINTENANCE_REGISTRY_TABLES_V2
        else:
            _fail('restore_runtime_registry_invalid')
        triggers = {row[0]: ' '.join(row[1].split()).casefold()
                    for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='maintenance_events'")}
        expected_triggers = {name: ' '.join(sql.split()).casefold()
                             for name, sql in _EVENT_IMMUTABLE_TRIGGERS.items()}
        if triggers != expected_triggers:
            _fail('restore_runtime_registry_invalid')
        if registry_version == 2:
            triggers = {row[0]: ' '.join(row[1].split()).casefold()
                        for row in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='maintenance_ticket_commitments'")}
            expected_triggers = {name: ' '.join(sql.split()).casefold()
                                 for name, sql in _TICKET_COMMITMENT_TRIGGERS.items()}
            if triggers != expected_triggers:
                _fail('restore_runtime_registry_invalid')
        result, size = {}, 0
        for table, columns in registry_tables.items():
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
        result['_registry_version'] = registry_version
        _validate_ticket_commitments(result)
        return result


def _validate_ticket_commitments(data):
    """Check persisted commitment integrity and ticket scope, never freshness or permission."""
    if data['_registry_version'] != 2:
        return
    from .restore_admission_binding import MaintenanceTicketV1, R4AdmissionBindingV1

    tickets = {row['id']: row for row in data['maintenance_tickets']}
    states = data['maintenance_state']
    if len(states) != 1:
        _fail('restore_runtime_registry_invalid')
    deployment_id = states[0]['deployment_id']
    ticket_keys = {'protocol', 'ticket_id', 'deployment_id', 'participant_id', 'run_id',
                   'kind', 'operation_id', 'fence', 'admission_binding', 'ticket_sha256'}
    binding_keys = {'protocol', 'deployment_id', 'restore_id', 'r3_id', 'epoch_id',
                    'source_generation_sha256', 'decision_id', 'decision_sha256', 'action',
                    'participant_id', 'run_id', 'operation_id', 'barrier_fence'}
    for row in data['maintenance_ticket_commitments']:
        try:
            raw = row['record']
            record = _json(raw)
            if (type(raw) is not str or set(record) != ticket_keys
                    or type(record['admission_binding']) is not dict
                    or set(record['admission_binding']) != binding_keys
                    or _canonical(record).decode('utf-8') != raw
                    or _hash(raw.encode('utf-8')) != row['record_sha256']
                    or record['ticket_id'] != row['ticket_id']):
                _fail('restore_runtime_registry_invalid')
            binding = R4AdmissionBindingV1(**record['admission_binding'])
            ticket = MaintenanceTicketV1(
                ticket_id=record['ticket_id'], deployment_id=record['deployment_id'],
                participant_id=record['participant_id'], run_id=record['run_id'],
                kind=record['kind'], operation_id=record['operation_id'],
                fence=record['fence'], admission_binding=binding, protocol=record['protocol'],
            )
            lifecycle = tickets.get(ticket.ticket_id)
            if (ticket.ticket_sha256 != record['ticket_sha256'] or lifecycle is None
                    or (ticket.deployment_id, ticket.participant_id, ticket.run_id, ticket.kind,
                        ticket.operation_id, ticket.fence)
                    != (deployment_id, lifecycle['participant_id'], lifecycle['run_id'], lifecycle['kind'],
                        lifecycle['operation_id'], lifecycle['fence'])):
                _fail('restore_runtime_registry_invalid')
        except RuntimeEvidenceError:
            raise
        except (KeyError, TypeError, ValueError, UnicodeError):
            _fail('restore_runtime_registry_invalid')


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


def _maintenance_fence_lineage(data, archive_manifest, state):
    """Bind the archived barrier to a coherent immutable old-generation log."""
    barrier_id = _uuid(archive_manifest.get('barrier_id'))
    barrier_fence = archive_manifest.get('barrier_fence')
    if type(barrier_fence) is not int or not 1 <= barrier_fence < 2**63:
        _fail('restore_runtime_archive_invalid')
    native = archive_manifest.get('native_quiescence')
    if (type(native) is not dict or native.get('barrier_id') != barrier_id
            or native.get('barrier_fence') != barrier_fence):
        _fail('restore_runtime_archive_invalid')

    commitment = archive_manifest.get('maintenance_fence_history')
    if type(commitment) is not dict:
        _fail('restore_runtime_fence_history_missing')
    if (set(commitment) != {'schema_version', 'deployment_id', 'barrier_id', 'barrier_fence',
                            'event_count', 'events_sha256'}
            or type(commitment['schema_version']) is not int or commitment['schema_version'] != 1
            or commitment['deployment_id'] != archive_manifest.get('deployment_id')
            or commitment['barrier_id'] != barrier_id
            or type(commitment['barrier_fence']) is not int or commitment['barrier_fence'] != barrier_fence
            or type(commitment['event_count']) is not int
            or not 1 <= commitment['event_count'] <= 10000
            or type(commitment['events_sha256']) is not str
            or not _SHA.fullmatch(commitment['events_sha256'])):
        _fail('restore_runtime_fence_history_invalid')

    events = data['maintenance_events']
    if not events or len(events) > 10000 or commitment['event_count'] > len(events):
        _fail('restore_runtime_fence_history_invalid')
    archived_events = events[:commitment['event_count']]
    if (_hash(_canonical(archived_events)) != commitment['events_sha256']
            or archived_events[-1]['kind'] != 'freeze'
            or (archived_events[-1]['barrier_id'], archived_events[-1]['fence']) != (barrier_id, barrier_fence)):
        _fail('restore_runtime_fence_history_invalid')
    mode, active, high_water = 'open', None, 0
    archive_barrier_seen = False
    for expected_id, event in enumerate(events, start=1):
        event_id = event.get('id')
        kind = event.get('kind')
        event_barrier_id = event.get('barrier_id')
        fence = event.get('fence')
        created_at = event.get('created_at')
        if (type(event_id) is not int or event_id != expected_id
                or kind not in ('begin', 'freeze', 'release')
                or type(fence) is not int or not 1 <= fence < 2**63):
            _fail('restore_runtime_fence_history_invalid')
        _uuid(event_barrier_id)
        try:
            stamp = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            _fail('restore_runtime_fence_history_invalid')

        if kind == 'begin':
            if mode != 'open' or fence != high_water + 1:
                _fail('restore_runtime_fence_history_invalid')
            mode, active, high_water = 'draining', (event_barrier_id, fence), fence
            if (event_barrier_id, fence) == (barrier_id, barrier_fence):
                archive_barrier_seen = True
        elif kind == 'freeze':
            if mode != 'draining' or active != (event_barrier_id, fence):
                _fail('restore_runtime_fence_history_invalid')
            mode = 'frozen'
        else:
            if mode not in ('draining', 'frozen') or active != (event_barrier_id, fence):
                _fail('restore_runtime_fence_history_invalid')
            mode, active = 'open', None

    if (not archive_barrier_seen or state['fence'] != high_water
            or state['mode'] not in ('draining', 'frozen', 'restore_blocked')
            or active is None or state['barrier_id'] != active[0]
            or state['fence'] != active[1]
            or (state['mode'] in ('draining', 'frozen') and state['mode'] != mode)):
        _fail('restore_runtime_fence_history_invalid')
    return dict(archive_barrier_id=barrier_id, archive_barrier_fence=barrier_fence,
        archive_event_count=commitment['event_count'], archive_events_sha256=commitment['events_sha256'],
        high_water_fence=high_water, event_count=len(events), events_sha256=_hash(_canonical(events)))


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
            fence_lineage = _maintenance_fence_lineage(data, archive_manifest, state)
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
                lifecycle_process_count=4, active_ticket_count=0,
                maintenance_fence_lineage=fence_lineage)
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
