"""Offline process fault ports only; never launch a native binary or HTTP."""
import inspect
import socket
import threading

import pytest

from secretary.infrastructure import restore_native_process as native
from secretary.infrastructure.team_job_observer import _MemberState, _Snapshot


class FaultPorts:
    root = 100

    def __init__(self):
        self.now = 0.0
        self.pid = None
        self.alive = False
        self.creation = '123456'
        self.total = 1
        self.extra = ()
        self.rows = ()
        self.calls = []
        self.exit_code = 1
        self.stop_fault = False
        self.reader_fault = False
        self.image_fault = False
        self.launch_fault = False
        self.release_fault = False
        self.state_fault = False
        self.snapshot_hook = None

    def clock(self):
        return self.now

    def snapshot(self):
        if self.snapshot_hook:
            self.snapshot_hook(self)
        pids = (self.root,) + ((self.pid,) if self.alive else ()) + self.extra
        return _Snapshot(pids, len(pids), len(pids), self.total, len(pids), 0)

    def verify_root(self):
        return self.root

    def listeners(self, port):
        return self.rows

    def pin_binary(self, binary):
        return native.PINNED_NATIVE_BINARY_SHA256

    def launch(self, argv, cwd, environment):
        self.calls.append(('launch', tuple(argv), dict(environment)))
        self.pid, self.alive, self.total = 200, True, 2
        if self.launch_fault:
            raise native.NativeProcessError('native_process_launch_failed')
        return self.pid

    def state(self):
        if self.state_fault:
            raise native.NativeProcessError('native_process_inspection_failed')
        return _MemberState(self.pid, self.creation, self.alive,
            259 if self.alive else self.exit_code, True, 'pinned.exe' if self.alive else None)

    def verify_image(self, state):
        if self.image_fault:
            raise native.NativeProcessError('native_process_image_mismatch')

    def start_reader(self, failed):
        self.failed = failed

    def stop(self, seconds):
        self.calls.append(('stop_original_handle', self.pid, seconds))
        if self.stop_fault:
            raise native.NativeProcessError('native_process_stop_timeout')
        self.alive = False
        return self.exit_code

    def join_reader(self, seconds):
        self.calls.append(('join_reader', seconds))
        return not self.reader_fault

    def release(self):
        self.calls.append(('release',))
        if self.release_fault:
            raise native.NativeProcessError('native_process_handle_cleanup_failed')

    def pause(self, seconds):
        self.now += max(seconds, .01)


def harness(tmp_path, ports=None):
    ports = ports or FaultPorts()
    process = native.ManagedNativeProcess._for_test(tmp_path / 'pinned.exe',
        tmp_path / 'config.yml', tmp_path, {'SystemRoot': 'safe', 'SECRET': 'do-not-print'},
        18888, ports, ports.clock)
    return process, ports


def owned_listener(ports):
    ports.rows = ((socket.AF_INET, '127.0.0.1', 18888, 200),)


def test_complete_lifecycle_always_remains_synthetic(tmp_path):
    process, ports = harness(tmp_path)
    with process:
        owned_listener(ports)
        process.wait_for_listener()
        ports.now = 30
        assert process.remaining_seconds() == 20
    receipt = process.receipt
    assert receipt.result == 'NATIVE_PROCESS_OBSERVED_CLOSED'
    assert receipt.evidence_kind == 'OFFLINE_FAULT_TEST'
    assert receipt.cleanup_complete and receipt.actual_job_root_only
    assert receipt.known_child_reaped and receipt.readers_stopped
    assert receipt.activation_supported is False and receipt.diagnostic_only
    assert 'do-not-print' not in repr(receipt)
    assert ports.calls[0][1] == (str(tmp_path / 'pinned.exe'), 'web', '--config', str(tmp_path / 'config.yml'))


@pytest.mark.parametrize('argument', ['proof', 'launcher', 'clock', 'ports', 'job', 'pid'])
def test_public_constructor_cannot_accept_fault_or_proof_inputs(tmp_path, argument):
    with pytest.raises(TypeError):
        native.ManagedNativeProcess(tmp_path / 'pinned.exe', tmp_path / 'config.yml',
            tmp_path, {'SystemRoot': 'safe'}, 18888, **{argument: object()})


