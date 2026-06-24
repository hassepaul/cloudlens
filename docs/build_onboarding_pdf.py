#!/usr/bin/env python3
"""CloudLens Tenant Onboarding Guide — bilingual EN/IT PDF."""
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak
)

W, H = A4
M = 18 * mm
CW = W - 2 * M

C = {
    "blue": colors.HexColor("#1D4ED8"), "blue_light": colors.HexColor("#DBEAFE"),
    "blue_dark": colors.HexColor("#1E3A5F"), "teal": colors.HexColor("#059669"),
    "teal_light": colors.HexColor("#D1FAE5"), "amber": colors.HexColor("#B45309"),
    "amber_light": colors.HexColor("#FEF3C7"), "red": colors.HexColor("#991B1B"),
    "gray_dark": colors.HexColor("#1A1A18"), "gray_mid": colors.HexColor("#4A4A46"),
    "gray_light": colors.HexColor("#F1F0EC"), "gray_border": colors.HexColor("#CCCCCC"),
    "white": colors.white, "it_blue": colors.HexColor("#4A4A9A"),
    "code_bg": colors.HexColor("#0F1620"), "code_fg": colors.HexColor("#D7E0EA"),
}
ss = getSampleStyleSheet()
def S(n, **k): return ParagraphStyle(n, parent=ss["Normal"], **k)

COVER_T = S("ct", fontName="Helvetica-Bold", fontSize=30, textColor=C["blue"], spaceAfter=4, leading=36)
COVER_S = S("cs", fontName="Helvetica", fontSize=15, textColor=C["gray_dark"], spaceAfter=3, leading=20)
COVER_IT= S("ci", fontName="Helvetica-Oblique", fontSize=12, textColor=C["it_blue"], spaceAfter=6)
H1 = S("h1", fontName="Helvetica-Bold", fontSize=15, textColor=C["blue_dark"], spaceBefore=16, spaceAfter=5)
H2 = S("h2", fontName="Helvetica-Bold", fontSize=12, textColor=C["blue"], spaceBefore=11, spaceAfter=4)
BODY = S("b", fontName="Helvetica", fontSize=9.5, textColor=C["gray_mid"], leading=14, spaceAfter=5)
BODY_IT = S("bi", fontName="Helvetica-Oblique", fontSize=9, textColor=C["it_blue"], leading=13, spaceAfter=5, leftIndent=8, borderColor=C["blue_light"])
CAP = S("cap", fontName="Helvetica", fontSize=8, textColor=C["gray_mid"])
CODE = S("code", fontName="Courier", fontSize=7.6, textColor=C["code_fg"], leading=11)

def hr(c=None, t=0.4): return HRFlowable(width="100%", thickness=t, color=c or C["blue_light"], spaceAfter=4)
def sp(n=3): return Spacer(1, n*mm)

def code_block(lines):
    data = [[Paragraph(l.replace(" ", "\u00a0").replace("<","&lt;").replace(">","&gt;"), CODE)] for l in lines]
    t = Table(data, colWidths=[CW])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,-1), C["code_bg"]),
        ("BOX",(0,0),(-1,-1),0.5,C["blue_dark"]),
        ("TOPPADDING",(0,0),(-1,-1),2.5),("BOTTOMPADDING",(0,0),(-1,-1),1),
        ("LEFTPADDING",(0,0),(-1,-1),10),("RIGHTPADDING",(0,0),(-1,-1),8),
    ]))
    return t

def it_note(text):
    t = Table([[Paragraph(text, BODY_IT)]], colWidths=[CW])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1), colors.HexColor("#F4F4FB")),
        ("LINEBEFORE",(0,0),(-1,-1),2,C["it_blue"]),
        ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
        ("LEFTPADDING",(0,0),(-1,-1),12),("RIGHTPADDING",(0,0),(-1,-1),10),
    ]))
    return t

def table(headers, rows, widths, accent="blue"):
    hbg = C[f"{accent}_light"]; hfg = C[accent]
    head = [Paragraph(f"<b>{h}</b>", S("th",fontName="Helvetica-Bold",fontSize=8,textColor=hfg)) for h in headers]
    data = [head] + [[Paragraph(str(c), CAP) for c in r] for r in rows]
    t = Table(data, colWidths=widths)
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),hbg),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[C["white"],C["gray_light"]]),
        ("GRID",(0,0),(-1,-1),0.3,C["gray_border"]),
        ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
        ("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
    ]))
    return t

