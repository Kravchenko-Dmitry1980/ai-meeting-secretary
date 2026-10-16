"""Malformed proof objects fail closed with code-only diagnostics."""
from dataclasses import replace
from uuid import uuid4
import pytest

from secretary.domain.restore_quarantine import (
    Origin, QuarantineEntry, QuarantineInput, QuarantineLink, QuarantinePlan,
    ReferenceSpec, RestoreQuarantineError, SourceWatermark, key_digest,
)


def inputs():
    return QuarantineInput(str(uuid4()), *(['a' * 64] * 5),
        tuple(SourceWatermark(role, *(['b' * 64] * 3), 1, 0)
              for role in ('secretary', 'team', 'billing', 'vikunja')))


def plan():
    origin = Origin('secretary', 'jobs', key_digest('secretary', 'jobs', ('old',)))
    return QuarantinePlan(inputs(), (QuarantineEntry(origin, 'c' * 64, 'd' * 64, 'no_replay'),),
                          (), (('secretary', 'jobs', 1),))


@pytest.mark.parametrize('case', [
    'source_item', 'entry_item', 'link_item', 'count_shape', 'count_order',
    'kind_type', 'entry_disposition', 'reference_columns', 'reference_relation',
])
def test_invalid_proof_shapes_never_escape_typed_code_only_rejection(case):
    value = plan()
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_input_invalid'):
        if case == 'source_item': replace(value.input, sources=('sensitive-token',) * 4)
        elif case == 'entry_item': replace(value, entries=('sensitive-token',))
        elif case == 'link_item': replace(value, links=('sensitive-token',))
        elif case == 'count_shape': replace(value, family_counts=(('secretary',),))
        elif case == 'count_order': replace(value, family_counts=(('team', 'jobs', 0), ('secretary', 'jobs', 1)))
        elif case == 'kind_type': key_digest('secretary', 'jobs', ('old',), kind=[])
        elif case == 'entry_disposition': replace(value.entries[0], disposition=[])
        elif case == 'reference_columns': ReferenceSpec('team', 'team_commands', [], 'team', 'bot_events', ('id',), 'authorized_by')
        elif case == 'reference_relation': ReferenceSpec('team', 'team_commands', ('operation_id',), 'team', 'bot_events', ('id',), [])


def test_deny_absence_never_replaces_a_required_fresh_authorization():
    value = plan()
    unseen = Origin('secretary', 'jobs', key_digest('secretary', 'jobs', ('new',)))
    assert value.disposition(unseen) is None
    assert value.assert_no_legacy_authority((unseen,)) is None
    for unbound in ((), [], (None,), 'new'):
        with pytest.raises(RestoreQuarantineError, match='restore_lineage_unbound'):
            value.assert_no_legacy_authority(unbound)
