"""Metric catalogue, first cut (PLATFORM_PRD.md task S2). Every field a list or screen may use:
label, group, unit, source, licence tag. Units: inr (rupees), pct (fraction shown as %),
x (multiple), days, num, text. Licence tags: `nse_eod` (NSE end-of-day; display licence
decided before open beta), `own_xbrl` (our parse of exchange filings), `own_derived`
(computed by us from the others), `systrader` (our price analysis), `exchange_ref`
(exchange reference data)."""
from __future__ import annotations

F = lambda label, group, unit, licence, desc="": {"label": label, "group": group, "unit": unit,
                                                    "licence": licence, "desc": desc}

FIELDS: dict[str, dict] = {
    "symbol": F("Symbol", "Identity", "text", "exchange_ref"),
    "name": F("Company", "Identity", "text", "exchange_ref"),
    "series": F("Series", "Identity", "text", "exchange_ref", "EQ/BE main board; SM/ST SME"),
    "isin": F("ISIN", "Identity", "text", "exchange_ref"),
    "macro_sector": F("Macro sector", "Classification", "text", "exchange_ref"),
    "sector": F("Sector", "Classification", "text", "exchange_ref"),
    "industry": F("Industry", "Classification", "text", "exchange_ref"),
    "basic_industry": F("Basic industry", "Classification", "text", "exchange_ref"),
    "size_band": F("Size", "Classification", "text", "own_derived", "Large: top 100 by market cap; Mid: 101-250; Small: 251-500; Micro: the rest"),
    "tags": F("Tags", "Classification", "tags", "own_derived"),
    "mcap": F("Market cap", "Size", "inr", "nse_eod"),
    "mcap_rank": F("Market-cap rank", "Size", "num", "own_derived"),
    "price": F("Price", "Price", "inr", "nse_eod", "Last close"),
    "price_date": F("Price date", "Price", "date", "nse_eod"),
    "ret_1d": F("1D", "Returns", "pct", "own_derived"),
    "ret_1w": F("1W", "Returns", "pct", "own_derived"),
    "ret_1m": F("1M", "Returns", "pct", "own_derived"),
    "ret_3m": F("3M", "Returns", "pct", "own_derived"),
    "ret_6m": F("6M", "Returns", "pct", "own_derived"),
    "ret_1y": F("1Y", "Returns", "pct", "own_derived"),
    "high_52w": F("52W high", "Price", "inr", "own_derived"),
    "low_52w": F("52W low", "Price", "inr", "own_derived"),
    "from_high_52w": F("From 52W high", "Technical", "pct", "own_derived"),
    "dma50_gap": F("vs 50-day avg", "Technical", "pct", "own_derived"),
    "dma200_gap": F("vs 200-day avg", "Technical", "pct", "own_derived"),
    "volatility_3m": F("Volatility (3M, yearly)", "Technical", "pct", "own_derived"),
    "traded_value_20d": F("Avg traded value (20D)", "Liquidity", "inr", "nse_eod"),
    "stage": F("Trend stage", "Technical", "num", "systrader", "Weinstein stage: 1 basing, 2 advancing, 3 topping, 4 declining"),
    "stage_label": F("Trend stage", "Technical", "text", "systrader"),
    "sales": F("Sales (TTM)", "Financials", "inr", "own_xbrl"),
    "profit": F("Net profit (TTM)", "Financials", "inr", "own_xbrl", "Attributable to owners where filed"),
    "ebitda": F("EBITDA (TTM)", "Financials", "inr", "own_xbrl"),
    "opm": F("Operating margin", "Profitability", "pct", "own_xbrl", "EBITDA / sales, last 12 months"),
    "npm": F("Net margin", "Profitability", "pct", "own_xbrl"),
    "roe": F("ROE", "Profitability", "pct", "own_xbrl", "Profit / average equity, last 12 months"),
    "roce": F("ROCE", "Profitability", "pct", "own_xbrl", "EBIT / average (equity + debt)"),
    "sales_yoy": F("Sales growth (TTM YoY)", "Growth", "pct", "own_xbrl"),
    "profit_yoy": F("Profit growth (TTM YoY)", "Growth", "pct", "own_xbrl"),
    "q_sales_yoy": F("Sales growth (last quarter YoY)", "Growth", "pct", "own_xbrl"),
    "q_profit_yoy": F("Profit growth (last quarter YoY)", "Growth", "pct", "own_xbrl"),
    "sales_cagr3": F("Sales CAGR 3Y", "Growth", "pct", "own_xbrl"),
    "profit_cagr3": F("Profit CAGR 3Y", "Growth", "pct", "own_xbrl"),
    "debt_equity": F("Debt / equity", "Balance sheet", "x", "own_xbrl"),
    "interest_cover": F("Interest cover", "Balance sheet", "x", "own_xbrl"),
    "cfo": F("Operating cash flow (TTM)", "Cash flow", "inr", "own_xbrl"),
    "fcf": F("Free cash flow (TTM)", "Cash flow", "inr", "own_xbrl"),
    "cash_conversion": F("Cash conversion", "Cash flow", "x", "own_xbrl", "Operating cash flow / profit"),
    "pe": F("P/E", "Valuation", "x", "own_derived"),
    "pb": F("P/B", "Valuation", "x", "own_derived"),
    "ps": F("Price / sales", "Valuation", "x", "own_derived"),
    "ev_ebitda": F("EV / EBITDA", "Valuation", "x", "own_derived"),
    "dividend_yield": F("Dividend yield", "Valuation", "pct", "own_derived", "Dividends paid last year / market cap"),
    "fin_period": F("Financials as of", "Financials", "date", "own_xbrl"),
    "basis": F("Basis", "Financials", "text", "own_xbrl", "consolidated or standalone"),
    "family": F("Company type", "Financials", "text", "own_xbrl", "company, nbfc, bank, insurer"),
    "spark": F("Price (60D)", "Price", "spark", "nse_eod"),
}

NUMERIC = {k for k, v in FIELDS.items() if v["unit"] in ("inr", "pct", "x", "days", "num")}

# One line per tag: the exact rule (serving/company.py tags()).
TAG_RULES = {
    "SME": "Listed on the NSE SME platform",
    "Loss-making": "Net profit over the last 12 months is negative",
    "Debt-free": "Borrowings at most 5% of equity",
    "High debt": "Debt / equity above 1.5",
    "High ROCE": "Return on capital employed of 20% or more",
    "Fast growth": "Sales and profit both up 25%+ over the last 12 months",
    "Sales falling": "Last quarter's sales down more than 10% from a year earlier",
    "Cash burn": "Profitable but operating cash flow negative over the last 12 months",
    "Near 52w high": "Within 5% of the 52-week high",
    "Far below high": "40% or more below the 52-week high",
    "Uptrend": "Weinstein stage 2 (advancing)",
    "Downtrend": "Weinstein stage 4 (declining)",
    "High dividend": "Dividend yield of 3% or more",
}
