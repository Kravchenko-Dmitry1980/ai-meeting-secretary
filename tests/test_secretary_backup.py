from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from secretary.infrastructure.database import Database
from secretary.infrastructure.instance_lock import DataDirectoryLock
import secretary.infrastructure.local_backup as local_backup
from secretary.infrastructure.local_backup import LocalBackupError, create_backup, restore_backup, verify_backup


def _fixture(data_dir: Path):
    db = Database(data_dir / "secretary.sqlite3")
    meeting = db.create_meeting("Synthetic restore sample")
    meeting_dir = data_dir / "audio" / meeting["id"]
    meeting_dir.mkdir(parents=True)
    media = meeting_dir / "original.wav"
    chunk_path = meeting_dir / "chunk.wav"
    media_bytes = b"synthetic-original-audio"
    chunk_bytes = b"synthetic-chunk-audio"
    media.write_bytes(media_bytes)
    chunk_path.write_bytes(chunk_bytes)
    chunk_hash = hashlib.sha256(chunk_bytes).hexdigest()
    db.update_meeting(meeting["id"], media_path=str(media))
    db.add_chunk(meeting["id"], {"path": str(chunk_path), "sequence": 0, "channel": "mic",
                                  "offset_ms": 0, "duration_ms": 1000, "sha256": chunk_hash})
    db.set_configuration({"cloud_enabled": True, "allow_unknown_price": True})
    (meeting_dir / "capture.json").write_text(json.dumps({
        "schema_version": 1, "meeting_id": meeting["id"], "status": "stopped",
        "chunks": [{"path": str(chunk_path), "sha256": chunk_hash}], "in_progress": {}
    }), encoding="utf-8")
    voice = data_dir / "voice" / "profiles" / "synthetic" / "generation.dpapi"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"synthetic-encrypted-voice-profile")
    billing = data_dir / "billing.sqlite3"
    with sqlite3.connect(billing) as conn:
        conn.execute("CREATE TABLE synthetic_receipt(id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        conn.execute("INSERT INTO synthetic_receipt VALUES('kept-unknown', 'unknown')")
    return db, meeting, media, chunk_path, voice, billing


def test_backup_verify_and_restore_relocates_assets_and_disables_cloud(tmp_path):
    source = tmp_path / "source-data"
    source.mkdir()
    original_db, meeting, media, chunk, voice, billing = _fixture(source)
    original_media_path = original_db.meeting(meeting["id"], internal=True)["media_path"]

    archive = tmp_path / "backups" / "backup-1"
    archive.parent.mkdir()
    receipt = create_backup(source, archive)

    assert receipt["state"] == "complete"
    assert verify_backup(archive)["backup_id"] == receipt["backup_id"]
    assert (archive / "databases" / "secretary.sqlite3").is_file()
    assert (archive / "databases" / "billing.sqlite3").is_file()
    assert (archive / "assets" / "voice" / "profiles" / "synthetic" / "generation.dpapi").read_bytes() == voice.read_bytes()

    restore_root = tmp_path / "restores"
    restore_root.mkdir()
    restored = restore_backup(archive, restore_root / "restored-data", restore_root=restore_root)
    assert restored["outbound_enabled"] is False
    restored_db = Database(restore_root / "restored-data" / "secretary.sqlite3")
    restored_meeting = restored_db.meeting(meeting["id"], internal=True)
    restored_chunk = restored_db.chunks(meeting["id"])[0]
    assert Path(restored_meeting["media_path"]).is_file()
    assert Path(restored_meeting["media_path"]).read_bytes() == media.read_bytes()
    assert Path(restored_chunk["path"]).is_file()
    assert hashlib.sha256(Path(restored_chunk["path"]).read_bytes()).hexdigest() == restored_chunk["sha256"]
    assert Path(restored_chunk["path"]).is_relative_to(restore_root / "restored-data")
    capture = json.loads((restore_root / "restored-data" / "audio" / meeting["id"] / "capture.json").read_text("utf-8"))
    assert Path(capture["chunks"][0]["path"]).is_relative_to(restore_root / "restored-data")
    assert restored_db.configuration()["cloud_enabled"] is False
    assert restored_db.configuration()["allow_unknown_price"] is False
    with sqlite3.connect(restore_root / "restored-data" / "billing.sqlite3") as conn:
        assert conn.execute("SELECT state FROM synthetic_receipt WHERE id='kept-unknown'").fetchone() == ("unknown",)
    assert original_db.configuration()["cloud_enabled"] is True
    assert original_db.configuration()["allow_unknown_price"] is True
    assert original_db.meeting(meeting["id"], internal=True)["media_path"] == original_media_path
    assert billing.is_file()


def test_backup_refuses_a_live_data_directory_before_creating_archive(tmp_path):
    source = tmp_path / "source-data"
    source.mkdir()
    _fixture(source)
    destination = tmp_path / "backups" / "must-not-exist"
    destination.parent.mkdir()

    with DataDirectoryLock(source):
        with pytest.raises(LocalBackupError, match="backup_data_directory_in_use"):
            create_backup(source, destination)

    assert not destination.exists()


def test_tampered_backup_fails_verification_and_restore_without_destination(tmp_path):
    source = tmp_path / "source-data"
    source.mkdir()
    _fixture(source)
    archive = tmp_path / "backup"
    create_backup(source, archive)
    asset = archive / "assets" / "audio" / next((source / "audio").iterdir()).name / "chunk.wav"
    asset.write_bytes(b"tampered")
    restore_root = tmp_path / "restores"
    restore_root.mkdir()

    with pytest.raises(LocalBackupError, match="backup_hash_mismatch"):
        verify_backup(archive)
    with pytest.raises(LocalBackupError, match="backup_hash_mismatch"):
        restore_backup(archive, restore_root / "restored-data", restore_root=restore_root)
    assert not (restore_root / "restored-data").exists()


def test_restore_rejects_asset_changed_after_initial_validation(tmp_path, monkeypatch):
    source = tmp_path / "source-data"
    source.mkdir()
    _fixture(source)
    archive = tmp_path / "backup"
    create_backup(source, archive)
    media = next((archive / "assets" / "audio").glob("*")) / "original.wav"
    restore_root = tmp_path / "restores"
    restore_root.mkdir()
    copy_file = local_backup._copy_file
    changed = False

    def change_before_copy(path, destination):
        nonlocal changed
        if path == media and not changed:
            original = path.read_bytes()
            path.write_bytes(b"x" * len(original))
            changed = True
        return copy_file(path, destination)

    monkeypatch.setattr(local_backup, "_copy_file", change_before_copy)
    with pytest.raises(LocalBackupError, match="backup_hash_mismatch"):
        restore_backup(archive, restore_root / "restored-data", restore_root=restore_root)
    assert changed


def test_backup_rejects_references_outside_the_data_directory(tmp_path):
    source = tmp_path / "source-data"
    source.mkdir()
    db, meeting, _, _, _, _ = _fixture(source)
    db.update_meeting(meeting["id"], media_path=str(tmp_path / "outside.wav"))
    archive = tmp_path / "backup"

    with pytest.raises(LocalBackupError, match="backup_asset_reference_outside_data"):
        create_backup(source, archive)


def test_restore_refuses_to_overlap_original_data_directory(tmp_path):
    source = tmp_path / "source-data"
    source.mkdir()
    _fixture(source)
    archive = tmp_path / "backup"
    create_backup(source, archive)

    with pytest.raises(LocalBackupError, match="backup_destination_overlaps_source"):
        restore_backup(archive, source / "restored-copy", restore_root=source)

    assert not (source / "restored-copy").exists()


@pytest.mark.parametrize("field", ["path", "bytes", "sha256", "schema_sha256", "row_counts", "user_version"])
def test_missing_database_manifest_fields_are_rejected_before_restore(tmp_path, field):
    source = tmp_path / "source-data"
    source.mkdir()
    _fixture(source)
    archive = tmp_path / "backup"
    create_backup(source, archive)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    del manifest["databases"]["secretary.sqlite3"][field]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restore_root = tmp_path / "restores"
    restore_root.mkdir()
    destination = restore_root / "copy"

    with pytest.raises(LocalBackupError, match="^backup_manifest_invalid$"):
        verify_backup(archive)
    with pytest.raises(LocalBackupError, match="^backup_manifest_invalid$"):
        restore_backup(archive, destination, restore_root=restore_root)
    assert not destination.exists()


@pytest.mark.parametrize("field,value", [
    ("bytes", None), ("bytes", True), ("bytes", -1), ("bytes", 1.0),
    ("sha256", None), ("sha256", "g" * 64),
    ("schema_sha256", None), ("schema_sha256", "f" * 63),
    ("row_counts", None), ("row_counts", []),
    ("row_counts", {"meetings": True}), ("row_counts", {"meetings": -1}),
    ("row_counts", {"meetings": 1.0}),
    ("user_version", None), ("user_version", False), ("user_version", 1.0),
])
def test_invalid_database_manifest_field_types_are_rejected_before_restore(tmp_path, field, value):
    source = tmp_path / "source-data"
    source.mkdir()
    _fixture(source)
    archive = tmp_path / "backup"
    create_backup(source, archive)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["databases"]["secretary.sqlite3"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restore_root = tmp_path / "restores"
    restore_root.mkdir()
    destination = restore_root / "copy"

    with pytest.raises(LocalBackupError, match="^backup_manifest_invalid$"):
        verify_backup(archive)
    with pytest.raises(LocalBackupError, match="^backup_manifest_invalid$"):
        restore_backup(archive, destination, restore_root=restore_root)
    assert not destination.exists()


def test_database_manifest_keeps_extension_fields_and_signed_sqlite_version_compatible(tmp_path):
    source = tmp_path / "source-data"
    source.mkdir()
    _fixture(source)
    with sqlite3.connect(source / "secretary.sqlite3") as conn:
        conn.execute("PRAGMA user_version=-1")
    archive = tmp_path / "backup"
    create_backup(source, archive)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["databases"]["secretary.sqlite3"]["extension_note"] = "synthetic"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert verify_backup(archive)["state"] == "verified"
    restore_root = tmp_path / "restores"
    restore_root.mkdir()
    assert restore_backup(archive, restore_root / "copy", restore_root=restore_root)["state"] == "verified"


def _directory_link(link: Path, target: Path):
    if os.name == "nt":
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def test_explicit_test_directory_exclusion_preserves_source_and_requires_restore_acknowledgement(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    db, meeting, media, _, _, _ = _fixture(source)
    legacy = source / "test-audio-tmp"
    legacy.mkdir()
    payload = legacy / "old-run"
    payload.mkdir()
    (payload / "sample.wav").write_bytes(b"synthetic-test")
    _directory_link(legacy / "current", payload)
    nested = source / "working" / "test-audio-tmp"
    nested.mkdir(parents=True)
    (nested / "keep.txt").write_text("working nested file", encoding="utf-8")
    with pytest.raises(LocalBackupError, match="^backup_reparse_forbidden$"):
        create_backup(source, tmp_path / "default-refused")

    archive = tmp_path / "explicit-backup"
    receipt = create_backup(source, archive, exclude_test_audio_tmp=True)
    manifest = json.loads((archive / "manifest.json").read_text("utf-8"))
    for result in (manifest, receipt, verify_backup(archive)):
        assert result["schema_version"] == 2
        assert result["full_recovery"] is False
        assert result["excluded_directories"] == ["test-audio-tmp"]
    assert not (archive / "assets" / "test-audio-tmp").exists()
    assert (archive / "assets" / "working" / "test-audio-tmp" / "keep.txt").is_file()
    assert (legacy / "current" / "sample.wav").read_bytes() == b"synthetic-test"
    restore_root = tmp_path / "restores"
    restore_root.mkdir()
    destination = restore_root / "copy"
    with pytest.raises(LocalBackupError, match="^restore_exclusion_acknowledgement_required$"):
        restore_backup(archive, destination, restore_root=restore_root)
    assert not destination.exists()
    restored = restore_backup(archive, destination, restore_root=restore_root,
                              accept_excluded_test_audio_tmp=True)
    assert restored["schema_version"] == 2
    assert restored["full_recovery"] is False
    assert restored["excluded_directories"] == ["test-audio-tmp"]
    assert restored["outbound_enabled"] is False
    assert json.loads((destination / "restore.json").read_text("utf-8")) == restored
    assert not (destination / "test-audio-tmp").exists()
    restored_db = Database(destination / "secretary.sqlite3")
    restored_media = Path(restored_db.meeting(meeting["id"], internal=True)["media_path"])
    assert restored_media.read_bytes() == media.read_bytes()
    assert restored_db.configuration()["cloud_enabled"] is False
    assert db.configuration()["cloud_enabled"] is True


@pytest.mark.parametrize("reference", ["media", "chunk", "capture"])
def test_exclusion_rejects_working_references_into_excluded_directory(tmp_path, reference):
    source = tmp_path / "source"
    source.mkdir()
    db, meeting, _, _, _, _ = _fixture(source)
    legacy = source / "test-audio-tmp"
    legacy.mkdir()
    asset = legacy / "needed.wav"
    asset.write_bytes(b"synthetic-required")
    if reference == "media":
        db.update_meeting(meeting["id"], media_path=str(asset))
    elif reference == "chunk":
        with sqlite3.connect(source / "secretary.sqlite3") as conn:
            conn.execute("UPDATE chunks SET path=?", (str(asset),))
    else:
        capture = source / "audio" / meeting["id"] / "capture.json"
        capture.write_text(json.dumps({"chunks": [{"path": str(asset)}]}), encoding="utf-8")
    code = "backup_capture_asset_missing" if reference == "capture" else "backup_asset_reference_missing"
    archive = tmp_path / "backup"
    with pytest.raises(LocalBackupError, match="^" + code + "$"):
        create_backup(source, archive, exclude_test_audio_tmp=True)
    assert not (archive / "manifest.json").exists()
    assert asset.read_bytes() == b"synthetic-required"


@pytest.mark.parametrize("link_location", ["excluded_root", "other_root", "nested_name"])
def test_exclusion_never_skips_root_reparse_or_other_links(tmp_path, link_location):
    source = tmp_path / "source"
    source.mkdir()
    _fixture(source)
    target = tmp_path / "external-synthetic"
    target.mkdir()
    if link_location == "excluded_root":
        link = source / "test-audio-tmp"
    elif link_location == "other_root":
        link = source / "other-current"
    else:
        parent = source / "working" / "test-audio-tmp"
        parent.mkdir(parents=True)
        link = parent / "current"
    _directory_link(link, target)
    archive = tmp_path / "backup"
    with pytest.raises(LocalBackupError, match="^backup_reparse_forbidden$"):
        create_backup(source, archive, exclude_test_audio_tmp=True)
    assert not (archive / "manifest.json").exists()


@pytest.mark.parametrize("entry_kind", ["file", "case_alias"])
def test_source_exclusion_requires_exact_real_directory(tmp_path, entry_kind):
    source = tmp_path / "source"
    source.mkdir()
    _fixture(source)
    if entry_kind == "file":
        (source / "test-audio-tmp").write_bytes(b"not-a-directory")
    else:
        (source / "TEST-AUDIO-TMP").mkdir()
    archive = tmp_path / "backup"
    with pytest.raises(LocalBackupError, match="^backup_excluded_directory_invalid$"):
        create_backup(source, archive, exclude_test_audio_tmp=True)
    assert not (archive / "manifest.json").exists()


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2.0}, {"schema_version": 3},
    {"full_recovery": True}, {"full_recovery": 0},
    {"excluded_directories": []}, {"excluded_directories": "test-audio-tmp"},
    {"excluded_directories": ["test-audio-tmp", "audio"]},
    {"excluded_directories": ["test-audio-tmp", "test-audio-tmp"]},
    {"excluded_directories": ["audio/test-audio-tmp"]},
    {"excluded_directories": ["TEST-AUDIO-TMP"]}, {"extra": "invalid"},
])
def test_v2_rejects_ambiguous_scope_before_restore(tmp_path, changes):
    source = tmp_path / "source"
    source.mkdir()
    _fixture(source)
    archive = tmp_path / "backup"
    create_backup(source, archive, exclude_test_audio_tmp=True)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest.update(changes)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restore_root = tmp_path / "restores"
    restore_root.mkdir()
    destination = restore_root / "copy"
    with pytest.raises(LocalBackupError):
        verify_backup(archive)
    with pytest.raises(LocalBackupError):
        restore_backup(archive, destination, restore_root=restore_root,
                       accept_excluded_test_audio_tmp=True)
    assert not destination.exists()


