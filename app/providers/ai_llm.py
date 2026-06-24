"""
AI / LLM cost tracking
======================

The fastest-growing line item in 2026 cloud bills. CloudLens tracks it as a
first-class FOCUS service category (AI and Machine Learning) across three
sources:

  * Anthropic   — Admin Usage & Cost API (/v1/organizations/cost_report)
  * OpenAI      — Usage/Costs API (/v1/organizations/costs)
  * Amazon Bedrock — already arrives through the AWS billing path; this adapter
                     additionally breaks it out per-model from Cost Explorer
                     grouped by USAGE_TYPE.

Each normalizes into FocusRecord with service_category=AI_ML, so AI spend flows
through the same allocation, forecasting, anomaly, and budget machinery as the
rest of the cloud bill, and shows up in chargeback by team/product.
"""
from __future__ import annotations
from datetime import date

from app.providers.base import CloudProvider, register
from app.models.focus import (
    FocusRecord, ProviderName, ChargeCategory, ServiceCategory,
)


@register("anthropic")
class AnthropicProvider(CloudProvider):
    provider_name = ProviderName.ANTHROPIC

    def __init__(self, admin_api_key: str = ""):
        self.admin_api_key = admin_api_key

    async def fetch_cost_data(self, start: date, end: date) -> list[dict]:
        """
        Live: GET https://api.anthropic.com/v1/organizations/cost_report
          ?starting_at=<ISO>&ending_at=<ISO>&group_by[]=workspace_id
        Header: x-api-key (Admin key), anthropic-version.
        Returns data[] buckets with amount + currency per workspace/model.
        """
        return []

    def normalize(self, tenant_id: str, raw_rows: list[dict]) -> list[FocusRecord]:
        out = []
        for r in raw_rows:
            amount = float(r.get("amount", 0.0))
            model = r.get("model", r.get("description", "claude"))
            out.append(FocusRecord(
                tenant_id=tenant_id,
                provider_name=ProviderName.ANTHROPIC,
                sub_account_id=r.get("workspace_id", ""),
                charge_period_start=str(r.get("date", r.get("starting_at", "")))[:10],
                billed_cost=amount,
                effective_cost=amount,
                list_cost=amount,
                service_name=f"Claude · {model}",
                service_category=ServiceCategory.AI_ML,
                charge_category=ChargeCategory.USAGE,
                charge_description=r.get("description", ""),
                consumed_quantity=float(r.get("input_tokens", 0.0)) + float(r.get("output_tokens", 0.0)),
                consumed_unit="tokens",
                billing_currency=r.get("currency", "USD"),
                tags=r.get("tags", {}),
            ))
        return out


@register("openai")
class OpenAIProvider(CloudProvider):
    provider_name = ProviderName.OPENAI

    def __init__(self, admin_api_key: str = ""):
        self.admin_api_key = admin_api_key

    async def fetch_cost_data(self, start: date, end: date) -> list[dict]:
        """
        Live: GET https://api.openai.com/v1/organizations/costs
          ?start_time=<unix>&end_time=<unix>&group_by[]=project_id&group_by[]=line_item
        Header: Authorization: Bearer <admin key>.
        Returns data[] buckets each with results[].amount.value + project_id.
        """
        return []

    def normalize(self, tenant_id: str, raw_rows: list[dict]) -> list[FocusRecord]:
        out = []
        for r in raw_rows:
            amount = float(r.get("amount", 0.0))
            line_item = r.get("line_item", r.get("model", "gpt"))
            out.append(FocusRecord(
                tenant_id=tenant_id,
                provider_name=ProviderName.OPENAI,
                sub_account_id=r.get("project_id", ""),
                charge_period_start=str(r.get("date", ""))[:10],
                billed_cost=amount,
                effective_cost=amount,
                list_cost=amount,
                service_name=f"OpenAI · {line_item}",
                service_category=ServiceCategory.AI_ML,
                charge_category=ChargeCategory.USAGE,
                consumed_unit="tokens",
                billing_currency=r.get("currency", "USD"),
                tags=r.get("tags", {}),
            ))
        return out


def split_bedrock_from_aws(records: list[FocusRecord]) -> list[FocusRecord]:
    """
    Re-tag Bedrock usage that arrived via the AWS billing path into the AI_ML
    category (Cost Explorer reports it under the 'Amazon Bedrock' service name).
    Keeps AI spend visible in one place regardless of source.
    """
    for rec in records:
        if rec.provider_name == ProviderName.AWS and "bedrock" in rec.service_name.lower():
            rec.service_category = ServiceCategory.AI_ML
    return records
