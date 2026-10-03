"""Pure conservative interpretation of supplied evidence, not an NLP classifier.

Semantic flags are untrusted drafts. Lexical checks only reject contradictions;
their absence never proves commitment or acceptance. No I/O, logging or cloud.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter

from secretary.domain.assignment_evidence import (
    AssignmentContext, AssignmentEvidenceError, AssignmentReview,
    CarryoverProvenance, DraftAction, QuoteAnchor, ResolvedAssignment, SourceSegment,
)

FINGERPRINT_VERSION = "assignment-evidence-v1"

# Display labels remain legal for manual/self assignment, never named evidence.
_GENERIC_NAMES = frozenset("""
я меня мне мной мною мы нас нам нами ты тебя тебе тобой тобою вы вас вам вами
он его ему нему им ним нем нём него она ее её ей ею нею ней нее неё оно они их ими ними них
себя себе собой собою мой моя моё мое мои наш наша наше наши твой твоя твое
твоё твои ваш ваша ваше ваши кто кто-то кто-нибудь кто-либо кого кого-то
кого-нибудь кого-либо кому кому-то кому-нибудь кому-либо кем кем-то кем-нибудь
кем-либо некто некому никто никого никому всякий каждый любой все всё
участник участники гость гости человек люди сотрудник сотрудники ответственный
неизвестный i me we us you he him she her they them someone anyone nobody
""".split())


def normalize_name(value: str) -> str:
    """Only Unicode/case/space normalization; no fuzzy or abbreviation inference."""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_hash(source: SourceSegment) -> str:
    return hashlib.sha256(source.text.encode("utf-8")).hexdigest()


def _name_allowed(value: str) -> bool:
    name = normalize_name(value)
    label = name.strip(" .,!?;:«»\"'()[]{}")
    indefinite = re.fullmatch(
        r"(?:кто|кого|кому|кем|ком|какой|какая|какое|какие|какого|какому|каким)"
        r"(?:[-\s](?:то|нибудь|либо))?", label)
    return bool(label) and label not in _GENERIC_NAMES and indefinite is None


def _whole_name(name: str, source: SourceSegment, anchor: QuoteAnchor) -> bool:
    """Check only name occurrences inside the anchor, with ORIGINAL edge chars.

    Another valid occurrence outside this quote cannot authorize a clipped name.
    Normalization is used for matching only; anchor offsets always remain literal.
    """
    # Python \w excludes combining marks. Treat them, join controls and compound
    # name punctuation as continuations so an embedded Unicode name cannot pass.
    def continuation(char: str) -> bool:
        return (char.isalnum() or char in "_-'’" or
                unicodedata.category(char) in {"Mn", "Mc", "Me", "Cf"})

    quote = source.text[anchor.start_char:anchor.end_char]
    normalized_name, normalized_quote = normalize_name(name), normalize_name(quote)
    # normalize_name strips outer whitespace. Such whitespace still constitutes
    # a real boundary inside the anchor, regardless of a word outside the quote.
    before = (quote[0] if quote[0].isspace() else
              source.text[anchor.start_char - 1] if anchor.start_char else "")
    after = (quote[-1] if quote[-1].isspace() else
             source.text[anchor.end_char] if anchor.end_char < len(source.text) else "")
    start = normalized_quote.find(normalized_name)
    while start >= 0:
        end = start + len(normalized_name)
        left = normalized_quote[start - 1] if start else before
        right = normalized_quote[end] if end < len(normalized_quote) else after
        if (not left or not continuation(left)) and (not right or not continuation(right)):
            return True
        start = normalized_quote.find(normalized_name, start + 1)
    return False


def _sources(action: DraftAction, context: AssignmentContext):
    reasons: list[str] = []
    if action.scope != context.scope:
        reasons.append("scope_mismatch")
    if not action.source_segment_ids:
        reasons.append("missing_sources")
    if len(set(action.source_segment_ids)) != len(action.source_segment_ids):
        reasons.append("duplicate_source_reference")
    found: list[SourceSegment] = []
    for segment_id in action.source_segment_ids:
        matches = [s for s in context.sources if s.segment_id == segment_id]
        if len(matches) != 1:
            reasons.append("missing_source" if not matches else "duplicate_source_id")
            continue
        source = matches[0]
        if (source.meeting_id, source.transcript_version) != (
                action.scope.meeting_id, action.scope.transcript_version):
            reasons.append("foreign_source")
        else:
            found.append(source)
    return found, reasons


def _anchor(action: DraftAction, sources: list[SourceSegment]):
    if not action.evidence_quote or not action.evidence_quote.strip():
        return None, "missing_evidence"
    anchors: list[QuoteAnchor] = []
    for source in sources:
        start = source.text.find(action.evidence_quote)
        while start >= 0:
            anchors.append(QuoteAnchor(
                segment_id=source.segment_id, start_char=start,
                end_char=start + len(action.evidence_quote),
                source_text_hash=_source_hash(source)))
            start = source.text.find(action.evidence_quote, start + 1)
    if len(anchors) != 1:
        return None, "quote_not_found" if not anchors else "ambiguous_quote_anchor"
    return anchors[0], None


def _fingerprint(action: DraftAction, sources: list[SourceSegment], anchor: QuoteAnchor):
    # Never summary version, ordinal, legacy action ID, owner or model-generated ID.
    return FINGERPRINT_VERSION + ":" + _hash({
        "version": FINGERPRINT_VERSION,
        "meeting_id": action.scope.meeting_id,
        "transcript_version": action.scope.transcript_version,
        "text": action.text,
        "sources": sorted((s.segment_id, _source_hash(s), s.start_ms, s.end_ms)
                          for s in sources),
        "quote": action.evidence_quote, "anchor": anchor.model_dump(),
    })


def _matching_participants(name: str, context: AssignmentContext):
    normalized = normalize_name(name)
    return [p for p in context.participants
            if p.meeting_id == context.scope.meeting_id and p.enabled
            and any(_name_allowed(label) and normalize_name(label) == normalized
                    for label in (p.display_name, *p.aliases))]


def _participant(participant_id: str | None, context: AssignmentContext):
    matches = [p for p in context.participants if p.participant_id == participant_id]
    if len(matches) != 1:
        return None, "participant_missing" if not matches else "participant_ambiguous"
    participant = matches[0]
    if participant.meeting_id != context.scope.meeting_id:
        return None, "participant_foreign"
    if not participant.enabled:
        return participant, "participant_disabled"
    return participant, None


def _semantic_contradictions(action: DraftAction, source: SourceSegment | None):
    if action.semantic_flag not in {"explicit_commitment", "directive"}:
        return ["semantic_" + action.semantic_flag]
    if action.basis == "self_commitment" and action.semantic_flag != "explicit_commitment":
        return ["self_requires_commitment"]
    # Inspect the original containing segment as well: an extracted positive
    # quote inside reported speech/question must not erase its framing.
    text = normalize_name(source.text if source else action.evidence_quote)
    reasons: list[str] = []
    if action.basis == "self_commitment":
        quote = normalize_name(action.evidence_quote)
        if quote in _GENERIC_NAMES:
            reasons.append("insufficient_commitment_evidence")
        if re.search(r"\bмы\b|\bкто(?:[-\s](?:то|нибудь|либо))?\b", quote):
            reasons.append("collective_or_indefinite_commitment")
    if "?" in text:
        reasons.append("contradiction_question")
    if re.search(r"\b(?:если|может|возможно|предположим)\b", text):
        reasons.append("contradiction_hypothesis")
    if re.search(r"\b(?:сказал|сказала|сказали|говорит|цитата)\b", text):
        reasons.append("contradiction_reported_speech")
    if re.search(
            r"\b(?:не\s+(?:буду|будет|будем|будут|будешь|стану|сделаю|подготовлю|берусь|хочу)|"
            r"отказываюсь|никогда)\b", text):
        reasons.append("contradiction_negation")
    return reasons


def _identity_for(action: DraftAction, context: AssignmentContext):
    matches = [i for i in context.identities if i.segment_id == action.commitment_segment_id]
    if len(matches) != 1:
        return None, "identity_missing" if not matches else "identity_ambiguous"
    identity = matches[0]
    if (identity.meeting_id, identity.transcript_version) != (
            action.scope.meeting_id, action.scope.transcript_version):
        return identity, "identity_foreign"
    if identity.stale or identity.revision > context.attribution_revision:
        return identity, "identity_stale"
    if identity.overlap or identity.ambiguous_author or len(set(identity.possible_author_ids)) > 1:
        return identity, "identity_ambiguous_author"
    if identity.possible_author_ids and identity.possible_author_ids != (identity.participant_id,):
        return identity, "identity_ambiguous_author"
    if identity.status != "confirmed":
        return identity, "identity_" + identity.status
    if identity.participant_id is None:
        return identity, "identity_unmapped"
    return identity, None


def _dependency_signature(action: DraftAction, context: AssignmentContext,
                          sources: list[SourceSegment], anchor: QuoteAnchor | None,
                          candidate: str | None, reasons: list[str]):
    # A revision bump alone is not a semantic change. Named assignments depend
    # on matching eligibility, self assignments on their exact source identity.
    identity = None
    if action.basis == "self_commitment":
        identity = [i.model_dump(exclude={"revision", "run_id", "method"})
                    for i in context.identities if i.segment_id == action.commitment_segment_id]
    return _hash({
        "scope": action.scope.model_dump(), "action": action.model_dump(),
        "sources": sorted((s.segment_id, _source_hash(s), s.start_ms, s.end_ms) for s in sources),
        "anchor": anchor.model_dump() if anchor else None,
        "identity": identity, "candidate": candidate, "reasons": sorted(set(reasons)),
    })


def resolve_assignment(action: DraftAction, context: AssignmentContext) -> ResolvedAssignment:
    """Resolve a model draft into proposed/review only; never confirm acceptance."""
    sources, reasons = _sources(action, context)
    anchor, anchor_reason = _anchor(action, sources)
    if anchor_reason:
        reasons.append(anchor_reason)
    fingerprint = _fingerprint(action, sources, anchor) if anchor and not reasons else None
    participant_id = None
    source = next((s for s in sources if anchor and s.segment_id == anchor.segment_id), None)
    if action.basis == "named_person":
        if not action.named_owner_text:
            reasons.append("missing_named_owner")
        elif not _name_allowed(action.named_owner_text):
            reasons.append("generic_named_owner")
        elif not anchor or not source or not _whole_name(action.named_owner_text, source, anchor):
            reasons.append("named_owner_not_in_quote")
        else:
            matches = _matching_participants(action.named_owner_text, context)
            if len(matches) == 1:
                participant_id = matches[0].participant_id
                _, participant_reason = _participant(participant_id, context)
                if participant_reason:
                    reasons.append(participant_reason)
            else:
                reasons.append("named_owner_missing" if not matches else "named_owner_ambiguous")
        reasons.extend(_semantic_contradictions(action, source))
    elif action.basis == "self_commitment":
        if not action.commitment_segment_id:
            reasons.append("missing_commitment_source")
        elif action.commitment_segment_id not in action.source_segment_ids:
            reasons.append("commitment_source_not_cited")
        elif not anchor or anchor.segment_id != action.commitment_segment_id:
            reasons.append("commitment_anchor_mismatch")
        else:
            identity, identity_reason = _identity_for(action, context)
            if identity_reason:
                reasons.append(identity_reason)
            elif identity:
                _, participant_reason = _participant(identity.participant_id, context)
                if participant_reason:
                    reasons.append(participant_reason)
                else:
                    participant_id = identity.participant_id
        reasons.extend(_semantic_contradictions(action, source))
    else:
        reasons.append("unknown_basis")
    eligible = participant_id is not None and not reasons
    # Invalid source scopes cannot provide even a displayable automatic candidate.
    if any(r in reasons for r in ("scope_mismatch", "foreign_source", "missing_source",
                                 "duplicate_source_reference", "duplicate_source_id")):
        participant_id = None
    codes = tuple(dict.fromkeys(reasons)) or ("draft_evidence_valid",)
    return ResolvedAssignment(
        action=action, participant_id=participant_id, basis=action.basis,
        status="proposed" if eligible else "needs_review", reason_codes=codes,
        confirm_eligible=eligible, anchor=anchor, fingerprint=fingerprint,
        dependency_signature=_dependency_signature(action, context, sources, anchor, participant_id, reasons),
        attribution_revision=context.attribution_revision, roster_revision=context.roster_revision,
    )


def resolve_assignments(actions: tuple[DraftAction, ...], context: AssignmentContext
                        ) -> tuple[ResolvedAssignment, ...]:
    """Mark duplicate content/action identifiers as ambiguous within one summary."""
    results = tuple(resolve_assignment(a, context) for a in actions)
    fingerprints = Counter(r.fingerprint for r in results if r.fingerprint)
    ids = Counter(a.action_id for a in actions)
    output = []
    for result in results:
        duplicate = (ids[result.action.action_id] > 1 or
                     result.fingerprint is not None and fingerprints[result.fingerprint] > 1)
        if duplicate:
            result = result.model_copy(update={"status": "needs_review", "confirm_eligible": False,
                                               "reason_codes": (*result.reason_codes, "duplicate_action_evidence")})
        output.append(result)
    return tuple(output)


def review_assignment(previous: ResolvedAssignment, review: AssignmentReview,
                      context: AssignmentContext) -> ResolvedAssignment:
    """Explicit human confirmation/manual choice. Application handles atomic CAS."""
    if review.scope != context.scope or review.scope != previous.action.scope or review.action_id != previous.action.action_id:
        raise AssignmentEvidenceError("review_scope_mismatch")
    if (review.expected_attribution_revision, review.expected_roster_revision) != (
            context.attribution_revision, context.roster_revision):
        raise AssignmentEvidenceError("review_revision_conflict")
    current = revalidate_assignment(previous, context)
    if review.decision == "clear":
        if review.participant_id is not None:
            raise AssignmentEvidenceError("clear_requires_null_participant")
        return current.model_copy(update={"basis": "unknown", "participant_id": None,
            "status": "needs_review", "confirm_eligible": False,
            "reason_codes": ("human_cleared",), "revision": previous.revision + 1})
    if review.decision == "set_manual":
        _, reason = _participant(review.participant_id, context)
        if review.participant_id is None or reason:
            raise AssignmentEvidenceError(reason or "manual_participant_required")
        return current.model_copy(update={"basis": "manual", "participant_id": review.participant_id,
            "status": "confirmed", "confirm_eligible": False,
            "reason_codes": ("human_manual_choice",), "revision": previous.revision + 1})
    if current.basis in {"manual", "unknown"} or not current.confirm_eligible:
        raise AssignmentEvidenceError("proposal_not_eligible")
    if review.participant_id is not None and review.participant_id != current.participant_id:
        raise AssignmentEvidenceError("proposal_participant_mismatch")
    if previous.dependency_signature != current.dependency_signature:
        raise AssignmentEvidenceError("proposal_evidence_changed")
    return current.model_copy(update={"status": "confirmed", "reason_codes": ("human_confirmed_assignment",),
                                      "revision": previous.revision + 1})


def revalidate_assignment(previous: ResolvedAssignment, context: AssignmentContext
                          ) -> ResolvedAssignment:
    """Local overlay only. Preserve explicit manual owner, expose conflicts."""
    current = resolve_assignment(previous.action, context)
    common = {"revision": previous.revision, "previous_provenance": previous.previous_provenance}
    if previous.basis == "manual":
        _, participant_reason = _participant(previous.participant_id, context)
        reasons = []
        if participant_reason:
            reasons.append("manual_" + participant_reason)
        # A changed source/quote is a conflict, not permission to replace a person.
        if previous.fingerprint != current.fingerprint or previous.action.scope != context.scope:
            reasons.append("manual_evidence_changed")
        return current.model_copy(update={**common, "participant_id": previous.participant_id,
            "basis": "manual", "status": "needs_review" if reasons else previous.status,
            "reason_codes": tuple(reasons) or ("human_manual_choice",), "confirm_eligible": False,
            "anchor": previous.anchor, "fingerprint": previous.fingerprint})
    if "human_cleared" in previous.reason_codes:
        return current.model_copy(update={**common, "participant_id": None, "basis": "unknown",
            "status": "needs_review", "reason_codes": ("human_cleared",), "confirm_eligible": False})
    if "duplicate_action_evidence" in previous.reason_codes:
        return current.model_copy(update={**common, "status": "needs_review", "confirm_eligible": False,
            "reason_codes": previous.reason_codes, "anchor": previous.anchor, "fingerprint": previous.fingerprint})
    if previous.fingerprint != current.fingerprint:
        # Preserve historical proof rather than silently rewriting it when an
        # allegedly immutable transcript source changed or disappeared.
        return current.model_copy(update={**common, "status": "needs_review", "confirm_eligible": False,
            "anchor": previous.anchor, "fingerprint": previous.fingerprint,
            "reason_codes": (*current.reason_codes, "source_evidence_changed")})
    if previous.dependency_signature != current.dependency_signature:
        return current.model_copy(update={**common, "status": "needs_review",
            "reason_codes": (*current.reason_codes, "derived_evidence_changed")})
    if previous.status == "confirmed" and current.confirm_eligible:
        return current.model_copy(update={**common, "status": "confirmed",
                                          "reason_codes": ("human_confirmed_assignment",)})
    return current.model_copy(update=common)


def revalidate_assignments(previous: tuple[ResolvedAssignment, ...], context: AssignmentContext
                           ) -> tuple[ResolvedAssignment, ...]:
    return tuple(revalidate_assignment(item, context) for item in previous)


def suggest_carryover(previous: tuple[ResolvedAssignment, ...],
                      current: tuple[ResolvedAssignment, ...], context: AssignmentContext
                      ) -> tuple[ResolvedAssignment, ...]:
    """Exact unique unchanged evidence gives a suggestion, never auto-confirmation.

    Task5b must load previous persisted provenance and pass current resolver results,
    not historical raw owner strings. No matching by action ID or ordinal.
    """
    old_counts = Counter(r.fingerprint for r in previous if r.fingerprint)
    new_counts = Counter(r.fingerprint for r in current if r.fingerprint)
    output = []
    for result in current:
        fingerprint = result.fingerprint
        if (result.action.scope != context.scope or fingerprint is None or
                old_counts[fingerprint] != 1 or new_counts[fingerprint] != 1):
            output.append(result)
            continue
        old = next(r for r in previous if r.fingerprint == fingerprint)
        # Verify old anchor/source text against this context, not merely an ID.
        old_action = old.action.model_copy(update={"scope": context.scope})
        verified = resolve_assignment(old_action, context)
        _, participant_reason = _participant(old.participant_id, context)
        if (verified.fingerprint != fingerprint or old.participant_id is None or participant_reason or
                old.status != "confirmed" or "duplicate_action_evidence" in old.reason_codes):
            output.append(result)
            continue
        if old.basis != "manual" and (not verified.confirm_eligible or verified.participant_id != old.participant_id):
            output.append(result)
            continue
        output.append(result.model_copy(update={
            "participant_id": old.participant_id, "basis": old.basis,
            "status": "proposed", "confirm_eligible": False,
            "reason_codes": ("carryover_requires_human_review",),
            "previous_provenance": CarryoverProvenance(
                summary_version=old.action.scope.summary_version, action_id=old.action.action_id,
                assignment_revision=old.revision, basis=old.basis, status=old.status),
        }))
    return tuple(output)
