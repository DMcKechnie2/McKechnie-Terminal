"""Data access layer.

One lazily-loaded, error-capturing wrapper around yfinance. Every remote surface
is fetched at most once per Source instance, failures are recorded rather than
raised, and DataFrame rows are read through accessors that tolerate the
inconsistent row naming yfinance returns across companies.

Nothing in this module formats for display. Everything returns raw numbers or
None. `None` always means "not available"; it never means zero.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Sequence

import pandas as pd
import yfinance as yf

# yfinance logs delisting notices and HTTP errors at WARNING. Those are expected
# conditions here — a failed surface is already reported through `Source.errors`
# — and the noise is actively harmful when this package backs an MCP server
# whose stdio channel is a protocol stream.
for _noisy in ("yfinance", "peewee", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

__all__ = [
    "Source",
    "num",
    "fiscal_year",
    "utc_now_iso",
    "run_with_deadline",
    "TTLCache",
]


# --------------------------------------------------------------------------
# Scalar helpers
# --------------------------------------------------------------------------

def num(x: Any) -> float | None:
    """Coerce anything to a finite float, or None.

    Absorbs None, NaN, +/-inf, pandas NA, empty strings, and comma-formatted
    numbers. This is the single chokepoint for "is this a usable number?" —
    every value that reaches the output passes through here.
    """
    if x is None:
        return None
    try:
        if isinstance(x, str):
            x = x.strip().replace(",", "")
            if not x:
                return None
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def fiscal_year(d: date) -> int:
    """Map a statement period-end date to a fiscal year label.

    Period ends in Jan-Mar are attributed to the prior calendar year, which
    matches how companies with early-calendar year-ends label their fiscal
    years (a January 2025 year-end is FY2024). Apple's September year-end maps
    to the same calendar year, which is also correct.
    """
    return d.year if d.month >= 4 else d.year - 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_date(v: Any) -> date | None:
    """Normalize a DataFrame column label (usually a Timestamp) to a date."""
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        ts = pd.Timestamp(v)
    except (TypeError, ValueError):
        return None
    if ts is pd.NaT:
        return None
    return ts.date()


def _drop_tz(idx: pd.Index) -> pd.Index:
    """Make a DatetimeIndex tz-naive so it can be compared against plain dates."""
    tz = getattr(idx, "tz", None)
    return idx.tz_localize(None) if tz is not None else idx


# --------------------------------------------------------------------------
# Deadline-bounded execution
# --------------------------------------------------------------------------

def run_with_deadline(fn: Callable[[], Any], timeout: float, default: Any = None
                      ) -> tuple[Any, str | None]:
    """Run `fn` in a daemon thread and abandon it if it overruns `timeout`.

    yfinance calls are blocking with no timeout parameter of their own, and a
    single slow upstream response should not stall an agent's whole request.
    The thread is abandoned rather than killed (Python cannot kill threads); it
    is a daemon so it will not hold up interpreter shutdown.

    Returns (value, error_message). On timeout returns (default, "...").
    """
    box: list[Any] = [default]
    err: list[str | None] = [None]
    done = threading.Event()

    def _run() -> None:
        try:
            box[0] = fn()
        except Exception as exc:  # noqa: BLE001 - deliberate: report, never raise
            err[0] = f"{type(exc).__name__}: {exc}"
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    if not done.wait(timeout=timeout):
        return default, f"timed out after {timeout:.0f}s"
    return box[0], err[0]


class TTLCache:
    """Minimal thread-safe TTL cache.

    Agents tend to re-request the same ticker within a single reasoning loop.
    Caching keeps that from turning into repeated upstream fetches.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            ts, value = hit
            if time.time() - ts > self.ttl:
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# --------------------------------------------------------------------------
# Source
# --------------------------------------------------------------------------

