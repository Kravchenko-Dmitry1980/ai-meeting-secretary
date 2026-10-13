"""Short-lived loopback HTTP adapter for the synthetic Team browser fixture.

This is not a production launcher and does not qualify TLS, MAX WebView, or
production cookie security. It maps only the exact local HTTP origin to the
synthetic fixture's HTTPS-origin contract. The real Gateway cookie is renamed
per fixture only at this local boundary, then restored before reaching the Gateway.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import psutil
import re
import socket
import subprocess
import sys
import time
import shutil
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.responses import Response

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = ROOT / '.runtime/team-rollout'
SCRIPT = Path(__file__).resolve()
HOSTNAME = 'secretary-t9.localhost'  # Inner HTTPS origin used by the real Gateway fixture.
DEFAULT_HTTP_HOSTNAME = 'localhost'  # Loopback host; per-run cookie names isolate local fixtures.
LOCAL_HOSTNAMES = (DEFAULT_HTTP_HOSTNAME, HOSTNAME)
_RUN_ID = re.compile(r'^t9-http-ui-([0-9a-f]{32})$')
_SESSION_COOKIE_NAME = b'__Host-secretary-team'
MAX_LIFETIME_SECONDS = 1800
MODE = 'HTTP_SYNTHETIC_UI_ONLY'
_MAX_BRIDGE_TAG = '<script id="team-max-bridge" async src="https://st.max.ru/js/max-web-app.js"></script>'
_FORWARDED_HEADERS = {b'forwarded', b'x-forwarded-for', b'x-forwarded-host', b'x-forwarded-proto'}
_ALLOWED_METHODS = {'GET', 'HEAD', 'POST', 'DELETE'}


class FixtureFailure(ValueError):
    """Fixed diagnostic code; details are kept out of stdout and the browser."""


def require(condition, code):
    if not condition:
        raise FixtureFailure(code)


def session_cookie_name_for_run(run_id):
    """Give each HTTP fixture a browser cookie name that cannot collide by port."""
    match = _RUN_ID.fullmatch(run_id) if isinstance(run_id, str) else None
    require(match is not None, 'fixture_run_id_invalid')
    return _SESSION_COOKIE_NAME + b'-t9-' + match.group(1).encode('ascii')


def _rewrite_request_cookie(value, fixture_cookie_name):
    kept, session_value = [], None
    for raw_pair in value.split(b';'):
        pair = raw_pair.strip()
        name, separator, cookie_value = pair.partition(b'=')
        if not separator:
            continue
        if name == fixture_cookie_name:
            if session_value is None:
                session_value = cookie_value
        elif name != _SESSION_COOKIE_NAME:
            kept.append(pair)
    if session_value is not None:
        kept.append(_SESSION_COOKIE_NAME + b'=' + session_value)
    return b'; '.join(kept)


def _rewrite_response_cookie(value, fixture_cookie_name):
    pair, separator, attributes = value.partition(b';')
    name, equals, cookie_value = pair.partition(b'=')
    if equals and name == _SESSION_COOKIE_NAME:
        pair = fixture_cookie_name + b'=' + cookie_value
        return pair + (separator + attributes if separator else b'')
    return value


def _base_module():
    path = SCRIPT.with_name('probe_team_ui.py')
    spec = importlib.util.spec_from_file_location('secretary_t9_tls_fixture_source', path)
    require(spec is not None and spec.loader is not None, 'fixture_source_unavailable')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_base = _base_module()


def _origin_parts(value, scheme):
    parsed = urlsplit(value)
    require(parsed.scheme == scheme and parsed.hostname in LOCAL_HOSTNAMES and parsed.port is not None
            and parsed.username is None and parsed.password is None and not parsed.path
            and not parsed.query and not parsed.fragment,
            'fixture_origin_invalid')
    require(parsed.netloc == f'{parsed.hostname}:{parsed.port}', 'fixture_origin_invalid')
    return parsed


class LocalHttpSyntheticAdapter:
    """Fail-closed ASGI wrapper; no forwarding headers or non-loopback peers."""

    def __init__(self, app, *, http_origin, secure_origin, run_id):
        self.app = app
        external = _origin_parts(http_origin, 'http')
        internal = _origin_parts(secure_origin, 'https')
        require(external.port == internal.port, 'fixture_origin_invalid')
        self.http_origin = http_origin
        self.secure_origin = secure_origin
        self.fixture_cookie_name = session_cookie_name_for_run(run_id)
        self.authority = external.netloc.encode('ascii')
        self.secure_authority = internal.netloc.encode('ascii')
        self.http_origin_bytes = http_origin.encode('ascii')
        self.secure_origin_bytes = secure_origin.encode('ascii')
        self.port = external.port

    async def __call__(self, scope, receive, send):
        kind = scope.get('type')
        if kind == 'lifespan':
            return await self.app(scope, receive, send)
        if kind == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if kind != 'http':
            return

        headers = list(scope.get('headers', ()))
        host_values = [value for name, value in headers if name.lower() == b'host']
        origin_values = [value for name, value in headers if name.lower() == b'origin']
        forwarded = any(name.lower() in _FORWARDED_HEADERS for name, _ in headers)
        client = scope.get('client')
        server = scope.get('server')
        try:
            peer_is_loopback = bool(client and ipaddress.ip_address(client[0]).is_loopback)
        except (ValueError, TypeError):
            peer_is_loopback = False
        allowed_path = (scope.get('path') in {'/fixture', '/fixture/status', '/team/', '/team/team.html'}
                        or scope.get('path', '').startswith('/team/assets/')
                        or scope.get('path', '').startswith('/api/team/v1/'))
        valid_server = bool(server and len(server) >= 2 and server[1] == self.port)
        valid_boundary = (scope.get('scheme') == 'http' and scope.get('method') in _ALLOWED_METHODS
                          and peer_is_loopback and valid_server and host_values == [self.authority]
                          and len(origin_values) <= 1 and not forwarded)
        if origin_values:
            valid_boundary = valid_boundary and origin_values == [self.http_origin_bytes]
        if scope.get('method') not in {'GET', 'HEAD', 'OPTIONS'}:
            valid_boundary = valid_boundary and origin_values == [self.http_origin_bytes]
        if not valid_boundary:
            return await Response(status_code=403, headers={'Cache-Control': 'no-store'})(scope, receive, send)
        if not allowed_path or scope.get('path') == '/hooks/max':
            return await Response(status_code=404, headers={'Cache-Control': 'no-store'})(scope, receive, send)

        # The trusted value is derived from constructor constants, not proxy input.
        translated = []
        for name, value in headers:
            if name.lower() == b'origin':
                translated.append((name, self.secure_origin_bytes))
            elif name.lower() == b'host':
                translated.append((name, self.secure_authority))
            elif name.lower() == b'cookie':
                translated.append((name, _rewrite_request_cookie(value, self.fixture_cookie_name)))
            else:
                translated.append((name, value))
        forwarded_scope = dict(scope)
        forwarded_scope['scheme'] = 'https'
        forwarded_scope['headers'] = translated

        async def rewrite_session_cookie(message):
            if message.get('type') == 'http.response.start':
                rewritten_headers = []
                for name, value in message.get('headers', ()):
                    if name.lower() == b'set-cookie':
                        value = _rewrite_response_cookie(value, self.fixture_cookie_name)
                    rewritten_headers.append((name, value))
                message = {**message, 'headers': rewritten_headers}
            await send(message)

        return await self.app(forwarded_scope, receive, rewrite_session_cookie)


def build_http_fixture(run_root, *, frontend_dist, http_origin):
    """Build the existing real-Gateway fixture behind the isolated HTTP wrapper."""
    run_root = _validate_run_root(run_root, parent=Path(run_root).absolute().parent)
    require(run_root.is_dir(), 'fixture_run_root_required')
    external = _origin_parts(http_origin, 'http')
    secure_origin = f'https://{HOSTNAME}:{external.port}'
    fixture_root = run_root / ('t9-ui-' + uuid4().hex)
    _base.no_reparse(fixture_root)
    fixture_root.mkdir()
    fixture = _base.build_fixture(fixture_root, frontend_dist=frontend_dist, origin=secure_origin)
    fixture.app.mode = MODE
    app = LocalHttpSyntheticAdapter(fixture.app, http_origin=http_origin, secure_origin=secure_origin,
        run_id=run_root.name)
    return fixture, app


def prepare_dist(frontend_dist, *, destination_parent=SCRATCH):
    """Freeze a local Team-only copy and remove only the exact MAX SDK script tag."""
    source = Path(frontend_dist).absolute()
    _base.no_reparse(source)
    source_html = source / 'team.html'
    source_assets = source / 'assets'
    _base.no_reparse(source_html)
    _base.no_reparse(source_assets)
    require(source_html.is_file() and source_assets.is_dir(), 'built_team_entry_required')
    original = source_html.read_text(encoding='utf-8')
    count = original.count(_MAX_BRIDGE_TAG)
    require(count in (0, 1), 'fixture_max_tag_shape_invalid')
    sanitized = original.replace(_MAX_BRIDGE_TAG, '', 1) if count else original
    copied_size, copied_files = 0, 0
    for item in source_assets.rglob('*'):
        _base.no_reparse(item)
        require(item.is_dir() or item.is_file(), 'fixture_asset_invalid')
        if item.is_file():
            copied_files += 1
            copied_size += item.stat().st_size
            require(copied_files <= 512 and item.stat().st_size <= 16 * 1024 * 1024
                    and copied_size <= 128 * 1024 * 1024, 'fixture_asset_graph_too_large')

    destination_parent = Path(destination_parent).absolute()
    _base.no_reparse(destination_parent)
    destination_parent.mkdir(parents=True, exist_ok=True)
    target = destination_parent / ('t9-http-dist-' + uuid4().hex)
    _base.no_reparse(target)
    target.mkdir()
    _base.no_reparse(target / 'assets')
    shutil.copytree(source_assets, target / 'assets')
    (target / 'team.html').write_text(sanitized, encoding='utf-8', newline='')
    allowed = _base.team_assets(target)
    assets = []
    for path in sorted(allowed):
        assets.append({'path': path.relative_to(target).as_posix(),
                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'bytes': path.stat().st_size})
    evidence = {'mode': MODE, 'source_team_html_sha256': hashlib.sha256(source_html.read_bytes()).hexdigest(),
                'prepared_team_html_sha256': hashlib.sha256((target / 'team.html').read_bytes()).hexdigest(),
                'removed_max_tag_count': count, 'asset_count': len(assets), 'assets': assets}
    evidence_path = target.with_name(target.name + '-evidence.json')
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=True, indent=2), encoding='ascii')
    return {'frontend_dist': str(target), 'evidence_path': str(evidence_path), **evidence}


def _validate_run_root(value, *, parent=SCRATCH):
    path = Path(value).absolute()
    parent = Path(parent).absolute()
    require(path.parent == parent and bool(re.fullmatch(r't9-http-ui-[0-9a-f]{32}', path.name)),
            'fixture_scope_invalid')
    _base.no_reparse(path)
    require(path.resolve().parent == parent.resolve(), 'fixture_scope_invalid')
    return path


def _write_manifest(root, data):
    _base.no_reparse(root)
    temporary, target = root / 'manifest.pending', root / 'manifest.json'
    _base.no_reparse(temporary)
    _base.no_reparse(target)
    temporary.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding='ascii')
    temporary.replace(target)


def _read_manifest(root):
    root = _validate_run_root(root)
    target = root / 'manifest.json'
    _base.no_reparse(target)
    require(target.is_file() and target.stat().st_size <= 16 * 1024, 'fixture_manifest_unavailable')
    try:
        data = json.loads(target.read_text(encoding='ascii'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise FixtureFailure('fixture_manifest_invalid') from None
    require(data.get('run_id') == root.name and data.get('mode') == MODE
            and data.get('state') in {'starting', 'ready', 'stop_requested', 'stopped', 'failed'},
            'fixture_manifest_invalid')
    return data


def _owner_process(root, data):
    try:
        process = psutil.Process(int(data['pid']))
        require(process.create_time() == data['create_time'], 'fixture_owner_changed')
        arguments = process.cmdline()
        require(str(SCRIPT) in arguments and 'serve' in arguments and str(root) in arguments,
                'fixture_owner_changed')
        return process
    except (psutil.Error, KeyError, TypeError, ValueError):
        raise FixtureFailure('fixture_owner_unavailable') from None


def _ready(root, data):
    port = int(data['port'])
    hostname = data.get('host', HOSTNAME)
    require(hostname in LOCAL_HOSTNAMES, 'fixture_manifest_invalid')
    require(1 <= port <= 65535, 'fixture_manifest_invalid')
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request('GET', '/fixture/status', headers={'Host': f'{hostname}:{port}'})
        response = connection.getresponse()
        body = response.read(8192)
        payload = json.loads(body)
        require(response.status == 200 and payload.get('run_id') == data.get('fixture_run_id')
                and payload.get('mode') == MODE and payload.get('tls_qualification') == 'not_tested',
                'fixture_readiness_failed')
    except (OSError, ValueError, json.JSONDecodeError):
        raise FixtureFailure('fixture_readiness_failed') from None
    finally:
        connection.close()


def _request_stop(root):
    data = _read_manifest(root)
    _owner_process(root, data)
    marker = root / 'stop.request'
    _base.no_reparse(marker)
    marker.write_text(root.name, encoding='ascii')
    return {'run_id': root.name, 'state': 'stop_requested', 'mode': MODE}


def _stop_requested(root):
    marker = root / 'stop.request'
    _base.no_reparse(marker)
    if not marker.exists():
        return False
    require(marker.is_file() and marker.stat().st_size <= 128, 'fixture_stop_marker_invalid')
    return marker.read_text(encoding='ascii') == root.name


async def _serve(root, dist, http_hostname=DEFAULT_HTTP_HOSTNAME):
    import uvicorn

    root = _validate_run_root(root)
    require(http_hostname in LOCAL_HOSTNAMES, 'fixture_hostname_invalid')
    require(root.is_dir() and all(item.name == 'lifecycle.log' for item in root.iterdir()),
            'fresh_fixture_directory_required')
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    fixture = worker = watcher = server = None
    clean = False
    data = None
    try:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.listen(128)
        data = {'version': 1, 'run_id': root.name, 'pid': os.getpid(),
                'create_time': psutil.Process().create_time(), 'state': 'starting',
                'mode': MODE, 'host': http_hostname, 'port': port,
                'origin': f'http://{http_hostname}:{port}',
                'max_lifetime_seconds': MAX_LIFETIME_SECONDS}
        _write_manifest(root, data)
        fixture, app = build_http_fixture(root, frontend_dist=dist, http_origin=data['origin'])
        data['fixture_run_id'] = fixture.run_root.name
        _write_manifest(root, data)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
            proxy_headers=False, access_log=False, log_config=None, log_level='critical',
            timeout_graceful_shutdown=5, ws='none'))

        async def watch():
            deadline = time.monotonic() + MAX_LIFETIME_SECONDS
            try:
                while not server.started and not server.should_exit:
                    if time.monotonic() >= deadline:
                        server.should_exit = True
                    await asyncio.sleep(.05)
                if server.started and not server.should_exit:
                    data['state'] = 'ready'
                    _write_manifest(root, data)
                while not server.should_exit:
                    if _stop_requested(root) or time.monotonic() >= deadline or worker.done():
                        server.should_exit = True
                        break
                    await asyncio.sleep(.2)
            finally:
                server.should_exit = True

        worker = asyncio.create_task(fixture.worker.run())
        watcher = asyncio.create_task(watch())
        await server.serve(sockets=[sock])
        clean = True
    finally:
        if fixture is not None:
            fixture.worker.stop()
        if server is not None:
            server.should_exit = True
        if worker is not None:
            await worker
        if watcher is not None:
            await watcher
        if fixture is not None:
            fixture.close()
        sock.close()
        if data is not None:
            data['state'] = 'stopped' if clean else 'failed'
            _write_manifest(root, data)


def start(frontend_dist, *, http_hostname=DEFAULT_HTTP_HOSTNAME):
    prepared = prepare_dist(frontend_dist)
    dist = Path(prepared['frontend_dist'])
    root = _validate_run_root(SCRATCH / ('t9-http-ui-' + uuid4().hex))
    require(http_hostname in LOCAL_HOSTNAMES, 'fixture_hostname_invalid')
    root.mkdir()
    args = [sys.executable, '-B', str(SCRIPT), 'serve', '--run-root', str(root),
            '--frontend-dist', str(dist), '--http-hostname', http_hostname]
    with (root / 'lifecycle.log').open('xb') as output:
        process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
            stderr=output, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            require(process.poll() is None, 'fixture_child_failed')
            if (root / 'manifest.json').is_file():
                data = _read_manifest(root)
                if data['state'] == 'ready':
                    _owner_process(root, data)
                    _ready(root, data)
                    return {**data, 'run_root': str(root), 'fixture_url': data['origin'] + '/fixture',
                            'team_url': data['origin'] + '/team/',
                            'fixture_run_root': str(root / data['fixture_run_id']),
                            'prepared_dist': prepared['frontend_dist'],
                            'prepared_dist_evidence': prepared['evidence_path']}
            time.sleep(.1)
        raise FixtureFailure('fixture_start_timeout')
    except BaseException:
        if process.poll() is None and (root / 'manifest.json').is_file():
            try:
                _request_stop(root)
            except (FixtureFailure, OSError):
                pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'serve', 'status', 'stop'))
    parser.add_argument('--run-root', type=Path)
    parser.add_argument('--frontend-dist', type=Path, default=ROOT / 'frontend/dist')
    parser.add_argument('--http-hostname', choices=LOCAL_HOSTNAMES, default=DEFAULT_HTTP_HOSTNAME)
    args = parser.parse_args()
    try:
        if args.action == 'start':
            value = start(args.frontend_dist, http_hostname=args.http_hostname)
        else:
            require(args.run_root is not None, 'fixture_run_root_required')
            root = _validate_run_root(args.run_root)
            if args.action == 'serve':
                asyncio.run(_serve(root, args.frontend_dist, args.http_hostname))
                return 0
            if args.action == 'stop':
                value = _request_stop(root)
            else:
                value = _read_manifest(root)
                if value['state'] in {'ready', 'stop_requested'}:
                    _owner_process(root, value)
                    _ready(root, value)
        print(json.dumps(value, ensure_ascii=True))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, FixtureFailure) else 'fixture_failed'
        print(json.dumps({'error_code': code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
