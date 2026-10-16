"""Owned, bounded Windows supervision of one Offline Secretary instance.

No Scheduler registration, process adoption, force kill or network provisioning.
The single processes.json is retained after stop/failure for diagnosis. Importing
this module performs no process, filesystem or native Job operations.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from uuid import UUID, uuid4

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
RESTART_DELAYS = (1, 2, 4)


class SupervisorError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def safe_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path).startswith(("\\\\", "//")):
        raise SupervisorError("absolute_local_path_required")
    for candidate in (path, *path.parents):
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if candidate.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise SupervisorError("reparse_path_forbidden")
            if candidate.is_file() and info.st_nlink != 1:
                raise SupervisorError("hardlink_path_forbidden")
    return path.resolve()


def runtime_path(value=None):
    base = safe_path(ROOT / ".runtime")
    path = safe_path(value if value is not None else base)
    if not path.is_relative_to(base):
        raise SupervisorError("runtime_outside_project")
    return path


def canonical_uuid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise SupervisorError("invalid_supervisor_run_id") from None
    return value


def stop_path(runtime, run_id):
    return safe_path(runtime / ("supervisor-stop-" + canonical_uuid(run_id) + ".request"))


def atomic_json(path, value):
    path = safe_path(path)
    temporary = safe_path(path.with_name(".write-" + uuid4().hex + ".tmp"))
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 19:
                raise SupervisorError("state_replace_busy") from None
            time.sleep(.01)


def read_state(runtime):
    path = safe_path(runtime / "processes.json")
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 128 * 1024:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise SupervisorError("invalid_owner_state") from None


@contextmanager
def owner_lease(runtime):
    """Same lock file as start.ps1 FileShare.None; never remove a live lock."""
    import msvcrt
    runtime.mkdir(parents=True, exist_ok=True)
    path = safe_path(runtime / "local-owner.lock")
    try:
        stream = path.open("a+b")
    except OSError:
        raise SupervisorError("local_owner_busy") from None
    locked = False
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError:
            raise SupervisorError("local_owner_busy") from None
        yield
    finally:
        if locked:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


def identity(pid=None):
    from secretary.infrastructure.team_process_identity import process_identity
    try:
        return process_identity(pid)
    except ValueError:
        raise SupervisorError("process_identity_unavailable") from None


def identity_alive(record):
    from secretary.infrastructure.team_process_identity import verify_dead_identity
    try:
        verify_dead_identity(record)
        return False
    except ValueError as exc:
        if str(exc) != "process_still_alive":
            raise SupervisorError("process_identity_unavailable") from None
    if identity(record["pid"]) != record:
        raise SupervisorError("process_identity_changed")
    return True


def require_supervisor_state(runtime, state):
    if (not state or state.get("owner_kind") != "local_supervisor"
            or state.get("schema_version") != 1 or state.get("project") != str(ROOT)
            or state.get("runtime_root") != str(runtime)):
        raise SupervisorError("supervisor_owner_state_required")
    canonical_uuid(state.get("supervisor_run_id"))
    return state


def status(runtime):
    state = read_state(runtime)
    if state is None:
        return {"state": "not_tracked", "supervisor_alive": False}
    require_supervisor_state(runtime, state)
    result = dict(state)
    result["supervisor_alive"] = identity_alive(state.get("supervisor_identity"))
    if not result["supervisor_alive"] and state.get("state") not in ("stopped", "failed"):
        result["state"] = "owner_exited"
    return result


def request_stop(runtime, *, timeout=35):
    state = require_supervisor_state(runtime, read_state(runtime))
    alive = identity_alive(state.get("supervisor_identity"))
    # Retain the intent even if the owner exited during observation. The UUID
    # binds it to this exact supervision lifetime, never a future explicit run.
    marker = stop_path(runtime, state["supervisor_run_id"])
    if not marker.exists():
        with marker.open("x", encoding="ascii") as stream:
            stream.write(state["supervisor_run_id"])
            stream.flush()
            os.fsync(stream.fileno())
    deadline = time.monotonic() + timeout
    while alive and time.monotonic() < deadline:
        time.sleep(.1)
        alive = identity_alive(state["supervisor_identity"])
    if alive:
        raise SupervisorError("graceful_stop_timeout")
    current = require_supervisor_state(runtime, read_state(runtime))
    if current["supervisor_run_id"] != state["supervisor_run_id"]:
        raise SupervisorError("owner_changed_during_stop")
    for record in current.get("attempt_identities", []):
        if identity_alive(record):
            raise SupervisorError("owned_child_still_running")
    return {"state": current["state"], "supervisor_alive": False, "stop_requested": True}


def prepare_manual(runtime):
    """Archive a terminal receipt; start.ps1 holds the shared exclusive lease.

    This helper never releases ownership, removes state, or starts a process.
    A direct CLI invocation can only preserve evidence of a dead lifetime.
    """
    state = require_supervisor_state(runtime, read_state(runtime))
    if state.get("state") not in ("stopped", "failed"):
        raise SupervisorError("terminal_supervisor_state_required")
    for record in [state.get("supervisor_identity"), *state.get("attempt_identities", [])]:
        if identity_alive(record):
            raise SupervisorError("existing_owner_alive")
    if read_state(runtime) != state:
        raise SupervisorError("owner_changed_during_handoff")
    directory = safe_path(runtime / "local-supervisor" / canonical_uuid(state["supervisor_run_id"]))
    directory.mkdir(parents=True, exist_ok=True)
    archive = safe_path(directory / "terminal-state.json")
    if archive.exists():
        if json.loads(archive.read_text(encoding="utf-8")) != state:
            raise SupervisorError("terminal_archive_conflict")
    else:
        with archive.open("x", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    return {"state": "terminal_archived", "supervisor_run_id": state["supervisor_run_id"]}


def capture_runtime_candidate():
    """Candidate evidence is optional for startup, mandatory for P6 qualification."""
    try:
        from local_candidate import capture_candidate
        value = capture_candidate(ROOT)
        if not isinstance(value, dict) or value.get("state") not in ("bound", "unbound"):
            raise ValueError("invalid_candidate_capture")
        if value["state"] == "bound":
            if (set(value) != {"state", "project", "git_sha", "frontend_sha256"}
                    or value["project"] != str(ROOT)
                    or not re.fullmatch(r"[0-9a-f]{40}", str(value["git_sha"]))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(value["frontend_sha256"]))):
                raise ValueError("invalid_candidate_capture")
        elif (set(value) != {"state", "code"} or not isinstance(value["code"], str)
              or not re.fullmatch(r"[a-z0-9_]{1,80}", value["code"])):
            raise ValueError("invalid_candidate_capture")
        return value
    except Exception:
        # An unavailable Git executable, dirty developer checkout or failing
        # diagnostic helper must not disable otherwise valid local operation.
        return {"state": "unbound", "code": "candidate_capture_failed"}


def run_loop(host):
    """Public policy seam: the host owns process I/O; this never infers identity."""
    restart_count = 0
    attempt = None
    try:
        while not host.stop_requested():
            host.publish("starting", restart_count=restart_count)
            attempt = host.start_attempt()  # Host checks durable stop again at spawn.
            if attempt is None:
                break
            host.await_ready(attempt)
            if not host.stop_requested():
                host.publish("running", restart_count=restart_count)
                exit_code = host.await_exit(attempt)
            else:
                exit_code = None
            host.drain_attempt(attempt)
            attempt = None
            if host.stop_requested():
                break
            if restart_count == len(RESTART_DELAYS):
                raise SupervisorError("restart_limit_exhausted")
            delay = RESTART_DELAYS[restart_count]
            restart_count += 1
            host.publish("backoff", restart_count=restart_count, last_exit_code=exit_code,
                         retry_delay_seconds=delay)
            host.backoff(delay)
        host.publish("stopped", restart_count=restart_count)
    except Exception as exc:
        if attempt is not None:
            try:
                host.drain_attempt(attempt)
            except Exception as drain_error:
                exc = drain_error
        error = exc.code if isinstance(exc, SupervisorError) else "supervisor_internal_error"
        host.publish("failed", restart_count=restart_count, error=error)
        raise SupervisorError(error) from None


class WindowsHost:
    def __init__(self, runtime, data_dir, port):
        self.runtime, self.data_dir, self.port = runtime, data_dir, port
        self.run_id = str(uuid4())
        self.run_root = safe_path(runtime / "local-supervisor" / self.run_id)
        self.run_root.mkdir(parents=True)
        self.owner = identity()
        self.fingerprint = self.configuration_fingerprint()
        self.state = {"schema_version": 1, "owner_kind": "local_supervisor",
                      "project": str(ROOT), "runtime_root": str(runtime), "data_dir": str(data_dir),
                      "port": port, "offline": True, "supervisor_run_id": self.run_id,
                      "supervisor_identity": self.owner, "attempt_identities": [],
                      "pid": self.owner["pid"], "launcher": str(ROOT / "scripts/local_supervisor.py"),
                      "restart_limit": len(RESTART_DELAYS), "configuration_sha256": self.fingerprint}
        self.initialize_candidate_binding()

    def initialize_candidate_binding(self):
        binding = capture_runtime_candidate()
        self._candidate_binding_invalidated = binding["state"] != "bound"
        self.state.update(candidate_binding=binding, candidate_binding_verified=False,
                          candidate_binding_reason=binding.get("code", "not_ready"))

    def check_candidate_binding(self, *, ready=False):
        current = capture_runtime_candidate()
        if self._candidate_binding_invalidated:
            return
        if current != self.state["candidate_binding"]:
            # Never bind a later checkout to a process launched from earlier
            # code, even if the files subsequently return to their old state.
            self._candidate_binding_invalidated = True
            self.state.update(candidate_binding_verified=False,
                              candidate_binding_reason=current.get("code", "candidate_changed"))
        else:
            self.state.update(candidate_binding_verified=ready,
                              candidate_binding_reason="verified" if ready else "not_ready")

    def configuration_fingerprint(self):
        digest = hashlib.sha256()
        for path in (ROOT / "scripts/local_supervisor.py", ROOT / "scripts/run_server.py",
                     ROOT / ".venv/Scripts/python.exe", ROOT / ".venv/pyvenv.cfg"):
            safe_path(path)
            digest.update(path.read_bytes())
        digest.update(json.dumps([str(self.runtime), str(self.data_dir), self.port]).encode())
        return digest.hexdigest()

    def publish(self, state, **fields):
        self.state.update(fields, state=state, updated_at=datetime.now(timezone.utc).isoformat())
        atomic_json(self.runtime / "processes.json", self.state)

    def stop_requested(self):
        return stop_path(self.runtime, self.run_id).exists()

    def validate_start(self):
        if self.configuration_fingerprint() != self.fingerprint:
            raise SupervisorError("configuration_changed")
        from secretary.infrastructure.instance_lock import DataDirectoryLock, DataDirectoryLockError
        try:
            with DataDirectoryLock(safe_path(self.data_dir)):
                pass
        except DataDirectoryLockError as exc:
            raise SupervisorError(exc.code) from None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", self.port))
            except OSError:
                raise SupervisorError("port_occupied") from None

    def start_attempt(self):
        self.check_candidate_binding()
        self.validate_start()
        from check_lifecycle import build_server_command
        child_run_id = str(uuid4())
        command, env = build_server_command(ROOT, data_dir=self.data_dir, port=self.port,
            run_id=child_run_id, log_directory=self.run_root / "logs",
            python_executable=ROOT / ".venv/Scripts/python.exe")
        command.extend(["--runtime-root", str(self.runtime), "--supervised-local"])
        stdout = self.run_root / f"{child_run_id}.stdout.log"
        stderr = self.run_root / f"{child_run_id}.stderr.log"
        # Last boundary immediately before spawn, including a stop during preflight.
        if self.stop_requested():
            return None
        with stdout.open("xb") as out, stderr.open("xb") as err:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
        attempt = {"process": process, "run_id": child_run_id, "identities": []}
        try:
            attempt["identities"].append(identity(process.pid))
        except SupervisorError:
            # Unknown child identity cannot enter the retry loop. Exiting this
            # supervisor closes its outer Job, containing all its descendants.
            self.publish("failed", error="launcher_identity_unavailable")
            raise SupervisorError("launcher_identity_unavailable") from None
        self.state.update(run_id=child_run_id, attempt_identities=attempt["identities"],
                          stdout_log=str(stdout), stderr_log=str(stderr), listener_identity=None)
        self.publish("starting")
        return attempt

    def discover_listener(self, attempt):
        process = attempt["process"]
        if not identity_alive(attempt["identities"][0]):
            raise SupervisorError("launcher_identity_changed")
        try:
            parent = psutil.Process(process.pid)
            candidates = [parent, *parent.children(recursive=True)]
            for candidate in candidates:
                if any(connection.status == psutil.CONN_LISTEN and connection.laddr.ip == "127.0.0.1"
                       and connection.laddr.port == self.port for connection in candidate.net_connections(kind="tcp")):
                    argv = candidate.cmdline()
                    if (str(ROOT / "scripts/run_server.py") not in argv or "--run-id" not in argv
                            or argv[argv.index("--run-id") + 1] != attempt["run_id"]):
                        raise SupervisorError("listener_identity_changed")
                    found = identity(candidate.pid)
                    if not identity_alive(attempt["identities"][0]):
                        raise SupervisorError("launcher_identity_changed")
                    if candidate.pid != process.pid and process.pid not in [ancestor.pid for ancestor in candidate.parents()]:
                        raise SupervisorError("listener_parent_changed")
                    if found not in attempt["identities"]:
                        attempt["identities"].append(found)
                    self.state["listener_identity"] = found
                    self.publish("starting")
                    return True
        except (psutil.Error, OSError, IndexError):
            raise SupervisorError("listener_identity_unavailable") from None
        return False

    def await_ready(self, attempt):
        deadline = time.monotonic() + 30
        with httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=1, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self.stop_requested():
                    return
                if attempt["process"].poll() is not None:
                    raise SupervisorError("server_exited_before_ready")
                if self.discover_listener(attempt):
                    try:
                        health = client.get("/health")
                        ready = client.get("/ready")
                        config = client.get("/api/v1/config")
                        if (health.status_code == 200 and health.json().get("status") == "ok"
                                and ready.status_code == 200 and ready.json().get("status") == "local_ok"
                                and config.status_code == 200 and config.json().get("cloud_enabled") is False
                                and self.discover_listener(attempt)):
                            self.check_candidate_binding(ready=True)
                            return
                    except (httpx.HTTPError, ValueError):
                        pass
                time.sleep(.2)
        raise SupervisorError("readiness_timeout")

    def await_exit(self, attempt):
        while not self.stop_requested():
            code = attempt["process"].poll()
            if code is not None:
                return code
            listener = self.state.get("listener_identity")
            if listener is not None and not identity_alive(listener):
                return -1
            time.sleep(.25)
        return None

    def drain_attempt(self, attempt):
        self.publish("draining")
        marker = safe_path(self.runtime / f"stop-{attempt['run_id']}.request")
        if not marker.exists():
            with marker.open("x", encoding="ascii") as stream:
                stream.write(attempt["run_id"])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if attempt["process"].poll() is not None and not any(identity_alive(item) for item in attempt["identities"]):
                return
            time.sleep(.1)
        raise SupervisorError("owned_tree_not_quiescent")

    def backoff(self, delay):
        deadline = time.monotonic() + delay
        while not self.stop_requested() and time.monotonic() < deadline:
            time.sleep(min(.1, max(0, deadline - time.monotonic())))


def run(runtime, data_dir, port):
    if os.name != "nt":
        raise SupervisorError("windows_required")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise SupervisorError("invalid_port")
    data_dir = safe_path(data_dir)
    if not data_dir.is_dir():
        raise SupervisorError("existing_data_directory_required")
    with owner_lease(runtime):
        old = read_state(runtime)
        if old is not None:
            require_supervisor_state(runtime, old)
            for record in [old.get("supervisor_identity"), *old.get("attempt_identities", [])]:
                if identity_alive(record):
                    raise SupervisorError("existing_owner_alive")
        from secretary.infrastructure.team_process_job import install_process_job
        job = install_process_job()
        job.verify_owned_identity()
        host = WindowsHost(runtime, data_dir, port)
        run_loop(host)
        return host.state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="operation", required=True)
    for operation in ("run", "status", "stop", "prepare-manual"):
        sub = subs.add_parser(operation)
        sub.add_argument("--runtime-root", type=Path)
        if operation == "run":
            sub.add_argument("--data-dir", required=True, type=Path)
            sub.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        runtime = runtime_path(args.runtime_root)
        result = (run(runtime, args.data_dir, args.port) if args.operation == "run" else
                  status(runtime) if args.operation == "status" else
                  prepare_manual(runtime) if args.operation == "prepare-manual" else request_stop(runtime))
        code = 0
    except (SupervisorError, OSError) as exc:
        result = {"state": "failed", "error": exc.code if isinstance(exc, SupervisorError) else "local_io_error"}
        code = 2
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
