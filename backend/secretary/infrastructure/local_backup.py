"""Cold, explicit backup and isolated restore for the local Secretary data directory.

The caller supplies every path. Backup refuses a running Secretary instance,
uses SQLite's online-backup API for authority databases, and copies only
regular files beneath the data root. Restore verifies the complete backup,
creates a new directory only, relocates stored media paths, and disables cloud
processing in the restored copy. It never deletes or overwrites a directory.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from urllib.parse import quote
from uuid import uuid4

from secretary.infrastructure.instance_lock import DataDirectoryLock, DataDirectoryLockError


SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
PARTIAL_NAME = "manifest.partial.json"
RESTORE_NAME = "restore.json"
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_DATABASES = ("secretary.sqlite3", "billing.sqlite3")
_SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
_INSTANCE_LOCK = ".secretary-instance.lock"
_REPARSE_POINT = 0x400


class LocalBackupError(ValueError):
    """Stable, path-free diagnostic returned by the local backup boundary."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,80}", code) else "backup_failed"
        super().__init__(self.code)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _safe_path(value, *, must_exist=False, directory=False, regular=False) -> Path:
    try:
        raw = Path(value)
        if not raw.is_absolute() or ".." in raw.parts:
            raise LocalBackupError("backup_path_invalid")
        path = Path(os.path.abspath(raw))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise LocalBackupError("backup_path_invalid") from None
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise LocalBackupError("backup_path_invalid") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise LocalBackupError("backup_reparse_forbidden")
    if must_exist:
        try:
            info = path.stat()
        except OSError:
            raise LocalBackupError("backup_path_missing") from None
        if directory and not stat.S_ISDIR(info.st_mode):
            raise LocalBackupError("backup_directory_invalid")
        if regular and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise LocalBackupError("backup_file_invalid")
    return path


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _new_destination(value, *, parent=None, disallow=()) -> Path:
    path = _safe_path(value)
    if path.exists():
        raise LocalBackupError("backup_destination_exists")
    if parent is not None:
        parent = _safe_path(parent, must_exist=True, directory=True)
        if path == parent or not path.is_relative_to(parent):
            raise LocalBackupError("backup_destination_outside_restore_root")
    for root in disallow:
        if _inside(path, root) or _inside(root, path):
            raise LocalBackupError("backup_destination_overlaps_source")
    ancestor = path.parent
    if not ancestor.exists() or not ancestor.is_dir():
        raise LocalBackupError("backup_destination_parent_missing")
    _safe_path(ancestor, must_exist=True, directory=True)
    return path


def _relative(value: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or value.startswith("/") or "\x00" in value):
        raise LocalBackupError("backup_manifest_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in ("", ".", "..") for part in value.split("/")):
        raise LocalBackupError("backup_manifest_invalid")
    return path


def _read_json(path: Path) -> dict:
    path = _safe_path(path, must_exist=True, regular=True)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise LocalBackupError("backup_manifest_too_large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise LocalBackupError("backup_manifest_invalid") from None
    if not isinstance(value, dict):
        raise LocalBackupError("backup_manifest_invalid")
    return value


def _write_json(path: Path, value: dict, *, replace=False) -> None:
    payload = _json(value).encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise LocalBackupError("backup_manifest_too_large")
    mode = "xb"
    if replace:
        temporary = path.with_name(path.name + ".tmp")
        mode = "xb"
        try:
            with temporary.open(mode) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return
    with path.open(mode) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with _safe_path(path, must_exist=True, regular=True).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _copy_file(source: Path, destination: Path) -> dict:
    source = _safe_path(source, must_exist=True, regular=True)
    before = source.stat()
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            opened = os.fstat(incoming.fileno())
            if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or opened.st_nlink != 1):
                raise LocalBackupError("backup_asset_changed")
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                outgoing.write(block)
                digest.update(block)
                size += len(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = source.stat()
    except LocalBackupError:
        raise
    except OSError:
        raise LocalBackupError("backup_copy_failed") from None
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink)):
        raise LocalBackupError("backup_asset_changed")
    return {"bytes": size, "sha256": digest.hexdigest()}


def _copy_verified_file(source: Path, destination: Path, expected: dict) -> None:
    copied = _copy_file(source, destination)
    if copied["bytes"] != expected["bytes"] or copied["sha256"] != expected["sha256"]:
        raise LocalBackupError("backup_hash_mismatch")


