#!/usr/bin/env python3
"""
Generate the complete CloudLens technical documentation as a single
self-contained HTML site with live-rendered Mermaid diagrams. Built from the
real codebase (models, routers, OpenAPI spec) so it stays accurate.
"""
import json
import html as html_mod

OPENAPI = json.load(open("/home/claude/cloudlens/docs/openapi.json"))


def esc(s):
    return html_mod.escape(str(s))


# ──────────────────────────────────────────────────────────────────────────────
# API reference built from the live OpenAPI spec
# ──────────────────────────────────────────────────────────────────────────────
def build_api_reference():
    paths = OPENAPI["paths"]
    schemas = OPENAPI.get("components", {}).get("schemas", {})
    rows = []
    # group by tag
    groups = {}
    for path in sorted(paths):
        for method, op in paths[path].items():
            tag = (op.get("tags") or ["misc"])[0]
            groups.setdefault(tag, []).append((method.upper(), path, op))

    tag_order = ["tenants", "costs", "waste", "forecast", "insights",
                 "budgets", "reports", "ingest", "health"]
    ordered = [t for t in tag_order if t in groups] + \
              [t for t in groups if t not in tag_order]

    out = []
    for tag in ordered:
        out.append(f'<h3 class="api-group">{esc(tag)}</h3>')
        out.append('<table class="api-table"><thead><tr>'
                   '<th>Method</th><th>Path</th><th>Summary</th><th>Auth</th></tr></thead><tbody>')
        for method, path, op in groups[tag]:
            summary = op.get("summary") or op.get("description", "").split("\n")[0]
            # infer auth from path/tag
            if tag == "tenants" or "/ingest/" in path:
                auth = "API key"
            elif tag in ("health",):
                auth = "none"
            else:
                auth = "Bearer + rate-limit"
            mcls = method.lower()
            out.append(
                f'<tr><td><span class="method {mcls}">{method}</span></td>'
                f'<td><code>{esc(path)}</code></td>'
                f'<td>{esc(summary)}</td><td>{esc(auth)}</td></tr>')
        out.append('</tbody></table>')

    # request/response schemas detail for the key models
    out.append('<h3 class="api-group">Core schemas</h3>')
    key_schemas = ["TenantCreate", "TenantConfig", "WasteItem", "CostSummary",
                   "SpendForecastResponse", "TrajectoryResponse", "AnomalyResponse",
                   "ChargebackResponse", "InsightDigestResponse", "BudgetCreate", "BudgetStatus"]
    for name in key_schemas:
        sch = schemas.get(name)
        if not sch:
            continue
        out.append(f'<h4 class="schema-name"><code>{esc(name)}</code></h4>')
        props = sch.get("properties", {})
        required = set(sch.get("required", []))
        out.append('<table class="schema-table"><thead><tr><th>Field</th><th>Type</th>'
                   '<th>Req</th><th>Description</th></tr></thead><tbody>')
        for fname, fdef in props.items():
            typ = _schema_type(fdef)
            desc = fdef.get("description", "")
            req = "✓" if fname in required else ""
            out.append(f'<tr><td><code>{esc(fname)}</code></td><td class="typ">{esc(typ)}</td>'
                       f'<td class="req">{req}</td><td>{esc(desc)}</td></tr>')
        out.append('</tbody></table>')
    return "\n".join(out)


def _schema_type(fdef):
    if "$ref" in fdef:
        return fdef["$ref"].split("/")[-1]
    if "anyOf" in fdef:
        parts = [_schema_type(x) for x in fdef["anyOf"] if x.get("type") != "null"]
        return " | ".join(parts) + ("?" if any(x.get("type") == "null" for x in fdef["anyOf"]) else "")
    t = fdef.get("type", "object")
    if t == "array":
        return f"array<{_schema_type(fdef.get('items', {}))}>"
    if "enum" in fdef:
        return "enum(" + "|".join(map(str, fdef["enum"])) + ")"
    return t


# Page assembled in part 2 (build_docs.py imports these).
if __name__ == "__main__":
    print(build_api_reference()[:500])
