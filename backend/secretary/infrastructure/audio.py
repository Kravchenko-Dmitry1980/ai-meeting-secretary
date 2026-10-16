"""Explicit Windows capture; native callbacks only enqueue bounded PCM blocks.

The disk writer and recovery code are independent of cloud/network adapters.
No Meetily source was copied: checkpoint/provider-boundary ideas informed this design.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import queue
import struct
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import UUID

from secretary.infrastructure.storage_space import (
    ensure_free_space,
    estimate_capture_workspace_bytes,
    remaining_capture_workspace_bytes,
    disk_full_storage_error,
)

MAX_RECORDING_SECONDS = 3 * 60 * 60
FRAMES_PER_BUFFER = 1024
QUEUE_BLOCKS = 256
MIN_SPACE_CHECK_INTERVAL_SECONDS = 1.0


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _meeting_folder(data_dir: Path, meeting_id: str) -> Path:
    # Public API uses UUIDs; reject paths before touching the filesystem.
    canonical_id = str(UUID(meeting_id))
    if canonical_id != meeting_id:
        raise ValueError("Идентификатор встречи должен быть UUID")
    return Path(data_dir).resolve() / "audio" / canonical_id


def _contained_file(folder: Path, value: str) -> Path:
    resolved = Path(value).resolve()
    if resolved.parent != folder.resolve():
        raise ValueError("Аудиофайл находится вне папки встречи")
    return resolved


class _ChannelWriter:
    """Write exact frame-sized chunks, keeping at most one callback block in RAM."""

    def __init__(self, folder: Path, channel: str, config: dict, chunk_seconds: int,
                 manifest: dict, save_manifest: Callable[[], None],
                 on_chunk: Callable[[dict], None]):
        self.folder, self.channel, self.config = folder, channel, config
        self.chunk_frames = int(config["sample_rate"] * chunk_seconds)
        self.frame_size = int(config["channels"]) * 2
        self.manifest, self.save_manifest, self.on_chunk = manifest, save_manifest, on_chunk
        self.total_frames = self.frames = self.sequence = 0
        self.handle = self.wav = self.part = None
        self.last_sync = time.monotonic()

    def _open(self) -> None:
        self.part = self.folder / f"{self.channel}_{self.sequence:06d}.wav.part"
        self.manifest["in_progress"][self.channel] = {
            "path": str(self.part), "sequence": self.sequence,
            "start_frame": self.total_frames, **self.config,
        }
        self.save_manifest()
        self.handle = self.part.open("xb")
        self.wav = wave.open(self.handle, "wb")
        self.wav.setnchannels(self.config["channels"])
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.config["sample_rate"])
        self.frames = 0

    def write(self, pcm: bytes) -> None:
        if len(pcm) % self.frame_size:
            raise ValueError("Неполный PCM frame: запись остановлена, аудио сохранено")
        remaining = memoryview(pcm)
        while remaining:
            if self.wav is None:
                self._open()
            take_frames = min(len(remaining) // self.frame_size, self.chunk_frames - self.frames)
            take_bytes = take_frames * self.frame_size
            # writeframes updates the RIFF header; no whole-recording audio buffer.
            self.wav.writeframes(remaining[:take_bytes])
            self.frames += take_frames
            remaining = remaining[take_bytes:]
            if time.monotonic() - self.last_sync >= 1:
                self.handle.flush()
                os.fsync(self.handle.fileno())
                self.last_sync = time.monotonic()
            if self.frames == self.chunk_frames:
                self._finish()

    def _finish(self) -> None:
        if self.wav is None:
            return
        part, handle, wav = self.part, self.handle, self.wav
        self.wav = self.handle = None
        try:
            wav.close()
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        final = part.with_suffix("")
        os.replace(part, final)
        rate = self.config["sample_rate"]
        chunk = {
            "path": str(final), "sequence": self.sequence, "channel": self.channel,
            "offset_ms": self.config["initial_offset_ms"] + self.total_frames * 1000 // rate,
            "duration_ms": self.frames * 1000 // rate, "sha256": _hash(final),
            "start_frame": self.total_frames, "frames": self.frames, "sample_rate": rate,
            "channels": self.config["channels"],
        }
        self.total_frames += self.frames
        self.sequence += 1
        self.frames = 0
        self.manifest["chunks"].append(chunk)
        self.manifest["in_progress"].pop(self.channel, None)
        # Persist receipt before DB callback: replay can repair an interrupted callback.
        self.save_manifest()
        try:
            self.on_chunk(dict(chunk))
        except Exception as exc:
            # Persistence is authoritative; an unavailable application/DB cannot
            # destroy subsequent audio. Replay the receipt at stop/restart.
            self.manifest["delivery_error"] = f"Аудио сохранено; требуется повторная регистрация чанков: {exc}"
            self.save_manifest()

    def close(self) -> None:
        self._finish()


def _repair_partial(folder: Path, channel: str, descriptor: dict) -> dict | None:
    """Salvage complete PCM16 frames using the actual file size, never header duration."""
    part = _contained_file(folder, descriptor["path"])
    if not part.exists():
        # Crash between WAV rename and manifest update: closed file is authoritative.
        final = part.with_suffix("")
        if not final.exists():
            return None
        with wave.open(str(final), "rb") as source:
            if (source.getsampwidth() != 2 or source.getnchannels() != descriptor["channels"]
                    or source.getframerate() != descriptor["sample_rate"]):
                raise ValueError("Формат завершённого WAV не совпадает с manifest")
            frames = source.getnframes()
    else:
        with part.open("rb") as source:
            header = source.read(44)
            if len(header) != 44:
                return None  # Untouched .part remains available for manual inspection.
            riff, _, wav_marker, fmt_marker, fmt_size, fmt_code, channels, rate, _, align, bits, data_marker, _ = struct.unpack("<4sI4s4sIHHIIHH4sI", header)
            if (riff != b"RIFF" or wav_marker != b"WAVE" or fmt_marker != b"fmt "
                    or data_marker != b"data" or fmt_size != 16 or fmt_code != 1
                    or bits != 16 or channels != descriptor["channels"]
                    or rate != descriptor["sample_rate"] or align != channels * 2):
                raise ValueError("Незавершённый WAV имеет неподтверждённый PCM формат")
            frames = (part.stat().st_size - 44) // align
            if frames <= 0:
                return None
            final = part.with_suffix("")
            temporary = folder / (final.name + ".recovery.tmp")
            with wave.open(str(temporary), "wb") as output:
                output.setnchannels(channels)
                output.setsampwidth(2)
                output.setframerate(rate)
                remaining = frames * align
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("Незавершённый WAV изменился при восстановлении")
                    output.writeframesraw(block)
                    remaining -= len(block)
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, final)
            # Preserve the original .part even after successful repair.
    rate = descriptor["sample_rate"]
    return {
        "path": str(final), "sequence": descriptor["sequence"], "channel": channel,
        "offset_ms": descriptor["initial_offset_ms"] + descriptor["start_frame"] * 1000 // rate,
        "duration_ms": frames * 1000 // rate, "sha256": _hash(final),
        "start_frame": descriptor["start_frame"], "frames": frames,
        "sample_rate": rate, "channels": descriptor["channels"], "recovered": True,
    }


class _Session:
    def __init__(self, folder: Path, meeting_id: str, selected: list[tuple[str, dict]],
                 module, manager, chunk_seconds: int, on_chunk, on_error, on_finish=None,
                 max_seconds=None, max_channels=8, storage_check=None):
        self.folder, self.meeting_id = folder, meeting_id
        self.module, self.manager = module, manager
        self.on_chunk, self.on_error = on_chunk, on_error
        self.on_finish = on_finish
        self.storage_check = storage_check
        self.max_seconds, self.max_channels = max_seconds if max_seconds is not None else MAX_RECORDING_SECONDS, max_channels
        self.native_closed = False
        self.lock = threading.RLock()
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_BLOCKS)
        self.stop_event = threading.Event()
        self.error: str | None = None
        self.status = "starting"
        self.started = time.monotonic()
        self.last_space_check = self.started
        self.finished_duration_ms: int | None = None
        self.streams: list = []
        self.writers: dict[str, _ChannelWriter] = {}
        self.thread: threading.Thread | None = None
        self.manifest = {"schema_version": 1, "meeting_id": meeting_id, "status": "recording",
                         "started_at": _utc(), "completed_at": None, "error": None,
                         "channels": {}, "chunks": [], "in_progress": {}, "recovery_notes": []}
        self.folder.mkdir(parents=True, exist_ok=True)
        for channel, device in selected:
            config = {"device_id": f"wasapi:{device['index']}", "name": device["name"],
                      "sample_rate": int(device["defaultSampleRate"]),
                      "channels": int(device["maxInputChannels"]), "sample_width": 2,
                      "initial_offset_ms": 0}
            if not (1 <= config["channels"] <= self.max_channels and 8000 <= config["sample_rate"] <= 192000):
                raise ValueError("Аудиоустройство имеет неподдерживаемый формат")
            self.manifest["channels"][channel] = config
            self.writers[channel] = _ChannelWriter(folder, channel, config, chunk_seconds,
                                                   self.manifest, self.save_manifest, on_chunk)
        self.save_manifest()

    def save_manifest(self) -> None:
        with self.lock:
            _atomic_json(self.folder / "capture.json", self.manifest)

    def fail(self, message: str) -> None:
        with self.lock:
            if self.error is None:
                self.error = message
        self.stop_event.set()

    def callback(self, channel: str):
        def receive(in_data, frame_count, time_info, status_flags):
            if self.stop_event.is_set():
                return None, self.module.paComplete
            if status_flags:
                self.fail(f"Сбой аудиопотока {channel} (PortAudio status {status_flags}); запись остановлена")
                return None, self.module.paAbort
            config = self.manifest["channels"][channel]
            if (frame_count <= 0 or frame_count > FRAMES_PER_BUFFER * 4
                    or not in_data or len(in_data) != frame_count * config["channels"] * 2):
                self.fail(f"Неполный аудиоблок {channel}; запись остановлена")
                return None, self.module.paAbort
            try:
                self.queue.put_nowait((channel, bytes(in_data)))
            except queue.Full:
                self.fail("Диск не успевает сохранять аудио: очередь заполнена; запись остановлена, сохранённые части доступны")
                return None, self.module.paAbort
            return None, self.module.paContinue
        return receive

    def start(self) -> None:
        try:
            for channel, config in self.manifest["channels"].items():
                self.streams.append(self.manager.open(
                    format=self.module.paInt16, channels=config["channels"],
                    rate=config["sample_rate"], frames_per_buffer=FRAMES_PER_BUFFER,
                    input=True, input_device_index=int(config["device_id"].split(":")[1]),
                    stream_callback=self.callback(channel), start=False,
                ))
            self.started = time.monotonic()
            for (channel, config), stream in zip(self.manifest["channels"].items(), self.streams):
                config["initial_offset_ms"] = int((time.monotonic() - self.started) * 1000)
                stream.start_stream()
            self.status = "recording"
            self.save_manifest()
            self.thread = threading.Thread(target=self.run, name=f"SecretaryAudio-{self.meeting_id}", daemon=False)
            self.thread.start()
        except Exception as exc:
            self.fail(f"Не удалось запустить выбранные аудиоустройства: {exc}")
            # A first device might have produced PCM before the second failed.
            # Drain that bounded queue and preserve its files before propagating.
            if self.thread is None:
                self.run()
            else:
                self.thread.join(timeout=30)
            raise

    def _close_native(self) -> None:
        closed = True
        pending = []
        for stream in self.streams:
            try:
                if stream.is_active():
                    stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                closed = False
                pending.append(stream)
        self.streams = pending
        try:
            self.manager.terminate()
        except Exception:
            closed = False
        self.native_closed = closed
        if not closed:
            self.fail("Audio native cleanup pending")

    def run(self) -> None:
        try:
            while not self.stop_event.is_set() or not self.queue.empty():
                now = time.monotonic()
                if now - self.started >= self.max_seconds:
                    self.stop_event.set()
                if (self.storage_check and not self.stop_event.is_set()
                        and now - self.last_space_check >= MIN_SPACE_CHECK_INTERVAL_SECONDS):
                    elapsed = now - self.started
                    self.last_space_check = now
                    try:
                        self.storage_check(elapsed)
                    except Exception as exc:
                        self.fail(f"Недостаточно свободного места; запись остановлена, сохранённые части доступны. {exc}")
                if not self.stop_event.is_set():
                    inactive = [channel for channel, stream in zip(self.writers, self.streams) if not stream.is_active()]
                    if inactive:
                        self.fail(f"Аудиоустройство {', '.join(inactive)} отключилось; запись остановлена, сохранённые части доступны")
                try:
                    channel, data = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                writer = self.writers[channel]
                rate = writer.config["sample_rate"]
                max_frames = max(0, (self.max_seconds * 1000 - writer.config["initial_offset_ms"]) * rate // 1000)
                allowed = max(0, max_frames - writer.total_frames - writer.frames)
                writer.write(data[:allowed * writer.frame_size])
                if allowed <= len(data) // writer.frame_size:
                    self.stop_event.set()
        except Exception as exc:
            storage_error = disk_full_storage_error(exc) if isinstance(exc, OSError) else None
            self.fail(str(storage_error) + " Запись остановлена; завершённые части сохранены." if storage_error
                      else f"Ошибка сохранения аудио: {exc}")
        finally:
            self.stop_event.set()
            self._close_native()
            for writer in self.writers.values():
                try:
                    writer.close()
                except Exception as exc:
                    storage_error = disk_full_storage_error(exc) if isinstance(exc, OSError) else None
                    self.fail(str(storage_error) + " Завершённые части сохранены." if storage_error
                              else f"Ошибка завершения аудиофайла: {exc}")
            with self.lock:
                self.finished_duration_ms = max((w.config["initial_offset_ms"] + w.total_frames * 1000 // w.config["sample_rate"] for w in self.writers.values()), default=0)
                self.status = "failed" if self.error else "stopped"
                self.manifest.update(status=self.status, completed_at=_utc(), error=self.error)
            try:
                self.save_manifest()
            except Exception as exc:
                self.fail(f"Не удалось сохранить audio manifest: {exc}")
                self.status = "failed"
            if self.error:
                try:
                    self.on_error(self.error)
                except Exception:
                    pass  # Manifest remains replayable even if the application is unavailable.
            if self.on_finish:
                try:
                    # Completion carries durable receipts so DB recovery does
                    # not call back into capture while stop is joining this thread.
                    completed = {**self.snapshot(), "chunks": [dict(chunk) for chunk in self.manifest["chunks"]]}
                    self.on_finish(completed)
                except Exception as exc:
                    self.manifest["delivery_error"] = f"Аудио завершено; требуется регистрация окончания: {exc}"
                    try:
                        self.save_manifest()
                    except OSError:
                        pass

    def snapshot(self) -> dict:
        with self.lock:
            duration = self.finished_duration_ms
            if duration is None:
                duration = min(int((time.monotonic() - self.started) * 1000), self.max_seconds * 1000)
            return {"meeting_id": self.meeting_id, "status": self.status,
                    "recording": self.status in {"starting", "recording", "stopping"}, "native_closed": self.native_closed,
                    "error": self.error, "delivery_error": self.manifest.get("delivery_error"), "duration_ms": duration,
                    "queue_blocks": self.queue.qsize(), "queue_capacity": QUEUE_BLOCKS,
                    "completed_chunks": len(self.manifest["chunks"]),
                    "channels": {name: {**config, "saved_frames": self.writers[name].total_frames}
                                 for name, config in self.manifest["channels"].items()}}


class AudioCapture:
    def __init__(self, settings, *, audio_module=None, folder_factory=None,
                 max_seconds=None, max_channels=8, chunk_seconds=None):
        self.settings = settings
        self._module = audio_module
        self._session: _Session | None = None
        self._lock = threading.RLock()
        self._native_pending = False
        self._pending_manager = None
        self._pending_owner = None
        self._folder = folder_factory or (lambda identifier: _meeting_folder(settings.data_dir, identifier))
        self.max_seconds, self.max_channels = max_seconds if max_seconds is not None else MAX_RECORDING_SECONDS, max_channels
        self.chunk_seconds = chunk_seconds or settings.chunk_seconds

    def _audio(self):
        return self._module or importlib.import_module("pyaudiowpatch")

    @staticmethod
    def _enumerate(module, manager) -> tuple[list[dict], dict[str, dict]]:
        host = manager.get_host_api_info_by_type(module.paWASAPI)
        default_input = int(host.get("defaultInputDevice", -1))
        default_loopback = -1
        try:
            default_loopback = int(manager.get_default_wasapi_loopback()["index"])
        except (OSError, ValueError):
            pass
        devices, raw = [], {}
        for info in manager.get_device_info_generator():
            if info.get("hostApi") != host["index"] or int(info.get("maxInputChannels", 0)) <= 0:
                continue
            kind = "system" if info.get("isLoopbackDevice", False) else "microphone"
            device_id = f"wasapi:{info['index']}"
            raw[device_id] = info
            devices.append({"id": device_id, "name": info["name"], "kind": kind,
                            "default": int(info["index"]) == (default_loopback if kind == "system" else default_input)})
        return devices, raw

    def list_devices(self) -> dict:
        manager = None
        try:
            module = self._audio()
            manager = module.PyAudio()
            devices, _ = self._enumerate(module, manager)
            return {"available": bool(devices), "devices": devices,
                    "error": None if devices else "Доступные WASAPI устройства не найдены"}
        except Exception as exc:
            return {"available": False, "devices": [], "error": f"Windows-аудиозахват недоступен: {exc}"}
        finally:
            if manager is not None:
                manager.terminate()

    def start(self, meeting_id: str, microphone_id: str | None = None,
              system_id: str | None = None, on_chunk: Callable[[dict], None] | None = None,
              on_error: Callable[[str], None] | None = None,
              on_finish: Callable[[dict], None] | None = None) -> dict:
        with self._lock:
            if self._native_pending or (self._session and (not self._session.native_closed or (self._session.thread and self._session.thread.is_alive()))):
                raise ValueError("Уже идёт запись; сначала завершите её")
            if not microphone_id and not system_id:
                raise ValueError("Выберите микрофон или системный звук")
            folder = self._folder(meeting_id)
            if (folder / "capture.json").exists():
                raise ValueError("У встречи уже есть запись; для новой записи создайте новую встречу")
            self._session = None
            module = self._audio()
            self._native_pending = True
            self._pending_owner = meeting_id
            session = None
            try:
                # Retain exact ownership independently of _Session. A constructor
                # exception has no returned cleanup handle and remains unconfirmed.
                manager = module.PyAudio()
                self._pending_manager = manager
                devices, raw = self._enumerate(module, manager)
                kinds = {device["id"]: device["kind"] for device in devices}
                selected = []
                for channel, device_id in (("microphone", microphone_id), ("system", system_id)):
                    if device_id is not None:
                        if kinds.get(device_id) != channel:
                            raise ValueError(f"Выбранное устройство {channel} недоступно; обновите список")
                        selected.append((channel, raw[device_id]))
                selected_devices = [device for _, device in selected]
                ensure_free_space(
                    self.settings.data_dir,
                    required_bytes=estimate_capture_workspace_bytes(selected_devices, self.max_seconds),
                )

                def check_remaining_space(elapsed_seconds: float) -> None:
                    required = remaining_capture_workspace_bytes(selected_devices, self.max_seconds, elapsed_seconds)
                    ensure_free_space(self.settings.data_dir, required_bytes=required)

                session = _Session(folder, meeting_id, selected, module, manager,
                                   self.chunk_seconds, on_chunk or (lambda chunk: None),
                                   on_error or (lambda error: None), on_finish,
                                   self.max_seconds, self.max_channels, check_remaining_space)
                self._session = session
                self._pending_manager = None
                self._pending_owner = None
                self._native_pending = False
                session.start()
                return session.snapshot()
            except BaseException as exc:
                if session is None:
                    self._close_pending_manager()
                storage_error = disk_full_storage_error(exc) if isinstance(exc, OSError) else None
                if storage_error is not None:
                    raise storage_error from None
                raise

    def _close_pending_manager(self):
        if not self._native_pending:
            return True
        if self._pending_manager is None:
            return False  # Uncertain constructor failure: no owned handle to retry.
        try:
            self._pending_manager.terminate()
        except Exception:
            return False
        self._pending_manager = None
        self._pending_owner = None
        self._native_pending = False
        return True

    def stop(self, meeting_id: str) -> dict:
        with self._lock:
            if self._native_pending and self._pending_owner == meeting_id:
                self._close_pending_manager()
                return self.state(meeting_id)
            session = self._session
            if session is None or session.meeting_id != meeting_id:
                return self.state(meeting_id)
            if session.thread and session.thread.is_alive():
                with session.lock:
                    if session.status in {"starting", "recording"}:
                        session.status = "stopping"
                session.stop_event.set()
                session.thread.join(timeout=30)
                if session.thread.is_alive():
                    raise RuntimeError("Аудиодрайвер или диск не завершил запись за 30 секунд; сохранённые части доступны")
            if not session.native_closed:
                session._close_native()
            return session.snapshot()

    def state(self, meeting_id: str) -> dict:
        with self._lock:
            if self._session and self._session.meeting_id == meeting_id:
                return self._session.snapshot()
            return {"meeting_id": meeting_id, "status": "cleanup_pending" if self._native_pending else "idle", "recording": False,
                    "error": "Audio native cleanup pending" if self._native_pending else None,
                    "duration_ms": 0, "completed_chunks": 0, "channels": {},
                    "native_closed": not self._native_pending}

    def close(self) -> None:
        with self._lock:
            if self._native_pending and not self._close_pending_manager():
                raise RuntimeError('Audio native cleanup pending')
            if self._session:
                self.stop(self._session.meeting_id)

    def recover(self, meeting_id: str, on_chunk: Callable[[dict], None] | None = None) -> dict:
        """Idempotent receipt replay; no device opening, cloud submission or raw deletion."""
        with self._lock:
            if self._session and self._session.meeting_id == meeting_id and self._session.thread and self._session.thread.is_alive():
                raise ValueError("Восстановление недоступно во время записи")
            folder = self._folder(meeting_id)
            manifest_path = folder / "capture.json"
            if not manifest_path.exists():
                return {"status": "none", "chunks": [], "notes": []}
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("schema_version") != 1 or manifest.get("meeting_id") != meeting_id:
                raise ValueError("Неподдерживаемый audio manifest")
            notes = manifest.setdefault("recovery_notes", [])
            changed = False
            for channel, descriptor in list(manifest.get("in_progress", {}).items()):
                try:
                    chunk = _repair_partial(folder, channel, descriptor)
                    if chunk is not None:
                        if not any(item["sequence"] == chunk["sequence"] and item["channel"] == channel for item in manifest["chunks"]):
                            manifest["chunks"].append(chunk)
                        manifest["in_progress"].pop(channel, None)
                        notes.append(f"{channel}: восстановлены полные PCM frames; исходный .part сохранён")
                        changed = True
                    else:
                        message = f"{channel}: завершённые PCM frames не подтверждены; исходная часть сохранена без обработки"
                        if message not in notes:
                            notes.append(message)
                            changed = True
                except (OSError, ValueError, wave.Error) as exc:
                    message = f"{channel}: незавершённая часть сохранена без обработки: {exc}"
                    if message not in notes:
                        notes.append(message)
                        changed = True
            if manifest["status"] in {"recording", "starting", "stopping"}:
                manifest.update(status="recovered", completed_at=_utc(), error="Запись прервалась; завершённые части восстановлены")
                changed = True
            if changed:
                _atomic_json(manifest_path, manifest)
            valid_chunks = []
            for chunk in manifest["chunks"]:
                path = _contained_file(folder, chunk["path"])
                if not path.exists() or _hash(path) != chunk["sha256"]:
                    raise ValueError("Сохранённый аудиочанк отсутствует или его hash изменился")
                valid_chunks.append(dict(chunk))
                if on_chunk:
                    on_chunk(dict(chunk))
            if on_chunk and manifest.pop("delivery_error", None):
                _atomic_json(manifest_path, manifest)
            return {"status": manifest["status"], "chunks": valid_chunks, "notes": list(notes)}
