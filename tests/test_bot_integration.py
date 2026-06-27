"""
Tests for Slack / Teams bot integration.

Coverage
────────
  TestSlackBlocks          — Slack Block Kit payload structure
  TestTeamsCards           — Teams Adaptive Card payload structure
  TestBuildSlashResponse   — slash-command response builder
  TestVerifySlackSig       — HMAC request signature verification
  TestSendSlack            — webhook delivery (mocked httpx)
  TestSendTeams            — webhook delivery (mocked httpx)
  TestNotifyBudgetBreach   — high-level dispatch
  TestNotifySpendSummary   — high-level dispatch
  TestNotifySpendSpike     — high-level dispatch
  TestRouterSlackEvents    — /slack/events endpoint (url_verification, events)
  TestRouterSlackCommand   — /slack/command endpoint
  TestRouterTeamsMessage   — /teams/message endpoint
  TestRouterNotify         — /notify/budget, /notify/spend, /notify/spike
  TestRouterChannels       — /channels endpoint
"""
from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

# ─────────────────────────────────────────────────────────────────────────────
# Block-kit / card helpers
# ─────────────────────────────────────────────────────────────────────────────

from app.services.bot_notifications import (
    _slack_budget_blocks,
    _slack_spend_blocks,
    _slack_anomaly_blocks,
    _teams_budget_card,
    _teams_spend_card,
    _teams_anomaly_card,
    build_slash_response,
    send_slack,
    send_teams,
    notify_budget_breach,
    notify_spend_summary,
    notify_spend_spike,
    verify_slack_signature,
)


# ── TestSlackBlocks ───────────────────────────────────────────────────────────

class TestSlackBlocks:
    def test_budget_blocks_structure(self):
        blocks = _slack_budget_blocks("t1", "Prod Budget", 150.0, 200.0, 75.0)
        types = [b["type"] for b in blocks]
        assert "header" in types
        assert "section" in types

    def test_budget_blocks_100pct_emoji(self):
        blocks = _slack_budget_blocks("t1", "Budget", 200.0, 200.0, 100.0)
        header_text = blocks[0]["text"]["text"]
        assert "🔴" in header_text

    def test_budget_blocks_80pct_emoji(self):
        blocks = _slack_budget_blocks("t1", "Budget", 160.0, 200.0, 80.0)
        header_text = blocks[0]["text"]["text"]
        assert "🟠" in header_text

    def test_spend_blocks_contain_total(self):
        by_service = [{"service": "Compute", "cost_eur": 100.0}]
        blocks = _slack_spend_blocks("t1", 100.0, by_service)
        text = str(blocks)
        assert "100" in text

    def test_spend_blocks_top_5_only(self):
        by_service = [{"service": f"svc{i}", "cost_eur": float(i)} for i in range(10)]
        blocks = _slack_spend_blocks("t1", 45.0, by_service)
        # svc9..svc5 are top-5; svc4 and below should be excluded
        section = [b for b in blocks if b["type"] == "section"][0]
        assert "svc4" not in section["text"]["text"]  # rank 6+

    def test_anomaly_blocks_contains_service(self):
        blocks = _slack_anomaly_blocks("t1", "AWS EC2", 500.0, 100.0, 400.0)
        text = str(blocks)
        assert "EC2" in text

    def test_anomaly_blocks_section_fields(self):
        blocks = _slack_anomaly_blocks("t1", "EC2", 500.0, 100.0, 400.0)
        section = next(b for b in blocks if b["type"] == "section")
        labels = [f["text"] for f in section["fields"]]
        assert any("Service" in l for l in labels)
        assert any("Increase" in l for l in labels)


# ── TestTeamsCards ────────────────────────────────────────────────────────────

