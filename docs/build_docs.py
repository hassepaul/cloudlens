#!/usr/bin/env python3
"""Assemble the full CloudLens documentation site into one HTML file."""
import sys
sys.path.insert(0, "/home/claude/cloudlens/docs")
from _docgen_diagrams import DIAGRAMS
from _docgen_api import build_api_reference

API_REF = build_api_reference()


def mermaid(key):
    return f'<div class="mermaid">\n{DIAGRAMS[key]}\n</div>'


# ── Written content ───────────────────────────────────────────────────────────

FUNCTIONAL_SPEC = """
<h2 id="func">1. Functional Specification</h2>
<p class="lead">CloudLens is a multi-tenant, read-only multi-cloud FinOps managed service. It ingests
cost and resource data from customer subscriptions across Microsoft Azure, AWS, Google Cloud,
Alibaba Cloud, and Oracle Cloud Infrastructure — plus AI/LLM spend (Amazon Bedrock, OpenAI,
Anthropic) — normalizes everything onto the FOCUS billing standard, detects waste, forecasts
spend, detects anomalies, allocates 100% of cost to business units, manages commitments, and
synthesises everything into ranked, multilingual business insights — delivered through a REST API
and a drill-down console available in eight EU languages.</p>

<h3>1.1 Goals and non-goals</h3>
<table class="data">
<thead><tr><th>Goal</th><th>Non-goal</th></tr></thead>
<tbody>
<tr><td>Surface recoverable spend with concrete, quantified actions</td><td>Make any change to customer resources (read-only by architecture)</td></tr>
<tr><td>Forecast spend and the cost of <em>not</em> acting on waste</td><td>Replace Azure-native billing as the system of record</td></tr>
<tr><td>Allocate cost to teams/cost-centres (chargeback/showback)</td><td>Provision or manage customer infrastructure</td></tr>
<tr><td>Run on the cheapest viable infrastructure (scale-to-zero)</td><td>Real-time (sub-hourly) cost streaming</td></tr>
</tbody></table>

<h3>1.2 Personas</h3>
<table class="data">
<thead><tr><th>Persona</th><th>Needs</th><th>Primary surfaces</th></tr></thead>
<tbody>
<tr><td>CloudLens operator (MSP)</td><td>Onboard tenants, manage the platform</td><td>Tenant API (API-key), deploy script</td></tr>
<tr><td>Customer FinOps lead</td><td>See recoverable spend, chargeback, budgets</td><td>Console, insights digest, chargeback view</td></tr>
<tr><td>Customer engineering director</td><td>Act on waste, understand anomalies</td><td>Drill-down explorer, waste detail, roadmap</td></tr>
<tr><td>Customer CFO / finance</td><td>Forecast, budget breach, exec summary</td><td>Forecast view, budget gauges, digest summary</td></tr>
</tbody></table>

<h3>1.3 Feature catalogue</h3>
<table class="data">
<thead><tr><th>#</th><th>Capability</th><th>What it does</th><th>Endpoint(s)</th></tr></thead>
<tbody>
<tr><td>F1</td><td>Tenant management</td><td>CRUD for customer tenants; SP credentials stored in Key Vault; soft-delete</td><td><code>/tenants</code></td></tr>
<tr><td>F2</td><td>Cost ingestion</td><td>Nightly inline job pulls cost + resource state, enriches with tags, persists (90d TTL)</td><td><code>/ingest/{tid}</code></td></tr>
<tr><td>F3</td><td>Cost analytics</td><td>Summary with period-over-period delta, breakdown by service/RG/location, daily trend</td><td><code>/costs/{tid}*</code></td></tr>
<tr><td>F4</td><td>Waste detection</td><td>12 rules (idle VM, unattached disk, orphan IP, oversized, dev/test, RI, idle app service, unused LB, old snapshots, cold storage, dup backup, expired cert) with bilingual recommendations</td><td><code>/waste/{tid}</code></td></tr>
<tr><td>F5</td><td>Spend forecast</td><td>Holt-Winters weekly-seasonal forecast with backtest MAPE and confidence</td><td><code>/forecast/{tid}</code></td></tr>
<tr><td>F6</td><td>Cost of inaction</td><td>Dual-trajectory: do-nothing vs. act; daily burn; cumulative inaction</td><td><code>/forecast/{tid}/cost-of-inaction</code></td></tr>
<tr><td>F7</td><td>Remediation roadmap</td><td>ROI-ordered phased plan; run-rate bends down per phase</td><td><code>/forecast/{tid}/roadmap</code></td></tr>
<tr><td>F8</td><td>Budget-breach prediction</td><td>Date budget is exceeded on each trajectory</td><td><code>/forecast/{tid}/budget-breach</code></td></tr>
<tr><td>F9</td><td>Anomaly detection</td><td>Days outside the seasonal prediction band, attributed to a driver service/RG</td><td><code>/insights/{tid}/anomalies</code></td></tr>
<tr><td>F10</td><td>Chargeback / showback</td><td>Allocate spend by tag dimension; distribute shared/untagged cost (proportional/even/showback); per-group budgets</td><td><code>/insights/{tid}/chargeback</code></td></tr>
<tr><td>F11</td><td>Business insights digest</td><td>Fuse waste+anomaly+budget+chargeback+forecast into ranked bilingual statements + efficiency score</td><td><code>/insights/{tid}/digest</code></td></tr>
<tr><td>F12</td><td>Budgets</td><td>First-class budget objects (tenant or tag-scoped) with live status + forecast projection</td><td><code>/budgets/{tid}*</code></td></tr>
<tr><td>F13</td><td>PDF reports</td><td>Bilingual monthly PDF generated to Blob, downloaded via 1h SAS URL</td><td><code>/reports/{tid}*</code></td></tr>
<tr><td>F14</td><td>Multi-cloud ingestion</td><td>Provider adapters (Azure, AWS, GCP, Alibaba, OCI) normalize native billing onto the FOCUS standard</td><td>provider layer</td></tr>
<tr><td>F15</td><td>AI/LLM cost tracking</td><td>Amazon Bedrock, OpenAI, and Anthropic spend tracked as first-class FOCUS "AI and Machine Learning" category</td><td><code>/multicloud/{tid}/spend</code></td></tr>
<tr><td>F16</td><td>Cross-cloud spend</td><td>Unified spend grouped by provider with AI/LLM broken out, localised labels</td><td><code>/multicloud/{tid}/spend</code></td></tr>
<tr><td>F17</td><td>100% allocation</td><td>Rule chain (tag → tag-map → account → name-pattern → shared split) leaves €0 unallocated, fully auditable</td><td><code>/multicloud/{tid}/allocate</code></td></tr>
<tr><td>F18</td><td>Commitment management</td><td>Coverage, utilization, idle-commitment waste, and conservative purchase recommendations across providers (RI/SP/CUD)</td><td><code>/multicloud/{tid}/commitments</code></td></tr>
<tr><td>F19</td><td>Multilingual UI</td><td>Eight EU languages (EN/IT/DE/FR/ES/NL/PT/PL) via label catalog + per-request localisation</td><td><code>/multicloud/labels</code></td></tr>
<tr><td>F20</td><td>Multi-cloud drill-down</td><td>Portfolio → Provider → Account/Subscription → Service → Resource, aggregated by level over FOCUS records</td><td><code>/drilldown/{tid}</code></td></tr>
<tr><td>F21</td><td>Resource-level anomalies</td><td>Flags the specific resource whose daily cost spiked (Holt-Winters band or robust median+MAD), with a noise floor</td><td><code>/drilldown/{tid}/resource-anomalies</code></td></tr>
<tr><td>F22</td><td>Alert rules &amp; events</td><td>Five rule types (budget, spend spike, resource anomaly, waste, idle commitment); tamper-evident event log; in-app/webhook/email channels</td><td><code>/alerts/{tid}/*</code></td></tr>
<tr><td>F23</td><td>Rightsizing engine</td><td>CloudLens's own CPU+memory recommendations with cross-family downgrades and a headroom buffer — not a resold advisor</td><td><code>/optimization/{tid}/rightsizing</code></td></tr>
<tr><td>F24</td><td>Scheduling</td><td>On/off schedule recommendations for non-prod 24/7 resources (nights &amp; weekends), quantified savings</td><td><code>/optimization/{tid}/scheduling</code></td></tr>
<tr><td>F25</td><td>Utilization dashboards</td><td>Estate-wide CPU/memory with over-capacity scoring on the binding dimension; reclaimable spend</td><td><code>/optimization/{tid}/utilization</code></td></tr>
<tr><td>F26</td><td>Realized-savings ledger</td><td>Closes the ROI loop: identified → actioned → realized on the bill, with realization rate</td><td><code>/optimization/{tid}/savings/*</code></td></tr>
<tr><td>F27</td><td>Tamper-evident audit log</td><td>SHA-256 hash-chained, append-only record of security &amp; change events; integrity verifiable</td><td><code>/admin/audit</code></td></tr>
<tr><td>F28</td><td>SOC 2 control matrix</td><td>Trust Services Criteria mapped to CloudLens controls with implementation status</td><td><code>/admin/compliance/matrix</code></td></tr>
<tr><td>F29</td><td>CLI evidence export</td><td>Per-control CLI commands that prove a control is live on the deployed resources; export logs itself</td><td><code>/admin/compliance/evidence-export</code></td></tr>
<tr><td>F30</td><td>Tenant isolation enforcement</td><td>Bearer token scoped to tenant A is rejected (403) for tenant B (require_tenant_scope)</td><td>auth layer</td></tr>
</tbody></table>

<h3>1.4 Non-functional requirements</h3>
<table class="data">
<thead><tr><th>Attribute</th><th>Requirement</th></tr></thead>
<tbody>
<tr><td>Cost</td><td>~€27–48/month at 10 tenants (&lt;6% of Starter plan revenue)</td></tr>
<tr><td>Isolation</td><td>Tenant data partitioned by <code>tenant_id</code> at every layer; cross-tenant queries structurally prevented</td></tr>
<tr><td>Security</td><td>Read-only SP (Reader + Cost Management Reader); managed identity for all internal calls; no secrets in code</td></tr>
<tr><td>Availability</td><td>Scale-to-zero; cold-start tolerated; rate-limiting never fails the request (degrades to default plan)</td></tr>
<tr><td>Observability</td><td>Structured JSON logs with request_id; health endpoint with Cosmos dependency check</td></tr>
<tr><td>Localisation</td><td>All recommendations and insights bilingual EN/IT</td></tr>
</tbody></table>
"""

