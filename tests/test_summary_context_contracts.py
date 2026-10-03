import copy
import pytest
from pydantic import ValidationError

from secretary.application.assignments import (
    draft_actions, locally_revalidate, project_summary, resolve_summary, review_batch,
)
from secretary.application.summary_context import (
    assignment_context, freeze_summary_context, provider_context, summary_contract,
)
from secretary.domain.assignment_evidence import AssignmentEvidenceError
from secretary.domain.summary_context import FrozenSummaryContext, ReviewTaskAssignments
from secretary.infrastructure.summary_context_payloads import (
    SummaryContextPayloadError, body_bytes, map_batches, merge_batches, summary_body,
)


def frozen(*, participant="p", status="confirmed", roster_revision=1, attribution_revision=1,
           enabled=True, aliases=("Павел",), text="Я подготовлю отчёт"):
    return freeze_summary_context(
        meeting_id="m", transcript_version=1,
        settings={"summary_model": "synthetic/model", "summary_batch_chars": 12000,
                  "summary_max_output_tokens": 8192},
        segments=[{"id": "s", "meeting_id": "m", "transcript_version": 1, "text": text,
                   "start_ms": 0, "end_ms": 1000, "timing_precision": "segment", "channel": "mixed"}],
        attribution={"meeting_id": "m", "transcript_version": 1,
                     "revision": attribution_revision, "roster_revision": roster_revision,
                     "participants": [{"id": "p", "display_name": "Павел Александрович", "enabled": enabled,
                                       "aliases": list(aliases), "person_profile_id": None}],
                     "items": [{"segment_id": "s", "participant_id": participant, "status": status,
                                "method": "manual", "revision": attribution_revision, "run_id": "run"}]},
    )


def summary(context, *, action_id="a", task="Подготовить отчёт", legacy=False):
    action = {"id": action_id, "text": task, "owner": "историческое имя", "due_date": None,
              "source_segment_ids": ["s"]}
    if not legacy:
        action.update(evidence_quote=context.sources[0].text, assignment_proposal={
            "basis": "self_commitment", "named_owner_text": None,
            "commitment_segment_id": "s", "semantic_flag": "explicit_commitment"})
    return {"meeting_id": "m", "transcript_version": 1, "summary_version": 1,
            "overview": "", "readable_transcript": "", "decisions": [],
            "action_items": [action], "open_questions": []}


def command(snapshot, decision="confirm_proposal", participant=None, **edits):
    return ReviewTaskAssignments.model_validate({
        "transcript_version": 1, "summary_version": snapshot.summary_version,
        "attribution_revision": snapshot.attribution_revision, "roster_revision": snapshot.roster_revision,
        "expected_revision": snapshot.revision, "operation_id": "op",
        "changes": [{"action_id": "a", "decision": decision, "participant_id": participant}], **edits,
    })


def initial(f, raw=None):
    ctx = assignment_context(f, 1)
    return ctx, resolve_summary(raw or summary(f), ctx, ctx, captured_context_hash=f.context_hash)


def test_context_roundtrip_exact_source_and_no_credentials():
    f = frozen(aliases=("Павел", "Павел", "П. А."), text="Я подготовлю отчёт — №1.")
    assert FrozenSummaryContext.model_validate_json(f.model_dump_json()) == f
    assert f.sources[0].text == "Я подготовлю отчёт — №1."
    assert frozen(aliases=("П. А.", "Павел"), text="Я подготовлю отчёт — №1.").context_hash == f.context_hash
    assert f.sources[0].speaker_observation_id != "s"
    assert "settings" not in provider_context(f)
    with pytest.raises(ValidationError):
        f.sources[0].text = "changed"


def test_context_tamper_and_foreign_source_fail_closed():
    f = frozen()
    body = f.model_dump()
    body["sources"][0]["text"] = "edited"
    with pytest.raises(ValidationError, match="summary_source_hash_mismatch"):
        FrozenSummaryContext.model_validate(body)
    with pytest.raises(ValueError, match="summary_context_foreign_source"):
        provider_context(f, {"foreign"})


