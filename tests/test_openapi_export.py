"""Schema generation must never initialize the owner database or live adapters."""
import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic_settings.sources import DotEnvSettingsSource

from secretary.api import create_app
from secretary.application.worker import Worker
from secretary.infrastructure.audio import AudioCapture
from secretary.settings import Settings
from sqlite_test_paths import sqlite_path


def test_frontend_missing_route_returns_documented_503(tmp_path):
    class NoopCapture:
        def close(self):
            pass

    previous_tempdir = tempfile.tempdir
    try:
        settings = Settings(_env_file=None, project_dir=tmp_path,
            data_dir=tmp_path / 'data', polza_api_key='', cloud_enabled=False)
        app = create_app(settings, capture=NoopCapture(),
            enrollment_capture=NoopCapture(), run_worker=False,
            enforce_single_instance=False, outbound_enabled=False)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            response = client.get('/')
            assert response.status_code == 503
            assert response.json() == {
                'status': 'frontend_build_required',
                'message': 'Build frontend with npm run build',
            }
            fallback = client.get('/openapi.json').json()['paths']['/']['get']['responses']
            assert '503' in fallback and '200' not in fallback
    finally:
        tempfile.tempdir = previous_tempdir


def test_export_isolates_database_env_devices_network_and_worker(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("isolated_export", root / "scripts/export_openapi.py")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)

    def denied(*args, **kwargs):
        raise AssertionError("OpenAPI export touched a live dependency")

    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", denied)
    monkeypatch.setattr(AudioCapture, "__init__", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)
    monkeypatch.setattr(Worker, "start", denied)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "forbidden-production"))
    monkeypatch.setenv("POLZA_API_KEY", "synthetic-export-env-only")
    monkeypatch.setenv("CLOUD_ENABLED", "true")
    original_connect = sqlite3.connect
    connections = []

    def guarded_connect(database, *args, **kwargs):
        path = sqlite_path(database, uri=kwargs.get('uri', False))
        assert path.is_relative_to((root / ".runtime/openapi-export").resolve())
        connections.append(path)
        return original_connect(database, *args, **kwargs)

    original_create = exporter.create_app

    def guarded_create(settings, **kwargs):
        assert not settings.key_configured and not settings.cloud_enabled
        assert kwargs["run_worker"] is False
        assert kwargs["enforce_single_instance"] is False
        assert isinstance(kwargs["capture"], exporter.NoopCapture)
        return original_create(settings, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    monkeypatch.setattr(exporter, "create_app", guarded_create)
    previous_tempdir = tempfile.tempdir
    target = tmp_path / "openapi.json"
    assert exporter.export_openapi(target) == target
    schema = json.loads(target.read_text(encoding="utf-8"))
    assert schema["openapi"].startswith("3.")
    root_responses = schema["paths"]["/"]["get"]["responses"]
    assert "503" in root_responses
    assert "200" not in root_responses
    assert root_responses["503"]["description"] == "Frontend build required"
    assert root_responses["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/FrontendBuildRequiredResponse"
    }
    assert connections and all(not path.parent.exists() for path in connections)
    assert not (tmp_path / "forbidden-production").exists()
    assert tempfile.tempdir == previous_tempdir
    forbidden = tmp_path / 'forbidden-production.sqlite3'
    with pytest.raises(AssertionError):
        sqlite3.connect(forbidden.as_uri() + '?mode=ro', uri=True)
    assert not forbidden.exists()


def test_failed_export_restores_temp_directory_and_removes_scratch(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("failed_export", root / "scripts/export_openapi.py")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    original_create = exporter.create_app
    isolated_dirs = []

    def guarded_create(settings, **kwargs):
        isolated_dirs.append(settings.data_dir.parent)
        assert kwargs["enforce_single_instance"] is False
        app = original_create(settings, **kwargs)
        def failed_schema():
            raise RuntimeError("synthetic schema failure")
        app.openapi = failed_schema
        return app

    monkeypatch.setattr(exporter, "create_app", guarded_create)
    previous_tempdir = tempfile.tempdir
    target = tmp_path / "failed.json"
    with pytest.raises(RuntimeError, match="synthetic schema failure"):
        exporter.export_openapi(target)
    assert tempfile.tempdir == previous_tempdir
    assert isolated_dirs and all(not path.exists() for path in isolated_dirs)
    assert not target.exists()
