"""Pure conversion of pinned package snapshots into Secretary import artifacts.

There is deliberately no package import, filesystem access or execution here.
The calling server owns provenance verification and must supply the original
source projection; hashes bind supplied documents but do not qualify a runtime.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import Field

from secretary.domain.local_asr import (
    AlignmentDiagnostics, Digest, ImportLocalAsrRun, LocalAsrArtifactsV1,
    LocalAsrError, LocalAsrV2, RuntimeIdentity, RuntimeReceipt, SegmentSources,
    SourceSegment, WordSpan, seconds_to_ms, text_fingerprint,
)
from secretary.domain.speakers import DTO


class PackageAdapterError(LocalAsrError):
    """An evidence mismatch, never an instruction to retry transcription."""


class PackageArtifactProvenance(DTO):
    """Server-supplied evidence binding; never accept this from an end user.

saved_transcript means the transcript-shaped input was projected from saved
segments without calling RNNT. This adapter performs neither kind of inference.
"""

    text_origin: Literal["saved_transcript", "rnnt"]
    input_sha256: Digest
    source_segments_sha256: Digest
    transcript_sha256: Digest
    alignment_sha256: Digest
    result_sha256: Digest
    identity: RuntimeIdentity
    tokenizer_compatibility: Literal["verified", "unverified", "unsupported"]
    runtime_receipts: list[RuntimeReceipt] = Field(max_length=999)


def package_document_digest(value: object) -> str:
    """Hash canonical UTF-8 JSON, not the original file's formatting bytes."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PackageAdapterError("package_invalid_json") from exc
    if len(encoded) > 16 * 1024 * 1024:
        raise PackageAdapterError("package_document_too_large")
    return hashlib.sha256(encoded).hexdigest()


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise PackageAdapterError(code)


def _interval(item: dict) -> tuple[int, int]:
    try:
        start, end = seconds_to_ms(item["start"]), seconds_to_ms(item["end"])
    except (KeyError, ValueError) as exc:
        raise PackageAdapterError("package_invalid_seconds") from exc
    _require(start < end, "package_interval_collapsed_to_ms")
    return start, end


def _speaker_kind(label: str) -> Literal["single", "unknown", "composite"]:
    if label == "Спикер неизвестен":
        return "unknown"
    if re.fullmatch(r"Спикер [1-9][0-9]*", label):
        return "single"
    parts = label.split(" + ")
    if (len(parts) >= 2 and len(set(parts)) == len(parts)
            and all(re.fullmatch(r"Спикер [1-9][0-9]*", part) for part in parts)):
        return "composite"
    raise PackageAdapterError("package_noncanonical_speaker_label")