def test_no_custom_deadline_input():
    assert tuple(inspect.signature(native.ManagedNativeProcess).parameters) == (
        'binary', 'config', 'cwd', 'environment', 'port')
    assert native.CYCLE_SECONDS == 60 and native.CLEANUP_RESERVE_SECONDS == 10


@pytest.mark.parametrize('port', [8765, 1, True, 65536])
def test_invalid_port_refused_before_any_ports(tmp_path, port):
    with pytest.raises(native.NativeProcessError, match='port_invalid'):
        native.ManagedNativeProcess(tmp_path / 'pinned.exe', tmp_path / 'config.yml',
            tmp_path, {'SystemRoot': 'safe'}, port)


def test_preexisting_listener_refused_without_launch(tmp_path):
    process, ports = harness(tmp_path)
    ports.rows = ((socket.AF_INET, '127.0.0.1', 18888, 999),)
    with pytest.raises(native.NativeProcessError, match='port_occupied'):
        with process:
            pass
    assert not any(call[0] == 'launch' for call in ports.calls)
    assert process.receipt.cleanup_complete


@pytest.mark.parametrize('row', [
    (socket.AF_INET, '127.0.0.1', 18888, 999),
    (socket.AF_INET, '0.0.0.0', 18888, 200),
    (socket.AF_INET, '192.168.1.2', 18888, 200),
    (socket.AF_INET6, '::1', 18888, 200),
    (socket.AF_INET, '127.0.0.1', 18888, None),
])
def test_exact_listener_bindings_are_required(tmp_path, row):
    process, ports = harness(tmp_path)
    with pytest.raises(native.NativeProcessError, match='listener_not_owned'):
        with process:
            ports.rows = (row,)
            process.wait_for_listener()
    assert any(call[0] == 'stop_original_handle' for call in ports.calls)


@pytest.mark.parametrize('short_lived', [False, True])
def test_unknown_lifetime_is_sticky_even_after_root_only(tmp_path, short_lived):
    process, ports = harness(tmp_path)
    with pytest.raises(native.NativeProcessError):
        with process:
            owned_listener(ports)
            process.wait_for_listener()
            ports.total = 3
            if not short_lived:
                ports.extra = (300,)
            process.checkpoint()
    ports.extra = ()
    assert process.finish().result == 'NATIVE_PROCESS_FAILED'
    assert any(call[0] == 'stop_original_handle' and call[1] == 200 for call in ports.calls)
    assert not any(call[0] == 'release' for call in ports.calls)


def test_operation_deadline_is_not_refreshed_between_reads(tmp_path):
    process, ports = harness(tmp_path)
    with pytest.raises(native.NativeProcessError, match='cycle_deadline'):
        with process:
            owned_listener(ports)
            process.wait_for_listener()
            for value in (10, 20, 30, 40):
                ports.now = value
                process.checkpoint()
            ports.now = 50
            process.checkpoint()
    assert process.receipt.elapsed_seconds == 50
    assert process.receipt.cleanup_complete
    assert 'native_process_cycle_deadline' in process.receipt.failures


@pytest.mark.parametrize('fault', ['image_fault', 'launch_fault', 'state_fault'])
def test_partial_launch_always_cleans_original_child(tmp_path, fault):
    process, ports = harness(tmp_path)
    setattr(ports, fault, True)
    with pytest.raises(native.NativeProcessError):
        with process:
            pass
    assert any(call[0] == 'stop_original_handle' and call[1] == 200 for call in ports.calls)
    assert not ports.alive


@pytest.mark.parametrize('fault', ['stop_fault', 'reader_fault', 'release_fault'])
def test_cleanup_uncertainty_never_succeeds(tmp_path, fault):
    process, ports = harness(tmp_path)
    with pytest.raises(native.NativeProcessError):
        with process:
            owned_listener(ports)
            process.wait_for_listener()
            setattr(ports, fault, True)
    assert process.receipt.result == 'NATIVE_PROCESS_FAILED'
    assert not process.receipt.cleanup_complete


