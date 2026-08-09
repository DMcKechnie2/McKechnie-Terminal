"""Compact text rendering.

A full report serialized as JSON runs to several thousand tokens, most of which
is punctuation and key names the model does not need repeated. This module
renders the same content as a dense digest that costs roughly a fifth as much
and reads more naturally.

Missing values render as `n/a`, never as 0 or a blank, so the distinction
between "unavailable" and "zero" survives into the text form.
"""

from __future__ import annotations

from typing import Any, Sequence

from .metrics import ordinal

__all__ = ["render_report", "render_comparison"]

NA = "n/a"


def _money(v: Any, currency: str = "") -> str:
    if v is None:
        return NA
    try:
        f = float(v)
    except (TypeError, ValueError):
        return NA
    sign = "-" if f < 0 else ""
    a = abs(f)
    prefix = f"{sign}{currency}" if currency else sign
    if a >= 1e12:
        return f"{prefix}{a / 1e12:.2f}T"
    if a >= 1e9:
        return f"{prefix}{a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{prefix}{a / 1e6:.2f}M"
    if a >= 1e3:
        return f"{prefix}{a / 1e3:.1f}K"
    return f"{prefix}{a:.2f}"


def _pct(v: Any, places: int = 1, signed: bool = False) -> str:
    if v is None:
        return NA
    try:
        f = float(v)
    except (TypeError, ValueError):
        return NA
    sign = "+" if (signed and f >= 0) else ""
    return f"{sign}{f:.{places}f}%"


def _n(v: Any, places: int = 2) -> str:
    if v is None:
        return NA
    try:
        return f"{float(v):.{places}f}"
    except (TypeError, ValueError):
        return NA


def _get(d: dict, *path: str) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _series_line(entries: Sequence[dict] | None, fmt, limit: int = 6) -> str:
    """Render a [{year, value}] list as `2021:x  2022:y ...`."""
    if not entries:
        return NA
    tail = entries[-limit:]
    return "  ".join(f"{e['year']}:{fmt(e.get('value'))}" for e in tail)


