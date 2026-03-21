from __future__ import annotations

from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class QuarterMonth(str, Enum):
    MARCH = "mar"
    JUNE = "jun"
    SEPTEMBER = "sep"
    DECEMBER = "dec"


class Timeframe(str, Enum):
    QUARTER_ENDED = "QuarterEnded"
    HALF_YEAR_ENDED = "HalfYearEnded"
    NINE_MONTHS_ENDED = "NineMonthsEnded"
    YEAR_ENDED = "YearEnded"


class RupeeUnit(str, Enum):
    RUPEES = "in rs"
    THOUSANDS = "rs in thousands"
    LACS = "rs in lacs"
    CRORES = "rs in crores"
    TEN_CRORES = "rs in ten crores"
    HUNDRED_CRORES = "rs in hundred crores"
    THOUSAND_CRORES = "rs in thousand crores"


class Breakup(BaseModel):
    finance_term: str = Field(strict=True, description="financial term mentioned in the document")
    value: float = Field(strict=True, description="value of the finance term")


class OrderBreakup(BaseModel):
    order_title: str = Field(strict=True, description="Title for the order")
    order_details: str = Field(strict=True, description="Details of the order")
    unit_of_rupees: RupeeUnit = Field(
        strict=True,
        description="unit of rupees used in the document, if not provided use 'in rs'",
    )
    order_value: float = Field(strict=True, description="Order value in rupees")
    months_to_execute: int = Field(strict=True, description="Number of months to execute the Order")


class ArbitrationAward(BaseModel):
    unit_of_rupees: RupeeUnit
    arbitration_amount: float
    arbitration_expected: float
    arbitration_payment_expected: float
    arbitration_title: str


class BalanceSheetAssets(BaseModel):
    unit_of_rupees: RupeeUnit
    month: QuarterMonth
    year: int
    accounts_receivable: float
    cash_and_cash_equivalents: float
    deferred_tax_assets: float
    intangible_assets: float
    intangible_assets_breakup: List[Breakup]
    inventory: float
    long_term_investments: float
    other_assets: float
    other_current_assets: float
    other_non_current_assets: float
    prepaid_expenses: float
    property_plant_and_equipment: float
    property_plant_and_equipment_breakup: List[Breakup]
    short_term_investments: float
    total_current_assets: float
    total_current_assets_breakup: List[Breakup]
    total_non_current_assets: float
    total_non_current_assets_breakup: List[Breakup]


class BalanceSheetEquity(BaseModel):
    unit_of_rupees: RupeeUnit
    month: QuarterMonth
    year: int
    additional_paid_in_capital: float
    non_controlling_interest: float
    other_comprehensive_income: float
    other_equity: float
    retained_earnings: float
    share_capital: float
    total_equity: float
    total_equity_breakup: List[Breakup]
    treasury_stock: float


class BalanceSheetLiabilities(BaseModel):
    unit_of_rupees: RupeeUnit
    month: QuarterMonth
    year: int
    accounts_payable: float
    accrued_expenses: float
    current_tax_payable: float
    deferred_revenue: float
    deferred_tax_liabilities: float
    long_term_debt: float
    other_current_liabilities: float
    other_liabilities: float
    other_non_current_liabilities: float
    pension_liabilities: float
    short_term_debt: float
    total_current_liabilities: float
    total_current_liabilities_breakup: List[Breakup]
    total_non_current_liabilities: float
    total_non_current_liabilities_breakup: List[Breakup]


class CashFlow(BaseModel):
    unit_of_rupees: RupeeUnit
    month: QuarterMonth
    year: int
    acquisition_companies: float
    cash_paid_inventory: float
    cash_paid_suppliers: float
    cash_received_customers: float
    closing_cash: float
    dividends_paid: float
    interest_paid: float
    interest_received: float
    net_change_cash: float
    opening_cash: float
    other_financing_cash_flows: float
    other_investing_cash_flows: float
    other_operating_cash_flows: float
    period: Timeframe
    proceeds_borrowings: float
    proceeds_issuing_shares: float
    purchase_fixed_assets: float
    purchase_investments: float
    redemption_debentures: float
    repayment_borrowings: float
    sale_fixed_assets: float
    sale_investments: float
    share_application_money: float
    share_buybacks: float
    taxes_paid: float
    total_cash_financing_activities: float
    total_cash_financing_activities_breakup: List[Breakup]
    total_cash_investing_activities: float
    total_cash_investing_activities_breakup: List[Breakup]
    total_cash_operating_activities: float
    total_cash_operating_activities_breakup: List[Breakup]


class DebtCapital(BaseModel):
    unit_of_rupees: RupeeUnit
    expected_payoff: float
    interest_cost_reduction: float
    ltb_amount: float
    ltb_finance_cost: float
    ltb_interest_rate: float
    ltb_reason: str
    stb_amount: float
    stb_interest_rate: float
    stb_reason: str
    stb_required: bool
    wc_amount_change: float
    wc_change_reason: str
    wc_interest_cost: float
    wc_interest_rate: float
    working_capital_change: bool


class EmployeeCount(BaseModel):
    unit_of_rupees: RupeeUnit
    planned_emp_increase: float
    recent_emp_increase: float


class FundRaise(BaseModel):
    unit_of_rupees: RupeeUnit
    fund_raise_amount: float
    fund_raise_parties: str
    fund_raise_planned: bool
    fund_raise_timeline: float
    fund_raise_type: str
    promoter_participation: bool


