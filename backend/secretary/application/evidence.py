from __future__ import annotations

from secretary.domain.models import Summary
from secretary.infrastructure.database import stable_id


def validate_summary(result: dict, meeting_id: str, version: int, segments: list[dict]) -> dict:
    """Reject hallucinated evidence and conservatively retain named owners/dates only."""
    sources = {segment["id"]: segment["text"] for segment in segments}
    clean = {
        "meeting_id": meeting_id, "transcript_version": version, "summary_version": 0,
        "overview": result.get("overview", ""), "readable_transcript": result.get("readable_transcript", ""),
        "decisions": result.get("decisions", []), "action_items": result.get("action_items", []),
        "open_questions": result.get("open_questions", []), "status": "succeeded",
        "excluded_items_count": result.get("excluded_items_count", 0),
    }
    for field in ("decisions", "action_items", "open_questions"):
        for ordinal, item in enumerate(clean[field]):
            refs = item.get("source_segment_ids", [])
            if not refs or any(ref not in sources for ref in refs):
                raise ValueError(f"Invalid source references in {field}")
            if not item.get("text", "").strip():
                raise ValueError(f"Empty evidence item in {field}")
            quote = item.get('evidence_quote','')
            if not isinstance(quote,str) or (quote and not any(quote in sources[ref] for ref in refs)):
                raise ValueError('invalid_evidence_quote')
            if field == 'action_items' and item.get('assignment_proposal') is not None and not quote:
                raise ValueError('assignment_quote_required')
            if field == "action_items":
                if any(key in item for key in ('participant_id','assignment_status','assignment_basis','assignment_revision')):
                    raise ValueError('provider_assignment_authority_forbidden')
                item["id"] = stable_id(meeting_id, version, "action", ordinal, item["text"])
                evidence = " ".join(sources[ref] for ref in refs).casefold()
                for named in ("owner", "due_date"):
                    value = item.get(named)
                    # A normalized date not stated literally is deliberately unknown.
                    if value and str(value).strip().casefold() not in evidence:
                        item[named] = None
                    else:
                        item[named] = value or None
    return Summary.model_validate(clean).model_dump()

