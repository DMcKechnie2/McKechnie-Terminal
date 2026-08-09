"""Offline tests for the Macrotrends scrape, its year bucketing and its gate.

yfinance returns five annual columns, and Yahoo's fundamentals-timeseries
endpoint caps at four however wide a window it is given, so Macrotrends is the
only source of a longer history. Everything here runs offline: `scrape_macrotrends`
is exercised against a captured chartData payload, and the merge helpers are
already pure. Live checks belong in test_live.py behind the `network` marker.
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402


# Apple's real net-income series as Macrotrends serves it, captured whole. Values
# are billions. Note v1 on each row repeats v2 from the row above — that stagger
# is the point of the fixture, and the identity test below re-proves it if this
# is ever recaptured.
_AAPL_NET_INCOME = [
    {'date': '2012-09-30', 'v1': 25.922, 'v2': 41.733, 'v3': 60.99},
    {'date': '2013-09-30', 'v1': 41.733, 'v2': 37.037, 'v3': -11.25},
    {'date': '2014-09-30', 'v1': 37.037, 'v2': 39.51,  'v3': 6.68},
    {'date': '2015-09-30', 'v1': 39.51,  'v2': 53.394, 'v3': 35.14},
    {'date': '2016-09-30', 'v1': 53.394, 'v2': 45.687, 'v3': -14.43},
    {'date': '2017-09-30', 'v1': 45.687, 'v2': 48.351, 'v3': 5.83},
    {'date': '2018-09-30', 'v1': 48.351, 'v2': 59.531, 'v3': 23.12},
    {'date': '2019-09-30', 'v1': 59.531, 'v2': 55.256, 'v3': -7.18},
    {'date': '2020-09-30', 'v1': 55.256, 'v2': 57.411, 'v3': 3.9},
    {'date': '2021-09-30', 'v1': 57.411, 'v2': 94.68,  'v3': 64.92},
    {'date': '2022-09-30', 'v1': 94.68,  'v2': 99.803, 'v3': 5.41},
    {'date': '2023-09-30', 'v1': 99.803, 'v2': 96.995, 'v3': -2.81},
    {'date': '2024-09-30', 'v1': 96.995, 'v2': 93.736, 'v3': -3.36},
    {'date': '2025-09-30', 'v1': 93.736, 'v2': 112.01, 'v3': 19.5},
]


@pytest.fixture
def mt_rows(monkeypatch):
    """Serve a canned chartData payload in place of the live scrape."""
    def _install(rows):
        monkeypatch.setattr(terminal, '_mt_chart_rows', lambda *a, **k: rows)
    return _install


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

def test_fixture_satisfies_the_v1_is_prior_year_identity():
    """The premise every other test here rests on: v1 is the PRIOR year's value.

    Each row's v1 equals the row above's v2, and v3 is the change between them.
    If this fixture is recaptured and this fails, Macrotrends changed its response
    shape and `scrape_macrotrends` needs rereading — without this check the rest
    of the file would keep passing against a payload that no longer means this.
    """
    for prev, row in zip(_AAPL_NET_INCOME, _AAPL_NET_INCOME[1:]):
        assert row['v1'] == pytest.approx(prev['v2'])
        growth = (row['v2'] - row['v1']) / abs(row['v1']) * 100
        assert row['v3'] == pytest.approx(growth, abs=0.02)


def test_parser_reads_v2_not_v1(mt_rows):
    """Apple earned $93.736B in fiscal 2024 and $112.01B in fiscal 2025.

    Reading v1 dated every value a year late — the 2025 bar carried 2024's
    earnings — on the FCF, margin and share-count charts alike.
    """
    mt_rows(_AAPL_NET_INCOME)
    out = terminal.scrape_macrotrends('AAPL', 'earnings')
    assert out['2024-09-30'] == pytest.approx(93.736e9)
    assert out['2025-09-30'] == pytest.approx(112.01e9)


def test_parser_would_differ_if_it_read_v1(mt_rows):
    """Keeps the test above from passing by accident on a flat series."""
    mt_rows(_AAPL_NET_INCOME)
    out = terminal.scrape_macrotrends('AAPL', 'earnings')
    v1_reading = {r['date']: r['v1'] * 1e9 for r in _AAPL_NET_INCOME}
    assert out != v1_reading
    # Specifically: reading v1 dated each value one row late.
    assert v1_reading['2025-09-30'] == pytest.approx(out['2024-09-30'])


def test_parser_keys_on_the_period_end_date_not_the_year(mt_rows):
    """Years are assigned by `_mt_years` under the consumer's own fiscal rule.

    Collapsing to a year inside the scraper is what would strand a January filer
    on a different label from the yfinance column it belongs beside.
    """
    mt_rows(_AAPL_NET_INCOME)
    out = terminal.scrape_macrotrends('AAPL', 'earnings')
    assert len(out) == 14
    assert all(len(k) == 10 for k in out)


@pytest.mark.parametrize('metric,expected', [
    ('earnings', 112.01e9),      # billions
    ('fcf',      112.01e6),      # millions
    ('eps',      112.01),        # dollars per share
    ('margin',   112.01),        # already a percentage
])
def test_scale_is_applied_per_metric(mt_rows, metric, expected):
    """Same endpoint, same field name, a different unit for each series."""
    mt_rows(_AAPL_NET_INCOME)
    assert terminal.scrape_macrotrends('AAPL', metric)['2025-09-30'] == pytest.approx(expected)


def test_capex_is_reported_negative_and_charted_positive(mt_rows):
    mt_rows([{'date': '2025-09-30', 'v1': 0, 'v2': -9.447, 'v3': 0}])
    assert terminal.scrape_macrotrends('AAPL', 'capex')['2025-09-30'] == pytest.approx(9.447e6)


def test_earnings_keeps_a_loss_negative(mt_rows):
    """A loss-making year has to render below the axis, not flipped above it."""
    mt_rows([{'date': '2020-12-31', 'v1': 0, 'v2': -5.786, 'v3': 0}])
    assert terminal.scrape_macrotrends('GE', 'earnings')['2020-12-31'] == pytest.approx(-5.786e9)


def test_a_zero_share_count_is_dropped_rather_than_charted(mt_rows):
    mt_rows([{'date': '2019-12-31', 'v1': 0, 'v2': 0, 'v3': 0},
             {'date': '2020-12-31', 'v1': 0, 'v2': 1.5, 'v3': 0}])
    assert set(terminal.scrape_macrotrends('X', 'shares')) == {'2020-12-31'}


def test_the_request_asks_for_more_than_the_default_window(monkeypatch):
    """`yb` is what gets us more than fourteen years, and losing it is silent.

    Fourteen is the endpoint's default, not its limit. Drop this parameter and
    every chart quietly shortens to that default — no error, no empty series, no
    log line, and the merge still passes its gate because the years it validates
    against are the recent ones that come back either way. It reads as working
    code. The only thing that ever says otherwise is a bar that isn't there.
    """
    seen = {}

    class _Resp:
        text = 'var chartData = [];'
        def raise_for_status(self): pass

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(params or {})
        return _Resp()

    import requests
    monkeypatch.setattr(requests, 'get', fake_get)
    terminal._mt_chart_rows('AAPL', 'free-cash-flow', 'cash-flow-statement')
    assert int(seen['yb']) == terminal._MT_YEARS_BACK
    # Wide enough to be worth the parameter at all: the default already reaches
    # fourteen, so anything near it buys nothing.
    assert terminal._MT_YEARS_BACK >= 25


def test_transport_failure_raises_rather_than_reading_empty(monkeypatch):
    """`_mt_cached` stores whatever this returns for six hours.

    Returning {} for a failed fetch would pin every chart to five years for the
    rest of the TTL — the same distinction `_fetch_div_events` keeps.
    """
    def boom(*a, **k):
        raise OSError('connection reset')
    monkeypatch.setattr(terminal, '_mt_chart_rows', boom)
    with pytest.raises(OSError):
        terminal.scrape_macrotrends('AAPL', 'earnings')


# ---------------------------------------------------------------------------
# Year bucketing
# ---------------------------------------------------------------------------

def test_years_bucket_a_january_filer_the_way_yfinance_does():
    """Walmart's fiscal 2025 ends 2026-01-31 in both sources.

    Macrotrends dates the column identically, so one fiscal rule lands them on
    one label; bucketing on the bare year would put them a year apart.
    """
    assert terminal._mt_years({'2026-01-31': 21.893e9}) == {2025: 21.893e9}
    assert terminal._fiscal_year(pd.Timestamp('2026-01-31')) == 2025


def test_years_keep_the_calendar_year_when_asked():
    """Free cash flow alone keys on the bare calendar year of the period end."""
    assert terminal._mt_years({'2026-01-31': 1.0}, fiscal=False) == {2026: 1.0}


def test_years_skip_an_unparseable_date():
    assert terminal._mt_years({'not-a-date': 1.0, '2025-09-30': 2.0}) == {2025: 2.0}


# ---------------------------------------------------------------------------
# The validation gate
# ---------------------------------------------------------------------------

def test_a_series_shifted_by_a_year_is_rejected():
    """The failure reading v1 used to cause, caught at data level."""
    yf      = {2022: 100.0, 2023: 120.0, 2024: 145.0, 2025: 175.0}
    shifted = {2022: 80.0,  2023: 100.0, 2024: 120.0, 2025: 145.0}
    assert not terminal._mt_check(shifted, yf, terminal._MT_SERIES['eps'])['ok']


def test_a_series_with_a_unit_error_is_rejected():
    yf = {2023: 1.2e9, 2024: 1.3e9, 2025: 1.4e9}
    assert not terminal._mt_check({y: v * 1000 for y, v in yf.items()}, yf,
                                  terminal._MT_SERIES['earnings'])['ok']


def test_a_series_from_a_different_company_is_rejected():
    """T.TO is Telus; Macrotrends' own 'T' is AT&T. Nothing lines up."""
    telus = {2023: 1.65e9,  2024: 1.31e9,  2025: 1.04e9}
    att   = {2023: 14.40e9, 2024: 10.90e9, 2025: 12.30e9}
    assert not terminal._mt_check(att, telus, terminal._MT_SERIES['earnings'])['ok']


