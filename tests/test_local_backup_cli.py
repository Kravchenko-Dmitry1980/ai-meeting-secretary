from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from secretary.infrastructure.database import Database


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_backup.py"


def _run(*arguments: object) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *(str(argument) for argument in arguments)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cli_backup_verify_and_restore_emits_path_free_receipts(tmp_path):
    source = tmp_path / "source-data"
    source.mkdir()
    Database(source / "secretary.sqlite3").create_meeting("Synthetic CLI sample")
    archive = tmp_path / "backup"
    restore_root = tmp_path / "restores"
    restore_root.mkdir()

    backup = _run("backup", "--data-dir", source, "--destination", archive)
    verified = _run("verify", "--backup-dir", archive)
    restored = _run("restore", "--backup-dir", archive, "--destination", restore_root / "copy",
                    "--restore-root", restore_root)

    assert backup["state"] == "complete"
    assert verified["state"] == "verified"
    assert restored["state"] == "verified"
    assert restored["outbound_enabled"] is False
    assert str(source) not in json.dumps(backup)
    assert str(archive) not in json.dumps(verified)
    with sqlite3.connect(restore_root / "copy" / "secretary.sqlite3") as conn:
        settings = dict(conn.execute("SELECT key,value FROM configuration"))
    assert json.loads(settings["cloud_enabled"]) is False


def test_cli_invalid_database_manifest_returns_path_free_json_without_restore(tmp_path):
    source = tmp_path / "source-data"
    source.mkdir()
    Database(source / "secretary.sqlite3").create_meeting("Synthetic invalid manifest")
    archive = tmp_path / "backup"
    _run("backup", "--data-dir", source, "--destination", archive)
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    del manifest["databases"]["secretary.sqlite3"]["bytes"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restore_root = tmp_path / "restores"
    restore_root.mkdir()
    destination = restore_root / "copy"

    for arguments in [
        ("verify", "--backup-dir", archive),
        ("restore", "--backup-dir", archive, "--destination", destination, "--restore-root", restore_root),
    ]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *(str(argument) for argument in arguments)],
            cwd=ROOT, text=True, capture_output=True, timeout=30, check=False,
        )
        assert result.returncode == 2
        assert result.stdout == ""
        assert json.loads(result.stderr) == {"ok": False, "code": "backup_manifest_invalid"}
        assert "Traceback" not in result.stderr
        assert str(source) not in result.stderr
        assert str(archive) not in result.stderr
        assert not destination.exists()


def test_cli_explicit_test_exclusion_and_restore_acknowledgement(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    Database(source / "secretary.sqlite3").create_meeting("Synthetic exclusion")
    legacy = source / "test-audio-tmp"
    legacy.mkdir()
    (legacy / "old.txt").write_text("synthetic", encoding="utf-8")
    archive = tmp_path / "backup"
    receipt = _run("backup", "--data-dir", source, "--destination", archive, "--exclude-test-audio-tmp")
    verified = _run("verify", "--backup-dir", archive)
    for value in (receipt, verified):
        assert value["schema_version"] == 2
        assert value["full_recovery"] is False
        assert value["excluded_directories"] == ["test-audio-tmp"]
        assert str(source) not in json.dumps(value)
    restore_root = tmp_path / "restores"
    restore_root.mkdir()
    destination = restore_root / "copy"
    arguments = ("restore", "--backup-dir", archive, "--destination", destination, "--restore-root", restore_root)
    refused = subprocess.run([sys.executable, str(SCRIPT), *(str(arg) for arg in arguments)],
                             cwd=ROOT, text=True, capture_output=True, timeout=30, check=False)
    assert refused.returncode == 2
    assert refused.stdout == ""
    assert json.loads(refused.stderr) == {"ok": False, "code": "restore_exclusion_acknowledgement_required"}
    assert not destination.exists()
    restored = _run(*arguments, "--accept-excluded-test-audio-tmp")
    assert restored["schema_version"] == 2
    assert restored["full_recovery"] is False
    assert restored["excluded_directories"] == ["test-audio-tmp"]
    assert restored["outbound_enabled"] is False
    assert (legacy / "old.txt").read_text("utf-8") == "synthetic"
    assert not (destination / "test-audio-tmp").exists()
