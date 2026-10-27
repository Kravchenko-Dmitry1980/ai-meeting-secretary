from dataclasses import replace

import pytest

from secretary.application.local_asr_alignment import (AlignmentWindow, SavedTextAlignmentResult,
    prepare_saved_text, unavailable_alignment, validate_saved_alignment)
from secretary.domain.local_asr import AlignmentDiagnostics, SourceSegment, WordSpan


def evidence():
    source = SourceSegment(segment_id="s1", text="  Привет,  мир!", channel="import", started_ms=0, ended_ms=2000)
    request = prepare_saved_text([source], 2000, (AlignmentWindow(0, 2000),))
    words = (WordSpan(token_index=0, text="Привет,", char_start=2, char_end=9, started_ms=100, ended_ms=700),
             WordSpan(token_index=1, text="мир!", char_start=11, char_end=15, started_ms=800, ended_ms=1800))
    # Keep exact character offsets checked by the same contract as producer output.
    words = tuple(w.model_copy(update={"char_start": request.selected_text.index(w.text),
                                      "char_end": request.selected_text.index(w.text) + len(w.text)}) for w in words)
    return request, SavedTextAlignmentResult(request, words, AlignmentDiagnostics(status="complete", tokenizer_compatibility="verified"))


def test_exact_text_and_source_segment_timing_survive_valid_alignment_and_fallback():
    request, result = evidence()
    assert validate_saved_alignment(request, result) == result
    fallback = unavailable_alignment(request)
    assert fallback.words == () and fallback.request == request
    assert fallback.diagnostics.status == "unavailable"
    assert SourceSegment.model_validate_json(fallback.request.source_segments_json[0]).ended_ms == 2000


@pytest.mark.parametrize("bad", ["text", "windows", "tokens", "tokenizer", "missing"])
def test_unqualified_or_corrupt_alignment_preserves_original(bad):
    request, result = evidence()
    if bad == "text":
        result = replace(result, words=(result.words[0].model_copy(update={"text": "подмена"}), result.words[1]))
    elif bad == "windows":
        result = replace(result, words=(result.words[0], result.words[1].model_copy(update={"ended_ms": 3000})))
    elif bad == "tokens":
        result = replace(result, words=(result.words[1], result.words[0]))
    elif bad == "tokenizer":
        result = replace(result, diagnostics=AlignmentDiagnostics(status="complete", tokenizer_compatibility="unverified"))
    else:
        result = replace(result, words=(result.words[0],))
    safe = validate_saved_alignment(request, result)
    assert safe.request == request and not safe.words
    assert safe.diagnostics.reason_codes


def test_window_boundary_and_no_implicit_inference():
    for bounds in ((0, 24001), (0, float("inf")), (True, 1000), (-1, 10), (10, 10)):
        with pytest.raises(ValueError):
            AlignmentWindow(*bounds)
    request, result = evidence()
    changed = replace(request, windows=())
    assert validate_saved_alignment(changed, replace(result, request=changed)).diagnostics.status == "unavailable"
    assert validate_saved_alignment(request, replace(result, request=replace(request, selected_text="changed"))).request == request
