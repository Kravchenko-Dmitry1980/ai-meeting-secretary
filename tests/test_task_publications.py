"""Publication application contracts over isolated source and Team SQLite."""
from __future__ import annotations

import importlib
import importlib.util
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.application.evidence import validate_summary
from secretary.domain.models import TranscriptSegment
from secretary.domain.speakers import AddParticipant, CreateProfile, PatchParticipant, SpeakerConflict
from secretary.domain.summary_context import ReviewTaskAssignments
from secretary.domain.task_delivery import PreviewPublications, PublicationSelection, PublicationScope, PublishCommand
from secretary.domain.team import (
    AcceptanceReceipt, CommandReceipt, ExecutionState, TaskSnapshot, TeamConflict, TeamForbidden, TeamMember,
)
from secretary.infrastructure.assignment_repository import AssignmentRepository
from secretary.infrastructure.database import Database
from secretary.infrastructure.speaker_repository import SpeakerRepository
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository


def uid():
    return str(uuid4())


def api():
    names = ('secretary.application.task_publications',
             'secretary.infrastructure.task_publication_repository')
    modules = []
    for name in names:
        assert importlib.util.find_spec(name) is not None, f'publication module missing: {name}'
        modules.append(importlib.import_module(name))
    return modules


@pytest.fixture
def source_case(tmp_path, request):
    options = getattr(request, 'param', {})
    db = Database(tmp_path / 'source.sqlite3')
    meeting = db.create_meeting('Synthetic publication')
    version = db.ensure_version(meeting['id'])
    chunk = db.add_chunk(meeting['id'], {'sequence': 0, 'channel': 'import', 'path': 'synthetic.wav',
        'offset_ms': 0, 'duration_ms': 1000, 'sha256': 'a' * 64})
    job = db.enqueue(meeting['id'], 'transcribe', chunk_id=chunk['id'], version=version)
    source = TranscriptSegment(id=options.get('source_id', 'source'), meeting_id=meeting['id'], transcript_version=version,
        chunk_id=chunk['id'], ordinal=0, text=options.get('text', 'Я подготовлю отчёт к пятнице.'),
        channel='import', timing_precision='unknown')
    db.save_segments(job, [source.model_dump()], [])
    speakers = SpeakerRepository(db)
    profile = speakers.create_profile(CreateProfile(display_name='Павел', operation_id=uid()))
    roster = speakers.add_participant(meeting['id'], AddParticipant(person_profile_id=profile.id,
        expected_roster_revision=0, operation_id=uid()))
    person = roster.participants[0]
    raw = {'overview': 'Synthetic', 'readable_transcript': '', 'decisions': [], 'open_questions': [],
        'action_items': [{'text': options.get('title', 'Подготовить отчёт'), 'owner': None, 'due_date': 'к пятнице',
            'source_segment_ids': [source.id], 'evidence_quote': source.text,
            'assignment_proposal': {'basis': 'unknown', 'semantic_flag': 'explicit_commitment'}}]}
    if options.get('actions', 1) > 1:
        original = raw['action_items'][0]
        raw['action_items'] = [{**original, 'text': f'{index}: ' + original['text']}
                               for index in range(options['actions'])]
    job = db.enqueue(meeting['id'], 'summarize', version=version)
    raw = db.save_summary(job, validate_summary(raw, meeting['id'], version, [source.model_dump()]))
    assignments = AssignmentRepository(db)
    initial = assignments.get(meeting['id'])
    def review(decision='set_manual'):
        state = assignments.get(meeting['id'])
        return assignments.review(meeting['id'], ReviewTaskAssignments(
            transcript_version=state.transcript_version, summary_version=state.summary_version,
            attribution_revision=state.attribution_revision, roster_revision=state.roster_revision,
            expected_revision=state.revision, operation_id=uid(), changes=tuple({'action_id': item.action.action_id,
                'decision': decision, 'participant_id': person.id if decision == 'set_manual' else None}
                for item in state.items))).current
    state = review()
    time = [datetime(2026, 10, 3, 10, tzinfo=timezone.utc)]
    team = TeamRepository(TeamDatabase(tmp_path / 'team.sqlite3'), clock=lambda: time[0])
    owner = TeamMember(id=uid(), display_name='Owner', role='owner', max_user_id='11',
                       vikunja_user_id='21', project_ids=('7',))
    member = TeamMember(id=uid(), display_name='Павел', person_profile_id=profile.id,
                        max_user_id='12', vikunja_user_id='22', project_ids=('7',))
    other = TeamMember(id=uid(), display_name='Павел', max_user_id='13', vikunja_user_id='23', project_ids=('7',))
    for row in (owner, member, other):
        team.upsert_member(row, expected_revision=None)
    scope = PublicationScope(meeting_id=meeting['id'], transcript_version=version,
                             summary_version=raw['summary_version'], destination_project_id='7')
    return SimpleNamespace(db=db, meeting=meeting['id'], assignments=assignments, speakers=speakers,
        person=person, profile=profile, state=state, initial=initial, review=review, source=source, raw=raw,
        team=team, owner=owner, member=member, other=other, time=time, scope=scope,
        action_id=state.items[0].action.action_id)


