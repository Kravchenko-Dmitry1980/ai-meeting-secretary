"""T12 coherent backup contracts using only synthetic databases/processes."""
import asyncio
import importlib
import importlib.util
import sqlite3
import hashlib
import json
from pathlib import Path
from contextlib import contextmanager
from uuid import uuid4

import pytest


def maintenance():
    assert importlib.util.find_spec('secretary.infrastructure.team_maintenance'), 'maintenance core absent'
    return importlib.import_module('secretary.infrastructure.team_maintenance')


def backup_module():
    assert importlib.util.find_spec('secretary.infrastructure.team_backup'), 'backup core absent'
    return importlib.import_module('secretary.infrastructure.team_backup')


def test_partial_backup_manifest_is_not_restorable(tmp_path):
    b=backup_module()
    source=tmp_path/'partial';source.mkdir()
    (source/'manifest.partial.json').write_text('{"state":"copying"}')
    with pytest.raises(b.BackupError,match='backup_incomplete'):
        b.restore_backup(source,tmp_path/'restored',restore_root=tmp_path)
    assert not (tmp_path/'restored').exists()


def test_restore_rejects_file_tampering_before_creating_destination(tmp_path):
    b=backup_module()
    source=tmp_path/'archive';source.mkdir()
    (source/'manifest.json').write_text('{"state":"complete"}')
    with pytest.raises(b.BackupError):
        b.restore_backup(source,tmp_path/'restored',restore_root=tmp_path)
    assert not (tmp_path/'restored').exists()


def registered(tmp_path):
    m=maintenance()
    gate=m.MaintenanceRepository(tmp_path/'control.sqlite3',str(uuid4()))
    gate.register_participant('synthetic',run_id=str(uuid4()),identity={
        'pid':123,'creation_time':'2026-10-04T00:00:00Z',
        'executable_sha256':'a'*64,'argv_sha256':'b'*64})
    return m,gate


def test_active_capture_defers_backup_without_interrupting_audio(tmp_path):
    m,gate=registered(tmp_path)
    ticket=gate.enter('synthetic','capture')
    with pytest.raises(m.MaintenanceError,match='capture_active'):
        gate.begin_barrier(owner_id='synthetic-backup')
    assert gate.status()['mode']=='open' and gate.status()['active_capture']==1
    gate.leave(ticket)
    claim=gate.begin_barrier(owner_id='synthetic-backup')
    assert gate.status()['mode']=='draining'
    gate.release(claim)


def test_new_outbound_refused_but_prior_admitted_nested_sql_drains(tmp_path):
    m,gate=registered(tmp_path)
    with gate.admission('synthetic','worker') as ticket:
        claim=gate.begin_barrier(owner_id='synthetic-backup')
        with gate.admission('synthetic','write') as nested:
            assert ticket==nested
        with pytest.raises(m.MaintenanceError,match='maintenance_blocked'):
            with gate.admission('synthetic','outbound_polza'):
                pytest.fail('must never dispatch')
        assert len(gate.active_tickets(claim))==1
    assert not gate.active_tickets(claim)
    gate.release(claim)


def test_async_ticket_remains_until_owned_operation_finishes(tmp_path):
    _,gate=registered(tmp_path)
    async def run():
        async with gate.async_admission('synthetic','worker') as ticket:
            claim=gate.begin_barrier(owner_id='synthetic-backup')
            await asyncio.sleep(0)
            assert gate.active_tickets(claim)[0].id==ticket.id
        assert gate.active_tickets(claim)==()
        gate.release(claim)
    asyncio.run(run())


def test_expired_time_and_new_repository_do_not_release_abandoned_ticket(tmp_path):
    m,gate=registered(tmp_path)
    ticket=gate.enter('synthetic','outbound_max')
    claim=gate.begin_barrier(owner_id='synthetic-backup')
    reopened=m.MaintenanceRepository(gate.path,gate.deployment_id)
    assert reopened.active_tickets(claim)==(ticket,)
    with pytest.raises(m.MaintenanceError,match='active_operations'):
        reopened.freeze(claim,{})
    with pytest.raises(m.MaintenanceError,match='maintenance_blocked'):
        reopened.enter('synthetic','write')
    reopened.leave(ticket)
    reopened.release(claim)


def test_wrong_control_path_does_not_change_unrelated_sqlite_header(tmp_path):
    m=maintenance()
    source=tmp_path/'unrelated.sqlite3'
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE synthetic(value TEXT)')
        connection.execute("INSERT INTO synthetic VALUES('preserve')")
    before=source.read_bytes()
    with pytest.raises(m.MaintenanceError,match='database_invalid'):
        m.MaintenanceRepository(source,str(uuid4()))
    assert source.read_bytes()==before


