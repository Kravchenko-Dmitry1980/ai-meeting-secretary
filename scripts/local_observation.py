"""Explicit local Offline observation; never starts or repairs the application.

An observation result is evidence for P6, not owner acceptance or production GO.
Importing this module has no I/O. Outputs contain hashes and counts, not meetings.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import sys
import os

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_candidate import checked_path, capture_candidate

import httpx


class ObservationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class LoopbackReader:
    """Only literal HTTP loopback; no proxies, redirects, auth or environment."""
    def __init__(self, origin, *, transport=None):
        match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", origin)
        if not match or not 1 <= int(match[1]) <= 65535:
            raise ObservationError("literal_loopback_origin_required")
        self.client = httpx.Client(base_url=origin, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(3), transport=transport, headers={"Accept-Encoding": "identity"})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def get(self, path):
        if not re.fullmatch(r"/(health|ready|api/v1/meetings/[A-Za-z0-9_-]{1,128}/(tasks|task-assignments))", path):
            raise ObservationError("read_route_not_allowed")
        try:
            with self.client.stream("GET", path) as response:
                if response.status_code != 200:
                    raise ObservationError("local_http_status_invalid")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ObservationError("local_http_encoding_invalid")
                data = bytearray()
                for chunk in response.iter_bytes(65536):
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise ObservationError("local_http_response_too_large")
                return json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except ObservationError:
            raise
        except (httpx.HTTPError, UnicodeError, ValueError):
            raise ObservationError("local_http_unavailable_or_invalid") from None


def read_database(data_dir):
    """No repository constructors/migrations. SQLite may create WAL read sidecars."""
    def rows(name, sql):
        path = checked_path(Path(data_dir) / name)
        if not path.is_file():
            raise ObservationError("database_missing")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
            deadline = time.monotonic() + 2
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            result, size = [], 0
            for row in conn.execute(sql):
                size += len(canonical(row))
                if len(result) >= 100000 or size > 16 * 1024 * 1024:
                    raise ObservationError("database_query_bound_exceeded")
                result.append(row)
            return result
        finally:
            conn.close()
    try:
        usage = rows("secretary.sqlite3", "SELECT id,meeting_id,job_id,kind,estimated_rub,confirmed_rub,status,provider_request_id,created_at FROM usage ORDER BY id")
        jobs = rows("secretary.sqlite3", "SELECT id,status,error,updated_at FROM jobs ORDER BY id")
        billing = rows("billing.sqlite3", "SELECT operation_id,status,estimated_micro,reserved_micro,confirmed_micro,observed_cost_micro,updated_ms FROM billing_charges ORDER BY operation_id")
        if len({row[0] for row in usage}) != len(usage) or len({row[0] for row in jobs}) != len(jobs):
            raise ObservationError("duplicate_database_identity")
        for row in usage:
            if any(value is not None and (not finite(value) or value < 0) for value in row[4:6]):
                raise ObservationError("usage_cost_invalid")
        return {"usage_digest": digest(usage), "billing_digest": digest(billing), "jobs_digest": digest(jobs),
            "job_incidents": {digest(row[0]): digest(row[1:3]) for row in jobs if row[1] not in ("queued", "running", "succeeded", "cancelled") or row[2]},
            "usage_count": len(usage), "unknown_cost_count": sum(row[6] == 'unknown' or (row[6] == 'reserved' and row[4] is None) for row in usage),
            "confirmed_rub": sum(row[5] for row in usage if row[5] is not None),
            "estimated_rub": sum(row[4] for row in usage if row[4] is not None and row[6] != 'released')}
    except (sqlite3.Error, OSError):
        raise ObservationError("database_read_unavailable") from None


def read_log(path, previous=None):
    """Track one rotating file family without retaining any log text."""
    path = checked_path(path)
    files = []
    for suffix in (".3", ".2", ".1", ""):
        candidate = checked_path(path.with_name(path.name + suffix))
        if not candidate.exists():
            continue
        before = candidate.stat()
        with candidate.open("rb") as stream:
            data = stream.read(6 * 1024 * 1024 + 1)
        after = candidate.stat()
        if len(data) > 6 * 1024 * 1024 or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ObservationError("log_changed_during_read_or_bound_exceeded")
        files.append((suffix, f"{before.st_dev}:{before.st_ino}", data))
    if not files or files[-1][0] != "":
        raise ObservationError("current_log_missing")
    current = files[-1]
    complete = current[2].rfind(b"\n") + 1
    cursor = {"file_identity": current[1], "bytes": len(current[2]), "complete_bytes": complete,
        "sha256": hashlib.sha256(current[2]).hexdigest()}
    pieces, gap = [], len({file[1] for file in files}) != len(files)
    if previous:
        matches = [index for index, file in enumerate(files) if file[1] == previous["file_identity"]]
        if len(matches) != 1:
            gap = True
        else:
            index = matches[0]
            old = files[index][2]
            suffixes = [file[0] for file in files[index:]]
            expected_suffixes = [".3", ".2", ".1", ""][3 - len(suffixes) + 1:]
            if suffixes != expected_suffixes:
                gap = True
            if len(old) < previous["bytes"] or hashlib.sha256(old[:previous["bytes"]]).hexdigest() != previous["sha256"]:
                gap = True
            else:
                pieces.append(old[previous["complete_bytes"]:old.rfind(b"\n") + 1])
                pieces.extend(file[2][:file[2].rfind(b"\n") + 1] for file in files[index + 1:])
    lines = b"".join(pieces).splitlines()
    return {"gap": gap, "new_errors": sum(bool(re.search(rb"\b(ERROR|CRITICAL)\b|Traceback", line)) for line in lines),
        "new_warnings": sum(bool(re.search(rb"\bWARNING\b", line)) for line in lines), "cursor": cursor}


ROOT = Path(__file__).resolve().parents[1]


def file_hash(path, limit=1024 * 1024):
    with checked_path(path).open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ObservationError("evidence_file_too_large")
    return hashlib.sha256(value).hexdigest()


def load_config(path):
    config = read_json(path)
    fields = {"schema_version", "scope", "candidate", "origin", "data_dir", "runtime_root", "interval_seconds", "max_gap_seconds", "accepted_tasks", "owner_acceptance", "backup"}
    if set(config) != fields or config["schema_version"] != 1 or config["scope"] != "local_offline":
        raise ObservationError("configuration_invalid")
    candidate = config["candidate"]
    if (set(candidate) != {"state", "project", "git_sha", "frontend_sha256"} or candidate["state"] != "bound"
            or checked_path(candidate["project"]) != ROOT
            or not re.fullmatch(r"[a-f0-9]{40}", candidate["git_sha"])
            or not re.fullmatch(r"[a-f0-9]{64}", candidate["frontend_sha256"])):
        raise ObservationError("candidate_configuration_invalid")
    for name in ("data_dir", "runtime_root"):
        value = checked_path(config[name])
        base = ROOT if name == "data_dir" else ROOT / ".runtime"
        if not value.is_dir() or not value.is_relative_to(base) or value == ROOT:
            raise ObservationError("runtime_or_data_scope_invalid")
    interval, gap = config["interval_seconds"], config["max_gap_seconds"]
    if not finite(interval) or not finite(gap) or not 1 <= interval <= 300 or not interval <= gap <= 2 * interval:
        raise ObservationError("sampling_policy_invalid")
    with LoopbackReader(config["origin"]):
        pass  # construction validates the origin; it makes no requests
    tasks = config["accepted_tasks"]
    if not isinstance(tasks, list) or not 3 <= len(tasks) <= 100:
        raise ObservationError("accepted_baseline_missing")
    keys = []
    for task in tasks:
        if (set(task) != {"meeting_id", "transcript_version", "summary_version", "assignment_revision", "action_id"}
                or any(not isinstance(task[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task[key]) for key in ("meeting_id", "action_id"))
                or any(type(task[key]) is not int or task[key] < 0 for key in ("transcript_version", "summary_version", "assignment_revision"))):
            raise ObservationError("accepted_task_scope_invalid")
        keys.append((task["meeting_id"], task["action_id"]))
    if len(set(keys)) != len(keys):
        raise ObservationError("accepted_task_duplicates")
    acceptance = config["owner_acceptance"]
    if set(acceptance) != {"path", "sha256"} or file_hash(acceptance["path"]) != acceptance["sha256"]:
        raise ObservationError("owner_acceptance_evidence_changed")
    receipt = read_json(acceptance["path"])
    if (set(receipt) - {"schema_version", "scope", "accepted_gates", "accepted_tasks", "synthetic"}
            or receipt.get("schema_version") != 1 or receipt.get("scope") != "local_offline"
            or receipt.get("accepted_gates") != ["P1", "P2", "P3", "P4", "P5"] or receipt.get("accepted_tasks") != tasks):
        raise ObservationError("owner_acceptance_scope_invalid")
    backup = config["backup"]
    if (set(backup) != {"directory", "manifest_sha256", "verify_receipt", "verify_receipt_sha256", "excluded_directories"}
            or backup["excluded_directories"] not in ([], ["test-audio-tmp"])):
        raise ObservationError("backup_configuration_invalid")
    for value in (acceptance["path"], backup["directory"], backup["verify_receipt"]):
        if not checked_path(value).is_relative_to(ROOT):
            raise ObservationError("evidence_outside_project")
    if file_hash(backup["verify_receipt"]) != backup["verify_receipt_sha256"]:
        raise ObservationError("backup_verify_evidence_changed")
    return config


def read_process(config):
    import psutil
    from secretary.infrastructure.team_process_identity import process_identity
    state = read_json(Path(config["runtime_root"]) / "processes.json", 128 * 1024)
    if (state.get("schema_version") != 1 or state.get("owner_kind") != "local_supervisor" or state.get("state") != "running"
            or state.get("project") != str(ROOT) or state.get("runtime_root") != config["runtime_root"]
            or state.get("data_dir") != config["data_dir"] or state.get("offline") is not True
            or f"http://127.0.0.1:{state.get('port')}" != config["origin"]
            or state.get("candidate_binding_verified") is not True or state.get("candidate_binding") != config["candidate"]):
        raise ObservationError("owned_bound_offline_runtime_required")
    try:
        identities = [state["supervisor_identity"], *state["attempt_identities"]]
        listener = state["listener_identity"]
        if len(identities) not in (2, 3) or listener not in identities[1:]:
            raise ObservationError("owned_process_identity_invalid")
        for expected in identities:
            if process_identity(expected["pid"]) != expected:
                raise ObservationError("owned_process_identity_changed")
        process = psutil.Process(listener["pid"])
        if identities[0]["pid"] not in [ancestor.pid for ancestor in process.parents()]:
            raise ObservationError("owned_listener_parent_changed")
        if not any(connection.status == psutil.CONN_LISTEN and connection.laddr.ip == "127.0.0.1"
                and connection.laddr.port == state["port"] for connection in process.net_connections(kind="tcp")):
            raise ObservationError("owned_listener_missing")
        if read_json(Path(config["runtime_root"]) / "processes.json", 128 * 1024) != state:
            raise ObservationError("owned_state_changed_during_read")
    except (KeyError, TypeError, ValueError, psutil.Error):
        raise ObservationError("owned_process_unavailable") from None
    return state


def read_backup(config, *, full_verify):
    backup = config["backup"]
    directory = checked_path(backup["directory"])
    if file_hash(directory / "manifest.json", 16 * 1024 * 1024) != backup["manifest_sha256"]:
        raise ObservationError("backup_manifest_changed")
    manifest = read_json(directory / "manifest.json", 16 * 1024 * 1024)
    receipt = read_json(backup["verify_receipt"])
    exclusions = manifest.get("excluded_directories", [])
    if (manifest.get("state") != "complete" or manifest.get("source_data_dir") != config["data_dir"]
            or exclusions != backup["excluded_directories"] or manifest.get("full_recovery") is not (not exclusions)
            or receipt.get("state") != "verified" or receipt.get("backup_id") != manifest.get("backup_id")
            or receipt.get("excluded_directories", []) != exclusions
            or file_hash(backup["verify_receipt"]) != backup["verify_receipt_sha256"]):
        raise ObservationError("backup_scope_or_receipt_invalid")
    if full_verify:
        from secretary.infrastructure.local_backup import verify_backup
        verified = verify_backup(directory)
        if verified.get("state") != "verified" or any(receipt.get(key) != value for key, value in verified.items()):
            raise ObservationError("backup_verification_failed")
    return {"manifest_sha256": backup["manifest_sha256"], "backup_id": manifest["backup_id"],
        "excluded_directories": exclusions, "age_seconds": (datetime.now(timezone.utc) - utc(manifest["created_at"])).total_seconds(), "verified": full_verify}


class LocalProbe:
    def __init__(self, config, *, reader=None, process_reader=read_process, candidate_reader=capture_candidate, backup_reader=read_backup):
        self.config, self.reader = config, reader or LoopbackReader(config["origin"])
        self.process_reader, self.candidate_reader, self.backup_reader = process_reader, candidate_reader, backup_reader
        self.log_cursors = {}
        self.baseline_collected = False

    def close(self):
        self.reader.__exit__(None, None, None)

    def sample(self, *, full_backup):
        config = self.config
        owner = self.process_reader(config)
        candidate = self.candidate_reader(ROOT)
        ready = self.reader.get("/ready")
        health = self.reader.get("/health")
        if (ready.get("production_qualified") is not False or ready.get("blockers") != []
                or ready["jobs"]["state"] != "healthy" or ready["components"]["processing_worker"]["state"] != "healthy"):
            raise ObservationError("readiness_degraded_or_invalid")
        projections = []
        for meeting in sorted({task["meeting_id"] for task in config["accepted_tasks"]}):
            tasks = self.reader.get(f"/api/v1/meetings/{meeting}/tasks")
            snapshot = self.reader.get(f"/api/v1/meetings/{meeting}/task-assignments")
            if not isinstance(tasks, list) or len(tasks) > 1000 or len({task["id"] for task in tasks}) != len(tasks):
                raise ObservationError("task_projection_duplicates_or_invalid")
            for accepted in [task for task in config["accepted_tasks"] if task["meeting_id"] == meeting]:
                if (any(snapshot[key] != accepted[key] for key in ("meeting_id", "transcript_version", "summary_version"))
                        or snapshot["revision"] < accepted["assignment_revision"]
                        or not self.baseline_collected and snapshot["revision"] != accepted["assignment_revision"]):
                    raise ObservationError("accepted_task_version_changed")
                matching = [task for task in tasks if task["id"] == accepted["action_id"]]
                if len(matching) != 1 or matching[0]["assignment_status"] != "confirmed" or not matching[0]["participant_id"]:
                    raise ObservationError("accepted_task_missing_or_unconfirmed")
                assignments = [item for item in snapshot["items"] if item["action"]["action_id"] == accepted["action_id"]]
                if (len(assignments) != 1 or assignments[0]["status"] != "confirmed"
                        or assignments[0]["participant_id"] != matching[0]["participant_id"]):
                    raise ObservationError("accepted_assignment_missing_or_unconfirmed")
                task = {key: value for key, value in matching[0].items() if key != "assignment_revision"}
                assignment = {key: assignments[0][key] for key in ("action", "participant_id", "status", "basis", "reason_codes")}
                projections.append([task, assignment])
        log_paths = [Path(owner["stdout_log"]), Path(owner["stderr_log"]),
            Path(config["runtime_root"]) / "local-supervisor" / owner["supervisor_run_id"] / "logs/secretary.error.log"]
        logs = []
        for index, path in enumerate(log_paths):
            if not checked_path(path).is_relative_to(checked_path(config["runtime_root"])):
                raise ObservationError("log_scope_invalid")
            log = read_log(path, self.log_cursors.get(str(index)))
            self.log_cursors[str(index)] = log["cursor"]
            logs.append(log)
        components = ready["components"]
        worker, storage = components["processing_worker"], components["storage"]
        metrics = {"candidate": candidate, "process": {"verified": True, "binding_verified": owner["candidate_binding_verified"],
                "identity_digest": digest([owner["supervisor_identity"], owner["attempt_identities"], owner["listener_identity"], owner["configuration_sha256"], owner["run_id"]])},
            "health": health["status"], "ready": ready["status"], "cloud": components["cloud"]["state"],
            "worker_age_seconds": worker["heartbeat_age_seconds"], "worker_stale_after_seconds": worker["heartbeat_stale_after_seconds"],
            "queue_counts": ready["jobs"]["counts"], "stale_running": ready["jobs"]["stale_running_candidate_count"],
            "free_bytes": storage["free_bytes"], "minimum_free_bytes": storage["minimum_free_bytes"],
            "tasks": {"accepted_count": len(config["accepted_tasks"]), "digest": digest(projections)},
            "database": read_database(Path(config["data_dir"])),
            "logs": {"gap": any(log["gap"] for log in logs), "new_errors": sum(log["new_errors"] for log in logs),
                "new_warnings": sum(log["new_warnings"] for log in logs), "cursor_digest": digest(self.log_cursors)},
            "backup": self.backup_reader(config, full_verify=full_backup)}
        if self.process_reader(config) != owner:
            raise ObservationError("owner_changed_during_sample")
        if file_hash(config["owner_acceptance"]["path"]) != config["owner_acceptance"]["sha256"]:
            raise ObservationError("owner_acceptance_evidence_changed")
        self.baseline_collected = True
        return metrics


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def utc(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("utc_required")
    return result


def sample_issues(config, sample, baseline):
    try:
        value = sample["metrics"]
        issues = list(sample["issues"])
        if value["candidate"] != config["candidate"] or value["candidate"]["state"] != "bound":
            issues.append("candidate_changed_or_unbound")
        if value["process"]["verified"] is not True or value["process"]["binding_verified"] is not True:
            issues.append("process_unverified")
        for proof in (value["process"]["identity_digest"], value["tasks"]["digest"],
                value["database"]["usage_digest"], value["database"]["billing_digest"], value["database"]["jobs_digest"], value["logs"]["cursor_digest"]):
            if not isinstance(proof, str) or not re.fullmatch(r"[a-f0-9]{64}", proof):
                issues.append("required_digest_invalid")
        if value["process"] != baseline["process"]:
            issues.append("process_identity_changed")
        if (value["health"], value["ready"], value["cloud"]) != ("ok", "local_ok", "disabled"):
            issues.append("local_offline_not_ready")
        age, threshold = value["worker_age_seconds"], value["worker_stale_after_seconds"]
        if not finite(age) or not finite(threshold) or not 0 <= age <= threshold or threshold != 10:
            issues.append("worker_stale_or_unknown")
        counts = value["queue_counts"]
        if (not isinstance(counts, dict) or any(type(count) is not int or count < 0 for count in counts.values())
                or type(value["stale_running"]) is not int or value["stale_running"] != 0
                or set(counts) - {"queued", "running", "waiting_config", "paused_budget", "uncertain", "failed", "cancelled", "succeeded"}):
            issues.append("queue_stale_or_unknown")
        free, minimum = value["free_bytes"], value["minimum_free_bytes"]
        if not finite(free) or not finite(minimum) or not 0 < minimum <= free:
            issues.append("storage_below_reserve_or_unknown")
        if value["tasks"]["accepted_count"] != len(config["accepted_tasks"]) or value["tasks"] != baseline["tasks"]:
            issues.append("accepted_tasks_changed_or_missing")
        db = value["database"]
        for field in ("usage_digest", "billing_digest", "usage_count", "unknown_cost_count", "confirmed_rub", "estimated_rub"):
            if db[field] != baseline["database"][field]:
                issues.append("database_projection_or_spend_changed")
        if any(baseline["database"]["job_incidents"].get(key) != value for key, value in db["job_incidents"].items()):
            issues.append("new_job_incident")
        if any(not finite(db[field]) or db[field] < 0 for field in ("usage_count", "unknown_cost_count", "confirmed_rub", "estimated_rub")):
            issues.append("spend_unknown_metric")
        if value["logs"]["gap"] is not False:
            issues.append("log_continuity_lost")
        if any(type(value["logs"][key]) is not int or value["logs"][key] < 0 for key in ("new_errors", "new_warnings")):
            issues.append("log_counts_invalid")
        if value["logs"]["new_errors"] or value["logs"]["new_warnings"]:
            issues.append("new_log_incident")
        if not isinstance(value["logs"]["cursor_digest"], str):
            issues.append("log_cursor_missing")
        backup = value["backup"]
        if (backup["manifest_sha256"] != config["backup"]["manifest_sha256"]
                or backup["excluded_directories"] != config["backup"]["excluded_directories"]
                or backup["backup_id"] != baseline["backup"]["backup_id"]
                or not finite(backup["age_seconds"]) or backup["age_seconds"] < 0):
            issues.append("backup_scope_or_identity_changed")
        return issues
    except (KeyError, TypeError, ValueError, AttributeError):
        return ["required_metric_missing_or_invalid"]


def evaluate_records(config, samples, terminal):
    report = {"status": "INCOMPLETE", "issues": [], "production_qualified": False, "team": "N/A"}
    if not samples or not terminal or terminal.get("state") != "completed":
        report["issues"] = sorted(set(["terminal_observation_missing"] + (terminal.get("issues", []) if terminal else [])
            + [issue for sample in samples for issue in sample.get("issues", [])]))
        if terminal and terminal.get("state") == "interrupted":
            report["status"] = "INTERRUPTED"
        return report
    issues = []
    try:
        if terminal["sample_count"] != len(samples) or len(samples) < 2:
            issues.append("sample_count_invalid")
        if len(config["accepted_tasks"]) < 3:
            issues.append("accepted_baseline_missing")
        interval, gap = config["interval_seconds"], config["max_gap_seconds"]
        if not finite(interval) or not finite(gap) or not 1 <= interval <= 300 or not interval <= gap <= interval * 2:
            issues.append("sampling_policy_invalid")
        first = samples[0]
        previous = None
        previous_metrics = None
        for index, sample in enumerate(samples):
            stamp = sample["clock"]
            if sample["sequence"] != index or not finite(stamp["monotonic"]):
                issues.append("sample_sequence_invalid")
            issues.extend(sample_issues(config, sample, first["metrics"]))
            if previous_metrics and any(previous_metrics["database"]["job_incidents"].get(key) != item
                    for key, item in sample["metrics"]["database"]["job_incidents"].items()):
                issues.append("new_job_incident")
            utc(stamp["utc"])
            if not stamp["boot_id"] or stamp["boot_id"] != first["clock"]["boot_id"]:
                issues.append("boot_identity_changed")
            if previous:
                elapsed = stamp["monotonic"] - previous["monotonic"]
                wall = (utc(stamp["utc"]) - utc(previous["utc"])).total_seconds()
                if not 0 < elapsed <= gap:
                    issues.append("observation_gap")
                if abs(wall - elapsed) > 5:
                    issues.append("clock_discontinuity")
            previous = stamp
            previous_metrics = sample["metrics"]
        elapsed = samples[-1]["clock"]["monotonic"] - first["clock"]["monotonic"]
        wall = (utc(samples[-1]["clock"]["utc"]) - utc(first["clock"]["utc"])).total_seconds()
        if abs(wall - elapsed) > 5:
            issues.append("clock_discontinuity")
        if first["metrics"]["backup"]["verified"] is not True or samples[-1]["metrics"]["backup"]["verified"] is not True:
            issues.append("backup_verification_missing")
        report.update(elapsed_seconds=elapsed, sample_count=len(samples),
            unknown_cost_count=first["metrics"]["database"]["unknown_cost_count"],
            backup_age_seconds=samples[-1]["metrics"]["backup"]["age_seconds"])
        if elapsed < 86400:
            issues.append("full_day_not_observed")
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        issues.append("required_evidence_missing_or_invalid")
    report["issues"] = sorted(set(issues))
    report["status"] = ("OBSERVATION_PASS" if not issues else
        "INCOMPLETE" if set(issues) <= {"full_day_not_observed"} else "NEEDS_REVIEW")
    return report


def read_json(path, limit=1024 * 1024):
    path = checked_path(path)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ObservationError("json_evidence_bound_exceeded")
    try:
        return json.loads(raw.decode("utf-8-sig"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError):
        raise ObservationError("json_evidence_invalid") from None


def write_json(path, value):
    with checked_path(path).open("x", encoding="utf-8") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


class SystemClock:
    def stamp(self):
        if os.name != "nt":
            raise ObservationError("windows_clock_required")
        import ctypes
        # SystemTimeOfDayInformation begins with exact boot FILETIME. This avoids
        # deriving a boot identity from a mutable wall clock / rounded uptime.
        buffer = ctypes.create_string_buffer(128)
        length = ctypes.c_ulong()
        status = ctypes.WinDLL("ntdll").NtQuerySystemInformation(3, buffer, len(buffer), ctypes.byref(length))
        if status != 0 or length.value < 8:
            raise ObservationError("boot_identity_unavailable")
        boot = int.from_bytes(buffer.raw[:8], "little", signed=True)
        return {"utc": datetime.now(timezone.utc).isoformat(), "monotonic": time.monotonic(), "boot_id": str(boot)}

    def sleep(self, seconds):
        time.sleep(seconds)


def observe(config, output, probe, *, clock=None, duration_seconds=86400):
    """One non-resumable collector lifetime. Short diagnostic runs stay incomplete."""
    if not finite(duration_seconds) or not 0 < duration_seconds <= 172800:
        raise ObservationError("duration_invalid")
    clock = clock or SystemClock()
    output = checked_path(output)
    output.mkdir(parents=False, exist_ok=False)
    write_json(output / "config.json", config)
    start = clock.stamp()
    samples, state = [], "completed"
    config_sha256 = digest(config)
    journal_bytes = 0
    terminal_issues = []
    try:
        with (output / "samples.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
            previous_hash = "0" * 64
            while True:
                before = clock.stamp()
                final = before["monotonic"] - start["monotonic"] >= duration_seconds
                issues = []
                try:
                    metrics = probe.sample(full_backup=not samples or final)
                except Exception as exc:
                    # Only stable allowlisted boundary errors escape into reports.
                    code = str(exc) if isinstance(exc, ObservationError) else "collector_unavailable"
                    issues = [code if re.fullmatch(r"[a-z0-9_]{1,100}", code) else "collector_unavailable"]
                    metrics = {}
                stamp = clock.stamp()
                sample = {"sequence": len(samples), "clock": stamp, "metrics": metrics, "issues": issues,
                    "previous_sha256": previous_hash, "config_sha256": config_sha256}
                previous_hash = digest(sample)
                sample["sha256"] = previous_hash
                encoded = canonical(sample) + "\n"
                journal_bytes += len(encoded.encode("utf-8"))
                if len(encoded.encode("utf-8")) > 1024 * 1024 or journal_bytes > 64 * 1024 * 1024 or len(samples) >= 200000:
                    state = "interrupted"
                    terminal_issues.append("journal_bound_exceeded")
                    break
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
                samples.append(sample)
                if len(samples) == 1:
                    start = stamp  # the full duration begins after the baseline was collected
                if final or issues:
                    if issues:
                        state = "interrupted"
                    break
                clock.sleep(min(config["interval_seconds"], max(.001, duration_seconds - (stamp["monotonic"] - start["monotonic"]))))
    except KeyboardInterrupt:
        state = "interrupted"
    terminal = {"state": state, "sample_count": len(samples), "last_sha256": samples[-1]["sha256"] if samples else None,
        "config_sha256": config_sha256, "issues": terminal_issues}
    write_json(output / "terminal.json", terminal)
    report = evaluate_records(config, samples, terminal)
    if config.get("owner_acceptance"):
        if file_hash(config["owner_acceptance"]["path"]) != config["owner_acceptance"]["sha256"]:
            report["status"] = "NEEDS_REVIEW"
            report["issues"].append("owner_acceptance_evidence_changed")
        else:
            report["synthetic_owner_acceptance"] = read_json(config["owner_acceptance"]["path"]).get("synthetic") is True
            if report["synthetic_owner_acceptance"] and report["status"] == "OBSERVATION_PASS":
                report["status"] = "NEEDS_REVIEW"
                report["issues"].append("synthetic_owner_acceptance")
    write_json(output / "report.json", report)
    return report


def evaluate_directory(output):
    output = checked_path(output)
    config = read_json(output / "config.json")
    samples = []
    previous = "0" * 64
    try:
        total = 0
        with (output / "samples.jsonl").open("rb") as stream:
            while line := stream.readline(1024 * 1024 + 1):
                total += len(line)
                if len(line) > 1024 * 1024 or total > 64 * 1024 * 1024 or len(samples) >= 200000:
                    raise ObservationError("journal_bound_exceeded")
                value = json.loads(line)
                expected = value.pop("sha256")
                if value["previous_sha256"] != previous or digest(value) != expected or value["config_sha256"] != digest(config):
                    raise ObservationError("journal_chain_invalid")
                value["sha256"] = expected
                previous = expected
                samples.append(value)
        terminal = read_json(output / "terminal.json") if (output / "terminal.json").exists() else None
        if terminal and (terminal.get("last_sha256") != previous or terminal.get("config_sha256") != digest(config)):
            raise ObservationError("journal_terminal_invalid")
        report = evaluate_records(config, samples, terminal)
        if config.get("owner_acceptance"):
            if file_hash(config["owner_acceptance"]["path"]) != config["owner_acceptance"]["sha256"]:
                raise ObservationError("owner_acceptance_evidence_changed")
            report["synthetic_owner_acceptance"] = read_json(config["owner_acceptance"]["path"]).get("synthetic") is True
            if report["synthetic_owner_acceptance"] and report["status"] == "OBSERVATION_PASS":
                report["status"] = "NEEDS_REVIEW"
                report["issues"].append("synthetic_owner_acceptance")
        return report
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "NEEDS_REVIEW", "issues": ["journal_invalid"], "production_qualified": False, "team": "N/A"}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("observe")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--duration-seconds", type=float, default=86400)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    probe = None
    try:
        output = checked_path(args.output)
        if output == ROOT / ".runtime" or not output.is_relative_to(ROOT / ".runtime"):
            raise ObservationError("output_outside_private_runtime")
        if args.command == "observe":
            config = load_config(args.config)
            probe = LocalProbe(config)
            report = observe(config, output, probe, duration_seconds=args.duration_seconds)
        else:
            report = evaluate_directory(output)
        print(canonical(report))
        return 0 if report["status"] == "OBSERVATION_PASS" else 2
    except (ObservationError, OSError, ValueError, KeyError, TypeError):
        print(canonical({"status": "NEEDS_REVIEW", "issues": ["observation_input_or_io_invalid"], "production_qualified": False}))
        return 2
    finally:
        if probe:
            probe.close()


if __name__ == "__main__":
    raise SystemExit(main())
