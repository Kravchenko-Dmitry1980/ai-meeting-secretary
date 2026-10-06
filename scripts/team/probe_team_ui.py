"""Isolated T9 browser fixture: real Gateway/auth, simulated Vikunja, local TLS.

No owner configuration is imported. This script is never a production launcher.
Run factory tests under run_offline_tests.py; actual loopback lifecycle separately.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import html
from html.parser import HTMLParser
import http.client
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = ROOT / '.runtime/team-rollout'
SCRIPT = Path(__file__).resolve()
HOSTNAME = 'secretary-t9.localhost'
MAX_LIFETIME_SECONDS = 3600
STATES = ('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled')
ZERO_DATE = '0001-01-01T00:00:00Z'
if str(ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(ROOT / 'backend'))

from secretary.application.team_tasks import TeamTaskService
from secretary.application.team_sync import TeamSyncService
from secretary.domain.team import TeamMember, TaskSnapshot
from secretary.infrastructure.team_auth_repository import AuthRepository
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import RepositoryMemberDirectory, TeamRepository
from secretary.infrastructure.team_read_repository import TeamReadRepository
from secretary.infrastructure.team_sync_repository import TeamSyncRepository
from secretary.infrastructure.vikunja import ProjectBinding, TaskReadContext, VikunjaClient
from secretary.interface.team_gateway import TeamGatewayClients, TeamGatewaySettings, create_team_app
from secretary.orchestration.team_worker import TeamWorker
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response


class ProbeFailure(ValueError):
    """Fixed diagnostic code, never raw credentials/provider/owner data."""


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def no_reparse(path):
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode)
                and not getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
                'fixture_reparse_forbidden')


def validate_run_root(value, *, parent=SCRATCH):
    path, parent = Path(value).absolute(), Path(parent).absolute()
    require(path.parent == parent and bool(re.fullmatch(r't9-ui-[0-9a-f]{32}', path.name)), 'fixture_scope_invalid')
    no_reparse(path)
    require(path.resolve().parent == parent.resolve(), 'fixture_scope_invalid')
    return path


def _page(rows, request):
    number, size = int(request.url.params.get('page', 1)), int(request.url.params.get('per_page', 50))
    return {'items': deepcopy(rows[(number - 1) * size:number * size]), 'page': number,
            'per_page': size, 'total': len(rows), 'total_pages': (len(rows) + size - 1) // size}


class SimulatedVikunja:
    """HTTP contract simulator. Never connects to a native or remote provider."""
    def __init__(self, bindings):
        self.bindings = {b.project_id: b for b in bindings}
        self.tasks, self.comments, self.requests, self.mutations = {}, {}, [], []
        self._lock = threading.RLock()

    def __call__(self, request):
        with self._lock:
            self.requests.append(request)
            require(request.url.host == '127.0.0.1' and request.url.path.startswith('/api/v2/'), 'simulator_scope_invalid')
            parts = request.url.path.removeprefix('/api/v2/').split('/')
            method = request.method
            body = json.loads(request.content) if request.content else {}
            if method not in ('GET', 'HEAD'):
                self.mutations.append((method, request.url.path))
            if parts == ['user'] and method == 'GET':
                return httpx.Response(200, json={'id': 19, 'bot_owner_id': 1})
            if parts[0] == 'labels' and method == 'GET':
                for b in self.bindings.values():
                    for key in ('important', 'urgent', 'cancelled'):
                        if parts[1] == getattr(b, key + '_label_id'):
                            return httpx.Response(200, json={'id': int(parts[1]), 'title': 'secretary:' + key,
                                                            'created_by': {'id': 19}})
            if parts[0] == 'projects' and parts[1] in self.bindings:
                b = self.bindings[parts[1]]
                view = {'id': int(b.manual_view_id), 'project_id': int(b.project_id), 'view_kind': 'kanban',
                        'bucket_configuration_mode': 'manual', 'done_bucket_id': 0, 'default_bucket_id': int(b.bucket_ids['inbox'])}
                if parts[2:] == ['views'] and method == 'GET':
                    return httpx.Response(200, json=_page([view], request))
                if parts[2:] == ['views', b.manual_view_id] and method == 'GET':
                    return httpx.Response(200, json=view)
                if parts[2:] == ['views', b.manual_view_id, 'buckets'] and method == 'GET':
                    return httpx.Response(200, json=_page([{'id': int(identifier), 'project_view_id': int(b.manual_view_id),
                        'title': state} for state, identifier in b.bucket_ids.items()], request))
                if len(parts) == 7 and parts[2:5] == ['views', b.manual_view_id, 'buckets'] and parts[6] == 'tasks' and method == 'PUT':
                    task = self.tasks[str(body['task_id'])]
                    require(str(task['project_id']) == b.project_id and parts[5] in b.bucket_ids.values(), 'simulator_move_scope_invalid')
                    task['buckets'] = [{'id': int(parts[5]), 'project_view_id': int(b.manual_view_id)}]
                    return httpx.Response(200, json={'task_id': body['task_id'], 'bucket_id': int(parts[5]),
                                                    'project_view_id': int(b.manual_view_id)})
                if parts[2:] == ['tasks']:
                    if method == 'GET':
                        rows = sorted([row for row in self.tasks.values() if str(row['project_id']) == b.project_id], key=lambda x: x['id'])
                        return httpx.Response(200, json=_page(rows, request))
                    if method == 'POST':
                        identifier = str(max([int(x) for x in self.tasks] + [9007199254740992]) + 1)
                        task = dict(id=int(identifier), project_id=int(b.project_id), title=body['title'], description=body['description'],
                                    due_date=body.get('due_date', ZERO_DATE), done=False, assignees=[], labels=[], repeat_after=0, repeat_mode=0,
                                    buckets=[{'id': int(b.bucket_ids['inbox']), 'project_view_id': int(b.manual_view_id)}])
                        self.tasks[identifier], self.comments[identifier] = task, []
                        return httpx.Response(201, json=deepcopy(task))
            if parts[0] == 'tasks' and parts[1] in self.tasks:
                identifier, task = parts[1], self.tasks[parts[1]]
                if len(parts) == 2 and method in ('GET', 'PATCH'):
                    if method == 'PATCH':
                        require(set(body) <= {'title', 'done', 'due_date'}, 'simulator_patch_invalid')
                        task.update(body)
                    return httpx.Response(200, json=deepcopy(task), headers={'ETag': '"simulated-' + hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest() + '"'})
                relation = parts[2] if len(parts) >= 3 else None
                if relation == 'comments':
                    if method == 'GET':
                        return httpx.Response(200, json=_page(self.comments[identifier], request))
                    if method == 'POST':
                        comment = {'id': len(self.comments[identifier]) + 1, 'comment': body['comment'], 'author': {'id': 19}}
                        self.comments[identifier].append(comment)
                        return httpx.Response(201, json=deepcopy(comment))
                if relation in ('labels', 'assignees'):
                    key = 'label_id' if relation == 'labels' else 'user_id'
                    if method == 'POST':
                        if not any(row['id'] == body[key] for row in task[relation]):
                            task[relation].append({'id': body[key]})
                        return httpx.Response(201, json={key: body[key]})
                    if method == 'DELETE' and len(parts) == 4:
                        task[relation] = [row for row in task[relation] if str(row['id']) != parts[3]]
                        return httpx.Response(204)
            raise ProbeFailure('simulator_unexpected_request')


class ProjectServices:
    def __init__(self, services):
        self.services = services

    def execute(self, claim):
        return self.services[claim.command.project_id].execute(claim)


@dataclass(repr=False)
class Fixture:
    run_root: Path
    origin: str
    db: TeamDatabase
    repository: TeamRepository
    auth: AuthRepository
    owners: tuple
    project_ids: tuple
    task_ids: tuple
    provider: SimulatedVikunja
    worker: TeamWorker
    clients: tuple
    codes: list = field(repr=False)
    sync_repository: object = None
    sync_services: dict = field(default_factory=dict)
    app: object = None
    states: tuple = STATES

    def close(self):
        for client in self.clients:
            client.close()

    def run_scenario(self, scenario):
        require(scenario in ('external_title', 'external_due', 'restore_due'), 'fixture_scenario_invalid')
        task_id, project_id = self.task_ids[0], self.project_ids[0]
        baseline = self.repository.get_projection(self.owners[0].id, task_id)
        with self.provider._lock:
            task = self.provider.tasks[task_id]
            if scenario == 'external_title':
                task['title'] = 'Внешнее изменение в симуляторе · ' + uuid4().hex[:8]
            elif scenario == 'external_due':
                task['due_date'] = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
            else:
                task['due_date'] = baseline.due_at.isoformat() if baseline.due_at else ZERO_DATE
        # Observe the simulator through the real HTTP adapter and sync service.
        return self.sync_services[project_id].sync_once()


def team_assets(dist):
    """Freeze the built Team entry's local import graph; never serve other entry bundles."""
    assets = dist / 'assets'
    pending, allowed = [], set()
    def add(reference, parent):
        require(isinstance(reference, str) and '\\' not in reference, 'fixture_asset_reference_invalid')
        parsed = urlsplit(reference)
        require(not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment,
                'fixture_asset_reference_invalid')
        if parsed.path.startswith('/team/assets/'):
            target = assets / parsed.path.removeprefix('/team/assets/')
        elif parsed.path.startswith('assets/') or parsed.path.startswith('./assets/'):
            target = dist / parsed.path.removeprefix('./')
        else:
            require(not parsed.path.startswith('/'), 'fixture_asset_reference_invalid')
            target = parent / parsed.path
        no_reparse(target)
        target = target.resolve()
        require(target.is_relative_to(assets) and target.is_file() and target.stat().st_size <= 16 * 1024 * 1024,
                'fixture_asset_reference_invalid')
        if target not in allowed:
            require(len(allowed) < 256, 'fixture_asset_graph_too_large')
            allowed.add(target)
            pending.append(target)
    class Entry(HTMLParser):
        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if tag == 'script' and attributes.get('src'):
                add(attributes['src'], dist)
            if tag == 'link' and attributes.get('rel') in ('stylesheet', 'modulepreload', 'preload', 'icon') and attributes.get('href'):
                add(attributes['href'], dist)
    entry = dist / 'team.html'
    no_reparse(entry)
    require(entry.stat().st_size <= 1024 * 1024, 'fixture_entry_too_large')
    Entry().feed(entry.read_text(encoding='utf-8'))
    total = 0
    while pending:
        path = pending.pop()
        total += path.stat().st_size
        require(total <= 32 * 1024 * 1024, 'fixture_asset_graph_too_large')
        if path.suffix == '.js':
            text = path.read_text(encoding='utf-8')
            # Vite emits ESM imports and literal paths for dynamic chunks.
            references = re.findall(r'''(?:from\s*|import\s*(?:\(\s*)?)["']([^"']+)["']''', text)
            references += re.findall(r'''["']((?:\./|assets/)[^"']+\.(?:js|css))["']''', text)
            for reference in references:
                add(reference, path.parent)
        elif path.suffix == '.css':
            for reference in re.findall(r'''url\(\s*["']?([^"')\s]+)''', path.read_text(encoding='utf-8')):
                if not reference.startswith('data:'):
                    add(reference, path.parent)
    return frozenset(allowed)


