from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


RUN_ID = "1a2b3c4d-5e6f-4789-8abc-0123456789ab"


def load_check():
    path = Path(__file__).parents[1] / "scripts" / "check_lifecycle.py"
    spec = importlib.util.spec_from_file_location("secretary_isolated_lifecycle_check", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_lifecycle_child_uses_only_scratch_data_and_synthetic_offline_configuration(tmp_path):
    check = load_check()
    build = getattr(check, "build_server_command", None)
    assert callable(build), "lifecycle check must expose its isolated launch boundary"

    root = tmp_path / "project with spaces"
    data_dir = tmp_path / "scratch data" / "sqlite"
    parent_env = {
        "PATH": "C:\\Windows\\System32",
        "SYSTEMROOT": "C:\\Windows",
        "TEMP": str(tmp_path),
        "POLZA_API_KEY": "owner-key-must-not-inherit",
        "DATA_DIR": "D:\\owner\\data",
        "VIKUNJA_TOKEN": "owner-token-must-not-inherit",
    }

    command, child_env = build(
        root,
        data_dir=data_dir,
        port=49152,
        run_id=RUN_ID,
        python_executable=Path("C:/Secretary/.venv/Scripts/python.exe"),
        parent_env=parent_env,
    )

    assert command == [
        "C:\\Secretary\\.venv\\Scripts\\python.exe",
        "-B",
        str(root / "scripts" / "run_server.py"),
        "--port",
        "49152",
        "--run-id",
        RUN_ID,
        "--offline",
        "--isolated-offline",
    ]
    assert child_env["DATA_DIR"] == str(data_dir.resolve())
    assert child_env["POLZA_BASE_URL"] == "https://polza.invalid/api/v1"
    assert child_env["CLOUD_ENABLED"] == "false"
    assert child_env["PATH"] == parent_env["PATH"]
    assert "POLZA_API_KEY" not in child_env
    assert "VIKUNJA_TOKEN" not in child_env
    assert all("owner-" not in value for value in child_env.values())


def test_lifecycle_stop_marker_is_scoped_to_its_run_id(tmp_path):
    check = load_check()
    marker_path = getattr(check, "stop_marker_path", None)
    assert callable(marker_path), "lifecycle stop must use a run-specific marker"

    root = tmp_path / "project"
    expected = root / ".runtime" / f"stop-{RUN_ID}.request"
    other_run = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    assert marker_path(root, RUN_ID) == expected
    assert marker_path(root, other_run) != expected


class ReadyResponse:
    status_code = 200

    def __init__(self, report):
        self.report = report

    def json(self):
        return self.report


class ReadyClient:
    def __init__(self, report):
        self.report = report

    def get(self, path):
        assert path == "/ready"
        return ReadyResponse(self.report)


def test_lifecycle_readiness_requires_production_build_and_verified_lockfiles():
    check = load_check()
    components = {
        "database": {"state": "healthy"},
        "storage": {"state": "healthy"},
        "processing_worker": {"state": "healthy"},
        "frontend_build": {"state": "available"},
        "release_parity": {
            "state": "healthy", "build_check": "vite-production", "lockfiles_verified": True,
        },
        "cloud": {"state": "disabled"},
        "device_capture": {"state": "not_qualified"},
        "backup": {"state": "not_checked"},
    }
    report = {
        "status": "local_ok", "production_qualified": False,
        "components": components, "jobs": {"state": "healthy"},
    }

    assert check._local_readiness_is_safe(ReadyClient(report)) is True
    components["release_parity"]["lockfiles_verified"] = False
    assert check._local_readiness_is_safe(ReadyClient(report)) is False
