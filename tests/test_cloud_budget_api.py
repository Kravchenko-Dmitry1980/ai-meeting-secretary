"""Owner-local budget API, never a real provider or production DB."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.domain.cloud_budget import AccountUsage
from secretary.settings import Settings
from test_backend import FakeCapture, FakeProvider
from test_monthly_budget import NOW


def app_fixture(tmp_path, *, outbound_enabled=True):
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data")
    return create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False,
                      outbound_enabled=outbound_enabled)


def test_refresh_and_scope_api_require_local_owner_token(tmp_path):
    app = app_fixture(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        for path in ("refresh", "scopes"):
            assert client.post("/api/v1/cloud-budget/" + path, json={}).status_code == 403
        budget = app.state.cloud_budget
        budget.clock = lambda: NOW
        class Account:
            async def read_key_usage(self):
                return AccountUsage("synthetic", 1000_000000, 996_000000, 4_000000, "monthly", NOW)
        budget.account_reader = Account()
        headers = {"X-Secretary-Token": client.get("/api/v1/session").json()["csrf_token"]}
        response = client.post("/api/v1/cloud-budget/refresh", headers=headers)
        assert response.status_code == 200
        assert response.json()["confirmed_rub"] == 4
        assert response.json()["effective_limit_rub"] == 1000
        operation = str(uuid4())
        body = {"operation_id": operation, "cap_rub": "5"}
        created = client.post("/api/v1/cloud-budget/scopes", headers=headers, json=body)
        assert created.status_code == 201
        scope = created.json()["scope_id"]
        assert client.post("/api/v1/cloud-budget/scopes", headers=headers, json=body).json()["scope_id"] == scope
        assert client.post("/api/v1/cloud-budget/scopes", headers=headers,
                           json={**body, "cap_rub": 6}).status_code == 409
        meeting = client.post("/api/v1/meetings", headers=headers,
                              json={"title": "Synthetic", "cloud_budget_scope_id": scope})
        assert meeting.status_code == 201
        assert budget.scope_for_meeting(meeting.json()["id"]) == scope
        state = client.get(f"/api/v1/cloud-budget/scopes/{scope}").json()
        assert state["remaining_rub"] == 5 and state["uncertain_count"] == 0


def test_unknown_scope_is_rejected_before_creating_meeting(tmp_path):
    app = app_fixture(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = {"X-Secretary-Token": client.get("/api/v1/session").json()["csrf_token"]}
        response = client.post("/api/v1/meetings", headers=headers,
                               json={"cloud_budget_scope_id": str(uuid4())})
        assert response.status_code == 404
        assert client.get("/api/v1/meetings").json() == []
        assert client.patch("/api/v1/config", headers=headers,
            json={"approved_monthly_external_costs_rub": 0}).status_code == 422


def test_exact_receipt_reconciliation_settles_once_and_refuses_wrong_key(tmp_path):
    from decimal import Decimal
    from secretary.domain.cloud_budget import CloudCharge
    app = app_fixture(tmp_path)
    budget = app.state.cloud_budget
    budget.clock = lambda: NOW
    budget.refresh_account(AccountUsage("synthetic", 3000_000000, 3000_000000, 0, "monthly", NOW))
    operation = str(uuid4())
    budget.reserve(CloudCharge(operation, "meeting_stt", "a" * 64, 1_000000, 1_000000))
    budget.bind_provider_receipt(operation, "gen_synthetic")
    budget.mark_uncertain(operation)
    class History:
        key_tag = "synthetic-wrong-key"
        calls = 0
        async def read_receipt(self, identifier):
            self.calls += 1
            assert identifier == "gen_synthetic"
            return {"provider_request_id": identifier, "confirmed_rub": Decimal("2"),
                    "provider_period": None, "status": "completed"}
    history = History()
    app.state.cloud_budget_history = history
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = {"X-Secretary-Token": client.get("/api/v1/session").json()["csrf_token"]}
        path = f"/api/v1/cloud-budget/operations/{operation}/reconcile"
        assert client.post(path, headers=headers).status_code == 409
        assert history.calls == 0
        history.key_tag = "synthetic"
        assert client.post(path, headers=headers).status_code == 200
        assert client.post(path, headers=headers).status_code == 200
        assert history.calls == 1
        assert budget.snapshot().confirmed_micro == 2_000000
        assert budget.snapshot().reserved_micro == 0
        detail = client.get(f"/api/v1/cloud-budget/operations/{operation}").json()
        assert [event["outcome"] for event in detail["events"]] == ["lookup_blocked", "lookup_started", "receipt_applied"]
        assert detail["events"][0]["reason_code"] == "monthly_budget_key_changed"
        assert detail["events"][-1]["provider_request_id"] == "gen_synthetic"
        assert detail["events"][-1]["provider_cost_rub"] == 2


def test_operation_review_is_bounded_sanitized_and_pending_receipt_stays_uncertain(tmp_path):
    import sqlite3
    from decimal import Decimal
    from secretary.domain.cloud_budget import CloudCharge
    app = app_fixture(tmp_path)
    budget = app.state.cloud_budget
    budget.clock = lambda: NOW
    budget.refresh_account(AccountUsage("synthetic", 3000_000000, 3000_000000, 0, "monthly", NOW))
    operation = str(uuid4())
    budget.reserve(CloudCharge(operation, "meeting_stt", "a" * 64, 1_000000, 1_000000,
                               meeting_id="meeting-synthetic"), key_tag="synthetic")
    budget.bind_provider_receipt(operation, "gen_synthetic")
    budget.mark_uncertain(operation)

    class History:
        key_tag = "synthetic"
        async def read_receipt(self, identifier):
            assert identifier == "gen_synthetic"
            return {"provider_request_id": identifier, "confirmed_rub": None,
                    "provider_period": None, "status": "pending"}
    app.state.cloud_budget_history = History()
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/api/v1/cloud-budget/operations?status=uncertain&limit=1&offset=0")
        assert page.status_code == 200
        row = page.json()["items"][0]
        assert row["operation_id"] == operation
        assert row["provider_request_id"] == "gen_synthetic"
        assert row["status"] == "uncertain"
        assert not {"key_tag", "payload_hash", "request_hash", "meeting_id", "command_id"} & row.keys()
        assert client.get("/api/v1/cloud-budget/operations?limit=201").status_code == 422
        headers = {"X-Secretary-Token": client.get("/api/v1/session").json()["csrf_token"]}
        response = client.post(f"/api/v1/cloud-budget/operations/{operation}/reconcile", headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "uncertain"
        detail = client.get(f"/api/v1/cloud-budget/operations/{operation}").json()
        assert detail["operation"]["status"] == "uncertain"
        assert [(event["outcome"], event["reason_code"], event["provider_status"])
                for event in detail["events"]] == [("lookup_started", None, None), ("provider_pending", "provider_pending", "pending")]
        assert budget.snapshot().reserved_micro == 1_000000
        assert row["provider_job_id"] is None
        with budget.repository.transaction() as connection:
            with pytest.raises(sqlite3.IntegrityError, match="billing_reconciliation_append_only"):
                connection.execute("UPDATE billing_reconciliation_events SET reason_code='edited' WHERE operation_id=?", (operation,))
            with pytest.raises(sqlite3.IntegrityError, match="billing_reconciliation_append_only"):
                connection.execute("DELETE FROM billing_reconciliation_events WHERE operation_id=?", (operation,))
            event = tuple(connection.execute("SELECT * FROM billing_reconciliation_events WHERE operation_id=? ORDER BY event_id LIMIT 1", (operation,)).fetchone())
            with pytest.raises(sqlite3.IntegrityError, match="billing_reconciliation_append_only"):
                connection.execute("INSERT OR REPLACE INTO billing_reconciliation_events VALUES(?,?,?,?,?,?,?,?,?,?)", event)
        assert client.get(f"/api/v1/cloud-budget/operations/{uuid4()}").status_code == 404


def test_cloud_budget_operation_openapi_contract_exposes_only_safe_fields(tmp_path):
    app = app_fixture(tmp_path)
    spec = app.openapi()
    page = spec["components"]["schemas"]["CloudBudgetOperationPage"]["properties"]["items"]["items"]["$ref"]
    operation_schema = spec["components"]["schemas"][page.rsplit("/", 1)[-1]]["properties"]
    assert {"operation_id", "status", "provider_request_id", "provider_job_id", "confirmed_rub"} <= operation_schema.keys()
    assert not {"key_tag", "payload_hash", "request_hash", "meeting_id", "command_id"} & operation_schema.keys()
    paths = spec["paths"]
    assert "/api/v1/cloud-budget/operations" in paths
    assert "/api/v1/cloud-budget/operations/{operation_id}" in paths


def test_legacy_guarded_operation_without_reconciliation_table_stays_readable(tmp_path):
    from secretary.domain.cloud_budget import CloudCharge
    app = app_fixture(tmp_path)
    budget = app.state.cloud_budget
    budget.clock = lambda: NOW
    budget.refresh_account(AccountUsage("synthetic", 3000_000000, 2996_000000, 4_000000, "monthly", NOW))
    operation = str(uuid4())
    budget.reserve(CloudCharge(operation, "meeting_stt", "b" * 64, 1_000000, 2_000000), key_tag="synthetic")
    budget.bind_provider_receipt(operation, "gen_legacy")
    budget.mark_uncertain(operation)
    with budget.repository.transaction() as connection:
        connection.execute("DROP TABLE billing_reconciliation_events")
        connection.execute("UPDATE billing_schema SET version=3")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        detail = client.get(f"/api/v1/cloud-budget/operations/{operation}")
        assert detail.status_code == 200
        assert detail.json()["operation"]["status"] == "uncertain"
        assert detail.json()["events"] == []


def test_offline_budget_refresh_is_rejected_before_provider_access(tmp_path):
    app = app_fixture(tmp_path, outbound_enabled=False)
    class Account:
        calls = 0
        async def read_key_usage(self):
            self.calls += 1
            return AccountUsage("synthetic", 3000_000000, 2999_000000, 1_000000, "monthly", NOW)
    account = Account()
    app.state.cloud_budget.account_reader = account
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = {"X-Secretary-Token": client.get("/api/v1/session").json()["csrf_token"]}
        response = client.post("/api/v1/cloud-budget/refresh", headers=headers)
        assert response.status_code == 409
        assert response.json()["detail"] == "runtime_outbound_disabled"
        assert account.calls == 0


def test_offline_receipt_reconciliation_is_rejected_before_provider_access(tmp_path):
    from decimal import Decimal
    from secretary.domain.cloud_budget import CloudCharge
    app = app_fixture(tmp_path, outbound_enabled=False)
    budget = app.state.cloud_budget
    budget.clock = lambda: NOW
    budget.refresh_account(AccountUsage("synthetic", 3000_000000, 3000_000000, 0, "monthly", NOW))
    operation = str(uuid4())
    budget.reserve(CloudCharge(operation, "meeting_stt", "a" * 64, 1_000000, 1_000000))
    budget.bind_provider_receipt(operation, "gen_synthetic")
    budget.mark_uncertain(operation)

    class History:
        key_tag = "synthetic"
        calls = 0
        async def read_receipt(self, identifier):
            self.calls += 1
            return {"provider_request_id": identifier, "confirmed_rub": Decimal("1"),
                    "provider_period": None, "status": "completed"}
    history = History()
    app.state.cloud_budget_history = history
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = {"X-Secretary-Token": client.get("/api/v1/session").json()["csrf_token"]}
        response = client.post(f"/api/v1/cloud-budget/operations/{operation}/reconcile", headers=headers)
        assert response.status_code == 409
        assert response.json()["detail"] == "runtime_outbound_disabled"
        assert history.calls == 0
        assert budget.reservation(operation).status == "uncertain"
        assert budget.snapshot().reserved_micro == 1_000000
        detail = client.get(f"/api/v1/cloud-budget/operations/{operation}").json()
        assert detail["events"][-1]["reason_code"] == "runtime_outbound_disabled"


def test_legacy_operation_category_remains_readable_in_operator_api(tmp_path):
    app = app_fixture(tmp_path)
    operation = str(uuid4())
    with app.state.cloud_budget.repository.transaction() as connection:
        connection.execute("""INSERT INTO billing_charges(operation_id,payload_hash,request_hash,category,
            period,key_tag,estimated_micro,reserved_micro,status,created_ms,updated_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (operation, "a" * 64, "b" * 64, "short_voice", "2026-10", "legacy-key",
             500_000, 500_000, "uncertain", 1, 1))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/api/v1/cloud-budget/operations?status=uncertain")
        assert page.status_code == 200
        assert page.json()["items"][0]["category"] == "short_voice"
        detail = client.get(f"/api/v1/cloud-budget/operations/{operation}")
        assert detail.status_code == 200
        assert detail.json()["operation"]["category"] == "short_voice"
