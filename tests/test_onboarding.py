"""Tests for self-service onboarding — credential validation + provisioning."""
from __future__ import annotations

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("INTERNAL_API_KEY", "test-key")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com:443/")
os.environ.setdefault("AZURE_TENANT_ID", "test-aad-tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "test-client-id")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "teststorage")

from app.services.onboarding import (
    validate_azure_credentials,
    validate_aws_credentials,
    validate_gcp_credentials,
    provision_tenant,
)


# ── Azure credential validation ───────────────────────────────────────────────

class TestValidateAzureCredentials:
    @pytest.mark.asyncio
    async def test_valid_credentials(self):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"access_token": "fake-token"}
        sub_resp = MagicMock(status_code=200)
        sub_resp.json.return_value = {"displayName": "Test Sub", "state": "Enabled"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=token_resp)
        mock_client.get = AsyncMock(return_value=sub_resp)

        with patch("app.services.onboarding.httpx.AsyncClient", return_value=mock_client):
            result = await validate_azure_credentials(
                "client-id", "secret", "tenant-id",
                ["12345678-1234-1234-1234-123456789abc"],
            )

        assert result.valid is True
        assert result.provider == "azure"
        assert len(result.account_info["subscriptions"]) == 1
        assert result.account_info["subscriptions"][0]["name"] == "Test Sub"

    @pytest.mark.asyncio
    async def test_token_acquisition_failure(self):
        fail_resp = MagicMock(status_code=401)
        fail_resp.json.return_value = {"error_description": "Invalid client secret"}
        fail_resp.text = "Invalid client secret"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=fail_resp)

        with patch("app.services.onboarding.httpx.AsyncClient", return_value=mock_client):
            result = await validate_azure_credentials(
                "bad-id", "bad-secret", "tenant-id", ["sub-1"],
            )

        assert result.valid is False
        assert "Authentication failed" in result.error

    @pytest.mark.asyncio
    async def test_subscription_access_denied(self):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"access_token": "fake-token"}
        denied_resp = MagicMock(status_code=403)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=token_resp)
        mock_client.get = AsyncMock(return_value=denied_resp)

        with patch("app.services.onboarding.httpx.AsyncClient", return_value=mock_client):
            result = await validate_azure_credentials(
                "client-id", "secret", "tenant-id", ["sub-no-access"],
            )

        assert result.valid is False
        assert "Cost Management Reader" in result.error

    @pytest.mark.asyncio
    async def test_partial_subscription_access(self):
        """If any subscription is inaccessible, the whole result is invalid."""
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"access_token": "tok"}
        ok_resp = MagicMock(status_code=200)
        ok_resp.json.return_value = {"displayName": "Sub A", "state": "Enabled"}
        bad_resp = MagicMock(status_code=403)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=token_resp)
        mock_client.get = AsyncMock(side_effect=[ok_resp, bad_resp])

        with patch("app.services.onboarding.httpx.AsyncClient", return_value=mock_client):
            result = await validate_azure_credentials(
                "c", "s", "t", ["sub-a", "sub-b"],
            )

        assert result.valid is False
        assert "sub-b" in result.error


# ── AWS credential validation ─────────────────────────────────────────────────

class TestValidateAwsCredentials:
    @pytest.mark.asyncio
    async def test_valid_role_arn(self):
        result = await validate_aws_credentials(
            "arn:aws:iam::123456789012:role/CloudLensRole",
            ["123456789012"],
        )
        assert result.valid is True
        assert result.provider == "aws"
        assert "iam_trust_policy" in result.account_info
        assert "external_id" in result.account_info
        assert len(result.account_info["external_id"]) == 36  # UUID

    @pytest.mark.asyncio
    async def test_invalid_role_arn_format(self):
        result = await validate_aws_credentials(
            "not-an-arn", ["123456789012"],
        )
        assert result.valid is False
        assert "Invalid role ARN" in result.error

    @pytest.mark.asyncio
    async def test_invalid_account_id_format(self):
        result = await validate_aws_credentials(
            "arn:aws:iam::123456789012:role/Role",
            ["123456789012", "bad-account"],
        )
        assert result.valid is False
        assert "bad-account" in result.error

    @pytest.mark.asyncio
    async def test_arn_account_not_in_accounts_list(self):
        result = await validate_aws_credentials(
            "arn:aws:iam::111111111111:role/Role",
            ["999999999999"],
        )
        assert result.valid is False
        assert "111111111111" in result.error

    @pytest.mark.asyncio
    async def test_custom_external_id_preserved(self):
        result = await validate_aws_credentials(
            "arn:aws:iam::123456789012:role/Role",
            ["123456789012"],
            external_id="my-custom-ext-id",
        )
        assert result.valid is True
        assert result.account_info["external_id"] == "my-custom-ext-id"

    @pytest.mark.asyncio
    async def test_trust_policy_structure(self):
        result = await validate_aws_credentials(
            "arn:aws:iam::123456789012:role/CloudLensRole",
            ["123456789012"],
        )
        policy = result.account_info["iam_trust_policy"]
        assert policy["Version"] == "2012-10-17"
        stmt = policy["Statement"][0]
        assert stmt["Action"] == "sts:AssumeRole"
        assert "sts:ExternalId" in stmt["Condition"]["StringEquals"]


