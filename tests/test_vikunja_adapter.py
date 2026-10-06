"""T4 adapter boundaries using only synthetic HTTP; never start a native server."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import asdict
import hashlib
import importlib
import json
import traceback
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
TASK = 9007199254740997
MEMBER = '00000000-0000-0000-0000-000000000011'
OPERATION = '00000000-0000-0000-0000-000000000012'
PUBLICATION = '00000000-0000-0000-0000-000000000013'
STATES = ('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled')
LABELS = {'secretary:important': 101, 'secretary:urgent': 102, 'secretary:cancelled': 103}
ZERO_DATE = '0001-01-01T00:00:00Z'


def module():
    assert (ROOT / 'backend/secretary/infrastructure/vikunja.py').is_file(), 'T4 adapter absent'
    return importlib.import_module('secretary.infrastructure.vikunja')


def page(items, *, number=1, total=None, pages=None, size=50):
    total = len(items or []) if total is None else total
    return {'items': items, 'page': number, 'per_page': size,
            'total': total, 'total_pages': (total + size - 1) // size if pages is None else pages}


def task(**changes):
    return {'id': TASK, 'project_id': 7, 'title': 'Synthetic task',
            'description': '<p>Synthetic description</p>', 'done': False,
            'due_date': ZERO_DATE, 'assignees': [{'id': 29}],
            'labels': [{'id': 999}, {'id': 101}],
            'buckets': [{'id': 201, 'project_view_id': 17}],
            'repeat_after': 0, 'repeat_mode': 0, **changes}


def case(*, override=None, record=None, credential_binding=None, **client_options):
    api = module()
    domain = importlib.import_module('secretary.domain.team')
    member = domain.TeamMember(id=MEMBER, display_name='Synthetic member',
                              max_user_id='39', vikunja_user_id='29', project_ids=('7',))
    binding = api.ProjectBinding(project_id='7', manual_view_id='17', bot_user_id='19',
        bucket_ids={name: str(201 + i) for i, name in enumerate(STATES)},
        important_label_id='101', urgent_label_id='102', cancelled_label_id='103')
    calls = []
    remote = deepcopy(record or task())
    def handler(request):
        calls.append(request)
        custom = override(request) if override else None
        if custom is not None:
            return custom
        path = request.url.path.removeprefix('/api/v2')
        if path == '/user':
            return httpx.Response(200, json={'id': 19, 'username': 'bot-secretary', 'bot_owner_id': 1})
        if path == '/projects/7/users':
            return httpx.Response(200, json=page([
                {'id': 19, 'username': 'bot-secretary', 'bot_owner_id': 1, 'permission': 1}]))
        if path == '/projects/7/views/17':
            return httpx.Response(200, json={'id': 17, 'project_id': 7, 'view_kind': 'kanban',
                'bucket_configuration_mode': 'manual', 'done_bucket_id': 0, 'default_bucket_id': 201})
        if path == '/projects/7/views':
            return httpx.Response(200, json=page([
                {'id': 16, 'project_id': 7, 'view_kind': 'kanban', 'done_bucket_id': 0},
                {'id': 17, 'project_id': 7, 'view_kind': 'kanban', 'bucket_configuration_mode': 'manual',
                 'done_bucket_id': 0, 'default_bucket_id': 201},
                {'id': 18, 'project_id': 7, 'view_kind': 'list'}]))
        if path == '/projects/7/views/17/buckets':
            return httpx.Response(200, json=page([
                {'id': 201 + i, 'project_view_id': 17, 'title': state} for i, state in enumerate(STATES)]))
        if path.startswith('/labels/'):
            label_id = int(path.rsplit('/', 1)[1])
            return httpx.Response(200, json={'id': label_id,
                'title': next(name for name, value in LABELS.items() if value == label_id),
                'created_by': {'id': 19}})
        if path == f'/tasks/{TASK}' and request.method == 'GET':
            return httpx.Response(200, json=remote, headers={'ETag': '"synthetic-1"'})
        if path == f'/tasks/{TASK}/comments' and request.method == 'GET':
            return httpx.Response(200, json=page([]))
        if path == '/projects/7/tasks' and request.method == 'GET':
            return httpx.Response(200, json=page([remote]))
        raise AssertionError(f'unexpected synthetic request: {request.method} {path}')
    options = dict(base_url='http://127.0.0.1:3456/api/v2/',
                   headers={'Authorization': 'Bearer SYNTHETIC_ONLY'}, timeout=5,
                   trust_env=False, follow_redirects=False, transport=httpx.MockTransport(handler))
    options.update(client_options)
    http = httpx.Client(**options)
    adapter = api.VikunjaClient(http, binding=binding, members=api.MappingMemberDirectory((member,)),
        **({'credential_binding': credential_binding} if credential_binding is not None else {}))
    baseline = domain.TaskSnapshot(task_id=str(TASK), project_id='7', revision=3,
        remote_fingerprint='a' * 64, title='Synthetic task', description='<p>Synthetic description</p>',
        assignee_id=MEMBER, bucket='inbox', important=True, urgent=False, classification_confirmed=True)
    return SimpleNamespace(api=api, domain=domain, adapter=adapter, http=http, binding=binding,
        calls=calls, remote=remote, baseline=baseline, member=member,
        context=api.TaskReadContext(revision=3, baseline=baseline))


def command(c, action, **values):
    return c.domain.TaskCommand(operation_id=OPERATION, project_id='7', task_id=str(TASK),
        expected_revision=3, expected_fingerprint=c.baseline.remote_fingerprint,
        action=action, values=c.domain.TaskChange(**values))


def test_get_maps_exact_ids_configured_view_and_local_metadata():
    c = case(record=task(buckets=[{'id': 301, 'project_view_id': 27}, {'id': 201, 'project_view_id': 17}]))
    got = c.adapter.get_task(str(TASK), context=c.context)
    assert isinstance(got, c.domain.TaskSnapshot)
    assert (got.task_id, got.project_id, got.assignee_id, got.revision, got.bucket) == (str(TASK), '7', MEMBER, 3, 'inbox')
    assert (got.important, got.urgent, got.classification_confirmed) == (True, False, True)
    assert got.due_at is None and got.origin is None
    assert len(got.remote_fingerprint) == 64
    assert c.calls[0].url.params['expand'] == 'buckets'
    assert c.calls[0].url.params['format'] == 'html'


@pytest.mark.parametrize('change', [
    {'project_id': 8}, {'id': TASK + 1}, {'id': str(TASK)}, {'id': float(TASK)}, {'id': True},
    {'labels': {}}, {'assignees': [{'id': 29}, {'id': 30}]}, {'assignees': [{'id': 30}]},
    {'buckets': []}, {'buckets': [{'id': 999, 'project_view_id': 17}]},
    {'buckets': [{'id': 201, 'project_view_id': 17}, {'id': 202, 'project_view_id': 17}]},
    {'done': True}, {'repeat_after': 60}, {'repeat_mode': 1}, {'title': ''}, {'description': None},
])
def test_incompatible_remote_snapshot_is_not_silently_projected(change):
    c = case(record=task(**change))
    with pytest.raises(c.api.VikunjaError):
        c.adapter.get_task(str(TASK), context=c.context)


def test_null_collections_are_empty_but_missing_required_fields_fail():
    c = case(record=task(labels=None, assignees=None))
    got = c.adapter.get_task(str(TASK), context=c.context)
    assert got.assignee_id is None and got.important is False and got.urgent is False
    assert got.classification_confirmed is False
    del c.remote['done']
    with pytest.raises(c.api.VikunjaError):
        c.adapter.get_task(str(TASK), context=c.context)


def test_foreign_due_change_fails_closed_without_invented_confirmation():
    c = case(record=task(due_date='2026-10-06T09:00:00Z'))
    with pytest.raises(c.api.VikunjaError, match='remote_due_unconfirmed'):
        c.adapter.get_task(str(TASK), context=c.context)
    cmd = command(c, 'set_due', due_at=datetime(2026, 10, 6, 9, tzinfo=timezone.utc),
                  due_confirmed=True, due_phrase='Tuesday 12:00', reason='Agreed')
    got = c.adapter.get_task(str(TASK), context=c.api.TaskReadContext(
        revision=4, baseline=c.baseline, verifying_command=cmd))
    assert got.due_confirmed is True and got.due_phrase == 'Tuesday 12:00'
    assert got.due_at == datetime(2026, 10, 6, 9, tzinfo=timezone.utc)


def test_observation_keeps_partial_remote_facts_without_fabricated_projection():
    c = case(record=task(done=True, due_date='2026-10-06T09:00:00Z'))
    observed = c.adapter.observe_task(str(TASK))
    assert observed.done is True and observed.bucket_id == '201'
    assert observed.due_at == datetime(2026, 10, 6, 9, tzinfo=timezone.utc)
    assert observed.assignee_ids == ('29',) and observed.label_ids == ('101', '999')
    assert observed.etag == '"synthetic-1"'
    assert not hasattr(observed, 'revision') and not hasattr(observed, 'due_confirmed')
    with pytest.raises(c.api.VikunjaError):
        c.adapter.get_task(str(TASK), context=c.context)


def test_final_context_checks_command_postcondition_before_local_metadata():
    c = case()
    cmd = command(c, 'classify', important=False, urgent=True, classification_confirmed=True)
    with pytest.raises(c.api.VikunjaError, match='postcondition'):
        c.adapter.get_task(str(TASK), context=c.api.TaskReadContext(
            revision=4, baseline=c.baseline, verifying_command=cmd))


def test_fingerprint_ignores_array_order_local_revision_and_provider_noise():
    c = case()
    first = c.adapter.get_task(str(TASK), context=c.context)
    c.remote.update(labels=list(reversed(c.remote['labels'])), updated='2099-01-01T00:00:00Z',
                    is_unread=True, max_permission=2)
    second = c.adapter.get_task(str(TASK), context=c.api.TaskReadContext(revision=4, baseline=c.baseline))
    assert first.remote_fingerprint == second.remote_fingerprint


@pytest.mark.parametrize('change', [
    {'title': 'Changed'}, {'description': '<p>Changed</p>'}, {'assignees': []},
    {'labels': [{'id': 101}, {'id': 998}]}, {'buckets': [{'id': 202, 'project_view_id': 17}]},
])
def test_fingerprint_tracks_remote_mutable_fields(change):
    c = case()
    first = c.adapter.get_task(str(TASK), context=c.context)
    c.remote.update(change)
    assert c.adapter.get_task(str(TASK), context=c.context).remote_fingerprint != first.remote_fingerprint


def test_comments_participate_in_fingerprint_and_are_fully_paginated():
    changed = False
    def override(request):
        if request.url.path.endswith('/comments'):
            number = int(request.url.params.get('page', 1))
            items = [{'id': number, 'comment': 'Changed' if changed and number == 2 else 'Comment',
                      'author': {'id': 19}, 'created': '2026-10-03T12:00:00Z'}]
            return httpx.Response(200, json=page(items, number=number, total=2, pages=2, size=1))
    c = case(override=override)
    before = c.adapter.get_task(str(TASK), context=c.context)
    changed = True
    after = c.adapter.get_task(str(TASK), context=c.context)
    assert before.remote_fingerprint != after.remote_fingerprint
    assert sum(r.url.path.endswith('/comments') for r in c.calls) == 4


def test_conditional_get_304_reuses_raw_task_but_rechecks_comments_and_context():
    def override(request):
        if request.method == 'GET' and request.url.path.endswith(f'/tasks/{TASK}') and 'If-None-Match' in request.headers:
            return httpx.Response(304)
    c = case(override=override)
    c.adapter.get_task(str(TASK), context=c.context)
    got = c.adapter.get_task(str(TASK), context=c.api.TaskReadContext(revision=4, baseline=c.baseline), conditional=True)
    assert got.revision == 4
    assert any(r.headers.get('If-None-Match') == '"synthetic-1"' for r in c.calls)
    c.adapter.get_task(str(TASK), context=c.context)
    assert 'If-None-Match' not in c.calls[-2].headers


def test_304_without_cached_representation_is_an_error():
    c = case(override=lambda request: httpx.Response(304))
    with pytest.raises(c.api.VikunjaError, match='304'):
        c.adapter.get_task(str(TASK), context=c.context, conditional=True)


@pytest.mark.parametrize('options', [
    {'base_url': 'https://evil.invalid/api/v2/'}, {'base_url': 'http://localhost:3456/api/v2/'},
    {'base_url': 'http://127.0.0.1:8765/api/v1/'}, {'base_url': 'http://127.0.0.1:3456/api/v2/?token=x'},
    {'base_url': 'http://user:pass@127.0.0.1:3456/api/v2/'},
    {'trust_env': True}, {'follow_redirects': True}, {'timeout': None}, {'headers': {}},
    {'params': {'filter': 'done = false'}},
    {'event_hooks': {'request': [lambda request: None]}},
])
def test_unsafe_transport_configuration_is_rejected_before_http(options):
    with pytest.raises(module().VikunjaError):
        case(**options)


@pytest.mark.parametrize('change', [
    {'done_bucket_id': 206}, {'view_kind': 'list'}, {'bucket_configuration_mode': 'filter'},
    {'project_id': 8}, {'id': 18}, {'default_bucket_id': 999},
])
def test_binding_validation_rejects_native_automation_or_wrong_view(change):
    def override(request):
        if request.url.path == '/api/v2/projects/7/views/17':
            return httpx.Response(200, json={'id': 17, 'project_id': 7, 'view_kind': 'kanban',
                'bucket_configuration_mode': 'manual', 'done_bucket_id': 0, 'default_bucket_id': 201, **change})
    c = case(override=override)
    with pytest.raises(c.api.VikunjaError):
        c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaError, match='binding_not_validated'):
        c.adapter.apply_task_change(str(TASK), {'title': 'No write'})
    assert all(r.method == 'GET' for r in c.calls)


def test_binding_validation_checks_exact_buckets_and_own_label_identity():
    for bad_path, body in [('/projects/7/views/17/buckets', page([{'id': 201, 'project_view_id': 17}])),
                          ('/labels/101', {'id': 101, 'title': 'secretary:important', 'created_by': {'id': 99}}),
                          ('/user', {'id': 29, 'username': 'human'})]:
        c = case(override=lambda r: httpx.Response(200, json=body) if r.url.path == '/api/v2' + bad_path else None)
        with pytest.raises(c.api.VikunjaError):
            c.adapter.validate_binding()


def test_partial_patch_preserves_unrelated_html_and_uses_one_request():
    def override(request):
        if request.method == 'PATCH':
            return httpx.Response(200, json=task(title='Renamed'))
    c = case(override=override)
    c.adapter.validate_binding()
    before = len(c.calls)
    assert c.adapter.apply_task_change(str(TASK), {'title': 'Renamed'}, expected_etag='"old"') is None
    assert len(c.calls) == before + 1
    sent = c.calls[-1]
    assert json.loads(sent.content) == {'title': 'Renamed'}
    assert sent.headers['Content-Type'] == 'application/merge-patch+json'
    assert sent.headers['X-Vikunja-Format'] == 'html'
    assert sent.headers['If-Match'] == '"old"'


@pytest.mark.parametrize('patch', [{'project_id': 8}, {'assignees': []}, {'labels': []},
                                  {'description': '<b>overwrite</b>'}, {'bucket_id': 202},
                                  {'due_date': None}, {'done': 1}, {'title': ''}, {}])
def test_patch_allowlist_and_types_fail_before_mutation(patch):
    c = case()
    c.adapter.validate_binding()
    before = len(c.calls)
    with pytest.raises(c.api.VikunjaError):
        c.adapter.apply_task_change(str(TASK), patch)
    assert len(c.calls) == before


def test_null_due_is_explicit_zero_time_provider_step_not_json_null():
    c = case(override=lambda r: httpx.Response(200, json=task()) if r.method == 'PATCH' else None)
    c.adapter.validate_binding()
    c.adapter.apply_task_change(str(TASK), {'due_date': ZERO_DATE})
    assert json.loads(c.calls[-1].content) == {'due_date': ZERO_DATE}


@pytest.mark.parametrize('method,args,verb,path,body,status,response', [
    ('add_assignee', (str(TASK), MEMBER), 'POST', f'/tasks/{TASK}/assignees', {'user_id': 29}, 201, {'user_id': 29}),
    ('remove_assignee', (str(TASK), '29'), 'DELETE', f'/tasks/{TASK}/assignees/29', None, 204, None),
    ('add_label', (str(TASK), '101'), 'POST', f'/tasks/{TASK}/labels', {'label_id': 101}, 201, {'label_id': 101}),
    ('remove_label', (str(TASK), '102'), 'DELETE', f'/tasks/{TASK}/labels/102', None, 204, None),
    ('move_task', (str(TASK), 'doing'), 'PUT', '/projects/7/views/17/buckets/203/tasks', {'task_id': TASK}, 200,
     {'task_id': TASK, 'bucket_id': 203, 'project_view_id': 17}),
    ('add_comment', (str(TASK), 'Synthetic result'), 'POST', f'/tasks/{TASK}/comments', {'comment': '<p>Synthetic result</p>'}, 201,
     {'id': 401, 'comment': '<p>Synthetic result</p>', 'author': {'id': 19}}),
    ('delete_task', (str(TASK),), 'DELETE', f'/tasks/{TASK}', None, 204, None),
])
def test_single_step_methods_use_frozen_paths_and_exact_provider_ids(method, args, verb, path, body, status, response):
    def override(request):
        if request.method != 'GET':
            return httpx.Response(status, json=response) if response is not None else httpx.Response(status)
    c = case(override=override)
    c.adapter.validate_binding()
    before = len(c.calls)
    result = getattr(c.adapter, method)(*args)
    assert result == ('401' if method == 'add_comment' else None)
    assert len(c.calls) == before + 1
    sent = c.calls[-1]
    assert sent.method == verb and sent.url.path == '/api/v2' + path
    assert (json.loads(sent.content) if sent.content else None) == body


def create_command(c):
    return c.domain.TaskCommand(operation_id=OPERATION, project_id='7', action='create',
        origin=c.domain.TaskOrigin(source_kind='manual', publication_id=PUBLICATION),
        values=c.domain.TaskChange(title='Synthetic task', description='Agreed description', assignee_id=MEMBER))


def test_create_marker_is_in_first_post_and_readonly_collections_are_not_sent():
    c = case(override=lambda r: httpx.Response(201, json=task()) if r.method == 'POST' else None)
    c.adapter.validate_binding()
    before = len(c.calls)
    assert c.adapter.create_task(create_command(c), 'secretary-origin:' + PUBLICATION) == str(TASK)
    assert len(c.calls) == before + 1
    body = json.loads(c.calls[-1].content)
    assert body['description'] == '<p>Agreed description</p><p>secretary-origin:' + PUBLICATION + '</p>'
    assert body['title'] == 'Synthetic task'
    assert not {'assignees', 'labels', 'project_id', 'id'} & body.keys()
    assert c.calls[-1].url.params['format'] == 'html'
    assert c.calls[-1].headers['X-Vikunja-Format'] == 'html'


@pytest.mark.parametrize('kind', ['timeout', 'wrong_status', 'bad_json', 'wrong_id', 'wrong_project', 'server_error'])
def test_ambiguous_create_is_uncertain_and_never_retries(kind):
    def override(request):
        if request.method != 'POST':
            return None
        if kind == 'timeout':
            raise httpx.ReadTimeout('PRIVATE provider detail', request=request)
        return {'wrong_status': httpx.Response(200, json=task()),
                'bad_json': httpx.Response(201, text='PRIVATE token text'),
                'wrong_id': httpx.Response(201, json=task(id=True)),
                'wrong_project': httpx.Response(201, json=task(project_id=8)),
                'server_error': httpx.Response(500, text='PRIVATE server error')}[kind]
    c = case(override=override)
    c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaMutationUncertain) as failed:
        c.adapter.create_task(create_command(c), 'secretary-origin:' + PUBLICATION)
    assert 'PRIVATE' not in str(failed.value)
    assert sum(r.method == 'POST' for r in c.calls) == 1


@pytest.mark.parametrize('status', [401, 403, 404, 409, 412, 422, 429])
def test_definite_http_rejection_is_sanitized_and_not_reported_as_applied(status):
    c = case(override=lambda r: httpx.Response(status, json={'detail': 'PRIVATE', 'status': status},
             headers={'Content-Type': 'application/problem+json'}) if r.method == 'PATCH' else None)
    c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaError) as failed:
        c.adapter.apply_task_change(str(TASK), {'title': 'Rename'})
    assert failed.value.status_code == status
    assert not isinstance(failed.value, c.api.VikunjaMutationUncertain)
    assert 'PRIVATE' not in str(failed.value)


def test_nonempty_delete_204_is_uncertain():
    c = case(override=lambda r: httpx.Response(204, content=b'PRIVATE') if r.method == 'DELETE' else None)
    c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaMutationUncertain):
        c.adapter.delete_task(str(TASK))


def test_marker_search_covers_all_pages_and_exact_visible_marker_only():
    marker = 'secretary-origin:' + PUBLICATION
    def override(request):
        if request.url.path == '/api/v2/projects/7/tasks':
            number = int(request.url.params['page'])
            rows = {1: [task(description='<p>' + marker + '-not-exact</p>')],
                    2: [task(id=TASK + 1, description='<p>' + marker + '</p>')],
                    3: [task(id=TASK + 2, description='<p>' + marker + '</p>')]}
            return httpx.Response(200, json=page(rows[number], number=number, total=3, pages=3, size=1))
    c = case(override=override)
    assert c.adapter.find_origin('7', marker) == (str(TASK + 1), str(TASK + 2))
    assert len(c.calls) == 3 and all(r.method == 'GET' for r in c.calls)
    assert all('q' not in r.url.params for r in c.calls)


def test_zero_marker_results_do_not_authorize_any_create_or_retry():
    c = case(override=lambda r: httpx.Response(200, json=page(None, total=0, pages=0)))
    assert c.adapter.find_origin('7', 'secretary-origin:' + PUBLICATION) == ()
    assert len(c.calls) == 1 and c.calls[0].method == 'GET'


@pytest.mark.parametrize('body', [
    {'items': {}}, {'items': []}, page([], number=2), page([task()], pages=True),
    page([task(project_id=8)]), page([task(id=1), task(id=1)]),
])
def test_bad_collection_or_scope_is_never_false_absence(body):
    c = case(override=lambda r: httpx.Response(200, json=body))
    with pytest.raises(c.api.VikunjaError):
        c.adapter.find_origin('7', 'secretary-origin:' + PUBLICATION)


def test_read_timeout_and_redirect_are_not_mutation_uncertain_or_retried():
    for kind in ('timeout', 'redirect'):
        def override(request):
            if kind == 'timeout':
                raise httpx.ReadTimeout('PRIVATE', request=request)
            return httpx.Response(302, headers={'Location': 'https://evil.invalid'})
        c = case(override=override)
        with pytest.raises(c.api.VikunjaError) as failed:
            c.adapter.get_task(str(TASK), context=c.context)
        assert not isinstance(failed.value, c.api.VikunjaMutationUncertain)
        assert len(c.calls) == 1 and 'PRIVATE' not in str(failed.value)


@pytest.mark.parametrize('owner', [None, 0, True, '1', 1.0, -1])
def test_binding_requires_provider_bot_owner_identity_not_username_prefix(owner):
    c = case(override=lambda r: httpx.Response(200, json={
        'id': 19, 'username': 'bot-human', 'bot_owner_id': owner}) if r.url.path.endswith('/user') else None)
    with pytest.raises(c.api.VikunjaError):
        c.adapter.validate_binding()
    assert len(c.calls) == 1


@pytest.mark.parametrize('mutation', [False, True])
def test_transport_exception_traceback_has_no_provider_exception_context(mutation):
    def override(request):
        if request.url.path.endswith(f'/tasks/{TASK}'):
            raise httpx.ReadTimeout('PRIVATE token and request details', request=request)
    c = case(override=override)
    c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaError) as failed:
        if mutation:
            c.adapter.apply_task_change(str(TASK), {'title': 'Renamed'})
        else:
            c.adapter.observe_task(str(TASK))
    assert 'PRIVATE' not in ''.join(traceback.format_exception(failed.value))


def test_final_create_requires_inbox_not_another_valid_state():
    marker = 'secretary-origin:' + PUBLICATION
    c = case(record=task(description='<p>Agreed description</p><p>' + marker + '</p>',
                         buckets=[{'id': 202, 'project_view_id': 17}]))
    with pytest.raises(c.api.VikunjaError, match='postcondition'):
        c.adapter.get_task(str(TASK), context=c.api.TaskReadContext(
            revision=0, verifying_command=create_command(c)))


@pytest.mark.parametrize('text,expected', [
    ('<p>Result</p><p>{marker}</p>', True),
    ('Result\n\n{marker}', True),
    ('<p>{marker}-different</p>', False),
    ('<a href="{marker}">Result</a>', False),
    ('<script>{marker}</script>', False),
    ('<p>quoted {marker}</p>', False),
])
def test_public_visible_marker_helper_handles_exact_command_markers(text, expected):
    api = module()
    marker = 'secretary-command:' + OPERATION + ':4'
    assert api.has_visible_marker(text.format(marker=marker), marker) is expected
    assert api.visible_text('<p>A&amp;B</p><script>hidden</script><p>C</p>').strip() == 'A&B\n\nC'


@pytest.mark.parametrize('marker', ['anything', 'secretary-command:' + OPERATION + ':04',
                                   'secretary-command:' + OPERATION + ':-1',
                                   'secretary-origin:' + PUBLICATION + ':4'])
def test_public_marker_helper_rejects_unbounded_or_noncanonical_markers(marker):
    api = module()
    with pytest.raises(api.VikunjaError):
        api.has_visible_marker(marker, marker)


@pytest.mark.parametrize('body', [
    b'{"id":1,"id":2,"project_id":7}',
    b'{"id":1,"project_id":7,"value":NaN}',
    b'{"id":1,"project_id":7,"value":' + b'[' * 1100 + b'0' + b']' * 1100 + b'}',
    b'{"id":1,"project_id":7,"value":"' + b'x' * (2 * 1024 * 1024) + b'"}',
    b'\xff',
], ids=['duplicate-key', 'nonfinite', 'deep-json', 'oversized', 'bad-utf8'])
def test_malformed_or_unbounded_json_success_is_uncertain_without_retry(body):
    c = case(override=lambda r: httpx.Response(201, content=body,
        headers={'Content-Type': 'application/json'}) if r.method == 'POST' else None)
    c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaMutationUncertain):
        c.adapter.create_task(create_command(c), 'secretary-origin:' + PUBLICATION)
    assert sum(r.method == 'POST' for r in c.calls) == 1


@pytest.mark.parametrize('author', [None, {}, {'id': 29}, {'id': True}, {'id': '19'}])
def test_comment_success_requires_the_bound_bot_as_author(author):
    c = case(override=lambda r: httpx.Response(201, json={
        'id': 401, 'comment': 'Result', 'author': author}) if r.method == 'POST' else None)
    c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaMutationUncertain):
        c.adapter.add_comment(str(TASK), 'Result')


def test_full_scan_rejects_count_drift_and_duplicate_ids_across_pages():
    for duplicate in (False, True):
        def override(request):
            number = int(request.url.params['page'])
            identifier = TASK if number == 1 or duplicate else TASK + 1
            total = 2 if number == 1 or duplicate else 3
            return httpx.Response(200, json=page([task(id=identifier)], number=number,
                total=total, pages=total, size=1))
        c = case(override=override)
        with pytest.raises(c.api.VikunjaError):
            c.adapter.find_origin('7', 'secretary-origin:' + PUBLICATION)
        assert len(c.calls) == 2


def test_beyond_last_page_is_empty_while_preserving_total_and_server_page_size():
    c = case(override=lambda r: httpx.Response(200, json=page([], number=3, total=2, pages=2, size=1)))
    got = c.adapter.list_project_tasks(page=3, per_page=50)
    assert got.items == () and got.total == 2 and got.next_page is None and got.per_page == 1


def test_unmanaged_labels_and_bad_ids_are_rejected_without_http():
    c = case()
    c.adapter.validate_binding()
    before = len(c.calls)
    for method, args in [('add_label', (str(TASK), '999')), ('remove_label', (str(TASK), '999')),
                         ('move_task', (str(TASK), 'foreign')), ('delete_task', ('1/2',)),
                         ('remove_assignee', (str(TASK), '29?x=y'))]:
        with pytest.raises(c.api.VikunjaError):
            getattr(c.adapter, method)(*args)
    assert len(c.calls) == before


@pytest.mark.parametrize('field', ['title', 'description'])
def test_invalid_unicode_in_successful_task_response_is_sanitized(field):
    body = json.dumps(task(**{field: '\ud800'})).encode('ascii')
    c = case(override=lambda r: httpx.Response(200, content=body,
        headers={'Content-Type': 'application/json'}))
    with pytest.raises(c.api.VikunjaError, match='invalid_provider_text'):
        c.adapter.observe_task(str(TASK))


@pytest.mark.parametrize('description', ['<!-- unclosed', '<script>unclosed', '```unclosed\ncode',
                                         '<b>literal</b>\nA & B'])
def test_create_literal_html_keeps_origin_visible_after_lost_ack(description):
    stored = None
    def override(request):
        nonlocal stored
        if request.method == 'POST':
            body = json.loads(request.content)
            stored = task(title=body['title'], description=body['description'])
            raise httpx.ReadTimeout('lost acknowledgement', request=request)
        if request.url.path == '/api/v2/projects/7/tasks' and stored:
            return httpx.Response(200, json=page([stored]))
    c = case(override=override)
    c.adapter.validate_binding()
    draft = create_command(c)
    draft = draft.model_copy(update={'values': draft.values.model_copy(update={'description': description})})
    marker = 'secretary-origin:' + PUBLICATION
    with pytest.raises(c.api.VikunjaMutationUncertain):
        c.adapter.create_task(draft, marker)
    assert c.api.visible_text(stored['description']).strip() == description + '\n\n' + marker
    assert c.adapter.find_origin('7', marker) == (str(TASK),)
    assert sum(r.method == 'POST' for r in c.calls) == 1


def test_comment_literal_input_keeps_command_marker_visible_and_does_not_interpret_html():
    def override(request):
        if request.method == 'POST':
            return httpx.Response(201, json={'id': 401, 'comment': json.loads(request.content)['comment'],
                                             'author': {'id': 19}})
    c = case(override=override)
    c.adapter.validate_binding()
    marker = 'secretary-command:' + OPERATION + ':4'
    literal = '<!-- unclosed\n@someone & <script>\n\n' + marker
    c.adapter.add_comment(str(TASK), literal)
    sent = c.calls[-1]
    encoded = json.loads(sent.content)['comment']
    assert c.api.visible_text(encoded).strip() == literal
    assert c.api.has_visible_marker(encoded, marker)
    assert sent.url.params['format'] == 'html' and sent.headers['X-Vikunja-Format'] == 'html'


def test_empty_patch_304_is_only_an_unverified_no_change_response():
    c = case(override=lambda r: httpx.Response(304) if r.method == 'PATCH' else None)
    c.adapter.validate_binding()
    before = len(c.calls)
    assert c.adapter.apply_task_change(str(TASK), {'done': False}) is None
    assert len(c.calls) == before + 1 and c.calls[-1].method == 'PATCH'
    # The primitive cannot establish target postconditions; callers must read.
    assert c.adapter.observe_task(str(TASK)).done is False


@pytest.mark.parametrize('method,args', [
    ('add_label', (str(TASK), '101')), ('move_task', (str(TASK), 'doing')),
    ('delete_task', (str(TASK),)),
])
def test_304_is_not_accepted_for_other_mutation_methods(method, args):
    c = case(override=lambda r: httpx.Response(304) if r.method != 'GET' else None)
    c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaMutationUncertain):
        getattr(c.adapter, method)(*args)


def test_patch_304_with_body_is_uncertain():
    c = case(override=lambda r: httpx.Response(304, content=b'PRIVATE') if r.method == 'PATCH' else None)
    c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaMutationUncertain):
        c.adapter.apply_task_change(str(TASK), {'done': False})


def test_service_reads_after_patch_304_and_rejects_stale_target():
    c = case(override=lambda r: httpx.Response(304) if r.method == 'PATCH' else None)
    baseline = c.adapter.get_task(str(TASK), context=c.context)
    cmd = c.domain.TaskCommand(operation_id=OPERATION, project_id='7', task_id=str(TASK),
        expected_revision=baseline.revision, expected_fingerprint=baseline.remote_fingerprint,
        action='rename', values=c.domain.TaskChange(title='Requested new title'))
    claim = SimpleNamespace(command=cmd, actor_id=MEMBER, reconciliation=False)
    events = []
    class Repository:
        def get_member(self, identifier):
            return c.member if identifier == c.member.id else None
        def authorize_claim(self, claim):
            return baseline
        def read_remote_progress(self, claim):
            return {'steps': []}
        def save_remote_plan(self, claim, steps):
            return {'plan': steps}
        def renew_claim(self, claim, **kwargs):
            pass
        def begin_remote_step(self, *args, **kwargs):
            events.append(('begin',))
        def record_remote_step(self, *args, **kwargs):
            events.append(('step', kwargs['state']))
        def record_remote_result(self, *args, **kwargs):
            events.append(('result', kwargs['state']))
            return kwargs
    service = importlib.import_module('secretary.application.team_tasks').TeamTaskService(Repository(), c.adapter)
    result = service.execute(claim)
    assert result['state'] == 'uncertain' and result['error_code'] == 'remote_step_postcondition_mismatch'
    sent = next(i for i, request in enumerate(c.calls) if request.method == 'PATCH')
    assert any(request.method == 'GET' and request.url.path.endswith(f'/tasks/{TASK}')
               for request in c.calls[sent + 1:])
    assert sum(request.method == 'PATCH' for request in c.calls) == 1
    assert ('result', 'applied') not in events


@pytest.mark.parametrize('title', ['я' * 4000, '😀' * 4000], ids=['cyrillic', 'unicode-four-byte'])
def test_title_limits_match_domain_in_create_patch_and_observation(title):
    def override(request):
        if request.method in ('POST', 'PATCH'):
            return httpx.Response(201 if request.method == 'POST' else 200, json=task(title=title))
    c = case(record=task(title=title), override=override)
    c.adapter.validate_binding()
    draft = create_command(c)
    draft = draft.model_copy(update={'values': draft.values.model_copy(update={'title': title})})
    assert c.adapter.create_task(draft, 'secretary-origin:' + PUBLICATION) == str(TASK)
    c.adapter.apply_task_change(str(TASK), {'title': title})
    assert c.adapter.observe_task(str(TASK)).title == title
    assert c.adapter.get_task(str(TASK), context=c.context).title == title


def test_title_over_domain_character_limit_is_rejected_before_patch():
    c = case(record=task(title='x' * 4001))
    c.adapter.validate_binding()
    before = len(c.calls)
    with pytest.raises(c.api.VikunjaError):
        c.adapter.apply_task_change(str(TASK), {'title': 'x' * 4001})
    assert len(c.calls) == before
    with pytest.raises(c.api.VikunjaError):
        c.adapter.observe_task(str(TASK))


def test_long_cyrillic_comment_and_protocol_metadata_remain_bounded_literal_text():
    def override(request):
        if request.method == 'POST':
            return httpx.Response(201, json={'id': 401, 'comment': json.loads(request.content)['comment'],
                                             'author': {'id': 19}})
    c = case(override=override)
    c.adapter.validate_binding()
    literal = 'я' * 4000 + '\nАвтор команды: ' + MEMBER + '\n\nsecretary-command:' + OPERATION + ':12'
    assert c.adapter.add_comment(str(TASK), literal) == '401'
    assert c.api.visible_text(json.loads(c.calls[-1].content)['comment']).strip() == literal
    before = len(c.calls)
    with pytest.raises(c.api.VikunjaError):
        c.adapter.add_comment(str(TASK), 'я' * 5001)
    assert len(c.calls) == before


@pytest.mark.parametrize('description,allowed', [('<' * 600_000, False), ('>' * 20_000, True)],
                         ids=['escaped-over-2MiB', 'escaped-under-2MiB'])
def test_create_payload_limit_is_checked_after_html_escaping_before_dispatch(description, allowed):
    c = case(override=lambda r: httpx.Response(201, json=task()) if r.method == 'POST' else None)
    c.adapter.validate_binding()
    draft = create_command(c)
    draft = draft.model_copy(update={'values': draft.values.model_copy(update={'description': description})})
    before = len(c.calls)
    if allowed:
        assert c.adapter.create_task(draft, 'secretary-origin:' + PUBLICATION) == str(TASK)
        assert len(c.calls[-1].content) <= 2 * 1024 * 1024
    else:
        with pytest.raises(c.api.VikunjaError, match='mutation_payload_too_large'):
            c.adapter.create_task(draft, 'secretary-origin:' + PUBLICATION)
        assert len(c.calls) == before


SYNTHETIC_API_TOKEN = 'tk_SYNTHETIC_ONLY_NOT_A_REAL_CREDENTIAL'


def token_binding(**changes):
    receipt = {'id': 77, 'owner_id': 19, 'token': SYNTHETIC_API_TOKEN, **changes}
    return module().ProvisionedTokenBinding.from_creation_receipt(receipt, expected_bot_id='19')


def test_token_binding_retains_only_ids_and_exact_digest_not_secret():
    value = token_binding()
    assert asdict(value) == {'owner_id': '19', 'token_id': '77',
        'token_sha256': hashlib.sha256(SYNTHETIC_API_TOKEN.encode('ascii')).hexdigest()}
    assert SYNTHETIC_API_TOKEN not in repr(value)
    with pytest.raises(AttributeError):
        value.owner_id = '29'


@pytest.mark.parametrize('change', [
    {'owner_id': 29}, {'owner_id': True}, {'owner_id': '19'}, {'owner_id': 19.0},
    {'id': True}, {'id': '77'}, {'id': 0}, {'id': 2 ** 63},
    {'token': None}, {'token': ''}, {'token': 'not-an-api-token'}, {'token': 'tk_'},
    {'token': SYNTHETIC_API_TOKEN + ' '}, {'token': SYNTHETIC_API_TOKEN + '\n'},
])
def test_untrusted_or_malformed_creation_receipt_cannot_bind_token(change):
    with pytest.raises(module().VikunjaError) as failed:
        token_binding(**change)
    assert SYNTHETIC_API_TOKEN not in str(failed.value)


@pytest.mark.parametrize('changes', [
    {'owner_id': '019'}, {'token_id': '0'}, {'token_sha256': 'x' * 64},
    {'token_sha256': 'A' * 64}, {'token_sha256': b'a' * 64},
])
def test_loaded_provisioning_binding_fields_are_strict(changes):
    values = {'owner_id': '19', 'token_id': '77', 'token_sha256': 'a' * 64, **changes}
    with pytest.raises(module().VikunjaError):
        module().ProvisionedTokenBinding(**values)


def test_api_token_requires_provisioned_owner_attestation_before_any_http():
    with pytest.raises(module().VikunjaError, match='provisioned_token_binding_required'):
        case(headers={'Authorization': 'Bearer ' + SYNTHETIC_API_TOKEN})


@pytest.mark.parametrize('mismatch', ['owner', 'digest', 'session-substitution'])
def test_receipt_binding_must_match_exact_bearer_and_project_bot(mismatch):
    api = module()
    binding = token_binding()
    token = SYNTHETIC_API_TOKEN
    if mismatch == 'owner':
        binding = api.ProvisionedTokenBinding('29', binding.token_id, binding.token_sha256)
    elif mismatch == 'digest':
        binding = api.ProvisionedTokenBinding(binding.owner_id, binding.token_id, 'a' * 64)
    else:
        token = 'SYNTHETIC_SESSION_TOKEN'
    with pytest.raises(api.VikunjaError):
        case(headers={'Authorization': 'Bearer ' + token}, credential_binding=binding)


def test_api_token_checks_all_direct_membership_pages_without_calling_user_endpoint():
    def override(request):
        if request.url.path == '/api/v2/projects/7/users':
            number = int(request.url.params['page'])
            row = {'id': 29, 'permission': 1} if number == 1 else {
                'id': 19, 'bot_owner_id': 1, 'permission': 2, 'username': 'exact-id-wins'}
            return httpx.Response(200, json=page([row], number=number, total=2, size=1))
        if request.url.path == '/api/v2/user':
            raise AssertionError('API token must not call unsupported /user')
    c = case(override=override, headers={'Authorization': 'Bearer ' + SYNTHETIC_API_TOKEN},
             credential_binding=token_binding())
    c.adapter.validate_binding()
    users = [r for r in c.calls if r.url.path == '/api/v2/projects/7/users']
    assert len(users) == 2 and all('q' not in r.url.params for r in users)


@pytest.mark.parametrize('row', [
    {'id': 29, 'bot_owner_id': 1, 'permission': 1},
    {'id': 19, 'bot_owner_id': 0, 'permission': 1},
    {'id': 19, 'bot_owner_id': True, 'permission': 1},
    {'id': 19, 'permission': 1},
    *[{'id': 19, 'bot_owner_id': 1, 'permission': p} for p in (None, 0, 3, True, '1', 1.0)],
])
def test_api_token_bot_must_have_direct_write_membership_with_exact_integer_fields(row):
    c = case(override=lambda r: httpx.Response(200, json=page([row]))
        if r.url.path == '/api/v2/projects/7/users' else None,
        headers={'Authorization': 'Bearer ' + SYNTHETIC_API_TOKEN}, credential_binding=token_binding())
    with pytest.raises(c.api.VikunjaError):
        c.adapter.validate_binding()
    assert len(c.calls) == 1


@pytest.mark.parametrize('new_token', ['tk_SYNTHETIC_DIFFERENT', 'SYNTHETIC_SESSION_TOKEN'])
def test_bearer_replacement_invalidates_previously_validated_binding(new_token):
    c = case(headers={'Authorization': 'Bearer ' + SYNTHETIC_API_TOKEN}, credential_binding=token_binding())
    c.adapter.validate_binding()
    before = len(c.calls)
    c.http.headers['Authorization'] = 'Bearer ' + new_token
    with pytest.raises(c.api.VikunjaError):
        c.adapter.observe_task(str(TASK))
    with pytest.raises(c.api.VikunjaError):
        c.adapter.apply_task_change(str(TASK), {'done': False})
    assert len(c.calls) == before


def test_direct_membership_success_cannot_replace_missing_token_owner_attestation():
    calls = []
    with pytest.raises(module().VikunjaError):
        case(override=lambda r: calls.append(r), headers={'Authorization': 'Bearer ' + SYNTHETIC_API_TOKEN})
    assert calls == []


def test_comment_protocol_envelope_may_use_up_to_5000_literal_characters():
    c = case(override=lambda r: httpx.Response(201, json={'id': 401,
        'comment': json.loads(r.content)['comment'], 'author': {'id': 19}}) if r.method == 'POST' else None)
    c.adapter.validate_binding()
    assert c.adapter.add_comment(str(TASK), 'я' * 5000) == '401'


@pytest.mark.parametrize('done_bucket', [205, True, None, '0', 0.0])
def test_other_kanban_view_done_automation_blocks_all_mutations(done_bucket):
    def override(request):
        if request.url.path == '/api/v2/projects/7/views':
            return httpx.Response(200, json=page([
                {'id': 16, 'project_id': 7, 'view_kind': 'kanban', 'done_bucket_id': done_bucket},
                {'id': 17, 'project_id': 7, 'view_kind': 'kanban', 'bucket_configuration_mode': 'manual',
                 'done_bucket_id': 0, 'default_bucket_id': 201}]))
    c = case(override=override)
    with pytest.raises(c.api.VikunjaError):
        c.adapter.validate_binding()
    with pytest.raises(c.api.VikunjaError, match='binding_not_validated'):
        c.adapter.apply_task_change(str(TASK), {'done': True})
    assert all(request.method == 'GET' for request in c.calls)


def test_complete_view_scan_checks_later_page_automation_before_mutation():
    def override(request):
        if request.url.path == '/api/v2/projects/7/views':
            number = int(request.url.params['page'])
            rows = {1: {'id': 17, 'project_id': 7, 'view_kind': 'kanban',
                        'bucket_configuration_mode': 'manual', 'done_bucket_id': 0, 'default_bucket_id': 201},
                    2: {'id': 18, 'project_id': 7, 'view_kind': 'kanban', 'done_bucket_id': 250}}
            return httpx.Response(200, json=page([rows[number]], number=number, total=2, size=1))
    c = case(override=override)
    with pytest.raises(c.api.VikunjaError):
        c.adapter.validate_binding()
    assert len([r for r in c.calls if r.url.path == '/api/v2/projects/7/views']) == 2


@pytest.mark.parametrize('rows', [
    [{'id': 18, 'project_id': 7, 'view_kind': 'list'}],
    [{'id': 17, 'project_id': 8, 'view_kind': 'kanban', 'done_bucket_id': 0}],
    [{'id': 17, 'project_id': 7, 'view_kind': 'list'}],
    [{'id': 17, 'project_id': 7, 'view_kind': 'kanban', 'done_bucket_id': 0,
      'bucket_configuration_mode': 'filter', 'default_bucket_id': 201}],
])
def test_views_collection_must_contain_same_configured_manual_view(rows):
    c = case(override=lambda r: httpx.Response(200, json=page(rows))
        if r.url.path == '/api/v2/projects/7/views' else None)
    with pytest.raises(c.api.VikunjaError):
        c.adapter.validate_binding()
