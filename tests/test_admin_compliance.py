"""
Admin & compliance router tests: audit log, control matrix, integrity, export.
Run: pytest tests/test_admin_compliance.py -v
"""
from __future__ import annotations
import os
from unittest.mock import patch

os.environ.setdefault("INTERNAL_API_KEY", "super-secret-key")
os.environ.setdefault("AZURE_TENANT_ID", "test")
os.environ.setdefault("AZURE_CLIENT_ID", "test")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "teststore")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")

from app.config import get_settings
KEY = {"X-API-Key": get_settings().internal_api_key}


def _client():
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


class TestComplianceMatrix:
    def test_matrix_requires_api_key(self):
        assert _client().get("/api/v1/admin/compliance/matrix").status_code in (401, 403)

    def test_matrix_has_controls_and_disclaimer(self):
        r = _client().get("/api/v1/admin/compliance/matrix", headers=KEY)
        assert r.status_code == 200
        body = r.json()
        assert "CPA firm" in body["disclaimer"]            # honest about attestation
        assert body["summary"]["total"] >= 12
        # every control has criteria id + status
        for c in body["controls"]:
            assert c["criteria_id"] and c["status"] in (
                "implemented", "partial", "needs_org_process")

    def test_matrix_includes_cli_evidence(self):
        r = _client().get("/api/v1/admin/compliance/matrix", headers=KEY)
        controls = r.json()["controls"]
        with_cli = [c for c in controls if c["cli_evidence"]]
        assert with_cli
        ev = with_cli[0]["cli_evidence"][0]
        assert ev["command"] and ev["expected"]

    def test_summary_counts_consistent(self):
        body = _client().get("/api/v1/admin/compliance/matrix", headers=KEY).json()
        s = body["summary"]
        assert s["implemented"] + s["partial"] + s["needs_org_process"] == s["total"]


class TestAuditLog:
    def test_write_and_chain(self):
        store = []

        async def fake_upsert(c, item):
            store.append(item)
            return item

        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            # return latest by reverse insertion for prev-hash lookup
            return [store[-1]] if store and "record_hash" in q else []

        import asyncio
        from app.routers.admin import write_audit
        from app.models.audit import AuditAction
        with patch("app.services.cosmos.upsert_item", new=fake_upsert), \
             patch("app.services.cosmos.query_items", new=fake_query):
            r1 = asyncio.get_event_loop().run_until_complete(
                write_audit("t-1", AuditAction.TENANT_CREATED, "ops", actor_type="api_key"))
            r2 = asyncio.get_event_loop().run_until_complete(
                write_audit("t-1", AuditAction.TENANT_UPDATED, "ops", actor_type="api_key"))
        assert r1.record_hash
        assert r2.prev_hash == r1.record_hash      # chained

    def test_audit_integrity_endpoint(self):
        from app.models.audit import AuditRecord, AuditAction, chain_record
        recs, prev = [], ""
        for _ in range(3):
            rr = chain_record(AuditRecord(tenant_id="t-1", action=AuditAction.WASTE_RESOLVED, actor="a"), prev)
            prev = rr.record_hash
            recs.append(rr.to_cosmos())

        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return recs
        with patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().get("/api/v1/admin/compliance/audit-integrity/t-1", headers=KEY)
        assert r.status_code == 200
        assert r.json()["intact"] is True
        assert r.json()["records"] == 3


class TestEvidenceExport:
    def test_export_generates_pack_and_audits_itself(self):
        upserts = []

        async def fake_upsert(c, item):
            upserts.append(item)
            return item

        async def fake_query(c, q, parameters=None, partition_key=None, **kw):
            return []
        with patch("app.services.cosmos.upsert_item", new=fake_upsert), \
             patch("app.services.cosmos.query_items", new=fake_query):
            r = _client().post("/api/v1/admin/compliance/evidence-export?tenant_id=_system&actor=auditor",
                               headers=KEY)
        assert r.status_code == 200
        body = r.json()
        assert body["export_id"].startswith("evidence-")
        assert body["controls"] and body["summary"]
        # the export itself was written to the audit log (CC7.2)
        assert any(u.get("action") == "evidence_exported" for u in upserts)

    def test_export_requires_api_key(self):
        assert _client().post("/api/v1/admin/compliance/evidence-export").status_code in (401, 403)
