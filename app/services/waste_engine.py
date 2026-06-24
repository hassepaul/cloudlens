"""
CloudLens Waste Detection Engine
12 configurable rules that run against cost_records + Advisor output.
Each rule is a pure async function returning list[WasteItem].
"""
from __future__ import annotations
import asyncio
from typing import Any, Optional

from app.logging_config import get_logger
from app.models.waste import WasteItem, WasteType, Priority

log = get_logger(__name__)

# ── Rule thresholds (override via env / config) ────────────────────────────
IDLE_VM_CPU_THRESHOLD_PCT = 5.0       # < 5% avg CPU over window
IDLE_VM_LOOKBACK_DAYS = 14
IDLE_APP_SERVICE_RPS_THRESHOLD = 1.0  # < 1 req/min
SNAPSHOT_AGE_DAYS = 90
RI_STABLE_DAYS = 30
COLD_STORAGE_TIERS = {"hot"}          # blobs in Hot tier that could move to Cool/Archive
CERT_EXPIRY_WARN_DAYS = 30            # warn when a KV cert expires within N days


# ── helpers ─────────────────────────────────────────────────────────────────

def _resource_name(resource_id: str) -> str:
    return resource_id.split("/")[-1] if resource_id else ""


def _resource_group(resource_id: str) -> str:
    parts = resource_id.lower().split("/")
    try:
        idx = parts.index("resourcegroups")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return ""


def _make_waste(
    tenant_id: str,
    subscription_id: str,
    resource_id: str,
    waste_type: WasteType,
    monthly_cost_eur: float,
    saving_eur: float,
    priority: Priority,
    recommendation: str,
    recommendation_it: str,
    evidence: dict,
    advisor_ref: Optional[str] = None,
) -> WasteItem:
    saving_pct = round((saving_eur / monthly_cost_eur * 100) if monthly_cost_eur > 0 else 0.0, 1)
    return WasteItem(
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        resource_id=resource_id,
        resource_name=_resource_name(resource_id),
        resource_group=_resource_group(resource_id),
        waste_type=waste_type,
        monthly_cost_eur=monthly_cost_eur,
        saving_eur=saving_eur,
        saving_pct=saving_pct,
        priority=priority,
        recommendation=recommendation,
        recommendation_it=recommendation_it,
        advisor_ref=advisor_ref,
        evidence=evidence,
    )


# ── Rule 1: Idle VMs ────────────────────────────────────────────────────────

async def rule_idle_vm(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    metrics_fetcher: Any,  # callable(resource_id) -> dict
) -> list[WasteItem]:
    """VMs with avg CPU < IDLE_VM_CPU_THRESHOLD_PCT over IDLE_VM_LOOKBACK_DAYS."""
    vm_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.compute/virtualmachines" in rid.lower():
            vm_costs[rid] = vm_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    waste_items: list[WasteItem] = []
    tasks = {rid: metrics_fetcher(rid) for rid in vm_costs}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for rid, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            log.warning("waste.idle_vm.metrics_error", resource_id=rid, error=str(result))
            continue
        cpu_avg = result.get("cpu_avg_pct")
        if cpu_avg is None or cpu_avg >= IDLE_VM_CPU_THRESHOLD_PCT:
            continue
        monthly = round(vm_costs[rid], 2)
        saving = round(monthly * 0.85, 2)  # deallocate saves ~85% (storage still runs)
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.IDLE_VM,
            monthly_cost_eur=monthly,
            saving_eur=saving,
            priority=Priority.CRITICAL if saving > 100 else Priority.HIGH,
            recommendation=(
                f"VM has averaged {cpu_avg:.1f}% CPU over {IDLE_VM_LOOKBACK_DAYS} days. "
                "Deallocate or resize to a burstable Bs-series SKU."
            ),
            recommendation_it=(
                f"La VM ha una media CPU del {cpu_avg:.1f}% negli ultimi {IDLE_VM_LOOKBACK_DAYS} giorni. "
                "Dealloca o ridimensiona a una SKU Bs-series burstable."
            ),
            evidence={"cpu_avg_pct": cpu_avg, "lookback_days": IDLE_VM_LOOKBACK_DAYS},
        ))
    return waste_items


