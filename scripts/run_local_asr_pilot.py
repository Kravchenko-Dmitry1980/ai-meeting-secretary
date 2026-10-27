"""One explicit local bootstrap experiment; never a production qualification.

The CLI accepts a pinned operator-authored authority file, not HTTP/queue input.
It cannot enable a runtime, publish a meeting, retry, or call a cloud provider.
Only the existing native controller and stages execute under Windows Job limits.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import Literal
from uuid import uuid4
import wave

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from pydantic import Field, model_validator
from secretary.application.local_asr_execution_protocol import ExecutionContext
from secretary.application.local_asr_executor import (
    CONTROLLER, LocalAsrExecutor, _base_environment, _copy_checked, _safe_file,
)
from secretary.application.local_asr_lifecycle import LocalExecutionError
from secretary.application.local_asr_package_adapter import PackageAdapterError
from secretary.application.local_asr_media import lock_verified_input_copy
from secretary.domain.speakers import DTO
from secretary.domain.local_asr import Digest
from secretary.infrastructure.local_asr_process import StageProcessStopped
from secretary.infrastructure.local_asr_resource_probe import sample_resources
from secretary.infrastructure.local_asr_resources import DeviceReservation, ResourceProfile, ResourceSnapshot
from secretary.infrastructure.local_asr_runtime import (
    PinnedRuntimeFile, PortableLocalAsrRuntime, PortableRuntimeProfile,
    _json, _plain, runtime_file_digest,
)
from secretary.infrastructure.local_asr_source import _relative

MIB = 1024 ** 2
INPUT_LIMIT = 2 * MIB
ENV_FILE_LIMIT = 4 * 1024 ** 3
PILOT_VRAM_MIB = 7168
STAGES = ("normalize", "diarize", "asr_rnnt", "align_rnnt", "assemble")


def require(condition, code):
    if not condition:
        raise LocalExecutionError(code)


def write_evidence(path, value):
    """Immutable, bounded JSON receipts: no replacement or partial publication."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
    require(len(encoded) <= 16 * MIB, "local_asr_pilot_evidence_too_large")
    with path.open("xb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())


class PilotAuthority(DTO):
    schema_version: Literal["secretary.local-asr.bootstrap-authority.v1"]
    owner_authorization_id: str = Field(min_length=1, max_length=160)
    purpose: Literal["first_bounded_hardware_pilot"]
    profile: PortableRuntimeProfile
    input: PinnedRuntimeFile
    input_provenance: PinnedRuntimeFile
    launcher_python: PinnedRuntimeFile
    pilot_runner_sha256: Digest
    environment_inventories: dict[Literal["launcher", "asr-cpu", "nemotron"], PinnedRuntimeFile]
    max_input_seconds: Literal[30] = 30
    max_attempts: Literal[1] = 1
    deadline_seconds: int = Field(default=1800, ge=1, le=1800, strict=True)
    stage_timeout_seconds: int = Field(default=600, ge=1, le=600, strict=True)
    production_activation: Literal[False] = False

    @model_validator(mode="after")
    def isolated(self):
        require(not self.profile.enabled and self.profile.qualification_file is None,
                "local_asr_pilot_production_profile_forbidden")
        require(self.profile.nemotron_free_vram_mib == PILOT_VRAM_MIB,
                "local_asr_pilot_vram_scope_invalid")
        require(self.profile.shared_resource_root is not None,
                "local_asr_pilot_shared_root_required")
        require(set(self.environment_inventories) == {"launcher", "asr-cpu", "nemotron"},
                "local_asr_pilot_environment_inventory_missing")
        require(self.profile.asr_python.path != self.profile.nemotron_python.path,
                "local_asr_pilot_separate_environments_required")
        return self


def check_pin(pin, *, max_bytes=512 * MIB):
    require(runtime_file_digest(pin.path, max_bytes=max_bytes) == pin.sha256,
            "local_asr_pilot_pin_mismatch")


def inspect_input(pin):
    check_pin(pin, max_bytes=INPUT_LIMIT)
    try:
        with wave.open(str(pin.path), "rb") as audio:
            require((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype())
                    == (1, 2, 16000, "NONE"), "local_asr_pilot_input_format_invalid")
            frames = audio.getnframes()
            require(0 < frames <= 30 * 16000, "local_asr_pilot_input_duration_invalid")
            require(len(audio.readframes(frames)) == frames * 2, "local_asr_pilot_input_truncated")
        return round(frames * 1000 / 16000)
    except (OSError, EOFError, wave.Error):
        raise LocalExecutionError("local_asr_pilot_input_invalid") from None


def verify_file_closure(root, rows):
    """Include native extensions, .pth, bytecode, and the complete base stdlib."""
    _plain(root, directory=True)
    require(isinstance(rows, list) and 0 < len(rows) <= 100000, "local_asr_pilot_environment_files_invalid")
    expected = {}
    folded = set()
    for row in rows:
        require(isinstance(row, dict), "local_asr_pilot_environment_files_invalid")
        relative = _relative(row.get("relative"))
        require(relative.casefold() not in folded,
                "local_asr_pilot_environment_duplicate")
        folded.add(relative.casefold())
        size = row.get("size_bytes")
        require(type(size) is int and 0 <= size <= ENV_FILE_LIMIT,
                "local_asr_pilot_environment_file_size")
        require(isinstance(row.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", row["sha256"]),
                "local_asr_pilot_environment_digest_invalid")
        expected[relative] = row
    # Do not use rglob/resolve: junctions and additional .pth/.pyc are rejected.
    observed = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        _plain(directory, directory=True)
        for path in directory.iterdir():
            if path.is_dir():
                _plain(path, directory=True)
                pending.append(path)
            else:
                _plain(path)
                observed.add(path.relative_to(root).as_posix())
            require(len(observed) + len(pending) <= 100000, "local_asr_pilot_environment_too_large")
    require(observed == set(expected), "local_asr_pilot_environment_file_set_mismatch")
    for relative, row in expected.items():
        path = root / relative
        require(_plain(path).st_size == row["size_bytes"]
                and runtime_file_digest(path, max_bytes=ENV_FILE_LIMIT) == row["sha256"],
                "local_asr_pilot_environment_file_mismatch")
    return len(expected)


def inventory_pin(value):
    require(isinstance(value, dict), "local_asr_pilot_environment_base_pin_missing")
    result = PinnedRuntimeFile(path=value.get("path"), sha256=value.get("sha256"))
    if "size_bytes" in value:
        require(_plain(result.path).st_size == value["size_bytes"], "local_asr_pilot_environment_file_size")
    return result


def verify_pyvenv_home(root, files, base_root, base_interpreter):
    """Bind a captured venv home alias to its exact pinned plain Python root.

    Existing launcher configs may name uv's minor-version junction. Reading
    that alias is allowed only while its strict target remains the captured
    base runtime; no environment config or junction is changed here.
    """
    require(isinstance(files, list), "local_asr_pilot_environment_files_invalid")
    matches = [row for row in files if isinstance(row, dict) and row.get("relative") == "pyvenv.cfg"]
    require(len(matches) == 1, "local_asr_pilot_pyvenv_pin_missing")
    expected, config = matches[0], root / "pyvenv.cfg"
    before = _plain(config)
    require(0 < before.st_size <= 65536, "local_asr_pilot_pyvenv_invalid")
    with config.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(65537)
        finished = os.fstat(stream.fileno())
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)
    require(signature(before) == signature(opened) == signature(finished) == signature(_plain(config))
            and len(raw) == expected.get("size_bytes")
            and hashlib.sha256(raw).hexdigest() == expected.get("sha256"),
            "local_asr_pilot_pyvenv_pin_mismatch")
    try:
        homes = [value.strip() for line in raw.decode("utf-8-sig").splitlines()
                 for key, separator, value in (line.partition("="),)
                 if separator and key.strip().casefold() == "home"]
        require(len(homes) == 1, "local_asr_pilot_pyvenv_home_invalid")
        home = Path(homes[0])
        require(home.is_absolute() and ".." not in home.parts and not str(home).startswith(("\\\\", "//")),
                "local_asr_pilot_pyvenv_home_invalid")
        _plain(base_root, directory=True)
        require(base_root.resolve(strict=True) == base_root and home.resolve(strict=True) == base_root,
                "local_asr_pilot_pyvenv_home_mismatch")
        require((home / "python.exe").resolve(strict=True) == base_interpreter.path,
                "local_asr_pilot_pyvenv_interpreter_mismatch")
    except (OSError, UnicodeError, RuntimeError):
        raise LocalExecutionError("local_asr_pilot_pyvenv_home_invalid") from None
    return dict(config_sha256=expected["sha256"], configured_home=str(home), resolved_home=str(base_root),
                base_interpreter=str(base_interpreter.path))


def verify_active_launcher(interpreter, base_root, base_interpreter):
    """Check this running launcher without starting another interpreter."""
    if interpreter.path != Path(sys.executable).absolute():
        return False
    try:
        require(Path(sys._base_executable).resolve(strict=True) == base_interpreter.path
                and Path(sys.base_prefix).resolve(strict=True) == base_root,
                "local_asr_pilot_active_base_mismatch")
    except (OSError, RuntimeError, TypeError, AttributeError):
        raise LocalExecutionError("local_asr_pilot_active_base_unavailable") from None
    return True


def verify_environment(pin, interpreter):
    """Full immutable environment file closure, including executable bytecode."""
    document = _json(pin.path, expected_sha256=pin.sha256)
    require(document.get("schema_version") == 1, "local_asr_pilot_environment_schema_invalid")
    root = Path(document.get("root", ""))
    require(inventory_pin(document.get("interpreter")) == interpreter and interpreter.path.is_relative_to(root),
            "local_asr_pilot_environment_interpreter_mismatch")
    for key in ("base_interpreter", "requirements_lock"):
        check_pin(inventory_pin(document.get(key)))
    base = document.get("base_runtime")
    require(isinstance(base, dict), "local_asr_pilot_base_runtime_missing")
    base_root = Path(base.get("root", ""))
    base_interpreter = inventory_pin(document.get("base_interpreter"))
    require(base_interpreter.path.is_relative_to(base_root),
            "local_asr_pilot_base_interpreter_mismatch")
    mapping = verify_pyvenv_home(root, document.get("files"), base_root, base_interpreter)
    active = verify_active_launcher(interpreter, base_root, base_interpreter)
    count = verify_file_closure(root, document.get("files"))
    base_count = verify_file_closure(base_root, base.get("files"))
    require(verify_pyvenv_home(root, document.get("files"), base_root, base_interpreter) == mapping,
            "local_asr_pilot_pyvenv_home_changed")
    require(verify_active_launcher(interpreter, base_root, base_interpreter) is active,
            "local_asr_pilot_active_base_changed")
    return dict(path=str(pin.path), sha256=pin.sha256, root=str(root), verified_file_count=count,
                base_root=str(base_root), verified_base_file_count=base_count, pyvenv_home=mapping,
                active_launcher_base_verified=active)


def verify_authority(authority):
    require(runtime_file_digest(Path(__file__).absolute()) == authority.pilot_runner_sha256,
            "local_asr_pilot_runner_mismatch")
    require(Path(sys.executable).absolute() == authority.launcher_python.path,
            "local_asr_pilot_launcher_mismatch")
    require(not os.environ.get("SECRETARY_LOCAL_ASR_PROFILE"), "local_asr_pilot_production_environment_present")
    check_pin(authority.launcher_python)
    check_pin(authority.input_provenance)
    duration_ms = inspect_input(authority.input)
    interpreters = {"launcher": authority.launcher_python, "asr-cpu": authority.profile.asr_python,
                    "nemotron": authority.profile.nemotron_python}
    inventories = {role: verify_environment(pin, interpreters[role])
                   for role, pin in authority.environment_inventories.items()}
    runtime = PortableLocalAsrRuntime(authority.profile)
    plan = runtime.plan_factory("bootstrap-pilot", 1, authority.input.sha256, None)
    inventory = runtime.verify_inventory(plan)
    return runtime, plan, duration_ms, dict(runtime=inventory, environments=inventories)


def bounded_specs(runtime, plan, context, workspace, timeout):
    specs = tuple(runtime.build_attempt(plan, context, workspace))
    LocalAsrExecutor._specs(plan, specs)
    require(tuple(spec.stage for spec in specs) == STAGES, "local_asr_pilot_stage_scope_invalid")
    bounded = []
    for spec in specs:
        command = list(spec.command)
        if spec.stage != "assemble":
            require(command.count("--max-duration-seconds") == 1, "local_asr_pilot_duration_guard_missing")
            # The immutable donor accepts only 1800/9000/14400. The tighter
            # pilot bound is enforced on the locked input and normalized WAV.
            command[command.index("--max-duration-seconds") + 1] = "1800"
        bounded.append(replace(spec, command=tuple(command), timeout_seconds=timeout))
    return tuple(bounded)


def diarization_artifact_receipt(path):
    """Canonical donor turns are [[start_seconds, end_seconds, label], ...].

    This list-only reader is restricted to the diarize stage artifact. It hashes
    the same bounded stable bytes that it parses; authority JSON stays dict-only.
    """
    before = _plain(path)
    limit = 8 * MIB
    require(before.st_size <= limit, "local_asr_pilot_diarization_size_invalid")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(limit + 1)
        finished = os.fstat(stream.fileno())
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)
    require(signature(before) == signature(opened) == signature(finished) == signature(_plain(path))
            and len(raw) == before.st_size, "local_asr_pilot_diarization_changed")
    try:
        turns = json.loads(raw.decode("utf-8"),
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise LocalExecutionError("local_asr_pilot_diarization_json_invalid") from None
    require(isinstance(turns, list) and 0 < len(turns) <= 500000,
            "local_asr_pilot_diarization_shape_invalid")
    for turn in turns:
        require(isinstance(turn, list) and len(turn) == 3
                and all(type(value) in (int, float) and math.isfinite(value) for value in turn[:2])
                and 0 <= turn[0] < turn[1] <= 30
                and isinstance(turn[2], str) and 0 < len(turn[2]) <= 200,
                "local_asr_pilot_diarization_turn_invalid")
    return dict(relative=path.name, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))


