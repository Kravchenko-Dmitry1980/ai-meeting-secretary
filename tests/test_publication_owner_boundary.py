"""Synthetic T5 review probes; no owner data or provider transport."""
from importlib import import_module
from uuid import uuid4

import pytest

from test_team_repository import api, case
from test_team_tasks import Remote


def publication(case, action):
    remote = Remote(case)
    values = ({'title': 'Synthetic create', 'assignee_id': case.member.id}
              if action == 'create' else {'comment': 'Synthetic proposal'})
    command = case.api.TaskCommand(
        operation_id=str(uuid4()), project_id='7', action=action, values=values,
        task_id=None if action == 'create' else case.snapshot.task_id,
        expected_revision=None if action == 'create' else case.snapshot.revision,
        expected_fingerprint=None if action == 'create' else case.snapshot.remote_fingerprint,
        origin={'source_kind': 'meeting', 'meeting_id': 'synthetic-meeting',
                'transcript_version': 1, 'summary_version': 1, 'action_id': 'synthetic-action',
                'publication_id': str(uuid4())})
    case.repo.accept_command(case.owner.id, command)
    claim = case.repo.claim_command('synthetic-worker')
    service = import_module('secretary.application.team_tasks').TeamTaskService(case.repo, remote)
    return command, claim, remote, service


@pytest.mark.parametrize('action', ['create', 'comment'])
def test_owner_demoted_before_publication_recovery_cannot_become_applied(case, action):
    command, claim, remote, service = publication(case, action)
    last = 'add_assignee' if action == 'create' else 'comment'
    def lose_ack(kind):
        if kind == last:
            raise remote.api.VikunjaMutationUncertain('synthetic_lost_ack')
    remote.hook = lose_ack
    assert service.execute(claim).execution_state.state == 'uncertain'
    recovery = case.repo.claim_reconciliation(command.operation_id, 'synthetic-recovery')
    case.repo.upsert_member(case.owner.model_copy(update={'role': 'member', 'revision': 1}),
                            expected_revision=0)
    calls_before = list(remote.calls)
    result = service.execute(recovery)
    assert result.execution_state.state == 'uncertain'
    assert result.execution_state.error_code == 'owner_required'
    assert remote.calls == calls_before


@pytest.mark.parametrize('action', ['create', 'comment'])
def test_owner_revocation_during_publication_final_read_cannot_commit_applied(case, monkeypatch, action):
    command, claim, remote, service = publication(case, action)
    original = remote.get_task
    def demote_in_final(*args, **kwargs):
        result = original(*args, **kwargs)
        case.repo.upsert_member(case.owner.model_copy(update={'role': 'member', 'revision': 1}),
                                expected_revision=0)
        return result
    monkeypatch.setattr(remote, 'get_task', demote_in_final)
    result = service.execute(claim)
    assert result.execution_state.state == 'uncertain'
    assert result.execution_state.error_code in {'owner_required', 'member_mapping_changed'}


def test_direct_applied_commit_cannot_bypass_current_meeting_owner_permission(case):
    _, claim, remote, service = publication(case, 'comment')
    case.repo.upsert_member(case.owner.model_copy(update={'role': 'member', 'revision': 1}), expected_revision=0)
    snapshot = case.snapshot.model_copy(update={'revision': 1})
    with pytest.raises(case.api.TeamForbidden, match='owner_required'):
        case.repo.record_remote_result(claim, state='applied', snapshot=snapshot)
    assert case.repo.get_projection(case.member.id, snapshot.task_id).revision == 0
