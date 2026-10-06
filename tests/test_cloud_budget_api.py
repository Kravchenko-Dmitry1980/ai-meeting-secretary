"""Owner-local budget API, never a real provider or production DB."""
from uuid import uuid4

from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.domain.cloud_budget import AccountUsage
from secretary.settings import Settings
from test_backend import FakeCapture, FakeProvider
from test_monthly_budget import NOW


def app_fixture(tmp_path):
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data")
    return create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)


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
