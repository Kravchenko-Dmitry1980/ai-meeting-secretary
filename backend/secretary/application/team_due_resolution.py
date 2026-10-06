"""Owner deadline previews are remote GETs; confirmation only queues a command."""
from __future__ import annotations

from secretary.application.team_tasks import _image
from secretary.domain.team import TeamConflict
from secretary.domain.team_due_resolution import DueResolutionCandidate, DueResolutionConfirmation, DueResolutionRequest


class TeamDueResolutionService:
    def __init__(self, repository, client, *, preview_ttl_seconds=600):
        if type(preview_ttl_seconds) is not int or not 30 <= preview_ttl_seconds <= 3600:
            raise ValueError('due_resolution_preview_ttl_invalid')
        self.repository, self.client, self.preview_ttl_seconds = repository, client, preview_ttl_seconds

    def _observe(self, baseline, fingerprint):
        if baseline.project_id != self.client.binding.project_id:
            raise TeamConflict('client_project_scope_mismatch')
        self.client.validate_binding()
        observed = self.client.observe_task(baseline.task_id)
        image = _image(observed)
        if observed.project_id != baseline.project_id or observed.remote_fingerprint != fingerprint:
            raise TeamConflict('due_resolution_remote_changed')
        if observed.repeat_after or observed.repeat_mode:
            raise TeamConflict('repeating_task_not_supported')
        bucket = next((state for state, identifier in self.client.binding.bucket_ids.items()
                       if identifier == observed.bucket_id), None)
        if (bucket is None or observed.done != (bucket in ('done', 'cancelled'))
                or (self.client.binding.cancelled_label_id in observed.label_ids) != (bucket == 'cancelled')):
            raise TeamConflict('inconsistent_remote_task_state')
        # Existing task execution expects the baseline assignee. Reject this
        # unsupported simultaneous external edit before any deadline write.
        assignee = self.repository.team.get_member(baseline.assignee_id) if baseline.assignee_id else None
        if baseline.assignee_id and assignee is None:
            raise TeamConflict('due_resolution_assignee_changed')
        expected_assignees = (assignee.vikunja_user_id,) if assignee else ()
        if (observed.assignee_ids != expected_assignees or baseline.assignee_id and
                (not assignee.enabled or baseline.project_id not in assignee.project_ids)):
            raise TeamConflict('due_resolution_assignee_changed')
        if observed.due_at == baseline.due_at:
            raise TeamConflict('due_resolution_not_required')
        return observed, image

    def candidate(self, actor, task_id, *, expected_actor_revision=None):
        _, baseline, observation = self.repository.pending(actor, task_id,
            expected_actor_revision=expected_actor_revision)
        observed, _ = self._observe(baseline, observation['after_fingerprint'])
        # Recheck after remote reads so an old session/ACL is not a read port.
        _, current, current_observation = self.repository.pending(actor, task_id,
            expected_actor_revision=expected_actor_revision, observation_id=observation['id'])
        if current != baseline or current_observation['after_fingerprint'] != observed.remote_fingerprint:
            raise TeamConflict('due_resolution_candidate_changed')
        return DueResolutionCandidate(observation_id=observation['id'], project_id=baseline.project_id,
            task_id=baseline.task_id, baseline_revision=baseline.revision, baseline_fingerprint=baseline.remote_fingerprint,
            observed_fingerprint=observed.remote_fingerprint, baseline_due_at=baseline.due_at,
            observed_due_at=observed.due_at, title=observed.title)

    def preview(self, actor, task_id, request, *, expected_actor_revision=None):
        request = DueResolutionRequest.model_validate(request.model_dump())
        _, baseline, observation = self.repository.pending(actor, task_id,
            expected_actor_revision=expected_actor_revision, observation_id=request.observation_id)
        if request.expected_revision != baseline.revision or request.expected_fingerprint != observation['after_fingerprint']:
            raise TeamConflict('due_resolution_candidate_changed')
        observed, image = self._observe(baseline, request.expected_fingerprint)
        candidate = DueResolutionCandidate(observation_id=request.observation_id, project_id=baseline.project_id,
            task_id=baseline.task_id, baseline_revision=baseline.revision, baseline_fingerprint=baseline.remote_fingerprint,
            observed_fingerprint=observed.remote_fingerprint, baseline_due_at=baseline.due_at,
            observed_due_at=observed.due_at, title=observed.title)
        return self.repository.save_preview(actor, request, candidate, image,
            expected_actor_revision=expected_actor_revision, ttl_seconds=self.preview_ttl_seconds)

    def confirm(self, actor, preview_id, request, *, expected_actor_revision=None):
        request = DueResolutionConfirmation.model_validate(request.model_dump())
        value, replay = self.repository.confirmation_state(actor, preview_id, request.operation_id,
            expected_actor_revision=expected_actor_revision)
        if replay is not None:
            return replay
        candidate = value.preview.candidate
        _, baseline, _ = self.repository.pending(actor, candidate.task_id,
            expected_actor_revision=expected_actor_revision, observation_id=candidate.observation_id)
        observed, image = self._observe(baseline, candidate.observed_fingerprint)
        if image != value.before_image:
            raise TeamConflict('due_resolution_remote_changed')
        return self.repository.accept(actor, value, request.operation_id, observed.remote_fingerprint,
            expected_actor_revision=expected_actor_revision)
