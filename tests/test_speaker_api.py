"""New routes inherit localhost/Origin/CSRF, with isolated offline factories."""
from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.infrastructure.database import uid
from secretary.settings import Settings


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Cloud/provider calls forbidden in speaker API tests')
    async def denied_async(*args, **kwargs):
        denied()
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', denied_async)
    monkeypatch.setattr(tempfile, 'tempdir', tempfile.tempdir)
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / 'data',
                        polza_api_key='', cloud_enabled=False, local_cost_limits_enabled=False)
    app = create_app(settings, provider_factory=denied, capture=SimpleNamespace(close=lambda: None), run_worker=False)
    with TestClient(app, base_url='http://127.0.0.1:8765') as api:
        headers = {'X-Secretary-Token': api.get('/api/v1/session').json()['csrf_token']}
        db = app.state.db
        meeting = db.create_meeting('Synthetic meeting')['id']
        db.update_meeting(meeting, transcript_version=1)
        chunk = db.add_chunk(meeting, {'sequence': 0, 'channel': 'microphone', 'path': 'synthetic-unused',
            'offset_ms': 0, 'duration_ms': 1000, 'sha256': 'synthetic'})['id']
        db.execute('INSERT INTO speakers VALUES(?,?,?,?,NULL)', ('raw', meeting, chunk, 'SPEAKER_01'))
        for ordinal, segment in enumerate(('segment', 'neighbor')):
            db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (segment, meeting, chunk, 1, ordinal,
                ordinal * 500, (ordinal + 1) * 500, 'segment', 'Synthetic private speech', .99, 'raw', 'microphone'))
        db.execute('INSERT INTO summaries VALUES(?,?,?,?)', (meeting, 1, 1, json.dumps({'action_items': []})))
        profile = api.post('/api/v1/participants', headers=headers,
                           json={'display_name': 'Synthetic Alex', 'aliases': ['A'], 'operation_id': uid()}).json()
        roster = api.post(f'/api/v1/meetings/{meeting}/participants', headers=headers,
                         json={'person_profile_id': profile['id'], 'expected_roster_revision': 0, 'operation_id': uid()}).json()
        yield SimpleNamespace(api=api, db=db, headers=headers, meeting=meeting,
                              profile=profile, participant=roster['participants'][0], app=app)


def review_body(ctx, **edits):
    return {'transcript_version': 1, 'expected_revision': 0, 'operation_id': uid(),
            'changes': [{'segment_id': 'segment', 'participant_id': ctx.participant['id']}], **edits}


def test_profiles_roster_manual_identity_legacy_speakers_and_readonly_get(ctx):
    api, base = ctx.api, f'/api/v1/meetings/{ctx.meeting}'
    before = ctx.db.rows('SELECT * FROM segments')
    untouched = api.get(base + '/attribution').json()
    assert untouched['revision'] == 0 and untouched['roster_revision'] == 1
    assert all(item['raw_score'] is None and item['participant_id'] is None for item in untouched['items'])
    assert api.get('/api/v1/participants').json() == [ctx.profile]
    assert api.get(base + '/speakers').json()[0]['id'] == 'raw'
    command = review_body(ctx)
    first = api.patch(base + '/attribution', headers=ctx.headers, json=command)
    assert first.status_code == 200 and first.json()['revision'] == 1
    assert api.patch(base + '/attribution', headers=ctx.headers, json=command).json() == first.json()
    rejected = api.patch(base + '/attribution', headers=ctx.headers, json=review_body(ctx))
    assert rejected.status_code == 409
    patched = api.patch('/api/v1/participants/' + ctx.profile['id'], headers=ctx.headers,
                       json={'display_name': 'New name', 'aliases': ['New alias'], 'expected_revision': 0, 'operation_id': uid()})
    assert patched.status_code == 200 and patched.json()['revision'] == 1
    assert api.get(base + '/participants').json()['participants'][0]['display_name'] == 'Synthetic Alex'
    applied = api.patch(base + '/participants/' + ctx.participant['id'], headers=ctx.headers,
                       json={'apply_profile': True, 'expected_roster_revision': 1, 'operation_id': uid()})
    assert applied.status_code == 200 and applied.json()['roster_revision'] == 2
    assert applied.json()['participants'][0]['display_name'] == 'New name'
    assert ctx.db.rows('SELECT * FROM segments') == before
    assert ctx.db.rows('SELECT * FROM jobs') == ctx.db.rows('SELECT * FROM usage') == []
    assert api.get('/api/v1/config').json()['local_cost_limits_enabled'] is False
    assert ctx.app.state.settings.local_cost_limits_enabled is False
    assert ctx.db.rows('SELECT * FROM voice_enrollments') == []


