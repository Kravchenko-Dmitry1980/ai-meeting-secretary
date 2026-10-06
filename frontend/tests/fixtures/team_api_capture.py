"""Export real, isolated Gateway responses for the T13 client/store test.

Only the external Vikunja wire is replaced by HTTPX MockTransport. The public
ASGI routes, authorization, SQLite repositories, mapper, worker and receipts
are actual application code. The offline guard denies owner data and network.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "backend"), str(ROOT / "tests")]
from run_offline_tests import SCRATCH, install_guard  # noqa: E402

install_guard()

from fastapi.testclient import TestClient  # noqa: E402
from secretary.application.team_sync import TeamSyncService  # noqa: E402
from secretary.domain.team import TeamMember, TaskSnapshot  # noqa: E402
from secretary.infrastructure.team_auth_repository import AuthRepository  # noqa: E402
from secretary.infrastructure.team_database import TeamDatabase  # noqa: E402
from secretary.infrastructure.team_read_repository import TeamReadRepository  # noqa: E402
from secretary.infrastructure.team_repository import TeamRepository  # noqa: E402
from secretary.infrastructure.team_sync_repository import TeamSyncRepository  # noqa: E402
from secretary.interface.team_gateway import (  # noqa: E402
    TeamGatewayClients, TeamGatewaySettings, create_team_app,
)
from team_e2e_support import make_native, seed_task  # noqa: E402

ORIGIN = "https://team.example.test"
TASK_ID = "9007199254740997"
FOREIGN_TASK_ID = "9007199254740999"
AUTH_SECRET = b"SYNTHETIC_T13_FRONTEND_AUTH_SECRET"


def safe_output(value):
    path = Path(value).absolute()
    if not path.is_relative_to(SCRATCH) or path.name != "canonical.json":
        raise ValueError("frontend_fixture_output_scope")
    for ancestor in (path.parent, *path.parents):
        if ancestor.exists():
            info = ancestor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("frontend_fixture_reparse")
    if path.exists() or not path.parent.is_dir():
        raise ValueError("frontend_fixture_fresh_output_required")
    return path


def export(output):
    now = datetime.now(timezone.utc)
    clock = lambda: now
    database_path = output.parent / "synthetic-team.sqlite3"
    if database_path.exists():
        raise ValueError("frontend_fixture_fresh_database_required")
    team = TeamRepository(TeamDatabase(database_path), clock=clock)
    owner = TeamMember(id="10000000-0000-4000-8000-000000000001", display_name="Synthetic owner",
        role="owner", max_user_id="11", vikunja_user_id="21", project_ids=("7",))
    member = TeamMember(id="10000000-0000-4000-8000-000000000002", display_name="Synthetic assignee",
        max_user_id="12", vikunja_user_id="22", project_ids=("7",))
    foreign = TeamMember(id="10000000-0000-4000-8000-000000000003", display_name="Synthetic other project",
        max_user_id="13", vikunja_user_id="23", project_ids=("8",))
    for person in (owner, member, foreign):
        team.upsert_member(person, expected_revision=None)
    native = make_native(team, clock)
    due = now.astimezone(timezone(timedelta(hours=3))).replace(hour=12, minute=0, second=0, microsecond=0)
    seed_task(native, team, owner, member, due_at=due, title="Synthetic cross-boundary task")
    team.save_projection(TaskSnapshot(task_id=FOREIGN_TASK_ID, project_id="8", revision=0,
        remote_fingerprint="f" * 64, title="Synthetic private project sentinel", assignee_id=foreign.id),
        expected_revision=None)
    sync = TeamSyncRepository(team, clock=clock)
    assert TeamSyncService(sync, native.client).sync_once().state == "ready"
    auth = AuthRepository(team.db, secret=AUTH_SECRET, clock=clock)
    settings = TeamGatewaySettings(public_origin=ORIGIN, bot_id="42", bot_token="SYNTHETIC_T13_FRONTEND_TOKEN")

    def application(repository):
        current_sync = TeamSyncRepository(repository, clock=clock)
        reads = TeamReadRepository(repository, sync=current_sync)
        return create_team_app(settings, repository, TeamGatewayClients(auth=auth, reads=reads))

    phases = []
    with TestClient(application(team), base_url=ORIGIN, client=("203.0.113.20", 45120)) as client:
        code = auth.issue_code(owner.max_user_id)
        login = client.post("/api/team/v1/session/code", json={"value": code.value}, headers={"Origin": ORIGIN})
        assert login.status_code == 200
        csrf = login.json()["csrf"]
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

        def capture(phase, method, path, *, body=None, status=200):
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == status, (method, path, response.status_code)
            phase["responses"].append({"method": method, "path": path, "status": response.status_code,
                "body": response.json()})
            return response.json()

        def reads(phase):
            capture(phase, "GET", "/api/team/v1/me")
            tasks = capture(phase, "GET", "/api/team/v1/tasks?project_id=7&limit=100")
            assert [item["task_id"] for item in tasks["items"]] == [TASK_ID]
            capture(phase, "GET", "/api/team/v1/members?project_id=7&limit=100")
            capture(phase, "GET", "/api/team/v1/status?project_id=7")
            phase["expected_snapshot"] = capture(phase, "GET", "/api/team/v1/tasks/" + TASK_ID)
            capture(phase, "GET", "/api/team/v1/tasks/" + TASK_ID + "/history?limit=100")
            capture(phase, "GET", "/api/team/v1/tasks?project_id=8&limit=50", status=403)
            assert FOREIGN_TASK_ID not in json.dumps(phase["responses"])

        initial = {"name": "initial", "responses": [], "csrf": csrf}
        reads(initial)
        phases.append(initial)
        future = (due + timedelta(days=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        mutations = (("move", "set_state", {"bucket": "doing"}),
            ("classify", "classify", {"important": True, "urgent": False, "classification_confirmed": True}),
            ("due", "set_due", {"due_at": future, "due_confirmed": True, "due_timezone": "Europe/Moscow",
                "reason": "Synthetic owner explicitly moves the deadline"}))
        for number, (name, action, values) in enumerate(mutations, start=101):
            previous = phases[-1]["expected_snapshot"]
            command = {"operation_id": f"10000000-0000-4000-8000-{number:012d}", "project_id": "7",
                "task_id": TASK_ID, "expected_revision": previous["revision"],
                "expected_fingerprint": previous["remote_fingerprint"], "action": action, "values": values}
            phase = {"name": name, "responses": [], "command_request": command, "csrf": csrf}
            accepted = capture(phase, "POST", "/api/team/v1/commands", body=command, status=202)
            assert accepted["acceptance_receipt"]["decision"] == "accepted"
            assert accepted["execution_state"]["state"] == "queued"
            assert team.get_projection(owner.id, TASK_ID).revision == previous["revision"]
            applied = asyncio.run(native.worker.run_once())
            assert applied.execution_state.state == "applied"
            receipt = capture(phase, "GET", "/api/team/v1/commands/" + command["operation_id"])
            assert receipt["execution_state"]["state"] == "applied"
            assert receipt["acceptance_receipt"] == accepted["acceptance_receipt"]
            reads(phase)
            assert phase["expected_snapshot"]["revision"] > previous["revision"]
            assert phase["expected_snapshot"] == receipt["current"]
            phases.append(phase)
        # A new Repository/Database/auth/app reads the same durable SQLite. No
        # task resaving, synthetic receipt injection or provider mutation occurs.
        original_mutations = len(native.simulator.mutations)
        reopened = TeamRepository(TeamDatabase(database_path), clock=clock)
        auth = AuthRepository(reopened.db, secret=AUTH_SECRET, clock=clock)
        with TestClient(application(reopened), base_url=ORIGIN, client=("203.0.113.20", 45120)) as fresh:
            fresh.cookies.update(client.cookies)
            old_client, client = client, fresh
            restart = {"name": "restart", "responses": [], "csrf": csrf}
            reads(restart)
            client = old_client
            phases.append(restart)
        assert phases[-1]["expected_snapshot"] == phases[-2]["expected_snapshot"]
        assert len(native.simulator.mutations) == original_mutations
    native.http.close()
    return {"schema_version": 1, "source": "actual_team_gateway_asgi_vikunja_mock",
        "now": now.isoformat().replace("+00:00", "Z"), "project_id": "7", "task_id": TASK_ID,
        "owner_id": owner.id, "phases": phases,
        "provider_mutations": native.simulator.mutations}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = safe_output(args.output)
    data = json.dumps(export(output), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with output.open("xb") as handle:
        handle.write(data)
    print(json.dumps({"sha256": hashlib.sha256(data).hexdigest(), "schema_version": 1}))


if __name__ == "__main__":
    main()
