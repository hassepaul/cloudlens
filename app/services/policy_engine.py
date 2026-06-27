"""
CloudLens Policy Enforcement Engine
====================================

Evaluates PolicyRules against live cost data for a tenant and executes
configured actions when conditions are met. Works across all clouds because
it queries the FOCUS-normalised cost_records container.

Condition evaluation overview
------------------------------
SPEND_THRESHOLD    — SUM cost over period vs. threshold (Cosmos query)
SPEND_ANOMALY      — Holt-Winters anomaly detection on recent daily series
RESOURCE_IDLE      — cost records with cpu_peak_pct < threshold
MISSING_TAG        — cost records whose tags dict lacks required_tag_key
UNBUDGETED_SPEND   — spend in services/accounts with no matching budget doc
RI_UTILIZATION_LOW — focus_records with commitment_discount_type set + low use
REGION_NOT_ALLOWED — distinct regions in cost records vs. allowed list
WASTE_THRESHOLD    — open waste items' total saving_eur vs. threshold

Action execution
----------------
SEND_ALERT        — write AlertEvent to Cosmos (in-app notification)
WEBHOOK           — HTTP POST with HMAC-SHA256 signature header
AUTOSTOP_RESOURCE — delegate to action_executor (only if action_execution_enabled)
TAG_RESOURCE      — ARM PATCH to apply enforcement tags (Azure only; others logged)

Cooldown
--------
Before executing, the engine checks last_triggered_at against cooldown_hours.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from app.config import get_settings
from app.logging_config import get_logger
from app.models.policy import (
    PolicyRule, PolicyViolation, PolicyCondition, PolicyAction,
    ConditionType, PolicyActionType,
)
from app.services import cosmos

log = get_logger(__name__)

ARM_TAGGING_API = "2021-04-01"


# ── Condition evaluators ──────────────────────────────────────────────────────

async def _eval_spend_threshold(
    tenant_id: str, cond: PolicyCondition, container: str
) -> tuple[bool, dict]:
    """Return (met, evidence)."""
    today = date.today()
    if cond.period == "daily":
        start = today
    elif cond.period == "weekly":
        start = today - timedelta(days=6)
    else:  # monthly
        start = today.replace(day=1)

    params = [
        {"name": "@tid", "value": tenant_id},
        {"name": "@start", "value": start.isoformat()},
        {"name": "@end",   "value": today.isoformat()},
    ]
    where = "c.tenant_id=@tid AND (c.record_date>=@start AND c.record_date<=@end OR c.charge_period_start>=@start AND c.charge_period_start<=@end)"

    if cond.cloud_filter:
        where += " AND c.provider_name=@cloud"
        params.append({"name": "@cloud", "value": cond.cloud_filter})
    if cond.service_filter:
        where += " AND CONTAINS(LOWER(c.service_name), @svc)"
        params.append({"name": "@svc", "value": cond.service_filter.lower()})

    rows = await cosmos.query_items(
        container,
        f"SELECT SUM(COALESCE(c.cost_eur, c.effective_cost, 0)) AS total FROM c WHERE {where}",
        params, partition_key=tenant_id,
    )
    total = float((rows[0].get("total") or 0.0) if rows else 0.0)
    met = total >= cond.threshold_eur
    return met, {"period": cond.period, "actual_eur": round(total, 2), "threshold_eur": cond.threshold_eur}


async def _eval_spend_anomaly(
    tenant_id: str, cond: PolicyCondition, container: str
) -> tuple[bool, dict]:
    rows = await cosmos.query_items(
        container,
        """SELECT c.record_date AS d, SUM(COALESCE(c.cost_eur, c.effective_cost, 0)) AS daily_cost
           FROM c WHERE c.tenant_id=@tid
           GROUP BY c.record_date ORDER BY c.record_date""",
        [{"name": "@tid", "value": tenant_id}], partition_key=tenant_id,
    )
    daily = [{"date": r["d"], "cost_eur": float(r["daily_cost"] or 0)} for r in rows if r.get("d")]
    if len(daily) < 10:
        return False, {"reason": "insufficient_history", "days": len(daily)}

    from app.services.anomaly import detect_anomalies
    result = detect_anomalies(daily)
    recent = [a for a in result.anomalies
              if a.z_score >= cond.min_z_score and a.excess_eur >= cond.min_excess_eur]
    met = len(recent) > 0
    evidence = {
        "anomalies_found": len(recent),
        "latest": {
            "day": recent[-1].day, "excess_eur": recent[-1].excess_eur,
            "z_score": recent[-1].z_score, "direction": recent[-1].direction,
        } if recent else None,
    }
    return met, evidence


async def _eval_resource_idle(
    tenant_id: str, cond: PolicyCondition, container: str
) -> tuple[bool, dict]:
    cutoff = (date.today() - timedelta(days=cond.lookback_days)).isoformat()
    params = [
        {"name": "@tid",   "value": tenant_id},
        {"name": "@cutoff","value": cutoff},
        {"name": "@pct",   "value": cond.cpu_threshold_pct},
    ]
    rows = await cosmos.query_items(
        container,
        """SELECT c.resource_id, c.resource_name, MAX(c.cpu_peak_pct) AS peak_cpu,
                  SUM(COALESCE(c.cost_eur, c.effective_cost, 0)) AS cost_eur,
                  MAX(COALESCE(c.provider_name, 'azure')) AS cloud
           FROM c
           WHERE c.tenant_id=@tid
             AND (c.record_date>=@cutoff OR c.charge_period_start>=@cutoff)
             AND IS_DEFINED(c.cpu_peak_pct) AND c.cpu_peak_pct < @pct
             AND IS_DEFINED(c.resource_id) AND c.resource_id != ''
           GROUP BY c.resource_id, c.resource_name""",
        params, partition_key=tenant_id,
    )
    idle = [
        {
            "resource_id": r.get("resource_id", ""),
            "resource_name": r.get("resource_name", ""),
            "peak_cpu_pct": float(r.get("peak_cpu") or 0),
            "cost_eur": round(float(r.get("cost_eur") or 0), 2),
            "cloud": r.get("cloud", "azure"),
        }
        for r in rows
    ]
    return len(idle) > 0, {"idle_resources": idle[:20], "total_idle": len(idle)}


async def _eval_missing_tag(
    tenant_id: str, cond: PolicyCondition, container: str
) -> tuple[bool, dict]:
    if not cond.required_tag_key:
        return False, {"error": "required_tag_key not set"}
    rows = await cosmos.query_items(
        container,
        """SELECT DISTINCT c.resource_id, c.tags,
                  MAX(COALESCE(c.provider_name, 'azure')) AS cloud
           FROM c
           WHERE c.tenant_id=@tid
             AND IS_DEFINED(c.resource_id) AND c.resource_id != ''
           GROUP BY c.resource_id, c.tags""",
        [{"name": "@tid", "value": tenant_id}], partition_key=tenant_id,
    )
    missing = []
    key = cond.required_tag_key.lower()
    for r in rows:
        tags = r.get("tags") or {}
        # normalise tag keys to lower-case for comparison
        normalised = {k.lower(): v for k, v in tags.items()}
        if key not in normalised:
            missing.append({
                "resource_id": r.get("resource_id", ""),
                "cloud": r.get("cloud", "azure"),
            })
    return len(missing) > 0, {
        "required_tag": cond.required_tag_key,
        "missing_count": len(missing),
        "sample": missing[:10],
    }


async def _eval_unbudgeted_spend(
    tenant_id: str, cond: PolicyCondition, container: str, budgets_container: str
) -> tuple[bool, dict]:
    today = date.today()
    start = today.replace(day=1) if cond.period == "monthly" else today - timedelta(days=6)
    budget_docs = await cosmos.query_items(
        budgets_container,
        "SELECT c.service_name, c.scope FROM c WHERE c.tenant_id=@tid AND c.type='budget'",
        [{"name": "@tid", "value": tenant_id}], partition_key=tenant_id,
    )
    budgeted_services = {d.get("service_name", "").lower() for d in budget_docs if d.get("service_name")}

    rows = await cosmos.query_items(
        container,
        """SELECT c.service_name, SUM(COALESCE(c.cost_eur, c.effective_cost, 0)) AS total
           FROM c WHERE c.tenant_id=@tid AND (c.record_date>=@start OR c.charge_period_start>=@start)
           GROUP BY c.service_name""",
        [{"name": "@tid", "value": tenant_id}, {"name": "@start", "value": start.isoformat()}],
        partition_key=tenant_id,
    )
    unbudgeted = [
        {"service": r.get("service_name", "Unknown"), "cost_eur": round(float(r.get("total") or 0), 2)}
        for r in rows
        if r.get("service_name", "").lower() not in budgeted_services
        and float(r.get("total") or 0) > 0
    ]
    total_unbudgeted = sum(u["cost_eur"] for u in unbudgeted)
    met = total_unbudgeted >= cond.min_unbudgeted_eur
    return met, {
        "total_unbudgeted_eur": round(total_unbudgeted, 2),
        "threshold_eur": cond.min_unbudgeted_eur,
        "unbudgeted_services": sorted(unbudgeted, key=lambda x: x["cost_eur"], reverse=True)[:10],
    }


async def _eval_ri_low(
    tenant_id: str, cond: PolicyCondition, container: str
) -> tuple[bool, dict]:
    # commitment_discount_type is set on FOCUS records; low coverage = on-demand dominant
    rows = await cosmos.query_items(
        container,
        """SELECT c.provider_name, c.service_name,
                  SUM(CASE WHEN c.commitment_discount_type != 'None' THEN c.effective_cost ELSE 0 END) AS committed,
                  SUM(c.effective_cost) AS total
           FROM c WHERE c.tenant_id=@tid AND c.type='focus_record'
           GROUP BY c.provider_name, c.service_name""",
        [{"name": "@tid", "value": tenant_id}], partition_key=tenant_id,
    )
    low_util = []
    for r in rows:
        total = float(r.get("total") or 0)
        committed = float(r.get("committed") or 0)
        if total > 0:
            util_pct = (committed / total) * 100
            if util_pct < cond.ri_threshold_pct and total > 100:  # only flag meaningful spend
                low_util.append({
                    "provider": r.get("provider_name", ""),
                    "service": r.get("service_name", ""),
                    "utilization_pct": round(util_pct, 1),
                    "on_demand_eur": round(total - committed, 2),
                })
    return len(low_util) > 0, {
        "threshold_pct": cond.ri_threshold_pct,
        "low_utilization_services": low_util[:10],
    }


async def _eval_region_blocked(
    tenant_id: str, cond: PolicyCondition, container: str
) -> tuple[bool, dict]:
    rows = await cosmos.query_items(
        container,
        """SELECT DISTINCT COALESCE(c.location, c.region_id, '') AS region,
                  MAX(COALESCE(c.provider_name, 'azure')) AS cloud
           FROM c WHERE c.tenant_id=@tid
             AND (IS_DEFINED(c.location) OR IS_DEFINED(c.region_id))
           GROUP BY COALESCE(c.location, c.region_id, '')""",
        [{"name": "@tid", "value": tenant_id}], partition_key=tenant_id,
    )
    allowed = {r.lower() for r in cond.allowed_regions}
    blocked = [
        {"region": r.get("region", ""), "cloud": r.get("cloud", "")}
        for r in rows
        if r.get("region") and r["region"].lower() not in allowed
    ]
    return len(blocked) > 0, {
        "allowed_regions": cond.allowed_regions,
        "blocked_regions": blocked[:20],
    }


async def _eval_waste_threshold(
    tenant_id: str, cond: PolicyCondition, waste_container: str
) -> tuple[bool, dict]:
    rows = await cosmos.query_items(
        waste_container,
        """SELECT SUM(c.saving_eur) AS total_saving, COUNT(1) AS count
           FROM c WHERE c.tenant_id=@tid AND c.type='waste_item'
             AND (NOT IS_DEFINED(c.resolved_at) OR c.resolved_at = null)""",
        [{"name": "@tid", "value": tenant_id}], partition_key=tenant_id,
    )
    total = float((rows[0].get("total_saving") or 0.0) if rows else 0.0)
    count = int((rows[0].get("count") or 0) if rows else 0)
    met = total >= cond.waste_threshold_eur
    return met, {
        "open_waste_eur": round(total, 2),
        "open_waste_items": count,
        "threshold_eur": cond.waste_threshold_eur,
    }


async def _evaluate_condition(
    tenant_id: str,
    cond: PolicyCondition,
    cost_container: str,
    waste_container: str,
    budgets_container: str,
) -> tuple[bool, dict]:
    """Dispatch to the right evaluator. Returns (condition_met, evidence)."""
    try:
        if cond.condition_type == ConditionType.SPEND_THRESHOLD:
            return await _eval_spend_threshold(tenant_id, cond, cost_container)
        if cond.condition_type == ConditionType.SPEND_ANOMALY:
            return await _eval_spend_anomaly(tenant_id, cond, cost_container)
        if cond.condition_type == ConditionType.RESOURCE_IDLE:
            return await _eval_resource_idle(tenant_id, cond, cost_container)
        if cond.condition_type == ConditionType.MISSING_TAG:
            return await _eval_missing_tag(tenant_id, cond, cost_container)
        if cond.condition_type == ConditionType.UNBUDGETED_SPEND:
            return await _eval_unbudgeted_spend(tenant_id, cond, cost_container, budgets_container)
        if cond.condition_type == ConditionType.RI_UTILIZATION_LOW:
            return await _eval_ri_low(tenant_id, cond, cost_container)
        if cond.condition_type == ConditionType.REGION_NOT_ALLOWED:
            return await _eval_region_blocked(tenant_id, cond, cost_container)
        if cond.condition_type == ConditionType.WASTE_THRESHOLD:
            return await _eval_waste_threshold(tenant_id, cond, waste_container)
        return False, {"error": f"unknown condition_type: {cond.condition_type}"}
    except Exception as exc:
        log.warning(
            "policy.condition_eval_error",
            tenant_id=tenant_id,
            condition_type=cond.condition_type,
            error=str(exc),
        )
        return False, {"error": str(exc)[:200]}


# ── Action executors ──────────────────────────────────────────────────────────

async def _action_send_alert(
    tenant_id: str,
    rule: PolicyRule,
    violation: PolicyViolation,
    action: PolicyAction,
    policy_container: str,
) -> None:
    """Write a PolicyViolation-sourced alert record to the alerts container."""
    msg = (action.message_template or
           f"Policy '{rule.name}' triggered: {', '.join(violation.conditions_met)}")
    for ph, val in [
        ("{{policy_name}}", rule.name),
        ("{{tenant_id}}", tenant_id),
        ("{{evidence}}", json.dumps(violation.evidence)[:300]),
    ]:
        msg = msg.replace(ph, val)
    alert_doc = {
        "id": str(__import__("uuid").uuid4()),
        "type": "alert_event",
        "tenant_id": tenant_id,
        "source": "policy_engine",
        "policy_id": rule.id,
        "policy_name": rule.name,
        "severity": action.severity,
        "message": msg,
        "evidence": violation.evidence,
        "triggered_at": violation.triggered_at.isoformat(),
    }
    await cosmos.upsert_item(policy_container, alert_doc)
    log.info("policy.alert_sent", tenant_id=tenant_id, policy=rule.name, severity=action.severity)


async def _action_webhook(
    tenant_id: str,
    rule: PolicyRule,
    violation: PolicyViolation,
    action: PolicyAction,
) -> None:
    """HTTP POST to the configured webhook URL with optional HMAC-SHA256 signing."""
    if not action.webhook_url:
        log.warning("policy.webhook_no_url", policy=rule.name)
        return

    payload = {
        "event": "policy_violation",
        "policy_id": rule.id,
        "policy_name": rule.name,
        "tenant_id": tenant_id,
        "severity": rule.severity,
        "triggered_at": violation.triggered_at.isoformat(),
        "conditions_met": violation.conditions_met,
        "evidence": violation.evidence,
        "resource_ids": violation.resource_ids[:20],
    }
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-CloudLens-Event": "policy_violation"}

    if action.webhook_secret:
        sig = hmac.new(action.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-CloudLens-Signature"] = f"sha256={sig}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
            resp = await client.post(action.webhook_url, content=body, headers=headers)
            log.info(
                "policy.webhook_sent",
                tenant_id=tenant_id,
                policy=rule.name,
                status=resp.status_code,
            )
    except Exception as exc:
        log.warning("policy.webhook_failed", policy=rule.name, error=str(exc))


async def _action_tag_resource(
    tenant_id: str,
    rule: PolicyRule,
    violation: PolicyViolation,
    action: PolicyAction,
    access_token: Optional[str],
) -> None:
    """Apply enforcement tags to offending resources via ARM (Azure) or log for others."""
    if not action.enforce_tags:
        return

    for resource_id in violation.resource_ids[:50]:
        cloud = action.tag_cloud or "azure"
        if cloud == "azure" and access_token:
            url = f"https://management.azure.com/{resource_id.lstrip('/')}?api-version={ARM_TAGGING_API}"
            body = json.dumps({"tags": action.enforce_tags}).encode()
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
                    resp = await client.patch(url, content=body, headers=headers)
                    if resp.status_code not in (200, 201):
                        log.warning("policy.tag_failed", resource=resource_id, status=resp.status_code)
                    else:
                        log.info("policy.tagged", resource=resource_id, tags=action.enforce_tags)
            except Exception as exc:
                log.warning("policy.tag_error", resource=resource_id, error=str(exc))
        else:
            # Non-Azure tagging logged as pending; implement per-provider as needed
            log.info(
                "policy.tag_pending",
                cloud=cloud,
                resource_id=resource_id,
                tags=action.enforce_tags,
                note="Non-Azure tag application requires provider-specific implementation",
            )


async def _action_autostop(
    tenant_id: str,
    rule: PolicyRule,
    violation: PolicyViolation,
    access_token: Optional[str],
    policy_container: str,
) -> None:
    """Create ActionRecords for idle/offending resources; execute if enabled."""
    settings = get_settings()
    from app.models.action import ActionRecord, ActionType, ActionStatus
    from app.services import action_executor

    for resource_id in violation.resource_ids[:50]:
        record = ActionRecord(
            tenant_id=tenant_id,
            action_type=ActionType.AUTOSTOP,
            resource_id=resource_id,
            initiated_by=f"policy:{rule.id}",
        )
        if settings.action_execution_enabled and access_token:
            try:
                record = await action_executor.execute_action(
                    record, access_token, container=policy_container
                )
            except Exception as exc:
                log.warning("policy.autostop_exec_failed", resource=resource_id, error=str(exc))
        else:
            await cosmos.upsert_item(policy_container, record.to_cosmos())
            log.info(
                "policy.autostop_queued",
                resource=resource_id,
                note="Set ACTION_EXECUTION_ENABLED=true to execute automatically",
            )


async def _execute_action(
    tenant_id: str,
    rule: PolicyRule,
    violation: PolicyViolation,
    action: PolicyAction,
    access_token: Optional[str],
    policy_container: str,
) -> str:
    """Dispatch a single action. Returns the action_type string."""
    if action.action_type == PolicyActionType.SEND_ALERT:
        await _action_send_alert(tenant_id, rule, violation, action, policy_container)
    elif action.action_type == PolicyActionType.WEBHOOK:
        await _action_webhook(tenant_id, rule, violation, action)
    elif action.action_type == PolicyActionType.TAG_RESOURCE:
        await _action_tag_resource(tenant_id, rule, violation, action, access_token)
    elif action.action_type == PolicyActionType.AUTOSTOP_RESOURCE:
        await _action_autostop(tenant_id, rule, violation, access_token, policy_container)
    return action.action_type.value


# ── Public interface ──────────────────────────────────────────────────────────

async def evaluate_tenant_policies(
    tenant_id: str,
    *,
    access_token: Optional[str] = None,
) -> list[PolicyViolation]:
    """
    Evaluate all enabled PolicyRules for a tenant. Called automatically at the
    end of each nightly ingest cycle and available on-demand via the API.

    Returns the list of new PolicyViolation records created during this run.
    """
    settings = get_settings()
    cost_container    = settings.cosmos_container_cost_records
    waste_container   = settings.cosmos_container_waste_items
    budgets_container = settings.cosmos_container_waste_items  # budgets co-located
    policy_container  = settings.cosmos_container_policies

    # Load enabled policy rules for this tenant
    try:
        docs = await cosmos.query_items(
            policy_container,
            "SELECT * FROM c WHERE c.tenant_id=@tid AND c.type='policy_rule' AND c.enabled=true",
            [{"name": "@tid", "value": tenant_id}], partition_key=tenant_id,
        )
    except Exception as exc:
        log.warning("policy.load_rules_failed", tenant_id=tenant_id, error=str(exc))
        return []

    rules = [PolicyRule.from_cosmos(d) for d in docs]
    now = datetime.now(timezone.utc)
    new_violations: list[PolicyViolation] = []

    for rule in rules:
        # Cooldown check
        if rule.last_triggered_at and rule.cooldown_hours > 0:
            elapsed_h = (now - rule.last_triggered_at).total_seconds() / 3600
            if elapsed_h < rule.cooldown_hours:
                log.debug(
                    "policy.cooldown_active",
                    tenant_id=tenant_id,
                    policy=rule.name,
                    remaining_h=round(rule.cooldown_hours - elapsed_h, 1),
                )
                continue

        # Evaluate all conditions
        results: list[tuple[bool, dict, str]] = []
        for cond in rule.conditions:
            met, evidence = await _evaluate_condition(
                tenant_id, cond, cost_container, waste_container, budgets_container
            )
            results.append((met, evidence, cond.condition_type.value))

        # AND/OR logic
        if rule.condition_logic == "AND":
            policy_triggered = all(r[0] for r in results)
        else:
            policy_triggered = any(r[0] for r in results)

        if not policy_triggered:
            continue

        # Build violation record
        conditions_met = [ct for met, _, ct in results if met]
        combined_evidence = {ct: ev for _, ev, ct in results}
        resource_ids = _extract_resource_ids(combined_evidence)

        violation = PolicyViolation(
            tenant_id=tenant_id,
            policy_id=rule.id,
            policy_name=rule.name,
            triggered_at=now,
            conditions_met=conditions_met,
            resource_ids=resource_ids,
            evidence=combined_evidence,
        )

        # Execute actions
        actions_taken: list[str] = []
        for action in rule.actions:
            try:
                taken = await _execute_action(
                    tenant_id, rule, violation, action, access_token, policy_container
                )
                actions_taken.append(taken)
            except Exception as exc:
                log.error(
                    "policy.action_failed",
                    tenant_id=tenant_id,
                    policy=rule.name,
                    action=action.action_type,
                    error=str(exc),
                )

        violation = violation.model_copy(update={"actions_taken": actions_taken})

        # Persist violation and update rule stats
        try:
            await cosmos.upsert_item(policy_container, violation.to_cosmos())
            updated_rule = rule.model_copy(update={
                "last_triggered_at": now,
                "trigger_count": rule.trigger_count + 1,
            })
            await cosmos.upsert_item(policy_container, updated_rule.to_cosmos())
        except Exception as exc:
            log.error("policy.persist_failed", tenant_id=tenant_id, policy=rule.name, error=str(exc))

        new_violations.append(violation)
        log.info(
            "policy.triggered",
            tenant_id=tenant_id,
            policy=rule.name,
            conditions=conditions_met,
            actions=actions_taken,
        )

    return new_violations


def _extract_resource_ids(evidence: dict) -> list[str]:
    """Pull resource_id lists from condition evidence dicts."""
    ids: set[str] = set()
    for ev in evidence.values():
        if isinstance(ev, dict):
            for key in ("idle_resources", "sample", "blocked_regions"):
                for item in ev.get(key, []):
                    if isinstance(item, dict) and item.get("resource_id"):
                        ids.add(item["resource_id"])
    return list(ids)[:100]
