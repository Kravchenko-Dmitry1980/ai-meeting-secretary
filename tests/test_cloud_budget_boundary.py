"""Every paid HTTP attempt passes the guard even with legacy limits disabled."""
import wave
import hashlib
from uuid import uuid4

import httpx
import pytest

from secretary.infrastructure.polza import PolzaClient, ProviderError
from secretary.settings import Settings
from test_monthly_budget import ledger

KEY_TAG = hashlib.sha256(b"synthetic-not-real").hexdigest()


def settings(tmp_path, **values):
    return Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
        polza_api_key="synthetic-not-real", local_cost_limits_enabled=False,
        stt_price_rub_per_minute=.1, **values)


def audio(tmp_path):
    path = tmp_path / "synthetic.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "none"))
        writer.writeframes(b"\0\0" * 16000)
    return path


@pytest.mark.asyncio
async def test_disabled_legacy_limits_cannot_bypass_monthly_guard(tmp_path):
    budget = ledger(tmp_path, cap=100_000000, usage=100_000000, key=KEY_TAG)
    calls = []
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(lambda r: calls.append(r)),
                         budget=budget, charge_context={"key_tag": KEY_TAG, "category": "meeting_stt"})
    with pytest.raises(ProviderError) as failure:
        await client.transcribe(audio(tmp_path))
    assert failure.value.code == "monthly_budget_exhausted"
    assert failure.value.uncertain is False
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_paid_result_is_still_charged(tmp_path):
    budget = ledger(tmp_path, key=KEY_TAG)
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(lambda r: httpx.Response(200,
        json={"id": "synthetic-paid", "text": 42, "usage": {"cost_rub": 2}})),
        budget=budget, charge_context={"key_tag": KEY_TAG, "category": "meeting_stt"})
    with pytest.raises(ProviderError):
        await client.transcribe(audio(tmp_path))
    assert budget.snapshot().confirmed_micro == 2_000000
    assert budget.snapshot().reserved_micro == 0


@pytest.mark.asyncio
async def test_timeout_never_replays_paid_post(tmp_path):
    budget = ledger(tmp_path, key=KEY_TAG)
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic-timeout", request=request)
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(timeout), budget=budget,
                         charge_context={"key_tag": KEY_TAG, "category": "meeting_stt"})
    with pytest.raises(ProviderError) as failure:
        await client.transcribe(audio(tmp_path))
    assert failure.value.uncertain is True
    assert len(calls) == 1
    assert budget.snapshot().reserved_micro > 0


@pytest.mark.asyncio
async def test_missing_paid_usage_retains_reservation(tmp_path):
    budget = ledger(tmp_path, key=KEY_TAG)
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(lambda r: httpx.Response(200,
        json={"id": "synthetic-unpriced", "text": "synthetic"})), budget=budget,
        charge_context={"key_tag": KEY_TAG, "category": "meeting_stt"})
    await client.transcribe(audio(tmp_path))
    assert budget.snapshot().confirmed_micro == 0
    assert budget.snapshot().reserved_micro > 0


@pytest.mark.asyncio
async def test_only_explicit_mock_transport_can_omit_guard(tmp_path):
    class UnapprovedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise AssertionError("dispatch must be blocked")
    client = PolzaClient(settings(tmp_path), UnapprovedTransport())
    with pytest.raises(ProviderError) as failure:
        await client.transcribe(audio(tmp_path))
    assert failure.value.code == "monthly_budget_required"
    assert failure.value.uncertain is False


@pytest.mark.asyncio
async def test_internal_raw_dispatch_cannot_accidentally_bypass_guard(tmp_path):
    class UnapprovedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise AssertionError("raw dispatch must be blocked")
    client = PolzaClient(settings(tmp_path), UnapprovedTransport())
    with pytest.raises(ProviderError) as failure:
        await client._raw_request("POST", "chat/completions", payload={"model": "synthetic"})
    assert failure.value.code == "monthly_budget_required"


