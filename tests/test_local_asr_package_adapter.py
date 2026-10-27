"""Package-shaped synthetic evidence only; no donor/runtime/model imports."""
import copy
import json
from pathlib import Path

import pytest

from secretary.application.local_asr_package_adapter import (
    PackageAdapterError, PackageArtifactProvenance, adapt_package_artifacts,
    package_document_digest,
)
from secretary.domain.local_asr import ImportLocalAsrRun, SourceSegment
from secretary.infrastructure.database import Database, now
from secretary.infrastructure.local_asr_repository import LocalAsrRepository, initialize_local_asr_schema


def package_inputs():
    original = json.loads((Path(__file__).parent / "fixtures/local_asr/synthetic_run.json").read_text("utf-8"))
    sources = original["artifacts"]["source_segments"]
    windows = [{"start": s["started_ms"] / 1000, "end": s["ended_ms"] / 1000,
                "text": s["text"]} for s in sources]
    aligned = copy.deepcopy(windows)
    for i, window in enumerate(aligned):
        window["words"] = [{"start": w["started_ms"] / 1000, "end": w["ended_ms"] / 1000,
                            "word": w["text"]} for w in original["artifacts"]["words"][i * 2:i * 2 + 2]]
    identity = original["artifacts"]["identity"]
    identity["models"] = [dict(identity["models"][0], role=role, model_id=f"synthetic/{role}")
                          for role in ("rnnt", "ctc", "diarizer")]
    return {
        "operation_id": "package-import", "meeting_id": "meeting", "transcript_version": 1,
        "duration_ms": 5000, "source_segments": [SourceSegment.model_validate(s) for s in sources],
        "transcript": {"segments": windows, "provider": "gigaam-v3-e2e-rnnt", "language": "ru"},
        "alignment": {"segments": aligned, "provider": "gigaam-v3-e2e-rnnt+gigaam-ctc",
                      "diarization_provider": "nemotron-3-diarization",
                      "metrics": copy.deepcopy(original["result"]["metrics"])},
        "result": original["result"],
        "provenance": {"text_origin": "saved_transcript", "identity": identity,
                       "input_sha256": "a" * 64, "tokenizer_compatibility": "verified",
                       "runtime_receipts": []},
    }


def bind(values):
    proof = values["provenance"]
    proof.update(
        source_segments_sha256=package_document_digest([s.model_dump(mode="json") for s in values["source_segments"]]),
        transcript_sha256=package_document_digest(values["transcript"]),
        alignment_sha256=package_document_digest(values["alignment"]),
        result_sha256=package_document_digest(values["result"]),
    )
    return {**values, "provenance": PackageArtifactProvenance.model_validate(proof)}


def test_exact_saved_text_projection_words_raw_overlap_and_composite_survive():
    inputs = bind(package_inputs())
    before = copy.deepcopy(inputs)
    command = adapt_package_artifacts(**inputs)
    assert inputs == before
    assert command.result.model_dump(mode="json") == inputs["result"]
    assert len(command.artifacts.words) == 10
    assert command.artifacts.alignment.status == "complete"
    assert command.artifacts.speaker_kinds["Спикер неизвестен"] == "unknown"
    assert command.artifacts.speaker_kinds["Спикер 1 + Спикер 2"] == "composite"
    assert command.result.speaker_turns[3].started_ms == command.result.speaker_turns[4].started_ms
    assert command.artifacts.words[2].char_start == 17
    assert command.artifacts.words[2].started_ms == 1000
    receipt = command.artifacts.runtime_receipts[-1]
    assert receipt.stage == "package_adapter_validation"
    assert receipt.receipt_sha256 == package_document_digest(inputs["provenance"].model_dump(mode="json"))
    assert ImportLocalAsrRun.model_validate_json(command.model_dump_json()) == command


def test_absolute_offset_is_not_added_twice_and_half_up_rounding_is_explicit():
    values = package_inputs()
    values["alignment"]["segments"][1]["words"][0]["start"] = 1.0005
    command = adapt_package_artifacts(**bind(values))
    assert command.artifacts.words[2].started_ms == 1001
    assert command.artifacts.words[2].ended_ms == 1390


@pytest.mark.parametrize("missing", [None, []])
def test_complete_fallback_retains_every_original_interval(missing):
    values = package_inputs()
    values["provenance"]["tokenizer_compatibility"] = "unverified"
    for window in values["alignment"]["segments"]:
        window["words"] = missing
    command = adapt_package_artifacts(**bind(values))
    assert command.artifacts.words == []
    assert command.artifacts.alignment.status == "unavailable"
    assert command.artifacts.alignment.unsupported_token_indices == list(range(10))
    assert command.artifacts.fallback_counters == {"ctc_fallback_windows": 5}
    assert command.result.model_dump(mode="json") == values["result"]


