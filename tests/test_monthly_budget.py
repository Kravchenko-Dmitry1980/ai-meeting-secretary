"""Durable shared budget behavior, with synthetic account observations only."""
from datetime import datetime, timezone
import importlib
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def types():
    assert (ROOT / "backend/secretary/application/monthly_budget.py").is_file(), "monthly ledger absent"
    domain = importlib.import_module("secretary.domain.cloud_budget")
    application = importlib.import_module("secretary.application.monthly_budget")
    infrastructure = importlib.import_module("secretary.infrastructure.budget_repository")
    return domain, application, infrastructure


def ledger(tmp_path, *, cap=3000_000000, usage=0, remaining=None, clock=None, key="synthetic-key-a"):
    domain, application, infrastructure = types()
    clock = clock or (lambda: NOW)
    budget = application.MonthlyBudget(infrastructure.BudgetRepository(tmp_path / "billing.sqlite3"), clock=clock)
    budget.refresh_account(domain.AccountUsage(key_tag=key, limit_micro=cap,
        remaining_micro=cap - usage if remaining is None else remaining, usage_micro=usage,
        reset="monthly", observed_at=clock()))
    return budget


def charge(*, amount=10_000000, operation_id=None, category="meeting_stt"):
    domain, _, _ = types()
    return domain.CloudCharge(operation_id=operation_id or str(uuid4()), category=category,
        request_hash="a" * 64, estimated_micro=amount, reserved_micro=amount)


def test_missing_late_receipt_cannot_lower_previously_observed_cost(tmp_path):
    budget = ledger(tmp_path)
    attempt = charge(amount=10_000000)
    budget.reserve(attempt)
    budget.mark_submitted(attempt.operation_id)
    budget.observe_cost(attempt.operation_id, 100)
    budget.settle(attempt.operation_id, {"confirmed_rub": None})
    assert budget.snapshot().reserved_micro == 100_000000


def test_conflicting_lower_receipt_cannot_release_observed_spend(tmp_path):
    budget = ledger(tmp_path)
    attempt = charge(amount=10_000000)
    budget.reserve(attempt)
    budget.observe_cost(attempt.operation_id, 100)
    domain, _, _ = types()
    with pytest.raises(domain.BudgetError, match="budget_receipt_conflict"):
        budget.settle(attempt.operation_id, {"confirmed_rub": 20})
    assert budget.snapshot().reserved_micro == 100_000000


@pytest.mark.asyncio
async def test_delayed_automatic_refresh_cannot_undo_owner_key_rotation(tmp_path):
    from datetime import timedelta
    current = [NOW]
    budget = ledger(tmp_path, clock=lambda: current[0], key="synthetic-key-a")
    current[0] += timedelta(seconds=65)
    domain, _, _ = types()
    class SlowAccount:
        async def read_key_usage(self):
            budget.refresh_account(domain.AccountUsage("synthetic-key-b", 3000_000000,
                3000_000000, 0, "monthly", current[0]))
            return domain.AccountUsage("synthetic-key-a", 3000_000000, 3000_000000,
                                       0, "monthly", current[0])
    budget.account_reader = SlowAccount()
    with pytest.raises(domain.BudgetError, match="monthly_budget_key_changed"):
        await budget.ensure_account(key_tag="synthetic-key-a")
    with budget.repository.transaction() as connection:
        assert connection.execute("SELECT value FROM billing_state WHERE name='active_key'").fetchone()[0] == "synthetic-key-b"


def test_two_processes_cannot_reserve_more_than_shared_remaining(tmp_path):
    budget = ledger(tmp_path, cap=1000_000000)
    child = tmp_path / "reserve.py"
    child.write_text("""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import sys
from secretary.domain.cloud_budget import CloudCharge, BudgetError
from secretary.application.monthly_budget import MonthlyBudget
from secretary.infrastructure.budget_repository import BudgetRepository
b = MonthlyBudget(BudgetRepository(Path(sys.argv[1])), clock=lambda: datetime(2026,10,3,12,tzinfo=timezone.utc))
try:
    b.reserve(CloudCharge(operation_id=str(uuid4()), category='meeting_stt', request_hash='a'*64,
                         estimated_micro=600_000000, reserved_micro=600_000000))
    print('reserved')
except BudgetError as e:
    print(e.code)
""", encoding="utf-8")
    def run_child(_):
        return subprocess.run([sys.executable, "-B", str(child), str(tmp_path / "billing.sqlite3")],
                              capture_output=True, text=True, check=True).stdout.strip()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(run_child, range(2)))
    assert results == ["monthly_budget_exhausted", "reserved"]
    assert budget.snapshot().reserved_micro == 600_000000


