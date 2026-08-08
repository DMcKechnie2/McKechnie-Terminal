"""Live smoke tests. These hit Yahoo Finance, so they are opt-in:

    pytest -m network

They assert on shape and invariants, never on specific prices.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402

pytestmark = pytest.mark.network


@pytest.fixture
def client():
    terminal._stock_cache.clear()
    return terminal.app.test_client()


def test_stock_lookup_returns_full_payload(client):
    r = client.get('/api/stock?ticker=AAPL')
    assert r.status_code == 200
    d = r.get_json()
    assert 'error' not in d
    for key in ('ticker', 'name', 'price', 'currency', 'market_cap', 'fcf_annual'):
        assert key in d, f'missing {key}'
    assert isinstance(d['price'], str)


def test_cached_lookup_is_faster_and_keeps_the_same_shape(client):
    t0 = time.time()
    first = client.get('/api/stock?ticker=MSFT').get_json()
    cold = time.time() - t0

    t0 = time.time()
    second = client.get('/api/stock?ticker=MSFT').get_json()
    warm = time.time() - t0

    assert set(first) == set(second), 'cached payload has a different key set'
    assert isinstance(second['price'], str), 'cached price must stay a 2dp string'
    assert warm < cold, f'cached call ({warm:.2f}s) not faster than cold ({cold:.2f}s)'


def test_fcf_rows_all_carry_raw(client):
    """The chart reads `raw` rather than re-parsing the display string, so every
    row must carry it."""
    d = client.get('/api/stock?ticker=MSFT').get_json()
    rows = d.get('fcf_annual') or []
    assert rows, 'expected FCF history for MSFT'
    assert all(r.get('raw') is not None for r in rows)
    assert all(isinstance(r['raw'], (int, float)) for r in rows)


# ---------------------------------------------------------------------------
# Macrotrends
#
# yfinance gives five annual columns and Yahoo's fundamentals-timeseries endpoint
# caps at four however wide a window it is given, so these are the only checks
# that can catch the scrape drifting — an offline fixture can only prove the
# parser reads what it was told to read.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('metric,row', [
    ('earnings', 'Net Income'),
    ('eps',      'Diluted EPS'),
])
def test_macrotrends_agrees_with_yfinance_on_the_overlap(metric, row):
    """The v1/v2 canary.

    Macrotrends' chartData carries the prior year in v1 and the labelled year in
    v2. Reading v1 dated every scraped value a year late, and nothing offline can
    notice that the live response changed shape — this can.
    """
    import pandas as pd
    import yfinance as yf

    fin = yf.Ticker('AAPL').financials
    assert row in fin.index, f'yfinance no longer reports {row}'
    yf_by_year = {terminal._fiscal_year(d): float(v)
                  for d, v in fin.loc[row].items() if v is not None and not pd.isna(v)}

    mt_by_year = terminal._mt_years(terminal.scrape_macrotrends('AAPL', metric))
    overlap = sorted(set(mt_by_year) & set(yf_by_year))
    assert len(overlap) >= 3, f'expected an overlap to compare, got {overlap}'
    for year in overlap:
        assert mt_by_year[year] == pytest.approx(yf_by_year[year], rel=0.02), (
            f'{metric} {year}: macrotrends {mt_by_year[year]} vs yfinance {yf_by_year[year]}'
            ' — check whether v1/v2 swapped')


def test_macrotrends_aligns_on_a_january_fiscal_year():
    """Walmart's fiscal 2025 ends 2026-01-31. Both sources date the column that
    way, so the shared fiscal rule has to land them on one label."""
    import pandas as pd
    import yfinance as yf

    fin = yf.Ticker('WMT').financials
    yf_by_year = {terminal._fiscal_year(d): float(v)
                  for d, v in fin.loc['Net Income'].items()
                  if v is not None and not pd.isna(v)}
    mt_by_year = terminal._mt_years(terminal.scrape_macrotrends('WMT', 'earnings'))

    overlap = set(mt_by_year) & set(yf_by_year)
    assert overlap, 'January filer landed on no shared label at all'
    for year in overlap:
        assert mt_by_year[year] == pytest.approx(yf_by_year[year], rel=0.02)


def test_stock_payload_carries_more_than_ten_earnings_years(client):
    """The point of the whole exercise: yfinance alone tops out at five."""
    d = client.get('/api/stock?ticker=AAPL').get_json()
    for key in ('earnings_by_year', 'eps_by_year', 'fcf_annual'):
        rows = d.get(key) or []
        assert len(rows) >= 10, f'{key} came back with only {len(rows)} rows'
        assert all(r.get('src') in ('yf', 'mt') for r in rows), f'{key} row missing src'
        assert any(r['src'] == 'mt' for r in rows), f'{key} got no scraped years'


def test_earnings_and_eps_rows_carry_both_raw_and_value(client):
    """Charts read `raw`; parsing the formatted string back quantises every bar."""
    d = client.get('/api/stock?ticker=AAPL').get_json()
    for key in ('earnings_by_year', 'eps_by_year'):
        rows = d.get(key) or []
        assert rows, f'expected {key}'
        assert all(isinstance(r.get('raw'), (int, float)) for r in rows)
        assert all(isinstance(r.get('value'), str) and r['value'] for r in rows)


def test_scraped_fcf_years_never_carry_the_ocf_marker(client):
    """The frontend sniffs ' (OCF)' out of `value` to paint a bar amber and warn
    that the year is operating cash flow rather than free cash flow. A
    Macrotrends year is a real FCF figure and must never inherit that marker."""
    d = client.get('/api/stock?ticker=AAPL').get_json()
    rows = d.get('fcf_annual') or []
    assert rows, 'expected FCF history'
    assert not [r for r in rows if r.get('src') == 'mt' and '(OCF)' in r['value']]


def test_a_dotted_ticker_still_returns_a_clean_payload(client):
    """Macrotrends carries no TSX listing, so RY.TO gets yfinance's years only —
    and must not end up with a US company's history stapled on."""
    d = client.get('/api/stock?ticker=RY.TO').get_json()
    rows = d.get('earnings_by_year') or []
    assert rows, 'expected earnings history for RY.TO'
    assert all(r.get('src') == 'yf' for r in rows)