ARCH = """
<h2 id="arch">2. Architecture</h2>

<h3 id="hld">2.1 High-level design (HLD)</h3>
<p>Five logical layers map to Azure services. Customer subscriptions are accessed read-only via
service principals; a nightly Container Apps Job pulls and processes data inline (no message queue);
the FastAPI backend serves the API; Cosmos/Blob/Key Vault persist data; a Static Web App renders the
console.</p>
{hld}

<h3 id="lld">2.2 Low-level design (LLD) — component map</h3>
<p>Routers depend on cross-cutting middleware (auth, rate-limit, exceptions, logging) and delegate to
services. The insights layer composes the anomaly, chargeback, and forecast services.</p>
{lld}

<h3 id="erd">2.3 Data model (ERD)</h3>
<p>Four Cosmos containers, all partitioned by <code>tenant_id</code> (tenants by <code>id</code>).
Budgets are co-located in the waste_items container, discriminated by <code>type='budget'</code>.</p>
{erd}

<h3 id="class">2.4 Class diagram — domain models</h3>
{cls}

<h3 id="proc">2.5 Process flows</h3>
<h4>2.5.1 Nightly ingestion</h4>
{seq_ingest}
<h4>2.5.2 Tenant onboarding</h4>
{seq_onboard}
<h4>2.5.3 Authenticated read request</h4>
{seq_request}

<h3 id="state">2.6 State flows</h3>
<h4>2.6.1 Waste item lifecycle</h4>
{state_waste}
<h4>2.6.2 Report lifecycle</h4>
{state_report}
<h4>2.6.3 Budget status</h4>
{state_budget}
<h4>2.6.4 Tenant lifecycle</h4>
{state_tenant}
""".format(
    hld=mermaid("hld"), lld=mermaid("lld"), erd=mermaid("erd"), cls=mermaid("class"),
    seq_ingest=mermaid("seq_ingest"), seq_onboard=mermaid("seq_onboard"),
    seq_request=mermaid("seq_request"), state_waste=mermaid("state_waste"),
    state_report=mermaid("state_report"), state_budget=mermaid("state_budget"),
    state_tenant=mermaid("state_tenant"),
)