def test_period_rolls_at_0100_moscow():
    domain, _, _ = types()
    assert domain.budget_period(datetime(2026, 10, 31, 21, 59, 59, tzinfo=timezone.utc)) == "2026-10"
    assert domain.budget_period(datetime(2026, 10, 31, 22, tzinfo=timezone.utc)) == "2026-11"


def test_unknown_reservation_survives_rollover(tmp_path):
    current = [datetime(2026, 10, 31, 21, 59, tzinfo=timezone.utc)]
    budget = ledger(tmp_path, clock=lambda: current[0])
    attempt = charge(amount=45_000000)
    budget.reserve(attempt)
    budget.mark_uncertain(attempt.operation_id)
    current[0] = datetime(2026, 10, 31, 22, tzinfo=timezone.utc)
    domain, _, _ = types()
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key-a", limit_micro=3000_000000,
        remaining_micro=3000_000000, usage_micro=0, reset="monthly", observed_at=current[0]))
    assert budget.snapshot().period == "2026-11"
    assert budget.snapshot().reserved_micro == 45_000000
    assert budget.snapshot().remaining_micro == 2955_000000


def test_opening_usage_and_receipts_are_not_double_counted(tmp_path):
    domain, _, _ = types()
    budget = ledger(tmp_path, usage=4_000000)
    attempt = charge(amount=10_000000)
    budget.reserve(attempt)
    budget.settle(attempt.operation_id, {"confirmed_rub": "8", "provider_request_id": "synthetic-r1"})
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key-a", limit_micro=3000_000000,
        remaining_micro=2988_000000, usage_micro=12_000000, reset="monthly", observed_at=NOW,
        included_receipt_ids=("synthetic-r1",)))
    snapshot = budget.snapshot()
    assert snapshot.confirmed_micro == 12_000000
    assert snapshot.reserved_micro == 0
    assert snapshot.remaining_micro == 2988_000000


def test_missing_usage_is_not_zero(tmp_path):
    budget = ledger(tmp_path)
    attempt = charge(amount=10_000000)
    budget.reserve(attempt)
    budget.settle(attempt.operation_id, {"confirmed_rub": None, "provider_request_id": "synthetic-r1"})
    assert budget.snapshot().reserved_micro == 10_000000
    assert budget.reservation(attempt.operation_id).status == "uncertain"


def test_late_receipt_settles_once(tmp_path):
    budget = ledger(tmp_path)
    attempt = charge()
    budget.reserve(attempt)
    budget.mark_uncertain(attempt.operation_id)
    receipt = {"confirmed_rub": "7.25", "provider_request_id": "synthetic-r1"}
    budget.settle(attempt.operation_id, receipt)
    budget.settle(attempt.operation_id, receipt)
    assert budget.snapshot().confirmed_micro == 7_250000
    assert budget.snapshot().reserved_micro == 0


def test_key_rotation_does_not_reset_month_spend(tmp_path):
    domain, _, _ = types()
    budget = ledger(tmp_path, usage=2000_000000)
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key-b", limit_micro=3000_000000,
        remaining_micro=3000_000000, usage_micro=0, reset="monthly", observed_at=NOW))
    assert budget.snapshot().remaining_micro == 1000_000000
    with pytest.raises(domain.BudgetError, match="monthly_budget_exhausted"):
        budget.reserve(charge(amount=1001_000000))


