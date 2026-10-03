"""Bounded reader for immutable server-owned PCM16 meeting chunks.

No arbitrary URL or user-original recording is accepted here. Caller owns
the snapshot and revalidates these dependencies transactionally at publication.
"""
from __future__ import annotations

import array
import hashlib
import math
import os
import stat
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from secretary.domain.voice import SAMPLE_RATE, VoiceError
from secretary.infrastructure.voice_process import run_owned

MAX_CHUNK_BYTES = 512 * 1024**2
READ_BLOCK_BYTES = 65536
MAX_EXCERPT_SAMPLES = 10 * SAMPLE_RATE
MAX_NATIVE_RATE = 192000
MAX_NATIVE_CHANNELS = 8
MAX_NATIVE_EXCERPT_BYTES = 10 * MAX_NATIVE_RATE * MAX_NATIVE_CHANNELS * 2


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise VoiceError("cancelled")


@dataclass(frozen=True, repr=False)
class ChunkAudioSnapshot:
    chunk_id: str
    meeting_id: str
    path: Path
    sha256: str
    offset_ms: int
    duration_ms: int
    channel: str


@dataclass(frozen=True)
class AudioReadCounters:
    chunks_hashed: int
    bytes_hashed: int
    excerpts_read: int
    samples_read: int


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_nlink)


