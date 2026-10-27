"""Frozen transport and application sidecars for model-free local ASR imports.

The v2 transport is unchanged. Words, source associations and runtime identity
belong to a separately versioned Secretary artifact, never invented v2 fields.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from secretary.domain.speakers import DTO, Identifier, Revision

MAX_DURATION_MS = 604_800_000
Millis = Annotated[int, Field(strict=True, ge=0, le=MAX_DURATION_MS)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Label = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Text = Annotated[str, Field(strict=True, max_length=10_000_000)]
Code = Annotated[str, Field(strict=True, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:/+-]+$")]


class LocalAsrError(ValueError):
    pass


class LocalAsrConflict(LocalAsrError):
    pass


class LocalAsrNotFound(LocalAsrError):
    pass


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seconds_to_ms(value: float | int) -> int:
    """Explicit round-half-up conversion; booleans/nonfinite/range overflow fail."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Finite numeric seconds required")
    if value < 0 or value > MAX_DURATION_MS / 1000:
        raise ValueError("Seconds outside supported bounds")
    return int((Decimal(str(value)) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class LocalAsrTurn(DTO):
    ordinal: Revision
    speaker_label: Label
    started_ms: Millis
    ended_ms: Millis

    @model_validator(mode="after")
    def interval(self):
        if self.started_ms >= self.ended_ms:
            raise ValueError("started_ms must precede ended_ms")
        return self


class LocalAsrSegment(LocalAsrTurn):
    text: Text


class LocalAsrV2(DTO):
    schema_version: Literal["neurohr.local-asr.v2"] = "neurohr.local-asr.v2"
    provider: Label
    language: Annotated[str, Field(strict=True, min_length=1, max_length=20)]
    input_sha256: Digest
    transcript_text: Text
    segments: Revision
    segment_items: list[LocalAsrSegment] = Field(max_length=200_000)
    speaker_turns: list[LocalAsrTurn] = Field(default_factory=list, max_length=500_000)
    warnings: list[str] = Field(default_factory=list, max_length=1000)
    metrics: dict = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def finite_metrics(cls, value):
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Metrics must be finite")
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("Metric keys must be strings")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif item is not None and not isinstance(item, (str, bool, int, float)):
                raise ValueError("Metrics must be JSON values")
        return value

    @model_validator(mode="after")
    def counts(self):
        if self.segments != len(self.segment_items):
            raise ValueError("segments must equal segment_items count")
        for items in (self.segment_items, self.speaker_turns):
            if [item.ordinal for item in items] != list(range(len(items))):
                raise ValueError("Ordinals must be contiguous in source order")
        return self


class SourceSegment(DTO):
    segment_id: Identifier
    text: Text
    channel: Code
    started_ms: Millis | None
    ended_ms: Millis | None

    @model_validator(mode="after")
    def interval(self):
        if (self.started_ms is None) != (self.ended_ms is None):
            raise ValueError("Source interval must be entirely known or unknown")
        if self.started_ms is not None and self.started_ms >= self.ended_ms:
            raise ValueError("Invalid source interval")
        return self


class SegmentSources(DTO):
    ordinal: Revision
    source_segment_ids: list[Identifier] = Field(min_length=1, max_length=10000)

    @field_validator("source_segment_ids")
    @classmethod
    def unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Duplicate source segment")
        return value


class WordSpan(DTO):
    token_index: Revision
    text: Annotated[str, Field(strict=True, min_length=1, max_length=10000)]
    char_start: Revision
    char_end: Revision
    started_ms: Millis
    ended_ms: Millis

    @model_validator(mode="after")
    def interval(self):
        if self.char_start >= self.char_end or self.started_ms >= self.ended_ms:
            raise ValueError("Word character and time spans must have positive length")
        return self


class AlignmentDiagnostics(DTO):
    status: Literal["complete", "partial", "unavailable"]
    reason_codes: list[Code] = Field(default_factory=list, max_length=1000)
    unsupported_token_indices: list[Revision] = Field(default_factory=list, max_length=200000)
    # Explicitly unknown until the pinned adapter has checked this text/tokenizer.
    tokenizer_compatibility: Literal["verified", "unverified", "unsupported"] = "unverified"


class RuntimeModel(DTO):
    role: Literal["rnnt", "ctc", "diarizer", "identity"]
    model_id: Code
    revision: Code
    weights_sha256: Digest
    loader_sha256: Digest
    processor_sha256: Digest
    custom_code_sha256: Digest


class RuntimeIdentity(DTO):
    profile: Literal["nemotron-ctc"] = "nemotron-ctc"
    profile_digest: Digest
    code_digest: Digest
    device: Code
    precision: Code
    models: list[RuntimeModel] = Field(min_length=1, max_length=10)
    # Older sidecars remain importable evidence. Publishing a newly executed
    # result additionally requires these exact stage/device identities.
    asr_device: Literal["cpu", "cuda"] | None = None
    diarization_device: Literal["cpu", "cuda"] | None = None
    physical_gpu_id: Code | None = None

    @model_validator(mode="after")
    def device_and_model_identity(self):
        if len({model.role for model in self.models}) != len(self.models):
            raise ValueError("Duplicate model role in runtime identity")
        if (self.asr_device is None) != (self.diarization_device is None):
            raise ValueError("Both stage device identities must be present or unknown")
        if "cuda" in (self.asr_device, self.diarization_device) and self.physical_gpu_id is None:
            raise ValueError("CUDA stages require physical GPU identity")
        return self


def model_manifest_digest(models: list[RuntimeModel] | list[dict]) -> str:
    """Full model/loader/processor/custom-code identity, independent of order."""
    values = [RuntimeModel.model_validate(model.model_dump(mode="json") if isinstance(model, RuntimeModel) else model)
              .model_dump(mode="json") for model in models]
    if len({model["role"] for model in values}) != len(values):
        raise ValueError("Duplicate model role in runtime identity")
    values.sort(key=lambda model: (model["role"], model["model_id"], model["revision"]))
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RuntimeReceipt(DTO):
    stage: Code
    receipt_sha256: Digest
    status: Literal["succeeded", "fallback", "not_run"]
    elapsed_ms: Revision | None = None


class LocalAsrArtifactsV1(DTO):
    schema_version: Literal["secretary.local-asr-artifacts.v1"] = "secretary.local-asr-artifacts.v1"
    audio_ref: Annotated[str, Field(strict=True, pattern=r"^meeting:[A-Za-z0-9_-]{1,128}$")]
    input_sha256: Digest
    duration_ms: Millis
    selected_text: Text
    selected_text_sha256: Digest
    source_segments: list[SourceSegment] = Field(min_length=1, max_length=200000)
    segment_sources: list[SegmentSources] = Field(min_length=1, max_length=200000)
    words: list[WordSpan] = Field(default_factory=list, max_length=1000000)
    alignment: AlignmentDiagnostics
    speaker_kinds: dict[Label, Literal["single", "unknown", "composite"]]
    identity: RuntimeIdentity
    warnings: list[Code] = Field(default_factory=list, max_length=1000)
    fallback_counters: dict[Code, Revision] = Field(default_factory=dict)
    runtime_receipts: list[RuntimeReceipt] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def integrity(self):
        if self.duration_ms == 0:
            raise ValueError("Positive duration required")
        if text_fingerprint(self.selected_text) != self.selected_text_sha256:
            raise ValueError("Selected text fingerprint mismatch")
        if self.selected_text != "\n".join(segment.text for segment in self.source_segments):
            raise ValueError("Selected text must exactly preserve the source projection")
        ids = [segment.segment_id for segment in self.source_segments]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate source segment")
        if any(segment.ended_ms is not None and segment.ended_ms > self.duration_ms
               for segment in self.source_segments):
            raise ValueError("Source interval outside input duration")
        if [item.ordinal for item in self.segment_sources] != list(range(len(self.segment_sources))):
            raise ValueError("Source mappings must have contiguous ordinals")
        referenced = {source for item in self.segment_sources for source in item.source_segment_ids}
        if referenced != set(ids):
            raise ValueError("Source mapping must cover exactly the source segments")
        matches = list(re.finditer(r"\S+", self.selected_text))
        indices = [word.token_index for word in self.words]
        if indices != sorted(set(indices)):
            raise ValueError("Word token indices must be unique and ordered")
        for word in self.words:
            if word.token_index >= len(matches):
                raise ValueError("Word index outside selected text")
            match = matches[word.token_index]
            if (word.char_start, word.char_end, word.text) != (match.start(), match.end(), match.group()):
                raise ValueError("Word span changed the selected token")
            if word.ended_ms > self.duration_ms:
                raise ValueError("Word outside input duration")
        unsupported = self.alignment.unsupported_token_indices
        if len(set(unsupported)) != len(unsupported) or any(i >= len(matches) for i in unsupported):
            raise ValueError("Invalid unsupported token index")
        if self.alignment.status == "complete":
            if indices != list(range(len(matches))) or unsupported or self.alignment.tokenizer_compatibility != "verified":
                raise ValueError("Complete alignment requires every verified token")
        elif not self.alignment.reason_codes:
            raise ValueError("Incomplete alignment requires an explicit diagnostic")
        if self.alignment.status == "unavailable" and self.words:
            raise ValueError("Unavailable alignment must not claim word timestamps")
        return self


class ImportLocalAsrRun(DTO):
    operation_id: Identifier
    transcript_version: Annotated[int, Field(strict=True, ge=1)]
    result: LocalAsrV2
    artifacts: LocalAsrArtifactsV1

    @model_validator(mode="after")
    def combined(self):
        result, sidecar = self.result, self.artifacts
        if result.input_sha256 != sidecar.input_sha256:
            raise ValueError("Input hashes differ")
        if len(sidecar.segment_sources) != result.segments:
            raise ValueError("Every assembled segment needs source associations")
        if re.findall(r"\S+", "\n".join(item.text for item in result.segment_items)) != re.findall(r"\S+", sidecar.selected_text):
            raise ValueError("Assembled segments changed selected text tokens")
        rendered = "\n".join(f"{s.speaker_label}: {s.text}" for s in result.segment_items)
        if result.transcript_text.rstrip("\n") not in (sidecar.selected_text.rstrip("\n"), rendered):
            raise ValueError("transcript_text must render the conserved segments")
        # Derive associations from conserved token positions instead of trusting
        # a producer to relabel an already manually confirmed source segment.
        token_owners = [source.segment_id for source in sidecar.source_segments
                        for _ in re.finditer(r"\S+", source.text)]
        offset = 0
        for segment, mapping in zip(result.segment_items, sidecar.segment_sources):
            count = len(re.findall(r"\S+", segment.text))
            expected = list(dict.fromkeys(token_owners[offset:offset + count]))
            if not count or mapping.source_segment_ids != expected:
                raise ValueError("Source association must match conserved token positions")
            offset += count
            if sidecar.alignment.status == "unavailable":
                sources = [source for source in sidecar.source_segments
                           if source.segment_id in mapping.source_segment_ids]
                if len(sources) != 1 or (segment.text, segment.started_ms, segment.ended_ms) != (
                        sources[0].text, sources[0].started_ms, sources[0].ended_ms):
                    raise ValueError("Unavailable alignment must retain original segments and times")
        for item in [*result.segment_items, *result.speaker_turns]:
            if item.ended_ms > sidecar.duration_ms:
                raise ValueError("Interval outside input duration")
            if item.speaker_label not in sidecar.speaker_kinds:
                raise ValueError("Every speaker label needs explicit semantics")
            if sidecar.speaker_kinds[item.speaker_label] == "single" and is_ambiguous_label(item.speaker_label):
                raise ValueError("Unknown/composite labels cannot claim a single speaker")
        return self


def is_ambiguous_label(label: str) -> bool:
    return bool(re.search(r"unknown|неизвест|overlap|mixed|[+|&,;/]", label, re.IGNORECASE))


class ReviewLocalAsrCluster(DTO):
    operation_id: Identifier
    expected_review_revision: Revision
    speaker_label: Label
    shown_segment_ordinals: list[Revision] = Field(min_length=1, max_length=200000)
    excluded_segment_ordinals: list[Revision] = Field(default_factory=list, max_length=200000)
    participant_id: Identifier
    listening_confirmed: StrictBool

    @model_validator(mode="after")
    def explicit(self):
        if not self.listening_confirmed:
            raise ValueError("Human listening confirmation is required")
        for values in (self.shown_segment_ordinals, self.excluded_segment_ordinals):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate segment ordinal")
        if not set(self.excluded_segment_ordinals) <= set(self.shown_segment_ordinals):
            raise ValueError("Excluded segments must have been shown")
        return self


class ReviewLocalAsrSegment(DTO):
    operation_id: Identifier
    expected_review_revision: Revision
    segment_ordinal: Revision
    participant_id: Identifier | None
    listening_confirmed: StrictBool

    @model_validator(mode="after")
    def explicit(self):
        if not self.listening_confirmed:
            raise ValueError("Human listening confirmation is required")
        return self