def test_semantic_draft_explicit_review_and_status_projection():
    f = frozen()
    ctx, state = initial(f)
    assert state.items[0].participant_id == "p"
    assert state.items[0].status == "proposed"
    assert state.items[0].confirm_eligible
    result = review_batch(state, command(state), ctx)
    assert result.revision == 1 and result.items[0].status == "confirmed"
    projected = project_summary(summary(f), result, ctx)["action_items"][0]
    assert projected["owner"] == "Павел Александрович"
    assert projected["assignment_status"] == "confirmed"
    assert projected["evidence_quote"] == f.sources[0].text


def test_legacy_raw_owner_not_proof_and_manual_choice_explicit():
    f = frozen()
    ctx, state = initial(f, summary(f, legacy=True))
    assert state.items[0].status == "needs_review" and state.items[0].participant_id is None
    assert project_summary(summary(f, legacy=True), state, ctx)["action_items"][0]["owner"] is None
    result = review_batch(state, command(state, "set_manual", "p"), ctx)
    assert result.items[0].basis == "manual" and result.items[0].status == "confirmed"
    assert result.items[0].action.evidence_quote == ""


def test_local_changed_identity_loses_confirmation_and_preserves_quote():
    f = frozen()
    ctx, state = initial(f)
    confirmed = review_batch(state, command(state), ctx)
    changed = assignment_context(frozen(participant=None, status="unknown", attribution_revision=2), 1)
    result = locally_revalidate(confirmed, changed)
    assert result.revision == 2 and result.items[0].status == "needs_review"
    assert result.items[0].participant_id is None
    assert result.items[0].action.evidence_quote == f.sources[0].text
    assert locally_revalidate(result, changed) == result


def test_manual_choice_keeps_id_with_disabled_conflict():
    f = frozen()
    ctx, state = initial(f)
    manual = review_batch(state, command(state, "set_manual", "p"), ctx)
    changed = assignment_context(frozen(enabled=False, roster_revision=2), 1)
    result = locally_revalidate(manual, changed)
    assert result.items[0].participant_id == "p" and result.items[0].basis == "manual"
    assert result.items[0].status == "needs_review"
    assert "manual_participant_disabled" in result.items[0].reason_codes


def test_regeneration_id_collision_no_carryover_but_exact_unique_is_proposal():
    f = frozen()
    ctx, state = initial(f)
    manual = review_batch(state, command(state, "set_manual", "p"), ctx)
    current = assignment_context(f, 2)
    unrelated = summary(f, task="Другая задача")
    unrelated["summary_version"] = 2
    different = resolve_summary(unrelated, current, current, captured_context_hash=f.context_hash, previous=manual)
    assert different.items[0].previous_provenance is None and different.items[0].status != "confirmed"
    exact = summary(f)
    exact["summary_version"] = 2
    proposal = resolve_summary(exact, current, current, captured_context_hash=f.context_hash, previous=manual)
    assert proposal.items[0].status == "proposed" and not proposal.items[0].confirm_eligible
    assert proposal.items[0].previous_provenance.summary_version == 1


def test_revision_scope_invalid_model_and_duplicate_batch_rejected():
    f = frozen()
    ctx, state = initial(f)
    with pytest.raises(AssignmentEvidenceError, match="review_revision_conflict"):
        review_batch(state, command(state, roster_revision=2), ctx)
    raw = summary(f)
    raw["action_items"][0]["assignment_proposal"]["basis"] = "manual"
    with pytest.raises(ValidationError):
        draft_actions(raw, ctx)
    change = {"action_id": "a", "decision": "clear", "participant_id": None}
    with pytest.raises(ValidationError, match="assignment_duplicate_changes"):
        command(state, changes=[change, copy.deepcopy(change)])


def test_legacy_paid_discriminator_and_frozen_input_is_reusable():
    f = frozen()
    assert summary_contract({}, has_paid_state=True) == "legacy-v1"
    assert summary_contract({}, has_paid_state=False) == f.contract
    assert summary_contract({"summary_context": f.model_dump()}, has_paid_state=True) == f.contract
    assert summary_contract({"summary_contract": "legacy-v1"}, has_paid_state=False) == "legacy-v1"


