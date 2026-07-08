"""
Tests for enterprise SSO (SAML session tokens), SCIM 2.0 provisioning, and the
Redis-backed / in-process rate limiter.
Run: pytest tests/test_sso_scim.py -v
"""
from __future__ import annotations
import os
import pytest

os.environ.setdefault("INTERNAL_API_KEY", "super-secret-key")
os.environ.setdefault("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000000")
os.environ.setdefault("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
os.environ.setdefault("COSMOS_ENDPOINT", "https://test.documents.azure.com")
os.environ.setdefault("STORAGE_ACCOUNT_NAME", "testaccount")
os.environ.setdefault("KEY_VAULT_NAME", "test-kv")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("SESSION_JWT_SECRET", "unit-test-session-secret")

from fastapi import HTTPException

from app.config import get_settings
from app.exceptions import NotFoundError, UnauthorizedError
from app.models.identity import ScimUser, IdentityConfig, hash_token
from app.services import session_token


# ══════════════════════════════════════════════════════════════════════════════
# In-memory Cosmos fake
# ══════════════════════════════════════════════════════════════════════════════

class FakeCosmos:
    def __init__(self):
        self.store: dict[tuple[str, str], dict] = {}

    async def get_item(self, container, item_id, partition_key):
        key = (container, item_id)
        if key not in self.store:
            raise NotFoundError(f"{item_id} not found")
        return dict(self.store[key])

    async def upsert_item(self, container, item):
        self.store[(container, item["id"])] = dict(item)
        return dict(item)

    async def delete_item(self, container, item_id, partition_key):
        key = (container, item_id)
        if key not in self.store:
            raise NotFoundError(f"{item_id} not found")
        del self.store[key]

    async def query_items(self, container, query, parameters=None, partition_key=None, max_item_count=100):
        params = {p["name"]: p["value"] for p in (parameters or [])}
        rows = [dict(v) for (c, _), v in self.store.items() if c == container]
        if "@t" in params:
            rows = [r for r in rows if r.get("tenant_id") == params["@t"]]
        if "@u" in params:
            rows = [r for r in rows if r.get("user_name") == params["@u"]]
        rows.sort(key=lambda r: r.get("created_at", ""))
        return rows


@pytest.fixture
def fake_cosmos(monkeypatch):
    fake = FakeCosmos()
    from app.services import cosmos as real
    monkeypatch.setattr(real, "get_item", fake.get_item)
    monkeypatch.setattr(real, "upsert_item", fake.upsert_item)
    monkeypatch.setattr(real, "delete_item", fake.delete_item)
    monkeypatch.setattr(real, "query_items", fake.query_items)
    return fake


# ══════════════════════════════════════════════════════════════════════════════
# SESSION TOKENS
# ══════════════════════════════════════════════════════════════════════════════

class TestSessionToken:
    def test_mint_and_verify_roundtrip(self):
        token, ttl = session_token.mint_session("u@acme.com", "t-acme",
                                                email="u@acme.com", roles=["admin"])
        assert ttl == get_settings().session_ttl_hours * 3600
        claims = session_token.verify_session(token)
        assert claims["sub"] == "u@acme.com"
        assert claims["tid"] == "t-acme"
        assert claims["roles"] == ["admin"]
        assert claims["iss"] == get_settings().session_issuer

    def test_tampered_token_rejected(self):
        token, _ = session_token.mint_session("u@acme.com", "t-acme")
        with pytest.raises(UnauthorizedError):
            session_token.verify_session(token + "x")

    def test_disabled_when_secret_empty(self):
        s = get_settings()
        original = s.session_jwt_secret
        object.__setattr__(s, "session_jwt_secret", "")
        try:
            assert session_token.is_enabled() is False
            with pytest.raises(UnauthorizedError):
                session_token.mint_session("u", "t")
        finally:
            object.__setattr__(s, "session_jwt_secret", original)

    @pytest.mark.asyncio
    async def test_bearer_auth_accepts_session_token(self):
        from app.auth import verify_bearer_token
        token, _ = session_token.mint_session("u@acme.com", "t-acme", roles=["viewer"])
        ctx = await verify_bearer_token(authorization=f"Bearer {token}")
        assert ctx.tenant_id == "t-acme"
        assert ctx.subject == "u@acme.com"
        assert "viewer" in ctx.scopes


# ══════════════════════════════════════════════════════════════════════════════
# SCIM PROVISIONING
# ══════════════════════════════════════════════════════════════════════════════

def _user_payload(username="jane@acme.com", active=True):
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": username,
        "externalId": "ext-123",
        "name": {"givenName": "Jane", "familyName": "Doe"},
        "emails": [{"value": username, "type": "work", "primary": True}],
        "active": active,
    }


