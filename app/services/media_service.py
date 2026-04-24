import subprocess
from pathlib import Path

from app.infrastructure.config.settings import settings


VIDEO_EXTENSIONS = {".mp4", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".wav"}


def prepare_audio_file(media_file_path: str) -> str:
    media_path = Path(media_file_path)
    extension = media_path.suffix.lower()
    if extension in AUDIO_EXTENSIONS:
        return str(media_path)

    if extension not in VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported media format: {extension}")

    audio_path = media_path.with_suffix(".wav")
    command = [
        settings.ffmpeg_binary,
        "-y",
        "-i",
        str(media_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        str(audio_path),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")
    return str(audio_path)
