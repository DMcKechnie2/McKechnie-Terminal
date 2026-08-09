"""Mechanical flags.

Deterministic, threshold-based observations computed from the assembled report.
Every flag carries the numbers that triggered it so the consumer can verify the
claim rather than trust it.

These are observations, not recommendations. A flag says "dividends exceeded
free cash flow last year", never "sell". Thresholds are conventional rules of
thumb, they are stated explicitly in each flag's `threshold` field, and they are
not tuned to any strategy. Interpretation is the caller's job.
"""

from __future__ import annotations

from typing import Any

from .metrics import ordinal

__all__ = ["evaluate", "SEVERITIES"]

SEVERITIES = ("info", "caution", "warning")


def _flag(code: str, severity: str, message: str, evidence: dict,
          threshold: str | None = None) -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "evidence": {k: v for k, v in evidence.items() if v is not None},
        "threshold": threshold,
    }


def _get(report: dict, *path: str) -> Any:
    """Walk a nested path, returning None if any hop is missing."""
    cur: Any = report
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def evaluate(report: dict) -> list[dict]:
    """Compute all applicable flags for an assembled report."""
    flags: list[dict] = []
    for rule in _RULES:
        try:
            result = rule(report)
        except Exception:  # noqa: BLE001 - a broken rule must not break the report
            continue
        if result:
            flags.extend(result if isinstance(result, list) else [result])

    order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
    flags.sort(key=lambda f: order.get(f["severity"], 99))
    return flags


# --------------------------------------------------------------------------
# Valuation integrity
# --------------------------------------------------------------------------

def _distorted_trailing_pe(r: dict) -> dict | None:
    """Trailing EPS far above forward EPS implies a one-time gain.

    Trailing P/E then reads artificially cheap. Worth surfacing explicitly
    because it is the single most common way a screen mistakes an accounting
    event for a bargain.
    """
    trailing = _get(r, "valuation", "eps_trailing")
    forward = _get(r, "valuation", "eps_forward")
    pe = _get(r, "valuation", "pe_trailing")
    if None in (trailing, forward, pe) or forward <= 0:
        return None
    if trailing > forward * 1.5:
        return _flag(
            "distorted_trailing_pe", "warning",
            "Trailing EPS is well above forward EPS, which usually means a "
            "one-time gain inflated trailing earnings. Trailing P/E understates "
            "the ongoing valuation; prefer forward P/E here.",
            {"eps_trailing": trailing, "eps_forward": forward,
             "pe_trailing": pe, "pe_forward": _get(r, "valuation", "pe_forward")},
            "trailing EPS > 1.5x forward EPS",
        )
    return None


def _pe_vs_history(r: dict) -> dict | None:
    hist = _get(r, "valuation", "pe_history_5y")
    if not hist:
        return None
    percentile = hist.get("percentile")
    current = hist.get("current")
    if percentile is None or current is None:
        return None
    dist = hist.get("distribution") or {}
    if percentile >= 90:
        return _flag(
            "pe_extended_vs_own_history", "caution",
            f"Current P/E sits in the {ordinal(percentile)} percentile of its own "
            "five-year range — near the expensive end of how this company has "
            "historically been priced.",
            {"pe_current": current, "percentile": percentile,
             "median_5y": dist.get("median"), "max_5y": dist.get("max")},
            "percentile >= 90",
        )
    if percentile <= 10:
        return _flag(
            "pe_depressed_vs_own_history", "info",
            f"Current P/E sits in the {ordinal(percentile)} percentile of its own "
            "five-year range — near the cheap end of its historical pricing. "
            "This reflects price relative to past multiples only, not whether "
            "the underlying business has changed.",
            {"pe_current": current, "percentile": percentile,
             "median_5y": dist.get("median"), "min_5y": dist.get("min")},
            "percentile <= 10",
        )
    return None


def _loss_making(r: dict) -> dict | None:
    ni = _get(r, "financials", "net_income_ttm")
    if ni is None or ni >= 0:
        return None
    return _flag(
        "loss_making", "warning",
        "Net income over the trailing period is negative. Earnings-based "
        "multiples (P/E, PEG, earnings yield) are not meaningful.",
        {"net_income_ttm": ni, "basis": _get(r, "financials", "net_income_basis")},
        "net income < 0",
    )


# --------------------------------------------------------------------------
# Cash flow and dividend sustainability
# --------------------------------------------------------------------------

