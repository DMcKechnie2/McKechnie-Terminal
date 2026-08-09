"""Report orchestration.

Assembles sections into one report, records what failed and why, and computes
flags over the finished object. The public entry points are `get_stock`,
`compare_stocks`, and `search_ticker`.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Sequence

from . import flags as flag_rules
from .core import Source, TTLCache, run_with_deadline, utc_now_iso
from .sections import SECTION_BUILDERS, SECTION_NAMES, surfaces_for

__all__ = [
    "get_stock",
    "compare_stocks",
    "search_ticker",
    "DEFAULT_SECTIONS",
    "ALL_SECTIONS",
    "UNITS",
    "clear_cache",
]

ALL_SECTIONS = SECTION_NAMES

# News is excluded by default: it is the slowest surface, it is the least
# decision-relevant per token, and an agent that wants it can ask.
DEFAULT_SECTIONS = tuple(s for s in SECTION_NAMES if s != "news")

UNITS = {
    "*_pct": "percentage; 12.5 means 12.5%",
    "*_ratio": "bare multiple; 1.8 means 1.8x",
    "*_date": "ISO-8601 date string",
    "money": "reporting currency units, unscaled (see profile.currency)",
    "null": "not available. Never means zero.",
}

_CACHE = TTLCache(ttl_seconds=300)


def clear_cache() -> None:
    _CACHE.clear()


def _resolve_sections(sections: Sequence[str] | None) -> tuple[list[str], list[str]]:
    """Return (valid, unknown) section names."""
    if not sections:
        return list(DEFAULT_SECTIONS), []
    requested = [s.strip().lower() for s in sections if s and s.strip()]
    if "all" in requested:
        return list(ALL_SECTIONS), []
    valid = [s for s in requested if s in SECTION_BUILDERS]
    unknown = [s for s in requested if s not in SECTION_BUILDERS]
    return (valid or list(DEFAULT_SECTIONS)), unknown


def get_stock(symbol: str,
              sections: Sequence[str] | None = None,
              section_timeout: float = 25.0,
              use_cache: bool = True) -> dict:
    """Full lookup for one ticker.

    Args:
        symbol: ticker, e.g. "AAPL" or "RY.TO".
        sections: subset of ALL_SECTIONS, or ["all"]. Defaults to everything
            except news.
        section_timeout: per-section wall-clock budget.
        use_cache: serve from the 5-minute cache when available.

    Returns a dict that is always JSON-serializable and always contains
    `symbol`, `as_of`, `data_quality`, and `meta`, even on failure.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return _error_report("", "no ticker supplied")

    wanted, unknown = _resolve_sections(sections)
    cache_key = (symbol, tuple(wanted))

    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            out = dict(cached)
            out["meta"] = {**out.get("meta", {}), "cache_hit": True}
            return out

    started = time.time()
    src = Source(symbol)

    resolvable, resolve_err = run_with_deadline(src.is_resolvable, timeout=20.0, default=False)
    if not resolvable:
        return _error_report(
            symbol,
            resolve_err or "symbol did not resolve to a known instrument",
        )

    # Warm every surface these sections need, concurrently, before building.
    src.prefetch(surfaces_for(wanted))

    report: dict[str, Any] = {
        "symbol": symbol,
        "as_of": utc_now_iso(),
    }
    failed: dict[str, str] = {}

    def _build(name: str) -> tuple[str, Any, str | None]:
        value, err = run_with_deadline(
            lambda: SECTION_BUILDERS[name](src), timeout=section_timeout
        )
        return name, value, err

    with ThreadPoolExecutor(max_workers=min(6, len(wanted))) as pool:
        for name, value, err in pool.map(_build, wanted):
            if err or value is None:
                failed[name] = err or "builder returned nothing"
            else:
                report[name] = value

    report["data_quality"] = {
        "sections_returned": [s for s in wanted if s in report],
        "failed_sections": failed,
        "unknown_sections_requested": unknown,
        "upstream_errors": dict(src.errors),
        "note": "A null value means the figure was unavailable upstream. It "
                "never means zero. A failed section means the data was not "
                "retrieved, not that it does not exist.",
    }

    report["flags"] = flag_rules.evaluate(report)

    report["meta"] = {
        "source": "Yahoo Finance via yfinance",
        "units": UNITS,
        "fetch_seconds": round(time.time() - started, 2),
        "cache_hit": False,
        "disclaimer": "Factual market and financial-statement data with "
                      "mechanically computed metrics and threshold flags. "
                      "Not investment advice and not a recommendation.",
    }

    if use_cache:
        _CACHE.put(cache_key, report)
    return report


def _error_report(symbol: str, reason: str) -> dict:
    return {
        "symbol": symbol,
        "as_of": utc_now_iso(),
        "error": reason,
        "data_quality": {
            "sections_returned": [],
            "failed_sections": {"*": reason},
            "note": "Lookup failed; no data was retrieved.",
        },
        "flags": [],
        "meta": {"source": "Yahoo Finance via yfinance", "units": UNITS},
    }


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

