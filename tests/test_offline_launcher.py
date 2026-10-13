from __future__ import annotations

import importlib.util
import logging
import logging.handlers
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest


def load_launcher():
    path = Path(__file__).parents[1] / "scripts" / "run_server.py"
    spec = importlib.util.spec_from_file_location("secretary_offline_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_offline_launcher_disables_outbound_before_serving(monkeypatch, tmp_path):
    launcher = load_launcher()
    calls = {}
    app = object()
    api = ModuleType("secretary.api")
    api.create_app = lambda **kwargs: calls.update(kwargs) or app
    monkeypatch.setitem(sys.modules, "secretary.api", api)

    class Server:
        should_exit = True

        async def serve(self, **kwargs):
            calls["served"] = True

    monkeypatch.setattr(launcher.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher.uvicorn, "Server", lambda config: Server())

    await launcher.run_owned(19876, object(), run_id=str(uuid4()), offline=True,
                             log_directory=tmp_path / "logs")

    assert calls == {"outbound_enabled": False, "served": True}


@pytest.mark.asyncio
async def test_offline_launcher_rejects_team_runtime_before_loading_it():
    launcher = load_launcher()

    with pytest.raises(ValueError, match="offline_mode_not_supported_with_team_runtime"):
        await launcher.run_owned(19876, object(), Path("synthetic-team.json"), str(uuid4()), offline=True)


@pytest.mark.asyncio
async def test_isolated_offline_launcher_does_not_load_project_env(monkeypatch, tmp_path):
    launcher = load_launcher()
    calls = []
    configuration = object()
    app = object()
    settings_module = ModuleType("secretary.settings")
    settings_module.Settings = lambda **kwargs: calls.append(("settings", kwargs)) or configuration
    api_module = ModuleType("secretary.api")
    api_module.create_app = lambda **kwargs: calls.append(("create_app", kwargs)) or app
    monkeypatch.setitem(sys.modules, "secretary.settings", settings_module)
    monkeypatch.setitem(sys.modules, "secretary.api", api_module)

    class Server:
        should_exit = True

        async def serve(self, **kwargs):
            calls.append(("served", kwargs))

    monkeypatch.setattr(launcher.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher.uvicorn, "Server", lambda config: Server())

    await launcher.run_owned(19876, object(), run_id=str(uuid4()), offline=True,
                             isolated_offline=True, log_directory=tmp_path / "logs")

    assert [kind for kind, _ in calls] == ["settings", "create_app", "served"]
    assert calls[0][1] == {"_env_file": None}
    assert calls[1][1] == {"settings": configuration, "outbound_enabled": False}


@pytest.mark.asyncio
async def test_isolated_offline_launcher_requires_offline_mode():
    launcher = load_launcher()

    with pytest.raises(ValueError, match="isolated_mode_requires_offline"):
        await launcher.run_owned(19876, object(), run_id=str(uuid4()), isolated_offline=True)


def test_windows_launcher_keeps_per_run_logs_instead_of_overwriting_shared_logs():
    script = (Path(__file__).parents[1] / "scripts" / "start.ps1").read_text(encoding="utf-8")

    assert "$StdoutLog = Join-Path $RuntimeDir ('server-' + $LaunchId + '.stdout.log')" in script
    assert "$StderrLog = Join-Path $RuntimeDir ('server-' + $LaunchId + '.stderr.log')" in script
    assert "-RedirectStandardOutput $StdoutLog" in script
    assert "-RedirectStandardError $StderrLog" in script
    assert "$LogDir = Join-Path $RuntimeDir 'logs'" in script
    assert "--log-directory \"' + $LogDir + '\"'" in script
    assert "stdout_log=$StdoutLog;stderr_log=$StderrLog" in script
    assert "Inspect $StderrLog." in script


def test_launcher_rotates_uvicorn_logs_with_bounded_retention(tmp_path):
    launcher = load_launcher()
    log_directory = tmp_path / "logs"
    log_directory.mkdir()

    config = launcher.build_log_config(log_directory, max_bytes=180, backup_count=2)

    assert config["handlers"]["error"]["class"] == "logging.handlers.RotatingFileHandler"
    assert config["handlers"]["access"]["class"] == "logging.handlers.RotatingFileHandler"
    assert config["handlers"]["error"]["maxBytes"] == 180
    assert config["handlers"]["error"]["backupCount"] == 2
    handler_config = config["handlers"]["access"]
    handler = logging.handlers.RotatingFileHandler(
        handler_config["filename"], maxBytes=handler_config["maxBytes"],
        backupCount=handler_config["backupCount"], encoding=handler_config["encoding"])
    handler.setFormatter(logging.Formatter(config["formatters"]["default"]["format"]))
    access_logger = logging.Logger("secretary_rotation_test", logging.INFO)
    access_logger.addHandler(handler)
    try:
        for index in range(20):
            access_logger.info("request-%02d %s", index, "x" * 30)
    finally:
        access_logger.removeHandler(handler)
        handler.close()

    generated = list(log_directory.glob("secretary.access.log*"))
    assert {path.name for path in generated} == {
        "secretary.access.log", "secretary.access.log.1", "secretary.access.log.2"
    }
    assert all(path.stat().st_size <= 180 for path in generated)


def test_launcher_rejects_log_directory_outside_runtime_root(tmp_path):
    launcher = load_launcher()

    with pytest.raises(ValueError, match="log_directory_outside_runtime"):
        launcher.resolve_log_directory(tmp_path / "project", tmp_path / "elsewhere" / "logs")
