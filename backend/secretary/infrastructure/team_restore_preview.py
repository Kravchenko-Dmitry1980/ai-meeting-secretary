"""Read-only inventory of an inactive restored deployment; never activation.

All paths are explicit. No settings, environment, credentials, maintenance
database, provider transport or owned process is opened by this module.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3
import time
from uuid import UUID

from .team_backup import (
    BackupError, ROLES, _capture_paths, _check, _hash, _inactive_source, _json,
    _member_path, _read, _ro, _safe, _schema, _tables, _validate_archive,
)

MAX_ROWS = 100000
MAX_CONTENT_BYTES = 64 * 1024 * 1024
MAX_CELL_BYTES = 1024 * 1024
MAX_TABLES = 512
MAX_SECONDS = 30
FAMILIES = {
    'secretary': {
        'jobs': 'status', 'meeting_pipeline_handoffs': None,
        'identification_intents': 'outcome', 'enrollment_recordings': 'status',
        'enrollment_commands': 'status', 'speaker_operations': None,
        'task_publication_outbox': 'state', 'task_publication_previews': None,
    },
    'team': {
        'team_commands': None, 'team_execution': 'state', 'team_message_state': 'state',
        'team_resources': None, 'bot_event_state': 'state', 'bot_reply_state': 'state',
        'bot_buttons': None, 'bot_proposals': None, 'bot_contexts': None,
        'voice_job_state': 'state', 'voice_notice_state': 'state',
        'voice_proposals': None, 'voice_contexts': None,
        'team_auth_sessions': None, 'team_auth_codes': None, 'team_auth_invitations': None,
        'notification_state': 'state', 'notification_planner_state': None,
        'team_due_resolution_previews': None,
    },
    'billing': {'billing_charges': 'status', 'billing_reconciliation_events': 'outcome'},
    'vikunja': {},
}
SAFE_STATES = frozenset({
    'queued', 'running', 'waiting_config', 'paused_budget', 'uncertain',
    'pending', 'processing', 'done', 'quarantined', 'rejected', 'sending', 'sent',
    'retryable', 'applied', 'conflict', 'reconciling', 'succeeded', 'failed',
    'cancelled', 'completed', 'stale', 'reserved', 'submitted', 'confirmed',
    'released', 'awaiting_review', 'interrupted', 'cleanup_pending', 'ready', 'revoked',
    'lookup_started', 'lookup_blocked', 'provider_pending', 'receipt_applied', 'receipt_unresolved',
})
PENDING_REQUIREMENTS = [
    'exclusive_owned_writers', 'current_owner_and_directory', 'provider_reconciliation',
    'legacy_work_and_authorization_quarantine', 'multi_authority_activation_protocol',
    'subsequent_backup_restore_generation', 'max_ingress_cutover', 'dpapi_owner_access',
    'manual_restore_and_24_hour_acceptance',
]


def _quoted(value):
    return '"' + value.replace('"', '""') + '"'


def _identity(path):
    stat = _safe(path, file=True).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, _hash(path))


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value: raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise BackupError('restore_preview_metadata_invalid') from None


def _metadata(archive, target):
    doc = _read(target / 'restore.json')
    required = {'schema_version', 'state', 'restore_id', 'source_manifest_sha256',
                'outbound_enabled', 'databases', 'asset_roots', 'archived_original_roots',
                'immutable_identification'}
    if (not isinstance(doc, dict) or set(doc) != required or type(doc['schema_version']) is not int
            or doc['schema_version'] != 1 or doc['state'] != 'restored_for_reconciliation'
            or doc['outbound_enabled'] is not False
            or not isinstance(doc['source_manifest_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', doc['source_manifest_sha256'])
            or not isinstance(doc['databases'], dict) or set(doc['databases']) != set(ROLES)):
        raise BackupError('restore_preview_metadata_invalid')
    _uuid(doc['restore_id'])
    if _hash(archive / 'manifest.json') != doc['source_manifest_sha256']:
        raise BackupError('restore_preview_manifest_mismatch')
    manifest = _validate_archive(archive)
    if (manifest['config_versions'].get('vikunja') != '2.7.0'
            or manifest['native_storage'] != 'local-v2.7.0'):
        raise BackupError('restore_preview_schema_unsupported')
    roots = {key: target / 'assets' / key for key in manifest['asset_roots']}
    paths = {role: target / 'databases' / (role + '.sqlite3') for role in ROLES}
    paths['secretary'] = roots['secretary_data'] / manifest['databases']['secretary']['original_basename']
    if (doc['databases'] != {role: str(path) for role, path in paths.items()}
            or doc['asset_roots'] != {key: str(path) for key, path in roots.items()}
            or doc['archived_original_roots'] != manifest['asset_roots']):
        raise BackupError('restore_preview_metadata_invalid')
    for path in (*paths.values(), *roots.values()):
        _safe(path)
        if not path.is_relative_to(target): raise BackupError('restore_preview_metadata_invalid')
    if len(set(paths.values())) != 4: raise BackupError('restore_preview_metadata_invalid')
    return doc, manifest, paths, roots


def _source_snapshot(role, path, doc, budget):
    _inactive_source(path)
    before = _identity(path)
    with closing(_ro(path, immutable=True)) as conn:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(MAX_CELL_BYTES, MAX_CONTENT_BYTES))
        conn.set_progress_handler(lambda: int(time.monotonic() > budget['deadline']), 1000)
        _check(conn)
        tables = _tables(conn)
        if len(tables) > MAX_TABLES: raise BackupError('restore_preview_inventory_bounded')
        if role != 'vikunja':
            if 'maintenance_restore_guard' not in tables:
                raise BackupError('restore_preview_guard_mismatch')
            guard = conn.execute('SELECT id,restore_id,reconciliation_required,manifest_sha256 FROM maintenance_restore_guard').fetchall()
            if guard != [(1, doc['restore_id'], 1, doc['source_manifest_sha256'])]:
                raise BackupError('restore_preview_guard_mismatch')
        version_table = {'secretary': 'schema_migrations', 'team': 'team_schema', 'billing': 'billing_schema'}.get(role)
        version_rows = list(conn.execute('SELECT version FROM ' + version_table)) if version_table and version_table in tables else []
        if version_table:
            versions = {row[0] for row in version_rows}
            supported = (versions == set(range(1, 10)) if role != 'billing'
                         else bool(version_rows) and len(versions) == 1 and next(iter(versions)) in {3, 4})
            if not supported:
                raise BackupError('restore_preview_schema_unsupported')
        elif 'files' not in tables:
            raise BackupError('backup_source_role_invalid')
        schema = _schema(conn)
        source = hashlib.sha256(_json(schema).encode())
        for table in sorted(tables):
            row_hashes = []
            for row in conn.execute('SELECT * FROM ' + _quoted(table)):
                budget['rows'] += 1
                if budget['rows'] > MAX_ROWS or time.monotonic() > budget['deadline']:
                    raise BackupError('restore_preview_inventory_bounded')
                values = [('blob', value.hex()) if isinstance(value, bytes) else (type(value).__name__, value) for value in row]
                raw = _json(values).encode()
                budget['bytes'] += len(raw)
                if budget['bytes'] > MAX_CONTENT_BYTES:
                    raise BackupError('restore_preview_inventory_bounded')
                row_hashes.append(hashlib.sha256(raw).hexdigest())
            source.update(_json([table, sorted(row_hashes)]).encode())
        inventory = {}
        for table, column in FAMILIES[role].items():
            if table not in tables:
                if role == 'billing' and table == 'billing_reconciliation_events' and version_rows[0][0] == 3:
                    inventory[table] = {'rows': 0, 'states': {}}
                    continue
                raise BackupError('restore_preview_schema_unsupported')
            entry = {'rows': schema['row_counts'][table]}
            if column:
                if column not in {item[1] for item in conn.execute('PRAGMA table_info(' + _quoted(table) + ')')}:
                    raise BackupError('restore_preview_schema_unsupported')
                states = {}
                for state, count in conn.execute('SELECT ' + _quoted(column) + ',count(*) FROM ' + _quoted(table) + ' GROUP BY ' + _quoted(column)):
                    label = state if state in SAFE_STATES else 'other'
                    states[label] = states.get(label, 0) + count
                entry['states'] = states
            inventory[table] = entry
        liabilities = {}
        if role == 'billing':
            for status in ('reserved', 'submitted', 'uncertain'):
                row = conn.execute('''SELECT count(*),COALESCE(sum(reserved_micro),0),
                    COALESCE(sum(MAX(reserved_micro,COALESCE(observed_cost_micro,0))),0)
                    FROM billing_charges WHERE status=?''', (status,)).fetchone()
                if any(type(value) is not int or value < 0 for value in row):
                    raise BackupError('restore_preview_billing_invalid')
                liabilities[status] = {'rows': row[0], 'reserved_micro': row[1], 'gross_hold_micro': row[2]}
        captures = _capture_paths(conn, Path(doc['asset_roots']['secretary_data'])) if role == 'secretary' else []
    _inactive_source(path)
    if _identity(path) != before: raise BackupError('restore_preview_input_changed')
    return {'schema_sha256': schema['schema_sha256'], 'logical_sha256': source.hexdigest(),
            'file_sha256': before[-1], 'tables': len(tables), 'rows': sum(schema['row_counts'].values())}, inventory, liabilities, captures


def _assets(archive, manifest, roots, captures):
    entries = []
    identities = {}
    captures = set(captures)
    def remap(value):
        if not isinstance(value, str): raise BackupError('restore_preview_asset_invalid')
        original = Path(value)
        for key, old in manifest['asset_roots'].items():
            if original.is_relative_to(Path(old)):
                return str(roots[key] / original.relative_to(Path(old)))
        raise BackupError('restore_preview_asset_invalid')
    for item in manifest['assets']:
        path = _member_path(roots[item['root']], item['path'])
        expected = item['sha256']
        if path in captures:
            source = _member_path(archive, 'assets/' + item['root'] + '/' + item['path'])
            document = _read(source)
            for chunk in document.get('chunks', []):
                if chunk.get('path'): chunk['path'] = remap(chunk['path'])
            for chunk in document.get('in_progress', {}).values():
                if chunk.get('path'): chunk['path'] = remap(chunk['path'])
            expected = hashlib.sha256(_json(document).encode()).hexdigest()
        identities[path] = _identity(path)
        digest = identities[path][-1]
        if digest != expected: raise BackupError('restore_preview_asset_mismatch')
        entries.append([item['root'], item['path'], digest])
    return {'files': len(entries), 'sha256': hashlib.sha256(_json(sorted(entries)).encode()).hexdigest()}, identities


def _archive_members(archive, manifest):
    entries = [(item['path'], item) for item in manifest['databases'].values()]
    entries.extend(('assets/' + item['root'] + '/' + item['path'], item) for item in manifest['assets'])
    identities = {}
    databases = set()
    for relative, item in entries:
        path = _member_path(archive, relative)
        if relative.startswith('databases/'):
            _inactive_source(path)
            databases.add(path)
        identity = _identity(path)
        if identity[2] != item['bytes'] or identity[-1] != item['sha256']:
            raise BackupError('restore_preview_input_changed')
        identities[path] = identity
    return identities, databases


def reconciliation_preview(backup_dir, restore_dir):
    """Observational digest only: no owner approval, quiescence or activation."""
    try:
        archive, target = _safe(backup_dir), _safe(restore_dir)
        if archive == target or archive.is_relative_to(target) or target.is_relative_to(archive):
            raise BackupError('restore_preview_metadata_invalid')
        metadata_paths = [archive / 'manifest.json', target / 'restore.json']
        metadata_before = {path: _identity(path) for path in metadata_paths}
        doc, manifest, paths, roots = _metadata(archive, target)
        archive_before, archive_databases = _archive_members(archive, manifest)
        for path in paths.values(): _inactive_source(path)
        before = {path: _identity(path) for path in paths.values()}
        budget = {'rows': 0, 'bytes': 0, 'deadline': time.monotonic() + MAX_SECONDS}
        sources, inventory, liabilities, captures = {}, {}, {}, []
        for role in ROLES:
            sources[role], inventory[role], role_liabilities, role_captures = _source_snapshot(role, paths[role], doc, budget)
            if role == 'billing': liabilities = role_liabilities
            captures.extend(role_captures)
        assets, asset_before = _assets(archive, manifest, roots, captures)
        for path, identity in {**metadata_before, **archive_before, **before, **asset_before}.items():
            if path in paths.values() or path in archive_databases: _inactive_source(path)
            if _identity(path) != identity: raise BackupError('restore_preview_input_changed')
        result = {'schema_version': 1, 'state': 'preview_blocked', 'restore_id': doc['restore_id'],
                  'source_manifest_sha256': doc['source_manifest_sha256'],
                  'restore_metadata_sha256': metadata_before[target / 'restore.json'][-1],
                  'source_bindings_sha256': hashlib.sha256(_json({'databases': doc['databases'],
                      'asset_roots': doc['asset_roots']}).encode()).hexdigest(),
                  'outbound_enabled': False, 'activation_supported': False,
                  'sources': sources, 'inventory': inventory, 'assets': assets,
                  'billing_liabilities': liabilities, 'pending_requirements': list(PENDING_REQUIREMENTS),
                  'native_containment_verified_in_backup': manifest['native_containment_verified']}
        result['preview_sha256'] = hashlib.sha256(_json(result).encode()).hexdigest()
        return result
    except BackupError:
        raise
    except sqlite3.DataError:
        raise BackupError('restore_preview_inventory_bounded') from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        raise BackupError('restore_preview_invalid') from None