def _negative_fcf(r: dict) -> dict | None:
    fcf = _get(r, "cash_flow", "free_cash_flow_ttm")
    if fcf is None:
        by_year = _get(r, "cash_flow", "free_cash_flow_by_year") or []
        if not by_year:
            return None
        fcf = by_year[-1]["value"]
    if fcf is None or fcf >= 0:
        return None
    return _flag(
        "negative_free_cash_flow", "warning",
        "Free cash flow is negative over the most recent period, meaning "
        "operations did not cover capital spending.",
        {"free_cash_flow": fcf, "fcf_trend": _get(r, "cash_flow", "fcf_trend")},
        "FCF < 0",
    )


def _payout_sustainability(r: dict) -> list[dict]:
    out: list[dict] = []
    if not _get(r, "dividends", "pays_dividend"):
        return out

    fcf_payout = _get(r, "dividends", "payout_ratio_fcf_pct")
    earn_payout = _get(r, "dividends", "payout_ratio_earnings_pct")
    yield_pct = _get(r, "dividends", "forward_yield_pct")

    if fcf_payout is not None and fcf_payout > 100:
        out.append(_flag(
            "dividend_exceeds_fcf", "warning",
            "The dividend rate exceeds free cash flow per share, so the payout "
            "is not currently funded by cash generation. This can persist for "
            "years via balance sheet or asset sales, but it is not self-funding.",
            {"payout_ratio_fcf_pct": fcf_payout,
             "annual_rate": _get(r, "dividends", "annual_rate"),
             "free_cash_flow_ttm": _get(r, "cash_flow", "free_cash_flow_ttm")},
            "dividend / FCF per share > 100%",
        ))
    elif fcf_payout is not None and fcf_payout > 80:
        out.append(_flag(
            "dividend_payout_elevated", "caution",
            "Dividend consumes more than 80% of free cash flow, leaving limited "
            "headroom for reinvestment or a downturn.",
            {"payout_ratio_fcf_pct": fcf_payout},
            "dividend / FCF per share > 80%",
        ))

    if earn_payout is not None and earn_payout > 100:
        out.append(_flag(
            "dividend_exceeds_earnings", "caution",
            "The dividend rate exceeds trailing EPS.",
            {"payout_ratio_earnings_pct": earn_payout,
             "eps_trailing": _get(r, "valuation", "eps_trailing")},
            "dividend / EPS > 100%",
        ))

    # A high yield is only informative alongside whether it is funded.
    if yield_pct is not None and yield_pct > 8:
        funded = (fcf_payout is not None and fcf_payout <= 100)
        out.append(_flag(
            "high_dividend_yield", "caution" if not funded else "info",
            f"Yield of {yield_pct:.1f}% is unusually high. High yields often "
            "reflect a depressed price rather than a generous policy; check "
            "whether the payout is covered before treating it as income.",
            {"forward_yield_pct": yield_pct, "payout_ratio_fcf_pct": fcf_payout,
             "covered_by_fcf": funded},
            "forward yield > 8%",
        ))

    return out


def _earnings_quality(r: dict) -> dict | None:
    ratio = _get(r, "cash_flow", "ocf_to_net_income_ratio")
    if ratio is None or ratio >= 0.8:
        return None
    return _flag(
        "earnings_quality_gap", "caution",
        "Operating cash flow is materially below net income, meaning reported "
        "profit is not fully converting to cash. Common causes are receivables "
        "growth, inventory build, or non-cash gains.",
        {"ocf_to_net_income_ratio": ratio},
        "OCF / net income < 0.8",
    )


# --------------------------------------------------------------------------
# Balance sheet
# --------------------------------------------------------------------------

def _leverage(r: dict) -> list[dict]:
    out: list[dict] = []
    nd_ebitda = _get(r, "balance_sheet", "net_debt_to_ebitda_ratio")
    coverage = _get(r, "balance_sheet", "interest_coverage_ratio")
    sector = _get(r, "profile", "sector")

    # Leverage ratios are not comparable for lenders and property trusts, where
    # debt is the raw material rather than a financing choice.
    leverage_meaningful = sector not in ("Financial Services", "Real Estate")

    if leverage_meaningful and nd_ebitda is not None and nd_ebitda > 4:
        out.append(_flag(
            "high_leverage", "warning" if nd_ebitda > 6 else "caution",
            f"Net debt is {nd_ebitda:.1f}x EBITDA, above the ~4x level "
            "conventionally treated as elevated for a non-financial issuer.",
            {"net_debt_to_ebitda_ratio": nd_ebitda,
             "net_debt": _get(r, "balance_sheet", "net_debt"), "sector": sector},
            "net debt / EBITDA > 4 (non-financials)",
        ))

    if coverage is not None and coverage < 2:
        if coverage < 0:
            # A negative ratio means operating income is negative, not that
            # interest is covered a negative number of times.
            message = ("Operating income is negative, so interest expense is not "
                       "covered by operations at all.")
        else:
            message = f"Operating income covers interest expense only {coverage:.1f}x."
        out.append(_flag(
            "weak_interest_coverage", "warning" if coverage < 1 else "caution",
            message,
            {"interest_coverage_ratio": coverage},
            "EBIT / interest expense < 2",
        ))

    equity = _get(r, "balance_sheet", "total_equity")
    if equity is not None and equity < 0:
        out.append(_flag(
            "negative_equity", "warning",
            "Total shareholders' equity is negative. Book-value multiples "
            "(P/B) are not meaningful; this often follows sustained buybacks "
            "or accumulated losses, which are very different causes.",
            {"total_equity": equity,
             "price_to_book": _get(r, "valuation", "price_to_book")},
            "total equity < 0",
        ))
    return out


