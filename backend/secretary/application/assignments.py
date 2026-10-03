"""Pure adapters to the approved assignment evidence resolver.

Atomic DB publication, CAS and operation replay belong to the repository layer;
these helpers perform no I/O, no seeding on GET and no paid/local enqueue.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping

from secretary.application.assignment_resolver import (
    resolve_assignments, review_assignment, revalidate_assignments, suggest_carryover,
)
from secretary.domain.assignment_evidence import (
    AssignmentContext, AssignmentEvidenceError, AssignmentReview, DraftAction, ResolvedAssignment,
)
from secretary.domain.summary_context import (
    AssignmentProposal, AssignmentSnapshot, ReviewTaskAssignments, content_hash,
)


def draft_actions(summary: Mapping, context: AssignmentContext) -> tuple[DraftAction, ...]:
    if (summary["meeting_id"], summary["transcript_version"], summary["summary_version"]) != (
            context.scope.meeting_id, context.scope.transcript_version, context.scope.summary_version):
        raise AssignmentEvidenceError("summary_scope_mismatch")
    result = []
    for item in summary["action_items"]:
        proposal = AssignmentProposal.model_validate(item.get("assignment_proposal") or {})
        result.append(DraftAction(
            scope=context.scope, action_id=item["id"], text=item["text"],
            evidence_quote=item.get("evidence_quote", ""), source_segment_ids=tuple(item.get("source_segment_ids",())),
            **proposal.model_dump(),
        ))
    return tuple(result)


def context_hash(context: AssignmentContext) -> str:
    """Full current proof watermark without mutable timestamps or settings."""
    value = context.model_dump(mode="json")
    value["sources"].sort(key=lambda row: row["segment_id"])
    value["identities"].sort(key=lambda row: row["segment_id"])
    value["participants"].sort(key=lambda row: row["participant_id"])
    for row in value["participants"]:
        row["aliases"] = sorted(set(row["aliases"]))
    return content_hash(value)


def snapshot(
    context: AssignmentContext, items: tuple[ResolvedAssignment, ...], *, revision: int,
    captured_context_hash: str | None,
) -> AssignmentSnapshot:
    # The repository publishes one whole-snapshot revision; pure row review may
    # increment only changed rows. Normalize validated output at publication.
    normalized = tuple(ResolvedAssignment.model_validate({**item.model_dump(), "revision": revision})
                       for item in items)
    return AssignmentSnapshot(
        meeting_id=context.scope.meeting_id, transcript_version=context.scope.transcript_version,
        summary_version=context.scope.summary_version, revision=revision,
        attribution_revision=context.attribution_revision, roster_revision=context.roster_revision,
        captured_context_hash=captured_context_hash, current_context_hash=context_hash(context), items=normalized,
    )


def resolve_summary(
    summary: Mapping, captured: AssignmentContext, current: AssignmentContext,
    *, captured_context_hash: str | None, previous: AssignmentSnapshot | None = None,
) -> AssignmentSnapshot:
    if captured.scope != current.scope:
        raise AssignmentEvidenceError("summary_scope_mismatch")
    resolved = resolve_assignments(draft_actions(summary, captured), captured)
    resolved = revalidate_assignments(resolved, current)
    if previous is not None:
        resolved = suggest_carryover(previous.items, resolved, current)
    return snapshot(current, resolved, revision=0, captured_context_hash=captured_context_hash)


def locally_revalidate(previous: AssignmentSnapshot, current: AssignmentContext) -> AssignmentSnapshot:
    if (previous.meeting_id, previous.transcript_version, previous.summary_version) != (
            current.scope.meeting_id, current.scope.transcript_version, current.scope.summary_version):
        raise AssignmentEvidenceError("summary_scope_mismatch")
    if previous.current_context_hash == context_hash(current):
        return previous  # Same evaluated input cannot create a new durable revision.
    updated = snapshot(current, revalidate_assignments(previous.items, current),
                       revision=previous.revision, captured_context_hash=previous.captured_context_hash)
    if updated == previous:
        return previous
    return snapshot(current, updated.items, revision=previous.revision + 1,
                    captured_context_hash=previous.captured_context_hash)


def review_batch(
    previous: AssignmentSnapshot, command: ReviewTaskAssignments, current: AssignmentContext,
) -> AssignmentSnapshot:
    """Repository replays first, verifies current summary and CAS before this call."""
    expected = (command.transcript_version, command.summary_version, command.expected_revision,
                command.attribution_revision, command.roster_revision)
    actual = (current.scope.transcript_version, current.scope.summary_version, previous.revision,
              current.attribution_revision, current.roster_revision)
    if expected != actual or (previous.meeting_id, previous.transcript_version, previous.summary_version) != (
            current.scope.meeting_id, current.scope.transcript_version, current.scope.summary_version):
        raise AssignmentEvidenceError("review_revision_conflict")
    changes = {change.action_id: change for change in command.changes}
    if changes.keys() - {item.action.action_id for item in previous.items}:
        raise AssignmentEvidenceError("review_action_not_found")
    revalidated = {item.action.action_id: item for item in revalidate_assignments(previous.items, current)}
    result = []
    for item in previous.items:
        change = changes.get(item.action.action_id)
        if change is None:
            result.append(revalidated[item.action.action_id])
            continue
        result.append(review_assignment(item, AssignmentReview(
            scope=current.scope, action_id=change.action_id, decision=change.decision,
            participant_id=change.participant_id,
            expected_attribution_revision=command.attribution_revision,
            expected_roster_revision=command.roster_revision,
        ), current))
    return snapshot(current, tuple(result), revision=previous.revision + 1,
                    captured_context_hash=previous.captured_context_hash)


def project_summary(summary: Mapping, assignments: AssignmentSnapshot,
                    current: AssignmentContext) -> dict:
    """Compatible owner is an explicitly status-bearing current assignment view."""
    if (summary["meeting_id"], summary["transcript_version"], summary["summary_version"]) != (
            assignments.meeting_id, assignments.transcript_version, assignments.summary_version):
        raise AssignmentEvidenceError("summary_scope_mismatch")
    if (assignments.meeting_id, assignments.transcript_version, assignments.summary_version) != (
            current.scope.meeting_id, current.scope.transcript_version, current.scope.summary_version):
        raise AssignmentEvidenceError("summary_scope_mismatch")
    if assignments.current_context_hash != context_hash(current):
        raise AssignmentEvidenceError("assignment_context_changed")
    people = {participant.participant_id: participant for participant in current.participants}
    items = {item.action.action_id: item for item in assignments.items}
    result = copy.deepcopy(dict(summary))
    for action in result["action_items"]:
        item = items.get(action["id"])
        if item is None:
            # Never let historical raw owner look like a new verified decision.
            action.update(owner=None, assignment_status="needs_review", assignment_basis="unknown",
                          participant_id=None, assignment_reason_codes=["assignment_evidence_unavailable"])
            continue
        person = people.get(item.participant_id)
        action.update(
            owner=person.display_name if person is not None else None,
            participant_id=item.participant_id, assignment_status=item.status,
            assignment_basis=item.basis, assignment_revision=assignments.revision,
            assignment_reason_codes=list(item.reason_codes), evidence_quote=item.action.evidence_quote,
            assignment_anchor=item.anchor.model_dump() if item.anchor is not None else None,
            assignment_provenance=item.previous_provenance.model_dump() if item.previous_provenance is not None else None,
        )
    return result