@pytest.mark.parametrize("cap,reset", [(None, "monthly"), (3001_000000, "monthly"), (3000_000000, "daily")])
def test_invalid_provider_cap_requires_configuration(tmp_path, cap, reset):
    domain, application, infrastructure = types()
    budget = application.MonthlyBudget(infrastructure.BudgetRepository(tmp_path / "billing.sqlite3"), clock=lambda: NOW)
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key", limit_micro=cap,
        remaining_micro=3000_000000, usage_micro=0, reset=reset, observed_at=NOW))
    with pytest.raises(domain.BudgetError, match="monthly_budget_configuration"):
        budget.reserve(charge())


def test_stale_or_future_snapshot_blocks_new_spend(tmp_path):
    domain, _, _ = types()
    current = [NOW]
    budget = ledger(tmp_path, clock=lambda: current[0])
    current[0] = datetime(2026, 10, 3, 12, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(domain.BudgetError, match="monthly_budget_stale"):
        budget.reserve(charge())


def test_warning_thresholds_are_durable_once_per_period(tmp_path):
    budget = ledger(tmp_path, usage=2800_000000)
    assert budget.take_warnings() == [50, 80, 90]
    assert budget.take_warnings() == []
    restarted = ledger(tmp_path, usage=2800_000000)
    assert restarted.take_warnings() == []


def test_operation_payload_conflict_does_not_release_reserve(tmp_path):
    domain, _, _ = types()
    budget = ledger(tmp_path)
    attempt = charge()
    budget.reserve(attempt)
    with pytest.raises(domain.BudgetError, match="budget_operation_conflict"):
        budget.reserve(charge(amount=11_000000, operation_id=attempt.operation_id))
    assert budget.snapshot().reserved_micro == 10_000000


def test_known_zero_cost_is_distinct_from_missing_cost(tmp_path):
    budget = ledger(tmp_path)
    attempt = charge()
    budget.reserve(attempt)
    budget.settle(attempt.operation_id, {"confirmed_rub": 0, "provider_request_id": "synthetic-free"})
    assert budget.reservation(attempt.operation_id).status == "confirmed"
    assert budget.snapshot().reserved_micro == 0


def test_external_spend_does_not_absorb_later_local_receipt(tmp_path):
    domain, _, _ = types()
    budget = ledger(tmp_path)
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key-a", limit_micro=3000_000000,
        remaining_micro=2900_000000, usage_micro=100_000000, reset="monthly", observed_at=NOW))
    attempt = charge()
    budget.reserve(attempt)
    budget.settle(attempt.operation_id, {"confirmed_rub": 10, "provider_request_id": "synthetic-later"})
    assert budget.snapshot().confirmed_micro == 110_000000
    assert budget.snapshot().remaining_micro == 2890_000000


def test_unproven_aggregate_overlap_stays_reserved_until_id_reconciliation(tmp_path):
    domain, _, _ = types()
    budget = ledger(tmp_path, usage=4_000000)
    attempt = charge()
    budget.reserve(attempt)
    budget.settle(attempt.operation_id, {"confirmed_rub": 8, "provider_request_id": "synthetic-local"})
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key-a", limit_micro=3000_000000,
        remaining_micro=2988_000000, usage_micro=12_000000, reset="monthly", observed_at=NOW))
    assert budget.snapshot().confirmed_micro == 12_000000
    assert budget.snapshot().reserved_micro == 8_000000
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key-a", limit_micro=3000_000000,
        remaining_micro=2988_000000, usage_micro=12_000000, reset="monthly", observed_at=NOW,
        included_receipt_ids=("synthetic-local",)))
    assert budget.snapshot().reserved_micro == 0


def test_rollover_opening_including_old_attempt_does_not_double_confirm(tmp_path):
    domain, _, _ = types()
    current = [datetime(2026, 10, 31, 21, 59, tzinfo=timezone.utc)]
    budget = ledger(tmp_path, clock=lambda: current[0])
    attempt = charge(amount=10_000000)
    budget.reserve(attempt)
    budget.mark_uncertain(attempt.operation_id)
    current[0] = datetime(2026, 10, 31, 22, tzinfo=timezone.utc)
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key-a", limit_micro=3000_000000,
        remaining_micro=2980_000000, usage_micro=20_000000, reset="monthly", observed_at=current[0],
        included_receipt_ids=("synthetic-late",)))
    budget.settle(attempt.operation_id, {"confirmed_rub": 20, "provider_request_id": "synthetic-late",
                                       "provider_period": "2026-11"})
    assert budget.snapshot().confirmed_micro == 20_000000
    assert budget.snapshot().reserved_micro == 0


