"""T11 read-only capability probe; all DNS/TCP/TLS responses are synthetic."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import ssl
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/team/check_ingress.py'


def module():
    assert SCRIPT.is_file(), 'T11 ingress helper absent'
    spec = importlib.util.spec_from_file_location('synthetic_ingress',SCRIPT)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def fixture(*, addresses=('8.8.8.8',), response=b'HTTP/1.1 200 OK\r\nContent-Length: 9000000\r\n\r\nPRIVATE_BODY'):
    api=module()
    calls=[]
    class Stream:
        def __init__(self):
            self.remaining=response
            self.closed=False
        def settimeout(self,value):
            assert 0 < value <= 3
        def getpeername(self):
            return addresses[0],443
        def sendall(self,value):
            calls.append(('send',value))
        def recv(self,size):
            calls.append(('recv',size))
            out,self.remaining=self.remaining[:size],self.remaining[size:]
            return out
        def version(self):
            return 'TLSv1.3'
        def close(self):
            self.closed=True
    stream=Stream()
    class Context:
        check_hostname=True
        verify_mode=ssl.CERT_REQUIRED
        minimum_version=ssl.TLSVersion.TLSv1_2
        def wrap_socket(self,sock,*,server_hostname):
            calls.append(('tls',server_hostname))
            return sock
    context=Context()
    def resolver(host,timeout):
        calls.append(('dns',host,timeout))
        return list(addresses)
    def connector(address,port,timeout):
        calls.append(('tcp',address,port,timeout))
        return stream
    options=dict(read_only=True,resolver=resolver,connector=connector,tls_factory=lambda:context,
        clock=lambda:datetime(2026,10,5,9,tzinfo=timezone.utc),monotonic=lambda:0.0)
    return SimpleNamespace(api=api,calls=calls,stream=stream,context=context,options=options)


def test_missing_url_reads_no_network_route_or_owner_config():
    c=fixture()
    def forbidden(*args,**kwargs):
        pytest.fail('missing URL must not resolve, connect, inspect routes or load TLS')
    c.options.update(resolver=forbidden,connector=forbidden,tls_factory=forbidden)
    report=c.api.check_ingress(**c.options)
    assert report['status']=='configuration_required' and report['target'] is None
    assert c.calls==[] and report['read_only'] is True
    assert report['external_reachability']['status']=='unknown'


@pytest.mark.parametrize('url',[
    'http://example.org/team/','https://user:PRIVATE@example.org/team/',
    'https://example.org:8443/team/','https://example.org/team/?PRIVATE=secret',
    'https://example.org/team/#PRIVATE','https://example.org/team/?','https://example.org/team/#',
    'https://example.org/%2e%2e/internal','https://example.org/team/../internal',
    'https://example.org/./team','https://example.org/team\\internal',
    'https://example.org/team/\r\nX-PRIVATE: secret','https://example.org/team/ hi',
    'https://example.org//team','https://example.org./team/','https://example.org:0443/team/',
    'https://[fe80::1%25eth0]/','https://example.org/team/"PRIVATE',
    'https://пример.рф/team/','https://example.org/'+'a'*3000,
])
def test_invalid_explicit_url_never_leaks_or_connects(url):
    c=fixture()
    report=c.api.check_ingress(url,**c.options)
    assert report['status']=='invalid_input' and report['target'] is None
    assert c.calls==[] and 'PRIVATE' not in json.dumps(report)


@pytest.mark.parametrize('addresses',[
    ('127.0.0.1',),('10.1.2.3',),('192.168.1.2',),('169.254.169.254',),
    ('100.64.1.2',),('::1',),('fe80::1',),('fc00::1',),('::ffff:127.0.0.1',),
    ('::ffff:8.8.8.8',),('224.0.0.1',),('0.0.0.0',),('8.8.8.8','192.168.1.2'),
])
def test_nonpublic_or_mixed_dns_is_not_connected_or_external_proof(addresses):
    c=fixture(addresses=addresses)
    report=c.api.check_ingress('https://example.org/team/',**c.options)
    assert report['status']=='non_public_target'
    assert [call[0] for call in c.calls]==['dns']
    assert report['tcp443']['status']==report['tls']['status']=='unknown'
    assert report['external_reachability']['status']=='unknown'


def test_explicit_localhost_and_private_ip_skip_dns_and_connect():
    for url in ('https://localhost/team/','https://foo.localhost/','https://127.0.0.1/','https://[::1]/'):
        c=fixture()
        report=c.api.check_ingress(url,**c.options)
        assert report['status']=='non_public_target' and c.calls==[]


def test_verified_local_tcp_tls_http_are_separate_and_never_external_proof():
    c=fixture()
    report=c.api.check_ingress('https://Example.org:443/team/',**c.options)
    assert report['status']=='local_observation_complete'
    assert report['dns']['status']==report['tcp443']['status']==report['tls']['status']=='verified'
    assert report['http']['status']=='observed' and report['http']['status_code']==200
    assert report['tls']['protocol']=='TLSv1.3'
    for key in ('external_reachability','cgnat','static_wan_ip','nat_permissions','mobile_network','max_callback'):
        assert report[key]['status']=='unknown'
    assert c.calls[0]==('dns','example.org',3.0)
    assert c.calls[1][:3]==('tcp','8.8.8.8',443) and c.calls[2]==('tls','example.org')
    request=next(call[1] for call in c.calls if call[0]=='send')
    assert request.startswith(b'HEAD /team/ HTTP/1.1\r\nHost: example.org\r\n')
    assert b'Authorization' not in request and b'Cookie' not in request
    assert c.stream.remaining==b'PRIVATE_BODY' and c.stream.closed
    assert 'PRIVATE_BODY' not in json.dumps(report)


def test_redirect_is_observed_without_following_or_exposing_location():
    c=fixture(response=b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/PRIVATE_TOKEN\r\n\r\n')
    report=c.api.check_ingress('https://example.org/',**c.options)
    assert report['http']['status_code']==302
    assert len([x for x in c.calls if x[0]=='tcp'])==1
    assert len([x for x in c.calls if x[0]=='send'])==1
    assert 'PRIVATE_TOKEN' not in json.dumps(report)


def test_tls_failure_preserves_only_verified_tcp_fact_and_closes_socket():
    c=fixture()
    def fail(*args,**kwargs):
        raise ssl.SSLCertVerificationError('PRIVATE_CERT_DETAIL')
    c.context.wrap_socket=fail
    report=c.api.check_ingress('https://example.org/',**c.options)
    assert report['status']=='probe_failed' and report['tcp443']['status']=='verified'
    assert report['tls']['status']=='failed' and report['http']['status']=='unknown'
    assert c.stream.closed and 'PRIVATE_CERT_DETAIL' not in json.dumps(report)


def test_peer_mismatch_and_unsafe_tls_context_never_send_head():
    for scenario in ('peer','tls'):
        c=fixture()
        if scenario=='peer':
            c.stream.getpeername=lambda:('127.0.0.1',443)
        else:
            c.context.check_hostname=False
        report=c.api.check_ingress('https://example.org/',**c.options)
        assert report['status']=='probe_failed' and not any(x[0]=='send' for x in c.calls)
        assert c.stream.closed


@pytest.mark.parametrize('response',[
    b'HTTP/1.1 200 OK\r\nX-Large: '+b'A'*17000,
    b'HTTP/1.1 200 OK\n\n', b'garbage\r\n\r\n',b'HTTP/1.1 101 Upgrade\r\n\r\n',
])
def test_response_headers_are_bounded_and_malformed_response_not_success(response):
    c=fixture(response=response)
    report=c.api.check_ingress('https://example.org/',**c.options)
    assert report['status']=='probe_failed' and report['http']['status']=='failed'
    assert sum(x[1] for x in c.calls if x[0]=='recv')<=16385
    assert c.stream.closed


def test_deadline_and_dns_failures_return_redacted_unknowns():
    c=fixture()
    def failed(*args):
        raise TimeoutError('PRIVATE_DNS_DETAILS')
    c.options['resolver']=failed
    report=c.api.check_ingress('https://example.org/',**c.options)
    assert report['dns']['status']=='failed' and not c.calls
    assert 'PRIVATE' not in json.dumps(report)
    c=fixture()
    ticks=iter([0.0,0.0,100.0])
    c.options['monotonic']=lambda:next(ticks,100.0)
    report=c.api.check_ingress('https://example.org/',**c.options)
    assert report['status']=='probe_failed' and not any(x[0]=='tcp' for x in c.calls)


def test_read_only_flag_is_mandatory_and_ps_wrapper_has_no_target_defaults():
    c=fixture()
    c.options['read_only']=False
    report=c.api.check_ingress('https://example.org/',**c.options)
    assert report['status']=='invalid_input' and report['error_code']=='read_only_required' and not c.calls
    wrapper=(ROOT/'scripts/team/check_ingress.ps1').read_text(encoding='utf-8')
    assert '[switch]$ReadOnly' in wrapper and '[string]$PublicUrl' in wrapper
    assert '15000' in wrapper and 'check_ingress.py' in wrapper
    for forbidden in ('Invoke-WebRequest','Resolve-DnsName','Test-NetConnection','Get-NetRoute',
                      'New-NetFirewallRule','Set-DnsClient','Stop-Process','Win32_Process','.env'):
        assert forbidden not in wrapper


def test_internal_resolver_mode_also_requires_explicit_read_only(monkeypatch,capsys):
    api=module()
    def forbidden(*args):
        pytest.fail('resolver mode without read-only must not do DNS')
    monkeypatch.setattr(api,'_resolve_child',forbidden)
    assert api.main(['--resolve','example.org'])==2
    report=json.loads(capsys.readouterr().out)
    assert report['error_code']=='read_only_required' and report['target'] is None


def test_dns_timeout_kills_only_exact_owned_child(monkeypatch):
    api=module()
    calls=[]
    class Child:
        returncode=0
        def communicate(self,*,timeout):
            calls.append(('wait',timeout))
            if timeout==3:
                raise api.subprocess.TimeoutExpired('SYNTHETIC_OWNED_HELPER',timeout)
            return b'',None
        def kill(self):
            calls.append(('kill_owned',))
    def start(command,**kwargs):
        calls.append(('start',command,kwargs))
        return Child()
    monkeypatch.setattr(api.subprocess,'Popen',start)
    with pytest.raises(TimeoutError):
        api._resolve('example.org',3)
    command=calls[0][1]
    assert '--read-only' in command and command[-2:]==['--resolve','example.org']
    assert calls[1:]==[('wait',3),('kill_owned',),('wait',1)]


def test_cli_parse_errors_and_oversized_stdin_are_redacted(monkeypatch,capsys):
    from io import StringIO
    api=module()
    assert api.main(['--read-only','--PRIVATE_TOKEN=secret'])==2
    output=capsys.readouterr()
    assert output.err=='' and 'PRIVATE' not in output.out
    assert json.loads(output.out)['error_code']=='arguments_invalid'
    monkeypatch.setattr(api.sys,'stdin',StringIO('PRIVATE'*2000))
    assert api.main(['--read-only','--stdin-url'])==2
    output=capsys.readouterr()
    assert 'PRIVATE' not in output.out and json.loads(output.out)['status']=='invalid_input'
