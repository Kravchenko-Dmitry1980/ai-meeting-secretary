"""T12 local control-DB evidence; no provider or owner data may be accessed."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import socket
import sqlite3
from types import SimpleNamespace
from uuid import uuid4
from urllib.parse import quote

import pytest

from secretary.domain.team import TeamForbidden, TeamMember
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository
from sqlite_test_paths import sqlite_path


MODULE = 'secretary.infrastructure.team_runtime_health'
OWN_URL = 'https://team.example/hooks/max'
TYPES = ('message_created', 'message_callback', 'bot_started')


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch, tmp_path):
    def forbidden_network(*args, **kwargs):
        raise AssertionError('Runtime health tests forbid all real network access')
    monkeypatch.setattr(socket, 'create_connection', forbidden_network)
    monkeypatch.setattr(socket.socket, 'connect', forbidden_network)
    original = sqlite3.connect
    def synthetic_only(database, *args, **kwargs):
        target = sqlite_path(database, uri=kwargs.get('uri', False))
        assert target.is_relative_to(tmp_path.resolve()), 'Only fresh pytest SQLite paths are allowed'
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', synthetic_only)


def api():
    assert importlib.util.find_spec(MODULE) is not None, 'T12 runtime health implementation missing'
    return importlib.import_module(MODULE).RuntimeHealthRepository


@pytest.mark.parametrize('basename', ['probe.sqlite3', 'probe space %25.sqlite3'])
def test_runtime_health_guard_accepts_local_readonly_uri(tmp_path, basename):
    path = tmp_path / basename
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('CREATE TABLE synthetic_evidence(value TEXT)')
        conn.execute("INSERT INTO synthetic_evidence VALUES('retain')")
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        assert conn.execute('SELECT value FROM synthetic_evidence').fetchone() == ('retain',)


def test_runtime_health_guard_still_denies_encoded_parent_escape(tmp_path):
    outside = tmp_path / '..' / ('outside-' + uuid4().hex + '.sqlite3')
    uri = 'file:' + quote(outside.as_posix(), safe='/:') + '?mode=ro'
    with pytest.raises(AssertionError, match='Only fresh pytest SQLite paths are allowed'):
        sqlite3.connect(uri, uri=True)
    assert not outside.exists()


def sample(tmp_path):
    cls = api()
    now = [datetime(2026, 10, 4, 12, tzinfo=timezone.utc)]
    team = TeamRepository(TeamDatabase(tmp_path / 'synthetic-team.sqlite3'), clock=lambda: now[0])
    owner = TeamMember(id=str(uuid4()), display_name='Synthetic owner', role='owner',
        max_user_id='11', vikunja_user_id='21', project_ids=('7',))
    member = TeamMember(id=str(uuid4()), display_name='Synthetic member',
        max_user_id='12', vikunja_user_id='22', project_ids=('7',))
    foreign = TeamMember(id=str(uuid4()), display_name='Synthetic foreign owner', role='owner',
        max_user_id='13', vikunja_user_id='23', project_ids=('8',))
    for value in (owner, member, foreign):
        team.upsert_member(value, expected_revision=None)
    path, deployment = tmp_path / 'synthetic-control.sqlite3', str(uuid4())
    repo = cls(path, deployment, team=team, clock=lambda: now[0])
    return SimpleNamespace(cls=cls, repo=repo, path=path, deployment=deployment, now=now,
        team=team, owner=owner, member=member, foreign=foreign)


def public(c, repo=None):
    return (repo or c.repo).read_webhook(c.owner.id, '7', expected_actor_revision=c.owner.revision)


def test_import_has_no_database_side_effect(monkeypatch):
    assert importlib.util.find_spec(MODULE) is not None, 'T12 runtime health implementation missing'
    def forbidden_database(*args, **kwargs):
        raise AssertionError('Import must not open any database')
    monkeypatch.setattr(sqlite3, 'connect', forbidden_database)
    importlib.reload(importlib.import_module(MODULE))


def test_configuration_is_local_evidence_and_public_status_is_redacted(tmp_path):
    c = sample(tmp_path)
    c.repo.configure_subscription(OWN_URL, TYPES)
    value = public(c).model_dump(mode='json')
    assert value['configured_subscription'] is True
    assert value['state'] == 'unknown'
    assert value['subscription_state'] is None
    assert value['last_subscription_observed_at'] is None
    assert value['last_trusted_callback_at'] is None
    assert value['phone_delivery'] == 'unknown'
    encoded = public(c).model_dump_json()
    assert all(secret not in encoded for secret in (OWN_URL, c.deployment, c.owner.id, 'sha256'))


def test_own_subscription_observation_survives_restart_and_foreign_url_is_rejected(tmp_path):
    c = sample(tmp_path)
    c.repo.configure_subscription(OWN_URL, TYPES)
    c.repo.record_subscription('present', observed_url=OWN_URL, update_types=TYPES)
    first = public(c)
    c.now[0] += timedelta(minutes=1)
    with pytest.raises(ValueError):
        c.repo.record_subscription('present', observed_url='https://foreign.example/hooks/max', update_types=TYPES)
    reopened = c.cls(c.path, c.deployment, team=c.team, clock=lambda: c.now[0])
    assert public(c, reopened) == first
    assert first.state == 'observed' and first.subscription_state == 'present'
    assert first.last_subscription_observed_at == datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    assert first.last_trusted_callback_at is None and first.phone_delivery == 'unknown'


def test_submitted_crash_blocks_new_post_even_after_get_reports_missing(tmp_path):
    c = sample(tmp_path)
    c.repo.configure_subscription(OWN_URL, TYPES)
    attempt = str(uuid4())
    c.repo.begin_subscription_attempt(attempt)
    c.repo.mark_subscription_submitted(attempt)
    reopened = c.cls(c.path, c.deployment, team=c.team, clock=lambda: c.now[0])
    reopened.record_subscription('missing', observed_url=OWN_URL)
    assert reopened.can_begin_subscription() is False
    with pytest.raises(ValueError):
        reopened.begin_subscription_attempt(str(uuid4()))
    with pytest.raises(ValueError):
        reopened.mark_subscription_submitted(attempt)
    status = reopened.read_status()
    assert status['registration_state'] in {'submitted', 'uncertain'}
    assert status['phone_delivery'] == 'unknown'


def test_uncertain_receipt_never_becomes_permission_for_another_registration(tmp_path):
    c = sample(tmp_path)
    c.repo.configure_subscription(OWN_URL, TYPES)
    attempt = str(uuid4())
    c.repo.begin_subscription_attempt(attempt)
    c.repo.mark_subscription_submitted(attempt)
    c.repo.complete_subscription_attempt(attempt, 'uncertain')
    c.repo.record_subscription('missing', observed_url=OWN_URL)
    assert c.repo.can_begin_subscription() is False
    with pytest.raises(ValueError):
        c.repo.begin_subscription_attempt(str(uuid4()))


def test_attempt_identity_and_terminal_receipt_are_immutable(tmp_path):
    c = sample(tmp_path)
    c.repo.configure_subscription(OWN_URL, TYPES)
    attempt = str(uuid4())
    c.repo.begin_subscription_attempt(attempt)
    c.repo.begin_subscription_attempt(attempt)
    c.repo.mark_subscription_submitted(attempt)
    c.repo.complete_subscription_attempt(attempt, 'verified')
    first = c.repo.read_status()
    c.now[0] += timedelta(minutes=1)
    c.repo.complete_subscription_attempt(attempt, 'verified')
    assert c.repo.read_status() == first
    with pytest.raises(ValueError):
        c.repo.complete_subscription_attempt(attempt, 'rejected')
    with pytest.raises(ValueError):
        c.repo.configure_subscription('https://changed.example/hooks/max', TYPES)


@pytest.mark.parametrize('stage', ['absent', 'prepared', 'submitted', 'verified'])
def test_exact_dead_registration_checkpoint_preserves_prepared_and_proven_receipt(tmp_path, stage):
    c = sample(tmp_path)
    c.repo.configure_subscription(OWN_URL, TYPES)
    operation = str(uuid4())
    if stage != 'absent':
        c.repo.begin_subscription_attempt(operation)
    if stage in {'submitted', 'verified'}:
        c.repo.mark_subscription_submitted(operation)
    if stage == 'verified':
        c.repo.complete_subscription_attempt(operation, 'verified')
    first = c.repo.recover_subscription_attempt(operation)
    c.now[0] += timedelta(minutes=1)
    assert c.repo.recover_subscription_attempt(operation) == first
    if stage == 'submitted':
        assert first == {'operation_id': operation, 'state': 'completed', 'receipt': 'uncertain'}
        assert c.repo.can_begin_subscription() is False
    elif stage == 'prepared':
        assert c.repo.prepared_attempt() == operation and first['receipt'] is None
    elif stage == 'verified':
        assert first['receipt'] == 'verified'
    else:
        assert first['state'] == 'absent'


def test_concurrent_submission_can_commit_only_once(tmp_path):
    c = sample(tmp_path)
    c.repo.configure_subscription(OWN_URL, TYPES)
    attempt = str(uuid4())
    c.repo.begin_subscription_attempt(attempt)
    def submit(_):
        try:
            c.repo.mark_subscription_submitted(attempt)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(submit, range(2))) == 1


def test_trusted_callback_is_durable_deduplicated_and_does_not_claim_phone_delivery(tmp_path):
    c = sample(tmp_path)
    event_id = str(uuid4())
    c.repo.record_callback(event_id, 'message_created')
    first = public(c)
    c.now[0] += timedelta(hours=1)
    c.repo.record_callback(event_id, 'message_created')
    reopened = c.cls(c.path, c.deployment, team=c.team, clock=lambda: c.now[0])
    assert public(c, reopened) == first
    assert first.last_trusted_callback_at == datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    assert first.phone_delivery == 'unknown'
    assert event_id not in first.model_dump_json()
    with pytest.raises(ValueError):
        c.repo.record_callback(event_id, 'message_callback')


@pytest.mark.parametrize('actor', ['member', 'foreign'])
def test_read_webhook_requires_current_owner_and_project(tmp_path, actor):
    c = sample(tmp_path)
    c.repo.record_callback(str(uuid4()), 'message_callback')
    with pytest.raises(TeamForbidden):
        c.repo.read_webhook(getattr(c, actor).id, '7', expected_actor_revision=0)


def test_read_webhook_rechecks_actor_revision(tmp_path):
    c = sample(tmp_path)
    c.team.upsert_member(c.owner.model_copy(update={'revision': 1, 'role': 'member'}), expected_revision=0)
    with pytest.raises(TeamForbidden):
        c.repo.read_webhook(c.owner.id, '7', expected_actor_revision=0)


def test_shared_control_database_keeps_deployment_observations_separate(tmp_path):
    c = sample(tmp_path)
    c.repo.configure_subscription(OWN_URL, TYPES)
    c.repo.record_subscription('present', observed_url=OWN_URL, update_types=TYPES)
    c.repo.record_callback(str(uuid4()), 'bot_started')
    other = c.cls(c.path, str(uuid4()), team=c.team, clock=lambda: c.now[0])
    value = public(c, other)
    assert value.last_subscription_observed_at is None
    assert value.last_trusted_callback_at is None
    assert value.phone_delivery == 'unknown'