class MeetingAudioReader:
    """Per-run cache holds only stat/digest metadata, never audio.

    Hashes each chunk once in 64KiB blocks. Every subsequent open/read checks
    its exact stat identity and path; an altered chunk is rejected. Use a new
    reader for each run. Files with symlink/reparse ancestors or extra links
    are rejected. This cannot replace caller-owned dependency/CAS validation.
    """
    def __init__(self, audio_root: Path, *, ffmpeg_path: str = "ffmpeg", runner=run_owned):
        self._root = Path(audio_root).absolute()
        self._verified = {}
        self._counts = [0, 0, 0, 0]
        self._ffmpeg_path, self._runner = ffmpeg_path, runner

    @property
    def counters(self):
        return AudioReadCounters(*self._counts)

    def revalidate(self, *, cancel=None) -> None:
        """Recheck every verified original chunk before caller publication.

        Reopens bounded file descriptors against this reader's exact path and
        descriptor baselines; no hash, audio read, conversion or new persistent
        record. This is a filesystem spot check, not publication/CAS authority.
        Caller still owns its coordinator, dependencies and transaction gates.
        """
        check_cancel(cancel)
        for snapshot, initial, opened in self._verified.values():
            check_cancel(cancel)
            path, current = self._safe_path(snapshot)
            if _identity(current) != _identity(initial):
                raise VoiceError("audio_changed")
            check_cancel(cancel)
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as handle:
                    check_cancel(cancel)
                    self._stable(handle, snapshot, initial, opened)
                    check_cancel(cancel)
            except OSError:
                raise VoiceError("audio_read_failed") from None
        check_cancel(cancel)

    def _safe_path(self, snapshot):
        try:
            path = Path(snapshot.path)
        except (TypeError, ValueError):
            raise VoiceError("unsafe_audio_path") from None
        if not path.is_absolute() or ".." in path.parts:
            raise VoiceError("unsafe_audio_path")
        try:
            path.relative_to(self._root)
            # Include ancestors of root too: an apparently contained path must
            # not traverse a junction or symlink on its way to the audio root.
            for part in (path, *path.parents):
                info = part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise VoiceError("unsafe_audio_path")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise VoiceError("unsafe_audio_path")
            if not 44 <= info.st_size <= MAX_CHUNK_BYTES:
                raise VoiceError("invalid_audio_size")
            return path, info
        except FileNotFoundError:
            raise VoiceError("audio_missing") from None
        except (OSError, ValueError, TypeError):
            raise VoiceError("unsafe_audio_path") from None

    def _stable(self, handle, snapshot, initial, opened):
        _, current = self._safe_path(snapshot)
        # Windows path stat ctime is creation time, while Python 3.12 fstat
        # ctime is change time. Compare like sources, and cross-check the
        # stable dev/inode/size/mtime/link fields independently of ctime.
        descriptor = os.fstat(handle.fileno())
        cross = lambda s: (_identity(s)[:4], s.st_nlink)
        if (_identity(descriptor) != _identity(opened) or _identity(current) != _identity(initial)
                or cross(descriptor) != cross(initial)):
            raise VoiceError("audio_changed")

    @staticmethod
    def _layout(handle, size, cancel):
        check_cancel(cancel)
        header = handle.read(12)
        check_cancel(cancel)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise VoiceError("invalid_pcm_audio")
        if struct.unpack_from("<I", header, 4)[0] + 8 != size:
            raise VoiceError("truncated_audio")
        position, fmt, data = 12, None, None
        for _ in range(128):
            if position == size:
                break
            if position + 8 > size:
                raise VoiceError("truncated_audio")
            handle.seek(position)
            check_cancel(cancel)
            block = handle.read(8)
            check_cancel(cancel)
            if len(block) != 8:
                raise VoiceError("truncated_audio")
            marker, length = struct.unpack("<4sI", block)
            end = position + 8 + length
            if end + (length % 2) > size:
                raise VoiceError("truncated_audio")
            if marker == b"fmt ":
                if fmt or not 16 <= length <= 40:
                    raise VoiceError("invalid_pcm_audio")
                check_cancel(cancel)
                raw = handle.read(length)
                check_cancel(cancel)
                if len(raw) != length:
                    raise VoiceError("truncated_audio")
                code, channels, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", raw[:16])
                if (code != 1 or bits != 16 or not 1 <= channels <= MAX_NATIVE_CHANNELS
                        or not 8000 <= rate <= MAX_NATIVE_RATE or align != channels*2
                        or byte_rate != rate*align):
                    raise VoiceError("invalid_pcm_audio")
                fmt = (channels, rate, align)
            if marker == b"data":
                if data is not None or length % 2:
                    raise VoiceError("invalid_pcm_audio")
                data = (position + 8, length)
            position = end + length % 2
        if position != size or not fmt or data is None:
            raise VoiceError("invalid_pcm_audio")
        channels, rate, align = fmt
        if data[1] % align:
            raise VoiceError("invalid_pcm_audio")
        return data[0], data[1] // align, channels, rate

    def _normalize(self, pcm, channels, rate, target_samples, cancel):
        if channels == 1 and rate == SAMPLE_RATE:
            return pcm
        if len(pcm) > MAX_NATIVE_EXCERPT_BYTES:
            raise VoiceError("native_audio_interval_limit")
        # Strict PCM header is rebuilt only for the already bounded interval.
        # No original source path or metadata goes to the subprocess.
        header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", len(pcm)+36, b"WAVE", b"fmt ",
                             16, 1, channels, rate, rate*channels*2, channels*2, 16, b"data", len(pcm))
        check_cancel(cancel)
        try:
            result = self._runner([self._ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin",
                                   "-xerror", "-err_detect", "explode", "-protocol_whitelist", "pipe",
                                   "-i", "pipe:0", "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                                   "-t", format(target_samples / SAMPLE_RATE, ".8f"), "-f", "s16le", "pipe:1"],
                                  input_bytes=header+pcm, stdout_limit=target_samples*2,
                                  stderr_limit=65536, timeout=30, rss_limit=2*1024**3, cancel=cancel)
        except VoiceError as error:
            if error.reason == "process_unavailable":
                raise VoiceError("audio_conversion_unavailable") from None
            if error.reason == "process_failed":
                raise VoiceError("audio_conversion_failed") from None
            raise
        check_cancel(cancel)
        if result.stderr_bytes:
            raise VoiceError("audio_conversion_failed")
        if not isinstance(result.stdout, bytes) or len(result.stdout) != target_samples*2:
            raise VoiceError("audio_conversion_length_mismatch")
        return result.stdout

    def read_excerpt(self, snapshot: ChunkAudioSnapshot, start_sample: int, end_sample: int, *, cancel=None) -> bytes:
        """Chunk-relative exclusive *16k timeline* offsets, 1..10 seconds.

        Native PCM16 device chunks use bounded original-frame reads followed by
        owned in-memory resampling. Normalized mono16k has no subprocess work.
        """
        check_cancel(cancel)
        if (type(start_sample) is not int or type(end_sample) is not int or start_sample < 0
                or not SAMPLE_RATE <= end_sample - start_sample <= MAX_EXCERPT_SAMPLES):
            raise VoiceError("invalid_audio_interval")
        if (not isinstance(snapshot.sha256, str) or len(snapshot.sha256) != 64
                or any(x not in "0123456789abcdef" for x in snapshot.sha256)):
            raise VoiceError("invalid_audio_hash")
        path, initial = self._safe_path(snapshot)
        key = (snapshot.chunk_id, str(path), snapshot.sha256)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                self._stable(handle, snapshot, initial, opened)
                previous = self._verified.get(key)
                identity = (_identity(initial), _identity(opened))
                if previous is not None and (_identity(previous[1]), _identity(previous[2])) != identity:
                    raise VoiceError("audio_changed")
                if previous is None:
                    digest = hashlib.sha256()
                    while True:
                        check_cancel(cancel)
                        block = handle.read(READ_BLOCK_BYTES)
                        check_cancel(cancel)
                        if not block:
                            break
                        digest.update(block)
                        self._counts[1] += len(block)
                    self._stable(handle, snapshot, initial, opened)
                    if digest.hexdigest() != snapshot.sha256:
                        raise VoiceError("audio_hash_mismatch")
                    self._verified[key] = (snapshot, initial, opened)
                    self._counts[0] += 1
                handle.seek(0)
                data_offset, frames, channels, rate = self._layout(handle, initial.st_size, cancel)
                if (type(snapshot.duration_ms) is not int or snapshot.duration_ms <= 0
                        or abs(frames*1000 - snapshot.duration_ms*rate) > rate):
                    raise VoiceError("audio_duration_mismatch")
                if end_sample > snapshot.duration_ms*16:
                    raise VoiceError("invalid_audio_interval")
                begin = start_sample*rate // SAMPLE_RATE
                end = end_sample*rate // SAMPLE_RATE
                if begin >= frames:
                    raise VoiceError("invalid_audio_interval")
                target_samples = end_sample-start_sample
                # Current prepare_audio rounds duration to milliseconds. Clamp
                # only its <=1ms EOF overshoot; keep the source timestamps and
                # never append missing PCM. Integer math preserves rate bounds.
                overshoot = end_sample*rate - frames*SAMPLE_RATE
                if overshoot > 0:
                    if overshoot*1000 > rate*SAMPLE_RATE:
                        raise VoiceError("invalid_audio_interval")
                    end = frames
                    target_samples = min(target_samples, (end-begin)*SAMPLE_RATE // rate)
                if end-begin < rate or target_samples < SAMPLE_RATE:
                    raise VoiceError("short_clip")
                native_bytes = (end-begin)*channels*2
                if native_bytes > MAX_NATIVE_EXCERPT_BYTES:
                    raise VoiceError("native_audio_interval_limit")
                handle.seek(data_offset + begin*channels*2)
                check_cancel(cancel)
                # Blocks make cancellation responsive even at192kHz/8channels.
                payload = bytearray()
                while len(payload) < native_bytes:
                    check_cancel(cancel)
                    block = handle.read(min(READ_BLOCK_BYTES, native_bytes-len(payload)))
                    check_cancel(cancel)
                    if not block:
                        raise VoiceError("truncated_audio")
                    payload.extend(block)
                pcm = bytes(payload)
                del payload
                check_cancel(cancel)
                self._stable(handle, snapshot, initial, opened)
                pcm = self._normalize(pcm, channels, rate, target_samples, cancel)
                check_cancel(cancel)
                self._stable(handle, snapshot, initial, opened)
                self._counts[2] += 1
                self._counts[3] += len(pcm)//2
                return pcm
        except OSError:
            raise VoiceError("audio_read_failed") from None


def technical_quality(pcm: bytes) -> bool:
    """Energy/clipping screening only; not speech or single-speaker proof."""
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples or sum(abs(x) >= 32734 for x in samples) / len(samples) > .01:
        return False
    return math.sqrt(sum(x*x for x in samples) / len(samples)) >= 327.68
