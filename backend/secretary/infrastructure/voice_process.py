"""Bounded concurrent pipes and sampled, identity-checked owned process tree."""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass

from secretary.domain.voice import VoiceError


@dataclass(frozen=True, repr=False)
class ProcessOutput:
    stdout: bytes
    stderr_bytes: int
    peak_rss_bytes: int


def run_owned(command: list[str], *, input_bytes: bytes = b"", stdout_limit: int,
              stderr_limit: int = 65536, timeout: float = 120,
              rss_limit: int = 2 * 1024**3, cancel: threading.Event | None = None,
              env: dict | None = None) -> ProcessOutput:
    try:
        import psutil
    except ImportError:
        raise VoiceError("runtime_monitor_unavailable") from None
    if cancel is not None and cancel.is_set():
        raise VoiceError("cancelled")
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False, env=env,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise VoiceError("process_unavailable") from None
    tracked = {}
    reason = []
    output = bytearray()
    stderr_count = [0]
    peak = 0
    lock = threading.Lock()

    def fail(code):
        with lock:
            if not reason:
                reason.append(code)

    def drain(pipe, limit, target=None):
        size = 0
        try:
            while block := pipe.read(4096):
                size += len(block)
                if size > limit:
                    fail("process_output_limit")
                elif target is not None:
                    target.extend(block)
        except OSError:
            fail("process_pipe_failure")
        finally:
            if target is None:
                stderr_count[0] = size
            pipe.close()

    def feed():
        try:
            process.stdin.write(input_bytes)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    def remember(p):
        identity = p.create_time()
        tracked[(p.pid, identity)] = p

    def alive(p, identity):
        try:
            return p.create_time() == identity and p.is_running()
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            fail("process_monitor_failure")
            return False

    def cleanup():
        # Refresh verified descendants immediately before terminating their
        # parents; the Windows venv redirector itself can own the Python child.
        for (_, identity), p in list(tracked.items()):
            try:
                if alive(p,identity):
                    for descendant in p.children(recursive=True):
                        remember(descendant)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for (_, identity), p in reversed(list(tracked.items())):
            try:
                if alive(p, identity):
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if process.poll() is None:
            try:
                process.kill()  # The original Popen owns its process handle.
            except OSError:
                fail("process_cleanup_failed")
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            fail("process_cleanup_failed")
        owned = [p for (_, identity), p in tracked.items() if alive(p, identity)]
        _, remaining = psutil.wait_procs(owned, timeout=2)
        if remaining:
            fail("process_cleanup_failed")

    threads = [threading.Thread(target=drain, args=(process.stdout, stdout_limit, output), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, stderr_limit), daemon=True),
               threading.Thread(target=feed, daemon=True)]
    start = time.monotonic()
    try:
        root = psutil.Process(process.pid)
        remember(root)
        for thread in threads:
            thread.start()
        while True:
            current = 0
            for (_, identity), p in list(tracked.items()):
                try:
                    if alive(p, identity):
                        for descendant in p.children(recursive=True):
                            remember(descendant)
                        current += p.memory_info().rss
                except psutil.NoSuchProcess:
                    pass
                except psutil.AccessDenied:
                    fail("process_monitor_failure")
            peak = max(peak, current)
            if peak > rss_limit:
                fail("process_memory_limit")
            if time.monotonic() - start >= timeout:
                fail("process_timeout")
            if cancel is not None and cancel.is_set():
                fail("cancelled")
            if reason or process.poll() is not None:
                break
            time.sleep(0.025)
    except (psutil.Error, OSError):
        fail("process_monitor_failure")
    finally:
        cleanup()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=3)
        if any(t.is_alive() for t in threads):
            fail("process_cleanup_failed")
    if reason:
        raise VoiceError(reason[0])
    if process.returncode != 0:
        raise VoiceError("process_failed")
    return ProcessOutput(bytes(output), stderr_count[0], peak)
