"""Synthetic postprocess evidence checks; no models, tokenizer or native worker."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from secretary.application.local_asr_package_adapter import package_document_digest
from secretary.domain.local_asr import RuntimeIdentity, text_fingerprint
from secretary.infrastructure.local_asr_runtime import PinnedRuntimeFile
from test_local_asr_package_adapter import package_inputs


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pilot_result_under_test", ROOT / "scripts/validate_local_asr_pilot_result.py")
postprocess = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(postprocess)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, document):
    data = json.dumps(document, ensure_ascii=False).encode("utf-8")
    path.write_bytes(data)
    return {"path": str(path), "size_bytes": len(data), "sha256": digest(data)}


def pin(path):
    return PinnedRuntimeFile(path=path, sha256=digest(path.read_bytes()))


def test_snapshot_pins_exact_bytes_and_separates_canonical_digest(tmp_path):
    path = tmp_path / "document.json"
    raw = b'{ "value": 1 }\n'
    path.write_bytes(raw)
    document, record = postprocess.read_snapshot(path, digest(raw))
    assert document == {"value": 1}
    assert record == {"path": str(path), "size_bytes": len(raw), "sha256": digest(raw),
                      "canonical_sha256": package_document_digest(document)}
    assert record["sha256"] != record["canonical_sha256"]
    path.write_bytes(b'{ "value": 2 }\n')
    with pytest.raises(postprocess.PilotResultError, match="pin_mismatch"):
        postprocess.read_snapshot(path, digest(raw))


@pytest.mark.parametrize("raw,reason", [
    (b'{"a":1,"a":2}', "duplicate_json_key"),
    (b'{"a":NaN}', "nonfinite"),
    (b'[]', "json_shape"),
])
def test_snapshot_rejects_ambiguous_or_invalid_evidence(tmp_path, raw, reason):
    path = tmp_path / "document.json"
    path.write_bytes(raw)
    with pytest.raises(postprocess.PilotResultError, match=reason):
        postprocess.read_snapshot(path, digest(raw))


def test_snapshot_size_limit_and_create_only_output(tmp_path):
    path = tmp_path / "document.json"
    path.write_bytes(b'{"value":123}')
    with pytest.raises(postprocess.PilotResultError, match="file_size"):
        postprocess.read_snapshot(path, max_bytes=4)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        postprocess.write_create(path, {"replacement": True})
    assert path.read_bytes() == before


def native_case(tmp_path):
    stages = []
    expected = {
        "normalize": ("normalize.json", "normalized.wav"),
        "diarize": ("diarize.json",), "asr_rnnt": ("transcribe.json",),
        "align_rnnt": ("align.json",), "assemble": ("result.json",),
    }
    for stage, names in expected.items():
        outputs = []
        for name in names:
            data = (b"synthetic non-executed audio bytes" if name.endswith(".wav")
                    else b'[{"start":0,"end":1,"speaker":0}]' if name == "diarize.json"
                    else b'{"synthetic":true}')
            (tmp_path / name).write_bytes(data)
            outputs.append({"relative": name, "sha256": digest(data), "size_bytes": len(data)})
        stages.append({"stage": stage, "cleanup_confirmed": True, "outputs": outputs})
    return {"schema_version": "secretary.local-asr.bootstrap-native-receipt.v1", "cleanup_confirmed": True,
            "returncode": 0, "attempt_count": 1, "max_attempts": 1, "failure_code": None,
            "completed_stages": stages}


def test_native_receipt_binds_all_six_original_output_files(tmp_path):
    receipt = native_case(tmp_path)
    before = copy.deepcopy(receipt)
    documents, records = postprocess.verify_native_artifacts(tmp_path, receipt)
    assert set(documents) == {"normalize.json", "normalized.wav", "diarize.json", "transcribe.json", "align.json", "result.json"}
    assert len(records) == 6 and isinstance(documents["normalized.wav"], bytes)
    assert isinstance(documents["diarize.json"], list)
    assert receipt == before


def test_only_diarization_accepts_a_top_level_list(tmp_path):
    receipt = native_case(tmp_path)
    data = b'[{"synthetic":true}]'
    (tmp_path / "transcribe.json").write_bytes(data)
    row = receipt["completed_stages"][2]["outputs"][0]
    row.update(sha256=digest(data), size_bytes=len(data))
    with pytest.raises(postprocess.PilotResultError, match="json_shape"):
        postprocess.verify_native_artifacts(tmp_path, receipt)


@pytest.mark.parametrize("field,value", [
    ("cleanup_confirmed", False), ("returncode", 1), ("returncode", False),
    ("attempt_count", 2), ("max_attempts", 2), ("failure_code", "local_asr_timeout"),
    ("attempt_count", True), ("max_attempts", True),
])
def test_native_receipt_refuses_failure_retry_and_boolean_counts(tmp_path, field, value):
    receipt = native_case(tmp_path)
    receipt[field] = value
    with pytest.raises(postprocess.PilotResultError, match="native_not_successful"):
        postprocess.verify_native_artifacts(tmp_path, receipt)


@pytest.mark.parametrize("mutation,reason", [
    ("order", "stage_set_mismatch"), ("cleanup", "stage_cleanup_unverified"),
    ("extra", "output_set_mismatch"), ("duplicate", "output_set_mismatch"),
    ("missing", "output_set_mismatch"), ("raw", "pin_mismatch"), ("size", "output_size_mismatch"),
])
def test_native_receipt_requires_exact_stage_outputs_and_unchanged_bytes(tmp_path, mutation, reason):
    receipt = native_case(tmp_path)
    stages = receipt["completed_stages"]
    if mutation == "order":
        stages[0], stages[1] = stages[1], stages[0]
    elif mutation == "cleanup":
        stages[2]["cleanup_confirmed"] = False
    elif mutation == "extra":
        stages[0]["outputs"].append({"relative": "../other.json", "size_bytes": 2, "sha256": "a" * 64})
    elif mutation == "duplicate":
        stages[0]["outputs"].append(copy.deepcopy(stages[0]["outputs"][0]))
    elif mutation == "missing":
        stages[0]["outputs"].pop()
    elif mutation == "raw":
        (tmp_path / "transcribe.json").write_bytes(b'{"modified":true}')
    else:
        stages[2]["outputs"][0]["size_bytes"] += 1
    with pytest.raises(postprocess.PilotResultError, match=reason):
        postprocess.verify_native_artifacts(tmp_path, receipt)


def inventory_case(tmp_path):
    source = tmp_path / "source"
    (source / "contracts").mkdir(parents=True)
    model_roots, rows = {}, []
    for role, filename in postprocess.MODEL_MANIFESTS.items():
        metadata = source / "model-metadata" / ("nemotron" if role == "diarizer" else role)
        metadata.mkdir(parents=True)
        model_roots[role] = tmp_path / "models" / role
        config = metadata / "config.json"
        config.write_bytes(b'{"synthetic":true}')
        files = {"config.json": {"size_bytes": config.stat().st_size, "sha256": digest(config.read_bytes())},
                 "model.safetensors" if role == "diarizer" else "pytorch_model.bin":
                     {"size_bytes": 9, "sha256": digest(b"synthetic")}}
        write_json(source / "contracts" / filename, {"files": files})
        if role == "diarizer":
            provenance = metadata / "PROVENANCE.json"
            provenance.write_bytes(b'{"synthetic_provenance":true}')
            files["PROVENANCE.json"] = {"size_bytes": provenance.stat().st_size, "sha256": digest(provenance.read_bytes())}
        rows.extend({"role": role, "path": str(model_roots[role] / name), **entry} for name, entry in files.items())
    pins = {}
    for role in ("launcher", "asr-cpu", "nemotron", "ffmpeg", "delivery"):
        path = tmp_path / (role + ".synthetic")
        path.write_bytes((role + " identity, never executed").encode())
        pins[role] = pin(path)
    profile = SimpleNamespace(identity=SimpleNamespace(profile_digest="b" * 64), source_root=source,
        contracts_root=None, model_roots=model_roots, delivery_manifest=pins["delivery"],
        asr_python=pins["asr-cpu"], nemotron_python=pins["nemotron"], ffmpeg=pins["ffmpeg"])
    authority = SimpleNamespace(profile=profile,
        environment_inventories={role: pins[role] for role in ("launcher", "asr-cpu", "nemotron")})
    document = {"runtime": {
        "schema_version": "secretary.local-asr.inventory-verification.v1", "profile_digest": "b" * 64,
        "qualification_granted": False, "source": {"status": "source_integrity_verified", "verified_files": 3378,
            "canonical_source_files": 65, "package_manifest_sha256": postprocess.PACKAGE_MANIFEST_SHA256,
            "delivery_manifest_sha256": pins["delivery"].sha256},
        "executables": [pins[role].model_dump(mode="json") for role in ("asr-cpu", "nemotron", "ffmpeg")],
        "model_files": rows}, "environments": {role: {"path": str(value.path), "sha256": value.sha256,
            "verified_file_count": 1, "verified_base_file_count": 1} for role, value in authority.environment_inventories.items()}}
    return authority, document


def test_inventory_evidence_preserves_authority_and_never_grants_qualification(tmp_path):
    authority, document = inventory_case(tmp_path)
    before = copy.deepcopy(document)
    assert postprocess.verify_inventory_evidence(authority, document) is None
    assert document == before and document["runtime"]["qualification_granted"] is False


@pytest.mark.parametrize("mutation,reason", [
    ("profile", "inventory_invalid"), ("qualification", "inventory_invalid"),
    ("source", "source_inventory_invalid"), ("environment", "environment_inventory_mismatch"),
    ("executables", "inventory_executables_mismatch"), ("model", "model_inventory_mismatch"),
    ("extra", "model_inventory_extra"), ("duplicate", "model_inventory_extra"),
    ("missing", "model_inventory_incomplete"),
])
def test_inventory_evidence_rejects_drift_and_unclosed_model_rows(tmp_path, mutation, reason):
    authority, document = inventory_case(tmp_path)
    runtime = document["runtime"]
    if mutation == "profile":
        runtime["profile_digest"] = "c" * 64
    elif mutation == "qualification":
        runtime["qualification_granted"] = True
    elif mutation == "source":
        runtime["source"]["verified_files"] = 3377
    elif mutation == "environment":
        document["environments"]["asr-cpu"]["sha256"] = "c" * 64
    elif mutation == "executables":
        runtime["executables"].reverse()
    elif mutation == "model":
        runtime["model_files"][0]["sha256"] = "c" * 64
    elif mutation == "extra":
        runtime["model_files"].append({"role": "rnnt", "path": str(authority.profile.model_roots["rnnt"] / "model.safetensors"),
                                       "size_bytes": 9, "sha256": digest(b"synthetic")})
    elif mutation == "duplicate":
        runtime["model_files"].append(copy.deepcopy(runtime["model_files"][0]))
    else:
        runtime["model_files"].pop()
    with pytest.raises(postprocess.PilotResultError, match=reason):
        postprocess.verify_inventory_evidence(authority, document)


class SyntheticTokenizer:
    def __init__(self):
        self.decoded = {}

    def unk_id(self):
        return 0

    def get_piece_size(self):
        return 10000

    def decode(self, ids):
        return self.decoded[tuple(ids)]


def successful_checker(calls):
    def check(tokenizer, text):
        calls.append(text)
        words = re.findall(r"\S+", text)
        ids = list(range(len(calls) * 100, len(calls) * 100 + len(words)))
        tokenizer.decoded[tuple(ids)] = text
        return words, ids, list(range(len(words)))
    return check


def test_probe_uses_each_exact_window_without_joining_trimming_or_normalizing():
    texts = ["  Первый  фрагмент. ", "Второй\tфрагмент."]
    windows = [{"source_segment_id": "source-" + str(i), "text": text, "text_sha256": text_fingerprint(text)}
               for i, text in enumerate(texts)]
    calls = []
    receipts = postprocess.probe_windows(SyntheticTokenizer(), successful_checker(calls), windows)
    assert calls == texts
    assert [row["text_sha256"] for row in receipts] == [text_fingerprint(text) for text in texts]
    assert [row["ordinal"] for row in receipts] == [0, 1]
    assert all(row["coverage"] is True and row["no_unk"] is True and row["exact_roundtrip"] is True for row in receipts)


@pytest.mark.parametrize("mutation,reason", [("pin", "text_pin_mismatch"), ("coverage", "token_coverage"),
                                             ("unk", "unknown_token"), ("roundtrip", "roundtrip_mismatch")])
def test_probe_refuses_unsupported_text_instead_of_promoting_verification(mutation, reason):
    text = "Два слова"
    window = {"source_segment_id": "source-0", "text": text, "text_sha256": text_fingerprint(text)}
    tokenizer = SyntheticTokenizer()
    if mutation == "pin":
        window["text_sha256"] = "a" * 64
    def checker(value, supplied):
        ids, owners = ([0, 2] if mutation == "unk" else [1, 2]), ([0, 0] if mutation == "coverage" else [0, 1])
        value.decoded[tuple(ids)] = "изменено" if mutation == "roundtrip" else supplied
        return supplied.split(), ids, owners
    with pytest.raises(postprocess.PilotResultError, match=reason):
        postprocess.probe_windows(tokenizer, checker, [window])


def adaptation_case():
    values = package_inputs()
    identity = RuntimeIdentity.model_validate(values["provenance"]["identity"])
    next(model for model in identity.models if model.role == "ctc").processor_sha256 = postprocess.TOKENIZER_SHA256
    profile = SimpleNamespace(identity=identity, tokenizer_compatibility="unverified", enabled=False, qualification_file=None)
    authority = SimpleNamespace(profile=profile, input=SimpleNamespace(sha256="a" * 64))
    documents = {"transcribe.json": values["transcript"], "align.json": values["alignment"], "result.json": values["result"]}
    sources = postprocess.project_sources(documents["transcribe.json"], authority.input.sha256)
    windows = [{"text": source.text, "text_sha256": text_fingerprint(source.text), "source_segment_id": source.segment_id}
               for source in sources]
    probe = {"status": "PASS", "scope": "exact_pilot_window_texts_only", "input_sha256": authority.input.sha256,
        "duration_ms": values["duration_ms"], "identity_sha256": package_document_digest(identity.model_dump(mode="json")),
        "selected_text_sha256": text_fingerprint("\n".join(source.text for source in sources)),
        "source_segments_sha256": package_document_digest([source.model_dump(mode="json") for source in sources]),
        "canonical_artifacts": {name: package_document_digest(document) for name, document in documents.items()},
        "tokenizer_sha256": postprocess.TOKENIZER_SHA256, "no_models_loaded": True, "production_qualified": False,
        "global_tokenizer_qualification": False,
        "windows": postprocess.probe_windows(SyntheticTokenizer(), successful_checker([]), windows)}
    return authority, documents, sources, values["duration_ms"], probe


def test_source_projection_matches_original_bootstrap_ids_and_exact_text():
    authority, documents, sources, _, _ = adaptation_case()
    for index, source in enumerate(sources):
        expected = "local-asr-" + digest(f"bootstrap-pilot:1:{authority.input.sha256}:{index}".encode())[:40]
        assert source.segment_id == expected
        assert source.text == documents["transcribe.json"]["segments"][index]["text"]


def test_adaptation_binds_receipt_without_changing_original_profile_or_identity():
    authority, documents, sources, duration_ms, probe = adaptation_case()
    original_profile, original_documents = copy.deepcopy(authority.profile.__dict__), copy.deepcopy(documents)
    command, proof = postprocess.build_adapted_result(authority, documents, sources, duration_ms, probe)
    assert authority.profile.__dict__ == original_profile and documents == original_documents
    assert command.artifacts.identity == authority.profile.identity == proof.identity
    assert authority.profile.tokenizer_compatibility == "unverified" and authority.profile.enabled is False
    assert proof.tokenizer_compatibility == command.artifacts.alignment.tokenizer_compatibility == "verified"
    assert command.result.model_dump(mode="json") == documents["result.json"]
    assert command.artifacts.runtime_receipts[0].receipt_sha256 == package_document_digest(probe)
    assert command.artifacts.runtime_receipts[-1].receipt_sha256 == package_document_digest(proof.model_dump(mode="json"))


@pytest.mark.parametrize("field,value", [
    ("status", "FAIL"), ("scope", "global"), ("identity_sha256", "e" * 64),
    ("selected_text_sha256", "e" * 64), ("source_segments_sha256", "e" * 64),
    ("canonical_artifacts", {}), ("tokenizer_sha256", "e" * 64),
    ("no_models_loaded", False), ("production_qualified", True), ("global_tokenizer_qualification", True),
    ("input_sha256", "e" * 64), ("duration_ms", 4999), ("windows", []),
])
def test_adaptation_refuses_receipt_binding_mismatch(field, value):
    authority, documents, sources, duration_ms, probe = adaptation_case()
    probe[field] = value
    reason = "probe_windows_mismatch" if field == "windows" else "probe_binding_mismatch"
    with pytest.raises(postprocess.PilotResultError, match=reason):
        postprocess.build_adapted_result(authority, documents, sources, duration_ms, probe)


@pytest.mark.parametrize("field,value", [("text_sha256", "e" * 64), ("source_segment_id", "unrelated"),
    ("ordinal", 8), ("coverage", False), ("no_unk", False), ("exact_roundtrip", False),
    ("word_count", 0), ("token_count", True)])
def test_adaptation_requires_every_probe_window_to_match_the_actual_source(field, value):
    authority, documents, sources, duration_ms, probe = adaptation_case()
    probe["windows"][0][field] = value
    with pytest.raises(postprocess.PilotResultError, match="probe_windows_mismatch"):
        postprocess.build_adapted_result(authority, documents, sources, duration_ms, probe)


@pytest.mark.parametrize("field,value", [("enabled", True), ("tokenizer_compatibility", "verified"),
                                         ("qualification_file", "unexpected")])
def test_adaptation_refuses_changed_original_profile(field, value):
    authority, documents, sources, duration_ms, probe = adaptation_case()
    setattr(authority.profile, field, value)
    with pytest.raises(postprocess.PilotResultError, match="original_profile_changed"):
        postprocess.build_adapted_result(authority, documents, sources, duration_ms, probe)
