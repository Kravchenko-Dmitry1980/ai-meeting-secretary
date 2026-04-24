from dataclasses import dataclass

from app.infrastructure.config.settings import settings


@dataclass
class DiarizationInterval:
    speaker_label: str
    start_sec: float
    end_sec: float


def diarize_audio_file(audio_file_path: str) -> list[DiarizationInterval]:
    if not settings.pyannote_auth_token:
        raise RuntimeError("PYANNOTE_AUTH_TOKEN is not configured")

    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise RuntimeError("pyannote.audio is not installed") from exc

    pipeline = Pipeline.from_pretrained(
        settings.pyannote_model_id,
        use_auth_token=settings.pyannote_auth_token,
    )
    diarization = pipeline(audio_file_path)
    results: list[DiarizationInterval] = []
    for turn, _track, speaker in diarization.itertracks(yield_label=True):
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
