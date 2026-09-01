"""Offline tests for the index browser: membership parsing, pricing, the route.

The two failures this file exists to catch are both silent. A Wikipedia table
that moves or is renamed yields an empty index rather than an error; and pandas
reads 'NA' — National Bank of Canada's symbol — as a missing value, which drops
a real constituent and leaves a table that still looks complete. Neither
surfaces as an exception anywhere, which is why they are asserted here.

Everything runs offline: the scrape is driven against captured HTML and the
pricing helpers are pure. The live check that all four sources still parse is in
test_live.py behind the `network` marker.
"""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402


# A Wikipedia components table cut down to the rows that matter: a plain symbol,
# one that pandas would read as NaN, two share classes that spell a dot at the
# exchange and a hyphen at Yahoo, a footnote marker, and the blank trailing row
# these tables carry.
_TABLE_HTML = """
<html><body>
<table class="wikitable">
<tr><th>Symbol</th><th>Company</th><th>Sector</th></tr>
<tr><td>AEM</td><td>Agnico Eagle Mines Limited</td><td>Basic Materials</td></tr>
<tr><td>NA</td><td>National Bank of Canada</td><td>Financial Services</td></tr>
<tr><td>TECK.B</td><td>Teck Resources Limited</td><td>Basic Materials</td></tr>
<tr><td>BIP.UN</td><td>Brookfield Infrastructure</td><td>Utilities</td></tr>
<tr><td>RY[a]</td><td>Royal Bank of Canada</td><td>Financial Services</td></tr>
<tr><td></td><td></td><td></td></tr>
</table>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


@pytest.fixture
def wiki(monkeypatch):
    """Serve captured HTML to _scrape_index_members, and lower the row gate.

    The gate is sized against a real index (50 rows for the TSX 60); the
    fixture is six. Lowering it here rather than padding the fixture keeps the
    gate itself testable — test_thin_table_raises drives it directly.
    """
    def serve(html, min_rows=3, key='tsx60'):
        monkeypatch.setattr(requests, 'get', lambda *a, **k: _FakeResponse(html))
        monkeypatch.setitem(terminal._INDEX_SOURCES, key,
                            {**terminal._INDEX_SOURCES[key], 'min': min_rows})
        return key
    return serve


# ── Symbol conversion ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, suffix, expected', [
    ('AAPL',    '',    'AAPL'),
    ('BRK.B',   '',    'BRK-B'),      # a class is a dot here and a hyphen at Yahoo
    ('BF.B',    '',    'BF-B'),
    ('AEM',     '.TO', 'AEM.TO'),
    ('TECK.B',  '.TO', 'TECK-B.TO'),
    ('BIP.UN',  '.TO', 'BIP-UN.TO'),
    ('RY[a]',   '.TO', 'RY.TO'),      # Wikipedia footnote marker
    (' aapl ',  '',    'AAPL'),
    ('',        '',    None),
    ('   ',     '',    None),
])
def test_symbol_conversion(raw, suffix, expected):
    assert terminal._index_symbol(raw, suffix) == expected


def test_symbol_refuses_a_nan_cell():
    """A float NaN must not become the ticker 'NAN'.

    keep_default_na=False should stop one ever reaching here, but clean_ticker
    accepts 'NAN' as a well-shaped symbol — so if that argument is ever
    dropped, the guard is what keeps a missing cell from being priced as a
    real listing instead of being skipped.
    """
    assert terminal._index_symbol(float('nan'), '') is None
    assert terminal._index_symbol('nan', '.TO') is None


def test_symbol_runs_through_clean_ticker():
    """Scraped text reaching a URL query goes through the same gate as any
    other ticker boundary here."""
    assert terminal._index_symbol('AAPL & calc', '') is None
    assert terminal._index_symbol('../../etc/passwd', '') is None


# ── Membership scrape ─────────────────────────────────────────────────────────

def test_na_is_a_ticker_not_a_missing_value(wiki):
    """pandas treats 'NA' as NaN by default, and NA is National Bank of Canada.

    Left at the default the TSX 60 came back with 59 members, the bank simply
    absent, and nothing anywhere reported a problem. 'NULL', 'NaN' and 'None'
    sit on the same default list and are all plausible symbols.
    """
    key  = wiki(_TABLE_HTML)
    rows = terminal._scrape_index_members(key)
    by_symbol = {r['full_ticker']: r['name'] for r in rows}

    assert 'NA.TO' in by_symbol
    assert by_symbol['NA.TO'] == 'National Bank of Canada'


def test_scrape_converts_and_drops_the_blank_row(wiki):
    key  = wiki(_TABLE_HTML)
    rows = terminal._scrape_index_members(key)

    assert [r['full_ticker'] for r in rows] == [
        'AEM.TO', 'NA.TO', 'TECK-B.TO', 'BIP-UN.TO', 'RY.TO',
    ]


def test_thin_table_raises(wiki):
    """A parse that comes back short raises rather than returning [].

    The caller cannot tell "this index is empty" from "the table moved", and
    only one of those may be allowed to overwrite a good stored list — the same
    distinction _fetch_div_events keeps.
    """
    key = wiki(_TABLE_HTML, min_rows=50)
    with pytest.raises(RuntimeError):
        terminal._scrape_index_members(key)


def test_missing_columns_raise(wiki):
    key = wiki('<html><body><table><tr><th>Nope</th></tr>'
               '<tr><td>x</td></tr></table></body></html>')
    with pytest.raises(RuntimeError):
        terminal._scrape_index_members(key)


# ── Membership cache ──────────────────────────────────────────────────────────

def _seed_members(key, rows, ts):
    terminal._index_members_store.save({key: {'rows': rows, 'ts': ts}})


def test_failed_refresh_keeps_the_stored_list(monkeypatch):
    """A Wikipedia outage degrades to a stale membership list, not an empty page.

    Persisted rather than memoised precisely so this survives a restart: an
    in-memory fallback would give a correct answer until the next deploy and an
    empty index after it.
    """
    stored = [{'full_ticker': 'AAPL', 'name': 'Apple Inc.'}]
    _seed_members('dow', stored, ts=0)          # ts=0 → stale, forces a refresh

    def boom(_key):
        raise RuntimeError('wikipedia moved the table')
    monkeypatch.setattr(terminal, '_scrape_index_members', boom)

    assert terminal._index_members('dow') == stored


def test_failed_refresh_with_nothing_stored_raises(monkeypatch):
    terminal._index_members_store.save({})
    monkeypatch.setattr(terminal, '_scrape_index_members',
                        lambda _k: (_ for _ in ()).throw(RuntimeError('down')))
    with pytest.raises(RuntimeError):
        terminal._index_members('dow')


def test_fresh_entry_is_not_refetched(monkeypatch):
    import time as _t
    stored = [{'full_ticker': 'MSFT', 'name': 'Microsoft'}]
    _seed_members('dow', stored, ts=_t.time())

    calls = []
    monkeypatch.setattr(terminal, '_scrape_index_members',
                        lambda k: calls.append(k) or [])
    assert terminal._index_members('dow') == stored
    assert calls == [], 'membership was re-scraped inside its TTL'


# ── Row building ──────────────────────────────────────────────────────────────

_MEMBER = {'full_ticker': 'AAPL', 'name': 'Apple Inc.'}


def _quote(**over):
    base = {
        'symbol': 'AAPL', 'regularMarketPrice': 150.0,
        'regularMarketChangePercent': 1.5, 'fiftyTwoWeekLow': 100.0,
        'fiftyTwoWeekHigh': 200.0, 'trailingPE': 30.0, 'marketCap': 3.0e12,
        'currency': 'USD', 'longName': 'Apple Inc.',
    }
    base.update(over)
    return base


def test_row_carries_every_requested_figure():
    row = terminal._index_row(_MEMBER, _quote())
    assert row['price'] == 150.0
    assert row['change'] == 1.5
    assert (row['week52_low'], row['week52_high']) == (100.0, 200.0)
    assert row['pe'] == 30.0
    assert row['mkt_cap'] == '$3.00T'


@pytest.mark.parametrize('field, key', [
    ('pe',          'trailingPE'),
    ('change',      'regularMarketChangePercent'),
    ('week52_low',  'fiftyTwoWeekLow'),
    ('week52_high', 'fiftyTwoWeekHigh'),
])
def test_missing_figures_are_null_not_zero(field, key):
    """Yahoo omits trailingPE for a loss-making company. A 0.0 in that column
    reads as a real and extraordinarily cheap valuation."""
    quote = _quote()
    del quote[key]
    assert terminal._index_row(_MEMBER, quote)[field] is None


def test_unpriced_member_is_dropped():
    assert terminal._index_row(_MEMBER, {}) is None
    assert terminal._index_row(_MEMBER, _quote(regularMarketPrice=None)) is None


@pytest.mark.parametrize('price, expected', [
    (100.0, 0.0),     # on the low
    (200.0, 1.0),     # on the high
    (150.0, 0.5),
    (125.0, 0.25),
])
def test_52_week_position(price, expected):
    row = terminal._index_row(_MEMBER, _quote(regularMarketPrice=price))
    assert row['week52_pos'] == pytest.approx(expected)


def test_degenerate_range_has_no_position():
    """A listing younger than a year divides by zero. It gets no position
    rather than a fabricated 0, which would draw the marker hard against the
    low and assert something the data does not say."""
    row = terminal._index_row(_MEMBER, _quote(fiftyTwoWeekLow=50.0,
                                              fiftyTwoWeekHigh=50.0))
    assert row['week52_pos'] is None


def test_position_is_clamped_into_the_range():
    """An intraday print outside the stored 52-week bounds is normal — the
    range is a daily figure. It pins to the end rather than overflowing the
    track."""
    assert terminal._index_row(_MEMBER, _quote(regularMarketPrice=250.0))['week52_pos'] == 1.0
    assert terminal._index_row(_MEMBER, _quote(regularMarketPrice=50.0))['week52_pos'] == 0.0


def test_market_cap_carries_the_listing_currency():
    """A TSX row is in CAD, and a bare '$' on it is the SK hynix defect."""
    row = terminal._index_row({'full_ticker': 'RY.TO', 'name': 'Royal Bank'},
                              _quote(symbol='RY.TO', currency='CAD',
                                     marketCap=2.4e11))
    assert row['mkt_cap'] == 'C$240.00B'
    assert row['cur_symbol'] == 'C$'


def test_market_cap_sends_raw_and_display():
    """The table sorts on this column; parsing '$3.00T' back into a number to
    do it would quantise every mega-cap to three digits."""
    row = terminal._index_row(_MEMBER, _quote())
    assert row['mkt_cap_raw'] == 3.0e12
    assert row['mkt_cap'] == '$3.00T'


def test_display_ticker_is_the_exchange_spelling():
    row = terminal._index_row({'full_ticker': 'TECK-B.TO', 'name': 'Teck'},
                              _quote(symbol='TECK-B.TO'))
    assert row['ticker'] == 'TECK.B'
    assert row['full_ticker'] == 'TECK-B.TO'


# ── Route ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def priced(monkeypatch):
    """A two-name index that needs no network."""
    monkeypatch.setattr(terminal, '_index_members', lambda k: [
        {'full_ticker': 'AAPL', 'name': 'Apple Inc.'},
        {'full_ticker': 'MSFT', 'name': 'Microsoft Corp.'},
    ])
    monkeypatch.setattr(terminal, '_index_quotes', lambda syms: {
        'AAPL': _quote(),
        'MSFT': _quote(symbol='MSFT', regularMarketPrice=400.0, trailingPE=None),
    })


def test_route_returns_the_index(priced):
    r = terminal.app.test_client().get('/api/index/sp500')
    assert r.status_code == 200
    body = r.get_json()
    assert body['label'] == 'S&P 500'
    assert body['count'] == 2 and body['members'] == 2
    assert [row['ticker'] for row in body['rows']] == ['AAPL', 'MSFT']
    assert body['rows'][1]['pe'] is None


def test_route_reports_unpriced_members(monkeypatch, priced):
    """count and members are both sent so the page can say "498 of 503" rather
    than printing a total the table does not contain."""
    monkeypatch.setattr(terminal, '_index_quotes', lambda syms: {'AAPL': _quote()})
    body = terminal.app.test_client().get('/api/index/sp500').get_json()
    assert (body['count'], body['members']) == (1, 2)


def test_market_cap_is_backfilled_when_the_quote_carries_none(monkeypatch):
    """Yahoo omits marketCap outright for ~26 of the S&P 500 — Home Depot,
    Exxon, Lowe's — and omits sharesOutstanding with it, so nothing in the
    response derives it. Left null they sort below every company in the index,
    because cap is the default sort."""
    monkeypatch.setattr(terminal, '_index_members', lambda k: [
        {'full_ticker': 'HD', 'name': 'Home Depot'},
    ])
    quote = _quote(symbol='HD', regularMarketPrice=355.62)
    del quote['marketCap']
    monkeypatch.setattr(terminal, '_index_quotes', lambda syms: {'HD': quote})
    monkeypatch.setattr(terminal, '_index_backfill_caps',
                        lambda syms, quotes: {'HD': 354.6e9} if syms == ['HD'] else {})

    row = terminal._build_index('dow')['rows'][0]
    assert row['mkt_cap_raw'] == 354.6e9
    assert row['mkt_cap'] == '$354.60B'


def test_backfill_is_only_asked_for_what_is_missing(monkeypatch):
    """A total quote failure must not turn into one fast_info request per
    member behind it."""
    monkeypatch.setattr(terminal, '_index_members', lambda k: [
        {'full_ticker': 'AAPL', 'name': 'Apple'},      # priced and capped
        {'full_ticker': 'HD',   'name': 'Home Depot'},  # priced, no cap
        {'full_ticker': 'ZZZZ', 'name': 'Unpriced'},    # no quote at all
    ])
    no_cap = _quote(symbol='HD')
    del no_cap['marketCap']
    monkeypatch.setattr(terminal, '_index_quotes',
                        lambda syms: {'AAPL': _quote(), 'HD': no_cap})

    asked = []
    monkeypatch.setattr(terminal, '_index_backfill_caps',
                        lambda syms, quotes: asked.append(list(syms)) or {})
    terminal._build_index('dow')
    assert asked == [['HD']]


def test_backfill_leaves_a_failed_lookup_null(monkeypatch):
    """A cap that cannot be found stays a dash. It is never guessed."""
    monkeypatch.setattr(terminal, '_index_members',
                        lambda k: [{'full_ticker': 'HD', 'name': 'Home Depot'}])
    no_cap = _quote(symbol='HD')
    del no_cap['marketCap']
    monkeypatch.setattr(terminal, '_index_quotes', lambda syms: {'HD': no_cap})
    monkeypatch.setattr(terminal, '_index_backfill_caps', lambda syms, quotes: {})

    row = terminal._build_index('dow')['rows'][0]
    assert row['mkt_cap_raw'] is None


# ── Market cap from a cached share count ──────────────────────────────────────
#
# What is cached is shares outstanding, not the cap. A cap held for a day is a
# day-stale number in the column the table sorts by; a share count moves on the
# slow clock membership does, and price x shares is the definition — verified
# against Yahoo's own marketCap at a ratio of 1.0000 on the names reporting both.

@pytest.fixture
def shares(monkeypatch):
    """Stub fast_info.shares, and start from an empty share-count store."""
    terminal._share_counts_store.save({})
    calls = []

    def fake_ticker(sym):
        calls.append(sym)
        return type('T', (), {'fast_info': type('F', (), {'shares': 1.0e9})})()

    monkeypatch.setattr(terminal.yf, 'Ticker', fake_ticker)
    return calls


def _quotes_for(symbols, price=2.0, volume=1):
    return {s: {'symbol': s, 'regularMarketPrice': price,
                'regularMarketVolume': volume} for s in symbols}


def test_cap_is_price_times_shares(shares):
    out = terminal._index_backfill_caps(['HD'], _quotes_for(['HD'], price=355.0))
    assert out == {'HD': 355.0 * 1.0e9}


def test_share_count_is_cached_but_the_cap_stays_live(shares):
    """The second build must not refetch — and must reprice, not replay.

    Caching the cap instead would pin this column to whatever the price was when
    the count was fetched, which on a sorted-by-cap table is a day-stale order.
    """
    first = terminal._index_backfill_caps(['HD'], _quotes_for(['HD'], price=100.0))
    assert first == {'HD': 100.0e9} and shares == ['HD']

    second = terminal._index_backfill_caps(['HD'], _quotes_for(['HD'], price=110.0))
    assert second == {'HD': 110.0e9}, 'cap did not follow the live price'
    assert shares == ['HD'], 'share count was refetched inside its TTL'


def test_a_stale_share_count_is_refetched(shares):
    terminal._share_counts_store.save({'HD': {'shares': 5.0e8, 'ts': 0}})
    terminal._index_backfill_caps(['HD'], _quotes_for(['HD']))
    assert shares == ['HD']


def test_backfill_is_bounded_and_takes_the_largest_first(shares, capsys):
    """Bounded so a wholesale Yahoo change cannot become 3,400 extra requests.

    Ordered by traded value because the uncapped set is not obscure — XOM, HD,
    MCD and CRM are all in it — and cap is this table's default sort, so a large
    name left null sorts below every micro-cap on the venue.
    """
    symbols = [f'SYM{i}' for i in range(terminal._INDEX_CAP_BACKFILL_MAX + 25)]
    # Volume ascending with the index, so the *last* symbols are the valuable
    # ones and a run that just truncated the input would take the wrong ones.
    quotes = {s: {'symbol': s, 'regularMarketPrice': 1.0,
                  'regularMarketVolume': i} for i, s in enumerate(symbols)}

    out = terminal._index_backfill_caps(symbols, quotes)

    assert len(shares) == terminal._INDEX_CAP_BACKFILL_MAX
    assert len(out) == terminal._INDEX_CAP_BACKFILL_MAX
    assert symbols[-1] in out, 'the most heavily traded name was not fetched'
    assert symbols[0] not in out, 'the bound did not order by traded value'
    assert 'backfilling' in capsys.readouterr().out


def test_successive_builds_walk_down_the_tail(shares):
    """A cached symbol drops out of the wanted list, so the next build spends its
    whole bound on names it has never fetched rather than redoing the head."""
    symbols = [f'SYM{i}' for i in range(terminal._INDEX_CAP_BACKFILL_MAX + 25)]
    quotes = {s: {'symbol': s, 'regularMarketPrice': 1.0,
                  'regularMarketVolume': i} for i, s in enumerate(symbols)}

    first = terminal._index_backfill_caps(symbols, quotes)
    shares.clear()
    second = terminal._index_backfill_caps(symbols, quotes)

    assert set(shares) == set(symbols) - set(first), \
        'the second run refetched symbols the first had already cached'
    assert len(second) == len(symbols), 'two runs did not cover the whole list'


def test_a_symbol_with_no_price_gets_no_cap(shares):
    """price x shares needs a price. Without one the cap stays null rather than
    becoming 0, which would sort as the smallest company on the venue."""
    out = terminal._index_backfill_caps(['HD'], {'HD': {'symbol': 'HD'}})
    assert 'HD' not in out


def test_share_counts_are_pruned(shares):
    now = terminal._time_mod.time()
    kept = {'ts': now, 'shares': 1.0}
    data = terminal._prune_share_counts(
        {'FRESH': kept, 'OLD': {'ts': now - 30 * 24 * 3600, 'shares': 1.0},
         'JUNK': 'not a dict'}, now)
    assert data == {'FRESH': kept}


def test_unknown_index_is_404():
    r = terminal.app.test_client().get('/api/index/ftse100')
    assert r.status_code == 404
    assert 'error' in r.get_json()


def test_traversal_in_the_key_is_404():
    r = terminal.app.test_client().get('/api/index/..%2f..%2fapp.py')
    assert r.status_code in (404, 400)


def test_build_failure_is_502(monkeypatch):
    monkeypatch.setattr(terminal, '_index_members',
                        lambda k: (_ for _ in ()).throw(RuntimeError('upstream')))
    r = terminal.app.test_client().get('/api/index/dow')
    assert r.status_code == 502
    assert 'error' in r.get_json()


def test_nothing_priced_is_502(monkeypatch):
    """An index where every quote failed is an error, not an empty table that
    looks like an index with no members."""
    monkeypatch.setattr(terminal, '_index_members',
                        lambda k: [{'full_ticker': 'AAPL', 'name': 'Apple'}])
    monkeypatch.setattr(terminal, '_index_quotes', lambda syms: {})
    assert terminal.app.test_client().get('/api/index/dow').status_code == 502


def test_route_is_cached(monkeypatch, priced):
    """Ten accounts opening the S&P 500 at once cause one build."""
    builds = []
    real = terminal._index_quotes
    monkeypatch.setattr(terminal, '_index_quotes',
                        lambda syms: builds.append(1) or real(syms))
    client = terminal.app.test_client()
    client.get('/api/index/sp500')
    client.get('/api/index/sp500')
    assert len(builds) == 1


def test_every_index_has_a_source_and_a_label():
    for key, src in terminal._INDEX_SOURCES.items():
        assert key == key.lower()
        for field in ('label', 'url', 'symbol', 'name', 'suffix', 'min'):
            assert field in src, f'{key} is missing {field!r}'
        assert src['url'].startswith('https://')
        assert src['min'] > 0


# ── Exchanges ─────────────────────────────────────────────────────────────────
#
# An exchange differs from an index in one place — where the membership comes
# from — so these tests cover the two listing-file parsers and the security-type
# filters. Everything downstream is the index path, already covered above.

_NASDAQ_FILE = (
    'Symbol|Security Name|Market Category|Test Issue|Financial Status|'
    'Round Lot Size|ETF|NextShares\r\n'
    'AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\r\n'
    'ABVX|Abivax SA - American Depositary Shares|G|N|N|100|N|N\r\n'
    'BRK.B|Berkshire Hathaway - Class B Common Stock|Q|N|N|100|N|N\r\n'
    'AACBR|Artius II Acquisition Inc. - Rights|G|N|N|100|N|N\r\n'
    'AACBU|Artius II Acquisition Inc. - Units|G|N|N|100|N|N\r\n'
    'AACOW|Armada Acquisition Corp. III - Warrants|G|N|N|100|N|N\r\n'
    'PFDX|Some Bank - Perpetual Preferred Stock|Q|N|N|100|N|N\r\n'
    'AEFC|Aegon Funding Company LLC 5.10% Sub Debt|Q|N|N|100|N|N\r\n'
    'QQQ|Invesco QQQ Trust|Q|N|N|100|Y|N\r\n'
    'ZZZT|Nasdaq TEST Stock|Q|Y|N|100|N|N\r\n'
    'File Creation Time: 0810202618:01|||||||\r\n')

_OTHER_FILE = (
    'ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|'
    'Test Issue|NASDAQ Symbol\r\n'
    'A|Agilent Technologies, Inc. Common Stock|N|A|N|100|N|A\r\n'
    'XOM|Exxon Mobil Corporation Common Stock|N|XOM|N|100|N|XOM\r\n'
    'IMO|Imperial Oil Limited Common Stock|A|IMO|N|100|N|IMO\r\n'
    'AAA|Alternative Access CLO Bond ETF|P|AAA|Y|100|N|AAA\r\n'
    'File Creation Time: 0810202618:01||||||\r\n')


@pytest.fixture
def listing(monkeypatch):
    """Serve captured listing-file text, and lower the row gate."""
    def serve(key, text, min_rows=1):
        monkeypatch.setattr(requests, 'get', lambda *a, **k: _FakeResponse(text))
        monkeypatch.setitem(terminal._EXCHANGE_SOURCES, key,
                            {**terminal._EXCHANGE_SOURCES[key], 'min': min_rows})
        return key
    return serve


@pytest.mark.parametrize('name, expected', [
    ('Apple Inc. - Common Stock',                       True),
    ('Alliance Entertainment Holding - common stock',   True),   # lowercased
    ('Adagene Inc. - Ordinary Shares',                  True),
    ('Kimco Realty - Shares of Beneficial Interest',    True),   # a REIT
    # An ADR is the common equity of a foreign issuer, and has to survive the
    # bare 'Depositary Shares' rule that marks a preferred.
    ('Abivax SA - American Depositary Shares',          True),
    ('Adagene Inc. - ADS, each representing 1.25 ords', True),
    ('Artius II Acquisition Inc. - Rights',             False),
    ('Artius II Acquisition Inc. - Units',              False),
    ('Armada Acquisition Corp. III - Warrant',          False),
    ('Armada Acquisition Corp. III - Warrants',         False),
    ('Some Bank - Perpetual Preferred Stock',           False),
    ('Some Bank - Depositary Shares',                   False),
    ('Some Corp - 6.25% Subordinated Notes due 2055',   False),
    # Spelled without any of the words above; the coupon is the only tell.
    ('Aegon Funding Company LLC 5.10%',                 False),
])
def test_common_stock_filter(name, expected):
    """A listing file names every security on a venue, not every stock. None of
    the excluded kinds has a P/E or a market cap, so a row for one is a row of
    dashes that still takes a slot in every sort."""
    assert terminal._is_common_stock(name) is expected


def test_symdir_keeps_only_common_stock(listing):
    key  = listing('nasdaq', _NASDAQ_FILE)
    rows = terminal._scrape_exchange_members(key)

    assert [r['full_ticker'] for r in rows] == ['AAPL', 'ABVX', 'BRK-B']


def test_symdir_drops_etfs_and_test_issues(listing):
    """A test issue is a symbol the venue reserves for its own systems checks.
    It is not a security and Yahoo does not price it."""
    rows = terminal._scrape_exchange_members(listing('nasdaq', _NASDAQ_FILE))
    symbols = [r['full_ticker'] for r in rows]
    assert 'QQQ' not in symbols and 'ZZZT' not in symbols


def test_symdir_drops_the_file_creation_footer(listing):
    """Left in, the footer parses as a security whose symbol is that literal
    text — refused one layer too late to be readable."""
    rows = terminal._scrape_exchange_members(listing('nasdaq', _NASDAQ_FILE))
    assert all('FILE' not in r['full_ticker'] for r in rows)


def test_symdir_trims_the_security_type_from_the_name(listing):
    rows = terminal._scrape_exchange_members(listing('nasdaq', _NASDAQ_FILE))
    assert rows[0]['name'] == 'Apple Inc.'


def test_symdir_selects_one_venue_out_of_the_shared_file(listing):
    """otherlisted.txt carries five venues in one file, so NYSE and NYSE
    American are the same download filtered two ways."""
    nyse = terminal._scrape_exchange_members(listing('nyse', _OTHER_FILE))
    amex = terminal._scrape_exchange_members(listing('amex', _OTHER_FILE))

    assert [r['full_ticker'] for r in nyse] == ['A', 'XOM']
    assert [r['full_ticker'] for r in amex] == ['IMO']


def test_a_thin_listing_file_raises(listing):
    """A venue that comes back with forty names is a parser that broke, not a
    venue that delisted three thousand companies — and only one of those may
    overwrite a good stored list."""
    with pytest.raises(RuntimeError):
        terminal._scrape_exchange_members(listing('nasdaq', _NASDAQ_FILE,
                                                  min_rows=500))


def test_tmx_takes_one_row_per_company(monkeypatch, listing):
    """The `instruments` array carries an issuer's other series — the USD class
    of a fund, separate unit classes — which are the same company twice in a
    table that is one row per company."""
    class _Json(_FakeResponse):
        def json(self):
            return {'results': [
                {'symbol': 'RY', 'name': 'Royal Bank of Canada',
                 'instruments': [{'symbol': 'RY'}]},
                {'symbol': 'BTCQ', 'name': '3iQ Bitcoin ETF',
                 'instruments': [{'symbol': 'BTCQ'}, {'symbol': 'BTCQ.U'}]},
                {'symbol': 'TECK.B', 'name': 'Teck Resources',
                 'instruments': [{'symbol': 'TECK.B'}]},
            ]}

    monkeypatch.setattr(requests, 'get', lambda *a, **k: _Json(''))
    monkeypatch.setitem(terminal._EXCHANGE_SOURCES, 'tsx',
                        {**terminal._EXCHANGE_SOURCES['tsx'], 'min': 1})
    rows = terminal._scrape_exchange_members('tsx')

    assert [r['full_ticker'] for r in rows] == ['RY.TO', 'BTCQ.TO', 'TECK-B.TO']
    assert 'BTCQ-U.TO' not in [r['full_ticker'] for r in rows]


# ── Depositary receipts ───────────────────────────────────────────────────────
#
# A CDR is a bank-created wrapper around a share listed somewhere else, and Yahoo
# attaches the underlying company's market cap to it — NVDA.TO comes back at
# C$6.81T. 133 of the 2,266 TSX entries are one, so left alone they take the
# whole first screen of a table whose default sort is market cap.

_CDR_MEMBER = {'full_ticker': 'NVDA.TO', 'name': 'Nvidia CDR (CAD Hedged)',
               'dr': True}


def _tmx(monkeypatch, results):
    class _Json(_FakeResponse):
        def json(self):
            return {'results': results}
    monkeypatch.setattr(requests, 'get', lambda *a, **k: _Json(''))
    monkeypatch.setitem(terminal._EXCHANGE_SOURCES, 'tsx',
                        {**terminal._EXCHANGE_SOURCES['tsx'], 'min': 1})


def test_tmx_flags_a_depositary_receipt(monkeypatch):
    _tmx(monkeypatch, [{'symbol': 'NVDA', 'name': 'Nvidia CDR (CAD Hedged)'},
                       {'symbol': 'RY', 'name': 'Royal Bank of Canada'}])
    rows = {r['full_ticker']: r for r in terminal._scrape_exchange_members('tsx')}

    assert rows['NVDA.TO'].get('dr') is True
    assert 'dr' not in rows['RY.TO']


def test_a_receipt_reports_no_market_cap():
    """Suppressed rather than converted: the figure for the program itself is
    nowhere in the payload, so there is nothing to put here instead."""
    row = terminal._index_row(_CDR_MEMBER,
                              _quote(symbol='NVDA.TO', currency='CAD',
                                     marketCap=6.81e12))
    assert row['mkt_cap_raw'] is None
    assert row['mkt_cap'] == 'N/A'


def test_a_receipt_keeps_its_own_price_and_range():
    """The row stays: a CAD-hedged NVDA is a real thing to buy in Toronto, and
    its price, day change and 52-week range are all genuinely its own."""
    row = terminal._index_row(_CDR_MEMBER,
                              _quote(symbol='NVDA.TO', currency='CAD',
                                     regularMarketPrice=48.64, marketCap=6.81e12))
    assert row['price'] == 48.64
    assert row['change'] == 1.5
    assert row['week52_pos'] is not None


def test_a_receipt_keeps_the_venue_name():
    """Yahoo calls NVDA.TO 'NVIDIA Corporation', which in a list of Toronto
    listings reads as NVIDIA being listed in Toronto."""
    row = terminal._index_row(_CDR_MEMBER,
                              _quote(symbol='NVDA.TO', longName='NVIDIA Corporation'))
    assert row['name'] == 'Nvidia CDR (CAD Hedged)'


def test_an_ordinary_listing_still_prefers_the_quote_name():
    row = terminal._index_row({'full_ticker': 'RY.TO', 'name': 'Royal Bank'},
                              _quote(symbol='RY.TO', longName='Royal Bank of Canada'))
    assert row['name'] == 'Royal Bank of Canada'


def test_a_receipt_is_not_sent_to_the_cap_backfill(monkeypatch):
    """Its cap is deliberately null, so a fast_info request for one would spend
    a call to produce a number the row throws away."""
    monkeypatch.setattr(terminal, '_index_members', lambda k: [
        dict(_CDR_MEMBER),
        {'full_ticker': 'RY.TO', 'name': 'Royal Bank'},
    ])
    no_cap = _quote(symbol='RY.TO')
    del no_cap['marketCap']
    monkeypatch.setattr(terminal, '_index_quotes', lambda syms: {
        'NVDA.TO': _quote(symbol='NVDA.TO', quoteType='EQUITY'),
        'RY.TO':   dict(no_cap, quoteType='EQUITY'),
    })
    asked = []
    monkeypatch.setattr(terminal, '_index_backfill_caps',
                        lambda syms, quotes: asked.append(list(syms)) or {})

    terminal._build_index('tsx')
    assert asked == [['RY.TO']]


def test_exchange_membership_shares_the_index_cache(monkeypatch):
    """A listing file is public, market-wide and republished daily — the same
    properties index membership has — so it degrades the same way rather than
    through a parallel mechanism."""
    stored = [{'full_ticker': 'AAPL', 'name': 'Apple Inc.'}]
    _seed_members('nasdaq', stored, ts=0)          # stale, forces a refresh
    monkeypatch.setattr(terminal, '_scrape_exchange_members',
                        lambda _k: (_ for _ in ()).throw(RuntimeError('down')))

    assert terminal._index_members('nasdaq') == stored


# ── Non-equity listings ───────────────────────────────────────────────────────

def _exchange(monkeypatch, quotes):
    monkeypatch.setattr(terminal, '_index_members', lambda k: [
        {'full_ticker': s, 'name': s} for s in quotes])
    monkeypatch.setattr(terminal, '_index_quotes', lambda syms: quotes)


def test_exchange_drops_funds_by_quote_type(monkeypatch):
    """A TMX listing file has no security-type column at all, so the type comes
    from the quote. Without this the TSX tab is 1,528 ETFs over 714 companies,
    and 70% of the table has no market cap because a fund has no such figure."""
    _exchange(monkeypatch, {
        'RY.TO':   _quote(symbol='RY.TO', quoteType='EQUITY'),
        'BTCQ.TO': _quote(symbol='BTCQ.TO', quoteType='ETF'),
        'XIU.TO':  _quote(symbol='XIU.TO', quoteType='MUTUALFUND'),
    })
    body = terminal._build_index('tsx')

    assert [r['ticker'] for r in body['rows']] == ['RY']
    # Counted apart from `members` so the page can say why a 2,266-name venue
    # draws 714 rows: those are excluded, not missing.
    assert body['funds'] == 2
    assert body['members'] == 3 and body['count'] == 1


def test_an_index_keeps_a_member_with_no_quote_type(monkeypatch):
    """An index constituent is an equity by construction. Applying the filter
    there would let a missing field drop a real member."""
    _exchange(monkeypatch, {'AAPL': _quote()})     # no quoteType at all
    body = terminal._build_index('dow')

    assert [r['ticker'] for r in body['rows']] == ['AAPL']
    assert body['funds'] == 0


# ── Universe registry and the route ───────────────────────────────────────────

def test_index_and_exchange_keys_do_not_collide():
    """Both halves are looked up in one registry, so a shared key would make
    dispatch depend on dict ordering."""
    assert not (set(terminal._INDEX_SOURCES) & set(terminal._EXCHANGE_SOURCES))
    assert set(terminal._UNIVERSES) == (set(terminal._INDEX_SOURCES)
                                        | set(terminal._EXCHANGE_SOURCES))


def test_every_exchange_has_a_source_and_a_label():
    for key, src in terminal._EXCHANGE_SOURCES.items():
        assert key == key.lower()
        assert src['label'] and src['min'] > 0
        # Exactly one of the two membership shapes: a SymDir file or a TMX board.
        assert bool(src.get('file')) != bool(src.get('board')), key
        if src.get('file'):
            assert src['file'] in terminal._SYMDIR_URLS
        else:
            assert src['suffix'].startswith('.')


def test_universe_kinds_are_tagged():
    assert terminal._UNIVERSES['sp500']['kind'] == 'index'
    assert terminal._UNIVERSES['nasdaq']['kind'] == 'exchange'


def test_listing_route_serves_an_exchange(monkeypatch):
    _exchange(monkeypatch, {'AAPL': _quote(quoteType='EQUITY')})
    body = terminal.app.test_client().get('/api/listing/nasdaq').get_json()

    assert body['label'] == 'Nasdaq' and body['kind'] == 'exchange'
    assert [r['ticker'] for r in body['rows']] == ['AAPL']


def test_both_urls_reach_one_endpoint(priced):
    """/api/index/<key> is what the four index keys were published under and
    still answer on; /api/listing/<key> is the honest spelling for a venue. One
    endpoint name, so there is still one rate-limit bucket."""
    client = terminal.app.test_client()
    assert client.get('/api/index/sp500').get_json()['count'] == 2
    terminal._index_payload_cache.clear()
    assert client.get('/api/listing/sp500').get_json()['count'] == 2


def test_unknown_exchange_is_404():
    assert terminal.app.test_client().get('/api/listing/lse').status_code == 404
