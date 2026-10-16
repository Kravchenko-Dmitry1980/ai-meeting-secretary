"""Offline lifecycle contract tests; no owner process or configuration access."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SPEC = importlib.util.spec_from_file_location("team_supervisor", Path(__file__).parents[1] / "scripts/team/supervisor.py")
life = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = life
SPEC.loader.exec_module(life)


@pytest.fixture
def configured(tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    for name in ("python.exe", "vikunja.exe", "caddy.exe", "runtime.json", "vikunja.yaml", "Caddyfile"):
        (root / name).write_text("synthetic", encoding="utf-8")
    value = {"schema_version": 1, "deployment_id": str(uuid4()), "project_root": str(root),
             "state_root": str(root / ".runtime/team/lifecycle"), "runtime_config": str(root / "runtime.json"),
             "maintenance_database": str(root / ".runtime/team/maintenance.sqlite"),
             "components": [
                 {"kind": "vikunja", "executable": str(root / "vikunja.exe"), "config": str(root / "vikunja.yaml"), "sha256": life.file_hash(root / "vikunja.exe"), "host": "127.0.0.1", "port": 18765},
                 {"kind": "gateway", "executable": str(root / "python.exe"), "config": str(root / "runtime.json"), "sha256": life.file_hash(root / "python.exe"), "host": "127.0.0.1", "port": 18766},
                 {"kind": "proxy", "executable": str(root / "caddy.exe"), "config": str(root / "Caddyfile"), "sha256": life.file_hash(root / "caddy.exe"), "host": "0.0.0.0", "port": 18443, "environment": {"TEAM_PUBLIC_HOST": "example.invalid", "TEAM_PUBLIC_ROOT": str(root), "TEAM_ASSET_ROUTES": str(root / "routes.caddy"), "TEAM_CADDY_DATA": str(root / ".runtime/caddy")}},
             ]}
    path = root / "manifest.json"
    (root / "runtime.json").write_text(json.dumps({"deployment_id": value["deployment_id"], "project_dir": str(root), "control_database_path": value["maintenance_database"], "gateway_port": 18766}))
    path.write_text(json.dumps(value), encoding="utf-8")
    return SimpleNamespace(root=root, path=path, value=value)


def load(c, **updates):
    c.value.update(updates)
    c.path.write_text(json.dumps(c.value), encoding="utf-8")
    return life.load_manifest(c.path)


def test_fixed_commands_absolute_and_independent_of_cwd(configured, monkeypatch, tmp_path):
    manifest = load(configured)
    monkeypatch.chdir(tmp_path)
    run = str(uuid4())
    argv = manifest.command("gateway", run)
    assert argv[:4] == [str(configured.root / "python.exe"), "-B", "-m", "secretary.interface.team_main"]
    assert argv[argv.index("--config") + 1] == str(configured.root / "runtime.json")
    assert argv[-2:] == ["--control-dir", str(manifest.control_dir(run))]
    assert "capture" not in " ".join(argv)


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("deployment_id", "bad"), ("project_root", "relative"), ("extra", True)])
def test_manifest_rejects_invalid_or_unknown_top_level(configured, field, value):
    with pytest.raises(life.LifecycleError):
        load(configured, **{field: value})


@pytest.mark.parametrize("change", [{"kind": "shell"}, {"argv": ["anything"]}, {"sha256": "0" * 64}, {"host": "8.8.8.8"}, {"port": True}])
def test_manifest_rejects_untrusted_launch_fields(configured, change):
    configured.value["components"][0].update(change)
    with pytest.raises(life.LifecycleError):
        load(configured)


def test_external_executable_and_duplicate_ports_rejected(configured, tmp_path):
    outside = tmp_path / "foreign.exe"
    outside.write_text("synthetic")
    configured.value["components"][0]["executable"] = str(outside)
    with pytest.raises(life.LifecycleError):
        load(configured)
    configured.value["components"][0]["executable"] = str(configured.root / "vikunja.exe")
    configured.value["components"][1]["port"] = 18765
    with pytest.raises(life.LifecycleError):
        load(configured)


def test_environment_drops_inherited_provider_and_proxy_configuration(configured):
    manifest = load(configured)
    env = manifest.environment("gateway", {"PATH": "safe", "MAX_BOT_TOKEN": "secret", "POLZA_API_KEY": "secret", "TEAM_AUTH_SECRET": "secret", "CADDY_ADMIN": "bad", "VIKUNJA_DATABASE_PATH": "bad"})
    assert not any(key.startswith(("MAX_", "POLZA_", "TEAM_", "CADDY_", "VIKUNJA_")) for key in env)
    assert env["PYTHONPATH"] == str(configured.root / "backend")


def test_command_hash_preserves_argument_boundaries():
    assert life.command_hash(["a b", "c"]) != life.command_hash(["a", "b c"])


def test_restart_budget_persists_and_clock_rollback_never_resets(configured):
    store = life.StateStore(load(configured))
    with store.lock():
        state = store.read()
        for attempt in range(5):
            delay = life.record_restart(state, "gateway", 1000 + attempt)
            assert delay in (1, 2, 4, 8, 30)
        store.write(state)
    recovered = store.read()
    with pytest.raises(life.LifecycleError, match="restart_exhausted"):
        life.record_restart(recovered, "gateway", 900)
    assert life.record_restart(recovered, "gateway", 1700) == 1


def test_atomic_state_is_deployment_bound_and_refuses_corruption(configured):
    store = life.StateStore(load(configured))
    store.write({"schema_version": 1, "deployment_id": configured.value["deployment_id"], "desired": "stopped"})
    assert store.read()["desired"] == "stopped"
    store.path.write_text('{"deployment_id":"foreign"}')
    with pytest.raises(life.LifecycleError):
        store.read()


def test_atomic_state_tolerates_bounded_windows_sharing_race(configured, monkeypatch):
    manifest = load(configured)
    store = life.StateStore(manifest)
    initial = store.read()
    store.write(initial)
    original_read = Path.read_text
    reads = []
    def shared_read(path, *args, **kwargs):
        if path == store.path and not reads:
            reads.append(True)
            raise PermissionError("synthetic sharing violation")
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", shared_read)
    assert store.read() == initial
    original_replace = life.os.replace
    writes = []
    def shared_replace(source, destination):
        if destination == store.path and not writes:
            writes.append(True)
            raise PermissionError("synthetic sharing violation")
        return original_replace(source, destination)
    monkeypatch.setattr(life.os, "replace", shared_replace)
    store.write(dict(initial, desired="running"))
    assert store.read()["desired"] == "running"


def test_manifest_requires_same_actual_runtime_barrier(configured):
    (configured.root / "runtime.json").write_text(json.dumps({"deployment_id": configured.value["deployment_id"], "project_dir": str(configured.root), "control_database_path": str(configured.root / "other.sqlite"), "gateway_port": 18766}))
    with pytest.raises(life.LifecycleError, match="runtime_boundary_mismatch"):
        load(configured)


@pytest.mark.parametrize("path", [r"\\server\share\runtime.json", r"\\?\C:\project\runtime.json", "//server/share/runtime.json"])
def test_no_network_or_device_paths_are_read(path):
    with pytest.raises(life.LifecycleError, match="absolute_path_required"):
        life.safe_path(path)


def test_doctor_rejects_stale_health_even_when_own_listener_is_present(configured, monkeypatch):
    manifest = load(configured)
    run = str(uuid4())
    record = {"pid": 10, "creation_time": "123", "executable": "fixture.exe", "argv_hash": "a", "run_id": run}
    life.StateStore(manifest).write({"schema_version": 1, "deployment_id": manifest.deployment_id, "run_id": run, "desired": "running", "components": {"gateway": record}})
    monkeypatch.setattr(life, "WindowsProcess", lambda *args: SimpleNamespace(identity=lambda: record, close=lambda: None))
    monkeypatch.setattr(life, "listeners", lambda: [(18766, 10)])
    life.atomic_json(manifest.control_dir(run) / "runtime-health.json", {"deployment_id": manifest.deployment_id, "run_id": run, "pid": 10, "observed_at": "2000-01-01T00:00:00+00:00", "phase": "running", "components": {}})
    result = life.doctor(manifest)
    assert result["components"]["gateway"] == {"state": "degraded", "error_code": "runtime_health_stale"}


def test_stop_request_is_run_bound_and_preview_does_not_write(configured):
    manifest = load(configured)
    result = life.request_stop(manifest, apply=False)
    assert result["mode"] == "preview"
    assert not manifest.state_root.exists()
    store = life.StateStore(manifest)
    run = str(uuid4())
    store.write({"schema_version": 1, "deployment_id": manifest.deployment_id, "run_id": run, "desired": "running"})
    life.request_stop(manifest, apply=True)
    request = json.loads((manifest.state_root / "stop.request.json").read_text())
    assert request["run_id"] == run
    assert store.read()["desired"] == "stopped"


def test_doctor_without_state_does_not_claim_healthy_or_create_state(configured):
    manifest = load(configured)
    status = life.doctor(manifest)
    assert all(item["state"] == "not_configured" for item in status["components"].values())
    assert not manifest.state_root.exists()


def test_identity_rejects_pid_reuse_and_changed_command():
    expected = {"pid": 20, "creation_time": 123, "executable": "C:/synthetic.exe", "argv_hash": life.command_hash(["a"]), "run_id": str(uuid4())}
    assert life.identity_matches(expected, dict(expected))
    for key, value in [("creation_time", 124), ("argv_hash", "0" * 64), ("run_id", str(uuid4()))]:
        actual = dict(expected, **{key: value})
        assert not life.identity_matches(expected, actual)


def test_readiness_rejects_foreign_listener_even_when_port_answers():
    assert not life.listener_owned(8766, 44, [(8766, 55)])
    assert life.listener_owned(8766, 44, [(8766, 44)])
    assert not life.listener_owned(8766, 44, [(8766, 44), (8766, 55)])


def test_maintenance_pause_stale_epoch_cannot_resume(configured):
    manifest = load(configured)
    state = {"schema_version": 1, "deployment_id": manifest.deployment_id, "maintenance": {"barrier_id": str(uuid4()), "fence": 2, "component": "vikunja"}}
    with pytest.raises(life.LifecycleError, match="maintenance_conflict"):
        life.check_pause(state, state["maintenance"]["barrier_id"], 1)
    life.check_pause(state, state["maintenance"]["barrier_id"], 2)


def test_scripts_default_to_preview_and_scheduler_never_uses_system():
    root = Path(__file__).parents[1]
    for name in ("start.ps1", "stop.ps1", "install_tasks.ps1", "uninstall_tasks.ps1"):
        source = (root / "scripts/team" / name).read_text(encoding="utf-8-sig")
        assert "[switch]$Apply" in source
    install = (root / "scripts/team/install_tasks.ps1").read_text(encoding="utf-8-sig")
    assert "InteractiveToken" in install and "LeastPrivilege" in install
    assert "Get-Credential" in install and "-NoBootstrap" in install
    assert "-Force" not in install


def test_foreign_port_refuses_before_job_or_process_creation(configured, monkeypatch):
    manifest = load(configured)
    monkeypatch.setattr(life, "listeners", lambda: [(18766, 999)])
    monkeypatch.setattr(life, "WindowsJob", lambda: pytest.fail("must refuse before job creation"))
    supervisor = life.Supervisor(manifest, str(uuid4()))
    with pytest.raises(life.LifecycleError, match="foreign_port_in_use"):
        supervisor.start()
    assert not manifest.state_root.exists()


def test_stale_supervisor_cannot_write_new_run_state(configured):
    manifest = load(configured)
    supervisor = life.Supervisor(manifest, str(uuid4()))
    new_run = str(uuid4())
    supervisor.store.write({"schema_version": 1, "deployment_id": manifest.deployment_id, "run_id": new_run, "desired": "running"})
    with pytest.raises(life.LifecycleError, match="launcher_ownership_lost"):
        supervisor.tick()
    assert supervisor.store.read()["run_id"] == new_run


def test_drain_timeout_keeps_job_and_native_database_alive(configured, monkeypatch):
    manifest = load(configured)
    supervisor = life.Supervisor(manifest, str(uuid4()))
    supervisor.store.write({"schema_version": 1, "deployment_id": manifest.deployment_id, "run_id": supervisor.run_id, "desired": "stopped"})
    stopped = []
    monkeypatch.setattr(supervisor, "_stop_native", stopped.append)
    monkeypatch.setattr(supervisor, "_stop_gateway", lambda: False)
    assert supervisor.tick() is True
    assert stopped == ["proxy"]
    assert supervisor.store.read()["error_code"] == "gateway_drain_timeout"


def test_paused_native_component_never_restarts(configured, monkeypatch):
    manifest = load(configured)
    supervisor = life.Supervisor(manifest, str(uuid4()))
    supervisor.store.write({"schema_version": 1, "deployment_id": manifest.deployment_id, "run_id": supervisor.run_id, "desired": "running", "maintenance": {"barrier_id": str(uuid4()), "fence": 1}})
    supervisor.children = {kind: (None, SimpleNamespace(alive=lambda: True), {}) for kind in ("gateway", "proxy")}
    monkeypatch.setattr(supervisor, "_control_maintenance", lambda: None)
    monkeypatch.setattr(supervisor, "_maintenance", lambda: SimpleNamespace(status=lambda: {"mode": "draining"}))
    monkeypatch.setattr(supervisor, "launch", lambda kind: pytest.fail("paused Vikunja was restarted"))
    assert supervisor.tick()
    assert not supervisor.store.read().get("restarts")


def test_existing_shared_barrier_refuses_launch_even_without_local_pause(configured, monkeypatch):
    manifest = load(configured)
    supervisor = life.Supervisor(manifest, str(uuid4()))
    monkeypatch.setattr(life, "listeners", lambda: [])
    monkeypatch.setattr(supervisor, "_maintenance", lambda: SimpleNamespace(status=lambda: {"mode": "draining"}))
    monkeypatch.setattr(life, "WindowsJob", lambda: pytest.fail("must refuse before launching"))
    with pytest.raises(life.LifecycleError, match="maintenance_resume_required"):
        supervisor.start()


@pytest.mark.parametrize("alive", [False, True])
def test_restart_releases_dead_handle_and_never_replaces_live_child(configured, monkeypatch, alive):
    manifest = load(configured)
    controller = life.Supervisor(manifest, str(uuid4()))
    controller.store.write({"schema_version": 1, "deployment_id": manifest.deployment_id, "run_id": controller.run_id, "desired": "running", "components": {}})
    closed = []
    controller.children["gateway"] = (None, SimpleNamespace(alive=lambda: alive, close=lambda: closed.append("old")), {})
    record = {"pid": 4444, "creation_time": "123", "executable": str(manifest.component("gateway").executable), "executable_sha256": manifest.component("gateway").sha256, "argv_hash": life.command_hash(manifest.command("gateway", controller.run_id)), "run_id": controller.run_id}
    launches = []
    monkeypatch.setattr(life.subprocess, "Popen", lambda *args, **kwargs: launches.append(True) or SimpleNamespace(pid=4444))
    monkeypatch.setattr(life, "WindowsProcess", lambda *args: SimpleNamespace(identity=lambda: record, alive=lambda: True))
    seen = iter([[], [(18766, 4444)]])
    monkeypatch.setattr(life, "listeners", lambda: next(seen))
    if alive:
        with pytest.raises(life.LifecycleError, match="component_already_running"):
            controller.launch("gateway")
        assert closed == launches == []
    else:
        controller.launch("gateway")
        assert closed == ["old"]
        assert launches == [True]


@pytest.mark.parametrize("port", [True, "19876", 0, 80, 1023, 65536, 18766])
def test_capture_port_is_validated_before_generating_task(configured, port):
    runtime = configured.root / "runtime.json"
    value = json.loads(runtime.read_text())
    value["local_secretary_port"] = port
    runtime.write_text(json.dumps(value))
    with pytest.raises(life.LifecycleError, match="capture_port_invalid"):
        load(configured)


@pytest.mark.asyncio
async def test_original_team_launcher_installs_process_job_before_config_and_factory(monkeypatch, tmp_path):
    import types
    from secretary.infrastructure import team_process_job
    spec = importlib.util.spec_from_file_location("secretary_job_launcher", Path(__file__).parents[1] / "scripts/run_server.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    observed = []
    monkeypatch.setattr(team_process_job, "install_process_job", lambda: observed.append("job"))
    settings = types.ModuleType("secretary.team_settings")
    settings.load_team_settings = lambda path: observed.append("config") or SimpleNamespace(local_secretary_port=19876)
    runtime = types.ModuleType("secretary.orchestration.secretary_team_runtime")
    runtime.create_secretary_team_app = lambda config, **kwargs: observed.append("factory") or object()
    monkeypatch.setitem(sys.modules, "secretary.team_settings", settings)
    monkeypatch.setitem(sys.modules, "secretary.orchestration.secretary_team_runtime", runtime)
    class Server:
        should_exit = True
        async def serve(self, **kwargs):
            observed.append("serve")
    monkeypatch.setattr(launcher.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher.uvicorn, "Server", lambda config: Server())
    await launcher.run_owned(19876, object(), tmp_path / "synthetic-config.json", str(uuid4()))
    assert observed == ["job", "config", "factory", "serve"]


@pytest.mark.asyncio
async def test_original_team_launcher_job_failure_prevents_config_and_factory(monkeypatch, tmp_path):
    import types
    from secretary.infrastructure import team_process_job
    spec = importlib.util.spec_from_file_location("secretary_job_failure", Path(__file__).parents[1] / "scripts/run_server.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    def denied():
        raise team_process_job.ProcessJobError("job_object_unavailable")
    monkeypatch.setattr(team_process_job, "install_process_job", denied)
    settings = types.ModuleType("secretary.team_settings")
    settings.load_team_settings = lambda path: pytest.fail("configuration read before containment")
    monkeypatch.setitem(sys.modules, "secretary.team_settings", settings)
    with pytest.raises(team_process_job.ProcessJobError, match="job_object_unavailable"):
        await launcher.run_owned(19876, object(), tmp_path / "synthetic-config.json", str(uuid4()))


def test_containment_proof_rejects_uninitialized_and_subclass_objects():
    from secretary.infrastructure.team_process_job import WindowsJob, ProcessJobError
    uninitialized = object.__new__(WindowsJob)
    with pytest.raises(ProcessJobError, match="job_ownership_unverified"):
        uninitialized.verify_owned_identity()
    class UntrustedJob(WindowsJob):
        pass
    subclass = object.__new__(UntrustedJob)
    with pytest.raises(ProcessJobError, match="job_ownership_unverified"):
        subclass.verify_owned_identity()


def test_handle_mutation_does_not_reopen_pid(monkeypatch):
    expected = {"pid": 20, "creation_time": "123", "executable": "C:/synthetic.exe", "argv_hash": "a", "run_id": "r", "account": "u", "executable_sha256": "b"}
    calls = []
    process = object.__new__(life.WindowsProcess)
    process.handle = "original-handle"
    process.kernel = SimpleNamespace(TerminateProcess=lambda handle, code: calls.append(handle) or True, WaitForSingleObject=lambda handle, timeout: 0)
    process.alive = lambda: True
    process.identity = lambda: dict(expected)
    process.terminate(expected)
    assert calls == ["original-handle"]
    process.identity = lambda: dict(expected, creation_time="new-process")
    with pytest.raises(life.LifecycleError, match="process_identity_changed"):
        process.terminate(expected)
    assert len(calls) == 1


def test_process_identity_shared_encoding_and_no_raw_arguments():
    from secretary.infrastructure.team_process_identity import process_identity
    actual = process_identity()
    own = life.WindowsProcess(os.getpid(), str(uuid4()))
    try:
        lifecycle = own.identity()
    finally:
        own.close()
    assert set(actual) == {"pid", "creation_time", "executable_sha256", "argv_sha256"}
    assert actual["creation_time"] == lifecycle["creation_time"]
    assert actual["argv_sha256"] == lifecycle["argv_hash"]


def test_dead_proof_refuses_live_current_process_and_accepts_different_lifetime():
    from secretary.infrastructure.team_process_identity import process_identity, verify_dead_identity
    identity = process_identity()
    with pytest.raises(ValueError, match="process_still_alive"):
        verify_dead_identity(identity)
    original_lifetime = dict(identity, creation_time=str(int(identity["creation_time"]) - 1))
    proof = verify_dead_identity(original_lifetime)
    assert proof.state == "pid_reused"
    assert proof.creation_time == original_lifetime["creation_time"]
    assert life.psutil.pid_exists(identity["pid"])


def test_actual_own_child_dead_proof_is_not_a_lease_timeout():
    from secretary.infrastructure.team_process_identity import process_identity, verify_dead_identity
    child = subprocess.Popen([sys.executable, "-B", "-c", "import time;time.sleep(30)"], creationflags=subprocess.CREATE_NO_WINDOW)
    handle = life.WindowsProcess(child.pid, str(uuid4()))
    try:
        identity = process_identity(child.pid)
        with pytest.raises(ValueError, match="process_still_alive"):
            verify_dead_identity(identity)
        handle.terminate(handle.identity())
        child.wait(timeout=5)
        assert verify_dead_identity(identity).state == "exited"
    finally:
        if handle.alive():
            handle.terminate(handle.identity())
        handle.close()


def native_probe():
    """Explicit local integration entry; never called by guarded pytest."""
    from uuid import uuid4
    root = Path(__file__).parents[1]
    evidence_root = root / ".runtime/team-rollout" / ("t12-lifecycle-" + str(uuid4()))
    evidence_root.mkdir()
    child_code = "import socket,time,json,os; s=socket.socket();s.bind(('127.0.0.1',0));s.listen();print(json.dumps({'pid':os.getpid(),'port':s.getsockname()[1]}),flush=True);time.sleep(120)"
    job_code = "import importlib.util,sys,subprocess,json,time; spec=importlib.util.spec_from_file_location('life',sys.argv[1]); m=importlib.util.module_from_spec(spec);sys.modules['life']=m;spec.loader.exec_module(m);job=m.WindowsJob();p=subprocess.Popen([sys.executable,'-u','-c',sys.argv[2]],stdout=subprocess.PIPE,text=True); print(p.stdout.readline(),flush=True);time.sleep(120)"
    flags = subprocess.CREATE_NO_WINDOW
    supervisor = subprocess.Popen([sys.executable, "-B", "-u", "-c", job_code, str(root / "scripts/team/supervisor.py"), child_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
    handle = None
    results = []
    try:
        # Pipe read has an independent deadline so a failed Job Object cannot hang.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            line = pool.submit(supervisor.stdout.readline).result(timeout=10)
        if not line.strip():
            raise AssertionError("native_job_start_failed")
        child = json.loads(line)
        handle = life.WindowsProcess(supervisor.pid, str(uuid4()))
        identity = handle.identity()
        assert life.listener_owned(child["port"], child["pid"], life.listeners())
        results.append("owned_child_listener_verified")
        # Real occupied loopback port must be refused before assigning a new job.
        manifest = SimpleNamespace(components=[SimpleNamespace(port=child["port"])], state_root=evidence_root, deployment_id=str(uuid4()))
        controller = life.Supervisor(manifest, str(uuid4()))
        try:
            controller.start()
            raise AssertionError("foreign_port_was_adopted")
        except life.LifecycleError as error:
            assert error.code == "foreign_port_in_use"
        assert life.psutil.pid_exists(child["pid"])
        results.append("foreign_port_refused_without_mutation")
        # Simulated supervisor crash using retained exact-process handle.
        handle.terminate(identity)
        supervisor.wait(timeout=10)
        deadline = time.monotonic() + 10
        while life.psutil.pid_exists(child["pid"]) and time.monotonic() < deadline:
            time.sleep(.1)
        assert not life.psutil.pid_exists(child["pid"])
        results.append("job_object_crash_terminated_owned_child")
        assert not any(port == child["port"] for port, _ in life.listeners())
        results.append("owned_listener_closed")
        life.atomic_json(evidence_root / "evidence.json", {"schema_version": 1, "kind": "LOCAL_INTEGRATION", "observed_at": life.utc(), "checks": results, "result": "PASS", "owner_state_touched": False})
        return evidence_root
    finally:
        if handle:
            if handle.alive():
                handle.terminate(identity)
            handle.close()
        elif supervisor.poll() is None:
            # The direct Popen handle still names only our own failed fixture.
            supervisor.kill()
            supervisor.wait(timeout=10)


class SyntheticNativeLifecycle:
    """Real temporary SQLite writer + production maintenance/handle control.

    Tests replace only the launch mechanism with a Python fixture program; stop,
    persisted epoch checks, quiescence evidence and process verification are real.
    This helper does not open listeners or import/configure owner applications.
    """
    def __init__(self, root, maintenance, database_path):
        import threading
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.maintenance, self.database_path = maintenance, Path(database_path)
        self.stop_event = threading.Event()
        self.error = None
        for name in ("python.exe", "vikunja.exe", "caddy.exe", "vikunja.yaml", "Caddyfile"):
            (self.root / name).write_text("synthetic fixture identity pin", encoding="utf-8")
        config = {"schema_version": 1, "deployment_id": maintenance.deployment_id, "project_root": str(self.root), "state_root": str(self.root / ".runtime/lifecycle"), "runtime_config": str(self.root / "runtime.json"), "maintenance_database": str(maintenance.path), "components": []}
        if not maintenance.path.is_relative_to(self.root):
            raise ValueError("fixture_maintenance_must_be_under_root")
        (self.root / "runtime.json").write_text(json.dumps({"deployment_id": maintenance.deployment_id, "project_dir": str(self.root), "control_database_path": str(maintenance.path), "gateway_port": 28766}))
        for kind, filename, port in (("vikunja", "vikunja.yaml", 28765), ("gateway", "runtime.json", 28766), ("proxy", "Caddyfile", 28443)):
            exe = "python.exe" if kind == "gateway" else kind + ".exe" if kind == "vikunja" else "caddy.exe"
            item = {"kind": kind, "executable": str(self.root / exe), "config": str(self.root / filename), "sha256": life.file_hash(self.root / exe), "host": "127.0.0.1", "port": port}
            if kind == "proxy":
                item["environment"] = {"TEAM_PUBLIC_HOST": "fixture.invalid", "TEAM_PUBLIC_ROOT": str(self.root), "TEAM_ASSET_ROUTES": str(self.root / "routes.caddy"), "TEAM_CADDY_DATA": str(self.root / ".runtime/caddy")}
            config["components"].append(item)
        self.manifest_path = self.root / "lifecycle-manifest.json"
        self.manifest_path.write_text(json.dumps(config))
        self.manifest = life.load_manifest(self.manifest_path)
        self.controller = life.Supervisor(self.manifest, str(uuid4()))
        self.controller._maintenance = lambda: self.maintenance
        self.controller.launch = self.launch
        self.thread = threading.Thread(target=self._control, daemon=True)

    def launch(self, kind):
        assert kind == "vikunja"
        code = "import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]);c.execute('CREATE TABLE IF NOT EXISTS lifecycle_fixture_ticks(id INTEGER PRIMARY KEY,value INTEGER)');c.commit();print('READY',flush=True);exec(\"while True:\\n c.execute('INSERT INTO lifecycle_fixture_ticks(value) VALUES(1)');c.commit();time.sleep(.1)\")"
        process = subprocess.Popen([sys.executable, "-B", "-u", "-c", code, str(self.database_path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        handle = life.WindowsProcess(process.pid, self.controller.run_id)
        record = handle.identity()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            assert executor.submit(process.stdout.readline).result(timeout=5).strip() == "READY"
        previous = self.controller.children.get("vikunja")
        if previous:
            assert not previous[1].alive()
            previous[1].close()
            previous[0].wait(timeout=5)
        self.controller.children["vikunja"] = (process, handle, record)
        with self.controller.store.lock():
            state = self.controller._state()
            state["components"]["vikunja"] = record
            self.controller.store.write(state)

    def _control(self):
        while not self.stop_event.wait(.03):
            try:
                self.controller._control_maintenance()
            except life.LifecycleError as error:
                if error.code != "lifecycle_busy":
                    self.error = error
                    return
            except Exception as error:
                self.error = error
                return

    def __enter__(self):
        own = life.WindowsProcess(os.getpid(), self.controller.run_id)
        try:
            record = own.identity()
        finally:
            own.close()
        self.controller.store.write({"schema_version": 1, "deployment_id": self.manifest.deployment_id, "run_id": self.controller.run_id, "desired": "running", "supervisor": record, "components": {}})
        self.launch("vikunja")
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=5)
        for process, handle, record in self.controller.children.values():
            try:
                handle.terminate(record)
                process.wait(timeout=5)
            finally:
                handle.close()

    def quiesce(self, claim):
        return life.quiesce_component(self.manifest_path, maintenance=self.maintenance, claim=claim, timeout=5)

    def verify(self, evidence, claim):
        return life.verify_quiescence(self.manifest_path, evidence, maintenance=self.maintenance, claim=claim)

    def resume(self, claim):
        return life.resume_component(self.manifest_path, maintenance=self.maintenance, claim=claim, timeout=5)


def test_real_sqlite_writer_quiesces_same_barrier_and_resumes_after_locks(tmp_path):
    import sqlite3
    from secretary.infrastructure.team_maintenance import MaintenanceRepository
    root = tmp_path / "native fixture"
    maintenance = MaintenanceRepository(root / ".runtime/control.sqlite", str(uuid4()))
    with SyntheticNativeLifecycle(root, maintenance, root / "vikunja.sqlite") as fixture:
        before = fixture.controller.children["vikunja"][2]
        claim = maintenance.begin_barrier(owner_id="backup")
        evidence = fixture.quiesce(claim)
        assert evidence["pid"] == before["pid"]
        assert life.process_absent(before)
        assert fixture.verify(evidence, claim) == evidence
        maintenance.freeze(claim, evidence)
        with sqlite3.connect(root / "vikunja.sqlite") as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute("SELECT COUNT(*) FROM lifecycle_fixture_ticks").fetchone()[0]
            assert count > 0
            assert fixture.verify(evidence, claim) == evidence
            connection.rollback()
        fixture.resume(claim)
        assert fixture.controller.children["vikunja"][2]["creation_time"] != before["creation_time"]
        assert fixture.controller.children["vikunja"][1].alive()
        maintenance.release(claim)
        with pytest.raises(Exception):
            fixture.verify(evidence, claim)
        assert fixture.error is None


def scheduler_probe():
    """Run actual PS entrypoints with EVERY Scheduler/credential cmdlet faked.

    Explicit CLI only: the offline runner correctly rejects general PowerShell.
    No fake function delegates to a real cmdlet, including failure paths.
    """
    import xml.etree.ElementTree as ET
    root = Path(__file__).parents[1]
    case = root / ".runtime/team-rollout" / ("t12-scheduler-regression-" + uuid4().hex)
    case.mkdir()
    identity = str(uuid4())
    for name in ("python.exe", "vikunja.exe", "caddy.exe", "vikunja.yaml", "Caddyfile"):
        (case / name).write_text("synthetic never-executed fixture")
    runtime = case / "runtime.json"
    control = case / "control.sqlite"
    runtime.write_text(json.dumps({"deployment_id": identity, "project_dir": str(root), "control_database_path": str(control), "gateway_port": 28101, "local_secretary_port": 19876}))
    components = []
    for kind, exe, config, port in (("gateway", "python.exe", "runtime.json", 28101), ("vikunja", "vikunja.exe", "vikunja.yaml", 28102), ("proxy", "caddy.exe", "Caddyfile", 28103)):
        item = {"kind": kind, "executable": str(case / exe), "config": str(case / config), "sha256": life.file_hash(case / exe), "host": "127.0.0.1", "port": port}
        if kind == "proxy":
            item["environment"] = {"TEAM_PUBLIC_HOST": "fixture.invalid", "TEAM_PUBLIC_ROOT": str(case), "TEAM_ASSET_ROUTES": str(case / "routes.caddy"), "TEAM_CADDY_DATA": str(case / "certificates")}
        components.append(item)
    manifest = case / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "deployment_id": identity, "project_root": str(root), "state_root": str(case / "lifecycle"), "runtime_config": str(runtime), "maintenance_database": str(control), "components": components}))
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    def run(name, source):
        script = case / (name + ".ps1")
        script.write_text(source, encoding="utf-8-sig")
        completed = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)], capture_output=True, text=True, timeout=20)
        return completed
    checks = {}
    for mode in ("export_failure", "register_uncertain"):
        output = case / mode
        source = f"""$ErrorActionPreference='Stop'
