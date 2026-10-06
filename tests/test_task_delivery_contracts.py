"""T5 publication contracts: exact identities and explicit owner decisions."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from secretary.domain.team import TaskCommand
from secretary.domain.task_delivery import PublicationTask


def ident():
    return str(uuid4())


def test_link_command_has_no_mutation_values_and_requires_expected_task_version():
    command = TaskCommand(operation_id=ident(), project_id='7', task_id='9007199254740997',
        expected_revision=3, expected_fingerprint='a' * 64, action='link', values={})
    assert command.values.model_fields_set == set()
    with pytest.raises(ValidationError):
        TaskCommand(**{**command.model_dump(), 'values': {'title': 'Unapproved rename'}})


def test_publication_update_proposal_is_bound_to_a_verified_target():
    common = dict(action_id='action', title='Report', assignee_id=ident(),
                  source_segment_ids=('segment',), evidence_quote='Literal source')
    with pytest.raises(ValidationError, match='publication_target_required'):
        PublicationTask(**common, intent='propose_update')
    task = PublicationTask(**common, intent='propose_update', target_publication_id=ident(),
        target_task_id='9007199254740997', expected_task_revision=3, expected_task_fingerprint='a' * 64)
    assert task.due_timezone == 'Europe/Moscow'


def test_creation_cannot_smuggle_an_existing_task_target():
    with pytest.raises(ValidationError, match='creation_has_no_target'):
        PublicationTask(action_id='action', title='Report', assignee_id=ident(),
            source_segment_ids=('segment',), evidence_quote='Literal source', target_task_id='7')


def test_publication_preserves_canonical_assignment_evidence_version():
    task = PublicationTask(action_id='action', title='Report', assignee_id=ident(),
        source_segment_ids=('segment',), evidence_quote='Literal source',
        source_fingerprint='assignment-evidence-v1:' + 'a' * 64)
    assert task.source_fingerprint.startswith('assignment-evidence-v1:')
    with pytest.raises(ValidationError):
        PublicationTask(**{**task.model_dump(), 'source_fingerprint': 'a' * 64})