def test_historical_speakers_and_attribution_use_explicit_version(ctx):
    base = f'/api/v1/meetings/{ctx.meeting}'
    first = ctx.api.patch(base + '/attribution', headers=ctx.headers, json=review_body(ctx)).json()
    ctx.db.update_meeting(ctx.meeting, transcript_version=2)
    chunk = ctx.db.add_chunk(ctx.meeting, {'sequence': 1, 'channel': 'import', 'path': 'synthetic-unused2',
        'offset_ms': 1000, 'duration_ms': 1000, 'sha256': 'synthetic2'})['id']
    ctx.db.execute('INSERT INTO speakers VALUES(?,?,?,?,NULL)', ('raw2', ctx.meeting, chunk, 'SPEAKER_01'))
    ctx.db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', ('version2', ctx.meeting, chunk, 2, 0, 1000, 2000,
        'segment', 'Synthetic new speech', .95, 'raw2', 'import'))
    assert [speaker['id'] for speaker in ctx.api.get(base + '/speakers').json()] == ['raw2']
    assert [speaker['id'] for speaker in ctx.api.get(base + '/speakers?version=1').json()] == ['raw']
    assert ctx.api.get(base + '/attribution?version=1').json() == first
    assert ctx.api.get(base + '/attribution').json()['revision'] == 0
    assert ctx.api.patch(base + '/attribution', headers=ctx.headers, json=review_body(ctx, expected_revision=1)).status_code == 409
    assert ctx.api.get(base + '/attribution?version=99').status_code == 404


def mutation(ctx, kind):
    base = f'/api/v1/meetings/{ctx.meeting}'
    if kind == 'create_profile':
        return 'post', '/api/v1/participants', {'display_name': 'Another profile', 'operation_id': uid()}
    if kind == 'patch_profile':
        return 'patch', '/api/v1/participants/' + ctx.profile['id'], {'enabled': False, 'expected_revision': 0, 'operation_id': uid()}
    if kind == 'add_participant':
        return 'post', base + '/participants', {'display_name': 'Guest', 'expected_roster_revision': 1, 'operation_id': uid()}
    if kind == 'patch_participant':
        return 'patch', base + '/participants/' + ctx.participant['id'], {'enabled': False, 'expected_roster_revision': 1, 'operation_id': uid()}
    return 'patch', base + '/attribution', review_body(ctx)


@pytest.mark.parametrize('kind', ['create_profile', 'patch_profile', 'add_participant', 'patch_participant', 'attribution'])
@pytest.mark.parametrize('attack', ['no_csrf', 'external_host', 'external_origin', 'wrong_local_port'])
def test_all_mutations_security(ctx, kind, attack):
    method, url, body = mutation(ctx, kind)
    headers = dict(ctx.headers)
    if attack == 'no_csrf':
        headers.clear()
    elif attack == 'external_host':
        headers['Host'] = 'external.example'
    elif attack == 'external_origin':
        headers['Origin'] = 'https://external.example'
    else:
        headers['Origin'] = 'http://127.0.0.1:9999'
    before = ctx.db.rows('SELECT * FROM speaker_operations')
    assert ctx.api.request(method, url, headers=headers, json=body).status_code == 403
    assert ctx.db.rows('SELECT * FROM speaker_operations') == before
    allowed = ctx.api.request(method, url, headers={**ctx.headers, 'Origin': 'http://localhost:8765'}, json=body)
    assert allowed.status_code in {200, 201}