function Get-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName); return $null }}
function Register-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName,[string]$Xml,[string]$User,[string]$Password); if ({quote(mode)} -eq 'register_uncertain') {{ throw 'synthetic_registration_outcome_unknown' }}; return $true }}
function Export-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName); throw 'synthetic_export_failure' }}
function Unregister-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName,[bool]$Confirm); throw 'unexpected_fake_uninstall' }}
function Get-Credential {{ [CmdletBinding()]param([string]$UserName,[string]$Message); $Secure=[Security.SecureString]::new();foreach($Char in 'synthetic-only'.ToCharArray()){{$Secure.AppendChar($Char)}};return [pscredential]::new($UserName,$Secure) }}
foreach($Name in @('Get-ScheduledTask','Register-ScheduledTask','Export-ScheduledTask','Unregister-ScheduledTask','Get-Credential')) {{ if((Get-Command $Name).CommandType -ne 'Function') {{throw 'fake_required'}} }}
& {quote(root / 'scripts/team/install_tasks.ps1')} -Manifest {quote(manifest)} -RuntimeAccount 'fixture-runtime' -CaptureAccount ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -OutputDirectory {quote(output)} -Apply
exit $LASTEXITCODE
"""
        completed = run(mode, source)
        record = json.loads((output / "tasks-manifest.json").read_text(encoding="utf-8-sig"))
        phases = [item["phase"] for item in record.get("registration_events", [])]
        if mode == "export_failure":
            checks[mode] = (completed.returncode == 2 and record.get("applied") is True and phases == ["registration_started", "registration_succeeded", "export_failed"] and not record.get("installed") and record.get("registered", [{}])[0].get("registered_xml_sha256") is None)
        else:
            checks[mode] = completed.returncode == 2 and phases == ["registration_started", "registration_uncertain"] and record.get("application_state") == "incomplete"
        xml = ET.parse(output / "capture.xml")
        args = xml.find(".//{http://schemas.microsoft.com/windows/2004/02/mit/task}Arguments").text
        checks["capture_explicit_port"] = "-Port 19876 " in args
    xml = "<Task/>"
    description = "synthetic owned definition"
    uninstall_manifest = case / "uninstall.json"
    uninstall_manifest.write_text(json.dumps({"schema_version": 1, "deployment_id": identity, "installed": [{"kind": "runtime", "name": "Secretary-Team-" + identity + "-runtime", "path": "\\SecretaryTeam\\", "description": description, "registered_xml_sha256": hashlib.sha256(xml.encode()).hexdigest()}]}))
    for mode in ("running", "description"):
        events = case / (mode + "-unregister.json")
        source = f"""$ErrorActionPreference='Stop';$script:Exports=0;$script:Changed=$false
