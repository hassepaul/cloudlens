"""
Internationalisation (i18n) for CloudLens
=========================================

Major EU languages: English, Italian, German, French, Spanish, Dutch,
Portuguese, Polish. Translation is key-based; the API can return localised
labels/messages by passing ?lang= or an Accept-Language header.

Recommendation text that is data-driven (numbers, resource names) is composed
from translated templates so it localises without per-record translation.
"""
from __future__ import annotations

SUPPORTED = ("en", "it", "de", "fr", "es", "nl", "pt", "pl")
DEFAULT = "en"

# UI / label catalog. Keys are stable; values per language.
CATALOG: dict[str, dict[str, str]] = {
    "en": {
        "recoverable_monthly": "Recoverable / month",
        "total_spend": "Total spend",
        "waste_ratio": "Waste ratio",
        "efficiency_score": "Efficiency score",
        "cost_of_inaction": "Cost of inaction",
        "forecast": "Forecast",
        "anomalies": "Anomalies",
        "chargeback": "Chargeback",
        "budgets": "Budgets",
        "commitments": "Commitments",
        "coverage": "Coverage",
        "utilization": "Utilization",
        "do_nothing": "Do nothing",
        "if_you_act": "If you act",
        "lost_per_day": "lost / day",
        "by_provider": "By cloud provider",
        "ai_spend": "AI / LLM spend",
        "unallocated": "Unallocated",
        "executive_summary": "Executive summary",
        "act_now": "act now",
        "review": "review",
        "optimized": "optimized",
    },
    "it": {
        "recoverable_monthly": "Recuperabile / mese",
        "total_spend": "Spesa totale",
        "waste_ratio": "Tasso di spreco",
        "efficiency_score": "Punteggio di efficienza",
        "cost_of_inaction": "Costo dell'inazione",
        "forecast": "Previsione",
        "anomalies": "Anomalie",
        "chargeback": "Ribaltamento costi",
        "budgets": "Budget",
        "commitments": "Impegni",
        "coverage": "Copertura",
        "utilization": "Utilizzo",
        "do_nothing": "Non agire",
        "if_you_act": "Se agisci",
        "lost_per_day": "persi / giorno",
        "by_provider": "Per provider cloud",
        "ai_spend": "Spesa AI / LLM",
        "unallocated": "Non allocato",
        "executive_summary": "Sintesi esecutiva",
        "act_now": "agire ora",
        "review": "rivedere",
        "optimized": "ottimizzato",
    },
    "de": {
        "recoverable_monthly": "Einsparbar / Monat",
        "total_spend": "Gesamtausgaben",
        "waste_ratio": "Verschwendungsquote",
        "efficiency_score": "Effizienzwert",
        "cost_of_inaction": "Kosten der Untätigkeit",
        "forecast": "Prognose",
        "anomalies": "Anomalien",
        "chargeback": "Kostenverrechnung",
        "budgets": "Budgets",
        "commitments": "Verpflichtungen",
        "coverage": "Abdeckung",
        "utilization": "Auslastung",
        "do_nothing": "Nichts tun",
        "if_you_act": "Wenn Sie handeln",
        "lost_per_day": "verloren / Tag",
        "by_provider": "Nach Cloud-Anbieter",
        "ai_spend": "KI / LLM-Ausgaben",
        "unallocated": "Nicht zugeordnet",
        "executive_summary": "Zusammenfassung",
        "act_now": "jetzt handeln",
        "review": "prüfen",
        "optimized": "optimiert",
    },
    "fr": {
        "recoverable_monthly": "Récupérable / mois",
        "total_spend": "Dépense totale",
        "waste_ratio": "Taux de gaspillage",
        "efficiency_score": "Score d'efficacité",
        "cost_of_inaction": "Coût de l'inaction",
        "forecast": "Prévision",
        "anomalies": "Anomalies",
        "chargeback": "Refacturation",
        "budgets": "Budgets",
        "commitments": "Engagements",
        "coverage": "Couverture",
        "utilization": "Utilisation",
        "do_nothing": "Ne rien faire",
        "if_you_act": "Si vous agissez",
        "lost_per_day": "perdus / jour",
        "by_provider": "Par fournisseur cloud",
        "ai_spend": "Dépense IA / LLM",
        "unallocated": "Non alloué",
        "executive_summary": "Synthèse",
        "act_now": "agir maintenant",
        "review": "à revoir",
        "optimized": "optimisé",
    },
    "es": {
        "recoverable_monthly": "Recuperable / mes",
        "total_spend": "Gasto total",
        "waste_ratio": "Tasa de desperdicio",
        "efficiency_score": "Puntuación de eficiencia",
        "cost_of_inaction": "Coste de la inacción",
        "forecast": "Previsión",
        "anomalies": "Anomalías",
        "chargeback": "Imputación de costes",
        "budgets": "Presupuestos",
        "commitments": "Compromisos",
        "coverage": "Cobertura",
        "utilization": "Utilización",
        "do_nothing": "No actuar",
        "if_you_act": "Si actúas",
        "lost_per_day": "perdidos / día",
        "by_provider": "Por proveedor cloud",
        "ai_spend": "Gasto IA / LLM",
        "unallocated": "Sin asignar",
        "executive_summary": "Resumen ejecutivo",
        "act_now": "actuar ya",
        "review": "revisar",
        "optimized": "optimizado",
    },
    "nl": {
        "recoverable_monthly": "Terugwinbaar / maand",
        "total_spend": "Totale uitgaven",
        "waste_ratio": "Verspillingsratio",
        "efficiency_score": "Efficiëntiescore",
        "cost_of_inaction": "Kosten van nietsdoen",
        "forecast": "Prognose",
        "anomalies": "Anomalieën",
        "chargeback": "Kostentoewijzing",
        "budgets": "Budgetten",
        "commitments": "Verplichtingen",
        "coverage": "Dekking",
        "utilization": "Benutting",
        "do_nothing": "Niets doen",
        "if_you_act": "Als u handelt",
        "lost_per_day": "verloren / dag",
        "by_provider": "Per cloudprovider",
        "ai_spend": "AI / LLM-uitgaven",
        "unallocated": "Niet toegewezen",
        "executive_summary": "Samenvatting",
        "act_now": "nu handelen",
        "review": "beoordelen",
        "optimized": "geoptimaliseerd",
    },
    "pt": {
        "recoverable_monthly": "Recuperável / mês",
        "total_spend": "Gasto total",
        "waste_ratio": "Taxa de desperdício",
        "efficiency_score": "Pontuação de eficiência",
        "cost_of_inaction": "Custo da inação",
        "forecast": "Previsão",
        "anomalies": "Anomalias",
        "chargeback": "Imputação de custos",
        "budgets": "Orçamentos",
        "commitments": "Compromissos",
        "coverage": "Cobertura",
        "utilization": "Utilização",
        "do_nothing": "Não agir",
        "if_you_act": "Se agir",
        "lost_per_day": "perdidos / dia",
        "by_provider": "Por fornecedor de cloud",
        "ai_spend": "Gasto IA / LLM",
        "unallocated": "Não alocado",
        "executive_summary": "Resumo executivo",
        "act_now": "agir agora",
        "review": "rever",
        "optimized": "otimizado",
    },
    "pl": {
        "recoverable_monthly": "Do odzyskania / miesiąc",
        "total_spend": "Całkowite wydatki",
        "waste_ratio": "Wskaźnik marnotrawstwa",
        "efficiency_score": "Wynik efektywności",
        "cost_of_inaction": "Koszt bezczynności",
        "forecast": "Prognoza",
        "anomalies": "Anomalie",
        "chargeback": "Rozliczenie kosztów",
        "budgets": "Budżety",
        "commitments": "Zobowiązania",
        "coverage": "Pokrycie",
        "utilization": "Wykorzystanie",
        "do_nothing": "Nie działać",
        "if_you_act": "Jeśli zadziałasz",
        "lost_per_day": "tracone / dzień",
        "by_provider": "Wg dostawcy chmury",
        "ai_spend": "Wydatki AI / LLM",
        "unallocated": "Nieprzydzielone",
        "executive_summary": "Podsumowanie",
        "act_now": "działaj teraz",
        "review": "przegląd",
        "optimized": "zoptymalizowane",
    },
}


def normalize_lang(lang: str | None) -> str:
    if not lang:
        return DEFAULT
    code = lang.split("-")[0].split(",")[0].strip().lower()
    return code if code in SUPPORTED else DEFAULT


def t(key: str, lang: str = DEFAULT) -> str:
    """Translate a label key into the requested language (falls back to EN)."""
    lang = normalize_lang(lang)
    return CATALOG.get(lang, {}).get(key) or CATALOG[DEFAULT].get(key, key)


def labels_for(lang: str = DEFAULT) -> dict[str, str]:
    """Return the full label dictionary for a language (for frontend bootstrap)."""
    lang = normalize_lang(lang)
    merged = dict(CATALOG[DEFAULT])
    merged.update(CATALOG.get(lang, {}))
    return merged
