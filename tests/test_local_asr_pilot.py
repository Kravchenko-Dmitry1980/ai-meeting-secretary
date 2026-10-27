"""Bootstrap authority and actual native synthetic controller; no models."""
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import wave

import pytest

from secretary.application.local_asr_execution_protocol import StageSpec
from secretary.application.local_asr_lifecycle import LocalExecutionError
from secretary.infrastructure.local_asr_resources import ResourceSnapshot
from secretary.infrastructure.local_asr_runtime import PinnedRuntimeFile, runtime_file_digest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pilot_under_test", ROOT / "scripts/run_local_asr_pilot.py")
pilot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pilot
spec.loader.exec_module(pilot)
controller_spec = importlib.util.spec_from_file_location("pilot_controller", ROOT / "scripts/local_asr_controller.py")
controller = importlib.util.module_from_spec(controller_spec)
controller_spec.loader.exec_module(controller)


def pin(path):
    return PinnedRuntimeFile(path=path, sha256=runtime_file_digest(path))


def wav(path, seconds=1):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * int(seconds * 16000))
    return pin(path)


def test_input_exact_hash_and_thirty_second_boundary(tmp_path):
    media = wav(tmp_path / "input.wav", 30)
    assert pilot.inspect_input(media) == 30000
    with pytest.raises(LocalExecutionError, match="duration_invalid"):
        pilot.inspect_input(wav(tmp_path / "long.wav", 30.01))
    media.path.write_bytes(b"modified")
    with pytest.raises(LocalExecutionError, match="pin_mismatch"):
        pilot.inspect_input(media)


def test_pinned_json_hashes_the_same_bytes_that_are_parsed(tmp_path):
    path = tmp_path / "authority.json"
    path.write_text('{"authority":"reviewed"}')
    original = pin(path)
    path.write_text('{"authority":"replaced"}')
    with pytest.raises(LocalExecutionError, match="runtime_json_invalid"):
        pilot._json(path, expected_sha256=original.sha256)


def environment(tmp_path):
    root = tmp_path / "environment"
    root.mkdir()
    interpreter = root / "python.exe"
    interpreter.write_bytes(b"synthetic-only")
    code = root / "package.py"
    code.write_text("value = 1\n", "utf-8")
    base_root = tmp_path / "base-python"
    base_root.mkdir()
    base_python = base_root / "python.exe"
    base_python.write_bytes(b"synthetic base executable")
    config = root / "pyvenv.cfg"
    config.write_text("home = " + str(base_root) + "\ninclude-system-site-packages = false\n", "utf-8")
    lock = tmp_path / "requirements.txt"
    lock.write_text("synthetic-package==1\n", "utf-8")
    document = dict(schema_version=1, root=str(root), interpreter=pin(interpreter).model_dump(mode="json"),
        base_interpreter=pin(base_python).model_dump(mode="json"), requirements_lock=pin(lock).model_dump(mode="json"),
        files=[dict(relative=p.name, size_bytes=p.stat().st_size, sha256=runtime_file_digest(p))
               for p in (interpreter, code, config)])
    document["base_runtime"] = dict(root=str(base_root), files=[dict(relative="python.exe",
        size_bytes=base_python.stat().st_size, sha256=runtime_file_digest(base_python))])
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(document), "utf-8")
    return pin(path), pin(interpreter), root


@pytest.mark.parametrize("mutation,reason", [("content", "file_mismatch"), ("pyc", "file_set_mismatch"),
                                             ("pth", "file_set_mismatch")])
def test_full_environment_hash_closure_rejects_code_changes(tmp_path, mutation, reason):
    inventory, interpreter, root = environment(tmp_path)
    assert pilot.verify_environment(inventory, interpreter)["verified_file_count"] == 3
    if mutation == "content":
        (root / "package.py").write_text("value = 2\n", "utf-8")
    else:
        (root / ("unexpected." + mutation)).write_bytes(b"unreviewed executable path")
    with pytest.raises(LocalExecutionError, match=reason):
        pilot.verify_environment(inventory, interpreter)