# --------------------------------------------------------------------------
# Operating trend
# --------------------------------------------------------------------------

def _trends(r: dict) -> list[dict]:
    out: list[dict] = []
    growth = _get(r, "financials", "growth") or {}

    if growth.get("revenue_trend") == "falling":
        out.append(_flag(
            "revenue_declining", "caution",
            "Revenue has trended down across the reported years.",
            {"revenue_trend": "falling",
             "revenue_cagr_3y_pct": growth.get("revenue_cagr_3y_pct"),
             "revenue_yoy_pct": growth.get("revenue_yoy_pct")},
            "negative slope over >= 3 years",
        ))

    if growth.get("net_margin_trend") == "falling":
        out.append(_flag(
            "margin_compression", "caution",
            "Net margin has trended down across the reported years.",
            {"net_margin_trend": "falling",
             "net_margin_pct": _get(r, "financials", "profitability", "net_margin_pct")},
            "negative slope over >= 3 years",
        ))

    if growth.get("share_count_trend") == "rising":
        out.append(_flag(
            "share_dilution", "caution",
            "Diluted share count has been rising, so per-share results are "
            "growing more slowly than absolute results.",
            {"share_count_trend": "rising"},
            "positive slope over >= 3 years",
        ))
    elif growth.get("share_count_trend") == "falling":
        out.append(_flag(
            "share_count_shrinking", "info",
            "Diluted share count has been falling, so buybacks are adding to "
            "per-share growth.",
            {"share_count_trend": "falling",
             "buyback_yield_pct": _get(r, "cash_flow", "buyback_yield_pct")},
            "negative slope over >= 3 years",
        ))
    return out


# --------------------------------------------------------------------------
# Price context and insiders
# --------------------------------------------------------------------------

def _price_context(r: dict) -> dict | None:
    pos = _get(r, "quote", "week52_range_position_pct")
    if pos is None:
        return None
    if pos <= 10:
        return _flag(
            "near_52_week_low", "info",
            f"Trading at {pos:.0f}% of its 52-week range, near the low.",
            {"week52_range_position_pct": pos,
             "price": _get(r, "quote", "price"),
             "week52_low": _get(r, "quote", "week52_low")},
            "position <= 10% of range",
        )
    if pos >= 90:
        return _flag(
            "near_52_week_high", "info",
            f"Trading at {pos:.0f}% of its 52-week range, near the high.",
            {"week52_range_position_pct": pos,
             "price": _get(r, "quote", "price"),
             "week52_high": _get(r, "quote", "week52_high")},
            "position >= 90% of range",
        )
    return None


def _insider_activity(r: dict) -> dict | None:
    windows = _get(r, "insiders", "net_shares_by_window") or {}
    six = windows.get("6m") or {}
    net_pct = six.get("net_pct_of_shares_outstanding")
    net = six.get("net_shares")
    if net_pct is None or net is None or net == 0:
        return None
    # Only surface moves large enough to be worth an agent's attention.
    if abs(net_pct) < 0.05:
        return None
    officers = six.get("net_shares_officers_directors")
    owners = six.get("net_shares_10pct_owners")
    detail = {"net_shares_6m": net, "net_pct_of_shares_outstanding": net_pct,
              "buy_count": six.get("buy_count"), "sell_count": six.get("sell_count"),
              "net_shares_officers_directors": officers,
              "net_shares_10pct_owners": owners}

    if net > 0:
        # A single large holder can dominate the aggregate and mean something
        # entirely different from officers buying with their own money.
        owner_driven = bool(owners) and abs(owners) > abs(net) * 0.5
        message = "Insiders were net buyers over the last six months."
        if owner_driven:
            message += (
                " Note this is driven mainly by a holder of more than 10% of the "
                "class, not by officers or directors — such stakes are often "
                "strategic or financing transactions rather than a view on price."
            )
        return _flag("insider_net_buying", "info", message, detail,
                     "|net| >= 0.05% of shares outstanding")
    return _flag(
        "insider_net_selling", "info",
        "Insiders were net sellers over the last six months. Insider selling "
        "is frequently scheduled or compensation-related and is weaker evidence "
        "than insider buying.",
        {"net_shares_6m": net, "net_pct_of_shares_outstanding": net_pct,
         "buy_count": six.get("buy_count"), "sell_count": six.get("sell_count")},
        "|net| >= 0.05% of shares outstanding",
    )


