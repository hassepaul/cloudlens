"""
Tests for drill-down, resource-level anomaly detection, and the alerts subsystem.
Run: pytest tests/test_drilldown_alerts.py -v
"""
from __future__ import annotations
import os
from datetime import date, timedelta
from unittest.mock import patch

import numpy as np

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("AZURE_TENANT_ID", "t")
os.environ.setdefault("AZURE_CLIENT_ID", "c")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "s")
os.environ.setdefault("KEY_VAULT_NAME", "k")

from app.services.anomaly import detect_resource_anomalies
from app.services.alerts import evaluate_rules
from app.models.alert import AlertRule, AlertType, AlertChannel, AlertSeverity

TID = "t-1"


def _client():
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


# ══════════════════════════════════════════════════════════════════════════════
# RESOURCE-LEVEL ANOMALY DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def _res_series(base, spike_last=0, n=14, seed=5):
    np.random.seed(seed)
    start = date(2026, 5, 1)
    return [{"date": (start + timedelta(days=i)).isoformat(),
             "cost_eur": round(max(0, base + np.random.normal(0, base * 0.04)
                                   + (spike_last if i == n - 1 else 0)), 2)}
            for i in range(n)]


class TestResourceAnomalies:
    def test_flags_only_spiking_resource(self):
        series = {
            "/sub/rg/vm-normal": {"meta": {"resource_name": "vm-normal", "provider_name": "AWS",
                                           "sub_account_id": "111", "service_name": "EC2"},
                                  "daily": _res_series(50)},
            "/sub/rg/vm-spike": {"meta": {"resource_name": "vm-spike", "provider_name": "AWS",
                                          "sub_account_id": "111", "service_name": "EC2"},
                                 "daily": _res_series(40, spike_last=200)},
        }
        res = detect_resource_anomalies(series, scan_last_days=3)
        assert res.flagged_resources == 1
        assert res.anomalies[0].resource_name == "vm-spike"
        assert res.anomalies[0].provider_name == "AWS"
        assert res.anomalies[0].excess_eur > 100

    def test_no_false_positive_on_stable(self):
        series = {f"/r/{i}": {"meta": {"resource_name": f"r{i}"}, "daily": _res_series(30, seed=i)}
                  for i in range(4)}
        res = detect_resource_anomalies(series, scan_last_days=3)
        assert res.flagged_resources == 0

    def test_sparse_resource_skipped(self):
        series = {"/r/x": {"meta": {}, "daily": _res_series(10, n=3)}}  # < min points
        res = detect_resource_anomalies(series, scan_last_days=3)
        assert res.scanned_resources == 0


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class TestAlertEngine:
    def test_budget_breach_fires(self):
        rule = AlertRule(tenant_id=TID, name="Budget", alert_type=AlertType.BUDGET_BREACH, threshold=90)
        ev = evaluate_rules([rule], budget_statuses=[
            {"name": "Eng", "amount_eur": 11000, "consumed_pct": 95,
             "projected_consumed_pct": 112, "budget_id": "b1"}])
        assert len(ev) == 1
        assert ev[0].severity == AlertSeverity.HIGH
        assert "112%" in ev[0].title

    def test_budget_critical_when_already_breached(self):
        rule = AlertRule(tenant_id=TID, name="Budget", alert_type=AlertType.BUDGET_BREACH, threshold=90)
        ev = evaluate_rules([rule], budget_statuses=[
            {"name": "Eng", "amount_eur": 11000, "consumed_pct": 104, "budget_id": "b1"}])
        assert ev[0].severity == AlertSeverity.CRITICAL

    def test_resource_anomaly_rule_with_scope_filter(self):
        from app.services.anomaly import ResourceAnomaly
        ra = ResourceAnomaly(resource_id="/x/vm-1", resource_name="vm-1", provider_name="AWS",
                             sub_account_id="111", service_name="EC2", day="2026-06-10",
                             actual_eur=240, expected_eur=40, excess_eur=200, z_score=12.0,
                             severity="high", method="median_mad")
        # rule scoped to AWS → fires
        rule = AlertRule(tenant_id=TID, name="R", alert_type=AlertType.RESOURCE_ANOMALY,
                         threshold=3.0, provider="AWS")
        assert len(evaluate_rules([rule], resource_anomalies=[ra])) == 1
        # rule scoped to GCP → does not fire
        rule2 = AlertRule(tenant_id=TID, name="R", alert_type=AlertType.RESOURCE_ANOMALY,
                          threshold=3.0, provider="Google Cloud")
        assert len(evaluate_rules([rule2], resource_anomalies=[ra])) == 0

    def test_waste_and_idle_thresholds(self):
        wr = AlertRule(tenant_id=TID, name="W", alert_type=AlertType.WASTE_THRESHOLD, threshold=5000)
        assert len(evaluate_rules([wr], recoverable_eur=6000)) == 1
        assert len(evaluate_rules([wr], recoverable_eur=4000)) == 0
        ir = AlertRule(tenant_id=TID, name="I", alert_type=AlertType.COMMITMENT_IDLE, threshold=500)
        assert len(evaluate_rules([ir], idle_commitment_eur=640)) == 1

    def test_disabled_rule_never_fires(self):
        rule = AlertRule(tenant_id=TID, name="X", alert_type=AlertType.WASTE_THRESHOLD,
                         threshold=1, enabled=False)
        assert evaluate_rules([rule], recoverable_eur=999999) == []


