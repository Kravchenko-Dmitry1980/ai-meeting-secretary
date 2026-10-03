"""Offline transactional identity overlays; synthetic evidence only."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from pydantic import ValidationError

from secretary.application.speakers import SpeakerService
from secretary.domain.speakers import (
    AddParticipant, CreateProfile, PatchParticipant, PatchProfile, ReviewAttribution,
    SpeakerConflict, SpeakerError, SpeakerNotFound, SpeakerAttribution,
)
from secretary.infrastructure import database
from secretary.infrastructure.database import Database, SCHEMA, now, uid
from secretary.infrastructure.speaker_repository import SpeakerRepository


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network forbidden in speaker tests")
    async def denied_async(*args, **kwargs):
        denied()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied_async)


def legacy_data(conn, audio_path):
    """A version3 fixture with exact raw/provider/financial/checkpoint payloads."""
    stamp = "2026-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO meetings(id,title,status,created_at,updated_at,transcript_version) VALUES('meeting','Synthetic','ready',?,?,1)", (stamp, stamp))
    conn.execute("INSERT INTO chunks VALUES('chunk','meeting',0,'import',?,0,1000,'transcribed','synthetic-hash')", (str(audio_path),))
    conn.execute("INSERT INTO speakers VALUES('raw','meeting','chunk','SPEAKER_01',NULL)")
    conn.execute("INSERT INTO segments VALUES('segment','meeting','chunk',1,0,30,900,'segment','Synthetic immutable speech',0.99,'raw','import')")
    conn.execute("INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload,provider_job_id) VALUES('job','meeting','summarize','uncertain',?,?,1,?,?)", (stamp, stamp, '{"receipt":"keep"}', 'synthetic-receipt'))
    conn.execute("INSERT INTO summaries VALUES('meeting',1,1,?)", ('{"action_items":[{"id":"action","text":"Synthetic task","owner":"Raw owner"}]}',))
    conn.execute("INSERT INTO summary_checkpoints VALUES('job','key',?)", ('{"usage":{"confirmed_rub":0.012},"text":"Original summary"}',))
    conn.execute("INSERT INTO usage(id,meeting_id,job_id,kind,estimated_rub,confirmed_rub,status,provider_request_id,created_at,reserved_rub) VALUES('usage','meeting','job','summary',NULL,0.012,'confirmed','receipt',?,0.02)", (stamp,))
    conn.execute("INSERT INTO configuration VALUES('local_cost_limits_enabled','false')")


RAW_TABLES = ('meetings', 'chunks', 'speakers', 'segments', 'jobs', 'summaries', 'usage', 'configuration', 'summary_checkpoints')
ENTITY_TABLES = ('person_profiles', 'voice_enrollments', 'meeting_participants', 'attribution_runs', 'speaker_attributions', 'task_assignments')


def exact_raw(db):
    return {name: db.rows(f"SELECT * FROM {name} ORDER BY rowid") for name in RAW_TABLES}


def old_database(tmp_path):
    path = tmp_path / 'old.sqlite3'
    audio_path = tmp_path / 'synthetic.audio'
    audio_path.write_bytes(b'synthetic bytes preserved exactly')
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("ALTER TABLE usage ADD COLUMN reserved_rub REAL")
        conn.execute("ALTER TABLE meetings ADD COLUMN capture_error TEXT")
        for version in (1, 2, 3):
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (version, now()))
        legacy_data(conn, audio_path)
        conn.commit()
        before = {name: conn.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall() for name in RAW_TABLES}
    return path, audio_path, before


def test_atomic_legacy_migration_backup_restore_and_repeat(tmp_path):
    path, audio, before = old_database(tmp_path)
    backup = tmp_path / 'backup.sqlite3'
    # SQLite backup covers WAL contents as well as the main database.
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    db = Database(path)
    with db.connection() as conn:
        # Schema7 adds public mode/intent columns; every original column remains byte exact.
        assert {name: [tuple(row)[:len(before[name][0])] for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in RAW_TABLES} == before
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert all(db.rows(f'SELECT * FROM {table}') == [] for table in ENTITY_TABLES)
    initial = exact_raw(db)
    assert exact_raw(Database(path)) == initial
    assert len(db.rows('SELECT * FROM schema_migrations WHERE version=4')) == 1
    assert audio.read_bytes() == b'synthetic bytes preserved exactly'
    restored = tmp_path / 'restored.sqlite3'
    with sqlite3.connect(backup) as source, sqlite3.connect(restored) as target:
        source.backup(target)
    assert exact_raw(Database(restored)) == initial


def test_migration_failure_rolls_back_all_new_ddl_and_marker(tmp_path, monkeypatch):
    path, _, before = old_database(tmp_path)
    statements = database.SPEAKER_MIGRATION
    monkeypatch.setattr(database, 'SPEAKER_MIGRATION', (*statements[:5], 'INVALID SQL FOR FAILURE', *statements[5:]))
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall() == [(1,), (2,), (3,)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name IN ('person_profiles','segments_identity_scope','voice_enrollments','meeting_participants')").fetchall() == []
        assert {name: conn.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall() for name in RAW_TABLES} == before
    monkeypatch.setattr(database, 'SPEAKER_MIGRATION', statements)
    assert Database(path).one('SELECT version FROM schema_migrations WHERE version=4')['version'] == 4


@pytest.fixture
def context(tmp_path):
    db = Database(tmp_path / 'synthetic.sqlite3')
    audio = tmp_path / 'synthetic.audio'
    audio.write_bytes(b'synthetic audio; never passed to an engine')
    with db.transaction() as conn:
        legacy_data(conn, audio)
        conn.execute("INSERT INTO chunks VALUES('chunk2','meeting',1,'microphone','synthetic-unused',1000,1000,'transcribed','synthetic2')")
        conn.execute("INSERT INTO speakers VALUES('raw2','meeting','chunk2','SPEAKER_01',NULL)")
        conn.execute("INSERT INTO segments VALUES('neighbor','meeting','chunk2',1,0,1030,1900,'segment','Another immutable utterance',0.2,'raw2','microphone')")
    return db, SpeakerService(SpeakerRepository(db)), audio


def profile(service, name='Alex'):
    return service.create_profile(CreateProfile(display_name=name, aliases=['A'], operation_id=uid()))


def add(service, meeting='meeting', expected=0, person=None, name='Guest'):
    return service.add_participant(meeting, AddParticipant(person_profile_id=person.id if person else None,
        display_name=None if person else name, expected_roster_revision=expected, operation_id=uid()))


def review(service, participant, segment='segment', revision=0, version=1, operation=None):
    return service.review_attribution('meeting', ReviewAttribution(transcript_version=version,
        expected_revision=revision, operation_id=operation or uid(), changes=[{'segment_id': segment, 'participant_id': participant}]))


def test_read_only_unknown_source_associations_do_not_invent_people(context):
    db, service, _ = context
    before = {table: db.rows(f'SELECT * FROM {table}') for table in (*RAW_TABLES, *ENTITY_TABLES, 'attribution_state', 'participant_roster_state', 'speaker_operations', 'speaker_audit')}
    result = service.get_attribution('meeting')
    assert result.revision == result.roster_revision == 0
    assert result.participants == []
    assert {item.provider_label for item in result.items} == {'SPEAKER_01'}
    assert {item.chunk_id for item in result.items} == {'chunk', 'chunk2'}
    assert all(item.participant_id is None and item.raw_score is None and item.method == 'unknown' for item in result.items)
    assert service.list_profiles() == []
    assert before == {table: db.rows(f'SELECT * FROM {table}') for table in before}


def test_known_person_multiple_meetings_and_independent_same_name_guests(context):
    db, service, _ = context
    person = profile(service)
    meeting2 = db.create_meeting('Synthetic second')['id']
    a = add(service, person=person).participants[0]
    b = add(service, meeting2, person=person).participants[0]
    assert a.id != b.id and a.person_profile_id == b.person_profile_id == person.id
    with pytest.raises(SpeakerConflict):
        add(service, expected=1, person=person)
    first = add(service, expected=1, name='Same').participants[-1]
    second = add(service, expected=2, name='Same').participants[-1]
    other = add(service, meeting2, expected=1, name='Same').participants[-1]
    assert len({first.id, second.id, other.id}) == 3
    assert all(person.person_profile_id is None for person in (first, second, other))


def test_profile_rename_explicit_apply_and_profile_roster_replays(context):
    db, service, _ = context
    initial_raw = exact_raw(db)
    command = CreateProfile(display_name='Old', operation_id=uid())
    person = service.create_profile(command)
    assert service.create_profile(command) == person
    roster_command = AddParticipant(person_profile_id=person.id, expected_roster_revision=0, operation_id=uid())
    original_roster = service.add_participant('meeting', roster_command)
    participant = original_roster.participants[0]
    patch = PatchProfile(display_name='New', aliases=['New alias'], expected_revision=0, operation_id=uid())
    renamed = service.patch_profile(person.id, patch)
    assert renamed.revision == 1
    assert service.patch_profile(person.id, patch) == renamed
    assert service.get_roster('meeting') == original_roster
    with pytest.raises(SpeakerConflict):
        service.patch_profile(person.id, patch.model_copy(update={'operation_id': uid()}))
    apply = PatchParticipant(apply_profile=True, expected_roster_revision=1, operation_id=uid())
    result = service.patch_participant('meeting', participant.id, apply)
    assert result.roster_revision == 2 and result.participants[0].display_name == 'New'
    assert result.participants[0].profile_revision == 1
    assert service.patch_participant('meeting', participant.id, apply) == result
    assert service.add_participant('meeting', roster_command) == original_roster
    assert exact_raw(db) == initial_raw


def test_guest_patch_unset_enabled_and_conflict(context):
    _, service, _ = context
    participant = add(service).participants[0]
    command = PatchParticipant(display_name='Edited', aliases=['Guest alias'], enabled=False,
                               expected_roster_revision=1, operation_id=uid())
    roster = service.patch_participant('meeting', participant.id, command)
    assert roster.roster_revision == 2 and roster.participants[0].enabled is False
    assert roster.participants[0].aliases == ['Guest alias']
    with pytest.raises(SpeakerError):
        review(service, participant.id)
    with pytest.raises(SpeakerError):
        service.patch_participant('meeting', participant.id, PatchParticipant(apply_profile=True, expected_roster_revision=2, operation_id=uid()))


def test_manual_segment_override_unset_append_only_replay_and_raw_preservation(context):
    db, service, audio = context
    raw = exact_raw(db)
    person = add(service).participants[0]
    operation = uid()
    first = review(service, person.id, operation=operation)
    items = {item.segment_id: item for item in first.items}
    assert items['segment'].participant_id == person.id and items['segment'].status == 'confirmed'
    assert items['neighbor'].participant_id is None and items['neighbor'].method == 'unknown'
    second = review(service, None, revision=1)
    third = review(service, person.id, segment='neighbor', revision=2)
    cleared = next(item for item in third.items if item.segment_id == 'segment')
    assert cleared.participant_id is None and cleared.method == 'manual' and cleared.reason_codes == ['manual_unset']
    assert review(service, person.id, operation=operation) == first
    assert service.get_attribution('meeting').revision == 3
    assert len(db.rows('SELECT * FROM attribution_runs')) == 3
    assert len(db.rows('SELECT * FROM speaker_attributions')) == 6
    assert exact_raw(db) == raw and audio.read_bytes() == b'synthetic audio; never passed to an engine'
    for table in ('speaker_operations', 'speaker_audit'):
        assert 'Synthetic immutable speech' not in json.dumps(db.rows(f'SELECT * FROM {table}'))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE speaker_attributions SET participant_id=NULL WHERE run_id=?", (first.items[0].run_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('DELETE FROM speaker_attributions')
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE attribution_runs SET roster_revision=999 WHERE id=?", (first.items[0].run_id,))
    assert second.revision == 2


def test_versions_independent_and_history_cannot_mutate(context):
    db, service, _ = context
    person = add(service).participants[0]
    first = review(service, person.id)
    db.update_meeting('meeting', transcript_version=2)
    db.execute("INSERT INTO segments VALUES('v2','meeting','chunk',2,0,30,900,'segment','New synthetic version',0.5,'raw','import')")
    assert service.get_attribution('meeting').revision == 0
    assert service.get_attribution('meeting', 1) == first
    review(service, None, segment='v2', version=2)
    assert service.get_attribution('meeting', 1) == first
    with pytest.raises(SpeakerConflict):
        review(service, person.id, revision=1, version=1)
    with pytest.raises(SpeakerNotFound):
        review(service, person.id, segment='segment', revision=1, version=2)
    with pytest.raises(SpeakerNotFound):
        service.get_attribution('meeting', 99)


def test_scope_batch_failure_and_operation_reuse_atomic(context):
    db, service, _ = context
    person = profile(service)
    participant = add(service, person=person).participants[0]
    other_meeting = db.create_meeting('Other')['id']
    other = add(service, other_meeting).participants[0]
    for bad_id in (other.id, person.id, 'missing'):
        with pytest.raises(SpeakerNotFound):
            review(service, bad_id)
    tables = ('speaker_attributions', 'attribution_runs', 'attribution_state', 'speaker_audit', 'speaker_operations')
    before = {table: db.rows(f'SELECT * FROM {table}') for table in tables}
    with pytest.raises(SpeakerNotFound):
        service.review_attribution('meeting', ReviewAttribution(transcript_version=1, expected_revision=0, operation_id=uid(),
            changes=[{'segment_id': 'segment', 'participant_id': participant.id}, {'segment_id': 'missing', 'participant_id': None}]))
    assert before == {table: db.rows(f'SELECT * FROM {table}') for table in tables}
    operation = uid()
    review(service, participant.id, operation=operation)
    with pytest.raises(SpeakerConflict):
        review(service, None, operation=operation)
    with pytest.raises(SpeakerConflict):
        service.create_profile(CreateProfile(display_name='Same op', operation_id=operation))
    with pytest.raises(SpeakerConflict):
        service.review_attribution(other_meeting, ReviewAttribution(transcript_version=1, expected_revision=0, operation_id=operation,
            changes=[{'segment_id': 'segment', 'participant_id': participant.id}]))


def test_failed_audit_commit_rolls_back_run_items_state_and_receipt(context):
    db, service, _ = context
    participant = add(service).participants[0]
    tables = ('attribution_runs', 'speaker_attributions', 'attribution_state', 'speaker_operations', 'speaker_audit')
    before = {table: db.rows(f'SELECT * FROM {table}') for table in tables}
    operation = uid()
    db.execute("CREATE TRIGGER synthetic_audit_failure BEFORE INSERT ON speaker_audit BEGIN SELECT RAISE(ABORT,'synthetic last-write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic last-write failure'):
        review(service, participant.id, operation=operation)
    assert before == {table: db.rows(f'SELECT * FROM {table}') for table in tables}
    db.execute('DROP TRIGGER synthetic_audit_failure')
    assert review(service, participant.id, operation=operation).revision == 1


@pytest.mark.parametrize('kind', ['attribution', 'profile', 'roster'])
def test_concurrent_same_revision_one_commit_one_conflict(context, kind):
    db, service, _ = context
    person = profile(service)
    participant = add(service, person=person).participants[0]
    barrier = Barrier(2)
    def command(index):
        barrier.wait()
        try:
            if kind == 'attribution':
                return review(service, participant.id if index else None)
            if kind == 'profile':
                return service.patch_profile(person.id, PatchProfile(display_name=f'Edit {index}', expected_revision=0, operation_id=uid()))
            return service.patch_participant('meeting', participant.id, PatchParticipant(display_name=f'Edit {index}', expected_roster_revision=1, operation_id=uid()))
        except SpeakerConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(command, (0, 1)))
    assert results.count('conflict') == 1
    assert service.get_attribution('meeting').revision == (1 if kind == 'attribution' else 0)
    assert service.get_roster('meeting').roster_revision == (2 if kind == 'roster' else 1)


def test_scoped_foreign_keys_and_finite_score_constraints(context):
    db, service, _ = context
    participant = add(service).participants[0]
    other_meeting = db.create_meeting('Other')['id']
    other = add(service, other_meeting).participants[0]
    run_id = service.create_attribution_run('meeting', 1, 0).id
    for fields in [('another-segment', 'meeting', 1, participant.id), ('segment', other_meeting, 1, other.id), ('segment', 'meeting', 2, participant.id)]:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO speaker_attributions VALUES(?,?,?,?,?,?,?,?,?,?)', (run_id, *fields, 'manual', None, 'confirmed', '[]', 1))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('INSERT INTO speaker_attributions VALUES(?,?,?,?,?,?,?,?,?,?)', (run_id, 'neighbor', 'meeting', 1, other.id, 'manual', None, 'confirmed', '[]', 1))
    for score in (float('inf'), -float('inf')):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO speaker_attributions VALUES(?,?,?,?,?,?,?,?,?,?)', (run_id, 'neighbor', 'meeting', 1, participant.id, 'voice_embedding', score, 'proposed', '[]', 1))
    db.execute('INSERT INTO speaker_attributions VALUES(?,?,?,?,?,?,?,?,?,?)', (run_id, 'neighbor', 'meeting', 1, participant.id, 'voice_embedding', .2, 'proposed', '[]', 1))
    valid = service.get_attribution('meeting').items[0].model_dump()
    valid['raw_score'] = float('nan')
    with pytest.raises(ValidationError):
        SpeakerAttribution(**valid)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO voice_enrollments(id,person_profile_id,material_version,revision,consent_confirmed,status,created_at) VALUES('e',?,1,0,0,'ready',?)", (profile(service).id, now()))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE participant_roster_state SET revision=0.5 WHERE meeting_id='meeting'")


def test_pending_run_captures_revisions_without_publishing(context):
    db, service, _ = context
    person = profile(service)
    add(service, person=person)
    run = service.create_attribution_run('meeting', 1, 0)
    assert run.status == 'pending' and run.kind == 'automatic'
    assert run.expected_revision == 0 and run.roster_revision == 1
    assert run.profile_snapshot[0]['id'] == person.id
    assert run.roster_snapshot[0].person_profile_id == person.id
    assert service.get_attribution('meeting').revision == 0
    assert db.rows('SELECT * FROM speaker_attributions') == []


def test_simultaneous_identical_command_replays_exact_snapshot(context):
    db, service, _ = context
    person = add(service).participants[0]
    operation = uid()
    barrier = Barrier(2)
    def command(_):
        barrier.wait()
        return review(service, person.id, operation=operation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(command, (0, 1))
    assert first == second and first.revision == 1
    assert len(db.rows('SELECT * FROM attribution_runs')) == 1


def test_run_roster_snapshot_retains_names_after_explicit_rename(context):
    db, service, _ = context
    person = add(service, name='Original guest').participants[0]
    snapshot = review(service, person.id)
    service.patch_participant('meeting', person.id, PatchParticipant(display_name='New guest', expected_roster_revision=1, operation_id=uid()))
    run = db.one('SELECT * FROM attribution_runs WHERE id=?', (snapshot.items[0].run_id,))
    assert json.loads(run['roster_snapshot'])[0]['display_name'] == 'Original guest'
    assert run['roster_revision'] == 1
    assert service.get_attribution('meeting').roster_revision == 2


@pytest.mark.parametrize('bad_revision', [True, -1, 0.5, '0'])
def test_strict_revisions(context, bad_revision):
    _, service, _ = context
    with pytest.raises(ValidationError):
        ReviewAttribution(transcript_version=1, expected_revision=bad_revision, operation_id=uid(), changes=[{'segment_id': 'segment', 'participant_id': None}])
    with pytest.raises(ValidationError):
        service.create_attribution_run('meeting', 1, bad_revision)


def test_invalid_empty_duplicate_or_null_edits():
    for changes in ([], [{'segment_id': 's', 'participant_id': None}] * 2):
        with pytest.raises(ValidationError):
            ReviewAttribution(transcript_version=1, expected_revision=0, operation_id=uid(), changes=changes)
    with pytest.raises(ValidationError):
        PatchProfile(expected_revision=0, operation_id=uid(), display_name=None)
    with pytest.raises(ValidationError):
        CreateProfile(display_name='  ', operation_id=uid())
    with pytest.raises(ValidationError):
        PatchParticipant(display_name='x', apply_profile=True, expected_roster_revision=0, operation_id=uid())
