"""Synthetic PCM/adapter contract tests: these never open real audio devices."""
from __future__ import annotations

import json
import struct
import threading
import tracemalloc
import wave
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.infrastructure.audio import AudioCapture, QUEUE_BLOCKS, _ChannelWriter, _Session, _atomic_json


class FakeStream:
    def __init__(self, callback):
        self.callback, self.active, self.closed = callback, False, False

    def start_stream(self):
        self.active = True

    def stop_stream(self):
        self.active = False

    def is_active(self):
        return self.active

    def close(self):
        self.closed = True
        self.active = False


class FakeManager:
    def __init__(self):
        self.streams, self.terminated = [], False

    def get_host_api_info_by_type(self, api):
        return {"index": 2, "defaultInputDevice": 3, "defaultOutputDevice": 4}

    def get_default_wasapi_loopback(self):
        return {"index": 5}

    def get_device_info_generator(self):
        return iter([
            {"index": 3, "hostApi": 2, "name": "Synthetic microphone", "maxInputChannels": 1,
             "defaultSampleRate": 8000, "isLoopbackDevice": False},
            {"index": 4, "hostApi": 2, "name": "Synthetic speaker", "maxInputChannels": 0,
             "defaultSampleRate": 8000, "isLoopbackDevice": False},
            {"index": 5, "hostApi": 2, "name": "Synthetic speaker [Loopback]", "maxInputChannels": 2,
             "defaultSampleRate": 8000, "isLoopbackDevice": True},
            {"index": 6, "hostApi": 0, "name": "Other API", "maxInputChannels": 1,
             "defaultSampleRate": 8000, "isLoopbackDevice": False},
        ])

    def open(self, **kwargs):
        assert kwargs["start"] is False
        assert kwargs["input"] is True
        stream = FakeStream(kwargs["stream_callback"])
        self.streams.append(stream)
        return stream

    def terminate(self):
        self.terminated = True


class FakeAudio:
    paWASAPI, paInt16, paContinue, paComplete, paAbort = 13, 8, 0, 1, 2

    def __init__(self):
        self.managers = []

    def PyAudio(self):
        manager = FakeManager()
        self.managers.append(manager)
        return manager


def settings(tmp_path, chunk_seconds=1):
    return SimpleNamespace(data_dir=tmp_path, chunk_seconds=chunk_seconds)


def test_listing_does_not_open_streams(tmp_path):
    module = FakeAudio()
    capture = AudioCapture(settings(tmp_path), audio_module=module)
    result = capture.list_devices()
    assert result["available"]
    assert [(d["kind"], d["default"]) for d in result["devices"]] == [("microphone", True), ("system", True)]
    assert module.managers[0].terminated and not module.managers[0].streams
    assert not list(tmp_path.iterdir())


def test_unavailable_audio_is_honest(tmp_path):
    module = FakeAudio()
    module.PyAudio = lambda: (_ for _ in ()).throw(OSError("No WASAPI"))
    result = AudioCapture(settings(tmp_path), audio_module=module).list_devices()
    assert result["available"] is False
    assert result["devices"] == [] and "No WASAPI" in result["error"]


def test_start_requires_explicit_valid_device_and_safe_uuid(tmp_path):
    module = FakeAudio()
    capture = AudioCapture(settings(tmp_path), audio_module=module)
    with pytest.raises(ValueError, match="Выберите"):
        capture.start(str(uuid4()))
    with pytest.raises(ValueError):
        capture.start("../../outside", "wasapi:3")
    with pytest.raises(ValueError, match="недоступно"):
        capture.start(str(uuid4()), system_id="wasapi:3")
    assert not list(tmp_path.iterdir())
    assert module.managers[-1].terminated