def render_report(report: dict) -> str:
    """Render a `get_stock` result as a compact text digest."""
    if report.get("error"):
        return f"{report.get('symbol', '?')}: LOOKUP FAILED — {report['error']}"

    cur = _get(report, "profile", "currency") or ""
    sym = report.get("symbol", "?")
    lines: list[str] = []

    # -- header ------------------------------------------------------------
    prof = report.get("profile") or {}
    bits = [b for b in (prof.get("sector"), prof.get("industry")) if b]
    head = f"{sym} — {prof.get('name') or NA}"
    if bits:
        head += f" | {' / '.join(bits)}"
    if prof.get("exchange"):
        head += f" | {prof['exchange']}"
    if cur:
        head += f" | reports in {cur}"
    lines.append(head)
    lines.append(f"as of {report.get('as_of', NA)}")

    # -- quote -------------------------------------------------------------
    q = report.get("quote")
    if q:
        lines.append("")
        lines.append(
            f"PRICE      {_n(q.get('price'))} ({_pct(q.get('day_change_pct'), signed=True)} day)"
            f"   52w {_n(q.get('week52_low'))}-{_n(q.get('week52_high'))}"
            f"  -> {_pct(q.get('week52_range_position_pct'), 0)} of range"
        )
        lines.append(
            f"           mcap {_money(q.get('market_cap'), cur)}"
            f"   EV {_money(q.get('enterprise_value'), cur)}"
            f"   beta {_n(q.get('beta'))}"
            f"   vs200dma {_pct(q.get('vs_200dma_pct'), signed=True)}"
            f"   1y {_pct(q.get('one_year_change_pct'), signed=True)}"
        )

    # -- valuation ---------------------------------------------------------
    v = report.get("valuation")
    if v:
        lines.append("")
        lines.append(
            f"VALUATION  P/E {_n(v.get('pe_trailing'))} trail | {_n(v.get('pe_forward'))} fwd"
            f"   PEG {_n(v.get('peg_ratio'))}"
            f"   P/B {_n(v.get('price_to_book'))}"
            f"   P/S {_n(v.get('price_to_sales'))}"
            f"   EV/EBITDA {_n(v.get('ev_to_ebitda'))}"
        )
        lines.append(
            f"           FCF yield {_pct(v.get('fcf_yield_pct'), 2)}"
            f"   earnings yield {_pct(v.get('earnings_yield_pct'), 2)}"
            f"   EPS {_n(v.get('eps_trailing'))} trail / {_n(v.get('eps_forward'))} fwd"
        )
        hist = v.get("pe_history_5y")
        if hist and hist.get("percentile") is not None:
            d = hist.get("distribution") or {}
            lines.append(
                f"           P/E vs own 5y history: {ordinal(hist['percentile'])} pct"
                f"  (median {_n(d.get('median'))}, range {_n(d.get('min'))}–{_n(d.get('max'))},"
                f" n={d.get('observations', '?')})"
            )

    # -- financials --------------------------------------------------------
    f = report.get("financials")
    if f:
        g = f.get("growth") or {}
        p = f.get("profitability") or {}
        lines.append("")
        lines.append(
            f"BUSINESS   revenue {_money(f.get('revenue_ttm'), cur)} ({f.get('revenue_basis') or NA})"
            f"   net income {_money(f.get('net_income_ttm'), cur)}"
        )
        lines.append(
            f"           margins: gross {_pct(p.get('gross_margin_pct'))}"
            f"  op {_pct(p.get('operating_margin_pct'))}"
            f"  net {_pct(p.get('net_margin_pct'))}"
            f"   ROE {_pct(p.get('return_on_equity_pct'))}"
            f"   ROIC {_pct(p.get('return_on_invested_capital_pct'))}"
        )
        lines.append(
            f"           growth: rev yoy {_pct(g.get('revenue_yoy_pct'), signed=True)}"
            f"  rev 3y CAGR {_pct(g.get('revenue_cagr_3y_pct'), signed=True)}"
            f"  EPS 3y CAGR {_pct(g.get('eps_cagr_3y_pct'), signed=True)}"
        )
        lines.append(
            f"           trends: revenue {g.get('revenue_trend') or NA}"
            f"  margin {g.get('net_margin_trend') or NA}"
            f"  share count {g.get('share_count_trend') or NA}"
        )
        lines.append(f"           revenue by yr   {_series_line(f.get('revenue_by_year'), lambda x: _money(x))}")
        lines.append(f"           EPS by yr       {_series_line(f.get('eps_by_year'), lambda x: _n(x))}")

    # -- balance sheet -----------------------------------------------------
    b = report.get("balance_sheet")
    if b:
        lines.append("")
        lines.append(
            f"BALANCE    [{b.get('period_basis') or 'period unknown'}"
            f" {b.get('as_of_period') or NA}]"
        )
        lines.append(
            f"           debt {_money(b.get('total_debt'), cur)}"
            f"   cash {_money(b.get('cash_and_equivalents'), cur)}"
            f"   (+STI {_money(b.get('cash_and_short_term_investments'), cur)})"
            f"   net debt {_money(b.get('net_debt'), cur)}"
            f"   equity {_money(b.get('total_equity'), cur)}"
        )
        def _mult(v: Any) -> str:
            """Render a multiple, without appending 'x' to an n/a."""
            s = _n(v)
            return s if s == NA else f"{s}x"

        lines.append(
            f"           netdebt/EBITDA {_mult(b.get('net_debt_to_ebitda_ratio'))}"
            f"   interest cover {_mult(b.get('interest_coverage_ratio'))}"
            f"   D/E {_pct(b.get('debt_to_equity_pct'))}"
            f"   current {_n(b.get('current_ratio'))}"
        )

    # -- cash flow ---------------------------------------------------------
    c = report.get("cash_flow")
    if c:
        lines.append("")
        lines.append(
            f"CASHFLOW   OCF {_money(c.get('operating_cash_flow_ttm'), cur)}"
            f"   capex {_money(c.get('capex_ttm'), cur)}"
            f"   FCF {_money(c.get('free_cash_flow_ttm'), cur)}"
            f"   trend {c.get('fcf_trend') or NA}"
        )
        lines.append(
            f"           FCF 3y CAGR {_pct(c.get('fcf_cagr_3y_pct'), signed=True)}"
            f"   buyback yield {_pct(c.get('buyback_yield_pct'), 2)}"
            f"   shareholder yield {_pct(c.get('shareholder_yield_pct'), 2)}"
            f"   OCF/NI {_n(c.get('ocf_to_net_income_ratio'))}"
        )
        lines.append(f"           FCF by yr       {_series_line(c.get('free_cash_flow_by_year'), lambda x: _money(x))}")

    # -- dividends ---------------------------------------------------------
    d = report.get("dividends")
    if d:
        lines.append("")
        if not d.get("pays_dividend"):
            lines.append("DIVIDEND   none")
        else:
            lines.append(
                f"DIVIDEND   fwd yield {_pct(d.get('forward_yield_pct'), 2)}"
                f"   trailing {_pct(d.get('trailing_yield_pct'), 2)}"
                f"   rate {_n(d.get('annual_rate'), 4)}/sh"
                f"   streak {d.get('growth_streak_years') if d.get('growth_streak_years') is not None else NA}y"
            )
            lines.append(
                f"           payout: {_pct(d.get('payout_ratio_earnings_pct'))} of EPS,"
                f" {_pct(d.get('payout_ratio_fcf_pct'))} of FCF"
                f"   5y CAGR {_pct(d.get('cagr_5y_pct'), signed=True)}"
            )
            lines.append(
                f"           next ex {d.get('next_ex_date') or NA}"
                f"   next pay {d.get('next_pay_date') or NA}"
            )

    # -- earnings ----------------------------------------------------------
    e = report.get("earnings")
    if e:
        lines.append("")
        lines.append(
            f"EARNINGS   next {e.get('next_earnings_date') or NA}"
            f"   est EPS {_n(e.get('next_eps_estimate'))}"
            f"   beat rate {_pct(e.get('beat_rate_pct'), 0)} of {e.get('quarters_assessed', 0)}q"
            f"   avg surprise {_pct(e.get('average_surprise_pct'), signed=True)}"
        )
        recent = (e.get("history") or [])[:4]
        if recent:
            parts = [
                f"{h['date'][:7]}:{'beat' if h.get('beat') else 'miss'} {_pct(h.get('surprise_pct'), 1, signed=True)}"
                for h in recent
            ]
            lines.append(f"           recent: {'  '.join(parts)}")

    # -- analysts ----------------------------------------------------------
    a = report.get("analysts")
    if a:
        lines.append("")
        lines.append(
            f"ANALYSTS   target {_n(a.get('target_mean'))}"
            f" (range {_n(a.get('target_low'))}–{_n(a.get('target_high'))})"
            f"   upside {_pct(a.get('upside_to_target_pct'), signed=True)}"
            f"   {a.get('recommendation_key') or NA}"
            f"   n={a.get('analyst_count') or NA}"
        )
        lines.append("           [analyst figures are opinion, not measured fact]")

    # -- ownership / insiders ---------------------------------------------
    o = report.get("ownership")
    if o:
        lines.append("")
        lines.append(
            f"OWNERSHIP  institutional {_pct(o.get('institutional_pct'))}"
            f"   insider {_pct(o.get('insider_pct'))}"
            f"   short/float {_pct(o.get('short_pct_of_float'))}"
        )

    ins = report.get("insiders")
    if ins and ins.get("available"):
        w = (ins.get("net_shares_by_window") or {}).get("6m") or {}
        net = w.get("net_shares")
        if net is not None:
            direction = "net BUYING" if net > 0 else "net SELLING" if net < 0 else "flat"
            lines.append(
                f"           insiders 6m (open-market only): {direction} {abs(net):,.0f} sh"
                f" ({_pct(w.get('net_pct_of_shares_outstanding'), 3)} of shares out)"
                f"  {w.get('buy_count', 0)} buys / {w.get('sell_count', 0)} sells"
            )
        exc = ins.get("excluded_from_net") or {}
        if exc.get("total_rows"):
            lines.append(
                f"           excluded {exc['total_rows']} non-market rows"
                f" ({exc.get('total_shares', 0):,} sh: vesting/grants/gifts/exercises)"
            )

    # -- news --------------------------------------------------------------
    news = report.get("news")
    if news and news.get("items"):
        lines.append("")
        lines.append("NEWS")
        for item in news["items"][:6]:
            lines.append(f"  · [{item.get('published') or NA}] {item.get('title')} ({item.get('source') or NA})")

    # -- flags -------------------------------------------------------------
    fl = report.get("flags") or []
    if fl:
        lines.append("")
        lines.append("FLAGS (mechanical, threshold-based — not recommendations)")
        for item in fl:
            lines.append(f"  [{item['severity']}] {item['code']}: {item['message']}")
            ev = item.get("evidence") or {}
            if ev:
                rendered = ", ".join(
                    f"{k}={_n(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else val}"
                    for k, val in list(ev.items())[:5]
                )
                lines.append(f"       evidence: {rendered}")

    # -- data quality ------------------------------------------------------
    dq = report.get("data_quality") or {}
    failed = dq.get("failed_sections") or {}
    if failed:
        lines.append("")
        lines.append("DATA GAPS  " + "; ".join(f"{k}: {v}" for k, v in failed.items()))
        lines.append("           (missing = unknown, not zero)")

    meta = report.get("meta") or {}
    lines.append("")
    lines.append(
        f"[{meta.get('source', 'unknown source')}"
        f" · {meta.get('fetch_seconds', '?')}s"
        f" · cache_hit={meta.get('cache_hit')}"
        " · percentages are shown as percent, e.g. 12.5% not 0.125]"
    )
    return "\n".join(lines)