def test_stock_lookup_stays_within_the_route_deadline(client):
    """Five concurrent scrapes now, where there were three. They share one
    absolute deadline precisely so this stays true."""
    started = time.time()
    r = client.get('/api/stock?ticker=NVDA')
    assert r.status_code == 200
    assert time.time() - started < 25


def test_canadian_insider_rows_are_classified(client):
    """RY.TO returns 'Disposition in the public market' phrasing, which the old
    substring test dropped entirely — the panel rendered empty."""
    d = client.get('/api/insider-buying/RY.TO').get_json()
    assert 'excluded' in d
    assert d.get('transactions'), 'no insider trades classified for RY.TO'


def test_insider_excluded_counts_are_reported(client):
    d = client.get('/api/insider-buying/RY.TO').get_json()
    excluded = d.get('excluded') or {}
    assert excluded, 'expected buybacks/vesting to be reported, not silently dropped'
    assert all(isinstance(v, int) for v in excluded.values())


# ---------------------------------------------------------------------------
# Breaking-news feed
#
# Feed URLs rot. Reuters killed its public RSS outright, which is why it is not
# in the registry — this is the canary for the next one to go dark.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('entry', terminal._MARKET_FEEDS,
                         ids=[f[1] for f in terminal._MARKET_FEEDS])
def test_every_registered_feed_still_returns_items(entry):
    items = terminal._fetch_feed(entry)
    assert items, f'{entry[1]} returned nothing'
    assert all(i['title'] and i['url'] for i in items)


def test_market_news_route_returns_ranked_items():
    terminal._market_news_cache.clear()
    r = terminal.app.test_client().get('/api/news/market')
    assert r.status_code == 200
    d = r.get_json()
    assert d['items'], 'no items built from live feeds'
    assert d['stale'] is False
    assert d['sources_ok'] >= 3
    for key in ('title', 'url', 'source', 'category', 'pub_ts', 'breaking'):
        assert key in d['items'][0], f'missing {key}'
    # pub_ts is naive UTC by contract; the frontend appends 'Z'.
    ts = d['items'][0]['pub_ts']
    assert ts and 'Z' not in ts and '+' not in ts[10:]