# ══════════════════════════════════════════════════════════════════════════════
# DRILL-DOWN ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestDrilldownRouter:
    def test_provider_level(self):
        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return [{"key": "Amazon Web Services", "spend": 18600, "records": 400},
                    {"key": "Microsoft Azure", "spend": 14200, "records": 300}]
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/drilldown/{TID}?level=provider")
        assert r.status_code == 200
        body = r.json()
        assert body["next_level"] == "account"
        assert body["children"][0]["key"] == "Amazon Web Services"
        assert body["children"][0]["has_children"] is True

    def test_resource_level_flags_anomalies(self):
        # spend aggregation query returns two resources; anomaly query returns series
        spend_rows = [{"key": "/sub/rg/vm-spike", "spend": 500, "records": 30},
                      {"key": "/sub/rg/vm-normal", "spend": 300, "records": 30}]
        series_rows = []
        for d in _res_series(40, spike_last=200):
            series_rows.append({"resource_id": "/sub/rg/vm-spike", "resource_name": "vm-spike",
                                "provider_name": "AWS", "sub_account_id": "111",
                                "service_name": "EC2", "day": d["date"], "cost": d["cost_eur"]})
        for d in _res_series(50, seed=9):
            series_rows.append({"resource_id": "/sub/rg/vm-normal", "resource_name": "vm-normal",
                                "provider_name": "AWS", "sub_account_id": "111",
                                "service_name": "EC2", "day": d["date"], "cost": d["cost_eur"]})

        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            if "GROUP BY c.resource_id, c.resource_name" in q:
                return series_rows
            return spend_rows
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/drilldown/{TID}?level=resource&provider=AWS&account=111&service=EC2")
        assert r.status_code == 200
        body = r.json()
        spike = next(c for c in body["children"] if c["key"] == "/sub/rg/vm-spike")
        normal = next(c for c in body["children"] if c["key"] == "/sub/rg/vm-normal")
        assert spike["anomaly"] is not None
        assert spike["anomaly"]["severity"] in ("high", "medium")
        assert normal["anomaly"] is None

    def test_resource_anomalies_endpoint(self):
        series_rows = []
        for d in _res_series(40, spike_last=200):
            series_rows.append({"resource_id": "/r/vm-spike", "resource_name": "vm-spike",
                                "provider_name": "AWS", "sub_account_id": "111",
                                "service_name": "EC2", "day": d["date"], "cost": d["cost_eur"]})

        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return series_rows
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/drilldown/{TID}/resource-anomalies")
        assert r.status_code == 200
        assert r.json()["flagged"] == 1

    def test_invalid_level_rejected(self):
        r = _client().get(f"/api/v1/drilldown/{TID}?level=galaxy")
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class TestAlertsRouter:
    def test_create_rule(self):
        captured = {}

        async def fake_upsert(c, item):
            captured["item"] = item
            return item
        payload = {"tenant_id": TID, "name": "Resource spikes", "alert_type": "resource_anomaly",
                   "threshold": 3.0, "channels": ["in_app", "webhook"],
                   "webhook_url": "https://hooks.example.com/x"}
        with patch("app.services.cosmos.upsert_item", new=fake_upsert):
            r = _client().post(f"/api/v1/alerts/{TID}/rules", json=payload)
        assert r.status_code == 201
        assert captured["item"]["type"] == "alert_rule"
        assert captured["item"]["alert_type"] == "resource_anomaly"

    def test_create_rule_tenant_mismatch(self):
        payload = {"tenant_id": "other", "name": "x", "alert_type": "waste_threshold"}
        r = _client().post(f"/api/v1/alerts/{TID}/rules", json=payload)
        assert r.status_code == 422

    def test_list_rules(self):
        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return [{"id": "r1", "type": "alert_rule", "tenant_id": TID, "name": "Budget",
                     "alert_type": "budget_breach", "threshold": 100,
                     "channels": ["in_app"], "enabled": True,
                     "created_at": "2026-06-01T00:00:00+00:00"}]
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/alerts/{TID}/rules")
        assert r.status_code == 200
        assert r.json()[0]["alert_type"] == "budget_breach"

    def test_list_events(self):
        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return [{"id": "e1", "type": "alert_event", "tenant_id": TID, "rule_id": "r1",
                     "rule_name": "Budget", "alert_type": "budget_breach", "severity": "high",
                     "title": "Budget 'Eng' at 112%", "triggered_at": "2026-06-10T00:00:00+00:00"}]
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get(f"/api/v1/alerts/{TID}/events")
        assert r.status_code == 200
        assert r.json()[0]["severity"] == "high"

    def test_acknowledge_event(self):
        async def fake_get(c, item_id, pk):
            return {"id": "e1", "type": "alert_event", "tenant_id": TID, "rule_id": "r1",
                    "rule_name": "Budget", "alert_type": "budget_breach", "severity": "high",
                    "title": "x", "triggered_at": "2026-06-10T00:00:00+00:00"}

        async def fake_upsert(c, item):
            return item
        with patch("app.services.cosmos.get_item", new=fake_get), \
             patch("app.services.cosmos.upsert_item", new=fake_upsert):
            r = _client().post(f"/api/v1/alerts/{TID}/events/e1/acknowledge")
        assert r.status_code == 200
        assert r.json()["acknowledged"] is True
