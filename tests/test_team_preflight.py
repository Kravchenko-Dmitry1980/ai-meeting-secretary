"""The offline test entry point must refuse live state and external effects."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run_offline_tests.py"
PREFLIGHT = ROOT / "scripts/team/check_prerequisites.ps1"


def invoke_runner(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    assert RUNNER.is_file(), "offline runner has not been implemented"
    suite = tmp_path / "test_isolation_fixture.py"
    suite.write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-B", str(RUNNER), str(suite), "-q"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )


def test_offline_runner_rejects_external_http_and_production_data(tmp_path):
    result = invoke_runner(tmp_path, f'''
from pathlib import Path
import socket, sqlite3
import pytest
from run_offline_tests import check_path

def test_network_and_live_data_are_denied():
    with socket.socket() as sock:
        with pytest.raises(PermissionError, match="offline"):
            sock.connect(("203.0.113.1", 443))
    with pytest.raises(PermissionError, match="offline"):
        Path({str(ROOT / '.env')!r}).read_text()
    with pytest.raises(PermissionError, match="offline"):
        sqlite3.connect({str(ROOT / 'data/secretary.sqlite3')!r})
    with pytest.raises(PermissionError, match="offline"):
        check_path(Path.home() / "offline-must-not-write", writing=True)

def test_synthetic_data_is_allowed(tmp_path):
    with sqlite3.connect(tmp_path / "synthetic.sqlite3") as conn:
        assert conn.execute("select 1").fetchone() == (1,)

def test_native_audio_fallback_is_denied():
    from secretary.infrastructure.audio import AudioCapture
    from types import SimpleNamespace
    capture = AudioCapture(SimpleNamespace(data_dir=Path.cwd(), chunk_seconds=120))
    with pytest.raises(PermissionError, match="offline"):
        capture._audio()
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout


def test_offline_runner_propagates_guard_to_python_children(tmp_path):
    result = invoke_runner(tmp_path, '''
import subprocess, sys

def test_child_is_guarded():
    script = "import socket; s=socket.socket();\\ntry: s.connect(('203.0.113.1',443))\\nexcept PermissionError: print('CHILD_GUARDED')"
    result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'CHILD_GUARDED' in result.stdout
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_offline_runner_rejects_live_lifecycle_and_keeps_exit_code(tmp_path):
    result = invoke_runner(tmp_path, f'''
import subprocess, sys
import pytest

def test_lifecycle_is_denied():
    with pytest.raises(PermissionError, match="offline"):
        subprocess.run([sys.executable, {str(ROOT / 'scripts/check_lifecycle.py')!r}])
''')
    assert result.returncode == 0, result.stdout + result.stderr
    failure = invoke_runner(tmp_path, "def test_failure():\n    assert False, 'synthetic failure'\n")
    assert failure.returncode == 1
    assert "1 failed" in failure.stdout


def test_offline_runner_does_not_read_inherited_credentials(tmp_path):
    assert RUNNER.is_file(), "offline runner has not been implemented"
    suite = tmp_path / "test_credentials.py"
    suite.write_text("import os\ndef test_key():\n    assert not os.environ.get('POLZA_API_KEY')\n", encoding="utf-8")
    env = {**os.environ, "POLZA_API_KEY": "synthetic-parent-key"}
    result = subprocess.run([sys.executable, "-B", str(RUNNER), str(suite), "-q"],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic-parent-key" not in result.stdout + result.stderr


def test_offline_runner_restores_tempdir_between_tests(tmp_path):
    result = invoke_runner(tmp_path, '''
import tempfile
ORIGINAL = tempfile.tempdir
def test_changes_process_temp_directory(tmp_path):
    tempfile.tempdir = str(tmp_path)
def test_next_test_is_not_contaminated():
    assert tempfile.tempdir == ORIGINAL
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_preflight_is_read_only_and_reports_unknown_ingress(tmp_path):
    assert PREFLIGHT.is_file(), "read-only preflight has not been implemented"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-File", str(PREFLIGHT)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["read_only"] is True
    assert data["network"]["public_https"]["status"] == "unknown"
    assert data["network"]["cgnat"]["status"] == "unknown"
    assert data["runtime"]["python"]["available"] is True
    assert data["host"]["architecture"]
    assert data["backup"]["separate_destination"]["status"] == "unknown"
    assert "POLZA_API_KEY" not in result.stdout
