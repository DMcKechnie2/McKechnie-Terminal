"""stock_intel — structured equity data for AI agents.

Wraps Yahoo Finance (via yfinance) and returns decision-grade structured data:
raw numerics rather than display strings, a single declared unit convention,
derived metrics computed once and consistently, mechanical threshold flags with
cited evidence, and explicit reporting of what could not be retrieved.

Typical use:

    from stock_intel import get_stock, render_report

    report = get_stock("AAPL")
    print(render_report(report))       # compact digest, ~1/5 the tokens of JSON

Design rules, in priority order:

    1. `None` means unavailable. It never means zero.
    2. Units are declared and consistent: `*_pct` is a percentage, `*_ratio` is
       a bare multiple, money is unscaled reporting currency.
    3. A metric with insufficient support returns None rather than a plausible
       but unsound number (no CAGR from a negative base, no percentile from
       four observations).
    4. Failures are reported, never silently swallowed into an empty result.
    5. Facts and mechanical flags only. Nothing here recommends an action.
"""

from .core import Source, num
from .flags import evaluate as evaluate_flags
from .render import render_comparison, render_report
from .report import (
    ALL_SECTIONS,
    DEFAULT_SECTIONS,
    UNITS,
    clear_cache,
    compare_stocks,
    get_stock,
    search_ticker,
)

__version__ = "1.0.0"

__all__ = [
    "get_stock",
    "compare_stocks",
    "search_ticker",
    "render_report",
    "render_comparison",
    "evaluate_flags",
    "clear_cache",
    "Source",
    "num",
    "ALL_SECTIONS",
    "DEFAULT_SECTIONS",
    "UNITS",
    "__version__",
]
