"""Account GET boundary tested offline with synthetic MockTransport only."""
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
SYNTHETIC_KEY = "pza_SYNTHETIC_NOT_A_CREDENTIAL"


def client_type():
    assert (ROOT / "backend/secretary/infrastructure/polza_account.py").is_file(), "account boundary absent"
    return importlib.import_module("secretary.infrastructure.polza_account").PolzaAccountClient


def fixture(**updates):
    return {"creator_user_id": None, "label": "SYNTHETIC_MASK", "limit": 1000,
            "limit_remaining": 900, "limit_reset": "monthly", "usage": 120,
            "usage_daily": 10, "usage_weekly": 40, "usage_monthly": 100, **updates}


def settings(**updates):
    return SimpleNamespace(polza_api_key=SecretStr(f"  {SYNTHETIC_KEY}  "),
                           polza_base_url="https://polza.ai/api/v1", **updates)


def client_for(response, *, clock=None):
    return client_type()(settings(), transport=httpx.MockTransport(lambda request: response),
                         clock=clock or (lambda: NOW))


async def test_flat_rub_response_uses_exact_stripped_identity_and_official_get():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=fixture(limit="1000.000000", limit_remaining="899.1234567",
                                              usage_monthly="100.8765433"))
    client = client_type()(settings(), transport=httpx.MockTransport(handler), clock=lambda: NOW)
    usage = await client.read_key_usage()
    assert usage.limit_micro == 1000_000000
    assert usage.remaining_micro == 899_123457
    assert usage.usage_micro == 100_876544
    assert usage.observed_at == NOW
    assert usage.reset == "monthly"
    assert usage.key_tag == client.key_tag == hashlib.sha256(SYNTHETIC_KEY.encode()).hexdigest()
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://polza.ai/api/v1/key"
    assert requests[0].headers["Authorization"] == f"Bearer {SYNTHETIC_KEY}"
    assert "authorization" not in requests[0].url.params


async def test_nullable_limits_and_zero_usage_are_available_not_invented_balances():
    usage = await client_for(httpx.Response(200, json=fixture(
        limit=None, limit_remaining=None, limit_reset=None, usage_monthly=0))).read_key_usage()
    assert usage.limit_micro is None and usage.remaining_micro is None
    assert usage.reset is None and usage.usage_micro == 0


@pytest.mark.parametrize("period", ["daily", "weekly", "monthly", "never"])
async def test_provider_reset_enum_is_preserved_for_the_budget_guard(period):
    usage = await client_for(httpx.Response(200, json=fixture(limit_reset=period))).read_key_usage()
    assert usage.reset == period


@pytest.mark.parametrize("field,value", [
    ("limit", True), ("limit", {}), ("limit_remaining", False),
    ("usage_monthly", None), ("usage_monthly", -1), ("usage_monthly", "NaN"),
    ("usage_monthly", "Infinity"), ("usage_monthly", []),
    ("limit_reset", "MONTHLY"), ("limit_reset", 1),
])
async def test_invalid_financial_schema_cannot_become_zero_or_a_valid_snapshot(field, value):
    from secretary.domain.cloud_budget import BudgetError
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, json=fixture(**{field: value}))).read_key_usage()
    assert failure.value.code == "monthly_budget_configuration"


@pytest.mark.parametrize("body", [{"data": fixture()}, {k: v for k, v in fixture().items() if k != "usage_monthly"}])
async def test_wrapped_or_missing_monthly_usage_is_rejected(body):
    from secretary.domain.cloud_budget import BudgetError
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, json=body)).read_key_usage()
    assert failure.value.code == "monthly_budget_configuration"


@pytest.mark.parametrize("status", [302, 400, 401, 402, 429, 500])
async def test_http_failure_does_not_follow_redirect_or_leak_sensitive_body(status):
    from secretary.domain.cloud_budget import BudgetError
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=SYNTHETIC_KEY + ": private-fixture-body",
                              headers={"Location": "https://untrusted.example.invalid/"})
    client = client_type()(settings(), transport=httpx.MockTransport(handler), clock=lambda: NOW)
    with pytest.raises(BudgetError) as failure:
        await client.read_key_usage()
    assert failure.value.code == "monthly_budget_account_unavailable"
    assert str(failure.value) == "monthly_budget_account_unavailable"
    assert len(calls) == 1