def test_a_series_agreeing_on_every_overlap_year_is_accepted():
    yf = {2023: 96.995e9, 2024: 93.736e9, 2025: 112.01e9}
    assert terminal._mt_check(dict(yf), yf, terminal._MT_SERIES['earnings'])['ok']


def test_one_restated_year_does_not_drop_the_whole_series():
    """yfinance carries restatements where Macrotrends is as-reported.

    Dropping fourteen years over a single divergent one would withhold the longer
    history from exactly the companies that most changed shape.
    """
    yf = {2022: 10.0e9, 2023: 11.0e9, 2024: 12.0e9, 2025: 13.0e9}
    report = terminal._mt_check({**yf, 2023: 4.0e9}, yf,
                                terminal._MT_SERIES['earnings'])
    assert report['ok'] and len(report['bad']) == 1


def test_one_divergent_year_out_of_two_is_not_tolerated():
    """A single bad year is noise among four and half the evidence among two."""
    yf = {2024: 12.0e9, 2025: 13.0e9}
    assert not terminal._mt_check({**yf, 2024: 4.0e9}, yf,
                                  terminal._MT_SERIES['earnings'])['ok']


def test_a_systematically_different_basis_is_rejected():
    """Amazon's free cash flow: both sources are right, they net leases apart.

    Splicing the older years on would put a definitional step change mid-chart,
    which reads as a real swing in the business.
    """
    yf = {2022: -16.89e9, 2023: 32.22e9, 2024: 32.88e9, 2025: 35.5e9}
    mt = {2022: -11.57e9, 2023: 36.81e9, 2024: 38.22e9, 2025: 42.0e9}
    assert not terminal._mt_check(mt, yf, terminal._MT_SERIES['fcf'])['ok']


