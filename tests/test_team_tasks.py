"""T4 remote progress is durable without changing the canonical projection."""
from copy import deepcopy
import asyncio
from dataclasses import replace
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_team_repository import api, case, command


def live_claim(c):
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    return c.repo.claim_command("task-worker")


def plan():
    return [{"kind": "move_bucket", "bucket": "doing"}, {"kind": "patch_done", "done": False}]


def test_remote_plan_is_immutable_and_replay_is_safe(case):
    c = case
    claim = live_claim(c)
    first = c.repo.save_remote_plan(claim, plan())
    assert c.repo.save_remote_plan(claim, plan()) == first
    with pytest.raises(c.api.TeamConflict, match="remote_plan_conflict"):
        c.repo.save_remote_plan(claim, [{"kind": "patch_done", "done": True}])
    assert c.repo.get_projection(c.member.id, c.snapshot.task_id) == c.snapshot


def test_started_remote_step_cannot_be_started_again_after_lost_ack(case):
    c = case
    claim = live_claim(c)
    c.repo.save_remote_plan(claim, plan())
    c.repo.begin_remote_step(claim, 0, "b" * 64, "a" * 64)
    with pytest.raises(c.api.TeamConflict, match="remote_step_already_started"):
        c.repo.begin_remote_step(claim, 0, "b" * 64, "a" * 64)
    with pytest.raises(c.api.TeamConflict, match="remote_step_predecessor_unverified"):
        c.repo.begin_remote_step(claim, 1, "c" * 64, "b" * 64)


def test_verified_steps_chain_fingerprints_and_keep_original_projection(case):
    c = case
    claim = live_claim(c)
    c.repo.save_remote_plan(claim, plan())
    c.repo.begin_remote_step(claim, 0, "b" * 64, "a" * 64)
    c.repo.record_remote_step(claim, 0, state="verified", after_fingerprint="d" * 64)
    with pytest.raises(c.api.TeamConflict, match="remote_step_fingerprint_conflict"):
        c.repo.begin_remote_step(claim, 1, "c" * 64, "a" * 64)
    c.repo.begin_remote_step(claim, 1, "c" * 64, "d" * 64)
    progress = c.repo.read_remote_progress(claim)
    assert progress["steps"][0]["state"] == "verified"
    assert progress["steps"][1]["state"] == "started"
    assert c.repo.authorize_claim(claim) == c.snapshot


def test_readonly_reconciliation_reads_progress_and_cannot_continue_mutations(case):
    c = case
    claim = live_claim(c)
    c.repo.save_remote_plan(claim, plan())
    c.repo.begin_remote_step(claim, 0, "b" * 64, "a" * 64)
    c.time[0] += timedelta(seconds=61)
    c.repo.recover_expired()
    recovery = c.repo.claim_reconciliation(claim.command.operation_id, "recovery")
    assert c.repo.read_remote_progress(recovery)["steps"][0]["state"] == "started"
    with pytest.raises(c.api.TeamConflict, match="reconciliation_is_read_only"):
        c.repo.begin_remote_step(recovery, 0, "b" * 64, "a" * 64)
    with pytest.raises(c.api.TeamConflict, match="stale_claim"):
        c.repo.record_remote_step(claim, 0, state="verified", after_fingerprint="d" * 64)


def test_revoke_between_steps_blocks_dispatch_intent(case):
    c = case
    claim = live_claim(c)
    c.repo.save_remote_plan(claim, plan())
    c.repo.upsert_member(c.member.model_copy(update={"enabled": False, "revision": 1}), expected_revision=0)
    with pytest.raises(c.api.TeamForbidden):
        c.repo.begin_remote_step(claim, 0, "b" * 64, "a" * 64)


def test_before_image_is_saved_for_readonly_recovery(case):
    c = case
    claim = live_claim(c)
    c.repo.save_remote_plan(claim, plan())
    image = {"title": "Synthetic task", "label_ids": ["101", "999"]}
    c.repo.begin_remote_step(claim, 0, "b" * 64, "a" * 64, before_image=image)
    image["label_ids"].append("123")
    assert c.repo.read_remote_progress(claim)["steps"][0]["before_image"]["label_ids"] == ["101", "999"]


