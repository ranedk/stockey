"""Fixed trigger list and instructions for the trigger study's tagger.

The tagger ticks a FIXED list, it is never asked "why did this stock rise": it does not know
whether a rally followed, and a free-form "why" invites a story for every rally. Changing the
list after looking at results is a new version of the study.
"""
from __future__ import annotations

TRIGGERS = {
    "order_win": "new order, contract, LoA, tender won, supply agreement",
    "capacity_expansion": "new plant, line, capacity addition, commissioning, capex approval, expansion",
    "new_product_or_approval": "product launch, drug/US FDA/regulatory approval, licence, patent, new market entry",
    "acquisition_or_jv": "acquiring a company/business/brand, joint venture, strategic partnership",
    "divestment_or_demerger": "selling a business/asset, demerger, spin-off, slump sale",
    "fundraise": "QIP, rights issue, preferential allotment, warrants, IPO of a subsidiary, NCDs, large borrowing",
    "promoter_buying": "promoter/insider buying shares, creeping acquisition, promoter warrants conversion",
    "large_investor_entry": "a fund, institution or strategic investor taking a stake (allotment or purchase)",
    "promoter_selling": "promoter/insider selling or reducing stake, offer for sale",
    "debt_reduction_or_rating_upgrade": "debt repaid/reduced, deleveraging, credit rating upgrade or outlook raised",
    "rating_downgrade_or_default": "credit rating downgrade/negative outlook, default, delayed payment, NPA",
    "results_commentary_positive": "record/strong results, guidance raised, strong business update or sales numbers",
    "results_commentary_negative": "weak results, guidance cut, profit warning, weak business update",
    "management_change": "CEO/MD/CFO appointed or resigned, key management change, board overhaul",
    "regulatory_or_legal_negative": "penalty, SEBI/tax/ED action, litigation lost, import alert, warning letter, ban",
    "regulatory_or_legal_positive": "case won, penalty quashed, favourable order or policy for the company",
    "operational_disruption": "fire, accident, plant shutdown, strike, cyber attack, force majeure",
    "capital_return": "dividend, buyback, bonus issue, stock split",
    "results_filed": "the filing just carries/announces financial results or a board meeting for them, no commentary",
    "routine": "compliance or procedural: trading window, AGM/EGM notices, newspaper ads, certificates, "
               "investor meet schedules, ESOP allotments, share transfer, KYC, loss of certificates",
    "other_material": "anything else that could move the stock",
}

_TRIGGER_LINES = "\n".join(f"- {k}: {v}" for k, v in TRIGGERS.items())

_COMMON = f"""You tag filings that Indian listed companies made to the National Stock Exchange.
For EACH input item return exactly one output item with the same id.

Fields:
- trigger: the single best match from this fixed list (ignore the company's own category, it is often wrong):
{_TRIGGER_LINES}
- direction: "positive", "negative" or "neutral" -- for the company's business, not the stock price.
- value_cr: the money amount the filing states, in Rs crore (convert lakh/million/billion/USD at 1 USD = Rs 80), else null.
  Never estimate an amount that is not stated.
- importance: 0 routine, 1 minor, 2 notable, 3 major for a company of this kind.
- needs_document: true only if the text is too vague to tag but the filing could be material
  (e.g. just "Press Release" or "Updates" with no detail).
- note: at most 15 words of what happened, from the text only.

Judge only from the text given. Do not use knowledge of what later happened to the company or its stock."""

PASS_PROMPTS = {
    "tag": _COMMON + "\nEach item has: id, company, sector, date, nse_category, text.",
    "document": _COMMON + "\nEach item also has 'document': the first pages of the attached file. "
                          "Use it; needs_document must be false.",
}

_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "trigger": {"type": "string", "enum": list(TRIGGERS)},
        "direction": {"type": "string", "enum": ["positive", "negative", "neutral"]},
        "value_cr": {"type": ["number", "null"]},
        "importance": {"type": "integer", "minimum": 0, "maximum": 3},
        "needs_document": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["id", "trigger", "direction", "value_cr", "importance", "needs_document", "note"],
}
_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": _ITEM}}, "required": ["items"]}
PASS_SCHEMAS = {"tag": _SCHEMA, "document": _SCHEMA}
