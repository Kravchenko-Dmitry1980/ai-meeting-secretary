"""T12 paid boundary uses real sidecar/Billing SQLite and synthetic HTTP only."""
import asyncio
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from secretary.application.monthly_budget import MonthlyBudget
from secretary.domain.cloud_budget import AccountUsage, BudgetError, CloudCharge
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.infrastructure.polza import PolzaClient, ProviderError, build_polza_client
from secretary.infrastructure.team_maintenance import MaintenanceError, MaintenanceRepository

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
KEY = "synthetic-paid-maintenance-not-credential"
KEY_TAG = hashlib.sha256(KEY.encode()).hexdigest()


def control(tmp_path):
    repo = MaintenanceRepository(tmp_path / "control.sqlite3", str(uuid4()), clock=lambda: NOW)
    repo.register_participant("main", run_id=str(uuid4()), identity={"pid": 123,
        "creation_time": "synthetic", "executable_sha256": "a" * 64, "argv_sha256": "b" * 64})
    return repo


def config(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, polza_api_key=SecretStr(KEY), cloud_enabled=True,
        polza_base_url="https://polza.ai/api/v1", request_timeout_seconds=5,
        stt_model="openai/whisper-large-v3-turbo", summary_model="synthetic-model",
        approved_monthly_external_costs_rub=0)


class Prices:
    async def quote(self, _):
        return {"prompt_per_million": 1, "completion_per_million": 1, "stt_per_minute": .1}


def budget(tmp_path, *, maintenance=None, billing_path=None):
    repo = BudgetRepository(billing_path or tmp_path / "billing.sqlite3", maintenance=maintenance,
                            participant_id="main" if maintenance else None)
    ledger = MonthlyBudget(repo, clock=lambda: NOW)
    ledger.refresh_account(AccountUsage(KEY_TAG, 3000_000000, 3000_000000, 0, "monthly", NOW))
    return ledger


def client(tmp_path, handler, *, maintenance=None, billing_path=None):
    ledger = budget(tmp_path, maintenance=maintenance, billing_path=billing_path)
    return PolzaClient(config(tmp_path), httpx.MockTransport(handler), budget=ledger,
        charge_context={"key_tag": KEY_TAG}, price_reader=Prices(), maintenance=maintenance,
        participant_id="main" if maintenance else None, billing_path=billing_path), ledger


def restore_guard(path):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO maintenance_restore_guard(id,restore_id,reconciliation_required,manifest_sha256) VALUES(1,?,1,?)",
                     (str(uuid4()), "c" * 64))


async def post(api, *, checkpoint=None):
    return await api._request("POST", "chat/completions", payload={"model": "synthetic-model", "messages": [],
        "max_tokens": 1}, response_checkpoint=checkpoint)


@pytest.mark.asyncio
async def test_paid_ticket_spans_transport_settlement_checkpoint_and_drain(tmp_path):
    maintenance = control(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    barrier = []
    async def handler(_):
        assert maintenance.status()["active_tickets"] == 1
        barrier.append(maintenance.begin_barrier(owner_id="backup"))
        with ledger.repository.transaction() as conn:
            operation_id = conn.execute("SELECT operation_id FROM billing_charges WHERE status='submitted'").fetchone()[0]
        assert maintenance.active_tickets(barrier[0])[0].operation_id == operation_id
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"id": "synthetic-response", "usage": {"cost_rub": "0.02"}})
    api, ledger = client(tmp_path, handler, maintenance=maintenance)
    async def checkpoint(_):
        assert maintenance.status()["mode"] == "draining"
        assert maintenance.status()["active_tickets"] == 1
        with ledger.repository.transaction() as conn:
            assert conn.execute("SELECT status FROM billing_charges").fetchone()[0] == "confirmed"
    operation = asyncio.create_task(post(api, checkpoint=checkpoint))
    await asyncio.wait_for(entered.wait(), 2)
    assert maintenance.status()["active_tickets"] == 1
    with pytest.raises(MaintenanceError, match="maintenance_active_operations"):
        maintenance.freeze(barrier[0], native_evidence={})
    release.set()
    await operation
    assert maintenance.status()["active_tickets"] == 0


@pytest.mark.asyncio
async def test_new_paid_attempt_cannot_inherit_old_job_after_drain(tmp_path):
    maintenance = control(tmp_path)
    api, ledger = client(tmp_path, lambda _: pytest.fail("No HTTP after drain"), maintenance=maintenance)
    with maintenance.admission("main", "worker"):
        maintenance.begin_barrier(owner_id="backup")
        with pytest.raises(ProviderError) as failure:
            await post(api)
        assert failure.value.code == "maintenance_blocked"
        with ledger.repository.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM billing_charges").fetchone()[0] == 0
    assert maintenance.status()["active_tickets"] == 0


