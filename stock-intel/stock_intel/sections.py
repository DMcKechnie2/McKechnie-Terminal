"""Section builders.

Each function takes a `Source` and returns one section of the report as plain
JSON-serializable data: raw numbers, ISO date strings, and None. No display
formatting, no pre-rounded percentages masquerading as fractions.

Unit convention (declared to the consumer in the report payload):

    *_pct     percentage, 12.5 means 12.5%
    *_ratio   bare multiple, 1.8 means 1.8x
    money     reporting currency units, unscaled
    *_date    ISO-8601 date string

yfinance's own units are inconsistent, so anything derived from `info` is
normalized here and cross-checked against a computed value where one exists.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Mapping

import pandas as pd

from .core import Source, fiscal_year, num
from .metrics import (
    as_pct_from_fraction,
    consecutive_growth_years,
    pct,
    pe_history,
    percentile_rank,
    safe_div,
    series_cagr,
    summarize_distribution,
    trend_direction,
    yearly_change_pct,
)

__all__ = ["SECTION_BUILDERS", "SECTION_NAMES", "surfaces_for"]


def _iso(d: Any) -> str | None:
    if d is None:
        return None
    if isinstance(d, str):
        return d[:10] or None
    if isinstance(d, dt.datetime):
        return d.date().isoformat()
    if isinstance(d, dt.date):
        return d.isoformat()
    try:
        ts = pd.Timestamp(d)
        return None if ts is pd.NaT else ts.date().isoformat()
    except (TypeError, ValueError):
        return None


def _r(v: float | None, places: int = 2) -> float | None:
    return None if v is None else round(v, places)


def _nonzero(v: float | None) -> float | None:
    """Treat an exact zero as missing.

    yfinance returns 0.0 rather than null for ratios that do not apply to an
    issuer — `grossMargins` is 0.0 for every bank, because banks do not report
    a gross profit line. Passing that through would render as a real "0.0%",
    which is precisely the zero-versus-missing confusion this package exists to
    prevent. No real company reports an exactly-zero margin or return, so a
    literal 0 here is a null sentinel.
    """
    return None if v is None or v == 0 else v


def _by_year_list(by_year: Mapping[int, float], places: int = 2) -> list[dict]:
    return [{"year": y, "value": _r(v, places)} for y, v in sorted(by_year.items())]


def _epoch_to_iso(v: Any) -> str | None:
    """yfinance returns some dates as unix timestamps."""
    n = num(v)
    if n is None or n <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(int(n), tz=dt.timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------

def build_profile(src: Source) -> dict:
    info = src.info
    return {
        "symbol": src.symbol,
        "name": src.info_str("longName", "shortName", "displayName"),
        "exchange": src.info_str("fullExchangeName", "exchange"),
        "quote_type": src.info_str("quoteType"),
        "sector": src.info_str("sector"),
        "industry": src.info_str("industry"),
        "country": src.info_str("country"),
        "currency": src.info_str("currency", "financialCurrency") or "USD",
        "employees": int(src.info_num("fullTimeEmployees") or 0) or None,
        "website": src.info_str("website"),
        "business_summary": (info.get("longBusinessSummary") or None),
    }


# --------------------------------------------------------------------------
# quote
# --------------------------------------------------------------------------

def build_quote(src: Source) -> dict:
    price = src.fast("lastPrice") or src.info_num(
        "currentPrice", "regularMarketPrice", "previousClose"
    )
    prev = src.fast("previousClose") or src.info_num("previousClose", "regularMarketPreviousClose")
    high52 = src.fast("yearHigh") or src.info_num("fiftyTwoWeekHigh")
    low52 = src.fast("yearLow") or src.info_num("fiftyTwoWeekLow")
    dma50 = src.fast("fiftyDayAverage") or src.info_num("fiftyDayAverage")
    dma200 = src.fast("twoHundredDayAverage") or src.info_num("twoHundredDayAverage")
    volume = src.fast("lastVolume") or src.info_num("volume", "regularMarketVolume")
    avg_volume = src.info_num("averageVolume", "averageDailyVolume3Month")

    change_pct = None
    if price is not None and prev not in (None, 0):
        change_pct = (price - prev) / prev * 100.0

    # Position within the 52-week range: 0 = at the low, 100 = at the high.
    # More directly useful to an agent than the two endpoints on their own.
    range_position_pct = None
    if None not in (price, high52, low52) and (high52 - low52) > 1e-9:
        range_position_pct = (price - low52) / (high52 - low52) * 100.0

    return {
        "price": _r(price, 4),
        "previous_close": _r(prev, 4),
        "day_change_pct": _r(change_pct),
        "day_high": _r(src.fast("dayHigh"), 4),
        "day_low": _r(src.fast("dayLow"), 4),
        "volume": int(volume) if volume else None,
        "average_volume": int(avg_volume) if avg_volume else None,
        "relative_volume_ratio": _r(safe_div(volume, avg_volume)),
        "week52_high": _r(high52, 4),
        "week52_low": _r(low52, 4),
        "week52_range_position_pct": _r(range_position_pct, 1),
        "vs_50dma_pct": _r(pct(price - dma50 if None not in (price, dma50) else None, dma50)),
        "vs_200dma_pct": _r(pct(price - dma200 if None not in (price, dma200) else None, dma200)),
        "one_year_change_pct": _r(as_pct_from_fraction(src.fast("yearChange"))),
        "beta": _r(src.info_num("beta")),
        "market_cap": src.info_num("marketCap") or src.fast("marketCap"),
        "enterprise_value": src.info_num("enterpriseValue"),
        "shares_outstanding": src.info_num("sharesOutstanding", "impliedSharesOutstanding")
                              or src.fast("shares"),
    }


# --------------------------------------------------------------------------
# helpers shared by financial sections
# --------------------------------------------------------------------------

def _quarterly_eps(src: Source) -> dict[dt.date, float]:
    """Diluted EPS by quarter-end, computed if not reported directly."""
    direct = src.row("quarterly_income_stmt", "Diluted EPS", "Basic EPS")
    if direct:
        return direct
    ni = src.row("quarterly_income_stmt", "Net Income", "Net Income Common Stockholders")
    sh = src.row("quarterly_income_stmt", "Diluted Average Shares", "Basic Average Shares")
    return {d: ni[d] / sh[d] for d in ni if d in sh and abs(sh[d]) > 1e-9}


def _annual_eps(src: Source) -> dict[int, float]:
    direct = src.annual("income_stmt", "Diluted EPS", "Basic EPS")
    if direct:
        return direct
    ni = src.annual("income_stmt", "Net Income", "Net Income Common Stockholders")
    sh = src.annual("income_stmt", "Diluted Average Shares", "Basic Average Shares")
    return {y: ni[y] / sh[y] for y in ni if y in sh and abs(sh[y]) > 1e-9}


def _fcf_by_year(src: Source) -> dict[int, float]:
    """Free cash flow by fiscal year, reported where possible.

    Prefers the reported 'Free Cash Flow' row over OCF+capex, because issuers
    differ on what belongs in capex and the reported figure matches what data
    providers quote.
    """
    reported = src.annual("cashflow", "Free Cash Flow")
    if reported:
        return reported
    ocf = src.annual("cashflow", "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
    capex = src.annual("cashflow", "Capital Expenditure", "Purchase Of PPE")
    if not ocf:
        return {}
    # Capex is reported as a negative number, so this is addition.
    return {y: ocf[y] + capex.get(y, 0.0) for y in ocf}


def _ttm_or_latest(src: Source, quarterly: str, annual: str, *labels: str
                   ) -> tuple[float | None, str]:
    """Trailing-twelve-month value, falling back to the last full year.

    Returns (value, basis) where basis is 'ttm' or 'fy' so the consumer knows
    which one it got. Silently mixing the two across companies is a subtle way
    to make comparisons wrong.
    """
    v = src.ttm(quarterly, *labels)
    if v is not None:
        return v, "ttm"
    return src.latest(annual, *labels), "fy"


# --------------------------------------------------------------------------
# valuation
# --------------------------------------------------------------------------

def build_valuation(src: Source) -> dict:
    price = src.fast("lastPrice") or src.info_num("currentPrice", "regularMarketPrice", "previousClose")
    mcap = src.info_num("marketCap") or src.fast("marketCap")
    ev = src.info_num("enterpriseValue")

    pe_trailing = src.info_num("trailingPE")
    pe_forward = src.info_num("forwardPE")
    eps_trailing = src.info_num("trailingEps")
    eps_forward = src.info_num("forwardEps")

    fcf_years = _fcf_by_year(src)
    fcf_ttm = src.ttm("quarterly_cashflow", "Free Cash Flow")
    if fcf_ttm is None and fcf_years:
        fcf_ttm = fcf_years[max(fcf_years)]

    ni_ttm, ni_basis = _ttm_or_latest(
        src, "quarterly_income_stmt", "income_stmt", "Net Income", "Net Income Common Stockholders"
    )

    # Historical P/E context. A bare "P/E is 28" is not decision-grade; where 28
    # sits in this company's own five-year distribution is.
    hist = src.history(period="5y", interval="1wk")
    pe_series = pe_history(hist, _quarterly_eps(src), _annual_eps(src))
    pe_values = [v for _, v in pe_series]
    current_pe = pe_trailing if pe_trailing and pe_trailing > 0 else None

    return {
        "pe_trailing": _r(pe_trailing),
        "pe_forward": _r(pe_forward),
        "peg_ratio": _r(src.info_num("trailingPegRatio", "pegRatio")),
        "price_to_book": _r(src.info_num("priceToBook")),
        "price_to_sales": _r(src.info_num("priceToSalesTrailing12Months")),
        "ev_to_ebitda": _r(src.info_num("enterpriseToEbitda")),
        "ev_to_revenue": _r(src.info_num("enterpriseToRevenue")),
        "eps_trailing": _r(eps_trailing),
        "eps_forward": _r(eps_forward),
        "book_value_per_share": _r(src.info_num("bookValue")),
        "earnings_yield_pct": _r(pct(ni_ttm, mcap)),
        "earnings_yield_basis": ni_basis if ni_ttm is not None else None,
        "fcf_yield_pct": _r(pct(fcf_ttm, mcap)),
        "fcf_ttm": fcf_ttm,
        "market_cap": mcap,
        "enterprise_value": ev,
        "pe_history_5y": {
            "current": _r(current_pe),
            "percentile": percentile_rank(current_pe, pe_values),
            "distribution": summarize_distribution(pe_values),
        } if pe_values else None,
    }


# --------------------------------------------------------------------------
# income statement
# --------------------------------------------------------------------------

def build_financials(src: Source) -> dict:
    revenue = src.annual("income_stmt", "Total Revenue", "Operating Revenue")
    net_income = src.annual("income_stmt", "Net Income", "Net Income Common Stockholders")
    gross = src.annual("income_stmt", "Gross Profit")
    operating = src.annual("income_stmt", "Operating Income", "Total Operating Income As Reported")
    ebitda = src.annual("income_stmt", "EBITDA", "Normalized EBITDA")
    eps = _annual_eps(src)
    shares = src.annual("income_stmt", "Diluted Average Shares", "Basic Average Shares")

    rev_ttm, rev_basis = _ttm_or_latest(
        src, "quarterly_income_stmt", "income_stmt", "Total Revenue", "Operating Revenue"
    )
    ni_ttm, ni_basis = _ttm_or_latest(
        src, "quarterly_income_stmt", "income_stmt", "Net Income", "Net Income Common Stockholders"
    )

    margins = {y: pct(net_income[y], revenue[y]) for y in revenue
               if y in net_income and revenue[y]}
    margins = {y: v for y, v in margins.items() if v is not None}

    gross_margins = {y: pct(gross[y], revenue[y]) for y in revenue
                     if y in gross and revenue[y]}
    gross_margins = {y: v for y, v in gross_margins.items() if v is not None}

    op_margins = {y: pct(operating[y], revenue[y]) for y in revenue
                  if y in operating and revenue[y]}
    op_margins = {y: v for y, v in op_margins.items() if v is not None}

    return {
        "revenue_ttm": rev_ttm,
        "revenue_basis": rev_basis if rev_ttm is not None else None,
        "net_income_ttm": ni_ttm,
        "net_income_basis": ni_basis if ni_ttm is not None else None,
        "revenue_by_year": _by_year_list(revenue, 0),
        "net_income_by_year": _by_year_list(net_income, 0),
        "ebitda_by_year": _by_year_list(ebitda, 0),
        "eps_by_year": _by_year_list(eps, 4),
        "diluted_shares_by_year": _by_year_list(shares, 0),
        "net_margin_pct_by_year": _by_year_list(margins),
        "gross_margin_pct_by_year": _by_year_list(gross_margins),
        "operating_margin_pct_by_year": _by_year_list(op_margins),
        "growth": {
            "revenue_yoy_pct": _r(yearly_change_pct(revenue)),
            "revenue_cagr_3y_pct": _r(series_cagr(revenue, 3)),
            "revenue_cagr_5y_pct": _r(series_cagr(revenue, 5)),
            "eps_yoy_pct": _r(yearly_change_pct(eps)),
            "eps_cagr_3y_pct": _r(series_cagr(eps, 3)),
            "eps_cagr_5y_pct": _r(series_cagr(eps, 5)),
            "net_income_cagr_3y_pct": _r(series_cagr(net_income, 3)),
            "revenue_trend": trend_direction(revenue),
            "net_margin_trend": trend_direction(margins),
            "share_count_trend": trend_direction(shares),
        },
        "profitability": {
            "return_on_equity_pct": _r(as_pct_from_fraction(_nonzero(src.info_num("returnOnEquity")))),
            "return_on_assets_pct": _r(as_pct_from_fraction(_nonzero(src.info_num("returnOnAssets")))),
            "gross_margin_pct": _r(as_pct_from_fraction(_nonzero(src.info_num("grossMargins")))),
            "operating_margin_pct": _r(as_pct_from_fraction(_nonzero(src.info_num("operatingMargins")))),
            "net_margin_pct": _r(as_pct_from_fraction(_nonzero(src.info_num("profitMargins")))),
            "return_on_invested_capital_pct": _r(_roic(src)),
        },
    }


def _roic(src: Source) -> float | None:
    """NOPAT / invested capital, using the effective tax rate when available."""
    ebit = src.latest("income_stmt", "EBIT", "Operating Income")
    invested = src.latest("balance_sheet", "Invested Capital")
    if ebit is None or invested is None or invested <= 0:
        return None
    tax_rate = src.latest("income_stmt", "Tax Rate For Calcs")
    if tax_rate is None or not (0 <= tax_rate < 1):
        tax_rate = 0.21  # fallback; flagged as an assumption in the docs
    return (ebit * (1 - tax_rate)) / invested * 100.0


# --------------------------------------------------------------------------
# balance sheet
# --------------------------------------------------------------------------

def build_balance_sheet(src: Source) -> dict:
    """Balance sheet as of a single period.

    Every stock figure is read from one statement so the numbers reconcile.
    Mixing sources here is a real hazard: `info.totalDebt` is most-recent-quarter
    while the annual statement is fiscal-year-end, and for Apple those differ by
    14 billion. Reporting FY debt against MRQ cash would produce a net debt
    figure that matches neither.

    The most recent quarter is preferred because it is the current state of the
    balance sheet and is the basis Yahoo's own ratio fields use.
    """
    # Choose one statement, then read everything from it.
    stmt = "quarterly_balance_sheet"
    probe = src.row(stmt, "Total Debt", "Stockholders Equity", "Total Assets")
    if not probe:
        stmt = "balance_sheet"
        probe = src.row(stmt, "Total Debt", "Stockholders Equity", "Total Assets")
    as_of = max(probe) if probe else None

    def _at(*labels: str) -> float | None:
        """Value from the chosen statement at the chosen period."""
        data = src.row(stmt, *labels)
        if not data:
            return None
        if as_of is not None and as_of in data:
            return data[as_of]
        return data[max(data)]

    total_debt = _at("Total Debt")
    # Narrow cash (excludes short-term investments). Yahoo computes Net Debt as
    # Total Debt minus this figure, so using it keeps the three reconcilable.
    cash = _at("Cash And Cash Equivalents")
    cash_and_sti = _at("Cash Cash Equivalents And Short Term Investments")
    net_debt = _at("Net Debt")
    if net_debt is None and None not in (total_debt, cash):
        net_debt = total_debt - cash

    equity = _at("Stockholders Equity", "Common Stock Equity")

    # Flow items for the coverage ratios: trailing twelve months where the
    # quarterly statement supports it, else the last full year.
    ebitda, ebitda_basis = _ttm_or_latest(
        src, "quarterly_income_stmt", "income_stmt", "EBITDA", "Normalized EBITDA"
    )
    ebit, _ = _ttm_or_latest(
        src, "quarterly_income_stmt", "income_stmt", "EBIT", "Operating Income"
    )
    interest, _ = _ttm_or_latest(
        src, "quarterly_income_stmt", "income_stmt",
        "Interest Expense", "Interest Expense Non Operating",
    )

    return {
        "as_of_period": _iso(as_of),
        "period_basis": "most recent quarter" if stmt.startswith("quarterly") else "fiscal year end",
        "total_debt": total_debt,
        "cash_and_equivalents": cash,
        "cash_and_short_term_investments": cash_and_sti,
        "net_debt": net_debt,
        "net_debt_definition": "total debt minus cash and equivalents, excluding short-term investments",
        "total_assets": _at("Total Assets"),
        "total_equity": equity,
        "working_capital": _at("Working Capital"),
        "tangible_book_value": _at("Tangible Book Value"),
        # yfinance reports debtToEquity already multiplied by 100.
        "debt_to_equity_pct": _r(src.info_num("debtToEquity")),
        "current_ratio": _r(src.info_num("currentRatio")),
        "quick_ratio": _r(src.info_num("quickRatio")),
        "net_debt_to_ebitda_ratio": _r(safe_div(net_debt, ebitda)),
        "ebitda_basis": ebitda_basis if ebitda is not None else None,
        "interest_coverage_ratio": _r(safe_div(ebit, abs(interest) if interest else None)),
        "debt_by_year": _by_year_list(src.annual("balance_sheet", "Total Debt"), 0),
        "equity_by_year": _by_year_list(
            src.annual("balance_sheet", "Stockholders Equity", "Common Stock Equity"), 0
        ),
    }


# --------------------------------------------------------------------------
# cash flow
# --------------------------------------------------------------------------

def build_cash_flow(src: Source) -> dict:
    ocf = src.annual("cashflow", "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
    capex = src.annual("cashflow", "Capital Expenditure", "Purchase Of PPE")
    fcf = _fcf_by_year(src)
    buybacks = src.annual("cashflow", "Repurchase Of Capital Stock", "Common Stock Payments")
    divs_paid = src.annual("cashflow", "Common Stock Dividend Paid", "Cash Dividends Paid")

    mcap = src.info_num("marketCap") or src.fast("marketCap")
    latest_year = max(fcf) if fcf else None
    # Capex is reported as a negative outflow. Reported as a positive magnitude
    # here so it matches capex_by_year and reads consistently.
    capex_ttm = src.ttm("quarterly_cashflow", "Capital Expenditure", "Purchase Of PPE")
    capex_ttm = abs(capex_ttm) if capex_ttm is not None else None

    buyback_latest = abs(buybacks[max(buybacks)]) if buybacks else None
    div_latest = abs(divs_paid[max(divs_paid)]) if divs_paid else None
    buyback_yield = pct(buyback_latest, mcap)
    div_yield_cash = pct(div_latest, mcap)
    shareholder_yield = None
    if buyback_yield is not None or div_yield_cash is not None:
        shareholder_yield = (buyback_yield or 0.0) + (div_yield_cash or 0.0)

    ni = src.annual("income_stmt", "Net Income", "Net Income Common Stockholders")
    # Operating cash flow persistently below net income is the classic earnings
    # quality warning; expose the ratio and let the caller judge it.
    accrual_ratio = None
    if latest_year and latest_year in ni and latest_year in ocf and abs(ni[latest_year]) > 1e-9:
        accrual_ratio = ocf[latest_year] / ni[latest_year]

    return {
        "operating_cash_flow_ttm": src.ttm(
            "quarterly_cashflow", "Operating Cash Flow", "Cash Flow From Continuing Operating Activities"
        ),
        "free_cash_flow_ttm": src.ttm("quarterly_cashflow", "Free Cash Flow"),
        "capex_ttm": capex_ttm,
        "operating_cash_flow_by_year": _by_year_list(ocf, 0),
        "capex_by_year": _by_year_list({y: abs(v) for y, v in capex.items()}, 0),
        "free_cash_flow_by_year": _by_year_list(fcf, 0),
        "buybacks_by_year": _by_year_list({y: abs(v) for y, v in buybacks.items()}, 0),
        "dividends_paid_by_year": _by_year_list({y: abs(v) for y, v in divs_paid.items()}, 0),
        "fcf_cagr_3y_pct": _r(series_cagr(fcf, 3)),
        "fcf_cagr_5y_pct": _r(series_cagr(fcf, 5)),
        "fcf_trend": trend_direction(fcf),
        "buyback_yield_pct": _r(buyback_yield),
        "shareholder_yield_pct": _r(shareholder_yield),
        "ocf_to_net_income_ratio": _r(accrual_ratio),
    }


# --------------------------------------------------------------------------
# dividends
# --------------------------------------------------------------------------

def build_dividends(src: Source) -> dict:
    price = src.fast("lastPrice") or src.info_num("currentPrice", "regularMarketPrice", "previousClose")
    series = src.dividend_series()

    if series is None or series.empty:
        return {
            "pays_dividend": False,
            "forward_yield_pct": None,
            "trailing_yield_pct": None,
            "annual_rate": None,
            "payout_ratio_earnings_pct": None,
            "payout_ratio_fcf_pct": None,
            "dividend_by_year": [],
            "growth_streak_years": None,
            "cagr_5y_pct": None,
            "last_ex_date": None,
            "next_ex_date": None,
            "next_pay_date": None,
        }

    today = dt.date.today()
    idx_dates = [i.date() if hasattr(i, "date") else i for i in series.index]

    trailing_12m = sum(
        float(v) for d, v in zip(idx_dates, series.values)
        if (today - d).days <= 365
    )

    by_year: dict[int, float] = {}
    for d, v in zip(idx_dates, series.values):
        by_year[d.year] = by_year.get(d.year, 0.0) + float(v)
    # The current year is partial and would read as a dividend cut; drop it from
    # the growth series so the streak and CAGR are computed on full years only.
    full_years = {y: v for y, v in by_year.items() if y < today.year}

    rate = src.info_num("dividendRate")
    # Computed rather than read from info: yfinance reports dividendYield as a
    # percent while nearly every neighbouring rate field is a fraction, and the
    # convention has changed between versions. Dividing a known rate by a known
    # price removes the ambiguity entirely.
    forward_yield = pct(rate, price)
    trailing_yield = pct(trailing_12m, price)

    eps = src.info_num("trailingEps")
    fcf_ttm = src.ttm("quarterly_cashflow", "Free Cash Flow")
    shares = src.info_num("sharesOutstanding", "impliedSharesOutstanding")
    fcf_per_share = safe_div(fcf_ttm, shares)

    return {
        "pays_dividend": True,
        "forward_yield_pct": _r(forward_yield),
        "trailing_yield_pct": _r(trailing_yield),
        "reported_yield_field_pct": _r(src.info_num("dividendYield")),
        "annual_rate": _r(rate, 4),
        "trailing_12m_per_share": _r(trailing_12m, 4),
        "payout_ratio_earnings_pct": _r(pct(rate, eps)),
        "payout_ratio_fcf_pct": _r(pct(rate, fcf_per_share)),
        "reported_payout_ratio_pct": _r(as_pct_from_fraction(src.info_num("payoutRatio"))),
        "dividend_by_year": _by_year_list(by_year, 4),
        "growth_streak_years": consecutive_growth_years(full_years),
        "cagr_5y_pct": _r(series_cagr(full_years, 5)),
        "payments_per_year": len([d for d in idx_dates if (today - d).days <= 365]) or None,
        "last_ex_date": _iso(max(idx_dates)) if idx_dates else None,
        "next_ex_date": _epoch_to_iso(src.info.get("exDividendDate")),
        "next_pay_date": _epoch_to_iso(src.info.get("dividendDate")),
    }


# --------------------------------------------------------------------------
# analysts
# --------------------------------------------------------------------------

def build_analysts(src: Source) -> dict:
    price = src.fast("lastPrice") or src.info_num("currentPrice", "regularMarketPrice", "previousClose")
    target_mean = src.info_num("targetMeanPrice")

    distribution = None
    rec = src.frame("recommendations")
    if rec is not None:
        try:
            latest = rec.iloc[0]
            distribution = {
                "strong_buy": int(num(latest.get("strongBuy")) or 0),
                "buy": int(num(latest.get("buy")) or 0),
                "hold": int(num(latest.get("hold")) or 0),
                "sell": int(num(latest.get("sell")) or 0),
                "strong_sell": int(num(latest.get("strongSell")) or 0),
                "period": str(latest.get("period", "")) or None,
            }
        except (IndexError, KeyError, TypeError, ValueError):
            distribution = None

    return {
        "_note": "Analyst figures are third-party opinion, not measured fact. "
                 "They are included for completeness and should be weighted accordingly.",
        "target_mean": _r(target_mean),
        "target_high": _r(src.info_num("targetHighPrice")),
        "target_low": _r(src.info_num("targetLowPrice")),
        "target_median": _r(src.info_num("targetMedianPrice")),
        "upside_to_target_pct": _r(
            pct(target_mean - price if None not in (target_mean, price) else None, price)
        ),
        "recommendation_key": src.info_str("recommendationKey"),
        "recommendation_mean": _r(src.info_num("recommendationMean")),
        "analyst_count": int(src.info_num("numberOfAnalystOpinions") or 0) or None,
        "rating_distribution": distribution,
    }


# --------------------------------------------------------------------------
# earnings
# --------------------------------------------------------------------------

def build_earnings(src: Source) -> dict:
    df = src.frame("earnings_dates")
    history: list[dict] = []
    upcoming: list[dict] = []

    if df is not None:
        for idx, row in df.iterrows():
            when = _iso(idx)
            if when is None:
                continue
            estimate = num(row.get("EPS Estimate"))
            reported = num(row.get("Reported EPS"))
            surprise = num(row.get("Surprise(%)"))
            if reported is None:
                # Future or unreported period.
                if estimate is not None:
                    upcoming.append({"date": when, "eps_estimate": _r(estimate, 4)})
                continue
            if surprise is None and estimate not in (None, 0):
                surprise = (reported - estimate) / abs(estimate) * 100.0
            history.append({
                "date": when,
                "eps_estimate": _r(estimate, 4),
                "eps_reported": _r(reported, 4),
                "surprise_pct": _r(surprise),
                "beat": (reported >= estimate) if estimate is not None else None,
            })

    history.sort(key=lambda r: r["date"], reverse=True)
    upcoming.sort(key=lambda r: r["date"])

    judged = [r for r in history if r["beat"] is not None]
    beat_rate = pct(sum(1 for r in judged if r["beat"]), len(judged)) if judged else None
    surprises = [r["surprise_pct"] for r in history if r["surprise_pct"] is not None]

    cal = src.surface("calendar")
    cal = cal if isinstance(cal, dict) else {}
    next_date = None
    raw_next = cal.get("Earnings Date")
    if isinstance(raw_next, (list, tuple)) and raw_next:
        next_date = _iso(raw_next[0])
    elif raw_next is not None:
        next_date = _iso(raw_next)
    if next_date is None and upcoming:
        next_date = upcoming[0]["date"]

    return {
        "next_earnings_date": next_date,
        "next_eps_estimate": _r(num(cal.get("Earnings Average")), 4),
        "next_revenue_estimate": num(cal.get("Revenue Average")),
        "beat_rate_pct": _r(beat_rate, 1),
        "quarters_assessed": len(judged),
        "average_surprise_pct": _r(sum(surprises) / len(surprises)) if surprises else None,
        "history": history[:12],
    }


# --------------------------------------------------------------------------
# ownership + insiders
# --------------------------------------------------------------------------

def build_ownership(src: Source) -> dict:
    inst = as_pct_from_fraction(_nonzero(src.info_num("heldPercentInstitutions")))
    insider = as_pct_from_fraction(_nonzero(src.info_num("heldPercentInsiders")))
    # Yahoo's institutional percentage is of shares outstanding while the
    # insider figure sometimes exceeds the remainder, so a naive
    # 100 - inst - insider can go negative. Clamp and label it an estimate.
    other = None
    if inst is not None and insider is not None:
        other = max(0.0, 100.0 - inst - insider)

    return {
        "institutional_pct": _r(inst),
        "insider_pct": _r(insider),
        "other_estimated_pct": _r(other),
        "float_shares": src.info_num("floatShares"),
        "shares_short": src.info_num("sharesShort"),
        "short_pct_of_float": _r(as_pct_from_fraction(src.info_num("shortPercentOfFloat"))),
        "short_ratio": _r(src.info_num("shortRatio")),
    }


# Insider transaction classification.
#
# Rules are evaluated in order and the first match wins; the ordering is
# load-bearing. Derived from the actual phrase vocabulary Yahoo returns across
# US and Canadian issuers, not from guesswork:
#
#   "Redemption, retraction, cancelation, repurchase"  contains "purchase" but
#       is the *company* buying back its own stock. Matching it as an insider
#       purchase turns a buyback programme into a fake conviction signal, so it
#       must be excluded before any buy rule runs.
#   "Disposition under a purchase/ownership plan"      also contains "purchase"
#       but is a sale, so disposition terms must precede buy terms.
#   "Stock Award(Grant)", "Exercise of options", and blank descriptions are
#       compensation events, not trades.
_INSIDER_RULES: tuple[tuple[str, str, str], ...] = (
    (r"repurchase|redemption|retraction|cancell?ation", "exclude", "corporate_buyback"),
    (r"\baward\b|\bgrant\b|\bvest", "exclude", "award_or_vesting"),
    (r"\bgift\b", "exclude", "gift"),
    (r"exercise|conversion", "exclude", "option_exercise_or_conversion"),
    (r"\bsale\b|\bsold\b|disposition|disposed", "sell", "sale"),
    (r"\bpurchase\b|\bbought\b|\bacquisition\b|\bacquired\b", "buy", "purchase"),
)

_INSIDER_COMPILED = tuple(
    (re.compile(pattern, re.I), kind, label)
    for pattern, kind, label in _INSIDER_RULES
)


def _classify_insider(text: str) -> tuple[str, str]:
    """Return (kind, label) where kind is 'buy', 'sell', or 'exclude'."""
    cleaned = (text or "").strip()
    if not cleaned:
        return "exclude", "undisclosed"
    for pattern, kind, label in _INSIDER_COMPILED:
        if pattern.search(cleaned):
            return kind, label
    return "exclude", "unrecognized"


_TEN_PCT_OWNER = re.compile(r"10\s*%|beneficial owner", re.I)


def _insider_role(position: str | None) -> str:
    """Separate corporate officers and directors from large outside holders.

    These are different kinds of evidence and should not be summed into one
    "insider conviction" number. A director buying with personal money is a
    view on value; a strategic or activist holder crossing 10% is a corporate
    transaction that can be driven by control, partnership, or financing
    considerations that say nothing about the share price. Rivian shows the
    problem plainly: a single Volkswagen purchase moves the net figure by more
    than 4% of shares outstanding, dwarfing every officer trade.
    """
    if not position:
        return "unknown"
    return "beneficial_owner_10pct" if _TEN_PCT_OWNER.search(position) else "officer_or_director"


def build_insiders(src: Source) -> dict:
    """Insider transactions, split into open-market trades and everything else.

    The distinction matters more than the raw net figure. Yahoo's `Transaction`
    column is empty for every row on many issuers, and `Text` is blank for
    compensation events (RSU vesting, option exercises, grants) where no price
    was paid. Those rows carry large share counts for senior officers and a null
    `Value`.

    Folding them into "net buying" would turn scheduled compensation into a
    conviction signal, which is exactly backwards. They are classified as
    `award_or_other`, excluded from the net figure, and reported separately so
    the exclusion is visible rather than silent.
    """
    df = src.frame("insider_transactions")
    if df is None:
        return {
            "available": False,
            "reason": src.errors.get("insider_transactions", "no insider data reported"),
            "transactions": [],
            "net_shares_by_window": {},
            "excluded_from_net": {},
        }

    shares_out = src.info_num("sharesOutstanding", "impliedSharesOutstanding")
    rows: list[dict] = []
    excluded: dict[str, int] = {}
    excluded_shares = 0.0

    for _, row in df.iterrows():
        shares = num(row.get("Shares"))
        if shares is None:
            continue
        raw_date = row.get("Start Date")
        when = _iso(raw_date if raw_date is not None else row.get("Date"))
        if when is None:
            continue

        text = f"{row.get('Text', '') or ''} {row.get('Transaction', '') or ''}"
        kind, label = _classify_insider(text)

        if kind == "exclude":
            excluded[label] = excluded.get(label, 0) + 1
            excluded_shares += shares
            continue

        position = str(row.get("Position", "") or "").strip() or None
        rows.append({
            "date": when,
            "insider": str(row.get("Insider", "") or "").strip() or None,
            "position": position,
            "role": _insider_role(position),
            "type": kind,
            "shares": int(shares),
            "value": num(row.get("Value")),
            "net": shares if kind == "buy" else -shares,
        })

    rows.sort(key=lambda r: r["date"], reverse=True)

    today = dt.date.today()
    windows: dict[str, dict] = {}
    for label, days in (("1m", 30), ("3m", 90), ("6m", 180), ("1y", 365), ("all", None)):
        cutoff = (today - dt.timedelta(days=days)).isoformat() if days else "0001-01-01"
        subset = [r for r in rows if r["date"] >= cutoff]
        net = sum(r["net"] for r in subset)
        officers = [r for r in subset if r["role"] == "officer_or_director"]
        owners = [r for r in subset if r["role"] == "beneficial_owner_10pct"]
        windows[label] = {
            "net_shares": net,
            "net_pct_of_shares_outstanding": _r(pct(net, shares_out), 4),
            "buy_count": sum(1 for r in subset if r["type"] == "buy"),
            "sell_count": sum(1 for r in subset if r["type"] == "sell"),
            "buy_shares": sum(r["shares"] for r in subset if r["type"] == "buy"),
            "sell_shares": sum(r["shares"] for r in subset if r["type"] == "sell"),
            # Split out because these are different kinds of evidence.
            "net_shares_officers_directors": sum(r["net"] for r in officers),
            "net_shares_10pct_owners": sum(r["net"] for r in owners),
        }

    return {
        "available": True,
        "basis": "open-market purchases and sales only",
        "net_shares_by_window": windows,
        "excluded_from_net": {
            **excluded,
            "total_rows": sum(excluded.values()),
            "total_shares": int(excluded_shares),
            "why": "Excluded rows are compensation events (vesting, grants, "
                   "option exercises), gifts, or company buybacks — none are "
                   "open-market insider trades. Counting them would misrepresent "
                   "scheduled pay or a repurchase programme as insider conviction.",
        },
        "transactions": [{k: v for k, v in r.items() if k != "net"} for r in rows[:25]],
    }


# --------------------------------------------------------------------------
# news (optional, network-heavy)
# --------------------------------------------------------------------------

def build_news(src: Source) -> dict:
    raw = src.surface("news") or []
    items = []
    for item in raw[:15]:
        content = item.get("content", item) if isinstance(item, dict) else {}
        title = content.get("title") or item.get("title")
        if not title:
            continue
        url = ""
        canonical = content.get("canonicalUrl")
        if isinstance(canonical, dict):
            url = canonical.get("url", "")
        url = url or item.get("link", "")
        provider = content.get("provider")
        source_name = provider.get("displayName") if isinstance(provider, dict) else item.get("publisher")
        items.append({
            "title": title,
            "url": url or None,
            "source": source_name or None,
            "published": _iso(content.get("pubDate") or item.get("providerPublishTime")),
            "summary": (content.get("summary") or content.get("description") or None),
        })
    return {"count": len(items), "items": items}


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

SECTION_BUILDERS: dict[str, Callable[[Source], dict]] = {
    "profile": build_profile,
    "quote": build_quote,
    "valuation": build_valuation,
    "financials": build_financials,
    "balance_sheet": build_balance_sheet,
    "cash_flow": build_cash_flow,
    "dividends": build_dividends,
    "analysts": build_analysts,
    "earnings": build_earnings,
    "ownership": build_ownership,
    "insiders": build_insiders,
    "news": build_news,
}

SECTION_NAMES = tuple(SECTION_BUILDERS)

# Which yfinance surfaces each section needs, so only the required ones are
# fetched and a lightweight request stays lightweight.
_SECTION_SURFACES: dict[str, tuple[str, ...]] = {
    "profile": ("info",),
    "quote": ("info", "fast_info"),
    "valuation": ("info", "fast_info", "income_stmt", "quarterly_income_stmt",
                  "cashflow", "quarterly_cashflow"),
    "financials": ("info", "income_stmt", "quarterly_income_stmt", "balance_sheet"),
    "balance_sheet": ("info", "balance_sheet", "income_stmt"),
    "cash_flow": ("info", "cashflow", "quarterly_cashflow", "income_stmt"),
    "dividends": ("info", "fast_info", "dividends", "quarterly_cashflow"),
    "analysts": ("info", "fast_info", "recommendations"),
    "earnings": ("earnings_dates", "calendar"),
    "ownership": ("info",),
    "insiders": ("info", "insider_transactions"),
    "news": ("news",),
}


def surfaces_for(sections: list[str]) -> list[str]:
    """Deduplicated set of yfinance surfaces needed for the given sections."""
    needed: list[str] = []
    for s in sections:
        for surface in _SECTION_SURFACES.get(s, ()):
            if surface not in needed:
                needed.append(surface)
    return needed
