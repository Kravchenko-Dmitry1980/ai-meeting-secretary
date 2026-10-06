"""Read-only public Polza prices. Quotes are estimates, never tariff ceilings."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json

import httpx

from secretary.domain.cloud_budget import BudgetError

_CATALOG_URL = "https://polza.ai/api/v1/models/catalog"
_PAGE_LIMIT = 50
_MAX_PAGES = 100
_MAX_BODY_BYTES = 2 * 1024 * 1024
_PRICE_FIELDS = (
    "stt_per_minute", "prompt_per_million", "completion_per_million",
    "internal_reasoning_per_million", "request_per_thousand", "per_request",
)
_ERROR = "monthly_budget_estimate_unavailable"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonfinite_json_number")


class PolzaPriceClient:
    def __init__(self, *, transport=None, clock=None):
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, tuple[datetime, dict[str, Decimal]]] = {}

    def _time(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BudgetError(_ERROR)
        return value

    @staticmethod
    async def _read_page(client, model: str, page: int, remaining_bytes: int) -> bytes:
        async with client.stream("GET", _CATALOG_URL,
                                 params={"search": model, "page": page, "limit": _PAGE_LIMIT},
                                 headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise BudgetError(_ERROR)
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                raise BudgetError(_ERROR)
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > remaining_bytes):
                raise BudgetError(_ERROR)
            if response.is_stream_consumed:
                if len(response.content) > remaining_bytes:
                    raise BudgetError(_ERROR)
                return response.content
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(chunk) > remaining_bytes - len(body):
                    raise BudgetError(_ERROR)
                body.extend(chunk)
            return bytes(body)

    @staticmethod
    def _page(body: bytes, requested_page: int):
        value = json.loads(body, parse_float=Decimal, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
        if not isinstance(value, dict) or not isinstance(value.get("data"), list):
            raise BudgetError(_ERROR)
        rows = value["data"]
        meta = value.get("meta")
        if not isinstance(meta, dict) or any(type(meta.get(field)) is not int
                                             for field in ("page", "limit", "total", "totalPages")):
            raise BudgetError(_ERROR)
        total, pages = meta["total"], meta["totalPages"]
        expected_pages = (total + _PAGE_LIMIT - 1) // _PAGE_LIMIT
        if (meta["page"] != requested_page or meta["limit"] != _PAGE_LIMIT or total < 1
                or pages != expected_pages or not 1 <= pages <= _MAX_PAGES
                or not 1 <= requested_page <= pages):
            raise BudgetError(_ERROR)
        expected_rows = min(_PAGE_LIMIT, total - (requested_page - 1) * _PAGE_LIMIT)
        if len(rows) != expected_rows:
            raise BudgetError(_ERROR)
        return rows, total, pages

    @staticmethod
    def _prices(row: dict) -> dict[str, Decimal]:
        provider = row.get("top_provider")
        pricing = provider.get("pricing") if isinstance(provider, dict) else None
        if not isinstance(pricing, dict) or pricing.get("currency") != "RUB":
            raise BudgetError(_ERROR)
        result = {}
        for field in _PRICE_FIELDS:
            if field not in pricing:
                continue
            value = pricing[field]
            if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
                raise BudgetError(_ERROR)
            number = Decimal(value)
            if not number.is_finite() or number < 0:
                raise BudgetError(_ERROR)
            result[field] = number
        has_prompt = "prompt_per_million" in result
        has_completion = "completion_per_million" in result
        if has_prompt != has_completion:
            raise BudgetError(_ERROR)
        if not any(field in result for field in (
                "stt_per_minute", "prompt_per_million", "request_per_thousand", "per_request")):
            raise BudgetError(_ERROR)
        return result

    async def quote(self, model: str) -> dict[str, Decimal]:
        try:
            if (not isinstance(model, str) or not 1 <= len(model) <= 256 or model.strip() != model
                    or any(ord(character) < 32 or ord(character) == 127 for character in model)):
                raise BudgetError(_ERROR)
            started = self._time()
            cached = self._cache.get(model)
            if cached is not None and 0 <= (started - cached[0]).total_seconds() <= 60:
                return dict(cached[1])
            remaining_bytes = _MAX_BODY_BYTES
            seen_ids = set()
            match = None
            expected = None
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(
                        transport=self.transport, verify=True, trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(10, connect=5, pool=5)) as client:
                    current = 1
                    while True:
                        body = await self._read_page(client, model, current, remaining_bytes)
                        remaining_bytes -= len(body)
                        rows, total, pages = self._page(body, current)
                        if expected is None:
                            expected = (total, pages)
                        elif expected != (total, pages):
                            raise BudgetError(_ERROR)
                        for row in rows:
                            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                                raise BudgetError(_ERROR)
                            identifier = row["id"]
                            if identifier in seen_ids:
                                raise BudgetError(_ERROR)
                            seen_ids.add(identifier)
                            if identifier == model:
                                match = row
                        if current == pages:
                            break
                        current += 1
            if match is None or len(seen_ids) != expected[0] or self._time() < started:
                raise BudgetError(_ERROR)
            prices = self._prices(match)
            # Age from the first fetch so pagination cannot extend the cache lifetime.
            self._cache[model] = (started, prices)
            return dict(prices)
        except BudgetError:
            raise
        except Exception:
            raise BudgetError(_ERROR) from None
