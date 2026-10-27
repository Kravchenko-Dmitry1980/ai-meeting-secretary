"""Real Windows-owned synthetic trees; no model imports or inference."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from secretary.infrastructure import local_asr_process as native


def command(code):
    return [sys.executable, "-B", "-c", code]


def gone(pid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(.02)
    return not psutil.pid_exists(pid)


def test_bounded_output_input_cwd_and_confirmed_receipt(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("External termination or host exit is forbidden")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "_exit", forbidden)
    result = native.run_owned_stage(command(
        "import sys,pathlib; data=sys.stdin.buffer.read();"
        "pathlib.Path('artifact').write_bytes(data);"
        "sys.stdout.buffer.write(data);sys.stderr.write('diagnostic')"),
        cwd=tmp_path, input_bytes=b"synthetic-input", timeout_seconds=5)
    assert result.stdout == b"synthetic-input" and result.stderr == b"diagnostic"
    assert (tmp_path / "artifact").read_bytes() == result.stdout
    assert result.returncode == 0 and result.cleanup_confirmed is True
    assert result.job_commit_limit_bytes == 8192 * native.MIB
    assert result.working_set_limit_bytes == 4608 * native.MIB
    assert result.peak_owned_rss_bytes >= 0


def test_started_callback_is_before_child_code_and_can_abort(tmp_path):
    marker = tmp_path / "never-ran"
    seen = []
    def started(process):
        seen.append((process.pid, psutil.Process(process.pid).create_time()))
        assert not marker.exists()
        raise RuntimeError("synthetic registration failure")
    with pytest.raises(native.StageProcessStopped) as caught:
        native.run_owned_stage(command(f"from pathlib import Path;Path({str(marker)!r}).write_text('bad')"),
            timeout_seconds=5, on_started=started)
    assert caught.value.code == "local_asr_stage_failed"
    assert caught.value.cleanup_confirmed is True
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert not marker.exists() and len(seen) == 1 and gone(seen[0][0])


@pytest.mark.parametrize("reason", ["cancel", "timeout", "callback"])
def test_stop_receipt_requires_owned_child_and_grandchild_exit(tmp_path, reason):
    marker = tmp_path / "pids.json"
    code = ("import os,sys,subprocess,pathlib,json,time;"
        "child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(8)']);"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]));time.sleep(8)")
    def poll(process):
        if reason == "callback" and marker.exists():
            raise RuntimeError("synthetic lease lost")
    with pytest.raises(native.StageProcessStopped) as caught:
        native.run_owned_stage(command(code), timeout_seconds=1.2 if reason == "timeout" else 5,
            on_poll=poll, cancel_requested=lambda: reason == "cancel" and marker.exists())
    assert caught.value.cleanup_confirmed is True
    assert caught.value.exit_code == {"cancel": 130, "timeout": 124, "callback": 78}[reason]
    pids = json.loads(marker.read_text())
    assert len(pids) == 2 and all(gone(pid) for pid in pids)


def test_success_cleans_detached_descendant(tmp_path):
    marker = tmp_path / "child-pid"
    code = ("import sys,subprocess,pathlib;child=subprocess.Popen("
        "[sys.executable,'-B','-c','import time;time.sleep(8)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))")
    result = native.run_owned_stage(command(code), timeout_seconds=5)
    assert result.cleanup_confirmed is True and result.returncode == 0
    assert gone(int(marker.read_text()))


def test_pid_metadata_reuse_never_targets_foreign_process():
    foreign = subprocess.Popen(command("import time;time.sleep(8)"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owned = []
    try:
        def poll(process):
            owned.append(process.pid)
            process.pid = foreign.pid
        with pytest.raises(native.StageProcessStopped) as caught:
            native.run_owned_stage(command("import time;time.sleep(8)"),
                timeout_seconds=5, on_poll=poll, cancel_requested=lambda: bool(owned))
        assert caught.value.cleanup_confirmed is True
        assert len(owned) == 1 and gone(owned[0]) and foreign.poll() is None
    finally:
        foreign.terminate()  # Test-created retained process handle only.
        foreign.wait(timeout=3)


def test_started_pid_mutation_never_resumes_foreign_suspended_process(tmp_path):
    marker = tmp_path / "foreign-must-stay-suspended"
    foreign = subprocess.Popen(command(f"from pathlib import Path;import time;"
        f"Path({str(marker)!r}).touch();time.sleep(8)"),
        creationflags=native.CREATE_SUSPENDED | native.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owned = []
    try:
        def started(process):
            owned.append(process.pid)
            process.pid = foreign.pid
        def stop(process):
            raise RuntimeError("synthetic stop after resume")
        with pytest.raises((native.StageProcessStopped, native.StageProcessCleanupError)):
            native.run_owned_stage(command("import time;time.sleep(8)"),
                timeout_seconds=3, on_started=started, on_poll=stop)
        deadline = time.monotonic() + .5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(.02)
        assert not marker.exists(), "callback PID metadata must never resume a foreign process"
        assert foreign.poll() is None and gone(owned[0])
    finally:
        foreign.terminate()  # Test-created retained handle, including suspended state.
        foreign.wait(timeout=3)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_overflow_is_bounded_and_cleans_process(stream):
    seen = []
    with pytest.raises(native.StageProcessStopped) as caught:
        native.run_owned_stage(command(f"import sys,time;sys.{stream}.buffer.write(b'x'*200000);"
            f"sys.{stream}.flush();time.sleep(8)"), timeout_seconds=5,
            stdout_limit=1024, stderr_limit=1024, on_started=lambda process: seen.append(process.pid))
    assert caught.value.code == f"local_asr_{stream}_limit"
    assert caught.value.cleanup_confirmed is True and gone(seen[0])


def test_real_native_commit_cap_denies_allocation_without_host_pressure():
    # 128 MiB requested allocation under a 96 MiB commit cap. No gigabyte load.
    code = "import sys\ntry:\n data=bytearray(128*1024**2)\nexcept MemoryError:\n sys.exit(73)\nsys.exit(99)"
    result = native.run_owned_stage(command(code), timeout_seconds=5,
        job_commit_limit_bytes=96 * native.MIB, working_set_limit_bytes=64 * native.MIB)
    assert result.returncode == 73 and result.cleanup_confirmed is True
    assert result.job_commit_limit_bytes == 96 * native.MIB


def test_aggregate_rss_watchdog_stops_real_owned_tree():
    seen = []
    with pytest.raises(native.StageProcessStopped) as caught:
        native.run_owned_stage(command("import time;time.sleep(8)"),
            timeout_seconds=5, owned_rss_limit_bytes=1,
            on_started=lambda process: seen.append(process.pid))
    assert caught.value.code == "local_asr_owned_rss_limit"
    assert caught.value.cleanup_confirmed is True and gone(seen[0])


def test_native_limit_readback_failure_never_spawns(monkeypatch):
    def fail(job, requested):
        raise OSError("synthetic readback failure")
    monkeypatch.setattr(native._WindowsJob, "_verify_limits", fail)
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda *args, **kwargs: pytest.fail("must not spawn"))
    with pytest.raises(native.StageProcessUnavailable):
        native.run_owned_stage(command("pass"), timeout_seconds=5)


def test_cleanup_accounting_failure_never_confirms_stop(monkeypatch):
    def fail(job, deadline):
        raise OSError("synthetic accounting failure")
    monkeypatch.setattr(native._WindowsJob, "wait_empty", fail)
    with pytest.raises(native.StageProcessCleanupError) as caught:
        native.run_owned_stage(command("import time;time.sleep(8)"),
            timeout_seconds=5, cancel_requested=lambda: True)
    assert caught.value.cleanup_confirmed is False


def test_postcreation_constructor_failure_reaps_suspended_child(tmp_path, monkeypatch):
    marker = tmp_path / "never-ran"
    seen = []
    original = subprocess.Popen.__init__
    def fail(process, *args, **kwargs):
        original(process, *args, **kwargs)
        seen.append(process.pid)
        raise OSError("synthetic registration failure")
    monkeypatch.setattr(subprocess.Popen, "__init__", fail)
    with pytest.raises(native.StageProcessStopped) as caught:
        native.run_owned_stage(command(f"from pathlib import Path;Path({str(marker)!r}).touch()"),
            timeout_seconds=5)
    assert caught.value.cleanup_confirmed is True
    assert not marker.exists() and len(seen) == 1 and gone(seen[0])


def test_unassigned_root_cleanup_does_not_require_job_terminate(monkeypatch):
    seen = []
    def reject(job, process):
        seen.append(process.pid)
        raise OSError("synthetic assignment failure")
    monkeypatch.setattr(native._WindowsJob, "assign", reject)
    monkeypatch.setattr(native._WindowsJob, "terminate", lambda job: pytest.fail("empty job termination"))
    with pytest.raises(native.StageProcessStopped) as caught:
        native.run_owned_stage(command("pass"), timeout_seconds=5)
    assert caught.value.cleanup_confirmed is True and gone(seen[0])


def test_rss_sampler_never_reads_foreign_handle_after_pid_reuse(monkeypatch):
    job = native._WindowsJob.__new__(native._WindowsJob)
    job.handle, job.owner_pid = 100, os.getpid()
    closed = []
    job.kernel = SimpleNamespace(OpenProcess=lambda *args: 200,
        IsProcessInJob=lambda *args: True, CloseHandle=lambda handle: closed.append(handle) or True)
    job.psapi = SimpleNamespace(GetProcessMemoryInfo=lambda *args: pytest.fail("foreign memory read"))
    monkeypatch.setattr(job, "process_ids", lambda: ())
    assert job._sample_rss((999,)) == 0
    assert closed == [200]


def test_owned_rss_is_aggregate_and_unstable_membership_fails_closed(monkeypatch):
    job = native._WindowsJob.__new__(native._WindowsJob)
    job.handle, job.owner_pid = 100, os.getpid()
    closed = []
    def member(handle, owner, out):
        ctypes.cast(out, ctypes.POINTER(ctypes.c_long)).contents.value = 1
        return True
    def memory(handle, out, size):
        ctypes.cast(out, ctypes.POINTER(native._MemoryCounters)).contents.WorkingSetSize = handle * 100
        return True
    job.kernel = SimpleNamespace(OpenProcess=lambda rights, inherit, pid: pid,
        IsProcessInJob=member, CloseHandle=lambda handle: closed.append(handle) or True)
    job.psapi = SimpleNamespace(GetProcessMemoryInfo=memory)
    monkeypatch.setattr(job, "process_ids", lambda: (10, 20))
    assert job.owned_rss() == 3000 and closed == [10, 20]
    sets = iter([(10,), (20,)] * 3)
    monkeypatch.setattr(job, "process_ids", lambda: next(sets))
    with pytest.raises(native.StageProcessStopped, match="measurement_unstable") as caught:
        job.owned_rss()
    assert caught.value.cleanup_confirmed is False


def test_preexisting_cancel_never_resumes_child(tmp_path):
    marker = tmp_path / "never-ran"
    seen = []
    with pytest.raises(native.StageProcessStopped) as caught:
        native.run_owned_stage(command(f"from pathlib import Path;Path({str(marker)!r}).touch()"),
            timeout_seconds=5, cancel_requested=lambda: True,
            on_started=lambda process: seen.append(process.pid))
    assert caught.value.cleanup_confirmed is True and caught.value.exit_code == 130
    assert not marker.exists() and gone(seen[0])


def test_failed_job_close_retains_handle_and_never_claims_cleanup():
    job = native._WindowsJob.__new__(native._WindowsJob)
    job.handle, job.owner_pid = 100, os.getpid()
    job.kernel = SimpleNamespace(CloseHandle=lambda handle: False)
    with pytest.raises(native.StageProcessCleanupError) as caught:
        job.close()
    assert job.handle == 100 and caught.value.cleanup_confirmed is False


@pytest.mark.parametrize("kwargs", [
    {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
    {"job_commit_limit_bytes": 8192 * native.MIB + 1},
    {"working_set_limit_bytes": 4608 * native.MIB + 1},
    {"owned_rss_limit_bytes": True}, {"stdout_limit": -1}, {"input_bytes": "text"}])
def test_invalid_or_weakened_boundaries_never_spawn(kwargs, monkeypatch):
    monkeypatch.setattr(native, "_WindowsJob", lambda **kwargs: pytest.fail("must not create job"))
    with pytest.raises(ValueError):
        native.run_owned_stage(command("pass"), **kwargs)


def test_native_layout_and_non_windows_failure(monkeypatch):
    assert ctypes.sizeof(native._ExtendedLimits) == 144
    assert ctypes.sizeof(native._MemoryCounters) == 72
    monkeypatch.setattr(native.os, "name", "posix")
    with pytest.raises(native.StageProcessUnavailable, match="windows_required"):
        native._WindowsJob(job_commit_limit_bytes=96*native.MIB, working_set_limit_bytes=64*native.MIB)
