"""Saved-text alignment seam, independent of transcription/model libraries.

This module only prepares requests and validates returned evidence. Calling a
real backend belongs to a separately authorized, qualified worker. In particular,
Whisper text is never assumed compatible with the pinned GigaAM CTC tokenizer.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from secretary.domain.local_asr import AlignmentDiagnostics, SourceSegment, WordSpan, text_fingerprint


@dataclass(frozen=True)
class AlignmentWindow:
    started_ms: int
    ended_ms: int

    def __post_init__(self):
        if (type(self.started_ms) is not int or type(self.ended_ms) is not int
                or self.started_ms < 0 or not 0 < self.ended_ms - self.started_ms <= 24_000):
            raise ValueError("Pinned alignment windows require integer ms and at most 24 seconds")


@dataclass(frozen=True)
class SavedTextAlignmentRequest:
    selected_text: str
    selected_text_sha256: str
    # Canonical JSON freezes original text, channel and segment timing together.
    source_segments_json: tuple[str, ...]
    duration_ms: int
    windows: tuple[AlignmentWindow, ...]


@dataclass(frozen=True)
class SavedTextAlignmentResult:
    request: SavedTextAlignmentRequest
    words: tuple[WordSpan, ...]
    diagnostics: AlignmentDiagnostics


class ExistingTextAligner(Protocol):
    def align(self, request: SavedTextAlignmentRequest) -> SavedTextAlignmentResult: ...


def prepare_saved_text(source_segments: list[SourceSegment], duration_ms: int,
                       windows: tuple[AlignmentWindow, ...] = ()) -> SavedTextAlignmentRequest:
    if not source_segments or type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("Existing transcript and positive input duration required")
    source = [SourceSegment.model_validate(item.model_dump(mode="json")) for item in source_segments]
    if any(item.ended_ms is not None and item.ended_ms > duration_ms for item in source):
        raise ValueError("Source interval outside input duration")
    if any(window.ended_ms > duration_ms for window in windows):
        raise ValueError("Alignment window outside input duration")
    text = "\n".join(item.text for item in source)
    return SavedTextAlignmentRequest(text, text_fingerprint(text), tuple(s.model_dump_json() for s in source),
                                     duration_ms, tuple(windows))


def unavailable_alignment(request: SavedTextAlignmentRequest, reason: str = "saved_text_tokenizer_not_qualified"):
    return SavedTextAlignmentResult(request, (), AlignmentDiagnostics(
        status="unavailable", reason_codes=[reason], tokenizer_compatibility="unverified"))


def validate_saved_alignment(request: SavedTextAlignmentRequest, proposal: SavedTextAlignmentResult):
    """Fail to original segment timing, never to another recognizer's words."""
    if proposal.request != request:
        return unavailable_alignment(request, "alignment_changed_source")
    if not request.windows:
        return unavailable_alignment(request, "alignment_windows_not_verified")
    try:
        words = tuple(WordSpan.model_validate(w.model_dump(mode="json")) for w in proposal.words)
        diagnostics = AlignmentDiagnostics.model_validate(proposal.diagnostics.model_dump(mode="json"))
    except ValueError:
        return unavailable_alignment(request, "alignment_invalid_evidence")
    tokens = list(re.finditer(r"\S+", request.selected_text))
    indices = [word.token_index for word in words]
    if diagnostics.tokenizer_compatibility != "verified":
        return unavailable_alignment(request, "saved_text_tokenizer_not_qualified")
    if indices != sorted(set(indices)):
        return unavailable_alignment(request, "alignment_invalid_token_indices")
    for word in words:
        if word.token_index >= len(tokens):
            return unavailable_alignment(request, "alignment_invalid_token_indices")
        token = tokens[word.token_index]
        if (word.text, word.char_start, word.char_end) != (token.group(), token.start(), token.end()):
            return unavailable_alignment(request, "alignment_changed_source")
        if not any(w.started_ms <= word.started_ms < word.ended_ms <= w.ended_ms for w in request.windows):
            return unavailable_alignment(request, "alignment_word_outside_window")
    unsupported = diagnostics.unsupported_token_indices
    if len(unsupported) != len(set(unsupported)) or any(i >= len(tokens) for i in unsupported):
        return unavailable_alignment(request, "alignment_invalid_token_indices")
    if diagnostics.status == "complete" and (indices != list(range(len(tokens))) or unsupported):
        return unavailable_alignment(request, "alignment_missing_tokens")
    if diagnostics.status == "unavailable" and words:
        return unavailable_alignment(request, "alignment_invalid_evidence")
    if diagnostics.status != "complete" and not diagnostics.reason_codes:
        return unavailable_alignment(request, "alignment_missing_diagnostics")
    return SavedTextAlignmentResult(request, words, diagnostics)