def test_partial_fallback_does_not_invent_timestamps_for_missing_window():
    values = package_inputs()
    del values["alignment"]["segments"][1]["words"]
    command = adapt_package_artifacts(**bind(values))
    assert command.artifacts.alignment.status == "partial"
    assert command.artifacts.alignment.unsupported_token_indices == [2, 3]
    assert [w.token_index for w in command.artifacts.words] == [0, 1, 4, 5, 6, 7, 8, 9]


@pytest.mark.parametrize("part", ["transcript", "alignment", "result"])
def test_document_provenance_mismatch_is_rejected(part):
    inputs = bind(package_inputs())
    inputs[part]["warnings"] = ["tampered"]
    with pytest.raises(PackageAdapterError, match="artifact_provenance"):
        adapt_package_artifacts(**inputs)


def test_source_provenance_covers_ids_channel_and_original_timing():
    inputs = bind(package_inputs())
    inputs["source_segments"][0] = inputs["source_segments"][0].model_copy(update={"channel": "loopback"})
    with pytest.raises(PackageAdapterError, match="source_provenance"):
        adapt_package_artifacts(**inputs)


@pytest.mark.parametrize("change,code", [
    (lambda v: v["result"].update(input_sha256="b" * 64), "input_provenance"),
    (lambda v: v["result"].pop("speaker_turns"), "independent_turns"),
    (lambda v: v["result"]["metrics"].update(experimental_nemotron_turn_end_pad_ms=100), "modified_raw_turns"),
    (lambda v: v["alignment"]["metrics"].update(ctc_selector_policy="selector"), "text_selector"),
    (lambda v: v["alignment"]["segments"][0].update(transcript_source="ctc"), "text_selector"),
    (lambda v: v["alignment"]["segments"][0].update(text="replaced"), "changed_source_text"),
    (lambda v: v["transcript"]["segments"][0].update(text="replaced"), "changed_source_text"),
    (lambda v: v["alignment"]["segments"][0].update(end=0.7), "changed_source_interval"),
    (lambda v: v["alignment"]["segments"][0]["words"][0].update(word="replaced"), "word_token_mismatch"),
    (lambda v: v["alignment"]["segments"][1]["words"][0].update(start=0.0), "word_outside_window"),
    (lambda v: v["alignment"]["segments"][0]["words"][0].update(start=True), "invalid_seconds"),
    (lambda v: v["alignment"]["segments"][0]["words"][0].update(end=0.0001), "interval_collapsed"),
    (lambda v: v["provenance"].update(tokenizer_compatibility="unsupported"), "tokenizer_not_verified"),
    (lambda v: v["provenance"]["identity"].update(asr_device=None, diarization_device=None), "stage_devices"),
    (lambda v: v["provenance"]["identity"]["models"].pop(), "complete_model_identity"),
    (lambda v: v["alignment"]["segments"].pop(), "source_window_mapping"),
    (lambda v: v["transcript"].update(provider="whisper"), "provider_mismatch"),
    (lambda v: v["alignment"].update(provider="gigaam-v3-e2e-rnnt+gigaam-v3-e2e-ctc"), "provider_mismatch"),
    (lambda v: v["alignment"].update(diarization_provider="not_run"), "provider_mismatch"),
    (lambda v: v["result"].update(provider="other"), "provider_mismatch"),
    (lambda v: v["alignment"]["metrics"].update(ctc_fallback_windows=1), "bridge_metrics_mismatch"),
    (lambda v: v["result"]["metrics"].clear(), "bridge_metrics_mismatch"),
    (lambda v: v["alignment"].update(warnings=["ctc_alignment_fallback_windows=1"]), "bridge_warnings_mismatch"),
    (lambda v: v["result"].update(warnings=["invented"]), "bridge_warnings_mismatch"),
    (lambda v: v["provenance"]["runtime_receipts"].append({"stage": "package_adapter_validation",
        "receipt_sha256": "b" * 64, "status": "succeeded"}), "reserved_receipt_stage"),
])
def test_rejects_incompatible_or_unqualified_evidence(change, code):
    values = package_inputs()
    change(values)
    with pytest.raises(PackageAdapterError, match=code):
        adapt_package_artifacts(**bind(values))


def test_bridge_punctuation_join_must_not_silently_rewrite_token_stream():
    values = package_inputs()
    source = values["source_segments"][0]
    values["source_segments"][0] = source.model_copy(update={"text": "Первый ,"})
    for payload in (values["transcript"], values["alignment"]):
        payload["segments"][0]["text"] = "Первый ,"
    values["alignment"]["segments"][0]["words"][1]["word"] = ","
    values["result"]["segment_items"][0]["text"] = "Первый,"
    with pytest.raises(PackageAdapterError, match="bridge_changed_source_tokens"):
        adapt_package_artifacts(**bind(values))


def test_unknown_source_timing_is_not_guessed_from_package():
    values = package_inputs()
    values["source_segments"][0] = values["source_segments"][0].model_copy(
        update={"started_ms": None, "ended_ms": None})
    with pytest.raises(PackageAdapterError, match="source_window_mapping"):
        adapt_package_artifacts(**bind(values))