def _walk_regular_files(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name, reverse=True)
        except OSError:
            raise LocalBackupError("backup_scan_failed") from None
        for entry in entries:
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                raise LocalBackupError("backup_scan_failed") from None
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
                raise LocalBackupError("backup_reparse_forbidden")
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                # Windows DirEntry.stat() reports st_nlink=0 for regular files;
                # _safe_path uses Path.stat() to enforce the hard-link check.
                _safe_path(path, must_exist=True, regular=True)
                yield path
            else:
                raise LocalBackupError("backup_file_invalid")


def _excluded_root_file(path: Path) -> bool:
    if len(path.parts) != 1:
        return False
    name = path.name
    if name == _INSTANCE_LOCK:
        return True
    for database in _DATABASES:
        if name == database or name in {database + suffix for suffix in _SQLITE_SUFFIXES}:
            return True
    return False


def _database_uri(path: Path, *, immutable=False) -> str:
    return path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")


def _sqlite_metadata(path: Path) -> dict:
    path = _safe_path(path, must_exist=True, regular=True)
    try:
        with closing(sqlite3.connect(_database_uri(path), uri=True, timeout=15)) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)] or conn.execute("PRAGMA foreign_key_check").fetchone():
                raise LocalBackupError("backup_integrity_failed")
            ddl = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
            names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                     if not row[0].startswith("sqlite_")]
            counts = {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                      for name in sorted(names)}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
    except LocalBackupError:
        raise
    except sqlite3.Error:
        raise LocalBackupError("backup_database_invalid") from None
    digest = hashlib.sha256(_json(ddl).encode("utf-8")).hexdigest()
    return {"bytes": path.stat().st_size, "row_counts": counts,
            "schema_sha256": digest, "sha256": _hash_file(path)[0], "user_version": version}


def _snapshot_database(source: Path, destination: Path) -> dict:
    source = _safe_path(source, must_exist=True, regular=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(_database_uri(source), uri=True, timeout=15)) as incoming:
            with closing(sqlite3.connect(destination)) as outgoing:
                incoming.backup(outgoing, pages=256, sleep=0.05)
                outgoing.commit()
                journal_mode = outgoing.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if journal_mode.lower() != "delete":
                    raise LocalBackupError("backup_database_snapshot_invalid")
                outgoing.commit()
    except sqlite3.Error:
        raise LocalBackupError("backup_database_snapshot_failed") from None
    metadata = _sqlite_metadata(destination)
    if metadata["bytes"] != destination.stat().st_size:
        raise LocalBackupError("backup_database_snapshot_invalid")
    return metadata


def _source_relative(value: str, source_root: Path) -> str:
    if not isinstance(value, str) or not value or value.startswith("uploading:"):
        raise LocalBackupError("backup_asset_reference_invalid")
    try:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise LocalBackupError("backup_asset_reference_invalid")
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(source_root):
            raise LocalBackupError("backup_asset_reference_outside_data")
        relative = path.relative_to(source_root).as_posix()
        _relative(relative)
        return relative
    except LocalBackupError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise LocalBackupError("backup_asset_reference_invalid") from None


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _capture_documents(root: Path):
    for candidate in (root / "audio", root / "voice" / "staging"):
        if candidate.exists():
            for path in candidate.rglob("capture.json"):
                _safe_path(path, must_exist=True, regular=True)
                yield path


