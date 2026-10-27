"""Explicit, offline DPAPI voice-bank transfer using a separately held AES key.

No command activates an application, modifies a database, replaces an existing
voice tree, or writes plaintext voice material. PowerShell 7.4+ uses built-in .NET
AES-256-GCM and Windows DPAPI. Keep the key separately from the encrypted package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from secretary.domain.voice import canonical_uuid
from secretary.infrastructure.voice_store import DPAPICipher, MAX_CIPHERTEXT, VoiceStore, _current_user_owned


class TransferError(Exception):
    pass


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plain_path(value: Path, *, new: bool = False) -> Path:
    path = Path(os.path.abspath(value))
    if str(path).startswith("\\\\"):
        raise TransferError("local_path_required")
    for ancestor in [*reversed(path.parents), path]:
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise TransferError("reparse_path_denied")
        if ancestor != path and not stat.S_ISDIR(info.st_mode):
            raise TransferError("invalid_parent")
        if ancestor == path and stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise TransferError("hardlink_denied")
    if new and (path.exists() or not path.parent.is_dir()):
        raise TransferError("new_destination_required")
    return path


def inspect_bank(data_dir: Path) -> list[dict]:
    store = VoiceStore(plain_path(data_dir))
    store._safe(store.root, directory=True)
    if not store.root.is_dir():
        raise TransferError("voice_bank_missing")
    rows = []
    total = 0
    for profile in sorted(store.root.iterdir()):
        store._safe(profile, directory=True)
        canonical_uuid(profile.name)
        for file in sorted(profile.iterdir()):
            store._safe(file)
            if file.suffix != ".dpapi":
                raise TransferError("unexpected_voice_file")
            canonical_uuid(file.stem)
            size = file.stat().st_size
            total += size
            if size > MAX_CIPHERTEXT or total > 20 * 1024 * 1024 or len(rows) >= 32:
                raise TransferError("voice_bank_limit")
            before = sha256(file)
            material = store.read(profile.name, file.stem)
            if sha256(file) != before:
                raise TransferError("source_changed")
            rows.append({"relative_path": file.relative_to(Path(data_dir).absolute()).as_posix(),
                         "profile_id": material.profile_id, "enrollment_id": material.enrollment_id,
                         "material_revision": material.material_revision,
                         "source_ciphertext_sha256": before})
    if not rows:
        raise TransferError("voice_bank_empty")
    return rows


def crypto(operation: str, package: Path, key_file: Path, *, data_dir: Path | None = None,
           target_data: Path | None = None, request: dict | None = None, pwsh: Path | None = None) -> dict:
    executable = pwsh or shutil.which("pwsh.exe")
    if not executable:
        raise TransferError("powershell7_required")
    command = [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
               str(ROOT / "scripts/voice_transport_crypto.ps1"), "-Operation", operation,
               "-Package", str(package), "-KeyFile", str(key_file)]
    if data_dir is not None:
        command += ["-DataDir", str(data_dir)]
    if target_data is not None:
        command += ["-TargetData", str(target_data)]
    try:
        result = subprocess.run(command, input=json.dumps(request) if request else "",
                                capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
        if result.returncode != 0 or len(result.stdout) > 65536:
            safe_reason = result.stderr.strip()
            if not re.fullmatch(r"voice_transport_failed:[A-Za-z0-9]+:\d+", safe_reason):
                safe_reason = "crypto_worker_failed"
            raise TransferError(safe_reason)
        receipt = json.loads(result.stdout)
        if receipt.get("schema") != "secretary.voice-bank-transfer-receipt.v1" or receipt.get("status") != "CRYPTO_VERIFIED":
            raise TransferError("invalid_crypto_receipt")
        return receipt
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise TransferError("crypto_worker_failed") from None


def transfer(operation: str, *, package: Path, key_file: Path, data_dir: Path | None = None,
             target_data: Path | None = None, source_backup_manifest_sha256: str | None = None,
             pwsh: Path | None = None) -> dict:
    if os.name != "nt":
        raise TransferError("windows_required")
    if source_backup_manifest_sha256 is not None and not re.fullmatch("[0-9a-f]{64}", source_backup_manifest_sha256):
        raise TransferError("invalid_backup_manifest_hash")
    package = plain_path(package, new=operation == "export")
    key_file = plain_path(key_file, new=operation == "export")
    # A copied package directory must never silently carry its decryption key.
    if package.parent == key_file.parent or key_file.is_relative_to(package.parent):
        raise TransferError("key_must_be_stored_separately")
    _current_user_owned(key_file.parent)
    if operation == "export":
        if data_dir is None:
            raise TransferError("source_data_required")
        data_dir = plain_path(data_dir)
        if key_file.is_relative_to(data_dir) or package.is_relative_to(data_dir):
            raise TransferError("transport_must_be_outside_source_data")
        before = inspect_bank(data_dir)
        receipt = crypto(operation, package, key_file, data_dir=data_dir, pwsh=pwsh,
                         request={"entries": before, "source_backup_manifest_sha256": source_backup_manifest_sha256})
        if inspect_bank(data_dir) != before:
            raise TransferError("source_changed")
        for expected, actual in zip(before, receipt["files"], strict=True):
            if any(actual.get(key) != value for key, value in expected.items()):
                raise TransferError("export_receipt_mismatch")
        receipt["status"] = "EXPORTED_CANONICAL_SOURCE_VALIDATED"
    elif operation in {"verify", "import"}:
        if not package.is_file() or not key_file.is_file():
            raise TransferError("transport_file_missing")
        # Authenticate and check backup binding before an import creates any file.
        verified = crypto("verify", package, key_file, pwsh=pwsh)
        if source_backup_manifest_sha256 is not None and verified.get("source_backup_manifest_sha256") != source_backup_manifest_sha256:
            raise TransferError("backup_binding_mismatch")
        receipt = verified
        receipt["status"] = "AUTHENTICATED_TRANSPORT"
        if operation == "import":
            if target_data is None:
                raise TransferError("isolated_target_required")
            target_data = plain_path(target_data, new=True)
            receipt = crypto("import", package, key_file, target_data=target_data, pwsh=pwsh)
            if receipt["package_sha256"] != verified["package_sha256"]:
                raise TransferError("transport_changed")
            actual = inspect_bank(target_data)
            expected = sorted(receipt["files"], key=lambda row: row["relative_path"])
            if len(actual) != len(expected):
                raise TransferError("import_file_set_mismatch")
            for observed, original in zip(actual, expected, strict=True):
                if (any(observed[name] != original[name] for name in ("relative_path", "profile_id", "enrollment_id", "material_revision"))
                        or observed["source_ciphertext_sha256"] != original["target_ciphertext_sha256"]):
                    raise TransferError("import_receipt_mismatch")
                # Recheck exact original envelope bytes, beyond semantic VoiceStore validation.
                envelope = DPAPICipher().unprotect((target_data / original["relative_path"]).read_bytes())
                if hashlib.sha256(envelope).hexdigest() != original["plaintext_sha256"]:
                    raise TransferError("envelope_rewrap_mismatch")
            receipt["status"] = "IMPORTED_ISOLATED_CANONICAL_VERIFIED"
            receipt["target_data"] = str(target_data)
    else:
        raise TransferError("invalid_operation")
    receipt["activation_performed"] = False
    receipt["key_included_in_package"] = False
    receipt["plaintext_written_to_disk"] = False
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("export", "verify", "import"))
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, help="Export only: authorized source data directory")
    parser.add_argument("--target-data", type=Path, help="Import only: absent isolated data directory")
    parser.add_argument("--source-backup-manifest-sha256")
    parser.add_argument("--receipt", type=Path, required=True, help="New receipt file outside source/target data")
    parser.add_argument("--pwsh", type=Path)
    args = parser.parse_args()
    try:
        receipt_path = plain_path(args.receipt, new=True)
        if receipt_path in (plain_path(args.package), plain_path(args.key_file)):
            raise TransferError("receipt_path_conflict")
        for directory in (args.data_dir, args.target_data):
            if directory is not None and receipt_path.is_relative_to(plain_path(directory)):
                raise TransferError("receipt_must_be_outside_data")
        receipt = transfer(args.operation, package=args.package, key_file=args.key_file,
                           data_dir=args.data_dir, target_data=args.target_data,
                           source_backup_manifest_sha256=args.source_backup_manifest_sha256, pwsh=args.pwsh)
        with receipt_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, indent=2)
            stream.write("\n")
        # IDs, envelope hashes and paths remain in the private receipt, not console logs.
        print(json.dumps({"status": receipt["status"], "files": len(receipt["files"]),
                          "activation_performed": False, "plaintext_written_to_disk": False}))
        return 0
    except Exception:
        # Never print process diagnostics or parser data that could contain private material.
        print(json.dumps({"status": "VOICE_TRANSFER_FAILED", "activation_performed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