function Get-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName); return [pscustomobject]@{{State=$(if($script:Changed -and {quote(mode)} -eq 'running'){{'Running'}}else{{'Ready'}}); Description=$(if($script:Changed -and {quote(mode)} -eq 'description'){{'changed'}}else{{{quote(description)}}})}} }}
function Export-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName); $script:Exports+=1;if($script:Exports -eq 2){{$script:Changed=$true}};return {quote(xml)} }}
function Unregister-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName,[bool]$Confirm); [IO.File]::WriteAllText({quote(events)},'unexpected') }}
foreach($Name in @('Get-ScheduledTask','Export-ScheduledTask','Unregister-ScheduledTask')) {{if((Get-Command $Name).CommandType -ne 'Function'){{throw 'fake_required'}}}}
& {quote(root / 'scripts/team/uninstall_tasks.ps1')} -Manifest {quote(uninstall_manifest)} -Apply
"""
        completed = run(mode, source)
        checks["uninstall_rechecks_" + mode] = completed.returncode != 0 and not events.exists()
    # An incomplete registration cannot be silently called uninstalled or
    # removed using a fabricated registered XML hash.
    unverified_manifest = case / "export_failure/tasks-manifest.json"
    for apply in (False, True):
        source = f"""$ErrorActionPreference='Stop'
