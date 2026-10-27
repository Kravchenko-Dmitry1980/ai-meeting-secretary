"""Read-only source handoff verification. Never imports or executes donor code.

Pins come from the application's reviewed intake record, not from the bundle
being checked. Source integrity is deliberately separate from runtime admission.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


MAX_FILES = 10_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
PACKAGE_MANIFEST = "contracts/dev-asr-package.v1.json"


class SourceVerificationError(ValueError):
    """A bounded diagnostic code, without source contents or secret values."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SourceVerificationError(code)


def _digest(value: object) -> str:
    _require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None,
             "invalid_digest")
    return value


def _relative(value: object) -> str:
    _require(isinstance(value, str) and bool(value), "invalid_relative_path")
    parts = value.split("/")
    _require(not PurePosixPath(value).is_absolute() and "\\" not in value
             and not any(part in ("", ".", "..") or part.endswith((".", " "))
                         or any(char in part for char in ':<>"|?*\x00')
                         or any(ord(char) < 32 for char in part) for part in parts),
             "invalid_relative_path")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    _require(not any(part.split(".")[0].upper() in reserved for part in parts),
             "reserved_relative_path")
    return value


def _plain(path: Path) -> os.stat_result:
    # Do not resolve first: that would hide a symlink or Windows junction.
    path = path.absolute()
    for ancestor in [*reversed(path.parents), path]:
        value = ancestor.lstat()
        _require(not stat.S_ISLNK(value.st_mode)
                 and not getattr(value, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
                 "source_reparse_point")
    return value


def _read(path: Path) -> bytes:
    before = _plain(path)
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "source_not_plain_file")
    _require(before.st_size <= MAX_FILE_BYTES, "source_file_too_large")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read(MAX_FILE_BYTES + 1)
        finished = os.fstat(stream.fileno())
    after = _plain(path)
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    _require(identity(before) == identity(opened) == identity(finished) == identity(after)
             and len(data) == before.st_size, "source_changed_during_read")
    return data


def _json(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    try:
        result = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SourceVerificationError("invalid_manifest_json") from error
    _require(isinstance(result, dict), "invalid_manifest_shape")
    return result


def _rows(value: object, path_key: str, *, sizes: bool) -> dict[str, dict]:
    _require(isinstance(value, list) and 0 < len(value) <= MAX_FILES, "invalid_manifest_files")
    result = {}
    folded = set()
    for row in value:
        _require(isinstance(row, dict), "invalid_file_record")
        name = _relative(row.get(path_key))
        _require(name.casefold() not in folded, "duplicate_manifest_path")
        folded.add(name.casefold())
        _digest(row.get("sha256"))
        if sizes:
            size = row.get("size_bytes")
            _require(type(size) is int and 0 <= size <= MAX_FILE_BYTES, "invalid_file_size")
        result[name] = row
    return result


def _file_set(root: Path) -> set[str]:
    pending = [root]
    result = set()
    visited = 0
    while pending:
        directory = pending.pop()
        _plain(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                _require(visited <= MAX_FILES * 2, "source_tree_too_large")
                path = Path(entry.path)
                info = _plain(path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                else:
                    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                             "source_not_plain_file")
                    result.add(_relative(path.relative_to(root).as_posix()))
    return result


def verify_source_bundle(
    package: Path, delivery_manifest: Path, *, delivery_manifest_sha256: str,
    package_manifest_sha256: str, expected_source_files: int = 65,
) -> dict:
    """Verify an extracted source package against independently pinned manifests.

    Callers must not use this receipt to authorize inference or claim that
    desktop model hashes were reverified locally. No model weights are read.
    """
    try:
        package = package.absolute()
        delivery_bytes = _read(delivery_manifest.absolute())
        _require(hashlib.sha256(delivery_bytes).hexdigest() == _digest(delivery_manifest_sha256),
                 "delivery_manifest_hash_mismatch")
        delivery = _json(delivery_bytes)
        _require(delivery.get("schema_version") == "neurosecretary.delivery-files.v1"
                 and delivery.get("algorithm") == "SHA-256", "unsupported_delivery_manifest")
        rows = _rows(delivery.get("files"), "relative_path", sizes=True)
        _require(type(delivery.get("file_count")) is int and delivery["file_count"] == len(rows),
                 "delivery_count_mismatch")
        _require(sum(row["size_bytes"] for row in rows.values()) <= MAX_TOTAL_BYTES,
                 "source_bundle_too_large")
        _require(_file_set(package) == set(rows), "source_file_set_mismatch")
        manifests = {}
        for relative, row in rows.items():
            data = _read(package / relative)
            _require(len(data) == row["size_bytes"], "source_size_mismatch")
            _require(hashlib.sha256(data).hexdigest() == row["sha256"], "source_hash_mismatch")
            if relative in ("FILES_MANIFEST.json", PACKAGE_MANIFEST):
                manifests[relative] = data
        _require(set(manifests) == {"FILES_MANIFEST.json", PACKAGE_MANIFEST},
                 "missing_source_manifest")
        inner = _json(manifests["FILES_MANIFEST.json"])
        _require(inner.get("schema_version") == "neurosecretary.handoff-files.v1"
                 and inner.get("algorithm") == "SHA-256", "unsupported_inner_manifest")
        inner_rows = _rows(inner.get("files"), "relative_path", sizes=True)
        _require(type(inner.get("file_count")) is int and inner["file_count"] == len(inner_rows)
                 and set(inner_rows) == set(rows) - {"FILES_MANIFEST.json"},
                 "inner_manifest_file_set_mismatch")
        _require(all((row["sha256"], row["size_bytes"]) ==
                     (rows[name]["sha256"], rows[name]["size_bytes"])
                     for name, row in inner_rows.items()), "inner_manifest_mismatch")
        package_digest = hashlib.sha256(manifests[PACKAGE_MANIFEST]).hexdigest()
        _require(package_digest == _digest(package_manifest_sha256), "package_manifest_hash_mismatch")
        canonical = _json(manifests[PACKAGE_MANIFEST])
        _require(canonical.get("schema_version") == "neurohr.dev-asr-package.v1"
                 and canonical.get("inference_allowed") is False
                 and canonical.get("platform_admissible") is False
                 and canonical.get("model_free_only") is True, "invalid_package_admission")
        sources = _rows(canonical.get("files"), "path", sizes=False)
        _require(len(sources) == expected_source_files, "canonical_source_count_mismatch")
        _require(all(name in rows and row["sha256"] == rows[name]["sha256"]
                     for name, row in sources.items()), "canonical_source_hash_mismatch")
        return {
            "status": "source_integrity_verified", "verified_files": len(rows),
            "canonical_source_files": len(sources), "package_manifest_sha256": package_digest,
            "delivery_manifest_sha256": delivery_manifest_sha256,
            "archive_sha256_recorded": _digest(delivery.get("archive_sha256")),
            "model_weights_verified": False, "inference_authorized": False,
            "runtime_qualified": False, "donor_code_executed": False,
        }
    except OSError as error:
        raise SourceVerificationError("source_io_unavailable") from error