@pytest.mark.asyncio
async def test_credential_cannot_be_booked_against_another_accounts_tag(tmp_path):
    budget = ledger(tmp_path, key="synthetic-wrong-account")
    calls = []
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(lambda request: calls.append(request)),
                        budget=budget, charge_context={"key_tag": "synthetic-wrong-account"})
    with pytest.raises(ProviderError) as failure:
        await client.transcribe(audio(tmp_path))
    assert failure.value.code == "monthly_budget_key_changed"
    assert calls == []


@pytest.mark.asyncio
async def test_decimal_usage_does_not_round_down_to_float(tmp_path):
    budget = ledger(tmp_path, key=KEY_TAG)
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(lambda request: httpx.Response(200,
        content=b'{"id":"synthetic-decimal","text":"test","usage":{"cost_rub":0.0000010000000000001}}')),
        budget=budget, charge_context={"key_tag": KEY_TAG})
    await client.transcribe(audio(tmp_path))
    assert budget.snapshot().confirmed_micro == 2


@pytest.mark.asyncio
async def test_async_job_mapping_survives_lost_caller_ack_and_poll_is_free(tmp_path):
    budget = ledger(tmp_path, key=KEY_TAG)
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gen_synthetic", "status": "processing",
                                             "usage": {"cost_rub": 8}})
        return httpx.Response(200, json={"id": "gen_synthetic", "status": "completed",
                                         "text": "synthetic", "usage": {"cost_rub": 8}})
    client = PolzaClient(settings(tmp_path, stt_model="aiesa/transcribe"), httpx.MockTransport(handler),
                         budget=budget, charge_context={"key_tag": KEY_TAG})
    def lost_ack(identifier):
        raise RuntimeError("synthetic lost local acknowledgement")
    with pytest.raises(ProviderError, match="ID Polza") as failure:
        await client.transcribe(audio(tmp_path), on_provider_job=lost_ack)
    assert failure.value.code == "receipt_persistence_failed"
    assert budget.operation_for_job("gen_synthetic")
    assert budget.snapshot().reserved_micro == 8_000000
    await client.transcribe(audio(tmp_path), provider_job_id="gen_synthetic")
    assert calls == ["POST", "GET"]
    assert budget.snapshot().confirmed_micro == 8_000000
    assert budget.snapshot().reserved_micro == 0


@pytest.mark.asyncio
async def test_worker_poll_failure_retains_reservations_and_resume_never_resubmits(tmp_path):
    from secretary.application.worker import Worker
    from secretary.infrastructure.database import Database
    from test_backend import prepared

    configuration = settings(tmp_path, stt_model="aiesa/transcribe").model_copy(update={
        "local_cost_limits_enabled": True, "meeting_budget_rub": 10,
    })
    budget = ledger(tmp_path, key=KEY_TAG)
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gen_poll_pending", "status": "processing",
                                             "usage": {"cost_rub": 8}})
        raise httpx.ReadTimeout("synthetic poll timeout", request=request)

    def provider_factory(configuration):
        return PolzaClient(configuration, httpx.MockTransport(handler), budget=budget,
                           charge_context={"key_tag": KEY_TAG})

    db = Database(configuration.data_dir / "secretary.sqlite3")
    meeting, _ = prepared(db, configuration)
    worker = Worker(db, configuration, provider_factory)
    job = worker.enqueue_transcription(meeting["id"])

    await worker.run_once()
    first = db.job(job["id"], internal=True)
    first_usage = db.rows("SELECT id,status,reserved_rub FROM usage WHERE job_id=?", (job["id"],))
    operation_id = budget.operation_for_job("gen_poll_pending")
    monthly_reservation = budget.reservation(operation_id)

    assert calls == ["POST", "GET"]
    assert first["provider_job_id"] == "gen_poll_pending" and first["status"] == "queued"
    assert len(first_usage) == 1 and first_usage[0]["status"] == "unknown"
    assert first_usage[0]["reserved_rub"] is not None
    assert monthly_reservation.status == "submitted"
    assert budget.snapshot().reserved_micro >= 8_000000

    await worker.run_once()
    second = db.job(job["id"], internal=True)
    second_usage = db.rows("SELECT id,status,reserved_rub FROM usage WHERE job_id=?", (job["id"],))

    assert calls == ["POST", "GET", "GET"]
    assert second["provider_job_id"] == "gen_poll_pending" and second["status"] == "queued"
    assert second_usage == first_usage
    assert budget.operation_for_job("gen_poll_pending") == operation_id
    assert budget.reservation(operation_id).status == "submitted"
    assert budget.snapshot().reserved_micro >= 8_000000


