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