class TestTeamsCards:
    def test_budget_card_type(self):
        card = _teams_budget_card("t1", "Prod", 150.0, 200.0, 75.0)
        assert card["type"] == "message"
        assert card["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"

    def test_budget_card_schema(self):
        card = _teams_budget_card("t1", "Prod", 150.0, 200.0, 75.0)
        content = card["attachments"][0]["content"]
        assert content["type"] == "AdaptiveCard"

    def test_budget_card_color_attention_at_100(self):
        card = _teams_budget_card("t1", "Prod", 200.0, 200.0, 100.0)
        color = card["attachments"][0]["content"]["body"][0]["color"]
        assert color == "attention"

    def test_budget_card_color_warning_at_80(self):
        card = _teams_budget_card("t1", "Prod", 160.0, 200.0, 80.0)
        color = card["attachments"][0]["content"]["body"][0]["color"]
        assert color == "warning"

    def test_spend_card_has_factset(self):
        card = _teams_spend_card("t1", 500.0, [{"service": "EC2", "cost_eur": 500.0}])
        body = card["attachments"][0]["content"]["body"]
        types = [b["type"] for b in body]
        assert "FactSet" in types

    def test_anomaly_card_contains_service(self):
        card = _teams_anomaly_card("t1", "RDS", 300.0, 50.0, 500.0)
        text = str(card)
        assert "RDS" in text

    def test_spend_card_top_5_limit(self):
        by_service = [{"service": f"svc{i}", "cost_eur": float(i)} for i in range(10)]
        card = _teams_spend_card("t1", 45.0, by_service)
        factset = [b for b in card["attachments"][0]["content"]["body"] if b["type"] == "FactSet"][0]
        # 2 fixed facts (Period, Total) + up to 5 services = 7 max
        assert len(factset["facts"]) <= 7


# ── TestBuildSlashResponse ────────────────────────────────────────────────────

class TestBuildSlashResponse:
    def test_error_returns_ephemeral(self):
        r = build_slash_response("t1", "spend", error="Not found")
        assert r["response_type"] == "ephemeral"
        assert "Not found" in r["text"]

    def test_spend_command_returns_blocks(self):
        by_service = [{"service": "EC2", "cost_eur": 100.0}]
        r = build_slash_response("t1", "spend", total_eur=100.0, by_service=by_service)
        assert r["response_type"] == "ephemeral"
        assert "blocks" in r

    def test_help_fallback(self):
        r = build_slash_response("t1", "unknown_command")
        assert "response_type" in r
        assert "cloudlens" in r["text"].lower()

    def test_budget_command(self):
        r = build_slash_response("t1", "budget")
        assert r["response_type"] == "ephemeral"


# ── TestVerifySlackSig ────────────────────────────────────────────────────────

class TestVerifySlackSig:
    def _make_sig(self, secret: str, timestamp: str, body: bytes) -> str:
        base = f"v0:{timestamp}:{body.decode()}"
        return "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()

    @pytest.mark.asyncio
    async def test_valid_signature(self):
        ts = str(int(time.time()))
        body = b"payload=hello"
        secret = "mysecret"
        sig = self._make_sig(secret, ts, body)

        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value=secret)):
            result = await verify_slack_signature(
                "t1", timestamp=ts, signature=sig, raw_body=body
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_invalid_signature(self):
        ts = str(int(time.time()))
        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value="secret")):
            result = await verify_slack_signature(
                "t1", timestamp=ts, signature="v0=bad", raw_body=b"body"
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_expired_timestamp(self):
        old_ts = str(int(time.time()) - 600)
        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value="secret")):
            result = await verify_slack_signature(
                "t1", timestamp=old_ts, signature="v0=anything", raw_body=b"body"
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_secret_configured(self):
        ts = str(int(time.time()))
        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value=None)):
            result = await verify_slack_signature(
                "t1", timestamp=ts, signature="v0=x", raw_body=b"body"
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_invalid_timestamp_format(self):
        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value="secret")):
            result = await verify_slack_signature(
                "t1", timestamp="not-an-int", signature="v0=x", raw_body=b"body"
            )
        assert result is False