@pytest.fixture
def case(source_case):
    c = source_case
    application, storage = api()
    c.repository = storage.TaskPublicationRepository(c.db, clock=lambda: c.time[0])
    c.service = application.TaskPublicationService(c.db, c.repository, c.team,
        actor_id=c.owner.id, project_id='7', clock=lambda: c.time[0])
    return c


def request(c, **edits):
    return PreviewPublications(scope=c.scope,
        selections=(PublicationSelection(action_id=c.action_id, due_resolution=edits.pop('due_resolution', 'none'), **edits),))


def preview(c, **edits):
    result = c.service.preview(c.meeting, request(c, **edits))
    assert result.preview is not None, result.candidates
    return result.preview


def command(value, **edits):
    return PublishCommand.model_validate({**value.model_dump(mode='json'), 'operation_id': uid(), **edits})


def next_summary(c):
    job = c.db.enqueue(c.meeting, 'summarize', version=1)
    raw = c.db.save_summary(job, copy.deepcopy(c.raw))
    c.scope = c.scope.model_copy(update={'summary_version': raw['summary_version']})
    c.state = c.review()
    return c.scope


def apply_synthetic_gateway(c, value, *, task_id='91'):
    """Explicit synthetic gateway proof; no remote provider or adapter call."""
    batch = c.service.confirm(c.meeting, command(value))
    claim = c.repository.claim_next('synthetic-worker')
    c.repository.mark_started(claim, c.service.validate_dispatch, expected_payload_hash='a' * 64)
    task = value.items[0].target_snapshot
    if task is None:
        task = TaskSnapshot(task_id=task_id, project_id='7', revision=0, remote_fingerprint='b' * 64,
                            title=value.items[0].title, assignee_id=c.member.id)
        c.team.save_projection(task, expected_revision=None)
    gateway = CommandReceipt(acceptance_receipt=AcceptanceReceipt(operation_id=claim.delivery_operation_id,
        payload_hash='a' * 64, decision='accepted', decided_at=c.time[0]),
        execution_state=ExecutionState(state='applied', revision=1, task_id=task.task_id, verified_at=c.time[0]), current=task)
    c.repository.update_gateway(claim.delivery_operation_id, gateway, expected_payload_hash='a' * 64)
    return c.service.read(c.meeting, batch.operation_id)


def test_publication_source_uses_supplied_transaction_and_fresh_context(source_case):
    c = source_case
    assert hasattr(c.assignments, 'publication_source'), 'source port missing'
    with c.db.transaction() as conn:
        state, context = c.assignments.publication_source(conn, c.scope)
        assert state == c.state
        assert context.scope.summary_version == c.scope.summary_version
        assert context.participants[0].person_profile_id == c.profile.id


