"""Shared single-backend resources. Caller cancellation never releases a native lease."""
import threading
import inspect
from contextlib import contextmanager
from secretary.domain.enrollment import EnrollmentFailure


class CaptureLease:
    def __init__(self):
        self._lock = threading.Lock()
        self.owner = None

    def acquire(self, owner):
        with self._lock:
            if self.owner is not None:
                raise EnrollmentFailure('capture_busy')
            self.owner = owner

    def release(self, owner, *, native_closed):
        with self._lock:
            if self.owner == owner and native_closed:
                self.owner = None


class InferenceCoordinator:
    """One app-wide slot. Nonblocking acquisition, caller-owned cancellation event.

    Task4 must wrap every LocalVoiceEngine.embed with slot(owner, cancel).
    Slot is held until the synchronous engine/process cleanup has returned.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._state = threading.Lock()
        self._active = None

    @contextmanager
    def slot(self, owner: str, cancel: threading.Event, *, material_ids=()):
        if not self._lock.acquire(blocking=False):
            raise EnrollmentFailure('inference_busy')
        try:
            finished = threading.Event()
            with self._state:
                self._active = (owner, cancel, frozenset(material_ids), finished)
            if cancel.is_set():
                raise EnrollmentFailure('operation_cancelled')
            yield
        finally:
            with self._state:
                self._active = None
                finished.set()
            self._lock.release()

    def cancel(self, owner: str | None = None):
        with self._state:
            if self._active and (owner is None or self._active[0] == owner):
                self._active[1].set()

    def cancel_material(self, enrollment_id):
        with self._state:
            if self._active and (self._active[0] == enrollment_id or enrollment_id in self._active[2]):
                self._active[1].set()

    def wait_material(self, enrollment_id, timeout):
        with self._state:
            active = self._active
            if not active or (active[0] != enrollment_id and enrollment_id not in active[2]):
                return True
            finished = active[3]
        return finished.wait(timeout)


class LeasedCapture:
    """Adapter for native capture and explicit injected test captures.

    Native AudioCapture supplies native_closed receipts. Legacy injected fakes
    close synchronously; their successful terminal return is the test receipt.
    """
    def __init__(self, capture, lease, namespace, *, legacy=False):
        self.capture, self.lease, self.namespace, self.legacy = capture, lease, namespace, legacy
        self._claimed = None

    def claim(self, identifier):
        owner = (self.namespace, identifier)
        self.lease.acquire(owner)
        self._claimed = identifier

    def _release(self, identifier, result):
        closed = result.get('native_closed', self.legacy and not result.get('recording', False))
        self.lease.release((self.namespace, identifier), native_closed=closed)
        if closed and self._claimed == identifier:
            self._claimed = None

    def start(self, identifier, microphone_id=None, system_id=None, on_chunk=None, on_error=None, on_finish=None):
        if self._claimed != identifier:
            self.claim(identifier)
        def finished(snapshot):
            self._release(identifier, snapshot)
            if on_finish:
                on_finish(snapshot)
        kwargs = {'on_finish': finished} if 'on_finish' in inspect.signature(self.capture.start).parameters else {}
        try:
            result = self.capture.start(identifier, microphone_id, system_id, on_chunk, on_error, **kwargs)
            self._release(identifier, result)
            return result
        except BaseException:
            # Only a native receipt can release uncertain start/driver state.
            try:
                snapshot = self.capture.state(identifier)
                self._release(identifier, snapshot)
            except Exception:
                pass
            raise

    def stop(self, identifier):
        result = self.capture.stop(identifier)
        self._release(identifier, result)
        return result

    def state(self, identifier):
        return self.capture.state(identifier)

    def close(self):
        self.capture.close()
        if self._claimed:
            self._release(self._claimed, self.capture.state(self._claimed))

    def __getattr__(self, name):
        return getattr(self.capture, name)
