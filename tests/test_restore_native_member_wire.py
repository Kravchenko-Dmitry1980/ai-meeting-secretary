"""Go omitempty membership wire facts, through synthetic GET response boundaries."""
from __future__ import annotations

from dataclasses import asdict
import json

import pytest

from test_restore_native_observations import (
    PRIVATE,
    TOKEN,
    assert_sanitized,
    case,
    member,
    page,
    read,
)


def ordinary_without_owner(identifier=20, *, permission=0):
    row = member(identifier, owner=0, permission=permission)
    del row['bot_owner_id']
    return row


def assert_private_diagnostic(result):
    facts = asdict(result)
    assert result.evidence_kind == 'synthetic_get'
    assert result.diagnostic_only is True
    assert result.activation_supported is False and result.outbound_enabled is False
    assert result.method == 'GET'
    serialized = json.dumps(facts) + repr(result)
    assert TOKEN not in serialized and PRIVATE not in serialized
    assert not {'token', 'token_id', 'username', 'email', 'body',
                'source_evidence', 'activation_permit'} & set(facts)


@pytest.mark.asyncio
@pytest.mark.parametrize('permission', [0, 2])
async def test_omitted_ordinary_owner_is_wire_zero_beside_bound_bot(permission):
    c = case(pages=[page([member(), ordinary_without_owner(permission=permission)])])
    result = await read(c)
    assert [asdict(row) for row in result.direct_members] == [
        dict(id=19, bot_owner_id=11, permission=1),
        dict(id=20, bot_owner_id=0, permission=permission),
    ]
    assert (result.principal_id, result.human_owner_id) == (19, 11)
    assert result.project_owner_id == 23  # Independent project ownership stays separate.
    assert result.pages == 1
    assert [request.url.path for request in c.calls] == [
        '/api/v2/user', '/api/v2/projects/7', '/api/v2/projects/7/users',
    ]
    assert_private_diagnostic(result)


@pytest.mark.asyncio
async def test_explicit_zero_and_omitted_owner_have_identical_resource_facts():
    explicit = case(pages=[page([member(), member(20, owner=0, permission=2)])])
    omitted = case(pages=[page([member(), ordinary_without_owner(permission=2)])])
    explicit_result, omitted_result = await read(explicit), await read(omitted)
    assert explicit_result.direct_members == omitted_result.direct_members
    assert explicit_result.resource_sha256 == omitted_result.resource_sha256
    assert explicit_result.binding_sha256 == omitted_result.binding_sha256
    assert explicit_result.pages == omitted_result.pages == 1
    # Observation timestamps describe separate reads and need not be equal.
    assert_private_diagnostic(explicit_result)
    assert_private_diagnostic(omitted_result)


@pytest.mark.asyncio
@pytest.mark.parametrize('omitted_on_later_page', [False, True])
async def test_mixed_capped_pages_normalize_zero_and_require_complete_membership(omitted_on_later_page):
    if omitted_on_later_page:
        pages = [page([member(permission=2), member(21, owner=0, permission=2)], size=2, total=3),
                 page([ordinary_without_owner()], number=2, size=2, total=3)]
        expected = [dict(id=19, bot_owner_id=11, permission=2),
                    dict(id=21, bot_owner_id=0, permission=2),
                    dict(id=20, bot_owner_id=0, permission=0)]
    else:
        pages = [page([ordinary_without_owner(), member(21, owner=0, permission=2)], size=2, total=3),
                 page([member(permission=2)], number=2, size=2, total=3)]
        expected = [dict(id=20, bot_owner_id=0, permission=0),
                    dict(id=21, bot_owner_id=0, permission=2),
                    dict(id=19, bot_owner_id=11, permission=2)]
    c = case(pages=pages)
    result = await read(c)
    assert [asdict(row) for row in result.direct_members] == expected
    assert result.pages == 2 and len(result.direct_members) == 3
    assert [request.url.params['page'] for request in c.calls[2:]] == ['1', '2']
    assert all(request.url.params['per_page'] == '50' for request in c.calls[2:])
    assert_private_diagnostic(result)


@pytest.mark.asyncio
async def test_omitted_and_explicit_zero_do_not_disguise_duplicate_ids_across_pages():
    c = case(pages=[
        page([member(), ordinary_without_owner()], size=2, total=4),
        page([member(20, owner=0), member(21, owner=0)], number=2, size=2, total=4),
    ])
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert str(caught.value) == 'native_pagination_invalid'
    assert len(c.calls) == 4
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', [None, False, True, '0', 0.0, -1, 2**63, PRIVATE])
async def test_present_invalid_nonprincipal_owner_is_not_treated_as_omitted(owner):
    c = case(pages=[page([member(), member(20, owner=owner)])])
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert str(caught.value) == 'native_membership_invalid'
    assert len(c.calls) == 3  # Even a valid bound principal cannot hide another invalid row.
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    'omitted_owner', 'zero_owner', 'wrong_owner', 'null_owner',
    'readonly_permission', 'bool_permission', 'omitted_permission',
])
async def test_matching_principal_still_requires_bound_positive_owner_and_write_permission(change):
    principal = member()
    if change == 'omitted_owner':
        del principal['bot_owner_id']
    elif change == 'zero_owner':
        principal['bot_owner_id'] = 0
    elif change == 'wrong_owner':
        principal['bot_owner_id'] = 12
    elif change == 'null_owner':
        principal['bot_owner_id'] = None
    elif change == 'readonly_permission':
        principal['permission'] = 0
    elif change == 'bool_permission':
        principal['permission'] = True
    else:
        del principal['permission']
    c = case(pages=[page([principal, member(20, owner=0)])])
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert str(caught.value) == 'native_membership_invalid'
    assert len(c.calls) == 3  # The earlier /user response is valid and does not replace membership.
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('identifier', [None, False, '20', 0, -1, 2**63])
async def test_owner_omission_does_not_relax_nonprincipal_identifier_types(identifier):
    c = case(pages=[page([member(), ordinary_without_owner(identifier)])])
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert str(caught.value) == 'native_membership_invalid'
    assert len(c.calls) == 3
    assert_sanitized(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('row', [None, [], PRIVATE])
async def test_omission_compatibility_does_not_accept_nonobject_member_rows(row):
    c = case(pages=[page([member(), row])])
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert str(caught.value) == 'native_membership_invalid'
    assert len(c.calls) == 3
    assert_sanitized(caught.value)


@pytest.mark.asyncio
async def test_membership_wire_zero_rule_does_not_apply_to_current_bot_identity():
    c = case(user=dict(id=19, username=PRIVATE, email=PRIVATE))
    with pytest.raises(c.target.NativeObservationError) as caught:
        await read(c)
    assert str(caught.value) == 'native_identity_mismatch'
    assert [request.url.path for request in c.calls] == ['/api/v2/user']
    assert_sanitized(caught.value)