DATA_MAPPING = ("""
<h2 id="multicloud">3. Multi-Cloud &amp; AI/LLM</h2>
<p class="lead">CloudLens ingests five clouds plus AI/LLM vendors and normalizes everything onto the
FinOps Open Cost and Usage Specification (FOCUS) — the FinOps Foundation's open billing standard.
Normalizing to FOCUS (rather than a bespoke schema) is what makes cross-cloud allocation,
commitment analysis, and unit economics work uniformly across providers.</p>
""" + mermaid("multicloud") + """
<h3>3.1 Provider adapters</h3>
<table class="data">
<thead><tr><th>Provider</th><th>Native source</th><th>Key normalization</th></tr></thead>
<tbody>
<tr><td>Microsoft Azure</td><td>Cost Management query</td><td>name-based column parser to FOCUS</td></tr>
<tr><td>Amazon Web Services</td><td>Cost Explorer / CUR (FOCUS export)</td><td>amortized below unblended implies Savings Plan discount</td></tr>
<tr><td>Google Cloud</td><td>BigQuery billing export</td><td>negative credits imply Committed Use Discount</td></tr>
<tr><td>Alibaba Cloud</td><td>BSS OpenAPI (DescribeInstanceBill)</td><td>Subscription implies Reserved commitment</td></tr>
<tr><td>Oracle Cloud (OCI)</td><td>Usage API (RequestSummarizedUsages)</td><td>compartment maps to sub-account</td></tr>
<tr><td>Anthropic</td><td>Admin Cost Report API</td><td>service_category = AI and Machine Learning</td></tr>
<tr><td>OpenAI</td><td>Organization Costs API</td><td>service_category = AI and Machine Learning</td></tr>
<tr><td>Amazon Bedrock</td><td>via AWS billing path</td><td>re-tagged from Compute to AI and Machine Learning</td></tr>
</tbody></table>

<h3>3.2 FOCUS record (normalized schema)</h3>
<table class="data">
<thead><tr><th>FOCUS field</th><th>Meaning</th></tr></thead>
<tbody>
<tr><td><code>billed_cost</code></td><td>What appears on the invoice</td></tr>
<tr><td><code>effective_cost</code></td><td>Amortized cost incl. commitments — the figure used for allocation/forecast</td></tr>
<tr><td><code>list_cost</code></td><td>Cost at public on-demand rates (for savings calc)</td></tr>
<tr><td><code>service_category</code></td><td>Compute / Storage / Databases / Networking / AI and Machine Learning / ...</td></tr>
<tr><td><code>commitment_discount_type</code></td><td>Reserved / Savings Plan / Committed Use Discount / Spot</td></tr>
<tr><td><code>sub_account_id</code></td><td>AWS account / GCP project / Azure subscription / OCI compartment</td></tr>
<tr><td><code>tags</code></td><td>resource tags, used by the allocation engine</td></tr>
</tbody></table>

<h3>3.3 100% allocation — rule chain</h3>
<p>Real environments are 40-70% tagged, so tag-only chargeback leaves a large unallocated bucket.
CloudLens applies an ordered rule chain to every record, then distributes the residual, so the final
unallocated total is zero and every euro records which rule assigned it (auditable):</p>
<table class="data">
<thead><tr><th>Order</th><th>Rule</th><th>Signal</th></tr></thead>
<tbody>
<tr><td>1</td><td>Direct tag</td><td>record carries the allocation tag (cost_center=...)</td></tr>
<tr><td>2</td><td>Tag inheritance</td><td>another tag implies the cost-center (team=payments to engineering)</td></tr>
<tr><td>3</td><td>Account / project</td><td>sub_account_id maps to a cost-center</td></tr>
<tr><td>4</td><td>Name pattern</td><td>resource/service name regex (^prod-erp- to erp)</td></tr>
<tr><td>5</td><td>Shared split</td><td>residual distributed proportionally or evenly to 100%</td></tr>
</tbody></table>

<h3>3.4 Commitment management</h3>
<p>Across all providers: <strong>coverage</strong> (% of eligible compute/database spend covered by a
commitment vs. on-demand), <strong>utilization</strong> (% of held commitments actually used — idle
reservations are waste), and conservative <strong>purchase recommendations</strong> that commit only the
stable on-demand baseline, never the peak, so a recommendation never over-commits a customer.</p>

<h3>3.5 Localisation (i18n)</h3>
<p>Eight EU languages: English, Italian, German, French, Spanish, Dutch, Portuguese, Polish. A label
catalog is served via <code>GET /api/v1/multicloud/labels?lang=</code> for frontend bootstrap; the
spend endpoint also returns localised labels per request.</p>

<h2 id="mapping">4. Interface Data Mapping</h2>
<p class="lead">How external provider data maps through the system to API responses. This is the
contract every integration point depends on.</p>
""")
DATA_MAPPING += """
<h3>4.1 Azure Cost Management → CostRecord</h3>
<p>The Cost Management query returns columnar data; the parser is <strong>strictly name-based</strong>
(column order varies by query) with safe defaults for absent columns.</p>
<table class="data">
<thead><tr><th>Azure column (aliases)</th><th>CostRecord field</th><th>Transform</th></tr></thead>
<tbody>
<tr><td>Cost, CostUSD, PreTaxCost</td><td><code>cost_eur</code></td><td>float; default 0.0; mandatory</td></tr>
<tr><td>UsageDate, Date, BillingMonth</td><td><code>record_date</code></td><td>date; mandatory</td></tr>
<tr><td>ResourceId</td><td><code>resource_id</code></td><td>lower-cased</td></tr>
<tr><td>ResourceGroupName, ResourceGroup</td><td><code>resource_group</code></td><td>lower-cased</td></tr>
<tr><td>ServiceName</td><td><code>service_name</code></td><td>verbatim</td></tr>
<tr><td>ResourceLocation, Location</td><td><code>location</code></td><td>verbatim</td></tr>
<tr><td>Currency, BillingCurrency</td><td><code>currency</code></td><td>default "EUR"</td></tr>
<tr><td>UsageQuantity, Quantity</td><td><code>quantity</code></td><td>float; default 0.0</td></tr>
<tr><td>(Resource Graph bulk query)</td><td><code>tags</code></td><td>joined by lower-cased resource_id</td></tr>
</tbody></table>

<h3>4.2 Resource Graph → waste-engine context</h3>
<table class="data">
<thead><tr><th>KQL collector</th><th>Returns</th><th>Consumed by rule(s)</th></tr></thead>
<tbody>
<tr><td>disk_states</td><td>{resource_id: state}</td><td>unattached_disk</td></tr>
<tr><td>ip_associations</td><td>{resource_id: is_associated}</td><td>orphan_public_ip</td></tr>
<tr><td>snapshot_ages</td><td>{resource_id: age_days}</td><td>old_snapshots</td></tr>
<tr><td>lb_backend_counts</td><td>{resource_id: count}</td><td>unused_load_balancer</td></tr>
<tr><td>storage_access_tiers</td><td>{resource_id: tier}</td><td>cold_storage</td></tr>
<tr><td>cert_expiries</td><td>{resource_id: days_to_expiry}</td><td>expired_cert</td></tr>
<tr><td>vm_power_states</td><td>{resource_id: state}</td><td>idle_vm</td></tr>
<tr><td>resource_tags</td><td>{resource_id: {k:v}}</td><td>cost-record enrichment, chargeback</td></tr>
</tbody></table>

<h3>4.3 Cosmos persistence map</h3>
<table class="data">
<thead><tr><th>Container</th><th>Partition key</th><th>Document types</th><th>TTL</th></tr></thead>
<tbody>
<tr><td>tenants</td><td><code>/id</code></td><td>tenant</td><td>none</td></tr>
<tr><td>cost_records</td><td><code>/tenant_id</code></td><td>cost_record</td><td>90 days</td></tr>
<tr><td>waste_items</td><td><code>/tenant_id</code></td><td>waste_item, budget</td><td>none</td></tr>
<tr><td>reports</td><td><code>/tenant_id</code></td><td>report</td><td>none</td></tr>
</tbody></table>

<h3>4.4 Error code mapping</h3>
<table class="data">
<thead><tr><th>Exception</th><th>HTTP</th><th>error_code</th><th>When</th></tr></thead>
<tbody>
<tr><td>NotFoundError</td><td>404</td><td>NOT_FOUND</td><td>document missing</td></tr>
<tr><td>ValidationError</td><td>422</td><td>VALIDATION_ERROR</td><td>bad body/param</td></tr>
<tr><td>ConflictError</td><td>409</td><td>CONFLICT</td><td>duplicate tenant name</td></tr>
<tr><td>UnauthorizedError</td><td>401</td><td>UNAUTHORIZED</td><td>missing/invalid token or key</td></tr>
<tr><td>ForbiddenError</td><td>403</td><td>FORBIDDEN</td><td>token not scoped to tenant</td></tr>
<tr><td>RateLimitError</td><td>429</td><td>RATE_LIMITED</td><td>per-tenant limit exceeded</td></tr>
<tr><td>AzureAPIError</td><td>502</td><td>AZURE_API_ERROR</td><td>Cost/Advisor/Graph failure</td></tr>
<tr><td>CosmosError / StorageError / KeyVaultError</td><td>503</td><td>*_ERROR</td><td>dependency failure after retries</td></tr>
</tbody></table>
""" + """
<h2 id="optimization">5. Optimization</h2>
<p class="lead">CloudLens generates its <em>own</em> cost-reduction recommendations rather than reselling a
provider's advisor. Four engines turn per-resource utilization and cost into actionable savings.</p>
""" + mermaid("optimization") + """

<h3 id="rightsize">5.1 Rightsizing (CPU + memory, cross-family)</h3>
<p>For each compute resource the engine takes observed CPU and memory peaks, the current SKU, and the
billed cost, applies a 30% headroom buffer, and searches an instance catalog for the cheapest SKU that
satisfies <strong>both</strong> dimensions — including cross-family moves. Using memory is a deliberate edge:
memory-bound workloads (analytics, in-memory DBs, ML) are where CPU-only tools wrongly downsize.</p>
""" + mermaid("rightsize") + """
<table class="data">
<thead><tr><th>Outcome</th><th>Condition</th></tr></thead>
<tbody>
<tr><td>Terminate</td><td>CPU &lt; 1% and memory &lt; 2% over the window</td></tr>
<tr><td>Downsize</td><td>a cheaper SKU covers required vCPU + memory (may be cross-family)</td></tr>
<tr><td>No change</td><td>nothing cheaper satisfies the memory/CPU requirement</td></tr>
</tbody></table>
<p>Confidence scales with the observation window (30+ days = high; a 14-day window on a monthly-seasonal
workload is down-weighted). Endpoint: <code>GET /optimization/{tid}/rightsizing</code>.</p>

<h3>5.2 Scheduling</h3>
<p>Non-production resources running 24/7 (168h/week) that are only used in working hours are flagged for an
on/off schedule — Mon–Fri 08:00–20:00 (60h) saves ~64%. Production is never scheduled. An activity-profile
mode computes the minimal covering schedule when hourly data exists.
Endpoint: <code>GET /optimization/{tid}/scheduling</code>.</p>

<h3>5.3 Utilization &amp; over-capacity</h3>
<p>An estate-wide CPU/memory view with a per-resource over-capacity score (0–100) computed on the
<em>binding</em> dimension — a box at 85% memory / 10% CPU is "hot", not wasteful — so reclaimable spend is
never overstated. Endpoint: <code>GET /optimization/{tid}/utilization</code>.</p>

<h3>5.4 Realized-savings ledger</h3>
<p>Closes the ROI loop every mature FinOps tool has: each opportunity moves identified → actioned → realized,
with a realization rate and per-category rollup, so finance sees what actually landed on the bill.
Endpoints: <code>POST /optimization/{tid}/savings</code>, <code>.../savings/{id}/action</code>,
<code>GET .../savings/ledger</code>.</p>

<h2 id="security">6. Security &amp; Compliance</h2>
<p class="lead">CloudLens is read-only by architecture and built to be SOC 2 audit-ready. It cannot
self-certify — a SOC 1/SOC 2 attestation is issued by a licensed CPA firm over a defined period — but it
implements the technical controls and produces the evidence that audit requires.</p>

<h3>6.1 Authentication &amp; tenant isolation</h3>
<table class="data">
<thead><tr><th>Control</th><th>Mechanism</th><th>Criterion</th></tr></thead>
<tbody>
<tr><td>Authentication</td><td>Azure AD bearer (JWKS-validated) for tenant endpoints; API key for operator endpoints</td><td>CC6.1</td></tr>
<tr><td>Tenant isolation</td><td><code>require_tenant_scope</code> rejects (403) a token scoped to tenant A accessing tenant B; every Cosmos query is partition-keyed by tenant_id</td><td>CC6.1</td></tr>
<tr><td>Least privilege</td><td>Customer SPs are read-only; internal calls use a managed identity, no stored secrets</td><td>CC6.2</td></tr>
<tr><td>Injection resistance</td><td>All Cosmos queries are parameterized; enum/range validation on inputs</td><td>secure coding</td></tr>
</tbody></table>

<h3>6.2 Tamper-evident audit log</h3>
<p>Security- and change-relevant events (tenant/budget/alert-rule CRUD, waste resolutions, ingest, report
download, evidence export) are written to an append-only audit trail. Each record is SHA-256 hash-chained to
the previous one for its tenant, so any alteration or back-dating breaks the chain and is detectable.</p>
""" + mermaid("audit") + """
<p>Verify integrity at <code>GET /admin/compliance/audit-integrity/{tid}</code>; query the trail at
<code>GET /admin/audit</code>. Records carry a 2-year TTL for SOC 2 evidence retention.</p>

<h3>6.3 Encryption &amp; secrets</h3>
<table class="data">
<thead><tr><th>Control</th><th>Setting</th><th>Criterion</th></tr></thead>
<tbody>
<tr><td>In transit</td><td>TLS 1.2+ enforced; Storage HTTPS-only</td><td>CC6.6</td></tr>
<tr><td>At rest</td><td>Cosmos/Blob/Key Vault encrypted; Storage infrastructure (double) encryption</td><td>CC6.7</td></tr>
<tr><td>Secrets</td><td>SP creds + API key only in Key Vault (purge protection on); none in code/config/logs</td><td>CC6.8</td></tr>
<tr><td>Backup</td><td>Cosmos continuous backup (point-in-time restore); Key Vault soft-delete</td><td>A1.2</td></tr>
</tbody></table>

<h3>6.4 SOC 2 control matrix &amp; CLI evidence export</h3>
<p>The admin surface exposes a control matrix mapping Trust Services Criteria (CC1–CC8, Availability,
Confidentiality) to CloudLens controls with an implementation status (implemented / partial /
needs-org-process). For each technical control it generates the exact <code>az</code>/<code>curl</code>
commands an auditor runs against the deployed resources to prove the control is live, with expected output.
The evidence-pack export (<code>POST /admin/compliance/evidence-export</code>) bundles the matrix with the
audit-integrity result and logs its own execution. ~80% of controls are satisfied in code/infra; the
remainder require organizational policy (training, background checks) that software cannot provide.</p>

"""