class Remote:
    """A synthetic canonical server, with injectable lost acknowledgements."""
    def __init__(self, c):
        self.api = import_module('secretary.infrastructure.vikunja')
        self.domain = import_module('secretary.domain.team')
        self.binding = self.api.ProjectBinding(project_id='7', manual_view_id='17', bot_user_id='19',
            bucket_ids={state: str(201 + i) for i, state in enumerate(self.api.STATES)},
            important_label_id='101', urgent_label_id='102', cancelled_label_id='103')
        self.members = self.api.MappingMemberDirectory((c.owner, c.member))
        self.calls, self.hook, self.read_error, self.origins = [], None, False, ()
        self.c = c
        self.current = self.api.TaskObservation(c.snapshot.task_id, '7', 'Synthetic task',
            '<p>Original HTML</p>', False, None, (c.member.vikunja_user_id,), ('999',),
            '201', (('17', '201'),), (), 0, 0, '', '"one"')
        self.current = self._hashed(self.current)
        c.snapshot = c.snapshot.model_copy(update={'remote_fingerprint': self.current.remote_fingerprint,
                                                   'description': self.current.description})
        # Initial fixture is replaced only within this disposable DB.
        with c.db.transaction() as conn:
            conn.execute('UPDATE team_projections SET payload=? WHERE task_id=?',
                         (c.snapshot.model_dump_json(), c.snapshot.task_id))

    def image(self, item):
        return {'schema': 1, 'task_id': item.task_id, 'project_id': item.project_id,
            'title': item.title, 'description': item.description, 'done': item.done,
            'due_at': item.due_at.isoformat() if item.due_at else None,
            'assignee_ids': item.assignee_ids, 'label_ids': item.label_ids, 'buckets': item.buckets,
            'repeat_after': item.repeat_after, 'repeat_mode': item.repeat_mode,
            'comments': [{'id': x.id, 'comment': x.comment, 'author_id': x.author_id} for x in item.comments]}

    def _hashed(self, item):
        return replace(item, remote_fingerprint=self.domain.content_hash(self.image(item)))

    def edit(self, **fields):
        self.current = self._hashed(replace(self.current, **fields))

    def validate_binding(self):
        self.calls.append(('validate',))

    def observe_task(self, task_id, **kwargs):
        if self.read_error:
            raise self.api.VikunjaError('synthetic_read_error')
        assert task_id == self.current.task_id
        return self.current

    def get_task(self, task_id, *, context, **kwargs):
        item = self.observe_task(task_id)
        bucket = next(k for k, v in self.binding.bucket_ids.items() if v == item.bucket_id)
        member = self.members.get_by_vikunja_id(item.assignee_ids[0]) if item.assignee_ids else None
        values = context.verifying_command.values
        baseline = context.baseline
        return self.domain.TaskSnapshot(task_id=task_id, project_id='7', revision=context.revision,
            remote_fingerprint=item.remote_fingerprint, title=item.title, description=item.description,
            assignee_id=member.id if member else None, bucket=bucket,
            important='101' in item.label_ids, urgent='102' in item.label_ids,
            classification_confirmed=values.classification_confirmed is True or bool(baseline and baseline.classification_confirmed),
            due_at=item.due_at, due_confirmed=values.due_confirmed is True or bool(baseline and baseline.due_confirmed),
            due_phrase=values.due_phrase if 'due_at' in values.model_fields_set else (baseline.due_phrase if baseline else None),
            origin=context.verifying_command.origin if not baseline else baseline.origin)

    def find_origin(self, project_id, marker):
        self.calls.append(('find', marker))
        return self.origins

    def _sent(self, kind, **fields):
        self.calls.append((kind, deepcopy(fields)))
        self.edit(**fields)
        if self.hook:
            self.hook(kind)

    def create_task(self, draft, marker):
        import html
        self._sent('create', task_id='123', title=draft.values.title,
            description='<p>' + html.escape(draft.values.description or '').replace('\n', '<br>') + '</p><p>' + marker + '</p>',
            due_at=draft.values.due_at, assignee_ids=(), label_ids=(), done=False,
            bucket_id='201', buckets=(('17', '201'),), comments=())
        self.origins = ('123',)
        return '123'

    def apply_task_change(self, task_id, patch, expected_etag=None):
        changes = dict(patch)
        if 'due_date' in changes:
            from datetime import datetime
            value = changes.pop('due_date')
            changes['due_at'] = None if value == self.api.ZERO_DATE else datetime.fromisoformat(value.replace('Z', '+00:00'))
        self._sent('patch', **changes)

    def move_task(self, task_id, bucket):
        identifier = self.binding.bucket_ids[bucket]
        self._sent('move', bucket_id=identifier, buckets=(('17', identifier),))

    def add_label(self, task_id, label_id):
        self._sent('add_label', label_ids=tuple(sorted((*self.current.label_ids, label_id))))

    def remove_label(self, task_id, label_id):
        self._sent('remove_label', label_ids=tuple(x for x in self.current.label_ids if x != label_id))

    def add_assignee(self, task_id, member_id):
        provider_id = self.members.get_by_id(member_id).vikunja_user_id
        self._sent('add_assignee', assignee_ids=tuple(sorted((*self.current.assignee_ids, provider_id))))

    def remove_assignee(self, task_id, provider_id):
        self._sent('remove_assignee', assignee_ids=tuple(x for x in self.current.assignee_ids if x != provider_id))

    def add_comment(self, task_id, text):
        import html
        comment = self.api.CommentObservation(str(900 + len(self.current.comments)), '<p>' + html.escape(text).replace('\n', '<br>') + '</p>', '19')
        self._sent('comment', comments=tuple(sorted((*self.current.comments, comment), key=lambda x: x.id)))
        return comment.id


