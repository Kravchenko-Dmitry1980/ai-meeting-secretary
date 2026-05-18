from app.services.v2_contracts import TranscriptSegmentV2

_FILLER_WORDS = {"ээ", "эм", "ну", "как бы", "типа", "в общем"}


def _cleanup_text(value: str) -> str:
    text = " ".join(value.split())
    lowered = text.lower()
    if lowered in _FILLER_WORDS:
        return ""
    if text and text[-1] not in ".!?":
        text = f"{text}."
    return text


def clean_transcript_segments(
    segments: list[TranscriptSegmentV2],
) -> list[TranscriptSegmentV2]:
    cleaned: list[TranscriptSegmentV2] = []
    for segment in segments:
        text_clean = _cleanup_text(segment.text_raw)
        if not text_clean:
            continue
        cleaned.append(
            TranscriptSegmentV2(
                speaker=segment.speaker,
                start=segment.start,
                end=segment.end,
                text_raw=segment.text_raw,
                text_clean=text_clean,
            )
        )
    return cleaned


def build_readable_transcript(segments: list[TranscriptSegmentV2]) -> str:
    lines = [
        f"{segment.speaker} [{segment.start:.2f}-{segment.end:.2f}]: {segment.text_clean}"
        for segment in segments
    ]
    return "\n".join(lines)