# Kept deliberately small. A comparison is for ranking candidates, and a wide
# table of every available field defeats that purpose.
_COMPARE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("name", ("profile", "name")),
    ("sector", ("profile", "sector")),
    ("currency", ("profile", "currency")),
    ("price", ("quote", "price")),
    ("market_cap", ("quote", "market_cap")),
    ("day_change_pct", ("quote", "day_change_pct")),
    ("week52_range_position_pct", ("quote", "week52_range_position_pct")),
    ("pe_trailing", ("valuation", "pe_trailing")),
    ("pe_forward", ("valuation", "pe_forward")),
    ("price_to_book", ("valuation", "price_to_book")),
    ("ev_to_ebitda", ("valuation", "ev_to_ebitda")),
    ("fcf_yield_pct", ("valuation", "fcf_yield_pct")),
    ("earnings_yield_pct", ("valuation", "earnings_yield_pct")),
    ("revenue_cagr_3y_pct", ("financials", "growth", "revenue_cagr_3y_pct")),
    ("eps_cagr_3y_pct", ("financials", "growth", "eps_cagr_3y_pct")),
    ("net_margin_pct", ("financials", "profitability", "net_margin_pct")),
    ("return_on_equity_pct", ("financials", "profitability", "return_on_equity_pct")),
    ("net_debt_to_ebitda_ratio", ("balance_sheet", "net_debt_to_ebitda_ratio")),
    ("interest_coverage_ratio", ("balance_sheet", "interest_coverage_ratio")),
    ("forward_yield_pct", ("dividends", "forward_yield_pct")),
    ("payout_ratio_fcf_pct", ("dividends", "payout_ratio_fcf_pct")),
    ("upside_to_target_pct", ("analysts", "upside_to_target_pct")),
)

_COMPARE_SECTIONS = ("profile", "quote", "valuation", "financials",
                     "balance_sheet", "cash_flow", "dividends", "analysts")


def _dig(d: dict, path: tuple[str, ...]) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def compare_stocks(symbols: Iterable[str], use_cache: bool = True) -> dict:
    """Side-by-side comparison of several tickers on a fixed metric set.

    One call instead of N, and every row is drawn from the same fields, so the
    numbers are actually comparable.
    """
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    if not syms:
        return {"error": "no tickers supplied", "rows": []}
    syms = syms[:20]

    with ThreadPoolExecutor(max_workers=min(6, len(syms))) as pool:
        reports = list(pool.map(
            lambda s: get_stock(s, sections=_COMPARE_SECTIONS, use_cache=use_cache), syms
        ))

    rows, failures = [], {}
    currencies = set()
    for sym, rep in zip(syms, reports):
        if rep.get("error"):
            failures[sym] = rep["error"]
            continue
        row = {"symbol": sym}
        for label, path in _COMPARE_FIELDS:
            row[label] = _dig(rep, path)
        row["flag_summary"] = [
            f["code"] for f in rep.get("flags", []) if f["severity"] in ("warning", "caution")
        ]
        if row.get("currency"):
            currencies.add(row["currency"])
        rows.append(row)

    notes = []
    if len(currencies) > 1:
        notes.append(
            "Tickers report in different currencies "
            f"({', '.join(sorted(currencies))}). Absolute figures such as price "
            "and market cap are NOT directly comparable; ratios and percentages are."
        )

    return {
        "as_of": utc_now_iso(),
        "requested": syms,
        "rows": rows,
        "failed": failures,
        "notes": notes,
        "meta": {"units": UNITS, "fields": [label for label, _ in _COMPARE_FIELDS]},
    }


# --------------------------------------------------------------------------
# Symbol search
# --------------------------------------------------------------------------

def search_ticker(query: str, limit: int = 10) -> list[dict]:
    """Resolve a company name or partial symbol to candidate tickers.

    Agents are frequently given a company name rather than a symbol; without
    this they guess, and a wrong guess silently returns a real report for the
    wrong company.
    """
    query = (query or "").strip()
    if not query:
        return []

    import requests

    def _fetch() -> list[dict]:
        resp = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": query, "quotesCount": max(1, min(limit, 25)),
                    "newsCount": 0, "enableFuzzyQuery": True},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        resp.raise_for_status()
        out = []
        for item in resp.json().get("quotes", []):
            qtype = item.get("quoteType", "")
            if qtype not in ("EQUITY", "ETF", "MUTUALFUND", "INDEX"):
                continue
            out.append({
                "symbol": item.get("symbol"),
                "name": item.get("longname") or item.get("shortname"),
                "exchange": item.get("exchDisp"),
                "type": qtype,
                "sector": item.get("sector"),
            })
        return out[:limit]

    results, err = run_with_deadline(_fetch, timeout=10.0, default=[])
    if err:
        return [{"error": err}]
    return results or []
