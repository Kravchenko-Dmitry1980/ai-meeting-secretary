from dataclasses import dataclass

from faster_whisper import WhisperModel

from app.infrastructure.config.settings import settings

_whisper_model: WhisperModel | None = None


@dataclass
class SttSegment:
    start_sec: float
    end_sec: float
    text: str


def _get_model() -> WhisperModel:
    global _whisper_model
    if _whisper_model is None:
        _whisper_model = WhisperModel(
            settings.whisper_model_size,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
    return _whisper_model


def transcribe_audio_file(audio_file_path: str) -> tuple[str, list[SttSegment]]:
    model = _get_model()
    segments, _info = model.transcribe(audio_file_path, beam_size=5)
    lines: list[str] = []
    normalized_segments: list[SttSegment] = []
    for segment in segments:
        cleaned = segment.text.strip()
        if cleaned:
            lines.append(cleaned)
            normalized_segments.append(
                SttSegment(
                    start_sec=float(segment.start),
                    end_sec=float(segment.end),
                    text=cleaned,
                )
            )
    return " ".join(lines), normalized_segments
