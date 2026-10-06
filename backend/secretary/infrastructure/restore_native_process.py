"""Internal, one-shot native lifetime in a disposable observation worker.

The argv marker prevents accidental application-process use; it is NOT authority.
Imports launch nothing. Receipts describe process observations only, never source
custody, operator approval, HTTP rights, or activation. Synchronous Win32 calls
have no hard OS cancellation. The coordinator must retain source custody until
cleanup is proved, including when this component reports failure.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import psutil

from . import team_job_observer as observer
from .team_process_job import install_process_job
from .restore_native_history import PINNED_NATIVE_BINARY_SHA256

WORKER_MARKER = '--managed-native-observation-worker'
CYCLE_SECONDS = 60.0
CLEANUP_RESERVE_SECONDS = 10.0
POLL_SECONDS = .05
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_CODES = frozenset(('native_process_absolute_paths_required', 'native_process_binary_pin_mismatch',
    'native_process_body_interrupted', 'native_process_child_identity_uncertain',
    'native_process_cleanup_incomplete', 'native_process_closed', 'native_process_cycle_deadline',
    'native_process_disposable_worker_required', 'native_process_environment_invalid',
    'native_process_final_job_failed', 'native_process_handle_cleanup_failed',
    'native_process_image_mismatch', 'native_process_image_unavailable',
    'native_process_inspection_failed', 'native_process_job_accounting_uncertain',
    'native_process_job_not_root_only', 'native_process_job_snapshot_raced',
    'native_process_launch_failed', 'native_process_launch_identity_unproven',
    'native_process_listener_inspection_failed', 'native_process_listener_not_owned',
    'native_process_listener_not_verified', 'native_process_monitor_cleanup_failed',
    'native_process_monitor_stop_failed', 'native_process_not_closed', 'native_process_output_bound',
    'native_process_port_invalid', 'native_process_port_occupied',
    'native_process_reader_cleanup_failed', 'native_process_reader_failed',
    'native_process_reap_failed', 'native_process_reentered', 'native_process_root_identity_changed',
    'native_process_stop_failed', 'native_process_stop_inspection_failed', 'native_process_stop_timeout',
    'native_process_terminal_identity_unavailable', 'native_process_terminal_identity_uncertain',
    'native_process_terminate_failed', 'native_process_unknown_job_member', 'native_process_windows_required'))


class NativeProcessError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _CODES | observer._CODES else 'native_process_inspection_failed'
        super().__init__(self.code)


def _fail(code):
    raise NativeProcessError(code) from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class NativeProcessReceipt:
    result: str
    evidence_kind: str
    root_pid: int
    child_pid: int | None
    creation_time: str | None
    binary_sha256: str | None
    argv_sha256: str
    environment_sha256: str
    listener_verified: bool
    terminal_exit_code: int | None
    known_child_reaped: bool
    readers_stopped: bool
    actual_job_root_only: bool
    cleanup_complete: bool
    failures: tuple[str, ...]
    elapsed_seconds: float
    diagnostic_only: bool = field(default=True, init=False)
    activation_supported: bool = field(default=False, init=False)
    hard_os_io_cancellation: bool = field(default=False, init=False)
    mapped_image_qualified: bool = field(default=False, init=False)


class _ActualPorts:
    def __init__(self, check):
        if os.name != 'nt':
            _fail('native_process_windows_required')
        if len(sys.argv) < 2 or sys.argv[1] != WORKER_MARKER:
            _fail('native_process_disposable_worker_required')
        self.job = install_process_job()
        self.win = observer._Win32Ports(self.job, check)
        self.root = self.win.verify_job()
        self.process = None
        self.lease = None
        self.kernel = self.win.kernel
        self.kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        self.kernel.TerminateProcess.restype = w.BOOL

    def snapshot(self):
        return self.win.job_snapshot()

    def verify_root(self):
        return self.win.verify_job()

    def listeners(self, port):
        try:
            return tuple((item.family, item.laddr.ip, item.laddr.port, item.pid)
                for item in psutil.net_connections(kind='tcp')
                if item.status == psutil.CONN_LISTEN and item.laddr.port == port)
        except (psutil.Error, OSError, AttributeError):
            _fail('native_process_listener_inspection_failed')

    def pin_binary(self, binary):
        self.lease = self.win.open_image(str(binary))
        facts = self.win.image_facts(self.lease)
        if facts.sha256 != PINNED_NATIVE_BINARY_SHA256:
            _fail('native_process_binary_pin_mismatch')
        self.binary_facts = facts
        return facts.sha256

    def launch(self, argv, cwd, environment):
        # Register returned Popen before any identity/deadline checks can fail.
        self.process = subprocess.Popen(argv, cwd=cwd, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS)
        return self.process.pid

    def state(self):
        return self.win.member_state(int(self.process._handle))

    def verify_image(self, state):
        if not state.image_path:
            _fail('native_process_image_unavailable')
        facts = self.win.image_facts(self.lease)
        lease = self.win.open_image(state.image_path)
        actual = self.win.image_facts(lease)
        same_file = ('volume_serial', 'file_id', 'size', 'sha256')
        if (facts != self.binary_facts
                or any(getattr(actual, field) != getattr(self.binary_facts, field) for field in same_file)):
            _fail('native_process_image_mismatch')

    def start_reader(self, failed):
        def drain():
            total = 0
            try:
                while data := os.read(self.process.stdout.fileno(), 4096):
                    total += len(data)
                    if total > MAX_OUTPUT_BYTES:
                        failed('native_process_output_bound')
                    # Discard raw native output, including possible secrets.
            except Exception:
                failed('native_process_reader_failed')
        self.reader = threading.Thread(target=drain, daemon=True,
            name='native-observation-output')
        self.reader.start()

    def stop(self, seconds):
        handle = int(self.process._handle)
        wait = self.kernel.WaitForSingleObject(handle, 0)
        if wait not in (0, 258):
            _fail('native_process_stop_inspection_failed')
        if wait == 258 and not self.kernel.TerminateProcess(handle, 1):
            _fail('native_process_terminate_failed')
        if self.kernel.WaitForSingleObject(handle, max(0, int(seconds * 1000))) != 0:
            _fail('native_process_stop_timeout')
        # wait() reaps using the retained original HANDLE, including exit 259.
        try:
            return self.process.wait(timeout=0)
        except (OSError, subprocess.TimeoutExpired):
            _fail('native_process_reap_failed')

    def join_reader(self, seconds):
        reader = getattr(self, 'reader', None)
        if reader:
            reader.join(max(0, seconds))
            if reader.is_alive():
                return False
        if self.process and self.process.stdout:
            self.process.stdout.close()
        return True

    def release(self):
        errors = self.win.close_all()
        if errors:
            _fail('native_process_handle_cleanup_failed')
        if self.process:
            self.process._handle.Close()

    def pause(self, seconds):
        time.sleep(seconds)

    def abort_worker(self):
        # One-shot worker only. This prevents custody context unwinding while
        # unknown Job members/readers remain; no numeric PID is targeted.
        os._exit(70)


class ManagedNativeProcess:
    """Complete lifecycle; caller supplies only coordinator-built launch inputs.

    Use as a context around fixed GET transport. checkpoint() brackets reads;
    a kernel monitor also runs while synchronous transport is blocked. The
    immutable 60s cycle includes a 10s cleanup reserve. No deadline refresh.
    Public input is not approval, custody, or proof of dedicated-worker origin.
    """
    def __init__(self, binary, config, cwd, environment, port):
        self._initialize(binary, config, cwd, environment, port,
            time.monotonic, 'CURRENT_KERNEL_MANAGED_NATIVE', True)
        self._ports = _ActualPorts(self._io_check)
        self._root = self._ports.root

    def _initialize(self, binary, config, cwd, environment, port, clock, kind, threaded):
        if type(port) is not int or not 1024 <= port <= 65535 or port == 8765:
            _fail('native_process_port_invalid')
        if (type(environment) is not dict or not environment
                or any(type(key) is not str or type(value) is not str or not key
                    or '\x00' in key + value or '=' in key for key, value in environment.items())):
            _fail('native_process_environment_invalid')
        paths = tuple(Path(value) for value in (binary, config, cwd))
        if any(not path.is_absolute() for path in paths):
            _fail('native_process_absolute_paths_required')
        self._binary, self._config, self._cwd = paths
        self._environment = dict(environment)
        self._port, self._clock, self._kind, self._threaded = port, clock, kind, threaded
        self._argv = [str(self._binary), 'web', '--config', str(self._config)]
        self._started = clock()
        self._deadline = self._started + CYCLE_SECONDS
        self._operation_deadline = self._deadline - CLEANUP_RESERVE_SECONDS
        self._cleanup = False
        self._ports = None
        self._root = 0
        self._child = None
        self._creation = None
        self._binary_sha = None
        self._attempted = False
        self._entered = False
        self._listener_verified = False
        self._receipt = None
        self._failures = []
        self._lock = threading.RLock()
        # Never hold this bookkeeping lock across native IO. A stuck monitor
        # must not prevent recording failure and stopping the original child.
        self._failure_lock = threading.Lock()
        self._stop_monitor = threading.Event()
        self._monitor = None

    @classmethod
    def _for_test(cls, binary, config, cwd, environment, port, ports, clock):
        instance = object.__new__(cls)
        instance._initialize(binary, config, cwd, environment, port, clock,
            'OFFLINE_FAULT_TEST', False)
        instance._ports, instance._root = ports, ports.root
        return instance

    def _sticky(self, code):
        code = NativeProcessError(code).code
        with self._failure_lock:
            if code not in self._failures:
                self._failures.append(code)

    def _failure_codes(self):
        with self._failure_lock:
            return tuple(self._failures)

    def _io_check(self):
        cutoff = self._deadline if self._cleanup else self._operation_deadline
        if self._clock() >= cutoff:
            _fail('native_process_cycle_deadline')

    def remaining_seconds(self):
        self.checkpoint()
        return max(0, self._operation_deadline - self._clock())

    def _snapshot(self, final=False):
        value = self._ports.snapshot()
        integers = (value.assigned, value.listed, value.total, value.active, value.terminated)
        expected = 2 if self._attempted else 1
        if (any(type(item) is not int or not 0 <= item < 2**32 for item in integers)
                or value.assigned != value.listed or value.listed != len(value.pids)
                or value.active != value.listed or len(set(value.pids)) != len(value.pids)
                or self._root not in value.pids or value.total != expected
                or value.terminated > value.total
                or any(type(pid) is not int or pid <= 0 for pid in value.pids)):
            _fail('native_process_job_accounting_uncertain')
        permitted = {self._root} | ({self._child} if self._child else set())
        if set(value.pids) - permitted:
            _fail('native_process_unknown_job_member')
        if final and value.pids != (self._root,):
            _fail('native_process_job_not_root_only')
        return value

    def _sample(self, listener=False):
        before = self._snapshot()
        if self._child is not None:
            state = self._ports.state()
            if (state.pid != self._child or state.creation != self._creation
                    or not state.alive or not state.member or self._child not in before.pids):
                _fail('native_process_child_identity_uncertain')
            if listener:
                rows = self._ports.listeners(self._port)
                if len(rows) != 1 or any(row != (socket.AF_INET, '127.0.0.1', self._port, self._child)
                    for row in rows):
                    _fail('native_process_listener_not_owned')
        after = self._snapshot()
        if before != after:
            _fail('native_process_job_snapshot_raced')

    def checkpoint(self):
        if self._receipt is not None:
            _fail('native_process_closed')
        cutoff = self._deadline if self._cleanup else self._operation_deadline
        remaining = max(0, cutoff - self._clock())
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            self._sticky('native_process_cycle_deadline')
            _fail('native_process_cycle_deadline')
        try:
            if self._receipt is not None:
                _fail('native_process_closed')
            try:
                self._io_check()
                failures = self._failure_codes()
                if failures:
                    _fail(failures[0])
                self._sample(self._listener_verified)
                self._io_check()
            except Exception as error:
                code = getattr(error, 'code', 'native_process_inspection_failed')
                self._sticky(code)
                raise NativeProcessError(code) from None
        finally:
            self._lock.release()

    def __enter__(self):
        if self._entered or self._receipt is not None:
            _fail('native_process_reentered')
        self._entered = True
        try:
            self.checkpoint()
            if self._ports.listeners(self._port):
                _fail('native_process_port_occupied')
            self._binary_sha = self._ports.pin_binary(self._binary)
            self.checkpoint()
            if self._ports.listeners(self._port):
                _fail('native_process_port_occupied')
            # The production port registers returned Popen before this call returns.
            self._attempted = True
            self._child = self._ports.launch(self._argv, self._cwd, self._environment)
            self._ports.start_reader(self._sticky)
            state = self._ports.state()
            self._creation = state.creation
            if (state.pid != self._child or type(state.creation) is not str
                    or not state.creation.isdecimal() or not 0 < int(state.creation) < 2**64
                    or not state.alive or not state.member):
                _fail('native_process_launch_identity_unproven')
            self._ports.verify_image(state)
            self.checkpoint()
            if self._threaded:
                self._monitor = threading.Thread(target=self._run_monitor, daemon=True,
                    name='native-observation-job')
                self._monitor.start()
            return self
        except BaseException as error:
            self._sticky(getattr(error, 'code', 'native_process_launch_failed'))
            self.finish()
            raise

    def _run_monitor(self):
        while not self._stop_monitor.wait(POLL_SECONDS):
            try:
                self.checkpoint()
            except Exception:
                # Stop ONLY our original Popen child while GET remains blocked.
                try:
                    self._ports.stop(max(0, self._deadline - self._clock()))
                except BaseException as error:
                    self._sticky(getattr(error, 'code', 'native_process_monitor_stop_failed'))
                return

    def wait_for_listener(self):
        while True:
            self.checkpoint()
            rows = self._ports.listeners(self._port)
            if rows:
                if len(rows) != 1 or any(row != (socket.AF_INET, '127.0.0.1', self._port, self._child) for row in rows):
                    self._sticky('native_process_listener_not_owned')
                    _fail('native_process_listener_not_owned')
                self._listener_verified = True
                self.checkpoint()
                return
            self._ports.pause(min(POLL_SECONDS, self.remaining_seconds()))

    def finish(self):
        if self._receipt is not None:
            return self._receipt
        self._stop_monitor.set()
        monitor_stopped = self._monitor is None
        if self._monitor:
            try:
                self._monitor.join(max(0, self._deadline - self._clock()))
                monitor_stopped = not self._monitor.is_alive()
            except BaseException:
                # Thread.start can fail after assigning an unstarted monitor;
                # its join/state errors must never bypass original-child stop.
                # Uncertain monitor cleanup retains handles for worker abort.
                self._sticky('native_process_monitor_cleanup_failed')
            if not monitor_stopped:
                self._sticky('native_process_monitor_cleanup_failed')
        if self._child is not None and not self._failure_codes():
            try:
                self.checkpoint()
            except BaseException as error:
                self._sticky(getattr(error, 'code', 'native_process_inspection_failed'))
        self._cleanup = True
        reaped, readers, root_only, released, code = False, False, False, False, None
        def attempt(action, failure):
            try:
                return action()
            except BaseException as error:
                self._sticky(getattr(error, 'code', failure))
                return None
        # Never skip original-child cleanup because an earlier proof failed.
        if self._attempted and self._ports is not None:
            code = attempt(lambda: self._ports.stop(max(0, self._deadline - self._clock())),
                'native_process_stop_failed')
            reaped = code is not None
            if reaped and self._child is not None and self._creation is not None:
                state = attempt(self._ports.state, 'native_process_terminal_identity_unavailable')
                if (state is None or state.pid != self._child or state.creation != self._creation
                        or state.alive):
                    self._sticky('native_process_terminal_identity_uncertain')
        elif not self._attempted:
            reaped = True
        if self._ports is not None:
            readers = attempt(lambda: self._ports.join_reader(max(0, self._deadline - self._clock())),
                'native_process_reader_cleanup_failed') is True
            if not readers:
                self._sticky('native_process_reader_cleanup_failed')
            # Stopped known child can briefly remain listed; bounded fresh checks only.
            while True:
                try:
                    before = self._snapshot(final=True)
                    if self._ports.verify_root() != self._root:
                        _fail('native_process_root_identity_changed')
                    if before != self._snapshot(final=True):
                        _fail('native_process_job_snapshot_raced')
                    root_only = True
                    break
                except BaseException as error:
                    fault = getattr(error, 'code', 'native_process_final_job_failed')
                    if fault != 'native_process_job_not_root_only' or self._clock() >= self._deadline:
                        self._sticky(fault)
                        break
                    self._ports.pause(min(POLL_SECONDS, max(0, self._deadline - self._clock())))
            if reaped and readers and root_only and monitor_stopped:
                released = attempt(lambda: (self._ports.release(), True)[1],
                    'native_process_handle_cleanup_failed') is True
        if self._clock() >= self._deadline:
            self._sticky('native_process_cycle_deadline')
        if not self._listener_verified:
            self._sticky('native_process_listener_not_verified')
        complete = reaped and readers and root_only and released
        failures = self._failure_codes()
        self._receipt = NativeProcessReceipt('NATIVE_PROCESS_FAILED' if failures or not complete
            else 'NATIVE_PROCESS_OBSERVED_CLOSED', self._kind, self._root, self._child,
            self._creation, self._binary_sha, _digest(self._argv), _digest(self._environment),
            self._listener_verified, code, reaped, readers, root_only, complete,
            failures, max(0, self._clock() - self._started))
        return self._receipt

    def _abort_worker_if_unclean(self):
        """Coordinator MUST call inside custody finally, before releasing files."""
        receipt = self.finish()
        if not receipt.cleanup_complete:
            if self._kind == 'CURRENT_KERNEL_MANAGED_NATIVE' and type(self._ports) is _ActualPorts:
                self._ports.abort_worker()
            _fail('native_process_cleanup_incomplete')

    @property
    def receipt(self):
        if self._receipt is None:
            _fail('native_process_not_closed')
        return self._receipt

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self._sticky(getattr(value, 'code', 'native_process_body_interrupted'))
        receipt = self.finish()
        if kind is None and receipt.result != 'NATIVE_PROCESS_OBSERVED_CLOSED':
            _fail(receipt.failures[0] if receipt.failures else 'native_process_cleanup_incomplete')
        return False
