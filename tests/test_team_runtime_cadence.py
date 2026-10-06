"""Normal cadence uses monotonic ticks; failures retain bounded retry delay."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from secretary.orchestration import team_runtime


def clocked_loop(monkeypatch, durations, *, failures=(), period=30, maximum=300, oversleep=0):
    logical = [0.0]
    starts, waits, components = [], [], []
    active = [0]
    stop = asyncio.Event()
    runtime = SimpleNamespace(_stop=stop, _work_halted=False, maintenance=None,
        participant_id='SYNTHETIC_CADENCE', _component=lambda *args: components.append(args))

    async def operation():
        active[0] += 1
        assert active[0] == 1, 'A prior admitted operation must complete before another starts'
        index = len(starts)
        starts.append(logical[0])
        await asyncio.sleep(0)
        logical[0] += durations[index]
        active[0] -= 1
        if len(starts) == len(durations):
            stop.set()
        if index in failures:
            raise team_runtime.RuntimeConfigurationError('synthetic_sync_failure')

    async def admitted(maintenance, participant_id, kind, callback):
        return await callback()

    async def wait_for(awaitable, timeout):
        awaitable.close()
        waits.append(timeout)
        logical[0] += timeout + oversleep
        raise TimeoutError

    monkeypatch.setattr(team_runtime, 'time', SimpleNamespace(monotonic=lambda: logical[0]))
    monkeypatch.setattr(team_runtime, 'admitted_operation', admitted)
    monkeypatch.setattr(team_runtime.asyncio, 'wait_for', wait_for)
    asyncio.run(team_runtime.TeamRuntime._loop(runtime, 'sync_7', 'sync', operation, period, maximum=maximum))
    assert active[0] == 0
    return starts, waits, components


@pytest.mark.parametrize('duration,next_start', [
    (0, 30), (5, 30), (30, 30), (45.393, 60), (59.999, 60),
    (60, 60), (60.001, 90), (3_000_005, 3_000_030),
])
def test_healthy_normal_ticks_skip_only_missed_deadlines(monkeypatch, duration, next_start):
    starts, waits, _ = clocked_loop(monkeypatch, [duration, 0])
    assert starts == pytest.approx([0, next_start])
    assert waits == pytest.approx([next_start-duration])


def test_variable_duration_does_not_accumulate_normal_drift(monkeypatch):
    starts, waits, _ = clocked_loop(monkeypatch, [5, 10, 0])
    assert starts == [0, 30, 60]
    assert waits == [25, 20]


def test_normal_period_is_not_truncated_by_backoff_cap(monkeypatch):
    starts, waits, _ = clocked_loop(monkeypatch, [5, 0], period=3600, maximum=30)
    assert starts == [0, 3600]
    assert waits == [3595]


def test_failures_keep_completion_based_backoff_cap_and_reset_on_success(monkeypatch):
    starts, waits, components = clocked_loop(monkeypatch, [5, 5, 5, 5, 5, 5, 0],
        failures=(0, 1, 2, 3, 5))
    assert starts == [0, 65, 190, 435, 740, 770, 835]
    assert waits == [60, 120, 240, 300, 25, 60]
    assert [item[1] for item in components] == ['degraded']*4 + ['healthy', 'degraded', 'healthy']


def test_retry_reanchors_at_actual_start_after_late_timer_wakeup(monkeypatch):
    starts, waits, _ = clocked_loop(monkeypatch, [5, 5, 0], failures=(0,), oversleep=10)
    assert starts == [0, 75, 115]
    assert waits == [60, 25]


def test_late_normal_wakeup_does_not_create_a_catchup_burst(monkeypatch):
    starts, waits, _ = clocked_loop(monkeypatch, [0, 0, 0], oversleep=70)
    assert starts == [0, 100, 190]
    assert waits == [30, 20]


@pytest.mark.asyncio
async def test_stop_during_owned_operation_prevents_timer_and_next_attempt(monkeypatch):
    stop = asyncio.Event()
    events = []

    @asynccontextmanager
    async def admission(participant_id, kind):
        events.append('admission_entered')
        try:
            yield
        finally:
            events.append('admission_released')

    runtime = SimpleNamespace(_stop=stop, _work_halted=False,
        maintenance=SimpleNamespace(async_admission=admission), participant_id='SYNTHETIC_STOP',
        _component=lambda *args: None)

    async def operation():
        events.append('operation_completed')
        stop.set()

    async def unexpected_wait(awaitable, timeout):
        awaitable.close()
        pytest.fail('Stopped loop must not schedule another timer')

    monkeypatch.setattr(team_runtime.asyncio, 'wait_for', unexpected_wait)
    await team_runtime.TeamRuntime._loop(runtime, 'sync_7', 'sync', operation, 30, maximum=300)
    assert events == ['admission_entered', 'operation_completed', 'admission_released']


@pytest.mark.asyncio
async def test_repeated_cancel_keeps_actual_admission_until_owned_work_drains():
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    @asynccontextmanager
    async def admission(participant_id, kind):
        events.append('admission_entered')
        try:
            yield
        finally:
            events.append('admission_released')

    runtime = SimpleNamespace(_stop=asyncio.Event(), _work_halted=False,
        maintenance=SimpleNamespace(async_admission=admission), participant_id='SYNTHETIC_CANCEL',
        _component=lambda *args: None)

    async def operation():
        events.append('operation_started')
        entered.set()
        await release.wait()
        events.append('operation_completed')

    task = asyncio.create_task(team_runtime.TeamRuntime._loop(runtime, 'sync_7', 'sync', operation, 30, maximum=300))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert events == ['admission_entered', 'operation_started']
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert events == ['admission_entered', 'operation_started', 'operation_completed', 'admission_released']
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