def test_a_series_needs_something_to_agree_with():
    """One overlapping year is not evidence; the default asks for two."""
    yf = {2025: 100.0e9}
    mt = {2025: 100.0e9, 2024: 90.0e9, 2023: 80.0e9}
    spec = terminal._MT_SERIES['earnings']
    assert not terminal._mt_check(mt, yf, spec)['ok']
    assert terminal._mt_check(mt, yf, spec, min_overlap=1)['ok']


def test_the_gate_is_symmetric_about_a_near_zero_year():
    """Scaling the limit by the yfinance value alone would let 0-vs-anything pass."""
    yf = {2024: 0.0,    2025: 1.0e9}
    mt = {2024: 5.0e9,  2025: 1.0e9}
    report = terminal._mt_check(mt, yf, terminal._MT_SERIES['earnings'])
    assert [b['year'] for b in report['bad']] == [2024]
    assert not report['ok']


def test_a_share_count_below_the_resolution_floor_is_dropped():
    """Macrotrends rounds to the nearest million, so a 13M-share company comes
    back as flat runs of one number — `_MT_SHARES_MIN` is the only gate that
    applies when there is nothing to compare against."""
    spec = terminal._MT_SERIES['shares']
    assert not terminal._mt_check({y: 14_000_000.0 for y in range(2015, 2023)},
                                  {}, spec, min_overlap=0)['ok']
    assert terminal._mt_check({2012: 26.2e9}, {}, spec, min_overlap=0)['ok']


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------

def test_merge_never_overwrites_a_yfinance_year():
    yf = {2024: 93.7e9, 2025: 112.0e9}
    mt = {2023: 97.0e9, 2024: 93.6e9, 2025: 112.1e9}
    merged, src, _ = terminal._merge_macrotrends(yf, mt, 'earnings')
    assert merged[2024] == 93.7e9 and src[2024] == 'yf'
    assert merged[2023] == 97.0e9 and src[2023] == 'mt'


