"""Bounded MAX media download and CPU normalization. No bot token or ASR.

The caller must authorize the current actor/claim before entry. CDN hosts have
no defaults: real audio-file and native-voice payloads need separate owner probes.
HTTPX/httpcore internals below are deliberately checked before each download.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4
import wave

import httpcore
from httpcore._backends.sync import SyncBackend
import httpx

from secretary.domain.bot import BotEvent


MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_FRAMES = 120 * 16000
MAX_WAV_BYTES = 121 * 32000 + 65536
DOWNLOAD_SECONDS = 30
PROCESS_SECONDS = 30
FORMATS = ('wav', 'mp3', 'ogg', 'flac', 'aac', 'mov', 'matroska', 'webm')
FORMAT_NAMES = frozenset((*FORMATS, 'mp4', 'm4a', '3gp', '3g2', 'mj2'))
CODECS = ('pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_u8', 'pcm_f32le',
          'mp3', 'mp3float', 'aac', 'aac_fixed', 'opus', 'vorbis', 'flac')
_CODES = frozenset({
    'max_media_configuration_required', 'max_media_configuration_invalid', 'max_media_transport_unsafe',
    'max_media_voice_required', 'max_media_url_forbidden', 'max_media_address_forbidden',
    'max_media_dns_failed', 'max_media_timeout', 'max_media_cancelled', 'max_media_download_failed',
    'max_media_http_rejected', 'max_media_encoding_invalid', 'max_media_too_large', 'max_media_invalid',
    'max_media_duration_exceeded', 'max_media_process_failed', 'max_media_process_output_invalid',
    'max_media_storage_unsafe', 'max_media_busy',
})
_MEDIA_LOG_CONTEXT = ContextVar('secretary_max_media_logs', default=False)


class MaxMediaError(ValueError):
    def __init__(self, code):
        self.code = code if code in _CODES else 'max_media_invalid'
        super().__init__(self.code)


@dataclass(frozen=True)
class ValidatedAudio:
    path: Path = field(repr=False)
    duration_ms: int
    sha256: str
    byte_size: int
    command_id: str

    def __post_init__(self):
        if (not isinstance(self.path, Path) or not self.path.is_absolute()
                or type(self.duration_ms) is not int or not 1 <= self.duration_ms <= 120000
                or type(self.byte_size) is not int or not 44 <= self.byte_size <= MAX_WAV_BYTES
                or not isinstance(self.sha256, str) or not re.fullmatch('[0-9a-f]{64}', self.sha256)):
            raise MaxMediaError('max_media_invalid')
        try:
            if not isinstance(self.command_id, str) or str(UUID(self.command_id)) != self.command_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise MaxMediaError('max_media_invalid') from None


@dataclass(frozen=True)
class MediaProcessResult:
    returncode: int
    stdout: bytes = field(repr=False)


class _MediaLogFilter(logging.Filter):
    def filter(self, record):
        if _MEDIA_LOG_CONTEXT.get():
            # Wire libraries log their own URL/headers even without app hooks.
            # ContextVar confines redaction to this call, including failures.
            record.msg, record.args = 'max_media_transport_event', ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


_LOG_FILTER = _MediaLogFilter()


def _install_log_filter():
    for name in ('httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2', 'httpcore.proxy', 'httpcore.socks'):
        logger = logging.getLogger(name)
        if _LOG_FILTER not in logger.filters:
            logger.addFilter(_LOG_FILTER)


def _check_cancel(cancelled):
    if cancelled is not None and cancelled():
        raise MaxMediaError('max_media_cancelled')


def _remaining(deadline, cancelled=None):
    _check_cancel(cancelled)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MaxMediaError('max_media_timeout')
    return remaining


def _public_ip(value):
    try:
        if not isinstance(value, str) or '%' in value:
            raise ValueError
        address = ipaddress.ip_address(value)
        if (not address.is_global or address.is_multicast or address.is_unspecified or address.is_reserved
                or address.is_loopback or address.is_link_local
                or isinstance(address, ipaddress.IPv6Address) and (
                    address.is_site_local or address.ipv4_mapped or address.sixtofour or address.teredo)):
            raise ValueError
        return address.compressed
    except ValueError:
        raise MaxMediaError('max_media_address_forbidden') from None


class _DeadlineStream:
    def __init__(self, stream, deadline, cancelled):
        self.stream, self.deadline, self.cancelled = stream, deadline, cancelled

    def _timeout(self, timeout):
        return min(timeout if timeout is not None else 10, _remaining(self.deadline, self.cancelled))

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout=self._timeout(timeout))

    def write(self, buffer, timeout=None):
        return self.stream.write(buffer, timeout=self._timeout(timeout))

    def close(self):
        return self.stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _DeadlineStream(self.stream.start_tls(ssl_context, server_hostname=server_hostname,
            timeout=self._timeout(timeout)), self.deadline, self.cancelled)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)


class _PinnedBackend:
    def __init__(self, backend, address, deadline, cancelled):
        self.backend, self.address, self.deadline, self.cancelled = backend, address, deadline, cancelled

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.address or port != 443 or local_address is not None:
            raise MaxMediaError('max_media_address_forbidden')
        stream = self.backend.connect_tcp(host, port,
            timeout=min(timeout or 10, _remaining(self.deadline, self.cancelled)), socket_options=socket_options)
        peer = stream.get_extra_info('server_addr')
        if peer is not None and _public_ip(peer[0]) != self.address:
            stream.close()
            raise MaxMediaError('max_media_address_forbidden')
        return _DeadlineStream(stream, self.deadline, self.cancelled)

    def connect_unix_socket(self, *args, **kwargs):
        raise MaxMediaError('max_media_address_forbidden')


def _system_resolver(host):
    return tuple({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def run_media_process(argv, *, cwd, timeout, max_output_bytes, cancelled=None):
    """Bounded CPU process; no shell, device input, raw stderr or unbounded capture."""
    _check_cancel(cancelled)
    process = None
    data = bytearray()
    overflow = threading.Event()
    reader = None
    try:
        environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
        environment.update(TEMP=str(cwd), TMP=str(cwd), AV_LOG_FORCE_NOCOLOR='1')
        process = subprocess.Popen(list(argv), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False, env=environment,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def read_output():
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                if len(data) + len(chunk) > max_output_bytes:
                    overflow.set()
                    break
                data.extend(chunk)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            _remaining(deadline, cancelled)
            if overflow.is_set():
                raise MaxMediaError('max_media_process_output_invalid')
            time.sleep(.02)
        reader.join(timeout=1)
        if reader.is_alive() or overflow.is_set():
            raise MaxMediaError('max_media_process_output_invalid')
        _check_cancel(cancelled)
        return MediaProcessResult(process.returncode, bytes(data))
    except MaxMediaError:
        raise
    except Exception:
        raise MaxMediaError('max_media_process_failed') from None
    finally:
        if process is not None:
            cleanup_failed = False
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                cleanup_failed = True
            try:
                process.wait(timeout=5)
            except Exception:
                cleanup_failed = True
            try:
                if reader is not None:
                    reader.join(timeout=1)
                if process.stdout is not None and (reader is None or not reader.is_alive()):
                    process.stdout.close()
                elif reader is not None and reader.is_alive():
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
            if cleanup_failed:
                raise MaxMediaError('max_media_process_failed') from None


def _no_reparse(path):
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise MaxMediaError('max_media_storage_unsafe')


def _local_path(value):
    try:
        path = Path(value)
        # Refuse UNC/device paths before stat/resolve can access a remote share.
        if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
            raise ValueError
        return path
    except (TypeError, ValueError):
        raise MaxMediaError('max_media_configuration_invalid') from None


class MAXMediaClient:
    def __init__(self, *, allowlisted_hosts=(), client, ffprobe_path, ffmpeg_path, scratch_dir,
                 resolver=None, process_runner=None, clock=None):
        self.allowlisted_hosts, self.client = allowlisted_hosts, client
        self.resolver = resolver or _system_resolver
        self.process_runner = process_runner or run_media_process
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ffprobe_path, self.ffmpeg_path = self._executable(ffprobe_path, 'ffprobe'), self._executable(ffmpeg_path, 'ffmpeg')
        self.scratch_dir = _local_path(scratch_dir)
        _no_reparse(self.scratch_dir)
        self.scratch_dir = self.scratch_dir.resolve()
        self._lock = threading.Lock()
        self._dns_thread = None
        self._check_hosts()
        self._check_client()
        _install_log_filter()

    @staticmethod
    def _executable(value, name):
        path = _local_path(value)
        if not path.is_file() or path.name.lower() not in {name, name + '.exe'}:
            raise MaxMediaError('max_media_configuration_invalid')
        return path.resolve()

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise MaxMediaError('max_media_configuration_invalid')
        return value.astimezone(timezone.utc)

    def _check_hosts(self):
        hosts = self.allowlisted_hosts
        if (not isinstance(hosts, tuple) or len(hosts) > 32 or len(set(hosts)) != len(hosts)
                or any(not isinstance(host, str) or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host)
                       or host.endswith('.localhost') for host in hosts)):
            raise MaxMediaError('max_media_configuration_invalid')

    def _check_client(self):
        client = self.client
        if type(client) is not httpx.Client:
            raise MaxMediaError('max_media_transport_unsafe')
        timeout = client.timeout
        mounts = getattr(client, '_mounts', None)
        if (client.is_closed or client.trust_env or client.follow_redirects or client.auth is not None
                or client.params or len(client.cookies) or any(client.event_hooks.values())
                or mounts is None or any(value is not None for value in mounts.values())
                or str(client.base_url) != ''
                or any(header.lower() not in ('accept', 'accept-encoding', 'connection', 'user-agent') for header in client.headers)
                or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 10
                    for value in (timeout.connect, timeout.read, timeout.write, timeout.pool))):
            raise MaxMediaError('max_media_transport_unsafe')
        transport = client._transport
        if type(transport) is httpx.MockTransport:
            return  # Trusted offline fixture only; no native TLS claim.
        pool = getattr(transport, '_pool', None)
        context = getattr(pool, '_ssl_context', None)
        if (type(transport) is not httpx.HTTPTransport or type(pool) is not httpcore.ConnectionPool
                or pool._retries != 0 or pool._http2 or not pool._http1 or pool._max_keepalive_connections != 0
                or pool._proxy is not None or pool._uds is not None or pool._local_address is not None
                or type(pool._network_backend) is not SyncBackend
                or not isinstance(context, ssl.SSLContext) or context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname):
            raise MaxMediaError('max_media_transport_unsafe')

    def _url(self, event):
        self._check_hosts()
        if not self.allowlisted_hosts:
            raise MaxMediaError('max_media_configuration_required')
        if (not isinstance(event, BotEvent) or not event.actor_id or event.actor_revision is None
                or not event.project_ids or event.quarantine_reason or not event.user_id
                or event.kind != 'message_created' or not isinstance(event.media, tuple) or len(event.media) != 1):
            raise MaxMediaError('max_media_voice_required')
        item = event.media[0]
        if not isinstance(item, dict) or item.get('type') not in {'audio', 'file'}:
            raise MaxMediaError('max_media_voice_required')
        raw = item.get('url')
        try:
            if (not isinstance(raw, str) or not 1 <= len(raw) <= 8192
                    or any(ord(char) <= 32 or ord(char) == 127 for char in raw) or '\\' in raw or '#' in raw):
                raise ValueError
            parsed, url = urlsplit(raw), httpx.URL(raw)
            if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                    or parsed.hostname not in self.allowlisted_hosts or parsed.port not in (None, 443)
                    or parsed.netloc not in {parsed.hostname, parsed.hostname + ':443'}
                    or url.host != parsed.hostname):
                raise ValueError
            return url, parsed.hostname
        except (ValueError, TypeError, httpx.InvalidURL):
            raise MaxMediaError('max_media_url_forbidden') from None

    def _resolve(self, host, deadline, cancelled):
        if self._dns_thread is not None and self._dns_thread.is_alive():
            raise MaxMediaError('max_media_dns_failed')
        answer = queue.Queue(maxsize=1)
        def lookup():
            try:
                answer.put((True, self.resolver(host)))
            except Exception:
                answer.put((False, None))
        self._dns_thread = threading.Thread(target=lookup, daemon=True)
        self._dns_thread.start()
        end = min(deadline, time.monotonic() + 5)
        while self._dns_thread.is_alive():
            _remaining(end, cancelled)
            self._dns_thread.join(timeout=.02)
        ok, result = answer.get_nowait()
        if not ok or not isinstance(result, (tuple, list, set)) or not 1 <= len(result) <= 32:
            raise MaxMediaError('max_media_dns_failed')
        # Reject mixed public/private answers, then use one validated literal.
        addresses = sorted({_public_ip(address) for address in result})
        return addresses[0]

    def _job(self, command_id):
        _no_reparse(self.scratch_dir)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        directory = self.scratch_dir / ('voice-' + uuid4().hex)
        directory.mkdir()
        try:
            self._manifest(directory, {'version': 1, 'command_id': command_id, 'created_at': self._now().timestamp(), 'state': 'processing'})
        except BaseException:
            try:
                self._remove(directory)
            except (OSError, MaxMediaError):
                pass
            raise
        return directory

    def _check_job(self, directory):
        _no_reparse(self.scratch_dir)
        _no_reparse(directory)
        if (directory.parent != self.scratch_dir or directory.resolve().parent != self.scratch_dir
                or not re.fullmatch(r'voice-[0-9a-f]{32}', directory.name) or not directory.is_dir()):
            raise MaxMediaError('max_media_storage_unsafe')
        for child in directory.iterdir():
            _no_reparse(child)
            if not child.is_file():
                raise MaxMediaError('max_media_storage_unsafe')

    def _manifest(self, directory, value):
        self._check_job(directory)
        (directory / 'manifest.json').write_text(json.dumps(value, separators=(',', ':')), encoding='utf-8')

    def _remove(self, directory):
        if not directory.exists():
            return
        self._check_job(directory)
        # Flat, verified, adapter-created directory only. Never recursive-delete
        # a computed path or traverse a Windows junction/symlink.
        children = tuple(directory.iterdir())
        for child in children:
            child.unlink()
        directory.rmdir()

    def _download(self, url, host, destination, cancelled):
        self._check_client()
        deadline = time.monotonic() + DOWNLOAD_SECONDS
        address = self._resolve(host, deadline, cancelled)
        pinned = url.copy_with(host=address, port=None)
        request = httpx.Request('GET', pinned, headers={'Host': host, 'Accept-Encoding': 'identity',
            'Connection': 'close', 'Accept': 'audio/*,application/octet-stream'}, extensions={
                'sni_hostname': host, 'timeout': {key: min(5, _remaining(deadline, cancelled))
                    for key in ('connect', 'read', 'write', 'pool')}})
        pool = getattr(self.client._transport, '_pool', None)
        backend = pool._network_backend if pool is not None else None
        response = None
        context = _MEDIA_LOG_CONTEXT.set(True)
        try:
            if pool is not None:
                pool._network_backend = _PinnedBackend(backend, address, deadline, cancelled)
            # Direct verified transport avoids client URL-info logging and
            # automatic cookie extraction. No mounts, auth or redirects apply.
            response = self.client._transport.handle_request(request)
            response.request = request
            _remaining(deadline, cancelled)
            if response.status_code != 200:
                raise MaxMediaError('max_media_http_rejected')
            if response.headers.get('content-encoding', '').lower().strip() not in ('', 'identity'):
                raise MaxMediaError('max_media_encoding_invalid')
            mime = response.headers.get('content-type', '').split(';')[0].lower().strip()
            if mime and not (mime.startswith('audio/') or mime in {'application/octet-stream', 'application/ogg', 'video/mp4'}):
                raise MaxMediaError('max_media_invalid')
            length = response.headers.get('content-length')
            if length is not None and (not re.fullmatch(r'[0-9]{1,20}', length) or int(length) > MAX_SOURCE_BYTES):
                raise MaxMediaError('max_media_too_large')
            size = 0
            with destination.open('xb') as target:
                for chunk in response.iter_bytes():
                    _remaining(deadline, cancelled)
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise MaxMediaError('max_media_too_large')
                    target.write(chunk)
            if size == 0 or length is not None and size != int(length):
                raise MaxMediaError('max_media_invalid')
        except MaxMediaError:
            raise
        except (httpx.TimeoutException, httpcore.TimeoutException):
            raise MaxMediaError('max_media_timeout') from None
        except Exception:
            raise MaxMediaError('max_media_download_failed') from None
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                raise MaxMediaError('max_media_download_failed') from None
            finally:
                if pool is not None:
                    pool._network_backend = backend
                _MEDIA_LOG_CONTEXT.reset(context)

    def _process(self, argv, directory, cancelled):
        _check_cancel(cancelled)
        result = self.process_runner(tuple(str(item) for item in argv), cwd=directory,
            timeout=PROCESS_SECONDS, max_output_bytes=65536, cancelled=cancelled)
        _check_cancel(cancelled)
        if (not isinstance(result, MediaProcessResult) or type(result.returncode) is not int
                or not isinstance(result.stdout, bytes) or len(result.stdout) > 65536):
            raise MaxMediaError('max_media_process_output_invalid')
        if result.returncode != 0:
            raise MaxMediaError('max_media_process_failed')
        return result.stdout

    def _normalize(self, directory, command_id, cancelled):
        source, output = directory / 'source.bin', directory / 'audio.wav'
        restrictions = ['-v', 'error', '-max_alloc', '16777216', '-protocol_whitelist', 'file,pipe',
                        '-format_whitelist', ','.join(FORMATS), '-codec_whitelist', ','.join(CODECS)]
        raw = self._process([self.ffprobe_path, *restrictions, '-show_entries',
            'stream=index,codec_type,codec_name,sample_rate,channels:format=format_name,duration',
            '-of', 'json', str(source)], directory, cancelled)
        try:
            info = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            streams, format_info = info['streams'], info['format']
            if (not isinstance(streams, list) or len(streams) != 1 or streams[0].get('codec_type') != 'audio'
                    or streams[0].get('codec_name') not in CODECS
                    or not 1 <= streams[0]['channels'] <= 8 or not 8000 <= int(streams[0]['sample_rate']) <= 192000
                    or not set(format_info['format_name'].split(',')) <= FORMAT_NAMES):
                raise ValueError
            if format_info.get('duration') is not None:
                duration = Decimal(format_info['duration'])
                if not duration.is_finite() or duration <= 0:
                    raise ValueError
                if duration > 120:
                    raise MaxMediaError('max_media_duration_exceeded')
        except MaxMediaError:
            raise
        except (ValueError, TypeError, KeyError, InvalidOperation, UnicodeError):
            raise MaxMediaError('max_media_invalid') from None
        self._process([self.ffmpeg_path, '-nostdin', '-y', *restrictions, '-hwaccel', 'none', '-threads', '1',
            '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn', '-map_metadata', '-1', '-map_chapters', '-1',
            '-t', '121', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-threads', '1', '-f', 'wav', str(output)], directory, cancelled)
        self._check_job(directory)
        try:
            size = output.stat().st_size
            if not 44 <= size <= MAX_WAV_BYTES:
                raise MaxMediaError('max_media_too_large')
            with wave.open(str(output), 'rb') as audio:
                frames = audio.getnframes()
                if frames > MAX_FRAMES:
                    raise MaxMediaError('max_media_duration_exceeded')
                if (audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000
                        or audio.getcomptype() != 'NONE' or frames <= 0 or len(audio.readframes(frames + 1)) != frames * 2):
                    raise MaxMediaError('max_media_invalid')
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            return ValidatedAudio(output, math.ceil(frames * 1000 / 16000), digest, size, command_id)
        except MaxMediaError:
            raise
        except (OSError, ValueError, wave.Error, EOFError):
            raise MaxMediaError('max_media_invalid') from None

    def download_voice(self, event, *, cancelled=None):
        directory = None
        if not self._lock.acquire(blocking=False):
            raise MaxMediaError('max_media_busy')
        try:
            _check_cancel(cancelled)
            url, host = self._url(event)
            directory = self._job(event.event_id)
            self._download(url, host, directory / 'source.bin', cancelled)
            audio = self._normalize(directory, event.event_id, cancelled)
            _check_cancel(cancelled)
            (directory / 'source.bin').unlink()
            self._manifest(directory, {'version': 1, 'command_id': event.event_id,
                'created_at': self._now().timestamp(), 'state': 'validated', 'sha256': audio.sha256,
                'byte_size': audio.byte_size, 'duration_ms': audio.duration_ms})
            return audio
        except BaseException as error:
            if directory is not None:
                try:
                    self._remove(directory)
                except (OSError, MaxMediaError):
                    pass  # Never expand cleanup outside the verified owned job.
            if isinstance(error, MaxMediaError):
                raise
            if not isinstance(error, Exception):
                raise
            raise MaxMediaError('max_media_invalid') from None
        finally:
            self._lock.release()

    def release(self, audio):
        if not isinstance(audio, ValidatedAudio) or audio.path.name != 'audio.wav':
            raise MaxMediaError('max_media_storage_unsafe')
        directory = audio.path.parent
        if not directory.exists():
            return
        self._check_job(directory)
        try:
            metadata = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
            if metadata['command_id'] != audio.command_id or metadata['state'] != 'validated':
                raise ValueError
        except (ValueError, OSError, KeyError):
            raise MaxMediaError('max_media_storage_unsafe') from None
        self._remove(directory)

    def cleanup_expired(self, *, now=None):
        if not self.scratch_dir.exists():
            return 0
        _no_reparse(self.scratch_dir)
        stamp = self._now() if now is None else now
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            raise MaxMediaError('max_media_configuration_invalid')
        count = 0
        for directory in self.scratch_dir.iterdir():
            if not re.fullmatch(r'voice-[0-9a-f]{32}', directory.name):
                continue
            self._check_job(directory)
            try:
                raw = (directory / 'manifest.json').read_bytes()
                if len(raw) > 4096:
                    raise ValueError
                metadata = json.loads(raw)
                if (metadata['version'] != 1 or metadata['state'] not in {'processing', 'validated'}
                        or type(metadata['created_at']) not in (int, float) or not math.isfinite(metadata['created_at'])):
                    raise ValueError
                expires = metadata['created_at'] + (86400 if metadata['state'] == 'validated' else 3600)
                if stamp.timestamp() >= expires:
                    self._remove(directory)
                    count += 1
            except (ValueError, KeyError, OSError):
                raise MaxMediaError('max_media_storage_unsafe') from None
        return count