class TestScimProvisioning:
    @pytest.mark.asyncio
    async def test_token_rotation_and_verify(self, fake_cosmos):
        from app.services import scim
        token = await scim.rotate_scim_token("t-acme")
        assert token.startswith("scim_")
        assert await scim.verify_scim_token("t-acme", token) is True
        assert await scim.verify_scim_token("t-acme", "wrong") is False
        # different tenant has no token
        assert await scim.verify_scim_token("t-other", token) is False

    @pytest.mark.asyncio
    async def test_create_get_and_conflict(self, fake_cosmos):
        from app.services import scim
        user, err = await scim.create_user("t-acme", _user_payload())
        assert err is None and user is not None
        assert user.user_name == "jane@acme.com"
        assert user.given_name == "Jane"
        # duplicate userName → conflict
        dup, err2 = await scim.create_user("t-acme", _user_payload())
        assert dup is None and err2 == "conflict"
        # fetch back
        got = await scim.get_user("t-acme", user.id)
        assert got is not None and got.id == user.id

    @pytest.mark.asyncio
    async def test_list_filter_and_pagination(self, fake_cosmos):
        from app.services import scim
        await scim.create_user("t-acme", _user_payload("a@acme.com"))
        await scim.create_user("t-acme", _user_payload("b@acme.com"))
        await scim.create_user("t-acme", _user_payload("c@acme.com"))
        users, total = await scim.list_users("t-acme")
        assert total == 3
        # filter by userName eq
        filtered, ftotal = await scim.list_users("t-acme", 'userName eq "b@acme.com"')
        assert ftotal == 1 and filtered[0].user_name == "b@acme.com"
        # pagination
        page, total = await scim.list_users("t-acme", "", start_index=2, count=1)
        assert total == 3 and len(page) == 1

    @pytest.mark.asyncio
    async def test_patch_deactivate(self, fake_cosmos):
        from app.services import scim
        user, _ = await scim.create_user("t-acme", _user_payload())
        # SCIM PATCH replace active=false (deprovisioning)
        patched = await scim.patch_user("t-acme", user.id,
                                        [{"op": "replace", "path": "active", "value": "False"}])
        assert patched is not None and patched.active is False

    @pytest.mark.asyncio
    async def test_replace_and_delete(self, fake_cosmos):
        from app.services import scim
        user, _ = await scim.create_user("t-acme", _user_payload())
        replaced = await scim.replace_user("t-acme", user.id, _user_payload(active=False))
        assert replaced.active is False
        assert await scim.delete_user("t-acme", user.id) is True
        assert await scim.get_user("t-acme", user.id) is None
        assert await scim.delete_user("t-acme", user.id) is False


