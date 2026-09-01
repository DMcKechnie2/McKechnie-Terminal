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
    for key in ('ticker', 'name', 'price', 'currency', 'financial_currency',
                'currency_symbol', 'financial_currency_symbol', 'same_currency',
                'market_cap', 'fcf_annual'):
        assert key in d, f'missing {key}'
    assert isinstance(d['price'], str)


def test_a_won_filer_is_not_labelled_in_dollars(client):
    """SK hynix files in won. Its revenue is genuinely in the tens of trillions
    KRW — the figure was never wrong, but rendering it as '$189.17T' made it
    read as the largest company in history."""
    d = client.get('/api/stock?ticker=000660.KS').get_json()
    assert d.get('financial_currency') == 'KRW'
    for field in ('market_cap', 'revenue_ttm', 'debt'):
        value = d.get(field)
        if value and value != 'N/A':
            assert '$' not in value, f'{field} still dollar-signed: {value}'
            assert value.startswith('₩'), f'{field} not in won: {value}'


def test_an_adr_labels_its_two_currencies_apart(client):
    """TSM trades in USD and files in TWD, so one symbol cannot serve the whole
    page: market cap is dollars, revenue is New Taiwan dollars."""
    d = client.get('/api/stock?ticker=TSM').get_json()
    assert d.get('currency') == 'USD'
    assert d.get('financial_currency') == 'TWD'
    assert d.get('same_currency') is False
    assert d['market_cap'].startswith('$')
    assert d['revenue_ttm'].startswith('NT$')
    # The cross-currency ratio is suppressed rather than reported ~31x off.
    assert (d.get('balance_sheet') or {}).get('price_to_tangible_book') is None


def test_market_cap_ships_raw_alongside_its_display_string(client):
    """The valuation calculator compares its equity value against the cap, so the
    float has to survive the trip — parsing '$4.41T' back would quantise every
    mega-cap to three digits."""
    d = client.get('/api/stock?ticker=AAPL').get_json()
    raw = d.get('market_cap_raw')
    assert isinstance(raw, (int, float)) and raw > 0
    # Same number, formatted. 2dp of a trillion is the string's own resolution.
    assert d['market_cap'] == terminal.format_large_number(raw, d['currency_symbol'])


@pytest.mark.parametrize('symbol', ['AEO', 'HD'])
def test_a_listing_yahoo_reports_no_cap_for_still_gets_one(client, symbol):
    """Yahoo omits `marketCap` for a stable minority of listings — from `info`
    and from the v7 quote endpoint alike — and omits `sharesOutstanding` with
    it, so nothing in the response derives one. American Eagle and Home Depot
    are two. Unfilled, the box reads 'N/A' and the valuation panel's margin of
    safety goes blank beside it, since MoS divides by the cap.

    Asserted against the price on the same payload rather than a fixed figure.
    Both are single-class issuers, so price x shares is the cap by definition
    whether Yahoo reported it or the fallback derived it — if Yahoo starts
    reporting these again the identity still holds and this keeps passing.
    """
    d = client.get(f'/api/stock?ticker={symbol}').get_json()
    cap, shares, price = d.get('market_cap_raw'), d.get('shares'), float(d['price'])
    assert cap and cap > 0, f'{symbol} still reports no market cap'
    assert d['market_cap'] != 'N/A'
    assert d['market_cap'] == terminal.format_large_number(cap, d['currency_symbol'])
    assert shares, f'{symbol} carries no share count'
    assert abs(shares * price - cap) / cap < 0.02


def test_a_dual_class_cap_is_not_reproducible_from_shares_outstanding(client):
    """The reason MoS reads the cap and not a share count. Yahoo reports BRK-B's
    B-class count against a marketCap covering both classes, so `shares x price`
    — what the calculator used to divide by — lands far under the reported cap.

    Asserted as an inequality, not a fixed gap: the two figures are on different
    bases, and the point is only that one cannot stand in for the other. If this
    ever starts passing at parity, Yahoo changed a basis and the fallback path in
    fillCalculators is worth rechecking.
    """
    d = client.get('/api/stock?ticker=BRK-B').get_json()
    cap, shares, price = d.get('market_cap_raw'), d.get('shares'), float(d['price'])
    assert cap and shares and price
    derived = shares * price
    assert derived < cap * 0.9, (
        f'shares x price ({derived:,.0f}) now reproduces the cap ({cap:,.0f})')


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
    ('revenue',  'Total Revenue'),
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
    for key in ('revenue_by_year', 'earnings_by_year', 'eps_by_year', 'fcf_annual'):
        rows = d.get(key) or []
        assert len(rows) >= 10, f'{key} came back with only {len(rows)} rows'
        assert all(r.get('src') in ('yf', 'mt') for r in rows), f'{key} row missing src'
        assert any(r['src'] == 'mt' for r in rows), f'{key} got no scraped years'


