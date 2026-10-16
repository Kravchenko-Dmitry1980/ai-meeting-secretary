"""T11 public ingress contract, offline ASGI and isolated rate ledgers only."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_auth_repository import AuthRepository
from secretary.interface import team_gateway as gateway

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def case(tmp_path):
    db = TeamDatabase(tmp_path / 'ingress.sqlite3')
    auth = AuthRepository(db, secret=b'synthetic-ingress-test-secret-32-bytes',
        clock=lambda:datetime(2026,10,4,tzinfo=timezone.utc))
    seen = []
    async def downstream(scope, receive, send):
        seen.append((scope, await receive()))
        await send({'type':'http.response.start','status':204,'headers':[]})
        await send({'type':'http.response.body','body':b''})
    return SimpleNamespace(db=db,auth=auth,seen=seen,downstream=downstream)


def request(c, *, path='/api/team/v1/me', raw=None, method='GET', headers=(), peer='127.0.0.1', body=b'', receive=None):
    messages = []
    async def received():
        return {'type':'http.request','body':body,'more_body':False}
    async def sent(message):
        messages.append(message)
    scope = {'type':'http','method':method,'path':path,'raw_path':raw or path.encode('ascii'),
        'query_string':b'', 'client':(peer,40001), 'headers':[(b'host',b'team.example'),*headers]}
    boundary = gateway._PublicBoundary(c.downstream,origin='https://team.example',
        webhook_secret='synthetic_webhook_secret',auth=c.auth)
    asyncio.run(boundary(scope,receive or received,sent))
    return messages[0]['status'], messages


@pytest.mark.parametrize('raw',[b'/api/team/v1/%6de',b'/api/team/v1/%256de',b'//api/team/v1/me',
    b'/api/./team/v1/me',b'/api/private/../team/v1/me',b'/api%2Fteam/v1/me',b'/api\\team/v1/me',
    b'/api/team/v1/me%00',b'/api/team/v1/me/'])
def test_raw_alias_never_reaches_public_handler(case,raw):
    status,_ = request(case,raw=raw)
    assert status == 404 and not case.seen


@pytest.mark.parametrize('path',['/internal/v1/task-publications','/api/v1/team/invitations','/config',
    '/session','/audio/test.wav','/docs','/openapi.json','/api/team/v1/tasks/01','/api/team/v1/tasks/1/edit'])
def test_private_and_unknown_routes_never_reach_handler(case,path):
    assert request(case,path=path)[0] == 404 and not case.seen


@pytest.mark.parametrize('peer,forwarded,expected',[
    ('127.0.0.1',b'203.0.113.7','203.0.113.7'),('::1',b'2001:db8::7','2001:db8::7'),
    ('203.0.113.8',b'203.0.113.7','203.0.113.8')])
def test_effective_peer_is_only_explicit_header_from_actual_loopback(case,peer,forwarded,expected):
    status,_ = request(case,peer=peer,headers=((b'x-team-client-ip',forwarded),
        (b'x-forwarded-for',b'10.0.0.1'),(b'forwarded',b'for=10.0.0.2')))
    assert status == 204
    scope = case.seen[0][0]
    assert scope['state']['team_client_ip'] == expected
    assert scope['client'][0] == peer


@pytest.mark.parametrize('headers',[
    ((b'x-team-client-ip',b'203.0.113.7'),(b'x-team-client-ip',b'203.0.113.8')),
    ((b'x-team-client-ip',b'203.0.113.7, 127.0.0.1'),),((b'x-team-client-ip',b'localhost'),),
    ((b'x-team-client-ip',b'203.0.113.7:45'),),((b'x-team-client-ip',b'fe80::1%eth0'),)])
def test_invalid_or_duplicate_trusted_proxy_identity_fails_closed(case,headers):
    assert request(case,headers=headers)[0] == 400 and not case.seen


def test_ip_rate_survives_boundary_recreation_and_preserves_other_peer(case):
    for _ in range(240):
        assert request(case,headers=((b'x-team-client-ip',b'203.0.113.7'),))[0] == 204
    assert request(case,headers=((b'x-team-client-ip',b'203.0.113.7'),))[0] == 429
    assert request(case,headers=((b'x-team-client-ip',b'203.0.113.8'),))[0] == 204


def test_verified_hook_has_separate_rate_budget_and_unknown_secret_no_ledger(case):
    with case.db.connection() as conn:
        before = conn.execute('SELECT COUNT(*) FROM team_auth_rates').fetchone()[0]
    assert request(case,path='/hooks/max',method='POST',body=b'{}')[0] == 401
    with case.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_auth_rates').fetchone()[0] == before
    kwargs = dict(path='/hooks/max',method='POST',body=b'{}',headers=((b'x-max-bot-api-secret',b'synthetic_webhook_secret'),))
    for _ in range(120):
        assert request(case,**kwargs)[0] == 204
    assert request(case,**kwargs)[0] == 429
    assert request(case)[0] == 204


def test_rate_ledger_runs_outside_event_loop(case,monkeypatch):
    original = case.auth.check_rate
    def checked(*args,**kwargs):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return original(*args,**kwargs)
    monkeypatch.setattr(case.auth,'check_rate',checked)
    assert request(case)[0] == 204


def test_complete_body_has_deadline(case,monkeypatch):
    monkeypatch.setattr(gateway,'BODY_TIMEOUT_SECONDS',0.01)
    async def slow():
        await asyncio.sleep(0.03)
        return {'type':'http.request','body':b'', 'more_body':False}
    assert request(case,receive=slow)[0] == 408 and not case.seen


def test_total_deadline_cannot_be_extended_by_frequent_chunks(case,monkeypatch):
    monkeypatch.setattr(gateway,'BODY_TIMEOUT_SECONDS',0.025)
    async def trickle():
        await asyncio.sleep(0.01)
        return {'type':'http.request','body':b' ', 'more_body':True}
    assert request(case,receive=trickle)[0] == 408 and not case.seen


@pytest.mark.parametrize('body',[b'{invalid',b'{"value":"a","value":"b"}'])
def test_login_json_failures_consume_original_login_quota(case,body):
    kwargs = dict(path='/api/team/v1/session/code',method='POST',body=body,
        headers=((b'origin',b'https://team.example'),))
    for _ in range(20):
        assert request(case,**kwargs)[0] == 400
    status,messages = request(case,**kwargs)
    assert status == 429
    assert json.loads(messages[1]['body'])['error_code'] == 'team_login_rate_limited'


@pytest.mark.parametrize('auth',[None,object(),SimpleNamespace(check_rate='misconfigured')])
def test_missing_rate_port_fails_closed_without_constructor_io(case,auth):
    case.auth = auth
    status,messages = request(case)
    assert status == 503 and not case.seen
    assert json.loads(messages[1]['body']) == {'error_code':'team_ingress_unavailable'}


@pytest.mark.parametrize('error',[TypeError('PRIVATE_PORT'),AttributeError('PRIVATE_PORT')])
def test_invalid_rate_port_failure_is_sanitized(case,error):
    def fail(*args,**kwargs):
        raise error
    case.auth = SimpleNamespace(check_rate=fail)
    status,messages = request(case)
    assert status == 503 and b'PRIVATE' not in messages[1]['body']


@pytest.mark.parametrize('path,method',[('/hooks/max','GET'),('/api/team/v1/me','POST'),
    ('/api/team/v1/session/max','GET'),('/api/team/v1/tasks','HEAD'),('/api/team/v1/commands','PUT')])
def test_wrong_methods_cannot_enter_public_handler(case,path,method):
    assert request(case,path=path,method=method)[0] == 404 and not case.seen


def test_streamed_body_limit_prevents_handler(case):
    chunks = iter((b' '*200000,b' '*100000))
    async def received():
        return {'type':'http.request','body':next(chunks),'more_body':True}
    assert request(case,receive=received)[0] == 413 and not case.seen


def test_proxy_template_and_deployment_manifest_exist_and_match_public_surface():
    template = ROOT/'config/team/Caddyfile.example'
    manifest = ROOT/'config/team/deployment-manifest.json'
    assert template.is_file() and manifest.is_file()
    data = json.loads(manifest.read_text(encoding='utf-8'))
    assert data['public_listener'] == {'protocol':'https','host':'owner_configured','tcp_port':443}
    assert data['gateway'] == {'host':'127.0.0.1','port':8766,'proxy_headers':False}
    assert data['caddy']['version'] == '2.11.7'
    assert data['caddy']['standard_modules_only'] is True
    assert data['qualification'] == 'OFFLINE_PACKAGE_ONLY'
    body = template.read_text(encoding='utf-8')
    assert 'rate_limit' not in body
    assert 'admin off' in body and 'persist_config off' in body
    assert 'import "{$TEAM_ASSET_ROUTES}"' in body
    assert 'lb_retries 0' in body
    assert 'disable_http_challenge' in body


def test_proxy_allows_each_canonical_public_route_with_only_its_method():
    import re
    template = (ROOT / 'config/team/Caddyfile.example').read_text(encoding='utf-8')
    patterns = {name: re.compile(re.search(r'path_regexp ' + name + r' (.+)', template).group(1))
        for name in ('api_read', 'api_write', 'api_logout', 'hook')}
    expected_method = {'api_read': 'GET', 'api_write': 'POST', 'api_logout': 'DELETE', 'hook': 'POST'}
    schema = json.loads((ROOT / 'docs/contracts/team.openapi.json').read_text(encoding='utf-8'))
    samples = {'task_id': '9007199254740997', 'operation_id': '10000000-0000-4000-8000-000000000001',
        'preview_id': '20000000-0000-4000-8000-000000000002'}
    for path, operations in schema['paths'].items():
        rendered = path
        for key, value in samples.items():
            rendered = rendered.replace('{' + key + '}', value)
        assert '{' not in rendered
        matches = [name for name, pattern in patterns.items() if pattern.fullmatch(rendered)]
        assert len(matches) == 1, rendered
        assert {method.upper() for method in operations} == {expected_method[matches[0]]}, rendered
    for path in ('/api/team/v1/tasks/01/due-resolution', '/api/team/v1/due-resolutions/../confirm',
        '/api/team/v1/tasks/1/due-resolution/private', '/api/team/v1/due-resolutions/000000000000000000000000000000000000/confirm'):
        assert not any(pattern.fullmatch(path) for pattern in patterns.values())


def native_proxy_probe(executable):
    """Explicit LOCAL_INTEGRATION entry point; never called by offline pytest.

    Standard pinned Caddy, owned hidden high-port loopback process, synthetic
    static files and upstream. No TLS listener, ACME or credential access.
    """
    import hashlib
    import http.client
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import os
    import socket
    import subprocess
    import threading
    import time
    from uuid import uuid4
    import psutil

    pin = json.loads((ROOT/'config/team/deployment-manifest.json').read_text())['caddy']
    executable = Path(executable).resolve()
    if not executable.is_relative_to(ROOT/'.runtime') or hashlib.sha256(executable.read_bytes()).hexdigest() != pin['binary_sha256']:
        raise ValueError('native_proxy_binary_pin_invalid')
    directory = ROOT/'.runtime/team-rollout'/('t11-native-proxy with spaces-'+uuid4().hex)
    directory.mkdir()
    public = directory/'public'
    (public/'assets').mkdir(parents=True)
    (public/'team.html').write_text('<!doctype html><title>synthetic-team</title>',encoding='utf-8')
    (public/'assets/fixture-Aa123.js').write_text('/* synthetic-public-team */',encoding='utf-8')
    (public/'assets/private.js').write_text('PRIVATE_SYNTHETIC_SECRETARY',encoding='utf-8')
    (public/'index.html').write_text('PRIVATE_SYNTHETIC_SECRETARY',encoding='utf-8')
    (public/'deploy-manifest.json').write_text('{"private":"metadata"}',encoding='utf-8')
    snippet = public/'asset-routes.caddy'
    snippet.write_text('@team_assets {\n method GET HEAD\n path_regexp team_assets `^/team/assets/fixture-Aa123\\.js$`\n}\n',encoding='utf-8')
    observed=[]
    class Upstream(BaseHTTPRequestHandler):
        def handle_request(self):
            length = int(self.headers.get('Content-Length','0'))
            incoming=self.rfile.read(length)
            if len(incoming)!=length:
                return  # Caddy closed an over-limit upstream stream before dispatch.
            observed.append({'path':self.path,'method':self.command,'headers':dict(self.headers)})
            body=b'{"synthetic":true}'
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_GET=handle_request
        do_POST=handle_request
        do_DELETE=handle_request
        def log_message(self,*args):
            pass
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    thread=threading.Thread(target=upstream.serve_forever,daemon=True)
    thread.start()
    with socket.socket() as port_socket:
        port_socket.bind(('127.0.0.1',0))
        port=port_socket.getsockname()[1]
    config=(ROOT/'config/team/Caddyfile.example').read_text(encoding='utf-8')
    config=config.replace('https://{$TEAM_PUBLIC_HOST}:443 {',f'http://{{$TEAM_PUBLIC_HOST}}:{port} {{\n    bind 127.0.0.1')
    config=config.replace('https://:443 {',f'http://:{port} {{\n    bind 127.0.0.1')
    config=config.replace('    tls {\n        issuer acme {\n            disable_http_challenge\n        }\n    }\n','')
    config=config.replace('auto_https disable_redirects','auto_https off')
    config=config.replace('127.0.0.1:8766',f'127.0.0.1:{upstream.server_port}')
    config_path=directory/'Caddyfile'
    config_path.write_text(config,encoding='utf-8')
    env={key:value for key,value in os.environ.items() if not key.startswith(('TEAM_','MAX_','POLZA_','CADDY_'))}
    env.update(TEAM_PUBLIC_HOST='team.example.invalid',TEAM_PUBLIC_ROOT=public.as_posix(),
        TEAM_ASSET_ROUTES=snippet.as_posix(),TEAM_CADDY_DATA=(directory/'cert-state').as_posix())
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    checks=[]
    def require(condition,name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
    def fetch(path,method='GET',host='team.example.invalid',headers=None,body=None):
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
        try:
            connection.request(method,path,body=body,headers={'Host':host,**(headers or {})})
            response=connection.getresponse()
            return response.status,dict(response.getheaders()),response.read()
        finally:
            connection.close()
    process=None
    creation=None
    error=None
    try:
        validated=subprocess.run([str(executable),'validate','--config',str(config_path),'--adapter','caddyfile'],
            cwd=directory,env=env,capture_output=True,timeout=15,creationflags=flags)
        require(validated.returncode==0,'native_configuration_valid')
        adapted=subprocess.run([str(executable),'adapt','--config',str(config_path),'--adapter','caddyfile'],
            cwd=directory,env=env,capture_output=True,timeout=15,creationflags=flags)
        require(adapted.returncode==0,'native_adapter_valid')
        configuration=json.loads(adapted.stdout)
        (directory/'adapted.json').write_text(json.dumps(configuration,indent=2),encoding='utf-8')
        listeners=[address for server in configuration['apps']['http']['servers'].values() for address in server['listen']]
        require(listeners==[f'127.0.0.1:{port}'],'only_owned_high_loopback_listener')
        with (directory/'process-output.log').open('wb') as output:
            process=subprocess.Popen([str(executable),'run','--config',str(config_path),'--adapter','caddyfile'],
                cwd=directory,env=env,stdout=output,stderr=output,creationflags=flags)
            creation=psutil.Process(process.pid).create_time()
            deadline=time.monotonic()+10
            while True:
                if process.poll() is not None:
                    raise AssertionError('native_process_exited')
                try:
                    if fetch('/team/')[0]==200:
                        break
                except OSError:
                    pass
                if time.monotonic()>=deadline:
                    raise AssertionError('native_process_not_ready')
                time.sleep(0.05)
            redirect_status=fetch('/team')[0]
            require(redirect_status==308,'team_redirect_'+str(redirect_status))
            index_status,index_headers,index_body=fetch('/team/')
            require(index_status==200 and b'synthetic-team' in index_body,'exact_team_entry')
            require('https://st.max.ru/js/max-web-app.js' in index_headers.get('Content-Security-Policy','')
                and "style-src 'self'" in index_headers['Content-Security-Policy'],'team_only_future_sdk_csp')
            require(b'synthetic-public-team' in fetch('/team/assets/fixture-Aa123.js')[2],'exact_team_asset')
            denied=['/','/index.html','/assets/fixture-Aa123.js','/team/team.html','/team/index.html',
                '/team/deploy-manifest.json','/team/asset-routes.caddy','/team/assets/private.js',
                '/team/assets/fixture-Aa123.js.map','/team/assets/FIXTURE-Aa123.js','/TEAM/',
                '/internal/v1/task-publications','/api/v1/team/invitations','/config','/session','/audio/file.wav',
                '/api/team/v1/%6de','/api/team/v1/%256de','/api%2fteam/v1/me',
                '//api/team/v1/me','/api/./team/v1/me','/api/private/../team/v1/me',
                '/api\\team/v1/me','/team/assets/../team.html','/team/%2e%2e/index.html',
                '/api/team/v1/tasks/01','/api/team/v1/tasks/1/private','/api/team/v1/ME',
                '/api/team/v1/tasks/01/due-resolution', '/api/team/v1/tasks/1/due-resolution/private',
                '/api/team/v1/due-resolutions/000000000000000000000000000000000000/confirm']
            for path in denied:
                before=len(observed)
                status,_,body=fetch(path)
                require(status==404 and len(observed)==before and b'PRIVATE_SYNTHETIC' not in body,'deny_'+path)
            require(fetch('/api/team/v1/me',method='POST')[0]==404,'deny_wrong_api_method')
            require(fetch('/team/',method='POST')[0]==404,'deny_static_mutation')
            status,headers,body=fetch('/api/team/v1/me?after=%31',headers={'X-Team-Client-IP':'203.0.113.222',
                'X-Forwarded-For':'203.0.113.223','Forwarded':'for=203.0.113.224'})
            require(status==200 and observed[-1]['path']=='/api/team/v1/me?after=%31','query_encoded_values_preserved')
            upstream_headers={key.lower():value for key,value in observed[-1]['headers'].items()}
            require(upstream_headers.get('x-team-client-ip')=='127.0.0.1','spoofed_proxy_identity_overwritten')
            require(upstream_headers.get('forwarded') is None,'inbound_forwarded_removed')
            require(upstream_headers.get('host')=='team.example.invalid','configured_host_preserved')
            before=len(observed)
            status,_,_=fetch('/api/team/v1/me',host='foreign.example.invalid')
            require(status in {400,403,404,421} and len(observed)==before,'foreign_host_denied')
            require('Content-Security-Policy' in headers and 'unsafe-inline' not in headers['Content-Security-Policy'],'restrictive_script_style_csp')
            require('st.max.ru' not in headers['Content-Security-Policy'],'api_has_no_sdk_csp')
            routes=[('/hooks/max','POST'),('/api/team/v1/session/max','POST'),('/api/team/v1/session/code','POST'),
                ('/api/team/v1/session','DELETE'),('/api/team/v1/me','GET'),('/api/team/v1/members','GET'),
                ('/api/team/v1/status','GET'),('/api/team/v1/dashboard','GET'),('/api/team/v1/tasks','GET'),
                ('/api/team/v1/tasks/9007199254740997','GET'),('/api/team/v1/tasks/9007199254740997/history','GET'),
                ('/api/team/v1/commands','POST'),('/api/team/v1/commands/10000000-0000-4000-8000-000000000001','GET'),
                ('/api/team/v1/tasks/9007199254740997/due-resolution', 'GET'),
                ('/api/team/v1/tasks/9007199254740997/due-resolution/previews', 'POST'),
                ('/api/team/v1/due-resolutions/10000000-0000-4000-8000-000000000001/confirm', 'POST')]
            for path,method in routes:
                status,response_headers,_=fetch(path,method=method,body=b'{}' if method=='POST' else None)
                require(status==200 and observed[-1]['path']==path and observed[-1]['method']==method,'allow_'+method+'_'+path)
                require('st.max.ru' not in response_headers.get('Content-Security-Policy',''),'no_sdk_'+path)
            for path, method in (('/api/team/v1/tasks/1/due-resolution', 'POST'),
                ('/api/team/v1/tasks/1/due-resolution/previews', 'GET'),
                ('/api/team/v1/due-resolutions/10000000-0000-4000-8000-000000000001/confirm', 'GET')):
                before = len(observed)
                require(fetch(path, method=method)[0] == 404 and len(observed) == before, 'deny_resolution_method_' + path)
            for path in ('/hooks/max','/api/team/v1/commands'):
                before=len(observed)
                maximum=1048576 if path=='/hooks/max' else 262144
                status,_,_=fetch(path,method='POST',body=b' '* (maximum+1))
                require(status==413 and len(observed)==before,'body_limit_'+path)
    except Exception as exc:
        error=str(exc) if isinstance(exc,AssertionError) else type(exc).__name__
    finally:
        if process is not None and process.poll() is None:
            if psutil.Process(process.pid).create_time()!=creation:
                raise RuntimeError('owned_process_identity_changed')
            process.terminate()
            process.wait(timeout=10)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
    evidence={'qualification':'LOCAL_INTEGRATION_HTTP_ONLY','binary_sha256':pin['binary_sha256'],
        'checks':checks,'passed':error is None,'error_code':error,'owned_process_stopped':process is None or process.poll() is not None,
        'tls_certificate_external_max':'NOT_TESTED'}
    (directory/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    return evidence,directory


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Explicit owned native loopback Caddy probe')
    parser.add_argument('--native-caddy',required=True,type=Path)
    args=parser.parse_args()
    result,artifact=native_proxy_probe(args.native_caddy)
    print(json.dumps({'passed':result['passed'],'checks':len(result['checks']),'error_code':result['error_code'],
        'evidence':str(artifact/'evidence.json')},ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
