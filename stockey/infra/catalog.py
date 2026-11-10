"""The announcement catalogue: what kinds of filings exist, how to summarise each, and which facts
to capture from them with which fields. infra/records.py builds its extraction prompt from this;
docs/ANNOUNCEMENT_CATALOG.md is generated from it (`python -m infra.catalog > docs/ANNOUNCEMENT_CATALOG.md`).

THIS IS EXPECTED TO CHANGE. When a filing turns up that fits nothing here, or a field keeps
landing in "other", add the kind / type / field, bump VERSION, and add a CHANGES line saying
why. records.py re-reads every record extracted under an older version.

The exchange's own category is NOT trusted: NSE tags are often wrong (a results filing under
"General Updates", an order under "Press Release"). The kind is decided from the content.
"""
from __future__ import annotations

VERSION = 2
CHANGES = [
    "1 (2026-11-10): first fact list, flat, no per-type fields.",
    "2 (2026-11-10): filing kinds with summary notes; per-type detail fields; kinds decided from "
    "content; added shareholding_pattern, fund_utilisation, scheme, disruption, clarification.",
]

# --- filing kinds -----------------------------------------------------------------------------
# name: (what it is, what the summary must say, routine?)
KINDS: dict[str, tuple[str, str, bool]] = {
    "financial_results": (
        "quarterly / annual results, standalone and/or consolidated",
        "period; revenue, EBITDA and profit with year-on-year change; consolidated vs standalone; "
        "exceptional items; any auditor qualification or emphasis of matter", False),
    "board_outcome": (
        "outcome of a board meeting",
        "every decision taken (results, dividend, fund raise, appointments, acquisitions, capex), "
        "each with its number and date", False),
    "investor_presentation": (
        "investor / analyst presentation",
        "order book and inflow, guidance, segment mix, capacity and capex, anything new versus the "
        "last presentation", False),
    "earnings_call": (
        "earnings call transcript or summary",
        "management's guidance, order book, margin and demand commentary, capex, and the risks or "
        "doubts analysts pressed on -- with the numbers said", False),
    "press_release": (
        "press / media release",
        "the event announced, its size, counterparty and timing", False),
    "order": (
        "order, contract, LoA/LoI, work order, or lowest-bidder (L1) status",
        "customer, value, scope, execution period, domestic or export; say if it repeats an "
        "earlier announcement", False),
    "capital_raise": (
        "preferential issue, warrants, QIP, rights, IPO/OFS, ESOP allotment, debt raise",
        "instrument, size, price, who gets it (promoter or not), stage (proposed / approved / "
        "allotted), lock-in and purpose", False),
    "shareholding_change": (
        "promoter / insider / substantial-holder disclosures (SAST, PIT), pledges, shareholding pattern",
        "who, how many shares, holding before and after, mode (market, off-market, gift, pledge) "
        "and price", False),
    "corporate_action": (
        "dividend, bonus, split, buyback, record date",
        "the action, amount or ratio, record and payment dates", False),
    "acquisition_investment": (
        "acquisition, investment in or formation of a subsidiary / JV / associate, divestment, "
        "scheme of arrangement",
        "target, stake, consideration and valuation, funding, completion status and date", False),
    "capex_capacity": (
        "new plant, capacity expansion, commissioning, disruption (fire, shutdown, strike)",
        "project, capacity, cost, location, timeline, and effect on operations", False),
    "management_change": (
        "director or key managerial person appointed, re-appointed, resigned or ceased; auditor change",
        "who, which role, effective date, term, and the stated reason (quote it for a resignation)", False),
    "credit_rating": (
        "credit rating action",
        "agency, instrument and amount, rating and outlook, previous rating, upgrade / downgrade / "
        "reaffirm", False),
    "meeting_and_voting": (
        "AGM / EGM / postal ballot notice, scrutiniser report, voting results",
        "the resolutions and, for results, whether each passed and with what margin if close", False),
    "annual_report": (
        "annual report or business responsibility report",
        "the year's key numbers, order book, capex plans, related-party dealings, contingent "
        "liabilities, and what management says changed", False),
    "fund_utilisation": (
        "monitoring agency report, statement of deviation or variation",
        "money raised, used so far, and any deviation from the stated purpose", False),
    "legal_regulatory": (
        "litigation, tax demand, penalty, regulator or exchange action",
        "forum, counterparty, amount, what is claimed, and the company's stated impact", False),
    "clarification": (
        "reply to an exchange query or news verification, price-movement clarification",
        "what was asked and what the company said, including any denial", False),
    "analyst_meet_intimation": (
        "schedule of analyst / investor meetings (not the content)",
        "one sentence: dates and organisers", True),
    "newspaper_copy": (
        "copy of a newspaper advertisement (results ad, AGM notice, etc.)",
        "one sentence naming what was advertised", True),
    "trading_window": ("trading window closure / opening", "one sentence with the dates", True),
    "registrar_compliance": (
        "depository / RTA certificates (Reg 74(5)), lost or duplicate share certificates, investor "
        "complaints statement",
        "one sentence", True),
    "other": ("anything else", "what the filing discloses, with numbers and dates", False),
}

