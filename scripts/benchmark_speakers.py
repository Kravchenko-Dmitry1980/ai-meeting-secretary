"""Bounded, dependency-free scoring of saved speaker results. Never opens audio."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys

VERSION = "secretary-speaker-benchmark/v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 100_000
MAX_MEETINGS = 100
COLLAR = 0.250
CONFIG_FIELDS = {"model_revision", "calibration_revision", "profile_revision", "microphone_revision"}


class FormatError(ValueError):
    """Invalid exchange contract; messages never interpolate user data."""


def require(condition, message):
    if not condition:
        raise FormatError(message)


def obj(value, required, optional=()):
    require(isinstance(value, dict), "Expected object")
    require(set(value) >= set(required) and set(value) <= set(required) | set(optional), "Invalid object fields")
    return value


def seq(value, limit=MAX_ITEMS):
    require(isinstance(value, list) and len(value) <= limit, "Invalid or oversized list")
    return value


def number(value, lower=0):
    require(type(value) in (int, float), "Expected finite number")
    try:
        require(math.isfinite(value) and lower <= value <= 1e12, "Invalid numeric range")
    except OverflowError:
        raise FormatError("Invalid numeric range") from None
    return value


def identifier(value, prefix):
    require(isinstance(value, str) and re.fullmatch(prefix + r"_[A-Za-z0-9_-]{1,64}", value) is not None,
            "Expected canonical identifier")
    return value


def token(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is not None,
            "Expected revision token")
    return value


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "Expected SHA256 declaration")
    return value


def boolean(value):
    require(type(value) is bool, "Expected boolean")
    return value


def choice(value, allowed):
    require(isinstance(value, str) and value in allowed, "Invalid enumeration value")
    return value


def index(items, field, prefix):
    result = {}
    for item in seq(items):
        require(isinstance(item, dict) and field in item, "Missing identifier")
        key = identifier(item[field], prefix)
        require(key not in result, "Duplicate identifier within scope")
        result[key] = item
    return result


def interval(item, duration):
    start, end = number(item["start"]), number(item["end"])
    require(start < end <= duration, "Invalid time interval")


def participant(value, roster, nullable=False):
    if nullable and value is None:
        return
    identifier(value, "p")
    require(value in roster, "Participant outside meeting scope")


def revisions(value):
    obj(value, CONFIG_FIELDS)
    for entry in value.values():
        token(entry)


def validate_reference(document):
    obj(document, {"format", "meetings"}, {"evidence"})
    require(document["format"] == VERSION, "Unsupported format")
    seq(document["meetings"], MAX_MEETINGS)
    meetings = index(document["meetings"], "meeting_id", "m")
    total = 0
    for meeting in meetings.values():
        obj(meeting, {"meeting_id", "transcript_version", "source_hash", "duration_seconds", "participants", "turns", "speech", "tasks"})
        require(type(meeting["transcript_version"]) is int and 0 < meeting["transcript_version"] <= 10**12, "Invalid transcript version")
        digest(meeting["source_hash"])
        duration = number(meeting["duration_seconds"])
        require(0 < duration <= 86400, "Invalid meeting duration")
        roster = index(meeting["participants"], "participant_id", "p")
        require(len(roster) <= 100, "Too many participants")
        for person in roster.values():
            obj(person, {"participant_id", "role"})
            choice(person["role"], {"known", "guest", "unknown"})
        turns = index(meeting["turns"], "turn_id", "t")
        tasks = index(meeting["tasks"], "task_id", "a")
        speech = seq(meeting["speech"])
        total += len(turns) + len(tasks) + len(speech)
        require(total <= MAX_ITEMS, "Too many reference items")
        for turn in turns.values():
            obj(turn, {"turn_id", "participant_id", "eligible", "short", "overlap"})
            participant(turn["participant_id"], roster, nullable=True)
            for key in ("eligible", "short", "overlap"):
                boolean(turn[key])
            require(not turn["eligible"] or (turn["participant_id"] is not None and
                    roster[turn["participant_id"]]["role"] == "known" and not turn["short"] and not turn["overlap"]),
                    "Eligible turn must have known identity and be neither short nor overlapped")
        for segment in speech:
            obj(segment, {"participant_id", "start", "end"})
            participant(segment["participant_id"], roster)
            interval(segment, duration)
        _validate_speaker_intervals(speech, "participant_id")
        for task in tasks.values():
            obj(task, {"task_id", "participant_id", "source_turn_ids"})
            participant(task["participant_id"], roster, nullable=True)
            _sources(task["source_turn_ids"], turns)
    evidence = document.get("evidence")
    if evidence is not None:
        obj(evidence, {"kind", "human_annotations", "independent_holdout", "enrollment_hashes", "calibration_hashes", "holdout_hashes", "frozen_config"})
        choice(evidence["kind"], {"real", "synthetic", "unspecified"})
        boolean(evidence["human_annotations"])
        boolean(evidence["independent_holdout"])
        for key in ("enrollment_hashes", "calibration_hashes", "holdout_hashes"):
            hashes = seq(evidence[key])
            for value in hashes:
                digest(value)
            require(len(set(hashes)) == len(hashes), "Duplicate source hash declaration")
        revisions(evidence["frozen_config"])
    return meetings


def _sources(values, turns):
    seq(values)
    require(values and all(isinstance(v, str) for v in values), "Missing task source references")
    require(len(set(values)) == len(values) and all(v in turns for v in values), "Invalid task source references")


def _validate_speaker_intervals(segments, label):
    grouped = {}
    for s in segments:
        grouped.setdefault(s[label], []).append((s["start"], s["end"]))
    for intervals in grouped.values():
        ordered = sorted(intervals)
        require(all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:])), "Same speaker has overlapping intervals")


def validate_predictions(document, references):
    obj(document, {"format", "meetings"})
    require(document["format"] == VERSION, "Unsupported format")
    seq(document["meetings"], MAX_MEETINGS)
    meetings = index(document["meetings"], "meeting_id", "m")
    require(set(meetings) <= set(references), "Unexpected prediction meeting")
    total = 0
    for key, meeting in meetings.items():
        obj(meeting, {"meeting_id", "transcript_version", "source_hash", "turns", "tasks", "speech", "label_mode"},
            {"label_mapping", "local_engine_elapsed_seconds", "runtime_config"})
        ref = references[key]
        require(type(meeting["transcript_version"]) is int and meeting["transcript_version"] == ref["transcript_version"], "Transcript scope mismatch")
        digest(meeting["source_hash"])
        require(meeting["source_hash"] == ref["source_hash"], "Source scope mismatch")
        roster = index(ref["participants"], "participant_id", "p")
        ref_turns = index(ref["turns"], "turn_id", "t")
        ref_tasks = index(ref["tasks"], "task_id", "a")
        turns = index(meeting["turns"], "turn_id", "t")
        tasks = index(meeting["tasks"], "task_id", "a")
        require(set(turns) <= set(ref_turns) and set(tasks) <= set(ref_tasks), "Unexpected turn or task identifier")
        speech = seq(meeting["speech"])
        total += len(turns) + len(tasks) + len(speech)
        require(total <= MAX_ITEMS, "Too many prediction items")
        for turn in turns.values():
            obj(turn, {"turn_id", "participant_id", "status", "method"})
            participant(turn["participant_id"], roster, nullable=True)
            choice(turn["method"], {"automatic", "manual"})
            choice(turn["status"], {"proposed", "confirmed", "unknown", "conflict"})
            require((turn["participant_id"] is not None) == (turn["status"] in {"proposed", "confirmed"}), "Inconsistent attribution status")
        for task in tasks.values():
            obj(task, {"task_id", "participant_id", "status", "source_turn_ids"})
            participant(task["participant_id"], roster, nullable=True)
            choice(task["status"], {"proposed", "confirmed", "needs_review"})
            require(task["status"] != "confirmed" or task["participant_id"] is not None, "Confirmed task needs an owner")
            _sources(task["source_turn_ids"], ref_turns)
            require(set(task["source_turn_ids"]) == set(ref_tasks[task["task_id"]]["source_turn_ids"]), "Task source mismatch")
        mode = meeting["label_mode"]
        choice(mode, {"canonical", "anonymous_mapped"})
        mapping = meeting.get("label_mapping", {})
        require(isinstance(mapping, dict), "Invalid label mapping")
        if mode == "canonical":
            require(not mapping, "Canonical labels cannot be remapped")
        else:
            require("label_mapping" in meeting, "Missing label mapping")
            require(len(mapping) <= 100, "Too many anonymous labels")
            for label, person in mapping.items():
                identifier(label, "s")
                participant(person, roster, nullable=True)
            assigned = [p for p in mapping.values() if p is not None]
            require(len(set(assigned)) == len(assigned), "Mapping must be injective")
        labels = set()
        for segment in speech:
            obj(segment, {"speaker_id", "start", "end"})
            if mode == "canonical":
                participant(segment["speaker_id"], roster)
            else:
                identifier(segment["speaker_id"], "s")
            labels.add(segment["speaker_id"])
            interval(segment, ref["duration_seconds"])
        if mode == "anonymous_mapped":
            require(labels == set(mapping), "Mapping must cover exactly all anonymous labels")
        _validate_speaker_intervals(speech, "speaker_id")
        if "local_engine_elapsed_seconds" in meeting:
            number(meeting["local_engine_elapsed_seconds"])
        if "runtime_config" in meeting:
            revisions(meeting["runtime_config"])
    return meetings


def proportion(successes, sample_size):
    """Two-sided 95% Wilson interval, with nulls for an empty denominator."""
    if sample_size == 0:
        return {"numerator": successes, "denominator": 0, "value": None, "wilson95": None}
    p, z = successes / sample_size, 1.959963984540054
    d = 1 + z*z / sample_size
    centre = (p + z*z / (2*sample_size)) / d
    delta = z * math.sqrt(p*(1-p)/sample_size + z*z/(4*sample_size*sample_size)) / d
    return {"numerator": successes, "denominator": sample_size, "value": p,
            "wilson95": [max(0, centre-delta), min(1, centre+delta)]}


def score_der(reference, prediction):
    """Event sweep over the entire recording; reference overlap/collars excluded."""
    duration = reference["duration_seconds"]
    events = {0: [], duration: []}
    def add(start, end, kind, label):
        events.setdefault(start, []).append((kind, label, 1))
        events.setdefault(end, []).append((kind, label, -1))
    merged = []
    by_speaker = {}
    for segment in reference["speech"]:
        by_speaker.setdefault(segment["participant_id"], []).append(segment)
    for segments in by_speaker.values():
        for segment in sorted(segments, key=lambda s: s["start"]):
            if merged and merged[-1]["participant_id"] == segment["participant_id"] and merged[-1]["end"] == segment["start"]:
                merged[-1]["end"] = segment["end"]
            else:
                merged.append(dict(segment))
    for segment in merged:
        add(segment["start"], segment["end"], "ref", segment["participant_id"])
        for boundary in (segment["start"], segment["end"]):
            add(max(0, boundary-COLLAR), min(duration, boundary+COLLAR), "collar", "c")
    for segment in prediction.get("speech", []):
        label = segment["speaker_id"]
        if prediction.get("label_mode") == "anonymous_mapped":
            label = prediction["label_mapping"][label]
        add(segment["start"], segment["end"], "hyp", label)
    active = {"ref": Counter(), "hyp": Counter(), "collar": Counter()}
    result = {k: 0.0 for k in ("reference_seconds", "false_alarm_seconds", "miss_seconds", "confusion_seconds", "excluded_overlap_seconds", "excluded_collar_seconds")}
    points = sorted(events)
    for left, right in zip(points, points[1:]):
        for kind, label, change in events[left]:
            active[kind][label] += change
            if active[kind][label] == 0:
                del active[kind][label]
        elapsed = right-left
        ref, hyp = active["ref"], active["hyp"]
        if len(ref) > 1:
            result["excluded_overlap_seconds"] += elapsed
        elif active["collar"]:
            result["excluded_collar_seconds"] += elapsed
        else:
            nr, nh = len(ref), sum(hyp.values())
            result["reference_seconds"] += nr*elapsed
            result["miss_seconds"] += max(0, nr-nh)*elapsed
            result["false_alarm_seconds"] += max(0, nh-nr)*elapsed
            correct = sum(min(count, hyp.get(label, 0)) for label, count in ref.items())
            result["confusion_seconds"] += (min(nr, nh)-correct)*elapsed
    result["error_seconds"] = sum(result[k] for k in ("false_alarm_seconds", "miss_seconds", "confusion_seconds"))
    result["value"] = result["error_seconds"] / result["reference_seconds"] if result["reference_seconds"] else None
    return result


def evaluate(reference, predictions):
    refs = validate_reference(reference)
    preds = validate_predictions(predictions, refs)
    eligible = proposed = correct = guest = guest_fa = unknown = unknown_fa = confirmed = wrong = missing = 0
    confusion = Counter()
    breakdown = {k: {"turns": 0, "automatic_named": 0, "correct_named": 0, "missing": 0}
                 for k in ("unknown", "short", "overlap", "guest", "eligible")}
    der_rows, elapsed = [], []
    all_turns = all_tasks = 0
    for key in sorted(refs):
        ref, pred = refs[key], preds.get(key, {})
        roster = index(ref["participants"], "participant_id", "p")
        pturns = {t["turn_id"]: t for t in pred.get("turns", [])}
        all_turns += len(ref["turns"])
        for turn in ref["turns"]:
            p = pturns.get(turn["turn_id"])
            named = bool(p and p["method"] == "automatic" and p["participant_id"] is not None)
            accepted_known = named and roster[p["participant_id"]]["role"] == "known"
            ok = named and p["participant_id"] == turn["participant_id"]
            role = roster[turn["participant_id"]]["role"] if turn["participant_id"] is not None else "unknown"
            missing += p is None
            if p and p["participant_id"] is not None:
                confusion[(key, turn["participant_id"], p["participant_id"], p["method"])] += 1
            if turn["eligible"]:
                eligible += 1
                proposed += named
                correct += ok
            if role == "guest":
                guest += 1
                guest_fa += accepted_known
            if role == "unknown":
                unknown += 1
                unknown_fa += accepted_known
            for category, applies in (("unknown", role == "unknown"), ("short", turn["short"]),
                                      ("overlap", turn["overlap"]), ("guest", role == "guest"), ("eligible", turn["eligible"])):
                if applies:
                    row = breakdown[category]
                    row["turns"] += 1
                    row["automatic_named"] += named
                    row["correct_named"] += ok
                    row["missing"] += p is None
        ptasks = {t["task_id"]: t for t in pred.get("tasks", [])}
        all_tasks += len(ref["tasks"])
        for task in ref["tasks"]:
            p = ptasks.get(task["task_id"])
            if p and p["status"] == "confirmed":
                confirmed += 1
                wrong += p["participant_id"] != task["participant_id"]
        der_rows.append({"meeting_id": key, "label_mode": pred.get("label_mode", "missing"), **score_der(ref, pred)})
        if "local_engine_elapsed_seconds" in pred:
            elapsed.append(pred["local_engine_elapsed_seconds"])
    der = {key: math.fsum(row[key] for row in der_rows) for key in der_rows[0] if key.endswith("seconds")} if der_rows else {
        key: 0.0 for key in ("reference_seconds", "false_alarm_seconds", "miss_seconds", "confusion_seconds", "error_seconds", "excluded_overlap_seconds", "excluded_collar_seconds")}
    der["value"] = der["error_seconds"] / der["reference_seconds"] if der["reference_seconds"] else None
    durations = math.fsum(m["duration_seconds"] for m in refs.values())
    metrics = {"meeting_count": len(refs), "recording_seconds": durations, "labeled_turns": all_turns,
               "missing_turn_predictions": missing, "name_precision": proportion(correct, proposed),
               "name_coverage": proportion(proposed, eligible), "guest_false_accepts": proportion(guest_fa, guest),
               "unknown_false_accepts": proportion(unknown_fa, unknown),
               "task_reference_count": all_tasks, "missing_task_predictions": all_tasks-sum(len(p["tasks"]) for p in preds.values()),
               "incorrect_confirmed_task_owners": proportion(wrong, confirmed), "der": der, "der_meetings": der_rows,
               "breakdown": breakdown,
               "confusion_counts": [{"meeting_id": k[0], "reference_participant_id": k[1], "predicted_participant_id": k[2], "method": k[3], "count": n}
                                    for k, n in sorted(confusion.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2], kv[0][3]))]}
    for row in breakdown.values():
        row["automatic_named_rate"] = proportion(row["automatic_named"], row["turns"])
        row["correct_named_precision"] = proportion(row["correct_named"], row["automatic_named"])
    ordered = sorted(elapsed)
    metrics["local_engine_elapsed"] = {"sample_count": len(ordered), "missing_count": len(refs)-len(ordered),
        "total_seconds": math.fsum(ordered) if ordered else None, "mean_seconds": math.fsum(ordered)/len(ordered) if ordered else None,
        "min_seconds": min(ordered) if ordered else None, "max_seconds": max(ordered) if ordered else None,
        "p50_seconds": ordered[math.ceil(len(ordered)*.5)-1] if ordered else None,
        "p95_seconds": ordered[math.ceil(len(ordered)*.95)-1] if ordered else None}
    insufficient = []
    evidence = reference.get("evidence")
    if evidence is None:
        insufficient.append("missing_evidence_declaration")
    else:
        if evidence["kind"] != "real": insufficient.append("not_declared_real_recordings")
        if not evidence["human_annotations"]: insufficient.append("human_annotations_not_declared")
        if not evidence["independent_holdout"]: insufficient.append("independent_holdout_not_declared")
        sets = [set(evidence[k]) for k in ("enrollment_hashes", "calibration_hashes", "holdout_hashes")]
        if any(not s for s in sets): insufficient.append("missing_source_partition_hashes")
        if any(sets[i] & sets[j] for i in range(3) for j in range(i+1, 3)):
            insufficient.append("declared_source_contamination")
        if sets[2] != {m["source_hash"] for m in refs.values()}:
            insufficient.append("holdout_hash_scope_mismatch")
        if any(preds.get(key, {}).get("runtime_config") != evidence["frozen_config"] for key in refs):
            insufficient.append("frozen_runtime_config_missing_or_mismatched")
    if len({m["source_hash"] for m in refs.values()}) != len(refs): insufficient.append("duplicate_meeting_source")
    if any(row["reference_seconds"] == 0 for row in der_rows):
        insufficient.append("meeting_without_der_scored_reference_time")
    for condition, reason in ((len(refs) >= 2, "fewer_than_two_meetings"), (durations >= 900, "less_than_15_minutes"),
        (all_turns >= 150, "fewer_than_150_labeled_turns"), (guest >= 30, "fewer_than_30_guest_turns"),
        (eligible > 0, "no_eligible_turns"), (proposed > 0, "no_automatic_name_proposals"),
        (der["reference_seconds"] > 0, "no_der_scored_reference_time"), (all_tasks > 0, "no_labeled_tasks"),
        (metrics["missing_task_predictions"] == 0, "missing_task_predictions")):
        if not condition: insufficient.append(reason)
    failed = []
    for condition, reason in ((proposed > 0 and correct/proposed >= .98, "name_precision_below_0.98"),
        (eligible > 0 and proposed/eligible >= .80, "name_coverage_below_0.80"),
        (guest_fa == 0, "observed_guest_false_accepts"), (der["value"] is not None and der["value"] <= .15, "der_above_0.15"),
        (wrong == 0, "incorrect_confirmed_task_owners")):
        if not condition: failed.append(reason)
    status = "NOT_QUALIFIED" if insufficient else "NEEDS_REVIEW" if failed else "PASS"
    return {"format": VERSION, "status": status, "qualification_scope": "declared_holdout_speaker_and_task_metrics_only",
        "insufficient_evidence": sorted(insufficient), "failed_thresholds": sorted(failed), "metrics": metrics,
        "limitations": ["Source hashes and human/real/independence declarations are not independently verified.",
            "No room conditions, consent, hardware, provider, enrollment, or calibration validity is certified.",
            "Turn proportions are unit-weighted; DER is duration-weighted; elapsed values are saved declarations.",
            "Canonical DER is identity-aware; anonymous DER uses the supplied injective mapping, not an optimized permutation.",
            "Thresholds use point estimates; Wilson intervals describe uncertainty and do not certify population accuracy."]}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def load_document(path):
    path = Path(path)
    require(path.suffix.lower() == ".json" and not str(path).startswith(("\\\\", "//")) and path.is_file(),
            "Expected regular local JSON file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES+1)
    require(len(raw) <= MAX_BYTES, "Input exceeds byte limit")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(FormatError("Nonfinite JSON number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise FormatError("Invalid JSON document") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Explicit new report path; never overwrites a file")
    args = parser.parse_args(argv)
    try:
        report = evaluate(load_document(args.reference), load_document(args.predictions))
        rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if args.output:
            require(args.output.suffix.lower() == ".json" and not str(args.output).startswith(("\\\\", "//")),
                    "Expected local JSON output path")
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
        else:
            sys.stdout.write(rendered)
        return {"PASS": 0, "NEEDS_REVIEW": 1, "NOT_QUALIFIED": 2}[report["status"]]
    except (FormatError, OSError, ValueError, RecursionError):
        sys.stderr.write("Invalid benchmark input or inaccessible/non-new output; no qualification report written.\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
