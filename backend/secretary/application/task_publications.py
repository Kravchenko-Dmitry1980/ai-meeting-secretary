"""Owner-reviewed source publications; storage owns atomic acceptance and delivery.

Source snapshots are read on the caller's Secretary transaction. Team identities
and task projections come from the separate current Team directory and are pinned
again for dispatch. No provider or remote task mutation is reachable here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pydantic import ValidationError

from secretary.application.publication_commands import PublicationCommandError, publication_task_command

from secretary.application.publication_evidence import (
    PublicationEvidenceError, validate_publication_evidence,
)
from secretary.domain.speakers import SpeakerConflict, SpeakerNotFound
from secretary.domain.task_delivery import (
    DeliveryReceipt, GatewayPublication, PreviewPublications, PublicationCandidate, PublicationContext,
    PublicationMember, PublicationPreviewResult, PublicationScope, PublicationSelection,
    PublicationTask, PublicationWatermarks, PublishCommand, PublishPreview,
    SupersedesCandidate, publication_uuid,
)
from secretary.domain.team import TeamConflict, TeamForbidden, uuid_string
from secretary.infrastructure.assignment_repository import AssignmentRepository


MAX_CONFIRM_BYTES = 1024 * 1024  # Secretary's non-upload HTTP body limit.


class TaskPublicationService:
    def __init__(self, db, repository, team_repository, *, actor_id, project_id,
                 project_name='Командные задачи', clock=None):
        self.db = db
        self.repository = repository
        self.team_repository = team_repository
        self.assignments = AssignmentRepository(db)
        self.actor_id = uuid_string(actor_id)
        self.project_id = project_id
        self.project_name = project_name
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('clock_requires_timezone')
        return value.astimezone(timezone.utc)

    def _members(self):
        return self.team_repository.list_members(self.actor_id, self.project_id)

    def _scope(self, meeting_id, scope):
        if scope.meeting_id != meeting_id or scope.destination_project_id != self.project_id:
            raise TeamForbidden('publication_scope_forbidden')

    @staticmethod
    def _watermarks(state):
        return PublicationWatermarks(assignment_revision=state.revision,
            attribution_revision=state.attribution_revision, roster_revision=state.roster_revision,
            context_hash=state.current_context_hash)

    def _source(self, conn, meeting_id, scope=None):
        if scope is None:
            summary = self.assignments._summary(conn, meeting_id)
            scope = PublicationScope(meeting_id=meeting_id, transcript_version=summary['transcript_version'],
                summary_version=summary['summary_version'], destination_project_id=self.project_id)
        self._scope(meeting_id, scope)
        state, context = self.assignments.publication_source(conn, scope)
        summary = self.assignments._summary(conn, meeting_id, scope.summary_version)
        due_phrases = {row['id']: row.get('due_date') for row in summary['action_items']}
        return scope, state, context, due_phrases

    @staticmethod
    def _mapping(proof, members, selected_id=None):
        if proof.person_profile_id is not None:
            matches = tuple(member for member in members if member.person_profile_id == proof.person_profile_id)
            if not matches:
                return None, 'team_member_unmapped'
            if len(matches) != 1:
                return None, 'team_member_ambiguous'
            if selected_id is not None and selected_id != matches[0].id:
                return None, 'team_member_mapping_conflict'
            return matches[0], None
        if selected_id is None:
            return None, 'team_member_selection_required'
        matches = tuple(member for member in members if member.id == selected_id)
        return (matches[0], None) if len(matches) == 1 else (None, 'team_member_unavailable')

    @staticmethod
    def _verified(publication):
        gateway = publication.gateway_receipt
        execution = publication.execution_state
        return bool(execution.state == 'applied' and execution.task_id and gateway
            and gateway.acceptance_receipt.decision == 'accepted'
            and gateway.acceptance_receipt.operation_id == publication.delivery_operation_id
            and gateway.execution_state.state == 'applied'
            and gateway.execution_state.verified_at is not None
            and gateway.execution_state.task_id == execution.task_id
            and gateway.current is not None and gateway.current.task_id == execution.task_id
            and gateway.current.project_id == publication.scope.destination_project_id)

    def _target(self, scope, target_id, history):
        targets = tuple(row for row in history if row.publication_id == target_id
            and row.scope.meeting_id == scope.meeting_id
            and row.scope.destination_project_id == scope.destination_project_id
            and row.scope.summary_version < scope.summary_version)
        task_ids = {row.execution_state.task_id for row in targets if self._verified(row)}
        if not task_ids:
            raise TeamConflict('publication_target_unverified')
        if len(task_ids) != 1:
            raise TeamConflict('publication_target_ambiguous')
        try:
            current = self.team_repository.get_projection(self.actor_id, next(iter(task_ids)))
        except TeamForbidden:
            raise TeamConflict('publication_target_unavailable') from None
        if current.project_id != scope.destination_project_id:
            raise TeamConflict('publication_target_unavailable')
        return current

    def _supersedes(self, action_id, fingerprint, title, scope, history):
        groups = {}
        for row in history:
            if (row.scope.meeting_id == scope.meeting_id
                    and row.scope.destination_project_id == scope.destination_project_id
                    and row.scope.summary_version != scope.summary_version):
                groups.setdefault(row.publication_id, []).append(row)
        candidates = []
        for identity, rows in sorted(groups.items()):
            if not any(row.item.action_id == action_id
                       or (fingerprint is not None and row.item.source_fingerprint == fingerprint)
                       or row.item.title == title for row in rows):
                continue
            task_ids = {row.execution_state.task_id for row in rows if self._verified(row)}
            verified = len(task_ids) == 1
            candidates.append(SupersedesCandidate(publication_id=identity, title=rows[-1].item.title,
                task_id=next(iter(task_ids)) if verified else None, verified=verified))
        return tuple(candidates)

    def _validate_payload(self, scope, item):
        try:
            publication_task_command(GatewayPublication(actor_id=self.actor_id, scope=scope, item=item))
        except PublicationCommandError as exc:
            code = ('publication_payload_too_large' if exc.code in {
                'publication_payload_too_large', 'publication_description_too_large',
                'publication_comment_too_large'} else 'publication_payload_invalid')
            raise TeamConflict(code) from None
        except ValidationError:
            raise TeamConflict('publication_payload_invalid') from None

    @staticmethod
    def _validate_confirmation_size(preview):
        # Include the future client operation UUID and hash, so every offered
        # preview can actually be submitted through the existing HTTP boundary.
        command = PublishCommand.model_validate({**preview.model_dump(),
            'operation_id': '00000000-0000-4000-8000-000000000000'})
        if len(command.model_dump_json().encode('utf-8')) > MAX_CONFIRM_BYTES:
            raise TeamConflict('publication_payload_too_large')

    def _candidate(self, item, context, scope, members, history, due_phrase, selection):
        reasons = []
        proof = None
        try:
            proof = validate_publication_evidence(item, context)
        except PublicationEvidenceError as exc:
            reasons.append(exc.code)
        member = None
        if proof is not None:
            member, reason = self._mapping(proof, members, selection.assignee_id)
            if reason:
                reasons.append(reason)
        if selection.due_resolution == 'unresolved':
            reasons.append('due_resolution_required')
        supersedes = self._supersedes(item.action.action_id, item.fingerprint, item.action.text, scope, history)
        identity = publication_uuid(scope, item.action.action_id, selection.separate_id)
        existing_rows = tuple(row for row in history if row.publication_id == identity)
        existing = next((row for row in existing_rows if self._verified(row)),
                        existing_rows[0] if existing_rows else None)
        if existing is not None and selection.intent != 'propose_update':
            reasons.append('already_published' if self._verified(existing) else 'publication_already_accepted')
        if selection.intent == 'publish' and supersedes:
            reasons.append('supersedes_decision_required')
        target = None
        if selection.intent in {'link', 'propose_update'}:
            try:
                target = self._target(scope, selection.target_publication_id, history)
            except TeamConflict as exc:
                reasons.append(str(exc))
        if len(item.action.text) > 4000 or len(item.action.evidence_quote) > 4000:
            reasons.append('publication_payload_too_large')
        publication = None
        if not reasons:
            try:
                publication = PublicationTask(action_id=item.action.action_id, title=proof.title,
                    assignee_id=member.id, source_segment_ids=proof.source_segment_ids,
                    evidence_quote=proof.evidence_quote, due_at=selection.due_at, due_phrase=due_phrase,
                    due_confirmed=True, publication_id=identity, member_revision=member.revision,
                    participant_id=proof.participant_id, source_fingerprint=proof.source_fingerprint,
                    intent=selection.intent, target_publication_id=selection.target_publication_id,
                    target_task_id=target.task_id if target else None,
                    expected_task_revision=target.revision if target else None,
                    expected_task_fingerprint=target.remote_fingerprint if target else None,
                    target_snapshot=target, separate_id=selection.separate_id)
                self._validate_payload(scope, publication)
            except ValidationError:
                publication = None
                reasons.append('publication_payload_too_large')
            except TeamConflict as exc:
                publication = None
                reasons.append(str(exc))
        candidate = PublicationCandidate(action_id=item.action.action_id, title=item.action.text,
            participant_id=item.participant_id, assignee_id=member.id if member else None,
            assignee_name=member.display_name if member else None, due_phrase=due_phrase,
            evidence_quote=item.action.evidence_quote, source_segment_ids=item.action.source_segment_ids,
            eligible=proof is not None, reason_codes=tuple(dict.fromkeys(reasons)),
            possible_supersedes=supersedes, existing_publication_id=existing.publication_id if existing else None)
        return candidate, publication

    def context(self, meeting_id) -> PublicationContext:
        members = self._members()
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            scope, state, context, due_phrases = self._source(conn, meeting_id)
        history = self.repository.publications(meeting_id, self.project_id)
        candidates = tuple(self._candidate(item, context, scope, members, history,
            due_phrases.get(item.action.action_id), PublicationSelection(action_id=item.action.action_id))[0]
            for item in state.items)
        return PublicationContext(scope=scope, watermarks=self._watermarks(state), project_name=self.project_name,
            members=tuple(PublicationMember(id=member.id, display_name=member.display_name, revision=member.revision)
                          for member in members), candidates=candidates)

    def preview(self, meeting_id, command: PreviewPublications) -> PublicationPreviewResult:
        members = self._members()
        self._scope(meeting_id, command.scope)
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            scope, state, context, due_phrases = self._source(conn, meeting_id, command.scope)
        history = self.repository.publications(meeting_id, self.project_id)
        actions = {item.action.action_id: item for item in state.items}
        if any(selection.action_id not in actions for selection in command.selections):
            raise TeamConflict('publication_action_unavailable')
        rows = tuple(self._candidate(actions[selection.action_id], context, scope, members, history,
            due_phrases.get(selection.action_id), selection) for selection in command.selections)
        frozen = None
        if all(publication is not None for _, publication in rows):
            frozen = PublishPreview(preview_id=str(uuid4()), scope=scope, watermarks=self._watermarks(state),
                items=tuple(publication for _, publication in rows), actor_id=self.actor_id,
                expires_at=self._now() + timedelta(minutes=15))
            try:
                self._validate_confirmation_size(frozen)
            except TeamConflict:
                frozen = None
                rows = tuple((candidate.model_copy(update={'reason_codes':
                    (*candidate.reason_codes, 'publication_payload_too_large')}), None) for candidate, _ in rows)
            else:
                self.repository.save_preview(frozen)
        return PublicationPreviewResult(candidates=tuple(candidate for candidate, _ in rows), preview=frozen)

    def _validate_source(self, conn, scope, watermarks, item, actor_id):
        if actor_id != self.actor_id:
            raise TeamForbidden('publication_actor_forbidden')
        members = self._members()
        self._scope(scope.meeting_id, scope)
        try:
            _, state, context, due_phrases = self._source(conn, scope.meeting_id, scope)
        except (SpeakerConflict, SpeakerNotFound):
            raise TeamConflict('publication_source_changed') from None
        if self._watermarks(state) != watermarks:
            raise TeamConflict('publication_source_changed')
        matches = tuple(row for row in state.items if row.action.action_id == item.action_id)
        if len(matches) != 1:
            raise TeamConflict('publication_action_unavailable')
        try:
            proof = validate_publication_evidence(matches[0], context)
        except PublicationEvidenceError as exc:
            raise TeamConflict(exc.code) from None
        if (proof.title, proof.evidence_quote, proof.source_segment_ids, proof.participant_id,
            proof.source_fingerprint) != (item.title, item.evidence_quote, item.source_segment_ids,
                                         item.participant_id, item.source_fingerprint):
            raise TeamConflict('publication_source_changed')
        member, reason = self._mapping(proof, members, item.assignee_id)
        if reason:
            raise TeamConflict(reason)
        if member.revision != item.member_revision:
            raise TeamConflict('member_mapping_changed')
        if (not item.due_confirmed or item.due_phrase != due_phrases.get(item.action_id)
                or item.publication_id != publication_uuid(scope, item.action_id, item.separate_id)):
            raise TeamConflict('publication_payload_changed')

    def validate_dispatch(self, conn, scope, watermarks, item, actor_id):
        self._validate_source(conn, scope, watermarks, item, actor_id)
        self._validate_payload(scope, item)
        history = self.repository.publications(scope.meeting_id, self.project_id)
        if item.intent == 'publish' and self._supersedes(
                item.action_id, item.source_fingerprint, item.title, scope, history):
            raise TeamConflict('supersedes_decision_required')
        if item.intent in {'link', 'propose_update'}:
            target = self._target(scope, item.target_publication_id, history)
            if target != item.target_snapshot:
                raise TeamConflict('publication_target_changed')

    def confirm(self, meeting_id, command: PublishCommand) -> DeliveryReceipt:
        self._members()
        self._scope(meeting_id, command.scope)
        def validate(conn, preview):
            if preview.actor_id != self.actor_id:
                raise TeamForbidden('publication_actor_forbidden')
            self._scope(meeting_id, preview.scope)
            self._validate_confirmation_size(preview)
            if preview.expires_at is None or self._now() >= preview.expires_at:
                raise TeamConflict('publication_preview_expired')
            for item in preview.items:
                self.validate_dispatch(conn, preview.scope, preview.watermarks, item, self.actor_id)
        # Acceptance replays a known operation before invoking source validation.
        receipt = self.repository.accept(self.actor_id, command, validate)
        return self._decorate(meeting_id, receipt)

    def _decorate(self, meeting_id, receipt):
        self._scope(meeting_id, receipt.scope)
        history = {row.delivery_operation_id: row for row in self.repository.publications(meeting_id, self.project_id)}
        items = []
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            for item in receipt.items:
                record = history.get(item.delivery_operation_id)
                if record is None:
                    raise TeamConflict('publication_state_unavailable')
                stale = False
                try:
                    self._validate_source(conn, record.scope, record.watermarks, record.item, record.actor_id)
                except (TeamConflict, SpeakerConflict, SpeakerNotFound):
                    stale = True
                items.append(item.model_copy(update={'source_stale': stale,
                    'correction_required': stale and item.execution_state.state == 'applied'}))
        return receipt.model_copy(update={'items': tuple(items)})

    def read(self, meeting_id, operation_id) -> DeliveryReceipt:
        self._members()
        receipt = self.repository.read(operation_id, self.actor_id)
        return self._decorate(meeting_id, receipt)

    def list(self, meeting_id) -> tuple[DeliveryReceipt, ...]:
        self._members()
        return tuple(self._decorate(meeting_id, receipt) for receipt in self.repository.list(meeting_id, self.actor_id)
                     if receipt.scope.destination_project_id == self.project_id)