class Guidance(BaseModel):
    unit_of_rupees: RupeeUnit
    capacity_expansion_status: str
    capacity_increase: float
    capacity_utilization: float
    current_order_book_value: float
    current_order_breakup: List[OrderBreakup]
    ebitda_margin_next_quarter: float
    ebitda_margin_current_year: float
    ebitda_margin_next_year: float
    ebitda_margin_year_after_next: float
    ebitda_margin_trend: float
    ebitda_margin_trend_reason: str
    execution_timeline_months: float
    long_term_revenue_guidance: str
    margin_trend: str
    margin_trend_reason: str
    new_capacity_date: float
    new_orders_next_quarter: float
    new_orders_current_year: float
    new_orders_next_year: float
    new_orders_year_after_next: float
    operating_margin_next_quarter: float
    operating_margin_current_year: float
    operating_margin_next_year: float
    operating_margin_year_after_next: float
    orders_next_quarter: float
    orders_current_year: float
    orders_next_year: float
    orders_year_after_next: float
    pat_margin_next_quarter: float
    pat_margin_current_year: float
    pat_margin_next_year: float
    pat_margin_year_after_next: float
    pat_margin_trend: float
    pat_margin_trend_reason: str
    revenue_next_quarter: float
    revenue_current_year: float
    revenue_next_year: float
    revenue_year_after_next: float
    revenue_from_capacity: float
    revenue_growth: float


class OrderBook(BaseModel):
    unit_of_rupees: RupeeUnit
    orders: List[OrderBreakup]


class Pnl(BaseModel):
    unit_of_rupees: RupeeUnit
    month: QuarterMonth
    year: int
    depreciation: float
    employee_expenses: float
    eps_basic: float
    eps_diluted: float
    interest: float
    net_profit: float
    net_profit_breakup: List[Breakup]
    other_expenses: float
    other_income: float
    period: Timeframe
    profit_before_tax: float
    total_expenses: float
    total_expenses_breakup: List[Breakup]
    total_revenue: float
    total_revenue_breakup: List[Breakup]
    total_tax: float
    total_tax_breakup: List[Breakup]


class EACategoryChoices:
    UNDEFINED = "UNDEFINED"
    IGNORE = "IGNORE"
    INTIMATION_MEETING = "INTIMATION_MEETING"
    EARNINGS_CALL = "EARNINGS_CALL"
    WORK_ORDER_CONTRACT = "WORK_ORDER_CONTRACT"
    L1_BIDDER = "L1_BIDDER"
    INVESTOR_PRESENTATION = "INVESTOR_PRESENTATION"
    CREDIT_RATING = "CREDIT_RATING"
    DIVIDEND = "DIVIDEND"
    QUARTER_FINANCIAL_RESULT = "QUARTER_FINANCIAL_RESULT"
    ANNUAL_FINANCIAL_RESULT = "ANNUAL_FINANCIAL_RESULT"
    ANNUAL_REPORT = "ANNUAL_REPORT"
    PERSONNEL_CHANGE = "PERSONNEL_CHANGE"
    ACQUISITION_OF_PROPERTY = "ACQUISITION_OF_PROPERTY"
    ACQUISITION_OF_COMPANY = "ACQUISITION_OF_COMPANY"
    ALLOTMENT_OF_DEBENTURES = "ALLOTMENT_OF_DEBENTURES"
    CHANGE_IN_RTA = "CHANGE_IN_RTA"
    AMALGAMATION = "AMALGAMATION"
    PREFERENTIAL_ISSUE = "PREFERENTIAL_ISSUE"
    BONUS = "BONUS"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    QIP = "QIP"
    ALLOTMENT_OF_SHARES = "ALLOTMENT_OF_SHARES"
    BUYBACK = "BUYBACK"
    HALF_YEARLY_FINANCIAL_RESULT = "HALF_YEARLY_FINANCIAL_RESULT"
    SHAREHOLDING = "SHAREHOLDING"
    LOAN_DEFAULT = "LOAN_DEFAULT"
    SAST = "SAST"


MODEL_TYPE_MAP = {
    ArbitrationAward: "Object",
    BalanceSheetAssets: "Object",
    BalanceSheetEquity: "Object",
    BalanceSheetLiabilities: "Object",
    CashFlow: "Object",
    DebtCapital: "Object",
    EmployeeCount: "Object",
    FundRaise: "Object",
    Guidance: "Future-Quarterly",
    OrderBook: "Object",
    Pnl: "Object",
}


DOCUMENT_PYDANTIC_MAP = {
    EACategoryChoices.ANNUAL_FINANCIAL_RESULT: [
        Pnl,
        BalanceSheetAssets,
        BalanceSheetLiabilities,
        BalanceSheetEquity,
        CashFlow,
    ],
    EACategoryChoices.QUARTER_FINANCIAL_RESULT: [Pnl],
    EACategoryChoices.EARNINGS_CALL: [Guidance, ArbitrationAward, DebtCapital],
    EACategoryChoices.ANNUAL_REPORT: [OrderBook, DebtCapital],
    EACategoryChoices.WORK_ORDER_CONTRACT: [OrderBook],
    EACategoryChoices.QIP: [FundRaise],
    EACategoryChoices.PREFERENTIAL_ISSUE: [FundRaise],
    EACategoryChoices.RIGHTS_ISSUE: [FundRaise],
    EACategoryChoices.CREDIT_RATING: [DebtCapital],
}
