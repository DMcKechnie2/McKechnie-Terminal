"""Offline tests for the quarterly view.

Macrotrends serves the same series by quarter off the same endpoint — `freq=Q`
instead of `freq=A` — and `yb=40` reaches 157 quarters for Apple, back to 1987.
Two things about that are load-bearing and neither announces itself when it
breaks, which is what most of this file is about:

  * v2 is still the labelled period's own value at both frequencies, but v1 is
    not the same field. Annually it is the prior year; quarterly it is the
    trailing twelve months. Reading v1 here would put a TTM figure on a
    quarter's bar — four times too large and rising smoothly where the real
    series is seasonal.

  * asking for a cash-flow-statement series by quarter does not fail. It returns
    the *annual* payload byte for byte, so charting it produces forty annual
    bars relabelled Q1..Q4 with every number on them real.

Live checks belong in test_live.py behind the `network` marker.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402


# Apple's revenue as Macrotrends serves it quarterly, captured whole. Billions.
# v1 is the TTM — the sum of this row's v2 and the three above it — which the
# identity test below re-proves if this is ever recaptured.
_AAPL_REVENUE_Q = [
    {'date': '2024-12-31', 'v1': 395.760, 'v2': 124.300, 'v3': 3.95},
    {'date': '2025-03-31', 'v1': 400.366, 'v2': 95.359,  'v3': 5.08},
    {'date': '2025-06-30', 'v1': 408.625, 'v2': 94.036,  'v3': 9.63},
    {'date': '2025-09-30', 'v1': 416.161, 'v2': 102.466, 'v3': 7.94},
    {'date': '2025-12-31', 'v1': 435.617, 'v2': 143.756, 'v3': 15.65},
    {'date': '2026-03-31', 'v1': 451.442, 'v2': 111.184, 'v3': 16.60},
    {'date': '2026-06-30', 'v1': 466.823, 'v2': 109.417, 'v3': 16.36},
]


# --------------------------------------------------------------------------
# The field the parser reads
# --------------------------------------------------------------------------

def test_v1_is_the_ttm_not_the_prior_quarter(monkeypatch):
    """The fixture's own shape, asserted so a recapture cannot quietly change it.

    This is the fact that makes reading v2 mandatory. On the annual endpoint a
    v1/v2 mix-up shifts the series by one period; here it swaps a quarter for a
    trailing year.
    """
    rows = _AAPL_REVENUE_Q
    for i in range(3, len(rows)):
        ttm = sum(r['v2'] for r in rows[i - 3:i + 1])
        assert abs(ttm - rows[i]['v1']) < 0.01, rows[i]['date']
    # And v1 is emphatically not the previous row's v2, which is what it means
    # on the annual endpoint.
    assert rows[-1]['v1'] != pytest.approx(rows[-2]['v2'])


def test_scrape_reads_v2_at_quarterly_frequency(monkeypatch):
    monkeypatch.setattr(terminal, '_mt_chart_rows',
                        lambda *a, **k: list(_AAPL_REVENUE_Q))
    out = terminal.scrape_macrotrends('AAPL', 'revenue', freq='Q')
    # v2 x the series' billions scale, never v1.
    assert out['2026-06-30'] == pytest.approx(109.417e9)
    assert out['2025-12-31'] == pytest.approx(143.756e9)
    # 466.823e9 is v1 on the last row — the TTM. If it appears, the parser
    # regressed to the field that is only correct on the annual endpoint.
    assert 466.823e9 not in out.values()


def test_quarterly_request_carries_freq_and_yb(monkeypatch):
    """Losing either parameter is silent.

    Without `freq` the endpoint serves annual columns under a quarterly request;
    without `yb` it serves its fourteen-year default. Neither errors, neither
    empties the series, and both shorten every chart with no log line.
    """
    seen = {}

    class _Resp:
        text = 'var chartData = [{"date":"2026-06-30","v1":1,"v2":2,"v3":3}];'
        def raise_for_status(self): pass

    import requests as req
    monkeypatch.setattr(req, 'get',
                        lambda url, **kw: (seen.update(kw.get('params') or {}), _Resp())[1])
    terminal._mt_chart_rows('AAPL', 'revenue', 'income-statement', freq='Q')
    assert seen['freq'] == 'Q'
    assert seen['yb'] == terminal._MT_YEARS_BACK


# --------------------------------------------------------------------------
# The annual-payload-under-freq=Q guard
# --------------------------------------------------------------------------

def _annual_dates(n=39, month=9):
    return {f'{1987 + i}-{month:02d}-30': float(i) for i in range(n)}


def _quarterly_dates(years=8):
    out = {}
    for y in range(2018, 2018 + years):
        for m in (3, 6, 9, 12):
            out[f'{y}-{m:02d}-30'] = 1.0
    return out


def test_an_annual_series_is_refused_under_freq_q():
    """The failure this exists to catch is silent and total.

    `free-cash-flow` with freq=Q returns thirty-nine rows twelve months apart.
    Charted as quarters that is a company which grew for four decades without
    one down quarter, and every value on it is a real figure — nothing
    downstream can tell.
    """
    with pytest.raises(ValueError, match='annual series'):
        terminal._mt_assert_quarterly(_annual_dates(), 'AAPL', 'fcf')


def test_a_genuine_quarterly_series_passes():
    terminal._mt_assert_quarterly(_quarterly_dates(), 'AAPL', 'revenue')


def test_a_series_too_short_to_judge_is_refused():
    """Two rows cannot be told apart from an annual pair, and two bars is not a
    chart either. The youngest company measured still returns 31 quarters."""
    with pytest.raises(ValueError):
        terminal._mt_assert_quarterly({'2026-03-31': 1.0, '2026-06-30': 2.0},
                                      'NEW', 'revenue')


def test_the_guard_runs_on_a_quarterly_scrape(monkeypatch):
    """Wired up, not merely defined — and it must raise rather than return {},
    so `_TtlCache` cannot hold a broken series for six hours."""
    monkeypatch.setattr(terminal, '_mt_chart_rows', lambda *a, **k: [
        {'date': d, 'v1': 0, 'v2': v, 'v3': 0} for d, v in sorted(_annual_dates().items())
    ])
    with pytest.raises(ValueError):
        terminal.scrape_macrotrends('AAPL', 'fcf', freq='Q')
    # ...and the same payload is perfectly fine when annual was what was asked
    # for, so the guard is not just rejecting the fixture.
    assert terminal.scrape_macrotrends('AAPL', 'fcf', freq='A')


def test_cash_flow_series_are_not_requested_quarterly():
    """`free-cash-flow` returns the annual payload and `capital-expenditures`
    404s, on every ticker measured. Not asking is what keeps the guard above a
    backstop for a future regression rather than a thing that fires every time
    somebody opens the toggle."""
    assert 'fcf' not in terminal._MT_QUARTERLY_METRICS
    assert 'capex' not in terminal._MT_QUARTERLY_METRICS
    # Every metric that IS asked for has to be a real series with a spec.
    for m in terminal._MT_QUARTERLY_METRICS:
        assert m in terminal._MT_SERIES
        assert terminal._MT_SERIES[m].statement == 'income-statement'


# --------------------------------------------------------------------------
# Keying and labelling
# --------------------------------------------------------------------------

def test_quarter_key_is_year_month():
    import pandas as pd
    assert terminal._quarter_key(pd.Timestamp('2026-06-30')) == '2026-06'
    # The day is deliberately dropped: 52/53-week filers end quarters on a
    # weekday, and keying on the exact date would stake the whole merge on both
    # sources normalising to month end forever. A mismatch there is silent —
    # the overlap falls to zero and every series is dropped with nothing on
    # screen to say why.
    assert terminal._quarter_key(pd.Timestamp('2026-06-27')) == '2026-06'


def test_quarter_keys_sort_chronologically():
    """`_q_rows` sorts on the raw key rather than parsing it, which only works
    because the month is zero-padded."""
    keys = ['2026-01', '2025-12', '2026-10', '2026-02']
    assert sorted(keys) == ['2025-12', '2026-01', '2026-02', '2026-10']


@pytest.mark.parametrize('fye,year,month,want', [
    # Apple: fiscal year ends September, so FY2026 runs Oct 2025 - Sep 2026.
    (9,  2025, 12, (2026, 1)),
    (9,  2026, 3,  (2026, 2)),
    (9,  2026, 6,  (2026, 3)),
    (9,  2026, 9,  (2026, 4)),
    # A calendar filer.
    (12, 2026, 3,  (2026, 1)),
    (12, 2026, 12, (2026, 4)),
    # Walmart: fiscal year ends January.
    (1,  2026, 1,  (2026, 4)),
    (1,  2026, 4,  (2027, 1)),
])
def test_fiscal_quarter_mapping(fye, year, month, want):
    assert terminal._fiscal_quarter(year, month, fye) == want


def test_fiscal_quarter_is_not_the_annual_rule():
    """`_fiscal_year` buckets a year on a fixed April cut. That is right for an
    annual column and wrong for three quarters in four of a non-calendar filer:
    Apple's December 2025 quarter is FY2026 Q1, and `_fiscal_year` files it as
    2025 — a year early, next to a bar it actually follows."""
    import datetime
    assert terminal._fiscal_year(datetime.date(2025, 12, 31)) == 2025
    assert terminal._fiscal_quarter(2025, 12, 9)[0] == 2026


def test_quarter_label():
    assert terminal._quarter_label('2026-06', 9) == "Q3 '26"
    assert terminal._quarter_label('2025-12', 9) == "Q1 '26"
    assert terminal._quarter_label('2026-01', 1) == "Q4 '26"


def test_mt_quarters_rekeys_by_period_end():
    out = terminal._mt_quarters({'2026-06-30': 5.0, '2026-03-31': 4.0})
    assert out == {'2026-06': 5.0, '2026-03': 4.0}


# --------------------------------------------------------------------------
# Row shape
# --------------------------------------------------------------------------

def test_q_rows_shape_and_order():
    merged = {'2026-06': 3.0, '2025-12': 1.0, '2026-03': 2.0}
    src    = {'2026-06': 'yf', '2025-12': 'mt', '2026-03': 'mt'}
    rows = terminal._q_rows(merged, src, 9, lambda v: f'${v:.2f}')
    assert [r['period'] for r in rows] == ['2025-12', '2026-03', '2026-06']
    assert [r['src'] for r in rows] == ['mt', 'mt', 'yf']
    assert rows[-1]['label'] == "Q3 '26"
    assert rows[-1]['raw'] == 3.0
    assert rows[-1]['value'] == '$3.00'
    # `year` must be absent: the frontend reads `label ?? year`, and a row
    # carrying both would let a quarterly series render under a year's label.
    assert 'year' not in rows[-1]


def test_q_rows_extra_key_carries_the_margin_alias():
    rows = terminal._q_rows({'2026-06': 27.23}, {}, 9,
                            lambda v: f'{v:.2f}%', extra_key='margin')
    assert rows[0]['margin'] == 27.23 == rows[0]['raw']


# --------------------------------------------------------------------------
# The merge gate, reused unchanged
# --------------------------------------------------------------------------

def test_the_gate_works_on_quarter_keys():
    """`_merge_macrotrends` and `_mt_check` never look at what a key means, which
    is why quarter keys go through the same take-or-drop-whole rule with the same
    tolerances instead of a second copy of it."""
    yf = {'2026-03': 111.184e9, '2026-06': 109.417e9}
    mt = {'2025-12': 143.756e9, '2026-03': 111.184e9, '2026-06': 109.417e9}
    merged, src, report = terminal._merge_macrotrends(yf, mt, 'revenue')
    assert report['ok']
    assert merged['2025-12'] == 143.756e9
    assert src['2025-12'] == 'mt'
    # A yfinance quarter is never overwritten by a scraped one.
    assert src['2026-06'] == 'yf'


def test_a_disagreeing_quarterly_series_is_dropped_whole():
    yf = {'2026-03': 100e9, '2026-06': 100e9}
    mt = {'2025-12': 50e9, '2026-03': 150e9, '2026-06': 150e9}
    merged, src, report = terminal._merge_macrotrends(yf, mt, 'revenue')
    assert not report['ok']
    assert '2025-12' not in merged   # nothing spliced in from a rejected series


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """A signed-in client whose per-user files live in a fresh temp directory."""
    monkeypatch.setattr(terminal, '_USER_DATA_ROOT', str(tmp_path / 'users'))
    monkeypatch.setattr(terminal, '_user_stores', {})
    return terminal.app.test_client()


@pytest.mark.parametrize('raw', ['', '..%2Fetc%2Fpasswd', 'AAPL%20%26%20calc', '%21%21%21'])
def test_quarterly_route_rejects_a_bad_ticker(client, raw):
    """`clean_ticker` at this boundary like every other. Nothing here reaches a
    path or an argv, but the cache key is a dict key and the value is echoed
    back in the 403 body."""
    r = client.get(f'/api/stock/quarterly?ticker={raw}')
    assert r.status_code == 400


def test_quarterly_route_refuses_a_blocked_symbol(client, monkeypatch):
    """Independently of /api/stock, which is reachable separately — and before
    the cache, which is shared by every account."""
    monkeypatch.setattr(terminal, '_blocked_set', lambda *a, **k: {'AAPL'})
    r = client.get('/api/stock/quarterly?ticker=AAPL')
    assert r.status_code == 403
    assert r.get_json()['blocked'] is True


def test_quarterly_route_is_rate_limited():
    assert 'get_stock_quarterly' in terminal._RATE_LIMITS
