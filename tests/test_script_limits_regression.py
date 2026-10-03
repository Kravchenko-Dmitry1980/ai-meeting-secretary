"""Offline release-script regression tests: all API requests use MockTransport."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[1] / 'scripts' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'regression_{name}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalBackend:
    def __init__(self, *, original_limits=False, failure=None):
        self.config = {'key_configured': True, 'cloud_enabled': False,
                       'local_cost_limits_enabled': original_limits,
                       'allow_unknown_price': True, 'meeting_budget_rub': 100,
                       'stt_model': 'mock/stt', 'summary_model': 'mock/summary',
                       'stt_price_rub_per_minute': .1,
                       'summary_input_rub_per_million': 1,
                       'summary_output_rub_per_million': 2}
        self.original = dict(self.config)
        self.failure = failure
        self.patches = []
        self.posts = []
        self.upload_config = None

    def __call__(self, request):
        assert request.url.host == '127.0.0.1'
        path = request.url.path
        if path == '/api/v1/config':
            if request.method == 'PATCH':
                patch = json.loads(request.content)
                self.patches.append(patch)
                if self.failure == 'restore' and len(self.patches) > 1 and patch.get('meeting_budget_rub') == 100:
                    raise httpx.ConnectError('mock restore unavailable', request=request)
                self.config.update(patch)
                if len(self.patches) == 1:
                    if self.failure == 'initial_patch_timeout':
                        raise httpx.ReadTimeout('mock response lost after applied PATCH', request=request)
                    if self.failure == 'ignored_flag':
                        self.config['local_cost_limits_enabled'] = False
                    if self.failure == 'ignored_cap':
                        self.config['meeting_budget_rub'] = 100
            return httpx.Response(200, json=self.config)
        if path == '/api/v1/session':
            return httpx.Response(200, json={'csrf_token': 'mock-script-token'})
        if path == '/api/v1/usage':
            if request.url.params.get('meeting_id') and self.failure in {'unknown', 'running'}:
                return httpx.Response(200, json={'records': [{'status': 'unknown'}], 'unknown_count': 1, 'confirmed_rub': 0})
            return httpx.Response(200, json={'records': [], 'unknown_count': 0, 'confirmed_rub': 0})
        if path == '/api/v1/meetings' and request.method == 'GET':
            return httpx.Response(200, json=[])
        if request.method == 'POST':
            self.posts.append(path)
        if path == '/api/v1/meetings':
            if self.failure == 'create':
                raise httpx.ReadTimeout('mock create response failed', request=request)
            return httpx.Response(201, json={'id': 'script-owned-meeting'})
        if path.endswith('/upload'):
            self.upload_config = dict(self.config)
            if self.failure == 'upload':
                return httpx.Response(500)
            if self.failure == 'interrupt':
                raise KeyboardInterrupt('mock interruption')
            return httpx.Response(202, json={'job_id': 'script-owned-job'})
        if path.endswith('/segments'):
            if self.failure in {'poll', 'running'}:
                raise httpx.ReadTimeout('mock polling failure', request=request)
            return httpx.Response(200, json={'total': 1, 'transcript_version': 1,
                'items': [{'id': 's1', 'text': 'Synthetic reference.'}]})
        if path.endswith('/jobs'):
            if self.failure == 'running':
                return httpx.Response(200, json=[{'id': 'script-owned-job', 'status': 'running'}])
            return httpx.Response(200, json=[{'id': 'script-owned-job', 'status': 'succeeded'}])
        if path.endswith('/cancel'):
            return httpx.Response(200, json={'id': 'script-owned-job', 'status': 'running'})
        if path.endswith('/summary'):
            return httpx.Response(200, json={'status': 'succeeded', 'overview': 'Synthetic summary.'})
        if path == '/api/v1/meetings/script-owned-meeting':
            return httpx.Response(200, json={'duration_ms': 1000})
        raise AssertionError((request.method, path))


def run_mock(tmp_path, monkeypatch, backend):
    benchmark = load_script('benchmark')
    original_client = httpx.Client
    monkeypatch.setattr(benchmark.httpx, 'Client', lambda **kwargs:
                        original_client(**kwargs, transport=httpx.MockTransport(backend)))
    monkeypatch.setattr(benchmark, '_backend_process', lambda *args: (None, None))
    abort = benchmark._abort
    monkeypatch.setattr(benchmark, '_abort', lambda client, mid, cap:
                        abort(client, mid, cap, wait_seconds=0))
    audio = tmp_path / 'synthetic.wav'
    audio.write_bytes(b'fake local upload only')
    output = tmp_path / 'report.json'
    result = benchmark.main(['--run-cloud', '--audio', str(audio), '--max-rub', '5', '--output', str(output)])
    return result, json.loads(output.read_text(encoding='utf-8'))


@pytest.mark.parametrize('original_limits', [False, True])
def test_benchmark_enforces_flag_and_cap_before_upload_and_restores(tmp_path, monkeypatch, original_limits):
    backend = LocalBackend(original_limits=original_limits)
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 0 and report['config_restored'] is True
    assert backend.upload_config['local_cost_limits_enabled'] is True
    assert backend.upload_config['meeting_budget_rub'] == 5
    assert backend.upload_config['allow_unknown_price'] is False
    assert backend.config == backend.original


@pytest.mark.parametrize('failure', ['initial_patch_timeout', 'create', 'upload', 'poll', 'interrupt'])
def test_benchmark_failures_restore_prior_disabled_limits_when_settled(tmp_path, monkeypatch, failure):
    backend = LocalBackend(failure=failure)
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is True
    assert backend.config == backend.original
    if failure == 'initial_patch_timeout':
        assert backend.posts == []
    if failure in {'upload', 'poll', 'interrupt'}:
        assert any(patch.get('cloud_enabled') is False and patch.get('local_cost_limits_enabled') is True
                   for patch in backend.patches)


@pytest.mark.parametrize('failure', ['ignored_flag', 'ignored_cap'])
def test_unconfirmed_limits_prevent_create_and_upload(tmp_path, monkeypatch, failure):
    backend = LocalBackend(failure=failure)
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is True
    assert backend.posts == []
    assert backend.config == backend.original


@pytest.mark.parametrize('failure', ['running', 'unknown'])
def test_unresolved_work_keeps_cloud_off_and_cap_enforced(tmp_path, monkeypatch, failure):
    backend = LocalBackend(failure=failure)
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is False
    assert backend.config['cloud_enabled'] is False
    assert backend.config['local_cost_limits_enabled'] is True
    assert backend.config['meeting_budget_rub'] == 5
    assert report['prior_config']['local_cost_limits_enabled'] is False


def test_failed_restore_is_failure_even_after_successful_processing(tmp_path, monkeypatch):
    backend = LocalBackend(failure='restore')
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is False
    assert report['prior_config']['local_cost_limits_enabled'] is False


@pytest.mark.parametrize('invalid_flag', [None, 0, 'false'])
def test_missing_or_nonboolean_limit_contract_is_rejected_without_mutation(tmp_path, monkeypatch, invalid_flag):
    backend = LocalBackend()
    if invalid_flag is None:
        backend.config.pop('local_cost_limits_enabled')
    else:
        backend.config['local_cost_limits_enabled'] = invalid_flag
    with pytest.raises(SystemExit) as caught:
        run_mock(tmp_path, monkeypatch, backend)
    assert caught.value.code == 2
    assert backend.patches == backend.posts == []


def test_resource_measure_is_offline_and_uses_unique_fixtures(tmp_path, monkeypatch):
    resource = load_script('resource_check')
    original_settings = resource.Settings
    settings_seen, directories, sources = [], [], []
    def settings_factory(**kwargs):
        assert kwargs.get('_env_file', 'not supplied') is None
        settings = original_settings(**kwargs)
        assert not settings.cloud_enabled and not settings.key_configured
        settings_seen.append(settings)
        return settings
    monkeypatch.setattr(resource, 'Settings', settings_factory)
    def generate(command, **kwargs):
        path = Path(command[-1])
        assert path.is_relative_to(tmp_path)
        assert not path.exists(), 'A prior fixture must never be overwritten'
        path.write_bytes(b'synthetic flac placeholder')
        sources.append(path)
    monkeypatch.setattr(resource.subprocess, 'run', generate)
    async def prepare(settings, source, directory):
        directory.mkdir(parents=True)
        output = directory / 'part-000000.wav'
        output.write_bytes(b'synthetic wav placeholder')
        directories.append(directory)
        return [{'path': str(output), 'duration_ms': 1000, 'offset_ms': 0}]
    monkeypatch.setattr(resource, 'prepare_audio', prepare)
    first = asyncio.run(resource.measure(tmp_path, 1))
    second = asyncio.run(resource.measure(tmp_path, 1))
    assert directories[0] != directories[1] and sources[0] != sources[1]
    assert all(source.exists() for source in sources)
    assert first['cloud_requests'] == second['cloud_requests'] == 0
    assert len(settings_seen) == 2


def test_resource_main_retains_a_separate_report_per_run(tmp_path, monkeypatch):
    resource = load_script('resource_check')
    (tmp_path / '.runtime').mkdir()
    monkeypatch.setattr(resource, '__file__', str(tmp_path / 'scripts' / 'resource_check.py'))
    async def measure(*args, **kwargs):
        return {'cloud_requests': 0}
    monkeypatch.setattr(resource, 'measure', measure)
    asyncio.run(resource.main())
    first = set((tmp_path / '.runtime').rglob('*.json'))
    asyncio.run(resource.main())
    second = set((tmp_path / '.runtime').rglob('*.json'))
    assert len(first) == 1 and len(second) == 2
    assert first < second
