"""T12 dead gateway checkpoints against temporary real schemas, without OS/network."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
import hashlib
import os
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4
from sqlite_test_paths import sqlite_path

import pytest

from secretary.application.monthly_budget import MonthlyBudget
from secretary.domain.cloud_budget import AccountUsage, CloudCharge
from secretary.domain.team import TaskCommand, TaskSnapshot, TeamMember
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_maintenance import MaintenanceError, MaintenanceRepository, RecoveryCheckpoint
from secretary.infrastructure.team_process_identity import DeadProcessProof
from secretary.infrastructure.team_repository import TeamRepository
from secretary.infrastructure.team_sync_repository import TeamSyncRepository


NAME = 'secretary.infrastructure.team_runtime_recovery'
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def uid():
    return str(uuid4())


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise AssertionError('Recovery fixtures forbid real network and process inspection')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    import secretary.infrastructure.team_process_identity as identity
    monkeypatch.setattr(identity, 'verify_dead_identity', denied)
    original = sqlite3.connect
    def scoped(database, *args, **kwargs):
        value = sqlite_path(database, uri=kwargs.get('uri', False))
        assert value.is_relative_to(tmp_path.resolve()), 'Only fresh synthetic DB paths allowed'
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', scoped)


def implementation():
    assert importlib.util.find_spec(NAME), 'T12 scoped runtime recovery module missing'
    return importlib.import_module(NAME)


def test_recovery_fixture_uri_containment_still_rejects_parent_escape(tmp_path):
    value = 'file:' + quote((tmp_path / '..' / ('outside-' + uid() + '.sqlite3')).as_posix(), safe='/:') + '?mode=ro'
    with pytest.raises(AssertionError, match='Only fresh synthetic DB paths allowed'):
        sqlite3.connect(value, uri=True)


def case(tmp_path, monkeypatch):
    module = implementation()
    old_run, new_run = uid(), uid()
    old, new = 'gateway:' + old_run, 'gateway:' + new_run
    maintenance = MaintenanceRepository(tmp_path / 'control.sqlite3', uid(), clock=lambda: NOW)
    maintenance.bind_sources({role: tmp_path / (role + '.sqlite3')
        for role in ('secretary', 'team', 'billing', 'vikunja')})
    # Trusted dependency for recovery units, not proof of an actual Windows Job.
    monkeypatch.setattr(maintenance,'has_native_containment',lambda participant,run:True,raising=False)
    identity = {'pid': 12345, 'creation_time': '987654321', 'executable_sha256': 'a'*64, 'argv_sha256': 'b'*64}
    maintenance.register_participant(old, run_id=old_run, identity=identity)
    maintenance.register_participant(new, run_id=new_run, identity={**identity, 'pid': 54321})
    participant = next(p for p in maintenance.status()['participants'] if p['participant_id'] == old)
    proof = DeadProcessProof(**identity, state='exited', observed_at=NOW.isoformat())
    calls = []
    def verified(actual):
        assert actual == identity
        calls.append(actual)
        return proof
    monkeypatch.setattr(module, 'verify_dead_identity', verified)
    import secretary.infrastructure.team_process_identity as identities
    monkeypatch.setattr(identities, 'verify_dead_identity', verified)
    db = TeamDatabase(tmp_path / 'team.sqlite3', maintenance=maintenance, participant_id=new)
    team = TeamRepository(db, clock=lambda: NOW)
    owner = TeamMember(id=uid(), display_name='Synthetic owner', role='owner', max_user_id='11',
        vikunja_user_id='21', project_ids=('7', '8'))
    team.upsert_member(owner, expected_revision=None)
    operations = []
    for task_id, worker in (('55', old+':task'), ('56', new+':task')):
        task = TaskSnapshot(task_id=task_id, project_id='7', revision=0, remote_fingerprint='c'*64,
            title='Synthetic task', assignee_id=owner.id)
        team.save_projection(task, expected_revision=None)
        command = TaskCommand(operation_id=uid(), project_id='7', task_id=task_id,
            expected_revision=0, expected_fingerprint=task.remote_fingerprint,
            action='set_state', values={'bucket':'doing'})
        team.accept_command(owner.id, command)
        team.claim_command(worker)
        operations.append(command.operation_id)
    billing = BudgetRepository(tmp_path / 'billing.sqlite3', maintenance=maintenance, participant_id=new)
    ledger = MonthlyBudget(billing, clock=lambda: NOW)
    ledger.refresh_account(AccountUsage('synthetic-key-tag', 3_000_000000, 3_000_000000, 0, 'monthly', NOW))
    charged, reserved, foreign_charge = uid(), uid(), uid()
    for operation in (charged, reserved, foreign_charge):
        ledger.reserve(CloudCharge(operation, 'voice_intent', 'd'*64, 20_000, 50_000), key_tag='synthetic-key-tag')
    ledger.mark_submitted(charged)
    ledger.mark_submitted(foreign_charge)
    ids = {'voice_pending':uid(), 'voice_cached':uid(), 'voice_foreign':uid(),
        'bot':uid(), 'bot_foreign':uid(), 'reply':uid(), 'notice':uid(),
        'notification':uid(), 'notification_preparing':uid()}
    with db.transaction() as conn:
        for name in ('bot', 'bot_foreign', 'voice_pending', 'voice_cached', 'voice_foreign'):
            identifier = ids[name]
            conn.execute('INSERT INTO bot_events VALUES(?,?,?,?,?,?,?,?)',
                (identifier, '42', 'message_created', identifier, 'e'*64, 'f'*64, '{}', NOW.timestamp()))
            if name.startswith('bot'):
                conn.execute('INSERT INTO bot_event_state VALUES(?,?,?,?,?,?)',
                    (identifier, 'processing', (old if name=='bot' else new)+':inbox', 4, NOW.timestamp()+3600, None))
            else:
                conn.execute('INSERT INTO voice_jobs VALUES(?,?,?,?,?,?,?)',
                    (identifier, identifier, '42', '11', 'e'*64, '{}', NOW.timestamp()))
                conn.execute('INSERT INTO voice_job_state VALUES(?,?,?,?,?,?,?)',
                    (identifier, 'processing', 'intent', (new if name=='voice_foreign' else old)+':voice', 4, NOW.timestamp()+3600, None))
                request_id = charged if name=='voice_pending' else uid()
                conn.execute('INSERT INTO voice_requests VALUES(?,?,?,?,?,?)',
                    (request_id, identifier, 'intent', 0, 'e'*64, '{}'))
                if name=='voice_cached':
                    conn.execute('INSERT INTO voice_responses VALUES(?,?,?)', (request_id, 'e'*64, '{"status":200,"synthetic":true}'))
        conn.execute('INSERT INTO bot_replies VALUES(?,?,?,?)', (ids['reply'], ids['bot'], 'e'*64, '{}'))
        conn.execute('INSERT INTO bot_reply_state VALUES(?,?,?,?,?,?,?,?)',
            (ids['reply'], 'sending', old+':bot', 4, NOW.timestamp()+3600, 1, 0, None))
        conn.execute('INSERT INTO voice_notices VALUES(?,?,?,?,?)', (ids['notice'], ids['voice_pending'], 'synthetic', 'e'*64, '{}'))
        conn.execute('INSERT INTO voice_notice_state VALUES(?,?,?,?,?,?,?,?)',
            (ids['notice'], 'sending', old+':notice', 4, NOW.timestamp()+3600, 1, 0, None))
        for name, state in (('notification', 'sending'), ('notification_preparing', 'pending')):
            identifier=ids[name]
            conn.execute('INSERT INTO notification_intents VALUES(?,?,?,?,?,?,?,?,?,?)',
                (identifier, identifier, owner.id, 'daily_digest', NOW.isoformat(), identifier, 0, 'e'*64, '{}', NOW.isoformat()))
            conn.execute('INSERT INTO notification_state VALUES(?,?,?,?,?,?,?,?)',
                (identifier, state, old+':reminder', 4, NOW.timestamp()+3600, 1 if state=='sending' else 0, NOW.timestamp(), None))
    sync = TeamSyncRepository(team, clock=lambda: NOW)
    old_sync=sync.begin('7', worker_id=old+':sync:7')
    sync.begin('8', worker_id=new+':sync:8')
    kinds = (('outbound_task', None), ('job_voice', None), ('outbound_bot', None),
        ('outbound_notification', None), ('sync', None), ('outbound_polza', charged), ('outbound_polza', reserved))
    tickets=tuple(maintenance.enter(old, kind, operation) for kind, operation in kinds)
    foreign_ticket=maintenance.enter(new, 'outbound_polza', foreign_charge)
    return SimpleNamespace(module=module, maintenance=maintenance, participant=participant,
        old=old, new=new, proof=proof, db=db, team=team, billing=billing, ledger=ledger,
        tickets=tickets, ids=ids, operations=operations, charged=charged, reserved=reserved,
        foreign_charge=foreign_charge, foreign_ticket=foreign_ticket, calls=calls, old_sync=old_sync)


def checkpoint(c, **changes):
    args=dict(maintenance=c.maintenance, participant=c.participant, tickets=c.tickets, proof=c.proof,
        team_database=c.db, billing_repository=c.billing, clock=lambda: NOW)
    args.update(changes)
    return c.module.checkpoint_dead_gateway(**args)


def row(c, table, identifier, column='id'):
    with c.db.connection() as conn:
        return dict(conn.execute(f'SELECT * FROM {table} WHERE {column}=?', (identifier,)).fetchone())


def test_import_does_not_open_database(monkeypatch):
    assert importlib.util.find_spec(NAME), 'T12 scoped runtime recovery module missing'
    def denied(*args, **kwargs):
        raise AssertionError('Recovery import is inert')
    monkeypatch.setattr(sqlite3, 'connect', denied)
    importlib.reload(importlib.import_module(NAME))


def test_checkpoint_all_dead_owned_states_before_retiring_exact_tickets(tmp_path, monkeypatch):
    c=case(tmp_path, monkeypatch)
    foreign_task=row(c,'team_execution',c.operations[1],'operation_id')
    cached_response=None
    with c.db.connection() as conn:
        cached_response=tuple(tuple(r) for r in conn.execute('SELECT * FROM voice_responses'))
    with c.maintenance.admission(c.new,'crash_recovery'):
        result=checkpoint(c)
        assert isinstance(result,RecoveryCheckpoint)
        assert set(result.ticket_ids)=={t.id for t in c.tickets}
        assert set(result.billing_operation_ids)=={c.charged,c.reserved}
        assert {key for key,_ in result.source_watermarks}=={'team','billing'}
        assert all(len(value)==64 for _,value in result.source_watermarks)
        assert c.maintenance.active_operations(c.old)==c.tickets  # Helper never leaves tickets.
        assert row(c,'team_execution',c.operations[0],'operation_id')['state']=='uncertain'
        assert row(c,'team_execution',c.operations[1],'operation_id')==foreign_task
        assert row(c,'bot_event_state',c.ids['bot'])['state']=='queued'
        assert row(c,'bot_event_state',c.ids['bot_foreign'])['state']=='processing'
        assert row(c,'bot_reply_state',c.ids['reply'])['state']=='uncertain'
        assert row(c,'voice_job_state',c.ids['voice_pending'])['state']=='uncertain'
        assert row(c,'voice_job_state',c.ids['voice_cached'])['state']=='queued'
        assert row(c,'voice_job_state',c.ids['voice_foreign'])['state']=='processing'
        assert row(c,'voice_notice_state',c.ids['notice'])['state']=='uncertain'
        assert row(c,'notification_state',c.ids['notification'],'notification_id')['state']=='uncertain'
        assert row(c,'notification_state',c.ids['notification_preparing'],'notification_id')['state']=='pending'
        assert row(c,'notification_state',c.ids['notification_preparing'],'notification_id')['worker_id'] is None
        assert row(c,'team_sync_state','7','project_id')['state']=='degraded'
        assert row(c,'team_sync_state','8','project_id')['state']=='syncing'
        assert c.ledger.reservation(c.charged).status=='uncertain'
        assert c.ledger.reservation(c.reserved).status=='reserved'
        assert c.ledger.reservation(c.foreign_charge).status=='submitted'
        c.maintenance.retire_dead_participant(result,proof=c.proof)
    assert c.maintenance.active_operations(c.old)==()
    assert c.foreign_ticket in c.maintenance.active_operations(c.new)
    assert len(c.calls)>=2  # Source preflight and retirement independently verify death.
    with c.db.connection() as conn:
        assert tuple(tuple(r) for r in conn.execute('SELECT * FROM voice_responses'))==cached_response
        assert conn.execute("SELECT COUNT(*) FROM team_journal WHERE operation_id=? AND event='uncertain'",(c.operations[0],)).fetchone()[0]==1
        assert conn.execute('SELECT operation_id FROM team_resources WHERE resource_key=?',('task:55',)).fetchone()[0]==c.operations[0]


@pytest.mark.parametrize('kind',['capture_active','publication_ingest','job_secretary','unknown_future_operation'])
def test_unknown_tickets_refuse_before_any_source_mutation(tmp_path,monkeypatch,kind):
    c=case(tmp_path,monkeypatch)
    tickets=(*c.tickets,c.maintenance.enter(c.old,kind))
    before=row(c,'team_execution',c.operations[0],'operation_id')
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError):
        checkpoint(c,tickets=tickets)
    assert row(c,'team_execution',c.operations[0],'operation_id')==before
    assert c.ledger.reservation(c.charged).status=='submitted'
    assert c.maintenance.active_operations(c.old)==tickets


def test_checkpoint_requires_distinct_ambient_recovery_admission(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    with pytest.raises(MaintenanceError):
        checkpoint(c)
    with c.maintenance.admission(c.old,'crash_recovery'),pytest.raises(MaintenanceError):
        checkpoint(c)
    assert c.ledger.reservation(c.charged).status=='submitted'


def test_fabricated_or_live_process_proof_cannot_mutate_sources(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    bad=replace(c.proof,pid=1)
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError):
        checkpoint(c,proof=bad)
    def live(_):raise ValueError('process_still_alive')
    monkeypatch.setattr(c.module,'verify_dead_identity',live)
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError):
        checkpoint(c)
    assert row(c,'team_execution',c.operations[0],'operation_id')['state']=='running'
    assert c.ledger.reservation(c.charged).status=='submitted'


def test_partial_ticket_inventory_cannot_checkpoint_or_retire(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError):
        checkpoint(c,tickets=c.tickets[:-1])
    assert c.ledger.reservation(c.charged).status=='submitted'


def test_shared_legacy_sync_owner_is_not_assumed_dead(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    with c.db.transaction() as conn:
        conn.execute("UPDATE team_sync_state SET worker_id='team-sync' WHERE project_id='7'")
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError):
        checkpoint(c)
    assert row(c,'team_execution',c.operations[0],'operation_id')['state']=='running'


def test_completed_source_checkpoint_is_idempotent_before_core_retirement(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    with c.maintenance.admission(c.new,'crash_recovery'):
        first=checkpoint(c)
        second=checkpoint(c)
        assert first==second
        c.maintenance.retire_dead_participant(second,proof=c.proof)


def test_source_failure_never_returns_checkpoint_or_finishes_tickets(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    @contextmanager
    def failed():raise sqlite3.OperationalError('synthetic failed commit');yield
    monkeypatch.setattr(c.billing,'transaction',failed)
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(sqlite3.OperationalError):
        checkpoint(c)
    assert c.maintenance.active_operations(c.old)==c.tickets


@pytest.mark.parametrize('state,expected', [('submitted','uncertain'), ('prepared',None), ('absent',None)])
def test_subscription_recovery_scopes_only_exact_operation_and_preserves_uncertainty(tmp_path,monkeypatch,state,expected):
    c=case(tmp_path,monkeypatch)
    from secretary.infrastructure.team_runtime_health import RuntimeHealthRepository
    health=RuntimeHealthRepository(c.maintenance.path,c.maintenance.deployment_id,clock=lambda:NOW)
    health.configure_subscription('https://synthetic.example/hooks/max',('message_created',))
    operation=uid()
    if state!='absent':
        health.begin_subscription_attempt(operation)
        if state=='submitted':health.mark_subscription_submitted(operation)
    # A separate deployment namespace is not part of this predecessor.
    other=RuntimeHealthRepository(c.maintenance.path,uid(),clock=lambda:NOW)
    other.configure_subscription('https://foreign.example/hooks/max',('message_created',))
    other_operation=uid()
    other.begin_subscription_attempt(other_operation)
    other.mark_subscription_submitted(other_operation)
    tickets=(*c.tickets,c.maintenance.enter(c.old,'outbound_subscription',operation))
    with c.maintenance.admission(c.new,'crash_recovery'):
        result=checkpoint(c,tickets=tickets)
        assert 'subscription' in dict(result.source_watermarks)
        assert health.recover_subscription_attempt(operation)['receipt']==expected
        with other._transaction() as conn:
            assert conn.execute('SELECT state FROM runtime_health_attempts WHERE operation_id=?',(other_operation,)).fetchone()[0]=='submitted'
        c.maintenance.retire_dead_participant(result,proof=c.proof)
    assert not c.maintenance.active_operations(c.old)


def test_subscription_without_operation_id_refuses_before_sources(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    tickets=(*c.tickets,c.maintenance.enter(c.old,'outbound_subscription'))
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError):
        checkpoint(c,tickets=tickets)
    assert c.ledger.reservation(c.charged).status=='submitted'


def secretary_case(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    from secretary.infrastructure.database import Database
    from secretary.infrastructure.task_publication_repository import TaskPublicationRepository
    from secretary.domain.task_delivery import (PublicationScope,PublicationTask,PublicationWatermarks,
        PublishPreview,PublishCommand,publication_uuid)
    c.config=SimpleNamespace(**{role+'_database_path':tmp_path/(role+'.sqlite3')
        for role in ('secretary','team','billing','vikunja')})
    c.source=Database(c.config.secretary_database_path,maintenance=c.maintenance,participant_id=c.new)
    c.maintenance.register_participant('secretary',run_id=c.participant['run_id'],identity=c.participant['identity'])
    c.secretary=next(p for p in c.maintenance.status()['participants'] if p['participant_id']=='secretary')
    monkeypatch.setattr(c.module,'process_identity',lambda:{**c.participant['identity'],'pid':99999},raising=False)
    c.jobs=[]
    for stage,cancel in (('prepare',0),('prepare',1),('transcribe',0)):
        meeting=c.source.create_meeting('Synthetic restart')
        job=c.source.enqueue(meeting['id'],stage)
        c.source.execute("UPDATE jobs SET status='running',cancel_requested=? WHERE id=?",(cancel,job['id']))
        c.jobs.append(job['id'])
    c.recording=c.source.create_meeting('Interrupted synthetic recording')['id']
    c.source.execute('UPDATE meetings SET recording=1 WHERE id=?',(c.recording,))
    c.upload=c.source.create_meeting('Interrupted synthetic upload')['id']
    c.source.execute("UPDATE meetings SET media_path='uploading:synthetic' WHERE id=?",(c.upload,))
    c.profile,c.enrollment,c.material,c.record,c.enroll_command=uid(),uid(),uid(),uid(),uid()
    with c.source.transaction() as conn:
        conn.execute('INSERT INTO person_profiles VALUES(?,?,?,?,?,?,?)',(c.profile,'Synthetic','[]',1,0,NOW.isoformat(),NOW.isoformat()))
        conn.execute('''INSERT INTO voice_enrollments(id,person_profile_id,material_version,revision,
            consent_confirmed,status,created_at) VALUES(?,?,0,0,1,'pending',?)''',(c.enrollment,c.profile,NOW.isoformat()))
        conn.execute('INSERT INTO enrollment_materials VALUES(?,?,?,?,?)',(c.material,c.enrollment,c.profile,0,'writing'))
        conn.execute('INSERT INTO enrollment_recordings VALUES(?,?,?,?,?,?,?,?,?)',
            (c.record,c.profile,c.enrollment,0,'recording','review',0,None,NOW.isoformat()))
        conn.execute("INSERT INTO enrollment_commands(operation_id,kind,scope,request_hash,status) VALUES(?,?,?,?,'running')",
            (c.enroll_command,'start',c.enrollment,'e'*64))
    meeting=c.source.create_meeting('Synthetic publication')['id']
    c.source.execute('INSERT INTO summaries VALUES(?,?,?,?)',(meeting,1,1,'{}'))
    scope=PublicationScope(meeting_id=meeting,transcript_version=1,summary_version=1,destination_project_id='7')
    items=tuple(PublicationTask(action_id='action-'+str(i),title='Synthetic '+str(i),assignee_id=c.profile,
        source_segment_ids=('segment-1',),evidence_quote='Synthetic agreement',due_confirmed=True,
        source_fingerprint='assignment-evidence-v1:'+'a'*64,publication_id=publication_uuid(scope,'action-'+str(i))) for i in range(3))
    preview=PublishPreview(preview_id=uid(),actor_id=c.profile,scope=scope,expires_at=NOW+timedelta(minutes=5),
        watermarks=PublicationWatermarks(assignment_revision=1,roster_revision=2,attribution_revision=3,context_hash='b'*64),items=items)
    c.publications=TaskPublicationRepository(c.source,clock=lambda:NOW)
    c.publications.save_preview(preview)
    command=PublishCommand(**preview.model_dump(exclude={'preview_hash'}),preview_hash=preview.preview_hash,operation_id=uid())
    c.publications.accept(c.profile,command,lambda *args:None)
    claims=[]
    for index in range(3):
        worker='secretary-publications-'+(c.secretary['run_id'] if index<2 else uid())
        claim=c.publications.claim_next(worker,lease_seconds=3600)
        if index!=1:claim=c.publications.mark_started(claim,lambda *args:None,expected_payload_hash='f'*64)
        claims.append(claim)
    c.publication_claims=claims
    c.secretary_tickets=tuple(c.maintenance.enter('secretary',kind,operation) for kind,operation in
        (('job',None),('background_job',uid()),('capture_meeting',None),('capture_enrollment',None),('outbound_publication',None),('outbound_polza',c.charged)))
    return c


def recover_secretary(c):
    port=getattr(c.module,'checkpoint_dead_secretary',None)
    assert callable(port),'T12 scoped Secretary recovery port missing'
    return port(c.config,c.maintenance,old_participant=c.secretary,proof=c.proof)


def test_original_secretary_checkpoint_recovers_jobs_enrollment_exact_publications_and_retires(tmp_path,monkeypatch):
    c=secretary_case(tmp_path,monkeypatch)
    foreign=c.source.one('SELECT * FROM task_publication_outbox WHERE delivery_operation_id=?',(c.publication_claims[2].delivery_operation_id,))
    checkpoint=recover_secretary(c)
    assert checkpoint.participant_id=='secretary'
    assert not c.maintenance.active_operations('secretary')
    assert c.maintenance.active_operations(c.old)==c.tickets
    assert [c.source.job(identifier)['status'] for identifier in c.jobs]==['queued','cancelled','uncertain']
    assert c.source.meeting(c.recording)['status']=='interrupted'
    assert c.source.one('SELECT media_path FROM meetings WHERE id=?',(c.upload,))['media_path'] is None
    assert c.source.one('SELECT status FROM voice_enrollments WHERE id=?',(c.enrollment,))['status']=='interrupted'
    assert c.source.one('SELECT state FROM enrollment_materials WHERE generation=?',(c.material,))['state']=='cleanup_pending'
    assert c.source.one('SELECT status FROM enrollment_recordings WHERE id=?',(c.record,))['status']=='interrupted'
    assert c.source.one('SELECT error FROM enrollment_commands WHERE operation_id=?',(c.enroll_command,))['error']=='operation_interrupted'
    for claim,state in zip(c.publication_claims[:2],('uncertain','queued')):
        row=c.source.one('SELECT * FROM task_publication_outbox WHERE delivery_operation_id=?',(claim.delivery_operation_id,))
        assert row['state']==state and row['worker_id'] is None and row['fence']==claim.fence+1
    assert c.source.one('SELECT * FROM task_publication_outbox WHERE delivery_operation_id=?',(c.publication_claims[2].delivery_operation_id,))==foreign
    assert c.ledger.reservation(c.charged).status=='uncertain'
    assert c.ledger.reservation(c.foreign_charge).status=='submitted'
    assert len(c.calls)>=2


@pytest.mark.parametrize('role',['secretary','billing'])
def test_original_secretary_restored_guard_refuses_before_jobs_or_charges_change(tmp_path,monkeypatch,role):
    c=secretary_case(tmp_path,monkeypatch)
    path=getattr(c.config,role+'_database_path')
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS maintenance_restore_guard(id INTEGER PRIMARY KEY,restore_id TEXT,reconciliation_required INTEGER,manifest_sha256 TEXT)')
        conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,1,?)',(uid(),'a'*64))
    with pytest.raises(MaintenanceError):recover_secretary(c)
    assert c.source.job(c.jobs[0])['status']=='running'
    assert c.ledger.reservation(c.charged).status=='submitted'
    assert c.maintenance.active_operations('secretary')==c.secretary_tickets


def test_gateway_crash_after_committed_checkpoint_can_recover_recovery_ticket(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    tickets=(*c.tickets,c.maintenance.enter(c.old,'crash_recovery'))
    with c.maintenance.admission(c.new,'crash_recovery'):
        first=checkpoint(c,tickets=tickets)  # Crash after source commit, before core retirement.
        second=checkpoint(c,tickets=tickets)
        assert first==second
        c.maintenance.retire_dead_participant(second,proof=c.proof)
    assert c.maintenance.active_operations(c.old)==()


def test_secretary_recovery_crash_ticket_is_retired_after_repeated_durable_checkpoint(tmp_path,monkeypatch):
    c=secretary_case(tmp_path,monkeypatch)
    dead_run=uid()
    actor='secretary-recovery:'+dead_run
    c.maintenance.register_participant(actor,run_id=dead_run,identity=c.secretary['identity'])
    ticket=c.maintenance.enter(actor,'crash_recovery')
    recover_secretary(c)
    assert ticket not in c.maintenance.active_operations()
    assert c.maintenance.active_operations(actor)==()


def test_secretary_active_recovery_actor_requires_fresh_dead_proof_before_source_updates(tmp_path,monkeypatch):
    c=secretary_case(tmp_path,monkeypatch)
    dead_run=uid()
    actor='secretary-recovery:'+dead_run
    alive_identity={**c.secretary['identity'],'pid':77777}
    c.maintenance.register_participant(actor,run_id=dead_run,identity=alive_identity)
    ticket=c.maintenance.enter(actor,'crash_recovery')
    original=c.module.verify_dead_identity
    def verify(identity):
        if identity==alive_identity:raise ValueError('process_still_alive')
        return original(identity)
    monkeypatch.setattr(c.module,'verify_dead_identity',verify)
    with pytest.raises(MaintenanceError):recover_secretary(c)
    assert c.source.job(c.jobs[0])['status']=='running'
    assert ticket in c.maintenance.active_operations(actor)


@pytest.mark.parametrize('role',['team','billing'])
def test_gateway_restore_guard_refuses_before_any_source_commit(tmp_path,monkeypatch,role):
    c=case(tmp_path,monkeypatch)
    path=c.db.path if role=='team' else c.billing.path
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS maintenance_restore_guard(id INTEGER PRIMARY KEY,restore_id TEXT,reconciliation_required INTEGER,manifest_sha256 TEXT)')
        conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,1,?)',(uid(),'a'*64))
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError):checkpoint(c)
    assert row(c,'team_execution',c.operations[0],'operation_id')['state']=='running'
    assert c.ledger.reservation(c.charged).status=='submitted'
    assert c.maintenance.active_operations(c.old)==c.tickets


def test_idle_secretary_still_requires_actual_dead_proof_before_replacement(tmp_path,monkeypatch):
    c=secretary_case(tmp_path,monkeypatch)
    for ticket in c.secretary_tickets:c.maintenance.leave(ticket)
    def alive(_):raise ValueError('process_still_alive')
    monkeypatch.setattr(c.module,'verify_dead_identity',alive)
    with pytest.raises(MaintenanceError):recover_secretary(c)
    assert c.source.job(c.jobs[0])['status']=='running'


def source_digest(path):
    with sqlite3.connect(path) as conn:
        return hashlib.sha256('\n'.join(conn.iterdump()).encode('utf-8')).hexdigest()


@pytest.mark.parametrize('ticket_kind',['job_voice','sql'])
def test_uncontained_gateway_native_state_keeps_sources_and_tickets(tmp_path,monkeypatch,ticket_kind):
    c=case(tmp_path,monkeypatch)
    monkeypatch.delattr(c.maintenance,'has_native_containment')
    assert not c.maintenance.has_native_containment(c.old,c.participant['run_id'])
    if ticket_kind=='sql':
        for ticket in c.tickets:c.maintenance.leave(ticket)
        c.tickets=(c.maintenance.enter(c.old,'sql'),)
    before=tuple(source_digest(path) for path in (c.db.path,c.billing.path))
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError,match='native_containment'):
        checkpoint(c)
    assert tuple(source_digest(path) for path in (c.db.path,c.billing.path))==before
    assert c.maintenance.active_operations(c.old)==c.tickets


@pytest.mark.parametrize('tickets',['writer','sql','none'])
def test_uncontained_secretary_persisted_native_work_cannot_be_cleared(tmp_path,monkeypatch,tickets):
    c=secretary_case(tmp_path,monkeypatch)
    monkeypatch.delattr(c.maintenance,'has_native_containment')
    assert not c.maintenance.has_native_containment('secretary',c.secretary['run_id'])
    if tickets!='writer':
        for ticket in c.secretary_tickets:c.maintenance.leave(ticket)
        c.secretary_tickets=(c.maintenance.enter('secretary','sql'),) if tickets=='sql' else ()
    before=tuple(source_digest(path) for path in (c.source.path,c.billing.path))
    with pytest.raises(MaintenanceError,match='native_containment'):recover_secretary(c)
    assert tuple(source_digest(path) for path in (c.source.path,c.billing.path))==before
    assert c.maintenance.active_operations('secretary')==c.secretary_tickets


def test_pure_outbound_gateway_without_owned_native_rows_can_recover_without_receipt(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    monkeypatch.delattr(c.maintenance,'has_native_containment')
    assert not c.maintenance.has_native_containment(c.old,c.participant['run_id'])
    with c.db.transaction() as conn:
        conn.execute("UPDATE voice_job_state SET state='queued',worker_id=NULL,lease_until=NULL WHERE worker_id=?",(c.old+':voice',))
    for ticket in c.tickets:c.maintenance.leave(ticket)
    c.tickets=tuple(c.maintenance.enter(c.old,kind,operation) for kind,operation in
        (('outbound_task',None),('outbound_polza',c.charged)))
    with c.maintenance.admission(c.new,'crash_recovery'):
        result=checkpoint(c)
        c.maintenance.retire_dead_participant(result,proof=c.proof)
    assert not c.maintenance.active_operations(c.old)
    assert row(c,'team_execution',c.operations[0],'operation_id')['state']=='uncertain'
    assert c.ledger.reservation(c.charged).status=='uncertain'


def test_current_containment_receipt_cannot_retroactively_qualify_dead_run(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    monkeypatch.delattr(c.maintenance,'has_native_containment')
    from secretary.infrastructure.team_process_job import WindowsJob,OwnedProcessJobProof
    current_run=uid()
    current='gateway:'+current_run
    identity={**c.participant['identity'],'pid':os.getpid()}
    c.maintenance.register_participant(current,run_id=current_run,identity=identity)
    monkeypatch.setattr(WindowsJob,'verify_owned_identity',lambda self:OwnedProcessJobProof(**identity))
    c.maintenance.register_native_containment(current,object.__new__(WindowsJob))
    assert c.maintenance.has_native_containment(current,current_run)
    assert not c.maintenance.has_native_containment(c.old,c.participant['run_id'])
    before=tuple(source_digest(path) for path in (c.db.path,c.billing.path))
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError,match='native_containment'):
        checkpoint(c)
    assert tuple(source_digest(path) for path in (c.db.path,c.billing.path))==before
    assert c.maintenance.active_operations(c.old)==c.tickets


def raw_preflight(c,tickets=None,**changes):
    port=getattr(c.module,'preflight_dead_gateway',None)
    assert callable(port),'Read-only raw path recovery preflight missing'
    arguments=dict(maintenance=c.maintenance,participant=c.participant,
        tickets=c.tickets if tickets is None else tickets,proof=c.proof,
        team_database_path=c.db.path,billing_database_path=c.billing.path,clock=lambda:NOW)
    arguments.update(changes)
    return port(**arguments)


def test_raw_preflight_rejects_uncontained_old_schema_before_any_constructor_or_migration(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    monkeypatch.delattr(c.maintenance,'has_native_containment')
    with c.db.transaction() as conn:
        conn.execute('DROP TRIGGER response_abort_guard')
        conn.execute('DROP TRIGGER abort_response_guard')
        conn.execute('DROP TABLE voice_request_aborts')
        for table in ('team_due_resolution_consumptions', 'team_due_resolution_previews'):
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
            conn.execute(f'DROP TABLE {table}')
        conn.execute('DELETE FROM team_schema WHERE version>7')
    before=tuple(source_digest(path) for path in (c.db.path,c.billing.path))
    def forbidden(*args,**kwargs):raise AssertionError('preflight must never construct a source repository')
    monkeypatch.setattr(c.module.TeamDatabase,'__init__',forbidden)
    monkeypatch.setattr(c.module.BudgetRepository,'__init__',forbidden)
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError,match='native_containment'):
        raw_preflight(c)
    assert tuple(source_digest(path) for path in (c.db.path,c.billing.path))==before


@pytest.mark.parametrize('contained',[True,False])
def test_zero_ticket_gateway_native_rows_are_readonly_and_never_auto_recovered(tmp_path,monkeypatch,contained):
    c=case(tmp_path,monkeypatch)
    if not contained:monkeypatch.delattr(c.maintenance,'has_native_containment')
    for ticket in c.tickets:c.maintenance.leave(ticket)
    before=tuple(source_digest(path) for path in (c.db.path,c.billing.path))
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError):raw_preflight(c,())
    assert tuple(source_digest(path) for path in (c.db.path,c.billing.path))==before
    assert c.maintenance.active_operations(c.old)==()


def test_idle_zero_ticket_gateway_can_pass_readonly_preflight_without_receipt(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    monkeypatch.delattr(c.maintenance,'has_native_containment')
    with c.db.transaction() as conn:
        for table,suffix in c.module._WORKERS.items():
            conn.execute(f'UPDATE {table} SET worker_id=NULL,lease_until=NULL WHERE worker_id=?',(c.old+':'+suffix,))
        conn.execute('UPDATE team_sync_state SET worker_id=NULL,lease_until=NULL WHERE worker_id=?',(c.old+':sync:7',))
    for ticket in c.tickets:c.maintenance.leave(ticket)
    before=tuple(source_digest(path) for path in (c.db.path,c.billing.path))
    with c.maintenance.admission(c.new,'crash_recovery'):assert raw_preflight(c,()) is None
    assert tuple(source_digest(path) for path in (c.db.path,c.billing.path))==before


def completed_bot_history(c,state):
    from secretary.domain.bot import BotEvent,DeterministicReply,SendReceipt
    from secretary.infrastructure.bot_repository import BotRepository
    from secretary.infrastructure.team_auth_repository import AuthRepository
    with c.db.connection() as conn:owner_id=conn.execute('SELECT id FROM team_members LIMIT 1').fetchone()[0]
    owner=c.team.get_member(owner_id)
    auth=AuthRepository(c.db,secret=b'SYNTHETIC_RECOVERY_AUTH_SECRET_32',clock=lambda:NOW)
    bot=BotRepository(c.db,c.team,auth,clock=lambda:NOW)
    event=BotEvent(event_id=uid(),dedup_key='completed:'+uid(),bot_id='42',kind='message_created',
        timestamp_ms=int(NOW.timestamp()*1000),user_id=owner.max_user_id,chat_id=owner.max_user_id,
        message_id='synthetic-finished',text='Статус')
    bot.intake(event)
    claim=bot.claim_event(c.old+':inbox')
    assert claim.event.event_id==event.event_id
    bot.finish_event(claim,DeterministicReply('Synthetic completed event'))
    delivery=bot.claim_reply(c.old+':bot')
    assert delivery.event.event_id==event.event_id
    receipt=SendReceipt(delivery.id,'retryable' if state=='pending' else state,
        message_id='synthetic-provider-message' if state=='sent' else None,
        error_code='max_rate_limited' if state=='pending' else None,retry_after=5 if state=='pending' else None)
    bot.finish_reply(delivery,receipt)
    return (event.event_id,delivery.id)


@pytest.mark.parametrize('state',['sent','rejected','pending'])
def test_zero_ticket_preflight_preserves_actual_completed_bot_history(tmp_path,monkeypatch,state):
    c=case(tmp_path,monkeypatch)
    with c.db.transaction() as conn:
        for table,suffix in c.module._WORKERS.items():
            conn.execute(f'UPDATE {table} SET worker_id=NULL,lease_until=NULL WHERE worker_id=?',(c.old+':'+suffix,))
        conn.execute('UPDATE team_sync_state SET worker_id=NULL,lease_until=NULL WHERE worker_id=?',(c.old+':sync:7',))
    event_id,reply_id=completed_bot_history(c,state)
    assert row(c,'bot_event_state',event_id)['state']=='done'
    assert row(c,'bot_reply_state',reply_id)['state']==state
    for ticket in c.tickets:c.maintenance.leave(ticket)
    before=source_digest(c.db.path)
    with c.maintenance.admission(c.new,'crash_recovery'):raw_preflight(c,())
    assert source_digest(c.db.path)==before


@pytest.mark.parametrize('state',['sent','rejected','pending'])
def test_active_ticket_checkpoint_never_rewrites_completed_bot_worker_or_receipt(tmp_path,monkeypatch,state):
    c=case(tmp_path,monkeypatch)
    event_id,reply_id=completed_bot_history(c,state)
    before=(row(c,'bot_event_state',event_id),row(c,'bot_reply_state',reply_id))
    with c.maintenance.admission(c.new,'crash_recovery'):checkpoint(c)
    assert (row(c,'bot_event_state',event_id),row(c,'bot_reply_state',reply_id))==before


def test_active_checkpoint_preserves_other_terminal_voice_and_notification_history(tmp_path,monkeypatch):
    c=case(tmp_path,monkeypatch)
    with c.db.transaction() as conn:
        conn.execute("UPDATE voice_job_state SET state='complete',stage='complete' WHERE id=?",(c.ids['voice_cached'],))
        conn.execute("UPDATE voice_notice_state SET state='sent',receipt=? WHERE id=?",('{"synthetic_accepted":true}',c.ids['notice']))
        conn.execute("UPDATE notification_state SET state='sent' WHERE notification_id=?",(c.ids['notification'],))
    queries=(('voice_job_state',c.ids['voice_cached'],'id'),('voice_notice_state',c.ids['notice'],'id'),
        ('notification_state',c.ids['notification'],'notification_id'))
    before=tuple(row(c,*query) for query in queries)
    with c.maintenance.admission(c.new,'crash_recovery'):checkpoint(c)
    assert tuple(row(c,*query) for query in queries)==before


@pytest.mark.parametrize('table,key,state,suffix,column',[
    ('team_execution','task','running','task','operation_id'),
    ('team_execution','task','reconciling','task','operation_id'),
    ('bot_event_state','bot','processing','inbox','id'),
    ('bot_reply_state','reply','sending','bot','id'),
    ('voice_job_state','voice_pending','processing','voice','id'),
    ('voice_notice_state','notice','sending','notice','id'),
    ('notification_state','notification_preparing','pending','reminder','notification_id'),
    ('notification_state','notification','sending','reminder','notification_id'),
    ('team_sync_state','sync','syncing','sync:7','project_id'),
])
def test_each_actual_inflight_row_without_ticket_still_blocks_preflight(tmp_path,monkeypatch,table,key,state,suffix,column):
    c=case(tmp_path,monkeypatch)
    with c.db.transaction() as conn:
        for owned,worker in c.module._WORKERS.items():
            conn.execute(f'UPDATE {owned} SET worker_id=NULL,lease_until=NULL WHERE worker_id=?',(c.old+':'+worker,))
        conn.execute('UPDATE team_sync_state SET worker_id=NULL,lease_until=NULL WHERE worker_id=?',(c.old+':sync:7',))
        identifier=c.operations[0] if key=='task' else '7' if key=='sync' else c.ids[key]
        conn.execute(f'UPDATE {table} SET state=?,worker_id=?,lease_until=? WHERE {column}=?',
            (state,c.old+':'+suffix,NOW.timestamp()+3600,identifier))
    for ticket in c.tickets:c.maintenance.leave(ticket)
    before=tuple(source_digest(path) for path in (c.db.path,c.billing.path))
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError,match='unrepresented_source_state'):
        raw_preflight(c,())
    assert tuple(source_digest(path) for path in (c.db.path,c.billing.path))==before
    assert c.maintenance.active_operations(c.old)==()
