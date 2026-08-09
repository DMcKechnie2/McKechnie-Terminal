"""MCP server exposing stock_intel over stdio.

Run directly (`python -m stock_intel.server`) or register with any MCP client.

The docstrings below are what a consuming model actually reads when deciding
which tool to call and how to interpret the result, so they state the unit
convention and the null semantics explicitly rather than leaving them implicit.
They are written as literals — FastMCP captures them at decoration time, so
building them dynamically would leave placeholders in the registered schema.
"""

from __future__ import annotations

import json
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .core import Source
from .render import render_comparison, render_report
from .report import (
    compare_stocks as _compare_stocks,
    get_stock as _get_stock,
    search_ticker as _search_ticker,
)

mcp = FastMCP("stock-intel")


@mcp.tool()
def get_stock(
    ticker: str,
    sections: list[str] | None = None,
    output: Literal["compact", "json"] = "compact",
) -> str:
    """Look up comprehensive fundamental and market data for one stock.

    Returns price and valuation multiples, multi-year income statement, balance
    sheet and cash flow history, growth rates and CAGRs, profitability and
    leverage ratios, dividend history and payout coverage, earnings beat/miss
    record, analyst targets, ownership and insider activity — plus mechanical
    threshold flags (for example: dividend not covered by free cash flow,
    trailing P/E distorted by a one-time gain, net debt above 4x EBITDA), each
    carrying the numbers that triggered it so you can verify the claim.

    Also reports where the current P/E sits within the company's own five-year
    distribution, which is what makes a multiple interpretable: "P/E 28" alone
    is not decision-grade, "P/E 28, 85th percentile of its own 5-year range" is.

    Args:
        ticker: Symbol. Use the exchange suffix for non-US listings, e.g.
            "AAPL", "RY.TO" (Toronto), "SHOP.TO", "BP.L" (London). If you only
            have a company name, call search_ticker first rather than guessing —
            a wrong guess returns a valid report for the wrong company.
        sections: Optional subset, to limit cost and context. Available:
            profile, quote, valuation, financials, balance_sheet, cash_flow,
            dividends, analysts, earnings, ownership, insiders, news. Omit for
            everything except news; pass ["all"] to include news too.
        output: "compact" (default) is a dense text digest costing roughly a
            fifth of the tokens. "json" returns the full structured object —
            use it only when you need to parse fields programmatically.

    Units: fields ending in _pct are percentages (12.5 means 12.5%, not 0.125);
    fields ending in _ratio are bare multiples (1.8 means 1.8x); money is in the
    issuer's reporting currency, unscaled. A null or "n/a" value means the
    figure was unavailable upstream — it never means zero. Sections that failed
    to fetch are listed separately under data gaps; treat those as unknown
    rather than as evidence of absence.

    This returns factual data and mechanically computed metrics. It does not
    recommend buying, selling, or holding anything.
    """
    report = _get_stock(ticker, sections=sections)
    if output == "json":
        return json.dumps(report, indent=2, default=str)
    return render_report(report)


@mcp.tool()
def compare_stocks(
    tickers: list[str],
    output: Literal["compact", "json"] = "compact",
) -> str:
    """Compare several stocks side by side on a consistent metric set.

    One call instead of several separate lookups, and every row is drawn from
    the same fields so the numbers are genuinely comparable. Covers valuation
    multiples, growth, margins, returns, leverage, dividend, and analyst upside,
    plus a per-ticker summary of any flags raised.

    Warns explicitly when the tickers report in different currencies, in which
    case absolute figures (price, market cap) are not comparable across rows,
    while ratios and percentages still are.

    Args:
        tickers: Two to twenty symbols, e.g. ["RY.TO", "TD.TO", "BNS.TO"].
        output: "compact" (default) renders an aligned table; "json" returns
            structured rows.

    Units: fields ending in _pct are percentages (12.5 means 12.5%); fields
    ending in _ratio are bare multiples. "n/a" means unavailable, not zero.

    Comparison is on reported figures only. It does not rank the tickers or
    suggest which to prefer.
    """
    result = _compare_stocks(tickers)
    if output == "json":
        return json.dumps(result, indent=2, default=str)
    return render_comparison(result)