# ── Rule 2: Unattached Managed Disks ───────────────────────────────────────

async def rule_unattached_disk(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    disk_states: dict[str, str],  # resource_id -> "Attached" | "Unattached"
) -> list[WasteItem]:
    """Managed disks in Unattached state."""
    waste_items: list[WasteItem] = []
    disk_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.compute/disks" in rid.lower():
            disk_costs[rid] = disk_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    for rid, state in disk_states.items():
        if state.lower() != "unattached":
            continue
        monthly = round(disk_costs.get(rid, 0.0), 2)
        saving = round(monthly * 0.80, 2)
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.UNATTACHED_DISK,
            monthly_cost_eur=monthly,
            saving_eur=saving,
            priority=Priority.CRITICAL,
            recommendation="Disk is unattached. Snapshot it for €0.02/GB/month, then delete.",
            recommendation_it="Il disco non è collegato. Crea uno snapshot (€0.02/GB/mese) poi eliminalo.",
            evidence={"disk_state": "Unattached"},
        ))
    return waste_items


# ── Rule 3: Orphan Public IPs ───────────────────────────────────────────────

async def rule_orphan_public_ip(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    ip_associations: dict[str, bool],  # resource_id -> is_associated
) -> list[WasteItem]:
    waste_items: list[WasteItem] = []
    ip_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.network/publicipaddresses" in rid.lower():
            ip_costs[rid] = ip_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    for rid, associated in ip_associations.items():
        if associated:
            continue
        monthly = round(ip_costs.get(rid, 0.0), 2)
        saving = round(monthly, 2)
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.ORPHAN_PUBLIC_IP,
            monthly_cost_eur=monthly,
            saving_eur=saving,
            priority=Priority.HIGH,
            recommendation="Public IP has no associated resource. Delete to eliminate cost.",
            recommendation_it="L'IP pubblico non ha risorse associate. Eliminalo per azzerare il costo.",
            evidence={"ip_associated": False},
        ))
    return waste_items


# ── Rule 4: Oversized VMs (from Advisor) ───────────────────────────────────

async def rule_oversized_vm(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    advisor_recommendations: list[dict],
) -> list[WasteItem]:
    waste_items: list[WasteItem] = []
    vm_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.compute/virtualmachines" in rid.lower():
            vm_costs[rid] = vm_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    for rec in advisor_recommendations:
        props = rec.get("properties", {})
        if props.get("impactedField", "").lower() != "microsoft.compute/virtualmachines":
            continue
        if "rightsize" not in rec.get("shortDescription", {}).get("solution", "").lower():
            continue
        rid = props.get("resourceMetadata", {}).get("resourceId", "").lower()
        if not rid:
            continue
        savings_info = props.get("extendedProperties", {})
        saving_eur = float(savings_info.get("savingsAmount", 0)) / 12
        monthly = round(vm_costs.get(rid, saving_eur / 0.4), 2)
        target_sku = savings_info.get("targetResourceId", "smaller SKU")
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.OVERSIZED_VM,
            monthly_cost_eur=monthly,
            saving_eur=round(saving_eur, 2),
            priority=Priority.HIGH,
            recommendation=f"Azure Advisor recommends resizing to {target_sku}.",
            recommendation_it=f"Azure Advisor raccomanda il ridimensionamento a {target_sku}.",
            advisor_ref=rec.get("id"),
            evidence={"advisor_recommendation": props.get("shortDescription", {})},
        ))
    return waste_items


# ── Rule 5: Dev/Test pricing eligible ──────────────────────────────────────

