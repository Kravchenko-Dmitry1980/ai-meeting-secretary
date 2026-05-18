from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sys
import tempfile
import wave

from faster_whisper import WhisperModel

from app.infrastructure.config.settings import settings

logger = logging.getLogger(__name__)
_whisper_model_init_error: Exception | None = None


def _build_whisper_model() -> WhisperModel | None:
    global _whisper_model_init_error
    try:
        return WhisperModel(
            settings.whisper_model_size,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
    except Exception as exc:  # noqa: BLE001
        _whisper_model_init_error = exc
        logger.warning("whisper_model_preload_failed: %s", exc)
        return None


def _warmup_whisper_model(model: WhisperModel | None) -> None:
    if model is None:
        return
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        return
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_path = Path(tmp_file.name)
        with wave.open(str(tmp_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00" * 1600)  # 100ms silence
        model.transcribe(str(tmp_path), beam_size=1)
        logger.info("whisper_model_warmup_done")
    except Exception as exc:  # noqa: BLE001
        logger.warning("whisper_model_warmup_failed: %s", exc)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


_whisper_model: WhisperModel | None = _build_whisper_model()
_warmup_whisper_model(_whisper_model)


@dataclass
class SttSegment:
    start_sec: float
    end_sec: float
    text: str


def _get_model() -> WhisperModel:
    global _whisper_model
    if _whisper_model is None:
        _whisper_model = _build_whisper_model()
    if _whisper_model is None:
        if _whisper_model_init_error is not None:
            raise RuntimeError("Whisper model is not available") from _whisper_model_init_error
        raise RuntimeError("Whisper model is not available")
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
