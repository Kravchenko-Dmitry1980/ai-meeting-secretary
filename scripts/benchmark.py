"""Explicitly gated cloud benchmark. No --run-cloud means no network activity.

Run against an idle Secretary instance. Source audio/reference files stay unchanged.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

PROJECT_DIR = Path(__file__).resolve().parents[1]
ACTIVE_JOB_STATUSES = {"queued", "running"}
ACTIVE_CAPTURE_STATUSES = {"starting", "recording", "stopping"}


def distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for i, item in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (item != other)))
        previous = current
    return previous[-1]


def accuracy(reference: str, hypothesis: str) -> dict:
    normalized = reference.casefold()
    ref, hyp = normalized.split(), hypothesis.casefold().split()
    return {
        "wer": distance(ref, hyp) / len(ref) if ref else None,
        "cer": distance(list(normalized), list(hypothesis.casefold())) / len(normalized) if normalized else None,
        "normalization": "casefold; whitespace split for WER; punctuation retained",
    }


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def local_api_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and
                parsed.port is not None and 1024 <= parsed.port <= 65535 and
                not parsed.username and not parsed.password and parsed.path in {"", "/"} and
                not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _jobs(client, meeting_id: str) -> list[dict]:
    return client.get(f"/api/v1/meetings/{meeting_id}/jobs").raise_for_status().json()


def _idle(client) -> bool:
    for meeting in client.get("/api/v1/meetings").raise_for_status().json():
        state = client.get(f'/api/v1/meetings/{meeting["id"]}/recording').raise_for_status().json()
        if state.get("recording") or state.get("status") in ACTIVE_CAPTURE_STATUSES:
            return False
        if any(job["status"] in ACTIVE_JOB_STATUSES for job in _jobs(client, meeting["id"])):
            return False
    return True


def _backend_process(state_path: Path, port: int):
    """Find the listening server, including Windows venv redirector child processes.

    The tracked parent proves ownership, but its RSS/CPU can be a launcher stub.
    A candidate must have the exact Python launcher/port, verified ancestry and
    its own loopback listening socket. Unprovable/ambiguous attribution is null.
    """
    try:
        import psutil
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        launcher = (PROJECT_DIR / "scripts" / "run_server.py").resolve()
        if state.get("port") != port or Path(state.get("launcher", "")).resolve() != launcher:
            return None, None
        if Path(state.get("project", "")).resolve() != PROJECT_DIR.resolve():
            return None, None
        tracked = psutil.Process(state["pid"])
        created = datetime.fromisoformat(state["creation_time"].replace("Z", "+00:00")).timestamp()
        if abs(tracked.create_time() - created) > 0.01:
            return None, None
        if not _server_launcher(tracked, launcher, port):
            return None, None
        candidates = []
        for process in [tracked, *tracked.children(recursive=True)]:
            try:
                if not _server_launcher(process, launcher, port):
                    continue
                if process.create_time() < created - 0.01 or not _owned_descendant(process, tracked, created):
                    continue
                connections = process.net_connections(kind="tcp")
                if any(connection.status == psutil.CONN_LISTEN and connection.laddr and
                       connection.laddr[0] == "127.0.0.1" and connection.laddr[1] == port for connection in connections):
                    candidates.append(process)
            except Exception:
                continue
        if len(candidates) != 1:
            return None, None
        server = candidates[0]
        return server, sum(server.cpu_times()[:2])
    except Exception:
        return None, None


def _server_launcher(process, launcher: Path, port: int) -> bool:
    command = process.cmdline()
    executable = Path(process.exe()).name.casefold()
    return (executable in {"python.exe", "pythonw.exe", "python", "python3", "python3.12"} and
            len(command) == 4 and Path(command[1]).resolve() == launcher and
            command[2] == "--port" and command[3] == str(port))


def _owned_descendant(process, tracked, created: float) -> bool:
    visited = set()
    current = process
    for _ in range(16):
        if current is None or current.pid in visited:
            return False
        if current.pid == tracked.pid:
            return abs(current.create_time() - created) <= 0.01
        visited.add(current.pid)
        current = current.parent()
    return False


def _resources(process, peak_rss: int | None) -> tuple[int | None, float | None]:
    if process is None:
        return peak_rss, None
    try:
        return max(peak_rss or 0, process.memory_info().rss), sum(process.cpu_times()[:2])
    except Exception:
        return peak_rss, None


def _transcript(client, meeting_id: str) -> list[dict]:
    items, offset, version = [], 0, None
    while True:
        page = client.get(f"/api/v1/meetings/{meeting_id}/segments", params={"offset": offset, "limit": 1000}).raise_for_status().json()
        if version is not None and page["transcript_version"] != version:
            raise RuntimeError("Transcript version changed during benchmark; use the retained meeting")
        version = page["transcript_version"]
        items.extend(page["items"])
        offset += len(page["items"])
        if offset >= page["total"]:
            return items
        if not page["items"]:
            raise RuntimeError("Transcript pagination ended before all segments were returned")


def cost_metrics(usage: dict, duration_ms: int | None) -> dict:
    records = usage.get("records", [])
    unknown = bool(usage.get("unknown_count")) or any(
        record.get("status") in {"unknown", "reserved"} or
        (record.get("status") == "confirmed" and record.get("confirmed_rub") is None) for record in records)
    confirmed_records = [record for record in records if record.get("status") == "confirmed"]
    total = usage.get("confirmed_rub")
    known = (not unknown and bool(confirmed_records) and isinstance(total, (float, int)) and
             not isinstance(total, bool) and math.isfinite(total) and total >= 0)
    return {"cost": "confirmed" if known else "unknown or no confirmed cloud receipt",
            "confirmed_rub_per_hour": total * 3_600_000 / duration_ms if known and duration_ms and duration_ms > 0 else None}


def unresolved_usage(usage: dict) -> bool:
    return bool(usage.get("unknown_count")) or any(
        record.get("status") in {"unknown", "reserved"} for record in usage.get("records", []))


def _valid_money(value) -> bool:
    return (type(value) in {int, float} and 0 <= value <= 1_000_000_000
            and math.isfinite(value))


def _monthly_ready(value: dict) -> bool:
    fields = ("approved_rub", "effective_limit_rub", "confirmed_rub", "reserved_rub", "remaining_rub")
    return (isinstance(value, dict) and all(_valid_money(value.get(name)) for name in fields)
            and isinstance(value.get("period"), str)
            and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value["period"]) is not None
            and "paused_code" in value and value["paused_code"] is None
            and 0 < value["effective_limit_rub"] <= value["approved_rub"] <= 3000
            and value["reserved_rub"] == 0 and value["remaining_rub"] > 0
            and value["remaining_rub"] <= max(0, value["effective_limit_rub"] - value["confirmed_rub"])
            and type(value.get("uncertain_count", 0)) is int and value.get("uncertain_count", 0) == 0)


def _scope_snapshot(client, scope_id: str, cap: float) -> dict:
    value = client.get(f"/api/v1/cloud-budget/scopes/{scope_id}").raise_for_status().json()
    fields = ("cap_rub", "confirmed_rub", "reserved_rub", "remaining_rub")
    if (not isinstance(value, dict) or not all(_valid_money(value.get(name)) for name in fields)
            or value["cap_rub"] != cap
            or "paused_code" not in value
            or value["paused_code"] not in {None, "monthly_budget_scope_exhausted"}
            or type(value.get("uncertain_count", 0)) is not int or value.get("uncertain_count", 0) < 0
            or value["remaining_rub"] > max(0, cap - value["confirmed_rub"] - value["reserved_rub"]) + 1e-6):
        raise RuntimeError("Backend returned an invalid benchmark scope snapshot")
    return value


def _scope_unresolved(value: dict) -> bool:
    return value["reserved_rub"] > 0 or value.get("uncertain_count", 0) > 0


def _scope_cost_metrics(value: dict, duration_ms: int | None) -> dict:
    known = not _scope_unresolved(value)
    return {"cost_source": "cloud_budget_scope",
            "cost": "confirmed" if known else "unknown or outstanding scoped cloud receipt",
            "confirmed_rub_per_hour": value["confirmed_rub"] * 3_600_000 / duration_ms
            if known and duration_ms and duration_ms > 0 else None}


def _set_configuration(client, changes: dict) -> None:
    """Require the backend to acknowledge every guard before any upload or restore."""
    applied = client.patch("/api/v1/config", json=changes).raise_for_status().json()
    if not isinstance(applied, dict) or any(
            (applied.get(name) is not value if isinstance(value, bool) else applied.get(name) != value)
            for name, value in changes.items()):
        raise RuntimeError("Backend did not confirm the requested benchmark configuration")


def _abort(client, meeting_id: str, cap: float, *, wait_seconds: float = 30) -> bool:
    """Freeze cloud before cancelling own jobs; the run cap stays in its scope."""
    _set_configuration(client, {"cloud_enabled": False})
    deadline = time.monotonic() + wait_seconds
    while True:
        active = [job for job in _jobs(client, meeting_id) if job["status"] in ACTIVE_JOB_STATUSES]
        if not active:
            return True
        for job in active:
            client.post(f'/api/v1/jobs/{job["id"]}/cancel').raise_for_status()
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--critical-terms", type=Path, help="JSON list of exact terms, names, numbers, dates and negations")
    parser.add_argument("--run-cloud", action="store_true", help="Explicitly authorize this bounded benchmark only")
    parser.add_argument("--max-rub", type=float, help="Total reservation/confirmed spend cap for this single run")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, default=PROJECT_DIR / "docs" / "benchmark" / "report.json")
    args = parser.parse_args(argv)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "cloud": "не измерено",
              "quality": "не измерено", "cost": "не измерено", "p50": None, "p95": None,
              "reason": "Эталонный benchmark не запускался: проверенная расшифровка и "
                        "полный набор подтверждённых расходов отсутствуют.",
              "decision_task_fidelity": "manual review of each evidence reference required", "sample_count": 0,
              "confirmed_rub_per_hour": None, "config_restored": None}

    def reject(message):
        report["reason"] = message
        write_report(args.output, report)
        parser.error(message)

    if not args.run_cloud:
        write_report(args.output, report)
        print(f"Cloud benchmark: not measured. Report: {args.output}")
        return 0
    if (args.audio is None or not args.audio.is_file() or args.max_rub is None or
            not math.isfinite(args.max_rub) or not 0 < args.max_rub <= 100000):
        reject("--run-cloud requires an existing --audio and finite --max-rub in (0, 100000]")
    if not local_api_url(args.url):
        reject("Benchmark API must be http://127.0.0.1:<port>, without userinfo, path or redirects")
    try:
        reference = args.reference.read_text(encoding="utf-8-sig") if args.reference else None
        terms = json.loads(args.critical_terms.read_text(encoding="utf-8-sig")) if args.critical_terms else None
        if terms is not None and (not isinstance(terms, list) or not all(isinstance(term, str) and term for term in terms)):
            reject("critical-terms must be a JSON list of nonempty strings")
    except (OSError, ValueError) as exc:
        reject(f"Invalid local benchmark input: {type(exc).__name__}; no request submitted")

    with httpx.Client(base_url=args.url, timeout=180, follow_redirects=False, trust_env=False) as client:
        try:
            config = client.get("/api/v1/config").raise_for_status().json()
            if not config.get("key_configured"):
                reject("Backend has no POLZA_API_KEY; no paid request was sent")
            if type(config.get("cloud_enabled")) is not bool:
                reject("Backend must expose a boolean cloud_enabled flag; no request submitted")
            if any(not isinstance(config.get(name), str) or not config[name].strip()
                   for name in ("stt_model", "summary_model")):
                reject("Select STT and summary models before the benchmark; no request submitted")
            if not _idle(client):
                reject("Secretary must be idle before a benchmark; no paid request was sent")
            if unresolved_usage(client.get("/api/v1/usage").raise_for_status().json()):
                reject("Existing reserved/unknown cloud expense needs reconciliation before another benchmark; no request submitted")
            token = client.get("/api/v1/session").raise_for_status().json()["csrf_token"]
            client.headers["X-Secretary-Token"] = token
            monthly = client.post("/api/v1/cloud-budget/refresh").raise_for_status().json()
            report["monthly_budget_preflight"] = monthly
            if not _monthly_ready(monthly):
                reject("Monthly cloud budget is not ready or has outstanding expense; no benchmark submitted")
        except Exception as exc:
            report["reason"] = f"Local API preflight failed: {type(exc).__name__}; no benchmark submitted"
            write_report(args.output, report)
            print(f"Report: {args.output}; preflight failed; no benchmark submitted")
            return 1
        prior_config = {"cloud_enabled": config["cloud_enabled"]}
        meeting_id, settled, started = None, False, None
        scope_id, scope, cloud_change_attempted = None, None, False
        scope_operation_id = str(uuid4())
        report.update({"cloud_budget_scope_operation_id": scope_operation_id, "max_rub": args.max_rub})
        exit_code = 0
        try:
            response = client.post("/api/v1/cloud-budget/scopes", json={
                "operation_id": scope_operation_id, "cap_rub": args.max_rub}).raise_for_status()
            if response.status_code != 201:
                raise RuntimeError("Backend did not acknowledge creation of the benchmark scope")
            accepted = response.json()
            value = accepted.get("scope_id") if isinstance(accepted, dict) else None
            if (not isinstance(value, str) or str(UUID(value)) != value
                    or not _valid_money(accepted.get("cap_rub")) or accepted["cap_rub"] != args.max_rub):
                raise RuntimeError("Backend did not confirm the requested benchmark scope")
            scope_id = value
            report["cloud_budget_scope_id"] = scope_id
            if not _idle(client):
                raise RuntimeError("Secretary became busy before benchmark activation")
            if not config["cloud_enabled"]:
                # An applied PATCH can lose its response; retain restoration ownership.
                cloud_change_attempted = True
                _set_configuration(client, {"cloud_enabled": True})
            if not _idle(client):
                raise RuntimeError("Secretary became busy before benchmark import")
            meeting = client.post("/api/v1/meetings", json={
                "title": f"Benchmark {datetime.now(timezone.utc).isoformat()}",
                "cloud_budget_scope_id": scope_id}).raise_for_status().json()
            meeting_id = meeting["id"]
            report.update({"meeting_id": meeting_id, "audio_path": str(args.audio.resolve()),
                           "models": {"stt": config["stt_model"], "summary": config["summary_model"]}, "max_rub": args.max_rub})
            process, cpu_start = _backend_process(PROJECT_DIR / ".runtime" / "processes.json", urlsplit(args.url).port)
            report["backend_process_pid"] = process.pid if process is not None else None
            started = time.monotonic()
            with args.audio.open("rb") as audio:
                client.post(f"/api/v1/meetings/{meeting_id}/upload", files={"file": (args.audio.name, audio, "application/octet-stream")}).raise_for_status()
            first_text, peak_rss, cpu_end = None, None, None
            while time.monotonic() - started < 3600:
                page = client.get(f"/api/v1/meetings/{meeting_id}/segments", params={"limit": 1}).raise_for_status().json()
                if page["total"] and first_text is None:
                    first_text = time.monotonic() - started
                peak_rss, cpu_end = _resources(process, peak_rss)
                jobs = _jobs(client, meeting_id)
                scope = _scope_snapshot(client, scope_id, args.max_rub)
                report["cloud_budget_scope"] = scope
                if scope.get("uncertain_count", 0) > 0:
                    raise RuntimeError("Benchmark has an uncertain cloud outcome; no resubmit")
                if scope["confirmed_rub"] > args.max_rub:
                    raise RuntimeError("Confirmed benchmark cost exceeded its scope; inspect the retained meeting")
                if jobs and not any(job["status"] in ACTIVE_JOB_STATUSES for job in jobs):
                    settled = True
                    break
                time.sleep(0.5)
            if not settled:
                raise RuntimeError("Benchmark timeout: existing jobs retained; no resubmit")
            items = _transcript(client, meeting_id)
            text = "\n".join(item["text"] for item in items)
            usage = client.get("/api/v1/usage", params={"meeting_id": meeting_id}).raise_for_status().json()
            final_meeting = client.get(f"/api/v1/meetings/{meeting_id}").raise_for_status().json()
            summary_response = client.get(f"/api/v1/meetings/{meeting_id}/summary").raise_for_status()
            complete = bool(jobs) and all(job["status"] == "succeeded" for job in jobs) and summary_response.status_code == 200
            report.update({"cloud": "measured" if complete else "partial or failed processing", "reason": None if complete else "Inspect job and summary statuses",
                           "sample_count": 1, "time_to_first_text_seconds": first_text,
                           "total_seconds": time.monotonic() - started, "jobs": jobs, "usage": usage,
                           "duration_ms": final_meeting.get("duration_ms"), "peak_backend_rss_bytes": peak_rss,
                           "backend_cpu_seconds": cpu_end - cpu_start if cpu_start is not None and cpu_end is not None else None,
                           "resource_sampling_interval_seconds": 0.5,
                           "measurement_note": "First-text time is observed by polling; RSS is the maximum sampled backend RSS, without browser/FFmpeg/provider resources.",
                           "summary_http_status": summary_response.status_code, "summary": summary_response.json(),
                           "statistics_note": "One sample is not p50/p95 statistics. At least 20 independent runs are needed for descriptive percentiles; tail estimates require more."})
            report.update(_scope_cost_metrics(scope, final_meeting.get("duration_ms")))
            if not complete:
                report["confirmed_rub_per_hour"] = None
                report["cost_hourly_note"] = "Full pipeline cost per hour is not qualified by partial processing"
            if reference is not None:
                report["quality"] = accuracy(reference, text)
            if terms is not None:
                report["critical_terms"] = [{"term": term, "exact_present": term.casefold() in text.casefold()} for term in terms]
            exit_code = 0 if complete else 1
        except (Exception, KeyboardInterrupt) as exc:
            report.update({"cloud": "incomplete", "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
                           "total_seconds": time.monotonic() - started if started is not None else None})
            exit_code = 1
        finally:
            scope_confirmed = False
            if meeting_id:
                try:
                    scope = _scope_snapshot(client, scope_id, args.max_rub)
                    report["cloud_budget_scope"] = scope
                    scope_confirmed = not _scope_unresolved(scope)
                except Exception:
                    scope_confirmed = False
            if meeting_id and (not settled or not scope_confirmed):
                try:
                    cloud_change_attempted = True
                    settled = _abort(client, meeting_id, args.max_rub)
                    report["cloud_pause_confirmed"] = True
                    report["jobs"] = _jobs(client, meeting_id)
                    report["usage"] = client.get("/api/v1/usage", params={"meeting_id": meeting_id}).raise_for_status().json()
                    scope = _scope_snapshot(client, scope_id, args.max_rub)
                    report["cloud_budget_scope"] = scope
                    scope_confirmed = not _scope_unresolved(scope)
                except Exception:
                    settled = False
                    scope_confirmed = False
                    report.setdefault("cloud_pause_confirmed", None)
            if scope is not None:
                report.update(_scope_cost_metrics(scope, report.get("duration_ms")))
            if meeting_id is not None and not scope_confirmed:
                report["cost"] = "unknown or outstanding scoped cloud receipt"
            if (meeting_id is not None and not scope_confirmed) or report["cloud"] != "measured":
                report["confirmed_rub_per_hour"] = None
            try:
                if (settled and scope_confirmed) or meeting_id is None:
                    if cloud_change_attempted:
                        _set_configuration(client, prior_config)
                    report["config_restored"] = True
                else:
                    exit_code = 1
                    report["config_restored"] = False
                    report["cleanup_note"] = ("Cloud disabled; reconcile retained scoped jobs/expenses before restoring its original flag."
                                              if report.get("cloud_pause_confirmed") else
                                              "Cloud pause could not be confirmed. Inspect retained jobs and the benchmark scope.")
                    report["prior_config"] = prior_config
            except Exception:
                exit_code = 1
                report["config_restored"] = False
                report["cleanup_note"] = "API unavailable while restoring the cloud flag; inspect backend configuration before another run."
                report["prior_config"] = prior_config
            write_report(args.output, report)
        print(f"Report: {args.output}; existing meeting retained: {meeting_id or 'not created'}; config_restored={report['config_restored']}")
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