async def rule_dev_test_eligible(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    subscription_offer_type: str,  # e.g. "MS-AZR-0003P" = pay-as-you-go
    env_tag_value: str = "",        # value of 'environment' tag
) -> list[WasteItem]:
    """Non-prod subscriptions running under pay-as-you-go pricing."""
    NON_PROD_KEYWORDS = {"staging", "stag", "dev", "development", "qa", "test", "perf", "sandbox"}
    is_non_prod = any(kw in env_tag_value.lower() for kw in NON_PROD_KEYWORDS)
    is_payg = subscription_offer_type in {"MS-AZR-0003P", "MS-AZR-0017P", ""}

    if not (is_non_prod and is_payg):
        return []

    total_windows_cost = sum(
        r.get("cost_eur", 0.0) for r in cost_records
        if "windows" in r.get("meter_sub_category", "").lower()
        or "windows" in r.get("service_name", "").lower()
    )
    if total_windows_cost < 10:
        return []

    saving = round(total_windows_cost * 0.45, 2)
    return [_make_waste(
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        resource_id=f"/subscriptions/{subscription_id}",
        waste_type=WasteType.DEV_TEST_ELIGIBLE,
        monthly_cost_eur=round(total_windows_cost, 2),
        saving_eur=saving,
        priority=Priority.HIGH,
        recommendation=(
            "This non-production subscription is using pay-as-you-go pricing. "
            "Switch to Dev/Test offer to save up to 60% on Windows licensing."
        ),
        recommendation_it=(
            "Questa sottoscrizione non-prod usa prezzi pay-as-you-go. "
            "Passa all'offerta Dev/Test per risparmiare fino al 60% sulle licenze Windows."
        ),
        evidence={"subscription_offer": subscription_offer_type, "env_tag": env_tag_value},
    )]


# ── Rule 6: Reserved Instance candidates ───────────────────────────────────

async def rule_reserved_instance(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    vm_uptime_days: dict[str, int],  # resource_id -> consecutive days running
) -> list[WasteItem]:
    """VMs running consistently for > RI_STABLE_DAYS — good RI candidates."""
    waste_items: list[WasteItem] = []
    vm_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.compute/virtualmachines" in rid.lower():
            vm_costs[rid] = vm_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    for rid, days in vm_uptime_days.items():
        if days < RI_STABLE_DAYS:
            continue
        monthly = round(vm_costs.get(rid, 0.0), 2)
        if monthly < 30:
            continue
        saving = round(monthly * 0.40, 2)  # 1yr RI ~40% saving
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.RESERVED_INSTANCE,
            monthly_cost_eur=monthly,
            saving_eur=saving,
            priority=Priority.MEDIUM,
            recommendation=(
                f"VM has run for {days} consecutive days. "
                "Purchase a 1-year Reserved Instance for ~40% saving."
            ),
            recommendation_it=(
                f"La VM è attiva da {days} giorni consecutivi. "
                "Acquista una Reserved Instance annuale per ~40% di risparmio."
            ),
            evidence={"consecutive_days": days},
        ))
    return waste_items


# ── Rule 7: Idle App Services ───────────────────────────────────────────────

async def rule_idle_app_service(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    app_service_metrics: dict[str, float],  # resource_id -> avg req/min
) -> list[WasteItem]:
    waste_items: list[WasteItem] = []
    asp_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.web/serverfarms" in rid.lower():
            asp_costs[rid] = asp_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    for rid, rps in app_service_metrics.items():
        if rps >= IDLE_APP_SERVICE_RPS_THRESHOLD:
            continue
        monthly = round(asp_costs.get(rid, 0.0), 2)
        saving = round(monthly * 0.65, 2)
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.IDLE_APP_SERVICE,
            monthly_cost_eur=monthly,
            saving_eur=saving,
            priority=Priority.MEDIUM,
            recommendation=(
                f"App Service Plan averages {rps:.2f} req/min. "
                "Scale down or move to Consumption plan."
            ),
            recommendation_it=(
                f"L'App Service Plan ha una media di {rps:.2f} req/min. "
                "Scala verso il basso o migra al piano Consumption."
            ),
            evidence={"avg_requests_per_min": rps},
        ))
    return waste_items


# ── Rule 8: Unused Load Balancers ──────────────────────────────────────────

async def rule_unused_load_balancer(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    lb_backend_counts: dict[str, int],  # resource_id -> backend instance count
) -> list[WasteItem]:
    waste_items: list[WasteItem] = []
    lb_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.network/loadbalancers" in rid.lower():
            lb_costs[rid] = lb_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    for rid, count in lb_backend_counts.items():
        if count > 0:
            continue
        monthly = round(lb_costs.get(rid, 0.0), 2)
        saving = round(monthly, 2)
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.UNUSED_LOAD_BALANCER,
            monthly_cost_eur=monthly,
            saving_eur=saving,
            priority=Priority.MEDIUM,
            recommendation="Load Balancer backend pool is empty. Delete if no longer needed.",
            recommendation_it="Il pool backend del Load Balancer è vuoto. Elimina se non più necessario.",
            evidence={"backend_instance_count": 0},
        ))
    return waste_items


