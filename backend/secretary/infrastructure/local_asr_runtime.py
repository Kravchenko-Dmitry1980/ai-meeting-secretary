"""Explicit, disabled-by-default portable binding to the received ASR package.

Loading/planning reads hashes and JSON only. Native stages are commands for the
owned executor, never imports or subprocesses in this module. Runtime admission
and model qualification are separate from source integrity.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Literal
import wave

from pydantic import Field, field_validator

from secretary.application.local_asr_execution_protocol import ExecutionContext, StageSpec
from secretary.application.local_asr_lifecycle import ExecutionPlan, LocalExecutionError, RuntimeQualification
from secretary.application.local_asr_package_adapter import (
    PackageArtifactProvenance, adapt_package_artifacts, package_document_digest,
)
from secretary.domain.local_asr import (
    Digest, ImportLocalAsrRun, RuntimeIdentity, RuntimeModel, SourceSegment,
    model_manifest_digest, seconds_to_ms, text_fingerprint,
)
from secretary.domain.speakers import DTO
from secretary.infrastructure.local_asr_source import SourceVerificationError, verify_source_bundle


PACKAGE_MANIFEST_SHA256 = "33e0a9a59d6278e9b2363e822b2ce7f875c3252b8d333270ffa139a3c45d7109"
APP_ROOT = Path(__file__).resolve().parents[3]
ASSEMBLER = APP_ROOT / "scripts/local_asr_assemble.py"
PINNED_STAGE = APP_ROOT / "scripts/local_asr_stage.py"
MODEL_MANIFESTS = {
    "rnnt": "dev-gigaam-model-manifest.json", "ctc": "dev-gigaam-ctc-model-manifest.json",
    "diarizer": "dev-nemotron3-model-manifest.json",
}
RNNT_BAD_REVISION = "7655ad717f8122257385bb4b2f373db3697e8686"
RNNT_VERIFIED_REVISION = "7655ad717f8122257385bb4b2f373db3697e8680"
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise LocalExecutionError(code)


def _plain(path: Path, *, directory: bool = False):
    _require(path.is_absolute() and ".." not in path.parts
             and not str(path).startswith(("\\\\", "//")), "local_asr_runtime_path_invalid")
    try:
        for item in (*reversed(path.parents), path):
            info = item.lstat()
            _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                     "local_asr_runtime_path_linked")
        _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                 "local_asr_runtime_path_invalid")
        return info
    except OSError:
        raise LocalExecutionError("local_asr_runtime_file_unavailable") from None


def runtime_file_digest(path: Path, *, max_bytes: int = MAX_INPUT_BYTES) -> str:
    before = _plain(path)
    _require(0 <= before.st_size <= max_bytes, "local_asr_runtime_file_size")
    signature = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_nlink)
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            hasher = hashlib.sha256()
            count = 0
            while block := stream.read(1024 * 1024):
                count += len(block)
                _require(count <= max_bytes, "local_asr_runtime_file_size")
                hasher.update(block)
            digest = hasher.hexdigest()
            finished = os.fstat(stream.fileno())
        _require(signature(before) == signature(opened) == signature(finished) == signature(_plain(path)),
                 "local_asr_runtime_file_changed")
        return digest
    except OSError:
        raise LocalExecutionError("local_asr_runtime_file_unavailable") from None


def _json(path: Path, *, expected_sha256: str | None = None) -> dict:
    info = _plain(path)
    _require(info.st_size <= MAX_JSON_BYTES, "local_asr_runtime_json_size")
    def unique(pairs):
        output = {}
        for key, value in pairs:
            _require(key not in output, "local_asr_runtime_duplicate_json_key")
            output[key] = value
        return output
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            data = stream.read(MAX_JSON_BYTES + 1)
            finished = os.fstat(stream.fileno())
        signature = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_nlink)
        _require(signature(info) == signature(opened) == signature(finished) == signature(_plain(path))
                 and len(data) == info.st_size, "local_asr_runtime_file_changed")
        if expected_sha256 is not None:
            _require(hashlib.sha256(data).hexdigest() == expected_sha256,
                     "local_asr_runtime_json_digest_mismatch")
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _require(isinstance(value, dict), "local_asr_runtime_json_shape")
        return value
    except (OSError, ValueError, UnicodeError):
        raise LocalExecutionError("local_asr_runtime_json_invalid") from None


class PinnedRuntimeFile(DTO):
    path: Path
    sha256: Digest

    @field_validator("path")
    @classmethod
    def absolute(cls, value):
        if not value.is_absolute() or ".." in value.parts or str(value).startswith(("\\\\", "//")):
            raise ValueError("Absolute local file required")
        return value


class PortableRuntimeProfile(DTO):
    schema_version: Literal["secretary.local-asr.portable-profile.v1"] = "secretary.local-asr.portable-profile.v1"
    enabled: bool = Field(default=False, strict=True)
    source_root: Path
    delivery_manifest: PinnedRuntimeFile
    # Optional separately reviewed metadata overlay, never an in-place repair.
    contracts_root: Path | None = None
    asr_python: PinnedRuntimeFile
    nemotron_python: PinnedRuntimeFile
    ffmpeg: PinnedRuntimeFile | None = None
    model_roots: dict[Literal["rnnt", "ctc", "diarizer"], Path]
    identity: RuntimeIdentity
    shared_resource_root: Path | None = None
    nemotron_free_vram_mib: int | None = Field(default=None, strict=True, ge=1)
    tokenizer_compatibility: Literal["verified", "unverified", "unsupported"] = "unverified"
    qualification_file: PinnedRuntimeFile | None = None

    @field_validator("source_root", "contracts_root", "shared_resource_root")
    @classmethod
    def local_root(cls, value):
        if value is not None and (not value.is_absolute() or ".." in value.parts
                                  or str(value).startswith(("\\\\", "//"))):
            raise ValueError("Absolute local root required")
        return value

    @field_validator("model_roots")
    @classmethod
    def roots(cls, value):
        if set(value) != {"rnnt", "ctc", "diarizer"}:
            raise ValueError("All pinned model roots required")
        for path in value.values():
            cls.local_root(path)
        return value


def portable_profile_digest(profile: PortableRuntimeProfile) -> str:
    data = profile.model_dump(mode="json", exclude={"enabled", "qualification_file"})
    data["identity"].pop("profile_digest")
    return package_document_digest(data)


def portable_code_digest(profile: PortableRuntimeProfile) -> str:
    # Publication, ownership, admission and checkpoint semantics are part of
    # processing identity just as much as the model adapter's own source.
    relative_host_files = (
        "backend/secretary/infrastructure/local_asr_runtime.py",
        "scripts/local_asr_assemble.py", "scripts/local_asr_stage.py", "scripts/local_asr_controller.py",
        "backend/secretary/application/local_asr_package_adapter.py",
        "backend/secretary/application/local_asr_execution_protocol.py",
        "backend/secretary/application/local_asr_executor.py",
        "backend/secretary/application/local_asr_lifecycle.py",
        "backend/secretary/application/local_asr_media.py",
        "backend/secretary/infrastructure/local_asr_process.py",
        "backend/secretary/infrastructure/local_asr_publication.py",
        "backend/secretary/infrastructure/meeting_voice_audio.py",
        "backend/secretary/infrastructure/local_asr_resource_probe.py",
        "backend/secretary/infrastructure/local_asr_resources.py",
        "backend/secretary/infrastructure/local_asr_repository.py",
        "backend/secretary/infrastructure/local_asr_source.py",
        "backend/secretary/infrastructure/instance_lock.py",
        "backend/secretary/infrastructure/voice_process.py",
        "backend/secretary/domain/local_asr.py", "backend/secretary/domain/speakers.py",
        "backend/secretary/domain/voice.py",
    )
    host_files = tuple(APP_ROOT / relative for relative in relative_host_files)
    contracts = profile.contracts_root or profile.source_root
    return package_document_digest({
        "generation": "canonical-release-20261021", "package_manifest": PACKAGE_MANIFEST_SHA256,
        "host": {path.relative_to(APP_ROOT).as_posix(): runtime_file_digest(path) for path in host_files},
        "contracts": {role: runtime_file_digest(contracts / "contracts" / name)
                      for role, name in MODEL_MANIFESTS.items()},
    })


def _model_descriptors(profile: PortableRuntimeProfile) -> list[RuntimeModel]:
    source = profile.source_root
    contracts = profile.contracts_root or source
    descriptors = []
    for role, name in MODEL_MANIFESTS.items():
        manifest = _json(contracts / "contracts" / name)
        original = _json(source / "contracts" / name)
        # Only the already documented RNNT metadata typo may differ. No model,
        # loader or tokenizer bytes can be substituted by an overlay.
        comparison = dict(manifest)
        if role == "rnnt":
            _require(manifest.get("revision") in {RNNT_BAD_REVISION, RNNT_VERIFIED_REVISION},
                     "local_asr_runtime_model_revision")
            comparison["revision"] = original["revision"]
        _require(comparison == original, "local_asr_runtime_model_manifest_changed")
        files = manifest["files"]
        if role == "diarizer":
            weights = files["model.safetensors"]["sha256"]
            loader = source / "scripts/dev_nemotron_provider.py"
            processor = runtime_file_digest(source / "external/transformers/src/transformers/models/nemotron3_diarization/processing_nemotron3_diarization.py")
            custom = runtime_file_digest(source / "external/transformers/src/transformers/models/nemotron3_diarization/modeling_nemotron3_diarization.py")
        else:
            weights = files["pytorch_model.bin"]["sha256"]
            loader = source / ("scripts/dev_gigaam_provider.py" if role == "rnnt" else "scripts/dev_gigaam_ctc_alignment.py")
            processor = files["tokenizer.model"]["sha256"]
            custom = files["modeling_gigaam.py"]["sha256"]
        descriptors.append(RuntimeModel(role=role, model_id=manifest["repository"], revision=manifest["revision"],
            weights_sha256=weights, loader_sha256=runtime_file_digest(loader),
            processor_sha256=processor, custom_code_sha256=custom))
    return descriptors


class PortableLocalAsrRuntime:
    def __init__(self, profile: PortableRuntimeProfile, *, clock=time.time):
        self.profile = PortableRuntimeProfile.model_validate(profile.model_dump(mode="json"))
        self.clock = clock
        self._verify_source()
        self._verify_identity()

    def _verify_source(self):
        try:
            return verify_source_bundle(self.profile.source_root, self.profile.delivery_manifest.path,
                delivery_manifest_sha256=self.profile.delivery_manifest.sha256,
                package_manifest_sha256=PACKAGE_MANIFEST_SHA256, expected_source_files=65)
        except SourceVerificationError:
            raise LocalExecutionError("local_asr_runtime_source_unverified") from None

    def _verify_identity(self):
        profile = self.profile
        _require(profile.identity.asr_device == "cpu" and profile.identity.diarization_device == "cuda"
                 and profile.identity.precision == "fp32", "local_asr_runtime_profile_unsupported")
        _require(profile.identity.code_digest == portable_code_digest(profile), "local_asr_runtime_code_mismatch")
        _require(profile.identity.profile_digest == portable_profile_digest(profile), "local_asr_runtime_profile_mismatch")
        _require(model_manifest_digest(profile.identity.models) == model_manifest_digest(_model_descriptors(profile)),
                 "local_asr_runtime_model_identity_mismatch")

    def plan_factory(self, meeting_id: str, transcript_version: int, input_sha256: str,
                     source_text_sha256: str | None) -> ExecutionPlan:
        self._verify_source()
        self._verify_identity()
        identity = self.profile.identity
        stages = ("normalize", "diarize", "align_saved_text", "assemble", "validate") if source_text_sha256 else (
            "normalize", "diarize", "asr_rnnt", "align_rnnt", "assemble", "validate")
        return ExecutionPlan(input_sha256=input_sha256, source_text_sha256=source_text_sha256,
            profile_digest=identity.profile_digest, code_digest=identity.code_digest,
            model_digest=model_manifest_digest(identity.models), asr_device="cpu", diarization_device="cuda",
            physical_gpu_id=identity.physical_gpu_id, precision=identity.precision, stages=stages,
            nemotron_free_vram_mib=self.profile.nemotron_free_vram_mib,
            deadline_at=self.clock() + 3600, max_attempts=2)

    plan_for = plan_factory

    def _check_plan(self, plan: ExecutionPlan):
        identity = self.profile.identity
        expected_stages = ("normalize", "diarize", "align_saved_text", "assemble", "validate") if plan.source_text_sha256 else (
            "normalize", "diarize", "asr_rnnt", "align_rnnt", "assemble", "validate")
        _require(plan.profile_digest == identity.profile_digest and plan.code_digest == identity.code_digest
                 and plan.model_digest == model_manifest_digest(identity.models)
                 and plan.physical_gpu_id == identity.physical_gpu_id
                 and plan.asr_device == "cpu" and plan.diarization_device == "cuda"
                 and plan.precision == "fp32" and plan.provider == "local"
                 and plan.stages == expected_stages and plan.cpu_threads == 4 and plan.max_attempts == 2
                 and plan.nemotron_free_vram_mib == self.profile.nemotron_free_vram_mib,
                 "local_asr_runtime_plan_mismatch")

    def qualification(self, plan: ExecutionPlan) -> RuntimeQualification:
        self._check_plan(plan)
        _require(self.profile.enabled, "local_asr_runtime_disabled")
        _require(self.profile.ffmpeg is not None, "local_asr_ffmpeg_pin_required")
        _require(self.profile.shared_resource_root is not None, "local_asr_shared_resource_root_required")
        _require(self.profile.nemotron_free_vram_mib is not None, "local_asr_nemotron_vram_guard_unverified")
        self._verify_source()
        self._verify_identity()
        proof_file = self.profile.qualification_file
        _require(proof_file is not None, "local_asr_runtime_not_qualified")
        _require(runtime_file_digest(proof_file.path) == proof_file.sha256, "local_asr_qualification_digest_mismatch")
        proof = _json(proof_file.path)
        _require(proof.get("schema_version") == "secretary.local-asr.runtime-qualification.v1"
                 and proof.get("profile_digest") == plan.profile_digest
                 and all(proof.get(key) is True for key in ("model_runtime_verified", "process_containment_verified",
                            "shared_admission_verified", "environment_inventory_verified"))
                 and isinstance(proof.get("owner_authorization_id"), str) and bool(proof["owner_authorization_id"])
                 and isinstance(proof.get("evidence_id"), str) and bool(proof["evidence_id"]),
                 "local_asr_runtime_not_qualified")
        self.verify_inventory(plan)
        return RuntimeQualification(plan.profile_digest, proof["evidence_id"], proof["owner_authorization_id"],
                                    True, True, True)

    def verify_inventory(self, plan: ExecutionPlan) -> dict:
        """Read-only integrity evidence, never authority to activate the runtime.

        An isolated, explicitly authorized bootstrap pilot may collect the first
        runtime evidence before qualification exists. The application gate above
        still requires enabled plus its independent qualification proof.
        """
        self._check_plan(plan)
        source = self._verify_source()
        self._verify_identity()
        _require(self.profile.ffmpeg is not None, "local_asr_ffmpeg_pin_required")
        _require(next(m for m in self.profile.identity.models if m.role == "rnnt").revision == RNNT_VERIFIED_REVISION,
                 "local_asr_rnnt_revision_unqualified")
        executables = []
        for executable in (self.profile.asr_python, self.profile.nemotron_python, self.profile.ffmpeg):
            _require(runtime_file_digest(executable.path) == executable.sha256, "local_asr_runtime_interpreter_mismatch")
            executables.append(executable.model_dump(mode="json"))
        contracts = self.profile.contracts_root or self.profile.source_root
        models = []
        for role, name in MODEL_MANIFESTS.items():
            manifest = _json(contracts / "contracts" / name)
            root = self.profile.model_roots[role]
            _plain(root, directory=True)
            expected = dict(manifest["files"])
            required = set(expected)
            if role == "diarizer":
                required.add("PROVENANCE.json")
            metadata = self.profile.source_root / "model-metadata" / ("nemotron" if role == "diarizer" else role)
            _plain(metadata, directory=True)
            for original in metadata.iterdir():
                info = _plain(original)
                entry = dict(size_bytes=info.st_size, sha256=runtime_file_digest(original))
                _require(original.name not in expected or expected[original.name] == entry,
                         "local_asr_runtime_model_metadata_mismatch")
                expected[original.name] = entry
            # Loaders may prefer an extra safetensors/checkpoint over the pinned
            # .bin. Verify the complete root, including provenance and metadata.
            present = {path.name for path in root.iterdir()}
            _require(required <= present <= set(expected),
                     "local_asr_runtime_model_file_set_mismatch")
            for relative in sorted(present):
                entry = expected[relative]
                _require(Path(relative).name == relative, "local_asr_runtime_model_path_invalid")
                path = root / relative
                _require(_plain(path).st_size == entry["size_bytes"]
                         and runtime_file_digest(path) == entry["sha256"], "local_asr_runtime_model_file_mismatch")
                models.append(dict(role=role, path=str(path), **entry))
            if role == "diarizer":
                _require(runtime_file_digest(root / "PROVENANCE.json") == manifest["source_provenance_sha256"],
                         "local_asr_runtime_model_provenance_mismatch")
        return dict(schema_version="secretary.local-asr.inventory-verification.v1",
                    profile_digest=plan.profile_digest, source=source, executables=executables,
                    model_files=models, qualification_granted=False)

    def build_attempt(self, plan: ExecutionPlan, context: ExecutionContext, workspace: Path) -> tuple[StageSpec, ...]:
        self._check_plan(plan)
        self._verify_source()
        self._verify_identity()
        _plain(workspace, directory=True)
        _require(not workspace.is_relative_to(self.profile.source_root)
                 and not self.profile.source_root.is_relative_to(workspace), "local_asr_runtime_workspace_overlaps_source")
        _require(context.input_sha256 == plan.input_sha256 and context.source_text_sha256 == plan.source_text_sha256,
                 "local_asr_runtime_context_mismatch")
        _require(runtime_file_digest(context.input_path) == plan.input_sha256, "local_asr_runtime_input_mismatch")
        _require(self.profile.ffmpeg is not None, "local_asr_ffmpeg_pin_required")
        _require(runtime_file_digest(self.profile.ffmpeg.path) == self.profile.ffmpeg.sha256,
                 "local_asr_runtime_ffmpeg_mismatch")
        if plan.source_text_sha256 is not None:
            _require(bool(context.source_segments)
                     and text_fingerprint("\n".join(s.text for s in context.source_segments)) == plan.source_text_sha256,
                     "local_asr_runtime_saved_source_mismatch")
            windows = []
            for source in context.source_segments:
                _require(source.started_ms is not None and source.ended_ms is not None
                         and 0 < source.ended_ms - source.started_ms <= 24_000 and bool(source.text.split()),
                         "local_asr_source_window_mapping_required")
                windows.append({"start": source.started_ms / 1000, "end": source.ended_ms / 1000, "text": source.text})
            projection = {"provider": "gigaam-v3-e2e-rnnt", "language": "ru", "segments": windows,
                          "warnings": [], "metrics": {"text_origin": "saved_transcript"}}
            target = workspace / "transcribe.json"
            encoded = json.dumps(projection, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if target.exists():
                _require(package_document_digest(_json(target)) == package_document_digest(projection),
                         "local_asr_runtime_projection_conflict")
            else:
                with target.open("xb") as output:
                    output.write(encoded)
        else:
            _require(not context.source_segments, "saved_text_must_not_be_retranscribed")
        profile = self.profile
        root = profile.contracts_root or profile.source_root
        script = profile.source_root / "scripts/dev_asr_pipeline.py"
        normalized = workspace / "normalized.wav"
        specs = []
        for stage in plan.stages:
            if stage == "validate":
                continue
            environment = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1", "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4",
                "OPENBLAS_NUM_THREADS": "4", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "CUDA_VISIBLE_DEVICES": str(profile.identity.physical_gpu_id) if stage == "diarize" else "-1",
                "DEV_ASR_TRACEBACK": "0", "DEV_CTC_TRACE_PROGRESS": "0",
                "NEUROHR_ASR_EXPERIMENTAL_GROUP_GAP_SECONDS": "1.5",
                "NEUROHR_ASR_EXPERIMENTAL_NEMOTRON_TURN_END_PAD_MS": "0"}
            python = profile.nemotron_python.path if stage == "diarize" else profile.asr_python.path
            python_paths = (profile.source_root / "external/transformers/src",) if stage == "diarize" else ()
            for path in python_paths:
                _plain(path, directory=True)
            if stage == "assemble":
                command = (str(python), "-B", str(ASSEMBLER), "--source-root", str(profile.source_root),
                    "--delivery-manifest", str(profile.delivery_manifest.path), "--delivery-sha256", profile.delivery_manifest.sha256,
                    "--workspace", str(workspace), "--input-sha256", plan.input_sha256)
                specs.append(StageSpec(stage=stage, command=command, outputs=("result.json",), environment=environment))
                continue
            actual = {"asr_rnnt": "transcribe", "align_rnnt": "align", "align_saved_text": "align"}.get(stage, stage)
            _require(actual in {"normalize", "diarize", "transcribe", "align"}, "local_asr_runtime_stage_unsupported")
            output_name = actual + ".json"
            command = (str(python), "-B", str(script), "--stage", actual, "--backend", "gigaam",
                "--quality-mode", "nemotron-ctc", "--asr-device", "cpu", "--max-duration-seconds", "14400",
                # Required by the upstream parser but unused in the GigaAM
                # branch. Supply its actual included compatibility profile;
                # backend=gigaam prevents any Whisper selection.
                "--root", str(root), "--profile", str(profile.source_root / "external/ai-media-factory/profiles/transcription/faster-whisper-small-ru.yaml"),
                "--media", str(context.input_path if actual == "normalize" else normalized),
                "--directory", str(workspace), "--output", str(workspace / output_name),
                "--gigaam-model-root", str(profile.model_roots["rnnt"]),
                "--ctc-model-root", str(profile.model_roots["ctc"]),
                "--nemotron-model-root", str(profile.model_roots["diarizer"]))
            if actual in {"transcribe", "align"}:
                command = (str(python), "-B", str(PINNED_STAGE), "--pipeline", str(script),
                    "--ffmpeg", str(profile.ffmpeg.path), "--ffmpeg-sha256", profile.ffmpeg.sha256, "--",
                    *command[3:])
            specs.append(StageSpec(stage=stage, command=command, outputs=(output_name,), environment=environment,
                                  binary_outputs=("normalized.wav",) if actual == "normalize" else (),
                                  python_paths=python_paths))
        return tuple(specs)

    def read_result(self, plan: ExecutionPlan, context: ExecutionContext, workspace: Path) -> ImportLocalAsrRun:
        self._check_plan(plan)
        self._verify_identity()
        _plain(workspace, directory=True)
        _require(context.input_sha256 == plan.input_sha256 and context.source_text_sha256 == plan.source_text_sha256,
                 "local_asr_runtime_context_mismatch")
        transcript, alignment, result = (_json(workspace / name) for name in ("transcribe.json", "align.json", "result.json"))
        normalized = _json(workspace / "normalize.json")
        wav = workspace / "normalized.wav"
        _plain(wav)
        try:
            with wave.open(str(wav), "rb") as audio:
                _require((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype())
                         == (1, 2, 16000, "NONE"), "local_asr_normalized_audio_invalid")
                duration_ms = seconds_to_ms(audio.getnframes() / 16000)
            _require(duration_ms == seconds_to_ms(normalized["media_duration_seconds"])
                     and 0 < duration_ms <= 14_400_000, "local_asr_normalized_duration_mismatch")
        except (OSError, EOFError, wave.Error, KeyError, ValueError):
            raise LocalExecutionError("local_asr_normalized_audio_invalid") from None
        sources = list(context.source_segments)
        if plan.source_text_sha256 is None:
            _require(not sources, "saved_text_must_not_be_retranscribed")
            for i, segment in enumerate(transcript.get("segments", [])):
                sources.append(SourceSegment(segment_id="local-asr-" + hashlib.sha256(
                    f"{context.meeting_id}:{context.transcript_version}:{plan.input_sha256}:{i}".encode()).hexdigest()[:40],
                    text=segment["text"], channel="import", started_ms=seconds_to_ms(segment["start"]),
                    ended_ms=seconds_to_ms(segment["end"])))
        else:
            _require(text_fingerprint("\n".join(source.text for source in sources)) == plan.source_text_sha256,
                     "local_asr_runtime_saved_source_mismatch")
        proof = PackageArtifactProvenance(text_origin="saved_transcript" if plan.source_text_sha256 else "rnnt",
            input_sha256=plan.input_sha256, identity=self.profile.identity,
            source_segments_sha256=package_document_digest([s.model_dump(mode="json") for s in sources]),
            transcript_sha256=package_document_digest(transcript), alignment_sha256=package_document_digest(alignment),
            result_sha256=package_document_digest(result), tokenizer_compatibility=self.profile.tokenizer_compatibility,
            runtime_receipts=[])
        command = adapt_package_artifacts(operation_id=context.operation_id, meeting_id=context.meeting_id,
            transcript_version=context.transcript_version, duration_ms=duration_ms, source_segments=sources,
            transcript=transcript, alignment=alignment, result=result, provenance=proof)
        proof_path = workspace / "package-provenance.json"
        encoded = proof.model_dump_json(indent=2).encode("utf-8")
        if proof_path.exists():
            _require(package_document_digest(_json(proof_path)) == package_document_digest(proof.model_dump(mode="json")),
                     "local_asr_runtime_provenance_conflict")
        else:
            with proof_path.open("xb") as output:
                output.write(encoded)
        return command


def load_local_asr_runtime(profile_path: Path) -> PortableLocalAsrRuntime:
    try:
        return PortableLocalAsrRuntime(PortableRuntimeProfile.model_validate(_json(Path(profile_path))))
    except LocalExecutionError:
        raise
    except (ValueError, TypeError, KeyError, OSError):
        raise LocalExecutionError("local_asr_runtime_profile_invalid") from None