# ── TestSendSlack ─────────────────────────────────────────────────────────────

class TestSendSlack:
    @pytest.mark.asyncio
    async def test_returns_false_when_not_configured(self):
        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value=None)):
            result = await send_slack("t1", text="hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_200(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value="https://hooks.slack.com/test")), \
             patch("httpx.AsyncClient") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
            mock_client.return_value = mock_cm
            result = await send_slack("t1", text="hello")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_http_error(self):
        from httpx import HTTPStatusError, Request, Response
        mock_resp = MagicMock()
        err = HTTPStatusError("error", request=MagicMock(), response=MagicMock(status_code=500))
        mock_resp.raise_for_status = MagicMock(side_effect=err)

        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value="https://hooks.slack.com/test")), \
             patch("httpx.AsyncClient") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
            mock_client.return_value = mock_cm
            result = await send_slack("t1", text="hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_uses_override_webhook_url(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        posted_urls = []

        async def fake_post(url, **kwargs):
            posted_urls.append(url)
            return mock_resp

        with patch("httpx.AsyncClient") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.post = fake_post
            mock_client.return_value = mock_cm
            await send_slack("t1", text="hello", webhook_url="https://override.url/webhook")

        assert posted_urls == ["https://override.url/webhook"]

    @pytest.mark.asyncio
    async def test_sends_blocks_when_provided(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        posted_bodies = []

        async def fake_post(url, json=None, **kwargs):
            posted_bodies.append(json)
            return mock_resp

        with patch("httpx.AsyncClient") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.post = fake_post
            mock_client.return_value = mock_cm
            await send_slack("t1", text="txt", blocks=[{"type": "divider"}], webhook_url="https://x.com/w")

        assert "blocks" in posted_bodies[0]


# ── TestSendTeams ─────────────────────────────────────────────────────────────

class TestSendTeams:
    @pytest.mark.asyncio
    async def test_returns_false_when_not_configured(self):
        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value=None)):
            result = await send_teams("t1", text="hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_200(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        with patch("app.services.bot_notifications._get_webhook_url", new=AsyncMock(return_value="https://outlook.office.com/webhook/test")), \
             patch("httpx.AsyncClient") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
            mock_client.return_value = mock_cm
            result = await send_teams("t1", text="hello")
        assert result is True

    @pytest.mark.asyncio
    async def test_sends_card_payload_when_provided(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        posted_bodies = []

        async def fake_post(url, json=None, **kwargs):
            posted_bodies.append(json)
            return mock_resp

        card = _teams_spend_card("t1", 100.0, [])
        with patch("httpx.AsyncClient") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.post = fake_post
            mock_client.return_value = mock_cm
            await send_teams("t1", card=card, webhook_url="https://x.com/w")

        assert posted_bodies[0]["type"] == "message"
        assert "attachments" in posted_bodies[0]


# ── TestNotifyBudgetBreach ────────────────────────────────────────────────────

class TestNotifyBudgetBreach:
    @pytest.mark.asyncio
    async def test_returns_slack_and_teams_keys(self):
        with patch("app.services.bot_notifications.send_slack", new=AsyncMock(return_value=True)), \
             patch("app.services.bot_notifications.send_teams", new=AsyncMock(return_value=False)):
            result = await notify_budget_breach("t1", "Budget", 150.0, 200.0)
        assert "slack" in result
        assert "teams" in result

    @pytest.mark.asyncio
    async def test_slack_true_teams_false(self):
        with patch("app.services.bot_notifications.send_slack", new=AsyncMock(return_value=True)), \
             patch("app.services.bot_notifications.send_teams", new=AsyncMock(return_value=False)):
            result = await notify_budget_breach("t1", "Budget", 200.0, 200.0)
        assert result["slack"] is True
        assert result["teams"] is False

    @pytest.mark.asyncio
    async def test_zero_budget_no_crash(self):
        with patch("app.services.bot_notifications.send_slack", new=AsyncMock(return_value=False)), \
             patch("app.services.bot_notifications.send_teams", new=AsyncMock(return_value=False)):
            result = await notify_budget_breach("t1", "Budget", 0.0, 0.0)
        assert isinstance(result, dict)


# ── TestNotifySpendSummary ────────────────────────────────────────────────────

class TestNotifySpendSummary:
    @pytest.mark.asyncio
    async def test_both_channels_attempted(self):
        slack_mock = AsyncMock(return_value=True)
        teams_mock = AsyncMock(return_value=True)
        with patch("app.services.bot_notifications.send_slack", new=slack_mock), \
             patch("app.services.bot_notifications.send_teams", new=teams_mock):
            await notify_spend_summary("t1", 500.0, [{"service": "EC2", "cost_eur": 500.0}])
        slack_mock.assert_awaited_once()
        teams_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_period_forwarded(self):
        called_with = {}

        async def fake_slack(tid, *, text, blocks=None, webhook_url=None):
            called_with["text"] = text
            return True

        with patch("app.services.bot_notifications.send_slack", new=fake_slack), \
             patch("app.services.bot_notifications.send_teams", new=AsyncMock(return_value=True)):
            await notify_spend_summary("t1", 100.0, [], period="weekly")

        assert "weekly" in called_with["text"]


# ── TestNotifySpendSpike ──────────────────────────────────────────────────────

class TestNotifySpendSpike:
    @pytest.mark.asyncio
    async def test_pct_computed_correctly(self):
        called_with = {}

        async def fake_slack(tid, *, text, blocks=None, webhook_url=None):
            called_with["text"] = text
            return True

        with patch("app.services.bot_notifications.send_slack", new=fake_slack), \
             patch("app.services.bot_notifications.send_teams", new=AsyncMock(return_value=False)):
            await notify_spend_spike("t1", "RDS", current_eur=300.0, baseline_eur=100.0)

        assert "200%" in called_with["text"]

    @pytest.mark.asyncio
    async def test_zero_baseline_no_crash(self):
        with patch("app.services.bot_notifications.send_slack", new=AsyncMock(return_value=False)), \
             patch("app.services.bot_notifications.send_teams", new=AsyncMock(return_value=False)):
            result = await notify_spend_spike("t1", "EC2", current_eur=100.0, baseline_eur=0.0)
        assert isinstance(result, dict)


# ── Router tests ──────────────────────────────────────────────────────────────

@pytest.fixture
def api_key():
    import os
    os.environ.setdefault("INTERNAL_API_KEY", "test-key")
    os.environ.setdefault("COSMOS_ENDPOINT", "https://fake.documents.azure.com:443/")
    os.environ.setdefault("STORAGE_ACCOUNT_NAME", "fakestorage")
    os.environ.setdefault("KEY_VAULT_NAME", "fakekv")
    os.environ.setdefault("AZURE_TENANT_ID", "fake-tenant")
    os.environ.setdefault("AZURE_CLIENT_ID", "fake-client")
    from app.config import get_settings
    return get_settings().internal_api_key


@pytest.fixture
def app(api_key):
    from app.main import app as _app
    return _app


@pytest.fixture
def transport(app):
    return ASGITransport(app=app)


def _make_slack_sig(secret: str, timestamp: str, body: bytes) -> str:
    base = f"v0:{timestamp}:{body.decode()}"
    return "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


# ── TestRouterSlackEvents ────────────────────────────────────────────────────

class TestRouterSlackEvents:
    @pytest.mark.asyncio
    async def test_url_verification_challenge(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/slack/events",
                json={"type": "url_verification", "challenge": "abc123"},
            )
        assert r.status_code == 200
        assert r.json()["challenge"] == "abc123"

    @pytest.mark.asyncio
    async def test_event_invalid_signature_returns_401(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/slack/events",
                content=b'{"type":"event_callback","event":{"type":"app_mention","text":"spend"}}',
                headers={
                    "Content-Type": "application/json",
                    "X-Slack-Request-Timestamp": str(int(time.time())),
                    "X-Slack-Signature": "v0=badsig",
                },
            )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_event_valid_signature_returns_200(self, transport):
        ts = str(int(time.time()))
        body = b'{"type":"event_callback","event":{"type":"app_mention","text":"help"}}'
        sig = _make_slack_sig("mysecret", ts, body)

        with patch("app.routers.bots.verify_slack_signature", new=AsyncMock(return_value=True)):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/slack/events",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Slack-Request-Timestamp": ts,
                        "X-Slack-Signature": sig,
                    },
                )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_event_bad_json_returns_400(self, transport):
        with patch("app.routers.bots.verify_slack_signature", new=AsyncMock(return_value=True)):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/slack/events",
                    content=b"not-json",
                    headers={
                        "Content-Type": "application/json",
                        "X-Slack-Request-Timestamp": str(int(time.time())),
                        "X-Slack-Signature": "v0=x",
                    },
                )
        assert r.status_code == 400