def test_two_channel_capture_drains_to_separate_atomic_wav_and_joins(tmp_path):
    module = FakeAudio()
    capture = AudioCapture(settings(tmp_path), audio_module=module)
    meeting_id, chunks, errors, finishes = str(uuid4()), [], [], []
    result = capture.start(meeting_id, "wasapi:3", "wasapi:5", chunks.append, errors.append, finishes.append)
    assert result["recording"] and result["completed_chunks"] == 0
    manager = module.managers[-1]
    for _ in range(10):
        manager.streams[0].callback(struct.pack("<h", 123) * 1000, 1000, {}, 0)
        manager.streams[1].callback(struct.pack("<hh", 321, -321) * 1000, 1000, {}, 0)
    stopped = capture.stop(meeting_id)
    assert stopped["status"] == "stopped" and not stopped["recording"]
    assert stopped["completed_chunks"] == 4 and not errors
    assert len(finishes) == 1 and finishes[0]["status"] == "stopped"
    assert finishes[0]["completed_chunks"] == 4 and not finishes[0]["recording"]
    assert manager.terminated and all(s.closed for s in manager.streams)
    assert not capture._session.thread.is_alive()
    assert {c["channel"] for c in chunks} == {"microphone", "system"}
    for channel in ("microphone", "system"):
        own = sorted([c for c in chunks if c["channel"] == channel], key=lambda c: c["sequence"])
        assert [c["frames"] for c in own] == [8000, 2000]
        assert own[1]["offset_ms"] - own[0]["offset_ms"] == 1000
        for chunk in own:
            with wave.open(chunk["path"], "rb") as audio:
                assert audio.getnframes() == chunk["frames"]
                assert audio.getnchannels() == (1 if channel == "microphone" else 2)
    folder = tmp_path / "audio" / meeting_id
    assert not list(folder.glob("*.part"))
    assert json.loads((folder / "capture.json").read_text())["status"] == "stopped"
    # Callback replay has stable (channel,sequence) pairs; DB inserts use unique IDs.
    recovered = capture.recover(meeting_id)
    assert len(recovered["chunks"]) == 4
    with pytest.raises(ValueError, match="уже есть запись"):
        capture.start(meeting_id, "wasapi:3")
    capture.close()


def test_callbacks_do_no_disk_or_business_work_and_overload_is_failure(tmp_path):
    module, manager = FakeAudio(), FakeManager()
    device = next(manager.get_device_info_generator())
    business_calls = []
    session = _Session(tmp_path, str(uuid4()), [("microphone", device)], module, manager, 1,
                       lambda chunk: business_calls.append(chunk), lambda error: business_calls.append(error))
    callback = session.callback("microphone")
    for _ in range(QUEUE_BLOCKS):
        assert callback(b"\0\0" * 16, 16, {}, 0)[1] == module.paContinue
    assert callback(b"\0\0" * 16, 16, {}, 0)[1] == module.paAbort
    assert session.stop_event.is_set() and "очередь заполнена" in session.error
    assert not business_calls and not list(tmp_path.glob("*.wav*"))


def test_overflow_flag_is_explicit_and_preserves_previous_blocks(tmp_path):
    module, manager = FakeAudio(), FakeManager()
    session = _Session(tmp_path, str(uuid4()), [("microphone", next(manager.get_device_info_generator()))],
                       module, manager, 1, lambda chunk: None, lambda error: None)
    callback = session.callback("microphone")
    callback(b"\0\0" * 16, 16, {}, 0)
    assert callback(b"\0\0" * 16, 16, {}, 2)[1] == module.paAbort
    assert session.queue.qsize() == 1 and "PortAudio status 2" in session.error


def partial_manifest(tmp_path, meeting_id):
    folder = tmp_path / "audio" / meeting_id
    folder.mkdir(parents=True)
    part = folder / "microphone_000000.wav.part"
    descriptor = {"path": str(part), "sequence": 0, "start_frame": 8000,
                  "channels": 1, "sample_rate": 8000, "initial_offset_ms": 12}
    manifest = {"schema_version": 1, "meeting_id": meeting_id, "status": "recording",
                "chunks": [], "in_progress": {"microphone": descriptor}, "recovery_notes": []}
    _atomic_json(folder / "capture.json", manifest)
    return folder, part, descriptor