async def test_transport_timeout_is_sanitized_and_not_automatically_retried():
    from secretary.domain.cloud_budget import BudgetError
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(SYNTHETIC_KEY + ": private-fixture-body", request=request)
    client = client_type()(settings(), transport=httpx.MockTransport(handler), clock=lambda: NOW)
    with pytest.raises(BudgetError) as failure:
        await client.read_key_usage()
    assert failure.value.code == "monthly_budget_account_unavailable"
    assert str(failure.value) == "monthly_budget_account_unavailable"
    assert len(calls) == 1
    assert failure.value.__cause__ is None


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


async def test_stream_stops_after_size_boundary_before_reading_tail():
    from secretary.domain.cloud_budget import BudgetError
    stream = Chunks([b"x" * 32768, b"x" * 32768, b"x", b"never-read"])
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, stream=stream)).read_key_usage()
    assert failure.value.code == "monthly_budget_account_unavailable"
    assert stream.read_count == 3


@pytest.mark.parametrize("body", [b"not json", b'{"usage_monthly":1,"usage_monthly":2}', b'{"usage_monthly":NaN}'])
async def test_malformed_or_ambiguous_json_is_unavailable(body):
    from secretary.domain.cloud_budget import BudgetError
    with pytest.raises(BudgetError) as failure:
        await client_for(httpx.Response(200, content=body)).read_key_usage()
    assert failure.value.code == "monthly_budget_account_unavailable"


async def test_snapshot_crossing_month_reset_is_read_once_again_with_new_usage():
    times = iter([datetime(2026, 9, 30, 21, 59, 59, tzinfo=timezone.utc),
                  datetime(2026, 9, 30, 22, 0, 1, tzinfo=timezone.utc),
                  datetime(2026, 9, 30, 22, 0, 2, tzinfo=timezone.utc),
                  datetime(2026, 9, 30, 22, 0, 3, tzinfo=timezone.utc)])
    responses = iter([fixture(usage_monthly=100), fixture(usage_monthly=2)])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=next(responses))
    client = client_type()(settings(), transport=httpx.MockTransport(handler), clock=lambda: next(times))
    usage = await client.read_key_usage()
    assert usage.usage_micro == 2_000000
    assert usage.observed_at == datetime(2026, 9, 30, 22, 0, 3, tzinfo=timezone.utc)
    assert len(calls) == 2


async def test_repeated_boundary_crossing_fails_without_an_unbounded_retry():
    from secretary.domain.cloud_budget import BudgetError
    times = iter([datetime(2026, 9, 30, 21, 59, 59, tzinfo=timezone.utc),
                  datetime(2026, 9, 30, 22, 0, 1, tzinfo=timezone.utc),
                  datetime(2026, 10, 31, 21, 59, 59, tzinfo=timezone.utc),
                  datetime(2026, 10, 31, 22, 0, 1, tzinfo=timezone.utc)])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=fixture())
    client = client_type()(settings(), transport=httpx.MockTransport(handler), clock=lambda: next(times))
    with pytest.raises(BudgetError) as failure:
        await client.read_key_usage()
    assert failure.value.code == "monthly_budget_account_unavailable"
    assert len(calls) == 2


@pytest.mark.parametrize("base", ["http://polza.ai/api/v1", "https://evil.example.invalid/api/v1",
    "https://polza.ai@evil.example.invalid/api/v1", "https://polza.ai/api/v1?url=evil", "https://polza.ai/api/v2"])
async def test_nonofficial_base_is_rejected_before_any_key_can_be_sent(base):
    from secretary.domain.cloud_budget import BudgetError
    called = []
    config = SimpleNamespace(polza_api_key=SecretStr(SYNTHETIC_KEY), polza_base_url=base)
    with pytest.raises(BudgetError) as failure:
        client = client_type()(config, transport=httpx.MockTransport(lambda request: called.append(request)), clock=lambda: NOW)
        await client.read_key_usage()
    assert failure.value.code == "monthly_budget_configuration"
    assert called == []


async def test_money_decimals_do_not_round_through_binary_float():
    response = httpx.Response(200, content=b'{"limit":3000,"limit_remaining":0.00000100000000000000001,"limit_reset":"monthly","usage_monthly":0}')
    usage = await client_for(response).read_key_usage()
    assert usage.remaining_micro == 2
