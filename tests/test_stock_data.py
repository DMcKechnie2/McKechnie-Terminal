"""Offline tests for the balance sheet, share count, analyst and short-interest
builders that feed /api/stock.

These four take already-fetched frames rather than a Ticker, which is what makes
them testable here with no network and no monkeypatching. Live checks belong in
test_live.py behind the `network` marker.
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402

NAN = float('nan')


def _frame(rows, dates):
    """A statement frame shaped like yfinance's: line items down, period-end
    timestamps across, newest column first."""
    return pd.DataFrame([rows[k] for k in rows],
                        index=list(rows),
                        columns=[pd.Timestamp(d) for d in dates])


def _est_frame(rows):
    """yfinance's estimate frame — indexed by period code ('0y', '+1y', ...)."""
    return pd.DataFrame(rows).T


# ---------------------------------------------------------------------------
# Balance sheet
# ---------------------------------------------------------------------------

def test_balance_sheet_ratios_come_from_one_period():
    """Every snapshot metric must be read from the same column."""
    bs = _frame({
        'Total Assets':        [500.0, 400.0],
        'Current Assets':      [200.0, 300.0],
        'Current Liabilities': [100.0,  50.0],
    }, ['2025-12-31', '2024-12-31'])

    out = terminal._build_balance_sheet(bs, None, None)
    assert out['current_ratio'] == 2.0          # not 200/50 == 4.0
    assert out['as_of'] == '2025-12-31'


def test_balance_sheet_will_not_splice_a_missing_row_from_an_older_column():
    """The newest filing reports no current liabilities. Reaching back a year
    for that one row would produce a ratio that never appeared on any filing,
    so the metric drops out instead."""
    bs = _frame({
        'Total Assets':        [500.0, 400.0],
        'Current Assets':      [200.0, 300.0],
        'Current Liabilities': [NAN,    50.0],
    }, ['2025-12-31', '2024-12-31'])

    assert terminal._build_balance_sheet(bs, None, None)['current_ratio'] is None


def test_balance_sheet_prefers_the_most_recent_quarter():
    annual    = _frame({'Total Assets': [400.0]}, ['2025-09-30'])
    quarterly = _frame({'Total Assets': [500.0]}, ['2026-03-31'])

    out = terminal._build_balance_sheet(annual, quarterly, None)
    assert out['period'] == 'MRQ'
    assert out['as_of']  == '2026-03-31'
    assert out['assets'] == 500.0


def test_balance_sheet_handles_a_filer_with_no_current_accounts():
    """Banks and insurers file no current assets or liabilities at all — RY.TO
    has neither. The section must still return leverage and book value."""
    bs = _frame({
        'Total Assets':                            [2000.0],
        'Total Liabilities Net Minority Interest': [1800.0],
        'Stockholders Equity':                     [200.0],
        'Total Debt':                              [500.0],
        'Tangible Book Value':                     [150.0],
    }, ['2025-10-31'])

    out = terminal._build_balance_sheet(bs, None, None, shares_out=100.0, price=10.0)
    assert out['current_ratio'] is None
    assert out['debt_to_equity'] == 2.5
    assert out['tangible_book_per_share'] == 1.5
    assert out['price_to_tangible_book'] == round(10.0 / 1.5, 2)


def test_debt_to_equity_is_dropped_when_equity_is_negative():
    """Negative equity makes the quotient negative, which reads like very low
    leverage when it means the opposite."""
    bs = _frame({
        'Total Assets':        [1000.0],
        'Stockholders Equity': [-50.0],
        'Total Debt':          [800.0],
    }, ['2025-12-31'])

    assert terminal._build_balance_sheet(bs, None, None)['debt_to_equity'] is None


def test_liabilities_are_derived_when_yahoo_omits_the_row():
    bs = _frame({
        'Total Assets':        [1000.0],
        'Stockholders Equity': [300.0],
    }, ['2025-12-31'])

    out = terminal._build_balance_sheet(bs, None, None)
    assert out['liabilities'] == 700.0
    assert out['by_year'][-1]['liabilities'] == 700.0