# --- fact types -------------------------------------------------------------------------------
# name: (what it is, {detail field: meaning}). Common columns (event_date, period, amount, pct,
# shares, price_inr, counterparty, person, role, direction) are listed per type in the prompt
# notes; the fields here go into `details`.
FACT_TYPES: dict[str, tuple[str, dict[str, str]]] = {
    "order_won": ("new order, contract, LoA/LoI, work order (counterparty = customer; amount = value)",
                  {"scope": "what is to be supplied or built", "execution_by": "completion date or period",
                   "market": "domestic / export", "segment": "business segment", "repeat": "yes if announced before"}),
    "order_book": ("order book / unexecuted orders as of a date (period = as-of date; amount = total)",
                   {"by_segment": "segment: amount, ...", "inflow_period": "order inflow in the period, with period"}),
    "l1_bid": ("declared lowest bidder (L1), not yet awarded (counterparty = client; amount = bid value)",
               {"tender": "tender name or number"}),
    "results": ("reported numbers for one period (period; amount = revenue)",
                {"basis": "consolidated / standalone", "ebitda_cr": "", "pat_cr": "profit after tax",
                 "eps": "", "revenue_yoy_pct": "", "pat_yoy_pct": "", "exceptional": "exceptional items"}),
    "guidance": ("management forecast (period = horizon)",
                 {"metric": "revenue / margin / order inflow / capex ...", "target": "the stated number or range"}),
    "preferential_issue": ("shares or CCPS on a preferential basis (counterparty = allottee; shares, price_inr, amount)",
                           {"allottee_category": "promoter / promoter group / investor / strategic",
                            "instrument": "equity / CCPS", "stage": "proposed / approved / allotted",
                            "lock_in": "", "purpose": ""}),
    "warrants": ("convertible warrants (counterparty = allottee; shares, price_inr, amount)",
                 {"allottee_category": "", "upfront_pct": "share paid upfront",
                  "stage": "proposed / approved / allotted / converted", "conversion_by": ""}),
    "public_issue": ("QIP, IPO, FPO, OFS or rights issue (amount = size; price_inr)",
                     {"route": "QIP / IPO / OFS / rights", "stage": "", "floor_price": "", "discount_pct": ""}),
    "debt_raise": ("loans, NCDs, CP, or other borrowing raised or repaid (amount)",
                   {"instrument": "", "rate": "interest / coupon", "tenor": "", "lender": "", "action": "raised / repaid"}),
    "esop": ("employee options granted, vested, exercised or allotted (shares, price_inr = exercise price)",
             {"scheme": "", "action": "grant / vest / allot"}),
    "buyback": ("share buyback (amount = size; price_inr; shares)", {"route": "tender / open market", "stage": ""}),
    "dividend": ("dividend (price_inr = per share)", {"kind": "interim / final / special", "record_date": ""}),
    "bonus_split": ("bonus issue or split", {"ratio": "", "record_date": ""}),
    "promoter_stake": ("promoter / founder holding change (person; shares; pct = holding after; price_inr)",
                       {"before_pct": "", "mode": "market / off-market / gift / inter-se / conversion / creeping",
                        "action": "buy / sell / transfer"}),
    "pledge": ("promoter pledge (person; shares; pct = share of holding pledged)",
               {"action": "create / release / invoke", "lender": "", "purpose": ""}),
    "insider_trade": ("director / KMP / designated person trade (person, role; shares; amount; direction)",
                      {"mode": ""}),
    "major_holder": ("non-promoter crossing or changing a disclosed holding (counterparty = holder; pct = after)",
                     {"before_pct": "", "mode": ""}),
    "shareholding_pattern": ("quarterly holding mix (period = quarter end; pct = promoter %)",
                             {"fii_pct": "", "dii_pct": "", "public_pct": "", "pledged_pct": ""}),
    "investment": ("money put into another entity or a stake taken (counterparty = investee; amount; pct)",
                   {"instrument": "", "purpose": "", "relation": "subsidiary / associate / third party"}),
    "acquisition": ("buying a company, business or asset (counterparty = target; amount = consideration; pct)",
                    {"valuation": "", "funding": "cash / shares / debt", "stage": "signed / completed",
                     "country": ""}),
    "divestment": ("selling a company, business, stake or asset (counterparty = buyer; amount; pct)",
                   {"asset": "", "stage": ""}),
    "subsidiary_jv": ("subsidiary / JV / associate formed, struck off or merged (counterparty = entity)",
                      {"partner": "", "country": "", "action": "formed / closed / merged"}),
    "scheme": ("merger, demerger, amalgamation or other scheme of arrangement",
               {"entities": "", "ratio": "share swap ratio", "stage": "board / NCLT / effective"}),
    "capex": ("capacity expansion, new plant, capex approved or commissioned (amount)",
              {"project": "", "capacity": "", "location": "", "timeline": "", "funding": "", "stage": ""}),
    "disruption": ("fire, accident, shutdown, strike, force majeure", {"site": "", "impact": "", "duration": ""}),
    "partnership": ("MoU, tie-up, licence, distribution or supply agreement without an order value "
                    "(counterparty)", {"scope": ""}),
    "product_approval": ("product launch, certification, licence or regulatory approval",
                         {"product": "", "authority": ""}),
    "credit_rating": ("rating action (counterparty = agency; amount = rated facility)",
                      {"instrument": "", "rating": "", "previous": "", "outlook": "",
                       "action": "upgrade / downgrade / reaffirm / assign / withdraw"}),
    "director_joined": ("director appointed or re-appointed (person, role)",
                        {"category": "independent / executive / non-executive / nominee", "term": ""}),
    "director_left": ("director resigned, retired, ceased or term ended (person, role)",
                      {"category": "", "reason": "the stated reason, quoted"}),
    "kmp_joined": ("CEO, CFO, CS or other senior manager appointed (person, role)", {}),
    "kmp_left": ("CEO, CFO, CS or other senior manager left (person, role)", {"reason": ""}),
    "auditor_change": ("auditor appointed, resigned or rotated (counterparty = new auditor)",
                       {"auditor_type": "statutory / secretarial / internal / cost", "previous": "", "reason": ""}),
    "fund_utilisation": ("use of money raised (amount = used so far)",
                         {"issue": "", "raised_cr": "", "deviation": "none or what changed"}),
    "litigation": ("legal case, arbitration, tax demand (counterparty; amount)",
                   {"forum": "", "status": "", "impact": "company's stated impact"}),
    "regulatory_action": ("regulator or exchange action, penalty, show-cause (counterparty = regulator; amount)",
                          {"action": "", "status": ""}),
    "related_party": ("material related-party transaction (counterparty; amount)", {"nature": ""}),
    "clarification": ("company's reply to a query or news report", {"asked": "", "answer": ""}),
    "other_material": ("any other fact an investor would care about", {}),
}