def test_earnings_and_eps_rows_carry_both_raw_and_value(client):
    """Charts read `raw`; parsing the formatted string back quantises every bar."""
    d = client.get('/api/stock?ticker=AAPL').get_json()
    for key in ('revenue_by_year', 'earnings_by_year', 'eps_by_year'):
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
    for key in ('revenue_by_year', 'earnings_by_year'):
        rows = d.get(key) or []
        assert rows, f'expected {key} for RY.TO'
        assert all(r.get('src') == 'yf' for r in rows)


def test_stock_lookup_stays_within_the_route_deadline(client):
    """Six concurrent scrapes now, where there were three. They share one
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


def test_a_listing_yahoo_has_no_ownership_for_reports_none(client):
    """Yahoo returns no ownership fields for most non-US listings and has no
    fallback (`major_holders` is empty for them too). The section must be absent
    rather than reading 0% institutional, which used to leave `retail` at 100%
    and draw a doughnut claiming GOOG.TO is entirely retail-held.

    Canary, not a rule about GOOG.TO: if Yahoo ever starts reporting these, the
    split simply has to be coherent instead of missing.
    """
    own = client.get('/api/stock?ticker=GOOG.TO').get_json().get('ownership')
    if own:
        assert own.get('institutional'), 'ownership present but institutional is 0/None'
    else:
        assert own == {}


def test_ownership_split_is_coherent_where_yahoo_reports_it(client):
    """The three parts either form a whole or say they cannot."""
    own = client.get('/api/stock?ticker=AAPL').get_json().get('ownership')
    assert own, 'AAPL should have ownership data'
    assert own['exceeds_outstanding'] is False
    total = own['institutional'] + own['insider'] + own['retail']
    assert abs(total - 100) < 0.01, f'parts sum to {total}, not 100'


def test_an_adr_still_reports_float_and_shares_out_on_different_bases():
    """The precondition `_FLOAT_BASIS_MAX` exists for. Yahoo quotes TSM's
    `floatShares` on the ordinary-share basis (37.8B) and everything else on the
    ADR basis (5.2B) — a float 7.3x the share count, which is impossible when
    both count the same thing.

    Canary, not a rule about TSM: if Yahoo ever puts the two on one basis the
    guard stops firing on its own, and this is how we find out rather than
    wondering later why a suppressed percentage came back.
    """
    info = terminal.yf.Ticker('TSM').info
    float_sh, out_sh = info.get('floatShares'), info.get('sharesOutstanding')
    assert float_sh and out_sh, 'TSM should report both share counts'
    assert float_sh > out_sh * terminal._FLOAT_BASIS_MAX, (
        f'float {float_sh:,} no longer exceeds the {out_sh:,} share count — '
        'Yahoo may have moved the two onto one basis')


def test_short_interest_percentages_stay_the_right_way_round(client):
    """The float is a subset of the shares outstanding, so a percentage *of
    float* can never sit below the percentage of shares outstanding. Dividing
    across two bases is exactly what breaks that: TSM's ordinary-share float
    puts 0.09% beside 0.64% of the receipts.

    Yahoo currently supplies `shortPercentOfFloat` for TSM, already on the ADR
    basis and passed through untouched, so what this pins is the served pair
    staying coherent — no live ADR exercises the fallback today.
    `test_short_percent_of_float_is_dropped_when_the_bases_disagree` covers that
    offline.
    """
    si = client.get('/api/stock?ticker=TSM').get_json().get('short_interest')
    assert si and si.get('pct_of_outstanding'), 'TSM should have short interest'
    if si.get('pct_of_float') is not None:
        assert si['pct_of_float'] >= si['pct_of_outstanding'] * 0.95, (
            f"{si['pct_of_float']}% of float sits below "
            f"{si['pct_of_outstanding']}% of shares outstanding — the two are "
            'being computed on different share bases')


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


# ── Index browser ─────────────────────────────────────────────────────────────
#
# The membership scrape is the fragile half of this feature: four Wikipedia
# pages, four different column names, and a failure mode that is a short table
# rather than an exception. These are the canary for that, in the same spirit as
# the Macrotrends-against-yfinance check above.

@pytest.fixture
def fresh_index():
    """Force a real scrape rather than reading the stored membership."""
    terminal._index_payload_cache.clear()
    terminal._index_members_store.save({})


@pytest.mark.parametrize('key, low, high', [
    ('sp500', 480, 520),    # 503 today; the index is "500" only nominally
    ('ndx',    95, 110),    # 100 companies, ~102 listings with dual classes
    ('dow',    30,  30),    # exactly thirty, by definition
    ('tsx60',  55,  65),
])
def test_membership_scrape_still_finds_the_components_table(fresh_index, key, low, high):
    """Catches the failure this whole area is built around: Wikipedia moving or
    renaming a table, which yields a short list rather than an error.

    The bounds are loose because an index genuinely gains and loses members;
    they are here to separate "a few names changed" from "we are now parsing
    the wrong table", which is always off by an order of magnitude.
    """
    rows = terminal._scrape_index_members(key)
    assert low <= len(rows) <= high, (
        f'{key}: parsed {len(rows)} members, expected {low}-{high}. The '
        f'components table at {terminal._INDEX_SOURCES[key]["url"]} has '
        f'probably moved or been renamed.'
    )
    assert all(r['full_ticker'] for r in rows)
    assert all(r['name'] for r in rows), f'{key}: a member came through unnamed'


def test_national_bank_survives_the_pandas_na_default(fresh_index):
    """NA is a ticker, and pandas reads it as a missing value by default.

    This is the one constituent that proves keep_default_na=False is still on
    the read_html call — without it the TSX 60 quietly returns 59 members.
    """
    symbols = {r['full_ticker'] for r in terminal._scrape_index_members('tsx60')}
    assert 'NA.TO' in symbols


@pytest.mark.parametrize('key', ['sp500', 'ndx', 'dow', 'tsx60'])
def test_index_prices_nearly_every_member(fresh_index, key):
    """A batch quote that silently stopped carrying a field would show up as a
    column of dashes on the page and nowhere else."""
    payload = terminal._build_index(key)
    rows = payload['rows']

    assert payload['count'] >= payload['members'] * 0.95, (
        f"{key}: only priced {payload['count']} of {payload['members']}"
    )
    assert all(r['price'] > 0 for r in rows)
    # Every listed company has a 52-week range; only earnings can be absent.
    assert sum(1 for r in rows if r['week52_pos'] is not None) >= len(rows) * 0.95
    # P/E is genuinely absent for a loss-making company — ~5% of the S&P 500.
    assert sum(1 for r in rows if r['pe'] is not None) >= len(rows) * 0.7
    # Market cap should be total, and is only because of the fast_info
    # backfill: Yahoo's quote endpoint alone leaves 26 of the S&P 500 and 4 of
    # the Dow — Home Depot, Exxon, Lowe's — with none at all. This reads 87%
    # on the Dow if that backfill is ever dropped, which matters because cap is
    # the table's default sort.
    assert sum(1 for r in rows if r['mkt_cap_raw']) >= len(rows) * 0.98


def test_a_tsx_index_is_priced_in_canadian_dollars(fresh_index):
    """The listing's own currency, not the '$' that would make a C$126B bank
    read as a US one."""
    rows = terminal._build_index('tsx60')['rows']
    assert {r['currency'] for r in rows} == {'CAD'}
    assert all(r['cur_symbol'] == 'C$' for r in rows)


def test_index_route_is_fast_once_warm(fresh_index):
    """Cold is a scrape plus five quote batches; warm must come off the cache."""
    client = terminal.app.test_client()
    assert client.get('/api/index/dow').status_code == 200

    start = time.time()
    r = client.get('/api/index/dow')
    assert r.status_code == 200 and time.time() - start < 0.5


# ── Exchange listing sources ──────────────────────────────────────────────────
#
# Same argument as the Wikipedia canaries above, and the same failure: a listing
# file that moves, changes its column names or starts returning an error page
# yields a short list rather than an exception. The `min` gate in
# _EXCHANGE_SOURCES catches the catastrophic case; these bounds catch the quiet
# one, where a parse still succeeds but on the wrong basis.
#
# Measured 2026-08-10: nasdaq 3402, nyse 2184, amex 268, tsx 2266, tsxv 1431.

@pytest.mark.parametrize('key, low, high', [
    ('nasdaq', 2400, 4600),
    ('nyse',   1500, 3000),
    ('amex',    150,  500),
    ('tsx',    1500, 3200),
    ('tsxv',    800, 2200),
])
def test_exchange_listing_source_still_parses(fresh_index, key, low, high):
    rows = terminal._scrape_exchange_members(key)
    assert low <= len(rows) <= high, (
        f'{key}: parsed {len(rows)} listings, expected {low}-{high}. The '
        f'listing source has probably changed shape.'
    )
    assert all(r['full_ticker'] for r in rows)
    assert all(r['name'] for r in rows), f'{key}: a listing came through unnamed'


def test_symdir_still_carries_the_columns_the_parser_reads(fresh_index):
    """The two US files are pipe-delimited with a header line. A renamed column
    is the failure that yields an empty venue with no error anywhere."""
    for which, columns in (('nasdaq', ('Symbol', 'Security Name', 'Test Issue', 'ETF')),
                           ('other', ('ACT Symbol', 'Security Name', 'Exchange',
                                      'Test Issue', 'ETF'))):
        rows = terminal._symdir_rows(which)
        assert rows, f'{which} listing file parsed to nothing'
        for column in columns:
            assert column in rows[0], f'{which} no longer carries {column!r}'


def test_the_common_stock_filter_still_removes_a_real_slice(fresh_index):
    """Rights, units, warrants and preferreds are ~16% of Nasdaq's symbols. If
    this drops to nothing the filter has stopped matching and the table fills
    with rows that have no price history, no P/E and no market cap."""
    raw   = [r for r in terminal._symdir_rows('nasdaq')
             if r.get('Test Issue') == 'N' and r.get('ETF') == 'N']
    kept  = [r for r in raw if terminal._is_common_stock(r['Security Name'])]
    assert raw, 'nasdaq listing file parsed to nothing'
    dropped = len(raw) - len(kept)
    assert 0.05 <= dropped / len(raw) <= 0.35, (
        f'the common-stock filter dropped {dropped} of {len(raw)} — it has '
        f'probably stopped matching, or started over-matching'
    )


def test_toronto_still_names_its_depositary_receipts(fresh_index):
    """The CDR flag rests entirely on TMX spelling it out in the company name.
    If that wording changes, 133 receipts silently reclaim the top of the TSX
    table with their underlying companies' market caps — Nvidia at C$6.8T above
    Royal Bank at C$409B."""
    rows = terminal._scrape_exchange_members('tsx')
    flagged = [r for r in rows if r.get('dr')]
    assert len(flagged) >= 50, (
        f'only {len(flagged)} TSX listings flagged as depositary receipts'
    )


def test_an_exchange_ranks_its_own_companies_first(fresh_index):
    """The end state that all of the above protects: sorted by market cap, the
    Toronto exchange must lead with Canadian banks and not with US mega-caps
    wrapped in CDRs."""
    rows = terminal._build_index('tsx')['rows']
    top  = sorted((r for r in rows if r['mkt_cap_raw']),
                  key=lambda r: -r['mkt_cap_raw'])[:10]
    tickers = {r['ticker'] for r in top}

    assert 'RY' in tickers, f'Royal Bank is not in the TSX top ten: {tickers}'
    assert not ({'NVDA', 'AAPL', 'MSFT', 'AMZN', 'GOOG'} & tickers), (
        f'a depositary receipt is being ranked as a Toronto company: {tickers}'
    )


@pytest.mark.parametrize('key', ['nasdaq', 'nyse'])
def test_an_exchange_prices_and_caps_nearly_everything(fresh_index, key):
    """Market cap is the default sort, and Yahoo omits it for ~260 NYSE
    equities — Exxon, Home Depot, McDonald's, Merck. Left null they sort below
    every micro-cap on the venue."""
    payload = terminal._build_index(key)
    rows    = payload['rows']

    assert len(rows) > 1500
    assert all(r['price'] > 0 for r in rows)
    # The share-count cache fills the gap over successive builds, so one cold
    # build is bounded at _INDEX_CAP_BACKFILL_MAX and does not reach 100%.
    terminal._index_payload_cache.clear()
    rows = terminal._build_index(key)['rows']
    capped = sum(1 for r in rows if r['mkt_cap_raw'])
    assert capped >= len(rows) * 0.97, (
        f'{key}: only {capped} of {len(rows)} carry a market cap'
    )


def test_an_exchange_payload_is_worth_compressing(fresh_index):
    """The whole-universe-in-one-response design rests on this ratio: ~876KB of
    JSON for Nasdaq, and the body is overwhelmingly repeated key names."""
    import gzip as _gz
    import json as _json

    body = _json.dumps(terminal._build_index('nasdaq')).encode()
    assert len(body) > 300_000
    assert len(body) / len(_gz.compress(body, 6)) >= 3.0


# --------------------------------------------------------------------------
# Quarterly view
# --------------------------------------------------------------------------

def test_macrotrends_still_serves_quarters_on_freq_q():
    """The canary for the whole quarterly view.

    Every way this breaks is silent. If Macrotrends stops honouring `freq=Q` the
    endpoint does not error — it serves the annual series, which is exactly what
    it already does for the cash-flow statement. If it stops honouring `yb` the
    series shortens to its fourteen-year default. Neither raises, neither empties
    the payload, and both leave a chart that looks entirely plausible.

    `_mt_assert_quarterly` catches the first inside the scrape, so this asserts
    the scrape succeeds *and* comes back deep. Apple has filed every quarter
    since 1987; anything under ~100 means the window silently narrowed.
    """
    out = terminal.scrape_macrotrends('AAPL', 'revenue', freq='Q')
    assert len(out) > 100, f'only {len(out)} quarters — check freq/yb'
    assert min(out) < '1995', f'series starts at {min(out)}; yb may be ignored'
    # A quarter, not a trailing twelve months: Apple's biggest quarter is well
    # under half its ~$466B year. Reading v1 instead of v2 would land ~4x here.
    assert max(out.values()) < 250e9


def test_the_cash_flow_statement_still_has_no_quarterly_series():
    """`_MT_QUARTERLY_METRICS` omits fcf and capex on the strength of this.

    If Macrotrends ever does publish them, this fails and the omission can be
    revisited — that is the point. Until then it documents *why* the two charts
    are five bars, so nobody re-adds them expecting depth.
    """
    rows = terminal._mt_chart_rows('AAPL', 'free-cash-flow', 'cash-flow-statement', freq='Q')
    dates = sorted(r['date'] for r in rows if r.get('date'))
    assert len(dates) >= 2
    gap = (int(dates[-1][:4]) - int(dates[-2][:4])) * 12 + (int(dates[-1][5:7]) - int(dates[-2][5:7]))
    assert gap >= 10, (
        'Macrotrends now serves a quarterly cash-flow series — _MT_QUARTERLY_METRICS '
        'can include fcf/capex and the five-bar caption can go'
    )


def test_quarterly_payload_builds_and_reconciles(client):
    """Four quarters must sum to the annual column the other tab already draws.

    That identity is what makes the toggle honest: the two views are the same
    filings at two resolutions, not two sources that happen to sit near each
    other. A unit slip or a v1/v2 regression breaks it immediately.
    """
    res = client.get('/api/stock/quarterly?ticker=AAPL')
    assert res.status_code == 200
    q = res.get_json()

    for key in ('revenue_by_q', 'earnings_by_q', 'margin_by_q', 'eps_by_q', 'shares_by_q'):
        assert len(q[key]) > 100, f'{key} came back with {len(q[key])} quarters'

    # Labels are fiscal quarters, and Apple's year ends in September.
    assert q['fiscal_year_end_month'] == 9
    assert q['revenue_by_q'][-1]['label'].startswith('Q')

    stock = client.get('/api/stock?ticker=AAPL').get_json()
    annual = {r['year']: r['raw'] for r in stock['revenue_by_year'] if r['year'] != 'TTM'}

    by_fy = {}
    for row in q['revenue_by_q']:
        year, month = int(row['period'][:4]), int(row['period'][5:7])
        fy, _ = terminal._fiscal_quarter(year, month, 9)
        by_fy.setdefault(fy, []).append(row['raw'])

    checked = 0
    for fy, quarters in by_fy.items():
        if len(quarters) != 4 or fy not in annual:
            continue
        assert sum(quarters) == pytest.approx(annual[fy], rel=0.01), f'FY{fy}'
        checked += 1
    assert checked >= 5, f'only reconciled {checked} fiscal years'


@pytest.mark.network
@pytest.mark.parametrize('symbol,fye_month', [
    ('MSFT', 6),    # fiscal year ends June
    ('AAPL', 9),    # ends September
    ('PEP',  12),   # the December majority
    ('LULU', 1),    # a retailer, ends January
])
def test_stock_payload_carries_the_fiscal_year_end(client, symbol, fye_month):
    """The guidance panel dates `FY2027` against this, so a wrong month is a
    span that is a whole year off with nothing on screen to give it away.

    LULU is the one that matters: it and NVIDIA both close in January and label
    their fiscal years the opposite way round, so the month has to be a real
    reading off the filer's own annual frame rather than anything inferred.
    """
    res = client.get(f'/api/stock?ticker={symbol}')
    assert res.status_code == 200
    assert res.get_json()['fiscal_year_end_month'] == fye_month
