from __future__ import annotations

import hashlib
import json
import sqlite3
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