def service_case(c, *, action='set_state', values=None, create=False):
    remote = Remote(c)
    module = import_module('secretary.application.team_tasks')
    cmd = c.api.TaskCommand(operation_id=str(uuid4()), project_id='7',
        task_id=None if create else c.snapshot.task_id,
        expected_revision=None if create else 0,
        expected_fingerprint=None if create else c.snapshot.remote_fingerprint,
        action='create' if create else action, values=values or {'bucket': 'doing'})
    actor = c.owner.id
    c.repo.accept_command(actor, cmd)
    claim = c.repo.claim_command('executor')
    service = module.TeamTaskService(c.repo, remote)
    return SimpleNamespace(c=c, remote=remote, service=service, command=cmd, claim=claim)


def test_service_moves_task_and_returns_verified_receipt(case):
    s = service_case(case)
    result = s.service.execute(s.claim)
    assert result.execution_state.state == 'applied'
    assert result.current.bucket == 'doing' and result.current.revision == 1
    assert result.current.description == '<p>Original HTML</p>'
    assert s.remote.current.label_ids == ('999',)


def test_simultaneous_max_and_board_edit_conflicts(case):
    s = service_case(case)
    second = s.command.model_copy(update={'operation_id': str(uuid4())})
    case.repo.accept_command(case.owner.id, second)
    assert case.repo.claim_command('second') is None
    assert s.service.execute(s.claim).execution_state.state == 'applied'
    assert case.repo.claim_command('second') is None
    assert case.repo.get_receipt(case.owner.id, second.operation_id).execution_state.state == 'conflict'


def test_partial_move_not_reported_applied(case):
    s = service_case(case, values={'bucket': 'done', 'result': 'Completed'})
    def hook(kind):
        if kind == 'move':
            raise s.remote.api.VikunjaMutationUncertain('lost_ack')
    s.remote.hook = hook
    receipt = s.service.execute(s.claim)
    assert receipt.execution_state.state == 'uncertain'
    assert receipt.current.bucket == 'inbox'
    assert s.remote.current.bucket_id == s.remote.binding.bucket_ids['done'] and not s.remote.current.done
    assert not any(row[0] == 'patch' for row in s.remote.calls)


def test_accepted_create_lost_response_reconciles_without_duplicate(case):
    s = service_case(case, create=True, values={'title': 'Created', 'assignee_id': case.member.id})
    def hook(kind):
        if kind == 'create':
            s.remote.origins = ('123',)
            raise s.remote.api.VikunjaMutationUncertain('lost_ack')
    s.remote.hook = hook
    assert s.service.execute(s.claim).execution_state.state == 'uncertain'
    recovery = case.repo.claim_reconciliation(s.command.operation_id, 'recovery')
    assert s.service.execute(recovery).execution_state.state == 'uncertain'
    assert [x[0] for x in s.remote.calls].count('create') == 1
    assert not any(x[0] == 'add_assignee' for x in s.remote.calls)