def test_balance_sheet_sends_both_raw_and_display_values():
    """Charts read the raw float; parsing the formatted string back would
    quantise every point to 2dp of its unit."""
    bs = _frame({'Total Assets': [1_234_000_000.0]}, ['2025-12-31'])

    out = terminal._build_balance_sheet(bs, None, None)
    assert out['assets'] == 1_234_000_000.0
    assert out['assets_str'] == '$1.23B'
    assert out['by_year'][-1]['assets'] == 1_234_000_000.0


def test_interest_coverage_falls_back_to_the_latest_complete_year():
    """Yahoo drops Interest Expense from recent years for some filers. The
    figure carries its own year so it is never read as the snapshot date."""
    fin = _frame({
        'EBIT':             [300.0, 200.0],
        'Interest Expense': [NAN,    20.0],
    }, ['2025-12-31', '2024-12-31'])

    out = terminal._build_balance_sheet(None, None, fin)
    assert out['interest_coverage'] == 10.0
    assert out['interest_coverage_year'] == 2024


def test_balance_sheet_keys_are_present_even_when_nothing_is_reported():
    """A payload shape that varies by filer pushes 'missing or zero?' onto
    every consumer."""
    out = terminal._build_balance_sheet(None, None, None)
    assert out['by_year'] == []
    assert out['assets'] is None and out['assets_str'] == 'N/A'
    assert out['current_ratio'] is None
    assert out['interest_coverage'] is None


# ---------------------------------------------------------------------------
# Shares outstanding
# ---------------------------------------------------------------------------

def test_shares_history_never_lets_macrotrends_overwrite_yfinance():
    bs = _frame({'Ordinary Shares Number': [15_000_000_000.0]}, ['2025-12-31'])
    mt = {2025: 15_400_000_000.0, 2024: 15_100_000_000.0}

    rows = {r['year']: r for r in terminal._build_shares_history(bs, mt)['by_year']}
    assert rows[2025]['raw'] == 15_000_000_000.0
    assert rows[2025]['src'] == 'yf'
    assert rows[2024]['src'] == 'mt'            # gap-filling is still allowed


def test_shares_history_drops_a_macrotrends_series_too_coarse_to_chart():
    """Macrotrends rounds to the nearest million shares. For a 13M-share
    company that quantises whole runs of years to the same number, which charts
    as 'no change' when the count actually moved."""
    bs = _frame({'Ordinary Shares Number': [13_132_000.0, 12_590_000.0]},
                ['2024-12-31', '2025-12-31'])
    mt = {y: 14_000_000.0 for y in range(2015, 2023)}

    years = [r['year'] for r in terminal._build_shares_history(bs, mt)['by_year']]
    assert years == [2024, 2025]


def test_shares_history_keeps_macrotrends_when_the_count_is_large():
    bs = _frame({'Ordinary Shares Number': [15_000_000_000.0]}, ['2025-12-31'])
    mt = {2012: 26_226_000_000.0}

    out = terminal._build_shares_history(bs, mt)
    assert out['from_year'] == 2012
    assert out['change_pct'] < 0                # a buyback reads negative


def test_shares_history_reports_dilution_as_a_positive_change():
    bs = _frame({'Ordinary Shares Number': [110.0, 100.0]},
                ['2025-12-31', '2024-12-31'])
    assert terminal._build_shares_history(bs)['change_pct'] == 10.0


def test_shares_history_falls_back_to_share_issued():
    bs = _frame({'Share Issued': [500.0]}, ['2025-12-31'])
    assert terminal._build_shares_history(bs)['latest'] == 500.0


def test_shares_history_of_nothing_is_empty_not_an_error():
    assert terminal._build_shares_history(None, {}) == {'by_year': []}


# ---------------------------------------------------------------------------
# Analyst estimates
# ---------------------------------------------------------------------------