def test_production_factory_always_connects_same_monthly_repository(tmp_path):
    from secretary.infrastructure.polza import build_polza_client
    a = build_polza_client(settings(tmp_path))
    b = build_polza_client(settings(tmp_path))
    assert a.budget.repository.path == b.budget.repository.path == (tmp_path / "data/billing.sqlite3").resolve()
    assert a.charge_context["key_tag"] == b.charge_context["key_tag"] == KEY_TAG
    assert a.budget.account_reader is not None and a.price_reader is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [("monthly_budget_exhausted", "paused_budget"),
    ("monthly_budget_configuration", "waiting_config"), ("monthly_budget_account_unavailable", "paused_budget")])
async def test_worker_budget_refusal_is_not_a_paid_uncertain_failure(tmp_path, code, expected):
    from secretary.application.worker import Worker
    from secretary.infrastructure.database import Database
    from test_backend import FakeProvider, prepared
    class RefusingProvider(FakeProvider):
        async def transcribe(self, *args, **kwargs):
            raise ProviderError(code, "Synthetic pre-dispatch refusal", uncertain=False)
    configuration = settings(tmp_path)
    db = Database(configuration.data_dir / "secretary.sqlite3")
    meeting, _ = prepared(db, configuration)
    worker = Worker(db, configuration, RefusingProvider)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == expected
    assert db.one("SELECT status FROM usage WHERE job_id=?", (job["id"],))["status"] == "released"


@pytest.mark.asyncio
async def test_worker_and_benchmark_keep_meeting_and_monthly_guards_independent(tmp_path):
    import json
    from decimal import Decimal
    from secretary.api import create_app
    from secretary.domain.cloud_budget import AccountUsage
    from secretary.infrastructure.polza import build_polza_client
    from fastapi.testclient import TestClient
    from test_backend import FakeCapture, prepared
    from test_monthly_budget import NOW
    configuration = settings(tmp_path).model_copy(update={"local_cost_limits_enabled": True,
        "meeting_budget_rub": 100, "stt_price_rub_per_minute": .1})
    class Account:
        key_tag = KEY_TAG
        async def read_key_usage(self):
            return AccountUsage(KEY_TAG, 3000_000000, 3000_000000, 0, "monthly", NOW)
    class Prices:
        async def quote(self, model):
            return {"stt_per_minute": Decimal(".1")}
    payloads = []
    def paid(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"text": "synthetic", "usage": {"cost_rub": 2}})
    def factory(settings):
        return build_polza_client(settings, transport=httpx.MockTransport(paid), account_reader=Account(),
                                  price_reader=Prices(), clock=lambda: NOW)
    app = create_app(configuration, provider_factory=factory, capture=FakeCapture(), run_worker=False)
    app.state.cloud_budget.clock = lambda: NOW
    app.state.cloud_budget.account_reader = Account()
    with TestClient(app, base_url="http://127.0.0.1:8765") as api:
        headers = {"X-Secretary-Token": api.get("/api/v1/session").json()["csrf_token"]}
        assert api.post("/api/v1/cloud-budget/refresh", headers=headers).status_code == 200
        scope = api.post("/api/v1/cloud-budget/scopes", headers=headers,
            json={"operation_id": str(uuid4()), "cap_rub": 3}).json()["scope_id"]
        meeting, _ = prepared(app.state.db, configuration)
        app.state.cloud_budget.bind_meeting_scope(meeting["id"], scope)
        job = app.state.worker.enqueue_transcription(meeting["id"])
        await app.state.worker.run_once()
        assert app.state.db.job(job["id"])["status"] == "succeeded"
        state = api.get(f"/api/v1/cloud-budget/scopes/{scope}").json()
        assert state["confirmed_rub"] == 2 and state["reserved_rub"] == 0
        assert api.get("/api/v1/cloud-budget").json()["confirmed_rub"] == 2
    assert len(payloads) == 1
    assert payloads[0]["provider"]["max_price"] == {"stt_per_minute": .1}


