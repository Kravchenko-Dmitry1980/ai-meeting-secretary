"""Synthetic admission/ownership checks; no models, paid calls, or live state."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import gc
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import weakref

import psutil
import pytest

from secretary.domain.voice import VoiceError
from secretary.infrastructure.local_asr_resources import (
    DeviceReservation, ResourceAdmissionError, ResourceProfile, ResourceSnapshot, check_admission,
)


GPU = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def cuda_profile(**kwargs):
    return ResourceProfile(asr_device="cuda", physical_gpu_id=GPU, **kwargs)


def snapshot(ram=20000, commit=20000, vram=16000, **kwargs):
    return ResourceSnapshot(ram, commit, {GPU: vram}, **kwargs)


@pytest.mark.parametrize("field,value,reason", [
    ("ram_reserve_mib", 6655, "ram_guard_weakened"),
    ("commit_reserve_mib", 9215, "commit_guard_weakened"),
    ("cuda_free_vram_mib", 10239, "vram_guard_weakened"),
    ("allocator_budget_mib", 8193, "allocator_guard_weakened"),
    ("owned_rss_limit_mib", 4609, "rss_guard_weakened"),
    ("owned_rss_limit_mib", 0, "rss_limit_invalid"),
    ("job_commit_limit_mib", 8193, "job_commit_guard_weakened"),
    ("job_commit_limit_mib", True, "job_commit_limit_invalid"),
    ("cpu_threads", 0, "cpu_threads_invalid"),
    ("cpu_threads", True, "cpu_threads_invalid"),
    ("deadline_seconds", 0, "deadline_invalid"),
    ("physical_gpu_id", "cuda:0", "physical_gpu_identity_required"),
    ("physical_gpu_id", "MIG-GPU-aaa/1/0", "physical_gpu_identity_required"),
])
def test_profile_cannot_weaken_historical_guards_or_alias_device(field, value, reason):
    with pytest.raises(ValueError, match=reason):
        ResourceProfile(**{field: value})


def test_profile_is_immutable_and_retains_guard_meaning():
    profile = cuda_profile()
    assert (profile.ram_reserve_mib, profile.commit_reserve_mib,
            profile.cuda_free_vram_mib, profile.allocator_budget_mib) == (6656, 9216, 10240, 8192)
    assert (profile.owned_rss_limit_mib, profile.job_commit_limit_mib) == (4608, 8192)
    with pytest.raises(FrozenInstanceError):
        profile.ram_reserve_mib = 1
    # Smaller allocator budget is a tighter ceiling, not a lower admission bar.
    assert cuda_profile(allocator_budget_mib=4096).allocator_budget_mib == 4096
    assert cuda_profile(owned_rss_limit_mib=4096, job_commit_limit_mib=6144).job_commit_limit_mib == 6144


@pytest.mark.parametrize("ram,commit,vram,reasons", [
    (6655, 9216, 10240, ("insufficient_ram",)),
    (6656, 9215, 10240, ("insufficient_commit",)),
    (6656, 9216, 10239, ("insufficient_vram",)),
    (None, 9216, 10240, ("ram_measurement_unavailable",)),
    (6656, None, 10240, ("commit_measurement_unavailable",)),
    (6656, 9216, None, ("vram_measurement_unavailable",)),
    (float("nan"), 9216, 10240, ("ram_measurement_unavailable",)),
    (6656, float("inf"), 10240, ("commit_measurement_unavailable",)),
    (6656, 9216, -1, ("vram_measurement_unavailable",)),
    (True, 9216, 10240, ("ram_measurement_unavailable",)),
])
def test_admission_denial_preserves_reasons_without_fallback(ram, commit, vram, reasons):
    result = check_admission(cuda_profile(), snapshot(ram, commit, vram))
    assert result.allowed is False
    assert result.state == "waiting_resources"
    assert result.reasons == reasons
    assert not hasattr(result, "fallback_provider")


def test_exact_thresholds_pass_but_stale_or_future_snapshots_do_not():
    profile = cuda_profile()
    assert check_admission(profile, snapshot(6656, 9216, 10240)).allowed
    assert "resource_snapshot_stale" in check_admission(
        profile, snapshot(sampled_at=10), monotonic_now=16).reasons
    assert "resource_snapshot_stale" in check_admission(
        profile, snapshot(sampled_at=20), monotonic_now=16).reasons


def test_cpu_asr_with_gpu_nemotron_requires_own_verified_guard_and_gpu_slot(tmp_path):
    unverified = ResourceProfile(physical_gpu_id=GPU)
    assert unverified.uses_gpu
    assert check_admission(unverified, snapshot()).reasons == ("nemotron_vram_guard_unverified",)
    cpu = ResourceProfile(physical_gpu_id=GPU, nemotron_free_vram_mib=6000)
    assert check_admission(cpu, snapshot(vram=6000)).allowed
    with DeviceReservation(tmp_path, cpu) as held:
        assert held.resource_key == DeviceReservation(tmp_path, cuda_profile()).resource_key
        with pytest.raises(ResourceAdmissionError, match="physical_gpu_busy"):
            with DeviceReservation(tmp_path, cuda_profile()):
                pytest.fail("Another profile acquired the same physical device")
    with DeviceReservation(tmp_path, cuda_profile()) as released:
        assert released.check_stage(snapshot).allowed


def test_unconfigured_shared_root_and_unknown_device_fail_closed(tmp_path):
    with pytest.raises(ResourceAdmissionError, match="shared_resource_root_unconfigured"):
        with DeviceReservation(None, cuda_profile()):
            pass
    with pytest.raises(ResourceAdmissionError, match="physical_gpu_identity_unverified"):
        with DeviceReservation(tmp_path, ResourceProfile()):
            pass
    cpu = ResourceProfile(asr_device="cpu", diarization_device="cpu")
    with DeviceReservation(None, cpu) as reservation:
        assert reservation.resource_key is None
        assert reservation.check_stage(lambda: ResourceSnapshot(6656, 9216)).allowed


def test_every_stage_rechecks_fresh_measurements_and_does_not_start_denied_process(tmp_path):
    marker = tmp_path / "must-not-exist.txt"
    with DeviceReservation(tmp_path / "locks", cuda_profile()) as reservation:
        assert reservation.check_stage(snapshot).allowed
        with pytest.raises(ResourceAdmissionError) as exc:
            reservation.run_stage([sys.executable, "-c", f"open({str(marker)!r}, 'w').write('bad')"],
                                  snapshot_supplier=lambda: snapshot(ram=1))
        assert exc.value.reasons == ("insufficient_ram",)
        assert not marker.exists()
        with pytest.raises(ResourceAdmissionError, match="resource_measurement_failed"):
            reservation.check_stage(lambda: 123)
        with pytest.raises(ResourceAdmissionError, match="resource_measurement_failed"):
            reservation.check_stage(lambda: (_ for _ in ()).throw(RuntimeError("private diagnostic")))


def test_reservation_cannot_be_used_before_enter_or_after_release(tmp_path):
    reservation = DeviceReservation(tmp_path, cuda_profile())
    with pytest.raises(ResourceAdmissionError, match="resource_reservation_not_owned"):
        reservation.check_stage(snapshot)
    with reservation:
        with pytest.raises(ResourceAdmissionError, match="resource_reservation_already_owned"):
            reservation.__enter__()
    with pytest.raises(ResourceAdmissionError, match="resource_reservation_not_owned"):
        reservation.check_stage(snapshot)


def test_os_lock_blocks_another_process_on_same_gpu_and_canonicalizes_case(tmp_path):
    # Child process proves OS arbitration rather than only an in-process mutex.
    root = Path(__file__).resolve().parents[1]
    code = "\n".join([
        "import sys",
        f"sys.path.insert(0, {str(root / 'backend')!r})",
        "from secretary.infrastructure.local_asr_resources import *",
        "try:",
        f"    with DeviceReservation({str(tmp_path)!r}, ResourceProfile(asr_device='cuda', physical_gpu_id={GPU.lower()!r})):",
        "        print('incorrectly_admitted')",
        "except ResourceAdmissionError as error:",
        "    print(error.code)",
    ])
    with DeviceReservation(tmp_path, cuda_profile()):
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.strip() == b"physical_gpu_busy"


@pytest.mark.parametrize("kind", ["cancel", "timeout"])
def test_synthetic_owned_tree_cleanup_leaves_other_process_alive(tmp_path, kind):
    # Both processes are synthetic test-owned instances. The runtime may only
    # terminate the child it launched, not another process with the same name.
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    marker = tmp_path / "owned.json"
    child_code = "import time; time.sleep(30)"
    script = ("import subprocess,sys,time,json,os;from pathlib import Path;"
              f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));"
              f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),p.pid]));time.sleep(30)")
    cancel = threading.Event()
    errors = []
    profile = cuda_profile(deadline_seconds=2 if kind == "timeout" else 20)
    reservation = DeviceReservation(tmp_path / "locks", profile)
    thread = None
    try:
        with reservation:
            def execute():
                try:
                    reservation.run_stage([sys.executable, "-c", script], snapshot_supplier=snapshot,
                                          cancel=cancel)
                except VoiceError as error:
                    errors.append(error.reason)
            thread = threading.Thread(target=execute)
            thread.start()
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.025)
            assert marker.exists()
            owned = json.loads(marker.read_text())
            time.sleep(0.1)  # Allow the existing runner's descendant identity sample.
            with pytest.raises(ResourceAdmissionError, match="resource_stage_still_running"):
                reservation.__exit__(None, None, None)
            with pytest.raises(ResourceAdmissionError, match="resource_stage_already_running"):
                reservation.run_stage([sys.executable, "-c", "print('not run')"], snapshot_supplier=snapshot)
            if kind == "cancel":
                cancel.set()
            thread.join(10)
            assert not thread.is_alive()
            assert errors == ["cancelled" if kind == "cancel" else "process_timeout"]
            assert all(not psutil.pid_exists(pid) for pid in owned)
            assert sentinel.poll() is None
            # Even though this fixture can inspect its two PIDs, the generic
            # runner's exception is not an exhaustive native-exit receipt.
            assert reservation.quarantined
            assert reservation.quarantine_path.is_dir()
            with pytest.raises(ResourceAdmissionError, match="physical_gpu_quarantined"):
                reservation.check_stage(snapshot)
            with pytest.raises(ResourceAdmissionError, match="physical_gpu_quarantined"):
                with DeviceReservation(tmp_path / "locks", cuda_profile()):
                    pytest.fail("An uncertain runner released its device")
    finally:
        cancel.set()
        if thread is not None:
            thread.join(10)
        sentinel.kill()
        sentinel.wait(timeout=5)


def test_owned_stage_success_returns_measured_output_without_model_imports(tmp_path):
    with DeviceReservation(tmp_path, cuda_profile()) as reservation:
        result = reservation.run_stage([sys.executable, "-c", "print('synthetic-only')"],
                                       snapshot_supplier=snapshot)
    assert result.stdout.strip() == b"synthetic-only"
    assert result.peak_rss_bytes > 0
    assert not reservation.quarantined
    assert not reservation.quarantine_path.exists()


def test_synthetic_runner_uses_rss_ceiling_but_does_not_claim_native_commit_enforcement(tmp_path, monkeypatch):
    from secretary.infrastructure import local_asr_resources as resources
    received = []

    def synthetic_runner(command, **kwargs):
        received.append(kwargs)
        return resources.ProcessOutput(b"synthetic-only", 0, 1)

    monkeypatch.setattr(resources, "run_owned", synthetic_runner)
    profile = cuda_profile()
    with DeviceReservation(tmp_path, profile) as reservation:
        reservation.run_stage(["synthetic-unused"], snapshot_supplier=snapshot)
    assert received[0]["rss_limit"] == 4608 * 1024**2
    assert profile.job_commit_limit_mib == 8192
    # Merely putting this ceiling in a profile does not turn the existing
    # sampled runner into a native aggregate-commit limiter.
    assert "job_commit_limit" not in received[0]


@pytest.mark.parametrize("failure", [VoiceError("process_timeout"), RuntimeError("private failure"), KeyboardInterrupt()])
def test_any_runner_failure_retains_lock_and_quarantine_even_after_caller_drops_reference(tmp_path, monkeypatch, failure):
    from secretary.infrastructure import local_asr_resources as resources

    def uncertain_runner(*args, **kwargs):
        raise failure

    monkeypatch.setattr(resources, "run_owned", uncertain_runner)
    reservation = DeviceReservation(tmp_path, cuda_profile())
    marker = reservation.quarantine_path
    reference = weakref.ref(reservation)
    with pytest.raises(type(failure)):
        with reservation:
            reservation.run_stage(["synthetic-unused"], snapshot_supplier=snapshot)
    assert marker.is_dir()
    del reservation
    gc.collect()
    assert reference() is not None, "Discarding a Python reference must not release the OS lock"
    with pytest.raises(ResourceAdmissionError, match="physical_gpu_quarantined"):
        with DeviceReservation(tmp_path, cuda_profile()):
            pytest.fail("Uncertain native state was retried")


def test_marker_is_written_before_runner_and_blocks_after_owner_process_death(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = "\n".join([
        "import os,sys",
        f"sys.path.insert(0, {str(root / 'backend')!r})",
        "from secretary.infrastructure import local_asr_resources as r",
        "r.run_owned = lambda *args, **kwargs: os._exit(0)",
        f"profile = r.ResourceProfile(asr_device='cuda', physical_gpu_id={GPU!r})",
        f"with r.DeviceReservation({str(tmp_path)!r}, profile) as reservation:",
        f"    reservation.run_stage(['synthetic-unused'], snapshot_supplier=lambda:r.ResourceSnapshot(20000,20000,{{{GPU!r}:16000}}))",
    ])
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr.decode()
    reservation = DeviceReservation(tmp_path, cuda_profile())
    assert reservation.quarantine_path.is_dir()
    # The child process and its OS handle are gone. Disk state must still deny
    # reentry, because process death is not proof of descendant containment.
    with pytest.raises(ResourceAdmissionError, match="physical_gpu_quarantined"):
        with reservation:
            pytest.fail("Process exit was mistaken for complete native cleanup")


def test_failed_marker_write_never_launches_runner_and_retains_lock(tmp_path, monkeypatch):
    from secretary.infrastructure import local_asr_resources as resources
    reservation = DeviceReservation(tmp_path, cuda_profile())
    marker = reservation.quarantine_path
    original_mkdir = Path.mkdir
    called = []

    def deny_marker(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("synthetic-only")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_marker)
    monkeypatch.setattr(resources, "run_owned", lambda *args, **kwargs: called.append(True))
    with pytest.raises(ResourceAdmissionError, match="resource_quarantine_unavailable"):
        with reservation:
            reservation.run_stage(["synthetic-unused"], snapshot_supplier=snapshot)
    assert not called
    assert reservation.quarantined
    with pytest.raises(ResourceAdmissionError, match="physical_gpu_busy"):
        with DeviceReservation(tmp_path, cuda_profile()):
            pytest.fail("Failure to persist quarantine released the OS handle")


def test_failed_marker_clear_is_quarantined_and_does_not_publish_success(tmp_path, monkeypatch):
    from secretary.infrastructure import local_asr_resources as resources
    reservation = DeviceReservation(tmp_path, cuda_profile())
    marker = reservation.quarantine_path
    original_rmdir = Path.rmdir

    def deny_clear(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("synthetic-only")
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rmdir", deny_clear)
    monkeypatch.setattr(resources, "run_owned", lambda *args, **kwargs: resources.ProcessOutput(b"ok", 0, 1))
    with pytest.raises(ResourceAdmissionError, match="resource_quarantine_unavailable"):
        with reservation:
            reservation.run_stage(["synthetic-unused"], snapshot_supplier=snapshot)
    assert reservation.quarantined
    assert marker.is_dir()
    with pytest.raises(ResourceAdmissionError, match="physical_gpu_quarantined"):
        with DeviceReservation(tmp_path, cuda_profile()):
            pytest.fail("Unable to establish safe release")