@pytest.mark.parametrize("change", ["retarget", "duplicate", "relative"])
def test_captured_pyvenv_home_must_select_pinned_base(tmp_path, change):
    inventory, interpreter, root = environment(tmp_path)
    document = json.loads(inventory.path.read_text())
    alternate = tmp_path / "different-base"
    alternate.mkdir()
    (alternate / "python.exe").write_bytes(b"foreign interpreter")
    config = root / "pyvenv.cfg"
    if change == "retarget":
        config.write_text("home = " + str(alternate) + "\n")
    elif change == "duplicate":
        config.write_text(config.read_text() + "HOME = " + str(alternate) + "\n")
    else:
        config.write_text("home = relative-python\n")
    # Even a freshly captured config hash cannot redirect the pinned base.
    row = next(row for row in document["files"] if row["relative"] == "pyvenv.cfg")
    row.update(size_bytes=config.stat().st_size, sha256=runtime_file_digest(config))
    inventory.path.write_text(json.dumps(document))
    with pytest.raises(LocalExecutionError, match="pyvenv_home_(mismatch|invalid)"):
        pilot.verify_environment(pin(inventory.path), interpreter)


def test_home_alias_retarget_during_closure_is_rejected(tmp_path, monkeypatch):
    inventory, interpreter, root = environment(tmp_path)
    document = json.loads(inventory.path.read_text())
    base_root = Path(document["base_runtime"]["root"])
    alias = tmp_path / "python-minor-alias"
    alternate = tmp_path / "different-base"
    alternate.mkdir()
    (alternate / "python.exe").write_bytes(b"foreign interpreter")
    config = root / "pyvenv.cfg"
    config.write_text("home = " + str(alias) + "\n")
    row = next(row for row in document["files"] if row["relative"] == "pyvenv.cfg")
    row.update(size_bytes=config.stat().st_size, sha256=runtime_file_digest(config))
    inventory.path.write_text(json.dumps(document))
    target = [base_root]
    original_resolve = Path.resolve
    def resolve(path, *, strict=False):
        if path in (alias, alias / "python.exe"):
            assert strict is True
            return target[0] if path == alias else target[0] / "python.exe"
        return original_resolve(path, strict=strict)
    monkeypatch.setattr(Path, "resolve", resolve)
    # Model the OS resolving the same pinned minor alias to a new installed
    # Python after closure hashing, without creating/changing real junctions.
    original_closure = pilot.verify_file_closure
    def closure(path, files):
        result = original_closure(path, files)
        if path == base_root:
            target[0] = alternate
        return result
    monkeypatch.setattr(pilot, "verify_file_closure", closure)
    with pytest.raises(LocalExecutionError, match="pyvenv_home_mismatch"):
        pilot.verify_environment(pin(inventory.path), interpreter)


@pytest.mark.parametrize("drift", ["_base_executable", "base_prefix"])
def test_active_launcher_base_mapping_must_match_captured_runtime(tmp_path, monkeypatch, drift):
    inventory, interpreter, _ = environment(tmp_path)
    document = json.loads(inventory.path.read_text())
    active = SimpleNamespace(executable=str(interpreter.path),
        _base_executable=document["base_interpreter"]["path"], base_prefix=document["base_runtime"]["root"])
    monkeypatch.setattr(pilot, "sys", active)
    assert pilot.verify_environment(inventory, interpreter)["active_launcher_base_verified"] is True
    alternate = tmp_path / "active-foreign-base"
    alternate.mkdir()
    alternate_python = alternate / "python.exe"
    alternate_python.write_bytes(b"unapproved active runtime")
    setattr(active, drift, str(alternate_python if drift == "_base_executable" else alternate))
    with pytest.raises(LocalExecutionError, match="active_base_mismatch"):
        pilot.verify_environment(inventory, interpreter)


def test_environment_traversal_and_duplicate_are_rejected(tmp_path):
    inventory, interpreter, _ = environment(tmp_path)
    document = json.loads(inventory.path.read_text())
    document["files"].append(dict(document["files"][0]))
    inventory.path.write_text(json.dumps(document))
    with pytest.raises(LocalExecutionError, match="duplicate"):
        pilot.verify_environment(pin(inventory.path), interpreter)
    document["files"][-1]["relative"] = "../outside.py"
    inventory.path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="invalid_relative_path"):
        pilot.verify_environment(pin(inventory.path), interpreter)


