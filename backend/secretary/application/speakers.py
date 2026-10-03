"""Application boundary for local identity metadata; no STT/cloud enqueue."""
from __future__ import annotations

from typing import Protocol

from secretary.domain.speakers import (
    AddParticipant, AttributionRun, AttributionSnapshot, CreateProfile,
    ParticipantRosterSnapshot, PatchParticipant, PatchProfile, PersonProfile,
    ReviewAttribution, Revision, SpeakerAttribution, TaskAssignment, VoiceEnrollment,
)
from secretary.infrastructure.speaker_repository import SpeakerRepository
from pydantic import TypeAdapter


class EnrollmentEngine(Protocol):
    def enroll(self, person_id: str, files_or_capture_session, consent_confirmed: bool,
               operation_id: str) -> VoiceEnrollment: ...


class IdentityEngine(Protocol):
    def identify(self, run_id: str, segments, immutable_profile_snapshot) -> list[SpeakerAttribution]: ...


class AssignmentResolver(Protocol):
    def resolve_assignments(self, summary, attributed_segments, participant_roster) -> list[TaskAssignment]: ...


class SpeakerService:
    def __init__(self, repository: SpeakerRepository):
        self.repository = repository

    def list_profiles(self) -> list[PersonProfile]:
        return self.repository.list_profiles()

    def create_profile(self, command: CreateProfile) -> PersonProfile:
        return self.repository.create_profile(CreateProfile.model_validate(command))

    def patch_profile(self, profile_id: str, command: PatchProfile) -> PersonProfile:
        return self.repository.patch_profile(profile_id, PatchProfile.model_validate(command))

    def get_roster(self, meeting_id: str) -> ParticipantRosterSnapshot:
        return self.repository.get_roster(meeting_id)

    def add_participant(self, meeting_id: str, command: AddParticipant) -> ParticipantRosterSnapshot:
        return self.repository.add_participant(meeting_id, AddParticipant.model_validate(command))

    def patch_participant(self, meeting_id: str, participant_id: str, command: PatchParticipant) -> ParticipantRosterSnapshot:
        return self.repository.patch_participant(meeting_id, participant_id, PatchParticipant.model_validate(command))

    def get_attribution(self, meeting_id: str, transcript_version: int | None = None) -> AttributionSnapshot:
        if transcript_version is not None:
            transcript_version = TypeAdapter(Revision).validate_python(transcript_version)
        return self.repository.get_attribution(meeting_id, transcript_version)

    def review_attribution(self, meeting_id: str, command: ReviewAttribution) -> AttributionSnapshot:
        return self.repository.review_attribution(meeting_id, ReviewAttribution.model_validate(command))

    def create_attribution_run(self, meeting_id: str, transcript_version: int, expected_revision: int) -> AttributionRun:
        return self.repository.create_attribution_run(meeting_id, TypeAdapter(Revision).validate_python(transcript_version),
                                                      TypeAdapter(Revision).validate_python(expected_revision))

    def list_task_assignments(self, meeting_id: str, summary_version: int, revision: int) -> list[TaskAssignment]:
        return self.repository.list_task_assignments(meeting_id, TypeAdapter(Revision).validate_python(summary_version),
                                                     TypeAdapter(Revision).validate_python(revision))