@pytest.mark.asyncio
async def test_paid_admission_precedes_catalog_await_and_reservation(tmp_path):
    maintenance = control(tmp_path)
    pricing, release = asyncio.Event(), asyncio.Event()
    posts = []
    def handler(request):
        posts.append(request)
        assert maintenance.status()["mode"] == "draining"
        return httpx.Response(200, json={"id": "catalog-synthetic", "usage": {"cost_rub": "0.02"}})
    api, ledger = client(tmp_path, handler, maintenance=maintenance)
    class SlowPrices:
        async def quote(self, _):
            assert maintenance.status()["active_tickets"] == 1
            with ledger.repository.transaction() as conn:
                assert conn.execute("SELECT COUNT(*) FROM billing_charges").fetchone()[0] == 0
            pricing.set()
            await release.wait()
            return await Prices().quote(None)
    api.price_reader = SlowPrices()
    operation = asyncio.create_task(post(api))
    await asyncio.wait_for(pricing.wait(), 2)
    barrier = maintenance.begin_barrier(owner_id="backup")
    assert not posts
    with pytest.raises(MaintenanceError, match="maintenance_active_operations"):
        maintenance.freeze(barrier, native_evidence={})
    release.set()
    await operation
    assert len(posts) == 1 and maintenance.status()["active_tickets"] == 0


@pytest.mark.asyncio
async def test_direct_raw_paid_attempt_requires_new_open_admission(tmp_path):
    maintenance = control(tmp_path)
    api = PolzaClient(config(tmp_path), httpx.MockTransport(lambda _: pytest.fail("No raw POST after drain")),
        maintenance=maintenance, participant_id="main")
    with maintenance.admission("main", "worker"):
        maintenance.begin_barrier(owner_id="backup")
        with pytest.raises(ProviderError) as failure:
            await api._raw_request("POST", "chat/completions", payload={})
        assert failure.value.code == "maintenance_blocked"


@pytest.mark.asyncio
async def test_cancelled_post_retains_ticket_until_uncertain_commit(tmp_path):
    maintenance = control(tmp_path)
    entered = asyncio.Event()
    async def handler(_):
        entered.set()
        await asyncio.Event().wait()
    api, ledger = client(tmp_path, handler, maintenance=maintenance)
    original = ledger.mark_uncertain
    seen = []
    def committed(operation_id):
        seen.append(maintenance.status()["active_tickets"])
        original(operation_id)
    ledger.mark_uncertain = committed
    task = asyncio.create_task(post(api))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [1]
    assert maintenance.status()["active_tickets"] == 0
    with ledger.repository.transaction() as conn:
        assert conn.execute("SELECT status FROM billing_charges").fetchone()[0] == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["request", "raw", "voice", "factory"])
async def test_restored_authority_blocks_reconstructed_default_paid_paths(tmp_path, entry):
    ledger = budget(tmp_path)
    restore_guard(ledger.repository.path)
    transport = httpx.MockTransport(lambda _: pytest.fail("Restored authority must never dispatch"))
    if entry == "factory":
        account = SimpleNamespace(key_tag=KEY_TAG)
        api = build_polza_client(config(tmp_path), transport=transport, account_reader=account,
                                  price_reader=Prices(), clock=lambda: NOW)
    else:
        api = PolzaClient(config(tmp_path), transport)
    if entry == "voice":
        api = api._voice_client(str(uuid4()), "voice_intent")
    with pytest.raises(ProviderError) as failure:
        if entry == "raw":
            await api._raw_request("POST", "chat/completions", payload={})
        else:
            await post(api)
    assert failure.value.code == "maintenance_restore_blocked"
    with ledger.repository.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM billing_charges").fetchone()[0] == 0


def test_restore_guard_preserves_reservation_and_denies_unverified_settlement(tmp_path):
    ledger = budget(tmp_path)
    charge = CloudCharge(str(uuid4()), "meeting_stt", "a" * 64, 1_000000, 1_000000)
    ledger.reserve(charge)
    restore_guard(ledger.repository.path)
    with pytest.raises(BudgetError, match="maintenance_restore_blocked"):
        ledger.mark_submitted(charge.operation_id)
    with pytest.raises(BudgetError, match="maintenance_restore_blocked"):
        ledger.reserve(CloudCharge(str(uuid4()), "meeting_stt", "b" * 64, 1_000000, 1_000000))
    # A dictionary supplied to the ordinary ledger is not current provider
    # reconciliation. The inactive restored authority retains its liability;
    # a future controlled reconciliation port must verify real observations.
    with pytest.raises(BudgetError, match="maintenance_restore_blocked"):
        ledger.settle(charge.operation_id, {"confirmed_rub": "0.02", "provider_request_id": "reconciled-synthetic"})
    retained = ledger.reservation(charge.operation_id)
    assert retained.status == "reserved" and retained.confirmed_micro is None
    with sqlite3.connect(ledger.repository.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM maintenance_restore_guard")


def test_bound_billing_constructor_refuses_drain_before_database_creation(tmp_path):
    maintenance = control(tmp_path)
    maintenance.begin_barrier(owner_id="backup")
    path = tmp_path / "new-billing.sqlite3"
    with pytest.raises(MaintenanceError, match="maintenance_blocked"):
        BudgetRepository(path, maintenance=maintenance, participant_id="main")
    assert not path.exists()


@pytest.mark.asyncio
async def test_explicit_billing_path_override_preserves_restore_authority(tmp_path):
    path = tmp_path / "separate" / "ledger.db"
    ledger = budget(tmp_path, billing_path=path)
    restore_guard(path)
    api = build_polza_client(config(tmp_path), billing_path=path,
        account_reader=SimpleNamespace(key_tag=KEY_TAG), price_reader=Prices(), clock=lambda: NOW,
        transport=httpx.MockTransport(lambda _: pytest.fail("No restored override POST")))
    with pytest.raises(ProviderError, match="maintenance_restore_blocked"):
        await post(api)
    assert api.budget.repository.path == path