# ── TestRouterSlackCommand ────────────────────────────────────────────────────

class TestRouterSlackCommand:
    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/slack/command",
                content=b"text=spend",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Slack-Request-Timestamp": str(int(time.time())),
                    "X-Slack-Signature": "v0=badsig",
                },
            )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_spend_command_returns_blocks(self, transport):
        with patch("app.routers.bots.verify_slack_signature", new=AsyncMock(return_value=True)), \
             patch("app.routers.bots._today_spend", new=AsyncMock(return_value=(100.0, [{"service": "EC2", "cost_eur": 100.0}]))):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/slack/command",
                    content=b"text=spend",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Slack-Request-Timestamp": str(int(time.time())),
                        "X-Slack-Signature": "v0=x",
                    },
                )
        assert r.status_code == 200
        data = r.json()
        assert "blocks" in data or "text" in data

    @pytest.mark.asyncio
    async def test_status_command_returns_200(self, transport):
        with patch("app.routers.bots.verify_slack_signature", new=AsyncMock(return_value=True)), \
             patch("app.routers.bots._scheduler_status", new=AsyncMock(return_value={"poll_enabled": True, "interval_minutes": 30})):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/slack/command",
                    content=b"text=status",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Slack-Request-Timestamp": str(int(time.time())),
                        "X-Slack-Signature": "v0=x",
                    },
                )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_help_returned_for_unknown_command(self, transport):
        with patch("app.routers.bots.verify_slack_signature", new=AsyncMock(return_value=True)):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/slack/command",
                    content=b"text=unknowncmd",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Slack-Request-Timestamp": str(int(time.time())),
                        "X-Slack-Signature": "v0=x",
                    },
                )
        assert r.status_code == 200
        assert "cloudlens" in r.json().get("text", "").lower()


