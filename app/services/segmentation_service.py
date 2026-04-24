from dataclasses import dataclass

from app.services.diarization_service import DiarizationInterval
from app.services.transcription_service import SttSegment


@dataclass
class SpeakerSegment:
    speaker_label: str
    start_sec: float
    end_sec: float
    text: str


def _interval_overlap(
    a_start: float,
    a_end: float,
    b_start: float,
    b_end: float,
) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers_to_stt_segments(
    stt_segments: list[SttSegment],
    diarization_intervals: list[DiarizationInterval],
) -> list[SpeakerSegment]:
    labeled_segments: list[SpeakerSegment] = []
    for stt_segment in stt_segments:
        best_label = "SPEAKER_01"
        best_overlap = -1.0
        for interval in diarization_intervals:
            overlap = _interval_overlap(
                stt_segment.start_sec,
                stt_segment.end_sec,
                interval.start_sec,
                interval.end_sec,
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = interval.speaker_label
        labeled_segments.append(
            SpeakerSegment(
                speaker_label=best_label,
                start_sec=stt_segment.start_sec,
                end_sec=stt_segment.end_sec,
                text=stt_segment.text,
            )
        )
    return labeled_segments


def build_speaker_aware_transcript(
    speaker_segments: list[SpeakerSegment],
) -> str:
    lines = [
        (
            f"{segment.speaker_label} [{segment.start_sec:.2f}-"
            f"{segment.end_sec:.2f}]: {segment.text}"
        )
        for segment in speaker_segments
    ]
    return "\n".join(lines)