function Get-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName); throw 'must_not_adopt_unverified_task' }}
function Export-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName); throw 'must_not_adopt_unverified_task' }}
function Unregister-ScheduledTask {{ [CmdletBinding()]param([string]$TaskPath,[string]$TaskName,[bool]$Confirm); throw 'must_not_remove_unverified_task' }}
foreach($Name in @('Get-ScheduledTask','Export-ScheduledTask','Unregister-ScheduledTask')) {{if((Get-Command $Name).CommandType -ne 'Function'){{throw 'fake_required'}}}}
& {quote(root / 'scripts/team/uninstall_tasks.ps1')} -Manifest {quote(unverified_manifest)} {'-Apply' if apply else ''}
exit $LASTEXITCODE
"""
        completed = run("unverified-" + str(apply), source)
        if apply:
            checks["unverified_apply_refused"] = completed.returncode != 0 and "task_reconciliation_required" in completed.stderr
        else:
            preview = json.loads(completed.stdout)
            checks["unverified_preview_explicit"] = completed.returncode == 0 and len(preview["reconciliation_required"]) == 1 and not preview["remove_tasks"]
    life.atomic_json(case / "evidence.json", {"schema_version": 1, "kind": "FAKE_SCHEDULER_ONLY", "checks": checks, "result": "PASS" if all(checks.values()) else "FAIL", "actual_scheduler_calls": 0})
    print(case)
    assert all(checks.values()), checks
    return case


def native_process_jobs_probe():
    """Own subprocesses only: nested runtime death and actual run_server seam."""
    import queue
    import sqlite3
    import threading
    root = Path(__file__).parents[1]
    case = root / ".runtime/team-rollout" / ("t12-process-jobs-" + uuid4().hex)
    case.mkdir()
    writer_code = "import sqlite3,time,json,os,sys;c=sqlite3.connect(sys.argv[1]);c.execute('CREATE TABLE ticks(value INTEGER)');c.execute('INSERT INTO ticks VALUES(1)');c.commit();print(json.dumps({'writer':os.getpid()}),flush=True);exec(\"while True:\\n c.execute('INSERT INTO ticks VALUES(1)');c.commit();time.sleep(.05)\")"
    runtime_code = "import sys,subprocess,time,os;sys.path.insert(0,sys.argv[1]);from secretary.infrastructure.team_process_job import install_process_job;job=install_process_job();assert job.verify_owned_identity().pid==os.getpid();p=subprocess.Popen([sys.executable,'-B','-u','-c',sys.argv[2],sys.argv[3]],stdout=subprocess.PIPE,text=True);print(p.stdout.readline(),flush=True);time.sleep(120)"
    outer_code = "import sys,subprocess,time,json,os;sys.path.insert(0,sys.argv[1]);from secretary.infrastructure.team_process_job import install_process_job;job=install_process_job();assert job.verify_owned_identity().pid==os.getpid();p=subprocess.Popen([sys.executable,'-B','-u','-c',sys.argv[2],sys.argv[1],sys.argv[3],sys.argv[4]],stdout=subprocess.PIPE,text=True);data=json.loads(p.stdout.readline());data['runtime']=p.pid;print(json.dumps(data),flush=True);time.sleep(120)"
    original_code = r'''
import sys,subprocess,types,asyncio,importlib.util,json
from pathlib import Path
from uuid import uuid4
sys.path.insert(0,sys.argv[1])
settings=types.ModuleType('secretary.team_settings')
settings.load_team_settings=lambda path:types.SimpleNamespace(local_secretary_port=19876)
runtime=types.ModuleType('secretary.orchestration.secretary_team_runtime')
def factory(config,**kwargs):
    from secretary.infrastructure.team_process_job import install_process_job
    import os
    assert install_process_job().verify_owned_identity().pid==os.getpid()
    p=subprocess.Popen([sys.executable,'-B','-u','-c',sys.argv[3],sys.argv[4]],stdout=subprocess.PIPE,text=True)
    print(p.stdout.readline(),flush=True)
    return object()
runtime.create_secretary_team_app=factory
sys.modules['secretary.team_settings']=settings
sys.modules['secretary.orchestration.secretary_team_runtime']=runtime
spec=importlib.util.spec_from_file_location('original_launcher',sys.argv[2])
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
class Server:
    should_exit=False
    async def serve(self,**kwargs):await asyncio.sleep(120)
launcher.uvicorn.Config=lambda *args,**kwargs:object()
launcher.uvicorn.Server=lambda config:Server()
asyncio.run(launcher.run_owned(19876,None,Path('synthetic-never-read.json'),str(uuid4())))
'''
    checks = []
    for kind in ("nested_runtime", "original_secretary"):
        database = case / (kind + ".sqlite")
        args = ([sys.executable, "-B", "-u", "-c", outer_code, str(root / "backend"), runtime_code, writer_code, str(database)] if kind == "nested_runtime" else [sys.executable, "-B", "-u", "-c", original_code, str(root / "backend"), str(root / "scripts/run_server.py"), writer_code, str(database)])
        parent = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        retained = []
        try:
            output = queue.Queue()
            threading.Thread(target=lambda: output.put(parent.stdout.readline()), daemon=True).start()
            line = output.get(timeout=15)
            assert line.strip(), "native_containment_fixture_failed_before_factory"
            children = json.loads(line)
            checks.append(kind + "_kernel_containment_identity_verified")
            own_parent = life.WindowsProcess(parent.pid, str(uuid4()))
            parent_record = own_parent.identity()
            retained.append((own_parent, parent_record))
            writer = life.WindowsProcess(children["writer"], str(uuid4()))
            writer_record = writer.identity()
            retained.append((writer, writer_record))
            target = own_parent
            target_record = parent_record
            if kind == "nested_runtime":
                target = life.WindowsProcess(children["runtime"], str(uuid4()))
                target_record = target.identity()
                retained.append((target, target_record))
            with sqlite3.connect(database) as connection:
                assert connection.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] > 0
            checks.append(kind + "_real_writer_started")
            target.terminate(target_record)
            deadline = time.monotonic() + 10
            while writer.alive() and time.monotonic() < deadline:
                time.sleep(.05)
            assert not writer.alive(), "writer_survived_own_parent_death"
            checks.append(kind + "_writer_terminated_with_parent")
            if kind == "nested_runtime":
                assert own_parent.alive(), "outer_supervisor_must_remain_alive"
                checks.append("outer_supervisor_survives_nested_runtime_crash")
            with sqlite3.connect(database) as connection:
                count = connection.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
                time.sleep(.15)
                assert connection.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == count
            checks.append(kind + "_writes_stopped")
        finally:
            for handle, record in reversed(retained):
                try:
                    if handle.alive():
                        handle.terminate(record)
                finally:
                    handle.close()
            if parent.poll() is None:
                parent.kill()  # Retained Popen handle, only this fixture parent.
            parent.wait(timeout=10)
    life.atomic_json(case / "evidence.json", {"schema_version": 1, "kind": "LOCAL_INTEGRATION", "result": "PASS", "checks": checks, "owner_state_touched": False, "actual_app_factory": False})
    print(case)
    return case


if __name__ == "__main__":
    if sys.argv[1:] == ["--native-fixture"]:
        print(native_probe())
    elif sys.argv[1:] == ["--scheduler-fixture"]:
        scheduler_probe()
    elif sys.argv[1:] == ["--process-jobs-fixture"]:
        native_process_jobs_probe()
    else:
        raise SystemExit("explicit_fixture_flag_required")