API_SECTION = f"""
<h2 id="api">7. API Reference</h2>
<p class="lead">Generated from the live OpenAPI 3.1 specification ({len(__import__('json').load(open('/home/claude/cloudlens/docs/openapi.json'))['paths'])} paths).
The machine-readable spec ships as <code>CloudLens_openapi.json</code>. Base path <code>/api/v1</code>.
Interactive docs at <code>/docs</code> (non-prod only).</p>
<h3>7.1 Authentication</h3>
<table class="data">
<thead><tr><th>Scheme</th><th>Header</th><th>Used by</th></tr></thead>
<tbody>
<tr><td>Internal API key</td><td><code>X-API-Key</code></td><td>Tenant management, manual ingest (operator)</td></tr>
<tr><td>Azure AD bearer (JWKS)</td><td><code>Authorization: Bearer</code></td><td>All per-tenant read endpoints; scope enforced against token claim</td></tr>
</tbody></table>
<h3>7.2 Rate limiting</h3>
<p>Per-tenant token bucket: Starter 60 / Growth 200 / Enterprise 600 requests per minute. Exceeding
returns <code>429</code> with <code>Retry-After</code>. Limiting never fails open into a crash — a plan
lookup failure degrades to the Growth limit.</p>
{API_REF}
"""

DEPLOY = """
<h2 id="deploy">8. Deployment Guide</h2>
<p class="lead">One script provisions everything and deploys the app. It is idempotent and fail-fast.</p>

<h3>5.1 Prerequisites</h3>
<table class="data">
<thead><tr><th>Tool</th><th>Purpose</th></tr></thead>
<tbody>
<tr><td><code>az</code> (logged in)</td><td>Azure control plane + ACR build</td></tr>
<tr><td><code>terraform</code> ≥ 1.9</td><td>Infrastructure provisioning</td></tr>
<tr><td><code>docker</code></td><td>(only if building locally; the script uses <code>az acr build</code>)</td></tr>
<tr><td><code>jq</code></td><td>Parsing az output in the script</td></tr>
</tbody></table>
<p>The subscription needs Owner/Contributor + the ability to create user-assigned identities and role
assignments.</p>

<h3>5.2 One-command deploy</h3>
<pre><code>export TF_VAR_internal_api_key="$(openssl rand -hex 32)"
./deploy.sh prod        # or: dev | staging</code></pre>

<h3>5.3 What the script does</h3>
{deploy}
<table class="data">
<thead><tr><th>Phase</th><th>Action</th><th>Idempotent because</th></tr></thead>
<tbody>
<tr><td>0 Validate</td><td>Check tools, env, login; read names from tfvars</td><td>read-only checks</td></tr>
<tr><td>1 State backend</td><td>Create RG + storage for Terraform state</td><td>create-if-not-exists</td></tr>
<tr><td>2 Image</td><td><code>az acr build</code> → push <code>:SHA</code> + <code>:latest</code></td><td>ACR created if absent; tags overwrite</td></tr>
<tr><td>3 Apply</td><td><code>terraform apply</code> pinning the SHA image tag</td><td>Terraform state reconciles</td></tr>
<tr><td>4 Frontend</td><td>Deploy to Static Web Apps</td><td>SWA created if absent</td></tr>
<tr><td>5 Smoke test</td><td><code>GET /api/v1/health</code> (12×5s retries)</td><td>idempotent read</td></tr>
<tr><td>6 Summary</td><td>Print API URL, identity, next steps</td><td>—</td></tr>
</tbody></table>

<h3>5.4 Resources provisioned (Terraform)</h3>
<table class="data">
<thead><tr><th>Resource</th><th>SKU / mode</th><th>Cost note</th></tr></thead>
<tbody>
<tr><td>Container App (API)</td><td>Consumption, min 0 / max 5</td><td>scale-to-zero</td></tr>
<tr><td>Container App Job (ingest)</td><td>Cron 02:00 UTC</td><td>pay per run</td></tr>
<tr><td>Cosmos DB</td><td>Serverless, 4 containers</td><td>pay per RU</td></tr>
<tr><td>Storage account</td><td>Standard LRS</td><td>reports only</td></tr>
<tr><td>Key Vault</td><td>Standard, purge protection</td><td>SP creds</td></tr>
<tr><td>Container Registry</td><td>Basic</td><td>fixed ~€5</td></tr>
<tr><td>Log Analytics</td><td>PerGB2018, daily cap</td><td>cap prevents runaway</td></tr>
<tr><td>User-assigned identity</td><td>—</td><td>all internal auth</td></tr>
<tr><td>Static Web App</td><td>Free</td><td>€0</td></tr>
</tbody></table>

<h3>5.5 CI/CD</h3>
<table class="data">
<thead><tr><th>Workflow</th><th>Trigger</th><th>Stages</th></tr></thead>
<tbody>
<tr><td>backend.yml</td><td>push to app/**</td><td>pytest → docker build → push ACR → deploy ACA → health → rollback</td></tr>
<tr><td>infra.yml</td><td>push to infra/**</td><td>fmt/validate → plan → PR comment → manual approval → apply</td></tr>
</tbody></table>
<p>Auth uses GitHub OIDC federated credentials — no stored client secrets.</p>

<h3>5.6 Post-deploy</h3>
<p>Onboard a tenant (Section 7), then either wait for the 02:00 UTC ingest or trigger it:</p>
<pre><code>curl -X POST "$API_URL/api/v1/ingest/&lt;tenant_id&gt;" -H "X-API-Key: $TF_VAR_internal_api_key"</code></pre>
""".format(deploy=mermaid("deploy"))