def test_publication_source_rejects_historical_transcript(source_case):
    c = source_case
    assert hasattr(c.assignments, 'publication_source'), 'source port missing'
    c.db.update_meeting(c.meeting, transcript_version=2)
    with c.db.transaction() as conn, pytest.raises(SpeakerConflict):
        c.assignments.publication_source(conn, c.scope)


def test_publication_source_requires_an_explicit_transaction(source_case):
    c = source_case
    assert hasattr(c.assignments, 'publication_source'), 'source port missing'
    with c.db.connection() as conn, pytest.raises(SpeakerConflict):
        c.assignments.publication_source(conn, c.scope)


def test_publication_source_rejects_previous_summary_of_current_transcript(source_case):
    c = source_case
    old = c.scope
    next_summary(c)
    with c.db.transaction() as conn, pytest.raises(SpeakerConflict):
        c.assignments.publication_source(conn, old)


def test_member_directory_keeps_profile_duplicates_for_ambiguity_detection(source_case):
    c = source_case
    assert hasattr(c.team, 'list_members'), 'team directory port missing'
    duplicate = c.other.model_copy(update={'person_profile_id': c.profile.id, 'revision': 1})
    c.team.upsert_member(duplicate, expected_revision=0)
    values = c.team.list_members(c.owner.id, '7')
    assert {m.id for m in values if m.person_profile_id == c.profile.id} == {c.member.id, c.other.id}


@pytest.mark.parametrize('change', [{'enabled': False}, {'role': 'member'}, {'project_ids': ('8',)}])
def test_member_directory_rechecks_current_owner_permissions(source_case, change):
    c = source_case
    assert hasattr(c.team, 'list_members'), 'team directory port missing'
    c.team.upsert_member(c.owner.model_copy(update={**change, 'revision': 1}), expected_revision=0)
    with pytest.raises(TeamForbidden):
        c.team.list_members(c.owner.id, '7')


def test_context_exposes_server_literal_evidence_and_explicit_due_blocker(case):
    c = case
    value = c.service.context(c.meeting)
    assert value.scope == c.scope
    assert value.watermarks.context_hash == c.state.current_context_hash
    row = value.candidates[0]
    assert row.assignee_id == c.member.id and row.title == 'Подготовить отчёт'
    assert row.evidence_quote == c.source.text and row.source_segment_ids == ('source',)
    assert row.due_phrase == 'к пятнице' and 'due_resolution_required' in row.reason_codes
    assert row.eligible
    assert c.repository.publications(c.meeting, '7') == ()


def test_preview_freezes_owner_mapping_source_and_fifteen_minute_expiry(case):
    c = case
    value = preview(c)
    assert value.actor_id == c.owner.id and value.expires_at == c.time[0] + timedelta(minutes=15)
    item = value.items[0]
    assert item.assignee_id == c.member.id and item.member_revision == 0
    assert item.participant_id == c.person.id and item.source_fingerprint == c.state.items[0].fingerprint
    assert item.due_confirmed and item.due_at is None and item.due_phrase == 'к пятнице'
    assert c.repository.get_preview(value.preview_id, c.owner.id) == value
    assert c.repository.publications(c.meeting, '7') == ()


def test_unresolved_due_never_creates_a_publishable_preview(case):
    c = case
    value = c.service.preview(c.meeting, PreviewPublications(scope=c.scope,
        selections=(PublicationSelection(action_id=c.action_id),)))
    assert value.preview is None and value.candidates[0].reason_codes == ('due_resolution_required',)


def test_explicit_date_is_normalized_to_utc_and_confirmed(case):
    c = case
    due = datetime(2026, 10, 9, 18, tzinfo=timezone(timedelta(hours=3)))
    value = c.service.preview(c.meeting, PreviewPublications(scope=c.scope,
        selections=(PublicationSelection(action_id=c.action_id, due_resolution='date', due_at=due),)))
    assert value.preview.items[0].due_at == datetime(2026, 10, 9, 15, tzinfo=timezone.utc)


