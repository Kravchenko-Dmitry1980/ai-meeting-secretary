"""Explicit Team runtime composition and owned, admitted operation lifetimes.

Constructing TeamRuntime is inert. start() creates the configured local stores
and clients; only explicitly enabled outbound loops may reach providers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from secretary.team_settings import TeamRuntimeSettings, RuntimeConfigurationError, local_path, secretary_settings, _uuid, _unique


async def admitted_operation(maintenance, participant_id, kind, operation):
    """Keep the durable admission until all owned work/receipts have drained."""
    from secretary.orchestration.team_worker import TeamWorker
    async with maintenance.async_admission(participant_id, kind):
        task = asyncio.create_task(operation())
        return await TeamWorker._drain(task, asyncio.Event())


async def _unavailable(send, code='team_runtime_unavailable'):
    body = json.dumps({'code': code}, separators=(',', ':')).encode('ascii')
    await send({'type': 'http.response.start', 'status': 503,
        'headers': [(b'content-type', b'application/json'), (b'cache-control', b'no-store')]})
    await send({'type': 'http.response.body', 'body': body})


class AdmittedASGI:
    def __init__(self, app, maintenance, participant_id):
        self.app, self.maintenance, self.participant_id = app, maintenance, participant_id

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        entered = False

        async def call():
            nonlocal entered
            entered = True
            return await self.app(scope, receive, send)
        try:
            return await admitted_operation(self.maintenance, self.participant_id, 'intake', call)
        except asyncio.CancelledError:
            raise
        except Exception:
            if entered:
                raise
            return await _unavailable(send, 'team_maintenance_unavailable')


@dataclass(frozen=True)
class RuntimeDependencies:
    """Trusted explicit test/runtime dependencies, never fallback demo behavior."""
    vikunja_transport: Any = None
    max_transport: Any = None
    media_transport: Any = None
    polza_transport: Any = None
    account_reader: Any = None
    price_reader: Any = None
    identity: dict | None = None


class _TaskRouter:
    def __init__(self, services):
        self.services = services

    def execute(self, claim):
        from secretary.domain.team import TeamConflict
        service = self.services.get(claim.command.project_id)
        if service is None:
            raise TeamConflict('client_project_scope_mismatch')
        return service.execute(claim)


class _DueRouter:
    def __init__(self, team, repository, services):
        self.team, self.repository, self.services = team, repository, services

    def _task(self, actor, task_id, revision):
        value = self.team.get_projection(actor, task_id, expected_actor_revision=revision)
        return self.services[value.project_id]

    def candidate(self, actor, task_id, *, expected_actor_revision=None):
        return self._task(actor, task_id, expected_actor_revision).candidate(actor, task_id,
            expected_actor_revision=expected_actor_revision)

    def preview(self, actor, task_id, request, *, expected_actor_revision=None):
        return self._task(actor, task_id, expected_actor_revision).preview(actor, task_id, request,
            expected_actor_revision=expected_actor_revision)

    def confirm(self, actor, preview_id, request, *, expected_actor_revision=None):
        value, _ = self.repository.confirmation_state(actor, preview_id, request.operation_id,
            expected_actor_revision=expected_actor_revision)
        return self.services[value.preview.candidate.project_id].confirm(actor, preview_id, request,
            expected_actor_revision=expected_actor_revision)


class _TrustedIntake:
    def __init__(self, bot, health):
        self.bot, self.health = bot, health

    def intake(self, event):
        result = self.bot.intake(event)
        if result.state != 'quarantined' and event.kind in {'message_created', 'message_callback', 'bot_started'}:
            stored = self.bot.get_event(result.event_id)
            if stored.quarantine_reason is None:
                self.health.record_callback(result.event_id, event.kind)
        return result


class _VoiceRouter:
    """Text remains available when native audio transport is not configured."""
    def __init__(self, service, media_configured):
        self.service, self.media_configured = service, media_configured

    def enqueue(self, event, text=None, *, context=None):
        if text is None and not self.media_configured:
            from secretary.domain.bot import DeterministicReply
            # No voice job is accepted/claimed and no download/paid request occurs.
            return DeterministicReply('Нужно настроить загрузку голосовых сообщений. Владелец должен проверить подключение MAX.')
        return self.service.enqueue(event, text, context=context)

    def confirm_intent(self, *args, **kwargs):
        return self.service.confirm_intent(*args, **kwargs)


class TeamRuntime:
    def __init__(self, settings: TeamRuntimeSettings, *, run_id: str, control_dir: Path,
                 dependencies: RuntimeDependencies | None = None, clock=None, native_job=None):
        if not isinstance(settings, TeamRuntimeSettings):
            raise RuntimeConfigurationError()
        self.settings, self.run_id = settings, _uuid(run_id)
        self.control_dir = local_path(control_dir)
        if self.control_dir == settings.project_dir or not self.control_dir.is_relative_to(settings.project_dir):
            raise RuntimeConfigurationError('team_runtime_control_path_invalid')
        self.dependencies, self.clock = dependencies or RuntimeDependencies(), clock or (lambda: datetime.now(timezone.utc))
        self.native_job = native_job
        self.participant_id = 'gateway:' + self.run_id
        self.phase, self._started, self._accepting = 'starting', False, False
        self._stop, self.stop_requested = asyncio.Event(), asyncio.Event()
        self._tasks, self._workers, self._clients = [], [], []
        self.components = {}
        self.maintenance = self.team = self.auth = self.bot = self.voices = self.notifications = None
        self.health_repository = self.budget = self.max_client = self.provider = None
        self.public_app = self.internal_app = None
        self._admitted_app = None
        self._planner_resumed = True
        self._closing = None
        self._work_halted = False

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeConfigurationError('team_runtime_clock_invalid')
        return value.astimezone(timezone.utc)

    def _component(self, name, state, code=None):
        self.components[name] = {'state': state, **({'error_code': code} if code else {})}

    def health(self):
        return {'schema_version': 1, 'deployment_id': self.settings.deployment_id, 'run_id': self.run_id,
            'pid': os.getpid(), 'observed_at': self._now().isoformat(), 'phase': self.phase,
            'components': dict(self.components)}

    def _write_health(self):
        self.control_dir.mkdir(parents=True, exist_ok=True)
        target = local_path(self.control_dir / 'runtime-health.json')
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding='utf-8'))
                if (existing.get('deployment_id'), existing.get('run_id')) != (self.settings.deployment_id, self.run_id):
                    raise RuntimeConfigurationError('team_runtime_control_identity_mismatch')
            except (OSError, ValueError, TypeError):
                raise RuntimeConfigurationError('team_runtime_control_identity_mismatch') from None
        temporary = self.control_dir / ('.health-' + uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(self.health(), stream, ensure_ascii=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _write_participant_descriptor(self):
        value = self.maintenance.participant_descriptor(self.participant_id, role='gateway')
        target = local_path(self.settings.control_database_path.parent / 'gateway-participant.json')
        if target.exists():
            try:
                if target.stat().st_size > 32768:
                    raise ValueError
                prior = json.loads(target.read_text(encoding='utf-8'), object_pairs_hook=_unique)
                if prior.get('deployment_id') != self.settings.deployment_id or prior.get('role') != 'gateway':
                    raise ValueError
            except (OSError, ValueError, TypeError, AttributeError):
                raise RuntimeConfigurationError('team_runtime_descriptor_identity_mismatch') from None
        temporary = target.parent / ('.gateway-participant-' + uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _restore_guard_present(self):
        from secretary.infrastructure.maintenance_binding import restored_runtime_guard_present
        return restored_runtime_guard_present(self.settings)

    async def start(self):
        if self._started:
            return
        # Read all authorities before creating a new control database or any
        # runtime store. A restored Vikunja guard alone must stop this entry
        # point just like the Secretary, Team, and Billing guards do.
        if self._restore_guard_present():
            self._component('native_containment', 'not_configured', 'team_restore_reconciliation_required')
            self._component('gateway', 'not_configured', 'team_restore_reconciliation_required')
            self._component('outbound', 'not_configured', 'team_restore_reconciliation_required')
            self._started = True
            self.phase = 'running'
            self._write_health()
            self._tasks.append(asyncio.create_task(self._monitor(), name='team-control-monitor'))
            return
        from secretary.infrastructure.team_maintenance import MaintenanceRepository
        from secretary.infrastructure.team_backup import validate_source_roles
        from secretary.infrastructure.team_process_identity import process_identity
        self.maintenance = MaintenanceRepository(self.settings.control_database_path, self.settings.deployment_id, clock=self.clock)
        self.maintenance.bind_sources({role: getattr(self.settings, role + '_database_path')
            for role in ('secretary', 'team', 'billing', 'vikunja')},
            validate_new=lambda bound: validate_source_roles(bound, allow_missing=True))
        identity = self.dependencies.identity or process_identity()
        self.maintenance.register_participant(self.participant_id, run_id=self.run_id, identity=identity)
        if self.native_job is not None:
            self.maintenance.register_native_containment(self.participant_id, self.native_job)
            self._component('native_containment', 'healthy')
        else:
            self._component('native_containment', 'not_configured', 'native_containment_not_configured')
        self._started = True
        self._write_health()
        if self._restore_guard_present() or self.maintenance.status()['mode'] == 'restore_blocked':
            self._component('gateway', 'not_configured', 'team_restore_reconciliation_required')
            self._component('outbound', 'not_configured', 'team_restore_reconciliation_required')
        elif not self._predecessors_verified():
            self._component('gateway', 'not_configured', 'team_runtime_predecessor_unverified')
        elif self.settings.configuration_code() is not None:
            self._component('gateway', 'not_configured', self.settings.configuration_code())
        else:
            async def construct():
                await asyncio.to_thread(self._construct)
            try:
                if self._dead_predecessors:
                    async def checkpoint():
                        await asyncio.to_thread(self._checkpoint_predecessors)
                    await admitted_operation(self.maintenance, self.participant_id, 'crash_recovery', checkpoint)
                    self._component('recovery', 'healthy')
                await admitted_operation(self.maintenance, self.participant_id, 'job_startup', construct)
                self._write_participant_descriptor()
                self._accepting = True
                if self.settings.outbound_allowed:
                    identity_verified = await self._verify_max_identity()
                    self._start_loops(identity_verified)
                else:
                    self._component('outbound', 'not_configured', 'team_outbound_disabled')
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception as error:
                from secretary.infrastructure.team_maintenance import MaintenanceError
                code = error.code if isinstance(error, (RuntimeConfigurationError, MaintenanceError)) else 'team_runtime_setup_unavailable'
                self._component('gateway', 'not_configured', code)
                self._accepting = False
        self.phase = 'running'
        self._write_health()
        self._tasks.append(asyncio.create_task(self._monitor(), name='team-control-monitor'))

    def _predecessors_verified(self):
        """A live or uninspectable prior Gateway remains authoritative."""
        from secretary.infrastructure.team_process_identity import verify_dead_identity
        self._dead_predecessors = []
        try:
            for participant in self.maintenance.status()['participants']:
                if participant['participant_id'] == self.participant_id:
                    continue
                if participant['participant_id'].startswith('gateway:'):
                    proof = verify_dead_identity(participant['identity'])
                    tickets = self.maintenance.active_operations(participant['participant_id'])
                    self._dead_predecessors.append((participant, tickets, proof))
            return True
        except ValueError:
            return False

    def _checkpoint_predecessors(self):
        from secretary.infrastructure.team_database import TeamDatabase
        from secretary.infrastructure.budget_repository import BudgetRepository
        from secretary.infrastructure.team_runtime_recovery import checkpoint_dead_gateway, preflight_dead_gateway
        for participant, tickets, proof in self._dead_predecessors:
            preflight_dead_gateway(self.maintenance, participant, tickets, proof,
                team_database_path=self.settings.team_database_path,
                billing_database_path=self.settings.billing_database_path, clock=self.clock)
        # All predecessor inventories are verified read-only before either
        # constructor can migrate a source schema or create the shared ledger.
        represented = tuple(value for value in self._dead_predecessors if value[1])
        if not represented:
            return
        db = TeamDatabase(self.settings.team_database_path, maintenance=self.maintenance, participant_id=self.participant_id)
        billing = BudgetRepository(self.settings.billing_database_path, maintenance=self.maintenance, participant_id=self.participant_id)
        for participant, tickets, proof in represented:
            checkpoint = checkpoint_dead_gateway(self.maintenance, participant, tickets, proof,
                team_database=db, billing_repository=billing, clock=self.clock)
            self.maintenance.retire_dead_participant(checkpoint, proof=proof)

    def _construct(self):
        import httpx
        from secretary.infrastructure.team_database import TeamDatabase
        from secretary.infrastructure.team_repository import TeamRepository, RepositoryMemberDirectory
        from secretary.infrastructure.team_auth_repository import AuthRepository
        from secretary.infrastructure.bot_repository import BotRepository
        from secretary.infrastructure.voice_repository import VoiceRepository
        from secretary.infrastructure.notification_repository import NotificationRepository
        from secretary.infrastructure.team_sync_repository import TeamSyncRepository
        from secretary.infrastructure.team_read_repository import TeamReadRepository
        from secretary.infrastructure.team_dashboard_repository import TeamDashboardRepository
        from secretary.infrastructure.team_runtime_health import RuntimeHealthRepository
        from secretary.infrastructure.vikunja import VikunjaClient, ProjectBinding, ProvisionedTokenBinding
        from secretary.infrastructure.max_bot import MaxClient
        from secretary.infrastructure.polza import PolzaClient
        from secretary.infrastructure.polza_account import PolzaAccountClient
        from secretary.infrastructure.polza_prices import PolzaPriceClient
        from secretary.infrastructure.cloud_budget_factory import build_monthly_budget
        from secretary.application.team_tasks import TeamTaskService
        from secretary.application.team_sync import TeamSyncService
        from secretary.application.voice_commands import VoiceCommandService
        from secretary.application.bot_commands import BotCommandService
        from secretary.application.reminders import ReminderScheduler
        from secretary.application.notification_sender import NotificationSender
        from secretary.interface.team_gateway import TeamGatewaySettings, TeamGatewayClients, create_team_app
        from secretary.interface.internal_publications import create_publication_gateway
        from secretary.orchestration.team_worker import TeamWorker
        from secretary.orchestration.bot_worker import BotInboxWorker, BotOutboxWorker
        from secretary.orchestration.voice_worker import VoiceWorker
        from secretary.orchestration.notification_worker import NotificationWorker
        cfg, deps = self.settings, self.dependencies
        db = TeamDatabase(cfg.team_database_path, maintenance=self.maintenance, participant_id=self.participant_id)
        self.team = TeamRepository(db, clock=self.clock)
        owner = self.team.get_member(cfg.local_owner_id)
        projects = tuple(binding.project_id for binding in cfg.project_bindings)
        if owner is None or not owner.enabled or owner.role != 'owner' or not set(projects) <= set(owner.project_ids):
            raise RuntimeConfigurationError('team_owner_configuration_required')
        self.auth = AuthRepository(db, secret=cfg.team_auth_secret.get_secret_value().encode('utf-8'), clock=self.clock)
        self.bot = BotRepository(db, self.team, self.auth, clock=self.clock)
        self.voices = VoiceRepository(db, self.team, self.bot, clock=self.clock)
        self.notifications = NotificationRepository(self.team, clock=self.clock)
        self.sync_repository = TeamSyncRepository(self.team, clock=self.clock)
        self.health_repository = RuntimeHealthRepository(cfg.control_database_path, cfg.deployment_id, team=self.team, clock=self.clock)
        self.health_repository.configure_subscription(cfg.public_origin + '/hooks/max', ('message_created', 'message_callback', 'bot_started'))
        directory = RepositoryMemberDirectory(self.team)
        credential = ProvisionedTokenBinding(**cfg.vikunja_credential_binding.model_dump()) if cfg.vikunja_credential_binding else None
        self.native_clients, self.sync_services, task_services = {}, {}, {}
        for value in cfg.project_bindings:
            client = httpx.Client(base_url=cfg.vikunja_base_url, headers={'Authorization': 'Bearer ' + cfg.vikunja_token.get_secret_value()},
                transport=deps.vikunja_transport, trust_env=False, follow_redirects=False, timeout=10)
            self._clients.append(client)
            native = VikunjaClient(client, binding=ProjectBinding(**value.model_dump()), members=directory, credential_binding=credential)
            self.native_clients[value.project_id] = native
            self.sync_services[value.project_id] = TeamSyncService(self.sync_repository, native,
                worker_id=self.participant_id + ':sync:' + value.project_id)
            task_services[value.project_id] = TeamTaskService(self.team, native)
        max_http = httpx.Client(transport=deps.max_transport or httpx.HTTPTransport(retries=0),
            trust_env=False, follow_redirects=False, timeout=10)
        self._clients.append(max_http)
        self.max_client = MaxClient(cfg.max_bot_token.get_secret_value(), bot_id=cfg.max_bot_id, client=max_http)
        cloud = secretary_settings(cfg)
        self.account_reader = deps.account_reader or PolzaAccountClient(cloud, transport=deps.polza_transport, clock=self.clock)
        self.budget = build_monthly_budget(cloud, account_reader=self.account_reader, clock=self.clock,
            billing_path=cfg.billing_database_path, maintenance=self.maintenance, participant_id=self.participant_id)
        key_tag = self.account_reader.key_tag if cfg.polza_api_key.get_secret_value() else None
        self.provider = PolzaClient(cloud, transport=deps.polza_transport, budget=self.budget,
            charge_context={'key_tag': key_tag}, price_reader=deps.price_reader or PolzaPriceClient(transport=deps.polza_transport, clock=self.clock),
            maintenance=self.maintenance, participant_id=self.participant_id)
        media = None
        if cfg.ffmpeg_path and cfg.ffprobe_path and cfg.voice_scratch_path:
            from secretary.infrastructure.max_media import MAXMediaClient
            media_http = httpx.Client(transport=deps.media_transport or httpx.HTTPTransport(retries=0, http1=True, http2=False,
                limits=httpx.Limits(max_keepalive_connections=0)), trust_env=False, follow_redirects=False, timeout=10)
            self._clients.append(media_http)
            media = MAXMediaClient(client=media_http, allowlisted_hosts=cfg.media_allowlisted_hosts,
                ffmpeg_path=cfg.ffmpeg_path, ffprobe_path=cfg.ffprobe_path, scratch_dir=cfg.voice_scratch_path, clock=self.clock)
        self.media = media
        voice = VoiceCommandService(self.team, self.bot, self.voices, media, self.provider, clock=self.clock)
        commands = BotCommandService(self.team, self.bot, public_origin=cfg.public_origin, bot_username=cfg.max_bot_username,
            clock=self.clock, voice=_VoiceRouter(voice, media is not None))
        sender = NotificationSender(self.team, self.native_clients, self.max_client, clock=self.clock, budget_reader=self.budget.snapshot)
        self.scheduler = ReminderScheduler(clock=self.clock)
        self.workers = {
            'task_worker': (TeamWorker(self.team, _TaskRouter(task_services), worker_id=self.participant_id + ':task'), 'outbound_task'),
            'bot_inbox': (BotInboxWorker(self.bot, commands, worker_id=self.participant_id + ':inbox'), 'job_bot'),
            'bot_outbox': (BotOutboxWorker(self.bot, self.max_client, self.auth, worker_id=self.participant_id + ':bot'), 'outbound_bot'),
            'voice_worker': (VoiceWorker(self.voices, voice, worker_id=self.participant_id + ':voice'), 'job_voice'),
            'voice_outbox': (BotOutboxWorker(self.voices, self.max_client, self.auth, worker_id=self.participant_id + ':notice'), 'outbound_bot'),
            'notification_worker': (NotificationWorker(self.notifications, sender, worker_id=self.participant_id + ':reminder'), 'outbound_notification'),
        }
        self._workers = [value[0] for value in self.workers.values()]
        reads = TeamReadRepository(self.team, sync=self.sync_repository, budget_reader=self.budget.snapshot)
        dashboard = TeamDashboardRepository(self.team, reads=reads, bot=self.bot, voices=self.voices,
            notifications=self.notifications, webhook_reader=self.health_repository.read_webhook)
        from secretary.infrastructure.team_due_resolution_repository import TeamDueResolutionRepository
        from secretary.application.team_due_resolution import TeamDueResolutionService
        due_repository = TeamDueResolutionRepository(self.team, clock=self.clock)
        due_services = {project: TeamDueResolutionService(due_repository, client) for project, client in self.native_clients.items()}
        clients = TeamGatewayClients(auth=self.auth, bot_intake=_TrustedIntake(self.bot, self.health_repository), reads=reads,
            dashboard=dashboard, due_resolution=_DueRouter(self.team, due_repository, due_services))
        self.public_app = create_team_app(TeamGatewaySettings(public_origin=cfg.public_origin, bot_id=cfg.max_bot_id,
            bot_token=cfg.max_bot_token, webhook_secret=cfg.max_webhook_secret), self.team, clients)
        if cfg.publication_configured:
            self.internal_app = create_publication_gateway(self.team, cfg.publication_service_secret.get_secret_value(), cfg.local_owner_id)
            self._component('publication_gateway', 'healthy')
        else:
            self._component('publication_gateway', 'not_configured', 'publication_configuration_required')
        self._admitted_app = AdmittedASGI(self._dispatch, self.maintenance, self.participant_id)
        self._component('gateway', 'healthy')
        self._component('media', 'healthy' if media is not None else 'not_configured',
            None if media is not None else 'max_media_configuration_required')

    async def _verify_max_identity(self):
        async def verify():
            return await asyncio.to_thread(self.max_client.read_bot_identity)
        try:
            await admitted_operation(self.maintenance, self.participant_id, 'sync_max_identity', verify)
            self._component('max_identity', 'healthy')
            return True
        except Exception as error:
            mismatch = getattr(error, 'code', '') in {'max_bot_identity_mismatch', 'max_configuration_invalid'}
            self._component('max_identity', 'not_configured' if mismatch else 'degraded',
                'max_bot_identity_mismatch' if mismatch else 'max_identity_unavailable')
            return False

    def _start_loops(self, identity_verified):
        if not identity_verified and self.components['max_identity'].get('error_code') == 'max_bot_identity_mismatch':
            self._halt_identity_mismatch()
            return
        for name, (worker, kind) in self.workers.items():
            if name == 'task_worker':
                self._tasks.append(asyncio.create_task(self._loop(name, kind, worker.run_once, 1), name='team-' + name))
        for project, service in self.sync_services.items():
            async def synchronize(service=service):
                result = await asyncio.to_thread(service.sync_once)
                if result.state != 'ready':
                    raise RuntimeConfigurationError('team_sync_degraded')
                return result
            self._tasks.append(asyncio.create_task(self._loop('sync_' + project, 'sync', synchronize, 30, maximum=300), name='team-sync-' + project))
        self._tasks.append(asyncio.create_task(self._loop('reminder_planner', 'job_planning', self._plan, 1), name='team-reminder-planner'))
        if identity_verified:
            self._start_max_loops()
        else:
            self._tasks.append(asyncio.create_task(self._retry_identity(), name='team-max-identity'))
        if self.settings.polza_api_key.get_secret_value():
            self._tasks.append(asyncio.create_task(self._loop('budget', 'job_budget_refresh', self._budget_refresh, 30, maximum=300), name='team-budget'))
        else:
            self._component('budget', 'not_configured', 'monthly_budget_configuration')
        if self.media is not None:
            async def cleanup():
                return await asyncio.to_thread(self.media.cleanup_expired, now=self._now())
            self._tasks.append(asyncio.create_task(self._loop('media_cleanup', 'job_cleanup', cleanup, 3600), name='team-media-cleanup'))

    def _start_max_loops(self):
        if self._stop.is_set() or self._work_halted or getattr(self, '_max_loops_started', False):
            return
        self._max_loops_started = True
        for name, (worker, kind) in self.workers.items():
            if name != 'task_worker':
                self._tasks.append(asyncio.create_task(self._loop(name, kind, worker.run_once, 1), name='team-' + name))
        self._tasks.append(asyncio.create_task(self._loop('max_subscription', 'job_subscription', self._subscription, 60, maximum=300), name='team-subscription'))

    async def _retry_identity(self):
        failures = 0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(300, 30 * 2 ** min(failures, 3)))
                return
            except TimeoutError:
                pass
            if await self._verify_max_identity():
                self._start_max_loops()
                return
            failures += 1
            if self.components['max_identity'].get('error_code') == 'max_bot_identity_mismatch':
                self._halt_identity_mismatch()
                return

    def _halt_identity_mismatch(self):
        self._work_halted, self._accepting = True, False
        self._component('gateway', 'not_configured', 'max_bot_identity_mismatch')
        self._component('outbound', 'not_configured', 'max_bot_identity_mismatch')
        for worker in self._workers:
            worker.stop()

    async def _loop(self, name, kind, operation, period, *, maximum=30):
        failures = 0
        next_tick = time.monotonic()
        while not self._stop.is_set():
            if self._work_halted:
                self._component(name, 'not_configured', 'max_bot_identity_mismatch')
                return
            if failures:
                next_tick = time.monotonic()
            try:
                await admitted_operation(self.maintenance, self.participant_id, kind, operation)
                failures = 0
                self._component(name, 'healthy')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                code = getattr(error, 'code', None)
                self._component(name, 'degraded', code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,80}', code)
                    else name.split('_')[0] + '_unavailable')
            if self._stop.is_set():
                return
            now = time.monotonic()
            if failures:
                # Failure backoff starts after the attempt completes; its
                # retry becomes the new cadence anchor after recovery.
                delay = min(maximum, period * 2 ** min(failures, 8))
                next_tick = now + delay
            else:
                # Advance the unconsumed monotonic tick, skipping overruns
                # without queuing or overlapping a completed operation.
                next_tick += period
                if next_tick < now:
                    next_tick += math.ceil((now - next_tick) / period) * period
                delay = max(0, next_tick - now)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _plan(self):
        now = self._now()
        def plan():
            budget = self.budget.snapshot(now)
            snapshot = self.notifications.planning_snapshot(tuple(self.native_clients), now=now,
                resumed=self._planner_resumed, budget=budget)
            intents = self.scheduler.plan(now, snapshot)
            self.notifications.commit_plan(snapshot, intents, planned_at=now)
        await asyncio.to_thread(plan)
        self._planner_resumed = False

    async def _budget_refresh(self):
        await self.budget.ensure_account(key_tag=self.account_reader.key_tag)
        snapshot = self.budget.snapshot()
        if snapshot.paused_code or snapshot.remaining_micro == 0:
            raise RuntimeConfigurationError(snapshot.paused_code or 'monthly_budget_exhausted')

    async def _subscription(self):
        from secretary.domain.bot import MaxSubscription
        own_url, types = self.settings.public_origin + '/hooks/max', ('message_created', 'message_callback', 'bot_started')
        def inspect():
            try:
                observations = self.max_client.read_subscription()
                own = [item for item in observations if item['url'] == own_url]
                present = len(own) == 1 and set(own[0]['update_types']) == set(types)
                self.health_repository.record_subscription('present' if present else 'missing',
                    observed_url=own_url, update_types=types if present else ())
            except Exception:
                self.health_repository.record_subscription('unavailable', observed_url=own_url)
                raise
            if present:
                return
            if not self.settings.subscription_reconcile_enabled:
                raise RuntimeConfigurationError('max_subscription_missing')
            prepared = self.health_repository.prepared_attempt()
            if prepared is None and not self.health_repository.can_begin_subscription():
                raise RuntimeConfigurationError('max_subscription_uncertain')
            operation_id = prepared or str(uuid4())
            with self.maintenance.admission(self.participant_id, 'outbound_subscription', operation_id):
                self.health_repository.begin_subscription_attempt(operation_id)
                self.health_repository.mark_subscription_submitted(operation_id)
                receipt = self.max_client.register_own_subscription(MaxSubscription(own_url, types, self.settings.max_webhook_secret.get_secret_value()))
                self.health_repository.complete_subscription_attempt(operation_id, receipt.state)
            if receipt.state != 'verified':
                raise RuntimeConfigurationError('max_subscription_' + receipt.state)
        await asyncio.to_thread(inspect)

    async def _dispatch(self, scope, receive, send):
        path = scope.get('path', '')
        raw = scope.get('raw_path', path.encode('ascii', errors='replace'))
        exact = raw == path.encode('ascii', errors='replace')
        internal = (scope.get('method') == 'POST' and path == '/internal/v1/task-publications'
            or scope.get('method') == 'GET' and re.fullmatch(r'/internal/v1/task-publications/[0-9a-f-]{36}', path))
        if exact and internal:
            if self.internal_app is None:
                return await _unavailable(send, 'publication_configuration_required')
            return await self.internal_app(scope, receive, send)
        return await self.public_app(scope, receive, send)

    async def app(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                message = await receive()
                if message['type'] == 'lifespan.startup':
                    try:
                        await self.start()
                    except Exception:
                        await send({'type': 'lifespan.startup.failed', 'message': 'team_runtime_startup_unavailable'})
                        return
                    await send({'type': 'lifespan.startup.complete'})
                elif message['type'] == 'lifespan.shutdown':
                    await self.close()
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        elif scope['type'] == 'http':
            if not self._accepting or self._admitted_app is None:
                return await _unavailable(send)
            return await self._admitted_app(scope, receive, send)
        else:
            await send({'type': 'websocket.close', 'code': 1008})

    def _requested_stop(self):
        path = local_path(self.control_dir / 'stop.request.json')
        if not path.exists():
            return False
        try:
            if path.stat().st_size > 4096:
                return False
            value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique)
            if set(value) != {'schema_version', 'deployment_id', 'run_id', 'request_id', 'requested_at'}:
                return False
            _uuid(value['request_id'])
            stamp = datetime.fromisoformat(value['requested_at'].replace('Z', '+00:00'))
            return (type(value['schema_version']) is int and value['schema_version'] == 1 and value['deployment_id'] == self.settings.deployment_id
                and value['run_id'] == self.run_id and stamp.tzinfo is not None
                and -30 <= (self._now() - stamp.astimezone(timezone.utc)).total_seconds() <= 86400)
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    async def _monitor(self):
        while not self._stop.is_set():
            self._write_health()
            if self._requested_stop():
                self.stop_requested.set()
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except TimeoutError:
                pass

    async def close(self):
        if self.phase == 'stopped':
            return
        from secretary.orchestration.team_worker import TeamWorker
        if self._closing is None:
            self._closing = asyncio.create_task(self._close_owned(), name='team-runtime-drain')
        return await TeamWorker._drain(self._closing, self._stop)

    async def _close_owned(self):
        self.phase, self._accepting = 'draining', False
        self._stop.set()
        if self._started:
            self._write_health()
        for worker in self._workers:
            worker.stop()
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for client in self._clients:
            await asyncio.to_thread(client.close)
        self.phase = 'stopped'
        if self._started:
            self._write_health()
