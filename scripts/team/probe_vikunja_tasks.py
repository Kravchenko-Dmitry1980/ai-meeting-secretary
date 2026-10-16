"""Synthetic FREE Vikunja task smoke against an owned hidden loopback instance.

Run with the repository Python, separately from the offline test guard. No owner
configuration, database, credentials, or service is used. ``isolated_vikunja``
and ``provision_team`` are reusable for later adapter integration checks.
"""
from __future__ import annotations

from contextlib import contextmanager
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import html as html_codec
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from typing import Iterator
import uuid

import httpx
import psutil


ROOT = Path(__file__).resolve().parents[2]
STAGES = ("inbox", "accepted", "doing", "blocked", "review", "done", "cancelled")
ZERO_DATE = "0001-01-01T00:00:00Z"
PATCH_HEADERS = {
    "Content-Type": "application/merge-patch+json",
    "X-Vikunja-Format": "html",
}
NATIVE_ERROR_SIGNALS = (
    ("sqlite_busy", b"sqlite_busy"),
    ("database_locked", b"database is locked"),
    ("table_locked", b"database table is locked"),
    ("unique_constraint", b"unique constraint failed"),
    ("database_closed", b"database is closed"),
)


class ProbeFailure(RuntimeError):
    """Only fixed diagnostic codes and synthetic IDs may cross this boundary."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProbeFailure(code)


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(repr=False)
class NativeFixture:
    run_root: Path
    base_url: str
    client: httpx.Client
    evidence: dict
    owner_token: str = field(repr=False, default="")

    def request(self, method: str, path: str, expected: int | tuple[int, ...] = 200, *,
                payload=None, params=None, headers=None):
        require(path.startswith("/") and not path.startswith("//")
                and "://" not in path, "relative_api_path_required")
        response = self.client.request(method, "/api/v2" + path,
                                       json=payload, params=params, headers=headers)
        accepted = (expected,) if isinstance(expected, int) else expected
        if response.status_code not in accepted:
            failure = {"method": method, "path": path, "status": response.status_code}
            try:
                provider_code = response.json().get("code")
                if type(provider_code) is int:
                    failure["provider_code"] = provider_code
            except (ValueError, AttributeError):
                pass
            self.evidence["http_failure"] = failure
        require(response.status_code in accepted,
                f"{method}_{path}_expected_{expected}_got_{response.status_code}")
        if method == "PATCH":
            statuses = self.evidence.setdefault("patch_status_counts", {})
            code = str(response.status_code)
            statuses[code] = statuses.get(code, 0) + 1
        if response.status_code in (204, 304):
            require(not response.content, "204_or_304_response_not_empty")
            return None
        return response.json()

    def use_token(self, token: str) -> None:
        self.client.headers["Authorization"] = "Bearer " + token


@dataclass(repr=False)
class TeamFixture:
    project_id: int
    view_id: int
    buckets: dict[str, int]
    bot_id: int
    member_id: int
    bot_token: str = field(repr=False)
    token_receipt: dict = field(repr=False)


@contextmanager
def isolated_vikunja(*, evidence: dict | None = None) -> Iterator[NativeFixture]:
    """Yield owner-authenticated fixture; always stop only our verified process.

    The token and signing secret remain in memory. Config and result evidence
    contain no credentials. SQLite contains only synthetic fixture accounts.
    All native outgoing HTTP is forced through an owned non-listening loopback
    socket, so accidental license/avatar/webhook requests cannot leave the host.
    """
    evidence = evidence if evidence is not None else {}
    manifest = json.loads((ROOT / "config/team/integration-manifest.json").read_text())
    pin = manifest["vikunja"]
    binaries = list((ROOT / ".runtime/team" / f"vikunja-{pin['version']}").glob("*.exe"))
    require(len(binaries) == 1, "pinned_binary_missing_or_ambiguous")
    executable = binaries[0]
    require(sha256_file(executable) == pin["binary_sha256"], "binary_hash_mismatch")
    require(sha256_file(ROOT / "docs/contracts/vikunja-v2.openapi.json")
            == pin["openapi_sha256"], "pinned_schema_hash_mismatch")
    run_root = ROOT / ".runtime/team-rollout" / f"t4-native-{uuid.uuid4().hex}"
    run_root.mkdir(parents=True)
    (run_root / "files").mkdir()
    evidence.update({
        "qualification": "LOCAL_INTEGRATION", "edition": "FREE",
        "version": pin["version"], "binary_sha256": pin["binary_sha256"],
        "openapi_sha256": pin["openapi_sha256"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "fixture_root": str(run_root), "checks": {}, "result": "FAIL",
    })
    process = None
    output_reader = None
    native_signals = set()
    creation_time = None
    client = None
    # Keep this socket bound without listening throughout the fixture lifetime.
    with socket.socket() as denied_proxy:
        denied_proxy.bind(("127.0.0.1", 0))
        proxy_port = denied_proxy.getsockname()[1]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        config = run_root / "config.yml"
        config.write_text(
            "service:\n"
            f"  interface: '127.0.0.1:{port}'\n"
            f"  publicurl: '{base_url}/'\n"
            f"  rootpath: '{run_root.as_posix()}'\n"
            "  enableregistration: false\n  enableemailreminders: false\n"
            "  enabletaskcomments: true\n"
            "database:\n  type: sqlite\n"
            f"  path: '{(run_root / 'fixture.db').as_posix()}'\n"
            f"files:\n  basepath: '{(run_root / 'files').as_posix()}'\n"
            "cors:\n  enable: false\nmailer:\n  enabled: false\n"
            "sentry:\n  enabled: false\nlog:\n  level: error\n"
            "defaultsettings:\n  avatar_provider: initials\n"
            "autotls:\n  enabled: false\nplugins:\n  enabled: false\n"
            "outgoingrequests:\n"
            f"  proxyurl: 'http://127.0.0.1:{proxy_port}'\n",
            encoding="utf-8",
        )
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("VIKUNJA_", "POLZA_", "MAX_"))
            and key.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
        }
        environment["VIKUNJA_SERVICE_SECRET"] = secrets.token_hex(32)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        def cli(*args: str) -> None:
            try:
                completed = subprocess.run(
                    [str(executable), *args, "--config", str(config)],
                    cwd=run_root, env=environment, capture_output=True,
                    timeout=60, creationflags=flags,
                )
            except subprocess.TimeoutExpired:
                # Never expose command arguments (user-create contains password).
                raise ProbeFailure("fixture_cli_timeout") from None
            require(completed.returncode == 0, "fixture_cli_failed")

        try:
            password = secrets.token_urlsafe(32)
            for username in ("fixture-owner", "fixture-member"):
                cli("user", "create", "--username", username,
                    "--email", username + "@example.invalid", "--password", password)
            process = subprocess.Popen(
                [str(executable), "web", "--config", str(config)],
                cwd=run_root, env=environment, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=flags,
            )
            def scan_native_output() -> None:
                # Drain continuously: a noisy failure must not block the native
                # process on a full pipe. Only one chunk plus a short tail exists.
                tail = b""
                while chunk := process.stdout.read1(4096):
                    scanned = tail + chunk.lower()
                    native_signals.update(code for code, phrase in NATIVE_ERROR_SIGNALS if phrase in scanned)
                    tail = scanned[-128:]

            output_reader = threading.Thread(target=scan_native_output, daemon=True,
                                              name="t4-native-error-scan")
            output_reader.start()
            creation_time = psutil.Process(process.pid).create_time()
            evidence["owned_process"] = {"pid": process.pid, "creation_time": creation_time}
            client = httpx.Client(base_url=base_url, trust_env=False,
                                  follow_redirects=False, timeout=10)
            fixture = NativeFixture(run_root, base_url, client, evidence)
            deadline = time.monotonic() + 30
            while True:
                require(process.poll() is None, "fixture_process_exited_before_ready")
                try:
                    if client.get("/api/v2/info").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                require(time.monotonic() < deadline, "fixture_readiness_timeout")
                time.sleep(0.1)
            schema = fixture.request("GET", "/openapi.json")
            require(schema.get("servers") == [{"url": "/api/v2"},
                    {"url": base_url + "/api/v2"}], "unexpected_schema_servers")
            schema["servers"] = [{"url": "/api/v2"}]
            normalized = json.dumps(schema, indent=2, ensure_ascii=False).encode() + b"\n"
            require(hashlib.sha256(normalized).hexdigest() == pin["openapi_sha256"],
                    "served_schema_hash_mismatch")
            login = fixture.request("POST", "/login", payload={
                "username": "fixture-owner", "password": password})
            fixture.owner_token = login["token"]
            fixture.use_token(fixture.owner_token)
            del password, login
            evidence["checks"]["pinned_binary_and_served_schema"] = "PASS"
            yield fixture
        finally:
            if client is not None:
                client.close()
            if process is not None:
                if process.poll() is None:
                    try:
                        current_creation = psutil.Process(process.pid).create_time()
                    except psutil.NoSuchProcess:
                        current_creation = None
                    require(current_creation == creation_time,
                            "owned_process_identity_changed_cleanup_refused")
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        require(psutil.Process(process.pid).create_time() == creation_time,
                                "owned_process_identity_changed_kill_refused")
                        process.kill()
                        process.wait(timeout=10)
                evidence["owned_process"]["stopped"] = process.poll() is not None
                evidence["owned_process"]["exit_code"] = process.returncode
                if process.stdout is not None:
                    output_reader.join(timeout=5)
                    require(not output_reader.is_alive(), "native_output_scan_incomplete")
                    evidence["native_error_signals"] = sorted(native_signals)
                    process.stdout.close()


def provision_team(fixture: NativeFixture) -> TeamFixture:
    """Create synthetic owner-managed view, then use a project-member bot token."""
    fixture.use_token(fixture.owner_token)
    project = fixture.request("POST", "/projects", 201,
                              payload={"title": "T4 synthetic FREE task smoke"})
    project_id = project["id"]
    member_relation = fixture.request("POST", f"/projects/{project_id}/users", 201,
                                     payload={"username": "fixture-member", "permission": 1})
    bot = fixture.request("POST", "/user/bots", 201, payload={
        "username": "bot-t4-fixture", "name": "Synthetic task bot", "status": 0})
    fixture.request("POST", f"/projects/{project_id}/users", 201,
                    payload={"username": bot["username"], "permission": 1})
    # POST returns ProjectUser.id (relation ID), not the actual user ID.
    project_users = fixture.request("GET", f"/projects/{project_id}/users")["items"]
    members = [user for user in project_users if user["username"] == "fixture-member"]
    require(len(members) == 1 and members[0]["permission"] == 1,
            "fixture_member_not_found_in_project_users")
    member = members[0]
    fixture.evidence["member_identity"] = {
        "username": member["username"], "project_relation_id": member_relation["id"],
        "user_id": member["id"], "permission": member["permission"],
    }
    existing_views = fixture.request("GET", f"/projects/{project_id}/views")["items"]
    # This is a newly owned Team project, never an existing user's project.
    # Native done coupling in any other view would mutate the full task image.
    for existing in existing_views:
        if existing["view_kind"] == "kanban":
            fixture.request("PATCH", f"/projects/{project_id}/views/{existing['id']}", (200, 304),
                            payload={"done_bucket_id": 0}, headers=PATCH_HEADERS)
    view = fixture.request("POST", f"/projects/{project_id}/views", 201, payload={
        "title": "Secretary seven stages", "view_kind": "kanban",
        "bucket_configuration_mode": "manual", "done_bucket_id": 0,
        "filter": {"filter": ""},
    })
    view_id = view["id"]
    auto_buckets = fixture.request("GET", f"/projects/{project_id}/views/{view_id}/buckets")["items"]
    fixture.evidence["auto_created_bucket_titles"] = [bucket["title"] for bucket in auto_buckets]
    buckets = {}
    for position, stage in enumerate(STAGES, start=1):
        bucket = fixture.request("POST", f"/projects/{project_id}/views/{view_id}/buckets",
                                 201, payload={"title": stage, "position": position * 100})
        buckets[stage] = bucket["id"]
    fixture.request("PATCH", f"/projects/{project_id}/views/{view_id}", payload={
        "default_bucket_id": buckets["inbox"], "done_bucket_id": 0,
    }, headers=PATCH_HEADERS)
    # A new manual view automatically receives three empty server-default buckets.
    # Remove only those IDs from this fresh fixture, after setting its own default.
    for bucket in auto_buckets:
        fixture.request("DELETE", f"/projects/{project_id}/views/{view_id}/buckets/{bucket['id']}", 204)
    observed_view = fixture.request("GET", f"/projects/{project_id}/views/{view_id}")
    require(observed_view["bucket_configuration_mode"] == "manual"
            and observed_view["done_bucket_id"] == 0
            and observed_view["default_bucket_id"] == buckets["inbox"],
            "manual_zero_done_bucket_not_retained")
    all_views = fixture.request("GET", f"/projects/{project_id}/views")["items"]
    kanban_done = {view["id"]: view["done_bucket_id"] for view in all_views if view["view_kind"] == "kanban"}
    require(len(kanban_done) >= 2 and all(value == 0 for value in kanban_done.values()),
            "project_has_native_done_coupled_view")
    fixture.evidence["all_project_kanban_done_bucket_ids"] = kanban_done
    fixture.evidence["checks"]["all_owned_project_kanban_views_disable_native_done_coupling"] = "PASS"
    observed_buckets = fixture.request("GET", f"/projects/{project_id}/views/{view_id}/buckets")
    actual = {bucket["title"]: bucket["id"] for bucket in observed_buckets["items"]}
    fixture.evidence["observed_view_buckets"] = actual
    require(len(observed_buckets["items"]) == 7 and actual == buckets,
            "seven_stage_buckets_not_exact")
    routes = fixture.request("GET", "/routes")
    permissions = {
        "projects": ["read_one", "read_all", "views_buckets", "views_buckets_tasks",
                     "views_buckets_tasks_get"],
        "projects_views": ["read_all", "read_one"],
        "projects_users": ["read_all"],
        "tasks": ["create", "read_one", "read_all", "update"],
        "tasks_assignees": ["create", "delete", "read_all", "update_bulk"],
        "labels": ["create", "read_all", "read_one"],
        "tasks_labels": ["create", "delete", "read_all"],
        "tasks_comments": ["create", "read_one", "read_all"],
    }
    for group, actions in permissions.items():
        require(all(action in routes.get(group, {}) for action in actions),
                "required_token_scope_missing")
    token = fixture.request("POST", "/tokens", 201, payload={
        "title": "T4 synthetic bot scopes", "owner_id": bot["id"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "permissions": permissions,
    })
    require(token["owner_id"] == bot["id"], "created_token_owner_not_bot")
    fixture.evidence["created_token_identity"] = {"token_id": token["id"], "owner_id": token["owner_id"]}
    fixture.use_token(token["token"])
    scoped_users = fixture.request("GET", f"/projects/{project_id}/users")["items"]
    scoped_bots = [user for user in scoped_users if user["id"] == bot["id"]]
    require(len(scoped_bots) == 1 and scoped_bots[0]["username"] == "bot-t4-fixture",
            "scoped_project_bot_identity_missing")
    fixture.evidence["project_scoped_bot_identity"] = {
        "id": scoped_bots[0]["id"], "username": scoped_bots[0]["username"],
        "bot_owner_id": scoped_bots[0].get("bot_owner_id"),
        "permission": scoped_bots[0].get("permission"),
    }
    fixture.evidence["token_permissions"] = permissions
    fixture.evidence["fixture_ids"] = {
        "project": project_id, "view": view_id, "bot": bot["id"],
        "member": member["id"], "buckets": buckets,
    }
    fixture.evidence["checks"]["manual_view_zero_done_and_seven_distinct_buckets"] = "PASS"
    return TeamFixture(project_id, view_id, buckets, bot["id"], member["id"], token["token"], token)


def get_task(fixture: NativeFixture, task_id: int) -> dict:
    return fixture.request("GET", f"/tasks/{task_id}", params={"format": "html", "expand": "buckets"})


def task_bucket(fixture: NativeFixture, team: TeamFixture, task_id: int) -> int:
    response = fixture.request("GET", f"/projects/{team.project_id}/views/{team.view_id}/buckets/tasks")
    found = [bucket["id"] for bucket in response["items"]
             if any(task["id"] == task_id for task in (bucket.get("tasks") or []))]
    require(len(found) == 1, "task_missing_or_duplicate_in_manual_view")
    return found[0]


def move_task(fixture: NativeFixture, team: TeamFixture, task_id: int, stage: str) -> None:
    bucket_id = team.buckets[stage]
    fixture.request("PUT", f"/projects/{team.project_id}/views/{team.view_id}/buckets/{bucket_id}/tasks",
                    payload={"task_id": task_id, "project_view_id": team.view_id,
                             "bucket_id": bucket_id})


def exercise_tasks(fixture: NativeFixture, team: TeamFixture) -> None:
    evidence = fixture.evidence
    checks = evidence["checks"]
    origin = "secretary-origin:" + str(uuid.uuid4())
    draft = 'Literal <!-- unfinished\n<script>not executable</script>\n```code & "quote"'
    escaped_draft = "<br>".join(html_codec.escape(line) for line in draft.splitlines())
    html = f"<p><u>Preserve underline</u> <strong>Synthetic task</strong></p><p>{escaped_draft}</p><p>{origin}</p>"
    created = fixture.request("POST", f"/projects/{team.project_id}/tasks", 201, payload={
        "title": "Synthetic review assignment", "description": html,
        "due_date": "2026-10-05T09:00:00Z", "priority": 2,
    }, params={"format": "html"}, headers={"X-Vikunja-Format": "html"})
    task_id = created["id"]
    evidence["fixture_ids"]["task"] = task_id
    initial = get_task(fixture, task_id)
    require(initial["project_id"] == team.project_id
            and initial["created_by"]["id"] == team.bot_id
            and initial["title"] == "Synthetic review assignment"
            and initial["due_date"] == "2026-10-05T09:00:00Z"
            and origin in initial["description"] and "<u>" in initial["description"]
            and "&lt;!-- unfinished" in initial["description"]
            and "&lt;script&gt;" in initial["description"]
            and initial["done"] is False
            and task_bucket(fixture, team, task_id) == team.buckets["inbox"],
            "create_read_postcondition_failed")
    checks["bot_create_and_read"] = "PASS"
    transitions = []
    for done in (False, True):
        fixture.request("PATCH", f"/tasks/{task_id}", (200, 304), payload={"done": done}, headers=PATCH_HEADERS)
        for stage in STAGES:
            move_task(fixture, team, task_id, stage)
            current = get_task(fixture, task_id)
            require(current["done"] is done and task_bucket(fixture, team, task_id) == team.buckets[stage],
                    f"move_{stage}_changed_done_or_wrong_bucket")
            require(current["description"] == initial["description"], "move_changed_html_description")
            transitions.append({"stage": stage, "done": done})
            for toggled in (not done, done):
                previous_buckets = {item["project_view_id"]: item["id"] for item in current["buckets"]}
                fixture.request("PATCH", f"/tasks/{task_id}", (200, 304), payload={"done": toggled}, headers=PATCH_HEADERS)
                current = get_task(fixture, task_id)
                require(current["done"] is toggled
                        and task_bucket(fixture, team, task_id) == team.buckets[stage],
                        f"patch_done_{stage}_moved_bucket_or_wrong_done")
                require(current["description"] == initial["description"], "patch_done_changed_html_description")
                changed_other_views = [
                    {"view": item["project_view_id"], "before": previous_buckets.get(item["project_view_id"]),
                     "after": item["id"], "done": toggled}
                    for item in current["buckets"] if item["project_view_id"] != team.view_id
                    and previous_buckets.get(item["project_view_id"]) != item["id"]
                ]
                if changed_other_views:
                    observed = evidence.setdefault("done_patch_other_view_side_effects", [])
                    observed.extend(change for change in changed_other_views if change not in observed)
                require(not changed_other_views, "patch_done_changed_other_project_view")
    evidence["stage_done_move_postconditions"] = transitions
    checks["moves_preserve_done_false_and_true_in_all_seven_buckets"] = "PASS"
    checks["patch_done_preserves_each_manual_bucket_both_directions"] = "PASS"
    checks["patch_done_preserves_all_other_project_view_buckets"] = "PASS"

    for user_id in (team.bot_id, team.member_id):
        fixture.request("PUT", f"/tasks/{task_id}/assignees/bulk",
                        payload={"assignees": [{"id": user_id}]})
        current = get_task(fixture, task_id)
        require([user["id"] for user in current["assignees"]] == [user_id],
                "bulk_assignee_get_postcondition_failed")
        expected_username = "bot-t4-fixture" if user_id == team.bot_id else "fixture-member"
        require(current["assignees"][0]["username"] == expected_username,
                "bulk_assignee_username_postcondition_failed")
    fixture.request("POST", f"/tasks/{task_id}/assignees", 201, payload={"user_id": team.bot_id})
    require({user["id"] for user in get_task(fixture, task_id)["assignees"]}
            == {team.bot_id, team.member_id}, "add_assignee_postcondition_failed")
    fixture.request("DELETE", f"/tasks/{task_id}/assignees/{team.bot_id}", 204)
    require([user["id"] for user in get_task(fixture, task_id)["assignees"]] == [team.member_id],
            "remove_assignee_postcondition_failed")
    evidence["member_identity"]["assigned_username"] = get_task(fixture, task_id)["assignees"][0]["username"]
    require(evidence["member_identity"]["assigned_username"] == "fixture-member",
            "final_member_identity_mismatch")
    checks["assign_bot_member_bulk_add_remove"] = "PASS"
    checks["project_relation_id_distinct_from_verified_assignee_identity"] = "PASS"

    labels = {}
    for kind in ("important", "urgent", "cancelled"):
        label = fixture.request("POST", "/labels", 201, payload={"title": "secretary:" + kind})
        read_label = fixture.request("GET", f"/labels/{label['id']}")
        require(read_label["created_by"]["id"] == team.bot_id
                and read_label["title"] == "secretary:" + kind, "bot_label_ownership_failed")
        labels[kind] = label["id"]
        fixture.request("POST", f"/tasks/{task_id}/labels", 201, payload={"label_id": label["id"]})
        require({item["id"] for item in get_task(fixture, task_id)["labels"]} == set(labels.values()),
                "label_attach_get_postcondition_failed")
    evidence["fixture_ids"]["labels"] = labels
    fixture.request("DELETE", f"/tasks/{task_id}/labels/{labels['cancelled']}", 204)
    require({item["id"] for item in get_task(fixture, task_id)["labels"]}
            == {labels["important"], labels["urgent"]}, "label_remove_get_postcondition_failed")
    checks["bot_owned_important_urgent_cancelled_labels_attach_remove"] = "PASS"

    fixture.request("PATCH", f"/tasks/{task_id}", payload={"due_date": ZERO_DATE}, headers=PATCH_HEADERS)
    current = get_task(fixture, task_id)
    require(current["due_date"] == ZERO_DATE, "zero_date_does_not_clear_due_date")
    require(current["description"] == initial["description"], "patch_due_date_changed_html_description")
    evidence["cleared_due_date"] = current["due_date"]
    checks["due_date_clear_zero_time"] = "PASS"
    before_title_patch = get_task(fixture, task_id)
    fixture.request("PATCH", f"/tasks/{task_id}", payload={"title": "Synthetic renamed only"}, headers=PATCH_HEADERS)
    after_title_patch = get_task(fixture, task_id)
    require(after_title_patch["title"] == "Synthetic renamed only"
            and after_title_patch["description"] == before_title_patch["description"],
            "title_patch_changed_html_description")
    for field_name in ("done", "due_date", "priority", "assignees", "labels", "project_id"):
        require(after_title_patch[field_name] == before_title_patch[field_name],
                f"title_patch_changed_{field_name}")
    checks["title_only_patch_preserves_html_and_other_task_fields"] = "PASS"

    command_marker = "secretary-command:" + str(uuid.uuid4())
    comment = fixture.request("POST", f"/tasks/{task_id}/comments", 201,
                              payload={"comment": f"<p>Synthetic comment {command_marker}</p>"},
                              params={"format": "html"})
    require(comment["author"]["id"] == team.bot_id, "comment_create_author_not_bot")
    read_comment = fixture.request("GET", f"/tasks/{task_id}/comments/{comment['id']}",
                                   params={"format": "html"})
    comments = fixture.request("GET", f"/tasks/{task_id}/comments", params={"format": "html"})
    require(command_marker in read_comment["comment"]
            and read_comment["author"]["id"] == team.bot_id
            and [item["id"] for item in comments["items"] if command_marker in item["comment"]]
            == [comment["id"]], "comment_marker_roundtrip_failed")
    evidence["fixture_ids"]["comment"] = comment["id"]
    checks["bot_comment_marker_single_and_collection_get"] = "PASS"

    move_task(fixture, team, task_id, "done")
    fixture.request("PATCH", f"/tasks/{task_id}", (200, 304), payload={"done": True}, headers=PATCH_HEADERS)
    require(get_task(fixture, task_id)["done"] is True
            and task_bucket(fixture, team, task_id) == team.buckets["done"],
            "close_get_postcondition_failed")
    checks["explicit_close_verified"] = "PASS"
    # A separate cancelled task proves the distinct terminal status mapping.
    cancelled = fixture.request("POST", f"/projects/{team.project_id}/tasks", 201,
                                payload={"title": "Synthetic cancelled task"})
    fixture.request("POST", f"/tasks/{cancelled['id']}/labels", 201,
                    payload={"label_id": labels["cancelled"]})
    move_task(fixture, team, cancelled["id"], "cancelled")
    fixture.request("PATCH", f"/tasks/{cancelled['id']}", payload={"done": True}, headers=PATCH_HEADERS)
    current = get_task(fixture, cancelled["id"])
    require(current["done"] is True
            and task_bucket(fixture, team, cancelled["id"]) == team.buckets["cancelled"]
            and {label["id"] for label in current["labels"]} == {labels["cancelled"]},
            "cancelled_get_postcondition_failed")
    evidence["fixture_ids"]["cancelled_task"] = cancelled["id"]
    checks["cancelled_distinct_bucket_label_and_done_verified"] = "PASS"


def exercise_service_tasks(fixture: NativeFixture, team: TeamFixture) -> None:
    """Exercise the actual T4 adapter/service/repository, never a stub or provider."""
    backend = str(ROOT / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from secretary.application.team_tasks import TeamTaskService
    from secretary.domain.team import TaskCommand, TaskOrigin, TeamMember
    from secretary.infrastructure.team_database import TeamDatabase
    from secretary.infrastructure.team_repository import TeamRepository
    from secretary.infrastructure.vikunja import (
        MappingMemberDirectory, ProjectBinding, ProvisionedTokenBinding, TaskReadContext, VikunjaClient,
        VikunjaError, has_visible_marker, visible_text,
    )

    evidence = fixture.evidence
    fixture.use_token(fixture.owner_token)
    remote_owner = fixture.request("GET", "/user")
    require(remote_owner["username"] == "fixture-owner", "service_fixture_owner_identity_failed")
    fixture.use_token(team.bot_token)
    labels = {}
    for kind in ("important", "urgent", "cancelled"):
        created = fixture.request("POST", "/labels", 201,
                                  payload={"title": "secretary:" + kind})
        read = fixture.request("GET", f"/labels/{created['id']}")
        require(read["created_by"]["id"] == team.bot_id, "service_fixture_label_owner_failed")
        labels[kind] = str(created["id"])
    project = str(team.project_id)
    owner = TeamMember(id=str(uuid.uuid4()), display_name="Synthetic owner", role="owner",
                       max_user_id="9007199254741001", vikunja_user_id=str(remote_owner["id"]),
                       project_ids=(project,))
    member = TeamMember(id=str(uuid.uuid4()), display_name="Synthetic member",
                        max_user_id="9007199254741002", vikunja_user_id=str(team.member_id),
                        project_ids=(project,))
    repository = TeamRepository(TeamDatabase(fixture.run_root / "service-team.db"))
    for account in (owner, member):
        repository.upsert_member(account, expected_revision=None)
    binding = ProjectBinding(project_id=project, manual_view_id=str(team.view_id),
        bot_user_id=str(team.bot_id), bucket_ids={stage: str(value) for stage, value in team.buckets.items()},
        important_label_id=labels["important"], urgent_label_id=labels["urgent"],
        cancelled_label_id=labels["cancelled"])
    operations = []
    evidence["service_operations"] = operations
    with httpx.Client(base_url=fixture.base_url + "/api/v2", trust_env=False,
                      follow_redirects=False, timeout=10,
                      headers={"Authorization": "Bearer " + team.bot_token}) as transport:
        client = VikunjaClient(transport, binding=binding,
                               members=MappingMemberDirectory((owner, member)),
                               credential_binding=ProvisionedTokenBinding.from_creation_receipt(
                                   team.token_receipt, expected_bot_id=str(team.bot_id)))
        try:
            client.validate_binding()
        except VikunjaError as error:
            evidence["service_adapter_error"] = {"code": error.code, "status": error.status_code}
            raise ProbeFailure("service_validate_binding_failed") from None
        evidence["checks"]["actual_adapter_validate_binding"] = "PASS"
        service = TeamTaskService(repository, client)

        def submit(action, values, *, baseline=None, actor=owner):
            operation_id = str(uuid.uuid4())
            arguments = {"operation_id": operation_id, "project_id": project,
                         "action": action, "values": values}
            if baseline is not None:
                arguments.update(task_id=baseline.task_id, expected_revision=baseline.revision,
                                 expected_fingerprint=baseline.remote_fingerprint)
            else:
                arguments["origin"] = TaskOrigin(source_kind="manual")
            command = TaskCommand(**arguments)
            accepted = repository.accept_command(actor.id, command)
            require(accepted.acceptance_receipt.decision == "accepted", "service_command_not_accepted")
            claim = repository.claim_command("native-service-fixture", lease_seconds=120)
            require(claim is not None and claim.command.operation_id == operation_id,
                    "service_claim_not_owned")
            service.execute(claim)
            receipt = repository.get_receipt(actor.id, operation_id)
            entry = {"operation_id": operation_id, "action": action,
                     "execution_state": receipt.execution_state.state,
                     "error_code": receipt.execution_state.error_code,
                     "task_id": receipt.execution_state.task_id}
            operations.append(entry)
            if receipt.execution_state.state != "applied":
                # Failed executions have released their live lease. Inspect only
                # this fixture's durable journal; do not claim or replay them.
                with repository.db.connection() as connection:
                    progress = repository._remote_progress(connection, operation_id)
                entry["steps"] = []
                for step in progress["steps"]:
                    safe = {key: step.get(key) for key in
                            ("ordinal", "state", "error_code", "remote_entity_id")}
                    safe["kind"] = progress["plan"][step["ordinal"]]["kind"]
                    entry["steps"].append(safe)
                raise ProbeFailure("service_command_not_applied_" + action)
            require(receipt.current is not None, "service_applied_without_projection")
            require(receipt.acceptance_receipt == accepted.acceptance_receipt,
                    "service_acceptance_receipt_changed")
            # A fresh adapter GET after the persisted receipt must still match it.
            verified = client.get_task(receipt.current.task_id,
                context=TaskReadContext(revision=receipt.current.revision, baseline=receipt.current))
            require(verified.remote_fingerprint == receipt.current.remote_fingerprint,
                    "service_receipt_get_fingerprint_mismatch")
            require(verified == receipt.current, "service_receipt_get_projection_mismatch")
            entry["verified_revision"] = verified.revision
            entry["get_verified"] = True
            return receipt.current, command

        literal = 'Literal <!-- unfinished\n<script>not executable</script>\n```code & "quote"'
        current, create_command = submit("create", {
            "title": "Actual service synthetic task", "description": literal,
            "assignee_id": owner.id,
        })
        require(current.assignee_id == owner.id and current.bucket == "inbox"
                and literal in visible_text(current.description)
                and has_visible_marker(current.description, "secretary-origin:" + create_command.operation_id),
                "service_literal_create_postcondition_failed")
        original_html = current.description
        current, _ = submit("assign", {"assignee_id": member.id}, baseline=current)
        require(current.assignee_id == member.id, "service_assign_postcondition_failed")
        current, _ = submit("set_state", {"bucket": "doing"}, baseline=current, actor=member)
        require(current.bucket == "doing", "service_doing_postcondition_failed")
        current, _ = submit("classify", {"important": False, "urgent": True,
                                         "classification_confirmed": True}, baseline=current)
        require(current.classification_confirmed and current.important is False and current.urgent is True,
                "service_classify_postcondition_failed")
        due = datetime(2026, 10, 6, 9, tzinfo=timezone.utc)
        current, _ = submit("set_due", {"due_at": due, "due_confirmed": True,
                                        "reason": "Synthetic deadline"}, baseline=current)
        require(current.due_at == due, "service_due_postcondition_failed")
        current, _ = submit("set_due", {"due_at": None, "due_confirmed": True,
                                        "reason": "Synthetic clear"}, baseline=current)
        require(current.due_at is None, "service_due_clear_postcondition_failed")
        current, _ = submit("rename", {"title": "Actual service renamed only"}, baseline=current)
        require(current.title == "Actual service renamed only", "service_rename_postcondition_failed")
        current, done_command = submit("set_state", {"bucket": "done", "result": "Synthetic result <!-- literal"},
                                       baseline=current, actor=member)
        require(current.bucket == "done" and current.description == original_html,
                "service_done_or_html_postcondition_failed")
        raw_done = get_task(fixture, int(current.task_id))
        done_comments = fixture.request("GET", f"/tasks/{current.task_id}/comments", params={"format": "html"})["items"]
        result_comments = [comment for comment in done_comments
                           if "secretary-command:" + done_command.operation_id + ":" in comment["comment"]]
        require(raw_done["done"] is True and raw_done["due_date"] == ZERO_DATE
                and raw_done["description"] == original_html
                and raw_done["title"] == "Actual service renamed only"
                and [user["id"] for user in raw_done["assignees"]] == [team.member_id]
                and {str(label["id"]) for label in raw_done["labels"]} == {labels["urgent"]}
                and task_bucket(fixture, team, int(current.task_id)) == team.buckets["done"]
                and len(result_comments) == 1 and result_comments[0]["author"]["id"] == team.bot_id
                and "Synthetic result <!-- literal" in html_codec.unescape(result_comments[0]["comment"])
                and member.id in result_comments[0]["comment"], "service_raw_done_postcondition_failed")
        evidence["checks"]["actual_service_create_assign_doing_classify_due_clear_rename_done"] = "PASS"
        cancelled, _ = submit("create", {"title": "Actual service owner cancellation",
                                          "description": "Synthetic cancellation", "assignee_id": owner.id})
        cancelled, cancelled_command = submit("set_state", {"bucket": "cancelled", "result": "Synthetic owner cancelled"},
                                              baseline=cancelled)
        require(cancelled.bucket == "cancelled", "service_cancelled_postcondition_failed")
        raw_cancelled = get_task(fixture, int(cancelled.task_id))
        cancelled_comments = fixture.request("GET", f"/tasks/{cancelled.task_id}/comments", params={"format": "html"})["items"]
        cancellation_results = [comment for comment in cancelled_comments
                                if "secretary-command:" + cancelled_command.operation_id + ":" in comment["comment"]]
        require(raw_cancelled["done"] is True
                and {str(label["id"]) for label in raw_cancelled["labels"]} == {labels["cancelled"]}
                and task_bucket(fixture, team, int(cancelled.task_id)) == team.buckets["cancelled"]
                and len(cancellation_results) == 1
                and cancellation_results[0]["author"]["id"] == team.bot_id
                and "Synthetic owner cancelled" in cancellation_results[0]["comment"]
                and owner.id in cancellation_results[0]["comment"], "service_raw_cancelled_postcondition_failed")
        evidence["checks"]["actual_service_owner_cancellation"] = "PASS"
        evidence["checks"]["actual_service_terminal_raw_get_and_result_comments"] = "PASS"
        require(len(operations) == 10 and all(entry.get("get_verified") for entry in operations),
                "service_operations_not_all_verified")
        evidence["checks"]["actual_service_all_receipts_applied_and_fresh_get_verified"] = "PASS"


def exercise_native_burst(fixture: NativeFixture, team: TeamFixture) -> None:
    """Twenty distinct operations, each GET-verified; no retry after any failure."""
    operations = []
    fixture.evidence["native_burst_operations"] = operations
    for index in range(20):
        entry = {"operation_id": str(uuid.uuid4()), "index": index, "create_verified": False,
                 "move_verified": False}
        operations.append(entry)
        created = fixture.request("POST", f"/projects/{team.project_id}/tasks", 201,
            payload={"title": f"Synthetic burst task {index}",
                     "description": "secretary-origin:" + entry["operation_id"]})
        task_id = created["id"]
        entry["task_id"] = task_id
        current = get_task(fixture, task_id)
        require(current["title"] == f"Synthetic burst task {index}" and current["done"] is False
                and current["project_id"] == team.project_id and current["created_by"]["id"] == team.bot_id
                and task_bucket(fixture, team, task_id) == team.buckets["inbox"],
                "burst_create_get_postcondition_failed")
        entry["create_verified"] = True
        stage = STAGES[(index % 6) + 1]
        move_task(fixture, team, task_id, stage)
        current = get_task(fixture, task_id)
        require(current["done"] is False
                and task_bucket(fixture, team, task_id) == team.buckets[stage],
                "burst_move_get_postcondition_failed")
        entry.update(move_verified=True, bucket_id=team.buckets[stage])
    require(len(operations) == 20 and all(item["create_verified"] and item["move_verified"] for item in operations),
            "native_burst_not_complete")
    fixture.evidence["checks"]["twenty_distinct_native_create_move_operations_get_verified"] = "PASS"


def probe(*, basic_only: bool = False) -> dict:
    evidence = {"result": "FAIL", "checks": {}}
    try:
        with isolated_vikunja(evidence=evidence) as fixture:
            team = provision_team(fixture)
            exercise_tasks(fixture, team)
            if not basic_only:
                exercise_service_tasks(fixture, team)
                exercise_native_burst(fixture, team)
        require(evidence["owned_process"]["stopped"], "fixture_process_not_stopped")
        evidence["checks"]["owned_pid_and_creation_time_cleanup"] = "PASS"
        evidence["result"] = "PASS"
    except ProbeFailure as error:
        evidence["failure_code"] = str(error)
    except Exception as error:
        # No raw exception, trace, response, environment, or CLI arguments.
        evidence["failure_code"] = "unexpected_" + type(error).__name__
    if "fixture_root" in evidence:
        destination = Path(evidence["fixture_root"]) / "result.json"
        evidence["evidence_file"] = str(destination)
        destination.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basic-only", action="store_true", help="Skip actual adapter/service integration")
    arguments = parser.parse_args()
    result = probe(basic_only=arguments.basic_only)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result["result"] == "PASS" else 1)