ROUTINE_KINDS = {k for k, (_, _, routine) in KINDS.items() if routine}


def prompt_kinds() -> str:
    return "\n".join(f"- {k}: {what}. Summary: {notes}." for k, (what, notes, _) in KINDS.items())


def prompt_facts() -> str:
    out = []
    for k, (what, fields) in FACT_TYPES.items():
        extra = "; details: " + ", ".join(f"{f} ({m})" if m else f for f, m in fields.items()) if fields else ""
        out.append(f"- {k}: {what}{extra}")
    return "\n".join(out)


def markdown() -> str:
    lines = ["# Announcement catalogue", "",
             "Generated from `stockey/infra/catalog.py` -- edit that file, not this one. "
             f"Version {VERSION}. **Expected to change** as new kinds of filings turn up; each change "
             "bumps the version and the records extracted under an older one are read again.", "",
             "The exchange's category is not trusted (NSE's tags are often wrong); the kind is decided "
             "from the filing's content.", "", "## Changes", ""] + [f"- {c}" for c in CHANGES]
    lines += ["", "## Filing kinds and how to summarise them", "", "| Kind | What it is | The summary says | Routine |",
              "|---|---|---|---|"]
    lines += [f"| `{k}` | {what} | {notes} | {'yes' if r else ''} |" for k, (what, notes, r) in KINDS.items()]
    lines += ["", "Routine kinds get a one-sentence summary and no facts.", "",
              "## Fact types", "",
              "Every fact has: type, one-sentence summary, event date, period, amount (as printed, with unit "
              "and currency; stored in Rs crore), pct, shares, price (Rs), counterparty, person, role, "
              "direction, details, and a verbatim quote. It is **verified** only when the quote is in the "
              "filing and every number given is in the quote.", "",
              "| Type | What it is | Details |", "|---|---|---|"]
    for k, (what, fields) in FACT_TYPES.items():
        det = ", ".join(f"`{f}`" + (f" ({m})" if m else "") for f, m in fields.items())
        lines.append(f"| `{k}` | {what} | {det} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(markdown(), end="")
