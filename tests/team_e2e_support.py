"""T13 composed fixtures: real local adapters; external wire boundaries are mocks.

No listener, provider, environment, device or process lifecycle is used here.
The reusable native simulator is imported inertly, never started as a server.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta, timezone
from functools import lru_cache
from importlib import util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from secretary.application.bot_commands import BotCommandService
from secretary.application.team_tasks import TeamTaskService
from secretary.domain.team import TaskSnapshot
from secretary.infrastructure.bot_repository import BotRepository
from secretary.infrastructure.max_bot import MaxClient
from secretary.infrastructure.publication_bridge import LocalPublicationBridge
from secretary.infrastructure.team_auth_repository import AuthRepository
from secretary.infrastructure.team_repository import RepositoryMemberDirectory
from secretary.infrastructure.vikunja import ProjectBinding, TaskReadContext, VikunjaClient
from secretary.interface.internal_publications import create_publication_gateway
from secretary.interface.team_gateway import TeamGatewayClients, TeamGatewaySettings, create_team_app
from secretary.orchestration.bot_worker import BotInboxWorker, BotOutboxWorker
from secretary.orchestration.team_worker import TeamWorker

ORIGIN = 'https://team.example.test'
WEBHOOK_SECRET = 'SYNTHETIC_T13_WEBHOOK_SECRET'
PUBLICATION_SECRET = 'SYNTHETIC_T13_PUBLICATION_SECRET'
BOT_ID = '42'


@lru_cache(maxsize=1)
def _simulator_type():
    path = Path(__file__).resolve().parents[1] / 'scripts/team/probe_team_ui.py'
    spec = util.spec_from_file_location('t13_synthetic_vikunja', path)
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SimulatedVikunja


def make_native(team, clock, *, binding=None, transport_hook=None):
    binding = binding or ProjectBinding('7', '17', '19',
        dict(zip(('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'),
            map(str, range(31, 38)))), '41', '42', '43')
    simulator = _simulator_type()((binding,))
    def transport(request):
        response = simulator(request)
        return transport_hook(request, response) if transport_hook else response
    http = httpx.Client(base_url='http://127.0.0.1:34891/api/v2/',
        headers={'Authorization': 'Bearer SYNTHETIC_T13_NATIVE_TOKEN'},
        trust_env=False, follow_redirects=False, timeout=5, transport=httpx.MockTransport(transport))
    client = VikunjaClient(http, binding=binding, members=RepositoryMemberDirectory(team))
    service = TeamTaskService(team, client)
    worker = TeamWorker(team, service, worker_id='t13-team-worker')
    return SimpleNamespace(team=team, clock=clock, binding=binding, simulator=simulator,
        http=http, client=client, service=service, worker=worker)


def seed_task(native, team, owner, member, *, due_at=None, title='Synthetic task',
              task_id='9007199254740997', important=False, urgent=False, bucket='accepted'):
    binding = native.binding
    labels = ([{'id': int(binding.important_label_id)}] if important else [])
    labels += ([{'id': int(binding.urgent_label_id)}] if urgent else [])
    native.simulator.tasks[task_id] = dict(id=int(task_id), project_id=int(binding.project_id),
        title=title, description='<p>Synthetic T13 task</p>', done=False,
        due_date=due_at.astimezone(timezone.utc).isoformat() if due_at else '0001-01-01T00:00:00Z',
        assignees=[{'id': int(member.vikunja_user_id)}], labels=labels,
        buckets=[{'id': int(binding.bucket_ids[bucket]), 'project_view_id': int(binding.manual_view_id)}],
        repeat_after=0, repeat_mode=0)
    native.simulator.comments[task_id] = []
    observed = native.client.observe_task(task_id)
    initial = TaskSnapshot(task_id=task_id, project_id=binding.project_id, revision=0,
        remote_fingerprint=observed.remote_fingerprint, title=observed.title,
        description=observed.description, assignee_id=member.id, bucket=bucket,
        important=important, urgent=urgent, classification_confirmed=True,
        due_at=observed.due_at, due_confirmed=True)
    mapped = native.client.get_task(task_id, context=TaskReadContext(0, baseline=initial))
    team.save_projection(mapped, expected_revision=None)
    assert team.get_projection(owner.id, task_id) == mapped
    return mapped


def make_max(clock, *, bot_id=BOT_ID):
    control = SimpleNamespace(requests=[], accepted=[], mode='sent')
    def transport(request):
        assert request.method == 'POST' and request.url.path == '/messages'
        assert request.url.host == 'platform-api2.max.ru'
        body = json.loads(request.content)
        user_id = request.url.params['user_id']
        control.requests.append(request)
        control.accepted.append((user_id, body))
        if control.mode == 'accepted_lost_ack':
            raise httpx.ReadTimeout('Synthetic accepted MAX response lost', request=request)
        return httpx.Response(200, json={'message': {'sender': {'user_id': int(bot_id)},
            'recipient': {'user_id': int(user_id), 'chat_type': 'dialog', 'chat_id': 1},
            'timestamp': int((clock() - timedelta(seconds=7)).timestamp() * 1000),
            'body': {'mid': 'synthetic-t13-mid-' + str(len(control.accepted)), 'text': body['text']}}})
    http = httpx.Client(trust_env=False, follow_redirects=False, timeout=5,
        transport=httpx.MockTransport(transport))
    return SimpleNamespace(client=MaxClient('SYNTHETIC_T13_MAX_TOKEN', bot_id=bot_id, client=http),
        http=http, control=control)


def make_bot(team, clock, *, voice=None, auth=None, repository=None):
    auth = auth or AuthRepository(team.db, secret=b'SYNTHETIC_T13_AUTH_SECRET_32_BYTES', clock=clock)
    repository = repository or BotRepository(team.db, team, auth, clock=clock)
    commands = BotCommandService(team, repository, public_origin=ORIGIN, voice=voice, clock=clock)
    inbox = BotInboxWorker(repository, commands, worker_id='t13-inbox')
    app = create_team_app(TeamGatewaySettings(public_origin=ORIGIN, bot_id=BOT_ID,
        bot_token='SYNTHETIC_T13_MAX_TOKEN', webhook_secret=WEBHOOK_SECRET), team,
        TeamGatewayClients(auth=auth, bot_intake=repository))
    return SimpleNamespace(auth=auth, repository=repository, commands=commands, inbox=inbox, app=app)


def message_wire(member, clock, *, identifier, text=None, media=None):
    body = {'mid': identifier, 'seq': 1, 'text': text}
    if media:
        body['attachments'] = media
    return {'update_type': 'message_created', 'timestamp': int(clock().timestamp() * 1000),
        'message': {'sender': {'user_id': int(member.max_user_id), 'is_bot': False},
            'recipient': {'user_id': int(BOT_ID), 'chat_type': 'dialog', 'chat_id': int(member.max_user_id)},
            'body': body}}


def callback_wire(member, clock, *, identifier, payload):
    return {'update_type': 'message_callback', 'timestamp': int(clock().timestamp() * 1000),
        'message': None, 'callback': {'callback_id': identifier,
            'user': {'user_id': int(member.max_user_id), 'is_bot': False}, 'payload': payload}}


@contextmanager
def internal_bridge(team, owner):
    app = create_publication_gateway(team, PUBLICATION_SECRET, owner.id)
    control = SimpleNamespace(requests=[], lose_ack=False)
    with TestClient(app, base_url='http://127.0.0.1:8766', client=('127.0.0.1', 43123)) as asgi:
        def forward(request):
            control.requests.append(request)
            response = asgi.request(request.method, str(request.url), content=request.content,
                headers=dict(request.headers))
            if control.lose_ack and request.method == 'POST':
                control.lose_ack = False
                assert response.status_code == 202
                raise httpx.ReadTimeout('Synthetic accepted Gateway ACK lost', request=request)
            return httpx.Response(response.status_code, headers=dict(response.headers), content=response.content)
        http = httpx.Client(base_url='http://127.0.0.1:8766', trust_env=False,
            follow_redirects=False, transport=httpx.MockTransport(forward))
        bridge = LocalPublicationBridge(http, base_url='http://127.0.0.1:8766',
            secret=PUBLICATION_SECRET, actor_id=owner.id)
        try:
            yield SimpleNamespace(bridge=bridge, control=control, app=app)
        finally:
            bridge.close()


def outbox(bot, max_client, *, voices=None):
    return BotOutboxWorker(voices or bot.repository, max_client, bot.auth, worker_id='t13-outbox')
