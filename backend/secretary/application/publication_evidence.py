"""Pure validation of a reviewed assignment immediately before publication.

The caller supplies the current context inside its source transaction and owns
the revision CAS. This module performs no storage, mapping or delivery work.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from secretary.application.assignment_resolver import (
    _participant, _semantic_contradictions, resolve_assignment, revalidate_assignment,
)
from secretary.domain.assignment_evidence import (
    AssignmentContext, QuoteAnchor, ResolvedAssignment,
)


_ERROR_CODES = frozenset({
    "assignment_not_confirmed", "publication_scope_mismatch",
    "publication_evidence_invalid", "publication_source_changed",
    "publication_participant_invalid", "publication_assignment_changed",
    "publication_semantic_conflict",
})


class PublicationEvidenceError(ValueError):
    """Allowlisted code only; never expose source text or participant details."""

    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else "publication_evidence_invalid"
        super().__init__(self.code)


@dataclass(frozen=True)
class PublicationEvidence:
    participant_id: str
    person_profile_id: str | None
    source_fingerprint: str
    anchor: QuoteAnchor
    source_segment_ids: tuple[str, ...]
    title: str = field(repr=False)
    evidence_quote: str = field(repr=False)


def validate_publication_evidence(
    item: ResolvedAssignment, context: AssignmentContext,
) -> PublicationEvidence:
    """Require reviewed ownership and exact, still-valid stored source proof.

    Manual choice can replace automatic ownership inference, but cannot repair
    a missing anchor, accept changed evidence, or override a contradiction.
    The canonical resolver owns source scoping, uniqueness, hashing and semantic
    rules; no independently computed alternative proof is accepted here.
    """
    if item.status != "confirmed":
        raise PublicationEvidenceError("assignment_not_confirmed")
    if item.action.scope != context.scope:
        raise PublicationEvidenceError("publication_scope_mismatch")

    fresh = resolve_assignment(item.action, context)
    if (fresh.anchor is None or fresh.fingerprint is None or
            item.anchor is None or item.fingerprint is None):
        raise PublicationEvidenceError("publication_evidence_invalid")
    if fresh.anchor != item.anchor or fresh.fingerprint != item.fingerprint:
        raise PublicationEvidenceError("publication_source_changed")

    participant, reason = _participant(item.participant_id, context)
    if item.participant_id is None or participant is None or reason:
        raise PublicationEvidenceError("publication_participant_invalid")

    if item.basis == "manual":
        # The fresh resolver proves unique scoped sources before this lookup.
        # Unknown draft basis also needs this check: its resolver path cannot
        # infer ownership and therefore does not inspect semantic contradictions.
        source = next(s for s in context.sources if s.segment_id == fresh.anchor.segment_id)
        if _semantic_contradictions(item.action, source):
            raise PublicationEvidenceError("publication_semantic_conflict")
    else:
        current = revalidate_assignment(item, context)
        if (item.basis != item.action.basis or current.status != "confirmed" or
                not current.confirm_eligible or current.participant_id != item.participant_id):
            raise PublicationEvidenceError("publication_assignment_changed")

    return PublicationEvidence(
        participant_id=participant.participant_id,
        person_profile_id=participant.person_profile_id,
        source_fingerprint=fresh.fingerprint,
        anchor=fresh.anchor,
        source_segment_ids=item.action.source_segment_ids,
        title=item.action.text,
        evidence_quote=item.action.evidence_quote,
    )
