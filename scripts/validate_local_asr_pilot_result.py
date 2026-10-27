"""Validate one completed pilot by probing its exact text/tokenizer pair.

Never loads acoustic/diarization weights, changes the runtime profile, retries
inference, or publishes. All derived evidence is create-only in a new .runtime
directory. The existing pilot's blocked outcome remains historical evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import types
import wave

ROOT = Path(__file__).resolve().parents[1]
# The ASR environment intentionally has no application/Pydantic dependencies.
# Its worker mode must remain standard-library + sentencepiece + numpy only.
PROBE_MODE = __name__ == "__main__" and "--probe-request" in sys.argv
if not PROBE_MODE:
    if str(ROOT / "backend") not in sys.path:
        sys.path.insert(0, str(ROOT / "backend"))
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from run_local_asr_pilot import PilotAuthority, verify_environment
    from secretary.application.local_asr_executor import _base_environment
    from secretary.application.local_asr_package_adapter import (
        PackageArtifactProvenance, adapt_package_artifacts, package_document_digest,
    )
    from secretary.domain.local_asr import RuntimeReceipt, SourceSegment, seconds_to_ms, text_fingerprint
    from secretary.infrastructure.local_asr_process import run_owned_stage
    from secretary.infrastructure.local_asr_runtime import (
        MODEL_MANIFESTS, PACKAGE_MANIFEST_SHA256, PinnedRuntimeFile, PortableLocalAsrRuntime,
        _plain, runtime_file_digest,
    )
else:
    def _plain(path, *, directory=False):
        require(path.is_absolute() and ".." not in path.parts and not str(path).startswith(("\\\\", "//")),
                "pilot_result_path_invalid")
        for item in (*reversed(path.parents), path):
            info = item.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                    "pilot_result_path_linked")
        require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                "pilot_result_path_invalid")
        return info

    def runtime_file_digest(path):
        return read_bytes(path)[1]["sha256"]

    def package_document_digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()

    def text_fingerprint(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

MIB = 1024 ** 2
CHECKER_SHA256 = "300cbac108839e0b44b999230e18ea1c155702026f38cc8c6c2ec8fea89f7668"
TOKENIZER_SHA256 = "0b9a1960898fbfdf5424ab852ea17445eb3da960fba23e977ff100eb0054fbc8"
TOKENIZER_BYTES = 240941
STAGE_OUTPUTS = {
    "normalize": {"normalize.json", "normalized.wav"}, "diarize": {"diarize.json"},
    "asr_rnnt": {"transcribe.json"}, "align_rnnt": {"align.json"}, "assemble": {"result.json"},
}
SCOPE = "exact_pilot_window_texts_only"


class PilotResultError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise PilotResultError(code)


def read_bytes(path, *, expected_sha256=None, max_bytes=8 * MIB):
    path = Path(path)
    before = _plain(path)
    require(0 < before.st_size <= max_bytes, "pilot_result_file_size")
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        data = source.read(max_bytes + 1)
        finished = os.fstat(source.fileno())
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)
    require(signature(before) == signature(opened) == signature(finished) == signature(_plain(path))
            and len(data) == before.st_size, "pilot_result_file_changed")
    digest = hashlib.sha256(data).hexdigest()
    require(expected_sha256 is None or digest == expected_sha256, "pilot_result_pin_mismatch")
    return data, dict(path=str(path), sha256=digest, size_bytes=len(data))


def read_snapshot(path, expected_sha256=None, max_bytes=8 * MIB, *, allow_list=False):
    data, record = read_bytes(path, expected_sha256=expected_sha256, max_bytes=max_bytes)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "pilot_result_duplicate_json_key")
            result[key] = value
        return result
    document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(PilotResultError("pilot_result_nonfinite")))
    require(isinstance(document, dict) or allow_list and isinstance(document, list), "pilot_result_json_shape")
    record["canonical_sha256"] = package_document_digest(document)
    return document, record


def write_create(path, document):
    data = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
    require(len(data) <= 16 * MIB, "pilot_result_output_size")
    with Path(path).open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return hashlib.sha256(data).hexdigest()


def verify_native_artifacts(pilot_root, native_receipt):
    _plain(pilot_root, directory=True)
    require(native_receipt.get("schema_version") == "secretary.local-asr.bootstrap-native-receipt.v1"
            and native_receipt.get("cleanup_confirmed") is True
            and type(native_receipt.get("returncode")) is int and native_receipt["returncode"] == 0
            and type(native_receipt.get("attempt_count")) is int and type(native_receipt.get("max_attempts")) is int
            and native_receipt.get("attempt_count") == native_receipt.get("max_attempts") == 1
            and native_receipt.get("failure_code") is None, "pilot_result_native_not_successful")
    stages = native_receipt.get("completed_stages")
    require(isinstance(stages, list) and [stage.get("stage") for stage in stages] == list(STAGE_OUTPUTS),
            "pilot_result_stage_set_mismatch")
    documents, records = {}, []
    for stage in stages:
        require(stage.get("cleanup_confirmed") is True, "pilot_result_stage_cleanup_unverified")
        outputs = stage.get("outputs")
        require(isinstance(outputs, list) and all(isinstance(row, dict) for row in outputs),
                "pilot_result_output_set_mismatch")
        names = [row.get("relative") for row in outputs]
        require(len(names) == len(set(names)) and set(names) == STAGE_OUTPUTS[stage["stage"]],
                "pilot_result_output_set_mismatch")
        for row in outputs:
            name = row["relative"]
            require(re.fullmatch(r"[a-f0-9]{64}", str(row.get("sha256"))) is not None
                    and type(row.get("size_bytes")) is int, "pilot_result_output_pin_invalid")
            if name == "normalized.wav":
                document, record = read_bytes(pilot_root / name, expected_sha256=row["sha256"], max_bytes=2 * MIB)
            else:
                document, record = read_snapshot(pilot_root / name, row["sha256"], allow_list=name == "diarize.json")
            require(record["size_bytes"] == row["size_bytes"], "pilot_result_output_size_mismatch")
            documents[name] = document
            records.append(record)
    return documents, records


def verify_inventory_evidence(authority, document):
    profile = authority.profile
    runtime = document.get("runtime", {})
    require(runtime.get("schema_version") == "secretary.local-asr.inventory-verification.v1"
            and runtime.get("profile_digest") == profile.identity.profile_digest
            and runtime.get("qualification_granted") is False, "pilot_result_inventory_invalid")
    source = runtime.get("source", {})
    require(source.get("status") == "source_integrity_verified" and source.get("verified_files") == 3378
            and source.get("canonical_source_files") == 65
            and source.get("package_manifest_sha256") == PACKAGE_MANIFEST_SHA256
            and source.get("delivery_manifest_sha256") == profile.delivery_manifest.sha256,
            "pilot_result_source_inventory_invalid")
    expected_executables = [pin.model_dump(mode="json") for pin in
                            (profile.asr_python, profile.nemotron_python, profile.ffmpeg)]
    require(runtime.get("executables") == expected_executables, "pilot_result_inventory_executables_mismatch")
    environments = document.get("environments", {})
    require(set(environments) == set(authority.environment_inventories), "pilot_result_environment_inventory_missing")
    for role, pin in authority.environment_inventories.items():
        row = environments[role]
        require(row.get("path") == str(pin.path) and row.get("sha256") == pin.sha256
                and type(row.get("verified_file_count")) is int and row["verified_file_count"] > 0
                and type(row.get("verified_base_file_count")) is int and row["verified_base_file_count"] > 0,
                "pilot_result_environment_inventory_mismatch")
    rows = runtime.get("model_files")
    require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "pilot_result_model_inventory_invalid")
    expected, required = {}, set()
    for role, filename in MODEL_MANIFESTS.items():
        manifest, _ = read_snapshot((profile.contracts_root or profile.source_root) / "contracts" / filename)
        for name, entry in manifest["files"].items():
            key = (role, str(profile.model_roots[role] / name))
            expected[key] = entry
            required.add(key)
        metadata = profile.source_root / "model-metadata" / ("nemotron" if role == "diarizer" else role)
        for path in metadata.iterdir():
            expected[(role, str(profile.model_roots[role] / path.name))] = dict(
                size_bytes=_plain(path).st_size, sha256=runtime_file_digest(path))
        if role == "diarizer":
            required.add((role, str(profile.model_roots[role] / "PROVENANCE.json")))
    actual = {}
    for row in rows:
        key = (row.get("role"), row.get("path"))
        require(key not in actual and key in expected, "pilot_result_model_inventory_extra")
        require({key: row.get(key) for key in ("size_bytes", "sha256")} == expected[key],
                "pilot_result_model_inventory_mismatch")
        actual[key] = row
    require(required <= set(actual), "pilot_result_model_inventory_incomplete")


def project_sources(transcript, input_sha256):
    windows = transcript.get("segments")
    require(isinstance(windows, list) and 0 < len(windows) <= 128, "pilot_result_windows_invalid")
    sources = []
    for index, window in enumerate(windows):
        require(isinstance(window, dict) and isinstance(window.get("text"), str), "pilot_result_window_invalid")
        identifier = hashlib.sha256(f"bootstrap-pilot:1:{input_sha256}:{index}".encode()).hexdigest()[:40]
        sources.append(SourceSegment(segment_id="local-asr-" + identifier, text=window["text"], channel="import",
            started_ms=seconds_to_ms(window["start"]), ended_ms=seconds_to_ms(window["end"])))
    require(sum(len(source.text) for source in sources) <= 65536, "pilot_result_text_too_large")
    return sources


def probe_windows(tokenizer, checker, windows):
    require(isinstance(windows, list) and 0 < len(windows) <= 128, "pilot_result_windows_invalid")
    results = []
    for index, window in enumerate(windows):
        text = window["text"]
        require(isinstance(text, str) and text_fingerprint(text) == window["text_sha256"], "pilot_result_text_pin_mismatch")
        words, ids, owners = checker(tokenizer, text)
        require(words == re.findall(r"\S+", text) and bool(ids) and len(ids) == len(owners)
                and set(owners) == set(range(len(words))), "pilot_result_token_coverage")
        require(tokenizer.unk_id() not in ids and all(0 <= item < tokenizer.get_piece_size() for item in ids),
                "pilot_result_unknown_token")
        require(tokenizer.decode(ids) == text, "pilot_result_roundtrip_mismatch")
        results.append(dict(ordinal=index, source_segment_id=window["source_segment_id"], text_sha256=window["text_sha256"],
            word_count=len(words), token_count=len(ids), token_ids_owners_sha256=package_document_digest([ids, owners]),
            coverage=True, no_unk=True, exact_roundtrip=True))
    return results


def probe_worker(request_path, request_sha256):
    request, _ = read_snapshot(request_path, request_sha256)
    require(request.get("schema_version") == "secretary.local-asr.tokenizer-probe-request.v1", "pilot_result_probe_schema")
    require(runtime_file_digest(Path(__file__).absolute()) == request["validator_sha256"], "pilot_result_validator_changed")
    require(request["checker"]["sha256"] == CHECKER_SHA256 and request["tokenizer"]["sha256"] == TOKENIZER_SHA256,
            "pilot_result_checker_identity")
    code, _ = read_bytes(Path(request["checker"]["path"]), expected_sha256=CHECKER_SHA256, max_bytes=MIB)
    tokenizer_data, _ = read_bytes(Path(request["tokenizer"]["path"]), expected_sha256=TOKENIZER_SHA256,
                                   max_bytes=TOKENIZER_BYTES)
    require(len(tokenizer_data) == TOKENIZER_BYTES, "pilot_result_tokenizer_size")
    module = types.ModuleType("verified_pilot_ctc_tokenizer_checker")
    module.__file__ = request["checker"]["path"]
    sys.modules[module.__name__] = module
    exec(compile(code, module.__file__, "exec"), module.__dict__)
    import sentencepiece
    tokenizer = sentencepiece.SentencePieceProcessor(model_proto=tokenizer_data)
    results = probe_windows(tokenizer, module._target_tokens, request["windows"])
    forbidden = ("torch", "torchaudio", "transformers", "modeling_gigaam")
    absent = {name: not any(key == name or key.startswith(name + ".") for key in sys.modules) for name in forbidden}
    require(all(absent.values()), "pilot_result_model_module_imported")
    write_create(request_path.parent / "probe-result.json", dict(
        schema_version="secretary.local-asr.tokenizer-probe-worker.v1", status="PASS", windows=results,
        request_sha256=request_sha256, sentencepiece_version=sentencepiece.__version__,
        no_models_loaded=True, model_modules_absent=absent))
    return 0


def build_adapted_result(authority, documents, sources, duration_ms, probe_receipt):
    identity = authority.profile.identity
    canonical = {name: package_document_digest(documents[name]) for name in ("transcribe.json", "align.json", "result.json")}
    source_digest = package_document_digest([source.model_dump(mode="json") for source in sources])
    require(probe_receipt.get("status") == "PASS" and probe_receipt.get("scope") == SCOPE
            and probe_receipt.get("input_sha256") == authority.input.sha256
            and type(probe_receipt.get("duration_ms")) is int and probe_receipt["duration_ms"] == duration_ms
            and probe_receipt.get("identity_sha256") == package_document_digest(identity.model_dump(mode="json"))
            and probe_receipt.get("selected_text_sha256") == text_fingerprint("\n".join(source.text for source in sources))
            and probe_receipt.get("source_segments_sha256") == source_digest
            and probe_receipt.get("canonical_artifacts") == canonical
            and probe_receipt.get("tokenizer_sha256") == TOKENIZER_SHA256
            and probe_receipt.get("no_models_loaded") is True
            and probe_receipt.get("production_qualified") is False
            and probe_receipt.get("global_tokenizer_qualification") is False, "pilot_result_probe_binding_mismatch")
    windows = probe_receipt.get("windows")
    require(isinstance(windows, list) and len(windows) == len(sources), "pilot_result_probe_windows_mismatch")
    for index, (window, source) in enumerate(zip(windows, sources)):
        require(isinstance(window, dict) and type(window.get("ordinal")) is int and window["ordinal"] == index
                and window.get("source_segment_id") == source.segment_id
                and window.get("text_sha256") == text_fingerprint(source.text)
                and type(window.get("word_count")) is int and window["word_count"] == len(re.findall(r"\S+", source.text))
                and type(window.get("token_count")) is int and window["token_count"] >= window["word_count"] > 0
                and re.fullmatch(r"[a-f0-9]{64}", str(window.get("token_ids_owners_sha256"))) is not None
                and all(window.get(key) is True for key in ("coverage", "no_unk", "exact_roundtrip")),
                "pilot_result_probe_windows_mismatch")
    require(authority.profile.tokenizer_compatibility == "unverified" and not authority.profile.enabled
            and authority.profile.qualification_file is None, "pilot_result_original_profile_changed")
    proof = PackageArtifactProvenance(text_origin="rnnt", input_sha256=authority.input.sha256, identity=identity,
        source_segments_sha256=source_digest, transcript_sha256=canonical["transcribe.json"],
        alignment_sha256=canonical["align.json"], result_sha256=canonical["result.json"], tokenizer_compatibility="verified",
        runtime_receipts=[RuntimeReceipt(stage="tokenizer_compatibility_probe", status="succeeded",
                                        receipt_sha256=package_document_digest(probe_receipt))])
    command = adapt_package_artifacts(operation_id="postprocess-" + package_document_digest(probe_receipt)[:40],
        meeting_id="bootstrap-pilot", transcript_version=1, duration_ms=duration_ms, source_segments=sources,
        transcript=documents["transcribe.json"], alignment=documents["align.json"], result=documents["result.json"], provenance=proof)
    require(command.artifacts.identity == identity, "pilot_result_identity_changed")
    return command, proof


def validate_result(authority_pin, pilot_root, native_sha256, inventory_after_sha256, output):
    authority_document, authority_record = read_snapshot(authority_pin.path, authority_pin.sha256)
    authority = PilotAuthority.model_validate(authority_document)
    require(authority.profile.tokenizer_compatibility == "unverified", "pilot_result_original_profile_changed")
    require(Path(sys.executable).absolute() == authority.launcher_python.path
            and runtime_file_digest(authority.launcher_python.path) == authority.launcher_python.sha256
            and not os.environ.get("SECRETARY_LOCAL_ASR_PROFILE"), "pilot_result_launcher_mismatch")
    require(runtime_file_digest(ROOT / "scripts/run_local_asr_pilot.py") == authority.pilot_runner_sha256,
            "pilot_result_original_runner_changed")
    _plain(pilot_root, directory=True)
    source_authority, _ = read_snapshot(pilot_root / "bootstrap-authority.json")
    source_pin, _ = read_snapshot(pilot_root / "authority-pin.json")
    require(source_authority == authority.model_dump(mode="json") and source_pin == authority_pin.model_dump(mode="json"),
            "pilot_result_authority_mismatch")
    # Source and runtime identity only: this constructor never reads model weights.
    PortableLocalAsrRuntime(authority.profile)
    native, native_record = read_snapshot(pilot_root / "native-receipt.json", native_sha256)
    inventory, inventory_record = read_snapshot(pilot_root / "inventory-after.json", inventory_after_sha256)
    verify_inventory_evidence(authority, inventory)
    documents, raw_records = verify_native_artifacts(pilot_root, native)
    outcome, outcome_record = read_snapshot(pilot_root / "pilot-outcome.json")
    require(outcome.get("status") in {"inference_succeeded_adapter_blocked", "bounded_pilot_succeeded"}
            and outcome.get("input_sha256") == authority.input.sha256
            and type(outcome.get("attempt_count")) is int and outcome["attempt_count"] == 1
            and outcome.get("production_qualified") is False and outcome.get("application_publication") is False
            and (outcome["status"] != "inference_succeeded_adapter_blocked"
                 or outcome.get("failure_code") == "package_tokenizer_not_verified"), "pilot_result_original_outcome_invalid")
    with wave.open(io.BytesIO(documents["normalized.wav"]), "rb") as audio:
        require((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) == (1, 2, 16000, "NONE"),
                "pilot_result_audio_format_invalid")
        frames = audio.getnframes()
        require(0 < frames <= 30 * 16000 and len(audio.readframes(frames)) == frames * 2, "pilot_result_audio_duration_invalid")
        duration_ms = seconds_to_ms(frames / 16000)
    require(duration_ms == outcome.get("duration_ms") == seconds_to_ms(documents["normalize.json"]["media_duration_seconds"]),
            "pilot_result_duration_mismatch")
    require(runtime_file_digest(authority.input.path, max_bytes=2 * MIB) == authority.input.sha256,
            "pilot_result_input_pin_mismatch")
    environment = verify_environment(authority.environment_inventories["asr-cpu"], authority.profile.asr_python)
    require(environment == inventory["environments"]["asr-cpu"], "pilot_result_environment_changed")
    launcher_environment = verify_environment(authority.environment_inventories["launcher"], authority.launcher_python)
    require(launcher_environment == inventory["environments"]["launcher"], "pilot_result_launcher_environment_changed")
    sources = project_sources(documents["transcribe.json"], authority.input.sha256)
    ctc = next(model for model in authority.profile.identity.models if model.role == "ctc")
    require(ctc.processor_sha256 == TOKENIZER_SHA256 and ctc.loader_sha256 == CHECKER_SHA256,
            "pilot_result_ctc_identity_mismatch")
    checker = authority.profile.source_root / "scripts/dev_gigaam_ctc_alignment.py"
    tokenizer = authority.profile.model_roots["ctc"] / "tokenizer.model"
    validator_digest = runtime_file_digest(Path(__file__).absolute())
    output = Path(output).absolute()
    require(output.parent == ROOT / ".runtime" and not output.exists()
            and not output.is_relative_to(pilot_root) and not output.is_relative_to(authority.profile.source_root),
            "pilot_result_output_path_invalid")
    _plain(output.parent, directory=True)
    output.mkdir()
    request = dict(schema_version="secretary.local-asr.tokenizer-probe-request.v1", validator_sha256=validator_digest,
        checker=dict(path=str(checker), sha256=CHECKER_SHA256), tokenizer=dict(path=str(tokenizer), sha256=TOKENIZER_SHA256),
        windows=[dict(source_segment_id=source.segment_id, text=source.text, text_sha256=text_fingerprint(source.text))
                 for source in sources])
    request_path = output / "probe-request.json"
    request_sha = write_create(request_path, request)
    native_probe = run_owned_stage([str(authority.profile.asr_python.path), "-I", "-B", str(Path(__file__).absolute()),
        "--probe-request", str(request_path), "--probe-request-sha256", request_sha],
        environment=_base_environment(output), cwd=str(output), timeout_seconds=60,
        stdout_limit=65536, stderr_limit=65536, job_commit_limit_bytes=2048 * MIB,
        working_set_limit_bytes=1024 * MIB, owned_rss_limit_bytes=1024 * MIB)
    require(native_probe.returncode == 0 and native_probe.cleanup_confirmed is True, "pilot_result_probe_native_failed")
    worker, worker_record = read_snapshot(output / "probe-result.json")
    absent = worker.get("model_modules_absent", {})
    require(worker.get("status") == "PASS" and worker.get("request_sha256") == request_sha
            and worker.get("no_models_loaded") is True and set(absent) == {"torch", "torchaudio", "transformers", "modeling_gigaam"}
            and all(value is True for value in absent.values())
            and len(worker.get("windows", [])) == len(sources), "pilot_result_probe_failed")
    # A tokenizer subprocess is the only new native operation. Retain its proof separately.
    receipt = dict(schema_version="secretary.local-asr.tokenizer-compatibility.v1", scope=SCOPE, status="PASS",
        authority=authority_record, native_inference_receipt=native_record, inventory_after=inventory_record,
        original_outcome=outcome_record, original_profile_tokenizer_compatibility="unverified",
        input_sha256=authority.input.sha256, duration_ms=duration_ms, identity_sha256=package_document_digest(authority.profile.identity.model_dump(mode="json")),
        profile_digest=authority.profile.identity.profile_digest, code_digest=authority.profile.identity.code_digest,
        validator_sha256=validator_digest, checker_sha256=CHECKER_SHA256, tokenizer_sha256=TOKENIZER_SHA256,
        tokenizer_size_bytes=TOKENIZER_BYTES, environment_inventory=authority.environment_inventories["asr-cpu"].model_dump(mode="json"),
        launcher_environment_inventory=authority.environment_inventories["launcher"].model_dump(mode="json"),
        artifacts=raw_records, canonical_artifacts={name: package_document_digest(documents[name]) for name in ("transcribe.json", "align.json", "result.json")},
        source_segments_sha256=package_document_digest([source.model_dump(mode="json") for source in sources]),
        selected_text_sha256=text_fingerprint("\n".join(source.text for source in sources)), windows=worker["windows"],
        probe_result=worker_record, sentencepiece_version=worker["sentencepiece_version"],
        native_probe=dict(returncode=0, cleanup_confirmed=True, peak_owned_rss_bytes=native_probe.peak_owned_rss_bytes,
                          deadline_seconds=60, job_commit_limit_bytes=2048 * MIB),
        no_models_loaded=True, model_modules_absent=worker["model_modules_absent"], inference_run=False,
        production_qualified=False, global_tokenizer_qualification=False, application_publication=False)
    command, proof = build_adapted_result(authority, documents, sources, duration_ms, receipt)
    require(runtime_file_digest(Path(__file__).absolute()) == validator_digest, "pilot_result_validator_changed")
    # Original native outputs are never rewritten; verify them again before committing the derived files.
    verify_native_artifacts(pilot_root, native)
    write_create(output / "tokenizer-compatibility.json", receipt)
    write_create(output / "package-provenance.json", proof.model_dump(mode="json"))
    write_create(output / "validated-import.json", command.model_dump(mode="json"))
    write_create(output / "postprocess-outcome.json", dict(schema_version="secretary.local-asr.pilot-postprocess.v1",
        status="one_pilot_result_validated", input_sha256=authority.input.sha256, duration_ms=duration_ms,
        tokenizer_receipt_sha256=package_document_digest(receipt), original_profile_unchanged=True,
        original_identity_unchanged=True, original_outcome_unchanged=True, no_models_loaded=True,
        inference_run=False, production_qualified=False, global_tokenizer_qualification=False, application_publication=False))
    return output


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-request", type=Path)
    parser.add_argument("--probe-request-sha256")
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--authority-sha256")
    parser.add_argument("--pilot-root", type=Path)
    parser.add_argument("--native-receipt-sha256")
    parser.add_argument("--inventory-after-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(arguments)
    try:
        if args.probe_request is not None:
            require(args.probe_request_sha256 is not None, "pilot_result_probe_pin_required")
            return probe_worker(args.probe_request, args.probe_request_sha256)
        require(all(value is not None for value in (args.authority, args.authority_sha256, args.pilot_root,
                args.native_receipt_sha256, args.inventory_after_sha256, args.output)), "pilot_result_arguments_missing")
        output = validate_result(PinnedRuntimeFile(path=args.authority, sha256=args.authority_sha256), args.pilot_root,
                                 args.native_receipt_sha256, args.inventory_after_sha256, args.output)
        print(json.dumps(dict(status="one_pilot_result_validated", output=str(output))))
        return 0
    except Exception as error:
        code = str(error)
        print(code if re.fullmatch(r"[a-z0-9_]+", code) else "pilot_result_validation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
