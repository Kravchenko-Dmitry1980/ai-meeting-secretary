"""Explicit owner-side Secretary binding, separate from the boot gateway.

Calling the factory opens configured local databases; importing it does not.
No directory member, project, token or recording is created automatically.
"""
from contextlib import asynccontextmanager
from uuid import uuid4

from secretary.team_settings import secretary_settings, RuntimeConfigurationError


def create_secretary_team_app(config, *, run_id=None, identity_reader=None, native_job=None,
                              run_worker=True, **api_dependencies):
    from secretary.api import create_app
    from secretary.infrastructure.team_maintenance import MaintenanceRepository
    from secretary.infrastructure.team_database import TeamDatabase
    from secretary.infrastructure.team_repository import TeamRepository
    from secretary.infrastructure.team_auth_repository import AuthRepository
    from secretary.infrastructure.team_process_identity import process_identity
    from secretary.infrastructure.team_backup import validate_source_roles
    from secretary.infrastructure.maintenance_binding import restored_runtime_guard_present, write_participant_descriptor

    if restored_runtime_guard_present(config):
        raise RuntimeConfigurationError('team_restore_reconciliation_required')

    gate = MaintenanceRepository(config.control_database_path, config.deployment_id)
    gate.bind_sources({'secretary': config.secretary_database_path, 'team': config.team_database_path,
        'billing': config.billing_database_path, 'vikunja': config.vikunja_database_path},
        validate_new=lambda paths: validate_source_roles(paths, allow_missing=True))
    participant_id = 'secretary'
    run_id = run_id or str(uuid4())
    identity = (identity_reader or process_identity)()
    prior = next((value for value in gate.status()['participants']
        if value['participant_id'] == participant_id), None)
    if prior is not None and (prior['run_id'] != run_id or prior['identity'] != identity):
        from secretary.infrastructure.team_process_identity import verify_dead_identity
        from secretary.infrastructure.team_runtime_recovery import checkpoint_dead_secretary
        proof = verify_dead_identity(prior['identity'])
        checkpoint_dead_secretary(config, gate, old_participant=prior, proof=proof)
    gate.register_participant(participant_id, run_id=run_id, identity=identity)
    if native_job is not None:
        gate.register_native_containment(participant_id, native_job)
    write_participant_descriptor(gate, participant_id, role='secretary')
    directory, auth = None, None
    if config.local_owner_id:
        team_db = TeamDatabase(config.team_database_path, maintenance=gate, participant_id=participant_id)
        candidate = TeamRepository(team_db)
        owner = candidate.get_member(config.local_owner_id)
        if owner is not None and owner.enabled and owner.role == 'owner':
            directory = candidate
            secret = config.team_auth_secret.get_secret_value().encode('utf-8')
            if len(secret) >= 32:
                auth = AuthRepository(team_db, secret=secret)

    resources = []

    def publications(db):
        if directory is None or not config.publication_configured:
            return None
        owner = directory.get_member(config.local_owner_id)
        if config.publication_project_id not in owner.project_ids:
            return None
        from secretary.application.task_publications import TaskPublicationService
        from secretary.infrastructure.task_publication_repository import TaskPublicationRepository
        return TaskPublicationService(db, TaskPublicationRepository(db), directory,
            actor_id=config.local_owner_id, project_id=config.publication_project_id)

    def publication_worker(service):
        if service is None:
            return None
        import httpx
        from secretary.infrastructure.publication_bridge import LocalPublicationBridge
        from secretary.application.publication_delivery import PublicationDispatcher
        from secretary.orchestration.publication_worker import PublicationWorker
        bridge = LocalPublicationBridge(httpx.Client(trust_env=False, follow_redirects=False, timeout=10),
            base_url=f'http://127.0.0.1:{config.gateway_port}',
            secret=config.publication_service_secret.get_secret_value(), actor_id=config.local_owner_id)
        resources.append(bridge)
        dispatcher = PublicationDispatcher(service.repository, bridge, validate_dispatch=service.validate_dispatch)
        return PublicationWorker(dispatcher, worker_id='secretary-publications-' + run_id,
            maintenance=gate, participant_id=participant_id)

    app = create_app(secretary_settings(config), database_path=config.secretary_database_path,
        billing_path=config.billing_database_path, maintenance=gate, participant_id=participant_id,
        run_worker=run_worker and config.outbound_allowed, outbound_enabled=config.outbound_allowed,
        task_publications_factory=publications,
        team_auth_factory=lambda db: (auth, config.local_owner_id) if auth is not None else None,
        publication_worker_factory=publication_worker, **api_dependencies)
    app.state.team_runtime_config = config
    app.state.team_runtime_run_id = run_id
    app.state.team_directory = directory
    app.state.team_auth = auth
    original = app.router.lifespan_context

    @asynccontextmanager
    async def owned_lifespan(application):
        try:
            async with original(application):
                yield
        finally:
            for resource in resources:
                resource.close()

    app.router.lifespan_context = owned_lifespan
    return app
