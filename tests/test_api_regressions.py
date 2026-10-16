"""Offline regressions for configuration, missing audio, and published API responses."""
from __future__ import annotations

import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.settings import Settings


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in API regressions")
    async def denied_async(*args, **kwargs):
        denied()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied_async)
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
                        polza_api_key="synthetic-api-regression-only", cloud_enabled=False)
    app = create_app(settings, capture=SimpleNamespace(close=lambda: None),
                     provider_factory=denied, run_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as api:
        headers = {"X-Secretary-Token": api.get("/api/v1/session").json()["csrf_token"]}
        yield SimpleNamespace(app=app, api=api, db=app.state.db, settings=settings, headers=headers)


@pytest.mark.parametrize("field", ["stt_price_rub_per_minute", "summary_input_rub_per_million", "summary_output_rub_per_million"])
@pytest.mark.parametrize("value", ["Infinity", "1e309", "NaN"])
def test_nonfinite_price_rejection_is_atomic(app_client, field, value):
    ctx = app_client
    accepted = ctx.api.patch("/api/v1/config", headers=ctx.headers, json={field: 0.25})
    assert accepted.status_code == 200
    before = ctx.api.get("/api/v1/config").json()
    stored = ctx.db.configuration()
    rejected = ctx.api.patch("/api/v1/config", headers=ctx.headers,
                            json={field: value, "summary_max_output_tokens": 1024})
    assert rejected.status_code == 422
    assert ctx.api.get("/api/v1/config").json() == before
    assert ctx.db.configuration() == stored


@pytest.mark.parametrize("field", ["stt_model", "summary_model"])
@pytest.mark.parametrize("value", ["", " \t ", "\r\n", None])
def test_empty_or_whitespace_model_patch_rejected_atomically(app_client, field, value):
    ctx = app_client
    before = ctx.api.get("/api/v1/config").json()
    response = ctx.api.patch("/api/v1/config", headers=ctx.headers,
                             json={field: value, "chunk_seconds": 30})
    assert response.status_code == 422
    assert ctx.api.get("/api/v1/config").json() == before
    assert ctx.db.configuration() == {}


def test_unconfigured_startup_allows_blank_models_without_key(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
                        polza_api_key="", stt_model="", summary_model="", cloud_enabled=False)
    app = create_app(settings, capture=SimpleNamespace(close=lambda: None), run_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as api:
        assert api.get("/health").status_code == 200
        headers = {"X-Secretary-Token": api.get("/api/v1/session").json()["csrf_token"]}
        assert api.patch("/api/v1/config", headers=headers, json={"chunk_seconds": 60}).status_code == 200
        repaired = api.patch("/api/v1/config", headers=headers, json={"stt_model": "openai/whisper-large-v3-turbo"})
        assert repaired.status_code == 200 and repaired.json()["key_configured"] is False


def test_config_rejects_read_only_roundtrip_with_actionable_message_atomically(app_client):
    ctx = app_client
    before = ctx.api.get("/api/v1/config").json()
    # Reproduce the settings form that stripped key_configured but returned
    # max_upload_bytes alongside the user's edited setting.
    payload = {key: value for key, value in before.items() if key != "key_configured"}
    payload["chunk_seconds"] = 30
    response = ctx.api.patch("/api/v1/config", headers=ctx.headers, json=payload)
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Изменять можно только параметры обработки. Обновите страницу и повторите сохранение."
    )
    assert ctx.api.get("/api/v1/config").json() == before
    assert ctx.db.configuration() == {}


@pytest.mark.parametrize("outbound_enabled", [True, False])
@pytest.mark.parametrize("cloud_enabled", [True, False])
def test_runtime_outbound_flag_is_distinct_from_saved_cloud_setting_and_read_only(
        tmp_path, monkeypatch, outbound_enabled, cloud_enabled):
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
                        polza_api_key="synthetic-offline-flag-only", cloud_enabled=cloud_enabled)
    app = create_app(settings, capture=SimpleNamespace(close=lambda: None), run_worker=False,
                     outbound_enabled=outbound_enabled)
    with TestClient(app, base_url="http://127.0.0.1:8765") as api:
        headers = {"X-Secretary-Token": api.get("/api/v1/session").json()["csrf_token"]}
        before = api.get("/api/v1/config").json()
        assert before["key_configured"] is True
        assert before["runtime_outbound_enabled"] is outbound_enabled
        assert before["cloud_enabled"] is (cloud_enabled and outbound_enabled)
        assert "synthetic-offline-flag-only" not in str(before)
        rejected = api.patch("/api/v1/config", headers=headers,
                             json={"runtime_outbound_enabled": not outbound_enabled, "chunk_seconds": 30})
        assert rejected.status_code == 422
        assert api.get("/api/v1/config").json() == before
        assert app.state.db.configuration() == {}
        updated = api.patch("/api/v1/config", headers=headers, json={"chunk_seconds": 60})
        assert updated.status_code == 200
        assert updated.json()["runtime_outbound_enabled"] is outbound_enabled
        assert app.state.db.configuration() == {"chunk_seconds": 60}
        if not outbound_enabled:
            denied = api.patch("/api/v1/config", headers=headers, json={"cloud_enabled": True})
            assert denied.status_code == 409
            assert denied.json()["detail"] == "runtime_outbound_disabled"
            assert api.get("/api/v1/config").json()["cloud_enabled"] is False