def test_inventory_verification_never_opens_production_gate(tmp_path, monkeypatch):
    from test_local_asr_runtime import make_profile, rebind
    from secretary.infrastructure.local_asr_runtime import PortableLocalAsrRuntime
    profile = make_profile(tmp_path)
    profile.shared_resource_root = tmp_path / "shared"
    profile.nemotron_free_vram_mib = 7168
    rebind(profile)
    monkeypatch.setattr(PortableLocalAsrRuntime, "_verify_source", lambda self: {})
    runtime = PortableLocalAsrRuntime(profile)
    verified = []
    monkeypatch.setattr(runtime, "verify_inventory", lambda plan: verified.append(plan) or {"qualification_granted": False})
    plan = runtime.plan_factory("pilot", 1, "a" * 64, None)
    assert runtime.verify_inventory(plan)["qualification_granted"] is False
    with pytest.raises(LocalExecutionError, match="runtime_disabled"):
        runtime.qualification(plan)
    runtime.profile.enabled = True
    with pytest.raises(LocalExecutionError, match="not_qualified"):
        runtime.qualification(plan)
    assert len(verified) == 1


@pytest.mark.parametrize("extra", ["model.safetensors", "pytorch_model.bin.index.json", "unexpected-directory"])
def test_model_inventory_rejects_alternate_loader_inputs(tmp_path, monkeypatch, extra):
    from secretary.infrastructure import local_asr_runtime as runtime_module
    runtime = object.__new__(runtime_module.PortableLocalAsrRuntime)
    source = tmp_path / "source"
    (source / "contracts").mkdir(parents=True)
    binary = tmp_path / "pinned.exe"
    binary.write_bytes(b"synthetic executable never run")
    roots = {}
    for role, filename in runtime_module.MODEL_MANIFESTS.items():
        root = tmp_path / "models" / role
        root.mkdir(parents=True)
        roots[role] = root
        metadata = source / "model-metadata" / ("nemotron" if role == "diarizer" else role)
        metadata.mkdir(parents=True)
        weight = root / "pytorch_model.bin"
        weight.write_bytes(b"synthetic weights never loaded")
        manifest = dict(files={weight.name: dict(size_bytes=weight.stat().st_size, sha256=runtime_file_digest(weight))})
        (metadata / "config.json").write_text("{}")
        (root / "config.json").write_text("{}")
        if role == "diarizer":
            (metadata / "PROVENANCE.json").write_text("{}")
            (root / "PROVENANCE.json").write_text("{}")
            manifest["source_provenance_sha256"] = runtime_file_digest(root / "PROVENANCE.json")
        (source / "contracts" / filename).write_text(json.dumps(manifest))
    runtime.profile = SimpleNamespace(source_root=source, contracts_root=None, asr_python=pin(binary),
        nemotron_python=pin(binary), ffmpeg=pin(binary), model_roots=roots,
        identity=SimpleNamespace(models=[SimpleNamespace(role="rnnt", revision=runtime_module.RNNT_VERIFIED_REVISION)]))
    monkeypatch.setattr(runtime, "_check_plan", lambda _: None)
    monkeypatch.setattr(runtime, "_verify_source", lambda: {"verified_files": 1})
    monkeypatch.setattr(runtime, "_verify_identity", lambda: None)
    plan = SimpleNamespace(profile_digest="a" * 64)
    assert runtime.verify_inventory(plan)["qualification_granted"] is False
    alternate = roots["rnnt"] / extra
    if extra == "unexpected-directory":
        alternate.mkdir()
    else:
        alternate.write_bytes(b"must never be selected")
    with pytest.raises(LocalExecutionError, match="model_file_set_mismatch"):
        runtime.verify_inventory(plan)


def test_pilot_authority_refuses_enabled_profile_and_retry(tmp_path):
    from test_local_asr_runtime import make_profile
    profile = make_profile(tmp_path)
    profile.nemotron_free_vram_mib = 7168
    profile.shared_resource_root = tmp_path / "shared"
    file = pin(profile.ffmpeg.path)
    document = dict(schema_version="secretary.local-asr.bootstrap-authority.v1",
        owner_authorization_id="synthetic-test-only", purpose="first_bounded_hardware_pilot",
        profile=profile.model_dump(mode="json"), input=file, input_provenance=file,
        launcher_python=file, pilot_runner_sha256="a" * 64,
        environment_inventories={role: file for role in ("launcher", "asr-cpu", "nemotron")})
    assert pilot.PilotAuthority.model_validate(document).max_attempts == 1
    with pytest.raises(ValueError):
        pilot.PilotAuthority.model_validate(document | {"max_attempts": 2})
    document["profile"]["enabled"] = True
    with pytest.raises(ValueError, match="production_profile_forbidden"):
        pilot.PilotAuthority.model_validate(document)


