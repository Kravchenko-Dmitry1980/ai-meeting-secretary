from app.services.diarization_service import DiarizationInterval
from app.services.transcription_service import SttSegment
from app.services.v2_contracts import TranscriptSegmentV2

_MIN_SEGMENT_LEN_SEC = 0.05


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _sanitize_bounds(start: float, end: float) -> tuple[float, float]:
    start_norm = max(0.0, float(start))
    end_norm = max(0.0, float(end))
    if end_norm <= start_norm:
        end_norm = start_norm + _MIN_SEGMENT_LEN_SEC
    return start_norm, end_norm


def _choose_speaker(
    start: float,
    end: float,
    intervals: list[DiarizationInterval],
) -> str:
    if not intervals:
        return "SPEAKER_01"

    best_label = "SPEAKER_01"
    best_overlap = 0.0
    for interval in intervals:
        value = _overlap(start, end, interval.start_sec, interval.end_sec)
        if value > best_overlap:
            best_overlap = value
            best_label = interval.speaker_label
    if best_overlap > 0:
        return best_label

    center = (start + end) / 2.0
    nearest = min(
        intervals,
        key=lambda item: abs(center - ((item.start_sec + item.end_sec) / 2.0)),
    )
    return nearest.speaker_label


def align_stt_with_diarization(
    stt_segments: list[SttSegment],
    diarization_intervals: list[DiarizationInterval],
) -> list[TranscriptSegmentV2]:
    aligned: list[TranscriptSegmentV2] = []
    for stt in stt_segments:
        start, end = _sanitize_bounds(stt.start_sec, stt.end_sec)
        cleaned_raw = stt.text.strip()
        if not cleaned_raw:
            continue
        speaker = _choose_speaker(start, end, diarization_intervals)
        aligned.append(
            TranscriptSegmentV2(
                speaker=speaker,
                start=start,
                end=end,
                text_raw=cleaned_raw,
                text_clean=cleaned_raw,
            )
        )
    return aligned