WIRING = """
<h2 id="wiring">9. Frontend Wiring Guide</h2>
<p class="lead">All console views ship with mock data shaped to the exact API responses. Going live means
swapping the mock arrays/objects for <code>fetch()</code> — the render functions are unchanged.</p>

<h3>9.1 Auth bootstrap (MSAL)</h3>
<pre><code>// acquire an Azure AD token for the API scope, then attach to every call
const token = await msalInstance.acquireTokenSilent({scopes: [API_SCOPE]});
const authHeaders = { "Authorization": `Bearer ${token.accessToken}` };
// operator-only views (compliance admin) use the internal API key instead
const adminHeaders = { "X-API-Key": OPERATOR_KEY };</code></pre>

<h3>9.2 View → endpoint map</h3>
<table class="data">
<thead><tr><th>View</th><th>UI element</th><th>Endpoint</th><th>Maps to</th></tr></thead>
<tbody>
<tr><td rowspan="5">explorer.html</td><td>Provider rows</td><td><code>GET /drilldown/{tid}?level=provider</code></td><td>children[]</td></tr>
<tr><td>Drill provider→account→service→resource</td><td><code>GET /drilldown/{tid}?level=&amp;provider=&amp;account=&amp;service=</code></td><td>children[] + next_level</td></tr>
<tr><td>Resource anomaly badges/drawers</td><td>inline on <code>level=resource</code> (or <code>/drilldown/{tid}/resource-anomalies</code>)</td><td>child.anomaly</td></tr>
<tr><td>Alerts tab (events + rules)</td><td><code>GET /alerts/{tid}/events</code>, <code>/alerts/{tid}/rules</code></td><td>EVENTS / RULES</td></tr>
<tr><td>Budgets tab</td><td><code>GET /budgets/{tid}</code> + <code>POST /budgets/{tid}</code></td><td>BUDGETS[]</td></tr>
<tr><td rowspan="4">optimization.html</td><td>Utilization heatmap + KPIs</td><td><code>GET /optimization/{tid}/utilization</code></td><td>summary, resources[]</td></tr>
<tr><td>Rightsizing recommendations</td><td><code>GET /optimization/{tid}/rightsizing</code></td><td>recommendations[]</td></tr>
<tr><td>Scheduling recommendations</td><td><code>GET /optimization/{tid}/scheduling</code></td><td>recommendations[]</td></tr>
<tr><td>Savings ledger funnel</td><td><code>GET /optimization/{tid}/savings/ledger</code></td><td>LEDGER</td></tr>
<tr><td rowspan="3">compliance_admin.html<br/>(operator)</td><td>Control matrix + CLI evidence</td><td><code>GET /admin/compliance/matrix</code></td><td>MATRIX.controls[]</td></tr>
<tr><td>Audit-integrity badge</td><td><code>GET /admin/compliance/audit-integrity/{tid}</code></td><td>intact, records</td></tr>
<tr><td>Evidence-pack export</td><td><code>POST /admin/compliance/evidence-export</code></td><td>downloaded pack</td></tr>
<tr><td rowspan="4">forecast.html</td><td>Hero + curves</td><td><code>GET /forecast/{tid}/cost-of-inaction</code></td><td>baseline[], opt[]</td></tr>
<tr><td>Accuracy / month-end</td><td><code>GET /forecast/{tid}</code></td><td>mape, month_end_projection</td></tr>
<tr><td>Roadmap phases</td><td><code>GET /forecast/{tid}/roadmap</code></td><td>phases[]</td></tr>
<tr><td>Budget tip</td><td><code>GET /forecast/{tid}/budget-breach?monthly_budget=</code></td><td>breach dates</td></tr>
<tr><td rowspan="2">insights.html</td><td>Exec summary + insights</td><td><code>GET /insights/{tid}/digest</code></td><td>DIGEST</td></tr>
<tr><td>Chargeback panel</td><td><code>GET /insights/{tid}/chargeback?strategy=</code></td><td>CHARGEBACK[strategy]</td></tr>
<tr><td>multicloud.html</td><td>Provider bars, AI/LLM, commitments, allocation</td><td><code>GET /multicloud/{tid}/spend</code>, <code>/allocate</code>, <code>/commitments</code></td><td>SPEND / ALLOC / COMMIT</td></tr>
</tbody></table>

<h3>9.3 Worked example — wiring the explorer drill-down</h3>
<pre><code>// before (mock): getChildren(level, keys, parentSpend) returns canned children
// after (live): one call per drill level, filters carry the path
async function getChildren(level, keys, _parent) {
  const [provider, account, service] = keys;
  const qs = new URLSearchParams({ level });
  if (provider) qs.set("provider", provider);
  if (account)  qs.set("account", account);
  if (service)  qs.set("service", service);
  const res = await fetch(`${API}/api/v1/drilldown/${TID}?${qs}`, { headers: authHeaders });
  const body = await res.json();
  return body.children;   // already includes .anomaly at the resource level
}</code></pre>
<p>The render functions take the same shape either way, so only the data source changes.</p>

<h3>9.4 Error handling</h3>
<table class="data">
<thead><tr><th>Status</th><th>UI behaviour</th></tr></thead>
<tbody>
<tr><td>401 / 403</td><td>Re-acquire token; if still failing, sign out (403 also = wrong tenant scope)</td></tr>
<tr><td>429</td><td>Back off using <code>Retry-After</code>; show a soft "refreshing" state</td></tr>
<tr><td>5xx</td><td>Show last-known data with a stale badge; retry with backoff</td></tr>
</tbody></table>
"""