@contextmanager
def full_fixture(tmp_path,*,secretary_path=None):
    from secretary.infrastructure.database import Database
    from secretary.infrastructure.team_database import TeamDatabase
    from secretary.infrastructure.budget_repository import BudgetRepository
    from secretary.infrastructure.team_process_identity import process_identity
    from test_team_lifecycle import SyntheticNativeLifecycle, life
    b=backup_module();m,gate=registered(tmp_path)
    gate.register_participant('synthetic',run_id=str(uuid4()),identity=process_identity())
    data=tmp_path/'data';data.mkdir(exist_ok=True)
    secretary_path=secretary_path or data/'secretary.sqlite3'
    Database(secretary_path)
    native=tmp_path/'vikunja.sqlite3'
    with sqlite3.connect(native) as connection:
        connection.execute('CREATE TABLE files(id INTEGER PRIMARY KEY,size INTEGER)')
        connection.execute('INSERT INTO files VALUES(1,27)')
    team=tmp_path/'team.sqlite3';TeamDatabase(team)
    billing=tmp_path/'billing.sqlite3';BudgetRepository(billing)
    files=tmp_path/'native-files';files.mkdir(exist_ok=True)
    (files/'1').write_bytes(b'synthetic native attachment')
    sources=dict(secretary=secretary_path,team=team,billing=billing,vikunja=native)
    with SyntheticNativeLifecycle(tmp_path,gate,native) as fixture:
        service=b.BackupService(gate,sources,backup_root=tmp_path/'backups',asset_roots={
            'secretary_data':secretary_path.parent,'vikunja_files':files},lifecycle=life,
            manifest_path=fixture.manifest_path,required_participants=['synthetic'],config_versions={'test':'1','vikunja':'2.7.0'},native_storage='local-v2.7.0')
        yield b,gate,service,fixture,sources


def test_backup_barrier_prevents_publication_and_charge_between_snapshots(tmp_path,monkeypatch):
    with full_fixture(tmp_path) as (b,gate,service,fixture,sources):
        seen=[];original=b._schema
        def during_snapshot(conn):
            # All four real database write locks already held before first snapshot.
            for source in sources.values():
                with sqlite3.connect(source,timeout=.01) as writer:
                    with pytest.raises(sqlite3.OperationalError,match='locked'):
                        writer.execute('BEGIN IMMEDIATE')
            for kind in ('publication','outbound_polza'):
                with pytest.raises(maintenance().MaintenanceBlocked):gate.enter('synthetic',kind)
            assert gate.status()['mode']=='frozen'
            assert not fixture.controller.children['vikunja'][1].alive()
            seen.append(True)
            return original(conn)
        monkeypatch.setattr(b,'_schema',during_snapshot)
        result=service.backup(tmp_path/'backups'/'coherent')
        assert result.state=='complete',result
        assert result.full_recovery and len(seen)==4
        assert gate.status()['mode']=='open'
        assert fixture.controller.children['vikunja'][1].alive()
        manifest=json.loads((result.destination/'manifest.json').read_text())
        assert set(manifest['databases'])=={'secretary','team','billing','vikunja'}
        assert manifest['barrier_id']==manifest['native_quiescence']['barrier_id']


def test_partial_snapshot_failure_never_becomes_restorable_and_resumes_owned_native(tmp_path,monkeypatch):
    with full_fixture(tmp_path) as (b,gate,service,fixture,_):
        calls=[];original=b._schema
        def fail_second(conn):
            calls.append(True)
            if len(calls)==2:raise b.BackupError('backup_test_interruption')
            return original(conn)
        monkeypatch.setattr(b,'_schema',fail_second)
        result=service.backup(tmp_path/'backups'/'partial')
        assert result.state=='failed' and result.code=='backup_test_interruption'
        assert not (result.destination/'manifest.json').exists()
        with pytest.raises(b.BackupError,match='backup_incomplete'):
            b.restore_backup(result.destination,tmp_path/'restored',restore_root=tmp_path)
        assert fixture.controller.children['vikunja'][1].alive() and gate.status()['mode']=='open'


def test_restored_outbox_cannot_charge_before_reconciliation(tmp_path):
    from secretary.infrastructure.budget_repository import BudgetRepository,assert_paid_billing_allowed
    from secretary.infrastructure.team_database import TeamDatabase
    from secretary.domain.cloud_budget import BudgetError
    with full_fixture(tmp_path) as (b,_,service,_,sources):
        result=service.backup(tmp_path/'backups'/'full')
        assert result.full_recovery,result
        before={role:hashlib.sha256(path.read_bytes()).hexdigest() for role,path in sources.items() if role!='vikunja'}
        restored=b.restore_backup(result.destination,tmp_path/'restore',restore_root=tmp_path)
        assert restored.state=='restored_for_reconciliation' and not restored.outbound_enabled
        with pytest.raises(BudgetError,match='maintenance_restore_blocked'):
            assert_paid_billing_allowed(restored.databases['billing'])
        reopened=BudgetRepository(restored.databases['billing'])
        with pytest.raises(BudgetError,match='maintenance_restore_blocked'):reopened.assert_paid_allowed()
        restored_team=TeamDatabase(restored.databases['team'])
        with restored_team.connection() as conn:
            with pytest.raises(sqlite3.DatabaseError):conn.execute("DELETE FROM team_execution")
        for role in ('secretary','team','billing'):
            with sqlite3.connect(restored.databases[role]) as connection:
                assert connection.execute('SELECT reconciliation_required FROM maintenance_restore_guard').fetchone()==(1,)
                with pytest.raises(sqlite3.IntegrityError):connection.execute('DELETE FROM maintenance_restore_guard')
                with pytest.raises(sqlite3.IntegrityError):connection.execute("INSERT OR REPLACE INTO maintenance_restore_guard VALUES(1,'new',1,'fake')")
        assert before=={role:hashlib.sha256(path.read_bytes()).hexdigest() for role,path in sources.items() if role!='vikunja'}


