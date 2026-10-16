import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.infrastructure.instance_lock import DataDirectoryLock, DataDirectoryLockError
from secretary.settings import Settings
from secretary.team_settings import RuntimeConfigurationError

ROOT = Path(__file__).resolve().parents[1]


class QuietCapture:
    def close(self):
        pass


def test_create_app_rejects_a_second_owner_of_the_same_data_dir(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", polza_api_key="synthetic-only")
    first = create_app(settings, capture=QuietCapture(), enrollment_capture=QuietCapture(), run_worker=False)
    database = settings.data_dir / "secretary.sqlite3"
    original = hashlib.sha256(database.read_bytes()).hexdigest()

    with TestClient(first, base_url="http://127.0.0.1:8765"):
        with pytest.raises(RuntimeConfigurationError, match="data_directory_in_use"):
            create_app(settings.model_copy(deep=True), capture=QuietCapture(),
                       enrollment_capture=QuietCapture(), run_worker=False)
        assert hashlib.sha256(database.read_bytes()).hexdigest() == original

        child_code = """
import sys
from secretary.api import create_app
from secretary.settings import Settings
from secretary.team_settings import RuntimeConfigurationError
settings = Settings(_env_file=None, data_dir=sys.argv[1], polza_api_key='synthetic-only')
try:
    create_app(settings, run_worker=False)
except RuntimeConfigurationError as error:
    print(error.code)
    raise SystemExit(23)
print('second-instance-created')
"""
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(ROOT / "backend")
        child = subprocess.run([sys.executable, "-B", "-c", child_code, str(settings.data_dir)],
                               cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=15)
        assert child.returncode == 23
        assert child.stdout.strip() == "data_directory_in_use"
        assert hashlib.sha256(database.read_bytes()).hexdigest() == original

    reopened = create_app(settings.model_copy(deep=True), capture=QuietCapture(),
                          enrollment_capture=QuietCapture(), run_worker=False)
    with TestClient(reopened, base_url="http://127.0.0.1:8766") as api:
        assert api.get("/health").status_code == 200


def test_instance_lock_distinguishes_unavailable_path_from_an_active_owner(tmp_path):
    data_path = tmp_path / "not-a-directory"
    data_path.write_text("synthetic", encoding="utf-8")
    with pytest.raises(DataDirectoryLockError) as caught:
        DataDirectoryLock(data_path)
    assert caught.value.code == "data_directory_lock_unavailable"

    data_path.unlink()
    with DataDirectoryLock(data_path):
        assert data_path.is_dir()