@pytest.mark.parametrize('field,value', [('expected_revision', True), ('expected_revision', '0'),
    ('expected_revision', -1), ('transcript_version', False), ('operation_id', ''), ('changes', []),
    ('changes', [{'segment_id': 'segment', 'participant_id': None}] * 2)])
def test_invalid_review_dto_422_atomic(ctx, field, value):
    response = ctx.api.patch(f'/api/v1/meetings/{ctx.meeting}/attribution', headers=ctx.headers,
                             json=review_body(ctx, **{field: value}))
    assert response.status_code == 422
    assert ctx.db.rows('SELECT * FROM attribution_runs') == []


def test_foreign_profile_participant_segment_and_unknown_resources(ctx):
    other_meeting = ctx.db.create_meeting('Synthetic other')['id']
    roster = ctx.api.post(f'/api/v1/meetings/{other_meeting}/participants', headers=ctx.headers,
                         json={'display_name': 'Guest', 'expected_roster_revision': 0, 'operation_id': uid()}).json()
    other_id = roster['participants'][0]['id']
    url = f'/api/v1/meetings/{ctx.meeting}/attribution'
    for bad_id in (ctx.profile['id'], other_id, 'missing'):
        body = review_body(ctx, changes=[{'segment_id': 'segment', 'participant_id': bad_id}])
        assert ctx.api.patch(url, headers=ctx.headers, json=body).status_code == 404
    body = review_body(ctx, changes=[{'segment_id': 'missing', 'participant_id': ctx.participant['id']}])
    assert ctx.api.patch(url, headers=ctx.headers, json=body).status_code == 404
    assert ctx.api.post(f'/api/v1/meetings/{ctx.meeting}/participants', headers=ctx.headers,
        json={'person_profile_id': 'missing', 'expected_roster_revision': 1, 'operation_id': uid()}).status_code == 404
    assert ctx.api.patch(f'/api/v1/meetings/{ctx.meeting}/participants/{other_id}', headers=ctx.headers,
        json={'enabled': True, 'expected_roster_revision': 1, 'operation_id': uid()}).status_code == 404
    assert ctx.api.get('/api/v1/meetings/missing/participants').status_code == 404
    assert ctx.api.get('/api/v1/meetings/missing/attribution').status_code == 404
    assert ctx.db.rows('SELECT * FROM attribution_runs') == []


def test_no_get_write_and_no_speech_in_audit_receipts(ctx):
    tables = ['person_profiles', 'voice_enrollments', 'meeting_participants', 'attribution_runs',
              'speaker_attributions', 'task_assignments', 'attribution_state', 'speaker_operations', 'speaker_audit']
    before = {table: ctx.db.rows(f'SELECT * FROM {table}') for table in tables}
    for url in ('/api/v1/participants', f'/api/v1/meetings/{ctx.meeting}/participants',
                f'/api/v1/meetings/{ctx.meeting}/attribution', f'/api/v1/meetings/{ctx.meeting}/speakers'):
        assert ctx.api.get(url).status_code == 200
    assert before == {table: ctx.db.rows(f'SELECT * FROM {table}') for table in tables}
    assert ctx.api.patch(f'/api/v1/meetings/{ctx.meeting}/attribution', headers=ctx.headers,
                         json=review_body(ctx)).status_code == 200
    metadata = json.dumps(ctx.db.rows('SELECT * FROM speaker_audit') + ctx.db.rows('SELECT * FROM speaker_operations'))
    assert 'Synthetic private speech' not in metadata
    assert 'polza_api_key' not in metadata


