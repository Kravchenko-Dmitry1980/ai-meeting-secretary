"""Explicit optional admission binding; imports never open a control database."""
from contextlib import nullcontext
from contextlib import closing
from pathlib import Path
import sqlite3
import json
import os
from uuid import uuid4


_DML = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_STRUCTURAL = {getattr(sqlite3, name) for name in (
    'SQLITE_CREATE_INDEX', 'SQLITE_CREATE_TABLE', 'SQLITE_CREATE_TRIGGER', 'SQLITE_CREATE_VIEW',
    'SQLITE_CREATE_TEMP_INDEX', 'SQLITE_CREATE_TEMP_TABLE', 'SQLITE_CREATE_TEMP_TRIGGER', 'SQLITE_CREATE_TEMP_VIEW',
    'SQLITE_DROP_INDEX', 'SQLITE_DROP_TABLE', 'SQLITE_DROP_TRIGGER', 'SQLITE_DROP_VIEW',
    'SQLITE_DROP_TEMP_INDEX', 'SQLITE_DROP_TEMP_TABLE', 'SQLITE_DROP_TEMP_TRIGGER', 'SQLITE_DROP_TEMP_VIEW',
    'SQLITE_ALTER_TABLE', 'SQLITE_ATTACH', 'SQLITE_DETACH',
    'SQLITE_CREATE_VTABLE', 'SQLITE_DROP_VTABLE',
)}
_READ_PRAGMAS = {'table_info', 'table_xinfo', 'index_info', 'index_xinfo',
                'index_list', 'foreign_key_list', 'integrity_check', 'quick_check', 'foreign_key_check'}
_RESTORE_MARKER_QUERY = """SELECT 1 FROM sqlite_master WHERE
    name GLOB 'backup_restore_no_*'
    OR name GLOB 'restore_source_epoch_*'
    OR name GLOB 'secretary_r4_native_*'
    OR name GLOB 'secretary_native_activation_v1_*'
    LIMIT 1"""


def _restore_protocol_marker(conn):
    """Recognize exact restore/R3/R4 artifacts even when the base guard is partial."""
    return conn.execute(_RESTORE_MARKER_QUERY).fetchone()


