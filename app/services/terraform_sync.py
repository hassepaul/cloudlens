"""
Terraform Drift Management
===========================
When CloudLens autonomously provisions or modifies cloud infrastructure via the
AI agent, Terraform state becomes stale — the resource exists in the cloud but
is unknown to any `*.tf` file or `terraform.tfstate`.

This service implements the "drift-visible, engineer-reconciles" pattern:

  1. **Tag** every autonomously created resource with a standard label set so
     Terraform can discover unmanaged resources via a tag-based resource scan.

  2. **Record** the action as a ``TerraformDriftRecord`` in Cosmos so the team
     has a persistent audit trail of what was done outside IaC.

  3. **Generate** a ready-to-paste HCL block and the ``terraform import``
     command for the resource, giving engineers the fastest path to
     reconciliation.

  4. **Notify** via a configurable webhook (Slack / Teams / PagerDuty) so
     engineers learn about drift immediately rather than at the next
     ``terraform plan``.

  5. **Track** reconciliation status (``pending`` → ``acknowledged`` →
     ``imported``) so the team knows what is still outstanding.

Reconciliation workflow
-----------------------
  AI proposes action
    → human approval gate (POST /agent/{tenant}/sessions/{s}/approve/{id})
    → action executes; resource gets cloudlens: tags
    → drift record created (status=pending)
    → webhook fires to #infra-alerts channel
  Engineer:
    → sees drift in /terraform/{tenant}/drift (or the in-app Drift panel)
    → copies HCL snippet into the relevant .tf file
    → runs the generated ``terraform import`` command
    → calls POST /terraform/{tenant}/drift/{record_id}/acknowledge
    → drift status moves to acknowledged

Why tags, not a direct Terraform run?
--------------------------------------
The AI agent is a *read-only-by-default* service.  Running
``terraform apply`` inline would require:
  - Storing state remotely (already the case if you use Terraform Cloud /
    remote backend) AND giving the agent write access to that backend —
    which is a significant blast radius.
  - Knowing the full module context (variables, backend config, etc.).
Instead, tags let ``terraform plan -refresh-only`` surface the resource as
"unmanaged", making the gap obvious.  Generating the import command eliminates
most of the engineer effort.

Tag conventions (all tags prefixed ``cloudlens:``)
---------------------------------------------------
  source        = "autonomous"          always
  action_id     = "<uuid>"              AI agent action ID
  approval_id   = "<uuid>"              approval record ID
  approved_by   = "<identifier>"        who approved (user/session ID)
  tenant_id     = "<slug>"             CloudLens tenant
  created_at    = "2026-06-27T…"       ISO-8601 UTC
  resource_type = "<tf_resource_type>"  e.g. aws_budgets_budget
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

try:
    import httpx as _httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos

log = get_logger(__name__)

# ── Tag constants ─────────────────────────────────────────────────────────────

AUTONOMOUS_SOURCE_TAG = "cloudlens:source"
AUTONOMOUS_ACTION_ID_TAG = "cloudlens:action_id"
AUTONOMOUS_APPROVAL_ID_TAG = "cloudlens:approval_id"
AUTONOMOUS_APPROVED_BY_TAG = "cloudlens:approved_by"
AUTONOMOUS_TENANT_TAG = "cloudlens:tenant_id"
AUTONOMOUS_CREATED_AT_TAG = "cloudlens:created_at"
AUTONOMOUS_RESOURCE_TYPE_TAG = "cloudlens:resource_type"

DRIFT_STATUS_PENDING = "pending"
DRIFT_STATUS_ACKNOWLEDGED = "acknowledged"
DRIFT_STATUS_IMPORTED = "imported"


def build_autonomous_tags(
    action_id: str,
    approval_id: str,
    tenant_id: str,
    resource_type: str,
    approved_by: str = "",
) -> dict[str, str]:
    """Return the standard tag dict to apply to every autonomously-created resource."""
    return {
        AUTONOMOUS_SOURCE_TAG: "autonomous",
        AUTONOMOUS_ACTION_ID_TAG: action_id,
        AUTONOMOUS_APPROVAL_ID_TAG: approval_id,
        AUTONOMOUS_APPROVED_BY_TAG: approved_by,
        AUTONOMOUS_TENANT_TAG: tenant_id,
        AUTONOMOUS_CREATED_AT_TAG: datetime.now(timezone.utc).isoformat(),
        AUTONOMOUS_RESOURCE_TYPE_TAG: resource_type,
    }


# ── HCL template registry ─────────────────────────────────────────────────────

def _hcl_aws_budgets_budget(resource_name: str, config: dict, tags: dict) -> str:
    name = config.get("name", resource_name)
    limit = config.get("monthly_limit_usd") or config.get("monthly_limit_eur") or 100
    currency = "USD" if "monthly_limit_usd" in config else "EUR"
    tag_block = "\n".join(f'    "{k}" = "{v}"' for k, v in tags.items())
    return textwrap.dedent(f"""\
        resource "aws_budgets_budget" "{resource_name}" {{
          name         = "{name}"
          budget_type  = "COST"
          limit_amount = "{limit}"
          limit_unit   = "{currency}"
          time_unit    = "MONTHLY"

          tags = {{
        {tag_block}
          }}
        }}
        """)


def _hcl_azure_budget(resource_name: str, config: dict, tags: dict) -> str:
    name = config.get("name", resource_name)
    limit = config.get("monthly_limit_eur") or config.get("monthly_limit_usd") or 100
    tag_block = "\n".join(f'    "{k}" = "{v}"' for k, v in tags.items())
    return textwrap.dedent(f"""\
        # NOTE: choose the right resource type for your scope:
        # - azurerm_consumption_budget_subscription
        # - azurerm_consumption_budget_resource_group
        # - azurerm_consumption_budget_management_group
        resource "azurerm_consumption_budget_subscription" "{resource_name}" {{
          name            = "{name}"
          subscription_id = data.azurerm_client_config.current.subscription_id

          amount     = {limit}
          time_grain = "Monthly"

          time_period {{
            start_date = "2026-07-01T00:00:00Z"
          }}

          notification {{
            enabled   = true
            threshold = 80
            operator  = "GreaterThan"
            contact_emails = ["infra@example.com"]
          }}

          tags = {{
        {tag_block}
          }}
        }}
        """)


def _hcl_azure_monitor_metric_alert(resource_name: str, config: dict, tags: dict) -> str:
    name = config.get("name", resource_name)
    threshold = config.get("threshold_eur") or config.get("threshold") or 0
    tag_block = "\n".join(f'    "{k}" = "{v}"' for k, v in tags.items())
    return textwrap.dedent(f"""\
        resource "azurerm_monitor_metric_alert" "{resource_name}" {{
          name                = "{name}"
          resource_group_name = var.resource_group_name
          scopes              = [data.azurerm_subscription.current.id]

          criteria {{
            metric_namespace = "Microsoft.CostManagement/budgets"
            metric_name      = "ActualCost"
            aggregation      = "Total"
            operator         = "GreaterThan"
            threshold        = {threshold}
          }}

          action {{
            action_group_id = var.alert_action_group_id
          }}

          tags = {{
        {tag_block}
          }}
        }}
        """)


def _hcl_aws_cloudwatch_metric_alarm(resource_name: str, config: dict, tags: dict) -> str:
    name = config.get("name", resource_name)
    threshold = config.get("threshold_eur") or config.get("threshold") or 0
    tag_block = "\n".join(f'    "{k}" = "{v}"' for k, v in tags.items())
    return textwrap.dedent(f"""\
        resource "aws_cloudwatch_metric_alarm" "{resource_name}" {{
          alarm_name          = "{name}"
          comparison_operator = "GreaterThanOrEqualToThreshold"
          evaluation_periods  = 1
          metric_name         = "EstimatedCharges"
          namespace           = "AWS/Billing"
          period              = 86400
          statistic           = "Maximum"
          threshold           = {threshold}
          alarm_description   = "CloudLens autonomous alert: {name}"
          treat_missing_data  = "notBreaching"

          tags = {{
        {tag_block}
          }}
        }}
        """)


def _hcl_cloudlens_budget(resource_name: str, config: dict, tags: dict) -> str:
    """Fallback HCL for CloudLens-internal budgets (Cosmos-backed, not cloud-native)."""
    name = config.get("name", resource_name)
    limit = config.get("monthly_limit_eur") or 100
    tag_block = "\n".join(f'  # {k} = "{v}"' for k, v in tags.items())
    return textwrap.dedent(f"""\
        # CloudLens internal budget — stored in Cosmos DB, not a cloud-native resource.
        # No terraform import is possible; managed via the CloudLens API.
        #
        # Resource details:
        #   name               = "{name}"
        #   monthly_limit_eur  = {limit}
        #
        # Autonomous-execution tags (informational):
        {tag_block}
        #
        # To align your IaC, document this budget in your CloudLens tenant config:
        #   resource "cloudlens_budget" "{resource_name}" {{
        #     name              = "{name}"
        #     monthly_limit_eur = {limit}
        #   }}
        """)


def _hcl_generic(resource_name: str, resource_type: str, config: dict, tags: dict) -> str:
    """Generic HCL skeleton for resource types without a specific template."""
    tag_block = "\n".join(f'    "{k}" = "{v}"' for k, v in tags.items())
    attrs = "\n".join(f'  # {k} = "{v}"' for k, v in config.items() if k not in ("name",))
    return textwrap.dedent(f"""\
        resource "{resource_type}" "{resource_name}" {{
          # TODO: fill in required attributes for this resource type
          name = "{config.get("name", resource_name)}"

          # Autonomous-execution config values:
        {attrs}

          tags = {{
        {tag_block}
          }}
        }}
        """)


_HCL_REGISTRY: dict[str, callable] = {
    "aws_budgets_budget": _hcl_aws_budgets_budget,
    "azurerm_consumption_budget_subscription": _hcl_azure_budget,
    "azurerm_consumption_budget_resource_group": _hcl_azure_budget,
    "azurerm_consumption_budget_management_group": _hcl_azure_budget,
    "azurerm_monitor_metric_alert": _hcl_azure_monitor_metric_alert,
    "aws_cloudwatch_metric_alarm": _hcl_aws_cloudwatch_metric_alarm,
    "cloudlens_budget": _hcl_cloudlens_budget,
}


def generate_hcl(
    resource_type: str,
    resource_name: str,
    config: dict,
    tags: dict,
) -> str:
    """Generate a Terraform HCL block for a resource.

    Uses a specific template if available, falls back to a generic skeleton.
    """
    fn = _HCL_REGISTRY.get(resource_type)
    if fn:
        return fn(resource_name, config, tags)
    return _hcl_generic(resource_name, resource_type, config, tags)


# ── terraform import command generation ───────────────────────────────────────

def generate_import_cmd(
    resource_type: str,
    resource_name: str,
    resource_id: str,
    prefix: str = "",
) -> str:
    """Generate the ``terraform import`` shell command for a resource.

    The generated ID format follows the Terraform provider conventions for
    each supported resource type.

    Parameters
    ----------
    resource_type:
        Terraform resource type (e.g. ``aws_budgets_budget``).
    resource_name:
        Logical Terraform name for the resource.
    resource_id:
        Cloud-provider resource identifier.
    prefix:
        Optional module path prefix (e.g. ``module.networking``).
    """
    full_name = f"{prefix}.{resource_type}.{resource_name}" if prefix else f"{resource_type}.{resource_name}"

    # AWS Budgets: <account_id>:<budget_name>
    if resource_type == "aws_budgets_budget":
        return f"terraform import {full_name} '<AWS_ACCOUNT_ID>:{resource_id}'"

    # Azure consumption budgets: full ARM resource path
    if resource_type.startswith("azurerm_consumption_budget"):
        scope_map = {
            "azurerm_consumption_budget_subscription": "/subscriptions/<SUBSCRIPTION_ID>",
            "azurerm_consumption_budget_resource_group": "/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RG_NAME>",
            "azurerm_consumption_budget_management_group": "/providers/Microsoft.Management/managementGroups/<MG_ID>",
        }
        scope = scope_map.get(resource_type, "/subscriptions/<SUBSCRIPTION_ID>")
        return (
            f"terraform import {full_name} "
            f"'{scope}/providers/Microsoft.Consumption/budgets/{resource_id}'"
        )

    # Azure Monitor metric alert
    if resource_type == "azurerm_monitor_metric_alert":
        return (
            f"terraform import {full_name} "
            f"'/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RG_NAME>"
            f"/providers/Microsoft.Insights/metricAlerts/{resource_id}'"
        )

    # CloudWatch alarm
    if resource_type == "aws_cloudwatch_metric_alarm":
        return f"terraform import {full_name} '{resource_id}'"

    # Generic fallback
    return f"terraform import {full_name} '{resource_id}'"


# ── Drift record ──────────────────────────────────────────────────────────────

@dataclass
class TerraformDriftRecord:
    id: str
    tenant_id: str
    action_id: str            # AI agent action ID
    approval_id: str          # approval record ID
    approved_by: str          # user/session that approved
    tool_name: str            # create_budget | create_alert_rule | …
    resource_type: str        # Terraform resource type
    resource_name: str        # Terraform logical name (used in HCL + import cmd)
    resource_id: str          # Cloud resource identifier
    provider: str             # aws | azure | gcp | internal
    region: str               # cloud region (empty for global resources)
    hcl_snippet: str          # ready-to-paste HCL block
    import_cmd: str           # terraform import command
    tags: dict = field(default_factory=dict)
    status: str = DRIFT_STATUS_PENDING  # pending | acknowledged | imported
    created_at: str = ""
    acknowledged_at: str = ""
    acknowledged_by: str = ""
    notification_sent: bool = False

    def to_cosmos(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "_partitionKey": self.tenant_id,
            "type": "terraform_drift",
            "action_id": self.action_id,
            "approval_id": self.approval_id,
            "approved_by": self.approved_by,
            "tool_name": self.tool_name,
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "resource_id": self.resource_id,
            "provider": self.provider,
            "region": self.region,
            "hcl_snippet": self.hcl_snippet,
            "import_cmd": self.import_cmd,
            "tags": self.tags,
            "status": self.status,
            "created_at": self.created_at,
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_by": self.acknowledged_by,
            "notification_sent": self.notification_sent,
        }

    @staticmethod
    def from_cosmos(doc: dict) -> "TerraformDriftRecord":
        return TerraformDriftRecord(
            id=doc["id"],
            tenant_id=doc["tenant_id"],
            action_id=doc.get("action_id", ""),
            approval_id=doc.get("approval_id", ""),
            approved_by=doc.get("approved_by", ""),
            tool_name=doc.get("tool_name", ""),
            resource_type=doc.get("resource_type", ""),
            resource_name=doc.get("resource_name", ""),
            resource_id=doc.get("resource_id", ""),
            provider=doc.get("provider", ""),
            region=doc.get("region", ""),
            hcl_snippet=doc.get("hcl_snippet", ""),
            import_cmd=doc.get("import_cmd", ""),
            tags=doc.get("tags") or {},
            status=doc.get("status", DRIFT_STATUS_PENDING),
            created_at=doc.get("created_at", ""),
            acknowledged_at=doc.get("acknowledged_at", ""),
            acknowledged_by=doc.get("acknowledged_by", ""),
            notification_sent=bool(doc.get("notification_sent", False)),
        )


# ── Resource-type inference ───────────────────────────────────────────────────

# Maps (tool_name, provider_hint) → (terraform_resource_type, cloud_provider)
_TOOL_RESOURCE_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("create_budget", "aws"):     ("aws_budgets_budget",                        "aws"),
    ("create_budget", "azure"):   ("azurerm_consumption_budget_subscription",   "azure"),
    ("create_budget", "gcp"):     ("google_billing_budget",                     "gcp"),
    ("create_budget", ""):        ("cloudlens_budget",                          "internal"),
    ("create_alert_rule", "aws"): ("aws_cloudwatch_metric_alarm",               "aws"),
    ("create_alert_rule", "azure"):("azurerm_monitor_metric_alert",             "azure"),
    ("create_alert_rule", ""):    ("azurerm_monitor_metric_alert",              "azure"),
}


def _infer_resource_type(tool_name: str, provider_hint: str = "") -> tuple[str, str]:
    """Return (terraform_resource_type, cloud_provider) for a tool + provider hint."""
    key = (tool_name, provider_hint.lower())
    if key in _TOOL_RESOURCE_MAP:
        return _TOOL_RESOURCE_MAP[key]
    # Try without provider hint
    fallback = (tool_name, "")
    if fallback in _TOOL_RESOURCE_MAP:
        return _TOOL_RESOURCE_MAP[fallback]
    return (f"cloudlens_{tool_name}", "internal")


# ── Core recording function ───────────────────────────────────────────────────

async def record_drift(
    tenant_id: str,
    action_id: str,
    approval_id: str,
    tool_name: str,
    tool_params: dict,
    tool_result: dict,
    approved_by: str = "",
    provider_hint: str = "",
) -> TerraformDriftRecord:
    """Record an autonomous action as a TerraformDriftRecord.

    Called by ``approve_action()`` after a write-tool executes successfully.
    Creates the drift record, generates HCL + import cmd, persists to Cosmos,
    and fires the configured webhook.

    Parameters
    ----------
    tenant_id:
        CloudLens tenant slug.
    action_id:
        AI agent pending-action UUID.
    approval_id:
        Same as action_id in current flow; separate to allow future decoupling.
    tool_name:
        Name of the write tool (``create_budget``, ``create_alert_rule``, …).
    tool_params:
        Parameters passed to the tool.
    tool_result:
        Return value from the tool (contains resource IDs).
    approved_by:
        Identifier of the approver (session ID / user ID).
    provider_hint:
        Cloud provider hint (``aws``, ``azure``, ``gcp``) if known.
    """
    settings = get_settings()
    resource_type, cloud_provider = _infer_resource_type(tool_name, provider_hint)

    # Derive a stable Terraform resource name: prefix + first 8 chars of action_id
    resource_name = f"{settings.terraform_drift_resource_prefix}_{action_id[:8]}"

    # Best-effort resource ID from tool result
    resource_id = (
        tool_result.get("budget_id")
        or tool_result.get("rule_id")
        or tool_result.get("id")
        or action_id
    )

    # Build the tag set that should have been applied to the cloud resource
    tags = build_autonomous_tags(
        action_id=action_id,
        approval_id=approval_id,
        tenant_id=tenant_id,
        resource_type=resource_type,
        approved_by=approved_by,
    )

    # Generate HCL and import command
    config = {**tool_params, **{k: v for k, v in tool_result.items() if k not in ("created", "status")}}
    hcl = generate_hcl(resource_type, resource_name, config, tags)
    import_cmd = generate_import_cmd(resource_type, resource_name, resource_id)

    now = datetime.now(timezone.utc).isoformat()
    record = TerraformDriftRecord(
        id=str(uuid4()),
        tenant_id=tenant_id,
        action_id=action_id,
        approval_id=approval_id,
        approved_by=approved_by,
        tool_name=tool_name,
        resource_type=resource_type,
        resource_name=resource_name,
        resource_id=resource_id,
        provider=cloud_provider,
        region=tool_params.get("region", ""),
        hcl_snippet=hcl,
        import_cmd=import_cmd,
        tags=tags,
        status=DRIFT_STATUS_PENDING,
        created_at=now,
    )

    # Persist
    try:
        await cosmos.upsert_item(settings.cosmos_container_terraform_drift, record.to_cosmos())
    except CosmosError as exc:
        log.error("terraform_drift.persist_failed", action_id=action_id, error=str(exc))

    # Fire notification
    webhook_url = settings.terraform_drift_webhook_url
    if webhook_url:
        record.notification_sent = await _fire_webhook(webhook_url, record)
        # Update notification_sent flag
        try:
            await cosmos.upsert_item(settings.cosmos_container_terraform_drift, record.to_cosmos())
        except CosmosError:
            pass

    log.info(
        "terraform_drift.recorded",
        tenant_id=tenant_id,
        action_id=action_id,
        resource_type=resource_type,
        resource_name=resource_name,
        provider=cloud_provider,
    )
    return record


# ── Query API ─────────────────────────────────────────────────────────────────

async def list_drift(tenant_id: str, status_filter: str = "") -> list[TerraformDriftRecord]:
    """List drift records for a tenant. Optionally filter by status."""
    conditions = "c.tenant_id=@t AND c.type='terraform_drift'"
    params: list[dict] = [{"name": "@t", "value": tenant_id}]
    if status_filter:
        conditions += " AND c.status=@s"
        params.append({"name": "@s", "value": status_filter})
    sql = f"SELECT * FROM c WHERE {conditions} ORDER BY c.created_at DESC"
    try:
        docs = await cosmos.query_items(
            get_settings().cosmos_container_terraform_drift, sql, params, partition_key=tenant_id
        )
        return [TerraformDriftRecord.from_cosmos(d) for d in docs]
    except CosmosError:
        return []


async def get_drift_record(tenant_id: str, record_id: str) -> Optional[TerraformDriftRecord]:
    """Fetch a single drift record by ID."""
    try:
        doc = await cosmos.get_item(
            get_settings().cosmos_container_terraform_drift, record_id, tenant_id
        )
        return TerraformDriftRecord.from_cosmos(doc)
    except Exception:
        return None


async def acknowledge_drift(
    tenant_id: str,
    record_id: str,
    acknowledged_by: str,
    new_status: str = DRIFT_STATUS_ACKNOWLEDGED,
) -> Optional[TerraformDriftRecord]:
    """Mark a drift record as acknowledged or imported.

    Parameters
    ----------
    new_status:
        ``acknowledged`` (HCL added to IaC, not yet imported) or
        ``imported`` (``terraform import`` completed successfully).
    """
    if new_status not in (DRIFT_STATUS_ACKNOWLEDGED, DRIFT_STATUS_IMPORTED):
        raise ValueError(f"Invalid status: {new_status!r}")

    record = await get_drift_record(tenant_id, record_id)
    if not record:
        return None

    record.status = new_status
    record.acknowledged_at = datetime.now(timezone.utc).isoformat()
    record.acknowledged_by = acknowledged_by

    try:
        await cosmos.upsert_item(get_settings().cosmos_container_terraform_drift, record.to_cosmos())
    except CosmosError as exc:
        log.error("terraform_drift.acknowledge_failed", record_id=record_id, error=str(exc))
        return None
    return record


async def dismiss_drift(tenant_id: str, record_id: str) -> bool:
    """Delete / dismiss a drift record."""
    try:
        await cosmos.delete_item(get_settings().cosmos_container_terraform_drift, record_id, tenant_id)
        return True
    except Exception:
        return False


async def get_drift_summary(tenant_id: str) -> dict:
    """Return counts by status for the drift dashboard KPI strip."""
    records = await list_drift(tenant_id)
    counts = {DRIFT_STATUS_PENDING: 0, DRIFT_STATUS_ACKNOWLEDGED: 0, DRIFT_STATUS_IMPORTED: 0}
    for r in records:
        if r.status in counts:
            counts[r.status] += 1
    return {
        "pending": counts[DRIFT_STATUS_PENDING],
        "acknowledged": counts[DRIFT_STATUS_ACKNOWLEDGED],
        "imported": counts[DRIFT_STATUS_IMPORTED],
        "total": len(records),
        "all_reconciled": counts[DRIFT_STATUS_PENDING] == 0,
    }


# ── Webhook notification ──────────────────────────────────────────────────────

def _build_webhook_payload(record: TerraformDriftRecord) -> dict:
    """Build a Slack-compatible webhook payload (also works for Teams via
    ``attachments`` being ignored gracefully)."""
    colour = "#e67e22"  # orange = drift / attention needed
    summary = (
        f"🔧 Terraform drift detected in *{record.tenant_id}*\n"
        f"Action `{record.tool_name}` created a `{record.resource_type}` "
        f"resource (`{record.resource_name}`) outside Terraform."
    )
    import_block = f"```\n{record.import_cmd}\n```"
    hcl_preview = record.hcl_snippet[:600] + ("…" if len(record.hcl_snippet) > 600 else "")

    return {
        "text": summary,
        "attachments": [
            {
                "color": colour,
                "title": f"Autonomous resource: {record.resource_type}.{record.resource_name}",
                "fields": [
                    {"title": "Provider",     "value": record.provider,       "short": True},
                    {"title": "Resource ID",  "value": record.resource_id,    "short": True},
                    {"title": "Action ID",    "value": record.action_id,      "short": True},
                    {"title": "Approved by",  "value": record.approved_by or "—", "short": True},
                ],
                "text": (
                    "*Terraform import command:*\n"
                    f"{import_block}\n\n"
                    "*HCL snippet (partial):*\n"
                    f"```\n{hcl_preview}\n```\n\n"
                    "_Run `terraform plan -refresh-only` to confirm, then update your .tf files._"
                ),
                "footer": "CloudLens Autonomous Execution | Drift Management",
                "ts": int(datetime.now(timezone.utc).timestamp()),
            }
        ],
    }


async def _fire_webhook(webhook_url: str, record: TerraformDriftRecord) -> bool:
    """POST the drift notification to the configured webhook URL.

    Returns True on success, False on failure.  Never raises — webhook
    failures must not block the autonomous action.
    """
    if not webhook_url or not _HTTPX_AVAILABLE:
        return False
    payload = _build_webhook_payload(record)
    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code < 300:
                log.info("terraform_drift.webhook_sent", action_id=record.action_id)
                return True
            log.warning(
                "terraform_drift.webhook_failed",
                status=resp.status_code,
                action_id=record.action_id,
            )
    except Exception as exc:
        log.warning("terraform_drift.webhook_error", error=str(exc), action_id=record.action_id)
    return False
