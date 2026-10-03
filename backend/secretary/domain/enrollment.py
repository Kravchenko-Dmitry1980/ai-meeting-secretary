"""Public enrollment metadata/commands. Private generations never cross this boundary."""
from typing import Literal
from pydantic import Field, StrictBool
from secretary.domain.speakers import DTO, Identifier, Revision, VoiceEnrollment


class Enrollment(VoiceEnrollment):
    reason_codes: list[str] = Field(default_factory=list)
    listening_confirmed: bool = False
    single_speaker_confirmed: bool = False


class EnrollCommand(DTO):
    consent_confirmed: StrictBool
    operation_id: Identifier


class ConfirmEnrollment(DTO):
    expected_revision: Revision
    listening_confirmed: StrictBool
    single_speaker_confirmed: StrictBool
    operation_id: Identifier


class DeleteEnrollment(DTO):
    expected_revision: Revision
    operation_id: Identifier


class StartEnrollmentRecording(EnrollCommand):
    microphone_id: Identifier


class StopEnrollmentRecording(DTO):
    recording_id: Identifier
    generation: Revision
    disposition: Literal['review', 'cancel']
    operation_id: Identifier


class EnrollmentRecording(DTO):
    recording_id: str
    person_profile_id: str
    enrollment_id: str
    generation: Revision
    status: Literal['starting', 'recording', 'stopping', 'processing', 'awaiting_review',
                    'cancelled', 'failed', 'interrupted', 'cleanup_pending']
    disposition: Literal['review', 'cancel']
    duration_ms: Revision
    reason_codes: list[str]


class EnrollmentFailure(Exception):
    def __init__(self, reason: str, status: int = 409):
        self.reason, self.status = reason, status
        super().__init__(reason)
