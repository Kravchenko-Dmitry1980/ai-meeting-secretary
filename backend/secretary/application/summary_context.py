"""Pure frozen-input adapters, without DB/provider effects."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from secretary.domain.assignment_evidence import (
    AssignmentContext, AssignmentScope, IdentityEvidence, RosterParticipant, SourceSegment,
)
from secretary.domain.summary_context import (
    FrozenSummaryContext, FrozenSummaryParticipant, FrozenSummarySettings,
    FrozenSummarySource, SUMMARY_CONTEXT_CONTRACT, content_hash,
)


def observation_id(meeting_id: str, transcript_version: int, segment_id: str) -> str:
    """Version-scoped observation, even when a legacy raw speaker ID is reused."""
    return content_hash(["summary-observation-v1", meeting_id, transcript_version, segment_id])


def freeze_summary_context(
    *, meeting_id: str, transcript_version: int, settings: Mapping,
    segments: Sequence[Mapping], attribution: Mapping,
) -> FrozenSummaryContext:
    """Caller supplies all rows from one consistent transaction.

    The attribution snapshot includes the explicit current meeting roster. This
    does not read identities on map/merge/resume and never normalizes raw speech.
    Input segment ordering is the canonical transcript query ordering.
    """
    if (attribution.get("meeting_id"), attribution.get("transcript_version")) != (
            meeting_id, transcript_version):
        raise ValueError("summary_context_scope_mismatch")
    revision = attribution["revision"]
    roster_revision = attribution["roster_revision"]
    participants = tuple(sorted((FrozenSummaryParticipant(
        participant_id=p["id"], display_name=p["display_name"],
        aliases=tuple(sorted(set(p.get("aliases", ())))), enabled=p["enabled"],
        person_profile_id=p.get("person_profile_id"),
    ) for p in attribution["participants"]), key=lambda p: p.participant_id))
    items = attribution["items"]
    identities = {item["segment_id"]: item for item in items}
    if len(identities) != len(items):
        raise ValueError("summary_context_duplicate_observation")
    source_ids = {segment["id"] for segment in segments}
    if set(identities) - source_ids:
        raise ValueError("summary_context_foreign_observation")
    sources = []
    # Manual identity review cannot erase an overlap observed in immutable
    # source intervals. Keep this independent of mutable attribution reasons.
    overlap_ids = set()
    channels = {}
    for segment in segments:
        if segment.get('timing_precision') in {'word','segment'} and segment.get('start_ms') is not None and segment.get('end_ms') is not None:
            channels.setdefault(segment['channel'],[]).append(segment)
    for channel_segments in channels.values():
        furthest = None
        for segment in sorted(channel_segments,key=lambda row:(row['start_ms'],row['end_ms'],row['id'])):
            if furthest is not None and furthest['end_ms'] > segment['start_ms']:
                overlap_ids.update((segment['id'],furthest['id']))
            if furthest is None or segment['end_ms'] > furthest['end_ms']:
                furthest = segment
    for segment in segments:
        if (segment.get("meeting_id"), segment.get("transcript_version")) != (
                meeting_id, transcript_version):
            raise ValueError("summary_context_source_scope_mismatch")
        identity = identities.get(segment["id"], {})
        reasons = set(identity.get("reason_codes", ()))
        stale = bool(identity.get("stale", False) or (
            attribution.get("automatic_overlay_stale", False)
            and identity.get("method") == "voice_embedding"))
        sources.append(FrozenSummarySource(
            id=segment["id"], text=segment["text"],
            text_hash=hashlib.sha256(segment["text"].encode("utf-8")).hexdigest(),
            start_ms=segment.get("start_ms"), end_ms=segment.get("end_ms"),
            timing_precision=segment.get("timing_precision", "unknown"), channel=segment["channel"],
            speaker_observation_id=observation_id(meeting_id, transcript_version, segment["id"]),
            participant_id=identity.get("participant_id"), identity_status=identity.get("status", "unknown"),
            identity_method=identity.get("method", "unknown"), identity_revision=identity.get("revision", revision),
            identity_run_id=identity.get("run_id"), stale=stale,
            overlap=bool(identity.get("overlap", False) or "overlap" in reasons or segment['id'] in overlap_ids),
            ambiguous_author=bool(identity.get("ambiguous_author", False) or
                                  reasons & {"group_conflict", "ambiguous_participant_mapping"} or segment.get('timing_precision')=='chunk'),
            possible_author_ids=tuple(sorted(set(identity.get("possible_author_ids", ())))),
        ))
    route = FrozenSummarySettings.model_validate({
        key: settings[key] for key in FrozenSummarySettings.model_fields if key in settings
    })
    body = {
        "contract": SUMMARY_CONTEXT_CONTRACT, "meeting_id": meeting_id,
        "transcript_version": transcript_version, "attribution_revision": revision,
        "attribution_run_id": attribution.get("run_id") or next(
            (item.get("run_id") for item in items if item.get("run_id")), None),
        "roster_revision": roster_revision, "settings": route.model_dump(mode="json"),
        "participants": [p.model_dump(mode="json") for p in participants],
        "sources": [s.model_dump(mode="json") for s in sources],
    }
    return FrozenSummaryContext.model_validate({**body, "context_hash": content_hash(body)})


def assignment_context(frozen: FrozenSummaryContext, summary_version: int) -> AssignmentContext:
    """Use for captured proof; a fresh transaction supplies local current proof."""
    return AssignmentContext(
        scope=AssignmentScope(meeting_id=frozen.meeting_id, transcript_version=frozen.transcript_version,
                              summary_version=summary_version),
        attribution_revision=frozen.attribution_revision, roster_revision=frozen.roster_revision,
        sources=tuple(SourceSegment(
            meeting_id=frozen.meeting_id, transcript_version=frozen.transcript_version,
            segment_id=s.id, text=s.text, start_ms=s.start_ms, end_ms=s.end_ms,
        ) for s in frozen.sources),
        participants=tuple(RosterParticipant(
            meeting_id=frozen.meeting_id, participant_id=p.participant_id, display_name=p.display_name,
            aliases=p.aliases, enabled=p.enabled, person_profile_id=p.person_profile_id,
        ) for p in frozen.participants),
        identities=tuple(IdentityEvidence(
            meeting_id=frozen.meeting_id, transcript_version=frozen.transcript_version,
            segment_id=s.id, participant_id=s.participant_id, status=s.identity_status,
            revision=s.identity_revision, method=s.identity_method, run_id=s.identity_run_id,
            stale=s.stale, overlap=s.overlap, ambiguous_author=s.ambiguous_author,
            possible_author_ids=s.possible_author_ids,
        ) for s in frozen.sources),
    )


def provider_context(frozen: FrozenSummaryContext, source_ids: set[str] | None = None) -> dict:
    """Complete roster and relevant immutable source metadata for each v2 call.

    Map uses its original source IDs; merge/repair uses all cited source IDs.
    Scope/hash describes the full frozen record, never a recaptured live subset.
    This avoids repeating a meeting-sized manifest in every bounded block.
    """
    if source_ids is not None and source_ids - {s.id for s in frozen.sources}:
        raise ValueError("summary_context_foreign_source")
    return {
        "contract": frozen.contract, "context_hash": frozen.context_hash,
        "meeting_id": frozen.meeting_id, "transcript_version": frozen.transcript_version,
        "attribution_revision": frozen.attribution_revision,
        "attribution_run_id": frozen.attribution_run_id, "roster_revision": frozen.roster_revision,
        "participants": [p.model_dump(mode="json") for p in frozen.participants],
        "source_identities": [s.model_dump(mode="json", exclude={"text"}) for s in frozen.sources
                              if source_ids is None or s.id in source_ids],
    }


def provider_segments(frozen: FrozenSummaryContext) -> list[dict]:
    """All split pieces must retain this metadata and their original source ID."""
    return [source.model_dump(mode="json") for source in frozen.sources]


def summary_contract(payload: Mapping, *, has_paid_state: bool) -> str:
    """No context upgrade of an old paid job, regardless of retry or current names.

    Infrastructure must prove reconstructable v1 settings/original input and
    reject uncertain/missing receipt state before permitting any new POST.
    Calling this function does not constitute that proof or authorize a POST.
    """
    if "summary_context" in payload:
        frozen = FrozenSummaryContext.model_validate(payload["summary_context"])
        return frozen.contract
    if payload.get("summary_contract") == "legacy-v1" or has_paid_state:
        return "legacy-v1"
    return SUMMARY_CONTEXT_CONTRACT
