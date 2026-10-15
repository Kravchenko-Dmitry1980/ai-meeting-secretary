"""Policy checks use a synthetic host; native containment needs separate evidence."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from uuid import uuid4

import pytest


def load_supervisor():
    spec = importlib.util.spec_from_file_location("secretary_local_supervisor", Path(__file__).parents[1] / "scripts/local_supervisor.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SyntheticHost:
    def __init__(self, exits, *, stop_backoff=False, ready_error=None):
        self.exits = iter(exits)
        self.stop_backoff = stop_backoff
        self.ready_error = ready_error
        self.stopped = False
        self.events = []

    def stop_requested(self):
        return self.stopped

    def publish(self, state, **fields):
        self.events.append((state, fields))

    def start_attempt(self):
        self.events.append(("spawn", {}))
        return object()

    def await_ready(self, attempt):
        if self.ready_error:
            raise self.ready_error

    def await_exit(self, attempt):
        outcome = next(self.exits)
        self.stopped = outcome == "stop"
        return 0 if self.stopped else 7

    def drain_attempt(self, attempt):
        self.events.append(("drained", {}))

    def backoff(self, delay):
        self.events.append(("delay", {"seconds": delay}))
        self.stopped = self.stop_backoff


def test_public_loop_stops_without_restart():
    module = load_supervisor()
    host = SyntheticHost(["stop"])
    module.run_loop(host)
    assert [name for name, _ in host.events].count("spawn") == 1
    assert host.events[-1][0] == "stopped"


def test_public_loop_exhausts_three_restarts_and_keeps_failure():
    module = load_supervisor()
    host = SyntheticHost(["crash"] * 4)
    with pytest.raises(module.SupervisorError, match="restart_limit_exhausted"):
        module.run_loop(host)
    assert [name for name, _ in host.events].count("spawn") == 4
    assert [fields["seconds"] for name, fields in host.events if name == "delay"] == [1, 2, 4]
    assert host.events[-1][0] == "failed"


def test_public_loop_stop_during_backoff_prevents_next_spawn():
    module = load_supervisor()
    host = SyntheticHost(["crash"], stop_backoff=True)
    module.run_loop(host)
    assert [name for name, _ in host.events].count("spawn") == 1
    assert host.events[-1][0] == "stopped"


def test_public_loop_startup_failure_is_terminal_without_retry():
    module = load_supervisor()
    host = SyntheticHost([], ready_error=module.SupervisorError("readiness_timeout"))
    with pytest.raises(module.SupervisorError, match="readiness_timeout"):
        module.run_loop(host)
    assert [name for name, _ in host.events].count("spawn") == 1
    assert [name for name, _ in host.events].count("drained") == 1
    assert host.events[-1][1]["error"] == "readiness_timeout"


def test_public_loop_honors_stop_at_last_spawn_boundary():
    module = load_supervisor()
    host = SyntheticHost([])

    def stopped_before_spawn():
        host.stopped = True
        return None

    host.start_attempt = stopped_before_spawn
    module.run_loop(host)
    assert host.events[-1][0] == "stopped"
    assert "spawn" not in [name for name, _ in host.events]


def test_public_loop_unknown_tree_identity_cannot_restart():
    module = load_supervisor()
    host = SyntheticHost(["crash"])

    def unknown_tree(attempt):
        raise module.SupervisorError("owned_tree_not_quiescent")

    host.drain_attempt = unknown_tree
    with pytest.raises(module.SupervisorError, match="owned_tree_not_quiescent"):
        module.run_loop(host)
    assert [name for name, _ in host.events].count("spawn") == 1
    assert host.events[-1][0] == "failed"


def fixture_state(module, tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    return runtime, {"schema_version": 1, "owner_kind": "local_supervisor",
                     "project": str(module.ROOT), "runtime_root": str(runtime),
                     "supervisor_run_id": str(uuid4()), "supervisor_identity": {"pid": 123},
                     "attempt_identities": [], "state": "backoff"}


def test_public_stop_during_backoff_retains_durable_intent_and_state(tmp_path, monkeypatch):
    module = load_supervisor()
    runtime, state = fixture_state(module, tmp_path)
    module.atomic_json(runtime / "processes.json", state)
    monkeypatch.setattr(module, "identity_alive", lambda identity: False)
    result = module.request_stop(runtime)
    marker = module.stop_path(runtime, state["supervisor_run_id"])
    assert marker.read_text() == state["supervisor_run_id"]
    assert result["stop_requested"] is True
    assert module.read_state(runtime) == state


def test_public_stop_unknown_identity_writes_no_marker(tmp_path, monkeypatch):
    module = load_supervisor()
    runtime, state = fixture_state(module, tmp_path)
    module.atomic_json(runtime / "processes.json", state)

    def unavailable(identity):
        raise module.SupervisorError("process_identity_unavailable")

    monkeypatch.setattr(module, "identity_alive", unavailable)
    with pytest.raises(module.SupervisorError, match="process_identity_unavailable"):
        module.request_stop(runtime)
    assert not module.stop_path(runtime, state["supervisor_run_id"]).exists()
    assert module.read_state(runtime) == state


def test_public_status_reports_dead_unfinished_owner_without_rewriting_receipt(tmp_path, monkeypatch):
    module = load_supervisor()
    runtime, state = fixture_state(module, tmp_path)
    module.atomic_json(runtime / "processes.json", state)
    monkeypatch.setattr(module, "identity_alive", lambda identity: False)
    assert module.status(runtime)["state"] == "owner_exited"
    assert module.read_state(runtime) == state


def test_public_status_keeps_terminal_failure_visible(tmp_path, monkeypatch):
    module = load_supervisor()
    runtime, state = fixture_state(module, tmp_path)
    state.update(state="failed", error="restart_limit_exhausted")
    module.atomic_json(runtime / "processes.json", state)
    monkeypatch.setattr(module, "identity_alive", lambda identity: False)
    result = module.status(runtime)
    assert result["state"] == "failed"
    assert result["error"] == "restart_limit_exhausted"


def test_runtime_rejects_outside_project_and_relative_paths(tmp_path, monkeypatch):
    module = load_supervisor()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(module, "ROOT", project)
    with pytest.raises(module.SupervisorError, match="runtime_outside_project"):
        module.runtime_path(tmp_path / "external")
    with pytest.raises(module.SupervisorError, match="absolute_local_path_required"):
        module.runtime_path(Path("relative"))


def test_manual_owner_cannot_be_adopted_by_status_or_stop(tmp_path):
    module = load_supervisor()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state = {"pid": 123, "project": str(module.ROOT)}
    module.atomic_json(runtime / "processes.json", state)
    for action in (module.status, module.request_stop):
        with pytest.raises(module.SupervisorError, match="supervisor_owner_state_required"):
            action(runtime)
    assert module.read_state(runtime) == state


def test_pythonw_no_stdout_still_returns_meaningful_error(tmp_path, monkeypatch):
    module = load_supervisor()
    monkeypatch.setattr(module.sys, "stdout", None)
    assert module.main(["status", "--runtime-root", "relative"]) == 2


def test_listener_discovery_refuses_exited_redirector_before_process_traversal(monkeypatch):
    module = load_supervisor()
    host = module.WindowsHost.__new__(module.WindowsHost)
    monkeypatch.setattr(module, "identity_alive", lambda record: False)
    monkeypatch.setattr(module.psutil, "Process", lambda pid: pytest.fail("Foreign PID must not be traversed"))
    process = type("Process", (), {"pid": 123})()
    with pytest.raises(module.SupervisorError, match="launcher_identity_changed"):
        host.discover_listener({"process": process, "identities": [{"pid": 123}]})


def test_manual_handoff_preserves_terminal_receipt_and_current_state(tmp_path, monkeypatch):
    module = load_supervisor()
    runtime, state = fixture_state(module, tmp_path)
    state["state"] = "stopped"
    module.atomic_json(runtime / "processes.json", state)
    monkeypatch.setattr(module, "identity_alive", lambda record: False)
    assert module.prepare_manual(runtime)["state"] == "terminal_archived"
    assert module.read_state(runtime) == state
    archive = runtime / "local-supervisor" / state["supervisor_run_id"] / "terminal-state.json"
    assert module.json.loads(archive.read_text()) == state
    assert module.prepare_manual(runtime)["state"] == "terminal_archived"


def test_manual_handoff_refuses_active_or_unknown_owner(tmp_path, monkeypatch):
    module = load_supervisor()
    runtime, state = fixture_state(module, tmp_path)
    module.atomic_json(runtime / "processes.json", state)
    with pytest.raises(module.SupervisorError, match="terminal_supervisor_state_required"):
        module.prepare_manual(runtime)
    state["state"] = "stopped"
    module.atomic_json(runtime / "processes.json", state)
    monkeypatch.setattr(module, "identity_alive", lambda record: True)
    with pytest.raises(module.SupervisorError, match="existing_owner_alive"):
        module.prepare_manual(runtime)


def test_start_identity_capture_failure_never_enters_retry_loop():
    module = load_supervisor()
    host = SyntheticHost([])

    def identity_race():
        host.events.append(("spawn", {}))
        raise module.SupervisorError("launcher_identity_unavailable")

    host.start_attempt = identity_race
    with pytest.raises(module.SupervisorError, match="launcher_identity_unavailable"):
        module.run_loop(host)
    assert [name for name, _ in host.events].count("spawn") == 1
    assert not any(name == "delay" for name, _ in host.events)


def test_configuration_drift_refused_before_data_lock_or_port_probe():
    module = load_supervisor()
    host = module.WindowsHost.__new__(module.WindowsHost)
    host.fingerprint = "original"
    host.configuration_fingerprint = lambda: "changed"
    with pytest.raises(module.SupervisorError, match="configuration_changed"):
        host.validate_start()


def test_backoff_error_is_visible_without_resetting_restart_counter():
    module = load_supervisor()
    host = SyntheticHost(["crash"])

    def configuration_changed(delay):
        raise module.SupervisorError("configuration_changed")

    host.backoff = configuration_changed
    with pytest.raises(module.SupervisorError, match="configuration_changed"):
        module.run_loop(host)
    assert host.events[-1] == ("failed", {"restart_count": 1, "error": "configuration_changed"})