def test_restore_preserves_identification_snapshot_and_quarantines_old_job(tmp_path):
    from test_speaker_jobs import ctx,meeting,transcribe
    tmp_path=tmp_path.parent/('identity-'+uuid4().hex[:6]);tmp_path.mkdir()
    case=ctx.__wrapped__(tmp_path)
    completed_meeting=meeting(case);completed_job=transcribe(case,completed_meeting)
    assert asyncio.run(case.worker.run_once()) # local identification, synthetic
    assert asyncio.run(case.worker.run_once()) # completed fake-cloud summary
    completed=dict(case.db.one('SELECT * FROM identification_intents WHERE job_id=?',(completed_job['id'],)))
    assert completed['outcome']=='completed'
    meeting_id=meeting(case);job=transcribe(case,meeting_id)
    original=dict(case.db.one('SELECT * FROM identification_intents WHERE job_id=?',(job['id'],)))
    chunk=dict(case.db.chunks(meeting_id)[0])
    with full_fixture(tmp_path,secretary_path=case.db.path) as (b,_,service,_,_):
        result=service.backup(tmp_path/'backups'/'identity')
        assert result.full_recovery,result
        restored=b.restore_backup(result.destination,tmp_path/'restored',restore_root=tmp_path)
        with sqlite3.connect(restored.databases['secretary']) as conn:
            conn.row_factory=sqlite3.Row
            actual=dict(conn.execute('SELECT * FROM identification_intents WHERE id=?',(original['id'],)).fetchone())
            assert dict(conn.execute('SELECT * FROM identification_intents WHERE id=?',(completed['id'],)).fetchone())==completed
            assert actual['snapshot']==original['snapshot'] and actual['intent_key']==original['intent_key']
            assert actual['pipeline']==original['pipeline'] and actual['outcome']=='stale'
            oldjob=conn.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
            assert oldjob['cancel_requested']==1 and oldjob['status']=='cancelled'
            newpath=Path(conn.execute('SELECT path FROM chunks WHERE id=?',(chunk['id'],)).fetchone()[0])
            assert newpath.is_relative_to(restored.destination)
            assert hashlib.sha256(newpath.read_bytes()).hexdigest()==chunk['sha256']
            assert conn.execute('SELECT summary_authorized FROM meeting_pipeline_handoffs WHERE meeting_id=?',(meeting_id,)).fetchone()[0]==0
            assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
        # The legacy path must now refuse all restored work, including ordinary
        # cloud jobs and handoffs. Diagnostic reads remain available.
        from secretary.infrastructure.database import Database
        from secretary.application.worker import Worker
        from secretary.infrastructure.voice_resources import InferenceCoordinator
        restored_db=Database(restored.databases['secretary'])
        settings=case.settings.model_copy(update={'data_dir':restored.asset_roots['secretary_data']})
        with pytest.raises(sqlite3.DatabaseError):
            Worker(restored_db,settings,lambda *_:pytest.fail('restored job called provider'),enrollments=case.materials,coordinator=InferenceCoordinator())
        restored_worker=Worker(restored_db,settings,lambda *_:pytest.fail('restored job called provider'),recovery=False,enrollments=case.materials,coordinator=InferenceCoordinator())
        # The cancelled identification has no eligible work; empty diagnostic
        # claims may return False without attempting a protected write.
        assert asyncio.run(restored_worker.run_once()) is False
        assert restored_db.one('SELECT count(*) AS n FROM identification_intents')['n']==2
        assert restored_db.job(job['id'])['status']=='cancelled'


def test_active_capture_service_does_not_pause_native_or_create_archive(tmp_path):
    with full_fixture(tmp_path) as (_,gate,service,fixture,_):
        ticket=gate.enter('synthetic','capture')
        oldpid=fixture.controller.children['vikunja'][2]['pid']
        result=service.backup(tmp_path/'backups'/'capture')
        assert result.state=='deferred' and result.code=='maintenance_capture_active'
        assert not result.destination.exists() and gate.status()['mode']=='open'
        assert gate.active_operations()==(ticket,)
        assert fixture.controller.children['vikunja'][2]['pid']==oldpid
        gate.leave(ticket)