def test_changed_creation_still_cleans_only_original_owned_handle(tmp_path):
    process, ports = harness(tmp_path)
    with pytest.raises(native.NativeProcessError, match='child_identity_uncertain'):
        with process:
            ports.creation = '999999'
            process.checkpoint()
    assert any(call[0] == 'stop_original_handle' and call[1] == 200 for call in ports.calls)
    assert 'native_process_terminal_identity_uncertain' in process.receipt.failures


def test_terminal_259_is_reaped_not_classified_alive(tmp_path):
    process, ports = harness(tmp_path)
    ports.exit_code = 259
    with process:
        owned_listener(ports)
        process.wait_for_listener()
    assert process.receipt.known_child_reaped
    assert process.receipt.terminal_exit_code == 259


def test_body_exception_keeps_cleanup_and_original_exception(tmp_path):
    process, ports = harness(tmp_path)
    with pytest.raises(RuntimeError, match='synthetic body'):
        with process:
            raise RuntimeError('synthetic body')
    assert process.receipt.cleanup_complete
    assert 'native_process_body_interrupted' in process.receipt.failures


def test_accounting_race_is_refused(tmp_path):
    process, ports = harness(tmp_path)
    with pytest.raises(native.NativeProcessError):
        with process:
            count = [0]
            def race(target):
                count[0] += 1
                if count[0] == 2:
                    target.total = 3
            ports.snapshot_hook = race
            process.checkpoint()
    assert process.receipt.result == 'NATIVE_PROCESS_FAILED'


def test_receipt_is_immutable_and_finish_idempotent(tmp_path):
    process, ports = harness(tmp_path)
    with process:
        owned_listener(ports)
        process.wait_for_listener()
    calls = tuple(ports.calls)
    assert process.finish() is process.receipt
    assert tuple(ports.calls) == calls
    with pytest.raises(AttributeError):
        process.receipt.activation_supported = True


def test_production_stop_waits_original_handle_even_for_terminal_259():
    calls = []
    class Kernel:
        def WaitForSingleObject(self, handle, milliseconds):
            calls.append(('wait', handle, milliseconds))
            return 0
        def TerminateProcess(self, *args):
            raise AssertionError('terminal original child must not be terminated')
    class Child:
        _handle = 777
        def wait(self, timeout):
            calls.append(('reap', timeout))
            return 259
    ports = object.__new__(native._ActualPorts)
    ports.kernel, ports.process = Kernel(), Child()
    assert ports.stop(1) == 259
    assert calls == [('wait', 777, 0), ('wait', 777, 1000), ('reap', 0)]


def test_production_stop_terminates_only_retained_original_handle():
    calls, signals = [], iter((258, 0))
    class Kernel:
        def WaitForSingleObject(self, handle, milliseconds):
            calls.append(('wait', handle, milliseconds))
            return next(signals)
        def TerminateProcess(self, handle, code):
            calls.append(('terminate', handle, code))
            return True
    class Child:
        _handle = 888
        pid = 123
        def wait(self, timeout):
            calls.append(('reap', timeout))
            return 259
    ports = object.__new__(native._ActualPorts)
    ports.kernel, ports.process = Kernel(), Child()
    assert ports.stop(2) == 259
    assert calls == [('wait', 888, 0), ('terminate', 888, 1),
        ('wait', 888, 2000), ('reap', 0)]