USER_GUIDE = """
<h2 id="user">10. User Guide</h2>
<p class="lead">How to use CloudLens day to day, by role.</p>

<h3>10.1 Onboarding a tenant (operator)</h3>
<ol class="steps">
<li><strong>Customer</strong> creates a read-only service principal in their Azure AD:
<pre><code>az ad sp create-for-rbac --name "cloudlens-readonly" \\
  --role "Reader" --scopes "/subscriptions/&lt;SUB_ID&gt;"</code></pre></li>
<li><strong>Customer</strong> adds the cost role:
<pre><code>az role assignment create --assignee "&lt;client_id&gt;" \\
  --role "Cost Management Reader" --scope "/subscriptions/&lt;SUB_ID&gt;"</code></pre></li>
<li><strong>Customer</strong> shares <code>client_id</code>, <code>client_secret</code>, <code>tenant_id</code> over a secure channel.</li>
<li><strong>Operator</strong> stores them in Key Vault as <code>sp-creds-{tenant_id}</code> and registers the tenant via <code>POST /api/v1/tenants</code>.</li>
<li>First ingest runs at 02:00 UTC, or trigger it immediately with <code>POST /api/v1/ingest/{tenant_id}</code>.</li>
</ol>
<p>Full commands are in <code>TENANT_ONBOARDING.md</code> (and its PDF). For other clouds, the customer
provisions the equivalent read-only role (AWS Cost Explorer read / GCP Billing Viewer / OCI usage read).</p>

<h3>10.2 The multi-cloud explorer (engineering)</h3>
<p>Drill from the portfolio total down to a single resource across every cloud:
<strong>Portfolio → Provider → Account/Subscription → Service → Resource</strong>. The pinned breadcrumb lets
you jump back to any level. Each row shows spend and its share of the level. At the resource level, a
resource flagged as a cost <strong>anomaly</strong> carries a red/amber badge — click it to open a drawer
showing actual vs. expected daily cost, the excess, the z-score, the date, and the detection method. The
<strong>Alerts</strong> tab lists triggered events and rules; the <strong>Budgets</strong> tab creates and
tracks budgets (scopable to a provider or account, not just tags).</p>
<p><strong>Reading a resource cost anomaly.</strong> The drawer fields map directly to the detector's output:</p>
<table class="data">
<thead><tr><th>Field</th><th>What it means</th></tr></thead>
<tbody>
<tr><td>Actual (day)</td><td>The resource's cost on the flagged day.</td></tr>
<tr><td>Expected</td><td>What the model predicted for that day from the resource's own history.</td></tr>
<tr><td>Excess</td><td>Actual − Expected — the euro size of the spike (cleared the ≥€10 / ≥25% noise floor).</td></tr>
<tr><td>Z-score</td><td>How many standard deviations of the resource's normal noise the spike represents. Flags at ≥2σ, "high" at ≥3.5σ; 6σ is a near-certain anomaly, not chance.</td></tr>
<tr><td>Date</td><td>The day the anomaly occurred (the engine scans the last few days).</td></tr>
<tr><td>Method</td><td><code>holt_winters</code> (seasonal model, ≥16 days of history) or <code>median_mad</code> (robust fallback for sparse resources). Tells you how strong the "expected" baseline is.</td></tr>
</tbody></table>
<p>Worked example: a VM at <code>€1,076</code> actual vs <code>€419</code> expected is <code>+€657</code>
excess at <code>6σ</code> via <code>holt_winters</code> — a high-confidence spike worth investigating.</p>

<h3>10.3 The forecast view (finance / CFO)</h3>
<p>The hero is the <strong>cost of inaction</strong>: the cumulative spend you forgo by not acting, plus a
per-day burn figure. The chart shows two diverging curves — "do nothing" vs. "if you act" — with the gap
shaded. Below, the <strong>remediation roadmap</strong> shows the monthly run-rate bending down as each phase
lands, and a <strong>budget-breach</strong> line predicts the date you exceed budget on each trajectory. Every
forecast carries a backtest MAPE so you know how much to trust it.</p>

<h3>10.4 The optimization console (engineering / FinOps)</h3>
<p>Four tabs answer "where can we save and are we over-capacitied?". <strong>Utilization</strong> is an
estate heatmap with a per-resource over-capacity score on the binding (CPU-or-memory) dimension.
<strong>Rightsizing</strong> lists CloudLens's own CPU+memory recommendations — downsize (often cross-family),
terminate, with the saving and confidence. <strong>Scheduling</strong> shows non-prod 24/7 resources that can
shut down nights and weekends and the % saved. The <strong>Savings ledger</strong> tracks identified →
actioned → realized so you can prove ROI on the bill.</p>

<h3>10.5 The business-insights console (FinOps lead)</h3>
<p>The <strong>efficiency score</strong> (0–100) and a one-paragraph executive summary lead. The
<strong>ranked insights</strong> list fuses waste, anomalies, budgets, allocation, and forecast into
plain-language statements ordered by severity × € impact, each with a recommended action. The
<strong>chargeback</strong> panel allocates spend to cost-centres — toggle proportional / even / showback —
showing direct vs. allocated-shared spend, budget status, and tagging coverage.</p>

<h3>10.6 The compliance admin (operator)</h3>
<p>Operator-only (API-key). Shows the SOC 2 control matrix; expand any control to see the exact
<code>az</code>/<code>curl</code> commands that prove it is live on the deployed resources, with expected
output. An audit-integrity badge verifies the hash-chained audit log is intact, and the evidence-pack export
bundles the matrix and integrity result (and logs its own execution). It states prominently that it is an
audit-readiness aid, not a SOC report.</p>

<h3>10.7 Acting on a waste finding</h3>
<table class="data">
<thead><tr><th>Action</th><th>Effect</th><th>API</th></tr></thead>
<tbody>
<tr><td>Resolve</td><td>Marks the finding resolved with actor + timestamp; removed from open lists</td><td><code>PATCH /waste/{id}/resolve</code></td></tr>
<tr><td>Snooze 30d</td><td>Hidden for 30 days; reappears if still wasteful</td><td><code>PATCH /waste/{id}/resolve</code> (snooze)</td></tr>
</tbody></table>
<p>Resolved findings still count toward historical savings realised.</p>

<h3>10.8 Setting a budget</h3>
<pre><code>curl -X POST "$API/api/v1/budgets/&lt;tid&gt;" -H "Authorization: Bearer &lt;jwt&gt;" \\
  -H "Content-Type: application/json" -d '{
    "tenant_id": "&lt;tid&gt;", "name": "Engineering", "amount_eur": 11000,
    "scope_dimension": "cost_center", "scope_value": "engineering" }'</code></pre>
<p>Check status (spend-to-date + forecast projection) any time:
<code>GET /api/v1/budgets/&lt;tid&gt;/&lt;budget_id&gt;/status</code>.</p>

<h3>7.7 Reading a monthly report</h3>
<p>Generate with <code>POST /api/v1/reports/{tid}/generate</code> (returns immediately, status
<code>pending</code>). Poll <code>GET /api/v1/reports/{tid}</code> until status is <code>ready</code>, then
download via the 1-hour SAS URL from <code>GET /api/v1/reports/{report_id}/download</code>.</p>
"""