def test_known_profile_mapping_cannot_be_overridden(case):
    c = case
    value = c.service.preview(c.meeting, request(c, assignee_id=c.other.id))
    assert value.preview is None and 'team_member_mapping_conflict' in value.candidates[0].reason_codes


def test_duplicate_profile_mapping_blocks_instead_of_selecting_first(case):
    c = case
    c.team.upsert_member(c.other.model_copy(update={'person_profile_id': c.profile.id, 'revision': 1}), expected_revision=0)
    value = c.service.preview(c.meeting, request(c))
    assert value.preview is None and 'team_member_ambiguous' in value.candidates[0].reason_codes


def test_equal_display_names_cannot_replace_a_missing_profile_mapping(case):
    c = case
    c.team.upsert_member(c.member.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    value = c.service.preview(c.meeting, request(c))
    assert value.preview is None and 'team_member_unmapped' in value.candidates[0].reason_codes


def test_guest_evidence_is_eligible_but_needs_explicit_active_team_member(case):
    c = case
    roster = c.speakers.add_participant(c.meeting, AddParticipant(display_name='Guest',
        expected_roster_revision=1, operation_id=uid()))
    guest = next(person for person in roster.participants if person.person_profile_id is None)
    state = c.assignments.get(c.meeting)
    c.assignments.review(c.meeting, ReviewTaskAssignments(
        transcript_version=1, summary_version=1, attribution_revision=state.attribution_revision,
        roster_revision=state.roster_revision, expected_revision=state.revision, operation_id=uid(),
        changes=({'action_id': c.action_id, 'decision': 'set_manual', 'participant_id': guest.id},)))
    missing = c.service.preview(c.meeting, request(c))
    assert missing.preview is None and missing.candidates[0].eligible
    assert 'team_member_selection_required' in missing.candidates[0].reason_codes
    selected = preview(c, assignee_id=c.other.id)
    assert selected.items[0].assignee_id == c.other.id and selected.items[0].participant_id == guest.id
    unknown = c.service.preview(c.meeting, request(c, assignee_id=uid()))
    assert unknown.preview is None and 'team_member_unavailable' in unknown.candidates[0].reason_codes


def test_confirmed_manual_assignment_still_requires_source_proof(case):
    c = case
    c.db.execute('UPDATE segments SET text=? WHERE id=?', ('Другой источник.', 'source'))
    value = c.service.preview(c.meeting, request(c))
    assert value.preview is None and not value.candidates[0].eligible


def test_confirmation_accepts_exact_preview_once_and_replays_after_expiry(case):
    c = case
    cmd = command(preview(c))
    first = c.service.confirm(c.meeting, cmd)
    assert first.acceptance_receipt.decision == 'accepted'
    assert first.items[0].execution_state.state == 'queued'
    c.time[0] += timedelta(hours=1)
    replay = c.service.confirm(c.meeting, cmd)
    assert replay.acceptance_receipt == first.acceptance_receipt
    assert len(c.repository.publications(c.meeting, '7')) == 1


def test_new_confirmation_rejects_expired_preview(case):
    c = case
    cmd = command(preview(c))
    c.time[0] += timedelta(minutes=15)
    with pytest.raises(TeamConflict):
        c.service.confirm(c.meeting, cmd)
    assert c.repository.publications(c.meeting, '7') == ()


@pytest.mark.parametrize('field,value', [('title', 'Forged title'), ('evidence_quote', 'Forged evidence'), ('assignee_id', None)])
def test_confirmation_cannot_replace_frozen_preview_fields(case, field, value):
    c = case
    original = preview(c)
    value = c.other.id if field == 'assignee_id' else value
    changed = original.items[0].model_copy(update={field: value})
    cmd = command(original, items=(changed,))
    with pytest.raises(TeamConflict):
        c.service.confirm(c.meeting, cmd)
    assert c.repository.publications(c.meeting, '7') == ()


def test_assignment_cas_change_rejects_new_confirmation(case):
    c = case
    cmd = command(preview(c))
    c.review()
    with pytest.raises((TeamConflict, SpeakerConflict)):
        c.service.confirm(c.meeting, cmd)
    assert c.repository.publications(c.meeting, '7') == ()


def test_mapping_revision_change_rejects_new_confirmation(case):
    c = case
    cmd = command(preview(c))
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'display_name': 'Новое имя'}), expected_revision=0)
    with pytest.raises(TeamConflict):
        c.service.confirm(c.meeting, cmd)
    assert c.repository.publications(c.meeting, '7') == ()