class RestoredDiagnosticConnection(sqlite3.Connection):
    """OS-read-only main store with a persistent normal-API attachment boundary.

    SQLite mode=ro applies to the main database, not an attached database.
    Clearing an optional caller authorizer must not open that second writer.
    Repositories use this factory only with their guarded mode=ro URI.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_authorizer(None)

    def set_authorizer(self, callback):
        def authorize(action, arg1, arg2, database, trigger):
            if (action in _STRUCTURAL or (action in _DML and database != 'main')
                    or (action == sqlite3.SQLITE_PRAGMA and arg2 is not None
                        and arg1.lower() not in _READ_PRAGMAS)):
                return sqlite3.SQLITE_DENY
            return callback(action, arg1, arg2, database, trigger) if callback is not None else sqlite3.SQLITE_OK
        return super().set_authorizer(authorize)


def admission(repository, participant_id, kind, operation_id=None):
    if repository is None:
        return nullcontext()
    if not participant_id:
        raise ValueError('maintenance_participant_required')
    return repository.admission(participant_id, kind, operation_id)


def async_admission(repository, participant_id, kind, operation_id=None):
    if repository is None:
        return nullcontext()
    if not participant_id:
        raise ValueError('maintenance_participant_required')
    return repository.async_admission(participant_id, kind, operation_id)


def readonly_authority_uri(path):
    """Avoid creating auxiliary files for an inactive main SQLite snapshot.

    When a journal exists, use normal read-only SQLite and never ignore it.
    This helper does not assert writer quiescence or authorize activation.
    """
    path = Path(path).resolve()
    journaled = any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))
    return path.as_uri() + '?mode=ro' + ('' if journaled else '&immutable=1')


def restore_guard_present(paths):
    """Inspect authority stores without migrations or write-capable connections."""
    paths = tuple(paths)
    if paths and _restore_metadata_present(paths):
        return True
    for value in paths:
        path = Path(value).resolve()
        if path.exists():
            with closing(sqlite3.connect(readonly_authority_uri(path), uri=True, timeout=.2)) as conn:
                table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_restore_guard'").fetchone()
                restored_marker = _restore_protocol_marker(conn)
                if restored_marker:
                    return True
                if table and conn.execute(
                        'SELECT 1 FROM maintenance_restore_guard WHERE id=1 AND reconciliation_required=1').fetchone():
                    return True
    return False


def _restore_metadata_present(paths):
    """Recognize a Team restore hold from bounded, database-derived roots.

    A four-authority runtime derives the shared target from its configured
    paths. A single repository also checks the two documented Team restore
    layouts (``databases/<role>.sqlite3`` and
    ``assets/secretary_data/<name>``), so opening an authority directly cannot
    bypass metadata when SQL guard rows are missing. Local Secretary restores
    use the same filename with state ``verified`` and remain writable.
    """
    resolved = tuple(Path(value).resolve() for value in paths)
    parents = tuple(path.parent for path in resolved)
    if not parents:
        raise ValueError('restore_authorities_missing')
    roots = [Path(os.path.commonpath(tuple(str(parent) for parent in parents)))]
    roots.extend(parents)
    for parent in parents:
        if parent.name == 'databases':
            roots.append(parent.parent)
        elif parent.name == 'secretary_data' and parent.parent.name == 'assets':
            roots.append(parent.parent.parent)
    seen = set()
    for root in roots:
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        # A restore may crash before the first database is copied or guarded.
        # Its durable partial marker must block a runtime pointed at that
        # directory just like the completed restore marker does.
        if os.path.lexists(root / 'restore.partial.json'):
            return True
        metadata = root / 'restore.json'
        if not os.path.lexists(metadata):
            continue
        try:
            if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > 4 * 1024 * 1024:
                return True
            document = json.loads(metadata.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError, TypeError):
            # A damaged restore marker must not silently release a restored copy.
            return True
        if not isinstance(document, dict):
            return True
        if document.get('state') == 'restored_for_reconciliation':
            return True
    return False


def restored_runtime_guard_present(settings):
    """Fail closed when any restored authority or explicit config is blocked.

    Runtime composition always owns all four restore authorities. Checking the
    complete set here prevents a partial restore (especially the native
    Vikunja store) from being treated as an ordinary deployment by one entry
    point. Inspection errors are blocked too; no caller flag can clear a stored
    restore guard.
    """
    if getattr(settings, 'restore_blocked', False):
        return True
    try:
        paths = tuple(getattr(settings, role + '_database_path') for role in
            ('secretary', 'team', 'billing', 'vikunja'))
        return restore_guard_present(paths)
    except (AttributeError, OSError, sqlite3.Error, TypeError, ValueError):
        return True


def protect_restored_connection(conn):
    """Keep a restored authority readable while denying SQL writes and escapes.

    Call before WAL setup or migrations, on every newly opened connection.
    A guard row or residual restore-specific trigger marks the copy; an
    application's outbound flag is not authority.
    """
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_restore_guard'").fetchone()
    restored_marker = _restore_protocol_marker(conn)
    guard_row = (conn.execute('SELECT 1 FROM maintenance_restore_guard WHERE id=1 AND reconciliation_required=1').fetchone()
        if table else None)
    main = next((row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main'), '')
    metadata_guard = _restore_metadata_present((main,)) if main else False
    if not (restored_marker or guard_row or metadata_guard):
        return False
    def authorize(action, arg1, arg2, database, trigger):
        if action in (_DML | _STRUCTURAL) or (action == sqlite3.SQLITE_PRAGMA and arg2 is not None
                                            and arg1.lower() not in _READ_PRAGMAS):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    return True


def write_participant_descriptor(repository, participant_id, *, role):
    """Publish one sanitized current-run descriptor beside its bound control DB."""
    descriptor = repository.participant_descriptor(participant_id, role=role)
    target = repository.path.parent / (role + '-participant.json')
    if target.exists():
        existing = json.loads(target.read_text('utf-8'))
        if existing.get('deployment_id') != descriptor['deployment_id'] or existing.get('role') != role:
            raise ValueError('maintenance_descriptor_ownership_mismatch')
    temporary = target.with_name(target.name + '.' + str(uuid4()) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(descriptor, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