@pytest.mark.parametrize("tamper", ["declared_asset", "case_alias_asset", "extra_file", "archive_link"])
def test_v2_archive_cannot_contain_excluded_content_or_hide_links(tmp_path, tamper):
    source = tmp_path / "source"
    source.mkdir()
    _fixture(source)
    archive = tmp_path / "backup"
    create_backup(source, archive, exclude_test_audio_tmp=True)
    if tamper == "archive_link":
        target = tmp_path / "external-synthetic"
        target.mkdir()
        _directory_link(archive / "assets" / "test-audio-tmp", target)
        expected = "backup_reparse_forbidden"
    else:
        directory = "TEST-AUDIO-TMP" if tamper == "case_alias_asset" else "test-audio-tmp"
        asset = archive / "assets" / directory / "sample.wav"
        asset.parent.mkdir()
        asset.write_bytes(b"forged")
        expected = "backup_file_set_mismatch"
        if tamper in {"declared_asset", "case_alias_asset"}:
            manifest_path = archive / "manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["assets"].append({"path": directory + "/sample.wav", "bytes": 6,
                                      "sha256": hashlib.sha256(b"forged").hexdigest()})
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            expected = "backup_manifest_invalid"
    with pytest.raises(LocalBackupError, match="^" + expected + "$"):
        verify_backup(archive)


