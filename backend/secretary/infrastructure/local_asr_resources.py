"""Model-free resource admission and a physical-device reservation seam.

Every participating application must use the same provisioned ``shared_root``
and the physical GPU UUID. This module neither discovers nor controls SERVER
processes, changes providers, loads models, or allocates GPU memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable, Literal, Mapping

from secretary.infrastructure.instance_lock import DataDirectoryLock, DataDirectoryLockError
from secretary.infrastructure.voice_process import ProcessOutput, run_owned


# A failed runner supplies no exhaustive native-exit proof. Keep its OS handle
# alive even if the caller discards the reservation. The durable marker below
# continues blocking after the owning Python process exits. No automatic
# reconciliation/removal is exposed in the model-free phase.
_quarantine_guard = threading.Lock()
_quarantined_reservations: dict[int, "DeviceReservation"] = {}


class ResourceAdmissionError(RuntimeError):
    """Stable, text-free reason suitable for a waiting/blocked job."""

    def __init__(self, code: str, reasons: tuple[str, ...] = ()):
        self.code = code
        self.reasons = reasons or (code,)
        super().__init__(code)


def _integer(value: object, minimum: int, name: str) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(name)


@dataclass(frozen=True)
class ResourceProfile:
    """Guard values are profile identity, not universal model requirements.

    Nemotron's historical VRAM minimum is not supplied by the handoff. The
    CPU-ASR/GPU-Nemotron profile therefore requires an independently verified
    ``nemotron_free_vram_mib``; absence blocks admission instead of guessing.
    ``job_commit_limit_mib`` records the native aggregate commit ceiling. The
    sampled runner below cannot enforce Windows job commit or equivalent
    container containment; model dispatch remains unqualified until a native
    adapter enforces that ceiling for the complete owned tree.
    """

    asr_device: Literal["cpu", "cuda"] = "cpu"
    diarization_device: Literal["cpu", "cuda"] = "cuda"
    physical_gpu_id: str | None = None
    cpu_threads: int = 4
    ram_reserve_mib: int = 6656
    commit_reserve_mib: int = 9216
    cuda_free_vram_mib: int = 10240
    allocator_budget_mib: int = 8192
    nemotron_free_vram_mib: int | None = None
    deadline_seconds: int = 1800
    owned_rss_limit_mib: int = 4608
    job_commit_limit_mib: int = 8192

    def __post_init__(self) -> None:
        if self.asr_device not in {"cpu", "cuda"} or self.diarization_device not in {"cpu", "cuda"}:
            raise ValueError("local_asr_device_invalid")
        _integer(self.cpu_threads, 1, "local_asr_cpu_threads_invalid")
        _integer(self.ram_reserve_mib, 6656, "local_asr_ram_guard_weakened")
        _integer(self.commit_reserve_mib, 9216, "local_asr_commit_guard_weakened")
        _integer(self.cuda_free_vram_mib, 10240, "local_asr_vram_guard_weakened")
        # Allocator budget is an upper bound. Raising it would weaken the
        # historic guard, just as lowering admission reserves would.
        _integer(self.allocator_budget_mib, 1, "local_asr_allocator_budget_invalid")
        if self.allocator_budget_mib > 8192:
            raise ValueError("local_asr_allocator_guard_weakened")
        _integer(self.deadline_seconds, 1, "local_asr_deadline_invalid")
        _integer(self.owned_rss_limit_mib, 1, "local_asr_rss_limit_invalid")
        if self.owned_rss_limit_mib > 4608:
            raise ValueError("local_asr_rss_guard_weakened")
        _integer(self.job_commit_limit_mib, 1, "local_asr_job_commit_limit_invalid")
        if self.job_commit_limit_mib > 8192:
            raise ValueError("local_asr_job_commit_guard_weakened")
        if self.nemotron_free_vram_mib is not None:
            _integer(self.nemotron_free_vram_mib, 1, "local_asr_nemotron_guard_invalid")
        if self.physical_gpu_id is not None:
            # CUDA ordinals and MIG instance IDs cannot arbitrate ownership of
            # one physical GPU across applications with different visibility.
            if not isinstance(self.physical_gpu_id, str) or not re.fullmatch(
                    r"GPU-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", self.physical_gpu_id, re.IGNORECASE):
                raise ValueError("local_asr_physical_gpu_identity_required")

    @property
    def uses_gpu(self) -> bool:
        return "cuda" in (self.asr_device, self.diarization_device)

    @property
    def gpu_identity(self) -> str | None:
        return self.physical_gpu_id.casefold() if self.physical_gpu_id else None

    @property
    def required_vram_mib(self) -> int | None:
        if self.asr_device == "cuda":
            return max(self.cuda_free_vram_mib, self.nemotron_free_vram_mib or 0)
        if self.diarization_device == "cuda":
            return self.nemotron_free_vram_mib
        return None


@dataclass(frozen=True)
class ResourceSnapshot:
    free_ram_mib: float | None
    free_commit_mib: float | None
    gpu_free_mib: Mapping[str, float | None] = field(default_factory=dict)
    sampled_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reasons: tuple[str, ...]
    state: Literal["admitted", "waiting_resources"]


def _finite_metric(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def check_admission(profile: ResourceProfile, snapshot: ResourceSnapshot, *,
                    monotonic_now: float | None = None, max_age_seconds: float = 5.0) -> Admission:
    """Read-only decision. Missing/stale measurements fail closed."""
    clock = time.monotonic() if monotonic_now is None else monotonic_now
    if not _finite_metric(clock) or not _finite_metric(max_age_seconds):
        raise ValueError("local_asr_admission_clock_invalid")
    reasons: list[str] = []
    if (not _finite_metric(snapshot.sampled_at)
            or not 0 <= clock - snapshot.sampled_at <= max_age_seconds):
        reasons.append("resource_snapshot_stale")
    for metric, required, name in (
        (snapshot.free_ram_mib, profile.ram_reserve_mib, "ram"),
        (snapshot.free_commit_mib, profile.commit_reserve_mib, "commit"),
    ):
        if not _finite_metric(metric):
            reasons.append(f"{name}_measurement_unavailable")
        elif metric < required:
            reasons.append(f"insufficient_{name}")
    if profile.uses_gpu:
        if profile.gpu_identity is None:
            reasons.append("physical_gpu_identity_unverified")
        required = profile.required_vram_mib
        if required is None:
            reasons.append("nemotron_vram_guard_unverified")
        free = {str(key).casefold(): value for key, value in snapshot.gpu_free_mib.items()}.get(
            profile.gpu_identity)
        if not _finite_metric(free):
            reasons.append("vram_measurement_unavailable")
        elif required is not None and free < required:
            reasons.append("insufficient_vram")
    return Admission(not reasons, tuple(reasons), "waiting_resources" if reasons else "admitted")


class DeviceReservation:
    """Nonblocking, OS-backed reservation for a physical GPU, across profiles.

    The configured root is a shared platform resource, never inferred from the
    current application's private data directory. Releasing after an expired
    job lease is forbidden by the caller's lifecycle contract; this handle is
    held until synchronous owned-process cleanup has completed successfully.
    Any runner error quarantines the device: a timeout/cancellation exception
    does not prove that all descendants exited. Process death alone cannot
    clear the durable quarantine either.
    """

    def __init__(self, shared_root: Path | str | None, profile: ResourceProfile):
        self.profile = profile
        self.shared_root = Path(shared_root).resolve() if shared_root is not None else None
        self._lock: DataDirectoryLock | None = None
        self._active = False
        self._owner_pid: int | None = None
        self._entered_at: float | None = None
        self._stage_guard = threading.Lock()
        self._running = False
        self._poisoned = False

    @property
    def resource_key(self) -> str | None:
        if not self.profile.uses_gpu or self.profile.gpu_identity is None:
            return None
        return sha256(self.profile.gpu_identity.encode("ascii")).hexdigest()

    @property
    def quarantine_path(self) -> Path | None:
        if self.shared_root is None or self.resource_key is None:
            return None
        return self.shared_root / "physical-gpu" / self.resource_key / "native-state-unverified"

    @property
    def quarantined(self) -> bool:
        return self._poisoned

    def _check_quarantine(self) -> None:
        marker = self.quarantine_path
        try:
            # lexists also rejects a dangling symlink in place of the marker.
            present = marker is not None and os.path.lexists(marker)
        except OSError:
            raise ResourceAdmissionError("resource_quarantine_unavailable") from None
        if present:
            raise ResourceAdmissionError("physical_gpu_quarantined")

    def _poison(self) -> None:
        self._poisoned = True
        with _quarantine_guard:
            _quarantined_reservations[id(self)] = self

    def _mark_native_pending(self) -> None:
        marker = self.quarantine_path
        if marker is not None:
            try:
                # Directory creation is atomic. An empty marker is sufficient:
                # its exact parent is the canonical physical-device hash.
                marker.mkdir()
            except OSError:
                self._poison()
                raise ResourceAdmissionError("resource_quarantine_unavailable") from None

    def _clear_native_pending(self) -> None:
        marker = self.quarantine_path
        if marker is not None:
            try:
                # Never recursively remove or overwrite an unexpected marker.
                marker.rmdir()
            except OSError:
                self._poison()
                raise ResourceAdmissionError("resource_quarantine_unavailable") from None

    def __enter__(self) -> DeviceReservation:
        if self._poisoned:
            raise ResourceAdmissionError("physical_gpu_quarantined")
        if self._active:
            raise ResourceAdmissionError("resource_reservation_already_owned")
        if self.profile.uses_gpu:
            if self.shared_root is None:
                raise ResourceAdmissionError("shared_resource_root_unconfigured")
            if self.resource_key is None:
                raise ResourceAdmissionError("physical_gpu_identity_unverified")
            self._check_quarantine()
            try:
                self._lock = DataDirectoryLock(self.shared_root / "physical-gpu" / self.resource_key)
            except DataDirectoryLockError as exc:
                code = "physical_gpu_busy" if exc.code == "data_directory_in_use" else "resource_lock_unavailable"
                raise ResourceAdmissionError(code) from None
            try:
                # A prior holder may have quarantined between our first check
                # and acquiring its released process-level lock.
                self._check_quarantine()
            except BaseException:
                self._lock.release()
                self._lock = None
                raise
        self._active = True
        self._owner_pid = os.getpid()
        self._entered_at = time.monotonic()
        return self

    def _owned(self, *, allow_quarantined: bool = False) -> None:
        if not self._active or self._owner_pid != os.getpid():
            raise ResourceAdmissionError("resource_reservation_not_owned")
        if self._poisoned and not allow_quarantined:
            raise ResourceAdmissionError("physical_gpu_quarantined")

    def check_stage(self, snapshot_supplier: Callable[[], ResourceSnapshot]) -> Admission:
        self._owned()
        if time.monotonic() - self._entered_at >= self.profile.deadline_seconds:
            raise ResourceAdmissionError("local_asr_deadline_exceeded")
        try:
            snapshot = snapshot_supplier()
        except Exception:
            raise ResourceAdmissionError("resource_measurement_failed") from None
        if not isinstance(snapshot, ResourceSnapshot):
            raise ResourceAdmissionError("resource_measurement_failed")
        decision = check_admission(self.profile, snapshot)
        if not decision.allowed:
            raise ResourceAdmissionError("resource_admission_denied", decision.reasons)
        return decision

    def run_stage(self, command: list[str], *, snapshot_supplier: Callable[[], ResourceSnapshot],
                  input_bytes: bytes = b"", stdout_limit: int = 1024 * 1024,
                  stderr_limit: int = 65536, cancel: threading.Event | None = None,
                  env: dict | None = None) -> ProcessOutput:
        """Run only after the caller's separate authorization/qualification gate.

        This is a synchronous owned-process adapter, not an inference launcher.
        No detached process handle or HTTP cancellation can release its slot
        while ``run_owned`` is still draining/cleaning up its process tree.
        The existing runner samples owned-tree RSS only. The profile's native
        job commit ceiling is NOT enforced here; native containment is a
        separate prerequisite for future model dispatch, not proven by these
        synthetic process checks.
        """
        with self._stage_guard:
            self._owned()
            if self._running:
                raise ResourceAdmissionError("resource_stage_already_running")
            self._running = True
        try:
            self.check_stage(snapshot_supplier)
            remaining = self.profile.deadline_seconds - (time.monotonic() - self._entered_at)
            if remaining <= 0:
                raise ResourceAdmissionError("local_asr_deadline_exceeded")
            self._mark_native_pending()
            try:
                result = run_owned(command, input_bytes=input_bytes, stdout_limit=stdout_limit,
                                   stderr_limit=stderr_limit, timeout=remaining,
                                   rss_limit=self.profile.owned_rss_limit_mib * 1024**2,
                                   cancel=cancel, env=env)
            except BaseException:
                # run_owned reports the first error; a subsequent cleanup
                # failure can be masked by timeout/cancel. Never infer safe
                # release from the error's reason or from elapsed lease time.
                self._poison()
                raise
            self._clear_native_pending()
            return result
        finally:
            with self._stage_guard:
                self._running = False

    def run_native_stage(self, command, *, snapshot_supplier, **kwargs):
        """Native containment adapter: release only after typed exhaustive proof.

        Unlike the historical sampled runner, an expected cancellation with
        confirmed native cleanup may release the device. Unknown cleanup keeps
        the durable quarantine and OS lock, including after caller disposal.
        """
        from secretary.infrastructure.local_asr_process import (
            run_owned_stage, StageProcessReceipt, StageProcessStopped,
        )
        with self._stage_guard:
            self._owned()
            if self._running:
                raise ResourceAdmissionError("resource_stage_already_running")
            self._running = True
        marked = False
        try:
            self.check_stage(snapshot_supplier)
            self._mark_native_pending()
            marked = True
            try:
                receipt = run_owned_stage(command, **kwargs)
            except BaseException as exc:
                if isinstance(exc, StageProcessStopped) and exc.cleanup_confirmed is True:
                    self._clear_native_pending()
                    marked = False
                else:
                    self._poison()
                raise
            if not isinstance(receipt, StageProcessReceipt) or receipt.cleanup_confirmed is not True:
                self._poison()
                raise ResourceAdmissionError("native_cleanup_unverified")
            self._clear_native_pending()
            marked = False
            return receipt
        finally:
            if marked:
                self._poison()
            with self._stage_guard:
                self._running = False

    def __exit__(self, exc_type, exc, traceback) -> None:
        with self._stage_guard:
            self._owned(allow_quarantined=True)
            if self._running:
                raise ResourceAdmissionError("resource_stage_still_running")
            if self._poisoned:
                # Preserve the original runner exception and retain the lock.
                # Operator reconciliation must independently establish that
                # the complete old tree is gone; there is no bool escape hatch.
                return None
            if self._lock is not None:
                self._lock.release()
                self._lock = None
            self._active = False
            self._owner_pid = None
            self._entered_at = None
