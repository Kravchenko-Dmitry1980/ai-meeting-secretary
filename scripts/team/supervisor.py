"""Explicit, project-local Windows process lifecycle. Importing performs no I/O.

State files are evidence, never executable command sources. All launches derive
from the validated manifest. No process is adopted or stopped by name or port.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID, uuid4

import psutil


class LifecycleError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def utc():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def command_hash(argv):
    return hashlib.sha256(canonical(argv).encode()).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def uuid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise LifecycleError("invalid_uuid") from None


def safe_path(value, root=None, *, exists=False):
    if not isinstance(value, str) or value.startswith(("\\\\", "//")) or not Path(value).is_absolute():
        raise LifecycleError("absolute_path_required")
    path = Path(os.path.abspath(value))
    if root is not None and not path.is_relative_to(root):
        raise LifecycleError("path_outside_project")
    for candidate in (path, *path.parents):
        if candidate.exists():
            info = candidate.lstat()
            if candidate.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise LifecycleError("reparse_path_forbidden")
    if exists and not path.is_file():
        raise LifecycleError("required_file_missing")
    return path


def read_json(path, *, limit=128 * 1024):
    for attempt in range(20):
        try:
            if path.stat().st_size > limit:
                raise LifecycleError("json_too_large")
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError:
            # A concurrent Windows atomic replacement can briefly deny a reader.
            # Only the filesystem read is retried; no operation/mutation is replayed.
            if attempt == 19:
                raise LifecycleError("invalid_local_json") from None
            time.sleep(.01)
        except ValueError:
            raise LifecycleError("invalid_local_json") from None


def atomic_json(path, value):
    safe_path(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not append a UUID to an already long Windows filename. The exclusive
    # creation preserves collision safety while keeping the sibling path short.
    temporary = path.with_name(".write-" + uuid4().hex[:16] + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise LifecycleError("state_replace_busy") from None
                time.sleep(.01)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class Component:
    kind: str
    executable: Path
    config: Path
    sha256: str
    host: str
    port: int
    environment: dict


@dataclass(frozen=True)
class Manifest:
    source: Path
    deployment_id: str
    project_root: Path
    state_root: Path
    runtime_config: Path
    maintenance_database: Path
    components: tuple[Component, ...]

    def component(self, kind):
        return next(item for item in self.components if item.kind == kind)

    def control_dir(self, run_id):
        return self.state_root / "runs" / uuid(run_id)

    def command(self, kind, run_id):
        item = self.component(kind)
        if kind == "gateway":
            return [str(item.executable), "-B", "-m", "secretary.interface.team_main", "--config", str(self.runtime_config), "--run-id", uuid(run_id), "--control-dir", str(self.control_dir(run_id))]
        if kind == "vikunja":
            return [str(item.executable), "--config", str(item.config)]
        return [str(item.executable), "run", "--config", str(item.config), "--adapter", "caddyfile"]

    def environment(self, kind, inherited=None):
        env = {key: value for key, value in (os.environ if inherited is None else inherited).items()
               if not key.upper().startswith(("MAX_", "POLZA_", "TEAM_", "CADDY_", "VIKUNJA_", "PYTHON"))}
        env["PYTHONPATH"] = str(self.project_root / "backend")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(self.component(kind).environment)
        return env


def load_manifest(path):
    source = safe_path(str(path), exists=True)
    value = read_json(source)
    required = {"schema_version", "deployment_id", "project_root", "state_root", "runtime_config", "maintenance_database", "components"}
    if not isinstance(value, dict) or set(value) != required or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise LifecycleError("manifest_schema_invalid")
    root = safe_path(value["project_root"])
    if not root.is_dir() or not source.is_relative_to(root):
        raise LifecycleError("manifest_project_invalid")
    state = safe_path(value["state_root"], root)
    if not state.is_relative_to(root / ".runtime" ) or state == root / ".runtime":
        raise LifecycleError("state_path_invalid")
    runtime = safe_path(value["runtime_config"], root, exists=True)
    maintenance = safe_path(value["maintenance_database"], root)
    if not isinstance(value["components"], list) or len(value["components"]) != 3:
        raise LifecycleError("components_invalid")
    components = []
    for raw in value["components"]:
        fields = {"kind", "executable", "config", "sha256", "host", "port"}
        if not isinstance(raw, dict) or not fields <= set(raw) or set(raw) - fields - {"environment"}:
            raise LifecycleError("component_schema_invalid")
        kind = raw["kind"]
        if kind not in ("gateway", "vikunja", "proxy"):
            raise LifecycleError("component_kind_invalid")
        exe = safe_path(raw["executable"], root, exists=True)
        config = safe_path(raw["config"], root, exists=True)
        if not isinstance(raw["sha256"], str) or len(raw["sha256"]) != 64 or file_hash(exe) != raw["sha256"]:
            raise LifecycleError("binary_hash_mismatch")
        if type(raw["port"]) is not int or not 1024 <= raw["port"] <= 65535:
            # Only the public proxy may use the privileged HTTPS port.
            if kind != "proxy" or type(raw["port"]) is not int or raw["port"] != 443:
                raise LifecycleError("port_invalid")
        if raw["host"] not in (("127.0.0.1", "0.0.0.0") if kind == "proxy" else ("127.0.0.1",)):
            raise LifecycleError("host_invalid")
        env = raw.get("environment", {})
        allowed = {"TEAM_PUBLIC_HOST", "TEAM_PUBLIC_ROOT", "TEAM_ASSET_ROUTES", "TEAM_CADDY_DATA"}
        if not isinstance(env, dict) or (kind != "proxy" and env) or (kind == "proxy" and set(env) != allowed):
            raise LifecycleError("environment_invalid")
        for key, item in env.items():
            if not isinstance(item, str) or not item or any(ch in item for ch in "\r\n\0"):
                raise LifecycleError("environment_invalid")
            if key != "TEAM_PUBLIC_HOST":
                safe_path(item, root)
            elif any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for ch in item) or "." not in item:
                raise LifecycleError("public_host_invalid")
        if kind == "gateway" and config != runtime:
            raise LifecycleError("runtime_config_mismatch")
        components.append(Component(kind, exe, config, raw["sha256"], raw["host"], raw["port"], env))
    if {item.kind for item in components} != {"gateway", "vikunja", "proxy"} or len({item.port for item in components}) != 3:
        raise LifecycleError("components_conflict")
    runtime_value = read_json(runtime)
    gateway = next(item for item in components if item.kind == "gateway")
    if (not isinstance(runtime_value, dict) or runtime_value.get("deployment_id") != value["deployment_id"]
            or runtime_value.get("project_dir") != str(root)
            or runtime_value.get("control_database_path") != str(maintenance)
            or runtime_value.get("gateway_port", 8766) != gateway.port):
        raise LifecycleError("runtime_boundary_mismatch")
    capture_port = runtime_value.get("local_secretary_port", 8765)
    if type(capture_port) is not int or not 1024 <= capture_port <= 65535 or capture_port in {item.port for item in components}:
        raise LifecycleError("capture_port_invalid")
    return Manifest(source, uuid(value["deployment_id"]), root, state, runtime, maintenance, tuple(components))


class StateStore:
    def __init__(self, manifest):
        self.manifest = manifest
        self.path = manifest.state_root / "lifecycle.json"

    @contextlib.contextmanager
    def lock(self):
        safe_path(str(self.path.parent))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / "lifecycle.lock").open("a+b") as stream:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                if stream.read(1) == b"":
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                deadline = time.monotonic() + 2
                while True:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise LifecycleError("lifecycle_busy") from None
                        time.sleep(.025)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def read(self):
        if not self.path.exists():
            return {"schema_version": 1, "deployment_id": self.manifest.deployment_id, "desired": "stopped", "components": {}, "restarts": {}}
        safe_path(str(self.path), self.manifest.project_root)
        value = read_json(self.path)
        if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("deployment_id") != self.manifest.deployment_id:
            raise LifecycleError("state_identity_invalid")
        return value

    def write(self, value):
        if value.get("deployment_id") != self.manifest.deployment_id or value.get("schema_version") != 1:
            raise LifecycleError("state_identity_invalid")
        atomic_json(self.path, value)


def record_restart(state, kind, now):
    record = state.setdefault("restarts", {}).setdefault(kind, {"last_clock": now, "events": []})
    effective = max(float(now), record["last_clock"])
    record["last_clock"] = effective
    events = [point for point in record["events"] if effective - point <= 600]
    if len(events) >= 5:
        raise LifecycleError("restart_exhausted")
    events.append(effective)
    record["events"] = events
    return (1, 2, 4, 8, 30)[len(events) - 1]


def identity_matches(expected, actual):
    return all(expected.get(key) is not None and expected.get(key) == actual.get(key)
               for key in ("pid", "creation_time", "executable", "argv_hash", "run_id"))


def listener_owned(port, pid, listeners):
    owners = [owner for number, owner in listeners if number == port]
    return bool(owners) and all(owner == pid for owner in owners)


def listeners():
    try:
        return [(item.laddr.port, item.pid) for item in psutil.net_connections(kind="tcp") if item.status == psutil.CONN_LISTEN]
    except (psutil.Error, OSError):
        raise LifecycleError("listener_inspection_unavailable") from None


class WindowsProcess:
    """Mutation uses this retained HANDLE, never a second PID lookup."""
    def __init__(self, pid, run_id):
        if os.name != "nt":
            raise LifecycleError("windows_required")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x1000 | 0x00100000 | 0x0001, False, pid)
        if not self.handle:
            raise LifecycleError("process_inspection_denied")
        self.pid, self.run_id = pid, run_id

    def alive(self):
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise LifecycleError("process_inspection_denied")
        return code.value == 259

    def identity(self):
        if not self.alive():
            raise LifecycleError("process_exited")
        fields = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(item) for item in fields)):
            raise LifecycleError("process_inspection_denied")
        created = fields[0].dwHighDateTime * 2**32 + fields[0].dwLowDateTime
        try:
            process = psutil.Process(self.pid)
            executable = str(Path(process.exe()).absolute())
            argv = process.cmdline()
            # Ensure the psutil read describes the process retained by our handle.
            if abs(process.create_time() - (created / 10**7 - 11644473600)) > .001 or not self.alive():
                raise LifecycleError("process_identity_changed")
            account = process.username()
        except psutil.Error:
            raise LifecycleError("process_inspection_denied") from None
        return {"pid": self.pid, "creation_time": str(created), "executable": executable, "executable_sha256": file_hash(executable), "argv_hash": command_hash(argv), "run_id": self.run_id, "account": account}

    def terminate(self, expected, timeout=10):
        if not self.alive():
            return
        actual = self.identity()
        if not identity_matches(expected, actual) or actual.get("executable_sha256") != expected.get("executable_sha256") or actual.get("account") != expected.get("account"):
            raise LifecycleError("process_identity_changed")
        if not self.kernel.TerminateProcess(self.handle, 1):
            raise LifecycleError("process_stop_failed")
        if self.kernel.WaitForSingleObject(self.handle, int(timeout * 1000)) != 0:
            raise LifecycleError("process_stop_timeout")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def WindowsJob():
    """Compatibility entry for the shared, process-retained containment helper."""
    backend = str(Path(__file__).resolve().parents[2] / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from secretary.infrastructure.team_process_job import ProcessJobError, install_process_job
    try:
        return install_process_job()
    except ProcessJobError as error:
        raise LifecycleError(error.code) from None


def process_absent(record):
    try:
        handle = WindowsProcess(record["pid"], record["run_id"])
    except LifecycleError:
        if not psutil.pid_exists(record["pid"]):
            return True
        raise
    try:
        if not handle.alive():
            return True
        # A reused PID is not the original process and is never mutated.
        return not identity_matches(record, handle.identity())
    finally:
        handle.close()


def check_pause(state, barrier_id, fence):
    pause = state.get("maintenance")
    if not pause or pause.get("barrier_id") != barrier_id or pause.get("fence") != fence:
        raise LifecycleError("maintenance_conflict")


def request_stop(manifest, *, apply=False):
    if not apply:
        return {"mode": "preview", "deployment_id": manifest.deployment_id, "order": ["proxy", "gateway_drain", "vikunja"], "affects_capture": False}
    store = StateStore(manifest)
    with store.lock():
        state = store.read()
        run = state.get("run_id")
        if run is None:
            return {"mode": "stopped", "deployment_id": manifest.deployment_id}
        uuid(run)
        state["desired"] = "stopped"
        store.write(state)
        atomic_json(manifest.state_root / "stop.request.json", {"schema_version": 1, "deployment_id": manifest.deployment_id, "run_id": run, "request_id": str(uuid4()), "requested_at": utc()})
    return {"mode": "stop_requested", "run_id": run, "deployment_id": manifest.deployment_id}


def doctor(manifest):
    store = StateStore(manifest)
    state = store.read()
    result = {"schema_version": 1, "deployment_id": manifest.deployment_id, "observed_at": utc(), "desired": state.get("desired"), "components": {}}
    current_listeners = None
    for item in manifest.components:
        record = state.get("components", {}).get(item.kind)
        entry = {"state": "not_configured", "error_code": "not_started"}
        if record:
            entry = {"state": "degraded", "error_code": record.get("error_code", "not_running")}
            try:
                handle = WindowsProcess(record["pid"], state["run_id"])
                try:
                    actual = handle.identity()
                    current_listeners = listeners() if current_listeners is None else current_listeners
                    if identity_matches(record, actual) and listener_owned(item.port, record["pid"], current_listeners):
                        entry = {"state": "healthy", "error_code": None}
                finally:
                    handle.close()
            except (LifecycleError, KeyError):
                pass
        result["components"][item.kind] = entry
    # Runtime's component details are evidence only when identity/run and freshness match.
    if state.get("run_id"):
        path = manifest.control_dir(state["run_id"]) / "runtime-health.json"
        if path.exists():
            try:
                health = read_json(path)
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(health["observed_at"])).total_seconds()
                valid = health.get("deployment_id") == manifest.deployment_id and health.get("run_id") == state["run_id"] and health.get("pid") == state.get("components", {}).get("gateway", {}).get("pid") and 0 <= age <= 60
                if valid:
                    result["runtime_phase"] = health.get("phase")
                    result["runtime_components"] = {key: {"state": value.get("state", "degraded"), "error_code": value.get("error_code")} for key, value in health.get("components", {}).items() if isinstance(value, dict)}
                    if health.get("phase") != "running" or any(value.get("state") != "healthy" for value in result["runtime_components"].values()):
                        result["components"]["gateway"] = {"state": "degraded", "error_code": "runtime_not_fully_healthy"}
                else:
                    result["components"]["gateway"] = {"state": "degraded", "error_code": "runtime_health_stale"}
            except (LifecycleError, ValueError, KeyError, TypeError):
                result["components"]["gateway"] = {"state": "degraded", "error_code": "runtime_health_invalid"}
        elif result["components"]["gateway"]["state"] == "healthy":
            result["components"]["gateway"] = {"state": "degraded", "error_code": "runtime_health_missing"}
    return result


class Supervisor:
    def __init__(self, manifest, run_id):
        self.manifest, self.run_id = manifest, uuid(run_id)
        self.store = StateStore(manifest)
        self.children = {}
        self.next_start = {}
        self.job = None
        self.resume_claim = None

    def _state(self):
        state = self.store.read()
        if state.get("run_id") != self.run_id:
            raise LifecycleError("launcher_ownership_lost")
        return state

    def _maintenance(self):
        sys.path.insert(0, str(self.manifest.project_root / "backend"))
        from secretary.infrastructure.team_maintenance import MaintenanceRepository
        return MaintenanceRepository(self.manifest.maintenance_database, self.manifest.deployment_id)

    def start(self):
        if any(port in {item.port for item in self.manifest.components} for port, _ in listeners()):
            raise LifecycleError("foreign_port_in_use")
        if self._maintenance().status()["mode"] != "open":
            raise LifecycleError("maintenance_resume_required")
        with self.store.lock():
            state = self.store.read()
            for record in [state.get("supervisor"), *state.get("components", {}).values()]:
                if record and not process_absent(record):
                    raise LifecycleError("existing_launcher_alive")
            if state.get("maintenance"):
                raise LifecycleError("maintenance_resume_required")
            self.job = WindowsJob()  # Fail before any launch; all children inherit this job.
            own = WindowsProcess(os.getpid(), self.run_id)
            try:
                state.update(run_id=self.run_id, supervisor=own.identity(), desired="running", phase="starting", components={})
            finally:
                own.close()
            self.store.write(state)
        for kind in ("vikunja", "gateway", "proxy"):
            self.launch(kind)
        with self.store.lock():
            state = self._state()
            state["phase"] = "running"
            self.store.write(state)

    def launch(self, kind):
        item = self.manifest.component(kind)
        if kind == "vikunja":
            maintenance = self._maintenance()
            if self.resume_claim is not None:
                maintenance.assert_held(self.resume_claim)
            elif maintenance.status()["mode"] != "open":
                raise LifecycleError("maintenance_resume_required")
        with self.store.lock():
            state = self._state()
            if state.get("desired") != "running" or (kind == "vikunja" and state.get("maintenance")):
                return
            previous = self.children.get(kind)
            if previous:
                if previous[1].alive():
                    raise LifecycleError("component_already_running")
                previous[1].close()
                del self.children[kind]
            if any(port == item.port for port, _ in listeners()):
                raise LifecycleError("foreign_port_in_use")
            if file_hash(item.executable) != item.sha256:
                raise LifecycleError("binary_hash_mismatch")
            self.manifest.control_dir(self.run_id).mkdir(parents=True, exist_ok=True)
            # No raw stdout/stderr or configuration content is written to evidence.
            process = subprocess.Popen(self.manifest.command(kind, self.run_id), cwd=self.manifest.project_root, env=self.manifest.environment(kind), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
            handle = WindowsProcess(process.pid, self.run_id)
            record = handle.identity()
            if record["argv_hash"] != command_hash(self.manifest.command(kind, self.run_id)) or Path(record["executable"]) != item.executable or record["executable_sha256"] != item.sha256:
                raise LifecycleError("launched_identity_mismatch")
            self.children[kind] = (process, handle, record)
            state["components"][kind] = record
            self.store.write(state)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not handle.alive():
                raise LifecycleError("component_start_failed")
            if listener_owned(item.port, record["pid"], listeners()):
                return
            time.sleep(.1)
        raise LifecycleError("component_readiness_timeout")

    def _stop_native(self, kind):
        child = self.children.get(kind)
        if child:
            child[1].terminate(child[2])

    def _stop_gateway(self):
        child = self.children.get("gateway")
        if not child or not child[1].alive():
            return True
        atomic_json(self.manifest.control_dir(self.run_id) / "stop.request.json", {"schema_version": 1, "deployment_id": self.manifest.deployment_id, "run_id": self.run_id, "request_id": str(uuid4()), "requested_at": utc()})
        deadline = time.monotonic() + 30
        while child[1].alive() and time.monotonic() < deadline:
            time.sleep(.1)
        return not child[1].alive()

    def tick(self):
        state = self._state()
        if state.get("desired") == "stopped":
            self._stop_native("proxy")
            if not self._stop_gateway():
                with self.store.lock():
                    state = self._state()
                    state.update(phase="degraded", error_code="gateway_drain_timeout")
                    self.store.write(state)
                return True  # Keep job handle alive; never force gateway as a normal stop.
            self._stop_native("vikunja")
            with self.store.lock():
                state = self._state()
                state["phase"] = "stopped"
                self.store.write(state)
            return False
        self._control_maintenance()
        state = self._state()
        if self._maintenance().status()["mode"] != "open":
            return True  # No automatic component restarts during a held barrier.
        for kind in ("vikunja", "gateway", "proxy"):
            if kind == "vikunja" and state.get("maintenance"):
                continue
            child = self.children.get(kind)
            if child and child[1].alive():
                continue
            if kind not in self.next_start:
                with self.store.lock():
                    state = self._state()
                    try:
                        delay = record_restart(state, kind, time.time())
                    except LifecycleError:
                        state.update(phase="degraded", error_code="restart_exhausted")
                        self.store.write(state)
                        continue
                    self.store.write(state)
                    self.next_start[kind] = time.monotonic() + delay
            if time.monotonic() >= self.next_start[kind]:
                self.next_start.pop(kind)
                try:
                    self.launch(kind)
                except LifecycleError as error:
                    with self.store.lock():
                        state = self._state()
                        state.update(phase="degraded", error_code=error.code)
                        self.store.write(state)
        return True

    def _control_maintenance(self):
        path = self.manifest.state_root / "maintenance.request.json"
        if not path.exists():
            return
        request = read_json(path)
        state = self._state()
        if request.get("run_id") != self.run_id or request.get("request_id") == state.get("last_maintenance_request"):
            return
        from secretary.infrastructure.team_maintenance import BarrierClaim
        claim = BarrierClaim(deployment_id=self.manifest.deployment_id, barrier_id=request["barrier_id"], owner_id=request["owner_id"], fence=request["fence"])
        maintenance = self._maintenance()
        maintenance.assert_held(claim)
        if request["action"] == "quiesce":
            with self.store.lock():
                state = self._state()
                if state.get("maintenance"):
                    check_pause(state, claim.barrier_id, claim.fence)
                state["maintenance"] = {"barrier_id": claim.barrier_id, "fence": claim.fence, "component": "vikunja"}
                self.store.write(state)
            child = self.children.get("vikunja")
            if not child:
                raise LifecycleError("native_process_unmanaged")
            self._stop_native("vikunja")
            maintenance.assert_held(claim)
            evidence = {"deployment_id": self.manifest.deployment_id, "run_id": self.run_id, "component": "vikunja", "barrier_id": claim.barrier_id, "barrier_fence": claim.fence, **{key: child[2][key] for key in ("pid", "creation_time", "executable_sha256", "argv_hash")}, "observed_stopped_at": utc()}
            with self.store.lock():
                state = self._state()
                state["quiescence"] = evidence
                state["last_maintenance_request"] = request["request_id"]
                self.store.write(state)
        elif request["action"] == "resume":
            with self.store.lock():
                state = self._state()
                check_pause(state, claim.barrier_id, claim.fence)
                state["maintenance"] = None
                self.store.write(state)
            try:
                self.resume_claim = claim
                self.launch("vikunja")
                maintenance.assert_held(claim)
            except Exception:
                with self.store.lock():
                    state = self._state()
                    state["maintenance"] = {"barrier_id": claim.barrier_id, "fence": claim.fence, "component": "vikunja"}
                    self.store.write(state)
                raise
            finally:
                self.resume_claim = None
            with self.store.lock():
                state = self._state()
                state["last_maintenance_request"] = request["request_id"]
                self.store.write(state)


def _maintenance_request(manifest, maintenance, claim, action, timeout):
    maintenance.assert_held(claim)
    store = StateStore(manifest)
    with store.lock():
        state = store.read()
        if state.get("desired") != "running" or not state.get("supervisor") or process_absent(state["supervisor"]):
            raise LifecycleError("launcher_unavailable")
        if action == "resume":
            check_pause(state, claim.barrier_id, claim.fence)
        request_id = str(uuid4())
        atomic_json(manifest.state_root / "maintenance.request.json", {"schema_version": 1, "deployment_id": manifest.deployment_id, "run_id": state["run_id"], "request_id": request_id, "action": action, "barrier_id": claim.barrier_id, "owner_id": claim.owner_id, "fence": claim.fence})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        maintenance.assert_held(claim)
        state = store.read()
        if state.get("last_maintenance_request") == request_id:
            return state
        time.sleep(.1)
    raise LifecycleError("maintenance_control_timeout")


def quiesce_component(manifest_path, *, maintenance, claim, timeout=35):
    manifest = load_manifest(manifest_path)
    state = _maintenance_request(manifest, maintenance, claim, "quiesce", timeout)
    evidence = state["quiescence"]
    verify_quiescence(manifest_path, evidence, maintenance=maintenance, claim=claim)
    return evidence


def verify_quiescence(manifest_path, evidence, *, maintenance, claim):
    manifest = load_manifest(manifest_path)
    maintenance.assert_held(claim)
    state = StateStore(manifest).read()
    check_pause(state, claim.barrier_id, claim.fence)
    record = state.get("components", {}).get("vikunja")
    if evidence != state.get("quiescence") or evidence.get("deployment_id") != manifest.deployment_id or evidence.get("run_id") != state.get("run_id") or evidence.get("barrier_id") != claim.barrier_id or evidence.get("barrier_fence") != claim.fence or not record or not process_absent(record):
        raise LifecycleError("quiescence_not_verified")
    return evidence


def resume_component(manifest_path, *, maintenance, claim, timeout=35):
    return _maintenance_request(load_manifest(manifest_path), maintenance, claim, "resume", timeout)


def launch_supervisor(manifest, *, apply=False):
    run = str(uuid4())
    if not apply:
        return {"mode": "preview", "deployment_id": manifest.deployment_id, "components": [{"kind": item.kind, "argv": manifest.command(item.kind, run), "port": item.port} for item in manifest.components], "affects_capture": False}
    python = manifest.component("gateway").executable
    subprocess.Popen([str(python), "-B", str(Path(__file__).absolute()), "run", "--manifest", str(manifest.source), "--launch-id", run], cwd=manifest.project_root, env=manifest.environment("gateway"), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        state = StateStore(manifest).read()
        if state.get("run_id") == run and state.get("phase") in ("running", "degraded", "failed"):
            return {"mode": state["phase"], "deployment_id": manifest.deployment_id, "run_id": run, "error_code": state.get("error_code")}
        time.sleep(.1)
    raise LifecycleError("launcher_start_timeout")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "doctor", "run"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--launch-id")
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        if args.action == "run":
            supervisor = Supervisor(manifest, args.launch_id or str(uuid4()))
            try:
                supervisor.start()
                while True:
                    try:
                        if not supervisor.tick():
                            break
                    except LifecycleError as error:
                        if error.code != "lifecycle_busy":
                            raise
                    time.sleep(.25)
            except LifecycleError as error:
                with supervisor.store.lock():
                    state = supervisor.store.read()
                    if state.get("run_id") == supervisor.run_id:
                        state.update(phase="failed", error_code=error.code)
                        supervisor.store.write(state)
                raise
            return 0
        result = doctor(manifest) if args.action == "doctor" else request_stop(manifest, apply=args.apply) if args.action == "stop" else launch_supervisor(manifest, apply=args.apply)
        print(canonical(result))
        return 0 if result.get("mode") not in ("failed", "degraded") else 2
    except (LifecycleError, OSError, KeyError, TypeError, psutil.Error) as error:
        print(canonical({"state": "degraded", "error_code": error.code if isinstance(error, LifecycleError) else "lifecycle_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