class FixtureApp:
    def __init__(self, fixture, gateway, dist):
        self.fixture, self.gateway, self.dist = fixture, gateway, dist
        self.assets = team_assets(dist)
        self.mode = 'TLS_SYNTHETIC_UI_ONLY'

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.gateway(scope, receive, send)
        request = Request(scope, receive)
        hosts = [v for k, v in scope.get('headers', ()) if k.lower() == b'host']
        if hosts != [urlsplit(self.fixture.origin).netloc.encode('ascii')]:
            return await Response(status_code=403)(scope, receive, send)
        path = scope['path']
        if path.startswith('/api/') or path == '/hooks/max':
            return await self.gateway(scope, receive, send)
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'}
        if path == '/fixture/status' and request.method == 'GET':
            status = {'simulation': True, 'run_id': self.fixture.run_root.name, 'gateway': 'real',
                      'provider': 'httpx.MockTransport'}
            if self.mode == 'HTTP_SYNTHETIC_UI_ONLY':
                status.update(mode=self.mode, tls_qualification='not_tested')
            else:
                status['tls_qualification'] = 'local_self_signed_only'
            response = JSONResponse(status, headers=headers)
            return await response(scope, receive, send)
        if path == '/fixture' and request.method in ('GET', 'POST'):
            if request.method == 'POST':
                origins = [v for k, v in scope.get('headers', ()) if k.lower() == b'origin']
                if origins != [self.fixture.origin.encode('ascii')]:
                    return await Response(status_code=403)(scope, receive, send)
                data = bytearray()
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > 2048:
                        return await Response(status_code=413)(scope, receive, send)
                try:
                    values = parse_qs(data.decode('ascii'), strict_parsing=True)
                    if set(values) == {'owner'}:
                        require(len(values['owner']) == 1 and values['owner'][0] in ('0', '1', '2'), 'fixture_owner_invalid')
                        index = int(values['owner'][0])
                        self.fixture.codes[index] = self.fixture.auth.issue_code(self.fixture.owners[index].max_user_id).value
                    else:
                        require(set(values) == {'scenario'} and len(values['scenario']) == 1, 'fixture_scenario_invalid')
                        await asyncio.to_thread(self.fixture.run_scenario, values['scenario'][0])
                except (ValueError, UnicodeError):
                    return await Response(status_code=400)(scope, receive, send)
            body = '<!doctype html><meta charset="utf-8"><title>Синтетический Team UI</title><h1>СИНТЕТИЧЕСКИЙ СТЕНД</h1>'
            body += '<p>Реальные Gateway, сессии и CSRF. Vikunja — локальный HTTP-симулятор. Нет MAX, Polza, записей или данных владельца.</p>'
            if self.mode == 'HTTP_SYNTHETIC_UI_ONLY':
                body += '<p><strong>HTTP_SYNTHETIC_UI_ONLY:</strong> TLS и MAX WebView не проверяются.</p>'
            else:
                body += '<p>Самоподписанный TLS только для локального теста. Не проверка публичного HTTPS или MAX WebView.</p>'
            body += '<a href="/team/">Открыть Team UI</a>'
            for index, owner in enumerate(self.fixture.owners):
                body += '<h2>' + html.escape(owner.display_name) + '</h2><p>Одноразовый синтетический код, 5 минут:</p><code>' + html.escape(self.fixture.codes[index]) + '</code>'
                body += '<form method="post" action="/fixture"><input type="hidden" name="owner" value="' + str(index) + '"><button>Новый синтетический код</button></form>'
            body += '<h2>Внешние изменения в HTTP-симуляторе</h2><p>Первая задача: ' + self.fixture.task_ids[0] + '. После изменения запускается настоящий цикл чтения и синхронизации.</p>'
            for key, title in (('external_title', 'Внешнее изменение названия'), ('external_due', 'Внешний неподтверждённый срок'),
                               ('restore_due', 'Восстановить подтверждённый срок')):
                body += '<form method="post" action="/fixture"><input type="hidden" name="scenario" value="' + key + '"><button>' + title + '</button></form>'
            return await HTMLResponse(body, headers=headers)(scope, receive, send)
        target = None
        if request.method in ('GET', 'HEAD') and path in ('/team/', '/team/team.html'):
            target = self.dist / 'team.html'
        elif request.method in ('GET', 'HEAD') and path.startswith('/team/assets/'):
            relative = path.removeprefix('/team/assets/')
            if relative and '\\' not in relative and all(part not in ('', '.', '..') for part in relative.split('/')):
                candidate = self.dist / 'assets' / relative
                if candidate in self.assets:
                    target = candidate
        if target is not None:
            try:
                no_reparse(target)
                require(target.resolve().is_relative_to(self.dist) and target.is_file() and target.stat().st_size <= 16 * 1024 * 1024, 'fixture_asset_invalid')
                content = target.read_bytes() if request.method == 'GET' else b''
                return await Response(content, media_type=mimetypes.guess_type(target.name)[0] or 'application/octet-stream', headers=headers)(scope, receive, send)
            except (OSError, ProbeFailure):
                pass
        return await Response(status_code=404, headers=headers)(scope, receive, send)