class Source:
    """Lazily-loaded view over one ticker's yfinance surfaces.

    Each remote attribute is fetched at most once. Failures are captured in
    `.errors` keyed by surface name so the caller can report *why* a section is
    missing instead of emitting a misleading empty result.
    """

    # yfinance attribute name -> per-surface timeout in seconds
    _SURFACES = {
        "info": 20.0,
        "fast_info": 10.0,
        "income_stmt": 20.0,
        "quarterly_income_stmt": 20.0,
        "balance_sheet": 20.0,
        "quarterly_balance_sheet": 20.0,
        "cashflow": 20.0,
        "quarterly_cashflow": 20.0,
        "dividends": 15.0,
        "splits": 15.0,
        "earnings_dates": 15.0,
        "insider_transactions": 15.0,
        "recommendations": 15.0,
        "calendar": 15.0,
        "news": 15.0,
    }

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.strip().upper()
        self._ticker = yf.Ticker(self.symbol)
        self._loaded: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.errors: dict[str, str] = {}

    # -- raw surface access -------------------------------------------------

    def surface(self, name: str) -> Any:
        """Fetch a yfinance attribute once, recording any failure."""
        with self._lock:
            if name in self._loaded:
                return self._loaded[name]

        timeout = self._SURFACES.get(name, 20.0)
        value, err = run_with_deadline(
            lambda: getattr(self._ticker, name), timeout=timeout
        )
        if err:
            self.errors[name] = err
            value = None

        with self._lock:
            self._loaded[name] = value
        return value

    def prefetch(self, names: Iterable[str], max_workers: int = 8) -> None:
        """Warm several surfaces concurrently.

        Sections are independent, so fetching them serially would make a full
        lookup unnecessarily slow. Errors are absorbed by `surface()` itself.
        """
        todo = [n for n in names if n not in self._loaded]
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=min(max_workers, len(todo))) as pool:
            list(pool.map(self.surface, todo))

    # -- typed accessors ----------------------------------------------------

    @property
    def info(self) -> dict:
        v = self.surface("info")
        return v if isinstance(v, dict) else {}

    def info_num(self, *keys: str) -> float | None:
        """First key in `keys` that yields a usable number.

        yfinance moves fields between names across versions and asset classes,
        so nearly every read needs a fallback chain.
        """
        info = self.info
        for k in keys:
            v = num(info.get(k))
            if v is not None:
                return v
        return None

    def info_str(self, *keys: str) -> str | None:
        info = self.info
        for k in keys:
            v = info.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def fast(self, key: str) -> float | None:
        fi = self.surface("fast_info")
        if fi is None:
            return None
        try:
            return num(fi[key])
        except (KeyError, TypeError, AttributeError, IndexError):
            try:
                return num(getattr(fi, key))
            except Exception:  # noqa: BLE001
                return None

    def frame(self, name: str) -> pd.DataFrame | None:
        v = self.surface(name)
        if isinstance(v, pd.DataFrame) and not v.empty:
            return v
        return None

    def series(self, name: str) -> pd.Series | None:
        v = self.surface(name)
        if isinstance(v, pd.Series) and not v.empty:
            return v
        return None

    # -- statement row reading ---------------------------------------------

    def row(self, statement: str, *candidates: str) -> dict[date, float]:
        """Read the first present row from a statement as {period_end: value}.

        `candidates` is an ordered fallback chain of row labels. Returns {} when
        the statement is unavailable or none of the labels are present, which
        the caller must distinguish from "the value is zero".
        """
        df = self.frame(statement)
        if df is None:
            return {}
        for label in candidates:
            if label not in df.index:
                continue
            row = df.loc[label]
            # Duplicate index labels yield a DataFrame rather than a Series.
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            out: dict[date, float] = {}
            for col, val in row.items():
                v = num(val)
                d = _as_date(col)
                if v is not None and d is not None:
                    out[d] = v
            if out:
                return out
        return {}

    def annual(self, statement: str, *candidates: str) -> dict[int, float]:
        """Same as `row`, keyed by fiscal year instead of period-end date."""
        return {fiscal_year(d): v for d, v in sorted(self.row(statement, *candidates).items())}

    def latest(self, statement: str, *candidates: str) -> float | None:
        """Most recent reported value for a statement row."""
        data = self.row(statement, *candidates)
        if not data:
            return None
        return data[max(data)]

    def ttm(self, statement: str, *candidates: str) -> float | None:
        """Sum of the four most recent quarters, or None if fewer than four.

        Only meaningful for flow items (revenue, income, cash flow). Never call
        this on a balance-sheet stock item.
        """
        data = self.row(statement, *candidates)
        if len(data) < 4:
            return None
        recent = [v for _, v in sorted(data.items(), reverse=True)[:4]]
        return sum(recent)

    # -- time series --------------------------------------------------------

    def dividend_series(self) -> pd.Series | None:
        """Dividends indexed by tz-naive ex-date."""
        s = self.series("dividends")
        if s is None:
            return None
        s = s.copy()
        s.index = _drop_tz(s.index)
        return s

    def split_series(self) -> pd.Series | None:
        s = self.series("splits")
        if s is None:
            return None
        s = s.copy()
        s.index = _drop_tz(s.index)
        return s

    def history(self, period: str = "5y", interval: str = "1wk") -> pd.DataFrame | None:
        """Price history, cached per (period, interval)."""
        key = f"__history_{period}_{interval}"
        with self._lock:
            if key in self._loaded:
                return self._loaded[key]

        value, err = run_with_deadline(
            lambda: self._ticker.history(period=period, interval=interval,
                                         auto_adjust=True),
            timeout=25.0,
        )
        if err:
            self.errors[key.lstrip("_")] = err
            value = None
        if isinstance(value, pd.DataFrame) and not value.empty:
            value = value.copy()
            value.index = _drop_tz(value.index)
        else:
            value = None

        with self._lock:
            self._loaded[key] = value
        return value

    # -- diagnostics --------------------------------------------------------

    def is_resolvable(self) -> bool:
        """Whether this symbol resolved to a real instrument.

        yfinance returns a sparse dict rather than raising for unknown symbols,
        so a thin `info` is the signal that the ticker does not exist.
        """
        info = self.info
        if len(info) >= 20:
            return True
        # A tradable instrument will have at least a price from one surface.
        return any(v is not None for v in (
            self.info_num("currentPrice", "regularMarketPrice", "previousClose"),
            self.fast("lastPrice"),
        ))