def test_source_stale_read_and_list_preserve_receipt_and_do_not_create_again(case):
    c = case
    cmd = command(preview(c))
    accepted = c.service.confirm(c.meeting, cmd)
    c.review()
    read = c.service.read(c.meeting, cmd.operation_id)
    listed = c.service.list(c.meeting)
    assert read.acceptance_receipt == accepted.acceptance_receipt
    assert read.items[0].source_stale and not read.items[0].correction_required
    assert listed == (read,)
    again = c.service.preview(c.meeting, request(c))
    assert again.preview is None and again.candidates[0].existing_publication_id == accepted.items[0].publication_id
    assert len(c.repository.publications(c.meeting, '7')) == 1


@pytest.mark.parametrize('method', ['context', 'preview', 'confirm', 'read', 'list'])
def test_all_public_methods_recheck_current_owner(case, method):
    c = case
    cmd = command(preview(c))
    accepted = c.service.confirm(c.meeting, cmd)
    c.team.upsert_member(c.owner.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    args = {'context': (c.meeting,), 'preview': (c.meeting, request(c)), 'confirm': (c.meeting, cmd),
            'read': (c.meeting, accepted.operation_id), 'list': (c.meeting,)}[method]
    with pytest.raises(TeamForbidden):
        getattr(c.service, method)(*args)


def test_route_scope_cannot_publish_another_meetings_preview(case):
    c = case
    with pytest.raises(TeamForbidden):
        c.service.preview(uid(), request(c))


def test_dispatch_rechecks_exact_source_and_member_revision_in_source_transaction(case):
    c = case
    value = preview(c)
    with c.db.transaction() as conn:
        c.service.validate_dispatch(conn, value.scope, value.watermarks, value.items[0], c.owner.id)
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'project_ids': ()}), expected_revision=0)
    with c.db.transaction() as conn, pytest.raises(TeamConflict):
        c.service.validate_dispatch(conn, value.scope, value.watermarks, value.items[0], c.owner.id)


def test_dispatch_cannot_swap_the_server_configured_owner(case):
    c = case
    value = preview(c)
    with c.db.transaction() as conn, pytest.raises(TeamForbidden):
        c.service.validate_dispatch(conn, value.scope, value.watermarks, value.items[0], c.other.id)


def test_confirm_preserves_source_records_and_paid_jobs(case):
    c = case
    tables = ('segments', 'summaries', 'assignment_snapshots', 'task_assignment_state', 'jobs', 'usage')
    before = {name: c.db.rows(f'SELECT * FROM {name}') for name in tables}
    c.service.confirm(c.meeting, command(preview(c)))
    assert {name: c.db.rows(f'SELECT * FROM {name}') for name in tables} == before


def test_repeated_summary_requires_an_explicit_supersedes_decision(case):
    c = case
    prior = apply_synthetic_gateway(c, preview(c))
    next_summary(c)
    result = c.service.preview(c.meeting, request(c))
    assert result.preview is None and result.candidates[0].eligible
    assert 'supersedes_decision_required' in result.candidates[0].reason_codes
    suggested = result.candidates[0].possible_supersedes
    assert len(suggested) == 1 and suggested[0].verified and suggested[0].task_id == '91'
    assert suggested[0].publication_id == prior.items[0].publication_id