# ── GCP credential validation ─────────────────────────────────────────────────

class TestValidateGcpCredentials:
    _VALID_SA = json.dumps({
        "type": "service_account",
        "project_id": "my-project",
        "private_key_id": "key-id",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
        "client_email": "cloudlens@my-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    })

    @pytest.mark.asyncio
    async def test_valid_service_account(self):
        result = await validate_gcp_credentials(self._VALID_SA, ["my-project"])
        assert result.valid is True
        assert result.account_info["client_email"] == "cloudlens@my-project.iam.gserviceaccount.com"
        assert "required_roles" in result.account_info

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        result = await validate_gcp_credentials("not-json{", ["my-project"])
        assert result.valid is False
        assert "Invalid JSON" in result.error

    @pytest.mark.asyncio
    async def test_wrong_type_field(self):
        sa = json.loads(self._VALID_SA)
        sa["type"] = "authorized_user"
        result = await validate_gcp_credentials(json.dumps(sa), ["p"])
        assert result.valid is False
        assert "service_account" in result.error

    @pytest.mark.asyncio
    async def test_missing_required_fields(self):
        sa = {"type": "service_account", "project_id": "p"}
        result = await validate_gcp_credentials(json.dumps(sa), ["p"])
        assert result.valid is False
        assert "missing required fields" in result.error


# ── Provisioning ──────────────────────────────────────────────────────────────

class TestProvisionTenant:
    @pytest.mark.asyncio
    async def test_azure_only_provision(self):
        with (
            patch("app.services.onboarding.keyvault.store_sp_credentials", new_callable=AsyncMock, return_value="kv-ref-123"),
            patch("app.services.onboarding.cosmos.upsert_item", new_callable=AsyncMock),
        ):
            result = await provision_tenant(
                tenant_name="Acme Corp",
                alert_email="ops@acme.com",
                plan_tier="growth",
                azure_config={
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "tenant_id": "aad-tid",
                    "subscription_ids": ["12345678-0000-0000-0000-000000000001"],
                },
            )

        assert result["tenant_name"] == "Acme Corp"
        assert "azure" in result["enabled_clouds"]
        assert "tenant_id" in result
        assert len(result["tenant_id"]) == 36
        assert "trigger_ingest" in result["next_steps"]

    @pytest.mark.asyncio
    async def test_multicloud_provision(self):
        with (
            patch("app.services.onboarding.keyvault.store_sp_credentials", new_callable=AsyncMock, return_value="ref"),
            patch("app.services.onboarding.cosmos.upsert_item", new_callable=AsyncMock),
        ):
            result = await provision_tenant(
                tenant_name="Multi Cloud Co",
                alert_email="fin@mc.com",
                plan_tier="enterprise",
                azure_config={"client_id": "c", "client_secret": "s", "tenant_id": "t", "subscription_ids": ["12345678-0000-0000-0000-000000000001"]},
                aws_config={"role_arn": "arn:aws:iam::123456789012:role/R", "account_ids": ["123456789012"], "external_id": "ext"},
                gcp_config={"service_account_json": "{}", "project_ids": ["proj-1"], "client_email": "sa@p.iam.gserviceaccount.com"},
            )

        assert sorted(result["enabled_clouds"]) == ["aws", "azure", "gcp"]

    @pytest.mark.asyncio
    async def test_each_provision_gets_unique_tenant_id(self):
        with (
            patch("app.services.onboarding.keyvault.store_sp_credentials", new_callable=AsyncMock, return_value="ref"),
            patch("app.services.onboarding.cosmos.upsert_item", new_callable=AsyncMock),
        ):
            r1 = await provision_tenant("T1", "a@b.com", "growth", azure_config={"client_id": "c", "client_secret": "s", "tenant_id": "t", "subscription_ids": ["12345678-0000-0000-0000-000000000001"]})
            r2 = await provision_tenant("T2", "a@b.com", "growth", azure_config={"client_id": "c", "client_secret": "s", "tenant_id": "t", "subscription_ids": ["12345678-0000-0000-0000-000000000001"]})

        assert r1["tenant_id"] != r2["tenant_id"]


# ── Wizard session ────────────────────────────────────────────────────────────

from app.services.onboarding import (
    create_wizard_session, get_wizard_session, update_wizard_session,
    create_invite, get_invite_by_token, mark_invite_used,
)


class TestWizardSession:
    @pytest.mark.asyncio
    async def test_create_returns_session_id(self):
        with patch("app.services.onboarding.cosmos.upsert_item", new_callable=AsyncMock):
            session = await create_wizard_session()
        assert session["id"].startswith("wiz-")
        assert session["status"] == "in_progress"
        assert session["current_step"] == 1

    @pytest.mark.asyncio
    async def test_create_with_invite_token(self):
        with patch("app.services.onboarding.cosmos.upsert_item", new_callable=AsyncMock):
            session = await create_wizard_session(invite_token="tok123")
        assert session["invite_token"] == "tok123"

    @pytest.mark.asyncio
    async def test_get_session_returns_doc(self):
        fake_doc = {"id": "wiz-abc123", "type": "wizard_session", "tenant_id": "wiz-abc123", "status": "in_progress", "current_step": 2}
        with patch("app.services.onboarding.cosmos.get_item", new_callable=AsyncMock, return_value=fake_doc):
            result = await get_wizard_session("wiz-abc123")
        assert result["id"] == "wiz-abc123"
        assert result["current_step"] == 2

    @pytest.mark.asyncio
    async def test_get_session_returns_none_on_missing(self):
        with patch("app.services.onboarding.cosmos.get_item", new_callable=AsyncMock, side_effect=Exception("not found")):
            result = await get_wizard_session("wiz-missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_session_merges_fields(self):
        fake_doc = {"id": "wiz-abc", "tenant_id": "wiz-abc", "type": "wizard_session", "status": "in_progress", "current_step": 1, "step_account": {}}
        upserted = {}

        async def fake_upsert(container, doc):
            upserted.update(doc)

        with patch("app.services.onboarding.cosmos.get_item", new_callable=AsyncMock, return_value=fake_doc), \
             patch("app.services.onboarding.cosmos.upsert_item", side_effect=fake_upsert):
            updated = await update_wizard_session("wiz-abc", {"current_step": 3, "status": "in_progress"})

        assert updated["current_step"] == 3
        assert upserted["current_step"] == 3

    @pytest.mark.asyncio
    async def test_update_nonexistent_raises(self):
        with patch("app.services.onboarding.cosmos.get_item", new_callable=AsyncMock, side_effect=Exception("not found")):
            with pytest.raises(ValueError, match="not found"):
                await update_wizard_session("wiz-missing", {"current_step": 2})

    @pytest.mark.asyncio
    async def test_session_has_ttl(self):
        with patch("app.services.onboarding.cosmos.upsert_item", new_callable=AsyncMock):
            session = await create_wizard_session()
        assert session["ttl"] == 86_400


class TestInviteLinks:
    @pytest.mark.asyncio
    async def test_create_invite_returns_token(self):
        with patch("app.services.onboarding.cosmos.upsert_item", new_callable=AsyncMock):
            inv = await create_invite(email="user@acme.com", plan_tier="growth")
        assert len(inv["token"]) >= 32
        assert inv["email"] == "user@acme.com"
        assert inv["plan_tier"] == "growth"
        assert inv["used"] is False

    @pytest.mark.asyncio
    async def test_invite_id_prefixed(self):
        with patch("app.services.onboarding.cosmos.upsert_item", new_callable=AsyncMock):
            inv = await create_invite(email="user@acme.com")
        assert inv["id"].startswith("inv-")

    @pytest.mark.asyncio
    async def test_invite_has_ttl(self):
        with patch("app.services.onboarding.cosmos.upsert_item", new_callable=AsyncMock):
            inv = await create_invite(email="user@acme.com")
        assert inv["ttl"] == 7 * 86_400

    @pytest.mark.asyncio
    async def test_get_invite_by_token(self):
        fake_inv = {"id": "inv-abc", "token": "tok-xyz", "email": "u@a.com", "used": False}
        with patch("app.services.onboarding.cosmos.query_items", new_callable=AsyncMock, return_value=[fake_inv]):
            result = await get_invite_by_token("tok-xyz")
        assert result["token"] == "tok-xyz"

    @pytest.mark.asyncio
    async def test_get_invite_returns_none_when_missing(self):
        with patch("app.services.onboarding.cosmos.query_items", new_callable=AsyncMock, return_value=[]):
            result = await get_invite_by_token("bad-token")
        assert result is None

    @pytest.mark.asyncio
    async def test_mark_invite_used(self):
        fake_inv = {"id": "inv-abc", "tenant_id": "inv-abc", "token": "tok-xyz", "used": False}
        upserted = {}

        async def fake_upsert(container, doc):
            upserted.update(doc)

        with patch("app.services.onboarding.cosmos.query_items", new_callable=AsyncMock, return_value=[fake_inv]), \
             patch("app.services.onboarding.cosmos.upsert_item", side_effect=fake_upsert):
            await mark_invite_used("tok-xyz", "tenant-999")

        assert upserted["used"] is True
        assert upserted["used_by_tenant_id"] == "tenant-999"
        assert "used_at" in upserted

    @pytest.mark.asyncio
    async def test_mark_invite_used_on_missing_is_noop(self):
        with patch("app.services.onboarding.cosmos.query_items", new_callable=AsyncMock, return_value=[]):
            # Should not raise
            await mark_invite_used("nonexistent", "tenant-x")


# ── Router: wizard + invite endpoints ────────────────────────────────────────

from httpx import AsyncClient, ASGITransport


@pytest.fixture(scope="module")
def _app():
    from app.main import app as _a
    return _a


@pytest.fixture(scope="module")
def transport(_app):
    return ASGITransport(app=_app)


def _key():
    from app.config import get_settings
    return get_settings().internal_api_key


class TestRouterWizardSession:
    @pytest.mark.asyncio
    async def test_create_session_201(self, transport):
        with patch("app.routers.onboarding.create_wizard_session", new_callable=AsyncMock,
                   return_value={"id": "wiz-001", "status": "in_progress", "current_step": 1}):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/api/v1/onboarding/wizard/session")
        assert r.status_code == 201
        assert r.json()["session_id"] == "wiz-001"

    @pytest.mark.asyncio
    async def test_create_session_invalid_invite_returns_404(self, transport):
        with patch("app.routers.onboarding.get_invite_by_token", new_callable=AsyncMock, return_value=None):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/api/v1/onboarding/wizard/session?invite_token=badtoken")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_create_session_used_invite_returns_409(self, transport):
        with patch("app.routers.onboarding.get_invite_by_token", new_callable=AsyncMock,
                   return_value={"token": "tok", "used": True}):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/api/v1/onboarding/wizard/session?invite_token=tok")
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_get_session_200(self, transport):
        fake = {"id": "wiz-001", "status": "in_progress", "current_step": 2, "tenant_id": "wiz-001"}
        with patch("app.routers.onboarding.get_wizard_session", new_callable=AsyncMock, return_value=fake):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/onboarding/wizard/session/wiz-001")
        assert r.status_code == 200
        assert r.json()["current_step"] == 2

    @pytest.mark.asyncio
    async def test_get_session_404_when_missing(self, transport):
        with patch("app.routers.onboarding.get_wizard_session", new_callable=AsyncMock, return_value=None):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/onboarding/wizard/session/wiz-missing")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_session_200(self, transport):
        fake_session = {"id": "wiz-001", "status": "in_progress", "current_step": 1, "tenant_id": "wiz-001"}
        updated = {"id": "wiz-001", "status": "in_progress", "current_step": 3}
        with patch("app.routers.onboarding.get_wizard_session", new_callable=AsyncMock, return_value=fake_session), \
             patch("app.routers.onboarding.update_wizard_session", new_callable=AsyncMock, return_value=updated):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.patch("/api/v1/onboarding/wizard/session/wiz-001",
                                   json={"current_step": 3})
        assert r.status_code == 200
        assert r.json()["current_step"] == 3

    @pytest.mark.asyncio
    async def test_patch_missing_session_returns_404(self, transport):
        with patch("app.routers.onboarding.get_wizard_session", new_callable=AsyncMock, return_value=None):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.patch("/api/v1/onboarding/wizard/session/wiz-missing",
                                   json={"current_step": 2})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_complete_session_201(self, transport):
        fake_session = {"id": "wiz-001", "status": "in_progress", "invite_token": None}
        provision_result = {"tenant_id": "t-abc123", "tenant_name": "Acme", "enabled_clouds": ["azure"]}
        with patch("app.routers.onboarding.get_wizard_session", new_callable=AsyncMock, return_value=fake_session), \
             patch("app.routers.onboarding.provision_tenant", new_callable=AsyncMock, return_value=provision_result), \
             patch("app.routers.onboarding.update_wizard_session", new_callable=AsyncMock, return_value=fake_session), \
             patch("app.routers.onboarding._keyvault") as mock_kv:
            mock_kv.set_secret = AsyncMock()
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/api/v1/onboarding/wizard/session/wiz-001/complete",
                    json={"session_id": "wiz-001", "tenant_name": "Acme", "alert_email": "a@b.com",
                          "plan_tier": "growth", "azure": {"client_id": "c", "client_secret": "s",
                          "tenant_id": "t", "subscription_ids": ["12345678-0000-0000-0000-000000000001"]}})
        assert r.status_code == 201
        assert r.json()["tenant_id"] == "t-abc123"

    @pytest.mark.asyncio
    async def test_complete_already_done_returns_409(self, transport):
        fake_session = {"id": "wiz-001", "status": "completed", "invite_token": None}
        with patch("app.routers.onboarding.get_wizard_session", new_callable=AsyncMock, return_value=fake_session):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/api/v1/onboarding/wizard/session/wiz-001/complete",
                    json={"session_id": "wiz-001", "tenant_name": "Co", "alert_email": "x@x.com",
                          "plan_tier": "growth", "azure": {"client_id": "c", "client_secret": "s",
                          "tenant_id": "t", "subscription_ids": ["12345678-0000-0000-0000-000000000001"]}})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_complete_no_clouds_returns_422(self, transport):
        fake_session = {"id": "wiz-001", "status": "in_progress", "invite_token": None}
        with patch("app.routers.onboarding.get_wizard_session", new_callable=AsyncMock, return_value=fake_session):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/api/v1/onboarding/wizard/session/wiz-001/complete",
                    json={"session_id": "wiz-001", "tenant_name": "Co", "alert_email": "x@x.com", "plan_tier": "growth"})
        assert r.status_code == 422