def test_running_watchdog_has_separate_host_floor_and_no_vram_readmission():
    current = [ResourceSnapshot(3000, 2000, {"GPU-synthetic": 12})]
    watchdog = pilot.ResourceWatchdog(lambda: current[0], "GPU-synthetic")
    watchdog.sample(running=True)
    assert watchdog.receipt()["minima_mib"]["free_vram_mib"] == 12
    current[0] = ResourceSnapshot(2047, 2000, {"GPU-synthetic": 12})
    with pytest.raises(LocalExecutionError, match="host_low_water"):
        watchdog.sample(running=True)
    current[0] = ResourceSnapshot(4000, 2000, {"GPU-synthetic": None})
    with pytest.raises(LocalExecutionError, match="measurement_unavailable"):
        watchdog.sample(running=True)


@pytest.mark.parametrize("enabled", [None, False, "yes", 1, True])
def test_controller_diagnostics_are_literal_opt_in_and_bounded(tmp_path, enabled):
    request = {} if enabled is None else {"retain_stage_diagnostics": enabled}
    receipt = SimpleNamespace(returncode=7, cleanup_confirmed=True, stdout=b"x" * 70000, stderr=b"e" * 70000)
    controller.retain_diagnostic(request, tmp_path, 2, "asr_rnnt", receipt, deadline=time.monotonic() + 2)
    path = tmp_path / "stage-2-diagnostic.json"
    assert path.exists() is (enabled is True)
    if path.exists():
        value = json.loads(path.read_text())
        assert value["returncode"] == 7
        assert len(value["stdout"]) == len(value["stderr"]) == 65536
        assert list(tmp_path.iterdir()) == [path]


def native_case(tmp_path, code, *, timeout=10):
    workspace = tmp_path / "attempt"
    workspace.mkdir()
    authority = SimpleNamespace(deadline_seconds=timeout,
        profile=SimpleNamespace(shared_resource_root=tmp_path / "shared"), launcher_python=pin(Path(sys.executable)))
    plan = SimpleNamespace(physical_gpu_id="GPU-synthetic-pilot", max_attempts=2)
    stage = StageSpec("assemble", (sys.executable, "-B", "-c", code), ("result.json",), timeout_seconds=8)
    supplier = lambda: ResourceSnapshot(10000, 50000, {plan.physical_gpu_id: 8000})
    return authority, plan, (stage,), workspace, supplier


def test_actual_native_controller_exactly_once_and_retains_private_diagnostics(tmp_path):
    args = native_case(tmp_path, "from pathlib import Path;import sys;Path('result.json').write_text('{}');sys.stderr.write('synthetic')")
    authority, plan, specs, workspace, supplier = args
    pilot.supervise_once(authority, plan, specs, workspace, cancelled=lambda: False, snapshot_supplier=supplier)
    receipt = json.loads((workspace / "native-receipt.json").read_text())
    assert receipt["attempt_count"] == receipt["max_attempts"] == 1
    assert receipt["runtime_template_max_attempts"] == 2
    assert receipt["cleanup_confirmed"] is True
    assert len(receipt["completed_stages"]) == len(receipt["stage_diagnostics"]) == 1
    assert receipt["resources"]["sample_count"] >= 4
    assert not list((tmp_path / "shared").rglob("native-state-unverified"))


def test_actual_native_diarization_list_is_validated_and_checkpointed(tmp_path):
    turns = [[0.0, .6, "speaker_0"], [.5, 1.0, "speaker_1"]]
    code = "from pathlib import Path;Path('diarize.json').write_text(" + repr(json.dumps(turns)) + ")"
    authority, plan, _, workspace, supplier = native_case(tmp_path, code)
    stage = StageSpec("diarize", (sys.executable, "-B", "-c", code), ("diarize.json",), timeout_seconds=8)
    pilot.supervise_once(authority, plan, (stage,), workspace, cancelled=lambda: False, snapshot_supplier=supplier)
    receipt = json.loads((workspace / "native-receipt.json").read_text())
    assert receipt["cleanup_confirmed"] is True and receipt["returncode"] == 0
    assert receipt["completed_stages"][0]["stage"] == "diarize"
    output = receipt["completed_stages"][0]["outputs"][0]
    assert output["sha256"] == runtime_file_digest(workspace / "diarize.json")
    assert (workspace / "control/checkpoint-0.json").exists()
    # Production checkpoints already use the generic bounded artifact parser.
    from secretary.application.local_asr_executor import _read_json
    assert _read_json(workspace / "diarize.json") == turns
    # The new list support cannot relax bootstrap authority/inventory parsing.
    with pytest.raises(LocalExecutionError, match="runtime_json_invalid"):
        pilot._json(workspace / "diarize.json")


