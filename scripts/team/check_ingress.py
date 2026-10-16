"""Read-only, explicitly targeted ingress observations, never external proof.

No project configuration, credentials, databases, local listeners or routes are
inspected. The only network operation is an explicit public HTTPS:443 target.
The PowerShell entry point owns a 15-second process deadline. DNS is additionally
isolated in a short-lived owned child; all network phases share a 10-second budget.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

MAX_HEADERS = 16 * 1024
MAX_ADDRESSES = 16


def _target(value):
    if (not isinstance(value,str) or not 1 <= len(value) <= 2048 or not value.isascii()
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
            or any(c in value for c in ('%', '\\', '?', '#', '@', '"', "'"))):
        raise ValueError
    parsed = urlsplit(value)
    host = parsed.hostname
    if parsed.scheme != 'https' or not host or parsed.port not in (None,443) or host.endswith('.'):
        raise ValueError
    try:
        address = ipaddress.ip_address(host)
        host = str(address)
    except ValueError:
        address = None
        if (len(host)>253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label)
                                    for label in host.split('.'))
                or re.fullmatch(r'[0-9.]+',host)):
            raise ValueError from None
    authority = '['+host+']' if address is not None and address.version==6 else host
    if parsed.netloc.lower() not in (authority,authority+':443'):
        raise ValueError
    path = parsed.path or '/'
    if (len(path)>1024 or not re.fullmatch(r'/[A-Za-z0-9/_.~-]*',path)
            or '//' in path or any(part in ('.','..') for part in path.split('/'))):
        raise ValueError
    origin = 'https://'+authority
    return {'url':origin+path,'origin':origin,'host':host,'port':443,'path':path}, address


def _public(address):
    return (address.is_global and not any((address.is_loopback,address.is_link_local,
            address.is_multicast,address.is_unspecified,address.is_reserved,address.is_private))
        and not (address.version==6 and (address.ipv4_mapped or address.sixtofour or address.teredo)))


def _resolve_child(host):
    # Internal child mode only resolves one validated hostname, never scans routes.
    target,_ = _target('https://'+host+'/')
    result = []
    for item in socket.getaddrinfo(target['host'],443,type=socket.SOCK_STREAM):
        value = str(ipaddress.ip_address(item[4][0]))
        if value not in result:
            result.append(value)
        if len(result)>MAX_ADDRESSES:
            raise ValueError
    if not result:
        raise ValueError
    return result


def _resolve(host,timeout):
    # Only this Popen instance is killed on timeout; no PID discovery or foreign
    # process interference. The child emits bounded IPs or a fixed error object.
    child = subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'--read-only','--resolve',host],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        output,_ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate(timeout=1)
        raise TimeoutError from None
    if child.returncode or len(output)>2048:
        raise ValueError
    value = json.loads(output)
    if not isinstance(value,list):
        raise ValueError
    return value


def _connect(address,port,timeout):
    parsed = ipaddress.ip_address(address)
    result = socket.socket(socket.AF_INET if parsed.version==4 else socket.AF_INET6,socket.SOCK_STREAM)
    try:
        result.settimeout(timeout)
        # Numeric sockaddr: no second DNS lookup between classification and use.
        result.connect((address,port) if parsed.version==4 else (address,port,0,0))
        return result
    except BaseException:
        result.close()
        raise


def _tls_context():
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _report(clock,read_only):
    observed = clock()
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError('ingress_clock_invalid')
    result = {'schema_version':1,'observed_at':observed.astimezone(timezone.utc).isoformat(),
        'read_only':read_only,'vantage':'current_host','status':'configuration_required',
        'target':None,'error_code':None,
        'limitations':['current_host_observation_is_not_external_reachability',
            'http_status_is_not_gateway_route_or_webhook_verification',
            'no_dns_router_firewall_trust_account_or_runtime_changes'],
        'owner_checks':['choose_explicit_public_https_url','verify_from_another_network',
            'confirm_router_wan_cgnat_and_nat_permissions','verify_max_callback_roundtrip']}
    for key in ('dns','tcp443','tls','http','external_reachability','cgnat',
                'static_wan_ip','nat_permissions','mobile_network','max_callback'):
        result[key] = {'status':'unknown','code':'not_probed' if key in ('dns','tcp443','tls','http') else 'requires_separate_evidence'}
    return result


def check_ingress(public_url=None,*,read_only=False,resolver=None,connector=None,
                  tls_factory=None,clock=None,monotonic=None):
    """Return a sanitized report; injected boundaries exist for offline tests."""
    report = _report(clock or (lambda:datetime.now(timezone.utc)),read_only is True)
    if read_only is not True:
        report.update(status='invalid_input',error_code='read_only_required')
        return report
    if public_url is None or public_url=='':
        return report
    try:
        target,literal = _target(public_url)
    except Exception:
        report.update(status='invalid_input',error_code='public_url_invalid')
        return report
    report['target'] = target
    host = target['host']
    if host=='localhost' or host.endswith('.localhost') or literal is not None and not _public(literal):
        report.update(status='non_public_target',error_code='public_target_required')
        report['dns'] = {'status':'not_needed','code':'non_public_target'}
        return report
    resolver,connector,tls_factory = resolver or _resolve,connector or _connect,tls_factory or _tls_context
    monotonic = monotonic or time.monotonic
    deadline = monotonic()+10.0
    def remaining(cap):
        value = min(cap,deadline-monotonic())
        if value<=0:
            raise TimeoutError
        return value
    try:
        raw = [str(literal)] if literal is not None else resolver(host,remaining(3.0))
        if not isinstance(raw,(tuple,list)) or not 1<=len(raw)<=MAX_ADDRESSES:
            raise ValueError
        addresses = []
        for item in raw:
            if not isinstance(item,str) or '%' in item:
                raise ValueError
            address = ipaddress.ip_address(item)
            if address not in addresses:
                addresses.append(address)
        if not all(_public(item) for item in addresses):
            report.update(status='non_public_target',error_code='public_target_required')
            report['dns'] = {'status':'verified','code':'non_public_or_mixed_addresses','address_count':len(addresses)}
            return report
        report['dns'] = {'status':'verified','code':'literal_public_ip' if literal is not None else 'public_addresses',
                         'addresses':[str(item) for item in addresses]}
    except Exception:
        report.update(status='probe_failed',error_code='dns_unavailable')
        report['dns'] = {'status':'failed','code':'dns_unavailable'}
        return report
    raw_socket = tls_socket = None
    phase = 'tcp443'
    try:
        pinned = addresses[0]
        raw_socket = connector(str(pinned),443,remaining(3.0))
        if ipaddress.ip_address(raw_socket.getpeername()[0]) != pinned:
            raise ValueError
        report['tcp443'] = {'status':'verified','code':'connected_from_current_host','address':str(pinned)}
        phase = 'tls'
        context = tls_factory()
        if not context.check_hostname or context.verify_mode!=ssl.CERT_REQUIRED or context.minimum_version<ssl.TLSVersion.TLSv1_2:
            raise ValueError
        raw_socket.settimeout(remaining(3.0))
        tls_socket = context.wrap_socket(raw_socket,server_hostname=host)
        protocol = tls_socket.version()
        if protocol not in ('TLSv1.2','TLSv1.3'):
            raise ValueError
        report['tls'] = {'status':'verified','code':'default_trust_chain_and_hostname_verified','protocol':protocol}
        phase = 'http'
        authority = target['origin'].removeprefix('https://')
        request = (f"HEAD {target['path']} HTTP/1.1\r\nHost: {authority}\r\n"
            'User-Agent: Secretary-Ingress-Check/1\r\nAccept: */*\r\nConnection: close\r\n\r\n').encode('ascii')
        tls_socket.settimeout(remaining(2.0))
        tls_socket.sendall(request)
        headers = bytearray()
        while not headers.endswith(b'\r\n\r\n'):
            tls_socket.settimeout(remaining(2.0))
            item = tls_socket.recv(1)  # Stop exactly at the header boundary; never read a body.
            if not item or len(headers)+len(item)>MAX_HEADERS:
                raise ValueError
            headers.extend(item)
        lines = bytes(headers[:-4]).split(b'\r\n')
        match = re.fullmatch(rb'HTTP/1\.[01] ([2-5][0-9]{2})(?: [\x20-\x7e]*)?',lines[0])
        if match is None or any(not re.fullmatch(rb'[!#$%&\x27*+.^_`|~A-Za-z0-9-]+:[\t\x20-\x7e\x80-\xff]*',line)
                                for line in lines[1:]):
            raise ValueError
        report['http'] = {'status':'observed','code':'head_response_no_redirect_followed','status_code':int(match[1])}
        report['status'] = 'local_observation_complete'
    except Exception:
        report.update(status='probe_failed',error_code=phase+'_unavailable')
        report[phase] = {'status':'failed','code':phase+'_unavailable'}
    finally:
        for stream in (tls_socket,raw_socket):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
    return report


class _Parser(argparse.ArgumentParser):
    def error(self,message):
        raise ValueError('arguments_invalid')  # Never echo caller arguments.


def main(arguments=None):
    parser = _Parser(description='Read-only current-host ingress observations')
    parser.add_argument('--read-only',action='store_true')
    parser.add_argument('--public-url')
    parser.add_argument('--stdin-url',action='store_true')
    parser.add_argument('--resolve',help=argparse.SUPPRESS)
    try:
        options = parser.parse_args(arguments)
    except Exception:
        report = _report(lambda:datetime.now(timezone.utc),False)
        report.update(status='invalid_input',error_code='arguments_invalid')
        print(json.dumps(report,separators=(',',':')))
        return 2
    if options.resolve is not None:
        if not options.read_only or options.public_url is not None or options.stdin_url:
            report = _report(lambda:datetime.now(timezone.utc),options.read_only)
            report.update(status='invalid_input',error_code='read_only_required' if not options.read_only else 'arguments_invalid')
            print(json.dumps(report,separators=(',',':')))
            return 2
        try:
            print(json.dumps(_resolve_child(options.resolve)))
            return 0
        except Exception:
            print('{"error_code":"dns_unavailable"}')
            return 1
    if options.stdin_url:
        # JSON stdin avoids exposing even the public target in a child command line.
        try:
            payload = sys.stdin.read(8193)
            if len(payload)>8192 or options.public_url is not None:
                raise ValueError
            options.public_url = json.loads(payload)
        except Exception:
            report = _report(lambda:datetime.now(timezone.utc),options.read_only)
            report.update(status='invalid_input',error_code='public_url_invalid')
            print(json.dumps(report,ensure_ascii=True,separators=(',',':')))
            return 2
    report = check_ingress(options.public_url,read_only=options.read_only)
    print(json.dumps(report,ensure_ascii=True,separators=(',',':')))
    return 2 if report['status']=='invalid_input' else 0


if __name__=='__main__':
    raise SystemExit(main())
