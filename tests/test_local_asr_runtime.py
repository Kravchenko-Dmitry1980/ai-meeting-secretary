"""Portable command/JSON conversion checks; no model, native stage or service."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

import pytest

from secretary.application.local_asr_execution_protocol import ExecutionContext
from secretary.application.local_asr_lifecycle import LocalExecutionError
from secretary.domain.local_asr import RuntimeIdentity, SourceSegment, text_fingerprint
from secretary.infrastructure import local_asr_runtime as module

ROOT = Path(__file__).resolve().parents[1]
PREPARATION = json.loads((ROOT / "config/local-asr/runtime-manifest.json").read_text("utf-8"))
SOURCE = ROOT / PREPARATION["source_package"]["locally_extracted_relative_path"]
DELIVERY = ROOT / PREPARATION["source_package"]["delivery_manifest_relative_path"]
SYNTHETIC = Path(__file__).parent / "fixtures/local_asr/synthetic_run.json"


def make_profile(tmp_path):
    previous = json.loads(SYNTHETIC.read_text("utf-8"))["artifacts"]["identity"]
    previous.update(asr_device="cpu", diarization_device="cuda", physical_gpu_id="GPU-synthetic-123", precision="fp32")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"Synthetic executable identity; never executed")
    profile = module.PortableRuntimeProfile(source_root=SOURCE,
        delivery_manifest={"path": DELIVERY, "sha256": PREPARATION["source_package"]["delivery_manifest_sha256"]},
        asr_python={"path": Path(sys.executable), "sha256": module.runtime_file_digest(Path(sys.executable))},
        nemotron_python={"path": tmp_path / "uninstalled-nemotron-python.exe", "sha256": "1" * 64},
        ffmpeg={"path": ffmpeg, "sha256": module.runtime_file_digest(ffmpeg)},
        model_roots={role: tmp_path / "uninstalled-models" / role for role in ("rnnt", "ctc", "diarizer")},
        identity=RuntimeIdentity.model_validate(previous))
    profile.identity.models = module._model_descriptors(profile)
    return rebind(profile)


def rebind(profile):
    profile.identity.code_digest = module.portable_code_digest(profile)
    profile.identity.profile_digest = module.portable_profile_digest(profile)
    return profile


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    profile = make_profile(tmp_path)
    # The real 3378-file closure is exercised by its own test and the actual
    # assembler below. These unit cases isolate the command/provenance contract.
    monkeypatch.setattr(module.PortableLocalAsrRuntime, "_verify_source", lambda self: {})
    return module.PortableLocalAsrRuntime(profile, clock=lambda: 1000.0)


def synthetic_wav(path):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 80000)


def context_and_plan(runtime, tmp_path, *, fresh=False):
    media = tmp_path / "input.wav"
    synthetic_wav(media)
    payload = json.loads(SYNTHETIC.read_text("utf-8"))
    source = tuple(SourceSegment.model_validate(s) for s in payload["artifacts"]["source_segments"])
    selected = text_fingerprint("\n".join(s.text for s in source))
    sha = module.runtime_file_digest(media)
    context = ExecutionContext("meeting", 1, media, sha, () if fresh else source,
                               "stable-import", None if fresh else selected, 5000)
    plan = runtime.plan_factory("meeting", 1, sha, context.source_text_sha256)
    workspace = tmp_path / "attempt"
    workspace.mkdir()
    return context, plan, workspace


def write_completed_fixture(context, workspace, *, fresh=False):
    payload = json.loads(SYNTHETIC.read_text("utf-8"))
    source = payload["artifacts"]["source_segments"]
    windows = [{"start": s["started_ms"] / 1000, "end": s["ended_ms"] / 1000, "text": s["text"]} for s in source]
    if fresh:
        (workspace / "transcribe.json").write_text(json.dumps({"provider": "gigaam-v3-e2e-rnnt", "segments": windows}), "utf-8")
    aligned = copy.deepcopy(windows)
    for i, item in enumerate(aligned):
        item["words"] = [{"start": w["started_ms"] / 1000, "end": w["ended_ms"] / 1000, "word": w["text"]}
                         for w in payload["artifacts"]["words"][i * 2:i * 2 + 2]]
    result = payload["result"]
    result["input_sha256"] = context.input_sha256
    alignment = {"provider": "gigaam-v3-e2e-rnnt+gigaam-ctc", "diarization_provider": "nemotron-3-diarization",
                 "segments": aligned, "warnings": [], "metrics": result["metrics"]}
    for name, document in (("align.json", alignment), ("result.json", result),
                           ("normalize.json", {"media_duration_seconds": 5.0})):
        (workspace / name).write_text(json.dumps(document, ensure_ascii=False), "utf-8")
    synthetic_wav(workspace / "normalized.wav")
    return result, alignment


def test_real_received_source_and_metadata_can_load_without_interpreters_or_weights(tmp_path, monkeypatch):
    profile = make_profile(tmp_path)
    path = tmp_path / "disabled-profile.json"
    path.write_text(profile.model_dump_json(), "utf-8")
    runtime = module.load_local_asr_runtime(path)
    monkeypatch.setattr(runtime, "_verify_source", lambda: {})
    assert runtime.profile.enabled is False
    assert {model.role for model in runtime.profile.identity.models} == {"rnnt", "ctc", "diarizer"}
    with pytest.raises(LocalExecutionError, match="runtime_disabled"):
        runtime.qualification(runtime.plan_factory("meeting", 1, "a" * 64, "b" * 64))


def test_saved_text_stage_commands_are_portable_and_never_transcribe(runtime, tmp_path):
    context, plan, workspace = context_and_plan(runtime, tmp_path)
    specs = runtime.build_attempt(plan, context, workspace)
    assert [s.stage for s in specs] == ["normalize", "diarize", "align_saved_text", "assemble"]
    assert not any("transcribe" in s.command for s in specs)
    assert specs[0].binary_outputs == ("normalized.wav",)
    assert specs[1].command[0] == str(runtime.profile.nemotron_python.path)
    assert specs[1].environment["CUDA_VISIBLE_DEVICES"] == "GPU-synthetic-123"
    assert specs[2].environment["CUDA_VISIBLE_DEVICES"] == "-1"
    assert all("PYTHONPATH" not in s.environment for s in specs)
    assert specs[1].python_paths == (SOURCE / "external/transformers/src",)
    assert specs[2].command[specs[2].command.index("--quality-mode") + 1] == "nemotron-ctc"
    assert specs[2].command[specs[2].command.index("--asr-device") + 1] == "cpu"
    assert str(module.PINNED_STAGE) in specs[2].command
    assert specs[2].command[specs[2].command.index("--ffmpeg") + 1] == str(runtime.profile.ffmpeg.path)
    assert all("C:\\AI\\Projects\\neurohr\\neurohr-dev" not in str(s.command) for s in specs)
    projection = json.loads((workspace / "transcribe.json").read_text("utf-8"))
    assert [s["text"] for s in projection["segments"]] == [s.text for s in context.source_segments]
    assert projection["metrics"]["text_origin"] == "saved_transcript"
    assert runtime.build_attempt(plan, context, workspace) == specs


def test_fresh_text_commands_include_rnnt_and_have_no_saved_projection(runtime, tmp_path):
    context, plan, workspace = context_and_plan(runtime, tmp_path, fresh=True)
    specs = runtime.build_attempt(plan, context, workspace)
    assert [s.stage for s in specs] == ["normalize", "diarize", "asr_rnnt", "align_rnnt", "assemble"]
    assert "transcribe" in specs[2].command
    assert not (workspace / "transcribe.json").exists()
    assert plan.max_attempts == 2 and plan.deadline_at == 4600


def test_result_reader_preserves_saved_source_and_writes_bound_proof(runtime, tmp_path):
    runtime.profile.tokenizer_compatibility = "verified"
    rebind(runtime.profile)
    context, plan, workspace = context_and_plan(runtime, tmp_path)
    runtime.build_attempt(plan, context, workspace)
    result, _ = write_completed_fixture(context, workspace)
    command = runtime.read_result(plan, context, workspace)
    assert command.result.model_dump(mode="json") == result
    assert command.artifacts.source_segments == list(context.source_segments)
    assert command.artifacts.alignment.status == "complete"
    proof = json.loads((workspace / "package-provenance.json").read_text("utf-8"))
    assert proof["text_origin"] == "saved_transcript"
    assert command.artifacts.runtime_receipts[-1].receipt_sha256 == module.package_document_digest(proof)
    assert runtime.read_result(plan, context, workspace) == command


def test_fresh_result_gets_deterministic_source_ids_without_writing_database(runtime, tmp_path):
    runtime.profile.tokenizer_compatibility = "verified"
    rebind(runtime.profile)
    context, plan, workspace = context_and_plan(runtime, tmp_path, fresh=True)
    runtime.build_attempt(plan, context, workspace)
    write_completed_fixture(context, workspace, fresh=True)
    first = runtime.read_result(plan, context, workspace)
    second = runtime.read_result(plan, context, workspace)
    assert first == second
    assert all(s.segment_id.startswith("local-asr-") for s in first.artifacts.source_segments)
    assert len({s.segment_id for s in first.artifacts.source_segments}) == 5
    assert json.loads((workspace / "package-provenance.json").read_text("utf-8"))["text_origin"] == "rnnt"


@pytest.mark.parametrize("mutation,code", [
    (lambda p: setattr(p.identity, "code_digest", "0" * 64), "code_mismatch"),
    (lambda p: setattr(p.identity, "profile_digest", "0" * 64), "profile_mismatch"),
    (lambda p: setattr(p.identity, "asr_device", "cuda"), "profile_unsupported"),
])
def test_profile_changes_cannot_reuse_qualified_identity(runtime, mutation, code):
    mutation(runtime.profile)
    with pytest.raises(LocalExecutionError, match=code):
        runtime.plan_factory("meeting", 1, "a" * 64, "b" * 64)


def test_missing_qualification_is_blocked_even_when_explicitly_enabled(runtime):
    runtime.profile.enabled = True
    runtime.profile.shared_resource_root = SOURCE.parent / "test-shared-root-not-created"
    runtime.profile.nemotron_free_vram_mib = 2048
    rebind(runtime.profile)
    plan = runtime.plan_factory("meeting", 1, "a" * 64, "b" * 64)
    with pytest.raises(LocalExecutionError, match="not_qualified"):
        runtime.qualification(plan)


def test_input_hash_and_existing_projection_conflicts_fail_closed(runtime, tmp_path):
    context, plan, workspace = context_and_plan(runtime, tmp_path)
    runtime.build_attempt(plan, context, workspace)
    (workspace / "transcribe.json").write_text('{"segments":[]}', "utf-8")
    with pytest.raises(LocalExecutionError, match="projection_conflict"):
        runtime.build_attempt(plan, context, workspace)
    context.input_path.write_bytes(b"changed synthetic input")
    with pytest.raises(LocalExecutionError, match="input_mismatch"):
        runtime.build_attempt(plan, context, workspace)


def test_unknown_saved_timing_requires_explicit_mapping(runtime, tmp_path):
    context, plan, workspace = context_and_plan(runtime, tmp_path)
    sources = list(context.source_segments)
    sources[0] = sources[0].model_copy(update={"started_ms": None, "ended_ms": None})
    altered = ExecutionContext(context.meeting_id, context.transcript_version, context.input_path,
        context.input_sha256, tuple(sources), context.operation_id, context.source_text_sha256, context.duration_ms)
    with pytest.raises(LocalExecutionError, match="source_window_mapping"):
        runtime.build_attempt(plan, altered, workspace)


def test_loader_errors_are_bounded_without_private_paths(tmp_path):
    path = tmp_path / "secret-looking-name.json"
    path.write_text('{"enabled":true,"unrecognized":"private"}', "utf-8")
    with pytest.raises(LocalExecutionError) as error:
        module.load_local_asr_runtime(path)
    assert str(error.value) == "local_asr_runtime_profile_invalid"


def test_runtime_refuses_missing_ffmpeg_pin_without_ambient_lookup(runtime, tmp_path):
    runtime.profile.ffmpeg = None
    rebind(runtime.profile)
    context, plan, workspace = context_and_plan(runtime, tmp_path)
    with pytest.raises(LocalExecutionError, match="ffmpeg_pin_required"):
        runtime.build_attempt(plan, context, workspace)


@pytest.mark.parametrize("name", ["local_asr_executor.py", "local_asr_controller.py", "local_asr_publication.py",
                                 "meeting_voice_audio.py", "voice.py"])
def test_processing_semantics_change_invalidates_code_identity(runtime, monkeypatch, name):
    original = module.runtime_file_digest
    monkeypatch.setattr(module, "runtime_file_digest", lambda path, **kw: "f" * 64
                        if path.name == name else original(path, **kw))
    with pytest.raises(LocalExecutionError, match="code_mismatch"):
        runtime.plan_factory("meeting", 1, "a" * 64, "b" * 64)


def test_ffmpeg_shim_preserves_arguments_and_rechecks_exact_executable(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("synthetic_local_asr_stage", module.PINNED_STAGE)
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"synthetic pinned binary; not executed")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    observed = []
    def fake_run(command, *args, **kwargs):
        observed.append((command, args, kwargs))
        return "synthetic result"
    monkeypatch.setattr(subprocess, "run", fake_run)
    command = ["ffmpeg", "-nostdin", "-threads", "0", "-i", "synthetic.wav", "-f", "s16le", "-"]
    with shim.redirect_ffmpeg(executable, digest):
        assert subprocess.run(command, capture_output=True, check=True) == "synthetic result"
        assert observed[0] == ([str(executable), *command[1:]], (), {"capture_output": True, "check": True})
        assert command[0] == "ffmpeg"
        executable.write_bytes(b"changed")
        with pytest.raises(ValueError, match="hash_mismatch"):
            subprocess.run(command, capture_output=True, check=True)
    assert subprocess.run is fake_run
    assert len(observed) == 1


def test_actual_verified_bridge_assembly_uses_no_model_stage(runtime, tmp_path):
    context, plan, workspace = context_and_plan(runtime, tmp_path)
    specs = runtime.build_attempt(plan, context, workspace)
    _, alignment = write_completed_fixture(context, workspace)
    # This fixture file is ours; create a fresh output name for the worker.
    (workspace / "result.json").unlink()
    (workspace / "diarize.json").write_text(json.dumps([[0, .8, "a"], [1, 1.8, "a"],
        [2, 2.8, "a"], [3, 3.8, "a"], [3, 3.8, "b"]]), "utf-8")
    stage = specs[-1]
    environment = dict(os.environ, **stage.environment)
    # Keep the pre-existing offline guard in this synthetic child. This is
    # assembly only; a model-class constructor is never selected.
    environment["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
    completed = subprocess.run(stage.command, cwd=workspace, env=environment,
                               capture_output=True, timeout=120, creationflags=0x08000000 if os.name == "nt" else 0)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    result = json.loads((workspace / "result.json").read_text("utf-8"))
    assert result["metrics"] == alignment["metrics"]
    assert len(result["speaker_turns"]) == 5
    assert result["speaker_turns"][3]["started_ms"] == result["speaker_turns"][4]["started_ms"]
    assert [w for s in result["segment_items"] for w in s["text"].split()] == [
        w for s in context.source_segments for w in s.text.split()]
