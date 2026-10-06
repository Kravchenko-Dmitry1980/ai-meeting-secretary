"""Serialized task commands with durable intent and read-only crash recovery.

Vikunja 2.7 does not enforce write CAS. The Team repository fences local
senders; fresh reads detect outside edits without inventing their author.
Native concurrent writers remain outside the supported workflow.
"""
from __future__ import annotations

from copy import deepcopy

from secretary.domain.team import TeamConflict, TeamForbidden, content_hash
from secretary.infrastructure.vikunja import (
    TaskReadContext, VikunjaError, VikunjaMutationUncertain, ZERO_DATE,
    has_visible_marker, visible_text,
)


def _image(item):
    """Exactly the pinned fingerprint input, stored before sending a step."""
    value = {'schema': 1, 'task_id': item.task_id, 'project_id': item.project_id,
        'title': item.title, 'description': item.description, 'done': item.done,
        'due_at': item.due_at.isoformat() if item.due_at else None,
        'assignee_ids': list(item.assignee_ids), 'label_ids': list(item.label_ids),
        'buckets': [list(pair) for pair in item.buckets],
        'repeat_after': item.repeat_after, 'repeat_mode': item.repeat_mode,
        'comments': [{'id': x.id, 'comment': x.comment, 'author_id': x.author_id} for x in item.comments]}
    if content_hash(value) != item.remote_fingerprint:
        raise VikunjaError('observation_fingerprint_mismatch')
    return value


def _date(value):
    return value.isoformat() if value else None