class TestRouterInvite:
    @pytest.mark.asyncio
    async def test_create_invite_requires_api_key(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post("/api/v1/onboarding/invite",
                              json={"email": "user@acme.com", "plan_tier": "growth"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_create_invite_201_with_key(self, transport):
        fake_inv = {"id": "inv-abc", "token": "tok-xyz123", "email": "u@acme.com", "plan_tier": "growth"}
        with patch("app.routers.onboarding.create_invite", new_callable=AsyncMock, return_value=fake_inv):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/api/v1/onboarding/invite",
                                  json={"email": "u@acme.com", "plan_tier": "growth"},
                                  headers={"X-API-Key": _key()})
        assert r.status_code == 201
        data = r.json()
        assert data["token"] == "tok-xyz123"
        assert "wizard_url" in data

    @pytest.mark.asyncio
    async def test_validate_invite_valid(self, transport):
        fake_inv = {"id": "inv-abc", "token": "tok", "email": "u@a.com", "plan_tier": "growth", "used": False}
        with patch("app.routers.onboarding.get_invite_by_token", new_callable=AsyncMock, return_value=fake_inv):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/onboarding/invite/tok")
        assert r.status_code == 200
        assert r.json()["valid"] is True

    @pytest.mark.asyncio
    async def test_validate_invite_used_returns_valid_false(self, transport):
        fake_inv = {"id": "inv-abc", "token": "tok", "email": "u@a.com", "plan_tier": "growth", "used": True}
        with patch("app.routers.onboarding.get_invite_by_token", new_callable=AsyncMock, return_value=fake_inv):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/onboarding/invite/tok")
        assert r.status_code == 200
        assert r.json()["valid"] is False

    @pytest.mark.asyncio
    async def test_validate_invite_missing_returns_404(self, transport):
        with patch("app.routers.onboarding.get_invite_by_token", new_callable=AsyncMock, return_value=None):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/onboarding/invite/nonexistent")
        assert r.status_code == 404

