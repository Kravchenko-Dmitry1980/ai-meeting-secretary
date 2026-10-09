"""Run an isolated, offline Secretary start/stop/restart check.

This check never uses start.ps1/stop.ps1, the tracked production state file,
the configured data directory, or project credentials. It owns one disposable
data directory and only the child processes started from this script.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import UUID, uuid4

import httpx


SAFE_PARENT_ENV = {
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC",
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
}


def _canonical_run_id(value: str) -> str:
    try:
        canonical = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid_lifecycle_run_id") from None
    if canonical != value:
        raise ValueError("invalid_lifecycle_run_id")
    return canonical


def build_server_command(root: Path, *, data_dir: Path, port: int, run_id: str,
                         python_executable: Path | None = None,
                         parent_env: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Build a child launch that cannot inherit provider credentials or data paths."""
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid_lifecycle_port")
    run_id = _canonical_run_id(run_id)
    root = Path(root).resolve()
    data_dir = Path(data_dir).resolve()
    python = str(python_executable or sys.executable)
    source_env = os.environ if parent_env is None else parent_env
    child_env = {key: value for key, value in source_env.items()
                 if key.upper() in SAFE_PARENT_ENV}
    child_env.update({
        "DATA_DIR": str(data_dir),
        "POLZA_BASE_URL": "https://polza.invalid/api/v1",
        "CLOUD_ENABLED": "false",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
    })
    command = [python, "-B", str(root / "scripts" / "run_server.py"),
               "--port", str(port), "--run-id", run_id,
               "--offline", "--isolated-offline"]
    return command, child_env


def stop_marker_path(root: Path, run_id: str) -> Path:
    run_id = _canonical_run_id(run_id)
    return Path(root).resolve() / ".runtime" / f"stop-{run_id}.request"


def _free_port(excluded: set[int]) -> int:
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        if port not in excluded:
            return port
    raise RuntimeError("no_distinct_loopback_port")


def _wait_ready(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 30
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1,
                      trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("isolated_server_exited_before_ready")
            try:
                response = client.get("/health")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.2)
    raise RuntimeError("isolated_server_readiness_timeout")


def _stop_server(root: Path, handle: dict) -> None:
    process = handle["process"]
    if process.poll() is None:
        marker = stop_marker_path(root, handle["run_id"])
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            with marker.open("x", encoding="ascii") as stream:
                stream.write(handle["run_id"])
        except FileExistsError:
            raise RuntimeError("isolated_stop_marker_collision") from None
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise RuntimeError("isolated_server_graceful_stop_timeout") from None
        finally:
            marker.unlink(missing_ok=True)
    if process.returncode != 0:
        raise RuntimeError("isolated_server_exit_nonzero")


def _start_server(root: Path, run_root: Path, data_dir: Path, port: int) -> dict:
    run_id = str(uuid4())
    command, child_env = build_server_command(root, data_dir=data_dir, port=port,
        run_id=run_id, python_executable=root / ".venv" / "Scripts" / "python.exe")
    stdout_path = run_root / f"server-{run_id}.stdout.log"
    stderr_path = run_root / f"server-{run_id}.stderr.log"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        process = subprocess.Popen(command, cwd=root, env=child_env,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            creationflags=flags)
    handle = {"process": process, "port": port, "run_id": run_id}
    try:
        _wait_ready(process, port)
    except Exception:
        try:
            _stop_server(root, handle)
        except Exception:
            pass
        raise
    return handle


def _configuration_is_offline(client: httpx.Client) -> bool:
    response = client.get("/api/v1/config")
    return response.status_code == 200 and response.json().get("cloud_enabled") is False


def _local_readiness_is_safe(client: httpx.Client) -> bool:
    response = client.get("/ready")
    if response.status_code != 200:
        return False
    report = response.json()
    components = report.get("components", {})
    return (report.get("status") == "local_ok"
            and report.get("production_qualified") is False
            and components.get("database", {}).get("state") == "healthy"
            and components.get("storage", {}).get("state") == "healthy"
            and components.get("processing_worker", {}).get("state") == "healthy"
            and components.get("frontend_build", {}).get("state") == "available"
            and components.get("release_parity", {}).get("state") == "healthy"
            and components.get("cloud", {}).get("state") == "disabled"
            and components.get("device_capture", {}).get("state") == "not_qualified"
            and components.get("backup", {}).get("state") == "not_checked"
            and report.get("jobs", {}).get("state") == "healthy")


