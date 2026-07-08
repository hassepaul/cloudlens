"""
CloudLens Commitment Purchaser
==============================

Automated execution of commitment purchases identified by the Commitment
Advisor across **AWS, Azure and GCP**:
  - AWS   — Reserved Instances (RI) + Savings Plans (SP)
  - Azure — Compute Savings Plans (azure-mgmt-billingbenefits)
  - GCP   — Committed Use Discounts / CUDs (google-cloud-compute)

SAFETY GATES — two independent locks, both must be open to execute:
  1. Global kill switch: Settings.commitment_auto_purchase_enabled (env var
     ``COMMITMENT_AUTO_PURCHASE_ENABLED=true``). Default: ``false``.
  2. Per-tenant flag: PurchaseSettings.enabled stored in Cosmos DB.
     Default: ``false``.

Even when both gates are open, execution is blocked when:
  - The advisor confidence is below ``min_confidence_score`` (default 0.70).
  - The individual purchase exceeds ``max_single_purchase_eur``.
  - The total purchased this calendar month already exceeds
    ``max_monthly_budget_eur``.
  - ``dry_run`` is ``true`` on the tenant settings (default ``true``) — in
    dry-run mode the service logs exactly what *would* be purchased but makes
    no mutating cloud API calls.

Per-cloud provider SDK calls are synchronous and run in asyncio thread-pool
executors so they never block the event loop. Azure/GCP purchasing SDKs are
optional dependencies — if a live purchase is attempted without them installed
the run produces a ``failed`` record with a clear message (never crashes).

Key Vault credential secrets (one per cloud, per tenant):
  ``aws-creds-<tenant_id>``    JSON: role_arn, external_id, region
  ``azure-creds-<tenant_id>``  JSON: tenant_id, client_id, client_secret,
                               subscription_id, billing_scope
  ``gcp-creds-<tenant_id>``    JSON: project_id, region, service_account (key dict)

Required provider permissions for LIVE purchases:
  AWS   — savingsplans:CreateSavingsPlan, ec2:PurchaseReservedInstancesOffering
          (+ the matching Describe* offerings actions)
  Azure — Microsoft.BillingBenefits/savingsPlanOrderAliases/write on the
          billing scope
  GCP   — compute.commitments.create on the project
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos
from app.services import keyvault as _keyvault

log = get_logger(__name__)

# ── Cosmos container key (added to config) ──────────────────────────────────
_CONTAINER_SETTINGS = "commitment_purchase_settings"
_CONTAINER_PURCHASES = "commitment_purchases"

# Only Savings Plans / RIs / CUDs make sense to auto-purchase. Purchasable
# commitment types are cloud-specific (they map to different provider APIs).
_PURCHASABLE_BY_CLOUD: dict[str, set[str]] = {
    "aws":   {"savings-plan-1yr", "savings-plan-3yr", "1yr-ri", "3yr-ri"},
    "azure": {"savings-plan-1yr", "savings-plan-3yr"},
    "gcp":   {"committed-use-1yr", "committed-use-3yr"},
}
_SUPPORTED_CLOUDS = set(_PURCHASABLE_BY_CLOUD)
# Union of all purchasable types (used by the settings validator).
_PURCHASABLE_TYPES = set().union(*_PURCHASABLE_BY_CLOUD.values())

# Per-cloud Key Vault credential secret name templates.
_CLOUD_CREDS_SECRET = {
    "aws":   "aws-creds-{tenant_id}",
    "azure": "azure-creds-{tenant_id}",
    "gcp":   "gcp-creds-{tenant_id}",
}

# Safety floor: refuse to auto-purchase if the advisor confidence is below this
# (overridable per-tenant via min_confidence_score).
_DEFAULT_MIN_CONFIDENCE = 0.70

# SP plan type strings → boto3 term lengths
_SP_TERM = {"savings-plan-1yr": "ONE_YEAR", "savings-plan-3yr": "THREE_YEAR"}
_RI_TERM = {"1yr-ri": "31536000", "3yr-ri": "94608000"}   # duration in seconds
# Azure savings-plan term (ISO 8601 duration) and GCP CUD plan enums.
_AZURE_TERM = {"savings-plan-1yr": "P1Y", "savings-plan-3yr": "P3Y"}
_GCP_PLAN = {"committed-use-1yr": "TWELVE_MONTH", "committed-use-3yr": "THIRTY_SIX_MONTH"}


class CommitmentAutoDisabledError(Exception):
    """Raised when auto-purchase is disabled (global or per-tenant gate)."""


class CommitmentPurchaseLimitError(Exception):
    """Raised when a purchase would exceed a configured budget cap."""


# ── Per-tenant settings model ────────────────────────────────────────────────

@dataclass
class PurchaseSettings:
    tenant_id: str
    enabled: bool = False           # MUST be explicitly True to purchase
    dry_run: bool = True            # simulate-only until explicitly disabled
    max_single_purchase_eur: float = 5_000.0
    max_monthly_budget_eur: float = 20_000.0
    min_confidence_score: float = _DEFAULT_MIN_CONFIDENCE
    # empty list = allow all purchasable types
    allowed_commitment_types: list[str] = field(default_factory=list)
    # empty list = allow all services
    allowed_services: list[str] = field(default_factory=list)
    updated_at: str = ""

    def to_cosmos(self) -> dict:
        return {
            "id": self.tenant_id,
            "tenant_id": self.tenant_id,
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "max_single_purchase_eur": self.max_single_purchase_eur,
            "max_monthly_budget_eur": self.max_monthly_budget_eur,
            "min_confidence_score": self.min_confidence_score,
            "allowed_commitment_types": self.allowed_commitment_types,
            "allowed_services": self.allowed_services,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_cosmos(cls, doc: dict) -> "PurchaseSettings":
        return cls(
            tenant_id=doc["tenant_id"],
            enabled=bool(doc.get("enabled", False)),
            dry_run=bool(doc.get("dry_run", True)),
            max_single_purchase_eur=float(doc.get("max_single_purchase_eur", 5_000.0)),
            max_monthly_budget_eur=float(doc.get("max_monthly_budget_eur", 20_000.0)),
            min_confidence_score=float(doc.get("min_confidence_score", _DEFAULT_MIN_CONFIDENCE)),
            allowed_commitment_types=list(doc.get("allowed_commitment_types", [])),
            allowed_services=list(doc.get("allowed_services", [])),
            updated_at=doc.get("updated_at", ""),
        )


# ── Purchase record model ────────────────────────────────────────────────────

@dataclass
class PurchaseRecord:
    id: str
    tenant_id: str
    purchased_at: str               # ISO-8601 UTC
    cloud: str                      # "aws"
    commitment_type: str            # "savings-plan-1yr" etc.
    service: str
    hourly_commitment_usd: float
    monthly_saving_eur: float
    term_months: int
    dry_run: bool
    status: str                     # "purchased" | "dry_run" | "failed" | "skipped"
    aws_commitment_id: Optional[str] = None  # SP ARN or RI ID
    error: Optional[str] = None
    skip_reason: Optional[str] = None
    confidence_score: float = 0.0

    def to_cosmos(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "purchased_at": self.purchased_at,
            "cloud": self.cloud,
            "commitment_type": self.commitment_type,
            "service": self.service,
            "hourly_commitment_usd": self.hourly_commitment_usd,
            "monthly_saving_eur": self.monthly_saving_eur,
            "term_months": self.term_months,
            "dry_run": self.dry_run,
            "status": self.status,
            "aws_commitment_id": self.aws_commitment_id,
            "error": self.error,
            "skip_reason": self.skip_reason,
            "confidence_score": self.confidence_score,
        }


@dataclass
class PurchaseRun:
    tenant_id: str
    run_at: str
    dry_run: bool
    purchased: list[PurchaseRecord] = field(default_factory=list)
    skipped: list[PurchaseRecord] = field(default_factory=list)
    failed: list[PurchaseRecord] = field(default_factory=list)
    total_committed_eur: float = 0.0
    notes: list[str] = field(default_factory=list)


# ── Settings CRUD ────────────────────────────────────────────────────────────

async def get_purchase_settings(tenant_id: str) -> PurchaseSettings:
    """
    Load per-tenant auto-purchase settings from Cosmos.
    Returns a default (disabled) settings object if none saved yet.
    """
    try:
        rows = await cosmos.query_items(
            _CONTAINER_SETTINGS,
            "SELECT * FROM c WHERE c.tenant_id=@t",
            parameters=[{"name": "@t", "value": tenant_id}],
            partition_key=tenant_id,
        )
    except CosmosError:
        rows = []
    if not rows:
        return PurchaseSettings(tenant_id=tenant_id)
    return PurchaseSettings.from_cosmos(rows[0])


async def save_purchase_settings(settings: PurchaseSettings) -> PurchaseSettings:
    """Upsert per-tenant auto-purchase settings."""
    settings.updated_at = datetime.now(timezone.utc).isoformat()
    doc = settings.to_cosmos()
    await cosmos.upsert_item(_CONTAINER_SETTINGS, doc)
    return settings


# ── Monthly spend guard ──────────────────────────────────────────────────────

async def _month_spend_eur(tenant_id: str) -> float:
    """Sum of non-dry-run purchases executed this calendar month."""
    ym = date.today().strftime("%Y-%m")
    try:
        rows = await cosmos.query_items(
            _CONTAINER_PURCHASES,
            """SELECT VALUE SUM(c.monthly_saving_eur)
               FROM c
               WHERE c.tenant_id=@t
                 AND c.status='purchased'
                 AND STARTSWITH(c.purchased_at, @ym)""",
            parameters=[
                {"name": "@t", "value": tenant_id},
                {"name": "@ym", "value": ym},
            ],
            partition_key=tenant_id,
        )
    except CosmosError:
        return 0.0
    return float(rows[0] if rows and rows[0] is not None else 0.0)


# ── AWS client helpers ───────────────────────────────────────────────────────

def _assume_role(role_arn: str, external_id: str, session_name: str = "cloudlens-purchase"):
    """STS AssumeRole — return temporary credentials dict."""
    import boto3
    sts = boto3.client("sts")
    kwargs: dict = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
        "DurationSeconds": 3600,
    }
    if external_id:
        kwargs["ExternalId"] = external_id
    return sts.assume_role(**kwargs)["Credentials"]


def _sp_client(creds: dict, region: str = "us-east-1"):
    import boto3
    return boto3.client(
        "savingsplans",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _ec2_client(creds: dict, region: str):
    import boto3
    return boto3.client(
        "ec2",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


# ── AWS Savings Plans purchase ───────────────────────────────────────────────

def _find_sp_offering_sync(
    sp: object,
    plan_type: str,              # "COMPUTE" | "EC2_INSTANCE" | "SAGEMAKER"
    term_seconds: str,           # "31536000" | "94608000"
    payment_option: str = "NO_UPFRONT",
) -> Optional[str]:
    """
    Find a matching Savings Plans offering ID.
    Returns the first offering's offeringId or None if none found.
    """
    resp = sp.describe_savings_plans_offerings(
        planTypes=[plan_type],
        durations=[int(term_seconds)],
        paymentOptions=[payment_option],
        currencies=["USD"],
        maxResults=10,
    )
    offerings = resp.get("searchResults", [])
    return offerings[0]["offeringId"] if offerings else None


def _purchase_sp_sync(
    sp: object,
    offering_id: str,
    hourly_commitment_usd: float,
    dry_run: bool,
) -> str:
    """
    Purchase a Savings Plan. Returns the savings plan ARN.
    Raises on AWS API error.
    """
    if dry_run:
        return f"dry-run-sp-arn-{uuid.uuid4().hex[:8]}"
    resp = sp.create_savings_plan(
        savingsPlanOfferingId=offering_id,
        commitment=f"{hourly_commitment_usd:.5f}",
        tags={"ManagedBy": "CloudLens", "PurchaseType": "auto"},
    )
    return resp["savingsPlanId"]


# ── AWS Reserved Instance purchase ──────────────────────────────────────────

def _find_ri_offering_sync(
    ec2: object,
    instance_type: str,
    region: str,
    term_seconds: int,
    offering_class: str = "convertible",
    product_description: str = "Linux/UNIX",
) -> Optional[str]:
    """
    Find the first EC2 RI offering matching instance_type + term.
    Returns offering ID or None.
    """
    resp = ec2.describe_reserved_instances_offerings(
        InstanceType=instance_type,
        IncludeMarketplace=False,
        OfferingClass=offering_class,
        ProductDescription=product_description,
        MinDuration=term_seconds,
        MaxDuration=term_seconds,
        OfferingType="No Upfront",
        MaxResults=5,
    )
    offerings = resp.get("ReservedInstancesOfferings", [])
    return offerings[0]["ReservedInstancesOfferingId"] if offerings else None


def _purchase_ri_sync(
    ec2: object,
    offering_id: str,
    instance_count: int,
    dry_run: bool,
) -> str:
    """Purchase a Reserved Instance. Returns the RI ID."""
    if dry_run:
        return f"dry-run-ri-id-{uuid.uuid4().hex[:8]}"
    resp = ec2.purchase_reserved_instances_offering(
        ReservedInstancesOfferingId=offering_id,
        InstanceCount=instance_count,
        DryRun=False,
    )
    return resp["ReservedInstancesId"]


# ── Core purchase dispatcher ─────────────────────────────────────────────────

async def _execute_aws_purchase(
    advisory: dict,
    tenant_id: str,
    creds: dict,
    dry_run: bool,
    fx_rate_eur_to_usd: float = 1.08,
) -> PurchaseRecord:
    """
    Execute a single AWS advisory purchase (SP or RI).
    Returns a PurchaseRecord regardless of success/failure.
    """
    role_arn = creds["role_arn"]
    external_id = creds.get("external_id", "")
    region = creds.get("region", "us-east-1")
    commitment_type = advisory["recommended_type"]
    service = advisory["service"]
    monthly_eur = float(advisory["on_demand_monthly_eur"])
    horizon_months = int(advisory.get("commitment_horizon_months", 12))
    confidence = float(advisory.get("confidence_score", 0.0))
    # Convert monthly EUR to hourly USD (conservative: use on_demand floor only)
    hourly_eur = monthly_eur / 30 / 24
    hourly_usd = round(hourly_eur * fx_rate_eur_to_usd, 5)

    rec_id = uuid.uuid4().hex
    now_utc = datetime.now(timezone.utc).isoformat()

    try:
        if commitment_type in _SP_TERM:
            term_str = _SP_TERM[commitment_type]          # "ONE_YEAR" | "THREE_YEAR"
            term_secs = "31536000" if term_str == "ONE_YEAR" else "94608000"
            # Map service → Savings Plan type
            plan_type = "COMPUTE"   # broadest coverage; works across instance families
            if "EC2" in service.upper() or "EC2" in service:
                plan_type = "EC2_INSTANCE"

            def _do_sp():
                creds = _assume_role(role_arn, external_id)
                sp = _sp_client(creds, region)
                oid = _find_sp_offering_sync(sp, plan_type, term_secs)
                if not oid:
                    raise RuntimeError(f"No Savings Plan offering found for {plan_type}/{term_secs}")
                return _purchase_sp_sync(sp, oid, hourly_usd, dry_run)

            loop = asyncio.get_running_loop()
            commitment_id = await loop.run_in_executor(None, _do_sp)

        elif commitment_type in _RI_TERM:
            # For RI we need an instance type; use a sensible default from
            # the service name or fall back to m5.large.
            instance_type = _infer_instance_type(service)
            term_secs = int(_RI_TERM[commitment_type])

            def _do_ri():
                creds = _assume_role(role_arn, external_id)
                ec2 = _ec2_client(creds, region)
                oid = _find_ri_offering_sync(ec2, instance_type, region, term_secs)
                if not oid:
                    raise RuntimeError(f"No RI offering found for {instance_type}/{term_secs}s")
                return _purchase_ri_sync(ec2, oid, 1, dry_run)

            loop = asyncio.get_running_loop()
            commitment_id = await loop.run_in_executor(None, _do_ri)

        else:
            return PurchaseRecord(
                id=rec_id, tenant_id=tenant_id, purchased_at=now_utc,
                cloud="aws", commitment_type=commitment_type, service=service,
                hourly_commitment_usd=hourly_usd, monthly_saving_eur=0.0,
                term_months=horizon_months, dry_run=dry_run,
                status="skipped",
                skip_reason=f"Commitment type '{commitment_type}' not purchasable via API",
                confidence_score=confidence,
            )

        discount = advisory.get("saving_pct", 0.27)
        monthly_saving = round(monthly_eur * discount, 2)
        status = "dry_run" if dry_run else "purchased"
        log.info(
            "commitment.purchased",
            tenant_id=tenant_id, service=service, type=commitment_type,
            hourly_usd=hourly_usd, dry_run=dry_run, id=commitment_id,
        )
        return PurchaseRecord(
            id=rec_id, tenant_id=tenant_id, purchased_at=now_utc,
            cloud="aws", commitment_type=commitment_type, service=service,
            hourly_commitment_usd=hourly_usd, monthly_saving_eur=monthly_saving,
            term_months=horizon_months, dry_run=dry_run,
            status=status, aws_commitment_id=commitment_id,
            confidence_score=confidence,
        )

    except Exception as exc:
        log.error(
            "commitment.purchase_failed",
            tenant_id=tenant_id, service=service, error=str(exc),
        )
        return PurchaseRecord(
            id=rec_id, tenant_id=tenant_id, purchased_at=now_utc,
            cloud="aws", commitment_type=commitment_type, service=service,
            hourly_commitment_usd=hourly_usd, monthly_saving_eur=0.0,
            term_months=horizon_months, dry_run=dry_run,
            status="failed", error=str(exc), confidence_score=confidence,
        )


def _infer_instance_type(service: str) -> str:
    """Heuristic: map a service name to a reasonable EC2 instance type for RI."""
    s = service.lower()
    if "rds" in s or "database" in s:
        return "db.m5.large"
    if "elasticache" in s or "cache" in s:
        return "cache.r6g.large"
    return "m5.large"   # broadest EC2 general-purpose default


# ── Common sizing helper ─────────────────────────────────────────────────────

def _sizing(advisory: dict, fx_rate_eur_to_usd: float, default_saving_pct: float):
    """Shared commitment-sizing math used by every cloud executor."""
    monthly_eur = float(advisory["on_demand_monthly_eur"])
    hourly_usd = round((monthly_eur / 30 / 24) * fx_rate_eur_to_usd, 5)
    saving_pct = float(advisory.get("saving_pct", default_saving_pct))
    monthly_saving = round(monthly_eur * saving_pct, 2)
    return monthly_eur, hourly_usd, monthly_saving


def _record(tenant_id, advisory, cloud, hourly_usd, monthly_saving, dry_run,
            status, commitment_id=None, error=None, skip_reason=None) -> PurchaseRecord:
    return PurchaseRecord(
        id=uuid.uuid4().hex, tenant_id=tenant_id,
        purchased_at=datetime.now(timezone.utc).isoformat(),
        cloud=cloud, commitment_type=advisory.get("recommended_type", ""),
        service=advisory.get("service", ""), hourly_commitment_usd=hourly_usd,
        monthly_saving_eur=(monthly_saving if status in ("purchased", "dry_run") else 0.0),
        term_months=int(advisory.get("commitment_horizon_months", 12)),
        dry_run=dry_run, status=status, aws_commitment_id=commitment_id,
        error=error, skip_reason=skip_reason,
        confidence_score=float(advisory.get("confidence_score", 0.0)),
    )


# ── Azure Compute Savings Plan purchase ──────────────────────────────────────

async def _execute_azure_purchase(
    advisory: dict, tenant_id: str, creds: dict, dry_run: bool,
    fx_rate_eur_to_usd: float = 1.08,
) -> PurchaseRecord:
    """Purchase an Azure Compute Savings Plan via azure-mgmt-billingbenefits."""
    commitment_type = advisory["recommended_type"]
    _, hourly_usd, monthly_saving = _sizing(advisory, fx_rate_eur_to_usd, 0.35)
    term = _AZURE_TERM.get(commitment_type)

    if term is None:
        return _record(tenant_id, advisory, "azure", hourly_usd, 0.0, dry_run,
                       "skipped", skip_reason=f"Azure commitment type '{commitment_type}' not purchasable")
    if dry_run:
        return _record(tenant_id, advisory, "azure", hourly_usd, monthly_saving, dry_run,
                       "dry_run", commitment_id=f"dry-run-azure-sp-{uuid.uuid4().hex[:8]}")

    billing_scope = creds.get("billing_scope") or creds.get("billing_scope_id")
    if not billing_scope:
        return _record(tenant_id, advisory, "azure", hourly_usd, 0.0, dry_run,
                       "failed", error="Azure billing_scope missing in credentials")

    def _do_azure() -> str:
        from azure.identity import ClientSecretCredential
        from azure.mgmt.billingbenefits import BillingBenefitsRP
        cred = ClientSecretCredential(
            tenant_id=creds["tenant_id"], client_id=creds["client_id"],
            client_secret=creds["client_secret"],
        )
        client = BillingBenefitsRP(cred)
        alias_name = f"cloudlens-{uuid.uuid4().hex[:12]}"
        body = {
            "sku": {"name": "Compute_Savings_Plan"},
            "properties": {
                "billingScopeId": billing_scope,
                "term": term,                 # "P1Y" | "P3Y"
                "billingPlan": "P1M",
                "appliedScopeType": "Shared",
                "commitment": {"grain": "Hourly", "currencyCode": "USD", "amount": hourly_usd},
            },
        }
        poller = client.savings_plan_order_alias.begin_create(
            savings_plan_order_alias_name=alias_name, body=body)
        result = poller.result()
        return (getattr(result, "savings_plan_order_id", None)
                or getattr(result, "id", None) or alias_name)

    try:
        loop = asyncio.get_running_loop()
        cid = await loop.run_in_executor(None, _do_azure)
        log.info("commitment.purchased", tenant_id=tenant_id, cloud="azure",
                 service=advisory.get("service"), type=commitment_type, id=cid)
        return _record(tenant_id, advisory, "azure", hourly_usd, monthly_saving, dry_run,
                       "purchased", commitment_id=cid)
    except Exception as exc:
        log.error("commitment.purchase_failed", tenant_id=tenant_id, cloud="azure",
                  service=advisory.get("service"), error=str(exc))
        return _record(tenant_id, advisory, "azure", hourly_usd, 0.0, dry_run,
                       "failed", error=str(exc))


# ── GCP Committed Use Discount purchase ──────────────────────────────────────

async def _execute_gcp_purchase(
    advisory: dict, tenant_id: str, creds: dict, dry_run: bool,
    fx_rate_eur_to_usd: float = 1.08,
) -> PurchaseRecord:
    """Purchase a GCP Committed Use Discount (CUD) via google-cloud-compute."""
    commitment_type = advisory["recommended_type"]
    monthly_eur, hourly_usd, monthly_saving = _sizing(advisory, fx_rate_eur_to_usd, 0.37)
    plan = _GCP_PLAN.get(commitment_type)

    if plan is None:
        return _record(tenant_id, advisory, "gcp", hourly_usd, 0.0, dry_run,
                       "skipped", skip_reason=f"GCP commitment type '{commitment_type}' not purchasable")
    if dry_run:
        return _record(tenant_id, advisory, "gcp", hourly_usd, monthly_saving, dry_run,
                       "dry_run", commitment_id=f"dry-run-gcp-cud-{uuid.uuid4().hex[:8]}")

    project = creds.get("project_id")
    region = creds.get("region")
    if not project or not region:
        return _record(tenant_id, advisory, "gcp", hourly_usd, 0.0, dry_run,
                       "failed", error="GCP project_id/region missing in credentials")

    def _do_gcp() -> str:
        from google.oauth2 import service_account
        from google.cloud import compute_v1
        sa = creds.get("service_account") or creds
        credentials = service_account.Credentials.from_service_account_info(sa)
        client = compute_v1.RegionCommitmentsClient(credentials=credentials)
        name = f"cloudlens-{uuid.uuid4().hex[:12]}"
        # Rough resource sizing from monthly spend (heuristic: ~€20/vCPU/mo,
        # 4 GB RAM per vCPU). CUDs commit to vCPU + memory amounts.
        vcpus = max(1, int(monthly_eur / 20))
        memory_mb = vcpus * 4 * 1024
        commitment = compute_v1.Commitment(
            name=name, plan=plan, type_="GENERAL_PURPOSE",
            resources=[
                compute_v1.ResourceCommitment(type_="VCPU", amount=vcpus),
                compute_v1.ResourceCommitment(type_="MEMORY", amount=memory_mb),
            ],
        )
        op = client.insert(project=project, region=region, commitment_resource=commitment)
        op.result()
        return name

    try:
        loop = asyncio.get_running_loop()
        cid = await loop.run_in_executor(None, _do_gcp)
        log.info("commitment.purchased", tenant_id=tenant_id, cloud="gcp",
                 service=advisory.get("service"), type=commitment_type, id=cid)
        return _record(tenant_id, advisory, "gcp", hourly_usd, monthly_saving, dry_run,
                       "purchased", commitment_id=cid)
    except Exception as exc:
        log.error("commitment.purchase_failed", tenant_id=tenant_id, cloud="gcp",
                  service=advisory.get("service"), error=str(exc))
        return _record(tenant_id, advisory, "gcp", hourly_usd, 0.0, dry_run,
                       "failed", error=str(exc))


# ── Cloud dispatcher ─────────────────────────────────────────────────────────

async def _execute_advisory_purchase(
    advisory: dict, tenant_id: str, cloud: str, creds: dict, dry_run: bool,
    fx_rate_eur_to_usd: float = 1.08,
) -> PurchaseRecord:
    """Route an advisory to the correct per-cloud purchase executor."""
    if cloud == "aws":
        return await _execute_aws_purchase(advisory, tenant_id, creds, dry_run, fx_rate_eur_to_usd)
    if cloud == "azure":
        return await _execute_azure_purchase(advisory, tenant_id, creds, dry_run, fx_rate_eur_to_usd)
    if cloud == "gcp":
        return await _execute_gcp_purchase(advisory, tenant_id, creds, dry_run, fx_rate_eur_to_usd)
    return _record(tenant_id, advisory, cloud, 0.0, 0.0, dry_run,
                   "skipped", skip_reason=f"Cloud '{cloud}' not supported")


# ── Main entry point ─────────────────────────────────────────────────────────

async def run_purchase(
    tenant_id: str,
    advisories: list[dict],
    fx_rate_eur_to_usd: float = 1.08,
) -> PurchaseRun:
    """
    Evaluate advisories and purchase commitments according to tenant settings.

    Raises CommitmentAutoDisabledError when either safety gate is closed.

    Parameters
    ----------
    tenant_id:
        The tenant to purchase for.
    advisories:
        List of advisory dicts as returned by the commitment_advisor service
        (keys: service, cloud, recommended_type, on_demand_monthly_eur,
         commitment_horizon_months, confidence_score, saving_pct, timing).
    fx_rate_eur_to_usd:
        Live EUR→USD FX rate for commitment sizing.
    """
    global_settings = get_settings()
    if not global_settings.commitment_auto_purchase_enabled:
        raise CommitmentAutoDisabledError(
            "Automated commitment purchasing is disabled globally "
            "(COMMITMENT_AUTO_PURCHASE_ENABLED is not set to true). "
            "Set it in the environment and ensure per-tenant 'enabled' is also true."
        )

    tenant_settings = await get_purchase_settings(tenant_id)
    if not tenant_settings.enabled:
        raise CommitmentAutoDisabledError(
            f"Automated commitment purchasing is disabled for tenant '{tenant_id}'. "
            "Set enabled=true via PUT /commitment-purchaser/{tenant_id}/settings."
        )

    # Load AWS credentials from Key Vault (same secret as ingest path)
    now_utc = datetime.now(timezone.utc).isoformat()
    dry_run = tenant_settings.dry_run
    run = PurchaseRun(tenant_id=tenant_id, run_at=now_utc, dry_run=dry_run)

    if dry_run:
        run.notes.append("DRY RUN — no mutating cloud API calls will be made.")

    # Per-cloud credentials are loaded lazily (only for clouds we actually need)
    # and cached. A missing/broken secret skips that cloud's advisories rather
    # than failing the whole run.
    _creds_cache: dict[str, Optional[dict]] = {}

    async def _creds_for(cloud: str) -> Optional[dict]:
        if cloud in _creds_cache:
            return _creds_cache[cloud]
        secret_name = _CLOUD_CREDS_SECRET.get(cloud, "").format(tenant_id=tenant_id)
        try:
            raw = await _keyvault.get_secret(secret_name)
            _creds_cache[cloud] = json.loads(raw)
        except Exception as exc:
            log.warning("commitment.creds_unavailable", cloud=cloud, error=str(exc))
            _creds_cache[cloud] = None
        return _creds_cache[cloud]

    # Check monthly budget already consumed
    month_spent = await _month_spend_eur(tenant_id)

    tenant_type_filter = set(tenant_settings.allowed_commitment_types)  # empty = all
    allowed_services = set(tenant_settings.allowed_services)            # empty = all

    for adv in advisories:
        cloud = adv.get("cloud", "").lower()
        if cloud not in _SUPPORTED_CLOUDS:
            _skip(run, adv, "cloud_unsupported",
                  f"Cloud '{cloud or 'unknown'}' is not supported for auto-purchase (aws, azure, gcp)")
            continue

        ctype = adv.get("recommended_type", "")
        if ctype not in _PURCHASABLE_BY_CLOUD[cloud]:
            _skip(run, adv, "type_not_purchasable",
                  f"Commitment type '{ctype}' is not auto-purchasable on {cloud}")
            continue

        if tenant_type_filter and ctype not in tenant_type_filter:
            _skip(run, adv, "type_not_allowed",
                  f"Commitment type '{ctype}' not in allowed_commitment_types")
            continue

        if allowed_services and adv.get("service") not in allowed_services:
            _skip(run, adv, "service_not_allowed",
                  f"Service '{adv.get('service')}' not in allowed_services")
            continue

        if adv.get("timing") != "commit_now":
            _skip(run, adv, "timing_wait",
                  f"Advisor recommends wait ({adv.get('wait_months', '?')} mo)")
            continue

        conf = float(adv.get("confidence_score", 0.0))
        if conf < tenant_settings.min_confidence_score:
            _skip(run, adv, "low_confidence",
                  f"Confidence {conf:.2f} < min {tenant_settings.min_confidence_score:.2f}")
            continue

        # Cost guard: estimate monthly saving
        monthly_eur = float(adv.get("on_demand_monthly_eur", 0.0))
        saving_pct = float(adv.get("saving_pct", 0.27))
        est_saving = monthly_eur * saving_pct

        if est_saving > tenant_settings.max_single_purchase_eur:
            _skip(run, adv, "exceeds_single_cap",
                  f"Estimated saving €{est_saving:.0f} > max_single_purchase_eur "
                  f"€{tenant_settings.max_single_purchase_eur:.0f}")
            continue

        if month_spent + est_saving > tenant_settings.max_monthly_budget_eur:
            _skip(run, adv, "exceeds_monthly_budget",
                  f"Monthly budget €{tenant_settings.max_monthly_budget_eur:.0f} would be exceeded")
            continue

        creds = await _creds_for(cloud)
        if creds is None:
            _skip(run, adv, "creds_unavailable",
                  f"{cloud} credentials not configured in Key Vault")
            continue

        rec = await _execute_advisory_purchase(
            adv, tenant_id, cloud, creds, dry_run, fx_rate_eur_to_usd
        )

        if rec.status in ("purchased", "dry_run"):
            run.purchased.append(rec)
            if not dry_run:
                month_spent += rec.monthly_saving_eur
        elif rec.status == "failed":
            run.failed.append(rec)
        else:
            run.skipped.append(rec)

        # Persist every record (including dry-run) for audit trail
        try:
            await cosmos.upsert_item(_CONTAINER_PURCHASES, rec.to_cosmos())
        except CosmosError as exc:
            log.warning("commitment.audit_write_failed", error=str(exc))

    run.total_committed_eur = round(
        sum(r.monthly_saving_eur for r in run.purchased), 2
    )
    run.notes.append(
        f"Processed {len(advisories)} advisories: "
        f"{len(run.purchased)} purchased, "
        f"{len(run.skipped)} skipped, "
        f"{len(run.failed)} failed."
    )
    log.info(
        "commitment.run_complete",
        tenant_id=tenant_id, dry_run=dry_run,
        purchased=len(run.purchased), skipped=len(run.skipped),
        failed=len(run.failed),
    )
    return run


def _skip(run: PurchaseRun, adv: dict, reason_code: str, reason: str) -> None:
    """Append a skipped record to the run."""
    run.skipped.append(PurchaseRecord(
        id=uuid.uuid4().hex,
        tenant_id=run.tenant_id,
        purchased_at=run.run_at,
        cloud=adv.get("cloud", ""),
        commitment_type=adv.get("recommended_type", ""),
        service=adv.get("service", ""),
        hourly_commitment_usd=0.0,
        monthly_saving_eur=0.0,
        term_months=adv.get("commitment_horizon_months", 12),
        dry_run=run.dry_run,
        status="skipped",
        skip_reason=f"[{reason_code}] {reason}",
        confidence_score=float(adv.get("confidence_score", 0.0)),
    ))


# ── Purchase history ─────────────────────────────────────────────────────────

async def get_purchase_history(
    tenant_id: str,
    limit: int = 50,
    status_filter: Optional[str] = None,
) -> list[dict]:
    """Return most-recent purchase records for a tenant, newest first."""
    query = "SELECT * FROM c WHERE c.tenant_id=@t"
    params: list[dict] = [{"name": "@t", "value": tenant_id}]
    if status_filter:
        query += " AND c.status=@s"
        params.append({"name": "@s", "value": status_filter})
    query += " ORDER BY c.purchased_at DESC OFFSET 0 LIMIT @lim"
    params.append({"name": "@lim", "value": limit})
    try:
        return await cosmos.query_items(
            _CONTAINER_PURCHASES, query, parameters=params, partition_key=tenant_id
        )
    except CosmosError:
        return []