def test_recover_partial_uses_file_frames_preserves_original_and_is_idempotent(tmp_path):
    meeting_id = str(uuid4())
    folder, part, _ = partial_manifest(tmp_path, meeting_id)
    # Simulate a stale header + full PCM frames + incomplete trailing byte after crash.
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36, b"WAVE", b"fmt ", 16,
                         1, 1, 8000, 16000, 2, 16, b"data", 0)
    original = header + b"\x12\x00" * 800 + b"\xFF"
    part.write_bytes(original)
    capture = AudioCapture(settings(tmp_path), audio_module=FakeAudio())
    first = capture.recover(meeting_id)
    assert first["status"] == "recovered" and len(first["chunks"]) == 1
    chunk = first["chunks"][0]
    assert chunk["offset_ms"] == 1012 and chunk["duration_ms"] == 100
    assert chunk["frames"] == 800 and chunk["recovered"]
    with wave.open(chunk["path"], "rb") as audio:
        assert audio.getnframes() == 800 and audio.readframes(800) == original[44:-1]
    assert part.read_bytes() == original
    second = capture.recover(meeting_id)
    assert second == first
    assert not list(folder.glob("*.tmp"))


def test_recover_rename_before_manifest_and_hash_mismatch(tmp_path):
    meeting_id = str(uuid4())
    _, part, _ = partial_manifest(tmp_path, meeting_id)
    final = part.with_suffix("")
    with wave.open(str(final), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 40)
    capture = AudioCapture(settings(tmp_path), audio_module=FakeAudio())
    assert capture.recover(meeting_id)["chunks"][0]["frames"] == 40
    with final.open("ab") as handle:
        handle.write(b"corrupted")
    with pytest.raises(ValueError, match="hash"):
        capture.recover(meeting_id)


def test_recover_unknown_partial_is_preserved_without_fake_chunk(tmp_path):
    meeting_id = str(uuid4())
    _, part, _ = partial_manifest(tmp_path, meeting_id)
    part.write_bytes(b"incomplete header")
    capture = AudioCapture(settings(tmp_path), audio_module=FakeAudio())
    result = capture.recover(meeting_id)
    assert result["chunks"] == [] and part.exists() and result["notes"]


def test_chunk_receipt_survives_application_callback_failure(tmp_path):
    manifest = {"chunks": [], "in_progress": {}}
    writer = _ChannelWriter(tmp_path, "microphone", {"sample_rate": 8000, "channels": 1,
                            "initial_offset_ms": 0}, 1, manifest,
                            lambda: _atomic_json(tmp_path / "manifest.json", manifest),
                            lambda chunk: (_ for _ in ()).throw(RuntimeError("DB unavailable")))
    writer.write(b"\x00\x00" * 16000)
    saved = json.loads((tmp_path / "manifest.json").read_text())
    assert len(saved["chunks"]) == 2 and Path(saved["chunks"][0]["path"]).exists()
    assert saved["in_progress"] == {}
    assert "DB unavailable" in saved["delivery_error"]
    writer.close()


def test_three_hour_limit_stops_automatically_without_cloud(monkeypatch, tmp_path):
    import secretary.infrastructure.audio as audio_module
    monkeypatch.setattr(audio_module, "MAX_RECORDING_SECONDS", 1)
    module = FakeAudio()
    capture = AudioCapture(settings(tmp_path), audio_module=module)
    meeting_id = str(uuid4())
    capture.start(meeting_id, "wasapi:3")
    capture._session.started -= 2
    capture._session.thread.join(timeout=2)
    assert not capture._session.thread.is_alive()
    assert not capture.state(meeting_id)["recording"]
    assert module.managers[-1].terminated
    capture.close()


