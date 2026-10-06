"""Pure publication proof checks: no application, storage or provider fixtures."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import importlib
import importlib.util

import pytest

from secretary.application.assignment_resolver import resolve_assignment, review_assignment
from secretary.domain.assignment_evidence import (
    AssignmentContext, AssignmentReview, AssignmentScope, DraftAction,
    IdentityEvidence, RosterParticipant, SourceSegment,
)


def api():
    name = "secretary.application.publication_evidence"
    assert importlib.util.find_spec(name) is not None, "publication evidence validator is not implemented"
    return importlib.import_module(name)


def evidence_case(*, manual=False, identity_status="confirmed", profile="11111111-1111-4111-8111-111111111111",
                  text="Я подготовлю отчёт.", quote=None, semantic="explicit_commitment", basis="self_commitment"):
    scope = AssignmentScope(meeting_id="meeting", transcript_version=3, summary_version=4)
    source = SourceSegment(meeting_id="meeting", transcript_version=3, segment_id="source", text=text)
    person = RosterParticipant(meeting_id="meeting", participant_id="person", display_name="Павел",
                              person_profile_id=profile)
    other = RosterParticipant(meeting_id="meeting", participant_id="other", display_name="Алексей")
    identity = IdentityEvidence(meeting_id="meeting", transcript_version=3, segment_id="source",
                                participant_id="person", status=identity_status, revision=2, method="manual")
    context = AssignmentContext(scope=scope, attribution_revision=2, roster_revision=5,
                                sources=(source,), participants=(person, other), identities=(identity,))
    action = DraftAction(scope=scope, action_id="action", text="Подготовить отчёт", basis=basis,
                         named_owner_text="Павел" if basis == "named_person" else None,
                         commitment_segment_id="source" if basis == "self_commitment" else None,
                         semantic_flag=semantic, evidence_quote=text if quote is None else quote,
                         source_segment_ids=("source",))
    draft = resolve_assignment(action, context)
    item = review_assignment(draft, AssignmentReview(scope=scope, action_id="action",
        decision="set_manual" if manual else "confirm_proposal", participant_id="person" if manual else None,
        expected_attribution_revision=2, expected_roster_revision=5), context)
    return item, context


def rejected(item, context, code=None):
    module = api()
    with pytest.raises(module.PublicationEvidenceError) as caught:
        module.validate_publication_evidence(item, context)
    assert caught.value.code
    assert str(caught.value) == caught.value.code
    assert item.action.text not in str(caught.value)
    assert item.action.evidence_quote not in str(caught.value) if item.action.evidence_quote else True
    if code is not None:
        assert caught.value.code == code


def test_confirmed_self_assignment_returns_exact_source_proof():
    item, context = evidence_case()
    result = api().validate_publication_evidence(item, context)
    assert result.participant_id == "person"
    assert result.person_profile_id == "11111111-1111-4111-8111-111111111111"
    assert result.source_segment_ids == ("source",)
    assert result.title == "Подготовить отчёт"
    assert result.evidence_quote == "Я подготовлю отчёт."
    assert result.source_fingerprint == item.fingerprint
    assert result.anchor.segment_id == "source"
    assert result.anchor.start_char == 0
    assert result.anchor.end_char == len("Я подготовлю отчёт.")
    assert result.anchor.source_text_hash == hashlib.sha256("Я подготовлю отчёт.".encode()).hexdigest()


def test_confirmed_named_assignment_does_not_require_speaker_identity():
    item, context = evidence_case(text="Павел подготовит отчёт.", basis="named_person", identity_status="unknown")
    assert api().validate_publication_evidence(item, context).participant_id == "person"


def test_manual_source_proof_is_valid_despite_false_automatic_eligibility():
    item, context = evidence_case(manual=True, identity_status="unknown")
    assert item.status == "confirmed" and item.confirm_eligible is False
    assert api().validate_publication_evidence(item, context).participant_id == "person"


def test_guest_valid_proof_preserves_missing_profile_for_explicit_team_resolution():
    item, context = evidence_case(manual=True, profile=None)
    assert api().validate_publication_evidence(item, context).person_profile_id is None


def test_validated_publication_proof_is_frozen_and_input_is_unchanged():
    item, context = evidence_case()
    original = (item.model_dump(), context.model_dump())
    result = api().validate_publication_evidence(item, context)
    with pytest.raises(FrozenInstanceError):
        result.participant_id = "other"
    assert (item.model_dump(), context.model_dump()) == original


@pytest.mark.parametrize("status", ["proposed", "needs_review"])
def test_only_current_confirmed_assignments_can_publish(status):
    item, context = evidence_case()
    rejected(item.model_copy(update={"status": status}), context, "assignment_not_confirmed")


@pytest.mark.parametrize("field,value", [("meeting_id", "foreign"), ("transcript_version", 2), ("summary_version", 3)])
def test_complete_action_scope_must_match_current_context(field, value):
    item, context = evidence_case()
    action = item.action.model_copy(update={"scope": item.action.scope.model_copy(update={field: value})})
    rejected(item.model_copy(update={"action": action}), context, "publication_scope_mismatch")


@pytest.mark.parametrize("references", [(), ("source", "source"), ("missing",)])
def test_empty_duplicate_or_missing_source_references_cannot_publish(references):
    item, context = evidence_case(manual=True)
    rejected(item.model_copy(update={"action": item.action.model_copy(update={"source_segment_ids": references})}), context)


@pytest.mark.parametrize("field,value", [("meeting_id", "foreign"), ("transcript_version", 2)])
def test_matching_source_id_cannot_authorize_a_foreign_source(field, value):
    item, context = evidence_case(manual=True)
    source = context.sources[0].model_copy(update={field: value})
    rejected(item, context.model_copy(update={"sources": (source,)}))


def test_duplicate_source_record_cannot_be_used_as_proof():
    item, context = evidence_case(manual=True)
    rejected(item, context.model_copy(update={"sources": context.sources * 2}))


@pytest.mark.parametrize("quote", ["", " ", "Я сделаю презентацию."])
def test_empty_or_nonliteral_quote_rejects_even_manual_confirmation(quote):
    item, context = evidence_case(manual=True, quote=quote)
    assert item.anchor is None or item.fingerprint is None
    rejected(item, context)


def test_repeated_quote_in_one_source_requires_review():
    item, context = evidence_case(manual=True, text="Я подготовлю отчёт. Я подготовлю отчёт.", quote="Я подготовлю отчёт.")
    rejected(item, context)


def test_repeated_quote_across_cited_sources_requires_review():
    item, context = evidence_case(manual=True)
    second = context.sources[0].model_copy(update={"segment_id": "second"})
    action = item.action.model_copy(update={"source_segment_ids": ("source", "second")})
    rejected(item.model_copy(update={"action": action}), context.model_copy(update={"sources": (*context.sources, second)}))


def test_source_text_change_outside_quote_invalidates_its_old_hash():
    item, context = evidence_case(manual=True)
    source = context.sources[0].model_copy(update={"text": "Я подготовлю отчёт. Дополнение."})
    rejected(item, context.model_copy(update={"sources": (source,)}), "publication_source_changed")


@pytest.mark.parametrize("update", [{"start_char": 1}, {"end_char": 1}, {"segment_id": "other"}, {"source_text_hash": "0" * 64}])
def test_stored_anchor_cannot_be_replaced_by_fresh_proof_without_review(update):
    item, context = evidence_case(manual=True)
    rejected(item.model_copy(update={"anchor": item.anchor.model_copy(update=update)}), context,
             "publication_source_changed")


@pytest.mark.parametrize("field,value", [("anchor", None), ("fingerprint", None), ("fingerprint", "forged")])
def test_manual_confirmation_does_not_repair_missing_or_forged_stored_proof(field, value):
    item, context = evidence_case(manual=True)
    rejected(item.model_copy(update={field: value}), context)


def test_changed_action_title_is_not_authorized_by_the_previous_fingerprint():
    item, context = evidence_case(manual=True)
    rejected(item.model_copy(update={"action": item.action.model_copy(update={"text": "Другое поручение"})}),
             context, "publication_source_changed")


@pytest.mark.parametrize("participant_id", [None, "ghost"])
def test_confirmed_but_missing_participant_cannot_publish(participant_id):
    item, context = evidence_case(manual=True)
    rejected(item.model_copy(update={"participant_id": participant_id}), context)


@pytest.mark.parametrize("update", [{"enabled": False}, {"meeting_id": "foreign"}])
def test_assigned_participant_must_be_enabled_and_from_this_meeting(update):
    item, context = evidence_case(manual=True)
    participant = context.participants[0].model_copy(update=update)
    rejected(item, context.model_copy(update={"participants": (participant, context.participants[1])}))


def test_duplicate_participant_identity_is_ambiguous():
    item, context = evidence_case(manual=True)
    rejected(item, context.model_copy(update={"participants": (*context.participants, context.participants[0])}))


def test_late_identity_change_cannot_publish_an_old_automatic_assignment():
    item, context = evidence_case()
    changed = context.identities[0].model_copy(update={"participant_id": "other"})
    rejected(item, context.model_copy(update={"identities": (changed,)}), "publication_assignment_changed")


def test_stale_voice_identity_cannot_publish_an_old_automatic_assignment():
    item, context = evidence_case()
    changed = context.identities[0].model_copy(update={"stale": True})
    rejected(item, context.model_copy(update={"identities": (changed,)}), "publication_assignment_changed")


def test_late_identity_change_does_not_replace_an_explicit_manual_owner():
    item, context = evidence_case(manual=True)
    changed = context.identities[0].model_copy(update={"participant_id": "other"})
    assert api().validate_publication_evidence(item, context.model_copy(update={"identities": (changed,)})).participant_id == "person"


def test_revision_bump_without_semantic_change_does_not_invalidate_evidence():
    item, context = evidence_case()
    assert api().validate_publication_evidence(item, context.model_copy(update={"attribution_revision": 3, "roster_revision": 6})).participant_id == "person"


@pytest.mark.parametrize("semantic", ["reported_speech", "question", "hypothesis", "negation", "ambiguous"])
def test_manual_owner_cannot_override_a_noncommitment_semantic_flag(semantic):
    item, context = evidence_case(manual=True, semantic=semantic)
    assert item.anchor is not None and item.fingerprint is not None
    rejected(item, context, "publication_semantic_conflict")


@pytest.mark.parametrize("text,quote", [
    ("Я подготовлю отчёт?", "Я подготовлю отчёт"),
    ("Он сказал: Я подготовлю отчёт.", "Я подготовлю отчёт."),
    ("Если потребуется, Я подготовлю отчёт.", "Я подготовлю отчёт."),
    ("Я не буду готовить отчёт. Я подготовлю отчёт.", "Я подготовлю отчёт."),
    ("Мы подготовим отчёт.", "Мы подготовим отчёт."),
])
def test_manual_confirmation_cannot_strip_contradictory_source_framing(text, quote):
    item, context = evidence_case(manual=True, text=text, quote=quote)
    rejected(item, context, "publication_semantic_conflict")


def test_unknown_draft_basis_can_be_manually_assigned_with_valid_source_proof():
    item, context = evidence_case(manual=True, basis="unknown")
    assert api().validate_publication_evidence(item, context).participant_id == "person"


def test_unknown_draft_basis_does_not_bypass_manual_semantic_checks():
    item, context = evidence_case(manual=True, basis="unknown", text="Я подготовлю отчёт?")
    rejected(item, context, "publication_semantic_conflict")


def test_self_commitment_cannot_export_a_directive_after_manual_confirmation():
    item, context = evidence_case(manual=True, semantic="directive")
    rejected(item, context, "publication_semantic_conflict")
