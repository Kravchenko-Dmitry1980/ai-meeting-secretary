"""Retention of runner-owned pytest payloads; never remove a run or its reports.

Locks prevent cooperating runners from cleaning active payloads. This is not a
security sandbox against a hostile process modifying paths during cleanup.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

import psutil

SCHEMA = "secretary-offline-retention-v1"
MARKER = "retention-summary.json"
MAX_FAILED_RUNS = 3
MAX_FAILED_BYTES = 4 * 1024**3
RUN_NAME = re.compile(r"offline-[0-9a-f]{32}\Z")


def _reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _native(path: Path) -> str:
    value = str(path)
    return "\\\\?\\" + value if os.name == "nt" and not value.startswith("\\\\") else value


def _plain(path: Path) -> None:
    """Check every existing ancestor without resolving/following a reparse."""
    for item in reversed((path, *path.parents)):
        try:
            info = os.lstat(_native(item))
        except FileNotFoundError:
            continue
        if _reparse(info):
            raise ValueError("retention_reparse_boundary")


def validate_run(scratch: Path, run: Path) -> Path:
    scratch = Path(os.path.abspath(scratch))
    run = Path(os.path.abspath(run))
    if run.parent != scratch or not RUN_NAME.fullmatch(run.name):
        raise ValueError("retention_run_outside_owned_boundary")
    _plain(run)
    return run


def read_summary(run: Path) -> dict:
    marker = run / MARKER
    _plain(marker)
    value = json.loads(marker.read_text(encoding="utf-8"))
    if value.get("schema") != SCHEMA or value.get("run") != run.name:
        raise ValueError("retention_unowned_run")
    return value


def write_summary(run: Path, value: dict) -> None:
    _plain(run / MARKER)
    temporary = run / ("retention-summary-" + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, run / MARKER)


class Lease:
    """Nonblocking OS lock, automatically released even after process death."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        _plain(self.path)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.handle.seek(0, os.SEEK_END) == 0:
                    self.handle.write(b"\0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise BlockingIOError("retention_run_active") from None
        return self

    def __exit__(self, *args):
        if self.handle:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None


def create_run(scratch: Path) -> Path:
    scratch = Path(os.path.abspath(scratch))
    _plain(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    run = scratch / ("offline-" + uuid4().hex)
    run.mkdir()
    return run


def initialize_run(run: Path) -> None:
    # Called while the supervisor owns its lease, before starting the worker.
    (run / "retention-processes").mkdir()
    write_summary(run, {"schema": SCHEMA, "run": run.name, "started_at": time.time(),
                        "state": "running", "exit_code": None, "pytest_temp": "active"})


def prepare_process(run: Path) -> Path:
    # A crash between spawn and PID registration must fail closed, rather than
    # make a running native child invisible to retention.
    path = run / "retention-processes" / ("process-" + uuid4().hex + ".json")
    _plain(path)
    with path.open("x", encoding="utf-8") as handle:
        json.dump({"pending": True}, handle)
    return path


def register_process(run: Path, pid: int | None, *, ticket: Path | None = None) -> None:
    """Record identities, not command lines, including nested Python children."""
    ticket = ticket or prepare_process(run)
    value = {"finished": True}
    if pid is not None:
        try:
            value = {"pid": pid, "created": psutil.Process(pid).create_time()}
        except psutil.NoSuchProcess:
            pass
    _plain(ticket)
    with ticket.open("w", encoding="utf-8") as handle:
        json.dump(value, handle)


def _has_processes(run: Path) -> bool:
    directory = run / "retention-processes"
    _plain(directory)
    for path in directory.glob("*.json"):
        _plain(path)
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("pending"):
            raise ValueError("retention_spawn_identity_unknown")
        if item.get("finished"):
            continue
        try:
            process = psutil.Process(item["pid"])
            if abs(process.create_time() - item["created"]) < 0.01 and process.is_running():
                return True
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            return True
    return False


def payload_bytes(scratch: Path, run: Path) -> int:
    payload = validate_run(scratch, run) / "pytest-temp"
    _plain(payload)
    total = 0
    pending = [payload]
    while pending:
        path = pending.pop()
        try:
            with os.scandir(_native(path)) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if _reparse(info):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    else:
                        total += info.st_size
        except FileNotFoundError:
            if path != payload:
                raise
    return total


def remove_payload(scratch: Path, run: Path) -> None:
    """Only exact pytest-temp; unlink internal links without entering them."""
    payload = validate_run(scratch, run) / "pytest-temp"
    _plain(payload)
    if not payload.exists():
        return
    pending = [(payload, False)]
    while pending:
        path, visited = pending.pop()
        info = os.lstat(_native(path))
        if _reparse(info):
            if stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x10:
                os.rmdir(_native(path))
            else:
                os.unlink(_native(path))
        elif stat.S_ISDIR(info.st_mode):
            if visited:
                os.rmdir(_native(path))
            else:
                pending.append((path, True))
                with os.scandir(_native(path)) as entries:
                    pending.extend((Path(entry.path), False) for entry in entries)
        else:
            try:
                os.unlink(_native(path))
            except PermissionError:
                current = os.lstat(_native(path))
                # chmod changes the shared inode, including any hardlink outside
                # the payload. Retry only a singly-linked, unchanged readonly file.
                if (_reparse(current) or current.st_nlink != 1
                        or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                        or current.st_mode & stat.S_IWRITE):
                    raise
                os.chmod(_native(path), stat.S_IWRITE | stat.S_IREAD)
                os.unlink(_native(path))


def finish_run(run: Path, exit_code: int) -> None:
    value = read_summary(run)
    value.update(state="finished", exit_code=exit_code, finished_at=time.time(), pytest_temp="pending")
    write_summary(run, value)


def prune(scratch: Path, *, max_failed_runs: int = MAX_FAILED_RUNS,
          max_failed_bytes: int = MAX_FAILED_BYTES) -> dict:
    """Prune marked, idle runs only. Preserve every summary and JUnit report."""
    if max_failed_runs < 0 or max_failed_bytes < 0:
        raise ValueError("retention_negative_budget")
    scratch = Path(os.path.abspath(scratch))
    _plain(scratch)
    result = {"removed_runs": 0, "removed_bytes": 0, "retained_runs": 0, "retained_bytes": 0,
              "active_runs": 0, "warnings": []}
    try:
        with Lease(scratch / ".offline-retention.lock"), ExitStack() as leases:
            candidates = []
            for run in scratch.iterdir():
                if not RUN_NAME.fullmatch(run.name):
                    continue
                # Unmarked historical directories are explicitly out of scope.
                try:
                    run = validate_run(scratch, run)
                    if not (run / MARKER).is_file():
                        continue
                    value = read_summary(run)
                    if value.get("pytest_temp") in {"removed", "absent"}:
                        continue
                    with ExitStack() as owned:
                        owned.enter_context(Lease(run / "retention-supervisor.lock"))
                        owned.enter_context(Lease(run / "retention-worker.lock"))
                        # A supervisor may have finished between the first read
                        # and acquiring its lease. Use the state under both locks.
                        value = read_summary(run)
                        if value.get("pytest_temp") in {"removed", "absent"}:
                            continue
                        if _has_processes(run):
                            result["active_runs"] += 1
                            continue
                        if value["state"] == "running":
                            value.update(state="interrupted", exit_code=2,
                                         finished_at=value.get("started_at", 0))
                            write_summary(run, value)
                        _plain(run / "pytest-temp")
                        if not (run / "pytest-temp").exists():
                            value.update(pytest_temp="absent", pytest_temp_bytes=0)
                            write_summary(run, value)
                            continue
                        size = payload_bytes(scratch, run)
                        candidates.append((run, value, size))
                        leases.enter_context(owned.pop_all())
                except BlockingIOError:
                    result["active_runs"] += 1
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    result["warnings"].append(f"{run.name}: {type(exc).__name__}; payload preserved")
            candidates.sort(key=lambda item: item[1].get("finished_at", 0), reverse=True)
            for run, value, size in candidates:
                keep = (value.get("exit_code") != 0 and result["retained_runs"] < max_failed_runs
                        and result["retained_bytes"] + size <= max_failed_bytes)
                if keep:
                    result["retained_runs"] += 1
                    result["retained_bytes"] += size
                    value.update(pytest_temp="retained", pytest_temp_bytes=size)
                    write_summary(run, value)
                    continue
                # Fence delayed workers before removing a crashed supervisor's payload.
                value.update(state="finished", pytest_temp="removing", pytest_temp_bytes=size)
                write_summary(run, value)
                try:
                    remove_payload(scratch, run)
                except (OSError, ValueError) as exc:
                    value["pytest_temp"] = "cleanup_failed"
                    result["warnings"].append(f"{run.name}: {type(exc).__name__}; cleanup incomplete")
                else:
                    value["pytest_temp"] = "removed"
                    result["removed_runs"] += 1
                    result["removed_bytes"] += size
                write_summary(run, value)
    except BlockingIOError:
        result["warnings"].append("another retention sweep is active; cleanup deferred")
    return result