@pytest.mark.parametrize('intent', ['link', 'propose_update'])
def test_link_and_update_freeze_verified_target_current_projection(case, intent):
    c = case
    prior = apply_synthetic_gateway(c, preview(c))
    next_summary(c)
    value = preview(c, intent=intent, target_publication_id=prior.items[0].publication_id)
    item = value.items[0]
    assert item.target_task_id == '91' and item.expected_task_revision == 0
    assert item.expected_task_fingerprint == 'b' * 64
    assert item.target_snapshot == c.team.get_projection(c.owner.id, '91')
    changed = item.target_snapshot.model_copy(update={'revision': 1, 'remote_fingerprint': 'c' * 64, 'bucket': 'doing'})
    c.team.save_projection(changed, expected_revision=0)
    with pytest.raises(TeamConflict, match='publication_target_changed'):
        c.service.confirm(c.meeting, command(value))


def test_unverified_earlier_publication_cannot_be_a_link_target(case):
    c = case
    prior = c.service.confirm(c.meeting, command(preview(c)))
    next_summary(c)
    result = c.service.preview(c.meeting, request(c, intent='link', target_publication_id=prior.items[0].publication_id))
    assert result.preview is None and 'publication_target_unverified' in result.candidates[0].reason_codes


def test_create_separate_uses_explicit_nonce_and_distinct_stable_identity(case):
    c = case
    prior = apply_synthetic_gateway(c, preview(c))
    next_summary(c)
    nonce = uid()
    first = preview(c, intent='create_separate', separate_id=nonce)
    second = preview(c, intent='create_separate', separate_id=nonce)
    assert first.items[0].publication_id == second.items[0].publication_id
    assert first.items[0].publication_id != prior.items[0].publication_id


def test_applied_source_change_requires_correction_and_keeps_remote_assignee(case):
    c = case
    applied = apply_synthetic_gateway(c, preview(c))
    original = c.team.get_projection(c.owner.id, '91')
    c.review()
    read = c.service.read(c.meeting, applied.operation_id)
    assert read.items[0].source_stale and read.items[0].correction_required
    assert c.team.get_projection(c.owner.id, '91') == original
    candidate = c.service.context(c.meeting).candidates[0]
    assert candidate.existing_publication_id == applied.items[0].publication_id
    assert 'already_published' in candidate.reason_codes


def test_remote_task_progress_is_not_a_source_correction_after_applied_link(case):
    c = case
    prior = apply_synthetic_gateway(c, preview(c))
    next_summary(c)
    linked = apply_synthetic_gateway(c, preview(c, intent='link', target_publication_id=prior.items[0].publication_id))
    task = c.team.get_projection(c.owner.id, '91')
    c.team.save_projection(task.model_copy(update={'revision': 1, 'remote_fingerprint': 'c' * 64, 'bucket': 'doing'}),
                           expected_revision=0)
    read = c.service.read(c.meeting, linked.operation_id)
    assert not read.items[0].source_stale and not read.items[0].correction_required


@pytest.mark.parametrize('source_case', [
    {'title': 'Д' * 4001}, {'text': 'Я подготовлю ' + 'д' * 4001},
    {'text': 'Я подготовлю ' + '😀' * (4000 - len('Я подготовлю ')), 'source_id': 's' * 128},
], indirect=True)
def test_oversized_valid_evidence_is_a_blocker_not_a_truncated_preview(case):
    c = case
    result = c.service.preview(c.meeting, request(c))
    row = result.candidates[0]
    assert row.eligible and result.preview is None
    assert 'publication_payload_too_large' in row.reason_codes
    assert row.title == c.raw['action_items'][0]['text'] and row.evidence_quote == c.source.text


