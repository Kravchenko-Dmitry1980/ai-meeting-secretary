"""Existing budget policy may be read after restore, never minted or repaired."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import sqlite3
from uuid import uuid4

import pytest

from secretary.application.monthly_budget import MonthlyBudget
from secretary.domain.cloud_budget import AccountUsage, BudgetError, CloudCharge
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.infrastructure.team_backup import _restore_guard


NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
CAP = 3000_000000


def hashes(path):
    return {item.name: hashlib.sha256(item.read_bytes()).hexdigest()
            for item in path.parent.iterdir() if item.is_file()}


def test_existing_monthly_policy_can_be_reopened_without_rewriting_restored_ledger(tmp_path):
    path = tmp_path / 'billing.sqlite3'
    budget = MonthlyBudget(BudgetRepository(path), clock=lambda: NOW)
    budget.refresh_account(AccountUsage('a' * 64, CAP, CAP, 0, 'monthly', NOW))
    operation = CloudCharge(str(uuid4()), 'meeting_stt', 'b' * 64, 2_000000, 2_000000)
    budget.reserve(operation)
    before_status = budget.reservation(operation.operation_id)
    with closing(sqlite3.connect(path)) as conn, conn:
        _restore_guard(conn, str(uuid4()), 'c' * 64)
    before = hashes(path)
    reopened = MonthlyBudget(BudgetRepository(path), clock=lambda: NOW)
    assert reopened.reservation(operation.operation_id) == before_status
    snapshot = reopened.snapshot()
    assert snapshot.approved_limit_micro == CAP and snapshot.reserved_micro == 2_000000
    with pytest.raises(BudgetError, match='^maintenance_restore_blocked$'):
        reopened.mark_submitted(operation.operation_id)
    assert hashes(path) == before


@pytest.mark.parametrize('policy,code', [(None, 'maintenance_restore_blocked'), ('1', 'monthly_budget_policy_changed')])
def test_restored_missing_or_different_policy_is_not_silently_initialized(tmp_path, policy, code):
    path = tmp_path / 'billing.sqlite3'
    BudgetRepository(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        if policy is not None:
            conn.execute("INSERT INTO billing_state VALUES('approved_micro',?)", (policy,))
        _restore_guard(conn, str(uuid4()), 'c' * 64)
    before = hashes(path)
    with pytest.raises(BudgetError, match='^' + code + '$'):
        MonthlyBudget(BudgetRepository(path), clock=lambda: NOW)
    assert hashes(path) == before
