from __future__ import annotations

import asyncio
import hashlib
import json
import math
import wave
from pathlib import Path
from uuid import uuid4


async def prepare_audio(settings, source: Path, directory: Path, *, cancelled=lambda: False) -> list[dict]:
    """Decode to bounded PCM WAV chunks. ffmpeg streams; Python never loads audio."""
    directory.mkdir(parents=True, exist_ok=True)
    # Each attempt owns its files; stale interrupted output never joins new audio.
    temporary = directory / ("building-" + uuid4().hex)
    temporary.mkdir()
    probe = await asyncio.create_subprocess_exec(settings.ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(probe.communicate(), timeout=60)
    except BaseException:
        probe.kill()
        await probe.wait()
        raise
    if probe.returncode:
        raise ValueError("Audio/video cannot be decoded: " + error.decode(errors="replace")[-1000:])
    try:
        duration = float(json.loads(output)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        raise ValueError("Recording duration is unavailable") from None
    if not math.isfinite(duration) or duration <= 0 or duration > 4 * 60 * 60 + 1:
        raise ValueError("Recording must have duration between 0 and 4 hours")
    process = await asyncio.create_subprocess_exec(settings.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "segment", "-segment_time", str(settings.chunk_seconds), "-reset_timestamps", "1", str(temporary / "part-%06d.wav"), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    # communicate drains error output concurrently, avoiding pipe deadlocks.
    completion = asyncio.create_task(process.communicate())
    started = asyncio.get_running_loop().time()
    try:
        while not completion.done():
            if asyncio.get_running_loop().time() - started > 1800:
                process.kill()
                await completion
                raise ValueError("Audio preparation exceeded 30 minute limit")
            if cancelled():
                process.terminate()
                await completion
                raise asyncio.CancelledError("Preparation cancelled")
            await asyncio.sleep(0.15)
        _, error = await completion
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise ValueError("Audio preparation failed: " + error.decode(errors="replace")[-1000:])
    chunks, offset = [], 0
    for sequence, path in enumerate(sorted(temporary.glob("part-*.wav"))):
        with wave.open(str(path), "rb") as audio:
            duration_ms = round(audio.getnframes() / audio.getframerate() * 1000)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        final = directory / path.name
        path.replace(final)
        chunks.append({"path": str(final), "sequence": sequence, "channel": "import", "offset_ms": offset, "duration_ms": duration_ms, "sha256": digest})
        offset += duration_ms
    if not chunks:
        raise ValueError("No audio stream found")
    temporary.rmdir()
    return chunks


async def cloud_audio(settings, chunk: dict) -> Path:
    """Create bounded 16 kHz mono derivative; preserve native capture original."""
    source = Path(chunk["path"])
    with wave.open(str(source), "rb") as audio:
        if audio.getnchannels() == 1 and audio.getframerate() == 16000 and audio.getsampwidth() == 2:
            return source
    directory = settings.data_dir / "stt" / chunk["meeting_id"]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{chunk['id']}.wav"
    if target.exists():
        return target
    partial = target.with_suffix(".partial")
    process = await asyncio.create_subprocess_exec(settings.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(partial), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, error = await asyncio.wait_for(process.communicate(), 120)
    except BaseException:
        process.kill()
        await process.wait()
        partial.unlink(missing_ok=True)
        raise
    if process.returncode:
        partial.unlink(missing_ok=True)
        raise ValueError("Cloud audio normalization failed: " + error.decode(errors="replace")[-500:])
    partial.replace(target)
    return target

