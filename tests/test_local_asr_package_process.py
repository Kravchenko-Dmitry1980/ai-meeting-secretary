"""Real synthetic child ownership; models and donor runtime are never loaded."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "secretary_package_process", ROOT / "config/local-asr/package-overlay/dev_asr_process.py")
process_adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(process_adapter)


def gone(pid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(.02)
    return not psutil.pid_exists(pid)


def test_returns_real_output_without_taskkill_or_parent_exit(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("External kill or parent exit forbidden")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "_exit", forbidden)
    assert process_adapter.run_owned_stage(
        [sys.executable, "-B", "-c", "import sys;sys.stderr.write('owned-result')"],
        timeout_seconds=5) == (0, b"owned-result")


@pytest.mark.parametrize("stop", ["cancel", "timeout", "poll_error"])
def test_stops_real_owned_child_and_grandchild(tmp_path, stop):
    marker = tmp_path / "pids.json"
    child_code = "import time;time.sleep(8)"
    code = ("import os,sys,subprocess,json,pathlib,time;"
            f"child=subprocess.Popen([sys.executable,'-B','-c',{child_code!r}]);"
            f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]));"
            "time.sleep(8)")
    def poll(process):
        if stop == "poll_error" and marker.exists():
            raise OSError("synthetic sampler failure")
    def cancel():
        return stop == "cancel" and marker.exists()
    expected = OSError if stop == "poll_error" else process_adapter.StageProcessStopped
    with pytest.raises(expected) as raised:
        process_adapter.run_owned_stage([sys.executable, "-B", "-c", code],
            timeout_seconds=1.5 if stop == "timeout" else 5,
            cancel_requested=cancel, on_poll=poll)
    if stop != "poll_error":
        assert raised.value.exit_code == (124 if stop == "timeout" else 130)
    assert marker.exists()
    pids = json.loads(marker.read_text())
    assert len(pids) == 2 and os.getpid() not in pids
    assert all(gone(pid) for pid in pids)


def test_success_cleans_detached_descendant(tmp_path):
    marker = tmp_path / "detached.json"
    code = ("import sys,subprocess,pathlib;"
            "child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(8)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))")
    assert process_adapter.run_owned_stage([sys.executable, "-B", "-c", code], timeout_seconds=5)[0] == 0
    assert gone(int(marker.read_text()))


def test_changed_pid_metadata_cannot_target_another_process():
    unrelated = subprocess.Popen([sys.executable, "-B", "-c", "import time;time.sleep(8)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    own_pid = []
    try:
        def changed_metadata(process):
            own_pid.append(process.pid)
            process.pid = unrelated.pid
        with pytest.raises(process_adapter.StageProcessStopped, match="cancel"):
            process_adapter.run_owned_stage([sys.executable, "-B", "-c", "import time;time.sleep(8)"],
                timeout_seconds=5, on_poll=changed_metadata, cancel_requested=lambda: True)
        assert len(own_pid) == 1 and gone(own_pid[0])
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()  # The test's own retained CreateProcess handle.
        unrelated.wait(timeout=3)


def test_assignment_failure_never_runs_child(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-exist"
    seen = []
    def reject(job, process):
        seen.append(process.pid)
        raise RuntimeError("synthetic assignment failure")
    monkeypatch.setattr(process_adapter._WindowsJob, "assign", reject)
    with pytest.raises(RuntimeError, match="assignment failure"):
        process_adapter.run_owned_stage([sys.executable, "-B", "-c",
            f"from pathlib import Path;Path({str(marker)!r}).write_text('ran')"], timeout_seconds=5)
    assert not marker.exists() and len(seen) == 1 and gone(seen[0])


def test_postcreation_constructor_failure_cleans_suspended_root(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-run"
    original = subprocess.Popen.__init__
    seen = []
    def postcreation_failure(process, *args, **kwargs):
        original(process, *args, **kwargs)
        seen.append(process.pid)
        raise OSError("synthetic registration failure")
    monkeypatch.setattr(subprocess.Popen, "__init__", postcreation_failure)
    with pytest.raises(OSError, match="registration failure"):
        process_adapter.run_owned_stage([sys.executable, "-B", "-c",
            f"from pathlib import Path;Path({str(marker)!r}).write_text('ran')"], timeout_seconds=5)
    assert not marker.exists() and len(seen) == 1 and gone(seen[0])


def test_unassigned_root_does_not_depend_on_empty_job_termination(tmp_path, monkeypatch):
    marker = tmp_path / "must-not-run"
    seen, terminated = [], []
    def reject(job, process):
        seen.append(process.pid)
        raise OSError("synthetic assignment failure")
    def bad_terminate(job):
        terminated.append(True)
        raise OSError("synthetic empty job failure")
    monkeypatch.setattr(process_adapter._WindowsJob, "assign", reject)
    monkeypatch.setattr(process_adapter._WindowsJob, "terminate", bad_terminate)
    with pytest.raises(OSError, match="assignment failure"):
        process_adapter.run_owned_stage([sys.executable, "-B", "-c",
            f"from pathlib import Path;Path({str(marker)!r}).write_text('ran')"], timeout_seconds=5)
    assert not terminated and not marker.exists() and len(seen) == 1 and gone(seen[0])


def test_thread_identity_change_is_never_resumed(monkeypatch):
    resumed, closed = [], []
    job = process_adapter._WindowsJob.__new__(process_adapter._WindowsJob)
    job.owner_pid, job.handle, job.assigned = os.getpid(), 100, True
    job.kernel = SimpleNamespace(
        OpenThread=lambda *args: 200,
        GetProcessIdOfThread=lambda handle: 998,
        ResumeThread=lambda handle: resumed.append(handle),
        CloseHandle=lambda handle: closed.append(handle),
    )
    monkeypatch.setattr(job, "_initial_thread", lambda process: 300)
    with pytest.raises(process_adapter.StageProcessCleanupError, match="owner changed"):
        job.resume(SimpleNamespace(pid=999, _handle=400))
    assert resumed == [] and closed == [200]


def test_resume_is_only_after_membership(monkeypatch):
    job = process_adapter._WindowsJob.__new__(process_adapter._WindowsJob)
    job.owner_pid, job.handle, job.assigned = os.getpid(), 100, False
    with pytest.raises(process_adapter.StageProcessCleanupError, match="uncontained"):
        job.resume(SimpleNamespace(pid=999, _handle=400))


def test_cleanup_failure_is_reported_without_exiting_host(monkeypatch):
    def fail(job, deadline):
        raise process_adapter.StageProcessCleanupError("synthetic accounting failure")
    monkeypatch.setattr(process_adapter._WindowsJob, "wait_empty", fail)
    def forbidden(*args):
        raise AssertionError("Host exit is forbidden")
    monkeypatch.setattr(os, "_exit", forbidden)
    with pytest.raises(process_adapter.StageProcessCleanupError, match="cleanup unverified"):
        process_adapter.run_owned_stage([sys.executable, "-B", "-c", "pass"], timeout_seconds=5)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_invalid_deadline_does_not_create_job(timeout, monkeypatch):
    monkeypatch.setattr(process_adapter, "_WindowsJob", lambda: pytest.fail("must not create job"))
    with pytest.raises(ValueError, match="deadline"):
        process_adapter.run_owned_stage([sys.executable, "-B", "-c", "pass"], timeout_seconds=timeout)


def test_native_x64_layout_is_explicit():
    assert ctypes.sizeof(ctypes.c_void_p) == 8
    assert [ctypes.sizeof(kind) for kind in (process_adapter._BasicLimits,
            process_adapter._IoCounters, process_adapter._ExtendedLimits,
            process_adapter._Accounting, process_adapter._ThreadEntry)] == [64, 48, 144, 48, 28]