def _literal(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


class TeamTaskService:
    def __init__(self, repository, client, *, lease_seconds=120):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('invalid_lease_seconds')
        self.repository, self.client, self.lease_seconds = repository, client, lease_seconds

    @staticmethod
    def _origin(command):
        return 'secretary-origin:' + (command.origin.publication_id
            if command.origin and command.origin.publication_id else command.operation_id)

    def _member_bindings(self, command, baseline, observed=None):
        identifiers = {value for value in (baseline.assignee_id if baseline else None, command.values.assignee_id) if value}
        result = []
        for identifier in sorted(identifiers):
            current = self.repository.get_member(identifier)
            mapped = self.client.members.get_by_id(identifier)
            if (not current or not mapped or current.vikunja_user_id != mapped.vikunja_user_id
                    or current.revision != mapped.revision):
                raise TeamConflict('member_mapping_changed')
            if (identifier == command.values.assignee_id and command.expected_assignee_revision is not None
                    and current.revision != command.expected_assignee_revision):
                raise TeamConflict('member_mapping_changed')
            if baseline and identifier == baseline.assignee_id and observed is not None and observed.assignee_ids != (current.vikunja_user_id,):
                raise TeamConflict('member_mapping_changed')
            result.append({'id': identifier, 'revision': current.revision, 'vikunja_user_id': current.vikunja_user_id})
        return result

    def _link_actor_binding(self, claim):
        """Authorize read-only publication and bind that permission to final commit."""
        actor = self.repository.get_member(claim.actor_id)
        if actor is None:
            raise TeamForbidden('actor_unknown')
        if not actor.enabled:
            raise TeamForbidden('actor_disabled')
        if claim.command.project_id not in actor.project_ids:
            raise TeamForbidden('project_forbidden')
        if actor.role != 'owner':
            raise TeamForbidden('owner_required')
        return {'id': actor.id, 'revision': actor.revision, 'vikunja_user_id': actor.vikunja_user_id}

    def _plan(self, command, observed, actor_id):
        values, binding, steps = command.values, self.client.binding, []
        create = command.action == 'create'
        if create:
            steps.append({'kind': 'create', 'marker': self._origin(command)})
            # A new task starts in the configured inbox, with no relations.
            assignees, labels, done, bucket = (), (), False, 'inbox'
        else:
            assignees, labels, done = observed.assignee_ids, observed.label_ids, observed.done
            bucket = next(key for key, value in binding.bucket_ids.items() if value == observed.bucket_id)

        if create or command.action == 'assign':
            member = self.client.members.get_by_id(values.assignee_id)
            if not member or not member.enabled or command.project_id not in member.project_ids:
                raise VikunjaError('assignee_unavailable')
            for identifier in assignees:
                if identifier != member.vikunja_user_id:
                    steps.append({'kind': 'remove_assignee', 'provider_id': identifier})
            if member.vikunja_user_id not in assignees:
                steps.append({'kind': 'add_assignee', 'member_id': member.id, 'provider_id': member.vikunja_user_id})
        if (create or command.action == 'classify') and values.classification_confirmed is True:
            for key, identifier in (('important', binding.important_label_id), ('urgent', binding.urgent_label_id)):
                wanted = getattr(values, key)
                if bool(wanted) != (identifier in labels):
                    steps.append({'kind': 'add_label' if wanted else 'remove_label', 'label_id': identifier})
        if command.action == 'set_state':
            target = values.bucket
            if bucket != target:
                steps.append({'kind': 'move', 'bucket': target, 'bucket_id': binding.bucket_ids[target]})
            if done != (target in ('done', 'cancelled')):
                steps.append({'kind': 'patch', 'fields': {'done': target in ('done', 'cancelled')}})
            cancelled = target == 'cancelled'
            if cancelled != (binding.cancelled_label_id in labels):
                steps.append({'kind': 'add_label' if cancelled else 'remove_label', 'label_id': binding.cancelled_label_id})
        if command.action == 'rename' and values.title != observed.title:
            steps.append({'kind': 'patch', 'fields': {'title': values.title}})
        if command.action in ('set_due', 'resolve_due') and values.due_at != observed.due_at:
            steps.append({'kind': 'patch', 'fields': {'due_date': _date(values.due_at) or ZERO_DATE}})

        body = None
        if command.action == 'comment':
            body = values.comment
        elif command.action == 'propose_due':
            body = f'Предложение срока: {_date(values.due_at) or "Без срока"}\nПричина: {values.reason}'
        elif command.action in ('set_due', 'resolve_due'):
            prefix = 'Подтверждение внешнего срока' if command.action == 'resolve_due' else 'Срок'
            body = f'{prefix}: {_date(observed.due_at) or "Без срока"} → {_date(values.due_at) or "Без срока"}\nПричина: {values.reason}'
            if command.action == 'resolve_due':
                body += f'\nПодтверждённый preview: {command.resolution_id}'
        elif command.action == 'set_state' and values.result:
            body = f'Результат: {values.result}'
        if body is not None:
            marker = f'secretary-command:{command.operation_id}:{len(steps)}'
            body += f'\nАвтор команды: {actor_id}\n\n{marker}'
            steps.append({'kind': 'comment', 'literal': body, 'text': body,
                          'marker': marker})
        return steps

    def _dispatch(self, command, task_id, step, etag):
        kind = step['kind']
        if kind == 'create':
            return self.client.create_task(command, step['marker'])
        if kind == 'patch':
            self.client.apply_task_change(task_id, step['fields'], expected_etag=etag)
        elif kind == 'move':
            self.client.move_task(task_id, step['bucket'])
        elif kind == 'add_label':
            self.client.add_label(task_id, step['label_id'])
        elif kind == 'remove_label':
            self.client.remove_label(task_id, step['label_id'])
        elif kind == 'add_assignee':
            self.client.add_assignee(task_id, step['member_id'])
        elif kind == 'remove_assignee':
            self.client.remove_assignee(task_id, step['provider_id'])
        elif kind == 'comment':
            return self.client.add_comment(task_id, step['text'])
        else:
            raise VikunjaError('unknown_remote_step')
        return None

    def _verify(self, command, step, before, after, entity_id=None):
        """Accept only this exact primitive's allowed difference."""
        if step['kind'] == 'create':
            values = command.values
            if (after['project_id'] != command.project_id or after['title'] != values.title
                    or after['due_at'] != _date(values.due_at) or after['done']
                    or after['assignee_ids'] or after['label_ids'] or after['comments']
                    or after['repeat_after'] or after['repeat_mode']
                    or [self.client.binding.manual_view_id, self.client.binding.bucket_ids['inbox']] not in after['buckets']
                    or not has_visible_marker(after['description'], step['marker'])
                    or visible_text(after['description']) != '\n' + _literal(values.description or '') + '\n\n' + step['marker'] + '\n'
                    or entity_id is not None and after['task_id'] != entity_id):
                raise VikunjaError('remote_step_postcondition_mismatch')
            return
        if not isinstance(before, dict):
            raise VikunjaError('remote_step_before_image_missing')
        expected = deepcopy(before)
        kind = step['kind']
        if kind == 'patch':
            for field, value in step['fields'].items():
                expected['due_at' if field == 'due_date' else field] = None if value == ZERO_DATE else value
        elif kind == 'move':
            expected['buckets'] = sorted([view, step['bucket_id'] if view == self.client.binding.manual_view_id else bucket]
                                         for view, bucket in expected['buckets'])
        elif kind in ('add_label', 'remove_label'):
            labels = set(expected['label_ids'])
            (labels.add if kind == 'add_label' else labels.remove)(step['label_id'])
            expected['label_ids'] = sorted(labels)
        elif kind in ('add_assignee', 'remove_assignee'):
            members = set(expected['assignee_ids'])
            (members.add if kind == 'add_assignee' else members.remove)(step['provider_id'])
            expected['assignee_ids'] = sorted(members)
        elif kind == 'comment':
            prior = {comment['id']: comment for comment in before['comments']}
            added = [comment for comment in after['comments'] if comment['id'] not in prior]
            if (len(added) != 1 or added[0]['author_id'] != self.client.binding.bot_user_id
                    or not has_visible_marker(added[0]['comment'], step['marker'])
                    or visible_text(added[0]['comment']) != '\n' + _literal(step['literal']) + '\n'
                    or entity_id is not None and added[0]['id'] != entity_id):
                raise VikunjaError('remote_step_postcondition_mismatch')
            expected['comments'] = sorted([*before['comments'], added[0]], key=lambda x: x['id'])
        else:
            raise VikunjaError('unknown_remote_step')
        if expected != after:
            raise VikunjaError('remote_step_postcondition_mismatch')

    def _final(self, claim, baseline, task_id, fingerprint):
        members = self._member_bindings(claim.command, baseline)
        if claim.command.action in ('link', 'resolve_due') or claim.command.origin and claim.command.origin.source_kind == 'meeting':
            members.append(self._link_actor_binding(claim))
        self.repository.renew_claim(claim, lease_seconds=self.lease_seconds)
        context = TaskReadContext(revision=baseline.revision + 1 if baseline else 0,
                                  baseline=baseline, verifying_command=claim.command)
        snapshot = self.client.get_task(task_id, context=context)
        expected_assignee = (claim.command.values.assignee_id if claim.command.action in ('create', 'assign')
                             else baseline.assignee_id)
        if snapshot.assignee_id != expected_assignee:
            raise TeamConflict('member_mapping_changed')
        if snapshot.remote_fingerprint != fingerprint:
            raise VikunjaError('external_change_during_final_read')
        return self.repository.record_remote_result(claim, state='applied', snapshot=snapshot, expected_member_bindings=members)

    def execute(self, claim):
        if claim.reconciliation:
            return self._reconcile(claim)
        repository, command = self.repository, claim.command
        started = False
        active_ordinal = None
        entity_id = None
        dispatching, verified_steps = False, 0
        try:
            baseline = repository.authorize_claim(claim)
            if command.project_id != self.client.binding.project_id:
                raise VikunjaError('client_project_scope_mismatch')
            progress = repository.read_remote_progress(claim)
            if progress['steps']:
                # Even a reused live claim cannot resend an already started POST.
                return repository.record_remote_result(claim, state='uncertain', error_code='remote_progress_requires_reconciliation')
            self.client.validate_binding()
            observed = self.client.observe_task(command.task_id) if command.task_id else None
            if observed is not None:
                _image(observed)
                start_fingerprint = repository.remote_start_fingerprint(claim) if command.action == 'resolve_due' else command.expected_fingerprint
                if observed.remote_fingerprint != start_fingerprint:
                    return repository.record_remote_result(claim, state='conflict', error_code='external_change_detected')
                if observed.repeat_after or observed.repeat_mode:
                    raise VikunjaError('repeating_task_not_supported')
            else:
                matches = self.client.find_origin(command.project_id, self._origin(command))
                if matches:
                    return repository.record_remote_result(claim, state='conflict' if len(matches) > 1 else 'uncertain',
                        error_code='duplicate_origin_marker' if len(matches) > 1 else 'existing_origin_requires_reconciliation')
            steps = self._plan(command, observed, claim.actor_id)
            members = self._member_bindings(command, baseline, observed)
            for step in steps:
                step['member_bindings'] = members
            progress = repository.save_remote_plan(claim, steps)
            task_id = command.task_id
            for ordinal, step in enumerate(progress['plan']):
                repository.renew_claim(claim, lease_seconds=self.lease_seconds)
                # Fresh reads after a previous verified step detect intervening edits.
                before = self.client.observe_task(task_id) if task_id else None
                before_image = _image(before) if before else None
                expected = observed.remote_fingerprint if observed else None
                if (before.remote_fingerprint if before else None) != expected:
                    return repository.record_remote_result(claim, state='uncertain' if started else 'conflict',
                        error_code='external_change_between_steps')
                # This transaction rereads ACL, baseline revision and the fence
                # immediately before one remote primitive. No hidden retries.
                repository.begin_remote_step(claim, ordinal, content_hash({'step': step, 'task_id': task_id}),
                    expected, before_image=before_image)
                started, active_ordinal, entity_id = True, ordinal, None
                dispatching = True
                entity_id = self._dispatch(command, task_id, step, before.etag if before else None)
                dispatching = False
                if step['kind'] == 'create':
                    task_id = entity_id
                observed = self.client.observe_task(task_id)
                after = _image(observed)
                self._verify(command, step, before_image, after, entity_id)
                repository.record_remote_step(claim, ordinal, state='verified',
                    after_fingerprint=observed.remote_fingerprint, remote_entity_id=entity_id)
                active_ordinal = None
                verified_steps += 1
                if ordinal + 1 < len(progress['plan']):
                    repository.record_remote_result(claim, state='reconciling')
            if observed is None:
                raise VikunjaError('missing_remote_task')
            return self._final(claim, baseline, task_id, observed.remote_fingerprint)
        except (TeamConflict, TeamForbidden, VikunjaError) as exc:
            code = getattr(exc, 'code', None) or str(exc)
            status = getattr(exc, 'status_code', None)
            if status is not None:
                code += '_' + str(status)
            if code == 'stale_claim':
                # An expired worker cannot update state. Recovery marks it uncertain.
                raise
            definite_refusal = dispatching and isinstance(exc, VikunjaError) and not isinstance(exc, VikunjaMutationUncertain)
            if active_ordinal is not None:
                repository.record_remote_step(claim, active_ordinal, state='rejected' if definite_refusal else 'uncertain', remote_entity_id=entity_id,
                                               error_code=code)
            if definite_refusal and not verified_steps:
                state = 'conflict' if status in (409, 412) else 'rejected'
            else:
                state = 'uncertain' if started else ('rejected' if isinstance(exc, TeamForbidden) else 'conflict')
            return repository.record_remote_result(claim, state=state, error_code=code)

    def _reconcile(self, claim):
        """No remote mutation is allowed on a recovery claim."""
        repository, command = self.repository, claim.command
        try:
            baseline = repository.authorize_reconciliation(claim)
            if command.project_id != self.client.binding.project_id:
                return repository.record_remote_result(claim, state='uncertain', error_code='client_project_scope_mismatch')
            progress = repository.read_remote_progress(claim)
            if command.action == 'link':
                self._link_actor_binding(claim)
                # Only this action has no outbound primitives by contract. A
                # missing plan, or another action's empty plan, proves nothing.
                if (progress['plan'] != [] or progress['steps'] != []
                        or progress['plan_hash'] != content_hash([])):
                    return repository.record_remote_result(claim, state='uncertain', error_code='remote_progress_missing')
                if (baseline is None or baseline.task_id != command.task_id or baseline.project_id != command.project_id
                        or baseline.revision != command.expected_revision
                        or baseline.remote_fingerprint != command.expected_fingerprint):
                    raise TeamConflict('reconciliation_baseline_changed')
                observed = self.client.observe_task(command.task_id)
                _image(observed)
                if observed.remote_fingerprint != command.expected_fingerprint:
                    raise VikunjaError('reconciliation_remote_changed')
                if observed.repeat_after or observed.repeat_mode:
                    raise VikunjaError('repeating_task_not_supported')
                self._member_bindings(command, baseline, observed)
                return self._final(claim, baseline, command.task_id, observed.remote_fingerprint)
            if not progress['plan'] or not progress['steps']:
                return repository.record_remote_result(claim, state='uncertain', error_code='remote_progress_missing')
            if command.task_id:
                if baseline.revision != command.expected_revision or baseline.remote_fingerprint != command.expected_fingerprint:
                    return repository.record_remote_result(claim, state='uncertain', error_code='reconciliation_baseline_changed')
                task_id = command.task_id
            else:
                matches = self.client.find_origin(command.project_id, self._origin(command))
                if len(matches) != 1:
                    return repository.record_remote_result(claim, state='conflict' if len(matches) > 1 else 'uncertain',
                        error_code='origin_not_found' if not matches else 'duplicate_origin_marker')
                task_id, baseline = matches[0], None
            # A lost create ack can reveal a partial task. It never authorizes
            # dispatching the remaining unstarted assignment/label requests.
            if len(progress['steps']) != len(progress['plan']):
                return repository.record_remote_result(claim, state='uncertain', error_code='partial_remote_plan')
            last = progress['steps'][-1]
            if last['state'] == 'rejected':
                return repository.record_remote_result(claim, state='uncertain', error_code='definite_remote_step_rejected')
            observed = self.client.observe_task(task_id)
            image = _image(observed)
            if last['state'] == 'verified':
                if observed.remote_fingerprint != last['after_fingerprint']:
                    raise VikunjaError('reconciliation_remote_changed')
            else:
                if last.get('before_image') is not None and content_hash(last['before_image']) != last['before_fingerprint']:
                    raise VikunjaError('reconciliation_before_image_invalid')
                self._verify(command, progress['plan'][-1], last.get('before_image'), image, last.get('remote_entity_id'))
            return self._final(claim, baseline, task_id, observed.remote_fingerprint)
        except (TeamForbidden, TeamConflict, VikunjaError) as exc:
            if str(exc) == 'stale_claim':
                raise
            return repository.record_remote_result(claim, state='uncertain', error_code=getattr(exc, 'code', None) or str(exc))