# ── TestRouterTeamsMessage ────────────────────────────────────────────────────

class TestRouterTeamsMessage:
    @pytest.mark.asyncio
    async def test_conversation_update_returns_greeting(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/teams/message",
                json={"type": "conversationUpdate"},
            )
        assert r.status_code == 200
        assert "bot" in r.json().get("text", "").lower() or "cloudlens" in r.json().get("text", "").lower()

    @pytest.mark.asyncio
    async def test_spend_command_returns_card(self, transport):
        with patch("app.routers.bots._today_spend", new=AsyncMock(return_value=(200.0, [{"service": "RDS", "cost_eur": 200.0}]))):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/teams/message",
                    json={"type": "message", "text": "spend t1"},
                )
        assert r.status_code == 200
        data = r.json()
        assert data.get("type") == "message" or "attachments" in data

    @pytest.mark.asyncio
    async def test_status_command_returns_200(self, transport):
        with patch("app.routers.bots._scheduler_status", new=AsyncMock(return_value={"poll_enabled": True, "interval_minutes": 30})):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/teams/message",
                    json={"type": "message", "text": "status"},
                )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_unknown_activity_returns_200(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/teams/message",
                json={"type": "invoke"},
            )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_help_for_unknown_command(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/teams/message",
                json={"type": "message", "text": "gibberish"},
            )
        assert r.status_code == 200
        assert "cloudlens" in r.json().get("text", "").lower()

    @pytest.mark.asyncio
    async def test_bad_json_returns_400(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/teams/message",
                content=b"not-json",
                headers={"Content-Type": "application/json"},
            )
        assert r.status_code == 400