# ── Rule 9: Old Snapshots ──────────────────────────────────────────────────

async def rule_old_snapshots(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    snapshot_ages: dict[str, int],  # resource_id -> age in days
) -> list[WasteItem]:
    waste_items: list[WasteItem] = []
    snap_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.compute/snapshots" in rid.lower():
            snap_costs[rid] = snap_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    for rid, age in snapshot_ages.items():
        if age < SNAPSHOT_AGE_DAYS:
            continue
        monthly = round(snap_costs.get(rid, 0.0), 2)
        saving = round(monthly, 2)
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.OLD_SNAPSHOTS,
            monthly_cost_eur=monthly,
            saving_eur=saving,
            priority=Priority.LOW,
            recommendation=f"Snapshot is {age} days old. Review and delete if no longer needed.",
            recommendation_it=f"Lo snapshot ha {age} giorni. Verifica ed elimina se non più necessario.",
            evidence={"age_days": age},
        ))
    return waste_items


# ── Rule 10: Cold storage candidates ────────────────────────────────────────

async def rule_cold_storage(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    storage_access_tiers: dict[str, str],  # resource_id -> access tier ("Hot"|"Cool"|"Archive")
) -> list[WasteItem]:
    """Storage accounts on the Hot tier that look like cold-data candidates."""
    waste_items: list[WasteItem] = []
    storage_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.storage/storageaccounts" in rid.lower():
            storage_costs[rid] = storage_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    for rid, tier in storage_access_tiers.items():
        if (tier or "").lower() not in COLD_STORAGE_TIERS:
            continue
        monthly = round(storage_costs.get(rid, 0.0), 2)
        if monthly < 5:  # not worth flagging tiny accounts
            continue
        # Cool tier is ~45% cheaper per GB than Hot for infrequently-accessed data.
        saving = round(monthly * 0.45, 2)
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.COLD_STORAGE,
            monthly_cost_eur=monthly,
            saving_eur=saving,
            priority=Priority.LOW,
            recommendation=(
                "Storage account is on the Hot tier. If this data is accessed "
                "infrequently, move it to the Cool or Archive tier to cut storage cost."
            ),
            recommendation_it=(
                "Lo storage account è sul tier Hot. Se i dati sono consultati "
                "raramente, spostali sul tier Cool o Archive per ridurre i costi."
            ),
            evidence={"access_tier": tier},
        ))
    return waste_items


# ── Rule 11: Duplicated backups ─────────────────────────────────────────────

async def rule_duplicated_backup(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    backup_policy_counts: dict[str, int],  # resource_id -> number of backup policies protecting it
) -> list[WasteItem]:
    """Resources protected by more than one backup policy (redundant retention cost)."""
    waste_items: list[WasteItem] = []
    backup_costs: dict[str, float] = {}
    for r in cost_records:
        rid = r.get("resource_id", "")
        if "microsoft.recoveryservices" in rid.lower() or "backup" in r.get("service_name", "").lower():
            backup_costs[rid] = backup_costs.get(rid, 0.0) + r.get("cost_eur", 0.0)

    for rid, count in backup_policy_counts.items():
        if count <= 1:
            continue
        monthly = round(backup_costs.get(rid, 0.0), 2)
        # Each redundant policy roughly duplicates retention storage; estimate the
        # saving as the cost of the extra policies beyond the first.
        redundant_fraction = (count - 1) / count
        saving = round(monthly * redundant_fraction, 2)
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.DUPLICATED_BACKUP,
            monthly_cost_eur=monthly,
            saving_eur=saving,
            priority=Priority.LOW,
            recommendation=(
                f"Resource is protected by {count} backup policies. Consolidate to a "
                "single policy to avoid paying for duplicated retention."
            ),
            recommendation_it=(
                f"La risorsa è protetta da {count} policy di backup. Consolida in una "
                "sola policy per evitare di pagare retention duplicata."
            ),
            evidence={"backup_policy_count": count},
        ))
    return waste_items


