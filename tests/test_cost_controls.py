"""Offline contracts for optional local spend limits; provider receipts stay durable."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.application.worker import Worker
from secretary.infrastructure.database import Database
from secretary.infrastructure.polza import PolzaClient
from secretary.settings import Settings
from test_backend import FakeCapture, FakeProvider, prepared


def config(tmp_path, **overrides):
    return Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
                    polza_api_key="test-cost-controls-not-real", cloud_enabled=True,
                    **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize("known_prices", [False, True])
async def test_disabled_limits_allow_worker_and_keep_receipts(tmp_path, known_prices):
    prices = {"stt_price_rub_per_minute": 50, "summary_input_rub_per_million": 100,
              "summary_output_rub_per_million": 200} if known_prices else {}
    settings = config(tmp_path, local_cost_limits_enabled=False, meeting_budget_rub=0,
                      allow_unknown_price=False, unknown_request_reservation_rub=None, **prices)
    db = Database(settings.data_dir / "test.sqlite3")
    meeting, _ = prepared(db, settings)
    worker = Worker(db, settings, FakeProvider)
    job = worker.enqueue_transcription(meeting["id"])
    assert "local_cost_limits_enabled" not in json.loads(db.job(job["id"], internal=True)["payload"])["settings"]
    assert await worker.run_once()
    assert db.job(job["id"])["status"] == "succeeded"
    assert await worker.run_once()
    assert db.summary(meeting["id"]) is not None
    receipts = db.rows("SELECT * FROM usage ORDER BY created_at")
    assert len(receipts) == 2
    assert all(row["status"] == "confirmed" for row in receipts)
    assert sum(row["confirmed_rub"] for row in receipts) == pytest.approx(.02)
    assert {row["provider_request_id"] for row in receipts} == {"test-stt", "test-summary"}
    if not known_prices:
        assert all(row["estimated_rub"] is None and row["reserved_rub"] is None for row in receipts)


@pytest.mark.asyncio
async def test_disabled_limits_cover_summary_provider_without_callbacks(tmp_path):
    class LegacyProvider(FakeProvider):
        async def summarize(self, segments):
            return await super().summarize(segments)

    settings = config(tmp_path, local_cost_limits_enabled=False, meeting_budget_rub=0)
    db = Database(settings.data_dir / "test.sqlite3")
    meeting, _ = prepared(db, settings)
    worker = Worker(db, settings, LegacyProvider)
    worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    await worker.run_once()
    assert db.summary(meeting["id"]) is not None
    assert len(db.rows("SELECT id FROM usage WHERE status='confirmed'")) == 2


def test_disabled_database_limits_preserve_unknown_ledger_entries(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    meeting = db.create_meeting("Synthetic")
    job = db.enqueue(meeting["id"], "transcribe")
    first = db.reserve(job, None, 0, False, enforce_limits=False)
    db.settle(first)
    second = db.reserve(job, 500, 0, False, enforce_limits=False)
    db.settle(second, {"confirmed_rub": 2, "provider_request_id": "mock-receipt"})
    assert db.one("SELECT status FROM usage WHERE id=?", (first,))["status"] == "unknown"
    assert db.one("SELECT confirmed_rub FROM usage WHERE id=?", (second,))["confirmed_rub"] == 2
    with pytest.raises(ValueError, match="Unknown model price"):
        db.reserve(job, None, 100, False)


@pytest.mark.asyncio
async def test_disabled_limits_omit_all_provider_price_filters(tmp_path):
    settings = config(tmp_path, local_cost_limits_enabled=False, stt_price_rub_per_minute=.1,
                      summary_input_rub_per_million=10, summary_output_rub_per_million=40)
    db = Database(settings.data_dir / "test.sqlite3")
    _, chunk = prepared(db, settings)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if request.url.path.endswith("transcriptions"):
            return httpx.Response(200, json={"text": "Текст", "usage": {"cost_rub": .02}})
        result = {"overview": "Текст", "readable_transcript": "Текст", "decisions": [],
                  "action_items": [], "open_questions": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)},
                                                     "finish_reason": "stop"}], "usage": {"cost_rub": .03}})

    provider = PolzaClient(settings, httpx.MockTransport(handler))
    assert (await provider.transcribe(chunk["path"]))["usage"]["confirmed_rub"] == .02
    await provider.summarize([{"id": "s1", "text": "Текст"}])
    assert len(requests) == 2
    assert all("max_price" not in payload["provider"] for payload in requests)
    assert all(payload["provider"]["allow_fallbacks"] is False for payload in requests)


def test_cost_control_flag_api_roundtrip_and_old_config_default(tmp_path):
    settings = config(tmp_path)
    assert settings.local_cost_limits_enabled is True
    db = Database(settings.data_dir / "secretary.sqlite3")
    db.set_configuration({"meeting_budget_rub": 1})  # older persisted configuration has no flag
    app = create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as api:
        assert api.get("/api/v1/config").json()["local_cost_limits_enabled"] is True
        token = {"X-Secretary-Token": api.get("/api/v1/session").json()["csrf_token"]}
        response = api.patch("/api/v1/config", headers=token, json={"local_cost_limits_enabled": False})
        assert response.status_code == 200
        assert response.json()["local_cost_limits_enabled"] is False
        assert db.configuration()["local_cost_limits_enabled"] is False
        # Existing clients can edit unrelated fields without resetting this choice.
        assert api.patch("/api/v1/config", headers=token, json={"chunk_seconds": 60}).json()["local_cost_limits_enabled"] is False
    reloaded = create_app(config(tmp_path), provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)
    with TestClient(reloaded, base_url="http://127.0.0.1:8765") as api:
        assert api.get("/api/v1/config").json()["local_cost_limits_enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [None, 10])
async def test_enabled_limits_still_pause_worker(tmp_path, price):
    settings = config(tmp_path, meeting_budget_rub=0, stt_price_rub_per_minute=price)
    db = Database(settings.data_dir / "test.sqlite3")
    meeting, _ = prepared(db, settings)
    worker = Worker(db, settings, FakeProvider)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == "paused_budget"
    assert db.rows("SELECT * FROM usage") == []
