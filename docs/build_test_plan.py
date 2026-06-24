#!/usr/bin/env python3
"""
Generate the CloudLens Test Plan & Test Case Catalog (HTML) by introspecting the
live pytest suite — so the document always reflects the real tests, not a stale
hand-written list. Each test function becomes a documented case with an ID,
the area it covers, and (from its name/docstring) what it verifies.
"""
import ast
import html as html_mod
import os

TESTS_DIR = "/home/claude/cloudlens/tests"

# map test files to functional areas + the requirement/risk they cover
AREA = {
    "test_cloudlens.py":        ("Core domain & waste engine", "Waste detection, models, cost analytics"),
    "test_cost_parser.py":      ("Cost ingestion", "Azure Cost Management response parsing (column-order safe)"),
    "test_forecast.py":         ("Forecasting", "Holt-Winters forecast, cost-of-inaction, roadmap, budget breach"),
    "test_insights.py":         ("Business intelligence", "Anomaly detection, chargeback, insight synthesis"),
    "test_budgets.py":          ("Budgets", "Budget CRUD + status with forecast projection"),
    "test_multicloud.py":       ("Multi-cloud", "FOCUS normalization, allocation, commitments, i18n"),
    "test_drilldown_alerts.py": ("Drill-down & alerts", "Resource drill-down, resource anomalies, alert engine"),
    "test_optimization.py":     ("Optimization", "Rightsizing (CPU+mem), scheduling, utilization, savings ledger"),
    "test_routers.py":          ("API surface", "Router wiring, response shapes, error handling"),
    "test_auth_ratelimit.py":   ("Auth & rate limiting", "API-key/bearer auth, per-tenant token bucket"),
    "test_security.py":         ("Security (SOC 2)", "AuthN/Z, tenant isolation, injection, secrets, audit integrity"),
    "test_admin_compliance.py": ("Compliance & admin", "Control matrix, audit log, evidence export"),
}


def esc(s):
    return html_mod.escape(str(s or ""))


def collect():
    """Return [{file, area, risk, cases:[{id, name, intent}]}] from the suite."""
    out = []
    for fname in sorted(os.listdir(TESTS_DIR)):
        if not fname.startswith("test_") or not fname.endswith(".py"):
            continue
        path = os.path.join(TESTS_DIR, fname)
        tree = ast.parse(open(path).read())
        cases = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                doc = ast.get_docstring(node) or ""
                intent = doc.strip().split("\n")[0] if doc else _humanize(node.name)
                cases.append({"id": _case_id(fname, node.name), "name": node.name, "intent": intent})
        area, risk = AREA.get(fname, ("Other", ""))
        out.append({"file": fname, "area": area, "risk": risk, "cases": cases})
    return out


def _case_id(fname, fn):
    # security tests already carry SEC-* ids in their names
    import re
    m = re.search(r"(SEC_[A-Z]+_\d+)", fn)
    if m:
        return m.group(1).replace("_", "-")
    stem = fname.replace("test_", "").replace(".py", "")[:4].upper()
    return f"TC-{stem}-{abs(hash(fn)) % 1000:03d}"


def _humanize(fn):
    return fn.replace("test_", "").replace("_", " ").capitalize()


DATA = collect()
total = sum(len(a["cases"]) for a in DATA)

rows = []
for a in DATA:
    rows.append(f'<tr class="area"><td colspan="3"><b>{esc(a["area"])}</b> '
                f'<span class="risk">{esc(a["risk"])}</span> '
                f'<span class="cnt">{len(a["cases"])} cases · {esc(a["file"])}</span></td></tr>')
    for c in a["cases"]:
        rows.append(f'<tr><td class="cid">{esc(c["id"])}</td>'
                    f'<td class="cname">{esc(c["name"])}</td>'
                    f'<td>{esc(c["intent"])}</td></tr>')