def test_zero_search_does_not_authorize_retry(case):
    s = service_case(case, create=True, values={'title': 'Created', 'assignee_id': case.member.id})
    s.remote.hook = lambda kind: (_ for _ in ()).throw(s.remote.api.VikunjaMutationUncertain('lost_ack'))
    s.service.execute(s.claim)
    s.remote.origins = ()
    recovery = case.repo.claim_reconciliation(s.command.operation_id, 'recovery')
    assert s.service.execute(recovery).execution_state.state == 'uncertain'
    assert [x[0] for x in s.remote.calls].count('create') == 1


def test_external_edit_does_not_fabricate_actor_history(case):
    s = service_case(case)
    s.remote.edit(title='External edit')
    receipt = s.service.execute(s.claim)
    assert receipt.execution_state.state == 'conflict'
    assert receipt.execution_state.error_code == 'external_change_detected'
    assert receipt.current == case.snapshot
    assert not any(row[0] == 'move' for row in s.remote.calls)


def test_rename_preserves_html_labels_due_and_comments(case):
    s = service_case(case, action='rename', values={'title': 'New title'})
    receipt = s.service.execute(s.claim)
    assert receipt.execution_state.state == 'applied'
    assert receipt.current.title == 'New title' and receipt.current.description == '<p>Original HTML</p>'
    assert s.remote.current.label_ids == ('999',)


def test_classification_changes_only_managed_labels(case):
    s = service_case(case, action='classify', values={'important': True, 'urgent': True, 'classification_confirmed': True})
    receipt = s.service.execute(s.claim)
    assert receipt.execution_state.state == 'applied' and receipt.current.classification_confirmed
    assert set(s.remote.current.label_ids) == {'101', '102', '999'}


def test_assign_uses_verified_separate_relation_steps(case):
    s = service_case(case, action='assign', values={'assignee_id': case.owner.id})
    receipt = s.service.execute(s.claim)
    assert receipt.execution_state.state == 'applied' and receipt.current.assignee_id == case.owner.id
    assert [x[0] for x in s.remote.calls if x[0] in {'remove_assignee', 'add_assignee'}] == ['remove_assignee', 'add_assignee']


def test_set_due_preserves_reason_and_old_new_dates(case):
    due = case.time[0] + timedelta(days=5)
    s = service_case(case, action='set_due', values={'due_at': due, 'due_phrase': 'Через пять дней',
        'due_confirmed': True, 'reason': 'Поставщик задержал доставку'})
    receipt = s.service.execute(s.claim)
    assert receipt.execution_state.state == 'applied' and receipt.current.due_at == due
    assert receipt.current.due_phrase == 'Через пять дней'
    comment = s.remote.current.comments[0].comment
    assert 'Поставщик задержал доставку' in comment and due.isoformat() in comment


def test_propose_due_never_changes_canonical_date(case):
    due = case.time[0] + timedelta(days=2)
    s = service_case(case, action='propose_due', values={'due_at': due, 'due_confirmed': True, 'reason': 'Предлагаю'})
    receipt = s.service.execute(s.claim)
    assert receipt.execution_state.state == 'applied' and receipt.current.due_at is None
    assert not any(x[0] == 'patch' for x in s.remote.calls)
    assert due.isoformat() in s.remote.current.comments[0].comment


def test_comment_marker_wrong_author_cannot_prove_success(case):
    s = service_case(case, action='comment', values={'comment': 'Нужен договор'})
    def hook(kind):
        if kind == 'comment':
            s.remote.edit(comments=tuple(replace(x, author_id='999') for x in s.remote.current.comments))
    s.remote.hook = hook
    receipt = s.service.execute(s.claim)
    assert receipt.execution_state.state == 'uncertain'
    assert receipt.current == case.snapshot