def test_missing_native_blob_is_incomplete_and_cannot_restore(tmp_path):
    with full_fixture(tmp_path) as (b,_,service,_,_):
        (service.roots['vikunja_files']/'1').unlink()
        result=service.backup(tmp_path/'backups'/'missing')
        assert result.state=='complete' and not result.full_recovery
        assert 'asset_native_reference_missing' in json.loads((result.destination/'manifest.json').read_text())['issues']
        with pytest.raises(b.BackupError,match='backup_incomplete'):
            b.restore_backup(result.destination,tmp_path/'restore',restore_root=tmp_path)


def test_changed_after_validation_asset_is_rejected(tmp_path,monkeypatch):
    with full_fixture(tmp_path) as (b,_,service,_,_):
        result=service.backup(tmp_path/'backups'/'full');assert result.full_recovery
        original=b._validate_archive
        def changed(source):
            manifest=original(source)
            (source/'assets'/'vikunja_files'/'1').write_bytes(b'changed after validation')
            return manifest
        monkeypatch.setattr(b,'_validate_archive',changed)
        with pytest.raises(b.BackupError,match='backup_hash_mismatch'):
            b.restore_backup(result.destination,tmp_path/'restore',restore_root=tmp_path)
        assert not (tmp_path/'restore'/'restore.json').exists()


def test_partial_restore_prior_authorities_already_blocked(tmp_path,monkeypatch):
    with full_fixture(tmp_path) as (b,_,service,_,_):
        result=service.backup(tmp_path/'backups'/'full');assert result.full_recovery
        original=b._copy
        def interrupted(source,target,deadline):
            if source==result.destination/'databases'/'vikunja.sqlite3':raise b.BackupError('backup_test_interruption')
            return original(source,target,deadline)
        monkeypatch.setattr(b,'_copy',interrupted)
        with pytest.raises(b.BackupError,match='backup_test_interruption'):
            b.restore_backup(result.destination,tmp_path/'restore',restore_root=tmp_path)
        for role in ('team','billing'):
            with sqlite3.connect(tmp_path/'restore'/'databases'/(role+'.sqlite3')) as connection:
                assert connection.execute('SELECT reconciliation_required FROM maintenance_restore_guard').fetchone()==(1,)
        assert not (tmp_path/'restore'/'restore.json').exists()


def test_outside_root_chunk_is_never_opened(tmp_path,monkeypatch):
    from secretary.infrastructure.database import Database
    with full_fixture(tmp_path) as (b,_,service,_,sources):
        outside=tmp_path/'not-an-asset.bin';outside.write_bytes(b'not authorized media')
        db=Database(sources['secretary']);meeting_id=db.create_meeting('Synthetic')['id']
        db.add_chunk(meeting_id,{'path':str(outside),'sha256':'a'*64,'sequence':0,'channel':'import','offset_ms':0,'duration_ms':1})
        original=b._hash
        def guarded(path):
            assert Path(path)!=outside,'read outside configured asset roots'
            return original(path)
        monkeypatch.setattr(b,'_hash',guarded)
        result=service.backup(tmp_path/'backups'/'outside')
        assert result.state=='complete' and not result.full_recovery


def test_backup_scripts_preview_requires_explicit_apply_without_reading_paths(tmp_path,capsys):
    source=Path(__file__).parents[1]/'scripts/team/backup_restore.py'
    spec=importlib.util.spec_from_file_location('backup_cli_test',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.main(['backup','--config','does-not-exist','--destination','not-absolute'])==0
    assert json.loads(capsys.readouterr().out)['state']=='preview'
    assert module.main(['restore','--backup','missing','--destination','missing','--restore-root','missing'])==0
    for name in ('backup.ps1','restore.ps1'):
        text=(source.parent/name).read_text()
        assert '[switch]$Apply' in text and 'Remove-Item' not in text


def test_dead_recovery_requires_exact_checkpoint_and_new_admission(tmp_path):
    import subprocess,sys
    from secretary.infrastructure.team_process_identity import process_identity,verify_dead_identity
    m,gate=registered(tmp_path)
    gate.bind_sources({role:tmp_path/(role+'.sqlite') for role in ('secretary','team','billing','vikunja')})
    process=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)'],creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        identity=process_identity(process.pid);run_id=str(uuid4())
        gate.register_participant('old',run_id=run_id,identity=identity)
        ticket=gate.enter('old','outbound_polza',str(uuid4()))
        process.terminate();process.wait(timeout=5)
        proof=verify_dead_identity(identity)
        checkpoint=m.RecoveryCheckpoint('old',run_id,(ticket.id,),(ticket.operation_id,),(('billing','committed:1'),),'c'*64)
        with pytest.raises(m.MaintenanceError,match='recovery_admission_required'):
            gate.retire_dead_participant(checkpoint,proof=proof)
        with gate.admission('synthetic','crash_recovery'):
            assert gate.active_operations('old')==(ticket,)
            gate.retire_dead_participant(checkpoint,proof=proof)
            gate.retire_dead_participant(checkpoint,proof=proof)
            assert gate.active_operations('old')==()
        with sqlite3.connect(gate.path) as connection:
            assert connection.execute('SELECT count(*) FROM maintenance_recovery_checkpoints').fetchone()==(1,)
            with pytest.raises(sqlite3.IntegrityError):connection.execute('DELETE FROM maintenance_recovery_checkpoints')
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)