def build_fixture(run_root, *, frontend_dist, origin):
    """Pure local factory, accepted by offline guard; no server or network start."""
    run_root = validate_run_root(run_root, parent=Path(run_root).absolute().parent)
    require(run_root.is_dir() and not (run_root / 'team.sqlite3').exists(), 'fresh_fixture_database_required')
    dist = Path(frontend_dist).absolute()
    no_reparse(dist)
    require((dist / 'team.html').is_file(), 'built_team_entry_required')
    require(urlsplit(origin).hostname == HOSTNAME and urlsplit(origin).port is not None, 'fixture_origin_invalid')
    settings = TeamGatewaySettings(public_origin=origin, bot_id='42', bot_token='SYNTHETIC_NO_MAX_CALLS')
    db, now = TeamDatabase(run_root / 'team.sqlite3'), datetime.now(timezone.utc)
    repository = TeamRepository(db)
    projects = ('7', '9007199254741009')
    owners = tuple(TeamMember(id=str(uuid4()), display_name=name, role='owner', max_user_id=str(7001 + i),
                    vikunja_user_id=str(8001 + i), project_ids=projects if i < 2 else (projects[1],))
                   for i, name in enumerate(('Анна · синтетический владелец', 'Борис · синтетический владелец', 'Вера · другой проект')))
    for owner in owners:
        repository.upsert_member(owner, expected_revision=None)
    bindings = tuple(ProjectBinding(project_id=project, manual_view_id=str(17 + i * 10), bot_user_id='19',
        bucket_ids={state: str(201 + i * 100 + n) for n, state in enumerate(STATES)},
        important_label_id=str(101 + i * 10), urgent_label_id=str(102 + i * 10), cancelled_label_id=str(103 + i * 10))
        for i, project in enumerate(projects))
    provider, clients, services, adapters = SimulatedVikunja(bindings), [], {}, {}
    for b in bindings:
        client = httpx.Client(base_url='http://127.0.0.1:3456/api/v2/', headers={'Authorization': 'Bearer SYNTHETIC_ONLY'},
                              trust_env=False, follow_redirects=False, timeout=5, transport=httpx.MockTransport(provider))
        clients.append(client)
        adapter = VikunjaClient(client, binding=b, members=RepositoryMemberDirectory(repository))
        adapters[b.project_id] = adapter
        services[b.project_id] = TeamTaskService(repository, adapter)
    task_ids = []
    for n in range(65):
        project_index = 0 if n < 62 else 1
        b = bindings[project_index]
        state = STATES[n % len(STATES)]
        owner = owners[n % 2 if project_index == 0 else 2]
        identifier = str(9007199254740993 + n)
        classified = n % 5 != 2
        important, urgent = classified and n % 3 == 0, classified and n % 4 == 0
        today_due = now.astimezone(timezone(timedelta(hours=3))).replace(hour=18, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        due = now - timedelta(days=2) if n % 4 == 0 else (today_due if n % 4 == 1 else None)
        title = ['Просроченная важная задача', 'Сегодня: проверить смету', 'Без категории и без срока', 'Подготовить результат'][n % 4] + ' · ' + str(n + 1)
        if n == 3:
            title = 'Длинное название: ' + 'Проверить совместимость интерфейса и перенос текста. ' * 12
        labels = ([{'id': int(b.important_label_id)}] if important else []) + ([{'id': int(b.urgent_label_id)}] if urgent else [])
        if state == 'cancelled':
            labels.append({'id': int(b.cancelled_label_id)})
        remote = dict(id=int(identifier), project_id=int(b.project_id), title=title,
            description='<p>Только синтетические данные.</p><p>&lt;script&gt;alert("fixture")&lt;/script&gt;</p>',
            done=state in ('done', 'cancelled'), due_date=due.isoformat() if due else ZERO_DATE,
            assignees=[{'id': int(owner.vikunja_user_id)}], labels=labels,
            buckets=[{'id': int(b.bucket_ids[state]), 'project_view_id': int(b.manual_view_id)}], repeat_after=0, repeat_mode=0)
        provider.tasks[identifier], provider.comments[identifier] = remote, []
        baseline = TaskSnapshot(task_id=identifier, project_id=b.project_id, revision=0, remote_fingerprint='a' * 64,
            title=title, description=remote['description'], assignee_id=owner.id, bucket=state,
            important=important, urgent=urgent, classification_confirmed=classified,
            due_at=due, due_confirmed=due is not None, due_phrase='Синтетический согласованный срок' if due else None)
        verified = adapters[b.project_id].get_task(identifier, context=TaskReadContext(revision=0, baseline=baseline))
        repository.save_projection(verified, expected_revision=None)
        task_ids.append(identifier)
    auth = AuthRepository(db, secret=secrets.token_bytes(32))
    worker = TeamWorker(repository, ProjectServices(services), worker_id='t9-simulated-worker', poll_seconds=.1)
    fixture = Fixture(run_root, settings.public_origin, db, repository, auth, owners, projects, tuple(task_ids), provider,
                      worker, tuple(clients), [auth.issue_code(owner.max_user_id).value for owner in owners])
    fixture.sync_repository = TeamSyncRepository(repository)
    fixture.sync_services = {project: TeamSyncService(fixture.sync_repository, adapter) for project, adapter in adapters.items()}
    for service in fixture.sync_services.values():
        require(service.sync_once().state == 'ready', 'fixture_initial_sync_failed')
    reads = TeamReadRepository(repository, sync=fixture.sync_repository)
    fixture.app = FixtureApp(fixture, create_team_app(settings, repository, TeamGatewayClients(auth=auth, reads=reads)), dist.resolve())
    return fixture


def _write_manifest(root, data):
    no_reparse(root)
    temporary = root / 'manifest.pending'
    no_reparse(temporary)
    no_reparse(root / 'manifest.json')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(root / 'manifest.json')


def _read_manifest(root):
    target = root / 'manifest.json'
    no_reparse(target)
    require(target.is_file() and target.stat().st_size <= 16384, 'fixture_manifest_invalid')
    value = json.loads(target.read_text(encoding='utf-8'))
    require(value.get('version') == 1 and value.get('run_id') == root.name and value.get('simulation') is True,
            'fixture_manifest_invalid')
    return value


def _owner_process(root, data):
    require(type(data.get('pid')) is int and data['pid'] > 0 and type(data.get('create_time')) in (int, float), 'fixture_owner_invalid')
    try:
        process = psutil.Process(data['pid'])
        require(process.create_time() == data['create_time'], 'fixture_owner_changed')
        arguments = process.cmdline()
        require(str(SCRIPT) in arguments and 'serve' in arguments and str(root) in arguments, 'fixture_owner_changed')
        return process
    except psutil.Error:
        raise ProbeFailure('fixture_owner_unavailable') from None


def request_stop(run_root, *, parent=SCRATCH):
    root = validate_run_root(run_root, parent=parent)
    data = _read_manifest(root)
    _owner_process(root, data)
    marker = root / 'stop.request'
    no_reparse(marker)
    marker.write_text(root.name, encoding='ascii')
    return {'run_id': root.name, 'state': 'stop_requested'}


def stop_requested(root):
    marker = root / 'stop.request'
    no_reparse(marker)
    if not marker.exists():
        return False
    require(marker.is_file() and marker.stat().st_size <= 128, 'fixture_stop_marker_invalid')
    return marker.read_text(encoding='ascii') == root.name


def _certificate(root, executable):
    executable = Path(executable).absolute()
    require(executable.is_file() and executable.name.lower() == 'openssl.exe', 'local_openssl_required')
    certificate, key = root / 'localhost.crt', root / 'localhost.key'
    no_reparse(certificate)
    no_reparse(key)
    require(not certificate.exists() and not key.exists(), 'fresh_fixture_certificate_required')
    env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
    env.update(TEMP=str(root), TMP=str(root))
    args = [str(executable), 'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes', '-days', '2',
            '-subj', '/CN=' + HOSTNAME, '-addext', 'subjectAltName=DNS:' + HOSTNAME + ',DNS:localhost',
            '-keyout', str(key), '-out', str(certificate)]
    try:
        result = subprocess.run(args, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        require(result.returncode == 0 and certificate.is_file() and key.is_file(), 'fixture_certificate_failed')
    except (OSError, subprocess.TimeoutExpired):
        raise ProbeFailure('fixture_certificate_failed') from None
    return certificate, key


def _ready(root, data):
    origin = urlsplit(data['origin'])
    require(origin.scheme == 'https' and origin.hostname == HOSTNAME and origin.port, 'fixture_origin_invalid')
    context = ssl.create_default_context(cafile=str(root / 'localhost.crt'))
    connection = http.client.HTTPSConnection(HOSTNAME, origin.port, context=context, timeout=2)
    connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection(('127.0.0.1', origin.port), timeout)
    try:
        connection.request('GET', '/fixture/status')
        response = connection.getresponse()
        body = response.read(8192)
        require(response.status == 200 and json.loads(body).get('run_id') == root.name, 'fixture_readiness_failed')
    finally:
        connection.close()


async def _serve(root, dist, openssl):
    import uvicorn
    root = validate_run_root(root)
    require(root.is_dir() and all(item.name == 'lifecycle.log' for item in root.iterdir()), 'fresh_fixture_directory_required')
    certificate, key = _certificate(root, openssl)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    fixture = worker = watcher = server = data = None
    clean = False
    try:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        data = dict(version=1, run_id=root.name, pid=os.getpid(), create_time=psutil.Process().create_time(),
                    state='starting', origin=f'https://{HOSTNAME}:{port}', simulation=True,
                    certificate_sha256=hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate.read_text(encoding='ascii'))).hexdigest(),
                    max_lifetime_seconds=MAX_LIFETIME_SECONDS)
        _write_manifest(root, data)
        fixture = build_fixture(root, frontend_dist=dist, origin=data['origin'])
        server = uvicorn.Server(uvicorn.Config(fixture.app, host='127.0.0.1', port=port, ssl_certfile=str(certificate),
            ssl_keyfile=str(key), proxy_headers=False, access_log=False, log_config=None, log_level='critical',
            timeout_graceful_shutdown=5))
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
                    if stop_requested(root) or time.monotonic() >= deadline or worker.done():
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
        try:
            if fixture is not None:
                fixture.worker.stop()
            if server is not None:
                server.should_exit = True
            if worker is not None:
                await worker
            if watcher is not None:
                await watcher
        finally:
            if fixture is not None:
                fixture.close()
            sock.close()
            if data is not None:
                data['state'] = 'stopped' if clean else 'failed'
                _write_manifest(root, data)


