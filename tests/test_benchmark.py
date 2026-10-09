import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest


def load_benchmark():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'benchmark.py'
    spec = importlib.util.spec_from_file_location('secretary_benchmark', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_error_metrics_are_reference_based_and_empty_is_unknown():
    benchmark = load_benchmark()
    assert benchmark.accuracy('не запускаем Kubernetes 12 октября', 'запускаем Kubernetes 12 октября')['wer'] == 0.2
    assert benchmark.accuracy('API', 'api')['cer'] == 0
    assert benchmark.accuracy('', 'anything')['wer'] is None


def fake_client(monkeypatch, benchmark, handler):
    original = httpx.Client
    monkeypatch.setattr(benchmark.httpx, 'Client', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(benchmark, '_backend_process', lambda *args: (None, None))


def config():
    return {'key_configured': True, 'cloud_enabled': True, 'allow_unknown_price': True,
            'local_cost_limits_enabled': True,
            'meeting_budget_rub': 100, 'stt_model': 'openai/whisper-large-v3-turbo',
            'summary_model': 'qwen/qwen3-30b-a3b-instruct-2507',
            'stt_price_rub_per_minute': .048, 'summary_input_rub_per_million': 5.6267127,
            'summary_output_rub_per_million': 22.5594369}


def cloud_args(tmp_path):
    audio = tmp_path / 'synthetic.wav'
    audio.write_bytes(b'not-a-real-recording-just-a-mock-upload')
    return ['--run-cloud', '--audio', str(audio), '--max-rub', '5', '--output', str(tmp_path / 'report.json')]


SCOPE_ID = 'cc82d2d1-c7c2-48ef-8181-0c4f021d90b3'


def budget_contract(request, scope):
    """Synthetic budget responses; no provider or real account is consulted."""
    path = request.url.path
    if path == '/api/v1/cloud-budget/refresh':
        assert request.method == 'POST' and request.headers['X-Secretary-Token'] == 'test-csrf'
        return httpx.Response(200, json={'period': '2026-10', 'approved_rub': 3000,
            'effective_limit_rub': 3000, 'confirmed_rub': 4, 'reserved_rub': 0,
            'remaining_rub': 2996, 'paused_code': None, 'uncertain_count': 0})
    if path == '/api/v1/cloud-budget/scopes':
        assert request.method == 'POST' and request.headers['X-Secretary-Token'] == 'test-csrf'
        body = json.loads(request.content)
        assert str(UUID(body['operation_id'])) == body['operation_id'] and body['cap_rub'] == 5
        return httpx.Response(201, json={'scope_id': SCOPE_ID, 'cap_rub': 5})
    if path == f'/api/v1/cloud-budget/scopes/{SCOPE_ID}':
        assert request.method == 'GET'
        return httpx.Response(200, json=scope)
    return None


def test_default_benchmark_never_opens_network(tmp_path, monkeypatch):
    benchmark = load_benchmark()
    def forbidden(*args, **kwargs):
        raise AssertionError('Default benchmark must not open a network client')
    monkeypatch.setattr(benchmark.httpx, 'Client', forbidden)
    output = tmp_path / 'offline.json'
    assert benchmark.main(['--output', str(output)]) == 0
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['cloud'] == 'не измерено' and report['sample_count'] == 0
    assert report['p50'] is report['p95'] is report['confirmed_rub_per_hour'] is None
    assert report['reason'] == ('Эталонный benchmark не запускался: проверенная расшифровка и '
                                'полный набор подтверждённых расходов отсутствуют.')


def test_no_key_reads_only_config_and_never_submits_or_changes_state(tmp_path, monkeypatch):
    benchmark = load_benchmark()
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={**config(), 'key_configured': False})
    fake_client(monkeypatch, benchmark, handler)
    with pytest.raises(SystemExit) as caught:
        benchmark.main(cloud_args(tmp_path))
    assert caught.value.code == 2
    assert calls == [('GET', '/api/v1/config')]
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert 'no paid request' in report['reason']
    assert report['sample_count'] == 0


def test_unknown_configured_price_defers_to_unready_monthly_guard_before_scope(tmp_path, monkeypatch):
    benchmark = load_benchmark()
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/api/v1/config':
            return httpx.Response(200, json={**config(), 'summary_input_rub_per_million': None})
        if request.url.path == '/api/v1/meetings': return httpx.Response(200, json=[])
        if request.url.path == '/api/v1/usage':
            return httpx.Response(200, json={'records': [], 'unknown_count': 0, 'confirmed_rub': 0})
        if request.url.path == '/api/v1/session': return httpx.Response(200, json={'csrf_token': 'test-csrf'})
        response = budget_contract(request, {})
        if response is not None:
            assert request.url.path == '/api/v1/cloud-budget/refresh'
            snapshot = response.json()
            snapshot['paused_code'] = 'monthly_budget_configuration'
            return httpx.Response(200, json=snapshot)
        raise AssertionError('An unready monthly guard must prevent any scope or config change')
    fake_client(monkeypatch, benchmark, handler)
    with pytest.raises(SystemExit) as caught:
        benchmark.main(cloud_args(tmp_path))
    assert caught.value.code == 2
    assert calls[-1] == ('POST', '/api/v1/cloud-budget/refresh')
    assert all(method == 'GET' for method, path in calls[:-1])
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert report['monthly_budget_preflight']['paused_code'] == 'monthly_budget_configuration'
    assert 'cloud_budget_scope_id' not in report


@pytest.mark.parametrize('state', [{'recording': True, 'status': 'recording'},
                                  {'recording': False, 'status': 'starting'},
                                  {'recording': False, 'status': 'stopping'}])
def test_live_capture_gate_uses_actual_recording_endpoint(tmp_path, monkeypatch, state):
    benchmark = load_benchmark()
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/api/v1/config': return httpx.Response(200, json=config())
        if request.url.path == '/api/v1/meetings': return httpx.Response(200, json=[{'id': 'owned-existing'}])
        if request.url.path == '/api/v1/meetings/owned-existing/recording': return httpx.Response(200, json=state)
        raise AssertionError('Must stop before mutation or benchmark create')
    fake_client(monkeypatch, benchmark, handler)
    with pytest.raises(SystemExit):
        benchmark.main(cloud_args(tmp_path))
    assert calls[-1] == ('GET', '/api/v1/meetings/owned-existing/recording')
    assert all(method == 'GET' for method, path in calls)


@pytest.mark.parametrize('url', ['http://127.0.0.1:8765@evil.example', 'http://127.0.0.1:8765/api',
                                'https://127.0.0.1:8765', 'http://localhost:8765',
                                'http://127.0.0.1:8765?target=x', 'http://127.0.0.1:99999'])
def test_url_gate_rejects_noncanonical_loopback(url):
    assert not load_benchmark().local_api_url(url)


def test_single_sample_is_not_percentiles_and_unknown_cost_stays_null(tmp_path, monkeypatch):
    benchmark = load_benchmark()
    patches, posts = [], []
    current_config = config()
    original_config = dict(current_config)
    scope = {'cap_rub': 5, 'confirmed_rub': .01, 'reserved_rub': 1,
             'remaining_rub': 3.99, 'paused_code': None, 'uncertain_count': 0}
    def handler(request):
        path = request.url.path
        response = budget_contract(request, scope)
        if response is not None: return response
        if path == '/api/v1/config':
            if request.method == 'PATCH':
                patch = json.loads(request.content)
                patches.append(patch)
                current_config.update(patch)
            return httpx.Response(200, json=current_config)
        if path == '/api/v1/session': return httpx.Response(200, json={'csrf_token': 'test-csrf'})
        if path == '/api/v1/meetings':
            if request.method == 'GET': return httpx.Response(200, json=[])
            assert json.loads(request.content)['cloud_budget_scope_id'] == SCOPE_ID
            posts.append(path)
            return httpx.Response(201, json={'id': 'benchmark-only'})
        if path.endswith('/upload'):
            assert current_config == original_config
            posts.append(path)
            return httpx.Response(202, json={'job_id': 'prepare-job'})
        if path.endswith('/segments'):
            return httpx.Response(200, json={'total': 1, 'transcript_version': 1, 'items': [{'id': 's1', 'text': 'Нет, не запускаем Kubernetes.'}]})
        if path.endswith('/jobs'):
            return httpx.Response(200, json=[{'id': 'stt', 'status': 'succeeded'}, {'id': 'summary', 'status': 'paused_budget'}])
        if path == '/api/v1/usage':
            if not request.url.params.get('meeting_id'):
                return httpx.Response(200, json={'records': [], 'unknown_count': 0, 'confirmed_rub': 0})
            return httpx.Response(200, json={'confirmed_rub': .01, 'unknown_count': 1,
                'records': [{'status': 'confirmed', 'confirmed_rub': .01}, {'status': 'unknown', 'confirmed_rub': None}]})
        if path.endswith('/summary'): return httpx.Response(202, json={'status': 'paused_budget'})
        if path == '/api/v1/meetings/benchmark-only': return httpx.Response(200, json={'duration_ms': 1000})
        raise AssertionError((request.method, path))
    fake_client(monkeypatch, benchmark, handler)
    assert benchmark.main(cloud_args(tmp_path)) == 1
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert report['cloud'] == 'partial or failed processing'
    assert report['sample_count'] == 1 and report['p50'] is report['p95'] is None
    assert report['confirmed_rub_per_hour'] is None
    assert report['summary_http_status'] == 202
    assert report['config_restored'] is False
    assert patches == [{'cloud_enabled': False}]
    assert current_config == {**original_config, 'cloud_enabled': False}
    assert report['cloud_budget_scope']['cap_rub'] == 5
    assert report['cost_source'] == 'cloud_budget_scope'
    assert len(posts) == 2


def test_failure_keeps_cloud_paused_while_own_scoped_job_is_running(tmp_path, monkeypatch):
    benchmark = load_benchmark()
    patches, cancels = [], []
    current_config = config()
    original_config = dict(current_config)
    scope = {'cap_rub': 5, 'confirmed_rub': 0, 'reserved_rub': 1,
             'remaining_rub': 4, 'paused_code': None, 'uncertain_count': 1}
    def handler(request):
        path = request.url.path
        response = budget_contract(request, scope)
        if response is not None: return response
        if path == '/api/v1/config':
            if request.method == 'PATCH':
                patch = json.loads(request.content)
                patches.append(patch)
                current_config.update(patch)
            return httpx.Response(200, json=current_config)
        if path == '/api/v1/session': return httpx.Response(200, json={'csrf_token': 'test-csrf'})
        if path == '/api/v1/meetings':
            if request.method == 'GET': return httpx.Response(200, json=[])
            assert json.loads(request.content)['cloud_budget_scope_id'] == SCOPE_ID
            return httpx.Response(201, json={'id': 'benchmark-only'})
        if path.endswith('/upload'):
            assert current_config == original_config
            return httpx.Response(202, json={'job_id': 'owned-job'})
        if path.endswith('/segments'): raise httpx.ReadTimeout('simulated local failure', request=request)
        if path.endswith('/jobs'): return httpx.Response(200, json=[{'id': 'owned-job', 'status': 'running'}])
        if path == '/api/v1/jobs/owned-job/cancel':
            cancels.append(path)
            return httpx.Response(200, json={'id': 'owned-job', 'status': 'running'})
        if path == '/api/v1/usage':
            return httpx.Response(200, json={'records': [{'status': 'unknown'}] if request.url.params.get('meeting_id') else [],
                                           'unknown_count': 1 if request.url.params.get('meeting_id') else 0})
        raise AssertionError((request.method, path))
    fake_client(monkeypatch, benchmark, handler)
    original_abort = benchmark._abort
    monkeypatch.setattr(benchmark, '_abort', lambda client, mid, cap: original_abort(client, mid, cap, wait_seconds=0))
    assert benchmark.main(cloud_args(tmp_path)) == 1
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert report['config_restored'] is False
    assert patches == [{'cloud_enabled': False}]
    assert current_config == {**original_config, 'cloud_enabled': False}
    assert cancels == ['/api/v1/jobs/owned-job/cancel']
    assert report['prior_config'] == {'cloud_enabled': True}
    assert report['cloud_budget_scope_id'] == SCOPE_ID
    assert report['cloud_budget_scope']['cap_rub'] == 5


def test_pending_reservation_even_without_unknown_count_is_unknown_cost():
    usage = {'records': [{'status': 'confirmed', 'confirmed_rub': 1}, {'status': 'reserved', 'estimated_rub': 2}],
             'confirmed_rub': 1, 'unknown_count': 0}
    assert load_benchmark().cost_metrics(usage, 60000)['confirmed_rub_per_hour'] is None


def test_resource_metrics_do_not_attach_to_foreign_runtime_port(tmp_path):
    benchmark = load_benchmark()
    state = tmp_path / 'processes.json'
    state.write_text(json.dumps({'pid': 1, 'port': 9000, 'launcher': str(benchmark.PROJECT_DIR / 'scripts' / 'run_server.py'),
                                 'project': str(benchmark.PROJECT_DIR)}), encoding='utf-8')
    assert benchmark._backend_process(state, 8765) == (None, None)


def test_existing_unknown_expense_rejected_without_new_submit(tmp_path, monkeypatch):
    benchmark = load_benchmark()
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/api/v1/config': return httpx.Response(200, json=config())
        if request.url.path == '/api/v1/meetings': return httpx.Response(200, json=[])
        if request.url.path == '/api/v1/usage': return httpx.Response(200, json={'records': [{'status': 'unknown'}], 'unknown_count': 1})
        raise AssertionError('Must not mutate or submit with unresolved expense')
    fake_client(monkeypatch, benchmark, handler)
    with pytest.raises(SystemExit):
        benchmark.main(cloud_args(tmp_path))
    assert all(method == 'GET' for method, path in calls)


def test_no_key_gate_against_current_backend_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from secretary.api import create_app
    from secretary.settings import Settings
    class SilentCapture:
        def close(self):
            pass
    benchmark = load_benchmark()
    app = create_app(Settings(_env_file=None, data_dir=tmp_path / 'backend',
                              polza_api_key=SecretStr('')), capture=SilentCapture(), run_worker=False)
    client = TestClient(app, base_url='http://127.0.0.1:8765')
    monkeypatch.setattr(benchmark.httpx, 'Client', lambda **kwargs: client)
    with pytest.raises(SystemExit) as caught:
        benchmark.main(cloud_args(tmp_path))
    assert caught.value.code == 2
    assert app.state.db.list_meetings() == []
    assert app.state.db.configuration() == {}


def test_local_api_failure_overwrites_stale_report_without_submit(tmp_path, monkeypatch):
    benchmark = load_benchmark()
    output = tmp_path / 'report.json'
    output.write_text(json.dumps({'cloud': 'old-success'}), encoding='utf-8')
    calls = []
    def handler(request):
        calls.append(request.method)
        raise httpx.ConnectError('API unavailable', request=request)
    fake_client(monkeypatch, benchmark, handler)
    assert benchmark.main(cloud_args(tmp_path)) == 1
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['cloud'] == 'не измерено' and 'preflight failed' in report['reason']
    assert calls == ['GET']


class SampleProcess:
    def __init__(self, pid, launcher, *, port=8765, parent=None, created=10000, cpu=0, listens=False, executable='python.exe'):
        self.pid, self.launcher, self.port = pid, launcher, port
        self._parent, self.created, self.cpu = parent, created, cpu
        self.listens, self.executable = listens, executable
        self.descendants = []

    def exe(self):
        return self.executable

    def cmdline(self):
        return [self.executable, str(self.launcher), '--port', str(self.port)]

    def create_time(self):
        return self.created

    def parent(self):
        return self._parent

    def children(self, recursive=False):
        assert recursive
        return self.descendants

    def cpu_times(self):
        return (self.cpu, 0)

    def net_connections(self, kind='tcp'):
        assert kind == 'tcp'
        return [SimpleNamespace(status='LISTEN', laddr=('127.0.0.1', 8765))] if self.listens else []


def runtime_state(tmp_path, benchmark):
    state = tmp_path / 'processes.json'
    state.write_text(json.dumps({'pid': 68512, 'port': 8765,
        'launcher': str(benchmark.PROJECT_DIR / 'scripts' / 'run_server.py'),
        'project': str(benchmark.PROJECT_DIR),
        'creation_time': datetime.fromtimestamp(10000, timezone.utc).isoformat()}), encoding='utf-8')
    return state


def test_windows_venv_redirector_samples_listening_python_child(tmp_path, monkeypatch):
    import psutil
    benchmark = load_benchmark()
    launcher = benchmark.PROJECT_DIR / 'scripts' / 'run_server.py'
    redirector = SampleProcess(68512, launcher, cpu=0)
    actual_server = SampleProcess(69232, launcher, parent=redirector, created=10000.05, cpu=3.25, listens=True)
    ffmpeg = SampleProcess(70000, launcher, parent=actual_server, created=10000.06, cpu=99, listens=True, executable='ffmpeg.exe')
    redirector.descendants = [actual_server, ffmpeg]
    monkeypatch.setattr(psutil, 'Process', lambda pid: redirector)
    process, cpu = benchmark._backend_process(runtime_state(tmp_path, benchmark), 8765)
    assert process is actual_server and process.pid == 69232
    assert cpu == 3.25


@pytest.mark.parametrize('reason', ['wrong_port', 'foreign_ancestry', 'no_listener', 'unavailable_listener'])
def test_unprovable_child_server_metrics_are_unknown(tmp_path, monkeypatch, reason):
    import psutil
    benchmark = load_benchmark()
    launcher = benchmark.PROJECT_DIR / 'scripts' / 'run_server.py'
    redirector = SampleProcess(68512, launcher, cpu=0)
    child = SampleProcess(69232, launcher, parent=redirector, created=10000.05, cpu=3.25, listens=True)
    if reason == 'wrong_port': child.port = 8766
    if reason == 'foreign_ancestry': child._parent = SampleProcess(999, launcher)
    if reason == 'no_listener': child.listens = False
    if reason == 'unavailable_listener':
        def unavailable(**kwargs):
            raise PermissionError('cannot prove socket ownership')
        child.net_connections = unavailable
    redirector.descendants = [child]
    monkeypatch.setattr(psutil, 'Process', lambda pid: redirector)
    assert benchmark._backend_process(runtime_state(tmp_path, benchmark), 8765) == (None, None)


def test_direct_python_server_can_be_sampled_without_redirector(tmp_path, monkeypatch):
    import psutil
    benchmark = load_benchmark()
    server = SampleProcess(68512, benchmark.PROJECT_DIR / 'scripts' / 'run_server.py', cpu=1.5, listens=True)
    monkeypatch.setattr(psutil, 'Process', lambda pid: server)
    assert benchmark._backend_process(runtime_state(tmp_path, benchmark), 8765) == (server, 1.5)
