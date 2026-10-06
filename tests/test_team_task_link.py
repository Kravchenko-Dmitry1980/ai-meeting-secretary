"""Owner-only linking reads an existing task; recovery never authorizes a write."""
from dataclasses import replace
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_team_repository import api, case
from test_team_tasks import Remote
from test_vikunja_adapter import case as http_case, task as provider_task


def link_case(c, monkeypatch, *, action='link', values=None):
    remote = Remote(c)
    command = c.api.TaskCommand(operation_id=str(uuid4()), project_id='7',
        task_id=c.snapshot.task_id, expected_revision=c.snapshot.revision,
        expected_fingerprint=c.snapshot.remote_fingerprint, action=action,
        values={} if values is None else values)
    c.repo.accept_command(c.owner.id, command)
    claim = c.repo.claim_command('link-worker')
    service = import_module('secretary.application.team_tasks').TeamTaskService(c.repo, remote)

    def mutation_forbidden(*args, **kwargs):
        pytest.fail('link and its recovery must never issue an HTTP mutation')

    for method in ('create_task', 'apply_task_change', 'move_task', 'add_label',
                   'remove_label', 'add_assignee', 'remove_assignee', 'add_comment'):
        monkeypatch.setattr(remote, method, mutation_forbidden)
    return SimpleNamespace(c=c, remote=remote, service=service, command=command, claim=claim)


def crash_after_saved_empty_plan(s, monkeypatch):
    with monkeypatch.context() as patch:
        def crash(*args, **kwargs):
            raise OSError('synthetic process crash before final GET')

        patch.setattr(s.remote, 'get_task', crash)
        with pytest.raises(OSError, match='synthetic process crash'):
            s.service.execute(s.claim)
    progress = s.c.repo.read_remote_progress(s.claim)
    assert progress['plan'] == [] and progress['steps'] == []
    s.c.time[0] += timedelta(seconds=121)
    assert s.c.repo.recover_expired() == 1
    return s.c.repo.claim_reconciliation(s.command.operation_id, 'link-recovery')


@pytest.mark.parametrize('recovery', [False, True], ids=['execute', 'crash-recovery'])
def test_link_confirms_unchanged_task_without_provider_mutations(case, monkeypatch, recovery):
    s = link_case(case, monkeypatch)
    original = s.remote.current
    claim = crash_after_saved_empty_plan(s, monkeypatch) if recovery else s.claim
    result = s.service.execute(claim)
    assert result.execution_state.state == 'applied'
    assert result.execution_state.task_id == case.snapshot.task_id
    assert result.current.revision == case.snapshot.revision + 1
    assert result.current.remote_fingerprint == case.snapshot.remote_fingerprint
    assert result.current.title == case.snapshot.title
    assert result.current.assignee_id == case.snapshot.assignee_id
    assert s.remote.current == original
    calls = list(s.remote.calls)
    replay = case.repo.accept_command(case.owner.id, s.command)
    assert replay.acceptance_receipt == result.acceptance_receipt
    assert replay.execution_state == result.execution_state
    assert s.remote.calls == calls


@pytest.mark.parametrize('recovery', [False, True], ids=['execute', 'crash-recovery'])
@pytest.mark.parametrize('change,error', [
    ({'enabled': False}, 'actor_disabled'),
    ({'role': 'member'}, 'owner_required'),
    ({'project_ids': ()}, 'project_forbidden'),
])
def test_link_rechecks_owner_acl_before_provider_reads(case, monkeypatch, recovery, change, error):
    s = link_case(case, monkeypatch)
    claim = crash_after_saved_empty_plan(s, monkeypatch) if recovery else s.claim
    case.repo.upsert_member(case.owner.model_copy(update={**change, 'revision': 1}),
                            expected_revision=0)

    def denied_read(*args, **kwargs):
        pytest.fail('revoked link actor must be checked before any provider read')

    for method in ('validate_binding', 'observe_task', 'get_task'):
        monkeypatch.setattr(s.remote, method, denied_read)
    result = s.service.execute(claim)
    assert result.execution_state.state == ('uncertain' if recovery else 'rejected')
    assert result.execution_state.error_code == error
    assert case.repo.get_projection(case.member.id, case.snapshot.task_id) == case.snapshot


@pytest.mark.parametrize('recovery', [False, True], ids=['execute', 'crash-recovery'])
def test_link_foreign_client_is_rejected_before_provider_reads(case, monkeypatch, recovery):
    s = link_case(case, monkeypatch)
    claim = crash_after_saved_empty_plan(s, monkeypatch) if recovery else s.claim
    s.remote.binding = replace(s.remote.binding, project_id='8')

    def denied_read(*args, **kwargs):
        pytest.fail('foreign project client must not read the task')

    for method in ('validate_binding', 'observe_task', 'get_task'):
        monkeypatch.setattr(s.remote, method, denied_read)
    result = s.service.execute(claim)
    assert result.execution_state.state == ('uncertain' if recovery else 'conflict')
    assert result.execution_state.error_code == 'client_project_scope_mismatch'
    assert result.current == case.snapshot