def test_identical_proposal_can_reuse_existing_delivery_after_target_progress(case):
    c = case
    prior = apply_synthetic_gateway(c, preview(c))
    next_summary(c)
    proposal = preview(c, intent='propose_update', target_publication_id=prior.items[0].publication_id)
    first = apply_synthetic_gateway(c, proposal)
    task = c.team.get_projection(c.owner.id, '91')
    c.team.save_projection(task.model_copy(update={'revision': 1, 'remote_fingerprint': 'c' * 64}), expected_revision=0)
    repeated = preview(c, intent='propose_update', target_publication_id=prior.items[0].publication_id)
    second = c.service.confirm(c.meeting, command(repeated))
    assert second.items[0].delivery_operation_id == first.items[0].delivery_operation_id
    assert second.items[0].execution_state.state == 'applied' and not second.items[0].source_stale
    assert len(c.repository.publications(c.meeting, '7')) == 2


def test_multiple_verified_deliveries_for_one_publication_coalesce_to_one_target(case):
    c = case
    prior = apply_synthetic_gateway(c, preview(c))
    next_summary(c)
    first = apply_synthetic_gateway(c, preview(c, intent='propose_update', target_publication_id=prior.items[0].publication_id))
    second = apply_synthetic_gateway(c, preview(c, intent='propose_update', target_publication_id=prior.items[0].publication_id,
        due_resolution='date', due_at=c.time[0] + timedelta(days=2)))
    assert first.items[0].publication_id == second.items[0].publication_id
    assert first.items[0].delivery_operation_id != second.items[0].delivery_operation_id
    next_summary(c)
    candidate = c.service.context(c.meeting).candidates[0]
    ids = [item.publication_id for item in candidate.possible_supersedes]
    assert len(ids) == len(set(ids)) == 2
    linked = preview(c, intent='link', target_publication_id=first.items[0].publication_id)
    assert linked.items[0].target_task_id == '91'


def test_ambiguous_applied_task_ids_for_one_publication_cannot_be_linked(case):
    c = case
    prior = apply_synthetic_gateway(c, preview(c))
    separate = apply_synthetic_gateway(c, preview(c, intent='create_separate', separate_id=uid()), task_id='92')
    next_summary(c)
    proposal = apply_synthetic_gateway(c, preview(c, intent='propose_update', target_publication_id=prior.items[0].publication_id))
    apply_synthetic_gateway(c, preview(c, intent='propose_update', target_publication_id=separate.items[0].publication_id))
    next_summary(c)
    result = c.service.preview(c.meeting, request(c, intent='link', target_publication_id=proposal.items[0].publication_id))
    assert result.preview is None and 'publication_target_ambiguous' in result.candidates[0].reason_codes


@pytest.mark.parametrize('source_case', [{'title': 'Д' * 4000}], indirect=True)
def test_proposal_comment_formatter_limit_blocks_preview_without_source_truncation(case):
    c = case
    prior = apply_synthetic_gateway(c, preview(c))
    next_summary(c)
    result = c.service.preview(c.meeting, request(c, intent='propose_update', target_publication_id=prior.items[0].publication_id))
    assert result.preview is None and result.candidates[0].eligible
    assert result.candidates[0].title == 'Д' * 4000
    assert 'publication_payload_too_large' in result.candidates[0].reason_codes


@pytest.mark.parametrize('source_case', [{'title': 'Д' * 3990, 'text': 'Я подготовлю ' + 'д' * 3980, 'actions': 80}], indirect=True)
def test_combined_preview_must_fit_exact_one_mebibyte_confirmation_body(case):
    c = case
    result = c.service.preview(c.meeting, PreviewPublications(scope=c.scope, selections=tuple(
        PublicationSelection(action_id=item.action.action_id, due_resolution='none') for item in c.state.items)))
    assert result.preview is None and len(result.candidates) == 80
    assert all(item.eligible and 'publication_payload_too_large' in item.reason_codes for item in result.candidates)
