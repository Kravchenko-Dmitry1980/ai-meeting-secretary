"""Explicit local-only commands and public progress; no material payloads."""
from pydantic import StrictBool
from secretary.domain.speakers import DTO, Identifier, Revision


class IdentifySpeakers(DTO):
    transcript_version: Revision
    expected_revision: Revision
    operation_id: Identifier
    retry: StrictBool = False


class BypassIdentification(DTO):
    transcript_version: Revision
    expected_revision: Revision
    operation_id: Identifier


class IdentificationAccepted(DTO):
    job_id: str
    run_id: str
    intent_key: str


class IdentificationState(DTO):
    job_id: str
    run_id: str
    intent_key: str
    transcript_version: int
    outcome: str
    reason_codes: list[str]
    progress: dict
    calibration_revision: str | None = None
    attribution_revision: int | None = None
    bypass_acknowledged: bool = False
    bypass_attribution_revision: int | None = None