@pytest.mark.asyncio
async def test_monthly_budget_does_not_disable_per_meeting_cap(tmp_path):
    from secretary.application.worker import Worker
    from secretary.infrastructure.database import Database
    from test_backend import FakeProvider, prepared

    configuration = settings(tmp_path).model_copy(update={
        "local_cost_limits_enabled": True,
        "meeting_budget_rub": 0,
    })
    budget = ledger(tmp_path, key=KEY_TAG)
    provider_calls = []

    class MonthlyBudgetProvider(FakeProvider):
        def __init__(self, provider_settings):
            super().__init__(provider_settings)
            self.budget = budget

        async def transcribe(self, path, *, offset_ms=0, channel="import"):
            provider_calls.append(path)
            return await super().transcribe(path, offset_ms=offset_ms, channel=channel)

    db = Database(configuration.data_dir / "secretary.sqlite3")
    meeting, _ = prepared(db, configuration)
    worker = Worker(db, configuration, MonthlyBudgetProvider)
    job = worker.enqueue_transcription(meeting["id"])

    await worker.run_once()

    assert db.job(job["id"])["status"] == "paused_budget"
    assert provider_calls == []
    assert db.one("SELECT COUNT(*) AS n FROM usage WHERE job_id=?", (job["id"],))["n"] == 0
    assert budget.snapshot().reserved_micro == 0


def test_monthly_budget_preserves_configured_polza_route_price_ceilings(tmp_path):
    configuration = settings(tmp_path).model_copy(update={
        "local_cost_limits_enabled": True,
        "stt_price_rub_per_minute": .10,
        "summary_input_rub_per_million": 10,
        "summary_output_rub_per_million": 40,
    })
    budget = ledger(tmp_path, key=KEY_TAG)
    client = PolzaClient(configuration, httpx.MockTransport(lambda request: httpx.Response(500)),
        budget=budget, charge_context={"key_tag": KEY_TAG, "category": "meeting_stt"})

    stt_payload = client._stt_payload(audio(tmp_path))
    summary_payload = client.summary_payload([{"id": "segment-1", "text": "Synthetic meeting text"}], merge=False)

    assert stt_payload["provider"]["max_price"] == {"stt_per_minute": .10}
    assert summary_payload["provider"]["max_price"] == {"prompt": 10, "completion": 40}


@pytest.mark.asyncio
async def test_async_http_rejection_with_cost_is_charged_and_never_auto_retried(tmp_path):
    budget = ledger(tmp_path, key=KEY_TAG)
    client = PolzaClient(settings(tmp_path, stt_model="aiesa/transcribe"),
        httpx.MockTransport(lambda request: httpx.Response(429,
            json={"id": "gen_billed_error", "error": {"code": "RATE_LIMITED"}, "usage": {"cost_rub": 5}})),
        budget=budget, charge_context={"key_tag": KEY_TAG})
    with pytest.raises(ProviderError) as failure:
        await client.transcribe(audio(tmp_path))
    assert budget.snapshot().confirmed_micro == 5_000000
    assert budget.snapshot().reserved_micro == 0
    assert failure.value.retryable is False
    assert failure.value.usage_records[0]["confirmed_rub"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [{"cost_rub": "invalid"}, {"cost_rub": 5, "currency": "USD"}])
async def test_rejection_with_ambiguous_money_cannot_trigger_paid_replay(tmp_path, usage):
    budget = ledger(tmp_path, key=KEY_TAG)
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(lambda request: httpx.Response(429,
        json={"error": {"code": "RATE_LIMITED"}, "usage": usage})), budget=budget,
        charge_context={"key_tag": KEY_TAG})
    with pytest.raises(ProviderError) as failure:
        await client.transcribe(audio(tmp_path))
    assert failure.value.uncertain is True
    assert failure.value.retryable is False
    assert budget.snapshot().reserved_micro > 0
    assert budget.snapshot().confirmed_micro == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [None, "x" * 257])