GLOSSARY = """
<h2 id="glossary">11. Glossary</h2>
<table class="data">
<thead><tr><th>Term</th><th>Meaning</th></tr></thead>
<tbody>
<tr><td>Showback</td><td>Reporting each team's cloud cost for visibility, without formally charging it back</td></tr>
<tr><td>Chargeback</td><td>Allocating cloud cost (including shared) to teams as an internal charge</td></tr>
<tr><td>Cost of inaction</td><td>Cumulative spend forgone by not remediating known waste</td></tr>
<tr><td>MAPE</td><td>Mean absolute percentage error — the forecast's backtested accuracy</td></tr>
<tr><td>Efficiency score</td><td>0–100 index; 100 = no detectable waste</td></tr>
<tr><td>Waste ratio</td><td>Recoverable spend as a percentage of total spend</td></tr>
<tr><td>Run-rate</td><td>Annualised/monthly projected spend at the current pace</td></tr>
<tr><td>SP</td><td>Service principal — the read-only identity CloudLens uses per customer</td></tr>
<tr><td>Z-score (σ)</td><td>How far an observed value sits above the model's expectation, in standard deviations of the resource's own historical noise. The resource-anomaly engine flags at ≥2σ and escalates to "high" at ≥3.5σ — so a 6σ reading is far past the alarm line and almost never occurs by chance.</td></tr>
<tr><td>Method (anomaly)</td><td>Which detector produced the expected figure: <code>holt_winters</code> (triple-exponential smoothing with weekly seasonality, used when ≥16 days of history exist) or <code>median_mad</code> (robust median + median-absolute-deviation, the fallback for sparse/new resources). Surfaced so the reader can judge how much to trust the expectation.</td></tr>
<tr><td>Holt-Winters</td><td>A forecasting method (triple exponential smoothing) that models level, trend, and seasonality — CloudLens uses an additive, weekly-seasonal variant for both spend forecasts and resource-anomaly bands.</td></tr>
<tr><td>Excess (anomaly)</td><td>Actual cost on the day minus the expected cost — the euro size of the spike. A noise floor (≥€10 and ≥25% above expected) suppresses trivial blips.</td></tr>
<tr><td>Over-capacity score</td><td>0–100 measure of how over-provisioned a resource is, computed on the binding dimension (the higher of CPU or memory utilization), so a memory-bound box isn't falsely flagged as wasteful.</td></tr>
</tbody></table>
<p class="footer-note">CloudLens — Multi-Cloud FinOps Managed Service · Technical Documentation v1.1 · June 2026 · CONFIDENTIAL</p>
"""