def test_late_known_cost_above_estimate_keeps_larger_reserve(tmp_path):
    domain, _, _ = types()
    current = [datetime(2026, 10, 31, 21, 59, tzinfo=timezone.utc)]
    budget = ledger(tmp_path, clock=lambda: current[0])
    attempt = charge()
    budget.reserve(attempt)
    current[0] = datetime(2026, 10, 31, 22, tzinfo=timezone.utc)
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key-a", limit_micro=3000_000000,
        remaining_micro=3000_000000, usage_micro=0, reset="monthly", observed_at=current[0]))
    budget.settle(attempt.operation_id, {"confirmed_rub": 100, "provider_request_id": "synthetic-late"})
    assert budget.snapshot().reserved_micro == 100_000000


def test_rotated_account_between_check_and_reserve_cannot_relabel_charge(tmp_path):
    domain, _, _ = types()
    budget = ledger(tmp_path)
    budget.refresh_account(domain.AccountUsage(key_tag="synthetic-key-b", limit_micro=3000_000000,
        remaining_micro=3000_000000, usage_micro=0, reset="monthly", observed_at=NOW))
    with pytest.raises(domain.BudgetError, match="monthly_budget_key_changed"):
        budget.reserve(charge(), key_tag="synthetic-key-a")
    assert budget.snapshot().reserved_micro == 0


@pytest.mark.asyncio
async def test_first_request_after_rollover_refreshes_account(tmp_path):
    domain, _, _ = types()
    current = [datetime(2026, 10, 31, 21, 59, tzinfo=timezone.utc)]
    budget = ledger(tmp_path, clock=lambda: current[0])
    calls = []
    class Reader:
        async def read_key_usage(self):
            calls.append(True)
            return domain.AccountUsage(key_tag="synthetic-key-a", limit_micro=3000_000000,
                remaining_micro=3000_000000, usage_micro=0, reset="monthly", observed_at=current[0])
    budget.account_reader = Reader()
    current[0] = datetime(2026, 10, 31, 22, tzinfo=timezone.utc)
    await budget.ensure_account(key_tag="synthetic-key-a")
    assert calls == [True]
    assert budget.snapshot().paused_code is None


def test_confirmed_receipt_cannot_move_to_another_period_on_replay(tmp_path):
    domain, _, _ = types()
    budget = ledger(tmp_path)
    attempt = charge()
    budget.reserve(attempt)
    budget.settle(attempt.operation_id, {"confirmed_rub": 1, "provider_request_id": "synthetic-r"})
    with pytest.raises(domain.BudgetError, match="budget_receipt_conflict"):
        budget.settle(attempt.operation_id, {"confirmed_rub": 1, "provider_request_id": "synthetic-r",
                                           "provider_period": "2026-11"})


def test_async_job_binding_survives_restart_and_cannot_be_replaced(tmp_path):
    domain, application, infrastructure = types()
    budget = ledger(tmp_path)
    attempt = charge()
    budget.reserve(attempt)
    budget.mark_submitted(attempt.operation_id)
    budget.bind_provider_job(attempt.operation_id, "synthetic-async-job")
    restarted = application.MonthlyBudget(infrastructure.BudgetRepository(tmp_path / "billing.sqlite3"), clock=lambda: NOW)
    assert restarted.operation_for_job("synthetic-async-job") == attempt.operation_id
    with pytest.raises(domain.BudgetError, match="budget_provider_job_conflict"):
        budget.bind_provider_job(attempt.operation_id, "synthetic-other-job")
    with pytest.raises(domain.BudgetError, match="budget_operation_already_dispatched"):
        restarted.mark_submitted(attempt.operation_id)