class ResourceWatchdog:
    """Fresh admission and separate low-water stop bounds during owned work."""
    def __init__(self, supplier, gpu_id):
        self.supplier, self.gpu_id = supplier, gpu_id.casefold()
        self.count, self.last_poll = 0, float("-inf")
        self.minimum = {"free_ram_mib": None, "free_commit_mib": None, "free_vram_mib": None}

    def sample(self, *, running=False):
        value = self.supplier()
        require(isinstance(value, ResourceSnapshot) and 0 <= time.monotonic() - value.sampled_at <= 5,
                "local_asr_pilot_resource_snapshot_invalid")
        metrics = dict(free_ram_mib=value.free_ram_mib, free_commit_mib=value.free_commit_mib,
                       free_vram_mib={k.casefold(): v for k, v in value.gpu_free_mib.items()}.get(self.gpu_id))
        require(all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in metrics.values()),
                "local_asr_pilot_resource_measurement_unavailable")
        self.count += 1
        for name, metric in metrics.items():
            previous = self.minimum[name]
            self.minimum[name] = metric if previous is None else min(previous, metric)
        if running:
            require(metrics["free_ram_mib"] >= 2048 and metrics["free_commit_mib"] >= 1024,
                    "local_asr_pilot_host_low_water")
        return value

    def poll(self):
        if time.monotonic() - self.last_poll >= .5:
            self.sample(running=True)
            self.last_poll = time.monotonic()

    def receipt(self):
        return dict(sample_count=self.count, minima_mib=self.minimum,
                    admission_ram_mib=6656, admission_commit_mib=9216,
                    pilot_admission_vram_mib=PILOT_VRAM_MIB,
                    running_ram_floor_mib=2048, running_commit_floor_mib=1024,
                    vram_guard_qualified_for_production=False)


