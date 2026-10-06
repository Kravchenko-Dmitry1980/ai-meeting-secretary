"""T8 media boundary: synthetic bytes, DNS fixtures, CPU subprocess fixtures."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module, util
import hashlib
import io
import json
import logging
from pathlib import Path
import ssl
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4
import wave

import httpx
import pytest

from secretary.domain.bot import BotEvent


def module():
    assert util.find_spec('secretary.infrastructure.max_media'), 'T8 media adapter absent'
    return import_module('secretary.infrastructure.max_media')


def wav_bytes(frames=1600, rate=16000, channels=1):
    target = io.BytesIO()
    with wave.open(target, 'wb') as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b'\x00\x00' * frames * channels)
    return target.getvalue()


@pytest.fixture
def media_case(tmp_path):
    a = module()
    clock = [datetime(2026, 10, 4, tzinfo=timezone.utc)]
    calls, processes = [], []
    content = [b'SYNTHETIC_COMPRESSED_AUDIO']
    response_options = [{'headers': {'content-type': 'audio/ogg'}}]
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=content[0], **response_options[0])
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, timeout=5)
    ffprobe, ffmpeg = tmp_path / 'ffprobe.exe', tmp_path / 'ffmpeg.exe'
    ffprobe.write_bytes(b'SYNTHETIC_NO_EXEC')
    ffmpeg.write_bytes(b'SYNTHETIC_NO_EXEC')
    frames = [1600]
    probe = [{'streams': [{'index': 0, 'codec_type': 'audio', 'codec_name': 'opus',
                          'sample_rate': '48000', 'channels': 1}],
              'format': {'format_name': 'ogg', 'duration': '0.1'}}]
    def runner(argv, **options):
        processes.append((argv, options))
        if Path(argv[0]).name == 'ffprobe.exe':
            return a.MediaProcessResult(0, json.dumps(probe[0]).encode())
        Path(argv[-1]).write_bytes(wav_bytes(frames[0]))
        return a.MediaProcessResult(0, b'')
    resolver_calls = []
    def resolver(host):
        resolver_calls.append(host)
        return ['8.8.8.8']
    adapter = a.MAXMediaClient(allowlisted_hosts=('cdn.example.test',), client=client,
        resolver=resolver, ffprobe_path=ffprobe, ffmpeg_path=ffmpeg, scratch_dir=tmp_path / 'media',
        process_runner=runner, clock=lambda: clock[0])
    event = BotEvent(event_id=str(uuid4()), dedup_key='synthetic', bot_id='42', kind='message_created',
        timestamp_ms=int(clock[0].timestamp() * 1000), user_id='222', actor_id=str(uuid4()),
        actor_revision=0, project_ids=('7',), media=({'type': 'audio',
            'url': 'https://cdn.example.test/audio.ogg?signature=SYNTHETIC_SIGNED_SECRET'},))
    return SimpleNamespace(a=a, adapter=adapter, event=event, client=client, calls=calls,
        processes=processes, content=content, response_options=response_options, probe=probe,
        frames=frames, clock=clock, resolver_calls=resolver_calls, tmp=tmp_path,
        ffprobe=ffprobe, ffmpeg=ffmpeg, runner=runner)


def test_download_pins_ip_host_and_sni_and_returns_exact_validated_wav(media_case):
    c = media_case
    audio = c.adapter.download_voice(c.event)
    assert audio.command_id == c.event.event_id and audio.duration_ms == 100
    assert audio.path.read_bytes() == wav_bytes()
    assert audio.byte_size == audio.path.stat().st_size
    assert audio.sha256 == hashlib.sha256(audio.path.read_bytes()).hexdigest()
    assert str(audio.path) not in repr(audio) and 'SIGNED_SECRET' not in repr(audio)
    assert c.resolver_calls == ['cdn.example.test']
    request = c.calls[0]
    assert request.url.host == '8.8.8.8' and request.headers['Host'] == 'cdn.example.test'
    assert request.extensions['sni_hostname'] == 'cdn.example.test'
    assert request.url.query == b'signature=SYNTHETIC_SIGNED_SECRET'
    assert 'authorization' not in request.headers and 'cookie' not in request.headers
    for argv, options in c.processes:
        assert '-protocol_whitelist' in argv and '-format_whitelist' in argv and '-codec_whitelist' in argv
        assert not any('https://' in item or 'SIGNED_SECRET' in item for item in argv)
        assert options['timeout'] <= 30


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1',
    '::ffff:8.8.8.8', 'ff02::1', '0.0.0.0', '100.64.0.1', '192.0.2.1', '2002:0808:0808::1',
    'fec0::1', 'fedf::1', '64:ff9b::7f00:1', '64:ff9b:1::a00:1'])
def test_dns_private_special_and_tunnel_addresses_fail_before_http(media_case, address):
    c = media_case
    c.adapter.resolver = lambda _: [address]
    with pytest.raises(c.a.MaxMediaError, match='max_media_address_forbidden'):
        c.adapter.download_voice(c.event)
    assert c.calls == [] and c.processes == []


@pytest.mark.parametrize('url', ['http://cdn.example.test/a', 'https://user:pw@cdn.example.test/a',
    'https://cdn.example.test.evil.invalid/a', 'https://cdn.example.test:8443/a',
    'https://127.0.0.1/a', 'https://cdn.example.test/a#fragment', 'https://cdn.example.test./a'])
def test_url_requires_exact_https_cdn_without_ambiguous_authority(media_case, url):
    c = media_case
    with pytest.raises(c.a.MaxMediaError):
        c.adapter.download_voice(replace(c.event, media=({'type': 'audio', 'url': url},)))
    assert not c.calls


def test_empty_host_contract_fails_closed_and_multiple_media_is_not_guessed(media_case):
    c = media_case
    c.adapter.allowlisted_hosts = ()
    with pytest.raises(c.a.MaxMediaError, match='max_media_configuration_required'):
        c.adapter.download_voice(c.event)
    c.adapter.allowlisted_hosts = ('cdn.example.test',)
    with pytest.raises(c.a.MaxMediaError, match='max_media_voice_required'):
        c.adapter.download_voice(replace(c.event, media=c.event.media * 2))
    assert not c.calls


def test_size_encoding_redirect_and_error_are_sanitized_and_cleaned(media_case):
    c = media_case
    for content, options in ((b'x' * (10 * 1024 * 1024 + 1), {'headers': {'content-type': 'audio/ogg'}}),
                             (b'x', {'headers': {'content-type': 'audio/ogg', 'content-encoding': 'gzip'}})):
        c.content[0], c.response_options[0] = content, options
        with pytest.raises(c.a.MaxMediaError) as caught:
            c.adapter.download_voice(c.event)
        assert 'SIGNED_SECRET' not in str(caught.value)
        assert not list((c.tmp / 'media').iterdir())


def test_actual_decoded_frames_reject_long_audio_even_when_probe_claims_short(media_case):
    c = media_case
    c.frames[0] = 120 * 16000 + 1
    with pytest.raises(c.a.MaxMediaError, match='max_media_duration_exceeded'):
        c.adapter.download_voice(c.event)
    assert not list((c.tmp / 'media').iterdir())


@pytest.mark.parametrize('change', ['playlist', 'video', 'codec', 'multiple', 'long'])
def test_probe_rejects_unsupported_or_ambiguous_input_before_decode(media_case, change):
    c = media_case
    if change == 'playlist':
        c.probe[0]['format']['format_name'] = 'hls'
    elif change == 'video':
        c.probe[0]['streams'][0]['codec_type'] = 'video'
    elif change == 'codec':
        c.probe[0]['streams'][0]['codec_name'] = 'unknown'
    elif change == 'multiple':
        c.probe[0]['streams'] *= 2
    else:
        c.probe[0]['format']['duration'] = '120.001'
    with pytest.raises(c.a.MaxMediaError):
        c.adapter.download_voice(c.event)
    assert len(c.processes) == 1
    assert not list((c.tmp / 'media').iterdir())


def test_success_retention_release_and_expiry_only_remove_owned_job(media_case):
    c = media_case
    audio = c.adapter.download_voice(c.event)
    unrelated = c.tmp / 'media' / 'unrelated'
    unrelated.mkdir()
    (unrelated / 'keep.txt').write_text('keep')
    c.clock[0] += timedelta(hours=23)
    assert c.adapter.cleanup_expired() == 0 and audio.path.exists()
    c.clock[0] += timedelta(hours=1)
    assert c.adapter.cleanup_expired() == 1 and not audio.path.exists()
    assert (unrelated / 'keep.txt').exists()
    second = c.adapter.download_voice(c.event)
    c.adapter.release(second)
    assert not second.path.exists()
    c.adapter.release(second)


def test_transport_and_exception_logs_never_expose_signed_query(media_case, caplog):
    c = media_case
    def noisy(request):
        logging.getLogger('httpcore.http11').debug('headers %r', {'location': str(request.url)})
        logging.getLogger('httpx').info('request %s', request.url)
        raise RuntimeError(str(request.url))
    c.client._transport = httpx.MockTransport(noisy)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(c.a.MaxMediaError) as caught:
            c.adapter.download_voice(c.event)
    assert 'SIGNED_SECRET' not in caplog.text and 'SIGNED_SECRET' not in str(caught.value)
    assert not list((c.tmp / 'media').iterdir())


def test_cancel_during_processing_reaps_and_cleans_owned_directory(media_case):
    c = media_case
    cancelled = [False]
    def runner(*args, **kwargs):
        cancelled[0] = True
        return c.runner(*args, **kwargs)
    c.adapter.process_runner = runner
    with pytest.raises(c.a.MaxMediaError, match='max_media_cancelled'):
        c.adapter.download_voice(c.event, cancelled=lambda: cancelled[0])
    assert not list((c.tmp / 'media').iterdir())


def test_unknown_actual_stream_length_is_capped_and_redirect_is_never_followed(media_case):
    c = media_case
    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            yield b'x' * (6 * 1024 * 1024)
            yield b'y' * (6 * 1024 * 1024)
    c.client._transport = httpx.MockTransport(lambda _: httpx.Response(200,
        headers={'content-type': 'audio/ogg'}, stream=Oversized()))
    with pytest.raises(c.a.MaxMediaError, match='max_media_too_large'):
        c.adapter.download_voice(c.event)
    seen = []
    def redirect(request):
        seen.append(request)
        return httpx.Response(302, headers={'location': 'http://169.254.169.254/secret'})
    c.client._transport = httpx.MockTransport(redirect)
    with pytest.raises(c.a.MaxMediaError, match='max_media_http_rejected'):
        c.adapter.download_voice(c.event)
    assert len(seen) == 1 and not c.processes
    assert not list((c.tmp / 'media').iterdir())


@pytest.mark.parametrize('mutation', ['cookies', 'headers', 'hooks', 'params', 'auth', 'redirects', 'environment', 'timeout'])
def test_mutated_or_unsafe_client_is_rechecked_before_dispatch(media_case, mutation):
    c = media_case
    if mutation == 'cookies':
        c.client.cookies.set('unsafe', 'synthetic')
    elif mutation == 'headers':
        c.client.headers['Authorization'] = 'SYNTHETIC_MUST_NOT_SEND'
    elif mutation == 'hooks':
        c.client.event_hooks['request'].append(lambda _: None)
    elif mutation == 'params':
        c.client.params = {'unsafe': 'synthetic'}
    elif mutation == 'auth':
        c.client.auth = ('synthetic', 'synthetic')
    elif mutation == 'redirects':
        c.client.follow_redirects = True
    elif mutation == 'environment':
        c.client._trust_env = True
    else:
        c.client.timeout = None
    with pytest.raises(c.a.MaxMediaError, match='max_media_transport_unsafe'):
        c.adapter.download_voice(c.event)
    assert not c.calls


@pytest.mark.parametrize('configuration', ['unverified_tls', 'http2', 'keepalive', 'retry'])
def test_native_transport_requires_verified_tls_and_no_connection_reuse(media_case, configuration):
    c = media_case
    options = {'trust_env': False, 'limits': httpx.Limits(max_keepalive_connections=0)}
    options.update({'unverified_tls': {'verify': False}, 'http2': {'http2': True},
                    'keepalive': {'limits': httpx.Limits(max_keepalive_connections=1)}, 'retry': {'retries': 1}}[configuration])
    with httpx.Client(transport=httpx.HTTPTransport(**options), trust_env=False, timeout=5) as unsafe:
        c.adapter.client = unsafe
        with pytest.raises(c.a.MaxMediaError, match='max_media_transport_unsafe'):
            c.adapter.download_voice(c.event)


def test_dns_deadline_and_mixed_answers_fail_without_connecting(media_case, monkeypatch):
    c = media_case
    release = threading.Event()
    c.adapter.resolver = lambda _: (release.wait(1), ['8.8.8.8'])[1]
    monkeypatch.setattr(c.a, 'DOWNLOAD_SECONDS', .05)
    started = time.monotonic()
    try:
        with pytest.raises(c.a.MaxMediaError, match='max_media_timeout'):
            c.adapter.download_voice(c.event)
        assert time.monotonic() - started < .6 and not c.calls
    finally:
        release.set()
        c.adapter._dns_thread.join(timeout=1)
    c.adapter.resolver = lambda _: ['8.8.8.8', '127.0.0.1']
    with pytest.raises(c.a.MaxMediaError, match='max_media_address_forbidden'):
        c.adapter.download_voice(c.event)


def test_download_total_deadline_is_not_reset_by_trickle_chunks(media_case, monkeypatch):
    c = media_case
    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(20):
                time.sleep(.01)
                yield b'x'
    monkeypatch.setattr(c.a, 'DOWNLOAD_SECONDS', .03)
    c.client._transport = httpx.MockTransport(lambda _: httpx.Response(200,
        headers={'content-type': 'audio/ogg'}, stream=Trickle()))
    with pytest.raises(c.a.MaxMediaError, match='max_media_timeout'):
        c.adapter.download_voice(c.event)
    assert not c.processes and not list((c.tmp / 'media').iterdir())


def test_process_deadline_and_output_cap_kill_only_owned_synthetic_child(tmp_path):
    a = module()
    for script, timeout, output, code in (
            ('import time; time.sleep(2)', .05, 1000, 'max_media_timeout'),
            ('import sys; sys.stdout.write("x"*1000000); sys.stdout.flush()', 2, 1000, 'max_media_process_output_invalid')):
        with pytest.raises(a.MaxMediaError, match=code):
            a.run_media_process((sys.executable, '-c', script), cwd=tmp_path, timeout=timeout,
                                max_output_bytes=output, cancelled=None)


def test_native_cpu_ffprobe_ffmpeg_roundtrip_on_synthetic_wav(media_case):
    c = media_case
    ffmpeg = Path('C:/Users/user/AppData/Local/Microsoft/WinGet/Links/ffmpeg.exe')
    ffprobe = ffmpeg.with_name('ffprobe.exe')
    if not ffmpeg.is_file() or not ffprobe.is_file():
        pytest.skip('Existing explicitly identified FFmpeg runtime unavailable; no install')
    c.adapter.ffmpeg_path, c.adapter.ffprobe_path = ffmpeg.resolve(), ffprobe.resolve()
    c.adapter.process_runner = c.a.run_media_process
    c.content[0] = wav_bytes(frames=4410, rate=44100, channels=2)
    c.response_options[0] = {'headers': {'content-type': 'audio/wav'}}
    audio = c.adapter.download_voice(c.event)
    with wave.open(str(audio.path), 'rb') as value:
        assert value.getframerate() == 16000 and value.getnchannels() == 1
        assert value.getnframes() == 1600
    assert audio.duration_ms == 100


def test_release_rejects_foreign_or_reparse_artifact_without_deleting_target(media_case):
    c = media_case
    audio = c.adapter.download_voice(c.event)
    foreign = c.tmp / 'foreign' / 'audio.wav'
    foreign.parent.mkdir()
    foreign.write_bytes(wav_bytes())
    with pytest.raises(c.a.MaxMediaError, match='max_media_storage_unsafe'):
        c.adapter.release(replace(audio, path=foreign))
    assert foreign.exists() and audio.path.exists()
    link = audio.path.parent / 'escape'
    try:
        link.symlink_to(foreign.parent, target_is_directory=True)
    except OSError:
        pytest.skip('Host does not grant synthetic symlink creation')
    with pytest.raises(c.a.MaxMediaError, match='max_media_storage_unsafe'):
        c.adapter.release(audio)
    assert foreign.exists()


def test_response_close_failure_always_restores_thread_logging_context(media_case, caplog):
    c = media_case
    class BadClose(httpx.SyncByteStream):
        def __iter__(self):
            yield b'SYNTHETIC'
        def close(self):
            raise OSError('SYNTHETIC_SIGNED_SECRET')
    c.client._transport = httpx.MockTransport(lambda _: httpx.Response(302,
        headers={'location': 'https://cdn.example.test/signed'}, stream=BadClose()))
    with caplog.at_level(logging.INFO):
        with pytest.raises(c.a.MaxMediaError):
            c.adapter.download_voice(c.event)
        logging.getLogger('httpx').info('unrelated_log_after_media_failure')
    assert 'unrelated_log_after_media_failure' in caplog.text
    assert 'SIGNED_SECRET' not in caplog.text
    assert not c.a._MEDIA_LOG_CONTEXT.get()


def test_dangling_windows_reparse_is_checked_with_lstat_even_when_target_missing(media_case, monkeypatch):
    c = media_case
    dangling = c.tmp / 'dangling-junction'
    original = Path.lstat
    def lstat(path):
        if path == dangling:
            return SimpleNamespace(st_file_attributes=1024, st_mode=0)
        return original(path)
    monkeypatch.setattr(Path, 'lstat', lstat)
    # Simulates Windows junction: exists follows missing target; is_symlink is
    # false for junctions, but lstat still reports FILE_ATTRIBUTE_REPARSE_POINT.
    monkeypatch.setattr(Path, 'is_symlink', lambda _: False)
    with pytest.raises(c.a.MaxMediaError, match='max_media_storage_unsafe'):
        c.a._no_reparse(dangling)


def test_process_receives_only_local_minimal_environment(tmp_path, monkeypatch):
    a = module()
    seen = []
    class Process:
        stdout = io.BytesIO(b'')
        returncode = 0
        def poll(self):
            return 0
        def wait(self, timeout):
            return 0
    def popen(argv, **options):
        seen.append(options)
        return Process()
    monkeypatch.setattr(a.subprocess, 'Popen', popen)
    monkeypatch.setenv('FFREPORT', 'file=SYNTHETIC_UNAUTHORIZED_LOG')
    monkeypatch.setenv('POLZA_API_KEY', 'SYNTHETIC_MUST_NOT_INHERIT')
    a.run_media_process(('synthetic.exe',), cwd=tmp_path, timeout=1, max_output_bytes=100)
    assert seen[0].get('env') is not None
    assert 'FFREPORT' not in seen[0]['env'] and 'POLZA_API_KEY' not in seen[0]['env']
    assert seen[0]['env']['TEMP'] == str(tmp_path)


def test_manifest_creation_failure_cleans_new_owned_directory(media_case, monkeypatch):
    c = media_case
    def fail(*args):
        raise OSError('SYNTHETIC_STORAGE_FAILURE')
    monkeypatch.setattr(c.adapter, '_manifest', fail)
    with pytest.raises(c.a.MaxMediaError):
        c.adapter.download_voice(c.event)
    assert not list((c.tmp / 'media').iterdir())


def test_unc_executable_is_rejected_before_any_filesystem_probe(media_case, monkeypatch):
    c = media_case
    attempted = []
    def is_file(path):
        attempted.append(path)
        return False
    monkeypatch.setattr(Path, 'is_file', is_file)
    with pytest.raises(c.a.MaxMediaError, match='max_media_configuration_invalid'):
        c.adapter._executable(r'\\untrusted.invalid\share\ffprobe.exe', 'ffprobe')
    assert not attempted