def test_post_write_read_error_is_uncertain_and_never_replayed(case):
    s = service_case(case)
    s.remote.hook = lambda kind: setattr(s.remote, 'read_error', True)
    assert s.service.execute(s.claim).execution_state.state == 'uncertain'
    s.remote.read_error = False
    recovery = case.repo.claim_reconciliation(s.command.operation_id, 'recovery')
    assert s.service.execute(recovery).execution_state.state == 'applied'
    assert [x[0] for x in s.remote.calls].count('move') == 1


def test_external_unrelated_edit_after_write_is_uncertain(case):
    s = service_case(case)
    s.remote.hook = lambda kind: s.remote.edit(title='Concurrent outside edit')
    receipt = s.service.execute(s.claim)
    assert receipt.execution_state.state == 'uncertain'
    assert receipt.execution_state.error_code == 'remote_step_postcondition_mismatch'
    assert receipt.current == case.snapshot


def test_revocation_during_remote_read_prevents_dispatch(case):
    s = service_case(case)
    original = s.remote.observe_task
    def observe(*args, **kwargs):
        case.repo.upsert_member(case.owner.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
        return original(*args, **kwargs)
    s.remote.observe_task = observe
    result = s.service.execute(s.claim)
    assert result.execution_state.state == 'rejected'
    assert not any(x[0] == 'move' for x in s.remote.calls)


def test_create_verifies_every_step_before_applied(case):
    s = service_case(case, create=True, values={'title': 'Created', 'assignee_id': case.member.id,
        'important': True, 'urgent': False, 'classification_confirmed': True})
    result = s.service.execute(s.claim)
    assert result.execution_state.state == 'applied' and result.current.task_id == '123'
    assert result.current.assignee_id == case.member.id and result.current.important
    assert [x[0] for x in s.remote.calls].count('create') == 1


def test_comment_records_actual_actor_not_operation_uuid(case):
    s = service_case(case, action='comment', values={'comment': 'Literal *Markdown* <script> & text'})
    assert s.service.execute(s.claim).execution_state.state == 'applied'
    assert f'Автор команды: {case.owner.id}' in s.remote.current.comments[0].comment


def test_worker_claims_only_one_command_and_does_not_replay_failure(case):
    s = service_case(case)
    # Requeue our own claim only by starting with a new disposable fixture.
    s.remote.hook = lambda kind: (_ for _ in ()).throw(s.remote.api.VikunjaMutationUncertain('lost_ack'))
    s.service.execute(s.claim)
    module = import_module('secretary.orchestration.team_worker')
    worker = module.TeamWorker(case.repo, s.service, worker_id='loop')
    assert asyncio.run(worker.run_once()) is None
    assert [x[0] for x in s.remote.calls].count('move') == 1


def test_worker_executes_queued_command_off_event_loop(case):
    remote = Remote(case)
    module = import_module('secretary.application.team_tasks')
    cmd = case.api.TaskCommand(operation_id=str(uuid4()), project_id='7', task_id=case.snapshot.task_id,
        expected_revision=0, expected_fingerprint=case.snapshot.remote_fingerprint,
        action='rename', values={'title': 'Worker title'})
    case.repo.accept_command(case.owner.id, cmd)
    service = module.TeamTaskService(case.repo, remote)
    worker_module = import_module('secretary.orchestration.team_worker')
    worker = worker_module.TeamWorker(case.repo, service, worker_id='loop')
    assert asyncio.run(worker.run_once()).execution_state.state == 'applied'
    assert asyncio.run(worker.run_once()) is None


def test_worker_unexpected_dispatch_failure_is_uncertain_not_retry(case):
    remote = Remote(case)
    cmd = case.api.TaskCommand(operation_id=str(uuid4()), project_id='7', task_id=case.snapshot.task_id,
        expected_revision=0, expected_fingerprint=case.snapshot.remote_fingerprint,
        action='rename', values={'title': 'Worker title'})
    case.repo.accept_command(case.owner.id, cmd)
    module = import_module('secretary.application.team_tasks')
    worker_module = import_module('secretary.orchestration.team_worker')
    remote.hook = lambda kind: (_ for _ in ()).throw(OSError('SYNTHETIC PRIVATE BODY'))
    worker = worker_module.TeamWorker(case.repo, module.TeamTaskService(case.repo, remote), worker_id='loop')
    result = asyncio.run(worker.run_once())
    assert result.execution_state.state == 'uncertain' and result.execution_state.error_code == 'team_worker_failure'
    assert asyncio.run(worker.run_once()) is None
    assert [x[0] for x in remote.calls].count('patch') == 1


def test_create_replacement_description_is_not_reported_applied(case):
    s = service_case(case, create=True, values={'title': 'Created', 'assignee_id': case.member.id,
                                               'description': 'Agreed literal description'})
    def hook(kind):
        if kind == 'create':
            s.remote.edit(description='<p>Replacement</p><p>' + s.service._origin(s.command) + '</p>')
    s.remote.hook = hook
    assert s.service.execute(s.claim).execution_state.state == 'uncertain'


def test_comment_preserves_spacing_and_linebreaks(case):
    s = service_case(case, action='comment', values={'comment': 'A  B\nSecond line'})
    def hook(kind):
        if kind == 'comment':
            old = s.remote.current.comments[0]
            s.remote.edit(comments=(replace(old, comment=old.comment.replace('A  B<br>Second line', 'A B Second line')),))
    s.remote.hook = hook
    assert s.service.execute(s.claim).execution_state.state == 'uncertain'


@pytest.mark.parametrize('status,expected', [(403, 'rejected'), (422, 'rejected'), (409, 'conflict'), (412, 'conflict')])
def test_definite_first_http_rejection_releases_task(case, status, expected):
    s = service_case(case, action='rename', values={'title': 'New'})
    def rejected(*args, **kwargs):
        raise s.remote.api.VikunjaError('vikunja_http_rejected', status_code=status)
    s.remote.apply_task_change = rejected
    result = s.service.execute(s.claim)
    assert result.execution_state.state == expected
    assert result.execution_state.error_code == f'vikunja_http_rejected_{status}'
    fresh = s.command.model_copy(update={'operation_id': str(uuid4())})
    case.repo.accept_command(case.owner.id, fresh)
    assert case.repo.claim_command('corrected') is not None


def test_definite_rejection_after_a_verified_step_preserves_partial_uncertainty(case):
    s = service_case(case, values={'bucket': 'done', 'result': 'Completed'})
    def rejected(*args, **kwargs):
        raise s.remote.api.VikunjaError('vikunja_http_rejected', status_code=403)
    s.remote.apply_task_change = rejected
    result = s.service.execute(s.claim)
    assert result.execution_state.state == 'uncertain'
    assert result.current == case.snapshot


def test_rejected_last_step_cannot_be_proven_by_later_external_change(case):
    s = service_case(case, action='assign', values={'assignee_id': case.owner.id})
    def denied(*args, **kwargs):
        raise s.remote.api.VikunjaError('vikunja_http_rejected', status_code=403)
    s.remote.add_assignee = denied
    assert s.service.execute(s.claim).execution_state.state == 'uncertain'
    s.remote.edit(assignee_ids=(case.owner.vikunja_user_id,))
    recovery = case.repo.claim_reconciliation(s.command.operation_id, 'recovery')
    result = s.service.execute(recovery)
    assert result.execution_state.state == 'uncertain' and result.execution_state.error_code == 'definite_remote_step_rejected'


def test_fresh_directory_rejects_stale_remote_assignee_identity_before_mutation(case):
    s = service_case(case)
    s.remote.members = case.api.RepositoryMemberDirectory(case.repo)
    case.repo.upsert_member(case.member.model_copy(update={'vikunja_user_id': '21', 'revision': 1}), expected_revision=0)
    result = s.service.execute(s.claim)
    assert result.execution_state.state == 'conflict'
    assert result.execution_state.error_code == 'member_mapping_changed'
    assert not any(x[0] == 'move' for x in s.remote.calls)


def test_identity_change_between_final_get_and_local_commit_is_uncertain(case):
    s = service_case(case)
    original = s.remote.get_task
    def final_get(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        case.repo.upsert_member(case.member.model_copy(update={'vikunja_user_id': '21', 'revision': 1}), expected_revision=0)
        return snapshot
    s.remote.get_task = final_get
    result = s.service.execute(s.claim)
    assert result.execution_state.state == 'uncertain'
    assert result.current == case.snapshot