NAV = [
    ("func", "1. Functional spec"),
    ("arch", "2. Architecture"),
    ("hld", "&nbsp;&nbsp;2.1 HLD"),
    ("lld", "&nbsp;&nbsp;2.2 LLD"),
    ("erd", "&nbsp;&nbsp;2.3 Data model"),
    ("class", "&nbsp;&nbsp;2.4 Class diagram"),
    ("proc", "&nbsp;&nbsp;2.5 Process flows"),
    ("state", "&nbsp;&nbsp;2.6 State flows"),
    ("multicloud", "3. Multi-cloud &amp; AI"),
    ("mapping", "4. Data mapping"),
    ("optimization", "5. Optimization"),
    ("security", "6. Security &amp; compliance"),
    ("api", "7. API reference"),
    ("deploy", "8. Deployment"),
    ("wiring", "9. Wiring guide"),
    ("user", "10. User guide"),
    ("glossary", "11. Glossary"),
]

nav_html = "\n".join(f'<a href="#{i}">{label}</a>' for i, label in NAV)

HTML = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>CloudLens — Technical Documentation</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;450;500;600&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
:root{{--bg:#0d1218;--bg2:#131b24;--panel:#18222e;--line:#263340;--lineSoft:#1e2a36;
--txt:#e6edf3;--txt2:#9fb0c0;--txt3:#647386;--teal:#2dd4bf;--amber:#f5a524;--red:#f0506e;--blue:#4d9fff;
--mono:'Space Grotesk',sans-serif;--body:'Inter',sans-serif;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--body);background:var(--bg);color:var(--txt);font-size:14.5px;line-height:1.65;display:flex}}
nav{{position:fixed;top:0;left:0;width:230px;height:100vh;background:var(--bg2);border-right:1px solid var(--lineSoft);
overflow-y:auto;padding:22px 16px}}
nav .brand{{font-family:var(--mono);font-weight:600;font-size:16px;color:var(--teal);margin-bottom:4px}}
nav .sub{{font-size:11px;color:var(--txt3);margin-bottom:18px}}
nav a{{display:block;color:var(--txt2);text-decoration:none;font-size:13px;padding:5px 8px;border-radius:6px;margin-bottom:1px}}
nav a:hover{{background:var(--panel);color:var(--teal)}}
main{{margin-left:230px;padding:36px 48px 80px;max-width:1000px}}
h1{{font-family:var(--mono);font-size:30px;font-weight:600;letter-spacing:-.02em;margin-bottom:6px}}
h1 .dot{{color:var(--teal)}}
.docsub{{color:var(--txt2);font-size:14px;margin-bottom:30px;padding-bottom:18px;border-bottom:1px solid var(--lineSoft)}}
h2{{font-family:var(--mono);font-size:21px;font-weight:600;margin:40px 0 14px;padding-top:14px;border-top:1px solid var(--lineSoft);color:var(--txt)}}
h3{{font-family:var(--mono);font-size:16px;font-weight:600;margin:26px 0 10px;color:var(--teal)}}
h4{{font-size:14px;font-weight:600;margin:18px 0 8px;color:var(--blue)}}
p{{margin-bottom:12px;color:var(--txt2)}}
p.lead{{color:var(--txt);font-size:15px}}
code{{font-family:var(--mono);font-size:12.5px;background:var(--panel);padding:1px 6px;border-radius:5px;color:#a5e8dd}}
pre{{background:#0a0f14;border:1px solid var(--lineSoft);border-radius:9px;padding:14px 16px;overflow-x:auto;margin:12px 0}}
pre code{{background:none;padding:0;color:#d7e0ea;font-size:12px;line-height:1.6}}
table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}}
th{{text-align:left;font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.04em;
color:var(--txt3);padding:8px 10px;border-bottom:1px solid var(--line);font-weight:500}}
td{{padding:8px 10px;border-bottom:1px solid var(--lineSoft);color:var(--txt2);vertical-align:top}}
tr:hover td{{background:var(--bg2)}}
td.typ,td.req{{font-family:var(--mono);font-size:12px;color:var(--amber)}}
td.req{{color:var(--teal);text-align:center}}
.method{{font-family:var(--mono);font-size:10.5px;font-weight:600;padding:2px 7px;border-radius:5px}}
.method.get{{background:#13314d;color:var(--blue)}} .method.post{{background:#14463f;color:var(--teal)}}
.method.patch{{background:#4a3410;color:var(--amber)}} .method.delete{{background:#4a1622;color:var(--red)}}
.api-group{{margin-top:24px}}
.schema-name{{margin-top:16px;color:var(--txt)}}
ol.steps li,ul.steps li{{margin:0 0 10px 18px;color:var(--txt2)}}
.mermaid{{background:var(--bg2);border:1px solid var(--lineSoft);border-radius:11px;padding:18px;margin:14px 0;text-align:center}}
.footer-note{{margin-top:30px;padding-top:16px;border-top:1px solid var(--lineSoft);font-size:12px;color:var(--txt3);text-align:center}}
em{{color:var(--txt);font-style:normal;border-bottom:1px dotted var(--txt3)}}
</style></head>
<body>
<nav>
  <div class="brand">CloudLens</div>
  <div class="sub">Technical Documentation v1.0</div>
  {nav_html}
</nav>
<main>
  <h1><span class="dot">◈</span> CloudLens Technical Documentation</h1>
  <div class="docsub">Multi-Cloud FinOps Managed Service · complete reference: functional spec, architecture,
  multi-cloud, optimization, security &amp; compliance, API, deployment, wiring, and user guide · June 2026 · CONFIDENTIAL</div>
  {FUNCTIONAL_SPEC}
  {ARCH}
  {DATA_MAPPING}
  {API_SECTION}
  {DEPLOY}
  {WIRING}
  {USER_GUIDE}
  {GLOSSARY}
</main>
<script>
mermaid.initialize({{startOnLoad:true, theme:'dark',
  themeVariables:{{darkMode:true, background:'#131b24', primaryColor:'#18222e',
  primaryTextColor:'#e6edf3', primaryBorderColor:'#2dd4bf', lineColor:'#647386',
  fontFamily:'Space Grotesk', fontSize:'13px'}}}});
</script>
</body></html>"""

open("/mnt/user-data/outputs/CloudLens_Documentation.html", "w").write(HTML)
print("Documentation site written:", len(HTML), "bytes")
