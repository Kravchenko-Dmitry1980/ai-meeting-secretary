"""Recovery and identity regressions against synthetic tasks and isolated SQLite."""
import pytest
from importlib import import_module
from uuid import uuid4

from test_team_repository import api, case
from test_team_tasks import Remote, service_case


def test_revoked_create_actor_cannot_read_or_publish_during_recovery(case, monkeypatch):
    s = service_case(case, create=True, values={
        'title': 'Synthetic create', 'assignee_id': case.member.id,
    })

    def lost_final_relation(kind):
        if kind == 'add_assignee':
            raise s.remote.api.VikunjaMutationUncertain('lost_ack')

    s.remote.hook = lost_final_relation
    assert s.service.execute(s.claim).execution_state.state == 'uncertain'
    recovery = case.repo.claim_reconciliation(s.command.operation_id, 'recovery')
    case.repo.upsert_member(case.owner.model_copy(update={'enabled': False, 'revision': 1}),
                            expected_revision=0)

    def forbidden_read(*args, **kwargs):
        pytest.fail('revoked create actor must be scoped before any provider read')

    monkeypatch.setattr(s.remote, 'find_origin', forbidden_read)
    monkeypatch.setattr(s.remote, 'observe_task', forbidden_read)
    result = s.service.execute(recovery)
    assert result.execution_state.state == 'uncertain'
    assert result.execution_state.error_code == 'actor_disabled'
    assert result.current is None
    assert [call[0] for call in s.remote.calls].count('create') == 1
    assert [call[0] for call in s.remote.calls].count('add_assignee') == 1


@pytest.mark.parametrize('during_recovery', [False, True], ids=['before-create', 'recovery'])
def test_duplicate_origins_are_conflict_without_another_mutation(case, during_recovery):
    s = service_case(case, create=True, values={
        'title': 'Synthetic create', 'assignee_id': case.member.id,
    })
    claim = s.claim
    if during_recovery:
        def lost_create(kind):
            if kind == 'create':
                s.remote.origins = ('123',)
                raise s.remote.api.VikunjaMutationUncertain('lost_ack')

        s.remote.hook = lost_create
        assert s.service.execute(claim).execution_state.state == 'uncertain'
        claim = case.repo.claim_reconciliation(s.command.operation_id, 'recovery')

    s.remote.origins = ('123', '124')
    result = s.service.execute(claim)
    assert result.execution_state.state == 'conflict'
    assert result.execution_state.error_code == 'duplicate_origin_marker'
    assert [call[0] for call in s.remote.calls].count('create') == int(during_recovery)
    assert not any(call[0] == 'add_assignee' for call in s.remote.calls)
    calls = list(s.remote.calls)
    replay = case.repo.accept_command(case.owner.id, s.command)
    assert replay.acceptance_receipt == result.acceptance_receipt
    assert replay.execution_state == result.execution_state
    assert s.remote.calls == calls


def test_immutable_directory_cannot_assign_a_remapped_provider_identity(case):
    s = service_case(case, action='assign', values={'assignee_id': case.owner.id})
    changed = case.owner.model_copy(update={'vikunja_user_id': '21', 'revision': 1})
    case.repo.upsert_member(changed, expected_revision=0)
    assert s.remote.members.get_by_id(case.owner.id).vikunja_user_id == '11'
    assert case.repo.get_member(case.owner.id).vikunja_user_id == '21'

    result = s.service.execute(s.claim)
    assert result.execution_state.state == 'conflict'
    assert result.execution_state.error_code == 'member_mapping_changed'
    assert result.current == case.snapshot
    assert s.remote.current.assignee_ids == (case.member.vikunja_user_id,)
    assert not any(call[0] in {'remove_assignee', 'add_assignee'} for call in s.remote.calls)


def test_member_remap_after_plan_before_step_prevents_relation_mutation(case, monkeypatch):
    s = service_case(case, action='assign', values={'assignee_id': case.owner.id})
    original = s.remote.observe_task
    reads = 0

    def remap_on_step_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            case.repo.upsert_member(case.owner.model_copy(update={
                'vikunja_user_id': '21', 'revision': 1,
            }), expected_revision=0)
        return original(*args, **kwargs)

    monkeypatch.setattr(s.remote, 'observe_task', remap_on_step_read)
    result = s.service.execute(s.claim)
    assert reads == 2
    assert result.execution_state.state == 'conflict'
    assert result.execution_state.error_code == 'member_mapping_changed'
    assert result.current == case.snapshot
    assert s.remote.current.assignee_ids == (case.member.vikunja_user_id,)
    assert not any(call[0] in {'remove_assignee', 'add_assignee'} for call in s.remote.calls)


@pytest.mark.parametrize('action', ['create', 'assign'])
@pytest.mark.parametrize('queued', [True, False], ids=['before-claim', 'before-execute'])
def test_expected_assignee_revision_blocks_queued_mapping_change(case, monkeypatch, action, queued):
    remote = Remote(case)
    values = {'assignee_id': case.owner.id}
    if action == 'create':
        values['title'] = 'Synthetic create'
    command = case.api.TaskCommand(operation_id=str(uuid4()), project_id='7', action=action,
        task_id=None if action == 'create' else case.snapshot.task_id,
        expected_revision=None if action == 'create' else case.snapshot.revision,
        expected_fingerprint=None if action == 'create' else case.snapshot.remote_fingerprint,
        expected_assignee_revision=case.owner.revision, values=values)
    accepted = case.repo.accept_command(case.owner.id, command)
    claim = None if queued else case.repo.claim_command('worker')
    case.repo.upsert_member(case.owner.model_copy(update={'display_name': 'New name', 'revision': 1}),
                            expected_revision=0)

    def mutation_forbidden(*args, **kwargs):
        pytest.fail('changed confirmed assignee revision must not authorize a mutation')

    for method in ('create_task', 'apply_task_change', 'move_task', 'add_label',
                   'remove_label', 'add_assignee', 'remove_assignee', 'add_comment'):
        monkeypatch.setattr(remote, method, mutation_forbidden)
    if queued:
        assert case.repo.claim_command('worker') is None
        result = case.repo.get_receipt(case.owner.id, command.operation_id)
    else:
        service = import_module('secretary.application.team_tasks').TeamTaskService(case.repo, remote)
        result = service.execute(claim)
    assert result.acceptance_receipt == accepted.acceptance_receipt
    assert result.execution_state.state == 'conflict'
    assert result.execution_state.error_code == 'member_mapping_changed'
    assert remote.calls == []


def test_legacy_assignment_without_expected_member_revision_keeps_fresh_mapping_contract(case):
    remote = Remote(case)
    command = case.api.TaskCommand(operation_id=str(uuid4()), project_id='7', action='assign',
        task_id=case.snapshot.task_id, expected_revision=case.snapshot.revision,
        expected_fingerprint=case.snapshot.remote_fingerprint, values={'assignee_id': case.owner.id})
    case.repo.accept_command(case.owner.id, command)
    case.repo.upsert_member(case.owner.model_copy(update={'display_name': 'New name', 'revision': 1}),
                            expected_revision=0)
    remote.members = case.api.RepositoryMemberDirectory(case.repo)
    claim = case.repo.claim_command('worker')
    service = import_module('secretary.application.team_tasks').TeamTaskService(case.repo, remote)
    result = service.execute(claim)
    assert result.execution_state.state == 'applied'
    assert result.current.assignee_id == case.owner.id
    assert remote.current.assignee_ids == (case.owner.vikunja_user_id,)