class TestScimUserModel:
    def test_to_scim_shape(self):
        u = ScimUser(tenant_id="t", user_name="x@y.com", given_name="X", family_name="Y",
                     emails=[{"value": "x@y.com", "type": "work", "primary": True}])
        doc = u.to_scim("https://api/scim/t/v2/Users")
        assert doc["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
        assert doc["userName"] == "x@y.com"
        assert doc["meta"]["location"].endswith(u.id)
        assert doc["active"] is True

    def test_from_scim_forces_primary_email(self):
        u = ScimUser.from_scim("t", {"userName": "z@y.com",
                                     "emails": [{"value": "z@y.com"}]})
        assert u.emails[0]["primary"] is True


# ══════════════════════════════════════════════════════════════════════════════
# RATE LIMITER — Redis atomic path + in-process fallback
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimiterRedis:
    def setup_method(self):
        from app.rate_limit import reset
        reset()

    @pytest.mark.asyncio
    async def test_ip_fallback_in_process(self):
        # No REDIS_URL configured → in-process fallback enforces the limit.
        from app.rate_limit import enforce_ip_rate_limit
        for _ in range(3):
            await enforce_ip_rate_limit("1.2.3.4", limit_per_min=3)
        with pytest.raises(HTTPException) as exc:
            await enforce_ip_rate_limit("1.2.3.4", limit_per_min=3)
        assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_redis_atomic_allow_and_deny(self, monkeypatch):
        import app.rate_limit as rl

        class FakeRedis:
            def __init__(self, verdict):
                self.verdict = verdict
                self.calls = 0

            async def eval(self, *args):
                self.calls += 1
                return self.verdict

        # Deny path → 429
        fake_deny = FakeRedis(0)
        monkeypatch.setattr(rl, "_redis_client", fake_deny)
        monkeypatch.setattr(rl, "_redis_unavailable", False)
        with pytest.raises(HTTPException) as exc:
            await rl.enforce_ip_rate_limit("9.9.9.9", limit_per_min=100)
        assert exc.value.status_code == 429
        assert fake_deny.calls == 1

        # Allow path → passes
        fake_allow = FakeRedis(1)
        monkeypatch.setattr(rl, "_redis_client", fake_allow)
        await rl.enforce_ip_rate_limit("9.9.9.9", limit_per_min=100)
        assert fake_allow.calls == 1

    @pytest.mark.asyncio
    async def test_redis_error_falls_back(self, monkeypatch):
        import app.rate_limit as rl

        class BrokenRedis:
            async def eval(self, *args):
                raise RuntimeError("connection refused")

        monkeypatch.setattr(rl, "_redis_client", BrokenRedis())
        monkeypatch.setattr(rl, "_redis_unavailable", False)
        # Should not raise 500 — falls back to in-process and allows within limit.
        await rl.enforce_ip_rate_limit("8.8.8.8", limit_per_min=5)


# ══════════════════════════════════════════════════════════════════════════════
# SAML SERVICE (toolkit-optional)
# ══════════════════════════════════════════════════════════════════════════════

class TestSamlService:
    def test_normalize_cert_strips_pem(self):
        from app.services import saml_sso
        pem = "-----BEGIN CERTIFICATE-----\nABC123\nDEF456\n-----END CERTIFICATE-----"
        assert saml_sso._normalize_cert(pem) == "ABC123DEF456"

    def test_urls_and_settings_shape(self):
        from app.services import saml_sso
        from app.models.identity import SamlConfig
        base = "https://api.cloudlens.io"
        assert saml_sso.acs_url("t-acme", base).endswith("/api/v1/auth/saml/t-acme/acs")
        cfg = SamlConfig(idp_entity_id="https://idp/meta", idp_sso_url="https://idp/sso",
                         idp_x509_cert="ABC")
        s = saml_sso.build_settings("t-acme", cfg, base)
        assert s["idp"]["entityId"] == "https://idp/meta"
        assert s["sp"]["assertionConsumerService"]["url"].endswith("/acs")
        assert s["security"]["wantAssertionsSigned"] is True

    def test_toolkit_missing_raises_unavailable(self):
        from app.services import saml_sso
        try:
            import onelogin.saml2  # noqa: F401
            pytest.skip("python3-saml is installed; SamlUnavailable path not exercised")
        except ImportError:
            from app.models.identity import SamlConfig
            cfg = SamlConfig(idp_entity_id="e", idp_sso_url="u", idp_x509_cert="c")
            with pytest.raises(saml_sso.SamlUnavailable):
                saml_sso.metadata_xml("t", cfg, "https://api")


class TestIdentityConfig:
    def test_roundtrip_cosmos(self):
        cfg = IdentityConfig(id="t-acme", tenant_id="t-acme",
                             scim_enabled=True, scim_token_hash=hash_token("abc"))
        doc = cfg.to_cosmos()
        assert doc["_partitionKey"] == "t-acme"
        back = IdentityConfig.from_cosmos(dict(doc))
        assert back.scim_enabled is True
        assert back.scim_token_hash == hash_token("abc")