def _storage_probe_is_safe(client: httpx.Client, data_dir: Path) -> bool:
    session = client.get("/api/v1/session")
    if session.status_code != 200:
        return False
    response = client.post("/ready/storage-probe", headers={
        "X-Secretary-Token": session.json().get("csrf_token", "")})
    return (response.status_code == 200 and response.json() == {"state": "healthy"}
            and not any(Path(data_dir).glob(".secretary-readiness-*")))


def _remove_owned_run(root: Path, run_root: Path) -> None:
    expected_parent = (root / ".runtime" / "lifecycle-isolated").resolve()
    target = run_root.resolve()
    if target.parent != expected_parent or not target.name.startswith("run-"):
        raise RuntimeError("lifecycle_cleanup_scope_invalid")
    shutil.rmtree(target)


def run_check(root: Path) -> dict:
    root = Path(root).resolve()
    runtime_root = root / ".runtime" / "lifecycle-isolated"
    runtime_root.mkdir(parents=True, exist_ok=True)
    run_root = runtime_root / f"run-{uuid4().hex}"
    run_root.mkdir()
    data_dir = run_root / "data"
    active: list[dict] = []
    used_ports: set[int] = set()
    success = False
    try:
        first = _start_server(root, run_root, data_dir, _free_port(used_ports))
        used_ports.add(first["port"])
        active.append(first)
        with httpx.Client(base_url=f"http://127.0.0.1:{first['port']}",
                          timeout=5, trust_env=False) as client:
            if not _configuration_is_offline(client):
                raise RuntimeError("isolated_server_cloud_not_disabled")
            if not _local_readiness_is_safe(client):
                raise RuntimeError("isolated_server_readiness_failed")
            if not _storage_probe_is_safe(client, data_dir):
                raise RuntimeError("isolated_server_storage_probe_failed")
            session = client.get("/api/v1/session")
            if session.status_code != 200 or not session.json().get("csrf_token"):
                raise RuntimeError("isolated_session_token_unavailable")
            created = client.post("/api/v1/meetings", json={
                "title": f"Lifecycle synthetic {uuid4().hex[:8]}"
            }, headers={"X-Secretary-Token": session.json()["csrf_token"]})
            if created.status_code != 201:
                raise RuntimeError("isolated_meeting_create_failed")
            meeting_id = created.json()["id"]
        _stop_server(root, first)
        active.remove(first)

        second = _start_server(root, run_root, data_dir, _free_port(used_ports))
        used_ports.add(second["port"])
        active.append(second)
        with httpx.Client(base_url=f"http://127.0.0.1:{second['port']}",
                          timeout=5, trust_env=False) as client:
            if not _configuration_is_offline(client):
                raise RuntimeError("isolated_restart_cloud_not_disabled")
            if not _local_readiness_is_safe(client):
                raise RuntimeError("isolated_restart_readiness_failed")
            if not _storage_probe_is_safe(client, data_dir):
                raise RuntimeError("isolated_restart_storage_probe_failed")
            meetings = client.get("/api/v1/meetings")
            if meetings.status_code != 200:
                raise RuntimeError("isolated_meetings_read_failed")
            if not any(item.get("id") == meeting_id for item in meetings.json()):
                raise RuntimeError("isolated_meeting_not_persisted")
        _stop_server(root, second)
        active.remove(second)
        if any(item["process"].poll() is None for item in (first, second)):
            raise RuntimeError("isolated_server_left_running")
        success = True
        return {
            "status": "PASS",
            "offline": True,
            "project_env_file_loaded": False,
            "credentials_inherited": False,
            "scratch_data_only": True,
            "unique_loopback_ports": len(used_ports) == 2,
            "meeting_persisted_after_restart": True,
            "readiness_report_verified_after_start_and_restart": True,
            "storage_write_probe_cleaned_after_start_and_restart": True,
            "test_meetings": 1,
            "test_servers_stopped": True,
        }
    finally:
        for handle in reversed(active):
            try:
                _stop_server(root, handle)
            except Exception:
                pass
        if success and not active:
            _remove_owned_run(root, run_root)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        result = run_check(root)
    except Exception as error:
        print(json.dumps({"status": "FAIL", "error_code": str(error)[:120]}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
