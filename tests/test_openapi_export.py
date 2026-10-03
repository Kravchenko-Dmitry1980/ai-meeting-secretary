"""Schema generation must never initialize the owner database or live adapters."""
import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path

import httpx
import pytest
from pydantic_settings.sources import DotEnvSettingsSource

from secretary.application.worker import Worker
from secretary.infrastructure.audio import AudioCapture


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
        path = Path(database).resolve()
        assert path.is_relative_to((root / ".runtime/openapi-export").resolve())
        connections.append(path)
        return original_connect(database, *args, **kwargs)

    original_create = exporter.create_app

    def guarded_create(settings, **kwargs):
        assert not settings.key_configured and not settings.cloud_enabled
        assert kwargs["run_worker"] is False
        assert isinstance(kwargs["capture"], exporter.NoopCapture)
        return original_create(settings, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    monkeypatch.setattr(exporter, "create_app", guarded_create)
    previous_tempdir = tempfile.tempdir
    target = tmp_path / "openapi.json"
    assert exporter.export_openapi(target) == target
    assert json.loads(target.read_text(encoding="utf-8"))["openapi"].startswith("3.")
    assert connections and all(not path.parent.exists() for path in connections)
    assert not (tmp_path / "forbidden-production").exists()
    assert tempfile.tempdir == previous_tempdir


def test_failed_export_restores_temp_directory_and_removes_scratch(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("failed_export", root / "scripts/export_openapi.py")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    original_create = exporter.create_app
    isolated_dirs = []

    def guarded_create(settings, **kwargs):
        isolated_dirs.append(settings.data_dir.parent)
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