@pytest.mark.parametrize("payload,code", [
    ({"turns": [[0, 1, "speaker"]]}, "shape_invalid"),
    ([], "shape_invalid"), ([{"start": 0, "end": 1, "label": "speaker"}], "turn_invalid"),
    ([[0, 1]], "turn_invalid"), ([[True, 1, "speaker"]], "turn_invalid"),
    ([[-1, 1, "speaker"]], "turn_invalid"), ([[1, 0, "speaker"]], "turn_invalid"),
    ([[0, 31, "speaker"]], "turn_invalid"), ([[0, 1, ""]], "turn_invalid"),
    ([[0, float("nan"), "speaker"]], "json_invalid"),
])
def test_diarization_list_reader_rejects_noncanonical_turns(tmp_path, payload, code):
    path = tmp_path / "diarize.json"
    path.write_text(json.dumps(payload), "utf-8")
    with pytest.raises(LocalExecutionError, match="diarization_" + code):
        pilot.diarization_artifact_receipt(path)


def test_successful_raw_run_preserves_unverified_tokenizer_gate(tmp_path, monkeypatch):
    from secretary.application.local_asr_package_adapter import PackageAdapterError
    source = tmp_path / "source"
    source.mkdir()
    authority = SimpleNamespace(input=wav(tmp_path / "input.wav"),
        profile=SimpleNamespace(source_root=source), stage_timeout_seconds=10,
        owner_authorization_id="synthetic-authority", model_dump=lambda **kw: {"synthetic": True})
    def unqualified(*args):
        raise PackageAdapterError("package_tokenizer_not_verified")
    runtime = SimpleNamespace(read_result=unqualified)
    plan = SimpleNamespace(physical_gpu_id="GPU-synthetic", model_dump=lambda **kw: {"synthetic": True})
    inventory_calls, dispatches = [], []
    def inventory(_):
        inventory_calls.append(True)
        return runtime, plan, 1000, {"synthetic": True}
    monkeypatch.setattr(pilot, "verify_authority", inventory)
    monkeypatch.setattr(pilot, "bounded_specs", lambda *args: ())
    monkeypatch.setattr(pilot, "supervise_once", lambda *args, **kw: dispatches.append(True))
    workspace = tmp_path / "raw-pilot"
    assert pilot.run_pilot(authority, workspace, authority_pin=authority.input) == 2
    outcome = json.loads((workspace / "pilot-outcome.json").read_text())
    assert outcome["status"] == "inference_succeeded_adapter_blocked"
    assert outcome["production_qualified"] is False and outcome["application_publication"] is False
    assert len(dispatches) == 1 and len(inventory_calls) == 2
    assert (workspace / "inventory-after.json").exists()
    assert not (workspace / "validated-import.json").exists()


@pytest.mark.parametrize("mode", ["cancel", "timeout", "host_floor"])
def test_actual_native_stop_is_confirmed_and_never_retries(tmp_path, mode):
    code = "from pathlib import Path;import time;Path('started').write_text('1');time.sleep(15)"
    authority, plan, specs, workspace, supplier = native_case(tmp_path, code, timeout=1 if mode == "timeout" else 10)
    def resources():
        return (ResourceSnapshot(1024, 50000, {plan.physical_gpu_id: 8000})
                if mode == "host_floor" and (workspace / "started").exists() else supplier())
    with pytest.raises(Exception):
        pilot.supervise_once(authority, plan, specs, workspace,
            cancelled=lambda: mode == "cancel" and (workspace / "started").exists(), snapshot_supplier=resources)
    receipt = json.loads((workspace / "native-receipt.json").read_text())
    assert receipt["cleanup_confirmed"] is True and receipt["attempt_count"] == 1
    # Either the controller deadline or its fresh-admission reservation can
    # observe the same one-second boundary first under concurrent host load.
    allowed = {"cancel": {"local_asr_cancelled"},
               "timeout": {"local_asr_timeout", "local_asr_deadline_exceeded"},
               "host_floor": {"local_asr_pilot_host_low_water"}}[mode]
    assert receipt["failure_code"] in allowed
    assert not receipt["completed_stages"]
    assert not list((tmp_path / "shared").rglob("native-state-unverified"))
