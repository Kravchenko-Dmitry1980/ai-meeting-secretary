"""Offline release-script regression tests: all API requests use MockTransport."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[1] / 'scripts' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'regression_{name}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalBackend:
    scope_id = '35f5013a-705b-487f-9ab2-fbf583aa9b94'

    def __init__(self, *, original_limits=False, original_cloud=False, failure=None, prices=None):
        self.config = {'key_configured': True, 'cloud_enabled': original_cloud,
                       'local_cost_limits_enabled': original_limits,
                       'allow_unknown_price': True, 'meeting_budget_rub': 100,
                       'stt_model': 'mock/stt', 'summary_model': 'mock/summary',
                       'stt_price_rub_per_minute': prices,
                       'summary_input_rub_per_million': prices,
                       'summary_output_rub_per_million': prices}
        self.original = dict(self.config)
        self.failure = failure
        self.patches = []
        self.posts = []
        self.events = []
        self.upload_config = None
        self.meeting_body = None
        self.scope_body = None
        self.scope_created = False
        self.scope_reads = 0
        self.scope = {'cap_rub': 5, 'confirmed_rub': .5, 'reserved_rub': 0,
                      'remaining_rub': 4.5, 'paused_code': None, 'uncertain_count': 0}
        if failure in {'unknown', 'running'}:
            self.scope.update(reserved_rub=1, remaining_rub=3.5)
        if failure == 'uncertain':
            self.scope['uncertain_count'] = 1
        self.monthly = {'period': '2026-10', 'approved_rub': 3000,
                        'effective_limit_rub': 3000, 'confirmed_rub': 4,
                        'reserved_rub': 0, 'remaining_rub': 2996, 'paused_code': None}

    def __call__(self, request):
        assert request.url.host == '127.0.0.1'
        path = request.url.path
        self.events.append((request.method, path))
        if request.method in {'POST', 'PATCH'}:
            assert request.headers.get('X-Secretary-Token') == 'mock-script-token'
        if request.method == 'POST':
            self.posts.append(path)
        if path == '/api/v1/config':
            if request.method == 'PATCH':
                patch = json.loads(request.content)
                self.patches.append(patch)
                if self.failure == 'restore' and len(self.patches) > 1 and patch == {'cloud_enabled': False}:
                    raise httpx.ConnectError('mock restore unavailable', request=request)
                self.config.update(patch)
                if len(self.patches) == 1:
                    if self.failure == 'initial_patch_timeout':
                        raise httpx.ReadTimeout('mock response lost after applied PATCH', request=request)
                    if self.failure == 'ignored_cloud':
                        self.config['cloud_enabled'] = False
            return httpx.Response(200, json=self.config)
        if path == '/api/v1/session':
            return httpx.Response(200, json={'csrf_token': 'mock-script-token'})
        if path == '/api/v1/cloud-budget/refresh':
            return httpx.Response(200, json=self.monthly)
        if path == '/api/v1/cloud-budget/scopes' and request.method == 'POST':
            self.scope_body = json.loads(request.content)
            assert str(UUID(self.scope_body['operation_id'])) == self.scope_body['operation_id']
            self.scope_created = True
            if self.failure == 'scope_create_timeout':
                raise httpx.ReadTimeout('mock scope response lost', request=request)
            return httpx.Response(200 if self.failure == 'scope_wrong_status' else 201, json={
                'scope_id': 'invalid-id' if self.failure == 'invalid_scope_id' else self.scope_id,
                'cap_rub': 100 if self.failure == 'ignored_scope_cap' else 5})
        if path == '/api/v1/cloud-budget/scopes/' + self.scope_id:
            self.scope_reads += 1
            if self.failure == 'scope_poll' or (self.failure == 'scope_final_timeout' and self.scope_reads > 1):
                raise httpx.ReadTimeout('mock scope snapshot unavailable', request=request)
            return httpx.Response(200, json=self.scope)
        if path == '/api/v1/usage':
            if request.url.params.get('meeting_id') and self.failure in {'unknown', 'running'}:
                return httpx.Response(200, json={'records': [{'status': 'unknown'}], 'unknown_count': 1, 'confirmed_rub': 0})
            if request.url.params.get('meeting_id'):
                return httpx.Response(200, json={'records': [
                    {'status': 'confirmed', 'confirmed_rub': 1},
                    {'status': 'confirmed', 'confirmed_rub': 1}],
                    'unknown_count': 0, 'confirmed_rub': 2})
            return httpx.Response(200, json={'records': [], 'unknown_count': 0, 'confirmed_rub': 0})
        if path == '/api/v1/meetings' and request.method == 'GET':
            if self.failure == 'busy_after_scope' and self.scope_created:
                return httpx.Response(200, json=[{'id': 'unrelated-meeting'}])
            return httpx.Response(200, json=[])
        if path == '/api/v1/meetings/unrelated-meeting/recording':
            return httpx.Response(200, json={'recording': True, 'status': 'recording'})
        if path == '/api/v1/meetings':
            self.meeting_body = json.loads(request.content)
            assert self.scope_created
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
@pytest.mark.parametrize('prices', [None, .1])
def test_benchmark_scopes_spend_without_changing_regular_limits(tmp_path, monkeypatch, original_limits, prices):
    backend = LocalBackend(original_limits=original_limits, prices=prices)
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 0 and report['config_restored'] is True
    assert backend.upload_config == {**backend.original, 'cloud_enabled': True}
    assert all(set(patch) == {'cloud_enabled'} for patch in backend.patches)
    assert backend.scope_body['cap_rub'] == 5
    assert backend.meeting_body['cloud_budget_scope_id'] == backend.scope_id
    assert report['cloud_budget_scope_id'] == backend.scope_id
    assert report['cloud_budget_scope_operation_id'] == backend.scope_body['operation_id']
    assert report['monthly_budget_preflight']['effective_limit_rub'] == 3000
    assert backend.events.index(('POST', '/api/v1/cloud-budget/refresh')) < backend.events.index(('POST', '/api/v1/cloud-budget/scopes'))
    assert backend.events.index(('POST', '/api/v1/cloud-budget/scopes')) < backend.events.index(('POST', '/api/v1/meetings'))
    assert backend.config == backend.original


def test_scope_cost_is_authoritative_over_duplicate_legacy_receipts(tmp_path, monkeypatch):
    result, report = run_mock(tmp_path, monkeypatch, LocalBackend())
    assert result == 0
    assert report['usage']['confirmed_rub'] == 2
    assert report['cloud_budget_scope']['confirmed_rub'] == .5
    assert report['confirmed_rub_per_hour'] == 1800
    assert report['cost_source'] == 'cloud_budget_scope'


@pytest.mark.parametrize('failure', ['initial_patch_timeout', 'create', 'upload', 'poll', 'interrupt'])
def test_benchmark_failures_restore_only_original_cloud_flag_when_settled(tmp_path, monkeypatch, failure):
    backend = LocalBackend(failure=failure)
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is True
    assert backend.config == backend.original
    if failure == 'initial_patch_timeout':
        assert '/api/v1/meetings' not in backend.posts
    if failure in {'upload', 'poll', 'interrupt'}:
        assert {'cloud_enabled': False} in backend.patches
    assert all(set(patch) == {'cloud_enabled'} for patch in backend.patches)


@pytest.mark.parametrize('failure', ['ignored_cloud', 'ignored_scope_cap', 'invalid_scope_id', 'scope_create_timeout', 'scope_wrong_status'])
def test_unconfirmed_scope_or_cloud_activation_prevents_meeting_and_upload(tmp_path, monkeypatch, failure):
    backend = LocalBackend(failure=failure)
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is True
    assert '/api/v1/meetings' not in backend.posts
    assert backend.config == backend.original
    if failure != 'ignored_cloud':
        assert backend.patches == []


@pytest.mark.parametrize('failure', ['running', 'unknown', 'scope_poll', 'uncertain'])
def test_unresolved_scope_keeps_cloud_off_without_changing_regular_limits(tmp_path, monkeypatch, failure):
    backend = LocalBackend(failure=failure)
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is False
    assert backend.config['cloud_enabled'] is False
    assert {k: v for k, v in backend.config.items() if k != 'cloud_enabled'} == {
        k: v for k, v in backend.original.items() if k != 'cloud_enabled'}
    assert report['prior_config'] == {'cloud_enabled': False}
    assert report['cloud_budget_scope_id'] == backend.scope_id
    assert report['confirmed_rub_per_hour'] is None


def test_settled_exhausted_scope_restores_original_cloud_flag(tmp_path, monkeypatch):
    backend = LocalBackend()
    backend.scope.update(confirmed_rub=5, remaining_rub=0, paused_code='monthly_budget_scope_exhausted')
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 0 and report['config_restored'] is True
    assert backend.config == backend.original
    assert report['cloud_budget_scope']['reserved_rub'] == 0
    assert report['cloud_budget_scope']['uncertain_count'] == 0
    assert report['confirmed_rub_per_hour'] == 18000


def test_successful_run_preserves_already_enabled_cloud_without_patches(tmp_path, monkeypatch):
    backend = LocalBackend(original_cloud=True)
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 0 and report['config_restored'] is True
    assert backend.config == backend.original and backend.patches == []
    assert backend.upload_config == backend.original


def test_uncertain_scope_keeps_originally_enabled_cloud_off(tmp_path, monkeypatch):
    backend = LocalBackend(original_cloud=True, failure='uncertain')
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is False
    assert backend.config == {**backend.original, 'cloud_enabled': False}
    assert report['prior_config'] == {'cloud_enabled': True}
    assert backend.patches == [{'cloud_enabled': False}]


def test_newly_busy_backend_is_rejected_before_cloud_flag_change(tmp_path, monkeypatch):
    backend = LocalBackend(failure='busy_after_scope')
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is True
    assert backend.patches == [] and backend.config == backend.original
    assert '/api/v1/meetings' not in backend.posts


def test_failed_final_scope_read_does_not_claim_cached_cost_is_final(tmp_path, monkeypatch):
    backend = LocalBackend(failure='scope_final_timeout')
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is False
    assert backend.config == {**backend.original, 'cloud_enabled': False}
    assert report['cloud_pause_confirmed'] is True
    assert report['cost'] == 'unknown or outstanding scoped cloud receipt'
    assert report['confirmed_rub_per_hour'] is None


def test_failed_restore_is_failure_even_after_successful_processing(tmp_path, monkeypatch):
    backend = LocalBackend(failure='restore')
    result, report = run_mock(tmp_path, monkeypatch, backend)
    assert result == 1 and report['config_restored'] is False
    assert report['prior_config'] == {'cloud_enabled': False}


@pytest.mark.parametrize('invalid_snapshot', [
    {'paused_code': 'monthly_budget_configuration'},
    {'reserved_rub': 1},
    {'remaining_rub': 0},
    {'effective_limit_rub': 3001},
    {'confirmed_rub': True},
    {'approved_rub': None},
    {'uncertain_count': 1},
])
def test_unready_monthly_budget_is_rejected_before_scope_or_configuration(tmp_path, monkeypatch, invalid_snapshot):
    backend = LocalBackend(prices=.1)
    backend.monthly.update(invalid_snapshot)
    with pytest.raises(SystemExit) as caught:
        run_mock(tmp_path, monkeypatch, backend)
    assert caught.value.code == 2
    assert backend.patches == []
    assert '/api/v1/cloud-budget/scopes' not in backend.posts
    assert '/api/v1/meetings' not in backend.posts


@pytest.mark.parametrize('field', ['stt_model', 'summary_model'])
def test_missing_selected_model_prevents_scope_or_cloud_change(tmp_path, monkeypatch, field):
    backend = LocalBackend()
    backend.config[field] = ''
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
