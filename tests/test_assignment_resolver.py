"""Tiny synthetic Russian fixtures. Pure functions, no DB/provider/device/files."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from secretary.application.assignment_resolver import (
    normalize_name, resolve_assignment, resolve_assignments, review_assignment,
    revalidate_assignment, revalidate_assignments, suggest_carryover,
)
from secretary.domain.assignment_evidence import (
    AssignmentContext, AssignmentEvidenceError, AssignmentReview, AssignmentScope,
    DraftAction, IdentityEvidence, RosterParticipant, SourceSegment,
)

SCOPE = AssignmentScope(meeting_id="meeting", transcript_version=1, summary_version=1)
PAVEL = RosterParticipant(meeting_id="meeting", participant_id="pavel", display_name="Павел Александрович")
ALEX = RosterParticipant(meeting_id="meeting", participant_id="alex", display_name="Алексей")
ME = RosterParticipant(meeting_id="meeting", participant_id="me", display_name="Я", aliases=("мы", "меня"))
GUEST = RosterParticipant(meeting_id="meeting", participant_id="guest", display_name="Гость 1")


def fixture(text="Я подготовлю отчёт", *, basis="self_commitment", name=None,
            semantic="explicit_commitment", identity_status="confirmed", participant="pavel",
            quote=None, roster=(PAVEL, ALEX, ME), identity_changes=None):
    source = SourceSegment(meeting_id="meeting", transcript_version=1, segment_id="s1",
                           text=text, start_ms=1000, end_ms=2000)
    identity = IdentityEvidence(meeting_id="meeting", transcript_version=1, segment_id="s1",
        participant_id=participant, status=identity_status, revision=2, method="manual")
    if identity_changes:
        identity = identity.model_copy(update=identity_changes)
    action = DraftAction(scope=SCOPE, action_id="action", text="Подготовить отчёт", basis=basis,
        named_owner_text=name, commitment_segment_id="s1" if basis == "self_commitment" else None,
        semantic_flag=semantic, evidence_quote=text if quote is None else quote, source_segment_ids=("s1",))
    context = AssignmentContext(scope=SCOPE, attribution_revision=2, roster_revision=3,
        sources=(source,), participants=roster, identities=(identity,))
    return action, context


def command(result, context, decision="confirm_proposal", participant=None):
    return AssignmentReview(scope=context.scope, action_id=result.action.action_id, decision=decision,
        participant_id=participant, expected_attribution_revision=context.attribution_revision,
        expected_roster_revision=context.roster_revision)


@pytest.mark.parametrize("kwargs,expected_participant,basis,status,reason", [
    ({}, "pavel", "self_commitment", "proposed", "draft_evidence_valid"),
    ({"text": "Павел Александрович подготовит отчёт", "basis": "named_person",
      "name": "Павел Александрович", "identity_status": "unknown", "participant": None},
     "pavel", "named_person", "proposed", "draft_evidence_valid"),
    ({"text": "Алексей, подготовь смету", "basis": "named_person", "name": "Алексей", "semantic": "directive"},
     "alex", "named_person", "proposed", "draft_evidence_valid"),
    ({"text": "Я не буду делать отчёт", "semantic": "negation"},
     "pavel", "self_commitment", "needs_review", "semantic_negation"),
    ({"text": "Павел сказал: я подготовлю", "semantic": "reported_speech"},
     "pavel", "self_commitment", "needs_review", "semantic_reported_speech"),
    ({"text": "Мы сделаем", "basis": "unknown", "semantic": "ambiguous"},
     None, "unknown", "needs_review", "unknown_basis"),
    ({"text": "Я сделаю", "identity_status": "unknown", "participant": None},
     None, "self_commitment", "needs_review", "identity_unknown"),
])
def test_seven_spec_rows(kwargs, expected_participant, basis, status, reason):
    action, context = fixture(**kwargs)
    result = resolve_assignment(action, context)
    assert (result.participant_id, result.basis, result.status) == (expected_participant, basis, status)
    assert reason in result.reason_codes
    assert result.action.evidence_quote == action.evidence_quote
    assert result.action.source_segment_ids == ("s1",)
    assert result.anchor is not None
    assert result.anchor.segment_id == "s1"
    assert (result.anchor.start_char, result.anchor.end_char) == (0, len(action.evidence_quote))
    assert len(result.anchor.source_text_hash) == 64
    assert result.confirm_eligible == (status == "proposed")
    assert result.status != "confirmed"


@pytest.mark.parametrize("text,flag,reason", [
    ("Я не буду делать отчёт", "explicit_commitment", "contradiction_negation"),
    ("Я подготовлю отчёт?", "explicit_commitment", "contradiction_question"),
    ("Если я подготовлю отчёт", "explicit_commitment", "contradiction_hypothesis"),
    ("Может, Алексей подготовит отчёт", "hypothesis", "semantic_hypothesis"),
    ("Я подготовлю отчёт", "question", "semantic_question"),
    ("Я", "ambiguous", "semantic_ambiguous"),
    ("Я подготовлю отчёт", "directive", "self_requires_commitment"),
    ("Я", "explicit_commitment", "insufficient_commitment_evidence"),
    ("Мы сделаем", "explicit_commitment", "collective_or_indefinite_commitment"),
    ("Кто-нибудь сделает", "explicit_commitment", "collective_or_indefinite_commitment"),
])
def test_flags_and_obvious_contradictions_do_not_confirm(text, flag, reason):
    action, context = fixture(text, semantic=flag)
    result = resolve_assignment(action, context)
    assert result.status == "needs_review" and not result.confirm_eligible
    assert reason in result.reason_codes
    with pytest.raises(AssignmentEvidenceError, match="proposal_not_eligible"):
        review_assignment(result, command(result, context), context)


@pytest.mark.parametrize("text,reason", [
    ("Павел сказал: «Я подготовлю отчёт»", "contradiction_reported_speech"),
    ("«Я подготовлю отчёт»?", "contradiction_question"),
    ("Я подготовлю отчёт, но не буду делать отчёт", "contradiction_negation"),
])
def test_positive_extracted_quote_cannot_erase_original_framing(text, reason):
    action, context = fixture(text, quote="Я подготовлю отчёт")
    result = resolve_assignment(action, context)
    assert result.status == "needs_review" and reason in result.reason_codes
    assert result.anchor.start_char == text.index(action.evidence_quote)


@pytest.mark.parametrize("name,text,roster,reason", [
    ("Алексей", "Алексей сделает", (ALEX, ALEX.model_copy(update={"participant_id": "alex2"})), "named_owner_ambiguous"),
    ("Павел", "Павел сделает", (PAVEL,), "named_owner_missing"),
    ("Павел", "Павел сделает", (PAVEL.model_copy(update={"aliases": ("Павел",)}),
       ALEX.model_copy(update={"aliases": ("Павел",)})), "named_owner_ambiguous"),
    ("Борис", "Борис сделает", (PAVEL,), "named_owner_missing"),
    ("Алексей", "СуперАлексей сделает", (ALEX,), "named_owner_not_in_quote"),
    ("Алексей", "Алексейский сделает", (ALEX,), "named_owner_not_in_quote"),
    ("Алексей", "Алексей\u0301 сделает", (ALEX,), "named_owner_not_in_quote"),
    ("Алексей", "Алексей\u200dский сделает", (ALEX,), "named_owner_not_in_quote"),
    ("Алексей", "Алексей-Борис сделает", (ALEX,), "named_owner_not_in_quote"),
    ("Алексей", "Алексей сделает", (ALEX.model_copy(update={"enabled": False}),), "named_owner_missing"),
    ("Алексей", "Алексей сделает", (ALEX.model_copy(update={"meeting_id": "foreign"}),), "named_owner_missing"),
])
def test_named_matching_is_exact_enabled_and_unambiguous(name, text, roster, reason):
    action, context = fixture(text, basis="named_person", name=name, roster=roster)
    result = resolve_assignment(action, context)
    assert result.participant_id is None and result.status == "needs_review"
    assert reason in result.reason_codes


@pytest.mark.parametrize("text,quote", [
    ("СуперАлексей сделает", "Алексей сделает"),
    ("Задача для Алексейского", "Задача для Алексей"),
    ("СуперАлексей", "Алексей"),
    ("Алексей придёт. СуперАлексей сделает", "Алексей сделает"),
    ("Задача для Алексейского. Алексей придёт", "Задача для Алексей"),
    ("Задача для Алексей\u0301", "Задача для Алексей"),
])
def test_clipped_name_boundaries_use_only_anchored_original_occurrence(text, quote):
    action, context = fixture(text, quote=quote, basis="named_person", name="Алексей",
                              semantic="directive", identity_status="unknown", participant=None)
    result = resolve_assignment(action, context)
    assert (result.participant_id, result.status, result.confirm_eligible) == (None, "needs_review", False)
    assert "named_owner_not_in_quote" in result.reason_codes
    assert result.anchor is not None
    assert context.sources[0].text[result.anchor.start_char:result.anchor.end_char] == quote
    with pytest.raises(AssignmentEvidenceError, match="proposal_not_eligible"):
        review_assignment(result, command(result, context), context)


@pytest.mark.parametrize("text,quote", [
    ("Слово Алексей сделает", " Алексей сделает"),
    ("Слово Алексей сделает", "Алексей сделает"),
    ("Алексей потом. СуперАлексей сделает. Алексей подготовит", "Алексей подготовит"),
    ("Сделает Алексей завтра", "Сделает Алексей "),
])
def test_valid_whole_name_inside_unique_anchor_remains_eligible(text, quote):
    action, context = fixture(text, quote=quote, basis="named_person", name="Алексей", semantic="directive")
    result = resolve_assignment(action, context)
    assert (result.participant_id, result.status, result.confirm_eligible) == ("alex", "proposed", True)
    assert review_assignment(result, command(result, context), context).status == "confirmed"


@pytest.mark.parametrize("label", ["Нему", "Нею"])
@pytest.mark.parametrize("roster_label", ["display_name", "alias"])
def test_personal_pronoun_forms_rejected_as_display_or_alias_even_on_confirmation(label, roster_label):
    roster_entry = ME.model_copy(update=(
        {"display_name": label, "aliases": ()} if roster_label == "display_name"
        else {"display_name": "Иван", "aliases": (label,)}))
    action, context = fixture(f"{label} поручено подготовить отчёт", basis="named_person", name=label,
        semantic="directive", roster=(roster_entry,), identity_status="unknown", participant=None)
    result = resolve_assignment(action, context)
    assert (result.participant_id, result.status, result.confirm_eligible) == (None, "needs_review", False)
    assert "generic_named_owner" in result.reason_codes
    with pytest.raises(AssignmentEvidenceError, match="proposal_not_eligible"):
        review_assignment(result, command(result, context), context)
    manual = review_assignment(result, command(result, context, "set_manual", "me"), context)
    assert (manual.participant_id, manual.basis, manual.status) == ("me", "manual", "confirmed")
    self_action, self_context = fixture("Я подготовлю отчёт", participant="me", roster=(roster_entry,))
    self_result = resolve_assignment(self_action, self_context)
    assert (self_result.participant_id, self_result.basis, self_result.status) == ("me", "self_commitment", "proposed")
    assert review_assignment(self_result, command(self_result, self_context), self_context).status == "confirmed"


def test_explicit_alias_unicode_case_and_whitespace():
    action, context = fixture("ПАВЕЛ   АЛЕКСАНДРОВИЧ сделает", basis="named_person", name="павел александрович")
    assert resolve_assignment(action, context).participant_id == "pavel"
    action, context = fixture("Павел сделает", basis="named_person", name="Павел",
        roster=(PAVEL.model_copy(update={"aliases": ("Павел",)}),))
    assert resolve_assignment(action, context).confirm_eligible
    assert normalize_name(" АлЕкСе\u0438\u0306  ") == normalize_name("алексей")


@pytest.mark.parametrize("name", ["Я", "я", "Я.", "«мы»", "мы", "меня", "мной", "он", "её", "ними", "кто-нибудь", "кому-либо", "какой-нибудь", "участник", "someone"])
def test_pronoun_and_generic_bypass_blocked_on_proposal_and_confirm(name):
    roster = (ME.model_copy(update={"display_name": name, "aliases": (name,)}),)
    action, context = fixture(f"{name} сделаю", basis="named_person", name=name,
                              roster=roster, identity_status="unknown", participant=None)
    result = resolve_assignment(action, context)
    assert result.participant_id is None
    assert "generic_named_owner" in result.reason_codes
    with pytest.raises(AssignmentEvidenceError, match="proposal_not_eligible"):
        review_assignment(result, command(result, context), context)


def test_display_ya_is_usable_only_for_confirmed_self_or_explicit_manual():
    action, context = fixture(participant="me")
    result = resolve_assignment(action, context)
    assert result.participant_id == "me" and result.confirm_eligible
    action, context = fixture("Я сделаю", participant=None, identity_status="unknown")
    result = resolve_assignment(action, context)
    manual = review_assignment(result, command(result, context, "set_manual", "me"), context)
    assert (manual.participant_id, manual.basis, manual.status) == ("me", "manual", "confirmed")
    assert manual.anchor == result.anchor and manual.action == result.action


@pytest.mark.parametrize("changes,reason", [
    ({"status": "unknown"}, "identity_unknown"),
    ({"status": "proposed"}, "identity_proposed"),
    ({"status": "conflict"}, "identity_conflict"),
    ({"stale": True}, "identity_stale"),
    ({"revision": 5}, "identity_stale"),
    ({"overlap": True}, "identity_ambiguous_author"),
    ({"ambiguous_author": True}, "identity_ambiguous_author"),
    ({"possible_author_ids": ("pavel", "alex")}, "identity_ambiguous_author"),
    ({"possible_author_ids": ("alex",)}, "identity_ambiguous_author"),
    ({"meeting_id": "other"}, "identity_foreign"),
    ({"participant_id": "profile-not-meeting-participant"}, "participant_missing"),
    ({"participant_id": None}, "identity_unmapped"),
])
def test_self_requires_exact_current_confirmed_unambiguous_identity(changes, reason):
    action, context = fixture(identity_changes=changes)
    result = resolve_assignment(action, context)
    assert result.participant_id is None and not result.confirm_eligible
    assert reason in result.reason_codes


def test_confirmed_mapped_guest_proposed_unknown_guest_needs_review():
    action, context = fixture("Я сделаю", participant="guest", roster=(GUEST,))
    assert resolve_assignment(action, context).participant_id == "guest"
    action, context = fixture("Я сделаю", participant="guest", roster=())
    result = resolve_assignment(action, context)
    assert result.participant_id is None and "participant_missing" in result.reason_codes


def test_exact_commitment_source_not_first_author_or_majority():
    action, context = fixture()
    extra = context.sources[0].model_copy(update={"segment_id": "s0", "text": "Поговорим"})
    other = context.identities[0].model_copy(update={"segment_id": "s0", "participant_id": "alex"})
    context = context.model_copy(update={"sources": (extra, *context.sources), "identities": (other, *context.identities)})
    action = action.model_copy(update={"source_segment_ids": ("s0", "s1")})
    result = resolve_assignment(action, context)
    assert result.participant_id == "pavel" and result.anchor.segment_id == "s1"
    bad = action.model_copy(update={"commitment_segment_id": "s0"})
    assert "commitment_anchor_mismatch" in resolve_assignment(bad, context).reason_codes
    bad = action.model_copy(update={"commitment_segment_id": None})
    assert "missing_commitment_source" in resolve_assignment(bad, context).reason_codes
    bad = action.model_copy(update={"commitment_segment_id": "uncited"})
    assert "commitment_source_not_cited" in resolve_assignment(bad, context).reason_codes


@pytest.mark.parametrize("case,reason", [
    ("scope", "scope_mismatch"), ("foreign", "foreign_source"),
    ("missing", "missing_source"), ("repeat_reference", "duplicate_source_reference"),
    ("repeat_id", "duplicate_source_id"), ("uncited", "quote_not_found"),
    ("two_spans", "ambiguous_quote_anchor"), ("two_sources", "ambiguous_quote_anchor"),
    ("empty_quote", "missing_evidence"),
])
def test_invalid_scope_source_or_ambiguous_anchor(case, reason):
    action, context = fixture()
    if case == "scope":
        action = action.model_copy(update={"scope": SCOPE.model_copy(update={"summary_version": 2})})
    elif case == "foreign":
        context = context.model_copy(update={"sources": (context.sources[0].model_copy(update={"meeting_id": "other"}),)})
    elif case == "missing":
        context = context.model_copy(update={"sources": ()})
    elif case == "repeat_reference":
        action = action.model_copy(update={"source_segment_ids": ("s1", "s1")})
    elif case == "repeat_id":
        context = context.model_copy(update={"sources": context.sources * 2})
    elif case == "uncited":
        action = action.model_copy(update={"evidence_quote": "Иная цитата"})
    elif case == "two_spans":
        context = context.model_copy(update={"sources": (context.sources[0].model_copy(update={"text": action.evidence_quote * 2}),)})
    elif case == "two_sources":
        context = context.model_copy(update={"sources": (*context.sources, context.sources[0].model_copy(update={"segment_id": "s2"}))})
        action = action.model_copy(update={"source_segment_ids": ("s1", "s2")})
    else:
        action = action.model_copy(update={"evidence_quote": ""})
    result = resolve_assignment(action, context)
    assert result.status == "needs_review" and not result.confirm_eligible
    assert reason in result.reason_codes and result.fingerprint is None


def test_legacy_missing_evidence_remains_unknown_without_invention():
    _, context = fixture()
    action = DraftAction(scope=SCOPE, action_id="legacy", text="Подготовить отчёт")
    result = resolve_assignment(action, context)
    assert (result.basis, result.participant_id, result.status) == ("unknown", None, "needs_review")
    assert result.action.evidence_quote == "" and result.action.named_owner_text is None
    assert result.anchor is None and result.fingerprint is None


def test_frozen_input_rejects_llm_manual_confirmed_and_raw_repr():
    action, context = fixture()
    with pytest.raises(ValidationError):
        DraftAction(**{**action.model_dump(), "basis": "manual"})
    with pytest.raises(ValidationError):
        DraftAction(**{**action.model_dump(), "status": "confirmed"})
    with pytest.raises(ValidationError):
        context.sources[0].text = "Изменение"
    assert action.evidence_quote not in repr(action)
    assert action.evidence_quote not in repr(context)


def test_human_confirm_current_revisions_and_unchanged_dependencies():
    action, context = fixture()
    result = resolve_assignment(action, context)
    confirmed = review_assignment(result, command(result, context), context)
    assert confirmed.status == "confirmed" and confirmed.revision == 1
    assert confirmed.anchor == result.anchor and confirmed.action == result.action
    irrelevant = context.model_copy(update={"attribution_revision": 7, "roster_revision": 8,
        "participants": (*context.participants, GUEST)})
    unchanged = revalidate_assignment(confirmed, irrelevant)
    assert unchanged.status == "confirmed" and unchanged.participant_id == "pavel"
    assert unchanged.dependency_signature == confirmed.dependency_signature
    assert unchanged.attribution_revision == 7 and unchanged.roster_revision == 8
    with pytest.raises(AssignmentEvidenceError, match="review_revision_conflict"):
        review_assignment(result, command(result, context), irrelevant)


def test_local_derived_identity_change_revokes_confirmation_named_unaffected():
    action, context = fixture()
    old = review_assignment(resolve_assignment(action, context), command(resolve_assignment(action, context), context), context)
    changed = context.model_copy(update={"attribution_revision": 3,
        "identities": (context.identities[0].model_copy(update={"participant_id": "alex", "revision": 3}),)})
    result = revalidate_assignment(old, changed)
    assert (result.participant_id, result.status) == ("alex", "needs_review")
    assert "derived_evidence_changed" in result.reason_codes
    assert result.anchor == old.anchor and result.action == old.action
    with pytest.raises(AssignmentEvidenceError, match="proposal_evidence_changed"):
        review_assignment(old, command(old, changed), changed)
    stale = changed.model_copy(update={"identities": (changed.identities[0].model_copy(update={"stale": True}),)})
    assert revalidate_assignment(old, stale).participant_id is None
    named, ncontext = fixture("Алексей, сделай", basis="named_person", name="Алексей", semantic="directive")
    named_result = resolve_assignment(named, ncontext)
    named_old = review_assignment(named_result, command(named_result, ncontext), ncontext)
    ncontext = ncontext.model_copy(update={"attribution_revision": 3, "identities": stale.identities})
    assert revalidate_assignment(named_old, ncontext).status == "confirmed"


@pytest.mark.parametrize("change", ["duplicate", "rename", "disable", "alias_removed"])
def test_relevant_roster_change_invalidates_named_confirmation(change):
    alex = ALEX.model_copy(update={"aliases": ("Лёша",)})
    name = "Лёша" if change == "alias_removed" else "Алексей"
    action, context = fixture(f"{name}, сделай", basis="named_person", name=name, roster=(alex,))
    old = resolve_assignment(action, context)
    old = review_assignment(old, command(old, context), context)
    roster = {
        "duplicate": (alex, alex.model_copy(update={"participant_id": "alex2"})),
        "rename": (alex.model_copy(update={"display_name": "Борис"}),),
        "disable": (alex.model_copy(update={"enabled": False}),),
        "alias_removed": (alex.model_copy(update={"aliases": ()}),),
    }[change]
    result = revalidate_assignment(old, context.model_copy(update={"roster_revision": 4, "participants": roster}))
    assert result.status == "needs_review" and not result.confirm_eligible
    assert "derived_evidence_changed" in result.reason_codes


def test_manual_owner_preserved_with_roster_identity_and_source_conflicts():
    action, context = fixture()
    result = resolve_assignment(action, context)
    manual = review_assignment(result, command(result, context, "set_manual", "alex"), context)
    changed = context.model_copy(update={"identities": (), "participants": (PAVEL,), "sources": ()})
    reviewed = revalidate_assignment(manual, changed)
    assert (reviewed.participant_id, reviewed.basis, reviewed.status) == ("alex", "manual", "needs_review")
    assert "manual_participant_missing" in reviewed.reason_codes and "manual_evidence_changed" in reviewed.reason_codes
    assert reviewed.anchor == manual.anchor and reviewed.fingerprint == manual.fingerprint
    assert reviewed.action == manual.action
    disabled = context.model_copy(update={"participants": (ALEX.model_copy(update={"enabled": False}),)})
    assert "manual_participant_disabled" in revalidate_assignment(manual, disabled).reason_codes


def test_changed_source_keeps_historical_proof_and_cannot_confirm():
    action, context = fixture()
    old = resolve_assignment(action, context)
    changed = context.model_copy(update={"sources": (context.sources[0].model_copy(update={"text": "Я не буду делать отчёт"}),)})
    current = revalidate_assignment(old, changed)
    assert current.status == "needs_review" and not current.confirm_eligible
    assert current.anchor == old.anchor and current.action.evidence_quote == old.action.evidence_quote
    assert "source_evidence_changed" in current.reason_codes
    with pytest.raises(AssignmentEvidenceError, match="proposal_not_eligible"):
        review_assignment(current, command(current, changed), changed)


def test_clear_does_not_resurrect_automatic_owner_and_manual_requires_roster_id():
    action, context = fixture()
    old = resolve_assignment(action, context)
    cleared = review_assignment(old, command(old, context, "clear"), context)
    assert revalidate_assignment(cleared, context).participant_id is None
    assert revalidate_assignment(cleared, context).basis == "unknown"
    with pytest.raises(AssignmentEvidenceError, match="participant_missing"):
        review_assignment(old, command(old, context, "set_manual", "profile-id"), context)
    with pytest.raises(AssignmentEvidenceError, match="proposal_participant_mismatch"):
        review_assignment(old, command(old, context, participant="alex"), context)


def test_regeneration_reorder_unique_exact_fingerprint_suggests_with_provenance():
    action, context = fixture()
    old = resolve_assignment(action, context)
    old = review_assignment(old, command(old, context, "set_manual", "alex"), context)
    new_scope = SCOPE.model_copy(update={"summary_version": 2})
    new_context = context.model_copy(update={"scope": new_scope})
    regenerated = action.model_copy(update={"scope": new_scope, "action_id": "different-ordinal"})
    unrelated = regenerated.model_copy(update={"action_id": "action", "text": "Другая задача"})
    current = resolve_assignments((unrelated, regenerated), new_context)
    results = suggest_carryover((old,), current, new_context)
    assert results[0].previous_provenance is None
    carry = results[1]
    assert (carry.participant_id, carry.basis, carry.status) == ("alex", "manual", "proposed")
    assert not carry.confirm_eligible and carry.reason_codes == ("carryover_requires_human_review",)
    assert carry.previous_provenance.summary_version == 1
    assert carry.previous_provenance.action_id == "action" and carry.previous_provenance.assignment_revision == 1
    rechecked = revalidate_assignment(carry, new_context)
    assert rechecked.status != "confirmed" and rechecked.previous_provenance == carry.previous_provenance


@pytest.mark.parametrize("case", ["task", "source", "quote", "transcript", "duplicate_old", "duplicate_new", "identity", "disabled"])
def test_no_carryover_for_changed_ambiguous_or_ineligible_evidence(case):
    action, context = fixture()
    old = resolve_assignment(action, context)
    old = review_assignment(old, command(old, context), context)
    new_scope = SCOPE.model_copy(update={"summary_version": 2})
    new_action = action.model_copy(update={"scope": new_scope})
    new_context = context.model_copy(update={"scope": new_scope})
    previous = (old,)
    if case == "task":
        new_action = new_action.model_copy(update={"text": "Иная задача"})
    elif case == "source":
        new_context = new_context.model_copy(update={"sources": (context.sources[0].model_copy(update={"text": "Я подготовлю отчёт сегодня"}),)})
    elif case == "quote":
        new_action = new_action.model_copy(update={"evidence_quote": "подготовлю отчёт"})
    elif case == "transcript":
        new_scope = new_scope.model_copy(update={"transcript_version": 2})
        new_action = new_action.model_copy(update={"scope": new_scope})
        new_context = new_context.model_copy(update={"scope": new_scope,
            "sources": (context.sources[0].model_copy(update={"transcript_version": 2}),)})
    elif case == "duplicate_old":
        previous = (old, old.model_copy(update={"action": action.model_copy(update={"action_id": "other"})}))
    elif case == "identity":
        new_context = new_context.model_copy(update={"identities": (context.identities[0].model_copy(update={"participant_id": "alex"}),)})
    elif case == "disabled":
        new_context = new_context.model_copy(update={"participants": (PAVEL.model_copy(update={"enabled": False}),)})
    current = (resolve_assignment(new_action, new_context),)
    if case == "duplicate_new":
        current = resolve_assignments((new_action, new_action.model_copy(update={"action_id": "other"})), new_context)
    result = suggest_carryover(previous, current, new_context)
    assert all(r.previous_provenance is None for r in result)
    assert all(r.status != "confirmed" for r in result)


def test_duplicate_content_and_action_id_are_review_and_batch_revalidates_locally():
    action, context = fixture()
    duplicate = action.model_copy(update={"action_id": "other"})
    results = resolve_assignments((action, duplicate), context)
    assert all(r.status == "needs_review" and not r.confirm_eligible for r in results)
    assert all("duplicate_action_evidence" in r.reason_codes for r in results)
    assert all(not r.confirm_eligible for r in revalidate_assignments(results, context))
    collision = action.model_copy(update={"text": "Иной текст"})
    assert all(not r.confirm_eligible for r in resolve_assignments((action, collision), context))
