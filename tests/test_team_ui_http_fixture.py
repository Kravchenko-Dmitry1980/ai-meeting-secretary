"""T9 local HTTP fallback: synthetic UI only, no TLS or production proof."""
import importlib.util
from pathlib import Path
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/team/probe_team_ui_http.py'
    assert path.is_file(), 'HTTP synthetic Team fixture adapter is missing'
    spec = importlib.util.spec_from_file_location('t9_http_fixture', path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def case(tmp_path):
    adapter = module()
    run = tmp_path / ('t9-http-ui-' + uuid4().hex)
    run.mkdir()
    (run / 'lifecycle.log').write_text('', encoding='utf-8')
    dist = tmp_path / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'team.html').write_text('<html>synthetic Team <script src="./assets/team.js"></script></html>', encoding='utf-8')
    (dist / 'assets/team.js').write_text('/* synthetic built Team UI */', encoding='utf-8')
    http_origin = 'http://secretary-t9.localhost:44443'
    fixture, app = adapter.build_http_fixture(run, frontend_dist=dist, http_origin=http_origin)
    try:
        with TestClient(app, base_url=http_origin, client=('127.0.0.1', 45001)) as client:
            yield adapter, fixture, client, http_origin
    finally:
        fixture.close()


def test_local_http_fixture_runs_real_gateway_and_preserves_secure_cookie(case):
    _, fixture, client, origin = case
    status = client.get('/fixture/status')
    assert status.status_code == 200
    assert status.json() == {
        'simulation': True,
        'run_id': fixture.run_root.name,
        'gateway': 'real',
        'provider': 'httpx.MockTransport',
        'mode': 'HTTP_SYNTHETIC_UI_ONLY',
        'tls_qualification': 'not_tested',
    }
    page = client.get('/fixture')
    assert page.status_code == 200
    assert 'HTTP_SYNTHETIC_UI_ONLY' in page.text
    assert 'TLS и MAX WebView не проверяются' in page.text
    response = client.post('/api/team/v1/session/code', json={'value': fixture.codes[0]},
        headers={'Origin': origin})
    assert response.status_code == 200
    cookie = response.headers['set-cookie'].lower()
    assert 'secure' in cookie and 'httponly' in cookie and 'samesite=lax' in cookie
    # httpx intentionally does not send Secure cookies over HTTP, even for
    # localhost. Exercise the real Gateway session with the exact issued pair;
    # the actual browser will separately decide whether its localhost exception
    # permits the Secure cookie.
    cookie_pair = response.headers['set-cookie'].split(';', 1)[0]
    me = client.get('/api/team/v1/me', headers={'Cookie': cookie_pair})
    assert me.status_code == 200 and me.json()['actor']['id'] == fixture.owners[0].id
    assert all(request.url.host == '127.0.0.1' for request in fixture.provider.requests)
    assert fixture.provider.mutations == []


def test_http_fixture_rejects_foreign_origin_duplicate_origin_and_missing_mutation_origin(case):
    _, fixture, client, _ = case
    code = fixture.codes[0]
    for headers in (
        {'Origin': 'http://attacker.example'},
        [("Origin", 'http://secretary-t9.localhost:44443'),
         ("Origin", 'http://secretary-t9.localhost:44443')],
        {},
    ):
        response = client.post('/api/team/v1/session/code', json={'value': code}, headers=headers)
        assert response.status_code == 403
    assert client.get('/api/team/v1/me').status_code == 401


def test_http_fixture_rejects_wrong_host_forbidden_routes_and_https_scope(case):
    adapter, _, client, _ = case
    assert client.get('/fixture', headers={'Host': 'attacker.example'}).status_code == 403
    assert client.get('/').status_code == 404
    assert client.get('/docs').status_code == 404
    assert client.post('/hooks/max', content=b'{}', headers={'Origin': 'http://secretary-t9.localhost:44443'}).status_code == 404

    async def call(scope):
        events = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            events.append(message)
        await adapter.LocalHttpSyntheticAdapter(
            lambda *_: pytest.fail('invalid scope reached inner app'),
            http_origin='http://secretary-t9.localhost:44443',
            secure_origin='https://secretary-t9.localhost:44443')(
                scope, receive, send)
        return events

    import asyncio
    scope = {
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': 'GET', 'scheme': 'https', 'path': '/fixture', 'raw_path': b'/fixture',
        'query_string': b'', 'root_path': '',
        'headers': [(b'host', b'secretary-t9.localhost:44443')],
        'client': ('127.0.0.1', 12345), 'server': ('127.0.0.1', 44443),
    }
    events = asyncio.run(call(scope))
    assert events[0]['type'] == 'http.response.start' and events[0]['status'] == 403


def test_http_fixture_rejects_non_loopback_peer_before_gateway(case):
    adapter, _, _, _ = case
    app = adapter.LocalHttpSyntheticAdapter(
        lambda *_: pytest.fail('non-loopback peer reached inner app'),
        http_origin='http://secretary-t9.localhost:44443',
        secure_origin='https://secretary-t9.localhost:44443')

    async def run():
        events = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            events.append(message)
        await app({
            'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': 'GET', 'scheme': 'http', 'path': '/fixture', 'raw_path': b'/fixture',
            'query_string': b'', 'root_path': '',
            'headers': [(b'host', b'secretary-t9.localhost:44443')],
            'client': ('192.0.2.8', 12345), 'server': ('127.0.0.1', 44443),
        }, receive, send)
        return events

    import asyncio
    events = asyncio.run(run())
    assert events[0]['type'] == 'http.response.start' and events[0]['status'] == 403


def test_prepare_dist_removes_only_the_exact_max_sdk_tag(tmp_path):
    adapter = module()
    source = tmp_path / 'source'
    (source / 'assets').mkdir(parents=True)
    bridge = '<script id="team-max-bridge" async src="https://st.max.ru/js/max-web-app.js"></script>'
    (source / 'team.html').write_text(
        '<html>' + bridge + '<script src="./assets/team.js"></script></html>', encoding='utf-8')
    (source / 'assets' / 'team.js').write_text('/* isolated test app */', encoding='utf-8')

    result = adapter.prepare_dist(source, destination_parent=tmp_path)
    prepared = Path(result['frontend_dist'])
    page = (prepared / 'team.html').read_text(encoding='utf-8')
    assert result['removed_max_tag_count'] == 1
    assert bridge not in page and 'team.js' in page
    assert result['asset_count'] == 1
    assert result['evidence_path'] and Path(result['evidence_path']).is_file()
