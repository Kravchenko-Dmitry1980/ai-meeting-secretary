"""Reject unsupported probe durations before starting a long media decoder."""
import json
from types import SimpleNamespace

import pytest

from secretary.application import preparation


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["NaN", "Infinity", "-Infinity", "0", "-1", "14401.1"])
async def test_invalid_import_duration_never_dispatches_ffmpeg(tmp_path, monkeypatch, duration):
    calls = []

    class Probe:
        returncode = 0

        async def communicate(self):
            return json.dumps({"format": {"duration": duration}}).encode(), b""

    async def subprocess(*args, **kwargs):
        calls.append(args[0])
        assert len(calls) == 1, "Invalid duration dispatched a decoder"
        return Probe()

    monkeypatch.setattr(preparation.asyncio, "create_subprocess_exec", subprocess)
    settings = SimpleNamespace(ffprobe_path="probe", ffmpeg_path="decoder", chunk_seconds=120)
    with pytest.raises(ValueError, match="duration"):
        await preparation.prepare_audio(settings, tmp_path / "original.m4a", tmp_path / "chunks")
    assert calls == ["probe"]