def test_analyst_growth_is_converted_from_yahoos_decimal():
    """yfinance hands `growth` over as a decimal (0.2054 = 20.54%) — the
    opposite convention to dividendYield, which arrives already scaled."""
    eps = _est_frame({'0y': {'avg': 8.76, 'growth': 0.2054, 'numberOfAnalysts': 41}})

    row = terminal._build_analyst({}, eps, None)['eps_estimates'][0]
    assert row['growth_pct'] == 20.54
    assert row['analysts'] == 41


def test_analyst_upside_is_measured_against_the_live_price():
    info = {'targetMeanPrice': 110.0, 'targetLowPrice': 90.0,
            'targetHighPrice': 130.0, 'recommendationKey': 'strong_buy'}

    out = terminal._build_analyst(info, None, None, price=100.0)
    assert out['upside_pct'] == 10.0
    assert out['rating'] == 'Strong Buy'
    assert out['eps_estimates'] == []


def test_revenue_estimates_are_money_and_eps_estimates_are_a_price():
    eps = _est_frame({'0y': {'avg': 8.76}})
    rev = _est_frame({'0y': {'avg': 4.788e11}})

    out = terminal._build_analyst({}, eps, rev)
    assert out['eps_estimates'][0]['avg_str'] == '$8.76'
    assert out['rev_estimates'][0]['avg_str'] == '$478.80B'


def test_analyst_estimates_keep_yahoos_period_order():
    eps = _est_frame({
        '+1y': {'avg': 9.73},
        '0q':  {'avg': 1.89},
        '0y':  {'avg': 8.76},
    })
    periods = [r['period'] for r in terminal._build_analyst({}, eps, None)['eps_estimates']]
    assert periods == ['0q', '0y', '+1y']


def test_analyst_without_a_price_reports_no_upside():
    out = terminal._build_analyst({'targetMeanPrice': 110.0}, None, None, price=None)
    assert 'upside_pct' not in out
    assert out['target_mean'] == 110.0


# ---------------------------------------------------------------------------
# Short interest
# ---------------------------------------------------------------------------

def test_short_percent_of_float_is_scaled_from_a_decimal():
    """shortPercentOfFloat arrives as 0.01 for 1%."""
    out = terminal._build_short_interest(
        {'sharesShort': 146_547_784, 'shortPercentOfFloat': 0.01})
    assert out['pct_of_float'] == 1.0


def test_short_percent_of_float_is_recomputed_when_yahoo_omits_it():
    """Yahoo leaves it null for most non-US listings — RY.TO reports a share
    count but no percentage."""
    out = terminal._build_short_interest(
        {'sharesShort': 7_852_304, 'floatShares': 1_388_470_896,
         'sharesOutstanding': 1_389_662_335, 'sharesShortPriorMonth': 7_457_794})
    assert out['pct_of_float'] == 0.57
    assert out['pct_of_outstanding'] == 0.57
    assert out['change_pct'] == 5.29            # short interest rose


def test_short_interest_date_is_read_as_utc():
    """Yahoo dates this at UTC midnight; reading it in local time rolls it back
    a day for anyone west of Greenwich."""
    out = terminal._build_short_interest(
        {'sharesShort': 1000, 'dateShortInterest': 1784073600})
    assert out['as_of'] == '2026-07-15'


def test_short_interest_survives_a_junk_date():
    out = terminal._build_short_interest(
        {'sharesShort': 1000, 'dateShortInterest': 'not-a-date'})
    assert out['as_of'] is None
    assert out['shares_short'] == 1000


def test_short_interest_is_empty_without_a_share_count():
    assert terminal._build_short_interest({'shortRatio': 2.4}) == {}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value,expected', [
    (15_408_000_000, '15.41B'),
    (247_000_000,    '247.00M'),
    (296_082,        '296.08K'),
    (None,           'N/A'),
])
def test_fmt_count_carries_no_dollar_sign(value, expected):
    """Share counts are not money — format_large_number would prefix a $."""
    assert terminal._fmt_count(value) == expected
