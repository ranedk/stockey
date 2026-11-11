"""Our line items from a parsed filing, per format family, plus the accounting checks
(docs/XBRL_FUNDAMENTALS.md, "The parser to build" 3-5).

A spec is a list of alternatives tried in order; an alternative is one tag, or a tuple of tags
summed (at least one must be present). Values stay in rupees as filed (EPS in rupees per share,
ratios as filed). A tag that is not mapped is not lost: the raw filing stays in S3 and a new
item is a new line here plus a MAPPING_VERSION bump.

Families: company, nbfc, bank, insurer. The old exchange listing used one taxonomy per family
(Ind-AS_entry_point, in-bse-fin, banking_entry_point); from 2025 all share in-capmkt-ent, so
the family is read from the tags present.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from fundamentals.xbrl.parse import Filing

MAPPING_VERSION = 2       # 2: excise_duty and revenue_net (duty filed as an 'other expenses' line)

Spec = list  # list[str | tuple[str, ...]]

PL_COMPANY: dict[str, Spec] = {
    "revenue": ["RevenueFromOperations"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income"],
    "expenses": ["Expenses"],
    "materials": [("CostOfMaterialsConsumed", "PurchasesOfStockInTrade",
                   "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade")],
    "employee": ["EmployeeBenefitExpense"],
    "finance_costs": ["FinanceCosts"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense"],
    "other_expenses": ["OtherExpenses"],
    "pbt_before_exceptional": ["ProfitBeforeExceptionalItemsAndTax"],
    "exceptional": ["ExceptionalItemsBeforeTax"],
    "pbt": ["ProfitBeforeTax"],
    "tax": ["TaxExpense"],
    "pat_continuing": ["ProfitLossForPeriodFromContinuingOperations"],
    "pat": ["ProfitLossForPeriod"],
    "pat_owners": ["ProfitOrLossAttributableToOwnersOfParent"],
    "share_of_associates": ["ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod"],
    "eps": ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "BasicEarningsLossPerShareFromContinuingOperations"],
}
PL_NBFC = {**PL_COMPANY, "interest_income": ["InterestEarned"],
           "fee_income": ["FeesAndCommissionIncome"]}
PL_BANK: dict[str, Spec] = {
    "revenue": ["InterestEarned"],                      # what a bank's "sales" means
    "interest_expended": ["InterestExpended"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income"],
    "operating_expenses": ["OperatingExpenses"],
    "employee": ["EmployeeBenefitExpense"],
    "expenses": ["ExpenditureExcludingProvisionsAndContingencies"],
    "operating_profit": ["OperatingProfitBeforeProvisionAndContingencies"],
    "provisions": ["ProvisionsOtherThanTaxAndContingencies"],
    "pbt": ["ProfitLossFromOrdinaryActivitiesBeforeTax"],
    "tax": ["TaxExpense"],
    "pat_after_tax": ["ProfitLossFromOrdinaryActivitiesAfterTax"],
    "pat": ["ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates", "ProfitLossForThePeriod"],
    "eps": ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "BasicEarningsPerShareBeforeExtraordinaryItems", "BasicEarningsPerShareAfterExtraordinaryItems"],
    "gross_npa": ["GrossNonPerformingAssets"],
    "net_npa": ["NonPerformingAssets"],
    "gross_npa_pct": ["PercentageOfGrossNpa"],
    "net_npa_pct": ["PercentageOfNpa"],
    "cet1_ratio": ["CET1Ratio"],
}
PL_INSURER: dict[str, Spec] = {
    "revenue": ["GrossPremiumIncome", "GrossPremiumsWritten", "NetPremiumIncome", "NetPremiumWritten"],  # premium
    "net_premium": ["NetPremiumIncome", "NetPremiumWritten"],
    "investment_income": ["IncomeFromInvestmentsNet"],
    "claims": ["IncurredClaims", "ClaimsPaid"],
    "commission": ["NetCommission", "CommissionsAndBrokerageNet", "Commission"],
    "total_income": ["Income"],                           # includes investment income: swings with markets
    "pbt": ["ProfitLossBeforeTax"],
    "pat": ["ProfitLossAfterTaxAndExtraordinaryItems", "ProfitLossAfterTaxAndExtraordinaryItem", "ProfitLossAfterTax"],
    "combined_ratio": ["CombinedRatio"],
    "eps": ["BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"],
}

BS_COMPANY: dict[str, Spec] = {
    "total_assets": ["Assets"],
    "equity_and_liabilities": ["EquityAndLiabilities"],
    "liabilities": ["Liabilities"],
    "total_equity": ["Equity"],
    "equity_owners": ["EquityAttributableToOwnersOfParent"],
    "minority": ["NonControllingInterest"],
    "share_capital": ["EquityShareCapital"],
    "borrowings": [("BorrowingsCurrent", "BorrowingsNoncurrent"), "Borrowings"],
    "cash": ["CashAndCashEquivalents"],
    "current_assets": ["CurrentAssets"],
    "current_liabilities": ["CurrentLiabilities"],
    "inventories": ["Inventories"],
    "receivables": [("TradeReceivablesCurrent", "TradeReceivablesNoncurrent")],
    "payables": [("TradePayablesCurrent", "TradePayablesNoncurrent"),
                 ("TotalOutstandingDuesOfMicroEnterpriseAndSmallEnterpriseCurrent",
                  "TotalOutstandingDuesOfCreditorsOtherThanMicroEnterpriseAndSmallEnterpriseCurrent",
                  "TotalOutstandingDuesOfMicroEnterpriseAndSmallEnterpriseNoncurrent",
                  "TotalOutstandingDuesOfCreditorsOtherThanMicroEnterpriseAndSmallEnterpriseNoncurrent")],
    "ppe": ["PropertyPlantAndEquipment"],
    "cwip": ["CapitalWorkInProgress"],
    "goodwill": ["Goodwill"],
    "investments": [("CurrentInvestments", "NoncurrentInvestments"), "Investments"],
}
BS_NBFC = {**BS_COMPANY, "loans": [("LoansCurrent", "LoansNoncurrent"), "Loans"], "deposits": ["Deposits"]}
BS_BANK: dict[str, Spec] = {
    "total_assets": ["Assets"],
    "capital_and_liabilities": ["CapitalAndLiabilities"],
    "share_capital": ["Capital"],
    "advances": ["Advances"],
    "deposits": ["Deposits"],
    "borrowings": ["Borrowings"],
    "investments": ["Investments"],
}
BS_INSURER: dict[str, Spec] = {"total_assets": ["Assets"], "investments": ["Investments"],
                               "share_capital": ["PaidUpEquityShareCapital", "PaidUpEquityCapital"]}

CF: dict[str, Spec] = {
    "cfo": ["CashFlowsFromUsedInOperatingActivities"],
    "cfi": ["CashFlowsFromUsedInInvestingActivities"],
    "cff": ["CashFlowsFromUsedInFinancingActivities"],
    "capex": [("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
               "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities")],
    "dividends_paid": ["DividendsPaidClassifiedAsFinancingActivities"],
    "net_change_cash_before_fx": ["IncreaseDecreaseInCashAndCashEquivalentsBeforeEffectOfExchangeRateChanges"],
    "net_change_cash": ["IncreaseDecreaseInCashAndCashEquivalents"],
}

FAMILY_MAPS = {
    "company": {**PL_COMPANY, **BS_COMPANY, **CF},
    "nbfc": {**PL_NBFC, **BS_NBFC, **CF},
    "bank": {**PL_BANK, **BS_BANK, **CF},
    "insurer": {**PL_INSURER, **BS_INSURER, **CF},
}
PER_SHARE = {"eps"}
RATIO = {"gross_npa_pct", "net_npa_pct", "cet1_ratio", "combined_ratio"}
# what screener.in calls "sales", for the 2-5% cross-check (insurers: total income, not premium)
SCREENER_SALES = {"company": "revenue_net", "nbfc": "revenue_net", "bank": "revenue", "insurer": "total_income"}
# Excise duty and GST are often filed inside revenue and then itemised as an "other expenses"
# line ("Excise duty on sales", "GST"); screener.in's sales is net of them (SULA, PREMIERPOL).
DUTY_LINE = re.compile(r"excise|\bgst\b|goods\s+and\s+services\s+tax", re.I)
_INSURER_TAGS = {"GrossPremiumIncome", "GrossPremiumsWritten", "NetPremiumIncome", "NetPremiumWritten"}


def family(f: Filing) -> str:
    tags = {x.tag for x in f.facts}
    if f.taxonomy.startswith("banking"):
        return "bank"
    if tags & _INSURER_TAGS:
        return "insurer"
    if "InterestEarned" in tags and ({"Advances", "Deposits"} <= tags or "InterestExpended" in tags):
        return "bank"
    if f.taxonomy == "in-bse-fin" or "InterestEarned" in tags:
        return "nbfc"
    return "company"


@dataclass(frozen=True)
class Row:
    item: str
    kind: str               # q (a quarter), h1, 9m, fy (year to date), i (an instant)
    start: date | None
    end: date
    value: float
    tag: str                # the tag(s) it came from
    context: str


def kind_of(start: date | None, end: date | None, instant: date | None) -> str | None:
    if instant:
        return "i"
    if not (start and end):
        return None
    months = round(((end - start).days + 1) / 30.44)
    return {3: "q", 6: "h1", 9: "9m", 12: "fy"}.get(months)


def _value(vals: dict[str, float], spec: Spec) -> tuple[float, str] | None:
    for alt in spec:
        if isinstance(alt, tuple):
            got = [(t, vals[t]) for t in alt if t in vals]
            if got:
                return sum(v for _, v in got), "+".join(t for t, _ in got)
        elif alt in vals:
            return vals[alt], alt
    return None


def extract(f: Filing) -> tuple[str, list[Row]]:
    fam = family(f)
    items = FAMILY_MAPS[fam]
    rows: dict[tuple[str, str, date], Row] = {}
    for cid, vals in f.plain().items():
        c = f.contexts.get(cid)
        k = kind_of(c.start, c.end, c.instant) if c else None
        if k is None:
            continue
        end = c.instant or c.end
        for item, spec in items.items():
            got = _value(vals, spec)
            if got is not None and (item, k, end) not in rows:   # Q1: quarter and YTD are the same
                rows[(item, k, end)] = Row(item, k, None if k == "i" else c.start, end, got[0], got[1], cid)
    if fam in ("company", "nbfc"):
        _net_revenue(f, rows)
    return fam, list(rows.values())


def _net_revenue(f: Filing, rows: dict) -> None:
    """excise_duty: the itemised other-expense lines described as excise or GST, per period;
    revenue_net: revenue less that (revenue itself when there is no such line)."""
    desc = {x.context: x.text for x in f.facts if x.tag == "DescriptionOfOtherExpenses" and x.text}
    duty: dict[tuple[str, date], tuple[float, str]] = {}
    for x in f.facts:
        c = f.contexts.get(x.context)
        if x.tag != "OtherExpenses" or x.value is None or c is None or not c.dims \
                or not DUTY_LINE.search(desc.get(x.context, "")):
            continue
        # the line's own context can carry the declared (wrong) dates; its main context's are fixed
        base = f.contexts.get(next((b for b in ("OneD", "FourD") if x.context.startswith(b[:-1])), ""), c)
        k = kind_of(base.start, base.end, base.instant)
        c = base
        if k and k != "i":
            v, _ = duty.get((k, c.end), (0.0, x.context))
            duty[(k, c.end)] = (v + x.value, x.context)
    for (item, k, end), r in list(rows.items()):
        if item != "revenue":
            continue
        d, cid = duty.get((k, end), (0.0, None))
        if cid:
            rows[("excise_duty", k, end)] = Row("excise_duty", k, r.start, end, d, "OtherExpenses", cid)
        rows[("revenue_net", k, end)] = Row("revenue_net", k, r.start, end, r.value - d, r.tag, r.context)


# --- checks --------------------------------------------------------------------------------

TOL_REL = 0.005          # half a percent of the larger side ...
TOL_ABS = 2e5            # ... or Rs 2 lakh, whichever is larger (rounding in small companies)

CHECKS = {
    "company": [("total_income = revenue + other_income", "total_income", ("revenue", "other_income"), ()),
                ("pbt_before_exceptional = total_income - expenses", "pbt_before_exceptional", ("total_income",), ("expenses",)),
                ("pat_continuing = pbt - tax", "pat_continuing", ("pbt",), ("tax",)),
                ("total_assets = equity_and_liabilities", "total_assets", ("equity_and_liabilities",), ()),
                ("total_assets = total_equity + liabilities", "total_assets", ("total_equity", "liabilities"), ()),
                ("net_change_cash_before_fx = cfo + cfi + cff", "net_change_cash_before_fx", ("cfo", "cfi", "cff"), ())],
    "bank": [("total_income = revenue + other_income", "total_income", ("revenue", "other_income"), ()),
             ("operating_profit = total_income - expenses", "operating_profit", ("total_income",), ("expenses",)),
             ("pat_after_tax = pbt - tax", "pat_after_tax", ("pbt",), ("tax",)),
             ("total_assets = capital_and_liabilities", "total_assets", ("capital_and_liabilities",), ()),
             ("net_change_cash_before_fx = cfo + cfi + cff", "net_change_cash_before_fx", ("cfo", "cfi", "cff"), ())],
    "insurer": [],
}
CHECKS["nbfc"] = CHECKS["company"]


def checks(fam: str, rows: list[Row]) -> list[dict]:
    """Each identity, for every period it can be tested on: ok / fail with the gap."""
    by = {}
    for r in rows:
        by.setdefault((r.kind, r.end), {})[r.item] = r.value
    out = []
    for (k, end), v in sorted(by.items(), key=lambda x: (x[0][1], x[0][0])):
        for name, lhs, plus, minus in CHECKS[fam]:
            if lhs not in v or not all(p in v for p in plus) or not all(m in v for m in minus):
                continue
            rhs = sum(v[p] for p in plus) - sum(v[m] for m in minus)
            gap = v[lhs] - rhs
            ok = abs(gap) <= max(TOL_ABS, TOL_REL * max(abs(v[lhs]), abs(rhs)))
            out.append({"check": name, "kind": k, "end": end.isoformat(), "ok": ok, "gap": round(gap, 2)})
    return out
