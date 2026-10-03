"""Synthetic inline labels test arithmetic/contracts, never room qualification."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("speaker_benchmark", Path(__file__).resolve().parents[1] / "scripts" / "benchmark_speakers.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def documents():
    config = {k: "revision1" for k in benchmark.CONFIG_FIELDS}
    refs, preds = [], []
    for ordinal in range(2):
        mid, source = f"m_{ordinal}", str(ordinal+1)*64
        turns = [{"turn_id": f"t_{i}", "participant_id": "p_A" if i < 60 else "p_G",
                  "eligible": i < 60, "short": False, "overlap": False} for i in range(75)]
        speech = [{"participant_id": "p_A", "start": 0, "end": 300},
                  {"participant_id": "p_G", "start": 300, "end": 450}]
        tasks = [{"task_id": "a_1", "participant_id": "p_A", "source_turn_ids": ["t_1"]}]
        refs.append({"meeting_id": mid, "transcript_version": 1, "source_hash": source,
            "duration_seconds": 450, "participants": [{"participant_id": "p_A", "role": "known"},
                {"participant_id": "p_B", "role": "known"}, {"participant_id": "p_G", "role": "guest"}],
            "turns": turns, "speech": speech, "tasks": tasks})
        preds.append({"meeting_id": mid, "transcript_version": 1, "source_hash": source,
            "turns": [{"turn_id": t["turn_id"], "participant_id": "p_A" if t["eligible"] else None,
                "status": "proposed" if t["eligible"] else "unknown", "method": "automatic"} for t in turns],
            "tasks": [{**tasks[0], "status": "confirmed"}], "label_mode": "canonical",
            "speech": [{"speaker_id": s["participant_id"], "start": s["start"], "end": s["end"]} for s in speech],
            "runtime_config": dict(config), "local_engine_elapsed_seconds": ordinal+1})
    reference = {"format": benchmark.VERSION, "meetings": refs, "evidence": {
        "kind": "synthetic", "human_annotations": False, "independent_holdout": False,
        "enrollment_hashes": ["a"*64], "calibration_hashes": ["b"*64],
        "holdout_hashes": ["1"*64, "2"*64], "frozen_config": config}}
    return reference, {"format": benchmark.VERSION, "meetings": preds}


def declared_real(reference):
    # Exercise the declaration gate with fictitious metadata, never real evidence.
    reference["evidence"].update(kind="real", human_annotations=True, independent_holdout=True)


def der(reference, hypotheses):
    return benchmark.score_der({"duration_seconds": 10, "speech": reference},
                               {"speech": hypotheses, "label_mode": "canonical"})


def r(person, start, end):
    return {"participant_id": person, "start": start, "end": end}


def h(person, start, end):
    return {"speaker_id": person, "start": start, "end": end}


def test_synthetic_default_cannot_qualify_and_metrics_are_exact():
    reference, prediction = documents()
    report = benchmark.evaluate(reference, prediction)
    assert report["status"] == "NOT_QUALIFIED"
    assert report["metrics"]["name_precision"]["value"] == 1
    assert report["metrics"]["name_coverage"]["denominator"] == 120
    assert report["metrics"]["guest_false_accepts"]["denominator"] == 30
    assert report["metrics"]["der"]["value"] == 0
    assert report["metrics"]["local_engine_elapsed"]["mean_seconds"] == 1.5


def test_declared_sufficient_data_passes_only_supported_domains():
    reference, prediction = documents()
    declared_real(reference)
    result = benchmark.evaluate(reference, prediction)
    assert result["status"] == "PASS"
    assert "declared_holdout" in result["qualification_scope"]
    assert "not independently verified" in result["limitations"][0]


def test_incorrect_names_guest_and_confirmed_tasks_need_review():
    reference, prediction = documents()
    declared_real(reference)
    prediction["meetings"][0]["turns"][0]["participant_id"] = "p_B"
    prediction["meetings"][0]["turns"][1]["participant_id"] = "p_B"
    prediction["meetings"][0]["turns"][2]["participant_id"] = "p_B"
    prediction["meetings"][0]["turns"][60].update(participant_id="p_A", status="confirmed")
    prediction["meetings"][0]["tasks"][0]["participant_id"] = "p_B"
    report = benchmark.evaluate(reference, prediction)
    assert report["status"] == "NEEDS_REVIEW"
    assert report["metrics"]["name_precision"]["numerator"] == 117
    assert report["metrics"]["guest_false_accepts"]["numerator"] == 1
    assert report["metrics"]["incorrect_confirmed_task_owners"]["numerator"] == 1


def test_missing_predictions_remain_in_denominators():
    reference, prediction = documents()
    prediction["meetings"][0]["turns"] = []
    report = benchmark.evaluate(reference, prediction)
    assert report["metrics"]["name_coverage"]["value"] == .5
    assert report["metrics"]["name_coverage"]["denominator"] == 120
    assert report["metrics"]["missing_turn_predictions"] == 75
    prediction["meetings"] = []
    empty = benchmark.evaluate(reference, prediction)["metrics"]
    assert empty["name_coverage"]["value"] == 0
    assert empty["name_precision"]["value"] is None
    assert empty["der"]["value"] == 1
    assert empty["missing_task_predictions"] == 2


def test_manual_confirmations_do_not_inflate_automatic_name_metrics():
    reference, prediction = documents()
    prediction["meetings"][0]["turns"][0].update(method="manual", status="confirmed")
    metrics = benchmark.evaluate(reference, prediction)["metrics"]
    assert metrics["name_precision"]["denominator"] == 119
    assert metrics["name_coverage"]["value"] == 119/120
    assert any(row["method"] == "manual" for row in metrics["confusion_counts"])


def test_unknown_short_overlap_breakdown_and_false_accepts():
    reference, prediction = documents()
    turn = reference["meetings"][0]["turns"][0]
    turn.update(participant_id=None, eligible=False, short=True, overlap=True)
    metrics = benchmark.evaluate(reference, prediction)["metrics"]
    assert metrics["unknown_false_accepts"]["value"] == 1
    assert metrics["breakdown"]["unknown"]["turns"] == 1
    assert metrics["breakdown"]["short"]["automatic_named"] == 1
    assert metrics["breakdown"]["overlap"]["correct_named"] == 0
    assert metrics["name_coverage"]["denominator"] == 119


def test_wilson_hand_calculation_and_zero_denominator():
    interval = benchmark.proportion(0, 30)["wilson95"]
    assert interval[0] == pytest.approx(0, abs=1e-15)
    assert interval[1] == pytest.approx(.1135133931739688)
    assert benchmark.proportion(0, 0)["wilson95"] is None
    assert benchmark.proportion(120, 120)["wilson95"][0] < .98


@pytest.mark.parametrize("hyp,expected", [([h("p_A", 0, 10)], 0), ([], 1), ([h("p_B", 0, 10)], 1)])
def test_der_perfect_missing_and_swap(hyp, expected):
    result = der([r("p_A", 0, 10)], hyp)
    assert result["reference_seconds"] == 9.5
    assert result["value"] == expected


def test_der_false_alarm_on_silence_can_exceed_one():
    result = der([r("p_A", 0, 2)], [h("p_A", 0, 10)])
    assert result["reference_seconds"] == 1.5
    assert result["false_alarm_seconds"] == 7.75
    assert result["value"] == pytest.approx(7.75/1.5)


def test_der_collar_excludes_boundary_errors_and_merges_adjacent_same_speaker():
    result = der([r("p_A", 0, 5), r("p_B", 5, 10)], [h("p_A", 0, 5.2), h("p_B", 5.2, 10)])
    assert result["reference_seconds"] == 9
    assert result["value"] == 0
    assert der([r("p_A", 0, 5), r("p_A", 5, 10)], [h("p_A", 0, 10)])["reference_seconds"] == 9.5


def test_der_overlap_exclusion_and_multiple_hypothesis_speakers():
    result = der([r("p_A", 0, 6), r("p_B", 4, 10)], [])
    assert result["excluded_overlap_seconds"] == 2
    assert result["reference_seconds"] == 7
    assert result["miss_seconds"] == 7
    extra = der([r("p_A", 0, 10)], [h("p_A", 0, 10), h("p_B", 0, 10)])
    assert extra["false_alarm_seconds"] == 9.5
    assert extra["confusion_seconds"] == 0
    assert extra["value"] == 1


def test_der_zero_scored_time_is_unknown():
    result = der([r("p_A", 0, .4)], [h("p_A", 0, .4)])
    assert result["reference_seconds"] == 0
    assert result["value"] is None


def test_anonymous_mapping_is_explicit_and_identity_predictions_are_not_remapped():
    reference, prediction = documents()
    p = prediction["meetings"][0]
    p["label_mode"] = "anonymous_mapped"
    p["label_mapping"] = {"s_1": "p_G", "s_2": "p_A"}
    p["speech"] = [h("s_2", 0, 300), h("s_1", 300, 450)]
    assert benchmark.evaluate(reference, prediction)["metrics"]["der"]["value"] == 0
    p["label_mapping"] = {"s_1": "p_A", "s_2": "p_G"}
    assert benchmark.evaluate(reference, prediction)["metrics"]["der"]["value"] == .5


@pytest.mark.parametrize("change,reason", [
    (lambda e: e.update(kind="unspecified"), "not_declared_real_recordings"),
    (lambda e: e.update(enrollment_hashes=[]), "missing_source_partition_hashes"),
    (lambda e: e.update(calibration_hashes=["1"*64]), "declared_source_contamination"),
    (lambda e: e.update(holdout_hashes=["3"*64]), "holdout_hash_scope_mismatch"),
    (lambda e: e["frozen_config"].update(model_revision="revision2"), "frozen_runtime_config_missing_or_mismatched"),
])
def test_evidence_and_contamination_gates(change, reason):
    reference, prediction = documents()
    declared_real(reference)
    change(reference["evidence"])
    report = benchmark.evaluate(reference, prediction)
    assert report["status"] == "NOT_QUALIFIED"
    assert reason in report["insufficient_evidence"]


@pytest.mark.parametrize("mutation", [
    lambda r, p: p["meetings"][0].update(transcript_version=2),
    lambda r, p: p["meetings"][0].update(source_hash="3"*64),
    lambda r, p: p["meetings"][0]["turns"].append(copy.deepcopy(p["meetings"][0]["turns"][0])),
    lambda r, p: p["meetings"][0]["turns"][0].update(turn_id="t_unexpected"),
    lambda r, p: p["meetings"][0]["turns"][0].update(participant_id="p_foreign"),
    lambda r, p: p["meetings"][0]["turns"][0].update(status="unknown"),
    lambda r, p: p["meetings"][0]["tasks"][0].update(source_turn_ids=["t_2"]),
    lambda r, p: r["meetings"][0]["tasks"][0].update(source_turn_ids=[]),
    lambda r, p: p["meetings"][0].update(local_engine_elapsed_seconds=float("nan")),
    lambda r, p: p["meetings"][0].update(local_engine_elapsed_seconds=True),
    lambda r, p: p["meetings"][0].update(local_engine_elapsed_seconds=1e300),
    lambda r, p: r["meetings"][0]["speech"][0].update(end=451),
    lambda r, p: p["meetings"][0]["speech"].append(h("p_A", 1, 2)),
    lambda r, p: r["meetings"][0]["turns"][0].update(overlap=True),
    lambda r, p: p["meetings"][0].update(label_mode="anonymous_mapped", label_mapping={"s_1": "p_A", "s_2": "p_A"}),
    lambda r, p: r["meetings"][0].update(display_name="forbidden"),
    lambda r, p: p["meetings"][0]["turns"][0].update(status=[]),
    lambda r, p: r["meetings"][0]["participants"][0].update(role={}),
])
def test_malformed_scopes_numbers_sources_statuses_and_sensitive_fields_rejected(mutation):
    reference, prediction = documents()
    mutation(reference, prediction)
    with pytest.raises(benchmark.FormatError):
        benchmark.evaluate(reference, prediction)


def test_cli_determinism_exit_codes_no_overwrite_and_no_sensitive_errors(tmp_path, capsys):
    reference, prediction = documents()
    rp, pp = tmp_path/"reference.json", tmp_path/"prediction.json"
    rp.write_text(json.dumps(reference), encoding="utf-8")
    pp.write_text(json.dumps(prediction), encoding="utf-8")
    args = ["--reference", str(rp), "--predictions", str(pp)]
    assert benchmark.main(args) == 2
    first = capsys.readouterr().out
    assert benchmark.main(args) == 2
    assert capsys.readouterr().out == first
    target = tmp_path/"report.json"
    assert benchmark.main(args+["--output", str(target)]) == 2
    assert target.read_text(encoding="utf-8") == first
    assert benchmark.main(args+["--output", str(target)]) == 3
    capsys.readouterr()
    assert target.read_text(encoding="utf-8") == first
    rp.write_text('{"secret":"PRIVATE STRING", "secret":2}', encoding="utf-8")
    assert benchmark.main(args) == 3
    error = capsys.readouterr()
    assert "PRIVATE" not in error.err and not error.out


def test_oversized_json_and_nonfinite_json_rejected(tmp_path, monkeypatch):
    path = tmp_path/"data.json"
    monkeypatch.setattr(benchmark, "MAX_BYTES", 10)
    path.write_bytes(b" "*11)
    with pytest.raises(benchmark.FormatError): benchmark.load_document(path)
    path.write_bytes(b"NaN")
    with pytest.raises(benchmark.FormatError): benchmark.load_document(path)


def test_empty_document_returns_unknown_metrics_without_division_errors():
    result = benchmark.evaluate({"format": benchmark.VERSION, "meetings": []}, {"format": benchmark.VERSION, "meetings": []})
    assert result["status"] == "NOT_QUALIFIED"
    assert result["metrics"]["der"]["value"] is None
    assert result["metrics"]["name_coverage"]["value"] is None


def test_transcript_version_respects_documented_numeric_bound():
    reference, prediction = documents()
    for meeting in reference["meetings"] + prediction["meetings"]:
        meeting["transcript_version"] = 10**12
    benchmark.evaluate(reference, prediction)
    for meeting in reference["meetings"] + prediction["meetings"]:
        meeting["transcript_version"] += 1
    with pytest.raises(benchmark.FormatError, match="Invalid transcript version"):
        benchmark.evaluate(reference, prediction)


def test_anonymous_empty_speech_requires_explicit_mapping():
    reference, prediction = documents()
    meeting = prediction["meetings"][0]
    meeting.update(label_mode="anonymous_mapped", speech=[])
    meeting.pop("label_mapping", None)
    with pytest.raises(benchmark.FormatError, match="Missing label mapping"):
        benchmark.evaluate(reference, prediction)
    meeting["label_mapping"] = {}
    result = benchmark.evaluate(reference, prediction)
    assert result["metrics"]["der"]["value"] == .5


def test_input_not_mutated_by_adjacent_interval_merging():
    reference, prediction = documents()
    before = copy.deepcopy((reference, prediction))
    benchmark.evaluate(reference, prediction)
    assert (reference, prediction) == before


def test_cli_pass_and_review_codes_with_fictitious_real_declaration(tmp_path, capsys):
    reference, prediction = documents()
    declared_real(reference)
    rp, pp = tmp_path/"reference.json", tmp_path/"prediction.json"
    rp.write_text(json.dumps(reference), encoding="utf-8")
    pp.write_text(json.dumps(prediction), encoding="utf-8")
    args = ["--reference", str(rp), "--predictions", str(pp)]
    assert benchmark.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"
    prediction["meetings"][0]["speech"] = []
    pp.write_text(json.dumps(prediction), encoding="utf-8")
    assert benchmark.main(args) == 1
    assert json.loads(capsys.readouterr().out)["metrics"]["der"]["value"] == .5


@pytest.mark.parametrize("mutation,reason", [
    (lambda r, p: r["meetings"][0]["turns"].pop(), "fewer_than_150_labeled_turns"),
    (lambda r, p: r["meetings"][0]["turns"][60].update(participant_id="p_A"), "fewer_than_30_guest_turns"),
    (lambda r, p: r["meetings"][0].update(speech=[]), "meeting_without_der_scored_reference_time"),
    (lambda r, p: p["meetings"][0].update(tasks=[]), "missing_task_predictions"),
])
def test_sample_sizes_and_missing_task_evidence(mutation, reason):
    reference, prediction = documents()
    declared_real(reference)
    mutation(reference, prediction)
    # A removed reference turn must also be removed from the saved prediction.
    ids = {t["turn_id"] for t in reference["meetings"][0]["turns"]}
    prediction["meetings"][0]["turns"] = [t for t in prediction["meetings"][0]["turns"] if t["turn_id"] in ids]
    report = benchmark.evaluate(reference, prediction)
    assert report["status"] == "NOT_QUALIFIED"
    assert reason in report["insufficient_evidence"]
    if reason == "meeting_without_der_scored_reference_time":
        # False alarms on empty reference speech are still retained in metrics.
        assert report["metrics"]["der"]["false_alarm_seconds"] == 450


def test_resource_limits_and_regular_json_file_requirement(tmp_path, monkeypatch):
    reference, prediction = documents()
    monkeypatch.setattr(benchmark, "MAX_MEETINGS", 1)
    with pytest.raises(benchmark.FormatError): benchmark.evaluate(reference, prediction)
    path = tmp_path/"settings.env"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(benchmark.FormatError): benchmark.load_document(path)
    with pytest.raises(benchmark.FormatError): benchmark.load_document(tmp_path/"missing.json")
