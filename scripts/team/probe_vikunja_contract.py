"""Owned, synthetic localhost contract probe; never reads Secretary data or keys.

Run separately from run_offline_tests.py: this is LOCAL_INTEGRATION evidence.
Creates a fresh fixture database and stops only the process it spawned.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
import traceback
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[2]


def probe(*, export_schema: bool = False) -> dict:
    manifest = json.loads((ROOT / "config/team/integration-manifest.json").read_text())
    pin = manifest["vikunja"]
    runtime = ROOT / ".runtime/team" / f"vikunja-{pin['version']}"
    binaries = list(runtime.glob("*.exe"))
    if len(binaries) != 1:
        raise RuntimeError("pinned_installation_missing")
    executable = binaries[0]
    with executable.open("rb") as binary:
        if hashlib.file_digest(binary, "sha256").hexdigest() != pin["binary_sha256"]:
            raise RuntimeError("installed_binary_hash_mismatch")
    run_root = ROOT / ".runtime/team-rollout" / f"t1-smoke-{uuid.uuid4().hex}"
    run_root.mkdir()
    (run_root / "files").mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = run_root / "config.yml"
    config.write_text(
        "service:\n"
        f"  interface: '127.0.0.1:{port}'\n"
        f"  publicurl: 'http://127.0.0.1:{port}/'\n"
        f"  rootpath: '{run_root.as_posix()}'\n"
        f"  secret: '{secrets.token_hex(32)}'\n"
        "  enableregistration: false\n"
        "database:\n  type: sqlite\n"
        f"  path: '{(run_root / 'fixture.db').as_posix()}'\n"
        f"files:\n  basepath: '{(run_root / 'files').as_posix()}'\n"
        "cors:\n  enable: false\nmailer:\n  enabled: false\n"
        "sentry:\n  enabled: false\nlog:\n  level: warning\n",
        encoding="utf-8",
    )
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("VIKUNJA_", "POLZA_", "MAX_"))}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    def cli(*args):
        result = subprocess.run([str(executable), *args, "--config", str(config)],
                                cwd=run_root, env=environment, capture_output=True,
                                timeout=60, creationflags=flags)
        if result.returncode:
            raise RuntimeError("fixture_cli_failed")
        return result.stdout.decode("utf-8", errors="replace").strip()
    version = cli("version")
    password = secrets.token_urlsafe(24)
    for username in ("fixture-owner", "fixture-pavel", "fixture-alexey"):
        cli("user", "create", "--username", username,
            "--email", username + "@example.invalid", "--password", password)
    log = (run_root / "process.log").open("wb")
    process = subprocess.Popen([str(executable), "web", "--config", str(config)],
                               cwd=run_root, env=environment, stdout=log, stderr=log,
                               creationflags=flags)
    result = {"qualification": "LOCAL_INTEGRATION", "version": version,
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "fixture_root": str(run_root), "checks": {}}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                          follow_redirects=False, timeout=10) as client:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError("fixture_server_exited")
                try:
                    health = client.get("/api/v2/info")
                    if health.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("fixture_server_not_ready")
                time.sleep(0.1)
            schema_response = client.get("/api/v2/openapi.json")
            if schema_response.status_code != 200:
                raise RuntimeError("openapi_unavailable")
            schema = schema_response.json()
            result["raw_openapi_sha256"] = hashlib.sha256(schema_response.content).hexdigest()
            # Only the ephemeral fixture URL varies; preserve original bytes as evidence.
            (run_root / "openapi.raw.json").write_bytes(schema_response.content)
            if schema.get("servers") != [{"url": "/api/v2"},
                                         {"url": f"http://127.0.0.1:{port}/api/v2"}]:
                raise RuntimeError("unexpected_openapi_servers")
            schema["servers"] = [{"url": "/api/v2"}]
            schema_bytes = json.dumps(schema, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
            result["openapi_sha256"] = hashlib.sha256(schema_bytes).hexdigest()
            (run_root / "openapi.json").write_bytes(schema_bytes)
            if export_schema:
                destination = ROOT / "docs/contracts/vikunja-v2.openapi.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.read_bytes() != schema_bytes:
                    raise RuntimeError("existing_contract_diff_requires_review")
                destination.write_bytes(schema_bytes)
            result["checks"]["openapi"] = "PASS"
            login = client.post("/api/v2/login", json={"username": "fixture-owner", "password": password})
            if login.status_code != 200:
                raise RuntimeError(f"fixture_login_status_{login.status_code}")
            client.headers["Authorization"] = "Bearer " + login.json()["token"]
            result["checks"]["free_cli_users_and_login"] = "PASS"
            # Further assertions use only endpoints from this exported schema.
            routes = client.get("/api/v2/routes")
            if routes.status_code != 200:
                raise RuntimeError(f"fixture_routes_status_{routes.status_code}")
            (run_root / "routes.json").write_text(json.dumps(routes.json(), indent=2), encoding="utf-8")
            result["checks"]["routes"] = "PASS"
            def request(method, path, status, *, payload=None, headers=None, params=None):
                response = client.request(method, "/api/v2" + path, json=payload,
                                          headers=headers, params=params)
                if response.status_code != status:
                    raise RuntimeError(f"{method}_{path}_expected_{status}_got_{response.status_code}")
                if status == 204:
                    if response.content:
                        raise RuntimeError("delete_204_nonempty")
                    return response, None
                return response, response.json()

            project = request("POST", "/projects", 201, payload={"title": "Synthetic team"})[1]
            unrelated = request("POST", "/projects", 201, payload={"title": "Synthetic unrelated"})[1]
            project_id = project["id"]
            member_relations = []
            for username in ("fixture-pavel", "fixture-alexey"):
                member = request("POST", f"/projects/{project_id}/users", 201,
                                 payload={"username": username, "permission": 1})[1]
                member_relations.append(member)
            # POST ProjectUser.id identifies the sharing relation, not its user.
            project_users = request("GET", f"/projects/{project_id}/users", 200)[1]["items"]
            members = []
            for relation in member_relations:
                matches = [user for user in project_users if user["username"] == relation["username"]]
                if len(matches) != 1 or matches[0]["permission"] != 1:
                    raise RuntimeError("fixture_member_identity_not_verified")
                members.append(matches[0])
            result["member_identity"] = [
                {"username": user["username"], "user_id": user["id"],
                 "project_relation_id": relation["id"]}
                for relation, user in zip(member_relations, members)
            ]
            bot = request("POST", "/user/bots", 201,
                          payload={"username": "bot-fixture", "name": "Fixture bot", "status": 0})[1]
            request("POST", f"/projects/{project_id}/users", 201,
                    payload={"username": bot["username"], "permission": 1})
            result["checks"]["free_project_members_and_bot"] = "PASS"
            permissions = {
                "projects": ["read_one", "read_all", "views_buckets", "views_buckets_put",
                             "views_buckets_post", "views_buckets_tasks", "views_buckets_tasks_get"],
                "projects_views": ["read_all", "read_one", "update"],
                "tasks": ["create", "read_one", "read_all", "update", "delete"],
                "tasks_assignees": ["create", "read_all", "delete"],
                "labels": ["create", "read_all", "read_one"],
                "tasks_labels": ["create", "read_all", "delete"],
            }
            available = routes.json()
            for group, actions in permissions.items():
                if any(action not in available.get(group, {}) for action in actions):
                    raise RuntimeError("required_scope_not_in_routes")
            token = request("POST", "/tokens", 201, payload={
                "title": "Synthetic scope", "owner_id": bot["id"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                "permissions": permissions})[1]
            client.headers["Authorization"] = "Bearer " + token["token"]
            page = request("GET", "/projects", 200, params={"page": 1, "per_page": 1})[1]
            if [p["id"] for p in page["items"]] != [project_id]:
                raise RuntimeError("bot_project_isolation_failed")
            denied = client.get(f"/api/v2/projects/{unrelated['id']}")
            if denied.status_code not in (403, 404):
                raise RuntimeError("bot_reads_unrelated_project")
            denied_token = client.post("/api/v2/tokens", json={"title": "Denied"})
            if denied_token.status_code not in (401, 403, 404):
                raise RuntimeError(f"bot_token_scope_escalation_status_{denied_token.status_code}")
            result["token_creation_denial_status"] = denied_token.status_code
            result["checks"]["project_scoped_token"] = "PASS"
            result["token_permissions"] = permissions
            task = request("POST", f"/projects/{project_id}/tasks", 201, payload={
                "title": "Synthetic task", "description": "secretary-origin:synthetic",
                "due_date": "2026-10-05T09:00:00Z", "priority": 2})[1]
            task_id = task["id"]
            request("POST", f"/projects/{project_id}/tasks", 201,
                    payload={"title": "Synthetic pagination task"})
            first = request("GET", f"/projects/{project_id}/tasks", 200,
                            params={"page": 1, "per_page": 1})[1]
            second = request("GET", f"/projects/{project_id}/tasks", 200,
                             params={"page": 2, "per_page": 1})[1]
            if (len(first["items"]) != 1 or len(second["items"]) != 1
                    or first["items"][0]["id"] == second["items"][0]["id"]
                    or first["total"] != 2 or first["total_pages"] != 2):
                raise RuntimeError("pagination_contract_failed")
            result["checks"]["create_201_and_pagination"] = "PASS"
            empty = request("GET", f"/projects/{project_id}/tasks", 200,
                            params={"page": 3, "per_page": 1})[1]
            if empty["items"] not in ([], None) or empty["total"] != 2:
                raise RuntimeError("empty_page_contract_failed")
            result["empty_page_items_shape"] = type(empty["items"]).__name__
            read, current = request("GET", f"/tasks/{task_id}", 200)
            etag = read.headers.get("ETag")
            if not etag:
                raise RuntimeError("task_etag_missing")
            cache = client.get(f"/api/v2/tasks/{task_id}", headers={"If-None-Match": etag})
            result["conditional_get_status"] = cache.status_code
            changed = dict(task, title="Synthetic updated")
            request("PUT", f"/tasks/{task_id}", 200, payload=changed)
            stale = client.put(f"/api/v2/tasks/{task_id}",
                               json=dict(changed, title="Stale synthetic write"),
                               headers={"If-Match": etag})
            stale_patch = client.patch(f"/api/v2/tasks/{task_id}",
                                      json={"title": "Stale synthetic patch"},
                                      headers={"If-Match": etag,
                                               "Content-Type": "application/merge-patch+json"})
            result["conditional_put_status"] = stale.status_code
            result["conditional_patch_status"] = stale_patch.status_code
            result["remote_cas_enforced"] = stale.status_code == stale_patch.status_code == 412
            result["checks"]["etag_behavior_observed"] = "PASS"
            invalid = client.post(f"/api/v2/projects/{project_id}/tasks", json={"title": ""})
            if invalid.status_code != 422 or "application/problem+json" not in invalid.headers.get("Content-Type", ""):
                raise RuntimeError("problem_json_contract_failed")
            result["checks"]["validation_problem_json_422"] = "PASS"
            # User ID in project membership is the user's provider ID.
            request("POST", f"/tasks/{task_id}/assignees", 201, payload={"user_id": members[0]["id"]})
            label = request("POST", "/labels", 201, payload={"title": "Secretary Important"})[1]
            request("POST", f"/tasks/{task_id}/labels", 201, payload={"label_id": label["id"]})
            assigned = request("GET", f"/tasks/{task_id}", 200)[1]
            if ({a["id"] for a in assigned["assignees"]} != {members[0]["id"]}
                    or {a["username"] for a in assigned["assignees"]} != {"fixture-pavel"}
                    or {l["id"] for l in assigned["labels"]} != {label["id"]}
                    or assigned["id"] != task_id or assigned["project_id"] != project_id):
                raise RuntimeError("assignment_or_label_not_persisted")
            result["checks"]["assignees_and_labels"] = "PASS"
            result["checks"]["assignment_uses_verified_user_id_and_username"] = "PASS"
            views = request("GET", f"/projects/{project_id}/views", 200)[1]["items"]
            kanban = next(view for view in views if view["view_kind"] == "kanban")
            view_id = kanban["id"]
            buckets = request("GET", f"/projects/{project_id}/views/{view_id}/buckets", 200)[1]
            bucket = request("POST", f"/projects/{project_id}/views/{view_id}/buckets", 201,
                             payload={"title": "Synthetic progress", "position": 200})[1]
            request("PUT", f"/projects/{project_id}/views/{view_id}/buckets/{bucket['id']}/tasks", 200,
                    payload={"task_id": task_id, "project_view_id": view_id, "bucket_id": bucket["id"]})
            bucket_tasks = request("GET", f"/projects/{project_id}/views/{view_id}/buckets/tasks", 200)[1]
            matching = [b for b in bucket_tasks["items"] if b["id"] == bucket["id"]]
            if len(matching) != 1 or task_id not in {t["id"] for t in (matching[0]["tasks"] or [])}:
                raise RuntimeError("task_not_in_requested_bucket")
            if any(task_id in {t["id"] for t in (b.get("tasks") or [])}
                   for b in bucket_tasks["items"] if b["id"] != bucket["id"]):
                raise RuntimeError("task_in_multiple_view_buckets")
            result["checks"]["free_buckets_and_move"] = "PASS"
            result["bucket_collection_shape"] = type(buckets).__name__
            result["bucket_tasks_shape"] = type(bucket_tasks).__name__
            request("DELETE", f"/tasks/{task_id}", 204)
            result["checks"]["delete_empty_204"] = "PASS"
            # This deployment never relies on external self-registration.
            public_register = client.post("/api/v2/register", json={
                "username": "fixture-denied", "email": "denied@example.invalid", "password": password},
                headers={"Authorization": ""})
            if public_register.status_code not in (403, 404):
                raise RuntimeError("public_registration_not_disabled")
            result["checks"]["registration_disabled"] = "PASS"
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
    (run_root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-schema", action="store_true")
    arguments = parser.parse_args()
    try:
        print(json.dumps(probe(export_schema=arguments.export_schema), indent=2))
    except (RuntimeError, OSError, httpx.HTTPError, KeyError, ValueError) as exc:
        # No HTTP body, token, password or complete request goes into output.
        print(json.dumps({"qualification": "FAIL", "code": str(exc) if isinstance(exc, RuntimeError)
                          else type(exc).__name__, "at": [
                              {"file": Path(frame.filename).name, "line": frame.lineno}
                              for frame in traceback.extract_tb(exc.__traceback__)[-3:]]}))
        raise SystemExit(1)
