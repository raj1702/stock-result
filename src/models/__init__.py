from typing import TypedDict

class StockData(TypedDict):
    symbol: str
    pe_ratio: float
    market_cap_crore: float
    market_cap_source: str
    profit_margin: float
    profit_margin_yoy_change: float
    yoy_revenue: float
    yoy_profit: float
    operating_cash_flow_yoy: float
    free_cash_flow_yoy: float
    net_debt_yoy: float
    debt_to_equity: float
    loan_book_yoy: float
    interest_income_yoy: float

class StockMetrics(TypedDict):
    average_pe: float
    average_profit_margin: float
    total_yoy_revenue: float