# ── Rule 12: Expiring certificates ──────────────────────────────────────────

async def rule_expired_cert(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    cert_expiries: dict[str, int],  # resource_id -> days until expiry (may be negative)
) -> list[WasteItem]:
    """
    Key Vault certificates that have expired or expire soon.
    This is a risk finding rather than a cost saving — saving_eur is 0 and the
    item exists to surface operational risk in the same dashboard.
    """
    waste_items: list[WasteItem] = []
    for rid, days in cert_expiries.items():
        if days is None or days > CERT_EXPIRY_WARN_DAYS:
            continue
        if days < 0:
            en = f"Certificate expired {abs(days)} days ago. Renew immediately to avoid outages."
            it = f"Il certificato è scaduto {abs(days)} giorni fa. Rinnovalo subito per evitare disservizi."
            priority = Priority.HIGH
        else:
            en = f"Certificate expires in {days} days. Schedule renewal to avoid an outage."
            it = f"Il certificato scade tra {days} giorni. Pianifica il rinnovo per evitare disservizi."
            priority = Priority.MEDIUM
        waste_items.append(_make_waste(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            resource_id=rid,
            waste_type=WasteType.EXPIRED_CERT,
            monthly_cost_eur=0.0,
            saving_eur=0.0,
            priority=priority,
            recommendation=en,
            recommendation_it=it,
            evidence={"days_to_expiry": days},
        ))
    return waste_items


# ── Orchestrator ─────────────────────────────────────────────────────────────

async def run_all_rules(
    tenant_id: str,
    subscription_id: str,
    cost_records: list[dict],
    context: dict,
) -> list[WasteItem]:
    """
    Run all waste detection rules concurrently.
    context must provide:
        metrics_fetcher: async callable(resource_id) -> dict
        disk_states: dict[str, str]
        ip_associations: dict[str, bool]
        advisor_recommendations: list[dict]
        subscription_offer_type: str
        env_tag_value: str
        vm_uptime_days: dict[str, int]
        app_service_metrics: dict[str, float]
        lb_backend_counts: dict[str, int]
        snapshot_ages: dict[str, int]
        storage_access_tiers: dict[str, str]
        backup_policy_counts: dict[str, int]
        cert_expiries: dict[str, int]
    """
    log.info("waste_engine.start", tenant_id=tenant_id, subscription_id=subscription_id)
    tasks = [
        rule_idle_vm(tenant_id, subscription_id, cost_records, context["metrics_fetcher"]),
        rule_unattached_disk(tenant_id, subscription_id, cost_records, context.get("disk_states", {})),
        rule_orphan_public_ip(tenant_id, subscription_id, cost_records, context.get("ip_associations", {})),
        rule_oversized_vm(tenant_id, subscription_id, cost_records, context.get("advisor_recommendations", [])),
        rule_dev_test_eligible(tenant_id, subscription_id, cost_records,
                               context.get("subscription_offer_type", ""),
                               context.get("env_tag_value", "")),
        rule_reserved_instance(tenant_id, subscription_id, cost_records, context.get("vm_uptime_days", {})),
        rule_idle_app_service(tenant_id, subscription_id, cost_records, context.get("app_service_metrics", {})),
        rule_unused_load_balancer(tenant_id, subscription_id, cost_records, context.get("lb_backend_counts", {})),
        rule_old_snapshots(tenant_id, subscription_id, cost_records, context.get("snapshot_ages", {})),
        rule_cold_storage(tenant_id, subscription_id, cost_records, context.get("storage_access_tiers", {})),
        rule_duplicated_backup(tenant_id, subscription_id, cost_records, context.get("backup_policy_counts", {})),
        rule_expired_cert(tenant_id, subscription_id, cost_records, context.get("cert_expiries", {})),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_waste: list[WasteItem] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            log.error("waste_engine.rule_failed", rule_index=i, error=str(result))
        else:
            all_waste.extend(result)

    # Sort by saving descending
    all_waste.sort(key=lambda w: w.saving_eur, reverse=True)
    log.info("waste_engine.complete", tenant_id=tenant_id, items_found=len(all_waste))
    return all_waste
