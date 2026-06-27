"""
CI/CD cost integration — full test suite
=========================================

Covers:
  - compute_gate: budget gate pass/fail logic
  - compute_drift: drift percentage calculation
  - record_run: Cosmos persistence, gate evaluation, dataclass fields
  - list_runs: query + _partitionKey stripping
  - Router: POST /estimate/terraform (existing), POST /estimate/terraform/record,
    GET /estimate/runs/{tenant_id}, POST /estimate/gate
  - Auth: /record and /runs require API key; /gate does not

Run: pytest tests/test_cicd_cost_integration.py -v
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("INTERNAL_API_KEY", "super-secret-key")
os.environ.setdefault("AZURE_TENANT_ID", "test")
os.environ.setdefault("AZURE_CLIENT_ID", "test")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "teststore")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")

from app.config import get_settings
from app.services.cost_estimator import (
    CostEstimate,
    ResourceEstimate,
    compute_drift,
    compute_gate,
    estimate_plan,
    list_runs,
    record_run,
)

KEY = {"X-API-Key": get_settings().internal_api_key}
TENANT = "t-acme"

# ── Minimal valid Terraform plan JSON ─────────────────────────────────────────

def _plan(resources: list[dict] | None = None) -> str:
    changes = resources or [
        {
            "address": "aws_instance.web",
            "type": "aws_instance",
            "change": {
                "actions": ["create"],
                "after": {"instance_type": "t3.medium"},
                "before": None,
            },
        }
    ]
    return json.dumps({"resource_changes": changes})


def _estimate(monthly: float = 100.0) -> CostEstimate:
    """Build a minimal CostEstimate for testing."""
    r = ResourceEstimate(
        address="aws_instance.web",
        resource_type="aws_instance",
        action="create",
        provider="aws",
        size="t3.medium",
        monthly_delta_eur=monthly,
        confidence="catalog",
        notes="",
    )
    return CostEstimate(
        resources=[r],
        total_monthly_delta_eur=monthly,
        breakdown_by_action={"create": monthly},
        unsupported_resource_types=[],
        total_resources_analyzed=1,
        generated_at="2026-01-01T00:00:00+00:00",
    )


def _run_doc(monthly: float = 100.0, gate_passed: bool | None = None) -> dict:
    return {
        "id": "prun-abc",
        "type": "pipeline_run",
        "tenant_id": TENANT,
        "label": "PR #1",
        "ci_system": "github_actions",
        "repo": "org/repo",
        "branch": "main",
        "commit_sha": "abc123",
        "pr_number": 1,
        "total_monthly_delta_eur": monthly,
        "total_annual_delta_eur": round(monthly * 12, 2),
        "total_resources_analyzed": 1,
        "budget_gate_eur": 200.0,
        "gate_passed": gate_passed,
        "breakdown_by_action": {"create": monthly},
        "resources": [],
        "unsupported_resource_types": [],
        "recorded_at": "2026-01-15T10:00:00+00:00",
        "ttl": 7776000,
        "_partitionKey": TENANT,
    }


def _client():
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


# ══════════════════════════════════════════════════════════════════════════════
# compute_gate
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeGate:
    def test_within_gate_passes(self):
        assert compute_gate(50.0, 100.0) is True

    def test_exactly_at_gate_passes(self):
        assert compute_gate(100.0, 100.0) is True

    def test_exceeds_gate_fails(self):
        assert compute_gate(150.01, 100.0) is False

    def test_negative_delta_always_passes(self):
        """Cost reductions always pass any positive gate."""
        assert compute_gate(-500.0, 0.0) is True

    def test_zero_gate_only_passes_reductions(self):
        assert compute_gate(0.01, 0.0) is False
        assert compute_gate(0.0, 0.0) is True


# ══════════════════════════════════════════════════════════════════════════════
# compute_drift
# ══════════════════════════════════════════════════════════════════════════════

class TestComputeDrift:
    def test_increase_positive_drift(self):
        assert compute_drift(150.0, 100.0) == pytest.approx(50.0, abs=0.01)

    def test_decrease_negative_drift(self):
        assert compute_drift(50.0, 100.0) == pytest.approx(-50.0, abs=0.01)

    def test_no_change_zero_drift(self):
        assert compute_drift(100.0, 100.0) == pytest.approx(0.0, abs=0.001)

    def test_zero_previous_returns_none(self):
        assert compute_drift(100.0, 0.0) is None

    def test_drift_rounded_to_2dp(self):
        result = compute_drift(133.33, 100.0)
        assert result == pytest.approx(33.33, abs=0.01)

    def test_from_negative_previous(self):
        """Drift from a negative baseline (net cost reduction run)."""
        result = compute_drift(-100.0, -200.0)
        # (-100 - -200) / 200 * 100 = 50%
        assert result == pytest.approx(50.0, abs=0.01)


# ══════════════════════════════════════════════════════════════════════════════
# record_run (async, Cosmos mocked)
# ══════════════════════════════════════════════════════════════════════════════

class TestRecordRun:
    @pytest.mark.asyncio
    async def test_basic_run_persisted(self):
        saved = []
        with patch("app.services.cost_estimator.cosmos.upsert_item",
                   new=AsyncMock(side_effect=lambda c, d: saved.append(d))):
            run = await record_run(TENANT, _estimate(100.0), label="PR #1")

        assert run.tenant_id == TENANT
        assert run.total_monthly_delta_eur == 100.0
        assert run.total_annual_delta_eur == 1200.0
        assert len(saved) == 1
        assert saved[0]["type"] == "pipeline_run"
        assert saved[0]["_partitionKey"] == TENANT

    @pytest.mark.asyncio
    async def test_gate_passed_when_within_budget(self):
        with patch("app.services.cost_estimator.cosmos.upsert_item", new=AsyncMock()):
            run = await record_run(TENANT, _estimate(80.0), budget_gate_eur=100.0)
        assert run.gate_passed is True
        assert run.budget_gate_eur == 100.0

    @pytest.mark.asyncio
    async def test_gate_failed_when_over_budget(self):
        with patch("app.services.cost_estimator.cosmos.upsert_item", new=AsyncMock()):
            run = await record_run(TENANT, _estimate(250.0), budget_gate_eur=100.0)
        assert run.gate_passed is False

    @pytest.mark.asyncio
    async def test_no_gate_gate_passed_is_none(self):
        with patch("app.services.cost_estimator.cosmos.upsert_item", new=AsyncMock()):
            run = await record_run(TENANT, _estimate(500.0))
        assert run.gate_passed is None
        assert run.budget_gate_eur is None

    @pytest.mark.asyncio
    async def test_ci_metadata_stored(self):
        saved = []
        with patch("app.services.cost_estimator.cosmos.upsert_item",
                   new=AsyncMock(side_effect=lambda c, d: saved.append(d))):
            run = await record_run(
                TENANT, _estimate(),
                ci_system="gitlab_ci",
                repo="org/infra",
                branch="feature/x",
                commit_sha="deadbeef",
                pr_number=42,
            )
        assert run.ci_system == "gitlab_ci"
        assert run.repo == "org/infra"
        assert run.branch == "feature/x"
        assert run.pr_number == 42
        assert saved[0]["commit_sha"] == "deadbeef"

    @pytest.mark.asyncio
    async def test_run_id_starts_with_prun(self):
        with patch("app.services.cost_estimator.cosmos.upsert_item", new=AsyncMock()):
            run = await record_run(TENANT, _estimate())
        assert run.id.startswith("prun-")

    @pytest.mark.asyncio
    async def test_resources_included_in_run(self):
        saved = []
        with patch("app.services.cost_estimator.cosmos.upsert_item",
                   new=AsyncMock(side_effect=lambda c, d: saved.append(d))):
            await record_run(TENANT, _estimate(50.0))
        assert isinstance(saved[0]["resources"], list)
        assert len(saved[0]["resources"]) == 1
        assert saved[0]["resources"][0]["address"] == "aws_instance.web"


class TestListRuns:
    @pytest.mark.asyncio
    async def test_strips_partition_key(self):
        docs = [_run_doc(100.0), _run_doc(50.0)]
        with patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=docs)):
            runs = await list_runs(TENANT)
        for r in runs:
            assert "_partitionKey" not in r

    @pytest.mark.asyncio
    async def test_returns_all_docs(self):
        docs = [_run_doc(100.0), _run_doc(200.0), _run_doc(75.0)]
        with patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=docs)):
            runs = await list_runs(TENANT, limit=10)
        assert len(runs) == 3

    @pytest.mark.asyncio
    async def test_empty_returns_empty_list(self):
        with patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=[])):
            runs = await list_runs(TENANT)
        assert runs == []


# ══════════════════════════════════════════════════════════════════════════════
# Router: POST /estimate/terraform  (stateless — unchanged)
# ══════════════════════════════════════════════════════════════════════════════

class TestRouterEstimateStateless:
    def test_valid_plan_returns_200(self):
        r = _client().post("/api/v1/estimate/terraform",
                           json={"plan_json": _plan()})
        assert r.status_code == 200
        body = r.json()
        assert "total_monthly_delta_eur" in body
        assert body["total_monthly_delta_eur"] > 0

    def test_label_included_in_response(self):
        r = _client().post("/api/v1/estimate/terraform",
                           json={"plan_json": _plan(), "label": "my-pr"})
        assert r.json()["label"] == "my-pr"

    def test_invalid_json_returns_422(self):
        r = _client().post("/api/v1/estimate/terraform",
                           json={"plan_json": "not-json"})
        assert r.status_code == 422

    def test_no_auth_required(self):
        """Stateless endpoint has no auth requirement."""
        r = _client().post("/api/v1/estimate/terraform",
                           json={"plan_json": _plan()})
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# Router: POST /estimate/terraform/record
# ══════════════════════════════════════════════════════════════════════════════

class TestRouterEstimateRecord:
    def _payload(self, **kwargs) -> dict:
        return {
            "plan_json": _plan(),
            "tenant_id": TENANT,
            "label": "Test PR",
            "ci_system": "github_actions",
            "repo": "org/repo",
            "branch": "main",
            "commit_sha": "abc123",
            **kwargs,
        }

    def test_requires_api_key(self):
        r = _client().post("/api/v1/estimate/terraform/record",
                           json=self._payload())
        assert r.status_code in (401, 403)

    def test_valid_request_returns_200(self):
        with patch("app.services.cost_estimator.cosmos.upsert_item", new=AsyncMock()), \
             patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=[])):
            r = _client().post("/api/v1/estimate/terraform/record",
                               json=self._payload(), headers=KEY)
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"].startswith("prun-")
        assert "gate_passed" in body
        assert "drift_vs_previous_pct" in body

    def test_gate_evaluated_when_provided(self):
        with patch("app.services.cost_estimator.cosmos.upsert_item", new=AsyncMock()), \
             patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=[])):
            r = _client().post("/api/v1/estimate/terraform/record",
                               json=self._payload(budget_gate_eur=999.0),
                               headers=KEY)
        assert r.status_code == 200
        body = r.json()
        assert body["gate_passed"] is True   # t3.medium ~€30 < €999 gate
        assert body["budget_gate_eur"] == 999.0

    def test_gate_fails_when_exceeded(self):
        with patch("app.services.cost_estimator.cosmos.upsert_item", new=AsyncMock()), \
             patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=[])):
            r = _client().post("/api/v1/estimate/terraform/record",
                               json=self._payload(budget_gate_eur=0.01),
                               headers=KEY)
        assert r.status_code == 200
        assert r.json()["gate_passed"] is False

    def test_drift_computed_against_previous_run(self):
        prev = [_run_doc(monthly=10.0)]
        with patch("app.services.cost_estimator.cosmos.upsert_item", new=AsyncMock()), \
             patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=prev)):
            r = _client().post("/api/v1/estimate/terraform/record",
                               json=self._payload(), headers=KEY)
        body = r.json()
        assert body["drift_vs_previous_pct"] is not None
        assert body["drift_vs_previous_pct"] > 0   # current > previous

    def test_no_drift_when_no_previous_run(self):
        with patch("app.services.cost_estimator.cosmos.upsert_item", new=AsyncMock()), \
             patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=[])):
            r = _client().post("/api/v1/estimate/terraform/record",
                               json=self._payload(), headers=KEY)
        assert r.json()["drift_vs_previous_pct"] is None

    def test_invalid_plan_json_returns_422(self):
        with patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=[])):
            r = _client().post("/api/v1/estimate/terraform/record",
                               json={**self._payload(), "plan_json": "bad"},
                               headers=KEY)
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# Router: GET /estimate/runs/{tenant_id}
# ══════════════════════════════════════════════════════════════════════════════

class TestRouterRunHistory:
    def test_requires_api_key(self):
        r = _client().get(f"/api/v1/estimate/runs/{TENANT}")
        assert r.status_code in (401, 403)

    def test_returns_run_list(self):
        docs = [_run_doc(100.0), _run_doc(80.0)]
        with patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=docs)):
            r = _client().get(f"/api/v1/estimate/runs/{TENANT}", headers=KEY)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert len(body["runs"]) == 2
        assert body["tenant_id"] == TENANT

    def test_empty_history(self):
        with patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=[])):
            r = _client().get(f"/api/v1/estimate/runs/{TENANT}", headers=KEY)
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_drift_annotated_between_runs(self):
        docs = [_run_doc(150.0), _run_doc(100.0)]
        with patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=docs)):
            r = _client().get(f"/api/v1/estimate/runs/{TENANT}", headers=KEY)
        runs = r.json()["runs"]
        # First run: drift vs second
        assert runs[0]["drift_vs_previous_pct"] == pytest.approx(50.0, abs=0.01)
        # Last run: no predecessor
        assert runs[1]["drift_vs_previous_pct"] is None

    def test_limit_query_param(self):
        docs = [_run_doc() for _ in range(5)]
        with patch("app.services.cost_estimator.cosmos.query_items",
                   new=AsyncMock(return_value=docs)):
            r = _client().get(f"/api/v1/estimate/runs/{TENANT}?limit=5", headers=KEY)
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# Router: POST /estimate/gate
# ══════════════════════════════════════════════════════════════════════════════

class TestRouterBudgetGate:
    def test_no_auth_required(self):
        r = _client().post("/api/v1/estimate/gate",
                           json={"monthly_delta_eur": 50.0, "gate_eur": 100.0})
        assert r.status_code == 200

    def test_pass_within_gate(self):
        r = _client().post("/api/v1/estimate/gate",
                           json={"monthly_delta_eur": 50.0, "gate_eur": 100.0})
        body = r.json()
        assert body["passed"] is True
        assert body["exit_code"] == 0
        assert "PASS" in body["message"]

    def test_fail_over_gate(self):
        r = _client().post("/api/v1/estimate/gate",
                           json={"monthly_delta_eur": 250.0, "gate_eur": 100.0})
        body = r.json()
        assert body["passed"] is False
        assert body["exit_code"] == 1
        assert "FAIL" in body["message"]

    def test_exact_boundary_passes(self):
        r = _client().post("/api/v1/estimate/gate",
                           json={"monthly_delta_eur": 100.0, "gate_eur": 100.0})
        assert r.json()["passed"] is True

    def test_negative_delta_always_passes(self):
        r = _client().post("/api/v1/estimate/gate",
                           json={"monthly_delta_eur": -500.0, "gate_eur": 0.0})
        assert r.json()["passed"] is True

    def test_label_echoed_in_response(self):
        r = _client().post("/api/v1/estimate/gate",
                           json={"monthly_delta_eur": 10.0, "gate_eur": 50.0,
                                 "label": "deploy-prod"})
        assert r.json()["label"] == "deploy-prod"

    def test_gate_eur_must_be_non_negative(self):
        r = _client().post("/api/v1/estimate/gate",
                           json={"monthly_delta_eur": 10.0, "gate_eur": -5.0})
        assert r.status_code == 422

    def test_missing_gate_eur_returns_422(self):
        r = _client().post("/api/v1/estimate/gate",
                           json={"monthly_delta_eur": 10.0})
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# Router: GET /estimate/catalog  (existing)
# ══════════════════════════════════════════════════════════════════════════════

class TestRouterCatalog:
    def test_returns_catalog(self):
        r = _client().get("/api/v1/estimate/catalog")
        assert r.status_code == 200
        body = r.json()
        assert body["total_resource_types"] >= 20
        assert "entries" in body

    def test_no_auth_required(self):
        r = _client().get("/api/v1/estimate/catalog")
        assert r.status_code == 200
