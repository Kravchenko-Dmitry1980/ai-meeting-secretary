"""Synthetic Windows DPAPI/AES-GCM round trip; never uses the working voice bank."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from secretary.domain.voice import Excerpt, ModelStamp, PreparedAudio, PrivateMaterial
from secretary.infrastructure.voice_store import VoiceStore


spec = importlib.util.spec_from_file_location(
    "voice_bank_transfer", Path(__file__).resolve().parents[1] / "scripts/voice_bank_transfer.py")
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)
pytestmark = pytest.mark.skipif(os.name != "nt" or not shutil.which("pwsh.exe"), reason="Windows and PowerShell 7 required")


@pytest.fixture
def bank(tmp_path):
    source = tmp_path / "synthetic source"
    package_dir = tmp_path / "private transport"
    key_dir = tmp_path / "separate key"
    package_dir.mkdir()
    key_dir.mkdir()
    material = PrivateMaterial(str(uuid4()), str(uuid4()), 3,
        ModelStamp("speechbrain/spkrec-ecapa-voxceleb", "a" * 40, "b" * 64),
        PreparedAudio(b"\0\0" * 240000, (Excerpt(0, 80000), Excerpt(80000, 160000), Excerpt(160000, 240000)), 240000),
        ((1.0,) + (0.0,) * 191,) * 3)
    VoiceStore(source).write(material)
    return {"source": source, "material": material,
            "package": package_dir / "voice.svbank", "key_file": key_dir / "voice.key",
            "target": tmp_path / "new isolated data", "backup_hash": "c" * 64}


def export(bank):
    return transfer.transfer("export", package=bank["package"], key_file=bank["key_file"],
        data_dir=bank["source"], source_backup_manifest_sha256=bank["backup_hash"])


def test_real_dpapi_aes_roundtrip_preserves_material_and_never_activates(bank):
    source_before = transfer.inspect_bank(bank["source"])
    exported = export(bank)
    assert exported["status"] == "EXPORTED_CANONICAL_SOURCE_VALIDATED"
    assert bank["key_file"].stat().st_size == 32
    assert bank["material"].profile_id.encode() not in bank["package"].read_bytes()
    verified = transfer.transfer("verify", package=bank["package"], key_file=bank["key_file"],
                                 source_backup_manifest_sha256=bank["backup_hash"])
    assert verified["status"] == "AUTHENTICATED_TRANSPORT"
    imported = transfer.transfer("import", package=bank["package"], key_file=bank["key_file"],
                                 target_data=bank["target"], source_backup_manifest_sha256=bank["backup_hash"])
    assert imported["status"] == "IMPORTED_ISOLATED_CANONICAL_VERIFIED"
    assert not imported["activation_performed"] and not imported["key_included_in_package"]
    assert VoiceStore(bank["target"]).read(bank["material"].profile_id, bank["material"].enrollment_id) == bank["material"]
    assert transfer.inspect_bank(bank["source"]) == source_before
    assert {p.suffix for p in bank["target"].rglob("*") if p.is_file()} == {".dpapi"}
    row = imported["files"][0]
    assert row["plaintext_sha256"] == exported["files"][0]["plaintext_sha256"]
    assert row["source_ciphertext_sha256"] == source_before[0]["source_ciphertext_sha256"]
    assert row["target_ciphertext_sha256"] == hashlib.sha256((bank["target"] / row["relative_path"]).read_bytes()).hexdigest()


@pytest.mark.parametrize("damage", ["tamper", "wrong_key", "backup_binding", "existing_target"])
def test_import_rejects_damaged_or_conflicting_inputs(bank, damage):
    export(bank)
    if damage == "tamper":
        contents = bytearray(bank["package"].read_bytes())
        contents[-1] ^= 1
        bank["package"].write_bytes(contents)
    elif damage == "wrong_key":
        bank["key_file"].write_bytes(os.urandom(32))
    elif damage == "backup_binding":
        bank["backup_hash"] = "d" * 64
    else:
        bank["target"].mkdir()
        (bank["target"] / "keep.txt").write_bytes(b"preserve")
    with pytest.raises(transfer.TransferError):
        transfer.transfer("import", package=bank["package"], key_file=bank["key_file"],
            target_data=bank["target"], source_backup_manifest_sha256=bank["backup_hash"])
    if damage == "existing_target":
        assert (bank["target"] / "keep.txt").read_bytes() == b"preserve"
    else:
        assert not bank["target"].exists()


def test_export_requires_separate_key_and_preserves_existing_package(bank):
    with pytest.raises(transfer.TransferError, match="separately"):
        transfer.transfer("export", package=bank["package"], key_file=bank["package"].with_suffix(".key"), data_dir=bank["source"])
    bank["package"].write_bytes(b"existing")
    with pytest.raises(transfer.TransferError, match="new_destination"):
        export(bank)
    assert bank["package"].read_bytes() == b"existing"
    assert not bank["key_file"].exists()


def test_key_with_extra_reader_is_rejected(bank, tmp_path):
    export(bank)
    script = tmp_path / "loosen synthetic key only.ps1"
    script.write_text('''param([string]$KeyPath)
$ErrorActionPreference = 'Stop'
$file = [IO.FileInfo]::new($KeyPath)
$acl = [IO.FileSystemAclExtensions]::GetAccessControl($file)
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    [Security.Principal.SecurityIdentifier]::new('S-1-5-11'),
    [Security.AccessControl.FileSystemRights]::Read,
    [Security.AccessControl.AccessControlType]::Allow))
[IO.FileSystemAclExtensions]::SetAccessControl($file, $acl)
''', encoding="utf-8")
    subprocess.run([shutil.which("pwsh.exe"), "-NoProfile", "-NonInteractive", "-File", str(script), str(bank["key_file"])],
                   check=True, capture_output=True)
    with pytest.raises(transfer.TransferError):
        transfer.transfer("verify", package=bank["package"], key_file=bank["key_file"])