# --------------------------------------------------------------------------
# Data integrity
# --------------------------------------------------------------------------

def _data_integrity(r: dict) -> list[dict]:
    out: list[dict] = []
    dq = r.get("data_quality") or {}

    failed = dq.get("failed_sections") or {}
    if failed:
        out.append(_flag(
            "incomplete_data", "warning",
            "One or more sections could not be retrieved. Absent values are "
            "unknown, not zero — do not treat a missing section as evidence "
            "of absence.",
            {"failed_sections": failed},
            None,
        ))

    excluded = _get(r, "insiders", "excluded_from_net") or {}
    excluded_rows = excluded.get("total_rows")
    if excluded_rows:
        out.append(_flag(
            "insider_non_market_rows_excluded", "info",
            f"{excluded_rows} insider row(s) were compensation events (vesting, "
            "grants, option exercises) or gifts rather than open-market trades, "
            "and are excluded from the net buy/sell figure. This is deliberate: "
            "scheduled compensation is not a conviction signal.",
            {"excluded_rows": excluded_rows,
             "excluded_shares": excluded.get("total_shares"),
             "breakdown": {k: v for k, v in excluded.items()
                           if k not in ("total_rows", "total_shares", "why") and v}},
            None,
        ))

    coverage = _get(r, "analysts", "analyst_count")
    if coverage is not None and coverage < 4:
        out.append(_flag(
            "thin_analyst_coverage", "info",
            f"Only {coverage} analyst estimate(s). Target prices and forward "
            "figures derived from consensus are correspondingly unreliable.",
            {"analyst_count": coverage},
            "fewer than 4 analysts",
        ))
    return out


def _sector_metric_applicability(r: dict) -> list[dict]:
    """Warn when standard metrics do not mean what they appear to mean.

    This is the highest-value flag in the set. For a bank, operating cash flow
    swings with deposit and loan flows, so "free cash flow" is an artefact
    rather than distributable cash — Royal Bank screens at a ~17% FCF yield,
    which an agent would reasonably read as extraordinarily cheap. It is not a
    meaningful figure at all. Likewise enterprise value and net debt treat
    customer deposits as borrowings.

    Rather than suppress the fields, they are returned with an explicit warning
    that names which ones to disregard and what to use instead.
    """
    sector = _get(r, "profile", "sector")
    out: list[dict] = []

    if sector == "Financial Services":
        out.append(_flag(
            "sector_metrics_not_applicable", "warning",
            "This is a financial issuer. Free cash flow, FCF yield, enterprise "
            "value, EV/EBITDA, net debt and net-debt/EBITDA are NOT meaningful "
            "here: operating cash flow tracks deposit and lending flows rather "
            "than distributable cash, and deposits are counted as debt. Ignore "
            "those fields. Use P/E, P/B, return on equity, net interest margin "
            "and capital ratios instead.",
            {"sector": sector,
             "disregard_fields": ["cash_flow.free_cash_flow_ttm",
                                  "valuation.fcf_yield_pct",
                                  "valuation.enterprise_value",
                                  "valuation.ev_to_ebitda",
                                  "balance_sheet.net_debt",
                                  "balance_sheet.net_debt_to_ebitda_ratio"],
             "reported_fcf_yield_pct": _get(r, "valuation", "fcf_yield_pct")},
            None,
        ))

    elif sector == "Real Estate":
        out.append(_flag(
            "sector_metrics_not_applicable", "caution",
            "This is a property issuer. Depreciation makes net income and "
            "therefore P/E a poor guide; the sector is normally assessed on "
            "funds from operations (FFO/AFFO), net asset value, and cap rates, "
            "which are not available here. High leverage is structural rather "
            "than a distress signal.",
            {"sector": sector,
             "disregard_fields": ["valuation.pe_trailing",
                                  "balance_sheet.net_debt_to_ebitda_ratio"]},
            None,
        ))

    return out


_RULES = (
    _sector_metric_applicability,
    _distorted_trailing_pe,
    _pe_vs_history,
    _loss_making,
    _negative_fcf,
    _payout_sustainability,
    _earnings_quality,
    _leverage,
    _trends,
    _price_context,
    _insider_activity,
    _data_integrity,
)