def render_comparison(result: dict) -> str:
    """Render a `compare_stocks` result as an aligned text table."""
    rows = result.get("rows") or []
    if not rows:
        return f"No comparable data. {result.get('error') or result.get('failed') or ''}".strip()

    columns: list[tuple[str, str, Any]] = [
        ("symbol", "TICKER", lambda r: r.get("symbol") or NA),
        ("price", "PRICE", lambda r: _n(r.get("price"))),
        ("market_cap", "MCAP", lambda r: _money(r.get("market_cap"))),
        ("pe_trailing", "P/E", lambda r: _n(r.get("pe_trailing"))),
        ("pe_forward", "fP/E", lambda r: _n(r.get("pe_forward"))),
        ("price_to_book", "P/B", lambda r: _n(r.get("price_to_book"))),
        ("ev_to_ebitda", "EV/EB", lambda r: _n(r.get("ev_to_ebitda"))),
        ("fcf_yield_pct", "FCF%", lambda r: _n(r.get("fcf_yield_pct"), 1)),
        ("return_on_equity_pct", "ROE%", lambda r: _n(r.get("return_on_equity_pct"), 1)),
        ("net_margin_pct", "NM%", lambda r: _n(r.get("net_margin_pct"), 1)),
        ("revenue_cagr_3y_pct", "REV3Y%", lambda r: _n(r.get("revenue_cagr_3y_pct"), 1)),
        ("net_debt_to_ebitda_ratio", "ND/EB", lambda r: _n(r.get("net_debt_to_ebitda_ratio"), 1)),
        ("forward_yield_pct", "DIV%", lambda r: _n(r.get("forward_yield_pct"), 2)),
        ("upside_to_target_pct", "UPS%", lambda r: _n(r.get("upside_to_target_pct"), 0)),
    ]

    rendered = [[fmt(r) for _, _, fmt in columns] for r in rows]
    headers = [h for _, h, _ in columns]
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rendered), default=0))
        for i in range(len(columns))
    ]

    def _line(cells: Sequence[str]) -> str:
        return "  ".join(c.rjust(widths[i]) if i else c.ljust(widths[i])
                         for i, c in enumerate(cells))

    out = [_line(headers), "-" * (sum(widths) + 2 * (len(widths) - 1))]
    out.extend(_line(row) for row in rendered)

    for note in result.get("notes") or []:
        out.append("")
        out.append(f"NOTE: {note}")

    flagged = [(r["symbol"], r.get("flag_summary") or []) for r in rows if r.get("flag_summary")]
    if flagged:
        out.append("")
        out.append("FLAGS")
        for sym, codes in flagged:
            out.append(f"  {sym}: {', '.join(codes)}")

    if result.get("failed"):
        out.append("")
        out.append("FAILED: " + "; ".join(f"{k} ({v})" for k, v in result["failed"].items()))

    out.append("")
    out.append("[values are percent where the header ends in %; blank/n-a means unavailable, not zero]")
    return "\n".join(out)