def warn(text):
    t = Table([[Paragraph(f"<b>{text}</b>", CAP)]], colWidths=[CW])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),C["amber_light"]),("BOX",(0,0),(-1,-1),0.8,C["amber"]),
        ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8),
        ("LEFTPADDING",(0,0),(-1,-1),10),
    ]))
    return t

def _hf(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(C["blue"]); canvas.setLineWidth(0.8)
    canvas.line(M, H-13*mm, W-M, H-13*mm)
    canvas.setFont("Helvetica",7.5); canvas.setFillColor(C["gray_mid"])
    canvas.drawString(M, H-11*mm, "CloudLens — Tenant Onboarding Guide  |  CONFIDENTIAL")
    canvas.drawRightString(W-M, H-11*mm, "v1.0 · June 2026")
    canvas.setStrokeColor(C["blue_light"]); canvas.setLineWidth(0.5)
    canvas.line(M, 11*mm, W-M, 11*mm)
    canvas.drawString(M, 7.5*mm, "CloudLens · Azure FinOps Managed Service")
    canvas.drawRightString(W-M, 7.5*mm, f"Page {doc.page}")
    canvas.restoreState()

def build(out):
    doc = SimpleDocTemplate(out, pagesize=A4, topMargin=18*mm, bottomMargin=18*mm, leftMargin=M, rightMargin=M)
    s = []

    # Cover
    s += [sp(14)]
    s.append(Paragraph("CloudLens", COVER_T))
    s.append(Paragraph("Tenant Onboarding Guide", COVER_S))
    s.append(Paragraph("Guida all'Onboarding di un Nuovo Cliente", COVER_IT))
    s += [sp(3)]; s.append(hr(C["blue"],1.0)); s += [sp(3)]
    s.append(Paragraph(
        "Onboarding a new customer takes under 20 minutes and requires zero code changes. The whole "
        "process is configuration-driven. The customer creates a read-only service principal in their "
        "own Azure AD — CloudLens never receives Owner or Contributor access.", BODY))
    s.append(it_note(
        "L'onboarding di un nuovo cliente richiede meno di 20 minuti e nessuna modifica al codice. Il "
        "cliente crea un service principal in sola lettura nel proprio Azure AD — CloudLens non riceve "
        "mai accesso Owner o Contributor."))
    s += [sp(4)]

    # At a glance
    s.append(Paragraph("At a Glance / In Sintesi", H1)); s.append(hr())
    s.append(table(
        ["Step","Who","Action","Time"],
        [["1","Customer","Create an App Registration (service principal)","2 min"],
         ["2","Customer","Assign Reader + Cost Management Reader on the subscription","3 min"],
         ["3","Customer","Share client_id, client_secret, tenant_id securely","2 min"],
         ["4","CloudLens ops","Store the SP credentials in Key Vault","2 min"],
         ["5","CloudLens ops","POST /api/v1/tenants to register the tenant","1 min"],
         ["6","System","Validates the SP, stores config to Cosmos","< 1 min"],
         ["7","System","First ingest runs at next nightly cycle (or on-demand)","~5 min"]],
        [CW*0.07, CW*0.18, CW*0.62, CW*0.13]))
    s += [sp(4)]

    # Security
    s.append(Paragraph("Security Model / Modello di Sicurezza", H1)); s.append(hr())
    s.append(Paragraph(
        "CloudLens is read-only by architecture. The service principal is granted only two built-in "
        "Azure roles, both read-only: Reader (list resources, read state) and Cost Management Reader "
        "(read cost and usage). There is no path by which CloudLens can modify, delete, or create "
        "anything in the customer subscription — even a compromise of CloudLens could only read "
        "cost and resource metadata.", BODY))
    s.append(it_note(
        "CloudLens è read-only per architettura. Il service principal riceve solo due ruoli Azure "
        "integrati, entrambi in sola lettura (Reader e Cost Management Reader). Non esiste alcun modo "
        "per CloudLens di modificare, eliminare o creare risorse nella sottoscrizione del cliente."))
    s.append(PageBreak())

    # Customer steps
    s.append(Paragraph("Customer Steps / Passi a Carico del Cliente", H1)); s.append(hr())
    s.append(Paragraph("Step 1 — Create the service principal", H2))
    s.append(Paragraph("The customer runs this in their own Azure tenant (Cloud Shell or local CLI):", BODY))
    s.append(code_block([
        "az ad sp create-for-rbac \\",
        "  --name \"cloudlens-readonly\" \\",
        "  --role \"Reader\" \\",
        "  --scopes \"/subscriptions/<SUBSCRIPTION_ID>\"",
    ]))
    s += [sp(2)]
    s.append(Paragraph("This prints appId (client_id), password (client_secret) and tenant (tenant_id).", BODY))
    s.append(it_note("Il cliente esegue il comando nel proprio tenant Azure. L'output contiene appId, password e tenant."))
    s += [sp(2)]
    s.append(Paragraph("Step 2 — Add the Cost Management Reader role", H2))
    s.append(Paragraph("create-for-rbac grants Reader; add the cost role as well:", BODY))
    s.append(code_block([
        "az role assignment create \\",
        "  --assignee \"<client_id>\" \\",
        "  --role \"Cost Management Reader\" \\",
        "  --scope \"/subscriptions/<SUBSCRIPTION_ID>\"",
    ]))
    s += [sp(2)]
    s.append(Paragraph("Step 3 — Share the credentials securely", H2))
    s.append(warn("Send client_id, client_secret and tenant_id via a one-time secret link or encrypted "
                  "message. NEVER email the secret in plaintext."))
    s.append(it_note("Inviare le credenziali tramite canale sicuro (link segreto monouso o messaggio cifrato). "
                     "Mai inviare il secret in chiaro via email."))
    s.append(PageBreak())

    # Ops steps
    s.append(Paragraph("CloudLens Ops Steps / Passi a Carico di CloudLens", H1)); s.append(hr())
    s.append(Paragraph("Step 4 — Store the credentials in Key Vault", H2))
    s.append(Paragraph("The TENANT_ID is the CloudLens-internal identifier (a UUID you choose for this "
                       "customer), not the customer's Azure AD tenant. Use the same UUID in Step 5.", BODY))
    s.append(code_block([
        "TENANT_ID=\"$(uuidgen | tr '[:upper:]' '[:lower:]')\"",
        "",
        "az keyvault secret set \\",
        "  --vault-name \"kv-cloudlens-prod\" \\",
        "  --name \"sp-creds-${TENANT_ID}\" \\",
        "  --value \"$(jq -nc \\",
        "      --arg cid '<client_id>' --arg sec '<client_secret>' \\",
        "      --arg tid '<customer_tenant_id>' \\",
        "      '{client_id:$cid, client_secret:$sec, azure_tenant_id:$tid}')\"",
        "",
        "echo \"Use this tenant_id when registering: ${TENANT_ID}\"",
    ]))
    s.append(it_note("Il TENANT_ID è l'identificativo interno CloudLens (un UUID scelto per questo "
                     "cliente), non il tenant Azure AD del cliente. Il secret è un JSON con le chiavi "
                     "client_id, client_secret, azure_tenant_id."))
    s += [sp(2)]
    s.append(Paragraph("Step 5 — Register the tenant via the API", H2))
    s.append(code_block([
        "API_URL=\"https://<api-fqdn>\"",
        "",
        "curl -X POST \"${API_URL}/api/v1/tenants\" \\",
        "  -H \"X-API-Key: ${INTERNAL_API_KEY}\" \\",
        "  -H \"Content-Type: application/json\" \\",
        "  -d '{",
        "    \"id\":               \"<TENANT_ID from Step 4>\",",
        "    \"tenant_name\":      \"Acme Manufacturing SpA\",",
        "    \"subscription_ids\": [\"<SUBSCRIPTION_ID>\"],",
        "    \"plan_tier\":        \"growth\",",
        "    \"alert_email\":      \"finops@acme.example\",",
        "    \"active\":           true",
        "  }'",
    ]))
    s += [sp(2)]
    s.append(Paragraph("plan_tier is one of starter, growth, enterprise. The API validates the "
                       "subscription IDs and email, then writes the config to Cosmos DB.", BODY))
    s += [sp(2)]
    s.append(Paragraph("Step 6 — (Optional) Trigger the first ingest now", H2))
    s.append(Paragraph("The nightly job picks up the new tenant automatically at 02:00 UTC. To populate "
                       "the dashboard immediately:", BODY))
    s.append(code_block([
        "curl -X POST \"${API_URL}/api/v1/ingest/<TENANT_ID>\" \\",
        "  -H \"X-API-Key: ${INTERNAL_API_KEY}\"",
    ]))
    s += [sp(2)]
    s.append(Paragraph("This runs the full ingest inline: pull costs, collect resource state via "
                       "Resource Graph, run the 12 waste rules, persist results (~5 min for 30 days).", BODY))
    s.append(it_note("Il job notturno acquisisce il nuovo tenant automaticamente alle 02:00 UTC. Per "
                     "popolare subito la dashboard, usare il trigger manuale (~5 minuti)."))
    s.append(PageBreak())

    # Troubleshooting
    s.append(Paragraph("Troubleshooting / Risoluzione Problemi", H1)); s.append(hr())
    s.append(table(
        ["Symptom","Likely cause","Fix"],
        [["401 on /tenants","Wrong or missing X-API-Key","Use the internal_api_key set at deploy time"],
         ["409 CONFLICT on create","Tenant name already exists","Use a unique name, or PATCH the existing tenant"],
         ["422 on create","Invalid subscription_id or email","Check the payload format"],
         ["Ingest runs, no cost data","SP missing Cost Management Reader","Re-run Step 2"],
         ["Ingest auth error","Wrong or expired client_secret","Rotate the SP secret, update Key Vault (Step 4)"],
         ["Waste empty, costs present","Resource Graph access blocked","Ensure SP has Reader at subscription scope"]],
        [CW*0.27, CW*0.34, CW*0.39], "amber"))
    s += [sp(3)]
    s.append(Paragraph("Rotating a service principal secret", H2))
    s.append(Paragraph("SP secrets expire (default 1 year). To rotate without downtime — the customer "
                       "regenerates the secret, CloudLens updates Key Vault under the same secret name. "
                       "The next ingest picks up the new secret automatically; no redeploy.", BODY))
    s.append(code_block([
        "# Customer:",
        "az ad sp credential reset --id \"<client_id>\"",
        "",
        "# CloudLens ops (same secret name):",
        "az keyvault secret set --vault-name \"kv-cloudlens-prod\" \\",
        "  --name \"sp-creds-<TENANT_ID>\" \\",
        "  --value \"$(jq -nc --arg cid '<client_id>' --arg sec '<new_secret>' \\",
        "             --arg tid '<customer_tenant_id>' \\",
        "             '{client_id:$cid, client_secret:$sec, azure_tenant_id:$tid}')\"",
    ]))
    s += [sp(3)]
    s.append(Paragraph("Offboarding / Disattivazione", H1)); s.append(hr())
    s.append(Paragraph("To stop monitoring a tenant without deleting its history (soft delete — sets "
                       "active=false, preserves all data):", BODY))
    s.append(code_block([
        "curl -X DELETE \"${API_URL}/api/v1/tenants/<TENANT_ID>\" \\",
        "  -H \"X-API-Key: ${INTERNAL_API_KEY}\"",
    ]))
    s += [sp(2)]
    s.append(Paragraph("The customer should also delete the cloudlens-readonly service principal on "
                       "their side.", BODY))
    s.append(it_note("Per smettere di monitorare un tenant senza eliminarne lo storico, usare DELETE "
                     "(soft-delete). Il cliente dovrebbe inoltre eliminare il service principal "
                     "cloudlens-readonly dal proprio lato."))
    s += [sp(6)]
    s.append(hr(C["blue_light"],0.4)); s += [sp(2)]
    s.append(Paragraph("— End of Document / Fine del Documento —   CloudLens Engineering · June 2026",
                       S("end",fontName="Helvetica-Oblique",fontSize=8,textColor=C["gray_mid"],alignment=1)))

    doc.build(s, onFirstPage=_hf, onLaterPages=_hf)
    print(f"Onboarding PDF written → {out}")

if __name__ == "__main__":
    build("/mnt/user-data/outputs/CloudLens_Tenant_Onboarding.pdf")
