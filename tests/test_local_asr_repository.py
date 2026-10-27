"""Real SQLite/WAL round trips on synthetic inputs; no model or paid requests."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sqlite3
from threading import Barrier

import httpx
import pytest

from secretary.domain.local_asr import (ImportLocalAsrRun,LocalAsrConflict,LocalAsrNotFound,
    ReviewLocalAsrCluster,ReviewLocalAsrSegment)
from secretary.domain.speakers import AddParticipant, ReviewAttribution
from secretary.infrastructure.database import Database, now
from secretary.infrastructure.local_asr_repository import LocalAsrRepository, initialize_local_asr_schema
from secretary.infrastructure.speaker_repository import SpeakerRepository

FIXTURE=Path(__file__).parent/'fixtures/local_asr/synthetic_run.json'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args,**kwargs): raise AssertionError('Network forbidden in model-free tests')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',denied)


@pytest.fixture
def setup(tmp_path):
    db=Database(tmp_path/'synthetic.sqlite3')
    stamp=now()
    db.execute("INSERT INTO meetings(id,title,status,created_at,updated_at,transcript_version,duration_ms) VALUES('meeting','Synthetic','ready',?,?,1,5000)",(stamp,stamp))
    db.execute("INSERT INTO chunks VALUES('chunk','meeting',0,'import','synthetic.wav',0,5000,'transcribed',?)",('a'*64,))
    payload=json.loads(FIXTURE.read_text(encoding='utf-8'))
    for i,item in enumerate(payload['artifacts']['source_segments']):
        db.execute("INSERT INTO segments VALUES(?, 'meeting','chunk',1,?,?,?,'segment',?,NULL,NULL,'import')",(item['segment_id'],i,item['started_ms'],item['ended_ms'],item['text']))
    db.execute("INSERT INTO meeting_transcript_routes VALUES('meeting',1,'ordinary',?,?,?)",('{"provider":"polza"}','preserved-route',stamp))
    db.execute("INSERT INTO summaries VALUES('meeting',1,1,?)",('{"summary":"Paid immutable summary","action_items":[]}',))
    db.execute("INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload) VALUES('paid','meeting','summarize','uncertain',?,?,1,?)",(stamp,stamp,'{"receipt":"persisted"}'))
    db.execute("INSERT INTO summary_checkpoints VALUES('paid','saved',?)",('{"paid":true}',))
    db.execute("INSERT INTO usage(id,meeting_id,job_id,kind,status,created_at) VALUES('usage','meeting','paid','summary','uncertain',?)",(stamp,))
    db.execute("INSERT INTO configuration VALUES('local_cost_limits_enabled','false')")
    speakers=SpeakerRepository(db)
    participant=speakers.add_participant('meeting',AddParticipant(operation_id='roster',expected_roster_revision=0,display_name='Synthetic reviewer')).participants[0].id
    initialize_local_asr_schema(db)
    return db,LocalAsrRepository(db),ImportLocalAsrRun.model_validate(payload),participant


def preserved(db):
    tables=('meetings','chunks','segments','meeting_transcript_routes','summaries','jobs','summary_checkpoints','usage','task_assignments','speaker_attributions','attribution_runs','attribution_state','configuration')
    return {table:db.rows(f'SELECT * FROM {table} ORDER BY rowid') for table in tables}


def imported(setup):
    db,repo,command,participant=setup
    return repo.import_run('meeting',command,trusted_input_sha256='a'*64)


def test_immutable_import_serialization_storage_read_export_and_no_business_mutation(setup):
    db,repo,command,_=setup
    before=preserved(db)
    response=imported(setup)
    assert response['result']==command.result.model_dump(mode='json')
    assert response['artifacts']==command.artifacts.model_dump(mode='json')
    assert repo.get_run('meeting',response['run_id'])==response
    assert repo.export_run('meeting',response['run_id'])==json.loads(json.dumps(response,ensure_ascii=False))
    assert len(repo.list_runs('meeting'))==1
    assert preserved(db)==before
    initialize_local_asr_schema(db)
    assert preserved(db)==before
    for statement in ('UPDATE local_asr_runs SET manifest=manifest','DELETE FROM local_asr_runs'):
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):db.execute(statement)
    with db.connection() as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_duplicate_artifact_and_operation_are_idempotent_but_changed_operation_conflicts(setup):
    db,repo,command,_=setup
    original=imported(setup)
    assert imported(setup)==original
    new_op=command.model_copy(update={'operation_id':'import-other'})
    assert repo.import_run('meeting',new_op,trusted_input_sha256='a'*64)['run_id']==original['run_id']
    assert len(repo.list_runs('meeting'))==1
    changed=command.model_dump(mode='json');changed['artifacts']['warnings']=['new_warning']
    with pytest.raises(LocalAsrConflict,match='operation_id'):
        repo.import_run('meeting',ImportLocalAsrRun.model_validate(changed),trusted_input_sha256='a'*64)
    changed['operation_id']='import-revision-2'
    second=repo.import_run('meeting',ImportLocalAsrRun.model_validate(changed),trusted_input_sha256='a'*64)
    assert second['revision']==2 and second['run_id']!=original['run_id'] and second['reviews']==[]


def test_scope_input_and_source_are_authoritative(setup):
    db,repo,command,_=setup
    with pytest.raises(LocalAsrConflict,match='server-verified'):
        repo.import_run('meeting',command,trusted_input_sha256='9'*64)
    response=imported(setup)
    other=LocalAsrRepository(db,owner_scope='other-owner')
    assert other.list_runs('meeting')==[]
    with pytest.raises(LocalAsrNotFound):other.get_run('meeting',response['run_id'])
    with pytest.raises(LocalAsrNotFound):repo.get_run('another-meeting',response['run_id'])
    db.execute("UPDATE segments SET text='Changed locally' WHERE id='source-0'")
    with pytest.raises(LocalAsrConflict,match='Source transcript'):
        repo.import_run('meeting',command.model_copy(update={'operation_id':'fresh'}),trusted_input_sha256='a'*64)


def test_concurrent_same_import_creates_exactly_one_run(setup):
    db,repo,command,_=setup
    barrier=Barrier(2)
    def apply(index):
        barrier.wait()
        return repo.import_run('meeting',command.model_copy(update={'operation_id':f'concurrent-{index}'}),trusted_input_sha256='a'*64)
    with ThreadPoolExecutor(max_workers=2) as executor: responses=list(executor.map(apply,[1,2]))
    assert responses[0]['run_id']==responses[1]['run_id']
    assert len(repo.list_runs('meeting'))==1


def test_fenced_caller_transaction_rolls_back_artifact_with_failed_application(setup):
    db,repo,command,_=setup
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            repo.import_run('meeting',command,trusted_input_sha256='a'*64,conn=conn)
            raise RuntimeError('fenced transaction interrupted')
    assert repo.list_runs('meeting')==[]
    assert db.rows('SELECT * FROM local_asr_operations')==[]


def test_preview_samples_exact_set_and_no_automatic_identity(setup):
    _,repo,_,_=setup
    run=imported(setup)
    preview=repo.cluster_preview('meeting',run['run_id'],'Спикер 1')
    assert preview['shown_segment_ordinals']==[0,1,2]
    assert preview['eligible_segment_ordinals']==[0,1,2]
    assert [s['segment_ordinal'] for s in preview['samples']]==[0,1,2]
    assert all(s['channel']=='import' for s in preview['samples'])
    for label in ('Спикер 1 + Спикер 2','Спикер неизвестен'):
        excluded=repo.cluster_preview('meeting',run['run_id'],label)
        assert excluded['eligible_segment_ordinals']==[]
        assert excluded['excluded_segments'][0]['reason']=='unknown_or_composite'
    assert run['reviews']==[]


def test_manual_bulk_preserves_exclusions_prior_source_decisions_and_individual_overrides(setup):
    db,repo,_,participant=setup
    SpeakerRepository(db).review_attribution('meeting',ReviewAttribution(operation_id='source-manual',transcript_version=1,expected_revision=0,changes=[{'segment_id':'source-1','participant_id':None}]))
    before=preserved(db)
    run=imported(setup);run_id=run['run_id']
    preview=repo.cluster_preview('meeting',run_id,'Спикер 1')
    assert preview['eligible_segment_ordinals']==[0,2]
    assert preview['excluded_segments']==[{'segment_ordinal':1,'reason':'existing_manual_source_decision'}]
    command=ReviewLocalAsrCluster(operation_id='bulk',expected_review_revision=0,speaker_label='Спикер 1',shown_segment_ordinals=[0,1,2],excluded_segment_ordinals=[1,2],participant_id=participant,listening_confirmed=True)
    reviewed=repo.review_cluster('meeting',run_id,command)
    assert [(r['segment_ordinal'],r['status'],r['source']) for r in reviewed['reviews']]==[(0,'confirmed','manual_cluster'),(2,'excluded','manual_segment')]
    assert repo.review_cluster('meeting',run_id,command)==reviewed
    individual=ReviewLocalAsrSegment(operation_id='individual',expected_review_revision=1,segment_ordinal=0,participant_id=None,listening_confirmed=True)
    final=repo.review_segment('meeting',run_id,individual)
    assert final['review_revision']==2 and all(r['status']=='excluded' for r in final['reviews'])
    assert repo.cluster_preview('meeting',run_id,'Спикер 1')['eligible_segment_ordinals']==[]
    assert preserved(db)==before
    assert len(db.rows('SELECT * FROM local_asr_review_events'))==3


def test_bad_exact_set_stale_version_wrong_meeting_person_and_mixed_assignment_are_rejected(setup):
    _,repo,_,participant=setup
    run=imported(setup);run_id=run['run_id']
    base=dict(operation_id='bad',expected_review_revision=0,speaker_label='Спикер 1',shown_segment_ordinals=[0,1,2],excluded_segment_ordinals=[],participant_id=participant,listening_confirmed=True)
    for changes in ({'shown_segment_ordinals':[0,1]},{'expected_review_revision':1},{'participant_id':'foreign'}):
        with pytest.raises(LocalAsrConflict):repo.review_cluster('meeting',run_id,ReviewLocalAsrCluster(**(base|changes)))
    for ordinal in (3,4):
        with pytest.raises(LocalAsrConflict,match='single person'):
            repo.review_segment('meeting',run_id,ReviewLocalAsrSegment(operation_id=f'mixed-{ordinal}',expected_review_revision=0,segment_ordinal=ordinal,participant_id=participant,listening_confirmed=True))
    assert repo.get_run('meeting',run_id)['review_revision']==0


def test_overlapping_turns_block_single_label_manual_assignment(setup):
    _,repo,command,_=setup
    payload=command.model_dump(mode='json')
    payload['result']['speaker_turns'].append({'ordinal':6,'speaker_label':'Спикер 2','started_ms':200,'ended_ms':300})
    run=repo.import_run('meeting',ImportLocalAsrRun.model_validate(payload),trusted_input_sha256='a'*64)
    preview=repo.cluster_preview('meeting',run['run_id'],'Спикер 1')
    assert preview['eligible_segment_ordinals']==[1,2]
    assert preview['excluded_segments']==[{'segment_ordinal':0,'reason':'overlap_or_mixed_turn'}]


def test_partial_raw_turn_does_not_certify_whole_segment_as_clean(setup):
    _,repo,command,_=setup
    payload=command.model_dump(mode='json')
    payload['result']['speaker_turns'][0]['ended_ms']=1
    run=repo.import_run('meeting',ImportLocalAsrRun.model_validate(payload),trusted_input_sha256='a'*64)
    preview=repo.cluster_preview('meeting',run['run_id'],'Спикер 1')
    assert preview['eligible_segment_ordinals']==[1,2]
    assert preview['excluded_segments']==[{'segment_ordinal':0,'reason':'incomplete_turn_coverage'}]