def test_progressive_writer_does_not_buffer_the_whole_long_recording(tmp_path):
    manifest = {"chunks": [], "in_progress": {}}
    writer = _ChannelWriter(tmp_path, "microphone", {"sample_rate": 8000, "channels": 1,
                            "initial_offset_ms": 0}, 20, manifest,
                            lambda: _atomic_json(tmp_path / "manifest.json", manifest), lambda chunk: None)
    one_second = b"\x00\x00" * 8000
    tracemalloc.start()
    try:
        for _ in range(600):
            writer.write(one_second)
        writer.close()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(manifest["chunks"]) == 30
    assert sum(chunk["frames"] for chunk in manifest["chunks"]) == 600 * 8000
    assert peak < 3 * 1024 * 1024  # 9.6 MB audio on disk; memory bounded by hash block/metadata.


def test_second_device_failure_preserves_first_device_pcm_and_closes_handles(tmp_path):
    module = FakeAudio()
    original_factory = module.PyAudio

    def manager_factory():
        manager = original_factory()
        original_open = manager.open

        def open_device(**kwargs):
            stream = original_open(**kwargs)
            if len(manager.streams) == 2:
                first = manager.streams[0]
                first_start = first.start_stream

                def first_start_and_data():
                    first_start()
                    first.callback(b"\x00\x00" * 80, 80, {}, 0)
                first.start_stream = first_start_and_data
                stream.start_stream = lambda: (_ for _ in ()).throw(OSError("Synthetic system unavailable"))
            return stream
        manager.open = open_device
        return manager

    module.PyAudio = manager_factory
    capture = AudioCapture(settings(tmp_path), audio_module=module)
    meeting_id, chunks = str(uuid4()), []
    with pytest.raises(OSError, match="system unavailable"):
        capture.start(meeting_id, "wasapi:3", "wasapi:5", chunks.append)
    assert capture.state(meeting_id)["status"] == "failed"
    assert chunks[0]["frames"] == 80 and Path(chunks[0]["path"]).exists()
    assert module.managers[-1].terminated and all(s.closed for s in module.managers[-1].streams)


def test_one_inactive_channel_is_detected_even_with_other_channel_backlog(tmp_path):
    module, manager = FakeAudio(), FakeManager()
    device_list = list(manager.get_device_info_generator())
    errors = []
    session = _Session(tmp_path, str(uuid4()), [("microphone", device_list[0]), ("system", device_list[2])],
                       module, manager, 1, lambda chunk: None, errors.append)
    microphone, system = FakeStream(None), FakeStream(None)
    microphone.active, system.active = True, False
    session.streams = [microphone, system]
    session.status = "recording"
    # The healthy channel keeps the queue non-empty throughout this check.
    for _ in range(20):
        session.queue.put(("microphone", b"\x00\x00" * 512))
    session.run()
    assert session.status == "failed" and "system" in errors[0]
    assert session.manifest["chunks"] and manager.terminated


def test_stop_during_completion_callback_preserves_terminal_state(tmp_path):
    module = FakeAudio()
    capture = AudioCapture(settings(tmp_path), audio_module=module)
    entered, release = threading.Event(), threading.Event()
    completions, results = [], []

    def finish(snapshot):
        completions.append(snapshot)
        entered.set()
        assert release.wait(timeout=3)

    meeting_id = str(uuid4())
    capture.start(meeting_id, "wasapi:3", on_finish=finish)
    capture._session.stop_event.set()
    assert entered.wait(timeout=2)
    joining = threading.Event()
    original_join = capture._session.thread.join

    def monitored_join(*args, **kwargs):
        joining.set()
        return original_join(*args, **kwargs)

    capture._session.thread.join = monitored_join
    stopping_thread = threading.Thread(target=lambda: results.append(capture.stop(meeting_id)))
    stopping_thread.start()
    assert joining.wait(timeout=2)
    release.set()
    stopping_thread.join(timeout=2)
    assert not stopping_thread.is_alive()
    assert results[0]["status"] == "stopped" and not results[0]["recording"]
    assert completions[0]["status"] == "stopped" and not completions[0]["recording"]
