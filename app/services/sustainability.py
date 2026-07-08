"""
Sustainability / CO₂ Emissions Service
=======================================

Estimates cloud carbon emissions (kgCO₂e) from FOCUS-normalised cost records
stored in Cosmos DB.

Methodology
-----------
Based on the open-source Cloud Carbon Footprint (CCF) approach:
  https://www.cloudcarbonfootprint.org/docs/methodology/

  kgCO₂e = effective_cost
            × kWh_per_USD[service_category]
            × PUE                               (data-centre overhead)
            × grid_intensity_gCO2_per_kWh[region] / 1000

Constants are derived from:
  - IEA 2023 grid emission factors (gCO₂e/kWh)
  - AWS/Azure/GCP renewable energy mix adjustments
  - CCF coefficient tables (kWh per $ of cloud spend)
  - Typical hyperscaler PUE of 1.10–1.20

All values are approximations (±30 %).  The primary value is trend detection
and regional hotspot identification — not CSRD-grade accounting.  Customers
needing certification-grade data should integrate the provider's native carbon
tools (AWS Customer Carbon Footprint Tool, Azure Emissions Insights, GCP Carbon
Footprint) as an additional data source; those APIs can be layered on top of
this service without breaking the public contract.

API summary
-----------
  GET /api/v1/sustainability/{tid}/summary     — headline totals + by-cloud
  GET /api/v1/sustainability/{tid}/by-region   — top emitting regions
  GET /api/v1/sustainability/{tid}/by-service  — emissions by cloud service
  GET /api/v1/sustainability/{tid}/trend       — daily kgCO₂e timeseries
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from app.config import get_settings
from app.exceptions import CosmosError
from app.logging_config import get_logger
from app.services import cosmos

log = get_logger(__name__)


# ── Carbon intensity lookup (gCO₂e / kWh) ────────────────────────────────────
# Sources: IEA 2023, EPA eGRID 2023, EMBER 2023, provider sustainability reports.
# Region IDs match the provider's native region_id strings from FOCUS records.

_GRID_INTENSITY: dict[str, float] = {
    # ── Azure ──────────────────────────────────────────────────────────────
    "eastus":                 350.1,   # Virginia (US)
    "eastus2":                350.1,
    "westus":                 270.5,   # California
    "westus2":                166.3,   # Washington State (hydro-heavy)
    "westus3":                305.0,   # Arizona
    "centralus":              519.6,   # Iowa (coal belt)
    "northcentralus":         408.0,   # Illinois
    "southcentralus":         371.0,   # Texas
    "westcentralus":          408.0,
    "northeurope":            276.5,   # Ireland
    "westeurope":             240.0,   # Netherlands
    "uksouth":                191.0,   # UK South
    "ukwest":                 191.0,
    "francecentral":           58.3,   # France (nuclear-heavy)
    "francesouth":             58.3,
    "germanywestcentral":     385.0,   # Germany
    "germanynorth":           385.0,
    "swedencentral":           13.0,   # Sweden (near-zero)
    "switzerlandnorth":        29.0,   # Switzerland
    "norwayeast":               8.0,   # Norway (hydro)
    "norwaywest":               8.0,
    "polandcentral":          752.0,   # Poland (coal)
    "spaincentral":           200.0,
    "italynorth":             233.0,
    "australiaeast":          690.0,   # NSW Australia
    "australiasoutheast":     977.0,   # Victoria (brown coal)
    "brazilsouth":            116.0,   # Brazil
    "canadacentral":           40.0,   # Canada (mostly hydro)
    "canadaeast":              40.0,
    "japaneast":              453.0,   # Japan
    "japanwest":              453.0,
    "koreacentral":           430.0,   # South Korea
    "koreasouth":             430.0,
    "southeastasia":          408.0,   # Singapore
    "eastasia":               700.0,   # Hong Kong
    "centralindia":           713.0,   # India (coal)
    "southindia":             713.0,
    "westindia":              713.0,
    "uaenorth":               420.0,   # UAE
    "uaecentral":             420.0,
    "southafricanorth":       840.0,   # South Africa (coal)
    "qatarcentral":           503.0,
    "israelcentral":          419.0,
    "mexicocentral":          410.0,
    "newzealandnorth":         77.0,

    # ── AWS ────────────────────────────────────────────────────────────────
    "us-east-1":              350.1,   # N. Virginia
    "us-east-2":              408.0,   # Ohio
    "us-west-1":              252.0,   # N. California
    "us-west-2":              166.3,   # Oregon
    "ca-central-1":            40.0,   # Canada
    "ca-west-1":               40.0,
    "eu-west-1":              276.5,   # Ireland
    "eu-west-2":              191.0,   # London
    "eu-west-3":               58.3,   # Paris
    "eu-central-1":           385.0,   # Frankfurt
    "eu-central-2":           155.0,   # Zurich
    "eu-north-1":              13.0,   # Stockholm
    "eu-south-1":             233.0,   # Milan
    "eu-south-2":             200.0,   # Spain
    "ap-southeast-1":         408.0,   # Singapore
    "ap-southeast-2":         690.0,   # Sydney
    "ap-southeast-3":         724.0,   # Jakarta
    "ap-southeast-4":         690.0,   # Melbourne
    "ap-northeast-1":         453.0,   # Tokyo
    "ap-northeast-2":         430.0,   # Seoul
    "ap-northeast-3":         453.0,   # Osaka
    "ap-south-1":             713.0,   # Mumbai
    "ap-south-2":             713.0,   # Hyderabad
    "ap-east-1":              700.0,   # Hong Kong
    "sa-east-1":              116.0,   # São Paulo
    "me-south-1":             503.0,   # Bahrain
    "me-central-1":           420.0,   # UAE
    "af-south-1":             840.0,   # Cape Town
    "il-central-1":           419.0,   # Israel
    "ap-southeast-5":         724.0,   # Malaysia
    "mx-central-1":           410.0,   # Mexico

    # ── GCP ────────────────────────────────────────────────────────────────
    "us-central1":            519.6,   # Iowa
    "us-east1":               350.1,   # South Carolina
    "us-east4":               350.1,   # Northern Virginia
    "us-east5":               350.1,
    "us-south1":              371.0,   # Dallas
    "us-west1":               166.3,   # Oregon
    "us-west2":               252.0,   # Los Angeles
    "us-west3":               305.0,   # Salt Lake City
    "us-west4":               266.0,   # Las Vegas
    "northamerica-northeast1": 40.0,   # Montréal
    "northamerica-northeast2": 40.0,   # Toronto
    "southamerica-east1":     116.0,   # São Paulo
    "southamerica-west1":     116.0,   # Chile
    "europe-west1":           167.0,   # Belgium
    "europe-west2":           191.0,   # London
    "europe-west3":           385.0,   # Frankfurt
    "europe-west4":           240.0,   # Netherlands
    "europe-west6":            29.0,   # Zurich
    "europe-west8":           233.0,   # Milan
    "europe-west9":            58.3,   # Paris
    "europe-west10":          385.0,   # Berlin
    "europe-west12":          233.0,   # Turin
    "europe-central2":        752.0,   # Warsaw
    "europe-north1":           13.0,   # Finland
    "europe-southwest1":      200.0,   # Madrid
    "asia-south1":            713.0,   # Mumbai
    "asia-south2":            713.0,   # Delhi
    "asia-east1":             541.0,   # Taiwan
    "asia-east2":             700.0,   # Hong Kong
    "asia-northeast1":        453.0,   # Tokyo
    "asia-northeast2":        453.0,   # Osaka
    "asia-northeast3":        430.0,   # Seoul
    "asia-southeast1":        408.0,   # Singapore
    "asia-southeast2":        724.0,   # Jakarta
    "australia-southeast1":   690.0,   # Sydney
    "australia-southeast2":   977.0,   # Melbourne
    "me-west1":               420.0,   # Tel Aviv
    "me-central1":            420.0,   # UAE
    "me-central2":            503.0,   # Dammam
    "africa-south1":          840.0,   # Johannesburg
}

_DEFAULT_INTENSITY: float = 420.0   # world average grid mix


# ── kWh per USD by FOCUS ServiceCategory ─────────────────────────────────────
# Derived from CCF (v0.14) coefficients normalised to USD spend.
# Compute is energy-intensive; storage moderate; networking low.

_KWH_PER_USD: dict[str, float] = {
    "Compute":                    3.5,
    "Storage":                    1.2,
    "Databases":                  2.8,
    "Networking":                 0.8,
    "AI and Machine Learning":    5.0,
    "Analytics":                  2.5,
    "Security":                   0.5,
    "Management and Governance":  0.4,
    "Other":                      1.8,
}

_DEFAULT_KWH_PER_USD: float = 1.8

# Average hyperscaler PUE (AWS ~1.12, Azure ~1.18, GCP ~1.10)
_PUE: float = 1.13


# ── Helpers ───────────────────────────────────────────────────────────────────

def _intensity(region: str) -> float:
    """Return gCO₂e/kWh for a region, falling back to world average."""
    return _GRID_INTENSITY.get(region.lower(), _DEFAULT_INTENSITY)


def _kwh_per_usd(service_category: str) -> float:
    return _KWH_PER_USD.get(service_category, _DEFAULT_KWH_PER_USD)


def _kg_co2(effective_cost: float, service_category: str, region: str) -> float:
    """Return kgCO₂e for a single cost record."""
    return effective_cost * _kwh_per_usd(service_category) * _PUE * _intensity(region) / 1000.0


def _cost_container() -> str:
    return get_settings().cosmos_container_cost_records


def _date_range(lookback_days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    return start.isoformat(), end.isoformat()


# ── Public API ────────────────────────────────────────────────────────────────

async def get_emissions_summary(
    tenant_id: str,
    lookback_days: int = 30,
) -> dict:
    """
    Return headline CO₂ totals + per-cloud breakdown.

    Response shape
    --------------
    {
      "tenant_id": "t-acme",
      "period_days": 30,
      "start_date": "2025-06-01",
      "end_date":   "2025-07-01",
      "total_kg_co2e": 12340.5,
      "total_tonnes_co2e": 12.34,
      "methodology_note": "...",
      "by_cloud": [
        {"provider": "Microsoft Azure", "kg_co2e": 9000.0, "pct": 73.0},
        ...
      ],
      "top_region": {"region_id": "centralindia", "kg_co2e": 5200.0},
      "top_service": {"service_name": "Virtual Machines", "kg_co2e": 6100.0},
    }
    """
    start, end = _date_range(lookback_days)
    container = _cost_container()
    query = (
        "SELECT c.provider_name, c.region_id, c.service_category, "
        "c.service_name, c.effective_cost "
        "FROM c "
        "WHERE c.tenant_id = @tid "
        "AND c.charge_period_start >= @start "
        "AND c.charge_period_start < @end "
        "AND c.charge_category = 'Usage' "
        "AND c.effective_cost > 0"
    )
    try:
        rows = await cosmos.query_items(
            container, query,
            parameters=[
                {"name": "@tid",   "value": tenant_id},
                {"name": "@start", "value": start},
                {"name": "@end",   "value": end},
            ],
            partition_key=tenant_id,
        )
    except CosmosError:
        log.warning("sustainability.query_failed", tenant_id=tenant_id)
        rows = []

    total = 0.0
    by_cloud: dict[str, float] = {}
    by_region: dict[str, float] = {}
    by_service: dict[str, float] = {}

    for r in rows:
        kg = _kg_co2(
            r.get("effective_cost", 0.0),
            r.get("service_category", "Other"),
            r.get("region_id", ""),
        )
        total += kg
        provider = r.get("provider_name", "Unknown")
        by_cloud[provider] = by_cloud.get(provider, 0.0) + kg
        region = r.get("region_id", "unknown") or "unknown"
        by_region[region] = by_region.get(region, 0.0) + kg
        svc = r.get("service_name", "Unknown") or "Unknown"
        by_service[svc] = by_service.get(svc, 0.0) + kg

    clouds_list = sorted(
        [
            {
                "provider": p,
                "kg_co2e": round(v, 2),
                "pct": round(v / total * 100, 1) if total > 0 else 0.0,
            }
            for p, v in by_cloud.items()
        ],
        key=lambda x: x["kg_co2e"],
        reverse=True,
    )

    top_region = (
        max(by_region.items(), key=lambda x: x[1])
        if by_region else ("—", 0.0)
    )
    top_service = (
        max(by_service.items(), key=lambda x: x[1])
        if by_service else ("—", 0.0)
    )

    return {
        "tenant_id": tenant_id,
        "period_days": lookback_days,
        "start_date": start,
        "end_date": end,
        "total_kg_co2e": round(total, 2),
        "total_tonnes_co2e": round(total / 1000, 3),
        "methodology_note": (
            "Estimates based on Cloud Carbon Footprint methodology: "
            "effective_cost × kWh/USD[service_category] × PUE(1.13) × "
            "grid_gCO₂e/kWh[region] / 1000.  Accuracy ±30 %."
        ),
        "by_cloud": clouds_list,
        "top_region": {
            "region_id": top_region[0],
            "kg_co2e": round(top_region[1], 2),
        },
        "top_service": {
            "service_name": top_service[0],
            "kg_co2e": round(top_service[1], 2),
        },
    }


async def get_emissions_by_region(
    tenant_id: str,
    lookback_days: int = 30,
    top_n: int = 15,
) -> list[dict]:
    """
    Return top-N emitting regions with intensity metadata.

    Each entry:
      {
        "region_id":          "centralindia",
        "kg_co2e":            5200.0,
        "grid_intensity":     713.0,    # gCO₂e/kWh
        "spend_usd":          480.0,
        "pct":                42.1,
      }
    """
    start, end = _date_range(lookback_days)
    container = _cost_container()
    query = (
        "SELECT c.region_id, c.service_category, "
        "c.effective_cost "
        "FROM c "
        "WHERE c.tenant_id = @tid "
        "AND c.charge_period_start >= @start "
        "AND c.charge_period_start < @end "
        "AND c.charge_category = 'Usage' "
        "AND c.effective_cost > 0"
    )
    try:
        rows = await cosmos.query_items(
            container, query,
            parameters=[
                {"name": "@tid",   "value": tenant_id},
                {"name": "@start", "value": start},
                {"name": "@end",   "value": end},
            ],
            partition_key=tenant_id,
        )
    except CosmosError:
        rows = []

    # Accumulate per region
    agg: dict[str, dict] = {}
    for r in rows:
        region = r.get("region_id", "unknown") or "unknown"
        cost = r.get("effective_cost", 0.0)
        kg = _kg_co2(cost, r.get("service_category", "Other"), region)
        if region not in agg:
            agg[region] = {"kg_co2e": 0.0, "spend": 0.0}
        agg[region]["kg_co2e"] += kg
        agg[region]["spend"] += cost

    total = sum(v["kg_co2e"] for v in agg.values()) or 1.0
    results = sorted(
        [
            {
                "region_id": region,
                "kg_co2e": round(v["kg_co2e"], 2),
                "grid_intensity": _intensity(region),
                "spend_eur": round(v["spend"], 2),
                "pct": round(v["kg_co2e"] / total * 100, 1),
            }
            for region, v in agg.items()
        ],
        key=lambda x: x["kg_co2e"],
        reverse=True,
    )
    return results[:top_n]


async def get_emissions_by_service(
    tenant_id: str,
    lookback_days: int = 30,
    top_n: int = 15,
) -> list[dict]:
    """
    Return top-N emitting cloud services.

    Each entry:
      {
        "service_name":   "Virtual Machines",
        "service_category": "Compute",
        "kg_co2e":        6100.0,
        "spend_eur":      1800.0,
        "pct":            49.5,
        "kg_co2e_per_eur": 3.39,
      }
    """
    start, end = _date_range(lookback_days)
    container = _cost_container()
    query = (
        "SELECT c.service_name, c.service_category, c.region_id, "
        "c.effective_cost "
        "FROM c "
        "WHERE c.tenant_id = @tid "
        "AND c.charge_period_start >= @start "
        "AND c.charge_period_start < @end "
        "AND c.charge_category = 'Usage' "
        "AND c.effective_cost > 0"
    )
    try:
        rows = await cosmos.query_items(
            container, query,
            parameters=[
                {"name": "@tid",   "value": tenant_id},
                {"name": "@start", "value": start},
                {"name": "@end",   "value": end},
            ],
            partition_key=tenant_id,
        )
    except CosmosError:
        rows = []

    agg: dict[str, dict] = {}
    for r in rows:
        svc = r.get("service_name") or "Unknown"
        cat = r.get("service_category", "Other")
        cost = r.get("effective_cost", 0.0)
        kg = _kg_co2(cost, cat, r.get("region_id", ""))
        if svc not in agg:
            agg[svc] = {"kg_co2e": 0.0, "spend": 0.0, "category": cat}
        agg[svc]["kg_co2e"] += kg
        agg[svc]["spend"] += cost

    total = sum(v["kg_co2e"] for v in agg.values()) or 1.0
    results = sorted(
        [
            {
                "service_name": svc,
                "service_category": v["category"],
                "kg_co2e": round(v["kg_co2e"], 2),
                "spend_eur": round(v["spend"], 2),
                "pct": round(v["kg_co2e"] / total * 100, 1),
                "kg_co2e_per_eur": (
                    round(v["kg_co2e"] / v["spend"], 3) if v["spend"] > 0 else 0.0
                ),
            }
            for svc, v in agg.items()
        ],
        key=lambda x: x["kg_co2e"],
        reverse=True,
    )
    return results[:top_n]


async def get_emissions_trend(
    tenant_id: str,
    days: int = 30,
) -> list[dict]:
    """
    Return daily kgCO₂e for the last `days` days.

    Each entry: {"date": "2025-06-15", "kg_co2e": 412.5, "spend_eur": 180.0}
    """
    start, end = _date_range(days)
    container = _cost_container()
    query = (
        "SELECT c.charge_period_start, c.service_category, "
        "c.region_id, c.effective_cost "
        "FROM c "
        "WHERE c.tenant_id = @tid "
        "AND c.charge_period_start >= @start "
        "AND c.charge_period_start < @end "
        "AND c.charge_category = 'Usage' "
        "AND c.effective_cost > 0"
    )
    try:
        rows = await cosmos.query_items(
            container, query,
            parameters=[
                {"name": "@tid",   "value": tenant_id},
                {"name": "@start", "value": start},
                {"name": "@end",   "value": end},
            ],
            partition_key=tenant_id,
        )
    except CosmosError:
        rows = []

    daily: dict[str, dict] = {}
    for r in rows:
        day = str(r.get("charge_period_start", ""))[:10]
        if not day:
            continue
        cost = r.get("effective_cost", 0.0)
        kg = _kg_co2(cost, r.get("service_category", "Other"), r.get("region_id", ""))
        if day not in daily:
            daily[day] = {"kg_co2e": 0.0, "spend": 0.0}
        daily[day]["kg_co2e"] += kg
        daily[day]["spend"] += cost

    return [
        {
            "date": d,
            "kg_co2e": round(v["kg_co2e"], 2),
            "spend_eur": round(v["spend"], 2),
        }
        for d, v in sorted(daily.items())
    ]