def test_v1_default_manifest_and_receipt_shapes_are_unchanged(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _fixture(source)
    legacy = source / "test-audio-tmp"
    legacy.mkdir()
    (legacy / "file.txt").write_text("retained by default", encoding="utf-8")
    archive = tmp_path / "backup"
    created = create_backup(source, archive)
    verified = verify_backup(archive)
    assert set(created) == set(verified) == {"state", "backup_id", "database_count", "asset_count", "total_bytes"}
    manifest = json.loads((archive / "manifest.json").read_text("utf-8"))
    assert set(manifest) == {"schema_version", "state", "full_recovery", "backup_id", "created_at",
                             "application_version", "source_data_dir", "databases", "assets"}
    assert manifest["schema_version"] == 1
    assert manifest["full_recovery"] is True
    assert (archive / "assets" / "test-audio-tmp" / "file.txt").is_file()
    restores = tmp_path / "restores"
    restores.mkdir()
    restored = restore_backup(archive, restores / "copy", restore_root=restores)
    assert set(restored) == {"schema_version", "restore_id", "state", "backup_id", "backup_manifest_sha256",
                              "restored_at", "outbound_enabled"}
    assert restored["schema_version"] == 1


@pytest.mark.parametrize("version", [True, 1.0])
def test_v1_schema_version_requires_an_integer_not_equal_boolean_or_float(tmp_path, version):
    source = tmp_path / "source"
    source.mkdir()
    _fixture(source)
    archive = tmp_path / "backup"
    create_backup(source, archive)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["schema_version"] = version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(LocalBackupError, match="^backup_incomplete$"):
        verify_backup(archive)