def _validate_references(archive: Path, source_root: Path, databases: dict, assets: dict) -> None:
    secretary = archive / "databases" / "secretary.sqlite3"
    try:
        with closing(sqlite3.connect(_database_uri(secretary, immutable=True), uri=True, timeout=5)) as conn:
            tables = _tables(conn)
            refs: list[tuple[str, str | None]] = []
            if "meetings" in tables:
                if conn.execute("SELECT 1 FROM meetings WHERE recording=1 LIMIT 1").fetchone():
                    raise LocalBackupError("backup_active_capture")
                for value, in conn.execute("SELECT media_path FROM meetings WHERE media_path IS NOT NULL"):
                    if value.startswith("uploading:"):
                        raise LocalBackupError("backup_incomplete_upload")
                    refs.append((value, None))
            if "chunks" in tables:
                refs.extend((value, digest) for value, digest in conn.execute("SELECT path,sha256 FROM chunks"))
            if "voice_enrollments" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(voice_enrollments)")}
                if {"person_profile_id", "storage_key"} <= columns:
                    for profile, generation in conn.execute(
                            "SELECT person_profile_id,storage_key FROM voice_enrollments WHERE storage_key IS NOT NULL AND storage_key!=''"):
                        refs.append((str(source_root / "voice" / "profiles" / profile / (generation + ".dpapi")), None))
            for value, expected_hash in refs:
                relative = _source_relative(value, source_root)
                info = assets.get(relative)
                if info is None:
                    raise LocalBackupError("backup_asset_reference_missing")
                if expected_hash is not None and info["sha256"] != expected_hash:
                    raise LocalBackupError("backup_chunk_hash_mismatch")
            capture_paths = []
            if "meetings" in tables:
                capture_paths.extend(source_root / "audio" / meeting_id / "capture.json"
                                     for meeting_id, in conn.execute("SELECT id FROM meetings"))
            if "enrollment_recordings" in tables:
                capture_paths.extend(source_root / "voice" / "staging" / recording_id / "capture.json"
                                     for recording_id, in conn.execute("SELECT id FROM enrollment_recordings"))
    except LocalBackupError:
        raise
    except (sqlite3.Error, OSError):
        raise LocalBackupError("backup_reference_check_failed") from None

    for source_manifest in capture_paths:
        relative_manifest = source_manifest.relative_to(source_root).as_posix()
        if relative_manifest not in assets:
            continue
        manifest_path = archive / "assets" / relative_manifest
        document = _read_json(manifest_path)
        try:
            chunks = document.get("chunks", [])
            in_progress = document.get("in_progress", {})
            if not isinstance(chunks, list) or not isinstance(in_progress, dict):
                raise LocalBackupError("backup_capture_manifest_invalid")
            capture_refs = [item["path"] for item in chunks if isinstance(item, dict) and item.get("path")]
            capture_refs.extend(item["path"] for item in in_progress.values()
                                if isinstance(item, dict) and item.get("path"))
            for value in capture_refs:
                relative = _source_relative(value, source_root)
                if relative not in assets:
                    raise LocalBackupError("backup_capture_asset_missing")
        except LocalBackupError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError):
            raise LocalBackupError("backup_capture_manifest_invalid") from None


def create_backup(data_dir, destination) -> dict:
    """Create a verified, non-overwriting cold snapshot from an explicit data root."""
    source_root = _safe_path(data_dir, must_exist=True, directory=True)
    if not (source_root / _DATABASES[0]).is_file():
        raise LocalBackupError("backup_secretary_database_missing")
    target = _new_destination(destination, disallow=(source_root,))

    try:
        lock = DataDirectoryLock(source_root)
    except DataDirectoryLockError:
        raise LocalBackupError("backup_data_directory_in_use") from None
    try:
        target.mkdir(parents=False, exist_ok=False)
        _write_json(target / PARTIAL_NAME, {"schema_version": SCHEMA_VERSION, "state": "copying"})
        (target / "databases").mkdir()
        (target / "assets").mkdir()
        databases = {}
        for name in _DATABASES:
            source_database = source_root / name
            if not source_database.exists():
                if name == _DATABASES[0]:
                    raise LocalBackupError("backup_secretary_database_missing")
                continue
            metadata = _snapshot_database(source_database, target / "databases" / name)
            databases[name] = {"path": "databases/" + name, **metadata}

        assets = []
        asset_map = {}
        for source in _walk_regular_files(source_root):
            relative = source.relative_to(source_root).as_posix()
            if _excluded_root_file(PurePosixPath(relative)):
                continue
            _relative(relative)
            copied = _copy_file(source, target / "assets" / Path(*PurePosixPath(relative).parts))
            assets.append({"path": relative, **copied})
            asset_map[relative] = copied

        _validate_references(target, source_root, databases, asset_map)
        created_at = datetime.now(timezone.utc)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "state": "complete",
            "full_recovery": True,
            "backup_id": str(uuid4()),
            "created_at": created_at.isoformat(),
            "application_version": "0.1.0",
            "source_data_dir": str(source_root),
            "databases": databases,
            "assets": sorted(assets, key=lambda item: item["path"]),
        }
        _write_json(target / "manifest.ready.json", manifest)
        os.replace(target / "manifest.ready.json", target / MANIFEST_NAME)
        (target / PARTIAL_NAME).unlink()
        return {"state": "complete", "backup_id": manifest["backup_id"],
                "database_count": len(databases), "asset_count": len(assets),
                "total_bytes": sum(item["bytes"] for item in assets)
                + sum(item["bytes"] for item in databases.values())}
    except LocalBackupError:
        raise
    except OSError:
        raise LocalBackupError("backup_io_failed") from None
    finally:
        lock.release()