def supervise_once(authority, plan, specs, workspace, *, cancelled, snapshot_supplier):
    """One controller dispatch, held reservation, sequential one-use permits."""
    profile = ResourceProfile(physical_gpu_id=plan.physical_gpu_id,
        nemotron_free_vram_mib=PILOT_VRAM_MIB, deadline_seconds=authority.deadline_seconds)
    watchdog = ResourceWatchdog(snapshot_supplier, plan.physical_gpu_id)
    token, base = uuid4().hex, _base_environment(workspace)
    base["LIBROSA_CACHE_DIR"] = str(workspace / "_runtime/cache/librosa")
    (workspace / "control").mkdir()
    def environment(spec):
        result = base | dict(spec.environment)
        result["PYTHONPATH"] = base["PYTHONPATH"] + "".join(os.pathsep + str(p) for p in spec.python_paths)
        return result
    request = dict(attempt_token=token, deadline_seconds=authority.deadline_seconds,
        job_commit_limit_bytes=profile.job_commit_limit_mib * MIB,
        working_set_limit_bytes=profile.owned_rss_limit_mib * MIB,
        owned_rss_limit_bytes=profile.owned_rss_limit_mib * MIB,
        retain_stage_diagnostics=True,
        stages=[dict(stage=s.stage, command=list(s.command), environment=environment(s),
                     timeout_seconds=s.timeout_seconds, skip=False) for s in specs])
    request_path = workspace / "controller-request.json"
    write_evidence(request_path, request)
    completed, permits, stage_receipts = set(), set(), []
    identity, callback_errors = {}, []
    native_result = None
    started = time.monotonic()
    failure = None
    try:
        with DeviceReservation(authority.profile.shared_resource_root, profile) as reservation:
            reservation.check_stage(watchdog.sample)
            def on_started(process):
                import psutil
                identity.update(pid=process.pid, created_at=psutil.Process(process.pid).create_time())
                write_evidence(workspace / "controller-identity.json", identity)

            def on_poll(process):
                try:
                    watchdog.poll()
                    event_path = workspace / "control/event.json"
                    if not event_path.exists():
                        return
                    event = _json(event_path)
                    require(event.get("attempt_token") == token, "local_asr_pilot_event_token_mismatch")
                    state = event.get("state")
                    if state == "finished":
                        require(len(completed) == len(specs), "local_asr_pilot_completion_missing")
                        return
                    index = event.get("index")
                    require(type(index) is int and 0 <= index < len(specs), "local_asr_pilot_event_index_invalid")
                    spec = specs[index]
                    require(event.get("stage") == spec.stage and all(i in completed for i in range(index)),
                            "local_asr_pilot_event_order_invalid")
                    if state == "awaiting_admission" and index not in permits:
                        require(not cancelled(), "local_asr_cancelled")
                        reservation.check_stage(watchdog.sample)
                        write_evidence(workspace / "control" / f"permit-{index}.json",
                                       dict(attempt_token=token, index=index, stage=spec.stage))
                        permits.add(index)
                    elif state == "completed" and index not in completed:
                        require(index in permits and event.get("cleanup_confirmed") is True,
                                "local_asr_pilot_stage_cleanup_unverified")
                        require(all(type(event.get(key)) is int and event[key] >= 0
                                    for key in ("elapsed_ms", "peak_owned_rss_bytes")),
                                "local_asr_pilot_measurement_invalid")
                        outputs = []
                        for name in spec.outputs + spec.binary_outputs:
                            path = _safe_file(workspace, name, binary=name in spec.binary_outputs)
                            if spec.stage == "diarize" and name == "diarize.json":
                                outputs.append(diarization_artifact_receipt(path))
                            else:
                                if name in spec.outputs:
                                    _json(path)
                                outputs.append(dict(relative=name, sha256=runtime_file_digest(path), size_bytes=path.stat().st_size))
                            if name == "normalized.wav":
                                inspect_input(PinnedRuntimeFile(path=path, sha256=outputs[-1]["sha256"]))
                        stage_receipts.append(dict(stage=spec.stage, elapsed_ms=event["elapsed_ms"],
                            peak_owned_rss_bytes=event["peak_owned_rss_bytes"], cleanup_confirmed=True, outputs=outputs))
                        completed.add(index)
                        write_evidence(workspace / "control" / f"checkpoint-{index}.json",
                                       dict(attempt_token=token, index=index, stage=spec.stage))
                    elif state == "failed":
                        code = event.get("code", "")
                        require(bool(re.fullmatch(r"[a-z0-9_]{1,100}", code)), "local_asr_pilot_failure_code_invalid")
                        raise LocalExecutionError(code)
                    elif state not in {"awaiting_admission", "completed"}:
                        raise LocalExecutionError("local_asr_pilot_event_invalid")
                except Exception as error:
                    callback_errors.append(error)
                    raise
            try:
                native_result = reservation.run_native_stage(
                    [str(authority.launcher_python.path), "-B", str(CONTROLLER), "--request", str(request_path)],
                    snapshot_supplier=watchdog.sample, environment=base, cwd=str(workspace),
                    timeout_seconds=authority.deadline_seconds, stdout_limit=65536, stderr_limit=65536,
                    job_commit_limit_bytes=profile.job_commit_limit_mib * MIB,
                    working_set_limit_bytes=profile.owned_rss_limit_mib * MIB,
                    owned_rss_limit_bytes=profile.owned_rss_limit_mib * MIB,
                    on_started=on_started, on_poll=on_poll, cancel_requested=cancelled)
            except StageProcessStopped as error:
                native_result = error
                raise callback_errors[-1] if callback_errors else error
            require(native_result.returncode == 0 and len(completed) == len(specs), "local_asr_pilot_stage_nonzero")
            require(not cancelled(), "local_asr_cancelled")
    except BaseException as error:
        failure = error
        raise
    finally:
        code = getattr(failure, "code", "local_asr_pilot_failed") if failure else None
        write_evidence(workspace / "native-receipt.json", dict(
            schema_version="secretary.local-asr.bootstrap-native-receipt.v1",
            controller=identity, attempt_count=1 if identity else 0, max_attempts=1,
            runtime_template_max_attempts=plan.max_attempts,
            cleanup_confirmed=getattr(native_result, "cleanup_confirmed", False),
            returncode=getattr(native_result, "returncode", getattr(native_result, "exit_code", None)),
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            peak_owned_rss_bytes=getattr(native_result, "peak_owned_rss_bytes", None),
            job_commit_limit_bytes=profile.job_commit_limit_mib * MIB,
            working_set_limit_bytes=profile.owned_rss_limit_mib * MIB,
            completed_stages=stage_receipts, resources=watchdog.receipt(), failure_code=code,
            stage_diagnostics=[dict(relative=path.relative_to(workspace).as_posix(),
                                    sha256=runtime_file_digest(path))
                               for path in sorted((workspace / "control").glob("stage-*-diagnostic.json"))]))


