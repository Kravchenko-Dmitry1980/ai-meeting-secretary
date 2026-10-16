"""Independent T6 import/launch surface and bounded public contract checks."""
import ast
import json
import subprocess
import sys
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gateway_import_does_not_load_secretary_runtime_or_cloud():
    source = """
import json, sys
import secretary.interface.team_gateway
for name in ('secretary.api', 'secretary.settings', 'secretary.application.worker',
             'secretary.application.enrollments', 'secretary.infrastructure.capture',
             'secretary.infrastructure.polza', 'secretary.infrastructure.database'):
    assert name not in sys.modules, name
print(json.dumps({'isolated': True}))
"""
    result = subprocess.run([sys.executable, '-B', '-c', source], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'isolated': True}


def test_existing_local_launcher_explicitly_disables_peer_rewriting():
    tree = ast.parse((ROOT / 'scripts/run_server.py').read_text(encoding='utf-8'))
    configs = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and node.func.attr == 'Config']
    assert len(configs) == 1
    proxy = [item.value for item in configs[0].keywords if item.arg == 'proxy_headers']
    assert len(proxy) == 1 and isinstance(proxy[0], ast.Constant) and proxy[0].value is False


def test_team_env_example_is_the_only_new_secret_config_exception():
    rules = (ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert '.env.*' in rules
    assert '!config/team/.env.team.example' in rules
    example = ROOT / 'config/team/.env.team.example'
    assert example.is_file()
    for line in example.read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            name, separator, value = line.partition('=')
            assert separator and name and not value.strip(), 'Example values must all be empty'


def test_public_schema_export_has_no_db_env_worker_or_network(tmp_path, monkeypatch):
    import sqlite3
    import socket
    from pydantic_settings.sources import DotEnvSettingsSource
    from secretary.interface.team_gateway import create_team_app, TeamGatewayClients, TeamGatewaySettings
    spec = importlib.util.spec_from_file_location('team_schema_export', ROOT / 'scripts/export_team_openapi.py')
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    def denied(*args, **kwargs):
        raise AssertionError('Team schema export touched runtime')
    monkeypatch.setattr(sqlite3, 'connect', denied)
    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(DotEnvSettingsSource, '_read_env_file', denied)
    target = tmp_path / 'team.json'
    assert exporter.export_team_openapi(target) == target
    schema = json.loads(target.read_text(encoding='utf-8'))
    actual = create_team_app(TeamGatewaySettings(public_origin='https://schema.example', bot_id='42',
        bot_token='synthetic-schema-token'), object(), TeamGatewayClients(auth=object())).openapi()
    assert schema == actual
    assert schema['components']['securitySchemes']['APIKeyCookie']['name'] == '__Host-secretary-team'
    assert len(schema['paths']) == 16
    assert set(schema['paths']['/hooks/max']) == {'post'}
    assert schema['paths']['/hooks/max']['post']['security'] == [{'APIKeyHeader': []}]


def test_saved_public_schema_matches_fresh_factory():
    from secretary.interface.team_gateway import create_team_app, TeamGatewayClients, TeamGatewaySettings
    app = create_team_app(TeamGatewaySettings(public_origin='https://schema.example', bot_id='42',
        bot_token='synthetic-schema-token'), object(), TeamGatewayClients(auth=object()))
    assert json.loads((ROOT / 'docs/contracts/team.openapi.json').read_text(encoding='utf-8')) == app.openapi()