@pytest.mark.parametrize('kind,field', [
    ('create_profile', 'display_name'), ('create_profile', 'aliases'), ('create_profile', 'operation_id'),
    ('patch_profile', 'display_name'), ('patch_profile', 'aliases'), ('patch_profile', 'operation_id'),
    ('add_participant', 'display_name'), ('add_participant', 'aliases'), ('add_participant', 'person_profile_id'),
    ('patch_participant', 'display_name'), ('patch_participant', 'aliases'), ('patch_participant', 'operation_id'),
    ('attribution', 'operation_id'), ('attribution', 'segment_id'), ('attribution', 'participant_id'),
])
@pytest.mark.parametrize('bad', ['\ud800', '\udc00'])
def test_escaped_lone_surrogate_is_422_with_no_writes(ctx, kind, field, bad):
    method, url, body = mutation(ctx, kind)
    if field == 'aliases':
        body[field] = [bad]
    elif field in {'segment_id', 'participant_id'}:
        body['changes'][0][field] = bad
    else:
        body[field] = bad
    # Avoid httpx's client-side UTF-8 encoding: send valid ASCII JSON carrying
    # the escaped surrogate that the server JSON parser will decode.
    raw = json.dumps(body, ensure_ascii=True).encode('ascii')
    tables = ['person_profiles', 'meeting_participants', 'participant_roster_state',
              'attribution_runs', 'speaker_attributions', 'attribution_state',
              'speaker_operations', 'speaker_audit', 'jobs', 'usage']
    before = {table: ctx.db.rows(f'SELECT * FROM {table}') for table in tables}
    response = ctx.api.request(method, url, headers={**ctx.headers, 'Content-Type': 'application/json'}, content=raw)
    assert response.status_code == 422
    assert 'String must be valid UTF-8' in response.text
    assert all(set(error) == {'loc', 'msg', 'type'} for error in response.json()['detail'])
    assert json.dumps(bad)[1:-1] not in response.text
    assert before == {table: ctx.db.rows(f'SELECT * FROM {table}') for table in tables}
    _, _, valid_body = mutation(ctx, kind)
    accepted = ctx.api.request(method, url, headers=ctx.headers, json=valid_body)
    assert accepted.status_code in {200, 201}
    assert ctx.api.request(method, url, headers=ctx.headers, json=valid_body).json() == accepted.json()
    assert ctx.db.rows('SELECT * FROM jobs') == ctx.db.rows('SELECT * FROM usage') == []


def test_cyrillic_and_emoji_roundtrip_in_names_aliases_and_operation_ids(ctx):
    body = {'display_name': 'Алексей 🙂', 'aliases': ['Лёша 🚀'], 'operation_id': 'операция-🙂'}
    raw = json.dumps(body, ensure_ascii=True).encode('ascii')
    headers = {**ctx.headers, 'Content-Type': 'application/json'}
    first = ctx.api.post('/api/v1/participants', headers=headers, content=raw)
    assert first.status_code == 201
    assert first.json()['display_name'] == body['display_name']
    assert first.json()['aliases'] == body['aliases']
    assert ctx.api.post('/api/v1/participants', headers=headers, content=raw).json() == first.json()
    assert first.json() in ctx.api.get('/api/v1/participants').json()


def test_deep_invalid_aliases_return_422_without_validator_recursion(ctx):
    # Below Python JSON parser's nesting limit but deep enough to expose a
    # recursive pre-validation walk before aliases' strict string validation.
    raw = (b'{"display_name":"Synthetic deep","aliases":' + b'[' * 400 +
           b'"nested"' + b']' * 400 + b',"operation_id":"deep-aliases"}')
    before = ctx.db.rows('SELECT * FROM speaker_operations')
    response = ctx.api.post('/api/v1/participants', headers={**ctx.headers, 'Content-Type': 'application/json'}, content=raw)
    assert response.status_code == 422
    assert all(set(error) == {'loc', 'msg', 'type'} for error in response.json()['detail'])
    assert ctx.db.rows('SELECT * FROM speaker_operations') == before