def start(frontend_dist, openssl):
    dist = Path(frontend_dist).absolute()
    no_reparse(dist)
    require((dist / 'team.html').is_file(), 'built_team_entry_required')
    no_reparse(SCRATCH)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    root = validate_run_root(SCRATCH / ('t9-ui-' + uuid4().hex))
    root.mkdir()
    args = [sys.executable, '-B', str(SCRIPT), 'serve', '--run-root', str(root), '--frontend-dist', str(dist), '--openssl', str(Path(openssl).absolute())]
    with (root / 'lifecycle.log').open('xb') as output:
        process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            require(process.poll() is None, 'fixture_child_failed')
            if (root / 'manifest.json').is_file():
                data = _read_manifest(root)
                if data['state'] == 'ready':
                    _owner_process(root, data)
                    _ready(root, data)
                    return {**data, 'run_root': str(root), 'fixture_url': data['origin'] + '/fixture', 'team_url': data['origin'] + '/team/'}
            time.sleep(.1)
        raise ProbeFailure('fixture_start_timeout')
    except BaseException:
        # Cooperative owner-only stop on timeout/readiness failure. No PID kill.
        if process.poll() is None and (root / 'manifest.json').is_file():
            try:
                request_stop(root)
            except (ProbeFailure, OSError):
                pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'serve', 'status', 'stop'))
    parser.add_argument('--run-root', type=Path)
    parser.add_argument('--frontend-dist', type=Path, default=ROOT / 'frontend/dist')
    parser.add_argument('--openssl', type=Path, default=Path(r'C:\Program Files\Git\usr\bin\openssl.exe'))
    args = parser.parse_args()
    try:
        if args.action == 'start':
            value = start(args.frontend_dist, args.openssl)
        else:
            require(args.run_root is not None, 'fixture_run_root_required')
            root = validate_run_root(args.run_root)
            if args.action == 'serve':
                asyncio.run(_serve(root, args.frontend_dist, args.openssl))
                return 0
            if args.action == 'stop':
                value = request_stop(root)
            else:
                value = _read_manifest(root)
                if value['state'] != 'stopped':
                    _owner_process(root, value)
                    _ready(root, value)
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ProbeFailure) else 'fixture_failed'
        print(json.dumps({'error_code': code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
