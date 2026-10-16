"""An explicitly approved benchmark cap shares the monthly ledger."""
from uuid import uuid4

import pytest

from secretary.domain.cloud_budget import BudgetError, CloudCharge
from test_monthly_budget import ledger


def attempt(scope_id, amount=2_000000):
    return CloudCharge(str(uuid4()), "benchmark", "b" * 64, amount, amount, scope_id=scope_id)


def test_scope_creation_is_immutable_and_deduplicated(tmp_path):
    budget = ledger(tmp_path)
    operation = str(uuid4())
    first = budget.create_scope(operation, 5_000000)
    assert budget.create_scope(operation, 5_000000) == first
    with pytest.raises(BudgetError, match="budget_operation_conflict"):
        budget.create_scope(operation, 6_000000)


def test_scope_and_monthly_reservations_are_atomic(tmp_path):
    budget = ledger(tmp_path, cap=100_000000)
    scope = budget.create_scope(str(uuid4()), 3_000000)
    budget.reserve(attempt(scope["scope_id"]))
    with pytest.raises(BudgetError, match="monthly_budget_scope_exhausted"):
        budget.reserve(attempt(scope["scope_id"]))
    assert budget.scope_snapshot(scope["scope_id"])["reserved_rub"] == 2
    assert budget.snapshot().reserved_micro == 2_000000


def test_scope_settled_cap_is_distinguished_from_uncertainty(tmp_path):
    budget = ledger(tmp_path)
    scope = budget.create_scope(str(uuid4()), 3_000000)
    charge = attempt(scope["scope_id"], 3_000000)
    budget.reserve(charge)
    budget.mark_uncertain(charge.operation_id)
    assert budget.scope_snapshot(scope["scope_id"])["uncertain_count"] == 1
    budget.settle(charge.operation_id, {"provider_request_id": "synthetic", "confirmed_rub": 3})
    result = budget.scope_snapshot(scope["scope_id"])
    assert result["confirmed_rub"] == 3
    assert result["reserved_rub"] == 0
    assert result["uncertain_count"] == 0
    assert result["paused_code"] == "monthly_budget_scope_exhausted"


def test_scope_persists_and_meeting_binding_cannot_move(tmp_path):
    budget = ledger(tmp_path)
    scope = budget.create_scope(str(uuid4()), 10_000000)
    meeting = str(uuid4())
    budget.bind_meeting_scope(meeting, scope["scope_id"])
    assert ledger(tmp_path).scope_for_meeting(meeting) == scope["scope_id"]
    other = budget.create_scope(str(uuid4()), 10_000000)
    with pytest.raises(BudgetError, match="budget_scope_binding_conflict"):
        budget.bind_meeting_scope(meeting, other["scope_id"])


def test_unknown_scope_cannot_dispatch_and_cap_cannot_exceed_policy(tmp_path):
    budget = ledger(tmp_path)
    with pytest.raises(BudgetError, match="unknown_budget_scope"):
        budget.reserve(attempt(str(uuid4())))
    with pytest.raises(BudgetError, match="invalid_budget_scope_cap"):
        budget.create_scope(str(uuid4()), 3001_000000)
