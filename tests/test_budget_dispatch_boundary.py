"""Final pre-POST budget authority: real temporary ledger, synthetic HTTP only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
from pydantic import SecretStr
import pytest

from secretary.application.monthly_budget import MonthlyBudget
from secretary.domain.cloud_budget import AccountUsage, BudgetError, CloudCharge
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.infrastructure.polza import PolzaClient, ProviderError

KEY = "synthetic-dispatch-boundary-not-a-credential"
TAG = hashlib.sha256(KEY.encode()).hexdigest()
CAP = 3000_000000
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def make_budget(tmp_path, *, cap=CAP, now=NOW):
    clock = [now]
    ledger = MonthlyBudget(BudgetRepository(tmp_path / "billing.sqlite3"), clock=lambda: clock[0])
    ledger.refresh_account(AccountUsage(TAG, cap, cap, 0, "monthly", clock[0]))
    return ledger, clock


def charge(amount, *, scope_id=None):
    return CloudCharge(str(uuid4()), "voice_intent", "a" * 64, amount, amount, scope_id=scope_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [
    ("stale", "monthly_budget_stale"), ("future", "monthly_budget_stale"),
    ("invalid_cap", "monthly_budget_configuration"), ("exhausted", "monthly_budget_exhausted"),
    ("new_period", "monthly_budget_period_changed"), ("new_period_fresh", "monthly_budget_period_changed"),
    ("key_rotation", "monthly_budget_key_changed"), ("concurrent_receipt", "monthly_budget_exhausted"),
])
async def test_checkpoint_cannot_dispatch_after_budget_authority_changes(tmp_path, change, code):
    initial = datetime(2026, 10, 31, 21, 59, 30, tzinfo=timezone.utc) if change.startswith("new_period") else NOW
    ledger, clock = make_budget(tmp_path, now=initial)
    # A second instance simulates another Secretary/Team process sharing authority.
    other = MonthlyBudget(BudgetRepository(ledger.repository.path), clock=lambda: clock[0])
    previous = charge(1_000000)
    if change == "concurrent_receipt":
        other.reserve(previous)
        other.mark_submitted(previous.operation_id)
    posts = []
    def provider(request):
        posts.append(request.method)
        return httpx.Response(200, json={"id": "synthetic-receipt", "usage": {"cost_rub": "0.01"}})
    class Prices:
        async def quote(self, _):
            return {"prompt_per_million": 1, "completion_per_million": 1, "stt_per_minute": .1}
    settings = SimpleNamespace(data_dir=tmp_path, polza_api_key=SecretStr(KEY), cloud_enabled=True,
        polza_base_url="https://polza.ai/api/v1", request_timeout_seconds=5,
        stt_model="openai/whisper-large-v3-turbo", summary_model="synthetic-model")
    client = PolzaClient(settings, httpx.MockTransport(provider), budget=ledger,
        charge_context={"key_tag": TAG}, price_reader=Prices())
    target = []
    def checkpoint(info):
        target.append(info["operation_id"])
        if change == "stale":
            clock[0] += timedelta(seconds=61)
        elif change == "future":
            clock[0] -= timedelta(seconds=1)
        elif change.startswith("new_period"):
            clock[0] += timedelta(seconds=31)
            if change == "new_period_fresh":
                other.refresh_account(AccountUsage(TAG, CAP, CAP, 0, "monthly", clock[0]))
        elif change == "invalid_cap":
            other.refresh_account(AccountUsage(TAG, 4000_000000, 4000_000000, 0, "monthly", clock[0]))
        elif change == "exhausted":
            other.refresh_account(AccountUsage(TAG, CAP, 0, CAP, "monthly", clock[0]))
        elif change == "key_rotation":
            other.refresh_account(AccountUsage("synthetic-other-key", CAP, CAP, 0, "monthly", clock[0]))
        else:
            other.settle(previous.operation_id, {"confirmed_rub": "3000", "provider_request_id": "synthetic-previous"})
    with pytest.raises(ProviderError) as error:
        await client._request("POST", "chat/completions", payload={"model": "synthetic-model", "messages": [], "max_tokens": 1}, before_submission=checkpoint)
    assert error.value.code == code
    assert posts == []
    assert ledger.reservation(target[0]).status == "released"
    if change == "concurrent_receipt":
        assert ledger.reservation(previous.operation_id).confirmed_micro == CAP


def test_own_reservation_can_use_exact_remaining_without_double_charge(tmp_path):
    ledger, _ = make_budget(tmp_path, cap=100_000000)
    target = charge(100_000000)
    ledger.reserve(target)
    assert ledger.snapshot().remaining_micro == 0
    ledger.mark_submitted(target.operation_id)
    assert ledger.reservation(target.operation_id).status == "submitted"
    assert ledger.snapshot().reserved_micro == 100_000000


def test_two_shared_reservations_each_credit_only_their_own_hold(tmp_path):
    ledger, clock = make_budget(tmp_path, cap=100_000000)
    targets = [charge(60_000000), charge(40_000000)]
    for target in targets:
        ledger.reserve(target)
    def submit(target):
        worker = MonthlyBudget(BudgetRepository(ledger.repository.path), clock=lambda: clock[0])
        worker.mark_submitted(target.operation_id)
        return worker.reservation(target.operation_id).status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit, targets)) == ["submitted", "submitted"]
    assert ledger.snapshot().reserved_micro == 100_000000


@pytest.mark.parametrize("cost,allowed", [(90, True), (91, False)])
@pytest.mark.parametrize("state", ["confirmed", "uncertain", "submitted"])
def test_final_affordability_keeps_concurrent_known_or_unknown_cost(tmp_path, cost, allowed, state):
    ledger, _ = make_budget(tmp_path, cap=100_000000)
    other, target = charge(10_000000), charge(10_000000)
    ledger.reserve(other)
    ledger.mark_submitted(other.operation_id)
    ledger.reserve(target)
    if state == "confirmed":
        ledger.settle(other.operation_id, {"confirmed_rub": str(cost), "provider_request_id": "synthetic-cost"})
    else:
        ledger.observe_cost(other.operation_id, str(cost))
        if state == "uncertain":
            ledger.mark_uncertain(other.operation_id)
    if allowed:
        ledger.mark_submitted(target.operation_id)
    else:
        with pytest.raises(BudgetError, match="monthly_budget_exhausted"):
            ledger.mark_submitted(target.operation_id)
        assert ledger.reservation(target.operation_id).status == "reserved"
    assert ledger.reservation(other.operation_id).status == state


@pytest.mark.parametrize("included", [False, True])
def test_account_refresh_receipt_overlap_is_not_credited_as_own_reserve(tmp_path, included):
    ledger, clock = make_budget(tmp_path, cap=100_000000)
    prior = charge(50_000000)
    ledger.reserve(prior)
    ledger.mark_submitted(prior.operation_id)
    ledger.settle(prior.operation_id, {"confirmed_rub": "50", "provider_request_id": "synthetic-overlap"})
    target = charge(50_000000)
    ledger.reserve(target)
    ledger.refresh_account(AccountUsage(TAG, 100_000000, 50_000000, 50_000000, "monthly", clock[0],
        included_receipt_ids=("synthetic-overlap",) if included else ()))
    if included:
        ledger.mark_submitted(target.operation_id)
    else:
        with pytest.raises(BudgetError, match="monthly_budget_exhausted"):
            ledger.mark_submitted(target.operation_id)
    assert ledger.reservation(prior.operation_id).confirmed_micro == 50_000000


@pytest.mark.parametrize("status", ["submitted", "uncertain", "confirmed", "released"])
def test_final_denial_never_releases_or_replays_an_already_dispatched_operation(tmp_path, status):
    ledger, clock = make_budget(tmp_path)
    target = charge(10_000000)
    ledger.reserve(target)
    if status == "released":
        ledger.release_unsubmitted(target.operation_id)
    else:
        ledger.mark_submitted(target.operation_id)
        if status == "uncertain":
            ledger.mark_uncertain(target.operation_id)
        elif status == "confirmed":
            ledger.settle(target.operation_id, {"confirmed_rub": "9", "provider_request_id": "synthetic-final"})
    clock[0] += timedelta(seconds=61)
    with pytest.raises(BudgetError, match="budget_operation_already_dispatched"):
        ledger.mark_submitted(target.operation_id)
    with pytest.raises(BudgetError, match="budget_operation_already_dispatched"):
        ledger.release_unsubmitted(target.operation_id)
    assert ledger.reservation(target.operation_id).status == status


@pytest.mark.parametrize("cost,allowed", [(90, True), (91, False)])
def test_scope_cap_is_rechecked_with_integer_own_credit(tmp_path, cost, allowed):
    ledger, _ = make_budget(tmp_path)
    scope = ledger.create_scope(str(uuid4()), 100_000000)["scope_id"]
    prior, target = charge(10_000000, scope_id=scope), charge(10_000000, scope_id=scope)
    ledger.reserve(prior)
    ledger.mark_submitted(prior.operation_id)
    ledger.reserve(target)
    ledger.settle(prior.operation_id, {"confirmed_rub": str(cost), "provider_request_id": "synthetic-scope"})
    if allowed:
        ledger.mark_submitted(target.operation_id)
    else:
        with pytest.raises(BudgetError, match="monthly_budget_scope_exhausted"):
            ledger.mark_submitted(target.operation_id)
        assert ledger.reservation(target.operation_id).status == "reserved"