async def test_invalid_async_identity_cannot_hide_reported_cost(tmp_path, identifier):
    budget = ledger(tmp_path, key=KEY_TAG)
    body = {"status": "completed", "text": "synthetic", "usage": {"cost_rub": 8}}
    if identifier is not None:
        body["id"] = identifier
    client = PolzaClient(settings(tmp_path, stt_model="aiesa/transcribe"),
        httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        budget=budget, charge_context={"key_tag": KEY_TAG})
    with pytest.raises(ProviderError):
        await client.transcribe(audio(tmp_path))
    snapshot = budget.snapshot()
    assert snapshot.confirmed_micro + snapshot.reserved_micro >= 8_000000


@pytest.mark.asyncio
async def test_async_poll_cannot_settle_an_operation_of_another_key(tmp_path):
    from secretary.domain.cloud_budget import AccountUsage, CloudCharge
    from test_monthly_budget import NOW
    old_tag = hashlib.sha256(b"synthetic-old-key").hexdigest()
    budget = ledger(tmp_path, key=old_tag)
    operation = str(uuid4())
    budget.reserve(CloudCharge(operation, "meeting_stt", "a" * 64, 1_000000, 1_000000))
    budget.bind_provider_job(operation, "gen_previous_key")
    budget.mark_uncertain(operation)
    budget.refresh_account(AccountUsage(KEY_TAG, 3000_000000, 3000_000000, 0, "monthly", NOW))
    calls = []
    client = PolzaClient(settings(tmp_path, stt_model="aiesa/transcribe"),
        httpx.MockTransport(lambda request: calls.append(request)), budget=budget,
        charge_context={"key_tag": KEY_TAG})
    with pytest.raises(ProviderError) as failure:
        await client.transcribe(audio(tmp_path), provider_job_id="gen_previous_key")
    assert failure.value.code == "monthly_budget_key_changed"
    assert calls == []


@pytest.mark.asyncio
async def test_summary_maps_reduce_and_repair_each_have_one_monthly_charge(tmp_path):
    import json
    from test_polza import empty_summary
    budget = ledger(tmp_path, key=KEY_TAG)
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        result = empty_summary()
        result["overview"] = "x" * 3500
        if len(requests) == 1:
            result["decisions"] = [{"text": "invented", "source_segment_ids": ["missing"],
                                     "evidence_quote": "invented"}]
        return httpx.Response(200, json={"id": f"gen_summary_{len(requests)}", "usage": {"cost_rub": 1},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]})
    client = PolzaClient(settings(tmp_path, summary_batch_chars=5000,
        summary_input_rub_per_million=10, summary_output_rub_per_million=40), httpx.MockTransport(handler),
        budget=budget, charge_context={"key_tag": KEY_TAG, "category": "meeting_summary"})
    await client.summarize([{"id": "s1", "text": "Я" * 4000}, {"id": "s2", "text": "Б" * 4000}])
    assert any(len(payload["messages"]) > 2 for payload in requests)  # Evidence repair.
    assert any("merge_partial_protocols" in payload["messages"][1]["content"] for payload in requests)
    assert all("max_price" not in payload["provider"] for payload in requests)
    with budget.repository.transaction() as connection:
        rows = connection.execute("SELECT * FROM billing_charges").fetchall()
    assert len(rows) == len(requests) and len({row["operation_id"] for row in rows}) == len(requests)
    assert all(row["status"] == "confirmed" for row in rows)
    assert budget.snapshot().confirmed_micro == len(requests) * 1_000000


@pytest.mark.asyncio
async def test_factory_policy_refusal_keeps_app_job_in_configuration_state(tmp_path):
    from secretary.application.worker import Worker
    from secretary.domain.cloud_budget import BudgetError
    from secretary.infrastructure.database import Database
    from test_backend import prepared
    configuration = settings(tmp_path)
    db = Database(configuration.data_dir / "secretary.sqlite3")
    meeting, _ = prepared(db, configuration)
    def refuse(_settings):
        raise BudgetError("monthly_budget_policy_changed")
    worker = Worker(db, configuration, refuse)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == "waiting_config"
    assert db.rows("SELECT * FROM usage WHERE job_id=?", (job["id"],)) == []
