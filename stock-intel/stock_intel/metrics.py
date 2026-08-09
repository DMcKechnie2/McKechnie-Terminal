"""Derived metrics.

Arithmetic an agent would otherwise have to perform on raw fields, done once,
consistently, with the degenerate cases handled. Every function returns None
rather than a misleading number when its inputs cannot support an answer:
a CAGR from a negative base, a ratio with a zero denominator, a percentile
from three data points.

Unit convention for this whole package:

    *_pct      a percentage      12.5 means 12.5%
    *_ratio    a bare multiple   1.8 means 1.8x
    money      reporting currency units, unscaled
    everything else unitless

yfinance is internally inconsistent here (dividendYield arrives as a percent,
returnOnEquity as a fraction), so values are normalized on the way in and the
convention is declared in the output payload.
"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

import pandas as pd

from .core import num

__all__ = [
    "ordinal",
    "safe_div",
    "pct",
    "as_pct_from_fraction",
    "cagr",
    "percentile_rank",
    "trend_direction",
    "consecutive_growth_years",
    "yearly_change_pct",
    "pe_history",
    "summarize_distribution",
]


def ordinal(value: float | None) -> str:
    """Format a number with its English ordinal suffix: 3 -> '3rd', 11 -> '11th'."""
    if value is None:
        return "n/a"
    n = int(round(float(value)))
    # 11, 12 and 13 take 'th' despite ending in 1, 2 and 3.
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    """Divide, returning None on missing inputs or a zero/near-zero denominator."""
    n, d = num(numerator), num(denominator)
    if n is None or d is None or abs(d) < 1e-12:
        return None
    return n / d


def pct(numerator: float | None, denominator: float | None) -> float | None:
    """Percentage of one value relative to another. 12.5 means 12.5%."""
    r = safe_div(numerator, denominator)
    return None if r is None else r * 100.0


def as_pct_from_fraction(v: float | None) -> float | None:
    """Convert a fraction (0.27) to this package's percent convention (27.0)."""
    f = num(v)
    return None if f is None else f * 100.0


def cagr(start: float | None, end: float | None, years: float) -> float | None:
    """Compound annual growth rate as a percent.

    Undefined — and returned as None — when the starting value is not strictly
    positive. A CAGR measured from a negative or zero base is not a meaningful
    number, and emitting one invites an agent to reason from nonsense (a swing
    from -100M to +50M is not "150% growth").
    """
    s, e = num(start), num(end)
    if s is None or e is None or years <= 0 or s <= 0:
        return None
    if e <= 0:
        return None
    return ((e / s) ** (1.0 / years) - 1.0) * 100.0


def series_cagr(by_year: Mapping[int, float], window: int) -> float | None:
    """CAGR across the last `window` years of a {year: value} mapping.

    Uses the actual span between the endpoints rather than assuming the
    requested window is fully populated.
    """
    if not by_year:
        return None
    years = sorted(by_year)
    if len(years) < 2:
        return None
    end_year = years[-1]
    target = end_year - window
    candidates = [y for y in years if y <= target]
    start_year = candidates[-1] if candidates else years[0]
    span = end_year - start_year
    if span <= 0:
        return None
    return cagr(by_year[start_year], by_year[end_year], span)


def yearly_change_pct(by_year: Mapping[int, float]) -> float | None:
    """Most recent year-over-year change as a percent.

    Unlike CAGR this tolerates a negative base by falling back to the absolute
    denominator, since a single-period swing is still interpretable — but only
    when the base is non-zero.
    """
    if len(by_year) < 2:
        return None
    years = sorted(by_year)
    prev, cur = by_year[years[-2]], by_year[years[-1]]
    if abs(prev) < 1e-12:
        return None
    return (cur - prev) / abs(prev) * 100.0