def _validate_manifest(backup_dir: Path) -> tuple[dict, dict]:
    backup_dir = _safe_path(backup_dir, must_exist=True, directory=True)
    if (backup_dir / PARTIAL_NAME).exists() or not (backup_dir / MANIFEST_NAME).is_file():
        raise LocalBackupError("backup_incomplete")
    manifest = _read_json(backup_dir / MANIFEST_NAME)
    required = {"schema_version", "state", "full_recovery", "backup_id", "created_at",
                "application_version", "source_data_dir", "databases", "assets"}
    if (set(manifest) != required or manifest["schema_version"] != SCHEMA_VERSION
            or manifest["state"] != "complete" or manifest["full_recovery"] is not True
            or not isinstance(manifest["source_data_dir"], str)):
        raise LocalBackupError("backup_incomplete")
    source_root = _safe_path(manifest["source_data_dir"])
    if not source_root.is_absolute():
        raise LocalBackupError("backup_manifest_invalid")
    if not isinstance(manifest["databases"], dict) or not set(manifest["databases"]) <= set(_DATABASES) or _DATABASES[0] not in manifest["databases"]:
        raise LocalBackupError("backup_manifest_invalid")
    if not isinstance(manifest["assets"], list):
        raise LocalBackupError("backup_manifest_invalid")

    expected: dict[str, dict] = {}
    for name, entry in manifest["databases"].items():
        if not isinstance(entry, dict) or entry.get("path") != "databases/" + name:
            raise LocalBackupError("backup_manifest_invalid")
        expected[entry["path"]] = entry
    asset_map = {}
    for entry in manifest["assets"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise LocalBackupError("backup_manifest_invalid")
        relative = _relative(entry.get("path"))
        if len(relative.parts) == 1 and _excluded_root_file(relative):
            raise LocalBackupError("backup_manifest_invalid")
        if type(entry["bytes"]) is not int or entry["bytes"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"])):
            raise LocalBackupError("backup_manifest_invalid")
        if entry["path"] in asset_map:
            raise LocalBackupError("backup_manifest_invalid")
        asset_map[entry["path"]] = entry
        expected["assets/" + entry["path"]] = entry
    if len(expected) != len(manifest["databases"]) + len(asset_map):
        raise LocalBackupError("backup_manifest_invalid")

    actual = {}
    for path in _walk_regular_files(backup_dir):
        relative = path.relative_to(backup_dir).as_posix()
        if relative in {MANIFEST_NAME, "manifest.ready.json"}:
            if relative == "manifest.ready.json":
                raise LocalBackupError("backup_incomplete")
            continue
        actual[relative] = path
    if set(actual) != set(expected):
        raise LocalBackupError("backup_file_set_mismatch")
    for relative, entry in expected.items():
        path = actual[relative]
        digest, size = _hash_file(path)
        if size != entry["bytes"] or digest != entry["sha256"]:
            raise LocalBackupError("backup_hash_mismatch")
        if relative.startswith("databases/"):
            metadata = _sqlite_metadata(path)
            for key in ("bytes", "sha256", "schema_sha256", "row_counts", "user_version"):
                if metadata[key] != entry[key]:
                    raise LocalBackupError("backup_database_manifest_mismatch")
    _validate_references(backup_dir, source_root, manifest["databases"], asset_map)
    return manifest, asset_map


def verify_backup(backup_dir) -> dict:
    """Validate the exact files, checksums, SQLite integrity and media links."""
    manifest, assets = _validate_manifest(_safe_path(backup_dir, must_exist=True, directory=True))
    return {"state": "verified", "backup_id": manifest["backup_id"],
            "database_count": len(manifest["databases"]), "asset_count": len(assets),
            "total_bytes": sum(item["bytes"] for item in assets.values())
            + sum(item["bytes"] for item in manifest["databases"].values())}


def _relocated(value: str, source_root: Path, target_root: Path, asset_map: dict) -> str:
    relative = _source_relative(value, source_root)
    if relative not in asset_map:
        raise LocalBackupError("backup_asset_reference_missing")
    return str(target_root / Path(*PurePosixPath(relative).parts))


def _disable_cloud(database: Path) -> None:
    try:
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS configuration(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            safe_values = {"cloud_enabled": False, "allow_unknown_price": False, "local_cost_limits_enabled": True}
            for key, value in safe_values.items():
                conn.execute("INSERT INTO configuration(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, json.dumps(value)))
            conn.commit()
    except sqlite3.Error:
        raise LocalBackupError("restore_configuration_failed") from None


def _relocate_database(database: Path, source_root: Path, target_root: Path, asset_map: dict) -> None:
    try:
        with closing(sqlite3.connect(database)) as conn:
            tables = _tables(conn)
            if "meetings" in tables:
                for meeting_id, media in conn.execute("SELECT id,media_path FROM meetings WHERE media_path IS NOT NULL").fetchall():
                    conn.execute("UPDATE meetings SET media_path=? WHERE id=?",
                                 (_relocated(media, source_root, target_root, asset_map), meeting_id))
            if "chunks" in tables:
                for chunk_id, path in conn.execute("SELECT id,path FROM chunks").fetchall():
                    conn.execute("UPDATE chunks SET path=? WHERE id=?",
                                 (_relocated(path, source_root, target_root, asset_map), chunk_id))
            conn.commit()
    except LocalBackupError:
        raise
    except sqlite3.Error:
        raise LocalBackupError("restore_database_relocation_failed") from None
    _disable_cloud(database)


def _relocate_capture_manifests(target_root: Path, source_root: Path, asset_map: dict) -> None:
    for path in _capture_documents(target_root):
        document = _read_json(path)
        try:
            for chunk in document.get("chunks", []):
                if chunk.get("path"):
                    chunk["path"] = _relocated(chunk["path"], source_root, target_root, asset_map)
            for chunk in document.get("in_progress", {}).values():
                if chunk.get("path"):
                    chunk["path"] = _relocated(chunk["path"], source_root, target_root, asset_map)
        except LocalBackupError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError):
            raise LocalBackupError("backup_capture_manifest_invalid") from None
        _write_json(path.with_name(path.name + ".relocating"), document)
        os.replace(path.with_name(path.name + ".relocating"), path)


def _verify_restored(target_root: Path) -> None:
    secretary = target_root / "secretary.sqlite3"
    try:
        with closing(sqlite3.connect(_database_uri(secretary, immutable=True), uri=True, timeout=5)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or conn.execute("PRAGMA foreign_key_check").fetchone():
                raise LocalBackupError("restore_integrity_failed")
            tables = _tables(conn)
            if "meetings" in tables:
                for media, in conn.execute("SELECT media_path FROM meetings WHERE media_path IS NOT NULL"):
                    path = _safe_path(media)
                    if not path.is_relative_to(target_root) or not path.is_file():
                        raise LocalBackupError("restore_asset_reference_invalid")
            if "chunks" in tables:
                for path_value, expected_hash in conn.execute("SELECT path,sha256 FROM chunks"):
                    path = _safe_path(path_value, must_exist=True, regular=True)
                    if not path.is_relative_to(target_root) or _hash_file(path)[0] != expected_hash:
                        raise LocalBackupError("restore_chunk_hash_mismatch")
            if "configuration" in tables:
                settings = {key: json.loads(value) for key, value in conn.execute("SELECT key,value FROM configuration")}
                if (settings.get("cloud_enabled") is not False or settings.get("allow_unknown_price") is not False
                        or settings.get("local_cost_limits_enabled") is not True):
                    raise LocalBackupError("restore_outbound_guard_invalid")
    except LocalBackupError:
        raise
    except (sqlite3.Error, OSError, ValueError):
        raise LocalBackupError("restore_integrity_failed") from None


def restore_backup(backup_dir, destination, *, restore_root) -> dict:
    """Restore a verified backup into a fresh child directory with cloud disabled."""
    archive = _safe_path(backup_dir, must_exist=True, directory=True)
    manifest, asset_map = _validate_manifest(archive)
    root = _safe_path(restore_root, must_exist=True, directory=True)
    source_root = _safe_path(manifest["source_data_dir"])
    target = _new_destination(destination, parent=root, disallow=(archive, source_root))
    target.mkdir(parents=False, exist_ok=False)
    restore_id = str(uuid4())
    partial = {"schema_version": 1, "restore_id": restore_id, "state": "copying", "outbound_enabled": False}
    _write_json(target / "restore.partial.json", partial)
    (target / "databases").mkdir()
    try:
        for entry in manifest["assets"]:
            relative = _relative(entry["path"])
            _copy_verified_file(archive / "assets" / Path(*relative.parts),
                                target / Path(*relative.parts), entry)
        for name, entry in manifest["databases"].items():
            _copy_verified_file(archive / entry["path"], target / name, entry)
        _relocate_database(target / "secretary.sqlite3", source_root, target, asset_map)
        _relocate_capture_manifests(target, source_root, asset_map)
        _verify_restored(target)
        report = {"schema_version": 1, "restore_id": restore_id, "state": "verified",
                  "backup_id": manifest["backup_id"],
                  "backup_manifest_sha256": _hash_file(archive / MANIFEST_NAME)[0],
                  "restored_at": datetime.now(timezone.utc).isoformat(),
                  "outbound_enabled": False}
        _write_json(target / RESTORE_NAME, report)
        (target / "restore.partial.json").unlink()
        return report
    except LocalBackupError:
        raise
    except OSError:
        raise LocalBackupError("restore_io_failed") from None
