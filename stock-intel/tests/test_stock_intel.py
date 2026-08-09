"""Tests for stock_intel.

The offline tests cover the logic that was actually wrong during development —
each one corresponds to a real bug found while validating against live data, and
they are the tests worth keeping.

Network tests are marked `network` and skipped by default:

    pytest tests/                     # offline only
    pytest tests/ -m network          # include live Yahoo Finance calls
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from stock_intel.core import fiscal_year, num
from stock_intel.metrics import (
    cagr,
    consecutive_growth_years,
    percentile_rank,
    safe_div,
    series_cagr,
    trend_direction,
)
from stock_intel.render import _money, _n, _pct
from stock_intel.sections import _classify_insider, _insider_role, _nonzero


# --------------------------------------------------------------------------
# num(): the single chokepoint for "is this a usable number?"
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (5, 5.0),
    ("5", 5.0),
    ("1,234.5", 1234.5),
    (None, None),
    ("", None),
    ("   ", None),
    (float("nan"), None),
    (float("inf"), None),
    (float("-inf"), None),
    ("abc", None),
    (0, 0.0),
])
def test_num_coercion(value, expected):
    assert num(value) == expected


def test_num_preserves_zero():
    """Zero is a real value and must survive; only unusable input becomes None."""
    assert num(0) == 0.0
    assert num(0) is not None


# --------------------------------------------------------------------------
# CAGR: must refuse degenerate bases rather than emit nonsense
# --------------------------------------------------------------------------

def test_cagr_normal():
    assert cagr(100, 121, 2) == pytest.approx(10.0)


@pytest.mark.parametrize("start,end", [
    (-100, 50),     # negative base: "150% growth" would be meaningless
    (0, 50),        # zero base: infinite
    (100, -50),     # negative end: no real root
    (100, 0),
])
def test_cagr_refuses_degenerate_bases(start, end):
    assert cagr(start, end, 3) is None


def test_series_cagr_uses_actual_span():
    """A 5-year window over 3 years of data must annualize over 3, not 5."""
    by_year = {2022: 100.0, 2023: 110.0, 2024: 121.0}
    assert series_cagr(by_year, 5) == pytest.approx(10.0)


def test_series_cagr_insufficient_data():
    assert series_cagr({2024: 100.0}, 3) is None
    assert series_cagr({}, 3) is None


# --------------------------------------------------------------------------
# Percentile: refuse to imply precision from a handful of points
# --------------------------------------------------------------------------

def test_percentile_requires_enough_observations():
    assert percentile_rank(5, [1, 2, 3, 4]) is None


def test_percentile_normal():
    assert percentile_rank(95, list(range(100))) == pytest.approx(95.0)


def test_percentile_ignores_unusable_points():
    dist = list(range(50)) + [float("nan"), None]
    assert percentile_rank(25, dist) is not None


# --------------------------------------------------------------------------
# safe_div / trend
# --------------------------------------------------------------------------

def test_safe_div_zero_denominator():
    assert safe_div(1, 0) is None
    assert safe_div(1, 1e-15) is None
    assert safe_div(None, 5) is None
    assert safe_div(10, 4) == 2.5


def test_trend_direction():
    assert trend_direction({1: 10.0, 2: 20.0, 3: 30.0}) == "rising"
    assert trend_direction({1: 30.0, 2: 20.0, 3: 10.0}) == "falling"
    assert trend_direction({1: 10.0, 2: 10.0, 3: 10.0}) == "flat"
    assert trend_direction({1: 10.0}) is None


def test_consecutive_growth_years():
    assert consecutive_growth_years({1: 1.0, 2: 2.0, 3: 3.0}) == 2
    assert consecutive_growth_years({1: 3.0, 2: 2.0, 3: 1.0}) == 0


# --------------------------------------------------------------------------
# Fiscal year mapping
# --------------------------------------------------------------------------

def test_fiscal_year_september_end_is_same_year():
    assert fiscal_year(dt.date(2025, 9, 30)) == 2025


def test_fiscal_year_january_end_is_prior_year():
    """A January 2025 year-end is FY2024, as retailers label it."""
    assert fiscal_year(dt.date(2025, 1, 31)) == 2024


# --------------------------------------------------------------------------
# Insider classification — every case here is a bug found against live data
# --------------------------------------------------------------------------

def test_repurchase_is_not_an_insider_purchase():
    """Regression: "repurchase" contains "purchase".

    Royal Bank's buyback rows were being counted as 50 insider *buys*, flipping
    the signal from net selling to net buying.
    """
    kind, label = _classify_insider(
        "Redemption, retraction, cancelation, repurchase at price 206.57 per share."
    )
    assert kind == "exclude"
    assert label == "corporate_buyback"


def test_disposition_under_purchase_plan_is_a_sale():
    """Regression: this phrase contains "purchase" but is a disposition."""
    kind, _ = _classify_insider("Disposition under a purchase/ownership plan")
    assert kind == "sell"


def test_disposition_in_public_market_is_a_sale():
    """Regression: the Canadian SEDI term was not matched at all, so real
    sells were silently dropped into the excluded bucket."""
    kind, _ = _classify_insider("Disposition in the public market at price 189.46 per share.")
    assert kind == "sell"


@pytest.mark.parametrize("text,expected_label", [
    ("Stock Award(Grant)", "award_or_vesting"),
    ("Stock Gift at price 0.00 per share.", "gift"),
    ("Exercise of options at price 63.74 per share.", "option_exercise_or_conversion"),
    ("Conversion of Exercise of derivative security", "option_exercise_or_conversion"),
    ("", "undisclosed"),
])
def test_non_market_events_are_excluded(text, expected_label):
    """Compensation events must never count as conviction."""
    kind, label = _classify_insider(text)
    assert kind == "exclude"
    assert label == expected_label


def test_genuine_purchase_and_sale():
    assert _classify_insider("Purchase at price 13.97 per share.")[0] == "buy"
    assert _classify_insider("Sale at price 295.14 per share.")[0] == "sell"


def test_insider_role_separates_large_holders():
    """A 10% holder's strategic stake is different evidence from a director's buy."""
    assert _insider_role("Beneficial Owner of more than 10% of a Class of Security") \
        == "beneficial_owner_10pct"
    assert _insider_role("Chief Executive Officer") == "officer_or_director"
    assert _insider_role("Director") == "officer_or_director"
    assert _insider_role(None) == "unknown"


# --------------------------------------------------------------------------
# Zero-versus-missing
# --------------------------------------------------------------------------

def test_nonzero_treats_sentinel_zero_as_missing():
    """Regression: yfinance returns grossMargins == 0.0 for every bank, which
    rendered as a real "0.0%" gross margin."""
    assert _nonzero(0) is None
    assert _nonzero(0.0) is None
    assert _nonzero(None) is None
    assert _nonzero(0.5) == 0.5
    assert _nonzero(-0.3) == -0.3


# --------------------------------------------------------------------------
# Rendering: None must never look like zero
# --------------------------------------------------------------------------

def test_renderers_never_show_none_as_zero():
    assert _money(None) == "n/a"
    assert _pct(None) == "n/a"
    assert _n(None) == "n/a"
    assert _money(0) == "0.00"
    assert _pct(0) == "0.0%"


def test_money_scaling_and_sign():
    assert _money(4.89e12) == "4.89T"
    assert _money(1.23e9) == "1.23B"
    assert _money(-5e6) == "-5.00M"
    assert _money(1.5e3) == "1.5K"


def test_pct_signed():
    assert _pct(3.7, signed=True) == "+3.7%"
    assert _pct(-3.7, signed=True) == "-3.7%"


# --------------------------------------------------------------------------
# Live smoke tests
# --------------------------------------------------------------------------

@pytest.mark.network
def test_live_lookup_us_ticker():
    from stock_intel import get_stock

    r = get_stock("AAPL", sections=["profile", "quote", "valuation"])
    assert not r.get("error")
    assert r["profile"]["currency"] == "USD"
    assert r["quote"]["price"] > 0
    assert "units" in r["meta"]


@pytest.mark.network
def test_live_balance_sheet_reconciles():
    """Regression: debt came from the annual statement while cash came from
    `info` (most recent quarter), so the figures did not add up."""
    from stock_intel import get_stock

    b = get_stock("AAPL", sections=["balance_sheet"])["balance_sheet"]
    if None in (b["total_debt"], b["cash_and_equivalents"], b["net_debt"]):
        pytest.skip("upstream did not report all three figures")
    assert b["net_debt"] == pytest.approx(
        b["total_debt"] - b["cash_and_equivalents"], rel=0.01
    )


@pytest.mark.network
def test_live_unresolvable_ticker_reports_failure():
    from stock_intel import get_stock

    r = get_stock("NOTAREALTICKER12345")
    assert r.get("error")
    assert r["data_quality"]["sections_returned"] == []


@pytest.mark.network
def test_live_financial_sector_is_flagged():
    """A bank's FCF yield is an artefact; the report must say so."""
    from stock_intel import get_stock

    r = get_stock("RY.TO", sections=["profile", "valuation", "cash_flow"])
    codes = {f["code"] for f in r["flags"]}
    assert "sector_metrics_not_applicable" in codes