def test_merge_tags_every_year_with_a_source():
    """The charts fade a scraped bar, so a row without a tag would render wrong."""
    yf = {2024: 93.7e9, 2025: 112.0e9}
    mt = {2012: 41.7e9, 2024: 93.6e9, 2025: 112.1e9}
    _, src, _ = terminal._merge_macrotrends(yf, mt, 'earnings')
    assert set(src) == {2012, 2024, 2025}
    assert src[2012] == 'mt' and src[2024] == 'yf'


def test_merge_of_a_rejected_series_leaves_yfinance_untouched():
    yf = {2024: 93.7e9, 2025: 112.0e9}
    merged, src, report = terminal._merge_macrotrends(
        yf, {2012: 1.0, 2024: 1.0, 2025: 1.0}, 'earnings')
    assert not report['ok']
    assert merged == yf and set(src.values()) == {'yf'}


def test_merge_without_any_macrotrends_data_is_a_no_op():
    yf = {2024: 93.7e9, 2025: 112.0e9}
    merged, src, _ = terminal._merge_macrotrends(yf, {}, 'earnings')
    assert merged == yf and set(src.values()) == {'yf'}


def test_merge_can_widen_the_tolerance_for_a_derived_series():
    """A derived EPS is net income over an average share count, which sits a few
    percent off the as-reported diluted figure without either being wrong."""
    yf = {2023: 1.00, 2024: 1.10, 2025: 1.20}
    mt = {2022: 0.90, 2023: 1.12, 2024: 1.23, 2025: 1.34}
    assert not terminal._merge_macrotrends(yf, mt, 'eps')[2]['ok']
    assert terminal._merge_macrotrends(yf, mt, 'eps', tol_rel=0.15)[2]['ok']


# ---------------------------------------------------------------------------
# Concurrency and caching
# ---------------------------------------------------------------------------

def test_a_dotted_ticker_starts_no_scrapes(monkeypatch):
    """Macrotrends carries no TSX listing, and the base symbol is a different
    company — 'T.TO' is Telus where Macrotrends' 'T' is AT&T."""
    def boom(*a, **k):
        raise AssertionError('should not have scraped a dotted ticker')
    monkeypatch.setattr(terminal, '_mt_cached', boom)
    scrapes = terminal._start_macrotrends('T.TO')
    assert all(terminal._mt_result(scrapes, m) == {} for m in terminal._MT_SERIES)


def test_scrapes_run_concurrently(monkeypatch):
    """Five scrapes at ten seconds each, run one after another, is more than the
    whole route deadline."""
    import time
    monkeypatch.setattr(terminal, '_mt_cached',
                        lambda t, m: (time.sleep(0.3), {'2025-09-30': 1.0})[1])
    started = time.monotonic()
    scrapes = terminal._start_macrotrends('AAPL')
    results = [terminal._mt_result(scrapes, m) for m in terminal._MT_LOOKUP_METRICS]
    elapsed = time.monotonic() - started
    assert all(r == {'2025-09-30': 1.0} for r in results)
    assert elapsed < 0.3 * len(terminal._MT_LOOKUP_METRICS) / 2


def test_reads_share_one_deadline_rather_than_one_each():
    """Per-key timeouts compound: five reads at twelve seconds each is a minute
    against a twenty-five second route deadline."""
    import concurrent.futures as cf
    import time
    never = cf.Future()
    scrapes = terminal._MtScrapes({m: never for m in terminal._MT_LOOKUP_METRICS},
                                  budget=0.4)
    started = time.monotonic()
    assert all(scrapes.get(m) == {} for m in terminal._MT_LOOKUP_METRICS)
    assert time.monotonic() - started < 1.5


def test_a_second_lookup_is_served_from_the_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal, 'scrape_macrotrends',
                        lambda t, m: (calls.append((t, m)), {'2025-09-30': 1.0})[1])
    terminal._mt_cache.clear()
    try:
        assert terminal._mt_cached('AAPL', 'earnings') == {'2025-09-30': 1.0}
        assert terminal._mt_cached('AAPL', 'earnings') == {'2025-09-30': 1.0}
        assert len(calls) == 1
    finally:
        terminal._mt_cache.clear()


def test_a_failed_scrape_is_not_cached(monkeypatch):
    """Caching a transient failure would pin every chart to five years for six
    hours — the rule `_fetch_div_events` already follows."""
    calls = []

    def flaky(tkkr, metric):
        calls.append(metric)
        if len(calls) == 1:
            raise OSError('connection reset')
        return {'2025-09-30': 1.0}

    monkeypatch.setattr(terminal, 'scrape_macrotrends', flaky)
    terminal._mt_cache.clear()
    try:
        with pytest.raises(OSError):
            terminal._mt_cached('AAPL', 'earnings')
        assert terminal._mt_cached('AAPL', 'earnings') == {'2025-09-30': 1.0}
        assert len(calls) == 2
    finally:
        terminal._mt_cache.clear()
