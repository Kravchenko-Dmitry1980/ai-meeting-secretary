"""Synthetic public catalog HTTP boundaries; never call real HTTP by default."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
MODEL = "openai/whisper-large-v3-turbo"


def client_type():
    assert (ROOT / "backend/secretary/infrastructure/polza_prices.py").is_file(), "price boundary absent"
    return importlib.import_module("secretary.infrastructure.polza_prices").PolzaPriceClient


def model(identifier=MODEL, **pricing):
    return {"id": identifier, "top_provider": {"name": "synthetic-provider", "pricing": {
        "currency": "RUB", "stt_per_minute": "0.04800000", **pricing}}}


def page(rows, *, current=1, total=None, pages=1):
    return {"data": rows, "meta": {"page": current, "limit": 50,
            "total": len(rows) if total is None else total, "totalPages": pages}}


def client_for(response, *, clock=None):
    return client_type()(transport=httpx.MockTransport(lambda request: response), clock=clock or (lambda: NOW))


async def test_exact_catalog_quote_keeps_decimal_units_and_never_sends_authorization():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=page([model()]))
    client = client_type()(transport=httpx.MockTransport(handler), clock=lambda: NOW)
    quote = await client.quote(MODEL)
    assert quote == {"stt_per_minute": Decimal("0.04800000")}
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "GET"
    assert request.url.scheme == "https" and request.url.host == "polza.ai"
    assert request.url.path == "/api/v1/models/catalog"
    assert dict(request.url.params) == {"search": MODEL, "page": "1", "limit": "50"}
    assert "Authorization" not in request.headers


async def test_catalog_matches_exact_identifier_rather_than_first_search_result():
    quote = await client_for(httpx.Response(200, json=page([
        model("openai/whisper-large-v3", stt_per_minute="99"), model()]))).quote(MODEL)
    assert quote == {"stt_per_minute": Decimal("0.04800000")}


async def test_all_applicable_llm_and_fee_fields_preserve_rub_units():
    pricing = {"currency": "RUB", "prompt_per_million": "5.63682420",
               "completion_per_million": "22.59997740", "internal_reasoning_per_million": "30.12345678",
               "request_per_thousand": "1.25", "per_request": "0.0125"}
    row = {"id": "qwen/synthetic-model", "top_provider": {"pricing": pricing}}
    quote = await client_for(httpx.Response(200, json=page([row]))).quote("qwen/synthetic-model")
    assert quote == {"prompt_per_million": Decimal("5.63682420"),
                     "completion_per_million": Decimal("22.59997740"),
                     "internal_reasoning_per_million": Decimal("30.12345678"),
                     "request_per_thousand": Decimal("1.25"), "per_request": Decimal("0.0125")}


async def test_explicit_zero_price_is_preserved_without_inventing_missing_fees():
    quote = await client_for(httpx.Response(200, json=page([model(stt_per_minute="0")]))).quote(MODEL)
    assert quote == {"stt_per_minute": Decimal("0")}


async def test_quote_is_reused_only_within_sixty_seconds_and_returned_as_a_copy():
    clock = [NOW]
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=page([model(stt_per_minute="0.048" if len(calls) == 1 else "0.12")]))
    client = client_type()(transport=httpx.MockTransport(handler), clock=lambda: clock[0])
    first = await client.quote(MODEL)
    first["stt_per_minute"] = Decimal("999")
    clock[0] = NOW + timedelta(seconds=60)
    assert await client.quote(MODEL) == {"stt_per_minute": Decimal("0.048")}
    assert len(calls) == 1
    clock[0] = NOW + timedelta(seconds=61)
    assert await client.quote(MODEL) == {"stt_per_minute": Decimal("0.12")}
    assert len(calls) == 2


async def test_backwards_clock_cannot_extend_a_cached_quote():
    clock = [NOW]
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=page([model(stt_per_minute="0.048" if len(calls) == 1 else "0.12")]))
    client = client_type()(transport=httpx.MockTransport(handler), clock=lambda: clock[0])
    await client.quote(MODEL)
    clock[0] = NOW - timedelta(seconds=1)
    assert await client.quote(MODEL) == {"stt_per_minute": Decimal("0.12")}
    assert len(calls) == 2


async def test_pagination_follows_actual_meta_until_exact_model_is_found_and_verified_unique():
    calls = []
    first = [model(f"synthetic/other-{i}") for i in range(50)]
    def handler(request):
        calls.append(request)
        current = int(request.url.params["page"])
        return httpx.Response(200, json=page(first if current == 1 else [model()],
                                            current=current, total=51, pages=2))
    quote = await client_type()(transport=httpx.MockTransport(handler), clock=lambda: NOW).quote(MODEL)
    assert quote == {"stt_per_minute": Decimal("0.04800000")}
    assert [int(request.url.params["page"]) for request in calls] == [1, 2]


async def test_duplicate_exact_model_on_later_page_is_refused():
    from secretary.domain.cloud_budget import BudgetError
    calls = []
    first = [model()] + [model(f"synthetic/other-{i}") for i in range(49)]
    def handler(request):
        calls.append(request)
        current = int(request.url.params["page"])
        return httpx.Response(200, json=page(first if current == 1 else [model(stt_per_minute="0.12")],
                                            current=current, total=51, pages=2))
    client = client_type()(transport=httpx.MockTransport(handler), clock=lambda: NOW)
    with pytest.raises(BudgetError) as failure:
        await client.quote(MODEL)
    assert failure.value.code == "monthly_budget_estimate_unavailable"
    assert len(calls) == 2


@pytest.mark.parametrize("body", [
    {"items": [model()], "meta": {"page": 1, "limit": 50, "total": 1, "totalPages": 1}},
    {"data": [model()]}, page([]), page([model()], total=51, pages=1),
    page([model()], current=2), page([model(), model()]),
])
async def test_missing_inconsistent_or_duplicate_catalog_cannot_become_a_quote(body):
    from secretary.domain.cloud_budget import BudgetError
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, json=body)).quote(MODEL)
    assert failure.value.code == "monthly_budget_estimate_unavailable"


@pytest.mark.parametrize("pricing", [
    {"currency": "USD", "stt_per_minute": "0.048"}, {"currency": "RUB"},
    {"currency": "RUB", "stt_per_minute": None}, {"currency": "RUB", "stt_per_minute": True},
    {"currency": "RUB", "stt_per_minute": {}}, {"currency": "RUB", "stt_per_minute": "NaN"},
    {"currency": "RUB", "stt_per_minute": "Infinity"}, {"currency": "RUB", "stt_per_minute": "-1"},
    {"currency": "RUB", "prompt_per_million": "5.6"},
])
async def test_missing_or_invalid_applicable_prices_have_no_defaults(pricing):
    from secretary.domain.cloud_budget import BudgetError
    row = {"id": MODEL, "top_provider": {"pricing": pricing}}
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, json=page([row]))).quote(MODEL)
    assert failure.value.code == "monthly_budget_estimate_unavailable"


@pytest.mark.parametrize("status", [302, 429, 500])
async def test_http_failure_never_follows_redirect_or_returns_a_fake_quote(status):
    from secretary.domain.cloud_budget import BudgetError
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-fixture-provider-body",
                              headers={"Location": "https://unexpected.example.invalid"})
    with pytest.raises(BudgetError) as failure:
        await client_type()(transport=httpx.MockTransport(handler), clock=lambda: NOW).quote(MODEL)
    assert str(failure.value) == "monthly_budget_estimate_unavailable"
    assert len(calls) == 1


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


async def test_stream_is_bounded_before_reading_the_tail_after_two_mib():
    from secretary.domain.cloud_budget import BudgetError
    stream = Chunks([b"x" * 1048576, b"x" * 1048576, b"x", b"never-read"])
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, stream=stream)).quote(MODEL)
    assert failure.value.code == "monthly_budget_estimate_unavailable"
    assert stream.read_count == 3


@pytest.mark.parametrize("body", [b'{"data":[', b'{"data":[],"data":[]}', b'{"price":NaN}'])
async def test_partial_or_ambiguous_json_is_refused(body):
    from secretary.domain.cloud_budget import BudgetError
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, content=body)).quote(MODEL)
    assert failure.value.code == "monthly_budget_estimate_unavailable"


async def test_stale_cache_is_not_used_after_a_refresh_timeout():
    from secretary.domain.cloud_budget import BudgetError
    clock = [NOW]
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json=page([model()]))
        raise httpx.ReadTimeout("private-fixture-provider-body", request=request)
    client = client_type()(transport=httpx.MockTransport(handler), clock=lambda: clock[0])
    await client.quote(MODEL)
    clock[0] = NOW + timedelta(seconds=61)
    with pytest.raises(BudgetError) as failure:
        await client.quote(MODEL)
    assert str(failure.value) == "monthly_budget_estimate_unavailable"
    assert len(calls) == 2


async def test_decimal_numeric_prices_never_round_through_binary_float():
    content = b'{"data":[{"id":"openai/whisper-large-v3-turbo","top_provider":{"pricing":{"currency":"RUB","stt_per_minute":0.00000100000000000000001}}}],"meta":{"page":1,"limit":50,"total":1,"totalPages":1}}'
    quote = await client_for(httpx.Response(200, content=content)).quote(MODEL)
    assert quote == {"stt_per_minute": Decimal("0.00000100000000000000001")}
