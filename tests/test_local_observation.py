"""P6 public collector/evaluator seams; all evidence and clocks are synthetic."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
import httpx
import sqlite3
import os
import json


def observer():
    path = Path(__file__).parents[1] / "scripts/local_observation.py"
    spec = importlib.util.spec_from_file_location("local_observation_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_empty_or_running_journal_never_proves_observation():
    module = observer()
    assert module.evaluate_records({"interval_seconds": 60, "max_gap_seconds": 90}, [], None)["status"] == "INCOMPLETE"


def candidate_module():
    spec = importlib.util.spec_from_file_location("local_candidate", Path(__file__).parents[1] / "scripts/local_candidate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_clean_candidate_binds_source_commit_and_frontend_content(tmp_path):
    module = candidate_module()
    dist = tmp_path / "frontend/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("synthetic page", encoding="utf-8")
    def git(arguments, **kwargs):
        output = str(tmp_path) if "--show-toplevel" in arguments else "a" * 40 + "\n" if "rev-parse" in arguments else ""
        return SimpleNamespace(returncode=0, stdout=output)
    before = module.capture_candidate(tmp_path, runner=git)
    assert before["state"] == "bound"
    assert before["git_sha"] == "a" * 40
    (dist / "index.html").write_text("different synthetic page", encoding="utf-8")
    assert module.capture_candidate(tmp_path, runner=git)["frontend_sha256"] != before["frontend_sha256"]


def good_sample(index):
    return {"sequence": index, "clock": {"utc": (datetime(2026, 10, 16, tzinfo=timezone.utc) + timedelta(seconds=index * 60)).isoformat(),
        "monotonic": 1000 + index * 60, "boot_id": "synthetic-boot"},
        "metrics": {"candidate": {"state": "bound", "git_sha": "a" * 40, "frontend_sha256": "b" * 64, "project": "synthetic"},
        "process": {"verified": True, "binding_verified": True, "identity_digest": "c" * 64},
        "health": "ok", "ready": "local_ok", "cloud": "disabled",
        "worker_age_seconds": 1, "worker_stale_after_seconds": 10,
        "queue_counts": {"succeeded": 40}, "stale_running": 0,
        "free_bytes": 9000000000, "minimum_free_bytes": 1000000000,
        "tasks": {"accepted_count": 3, "digest": "d" * 64},
        "database": {"usage_digest": "e" * 64, "billing_digest": "f" * 64, "jobs_digest": "1" * 64,
            "job_incidents": {},
            "usage_count": 261, "unknown_cost_count": 2, "confirmed_rub": 4.22, "estimated_rub": 4.5},
        "logs": {"gap": False, "new_errors": 0, "new_warnings": 0, "cursor_digest": "2" * 64},
        "backup": {"manifest_sha256": "3" * 64, "backup_id": "synthetic-backup", "excluded_directories": ["test-audio-tmp"],
            "age_seconds": 80000 + index * 60, "verified": index in (0, 1440)}}, "issues": []}


def policy():
    return {"interval_seconds": 60, "max_gap_seconds": 90, "candidate": good_sample(0)["metrics"]["candidate"],
        "accepted_tasks": [{"action_id": str(i)} for i in range(3)],
        "backup": {"manifest_sha256": "3" * 64, "excluded_directories": ["test-audio-tmp"]}}


def test_continuous_full_day_returns_observation_only_not_production():
    module = observer()
    samples = [good_sample(i) for i in range(1441)]
    report = module.evaluate_records(policy(), samples, {"state": "completed", "sample_count": 1441})
    assert report["status"] == "OBSERVATION_PASS"
    assert report["elapsed_seconds"] == 86400
    assert report["production_qualified"] is False
    assert report["unknown_cost_count"] == 2
    assert report["team"] == "N/A"


@pytest.mark.parametrize("response", [httpx.Response(302, headers={"location": "https://external.invalid"}),
    httpx.Response(200, content=b"not json"), httpx.Response(200, content=b"x" * (1024 * 1024 + 1))])
def test_loopback_reader_rejects_redirect_malformed_and_oversize(response):
    module = observer()
    seen = []
    def request(req):
        seen.append(str(req.url))
        return response
    with module.LoopbackReader("http://127.0.0.1:8765", transport=httpx.MockTransport(request)) as reader:
        with pytest.raises(module.ObservationError):
            reader.get("/health")
    assert seen == ["http://127.0.0.1:8765/health"]


def databases(path):
    from secretary.infrastructure.database import Database
    from secretary.infrastructure.budget_repository import BudgetRepository
    db = Database(path / "secretary.sqlite3")
    meeting = db.create_meeting("private meeting")
    job = db.enqueue(meeting["id"], "prepare")
    with db.transaction() as connection:
        connection.execute("INSERT INTO usage(id,meeting_id,job_id,kind,estimated_rub,status,created_at) VALUES(?,?,?,?,?,?,?)",
            ("u1", meeting["id"], job["id"], "stt", .2, "unknown", "date"))
    BudgetRepository(path / "billing.sqlite3")


def test_readonly_database_metrics_preserve_unknown_cost_without_private_text(tmp_path):
    module = observer()
    databases(tmp_path)
    before = module.read_database(tmp_path)
    assert before["unknown_cost_count"] == 1
    assert before["confirmed_rub"] == 0
    assert before["estimated_rub"] == 0.2
    assert "private meeting" not in str(before)
    with sqlite3.connect(tmp_path / "secretary.sqlite3") as db:
        db.execute("UPDATE jobs SET status='succeeded'")
    after = module.read_database(tmp_path)
    assert before["jobs_digest"] != after["jobs_digest"]
    assert before["usage_digest"] == after["usage_digest"]
    assert after["job_incidents"] == {}


def test_log_reader_counts_new_errors_across_rotation_and_detects_lost_cursor(tmp_path):
    module = observer()
    log = tmp_path / "secretary.error.log"
    log.write_bytes(b"old ERROR historical\n")
    baseline = module.read_log(log)
    assert baseline["new_errors"] == 0
    with log.open("ab") as stream:
        stream.write(b"new ERROR synthetic private text\n")
    os.rename(log, tmp_path / "secretary.error.log.1")
    log.write_bytes(b"new INFO current\n")
    result = module.read_log(log, baseline["cursor"])
    assert result["new_errors"] == 1
    assert result["gap"] is False
    assert "private text" not in str(result)
    os.rename(log, tmp_path / "lost.log")
    log.write_bytes(b"new unrelated file\n")
    assert module.read_log(log, result["cursor"])["gap"] is True


def test_short_explicit_observation_writes_terminal_incomplete_journal(tmp_path):
    module = observer()
    class Clock:
        seconds = 0
        def stamp(self):
            return {"utc": (datetime(2026, 10, 16, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)).isoformat(),
                "monotonic": 1000 + self.seconds, "boot_id": "synthetic"}
        def sleep(self, value):
            self.seconds += value
    class Probe:
        def sample(self, *, full_backup):
            metrics = good_sample(0)["metrics"]
            metrics["backup"]["verified"] = full_backup
            return metrics
    clock = Clock()
    result = module.observe(policy(), tmp_path / "observation", Probe(), clock=clock, duration_seconds=120)
    assert result["status"] == "INCOMPLETE"
    assert result["elapsed_seconds"] == 120
    assert module.evaluate_directory(tmp_path / "observation") == result
    assert (tmp_path / "observation/terminal.json").is_file()
    raw_journal = (tmp_path / "observation/samples.jsonl").read_bytes()
    assert b"\r\n" not in raw_journal  # writer byte cap must match bytes on Windows
    assert raw_journal.count(b"\n") == 3
    with pytest.raises(FileExistsError):
        module.observe(policy(), tmp_path / "observation", Probe(), clock=clock, duration_seconds=120)
    saved_config = json.loads((tmp_path / "observation/config.json").read_text())
    saved_config["accepted_tasks"][0]["action_id"] = "different-task"
    (tmp_path / "observation/config.json").write_text(json.dumps(saved_config))
    assert module.evaluate_directory(tmp_path / "observation")["status"] == "NEEDS_REVIEW"


def test_real_probe_collects_bounded_http_sql_and_log_metrics(tmp_path):
    module = observer()
    databases(tmp_path)
    runtime = tmp_path / "runtime"
    logs = runtime / "local-supervisor/run/logs"
    logs.mkdir(parents=True)
    (logs / "secretary.error.log").write_text("INFO fixture\n")
    for name in ("stdout", "stderr"):
        (runtime / name).write_bytes(b"")
    acceptance = tmp_path / "acceptance.json"
    acceptance.write_text('{"synthetic":true}')
    config = policy() | {"runtime_root": str(runtime), "data_dir": str(tmp_path),
        "owner_acceptance": {"path": str(acceptance), "sha256": module.file_hash(acceptance)}}
    config["accepted_tasks"] = [{"meeting_id": "meeting1", "transcript_version": 1, "summary_version": 1,
        "assignment_revision": 2, "action_id": f"task{i}"} for i in range(3)]
    owner = {"stdout_log": str(runtime / "stdout"), "stderr_log": str(runtime / "stderr"),
        "supervisor_run_id": "run", "candidate_binding_verified": True, "supervisor_identity": {"pid": 1},
        "attempt_identities": [{"pid": 2}], "listener_identity": {"pid": 2}, "configuration_sha256": "c" * 64, "run_id": "child"}
    snapshot = {"meeting_id": "meeting1", "transcript_version": 1, "summary_version": 1, "revision": 2,
        "items": [{"action": {"action_id": f"task{i}"}, "status": "confirmed", "participant_id": "person1", "basis": "manual", "reason_codes": []} for i in range(3)]}
    responses = {"/health": {"status": "ok"}, "/ready": {"status": "local_ok", "production_qualified": False, "blockers": [],
        "components": {"processing_worker": {"state": "healthy", "heartbeat_age_seconds": 1, "heartbeat_stale_after_seconds": 10},
            "cloud": {"state": "disabled"}, "storage": {"free_bytes": 9000000000, "minimum_free_bytes": 1000000000}},
        "jobs": {"state": "healthy", "counts": {"queued": 1}, "stale_running_candidate_count": 0}},
        "/api/v1/meetings/meeting1/task-assignments": snapshot,
        "/api/v1/meetings/meeting1/tasks": [{"id": f"task{i}", "assignment_status": "confirmed", "participant_id": "person1", "text": "private task"} for i in range(3)]}
    with module.LoopbackReader("http://127.0.0.1:8765", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=responses[request.url.path]))) as reader:
        probe = module.LocalProbe(config, reader=reader, process_reader=lambda _: owner,
            candidate_reader=lambda _: config["candidate"], backup_reader=lambda *args, **kwargs: good_sample(0)["metrics"]["backup"])
        metrics = probe.sample(full_backup=True)
        assert metrics["tasks"]["accepted_count"] == 3
        assert metrics["database"]["unknown_cost_count"] == 1
        assert metrics["queue_counts"] == {"queued": 1}
        assert "private task" not in str(metrics)
        responses["/api/v1/meetings/meeting1/tasks"].append({"id": "unrelated", "text": "changed unrelated task"})
        snapshot["revision"] = 3
        assert probe.sample(full_backup=False)["tasks"]["digest"] == metrics["tasks"]["digest"]
        responses["/api/v1/meetings/meeting1/tasks"][0]["text"] = "changed selected task"
        assert probe.sample(full_backup=False)["tasks"]["digest"] != metrics["tasks"]["digest"]
        responses["/api/v1/meetings/meeting1/tasks"][0]["assignment_status"] = "needs_review"
        with pytest.raises(module.ObservationError, match="accepted_task_missing_or_unconfirmed"):
            probe.sample(full_backup=False)


@pytest.mark.parametrize("change", ["short", "gap", "clock", "boot", "pid", "sha", "missing", "nan", "worker", "usage", "tasks", "backup", "log", "duplicate"])
def test_incomplete_or_changed_evidence_never_returns_pass(change):
    module = observer()
    samples = [good_sample(i) for i in range(1441)]
    sample = samples[10]
    if change == "short":
        samples = samples[:3]
    elif change == "gap":
        del samples[10]
        for i, record in enumerate(samples):
            record["sequence"] = i
    elif change == "clock":
        sample["clock"]["utc"] = "2026-10-18T00:00:00+00:00"
    elif change == "boot":
        sample["clock"]["boot_id"] = "other-boot"
    elif change == "pid":
        sample["metrics"]["process"]["identity_digest"] = "9" * 64
    elif change == "sha":
        sample["metrics"]["candidate"]["git_sha"] = "9" * 40
    elif change == "missing":
        del sample["metrics"]["free_bytes"]
    elif change == "nan":
        sample["metrics"]["worker_age_seconds"] = float("nan")
    elif change == "worker":
        sample["metrics"]["worker_age_seconds"] = 11
    elif change == "usage":
        sample["metrics"]["database"]["usage_digest"] = "9" * 64
    elif change == "tasks":
        sample["metrics"]["tasks"]["digest"] = "9" * 64
    elif change == "backup":
        sample["metrics"]["backup"]["excluded_directories"] = ["uploads"]
    elif change == "log":
        sample["metrics"]["logs"]["gap"] = True
    elif change == "duplicate":
        sample["sequence"] = 9
    assert module.evaluate_records(policy(), samples, {"state": "completed", "sample_count": len(samples)})["status"] != "OBSERVATION_PASS"


def test_normal_queue_activity_is_allowed_but_recurrent_incident_is_not():
    module = observer()
    samples = [good_sample(i) for i in range(1441)]
    for sample in samples[:3]:
        sample["metrics"]["queue_counts"] = {"running": 1}
        sample["metrics"]["database"]["jobs_digest"] = "9" * 64
    assert module.evaluate_records(policy(), samples, {"state": "completed", "sample_count": 1441})["status"] == "OBSERVATION_PASS"
    samples[0]["metrics"]["database"]["job_incidents"] = {"old": "failure"}
    samples[2]["metrics"]["database"]["job_incidents"] = {"old": "failure"}
    assert "new_job_incident" in module.evaluate_records(policy(), samples, {"state": "completed", "sample_count": 1441})["issues"]


def test_rotation_with_missing_intermediate_generation_is_a_gap(tmp_path):
    module = observer()
    log = tmp_path / "error.log"
    log.write_bytes(b"INFO baseline\n")
    first = module.read_log(log)
    os.rename(log, tmp_path / "error.log.2")
    log.write_bytes(b"INFO current\n")
    assert module.read_log(log, first["cursor"])["gap"] is True


def test_cost_aggregation_matches_usage_api_status_semantics(tmp_path):
    module = observer()
    databases(tmp_path)
    with sqlite3.connect(tmp_path / "secretary.sqlite3") as db:
        db.execute("DELETE FROM usage")
        for index, (status, estimate, confirmed) in enumerate([
                ("released", 10, None), ("reserved", 2, None), ("reserved", None, None),
                ("confirmed", 3, 4), ("unknown", 5, None)]):
            db.execute("INSERT INTO usage(id,meeting_id,job_id,kind,estimated_rub,confirmed_rub,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (str(index), "m", "j", "stt", estimate, confirmed, status, "date"))
    metrics = module.read_database(tmp_path)
    assert metrics["unknown_cost_count"] == 2
    assert metrics["estimated_rub"] == 10
    assert metrics["confirmed_rub"] == 4


@pytest.mark.parametrize("reason", ["dirty", "parent_root", "missing_git", "bound_exceeded"])
def test_candidate_never_binds_unknown_dirty_or_out_of_scope_source(tmp_path, reason):
    module = candidate_module()
    dist = tmp_path / "frontend/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("synthetic")
    if reason == "bound_exceeded":
        for i in range(512):
            (dist / str(i)).write_bytes(b"")
    def git(arguments, **kwargs):
        if reason == "missing_git":
            raise FileNotFoundError()
        output = (str(tmp_path.parent if reason == "parent_root" else tmp_path) if "--show-toplevel" in arguments else
            "a" * 40 if "rev-parse" in arguments else " M file.py" if reason == "dirty" else "")
        return SimpleNamespace(returncode=0, stdout=output)
    assert module.capture_candidate(tmp_path, runner=git)["state"] == "unbound"


def test_real_backup_verification_requires_exact_scope_and_receipt(tmp_path):
    from secretary.infrastructure.local_backup import create_backup, verify_backup
    module = observer()
    source = tmp_path / "data"
    source.mkdir()
    databases(source)
    archive = tmp_path / "backup"
    create_backup(source, archive)
    receipt_path = tmp_path / "verify.json"
    receipt = verify_backup(archive)
    receipt_path.write_text(json.dumps(receipt))
    config = {"data_dir": str(source), "backup": {"directory": str(archive),
        "manifest_sha256": module.file_hash(archive / "manifest.json"), "verify_receipt": str(receipt_path),
        "verify_receipt_sha256": module.file_hash(receipt_path), "excluded_directories": []}}
    assert module.read_backup(config, full_verify=True)["verified"] is True
    config["backup"]["excluded_directories"] = ["test-audio-tmp"]
    with pytest.raises(module.ObservationError, match="backup_scope_or_receipt_invalid"):
        module.read_backup(config, full_verify=True)
    config["backup"]["excluded_directories"] = []
    receipt["total_bytes"] += 1
    receipt_path.write_text(json.dumps(receipt))
    config["backup"]["verify_receipt_sha256"] = module.file_hash(receipt_path)
    with pytest.raises(module.ObservationError, match="backup_verification_failed"):
        module.read_backup(config, full_verify=True)


def test_writer_refuses_oversized_record_without_appending_or_deleting(tmp_path):
    module = observer()
    class Clock:
        def stamp(self):
            return good_sample(0)["clock"]
    class Probe:
        def sample(self, **_):
            return {"oversized": "x" * (1024 * 1024)}
    output = tmp_path / "observation"
    report = module.observe(policy(), output, Probe(), clock=Clock(), duration_seconds=1)
    assert report["status"] == "INTERRUPTED"
    assert "journal_bound_exceeded" in report["issues"]
    assert (output / "samples.jsonl").stat().st_size == 0
    assert (output / "terminal.json").is_file()


def test_collector_error_is_terminal_and_keeps_only_sanitized_reason(tmp_path):
    module = observer()
    class Clock:
        def stamp(self):
            return good_sample(0)["clock"]
    class Probe:
        def sample(self, **_):
            raise ValueError("private transcript must never enter report")
    report = module.observe(policy(), tmp_path / "observation", Probe(), clock=Clock(), duration_seconds=1)
    assert report["status"] == "INTERRUPTED"
    assert "collector_unavailable" in report["issues"]
    assert "private transcript" not in (tmp_path / "observation/samples.jsonl").read_text()


def test_changed_acceptance_receipt_cannot_change_final_or_replayed_decision(tmp_path):
    module = observer()
    receipt = tmp_path / "acceptance.json"
    receipt.write_text('{"synthetic":true}')
    config = policy() | {"owner_acceptance": {"path": str(receipt), "sha256": module.file_hash(receipt)}}
    class Clock:
        seconds = 0
        def stamp(self):
            return {"utc": (datetime(2026, 10, 16, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)).isoformat(),
                "monotonic": 1000 + self.seconds, "boot_id": "synthetic"}
        def sleep(self, seconds):
            self.seconds += seconds
    class Probe:
        count = 0
        change_on_final = False
        def sample(self, **_):
            self.count += 1
            if self.count == 2 and self.change_on_final:
                receipt.write_text('{"synthetic":false}')
            return good_sample(0)["metrics"]
    clean = module.observe(config, tmp_path / "clean", Probe(), clock=Clock(), duration_seconds=60)
    assert clean["synthetic_owner_acceptance"] is True
    receipt.write_text('{"synthetic":false}')
    assert module.evaluate_directory(tmp_path / "clean")["status"] == "NEEDS_REVIEW"
    receipt.write_text('{"synthetic":true}')
    probe = Probe()
    probe.change_on_final = True
    changed = module.observe(config, tmp_path / "changed", probe, clock=Clock(), duration_seconds=60)
    assert changed["status"] == "NEEDS_REVIEW"
    assert "owner_acceptance_evidence_changed" in changed["issues"]


@pytest.mark.skipif(os.name != "nt", reason="The production clock reads Windows boot identity")
def test_real_windows_clock_produces_stable_boot_identity_and_monotonic_utc_stamps():
    module = observer()
    clock = module.SystemClock()
    first, second = clock.stamp(), clock.stamp()
    assert first["boot_id"] == second["boot_id"]
    assert int(first["boot_id"]) > 0
    assert module.utc(first["utc"]).utcoffset().total_seconds() == 0
    assert 0 <= second["monotonic"] - first["monotonic"] < 5
