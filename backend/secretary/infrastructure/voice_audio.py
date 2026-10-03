"""Enrollment decoding is separate from long meeting audio preparation."""
from __future__ import annotations

import array
import io
import math
import wave

from secretary.domain.voice import Excerpt, MAX_PAYLOAD, MAX_SAMPLES, PreparedAudio, SAMPLE_RATE, VoiceError
from secretary.infrastructure.voice_process import run_owned


def wav_bytes(pcm16: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm16)
    return output.getvalue()


def screen_pcm(pcm16: bytes) -> PreparedAudio:
    if len(pcm16) % 2:
        raise VoiceError("truncated_audio")
    if len(pcm16) > MAX_SAMPLES * 2:
        raise VoiceError("overlong_audio")
    samples = array.array("h")
    samples.frombytes(pcm16)
    if len(samples) < 15 * SAMPLE_RATE:
        raise VoiceError("insufficient_usable_speech")
    # Conservative technical policy, not VAD or one-speaker proof. 20ms frames,
    # RMS >= 0.01 full scale and <= 1% samples at >= 0.999 full scale.
    if sum(abs(x) >= 32734 for x in samples) / len(samples) > 0.01:
        raise VoiceError("clipped_audio")
    frame = 320
    usable = [math.sqrt(sum(x*x for x in samples[i:i+frame]) / frame) >= 327.68
              for i in range(0, len(samples) - frame + 1, frame)]
    if not any(usable):
        raise VoiceError("silent_audio")
    excerpts = []
    start, end = 0, len(usable) * frame
    # Preserve pauses and original timeline rather than stitching active frames.
    # Enrollment is <=30s, so at most three windows cover it. Count actual active
    # frames separately; window duration is not asserted to be speech duration.
    while end - start >= 5 * SAMPLE_RATE and len(excerpts) < 3:
        length = min(10 * SAMPLE_RATE, end - start)
        if 0 < end - start - length < 5 * SAMPLE_RATE:
            length = end - start - 5 * SAMPLE_RATE
        excerpts.append(Excerpt(start, start + length))
        start += length
    usable_samples = sum(usable[:start//frame]) * frame
    if usable_samples < 15 * SAMPLE_RATE:
        raise VoiceError("insufficient_usable_speech")
    return PreparedAudio(pcm16, tuple(excerpts), usable_samples)


class EnrollmentDecoder:
    def __init__(self, *, ffmpeg_path: str = "ffmpeg", runner=run_owned):
        self.ffmpeg_path, self.runner = ffmpeg_path, runner

    def decode(self, payload: bytes, *, cancel=None) -> PreparedAudio:
        """Bytes only. Caller may read bounded server-owned staging; never paths/URLs."""
        if not isinstance(payload, bytes) or not payload:
            raise VoiceError("invalid_audio")
        if len(payload) > MAX_PAYLOAD:
            raise VoiceError("audio_payload_limit")
        # Byte-fed stdin avoids arbitrary input protocols/paths. RIFF additionally
        # requires exact advertised size; FFmpeg tolerates truncated WAV otherwise.
        if payload.startswith(b"RIFF"):
            if len(payload) < 12 or int.from_bytes(payload[4:8], "little") + 8 != len(payload):
                raise VoiceError("truncated_audio")
            try:
                with wave.open(io.BytesIO(payload), "rb") as wav:
                    if len(wav.readframes(wav.getnframes())) != wav.getnframes() * wav.getnchannels() * wav.getsampwidth():
                        raise VoiceError("truncated_audio")
            except (wave.Error, EOFError):
                raise VoiceError("invalid_audio") from None
        result = self.runner([self.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin",
                              "-xerror", "-err_detect", "explode", "-protocol_whitelist", "pipe",
                              "-i", "pipe:0", "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                              "-t", "30.0000625", "-f", "s16le", "pipe:1"],
                             input_bytes=payload, stdout_limit=MAX_SAMPLES * 2 + 2,
                             stderr_limit=65536, timeout=30, cancel=cancel)
        if result.stderr_bytes:
            raise VoiceError("decode_error")
        return screen_pcm(result.stdout)
