from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sys
import tempfile
import wave

from app.infrastructure.config.settings import settings

logger = logging.getLogger(__name__)
_diarization_pipeline_init_error: Exception | None = None


def _build_diarization_pipeline():
    global _diarization_pipeline_init_error
    if not settings.pyannote_auth_token:
        _diarization_pipeline_init_error = RuntimeError(
            "PYANNOTE_AUTH_TOKEN is not configured"
        )
        return None
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        _diarization_pipeline_init_error = RuntimeError(
            "pyannote.audio is not installed"
        )
        return None
    try:
        return Pipeline.from_pretrained(
            settings.pyannote_model_id,
            token=settings.pyannote_auth_token,
        )
    except Exception as exc:  # noqa: BLE001
        _diarization_pipeline_init_error = exc
        logger.warning("diarization_pipeline_preload_failed: %s", exc)
        return None


def _warmup_diarization_pipeline(pipeline) -> None:
    if pipeline is None:
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
        pipeline(str(tmp_path))
        logger.info("diarization_pipeline_warmup_done")
    except Exception as exc:  # noqa: BLE001
        logger.warning("diarization_pipeline_warmup_failed: %s", exc)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


_diarization_pipeline = _build_diarization_pipeline()
_warmup_diarization_pipeline(_diarization_pipeline)


@dataclass
class DiarizationInterval:
    speaker_label: str
    start_sec: float
    end_sec: float


def diarize_audio_file(audio_file_path: str) -> list[DiarizationInterval]:
    pipeline = _diarization_pipeline
    if pipeline is None:
        if _diarization_pipeline_init_error is not None:
            raise RuntimeError("Diarization pipeline is not available") from _diarization_pipeline_init_error
        raise RuntimeError("Diarization pipeline is not available")
    diarization = pipeline(audio_file_path)
    if hasattr(diarization, "itertracks"):
        iterator = diarization.itertracks(yield_label=True)
    else:
        iterator = diarization.speaker_diarization.itertracks(yield_label=True)

    results: list[DiarizationInterval] = []
    for turn, _track, speaker in iterator:
        results.append(
            DiarizationInterval(
                speaker_label=speaker,
                start_sec=float(turn.start),
                end_sec=float(turn.end),
            )
        )
    if not results:
        raise RuntimeError("Diarization produced no speaker intervals")
    return results