def audio_fixture(ctx, count):
    meeting = ctx.db.create_meeting("Synthetic missing source")
    folder = ctx.settings.data_dir / "audio" / meeting["id"]
    folder.mkdir(parents=True)
    paths = []
    for sequence in range(count):
        path = folder / f"{sequence}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 1600)
        ctx.db.add_chunk(meeting["id"], {"path": str(path), "sequence": sequence,
            "channel": "import", "offset_ms": sequence * 100, "duration_ms": 100,
            "sha256": f"synthetic-{sequence}"})
        paths.append(path)
    return meeting["id"], paths


def assert_missing_audio(response):
    assert response.status_code == 404
    assert "не найден" in response.json()["detail"].lower()
    assert "восстанов" in response.json()["detail"].lower()


@pytest.mark.parametrize("count,missing,cached", [(1, 0, False), (2, 0, False), (2, 1, False), (2, 1, True)])
def test_missing_audio_source_has_actionable_404_without_losing_metadata(app_client, count, missing, cached):
    ctx = app_client
    meeting_id, paths = audio_fixture(ctx, count)
    url = f"/api/v1/meetings/{meeting_id}/audio"
    if cached:
        assert ctx.api.get(url).status_code == 200
    saved_chunks = ctx.db.chunks(meeting_id)
    paths[missing].unlink()
    response = ctx.api.get(url)
    assert_missing_audio(response)
    assert str(paths[missing]) not in response.text
    assert ctx.db.chunks(meeting_id) == saved_chunks


def test_audio_disappearing_during_assembly_has_actionable_404(app_client, monkeypatch):
    ctx = app_client
    meeting_id, paths = audio_fixture(ctx, 2)
    original_open = wave.open
    def disappearing_open(path, mode):
        if mode == "rb" and Path(path) == paths[1]:
            paths[1].unlink(missing_ok=True)
        return original_open(path, mode)
    monkeypatch.setattr(wave, "open", disappearing_open)
    assert_missing_audio(ctx.api.get(f"/api/v1/meetings/{meeting_id}/audio"))
    assert paths[0].is_file()
    assert not list(paths[0].parent.glob("*.partial"))


def test_openapi_distinguishes_ready_pending_and_media(app_client):
    paths = app_client.app.openapi()["paths"]
    for name in ("summary", "tasks"):
        responses = paths[f"/api/v1/meetings/{{meeting_id}}/{name}"]["get"]["responses"]
        assert responses["202"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/StatusResponse"}
        assert "StatusResponse" not in str(responses["200"])
    for name, media in (("events", "text/event-stream"), ("audio", "audio/wav")):
        responses = paths[f"/api/v1/meetings/{{meeting_id}}/{name}"]["get"]["responses"]
        assert set(responses["200"]["content"]) == {media}
    audio = paths["/api/v1/meetings/{meeting_id}/audio"]["get"]["responses"]
    assert "audio/wav" in audio["206"]["content"]
    assert "404" in audio and "416" in audio and "507" in audio
    assert "507" in paths["/api/v1/meetings/{meeting_id}/upload"]["post"]["responses"]
    assert "507" in paths["/api/v1/meetings/{meeting_id}/recording/start"]["post"]["responses"]
    assert "507" in paths["/api/v1/participants/{person_id}/enrollments"]["post"]["responses"]
    assert "507" in paths["/api/v1/participants/{person_id}/enrollment-recording/start"]["post"]["responses"]
    exported = paths["/api/v1/meetings/{meeting_id}/export"]["get"]["responses"]["200"]["content"]
    assert set(exported) == {"text/plain", "text/markdown", "application/json",
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def test_openapi_documents_multipart_and_invalid_ranges(app_client):
    ctx = app_client
    meeting_id, _ = audio_fixture(ctx, 1)
    url = f"/api/v1/meetings/{meeting_id}/audio"
    ranges = ctx.api.get(url, headers={"Range": "bytes=0-3,12-15"})
    assert ranges.status_code == 206
    assert ranges.headers["content-type"].startswith("multipart/byteranges;")
    documented = ctx.app.openapi()["paths"]["/api/v1/meetings/{meeting_id}/audio"]["get"]["responses"]
    assert "multipart/byteranges" in documented["206"]["content"]
    for range_header, code in (("invalid", 400), ("bytes=999999-", 416)):
        response = ctx.api.get(url, headers={"Range": range_header})
        assert response.status_code == code
        assert response.headers["content-type"].startswith("text/plain")
        assert "text/plain" in documented[str(code)]["content"]