@mcp.tool()
def search_ticker(query: str, limit: int = 10) -> str:
    """Resolve a company name or partial symbol to candidate ticker symbols.

    Call this whenever you have a company name rather than a confirmed symbol.
    Guessing a ticker is a silent failure mode: the wrong symbol usually still
    resolves to a real company and returns a plausible report about it.

    Args:
        query: Company name or partial symbol, e.g. "Royal Bank of Canada",
            "shopify", "brkb".
        limit: Maximum candidates to return (default 10).

    Returns candidates with symbol, name, exchange, and instrument type. Check
    the exchange before using a symbol — many companies are cross-listed, and
    the listings differ in currency and liquidity.
    """
    results = _search_ticker(query, limit=limit)
    if not results:
        return f'No matches for "{query}".'
    if isinstance(results[0], dict) and results[0].get("error"):
        return f'Search failed: {results[0]["error"]}'

    lines = [f'Matches for "{query}":']
    for r in results:
        lines.append(
            f"  {r.get('symbol', '?'):<12} {(r.get('name') or '')[:44]:<46}"
            f" {(r.get('exchange') or ''):<16} {r.get('type') or ''}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_statement(
    ticker: str,
    statement: Literal["income", "balance", "cashflow"],
    period: Literal["annual", "quarterly"] = "annual",
    line_items: list[str] | None = None,
) -> str:
    """Read raw financial statement line items not covered by get_stock.

    get_stock returns a curated set of headline figures. Use this when you need
    a specific line — R&D spend, inventory, deferred revenue, goodwill, a
    particular working-capital movement — under the issuer's own row labels.

    Args:
        ticker: Symbol, e.g. "AAPL".
        statement: "income", "balance", or "cashflow".
        period: "annual" (default, about 5 years) or "quarterly" (about 5
            quarters).
        line_items: Optional exact row labels to return, e.g.
            ["Research And Development", "Gross Profit"]. Omit to list every
            available label first, then call again with the ones you want —
            labels vary by issuer and are not standardized.

    Values are in the issuer's reporting currency, unscaled. Periods with no
    reported value are omitted rather than zero-filled.
    """
    src = Source(ticker)
    attr = {
        ("income", "annual"): "income_stmt",
        ("income", "quarterly"): "quarterly_income_stmt",
        ("balance", "annual"): "balance_sheet",
        ("balance", "quarterly"): "quarterly_balance_sheet",
        ("cashflow", "annual"): "cashflow",
        ("cashflow", "quarterly"): "quarterly_cashflow",
    }[(statement, period)]

    df = src.frame(attr)
    if df is None:
        reason = src.errors.get(attr, "not reported for this issuer")
        return f"{ticker.upper()}: no {period} {statement} statement available ({reason})."

    available = [str(i) for i in df.index]
    if not line_items:
        listing = "\n".join(f"  {label}" for label in available)
        return (
            f"{ticker.upper()} — {period} {statement} statement\n"
            f"Periods: {', '.join(str(c)[:10] for c in df.columns)}\n\n"
            f"Available line items ({len(available)}). Call again with "
            f"line_items=[...] to fetch values:\n{listing}"
        )

    lines = [
        f"{ticker.upper()} — {period} {statement} statement",
        f"currency: {src.info_str('financialCurrency', 'currency') or 'unknown'}",
        "",
    ]
    missing: list[str] = []
    for label in line_items:
        values = src.row(attr, label)
        if not values:
            missing.append(label)
            continue
        rendered = "  ".join(
            f"{d.isoformat()}:{v:,.0f}" for d, v in sorted(values.items(), reverse=True)
        )
        lines.append(f"{label}: {rendered}")

    if missing:
        close = [a for a in available if any(m.lower() in a.lower() for m in missing)]
        lines.append("")
        lines.append(f"Not found: {', '.join(missing)}")
        if close:
            lines.append(f"Did you mean: {', '.join(close[:8])}")
    return "\n".join(lines)


def main() -> None:
    """Entry point for `python -m stock_intel.server`."""
    mcp.run()


if __name__ == "__main__":
    main()