def run_pilot(authority, workspace, *, authority_pin, cancelled=lambda: False):
    # Fixed local scope; no supplied command, output path in data/, or live DB.
    workspace = Path(workspace).absolute()
    allowed = ROOT / ".runtime"
    require(workspace.is_relative_to(allowed) and workspace != allowed and not workspace.exists(),
            "local_asr_pilot_workspace_invalid")
    _plain(workspace.parent, directory=True)
    require(not workspace.is_relative_to(authority.profile.source_root), "local_asr_pilot_workspace_invalid")
    workspace.mkdir()
    write_evidence(workspace / "authority-pin.json", authority_pin.model_dump(mode="json"))
    write_evidence(workspace / "bootstrap-authority.json", authority.model_dump(mode="json"))
    try:
        runtime, plan, duration_ms, inventory = verify_authority(authority)
        require(not cancelled(), "local_asr_cancelled")
        write_evidence(workspace / "inventory-before.json", inventory)
        write_evidence(workspace / "runtime-template-plan.json", plan.model_dump(mode="json"))
        copied = workspace / "input.wav"
        with ExitStack() as stack:
            stack.enter_context(lock_verified_input_copy(authority.input.path,
                expected_sha256=authority.input.sha256, max_bytes=INPUT_LIMIT))
            _copy_checked(authority.input.path, copied, limit=INPUT_LIMIT, expected=authority.input.sha256)
            stack.enter_context(lock_verified_input_copy(copied,
                expected_sha256=authority.input.sha256, max_bytes=INPUT_LIMIT))
            context = ExecutionContext("bootstrap-pilot", 1, copied, authority.input.sha256, (),
                                       "bootstrap-" + uuid4().hex, None, duration_ms)
            specs = bounded_specs(runtime, plan, context, workspace, authority.stage_timeout_seconds)
            supervise_once(authority, plan, specs, workspace, cancelled=cancelled,
                           snapshot_supplier=lambda: sample_resources(plan.physical_gpu_id))
            require(not cancelled(), "local_asr_cancelled")
            # Capture post-inference integrity even when the separate adapter
            # gate still requires tokenizer evidence from this first run.
            _, _, _, after = verify_authority(authority)
            write_evidence(workspace / "inventory-after.json", after)
            try:
                command = runtime.read_result(plan, context, workspace)
            except PackageAdapterError as error:
                if str(error) != "package_tokenizer_not_verified":
                    raise
                write_evidence(workspace / "pilot-outcome.json", dict(
                    schema_version="secretary.local-asr.bootstrap-outcome.v1",
                    status="inference_succeeded_adapter_blocked", failure_code=str(error),
                    owner_authorization_id=authority.owner_authorization_id,
                    input_sha256=authority.input.sha256, duration_ms=duration_ms,
                    attempt_count=1, production_qualified=False,
                    quality_evaluation="not_performed", application_publication=False))
                return 2
            require(command.artifacts.duration_ms == duration_ms, "local_asr_pilot_result_duration_mismatch")
            write_evidence(workspace / "validated-import.json", command.model_dump(mode="json"))
        write_evidence(workspace / "pilot-outcome.json", dict(
            schema_version="secretary.local-asr.bootstrap-outcome.v1", status="bounded_pilot_succeeded",
            owner_authorization_id=authority.owner_authorization_id, input_sha256=authority.input.sha256,
            duration_ms=duration_ms, attempt_count=1, production_qualified=False,
            quality_evaluation="not_performed", application_publication=False))
        return 0
    except BaseException as error:
        code = getattr(error, "code", "local_asr_pilot_failed")
        write_evidence(workspace / "pilot-outcome.json", dict(
            schema_version="secretary.local-asr.bootstrap-outcome.v1", status="pilot_failed",
            failure_code=code, production_qualified=False, application_publication=False))
        raise


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority", required=True, type=Path)
    parser.add_argument("--authority-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(arguments)
    stopped = [False]
    def request_stop(signum, frame):
        stopped[0] = True
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)
    try:
        pin = PinnedRuntimeFile(path=args.authority, sha256=args.authority_sha256)
        authority = PilotAuthority.model_validate(_json(pin.path, expected_sha256=pin.sha256))
        return run_pilot(authority, args.output, authority_pin=pin, cancelled=lambda: stopped[0])
    except Exception as error:
        code = getattr(error, "code", "local_asr_pilot_failed")
        print(code if re.fullmatch(r"[a-z0-9_]+", str(code)) else "local_asr_pilot_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