def test_live_process_ticket_cannot_be_retired_with_stale_proof(tmp_path):
    from secretary.infrastructure.team_process_identity import process_identity,DeadProcessProof
    m,gate=registered(tmp_path);identity=process_identity();run_id=str(uuid4())
    gate.bind_sources({role:tmp_path/(role+'.sqlite') for role in ('secretary','team','billing','vikunja')})
    gate.register_participant('living',run_id=run_id,identity=identity)
    ticket=gate.enter('living','job')
    fake=DeadProcessProof(**identity,state='exited',observed_at='2026-10-04T00:00:00Z')
    checkpoint=m.RecoveryCheckpoint('living',run_id,(ticket.id,),(),(('team','1'),),'c'*64)
    with gate.admission('synthetic','crash_recovery'):
        with pytest.raises(m.MaintenanceError,match='process_unverified'):gate.retire_dead_participant(checkpoint,proof=fake)
    assert gate.active_operations('living')==(ticket,)
    gate.leave(ticket)


def test_owned_executor_ticket_context_drains_and_rejects_finished_ticket(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    m,gate=registered(tmp_path);ticket=gate.enter('synthetic','enrollment')
    claim=gate.begin_barrier(owner_id='backup')
    def finish_existing():
        with gate.ticket_context(ticket):
            with gate.admission('synthetic','sql') as reused:assert reused==ticket
            with pytest.raises(m.MaintenanceBlocked):gate.enter('synthetic','outbound_polza')
    with ThreadPoolExecutor(max_workers=1) as executor:executor.submit(finish_existing).result(timeout=5)
    assert gate.active_tickets(claim)==(ticket,)
    gate.leave(ticket)
    with pytest.raises(m.MaintenanceError,match='ticket_finished'):
        with gate.ticket_context(ticket):pytest.fail('stale thread context')
    gate.release(claim)


def test_deployment_sources_immutable_before_any_new_source_is_opened(tmp_path):
    m,gate=registered(tmp_path)
    sources={role:tmp_path/(role+'.sqlite') for role in ('secretary','team','billing','vikunja')}
    gate.bind_sources(sources);gate.bind_sources(sources)
    changed=dict(sources,billing=tmp_path/'different-billing.sqlite')
    with pytest.raises(m.MaintenanceError,match='sources_mismatch'):gate.bind_sources(changed)
    assert not any(path.exists() for path in sources.values())
    assert not changed['billing'].exists()
    with sqlite3.connect(gate.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):conn.execute('DELETE FROM maintenance_sources')


def test_unbound_historical_ticket_cannot_gain_new_source_binding(tmp_path):
    m,gate=registered(tmp_path)
    ticket=gate.enter('synthetic','job');gate.leave(ticket)
    with pytest.raises(m.MaintenanceError,match='sources_unverified'):
        gate.bind_sources({role:tmp_path/(role+'.sqlite') for role in ('secretary','team','billing','vikunja')})


def test_retention_preview_verifies_complete_manifests_and_never_deletes(tmp_path):
    import shutil
    from datetime import datetime,timedelta,timezone
    with full_fixture(tmp_path) as (b,gate,service,_,_):
        result=service.backup(tmp_path/'backups'/'base');assert result.full_recovery
        base=json.loads((result.destination/'manifest.json').read_text())
        for index in range(1,35):
            destination=tmp_path/'backups'/('day-'+str(index))
            shutil.copytree(result.destination,destination)
            doc=dict(base,created_at=(datetime(2026,9,1,tzinfo=timezone.utc)+timedelta(days=index)).isoformat())
            (destination/'manifest.json').write_text(json.dumps(doc))
        incomplete=tmp_path/'backups'/'partial';incomplete.mkdir()
        (incomplete/'manifest.partial.json').write_text('{}')
        before=set((tmp_path/'backups').iterdir())
        report=b.retention_preview(tmp_path/'backups',gate.deployment_id)
        assert report['state']=='preview' and report['deletion_authorized'] is False
        assert 7<=len(report['keep'])<=11 and report['review_candidates']
        assert report['ignored_directories']==1
        assert all(len(item['manifest_sha256'])==64 for item in (*report['keep'],*report['review_candidates']))
        assert set((tmp_path/'backups').iterdir())==before


def test_stale_required_participant_identity_defers_without_stopping_native(tmp_path):
    with full_fixture(tmp_path) as (_,gate,service,fixture,_):
        identity=dict(gate.status()['participants'][0]['identity'],creation_time='1')
        gate.register_participant('synthetic',run_id=str(uuid4()),identity=identity)
        result=service.backup(tmp_path/'backups'/'stale')
        assert result.state=='deferred' and result.code=='backup_participant_unverified'
        assert not result.destination.exists() and fixture.controller.children['vikunja'][1].alive()


def test_explicit_participant_descriptors_follow_restart_and_reject_stale_run(tmp_path):
    from secretary.infrastructure.team_process_identity import process_identity
    with full_fixture(tmp_path) as (b,gate,old,fixture,sources):
        gate.register_participant('gateway-current',run_id=str(uuid4()),identity=process_identity())
        paths={'secretary':tmp_path/'secretary-participant.json','gateway':tmp_path/'gateway-participant.json'}
        paths['secretary'].write_text(json.dumps(gate.participant_descriptor('synthetic',role='secretary')))
        paths['gateway'].write_text(json.dumps(gate.participant_descriptor('gateway-current',role='gateway')))
        service=b.BackupService(gate,sources,backup_root=old.backup_root,asset_roots=old.roots,
            lifecycle=old.lifecycle,manifest_path=old.manifest_path,participant_descriptors=paths,
            require_native_containment=False,
            config_versions=old.versions,native_storage=old.native_storage)
        gate.register_participant('gateway-current',run_id=str(uuid4()),identity=process_identity())
        rejected=service.backup(tmp_path/'backups'/'stale-descriptor')
        assert rejected.state=='deferred' and rejected.code=='backup_descriptor_unverified'
        assert fixture.controller.children['vikunja'][1].alive() and not rejected.destination.exists()
        paths['gateway'].write_text(json.dumps(gate.participant_descriptor('gateway-current',role='gateway')))
        result=service.backup(tmp_path/'backups'/'current-descriptor')
        assert result.full_recovery,result
        assert set(json.loads((result.destination/'manifest.json').read_text())['required_participants'])=={'synthetic','gateway-current'}


def test_descriptor_role_and_sources_hash_are_exact_not_discovered(tmp_path):
    from secretary.infrastructure.team_process_identity import process_identity
    with full_fixture(tmp_path) as (b,gate,old,_,sources):
        gate.register_participant('gateway-current',run_id=str(uuid4()),identity=process_identity())
        paths={'secretary':tmp_path/'secretary-participant.json','gateway':tmp_path/'gateway-participant.json'}
        paths['secretary'].write_text(json.dumps(gate.participant_descriptor('synthetic',role='secretary')))
        descriptor=gate.participant_descriptor('gateway-current',role='gateway')
        paths['gateway'].write_text(json.dumps(dict(descriptor,sources_sha256='0'*64)))
        service=b.BackupService(gate,sources,backup_root=old.backup_root,asset_roots=old.roots,
            lifecycle=old.lifecycle,manifest_path=old.manifest_path,participant_descriptors=paths,
            config_versions=old.versions,native_storage=old.native_storage)
        assert service.backup(tmp_path/'backups'/'bad-hash').code=='backup_descriptor_unverified'
        paths['gateway'].write_text(json.dumps(dict(descriptor,role='secretary')))
        assert service.backup(tmp_path/'backups'/'bad-role').code=='backup_descriptor_unverified'
        paths['gateway'].unlink()
        assert service.backup(tmp_path/'backups'/'missing').code=='backup_descriptor_unverified'


def test_asset_walk_error_never_qualifies_partial_tree_as_full(tmp_path,monkeypatch):
    with full_fixture(tmp_path) as (b,_,service,_,_):
        def denied(root,*,followlinks,onerror):
            onerror(PermissionError('synthetic unreadable subtree'))
            yield
        monkeypatch.setattr(b.os,'walk',denied)
        result=service.backup(tmp_path/'backups'/'denied')
        assert result.state=='failed' and result.code=='backup_asset_unreadable'
        assert not (result.destination/'manifest.json').exists()


def test_hardlinked_asset_is_rejected_without_deleting_either_name(tmp_path):
    import os
    b=backup_module();original=tmp_path/'one';linked=tmp_path/'two'
    original.write_bytes(b'synthetic');os.link(original,linked)
    with pytest.raises(b.BackupError,match='backup_file_invalid'):b._safe(original,file=True)
    assert original.read_bytes()==linked.read_bytes()==b'synthetic'


def test_dangling_reparse_metadata_is_checked_without_exists_gate(tmp_path,monkeypatch):
    from types import SimpleNamespace
    b=backup_module();missing=tmp_path/'dangling';original=Path.lstat
    def metadata(path):
        if path==missing:return SimpleNamespace(st_mode=0,st_file_attributes=0x400)
        return original(path)
    monkeypatch.setattr(Path,'lstat',metadata)
    with pytest.raises(b.BackupError,match='backup_reparse_forbidden'):b._safe(missing)


def test_failed_initial_source_role_validation_does_not_poison_binding(tmp_path):
    from secretary.infrastructure.database import Database
    from secretary.infrastructure.team_database import TeamDatabase
    from secretary.infrastructure.budget_repository import BudgetRepository
    b=backup_module();_,gate=registered(tmp_path)
    data=tmp_path/'data';data.mkdir()
    sources={'secretary':data/'secretary.sqlite','team':tmp_path/'team.sqlite',
             'billing':tmp_path/'billing.sqlite','vikunja':tmp_path/'vikunja.sqlite'}
    Database(sources['secretary']);TeamDatabase(sources['team']);BudgetRepository(sources['billing'])
    with sqlite3.connect(sources['vikunja']) as conn:conn.execute('CREATE TABLE files(id INTEGER,size INTEGER)')
    before={role:path.read_bytes() for role,path in sources.items()}
    manifest=tmp_path/'manifest.json';manifest.write_text('{}')
    bad=dict(sources,team=sources['billing'],billing=sources['team'])
    args={'backup_root':tmp_path/'backups','asset_roots':{},'lifecycle':None,'manifest_path':manifest,'required_participants':['synthetic']}
    with pytest.raises(b.BackupError,match='source_role_invalid'):b.BackupService(gate,bad,**args)
    assert gate.bound_sources()=={}
    assert before=={role:path.read_bytes() for role,path in sources.items()}
    b.BackupService(gate,sources,**args)
    assert len(gate.bound_sources())==4


def test_changed_bound_source_rejects_before_validator_opens_new_file(tmp_path):
    m,gate=registered(tmp_path)
    sources={role:tmp_path/(role+'.sqlite') for role in ('secretary','team','billing','vikunja')}
    gate.bind_sources(sources)
    changed=dict(sources,billing=tmp_path/'wrong.sqlite')
    with pytest.raises(m.MaintenanceError,match='sources_mismatch'):
        gate.bind_sources(changed,validate_new=lambda _:pytest.fail('validator opened changed source'))
    assert not changed['billing'].exists()


def test_first_binding_validator_failure_rolls_back_all_sources(tmp_path):
    _,gate=registered(tmp_path)
    sources={role:tmp_path/(role+'.sqlite') for role in ('secretary','team','billing','vikunja')}
    def invalid(_):raise ValueError('synthetic validation failed')
    with pytest.raises(ValueError,match='validation failed'):gate.bind_sources(sources,validate_new=invalid)
    assert gate.bound_sources()=={} and gate.active_operations()==()


def test_shared_role_validator_allows_absent_new_stores_without_creation(tmp_path):
    b=backup_module();sources={role:tmp_path/(role+'.sqlite') for role in b.ROLES}
    b.validate_source_roles(sources,allow_missing=True)
    assert not any(path.exists() for path in sources.values())


@contextmanager
def contained_participants(gate,tmp_path):
    """Only this owned child installs a native Job; never the pytest/owner PID."""
    import subprocess,sys,queue,threading
    code='''import sys,json,time
sys.path.insert(0,sys.argv[1])
from uuid import uuid4
from secretary.infrastructure.team_maintenance import MaintenanceRepository,MaintenanceError
from secretary.infrastructure.team_process_identity import process_identity
from secretary.infrastructure.team_process_job import install_process_job
gate=MaintenanceRepository(sys.argv[2],sys.argv[3])
job=install_process_job()
identity=process_identity()
descriptors={}
for role in ('secretary','gateway'):
    name='contained-'+role
    gate.register_participant(name,run_id=str(uuid4()),identity=identity)
    gate.register_native_containment(name,job)
    gate.register_native_containment(name,job)
    descriptors[role]=gate.participant_descriptor(name,role=role)
gate.register_participant('late',run_id=str(uuid4()),identity=identity)
ticket=gate.enter('late','sql');gate.leave(ticket)
try:
    gate.register_native_containment('late',job)
except MaintenanceError as error:
    assert error.code=='maintenance_containment_late'
else:
    raise AssertionError('late proof accepted')
print(json.dumps(descriptors),flush=True)
time.sleep(60)
'''
    process=subprocess.Popen([sys.executable,'-B','-u','-c',code,str(Path(__file__).parents[1]/'backend'),str(gate.path),gate.deployment_id],
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
    messages=queue.Queue();reader=threading.Thread(target=lambda:messages.put(process.stdout.readline()),daemon=True);reader.start()
    try:
        line=messages.get(timeout=10)
        assert line.strip(),'owned containment child did not report verified proof'
        descriptors=json.loads(line);paths={}
        for role,value in descriptors.items():
            path=tmp_path/(role+'-verified.json');path.write_text(json.dumps(value));paths[role]=path
        yield descriptors,paths
    finally:
        if process.poll() is None:process.terminate()
        process.wait(timeout=5);reader.join(timeout=2)


def test_kernel_verified_containment_receipt_qualifies_descriptor_backup(tmp_path):
    with full_fixture(tmp_path) as (b,gate,old,_,sources):
        with contained_participants(gate,tmp_path) as (descriptors,paths):
            for role,descriptor in descriptors.items():
                assert descriptor['containment_version']==1
                assert gate.has_native_containment(descriptor['participant_id'],descriptor['run_id'])
            service=b.BackupService(gate,sources,backup_root=old.backup_root,asset_roots=old.roots,
                lifecycle=old.lifecycle,manifest_path=old.manifest_path,participant_descriptors=paths,
                config_versions=old.versions,native_storage=old.native_storage)
            result=service.backup(tmp_path/'backups'/'contained')
            assert result.state=='complete' and result.full_recovery and result.native_containment_verified,result
            manifest=json.loads((result.destination/'manifest.json').read_text())
            assert manifest['native_containment_verified'] is True
            with sqlite3.connect(gate.path) as conn:
                assert conn.execute('SELECT count(*) FROM maintenance_native_containment').fetchone()==(2,)
                for sql in ('DELETE FROM maintenance_native_containment',"UPDATE maintenance_native_containment SET containment_version=1",
                            'INSERT OR REPLACE INTO maintenance_native_containment SELECT * FROM maintenance_native_containment'):
                    with pytest.raises(sqlite3.IntegrityError):conn.execute(sql)


def test_descriptor_backup_defers_without_native_containment_receipt(tmp_path):
    from secretary.infrastructure.team_process_identity import process_identity
    with full_fixture(tmp_path) as (b,gate,old,fixture,sources):
        gate.register_participant('uncontained-gateway',run_id=str(uuid4()),identity=process_identity())
        paths={}
        for role,name in (('secretary','synthetic'),('gateway','uncontained-gateway')):
            descriptor=gate.participant_descriptor(name,role=role)
            assert descriptor['containment_version']==0
            path=tmp_path/(role+'-uncontained.json');path.write_text(json.dumps(descriptor));paths[role]=path
        service=b.BackupService(gate,sources,backup_root=old.backup_root,asset_roots=old.roots,
            lifecycle=old.lifecycle,manifest_path=old.manifest_path,participant_descriptors=paths,
            config_versions=old.versions,native_storage=old.native_storage)
        result=service.backup(tmp_path/'backups'/'denied')
        assert result.state=='deferred' and result.code=='backup_native_containment_unverified'
        assert not result.destination.exists() and fixture.controller.children['vikunja'][1].alive()
        # A forged descriptor flag cannot promote an absent immutable receipt.
        value=json.loads(paths['gateway'].read_text());value['containment_version']=1
        paths['gateway'].write_text(json.dumps(value))
        assert service.backup(tmp_path/'backups'/'forged').code=='backup_descriptor_unverified'


def test_containment_is_bound_to_exact_run_and_rejects_fake_objects(tmp_path):
    from secretary.infrastructure.team_process_job import WindowsJob
    m,gate=registered(tmp_path)
    gate.bind_sources({role:tmp_path/(role+'.sqlite') for role in ('secretary','team','billing','vikunja')})
    with pytest.raises(m.MaintenanceError,match='containment_unverified'):gate.register_native_containment('synthetic',object())
    with pytest.raises(m.MaintenanceError,match='containment_unverified'):gate.register_native_containment('synthetic',object.__new__(WindowsJob))
    with contained_participants(gate,tmp_path) as (descriptors,_):
        old=descriptors['gateway']
        assert gate.has_native_containment(old['participant_id'],old['run_id'])
        gate.register_participant(old['participant_id'],run_id=str(uuid4()),identity=old['identity'])
        assert not gate.has_native_containment(old['participant_id'],old['run_id'])
        assert gate.participant_descriptor(old['participant_id'],role='gateway')['containment_version']==0


def test_explicit_fixture_backup_does_not_claim_native_containment(tmp_path):
    with full_fixture(tmp_path) as (_,_,service,_,_):
        result=service.backup(tmp_path/'backups'/'fixture-only')
        assert result.full_recovery and not result.native_containment_verified
        assert json.loads((result.destination/'manifest.json').read_text())['native_containment_verified'] is False


def test_changed_participant_after_containment_check_defers_before_quiesce(tmp_path,monkeypatch):
    with full_fixture(tmp_path) as (b,gate,old,fixture,sources):
        with contained_participants(gate,tmp_path) as (descriptors,paths):
            service=b.BackupService(gate,sources,backup_root=old.backup_root,asset_roots=old.roots,
                lifecycle=old.lifecycle,manifest_path=old.manifest_path,participant_descriptors=paths,
                config_versions=old.versions,native_storage=old.native_storage)
            begin=gate.begin_barrier
            def concurrent_restart(*,owner_id):
                actor=descriptors['gateway']
                gate.register_participant(actor['participant_id'],run_id=str(uuid4()),identity=actor['identity'])
                return begin(owner_id=owner_id)
            monkeypatch.setattr(gate,'begin_barrier',concurrent_restart)
            old_native_pid=fixture.controller.children['vikunja'][2]['pid']
            result=service.backup(tmp_path/'backups'/'race')
            assert result.state=='deferred' and result.code=='backup_participant_changed'
            assert not result.destination.exists() and gate.status()['mode']=='open'
            assert fixture.controller.children['vikunja'][2]['pid']==old_native_pid