def test_fallback_cannot_merge_or_change_original_interval():
    values = package_inputs()
    del values["alignment"]["segments"][0]["words"]
    values["result"]["segment_items"][0]["ended_ms"] = 790
    with pytest.raises(PackageAdapterError, match="fallback_requires_original_segment"):
        adapt_package_artifacts(**bind(values))


def test_human_name_is_not_converted_to_anonymous_confirmed_speaker():
    values = package_inputs()
    values["result"]["segment_items"][0]["speaker_label"] = "Иван"
    with pytest.raises(PackageAdapterError, match="noncanonical_speaker_label"):
        adapt_package_artifacts(**bind(values))


def test_json_digest_rejects_nan_and_is_order_independent():
    assert package_document_digest({"a": 1, "b": 2}) == package_document_digest({"b": 2, "a": 1})
    with pytest.raises(PackageAdapterError, match="invalid_json"):
        package_document_digest({"value": float("nan")})


@pytest.mark.parametrize("key,value", [("ctc_fallback_windows", 1), ("ctc_fallback_windows", False),
                                      ("aligned_words", 9), ("aligned_words", 10.0)])
def test_matching_but_false_alignment_counters_are_rejected(key, value):
    values = package_inputs()
    for payload in (values["alignment"], values["result"]):
        payload["metrics"][key] = value
    with pytest.raises(PackageAdapterError, match="alignment_counter_mismatch"):
        adapt_package_artifacts(**bind(values))


def test_diagnostics_warning_truncation_and_proof_binding_survive_export():
    values = package_inputs()
    values["alignment"]["warnings"] = ["x" * 550, "ctc_alignment_fallback_windows=0"]
    values["result"]["warnings"] = ["x" * 500, "ctc_alignment_fallback_windows=0"]
    for payload in (values["alignment"], values["result"]):
        payload["metrics"].update(ctc_fallback_windows=0, aligned_words=10,
                                  ctc_gap_edges={"policy": "gap_edges", "changed_words": 0})
    inputs = bind(values)
    command = adapt_package_artifacts(**inputs)
    restored = ImportLocalAsrRun.model_validate_json(command.model_dump_json())
    assert restored.result.metrics == values["alignment"]["metrics"]
    assert restored.result.warnings == values["result"]["warnings"]
    assert restored.artifacts.runtime_receipts[-1].receipt_sha256 == package_document_digest(
        inputs["provenance"].model_dump(mode="json"))
    modified = inputs["provenance"].model_dump(mode="json")
    modified["text_origin"] = "rnnt"
    assert package_document_digest(modified) != restored.artifacts.runtime_receipts[-1].receipt_sha256


def test_provenance_reserves_one_receipt_slot_for_adapter_validation():
    values = package_inputs()
    values["provenance"]["runtime_receipts"] = [{"stage": "synthetic", "receipt_sha256": "c" * 64,
                                               "status": "not_run"}] * 999
    command = adapt_package_artifacts(**bind(values))
    assert len(command.artifacts.runtime_receipts) == 1000
    values["provenance"]["runtime_receipts"].append(values["provenance"]["runtime_receipts"][0])
    with pytest.raises(ValueError, match="999"):
        bind(values)


def test_import_export_preserves_ready_saved_segments_and_paid_outputs(tmp_path):
    command = adapt_package_artifacts(**bind(package_inputs()))
    db = Database(tmp_path / "package-adapter.sqlite3")
    stamp = now()
    db.execute("INSERT INTO meetings(id,title,status,created_at,updated_at,transcript_version,duration_ms) VALUES('meeting','Synthetic','ready',?,?,1,5000)", (stamp, stamp))
    db.execute("INSERT INTO chunks VALUES('chunk','meeting',0,'import','synthetic.wav',0,5000,'transcribed',?)", ("a" * 64,))
    for i, source in enumerate(command.artifacts.source_segments):
        db.execute("INSERT INTO segments VALUES(?, 'meeting','chunk',1,?,?,?,'segment',?,NULL,NULL,'import')",
                   (source.segment_id, i, source.started_ms, source.ended_ms, source.text))
    db.execute("INSERT INTO summaries VALUES('meeting',1,1,?)", ('{"summary":"Synthetic paid output","action_items":[]}',))
    before = {table: db.rows(f"SELECT * FROM {table} ORDER BY rowid")
              for table in ("meetings", "segments", "summaries", "jobs", "usage")}
    initialize_local_asr_schema(db)
    repo = LocalAsrRepository(db)
    imported = repo.import_run("meeting", command, trusted_input_sha256="a" * 64)
    exported = repo.export_run("meeting", imported["run_id"])
    assert exported["result"] == command.result.model_dump(mode="json")
    assert exported["artifacts"] == command.artifacts.model_dump(mode="json")
    assert repo.import_run("meeting", command, trusted_input_sha256="a" * 64)["run_id"] == imported["run_id"]
    assert before == {table: db.rows(f"SELECT * FROM {table} ORDER BY rowid") for table in before}
