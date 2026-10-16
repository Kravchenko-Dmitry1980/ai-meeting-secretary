"""Explicit local summary continuation: immutable uncertainty and synthetic HTTP."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.domain.models import TranscriptSegment
from secretary.infrastructure.database import Database, now
from secretary.infrastructure.polza import PolzaClient
from secretary.settings import Settings
from test_backend import prepared
from test_summary_rate_retry import successful_response


@pytest.fixture
def case(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in summary continuation tests")
    async def denied_async(*args, **kwargs):
        denied()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied_async)
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
        polza_api_key="synthetic-summary-resume-only", cloud_enabled=True,
        local_cost_limits_enabled=False, summary_batch_chars=5000)
    sent = []
    def handler(request):
        assert request.method == "POST" and request.url.path == "/api/v1/chat/completions"
        sent.append(json.loads(request.content))
        return successful_response(f"gen_synthetic_resume_{len(sent)}")
    app = create_app(settings, capture=SimpleNamespace(close=lambda: None), run_worker=False,
        provider_factory=lambda config: PolzaClient(config, httpx.MockTransport(handler)))
    with TestClient(app, base_url="http://127.0.0.1:8765") as api:
        db, worker = app.state.db, app.state.worker
        meeting, chunk = prepared(db, settings)
        version = db.ensure_version(meeting["id"])
        stt = db.enqueue(meeting["id"], "transcribe", chunk_id=chunk["id"], version=version)
        segments = [TranscriptSegment(id=f"synthetic-source-{i}", meeting_id=meeting["id"],
            chunk_id=chunk["id"], transcript_version=version, ordinal=i,
            text=letter * 1500, channel="import").model_dump()
            for i, letter in enumerate(("Я", "Б"))]
        db.save_segments(stt, segments, [])
        source = worker.enqueue_summary(meeting["id"])
        db.finish_job(source["id"], "uncertain", "Synthetic response and cost unknown")
        db.update_meeting(meeting["id"], status="partial_error", error="Synthetic uncertainty")
        headers = {"X-Secretary-Token": api.get("/api/v1/session").json()["csrf_token"]}
        yield SimpleNamespace(api=api, db=db, worker=worker, settings=settings, meeting=meeting,
            source=source, version=version, sent=sent, app=app, headers=headers,
            url=f"/api/v1/meetings/{meeting['id']}/summary-resume")


def command(case, **changes):
    return {"source_job_id": case.source["id"], "transcript_version": case.version,
            "operation_id": str(uuid4()), "acknowledge_possible_duplicate_charge": True, **changes}


def resume(case, body=None):
    return case.api.post(case.url, headers=case.headers, json=body or command(case))


@pytest.mark.parametrize("ack", [False, None, 0, 1, "true", "false"])
def test_acknowledgement_is_strict_true(case, ack):
    response = resume(case, command(case, acknowledge_possible_duplicate_charge=ack))
    assert response.status_code == 422
    assert len(case.db.jobs(case.meeting["id"])) == 2
    assert case.sent == []


def test_missing_ack_bad_uuid_and_csrf_cannot_create_continuation(case):
    missing = command(case); missing.pop("acknowledge_possible_duplicate_charge")
    assert resume(case, missing).status_code == 422
    assert resume(case, command(case, operation_id="not-a-uuid")).status_code == 422
    assert case.api.post(case.url, json=command(case)).status_code == 403
    assert case.api.post(case.url, headers={**case.headers, "Origin": "https://evil.example"},
                         json=command(case)).status_code == 403
    assert len(case.db.jobs(case.meeting["id"])) == 2


def test_child_is_atomic_idempotent_and_source_and_usage_are_unchanged(case):
    from secretary.domain.cloud_budget import CloudCharge
    from test_monthly_budget import ledger
    budget = ledger(case.settings.data_dir, key=hashlib.sha256(b"synthetic-summary-resume-only").hexdigest())
    liability = CloudCharge(str(uuid4()), "meeting_summary", "a" * 64, 1000000, 1000000,
                            meeting_id=case.meeting["id"])
    budget.reserve(liability)
    budget.mark_submitted(liability.operation_id)
    budget.mark_uncertain(liability.operation_id)
    with budget.repository.transaction() as conn:
        billing_before = [dict(row) for row in conn.execute("SELECT * FROM billing_charges")]
    reservation = case.db.reserve(case.source, None, 100, True, 1, enforce_limits=False)
    case.db.settle(reservation)
    before = case.db.job(case.source["id"], internal=True)
    usage = case.db.rows("SELECT * FROM usage")
    body = command(case)
    first = resume(case, body)
    assert first.status_code == 202
    child_id = first.json()["job_id"]
    assert first.json()["source_job_id"] == case.source["id"]
    assert resume(case, body).json() == first.json()
    assert resume(case, command(case)).status_code == 409
    assert resume(case, {**body, "transcript_version": case.version + 1}).status_code == 409
    child = case.db.job(child_id, internal=True)
    assert child["stage"] == "summarize" and child["status"] == "queued"
    assert child["payload"] == before["payload"]
    assert case.db.job(child_id)["resumed_from_job_id"] == case.source["id"]
    assert case.db.job(case.source["id"], internal=True) == before
    assert case.db.rows("SELECT * FROM usage") == usage
    with budget.repository.transaction() as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM billing_charges")] == billing_before
    assert case.db.one("SELECT COUNT(*) AS n FROM summary_resume_operations")["n"] == 1
    assert case.db.one("SELECT COUNT(*) AS n FROM jobs WHERE stage='transcribe'")["n"] == 1
    assert case.sent == []


@pytest.mark.parametrize("change", ["foreign_meeting", "stale_version", "wrong_stage", "not_uncertain", "context", "model", "recording"])
def test_conflicting_source_or_current_context_cannot_create_child(case, change):
    body = command(case)
    if change == "foreign_meeting":
        other = case.db.create_meeting("Other synthetic meeting")
        case.url = f"/api/v1/meetings/{other['id']}/summary-resume"
    elif change == "stale_version":
        case.db.update_meeting(case.meeting["id"], transcript_version=case.version + 1)
    elif change == "wrong_stage":
        case.db.execute("UPDATE jobs SET stage='transcribe' WHERE id=?", (case.source["id"],))
    elif change == "not_uncertain":
        case.db.finish_job(case.source["id"], "failed")
    elif change == "context":
        case.db.execute("UPDATE segments SET text='Changed synthetic evidence' WHERE meeting_id=?", (case.meeting["id"],))
    elif change == "model":
        case.worker.settings.summary_model = "synthetic/other-model"
    elif change == "recording":
        case.db.update_meeting(case.meeting["id"], recording=1)
    assert resume(case, body).status_code == 409
    assert case.db.one("SELECT COUNT(*) AS n FROM summary_resume_operations")["n"] == 0
    assert case.sent == []


def test_ordinary_retry_and_cancel_preserve_uncertain_guard(case):
    for retry in (False, True):
        result = case.api.post(f"/api/v1/meetings/{case.meeting['id']}/process", headers=case.headers,
                               json={"stage": "summarize", "retry": retry})
        assert result.status_code == 409
    assert case.db.cancel(case.source["id"])["status"] == "uncertain"
    assert case.sent == []


def test_parallel_equal_and_different_commands_create_only_one_child(case):
    from secretary.api import ResumeSummaryRequest
    body = command(case)
    def invoke(value):
        try:
            return case.worker.resume_uncertain_summary(case.meeting["id"], ResumeSummaryRequest.model_validate(value))["id"]
        except ValueError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=4) as pool:
        same = list(pool.map(invoke, [body] * 4))
    assert len(set(same)) == 1 and same[0] != "conflict"
    with ThreadPoolExecutor(max_workers=4) as pool:
        other = list(pool.map(invoke, [command(case) for _ in range(4)]))
    assert other == ["conflict"] * 4
    assert case.db.one("SELECT COUNT(*) AS n FROM summary_resume_operations")["n"] == 1


def test_parallel_competing_new_operations_have_only_one_winner(case):
    from secretary.api import ResumeSummaryRequest
    def invoke(body):
        try:
            return case.worker.resume_uncertain_summary(case.meeting["id"], ResumeSummaryRequest.model_validate(body))["id"]
        except ValueError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(invoke, [command(case) for _ in range(4)]))
    assert results.count("conflict") == 3
    assert case.db.one("SELECT COUNT(*) AS n FROM summary_resume_operations")["n"] == 1


def test_missing_source_is_not_found_and_other_active_work_blocks_continuation(case):
    assert resume(case, command(case, source_job_id=str(uuid4()))).status_code == 404
    case.db.enqueue(case.meeting["id"], "prepare", version=0)
    assert resume(case).status_code == 409
    assert case.db.rows("SELECT * FROM summary_resume_operations") == []


def test_transcription_unique_guard_is_unchanged(case):
    original = case.db.one("SELECT * FROM jobs WHERE stage='transcribe'")
    statement = """INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,chunk_id,version)
        VALUES(?,?,'transcribe',?,?,?,?,?)"""
    with case.db.transaction() as conn:
        conn.execute(statement, (str(uuid4()),case.meeting["id"],"uncertain",now(),now(),original["chunk_id"],case.version))
    with pytest.raises(sqlite3.IntegrityError):
        case.db.execute(statement, (str(uuid4()),case.meeting["id"],"queued",now(),now(),original["chunk_id"],case.version))


def test_direct_insert_and_update_cannot_bypass_unresolved_leaf(case):
    def insert(conn, job_id, status):
        conn.execute("INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload) VALUES(?,?,'summarize',?,?,?,?,?)",
            (job_id, case.meeting["id"], status, now(), now(), case.version, case.source["payload"]))
    with pytest.raises(sqlite3.IntegrityError):
        with case.db.transaction() as conn:
            insert(conn, str(uuid4()), "queued")
    dormant_id = str(uuid4())
    with case.db.transaction() as conn:
        insert(conn, dormant_id, "failed")
    with pytest.raises(sqlite3.IntegrityError):
        case.db.execute("UPDATE jobs SET status='queued' WHERE id=?", (dormant_id,))


def test_child_can_become_uncertain_and_requires_its_own_acknowledged_command(case):
    first = resume(case)
    assert first.status_code == 202
    child = first.json()["job_id"]
    case.db.finish_job(child, "uncertain", "Synthetic child uncertainty")
    assert case.worker.enqueue_summary(case.meeting["id"], retry=True)["id"] == child
    second = resume(case, command(case, source_job_id=child))
    assert second.status_code == 202 and second.json()["job_id"] != child
    assert case.db.job(child)["status"] == "uncertain"
    counts = case.api.get("/ready").json()["jobs"]["counts"]
    assert counts.get("uncertain", 0) == 0 and counts["queued"] == 1


def test_ordinary_child_resume_follows_lineage_when_parent_timestamp_sorts_later(case):
    case.db.execute("UPDATE jobs SET created_at='2099-01-01T00:00:00+00:00' WHERE id=?", (case.source["id"],))
    child = resume(case).json()["job_id"]
    case.db.finish_job(child, "failed", "Synthetic known terminal failure")
    selected = case.worker.enqueue_summary(case.meeting["id"], retry=True)
    assert selected["id"] == child and selected["status"] == "queued"
    assert case.db.job(case.source["id"])["status"] == "uncertain"


def test_completed_child_reuses_paid_checkpoint_without_stt_or_source_ledger_changes(case):
    from secretary.domain.summary_context import FrozenSummaryContext
    from secretary.infrastructure.summary_context_payloads import map_batches
    context = FrozenSummaryContext.model_validate(json.loads(case.source["payload"])["summary_context"])
    batches = map_batches(context, case.settings.summary_batch_chars)
    assert len(batches) == 2
    provider = PolzaClient(case.settings, httpx.MockTransport(lambda request: successful_response("gen_paid_cached")))
    checkpoints = {}
    first_sources = {item["id"]: item["text"] for item in batches[0]}
    asyncio.run(provider._summary_call(batches[0], first_sources, merge=False, receipts=[], on_usage=None,
        before_request=None, checkpoint_get=lambda key: None,
        checkpoint_put=lambda key, value: checkpoints.setdefault(key, value), context=context))
    for key, value in checkpoints.items():
        case.db.execute("INSERT INTO summary_checkpoints VALUES(?,?,?)", (case.source["id"], key, json.dumps(value)))
    old_usage = case.db.rows("SELECT * FROM usage WHERE job_id=?", (case.source["id"],))
    first = resume(case)
    assert first.status_code == 202
    child_id = first.json()["job_id"]
    assert case.db.rows("SELECT key,payload FROM summary_checkpoints WHERE job_id=?", (child_id,)) == case.db.rows(
        "SELECT key,payload FROM summary_checkpoints WHERE job_id=?", (case.source["id"],))
    assert asyncio.run(case.worker.run_once())
    assert case.db.job(child_id)["status"] == "succeeded"
    assert case.db.job(case.source["id"])["status"] == "uncertain"
    assert case.db.meeting(case.meeting["id"])["status"] == "ready"
    assert case.db.rows("SELECT * FROM usage WHERE job_id=?", (case.source["id"],)) == old_usage
    assert len(case.sent) == 2  # The missing map and merge; first paid map is reused.
    assert case.db.one("SELECT COUNT(*) AS n FROM jobs WHERE stage='transcribe'")["n"] == 1
    assert case.api.get("/ready").json()["jobs"]["counts"].get("uncertain", 0) == 0
    regenerated = case.worker.enqueue_summary(case.meeting["id"], retry=True)
    assert regenerated["id"] != child_id and regenerated["status"] == "queued"


def test_hard_offline_process_rejects_continuation_before_creating_work(case):
    offline_settings = case.settings.model_copy(update={"data_dir": case.settings.data_dir.parent / "offline-data"})
    app = create_app(offline_settings, capture=SimpleNamespace(close=lambda: None), run_worker=False,
                     outbound_enabled=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as api:
        headers = {"X-Secretary-Token": api.get("/api/v1/session").json()["csrf_token"]}
        response = api.post(case.url, headers=headers, json=command(case))
        assert response.status_code == 409
        assert response.json()["detail"] == "runtime_outbound_disabled"
        assert app.state.db.rows("SELECT * FROM summary_resume_operations") == []


@pytest.mark.parametrize("change", ["lineage_update", "lineage_delete", "source_status", "child_scope"])
def test_durable_processing_lineage_cannot_be_rewritten(case, change):
    child = resume(case).json()["job_id"]
    with pytest.raises(sqlite3.IntegrityError):
        if change == "lineage_update":
            case.db.execute("UPDATE summary_resume_operations SET command_hash=?", ("f" * 64,))
        elif change == "lineage_delete":
            case.db.execute("DELETE FROM summary_resume_operations")
        elif change == "source_status":
            case.db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (case.source["id"],))
        else:
            case.db.execute("UPDATE jobs SET version=version+1 WHERE id=?", (child,))
    assert case.db.job(case.source["id"])["status"] == "uncertain"


def test_migration_ten_is_atomic_preserves_schema_nine_and_repeats_safely(tmp_path, monkeypatch):
    import secretary.infrastructure.summary_resume_repository as module
    original_migrate, original_statements = module.migrate_summary_resumes, module.SUMMARY_RESUME_MIGRATION
    monkeypatch.setattr(module, "migrate_summary_resumes", lambda db: None)
    path = tmp_path / "legacy.sqlite3"
    old = Database(path)
    meeting = old.create_meeting("Synthetic schema nine")
    source = old.enqueue(meeting["id"], "summarize", version=1)
    old.finish_job(source["id"], "uncertain", "Preserve historical request")
    before = old.rows("SELECT * FROM jobs")
    old_index = old.one("SELECT sql FROM sqlite_master WHERE name='jobs_one_active'")
    monkeypatch.setattr(module, "migrate_summary_resumes", original_migrate)
    monkeypatch.setattr(module, "SUMMARY_RESUME_MIGRATION", (*original_statements, "invalid migration statement"))
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    assert old.rows("SELECT * FROM jobs") == before
    assert old.one("SELECT sql FROM sqlite_master WHERE name='jobs_one_active'") == old_index
    assert old.one("SELECT MAX(version) AS version FROM schema_migrations")["version"] == 9
    assert old.one("SELECT name FROM sqlite_master WHERE name='summary_resume_operations'") is None
    monkeypatch.setattr(module, "SUMMARY_RESUME_MIGRATION", original_statements)
    migrated = Database(path)
    assert migrated.rows("SELECT * FROM jobs") == before
    assert migrated.one("SELECT MAX(version) AS version FROM schema_migrations")["version"] == 10
    assert Database(path).rows("SELECT * FROM summary_resume_operations") == []


@pytest.mark.parametrize("malformed", ["attempts", "cancel", "provider_id", "payload", "missing_child"])
def test_deferred_lineage_rejects_malformed_fresh_child_and_rolls_back(case, malformed):
    context_hash = json.loads(case.source["payload"])["summary_context"]["context_hash"]
    child_id = str(uuid4())
    with pytest.raises(sqlite3.IntegrityError):
        with case.db.transaction() as conn:
            conn.execute("INSERT INTO summary_resume_operations VALUES(?,?,?,?,?,?,?,?,?)", (
                str(uuid4()),case.meeting["id"],case.version,case.source["id"],child_id,"a"*64,context_hash,1,now()))
            if malformed != "missing_child":
                conn.execute("""INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload,
                    attempts,cancel_requested,provider_job_id) VALUES(?,?,'summarize','queued',?,?,?,?,?,?,?)""", (
                    child_id,case.meeting["id"],now(),now(),case.version,
                    '{}' if malformed == "payload" else case.source["payload"],
                    1 if malformed == "attempts" else 0,1 if malformed == "cancel" else 0,
                    "gen_injected" if malformed == "provider_id" else None))
    assert case.db.rows("SELECT * FROM summary_resume_operations") == []
    assert case.db.job(case.source["id"])["status"] == "uncertain"
