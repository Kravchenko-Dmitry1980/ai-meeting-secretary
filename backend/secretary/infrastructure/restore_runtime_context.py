"""Resolve the one configured, inactive R3 restore without granting activation.

Every location comes from the exact loaded runtime config and the fixed R3
inventory for that restored copy. The inventory is only a path hint: the
existing prepared-context collector revalidates all of its contents under
read-only custody. This resolver never creates stores or enables outbound work.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from secretary.team_settings import (
    TeamRuntimeSettings, _CONFIG_LOADER_MARKER, load_team_settings,
)
from .restore_activation_context import (
    ActivationContextError, PreparedActivationContext,
    collect_prepared_activation_context,
)
from .team_backup import _safe
from .team_restore_reconciliation import _identity, _pin_file, _read


_SOURCE_ROLES = frozenset(('secretary', 'team', 'billing', 'vikunja'))
_SCOPE_KEYS = frozenset(('root', 'root_id', 'archive', 'archive_id',
    'target', 'target_id', 'files', 'runtime'))


@dataclass(frozen=True)
class ResolvedRuntimeRestoreContext:
    project_root: Path
    backup_dir: Path
    restore_dir: Path
    maintenance_path: Path
    lifecycle_path: Path
    config_path: Path
    prepared_context: PreparedActivationContext

    @property
    def activation_supported(self) -> bool:
        return False

    @property
    def outbound_enabled(self) -> bool:
        return False


@dataclass(frozen=True)
class FixedRuntimeRestoreInputs:
    """Path hints from validated loaded config and fixed R3 inventory only.

    Callers must still re-read these inputs through the current R4 evidence
    collector; this object alone is neither source evidence nor permission.
    """

    project_root: Path
    backup_dir: Path
    restore_dir: Path
    maintenance_path: Path
    lifecycle_path: Path
    config_path: Path
    restore_id: str

    @property
    def activation_supported(self) -> bool:
        return False

    @property
    def outbound_enabled(self) -> bool:
        return False


def _fail():
    raise ActivationContextError() from None


def _fixed_path(value, *, file=False):
    try:
        raw = Path(value)
        if not raw.is_absolute() or '..' in raw.parts or str(raw).startswith(('\\\\', '//')):
            _fail()
        return _safe(raw, file=file)
    except ActivationContextError:
        raise
    except (OSError, TypeError, ValueError):
        _fail()


def _settings_config(settings):
    if type(settings) is not TeamRuntimeSettings:
        _fail()
    if settings._config_loader_marker is not _CONFIG_LOADER_MARKER:
        _fail()
    config_path = settings._config_path
    if not isinstance(config_path, Path):
        _fail()
    root = _fixed_path(settings.project_dir)
    config_path = _fixed_path(config_path, file=True)
    if (not root.is_dir() or not config_path.is_relative_to(root)
            or config_path.suffix.lower() != '.json'):
        _fail()
    try:
        with config_path.open('rb') as stream:
            before = config_path.stat()
            if before.st_nlink != 1 or before.st_size > 128 * 1024:
                _fail()
            raw = stream.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                _fail()
            current = load_team_settings(config_path)
            after = config_path.stat()
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or current.model_dump(mode='python') != settings.model_dump(mode='python')
                    or current._config_path != config_path):
                _fail()
    except ActivationContextError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        _fail()
    if not settings.restore_blocked or settings.outbound_enabled or settings.subscription_reconcile_enabled:
        _fail()
    return root, config_path


def _source_paths(settings, root):
    paths = {}
    try:
        for role in _SOURCE_ROLES:
            path = _fixed_path(getattr(settings, role + '_database_path'), file=True)
            if not path.is_relative_to(root):
                _fail()
            paths[role] = path
    except ActivationContextError:
        raise
    except (AttributeError, OSError, TypeError, ValueError):
        _fail()
    if len(set(paths.values())) != len(_SOURCE_ROLES):
        _fail()
    return paths


def _restore_metadata(settings, root, paths):
    # Team/Billing/Vikunja are fixed under the restore's databases directory;
    # Secretary may be relocated into the restored Secretary data tree.
    target = _fixed_path(paths['team'].parent.parent)
    if (target == root or not target.is_relative_to(root)
            or any(paths[role].parent != target / 'databases'
                   for role in ('team', 'billing', 'vikunja'))):
        _fail()
    metadata_path = _fixed_path(target / 'restore.json', file=True)
    metadata = _read(metadata_path, 4 * 1024 * 1024)
    if (type(metadata) is not dict or metadata.get('schema_version') != 1
            or metadata.get('state') != 'restored_for_reconciliation'
            or metadata.get('outbound_enabled') is not False
            or set(metadata.get('databases', {})) != _SOURCE_ROLES):
        _fail()
    try:
        restore_id = metadata['restore_id']
        if type(restore_id) is not str or str(UUID(restore_id)) != restore_id:
            _fail()
        metadata_paths = {role: _fixed_path(metadata['databases'][role], file=True)
                          for role in _SOURCE_ROLES}
    except ActivationContextError:
        raise
    except (KeyError, TypeError, ValueError, OSError):
        _fail()
    if metadata_paths != paths:
        _fail()
    return target, restore_id


def _inventory_scope(settings, root, target, restore_id, paths):
    directory = _fixed_path(root / '.runtime' / 'team-operator' / 'restores' / restore_id)
    inventory_path = _fixed_path(directory / 'inventory.json', file=True)
    envelope = _read(inventory_path)
    if (type(envelope) is not dict
            or set(envelope) != {'version', 'scope', 'plan', 'rowids', 'retained',
                                 'runtime_evidence_sha256'}
            or type(envelope.get('version')) is not int or envelope['version'] != 1):
        _fail()
    scope = envelope.get('scope')
    if type(scope) is not dict or set(scope) != _SCOPE_KEYS:
        _fail()
    files = scope.get('files')
    runtime = scope.get('runtime')
    if (type(files) is not dict or set(files) != _SOURCE_ROLES
            or type(runtime) is not list or len(runtime) != 2
            or any(type(item) is not str for item in runtime)):
        _fail()
    try:
        if (scope['root'] != str(root) or scope['root_id'] != _identity(root)
                or scope['target'] != str(target) or scope['target_id'] != _identity(target)):
            _fail()
        for role, path in paths.items():
            row = files[role]
            if (type(row) is not list or len(row) != 2
                    or row != [str(path), _identity(path)]):
                _fail()
        archive = _fixed_path(scope['archive'])
        if (scope['archive_id'] != _identity(archive) or archive == target
                or archive == root or not archive.is_relative_to(root)
                or archive.is_relative_to(target) or target.is_relative_to(archive)):
            _fail()
    except ActivationContextError:
        raise
    except (KeyError, OSError, TypeError, ValueError):
        _fail()
    maintenance = _fixed_path(runtime[0], file=True)
    lifecycle = _fixed_path(runtime[1], file=True)
    configured_control = _fixed_path(settings.control_database_path, file=True)
    if (maintenance != configured_control
            or not maintenance.is_relative_to(root) or not lifecycle.is_relative_to(root)
            or maintenance.is_relative_to(target) or lifecycle.is_relative_to(target)
            or maintenance.is_relative_to(archive) or lifecycle.is_relative_to(archive)):
        _fail()
    return archive, maintenance, lifecycle


def resolve_fixed_runtime_restore_inputs(settings: TeamRuntimeSettings) -> FixedRuntimeRestoreInputs:
    """Resolve fixed restore paths without requiring the pre-R4 source bytes.

    The current R4 collector must independently validate all source contents
    before these hints are used. No caller path, store, or permission is accepted.
    """
    try:
        if type(settings) is not TeamRuntimeSettings or settings._config_loader_marker is not _CONFIG_LOADER_MARKER:
            _fail()
        root_hint = _fixed_path(settings.project_dir)
        config_path = _fixed_path(settings._config_path, file=True)
        if (not root_hint.is_dir() or not config_path.is_relative_to(root_hint)
                or config_path.suffix.lower() != '.json'):
            _fail()
        with _pin_file(config_path, deny_write=True):
            root, config_path = _settings_config(settings)
            paths = _source_paths(settings, root)
            target, restore_id = _restore_metadata(settings, root, paths)
            archive, maintenance, lifecycle = _inventory_scope(
                settings, root, target, restore_id, paths)
            return FixedRuntimeRestoreInputs(root, archive, target, maintenance,
                lifecycle, config_path, restore_id)
    except ActivationContextError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _fail()


def resolve_runtime_restore_context(settings: TeamRuntimeSettings) -> ResolvedRuntimeRestoreContext:
    """Resolve and re-read the configured inactive restore; never activates it."""
    try:
        if type(settings) is not TeamRuntimeSettings or settings._config_loader_marker is not _CONFIG_LOADER_MARKER:
            _fail()
        root_hint = _fixed_path(settings.project_dir)
        config_path = _fixed_path(settings._config_path, file=True)
        if (not root_hint.is_dir() or not config_path.is_relative_to(root_hint)
                or config_path.suffix.lower() != '.json'):
            _fail()
        with _pin_file(config_path, deny_write=True):
            root, config_path = _settings_config(settings)
            paths = _source_paths(settings, root)
            target, restore_id = _restore_metadata(settings, root, paths)
            archive, maintenance, lifecycle = _inventory_scope(settings, root, target, restore_id, paths)
            prepared = collect_prepared_activation_context(root, archive, target,
                maintenance_path=maintenance, lifecycle_path=lifecycle)
            if prepared.restore_id != restore_id or prepared.activation_supported or prepared.outbound_enabled:
                _fail()
            return ResolvedRuntimeRestoreContext(root, archive, target, maintenance,
                lifecycle, config_path, prepared)
    except ActivationContextError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _fail()