# ── TestRouterNotify ──────────────────────────────────────────────────────────

class TestRouterNotify:
    @pytest.mark.asyncio
    async def test_budget_notify_requires_api_key(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/notify/budget",
                json={"budget_name": "Prod", "spent_eur": 150.0, "budget_eur": 200.0},
            )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_budget_notify_200_with_key(self, transport, api_key):
        with patch("app.services.bot_notifications.send_slack", new=AsyncMock(return_value=True)), \
             patch("app.services.bot_notifications.send_teams", new=AsyncMock(return_value=False)):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/notify/budget",
                    json={"budget_name": "Prod", "spent_eur": 150.0, "budget_eur": 200.0},
                    headers={"X-API-Key": api_key},
                )
        assert r.status_code == 200
        assert r.json()["delivered"]["slack"] is True

    @pytest.mark.asyncio
    async def test_spend_notify_requires_api_key(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/notify/spend",
                json={"total_eur": 500.0, "by_service": [], "period": "today"},
            )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_spend_notify_200_with_key(self, transport, api_key):
        with patch("app.services.bot_notifications.send_slack", new=AsyncMock(return_value=False)), \
             patch("app.services.bot_notifications.send_teams", new=AsyncMock(return_value=True)):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/notify/spend",
                    json={"total_eur": 500.0, "by_service": [], "period": "today"},
                    headers={"X-API-Key": api_key},
                )
        assert r.status_code == 200
        assert r.json()["delivered"]["teams"] is True

    @pytest.mark.asyncio
    async def test_spike_notify_requires_api_key(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/v1/bots/t1/notify/spike",
                json={"service": "EC2", "current_eur": 300.0, "baseline_eur": 100.0},
            )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_spike_notify_200_with_key(self, transport, api_key):
        with patch("app.services.bot_notifications.send_slack", new=AsyncMock(return_value=True)), \
             patch("app.services.bot_notifications.send_teams", new=AsyncMock(return_value=True)):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post(
                    "/api/v1/bots/t1/notify/spike",
                    json={"service": "EC2", "current_eur": 300.0, "baseline_eur": 100.0},
                    headers={"X-API-Key": api_key},
                )
        assert r.status_code == 200
        assert r.json()["delivered"]["slack"] is True


# ── TestRouterChannels ────────────────────────────────────────────────────────

class TestRouterChannels:
    @pytest.mark.asyncio
    async def test_channels_requires_api_key(self, transport):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/api/v1/bots/t1/channels")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_channels_both_configured(self, transport, api_key):
        with patch("app.routers.bots._get_webhook_url", new=AsyncMock(return_value="https://hook.url")):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/bots/t1/channels", headers={"X-API-Key": api_key})
        assert r.status_code == 200
        data = r.json()
        assert data["slack_configured"] is True
        assert data["teams_configured"] is True

    @pytest.mark.asyncio
    async def test_channels_neither_configured(self, transport, api_key):
        with patch("app.routers.bots._get_webhook_url", new=AsyncMock(return_value=None)):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/api/v1/bots/t1/channels", headers={"X-API-Key": api_key})
        assert r.status_code == 200
        data = r.json()
        assert data["slack_configured"] is False
        assert data["teams_configured"] is False
