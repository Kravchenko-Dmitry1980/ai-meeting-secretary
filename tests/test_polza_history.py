"""Exact-ID history receipts using synthetic HTTP only; no live keys or calls."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
KEY = "pza_SYNTHETIC_HISTORY_NOT_A_CREDENTIAL"
GENERATION = "gen_synthetic_contract_1"


def client_type():
    assert (ROOT / "backend/secretary/infrastructure/polza_history.py").is_file(), "history boundary absent"
    return importlib.import_module("secretary.infrastructure.polza_history").PolzaHistoryClient


def settings(**updates):
    values = {"polza_api_key": SecretStr(f"  {KEY}  "), "polza_base_url": "https://polza.ai/api/v1"}
    return SimpleNamespace(**{**values, **updates})


def fixture(**updates):
    return {"id": GENERATION, "status": "completed", "clientCost": "0.1234567890123456789",
            "apiKeyId": "key_synthetic", "createdAt": "2026-09-30T21:59:00Z",
            "completedAt": "2026-09-30T22:00:01Z", **updates}


def client_for(response):
    return client_type()(settings(), transport=httpx.MockTransport(lambda request: response), clock=lambda: NOW)


async def test_completed_receipt_keeps_exact_rub_amount_and_does_not_guess_period_from_dates():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=fixture())
    client = client_type()(settings(), httpx.MockTransport(handler), lambda: NOW)
    receipt = await client.read_receipt(GENERATION)
    assert receipt == {"provider_request_id": GENERATION, "confirmed_rub": Decimal("0.1234567890123456789"),
                       "status": "completed", "provider_period": None}
    assert client.key_tag == hashlib.sha256(KEY.encode("utf-8")).hexdigest()
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "GET"
    assert str(request.url) == f"https://polza.ai/api/v1/history/generations/{GENERATION}"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert not request.url.query


async def test_failed_receipt_preserves_a_reported_nonzero_charge():
    receipt = await client_for(httpx.Response(200, json=fixture(status="failed", clientCost="0.5"))).read_receipt(GENERATION)
    assert receipt == {"provider_request_id": GENERATION, "confirmed_rub": Decimal("0.5"),
                       "status": "failed", "provider_period": None}


@pytest.mark.parametrize("reported", [{}, {"clientCost": "0"}, {"clientCost": "12.34"}])
async def test_pending_price_never_becomes_a_settled_amount(reported):
    body = {"id": GENERATION, "status": "pending", **reported}
    receipt = await client_for(httpx.Response(200, json=body)).read_receipt(GENERATION)
    assert receipt == {"provider_request_id": GENERATION, "confirmed_rub": None,
                       "status": "pending", "provider_period": None}


async def test_explicit_rub_currency_and_zero_cost_are_valid():
    receipt = await client_for(httpx.Response(200, json=fixture(clientCost="0", currency="RUB"))).read_receipt(GENERATION)
    assert receipt["confirmed_rub"] == Decimal("0")


async def test_identifier_is_one_quoted_path_segment_without_query_or_fragment():
    identifier = "gen_synthetic/segment?x#y%"
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=fixture(id=identifier))
    client = client_type()(settings(), transport=httpx.MockTransport(handler), clock=lambda: NOW)
    assert (await client.read_receipt(identifier))["provider_request_id"] == identifier
    assert calls[0].url.raw_path == b"/api/v1/history/generations/gen_synthetic%2Fsegment%3Fx%23y%25"
    assert not calls[0].url.query and not calls[0].url.fragment


@pytest.mark.parametrize("body", [
    {"status": "completed", "clientCost": "1"}, fixture(id="gen_changed"), fixture(id=None),
    fixture(id=1), fixture(status="processing"), fixture(status=None),
    {"id": GENERATION, "status": "completed"}, fixture(clientCost=None), fixture(clientCost=True),
    fixture(clientCost=0.15), fixture(clientCost={}), fixture(clientCost="-1"),
    fixture(clientCost="NaN"), fixture(clientCost="Infinity"), fixture(clientCost=""),
    fixture(currency="USD"), fixture(currency=None),
    {"data": fixture()}, fixture(status="pending", clientCost="NaN"),
])
async def test_invalid_identity_terminal_cost_currency_or_status_never_yields_a_receipt(body):
    from secretary.domain.cloud_budget import BudgetError
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, json=body)).read_receipt(GENERATION)
    assert str(failure.value) == "monthly_budget_receipt_unavailable"


@pytest.mark.parametrize("status", [302, 400, 401, 404, 429, 500])
async def test_http_failure_is_sanitized_and_is_not_retried_or_redirected(status):
    from secretary.domain.cloud_budget import BudgetError
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=f"private-fixture-body {KEY}",
                              headers={"Location": "https://unexpected.example.invalid"})
    with pytest.raises(BudgetError) as failure:
        await client_type()(settings(), transport=httpx.MockTransport(handler), clock=lambda: NOW).read_receipt(GENERATION)
    assert str(failure.value) == "monthly_budget_receipt_unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize("content", [
    b'{"id":"gen_synthetic_contract_1",',
    b'{"id":"gen_synthetic_contract_1","id":"gen_synthetic_contract_1","status":"completed","clientCost":"1"}',
    b'{"id":"gen_synthetic_contract_1","status":"completed","clientCost":NaN}',
])
async def test_partial_duplicate_or_nonfinite_json_is_refused(content):
    from secretary.domain.cloud_budget import BudgetError
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, content=content)).read_receipt(GENERATION)
    assert failure.value.code == "monthly_budget_receipt_unavailable"


class Chunks(httpx.AsyncByteStream):
    def __init__(self):
        self.read_count = 0

    async def __aiter__(self):
        for chunk in [b"x" * 32768, b"x" * 32768, b"x", b"never-read"]:
            self.read_count += 1
            yield chunk


async def test_stream_stops_at_sixty_four_kib_before_reading_its_tail():
    from secretary.domain.cloud_budget import BudgetError
    stream = Chunks()
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, stream=stream)).read_receipt(GENERATION)
    assert failure.value.code == "monthly_budget_receipt_unavailable"
    assert stream.read_count == 3


async def test_preloaded_body_is_also_bounded():
    from secretary.domain.cloud_budget import BudgetError
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, content=b"x" * 65537)).read_receipt(GENERATION)
    assert failure.value.code == "monthly_budget_receipt_unavailable"


async def test_network_exception_does_not_leak_provider_or_credential_text():
    from secretary.domain.cloud_budget import BudgetError
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(f"private-fixture-body {KEY}", request=request)
    with pytest.raises(BudgetError) as failure:
        await client_type()(settings(), transport=httpx.MockTransport(handler), clock=lambda: NOW).read_receipt(GENERATION)
    assert str(failure.value) == "monthly_budget_receipt_unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize("identifier", ["", ".", "..", " gen_synthetic", "gen_synthetic\n", 123, "x" * 257])
async def test_invalid_identifier_is_refused_before_any_http_call(identifier):
    from secretary.domain.cloud_budget import BudgetError
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=fixture())
    with pytest.raises(BudgetError) as failure:
        await client_type()(settings(), transport=httpx.MockTransport(handler), clock=lambda: NOW).read_receipt(identifier)
    assert failure.value.code == "monthly_budget_receipt_unavailable"
    assert calls == []


@pytest.mark.parametrize("updates", [
    {"polza_api_key": None}, {"polza_api_key": SecretStr("  ")},
    {"polza_api_key": SecretStr("synthetic\r\nkey")},
    {"polza_base_url": "https://unexpected.example.invalid/api/v1"},
    {"polza_base_url": "http://polza.ai/api/v1"},
])
async def test_bad_configuration_fails_without_sending_a_key(updates):
    from secretary.domain.cloud_budget import BudgetError
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=fixture())
    with pytest.raises(BudgetError) as failure:
        await client_type()(settings(**updates), transport=httpx.MockTransport(handler), clock=lambda: NOW).read_receipt(GENERATION)
    assert failure.value.code == "monthly_budget_configuration"
    assert calls == []