def test_actual_launch_registers_popen_and_uses_fixed_detached_flags(tmp_path, monkeypatch):
    seen = {}
    def fake_popen(argv, **options):
        seen.update(argv=argv, **options)
        return type('SyntheticPopen', (), {'pid': 200})()
    monkeypatch.setattr(native.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(native.subprocess, 'DETACHED_PROCESS', 8, raising=False)
    ports = object.__new__(native._ActualPorts)
    argv = [str(tmp_path / 'pinned.exe'), 'web', '--config', str(tmp_path / 'config.yml')]
    assert ports.launch(argv, tmp_path, {'SystemRoot': 'safe'}) == 200
    assert ports.process.pid == 200
    assert seen['creationflags'] == 8 and seen['close_fds'] is True
    assert seen['stdin'] == native.subprocess.DEVNULL
    assert seen['stderr'] == native.subprocess.STDOUT
    assert seen['env'] == {'SystemRoot': 'safe'}
    assert seen['argv'] == argv


def test_actual_reader_discards_output_continues_after_size_bound(monkeypatch):
    faults, reads = [], iter((b'123', b'456', b'789', b''))
    monkeypatch.setattr(native, 'MAX_OUTPUT_BYTES', 4)
    monkeypatch.setattr(native.os, 'read', lambda *_: next(reads))
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(native.threading, 'Thread', InlineThread)
    ports = object.__new__(native._ActualPorts)
    ports.process = type('SyntheticPopen', (), {'stdout': type('Stream', (), {'fileno': lambda self: 7})()})()
    ports.start_reader(faults.append)
    assert faults == ['native_process_output_bound', 'native_process_output_bound']


def test_live_reader_prevents_stream_close():
    class Reader:
        def join(self, seconds):
            pass
        def is_alive(self):
            return True
    class Stream:
        def close(self):
            raise AssertionError('closing a live BufferedReader can block')
    ports = object.__new__(native._ActualPorts)
    ports.reader = Reader()
    ports.process = type('SyntheticPopen', (), {'stdout': Stream()})()
    assert ports.join_reader(0) is False


def test_lost_listener_after_initial_success_is_sticky(tmp_path):
    process, ports = harness(tmp_path)
    with pytest.raises(native.NativeProcessError, match='listener_not_owned'):
        with process:
            owned_listener(ports)
            process.wait_for_listener()
            ports.rows = ()
            process.checkpoint()
    assert process.receipt.cleanup_complete
    assert 'native_process_listener_not_owned' in process.receipt.failures


def test_monitor_stops_original_child_at_operation_deadline_while_caller_blocked(tmp_path):
    process, ports = harness(tmp_path)
    process.__enter__()
    owned_listener(ports)
    process.wait_for_listener()
    class DeadlineEvent:
        def wait(self, seconds):
            ports.now = 50
            return False
        def set(self):
            pass
    process._stop_monitor = DeadlineEvent()
    process._run_monitor()  # Synchronous synthetic monitor, no child/thread launch.
    assert not ports.alive
    assert 'native_process_cycle_deadline' in process._failures
    assert any(call[0] == 'stop_original_handle' and call[1] == 200 for call in ports.calls)
    assert process.finish().cleanup_complete


def test_monitor_cancellation_prevents_extra_samples_and_finish_still_reaps(tmp_path):
    process, ports = harness(tmp_path)
    process.__enter__()
    owned_listener(ports)
    process.wait_for_listener()
    process._stop_monitor.set()
    process._run_monitor()
    assert ports.alive
    assert process.finish().cleanup_complete
    assert not ports.alive


def test_offline_unclean_abort_never_terminates_worker_or_promotes_authority(tmp_path):
    process, ports = harness(tmp_path)
    process.__enter__()
    owned_listener(ports)
    process.wait_for_listener()
    ports.reader_fault = True
    with pytest.raises(native.NativeProcessError, match='cleanup_incomplete'):
        process._abort_worker_if_unclean()
    assert process.receipt.evidence_kind == 'OFFLINE_FAULT_TEST'
    assert process.receipt.activation_supported is False


def test_receipt_authority_flags_are_not_constructor_inputs():
    parameters = inspect.signature(native.NativeProcessReceipt).parameters
    for flag in ('diagnostic_only', 'activation_supported', 'hard_os_io_cancellation',
            'mapped_image_qualified'):
        assert flag not in parameters


def test_error_and_sticky_receipt_do_not_echo_unclassified_messages(tmp_path):
    assert str(native.NativeProcessError('secret raw error')) == 'native_process_inspection_failed'
    process, ports = harness(tmp_path)
    process._sticky('secret raw error')
    assert process._failures == ['native_process_inspection_failed']


def test_monitor_start_failure_still_stops_launched_child_and_builds_receipt(tmp_path, monkeypatch):
    process, ports = harness(tmp_path)
    process._threaded = True
    class UnstartedMonitor:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError('synthetic Thread.start failure')
        def join(self, seconds):
            raise RuntimeError('cannot join thread before it is started')
        def is_alive(self):
            return False
    monkeypatch.setattr(native.threading, 'Thread', UnstartedMonitor)
    with pytest.raises(RuntimeError):
        process.__enter__()
    assert any(call[0] == 'stop_original_handle' and call[1] == 200 for call in ports.calls), (
        'a monitor start/join failure must not bypass cleanup of the already-launched original child')
    assert not ports.alive
    assert process.receipt.result == 'NATIVE_PROCESS_FAILED'
    assert process.receipt.known_child_reaped and process.receipt.readers_stopped
    assert process.receipt.actual_job_root_only
    assert 'native_process_launch_failed' in process.receipt.failures


def test_monitor_join_failure_still_stops_child_and_builds_failure_receipt(tmp_path, monkeypatch):
    process, ports = harness(tmp_path)
    process._threaded = True
    class JoinFailureMonitor:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def join(self, seconds):
            raise RuntimeError('synthetic Thread.join failure')
        def is_alive(self):
            return False
    monkeypatch.setattr(native.threading, 'Thread', JoinFailureMonitor)
    with pytest.raises(native.NativeProcessError, match='native_process_monitor_cleanup_failed'):
        with process:
            owned_listener(ports)
            process.wait_for_listener()
    assert any(call[0] == 'stop_original_handle' and call[1] == 200 for call in ports.calls)
    assert not ports.alive
    assert process.receipt.result == 'NATIVE_PROCESS_FAILED'
    assert process.receipt.known_child_reaped and process.receipt.readers_stopped
    assert process.receipt.actual_job_root_only
    assert 'native_process_monitor_cleanup_failed' in process.receipt.failures


def _hold_native_inspection_lock(process):
    acquired, release = threading.Event(), threading.Event()
    def hold():
        with process._lock:
            acquired.set()
            release.wait(2)
    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert acquired.wait(1), 'synthetic lock holder failed to acquire inspection lock'
    return release, holder


def test_finish_bookkeeping_does_not_block_on_monitor_inspection_lock(tmp_path):
    process, ports = harness(tmp_path)
    process.__enter__()
    owned_listener(ports)
    process.wait_for_listener()
    class StillAliveMonitor:
        def join(self, seconds):
            pass  # Model the bounded join returning while synchronous IO remains blocked.
        def is_alive(self):
            return True
    process._monitor = StillAliveMonitor()
    release, holder = _hold_native_inspection_lock(process)
    done, results, errors = threading.Event(), [], []
    def finish():
        try:
            results.append(process.finish())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()
    finisher = threading.Thread(target=finish, daemon=True)
    finisher.start()
    try:
        assert done.wait(.2), 'finish failure bookkeeping must not wait on blocked monitor inspection IO'
        assert not errors
        assert results[0].known_child_reaped and not results[0].cleanup_complete
        assert 'native_process_monitor_cleanup_failed' in results[0].failures
    finally:
        release.set()
        holder.join(1)
        finisher.join(1)


def test_checkpoint_wait_for_monitor_lock_is_bounded_by_remaining_cycle(tmp_path):
    process, ports = harness(tmp_path)
    process.__enter__()
    owned_listener(ports)
    process.wait_for_listener()
    ports.now = 49.99  # Only 10ms remain in the immutable operational budget.
    release, holder = _hold_native_inspection_lock(process)
    done, errors = threading.Event(), []
    def checkpoint():
        try:
            process.checkpoint()
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()
    waiter = threading.Thread(target=checkpoint, daemon=True)
    waiter.start()
    try:
        assert done.wait(.2), 'checkpoint inspection-lock acquisition must use the remaining fixed budget'
        assert len(errors) == 1 and isinstance(errors[0], native.NativeProcessError)
        assert errors[0].code == 'native_process_cycle_deadline'
    finally:
        release.set()
        holder.join(1)
        waiter.join(1)
        process.finish()