def percentile_rank(value: float | None, distribution: Sequence[float],
                    min_points: int = 20) -> float | None:
    """Where `value` sits within `distribution`, as a 0-100 percentile.

    Returns None below `min_points` observations; a percentile computed from a
    handful of points is noise dressed up as precision. 90 means the value is
    higher than 90% of the distribution.
    """
    v = num(value)
    if v is None:
        return None
    pts = [p for p in (num(x) for x in distribution) if p is not None]
    if len(pts) < min_points:
        return None
    below = sum(1 for p in pts if p < v)
    return round(below / len(pts) * 100.0, 1)


def summarize_distribution(distribution: Sequence[float]) -> dict | None:
    """Min / percentiles / median / max of a numeric series."""
    pts = sorted(p for p in (num(x) for x in distribution) if p is not None)
    if len(pts) < 4:
        return None
    s = pd.Series(pts)
    return {
        "min": round(float(s.min()), 2),
        "p25": round(float(s.quantile(0.25)), 2),
        "median": round(float(s.median()), 2),
        "p75": round(float(s.quantile(0.75)), 2),
        "max": round(float(s.max()), 2),
        "observations": len(pts),
    }


def trend_direction(by_year: Mapping[int, float], min_years: int = 3) -> str | None:
    """Coarse direction of a yearly series: rising / falling / flat.

    Uses the sign of a least-squares slope normalized by the mean magnitude, so
    the threshold is scale-free and works for both margins and revenue.
    """
    if len(by_year) < min_years:
        return None
    years = sorted(by_year)
    xs = list(range(len(years)))
    ys = [by_year[y] for y in years]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)) / denom
    scale = max(abs(mean_y), 1e-9)
    normalized = slope / scale
    if normalized > 0.02:
        return "rising"
    if normalized < -0.02:
        return "falling"
    return "flat"


def consecutive_growth_years(by_year: Mapping[int, float]) -> int | None:
    """Count of consecutive most-recent years in which the value increased."""
    if len(by_year) < 2:
        return None
    years = sorted(by_year)
    streak = 0
    for i in range(len(years) - 1, 0, -1):
        if by_year[years[i]] > by_year[years[i - 1]]:
            streak += 1
        else:
            break
    return streak


# --------------------------------------------------------------------------
# Valuation history
# --------------------------------------------------------------------------

def pe_history(price_history: pd.DataFrame | None,
               quarterly_eps: Mapping[date, float] | None,
               annual_eps: Mapping[int, float] | None,
               max_pe: float = 500.0) -> list[tuple[date, float]]:
    """Reconstruct a historical P/E series from prices and reported EPS.

    At each price observation the P/E uses trailing-twelve-month EPS as it would
    have been known at that date: the sum of the four most recent quarters
    already reported. Falls back to the most recent annual EPS when fewer than
    four quarters are available, which is what happens beyond the ~5 years of
    quarterly data yfinance returns.

    Periods with non-positive EPS are omitted entirely rather than recorded as a
    negative or enormous P/E — a loss-making quarter makes the ratio
    meaningless, and leaving it in would corrupt any percentile computed from
    the series.
    """
    if price_history is None or price_history.empty:
        return []
    if "Close" not in price_history.columns:
        return []

    quarters: list[tuple[date, float]] = sorted((quarterly_eps or {}).items())
    annual = dict(annual_eps or {})
    if not quarters and not annual:
        return []

    out: list[tuple[date, float]] = []
    for idx, row in price_history.iterrows():
        price = num(row.get("Close"))
        if price is None or price <= 0:
            continue
        d = idx.date() if hasattr(idx, "date") else idx
        if not isinstance(d, date):
            continue

        eps_ttm = None
        if quarters:
            reported = [v for (qd, v) in quarters if qd <= d]
            if len(reported) >= 4:
                eps_ttm = sum(reported[-4:])
        if (eps_ttm is None or eps_ttm <= 0) and annual:
            prior = [y for y in annual if y <= d.year]
            if prior:
                eps_ttm = annual[max(prior)]
        if eps_ttm is None or eps_ttm <= 0:
            continue

        pe = price / eps_ttm
        if 0 < pe < max_pe:
            out.append((d, round(pe, 2)))
    return out