@pytest.mark.parametrize('recovery', [False, True], ids=['execute', 'crash-recovery'])
def test_link_requires_fresh_remote_fingerprint(case, monkeypatch, recovery):
    s = link_case(case, monkeypatch)
    claim = crash_after_saved_empty_plan(s, monkeypatch) if recovery else s.claim
    s.remote.edit(title='Synthetic outside edit')
    result = s.service.execute(claim)
    assert result.execution_state.state == ('uncertain' if recovery else 'conflict')
    assert result.execution_state.error_code == ('reconciliation_remote_changed' if recovery else 'external_change_detected')
    assert result.current == case.snapshot


@pytest.mark.parametrize('recovery', [False, True], ids=['execute', 'crash-recovery'])
def test_link_refuses_stale_directory_member_revision(case, monkeypatch, recovery):
    s = link_case(case, monkeypatch)
    claim = crash_after_saved_empty_plan(s, monkeypatch) if recovery else s.claim
    case.repo.upsert_member(case.member.model_copy(update={'display_name': 'New name', 'revision': 1}),
                            expected_revision=0)
    result = s.service.execute(claim)
    assert result.execution_state.state == ('uncertain' if recovery else 'conflict')
    assert result.execution_state.error_code == 'member_mapping_changed'
    assert result.current == case.snapshot


def test_link_recovery_requires_original_gateway_revision(case, monkeypatch):
    s = link_case(case, monkeypatch)
    claim = crash_after_saved_empty_plan(s, monkeypatch)
    changed = case.snapshot.model_copy(update={'revision': 1})
    case.repo.save_projection(changed, expected_revision=0)

    def denied_read(*args, **kwargs):
        pytest.fail('changed gateway baseline must be detected before provider reads')

    monkeypatch.setattr(s.remote, 'observe_task', denied_read)
    result = s.service.execute(claim)
    assert result.execution_state.state == 'uncertain'
    assert result.execution_state.error_code == 'reconciliation_baseline_changed'
    assert result.current == changed


def test_link_acl_revocation_during_final_read_cannot_commit_applied(case, monkeypatch):
    s = link_case(case, monkeypatch)
    original = s.remote.get_task

    def revoke_before_commit(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        case.repo.upsert_member(case.owner.model_copy(update={'enabled': False, 'revision': 1}),
                                expected_revision=0)
        return snapshot

    monkeypatch.setattr(s.remote, 'get_task', revoke_before_commit)
    result = s.service.execute(s.claim)
    assert result.execution_state.state != 'applied'
    assert case.repo.get_projection(case.member.id, case.snapshot.task_id) == case.snapshot


@pytest.mark.parametrize('action,values', [
    ('link', {}), ('rename', {'title': 'Synthetic task'}),
])
def test_recovery_without_saved_plan_is_not_authorized(case, monkeypatch, action, values):
    s = link_case(case, monkeypatch, action=action, values=values)
    case.time[0] += timedelta(seconds=61)
    case.repo.recover_expired()
    claim = case.repo.claim_reconciliation(s.command.operation_id, 'recovery')
    result = s.service.execute(claim)
    assert result.execution_state.state == 'uncertain'
    assert result.execution_state.error_code == 'remote_progress_missing'
    assert result.current == case.snapshot


def test_other_action_empty_plan_does_not_gain_link_recovery(case, monkeypatch):
    s = link_case(case, monkeypatch, action='rename', values={'title': 'Synthetic task'})
    claim = crash_after_saved_empty_plan(s, monkeypatch)
    result = s.service.execute(claim)
    assert result.execution_state.state == 'uncertain'
    assert result.execution_state.error_code == 'remote_progress_missing'
    assert result.current == case.snapshot


def test_link_real_adapter_sends_only_get_requests(case):
    transport = http_case(record=provider_task(
        assignees=[{'id': int(case.member.vikunja_user_id)}], labels=[{'id': 999}]))
    adapter = transport.adapter
    adapter.members = case.api.RepositoryMemberDirectory(case.repo)
    baseline = adapter.get_task(case.snapshot.task_id, context=transport.api.TaskReadContext(
        revision=1, baseline=case.snapshot))
    case.repo.save_projection(baseline, expected_revision=0)
    command = case.api.TaskCommand(operation_id=str(uuid4()), project_id=baseline.project_id,
        task_id=baseline.task_id, expected_revision=baseline.revision,
        expected_fingerprint=baseline.remote_fingerprint, action='link', values={})
    case.repo.accept_command(case.owner.id, command)
    claim = case.repo.claim_command('http-link-worker')
    service = import_module('secretary.application.team_tasks').TeamTaskService(case.repo, adapter)
    result = service.execute(claim)
    assert result.execution_state.state == 'applied'
    assert result.current.remote_fingerprint == baseline.remote_fingerprint
    assert transport.calls
    assert {request.method for request in transport.calls} == {'GET'}