def test_projection_refuses_mixed_revision_context():
    f = frozen()
    _, state = initial(f)
    changed = assignment_context(frozen(roster_revision=2), 1)
    with pytest.raises(AssignmentEvidenceError, match="assignment_context_changed"):
        project_summary(summary(f), state, changed)


def test_map_split_counts_serialized_metadata_and_preserves_exact_speech():
    text = "Я подготовлю отчёт — №1. " * 500
    f = frozen(text=text)
    groups = map_batches(f, 2400)
    pieces = [piece for group in groups for piece in group]
    assert len(groups) > 1
    assert "".join(piece["text"] for piece in pieces) == text
    assert all(body_bytes(f, group, merge=False) <= 2400 for group in groups)
    assert all(piece["participant_id"] == "p" and piece["identity_status"] == "confirmed" for piece in pieces)
    assert all(piece["speaker_observation_id"] == f.sources[0].speaker_observation_id for piece in pieces)
    assert all(piece["text_hash"] == f.sources[0].text_hash for piece in pieces)
    assert pieces[0]["piece_start_char"] == 0 and pieces[-1]["piece_end_char"] == len(text)
    assert all(summary_body(f, group, merge=False)["untrusted_summary_context"]["context_hash"] == f.context_hash
               for group in groups)


def test_metadata_overflow_fails_without_trimming_context():
    f = frozen(aliases=("Очень длинное имя " * 100,))
    with pytest.raises(SummaryContextPayloadError, match="summary_context_metadata_too_large"):
        map_batches(f, 2400)


def test_merge_preserves_quote_proposal_and_captured_roster():
    f = frozen()
    partial = {"overview": "", "decisions": [], "open_questions": [],
               "action_items": summary(f)["action_items"]}
    original = copy.deepcopy(partial)
    groups = merge_batches(f, [partial, copy.deepcopy(partial)], 2400)
    assert partial == original
    assert all(body_bytes(f, group, merge=True) <= 2400 for group in groups)
    for group in groups:
        body = summary_body(f, group, merge=True)
        assert body["untrusted_summary_context"]["participants"][0]["display_name"] == "Павел Александрович"
        assert body["untrusted_summary_context"]["source_identities"][0]["id"] == "s"
        assert group[0]["action_items"][0]["evidence_quote"] == f.sources[0].text
        assert group[0]["action_items"][0]["assignment_proposal"]["basis"] == "self_commitment"


def test_manual_identity_cannot_clear_original_overlap_or_chunk_ambiguity():
    f=frozen()
    segments=[{'id':'s','meeting_id':'m','transcript_version':1,'text':'Я подготовлю отчёт','start_ms':0,'end_ms':1000,'timing_precision':'segment','channel':'mixed'},
              {'id':'s2','meeting_id':'m','transcript_version':1,'text':'Параллельная речь','start_ms':500,'end_ms':1500,'timing_precision':'segment','channel':'mixed'}]
    attribution={'meeting_id':'m','transcript_version':1,'revision':1,'roster_revision':1,
                 'participants':[{'id':'p','display_name':'Павел','enabled':True,'aliases':[]}],
                 'items':[{'segment_id':'s','participant_id':'p','status':'confirmed','method':'manual','revision':1,'run_id':'run'}]}
    overlapping=freeze_summary_context(meeting_id='m',transcript_version=1,settings=f.settings.model_dump(),segments=segments,attribution=attribution)
    assert overlapping.sources[0].overlap and overlapping.sources[1].overlap
    _,state=initial(overlapping)
    assert not state.items[0].confirm_eligible and state.items[0].status=='needs_review'
    chunk=copy.deepcopy(segments[:1]); chunk[0]['timing_precision']='chunk'
    coarse=freeze_summary_context(meeting_id='m',transcript_version=1,settings=f.settings.model_dump(),segments=chunk,attribution=attribution)
    assert coarse.sources[0].ambiguous_author