HTML = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>CloudLens — Test Plan & Case Catalog</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;450;500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
:root{{--bg:#0d1218;--bg2:#131b24;--panel:#18222e;--line:#263340;--lineSoft:#1e2a36;--txt:#e6edf3;
--txt2:#9fb0c0;--txt3:#647386;--teal:#2dd4bf;--blue:#4d9fff;--amber:#f5a524;
--mono:'Space Grotesk',sans-serif;--body:'Inter',sans-serif;--code:'JetBrains Mono',monospace;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--body);background:var(--bg);color:var(--txt);font-size:14px;padding:30px;line-height:1.55}}
.wrap{{max-width:980px;margin:0 auto}}
h1{{font-family:var(--mono);font-size:24px;font-weight:600;margin-bottom:4px}}
.sub{{color:var(--txt2);font-size:13px;margin-bottom:22px;padding-bottom:16px;border-bottom:1px solid var(--lineSoft)}}
.kpis{{display:flex;gap:12px;margin-bottom:22px;flex-wrap:wrap}}
.kpi{{background:var(--panel);border:1px solid var(--lineSoft);border-radius:10px;padding:12px 18px}}
.kpi .v{{font-family:var(--mono);font-size:22px;font-weight:600;color:var(--teal)}}
.kpi .l{{font-size:11px;color:var(--txt3);text-transform:uppercase;letter-spacing:.04em}}
.meta{{background:var(--bg2);border:1px solid var(--lineSoft);border-radius:10px;padding:14px 16px;margin-bottom:22px;font-size:12.5px;color:var(--txt2);line-height:1.6}}
.meta b{{color:var(--txt)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td{{padding:8px 10px;border-bottom:1px solid var(--lineSoft);vertical-align:top}}
tr.area td{{background:var(--bg2);padding-top:14px;font-family:var(--mono)}}
tr.area .risk{{font-size:11px;color:var(--txt3);font-weight:400;margin-left:8px}}
tr.area .cnt{{float:right;font-size:11px;color:var(--txt3);font-weight:400}}
.cid{{font-family:var(--code);font-size:11.5px;color:var(--blue);white-space:nowrap}}
.cname{{font-family:var(--code);font-size:11.5px;color:var(--txt2)}}
.foot{{margin-top:24px;font-size:11.5px;color:var(--txt3);border-top:1px solid var(--lineSoft);padding-top:14px}}
</style></head><body><div class="wrap">
<h1>CloudLens — Test Plan &amp; Case Catalog</h1>
<div class="sub">Generated from the live pytest suite · SOC 2-aligned · June 2026 · CONFIDENTIAL</div>
<div class="kpis">
  <div class="kpi"><div class="v">{total}</div><div class="l">test cases</div></div>
  <div class="kpi"><div class="v">{len(DATA)}</div><div class="l">suites</div></div>
  <div class="kpi"><div class="v">{sum(len(a['cases']) for a in DATA if 'Security' in a['area'] or 'Compliance' in a['area'])}</div><div class="l">security/compliance</div></div>
</div>
<div class="meta">
<b>Strategy.</b> Unit tests cover pure logic (parsers, engines, models); integration tests exercise routers
end-to-end with mocked Cosmos; security tests map to SOC 2 Common Criteria. <b>Environment.</b> pytest with
per-test rate-limiter reset (conftest.py); env vars provide config. <b>Pass bar.</b> 100% green + pyflakes clean
required before any deploy (CI gate). <b>Out of scope here:</b> live-cloud connector validation (needs real
credentials), Terraform apply (needs an Azure subscription), and load/soak testing (separate perf plan).
</div>
<table><thead><tr><td class="cid"><b>Case ID</b></td><td class="cname"><b>Test</b></td><td><b>Verifies</b></td></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table>
<div class="foot">Each case is a runnable pytest. Re-run the full suite with:
<span style="font-family:var(--code)">pytest tests/ -v</span>. This catalog regenerates from the suite via
<span style="font-family:var(--code)">docs/build_test_plan.py</span>, so it never drifts from the real tests.</div>
</div></body></html>"""

open("/mnt/user-data/outputs/CloudLens_Test_Plan.html", "w").write(HTML)
print(f"Test plan written: {total} cases across {len(DATA)} suites")