def adapt_package_artifacts(
    *, operation_id: str, meeting_id: str, transcript_version: int,
    duration_ms: int, source_segments: list[SourceSegment], transcript: dict,
    alignment: dict, result: dict, provenance: PackageArtifactProvenance,
) -> ImportLocalAsrRun:
    """Convert document values without changing tokens, sources or v2 output.

Supported input is one <=24-second package window per canonical source segment.
Word seconds in align.json are absolute on the input timeline (the package has
already added each window offset). Relative align_window results are not this
format. Longer/split/unknown-time saved sources need a verified window mapping
before they can use an extended adapter; no mapping is guessed here.
"""
    proof = PackageArtifactProvenance.model_validate(provenance.model_dump(mode="json"))
    sources = [SourceSegment.model_validate(s.model_dump(mode="json")) for s in source_segments]
    identity = proof.identity
    _require(identity.asr_device is not None and identity.diarization_device is not None,
             "package_stage_devices_required")
    _require({m.role for m in identity.models} == {"rnnt", "ctc", "diarizer"},
             "package_complete_model_identity_required")
    _require(type(duration_ms) is int and duration_ms > 0, "package_duration_invalid")
    _require(bool(sources), "package_source_projection_required")
    _require(package_document_digest([s.model_dump(mode="json") for s in sources])
             == proof.source_segments_sha256, "package_source_provenance_mismatch")
    for payload, expected in ((transcript, proof.transcript_sha256),
                              (alignment, proof.alignment_sha256), (result, proof.result_sha256)):
        _require(isinstance(payload, dict), "package_object_required")
        _require(package_document_digest(payload) == expected, "package_artifact_provenance_mismatch")
    _require("speaker_turns" in result and bool(result["speaker_turns"]),
             "package_independent_turns_required")
    parsed = LocalAsrV2.model_validate(result)
    _require(parsed.input_sha256 == proof.input_sha256, "package_input_provenance_mismatch")
    _require(parsed.metrics.get("experimental_nemotron_turn_end_pad_ms", 0) == 0,
             "package_modified_raw_turns")
    _require(transcript.get("provider") == "gigaam-v3-e2e-rnnt"
             and alignment.get("provider") == "gigaam-v3-e2e-rnnt+gigaam-ctc"
             and alignment.get("diarization_provider") == "nemotron-3-diarization"
             and parsed.provider == "gigaam-v3-e2e-rnnt+gigaam-ctc+nemotron-3-diarization",
             "package_provider_mismatch")
    metrics = alignment.get("metrics", {})
    _require(isinstance(metrics, dict) and all(isinstance(k, str) for k in metrics)
             and not any("selector" in k for k in metrics),
             "package_text_selector_not_supported")
    _require(metrics == parsed.metrics, "package_bridge_metrics_mismatch")
    warnings = alignment.get("warnings", [])
    _require(isinstance(warnings, list)
             and [str(item)[:500] for item in warnings] == parsed.warnings,
             "package_bridge_warnings_mismatch")
    _require(all(receipt.stage != "package_adapter_validation" for receipt in proof.runtime_receipts),
             "package_reserved_receipt_stage")
    original_windows, aligned_windows = transcript.get("segments"), alignment.get("segments")
    _require(isinstance(original_windows, list) and isinstance(aligned_windows, list)
             and len(original_windows) == len(aligned_windows) == len(sources),
             "package_source_window_mapping_required")

    selected_text = "\n".join(s.text for s in sources)
    token_matches = list(re.finditer(r"\S+", selected_text))
    words: list[WordSpan] = []
    token_owners: list[str] = []
    missing: list[int] = []
    fallback_sources: list[SourceSegment] = []
    token_offset = 0
    previous_start = -1
    for source, original, aligned in zip(sources, original_windows, aligned_windows):
        _require(isinstance(original, dict) and isinstance(aligned, dict), "package_window_invalid")
        _require(isinstance(original.get("text"), str) and original.get("text") == source.text
                 and aligned.get("text") == source.text, "package_changed_source_text")
        _require(aligned.get("transcript_source", "rnnt") == "rnnt",
                 "package_text_selector_not_supported")
        interval = _interval(original)
        _require((original["start"], original["end"]) == (aligned.get("start"), aligned.get("end")),
                 "package_changed_source_interval")
        _require(interval == _interval(aligned), "package_changed_source_interval")
        _require(interval == (source.started_ms, source.ended_ms), "package_source_window_mapping_required")
        _require(interval[0] >= previous_start and interval[1] - interval[0] <= 24_000
                 and interval[1] <= duration_ms, "package_window_outside_profile")
        previous_start = interval[0]
        tokens = list(re.finditer(r"\S+", source.text))
        _require(bool(tokens), "package_empty_source_not_supported")
        token_owners.extend([source.segment_id] * len(tokens))
        aligned_words = aligned.get("words")
        if aligned_words is None or aligned_words == []:
            fallback_sources.append(source)
            missing.extend(range(token_offset, token_offset + len(tokens)))
        else:
            _require(proof.tokenizer_compatibility == "verified", "package_tokenizer_not_verified")
            _require(isinstance(aligned_words, list) and len(aligned_words) == len(tokens),
                     "package_word_token_mismatch")
            word_start = -1
            for i, word in enumerate(aligned_words):
                _require(isinstance(word, dict) and word.get("word") == tokens[i].group(),
                         "package_word_token_mismatch")
                start, end = _interval(word)
                _require(interval[0] <= start < end <= interval[1] and start >= word_start,
                         "package_word_outside_window")
                word_start = start
                token = token_matches[token_offset + i]
                words.append(WordSpan(token_index=token_offset + i, text=token.group(),
                                      char_start=token.start(), char_end=token.end(),
                                      started_ms=start, ended_ms=end))
        token_offset += len(tokens)

    for key, expected in (("ctc_fallback_windows", len(fallback_sources)), ("aligned_words", len(words))):
        if key in metrics:
            _require(type(metrics[key]) is int and metrics[key] == expected,
                     "package_alignment_counter_mismatch")

    _require([m.group() for m in token_matches] ==
             [word for segment in parsed.segment_items for word in segment.text.split()],
             "package_bridge_changed_source_tokens")
    associations: list[SegmentSources] = []
    offset = 0
    for segment in parsed.segment_items:
        count = len(segment.text.split())
        associations.append(SegmentSources(ordinal=segment.ordinal,
            source_segment_ids=list(dict.fromkeys(token_owners[offset:offset + count]))))
        offset += count
    for source in fallback_sources:
        matches = [(segment, mapping) for segment, mapping in zip(parsed.segment_items, associations)
                   if source.segment_id in mapping.source_segment_ids]
        _require(len(matches) == 1, "package_fallback_requires_original_segment")
        segment, mapping = matches[0]
        _require(mapping.source_segment_ids == [source.segment_id]
                 and (segment.text, segment.started_ms, segment.ended_ms)
                 == (source.text, source.started_ms, source.ended_ms),
                 "package_fallback_requires_original_segment")
    labels = {s.speaker_label for s in [*parsed.segment_items, *parsed.speaker_turns]}
    kinds = {label: _speaker_kind(label) for label in labels}
    status = "complete" if not missing else ("partial" if words else "unavailable")
    sidecar = LocalAsrArtifactsV1(
        audio_ref=f"meeting:{meeting_id}", input_sha256=proof.input_sha256,
        duration_ms=duration_ms, selected_text=selected_text,
        selected_text_sha256=text_fingerprint(selected_text), source_segments=sources,
        segment_sources=associations, words=words,
        alignment=AlignmentDiagnostics(status=status,
            reason_codes=["package_alignment_fallback"] if missing else [],
            unsupported_token_indices=missing, tokenizer_compatibility=proof.tokenizer_compatibility),
        speaker_kinds=kinds, identity=identity,
        warnings=["package_alignment_fallback"] if missing else [],
        fallback_counters={"ctc_fallback_windows": len(fallback_sources)},
        # This receipt attests only pure adapter validation. The caller retains
        # the proof document and its snapshots; it is not an inference receipt.
        runtime_receipts=[*proof.runtime_receipts, RuntimeReceipt(
            stage="package_adapter_validation", status="succeeded",
            receipt_sha256=package_document_digest(proof.model_dump(mode="json")))],
    )
    return ImportLocalAsrRun(operation_id=operation_id, transcript_version=transcript_version,
                             result=parsed, artifacts=sidecar)
