"""Hiding a stock, and the line between what that filters and what it must not.

A block is a *discovery* filter. The tests here come in two halves and the
second half is the one that matters:

  - every route that offers a stock you did not ask for by name drops a hidden
    symbol, and /api/stock refuses one outright;
  - and nothing in the ledger moves. Holdings, transactions, sales, options and
    every figure derived from them keep counting a hidden position in full,
    because `realized + unrealized + dividends + option P/L == (cash_pool +
    market value) − invested` holds only if nothing filters those walks. A
    hidden holding quietly dropped from `_compute_invested()` would not hide a
    stock — it would misreport the portfolio's return, which is the failure
    here that looks most like working code.

Nothing in this file reaches the network: the caches these routes read from are
seeded directly, which is also the point — they are shared between accounts and
filtered per reader.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402

HIDDEN = 'ZHIDE'
KEPT   = 'ZKEEP'


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A signed-in client whose per-user files live in a fresh temp directory."""
    monkeypatch.setattr(terminal, '_USER_DATA_ROOT', str(tmp_path / 'users'))
    monkeypatch.setattr(terminal, '_user_stores', {})
    return terminal.app.test_client()


def hide(client, ticker, name=''):
    res = client.post('/api/blocked', json={'ticker': ticker, 'name': name})
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def _row(sym, **extra):
    """A discovery row in the shape every one of these payloads uses."""
    return {'ticker': sym.replace('.TO', ''), 'full_ticker': sym,
            'name': f'{sym} Inc', 'change': 1.0, 'price': 10.0, **extra}


# ---------------------------------------------------------------------------
# The list itself
# ---------------------------------------------------------------------------

def test_hiding_then_unhiding_round_trips(client):
    assert client.get('/api/blocked').get_json()['items'] == []

    body = hide(client, HIDDEN, 'Hidden Co')
    assert [r['ticker'] for r in body['items']] == [HIDDEN]
    assert body['items'][0]['name'] == 'Hidden Co'
    assert body['items'][0]['blocked']          # stamped server-side

    res = client.delete(f'/api/blocked/{HIDDEN}')
    assert res.status_code == 200
    assert res.get_json()['items'] == []
    assert client.get('/api/blocked').get_json()['items'] == []


def test_hiding_the_same_stock_twice_is_idempotent(client):
    hide(client, HIDDEN)
    assert len(hide(client, HIDDEN)['items']) == 1


def test_an_unknown_ticker_is_refused_not_stored(client):
    """clean_ticker guards the write boundary here like everywhere else."""
    for bad in ('', 'not a ticker', '../../etc/passwd', 'AAPL & calc', 123, None):
        assert client.post('/api/blocked', json={'ticker': bad}).status_code == 400
    assert client.get('/api/blocked').get_json()['items'] == []


def test_unhiding_something_that_was_never_hidden_is_a_no_op(client):
    hide(client, HIDDEN)
    assert client.delete('/api/blocked/NOSUCH').get_json()['items'][0]['ticker'] == HIDDEN


def test_the_name_is_bounded(client):
    """It reaches a table cell and a JSON file; an unbounded string is neither."""
    body = hide(client, HIDDEN, 'x' * 500)
    assert len(body['items'][0]['name']) == 120


# ---------------------------------------------------------------------------
# The discovery surfaces
# ---------------------------------------------------------------------------

def test_movers_drops_a_hidden_row(client, monkeypatch):
    monkeypatch.setitem(terminal._movers_cache, 'TSX', {
        'gainers': [_row('ZHIDE.TO'), _row('ZKEEP.TO')],
        'losers': [], 'ts': terminal._time_mod.time(),
    })
    hide(client, 'ZHIDE.TO')

    results = client.get('/api/movers?exchange=TSX').get_json()['results']
    assert [r['full_ticker'] for r in results] == ['ZKEEP.TO']


def test_movers_filters_before_the_limit(client, monkeypatch):
    """Hiding three names must not leave a 22-row "top 25"."""
    rows = [_row(f'Z{i}.TO') for i in range(30)]
    monkeypatch.setitem(terminal._movers_cache, 'TSX', {
        'gainers': rows, 'losers': [], 'ts': terminal._time_mod.time(),
    })
    for i in range(3):
        hide(client, f'Z{i}.TO')

    body = client.get('/api/movers?exchange=TSX&limit=25').get_json()
    assert len(body['results']) == 25
    assert not any(r['full_ticker'] in ('Z0.TO', 'Z1.TO', 'Z2.TO')
                   for r in body['results'])


def test_the_tape_drops_a_hidden_row_and_rebatches(client, monkeypatch):
    monkeypatch.setitem(terminal._tape_cache, 'data',
                        [_row('ZHIDE.TO'), _row('ZKEEP.TO')])
    monkeypatch.setitem(terminal._tape_cache, 'ts', terminal._time_mod.time())
    hide(client, 'ZHIDE.TO')

    body = client.get('/api/tape?batch=0').get_json()
    assert [i['full_ticker'] for i in body['items']] == ['ZKEEP.TO']
    # has_more/total_batches describe what this account will actually be sent.
    assert body['has_more'] is False


def test_the_static_tape_placeholder_is_filtered_too(client):
    """It is drawn before any price arrives; an unfiltered one flashes the row."""
    hide(client, terminal.TAPE_STATIC[0]['full_ticker'])
    items = client.get('/api/tape?static=1').get_json()['items']
    assert terminal.TAPE_STATIC[0]['full_ticker'] not in [i['full_ticker'] for i in items]
    assert len(items) == len(terminal.TAPE_STATIC) - 1


def test_search_does_not_offer_a_hidden_stock(client, monkeypatch):
    class _Resp:
        @staticmethod
        def json():
            return {'quotes': [
                {'symbol': 'ZHIDE.TO', 'quoteType': 'EQUITY', 'longname': 'Hidden Co'},
                {'symbol': 'ZKEEP.TO', 'quoteType': 'EQUITY', 'longname': 'Kept Co'},
            ]}

    # The route does `import requests as req` in its own body, so the patch has
    # to land on the module rather than on a name in app.py.
    import requests as real_requests
    monkeypatch.setattr(real_requests, 'get', lambda *a, **k: _Resp())
    hide(client, 'ZHIDE.TO')

    assert [r['ticker'] for r in client.get('/api/search?q=z').get_json()] == ['ZKEEP.TO']


def test_the_market_browser_drops_a_hidden_row(client, monkeypatch):
    monkeypatch.setattr(terminal, '_build_index', lambda key: {
        'key': key, 'kind': 'index', 'label': 'Test',
        'rows': [_row('ZHIDE'), _row('ZKEEP')],
        'count': 2, 'members': 2, 'funds': 0,
    })
    hide(client, HIDDEN)

    body = client.get('/api/index/sp500').get_json()
    assert [r['full_ticker'] for r in body['rows']] == ['ZKEEP']
    assert body['count'] == 1
    assert body['hidden'] == 1
    # members carries the frontend's "unpriced" arithmetic (members − funds −
    # rows). Left at 2, the hidden name is reported as one Yahoo would not
    # price, which is a different and untrue statement about the data.
    assert body['members'] == 1


def test_the_watchlist_hides_but_does_not_delete(client):
    client.post('/api/watchlist', json={'ticker': HIDDEN, 'name': 'Hidden Co'})
    client.post('/api/watchlist', json={'ticker': KEPT,   'name': 'Kept Co'})
    hide(client, HIDDEN)

    assert [w['ticker'] for w in client.get('/api/watchlist').get_json()] == [KEPT]
    # The row is filtered, not pruned — unhiding restores it as it was.
    assert [w['ticker'] for w in terminal.load_watchlist(owner='testrunner')] \
        == [HIDDEN, KEPT]

    client.delete(f'/api/blocked/{HIDDEN}')
    restored = client.get('/api/watchlist').get_json()
    assert [w['ticker'] for w in restored] == [HIDDEN, KEPT]
    assert restored[0]['name'] == 'Hidden Co'


def test_adding_a_hidden_stock_to_the_watchlist_is_refused(client):
    """A write the GET would filter back out is an Add button that does nothing."""
    hide(client, HIDDEN)
    res = client.post('/api/watchlist', json={'ticker': HIDDEN})
    assert res.status_code == 409
    assert res.get_json()['blocked'] is True
    assert terminal.load_watchlist(owner='testrunner') == []


def test_the_positions_feed_skips_a_hidden_symbol(client, monkeypatch):
    monkeypatch.setattr(terminal, '_build_news', lambda s, name='', lite=False: [])
    client.post('/api/watchlist', json={'ticker': HIDDEN})
    client.post('/api/watchlist', json={'ticker': KEPT})
    hide(client, HIDDEN)

    assert client.get('/api/news/positions').get_json()['symbols'] == [KEPT]


def test_the_exchange_switcher_drops_a_hidden_listing(client, monkeypatch):
    """The switcher is an offer to go somewhere; a hidden venue is not on it."""
    hide(client, 'ZKEEP.TO')

    class _Ticker:
        info = {'longName': 'Zkeep Inc', 'exchange': 'NMS',
                'fullExchangeName': 'NasdaqGS'}

    class _Resp:
        @staticmethod
        def json():
            return {'quotes': [
                {'symbol': 'ZKEEP.TO', 'quoteType': 'EQUITY',
                 'longname': 'Zkeep Inc', 'exchange': 'TOR', 'exchDisp': 'Toronto'},
            ]}

    monkeypatch.setattr(terminal.yf, 'Ticker', lambda *a, **k: _Ticker())
    import requests as real_requests
    monkeypatch.setattr(real_requests, 'get', lambda *a, **k: _Resp())

    listings = client.get('/api/crosslist?ticker=ZKEEP').get_json()
    assert [row['ticker'] for row in listings] == ['ZKEEP']   # the current one only


# ---------------------------------------------------------------------------
# The detail page
# ---------------------------------------------------------------------------

def test_the_stock_page_refuses_a_hidden_ticker(client, monkeypatch):
    """The one surface a symbol typed in full would otherwise walk straight to."""
    called = []

    def _stub(ticker):
        called.append(ticker)
        return {'ticker': ticker}, False

    monkeypatch.setattr(terminal, '_cached_stock', _stub)
    hide(client, HIDDEN)

    res = client.get(f'/api/stock?ticker={HIDDEN}')
    assert res.status_code == 403
    body = res.get_json()
    assert body['blocked'] is True and body['ticker'] == HIDDEN
    # Refused before the shared payload cache is consulted, so nothing upstream
    # is spent on a stock this account asked not to see.
    assert called == []

    assert client.get(f'/api/stock?ticker={KEPT}').status_code == 200


def test_per_ticker_news_refuses_independently(client, monkeypatch):
    """It fires in parallel with /api/stock, so it cannot rely on that refusal."""
    monkeypatch.setattr(terminal, '_build_news',
                        lambda *a, **k: pytest.fail('spent a key on a hidden stock'))
    hide(client, HIDDEN)
    assert client.get(f'/api/news?ticker={HIDDEN}').get_json() == {'news': []}


def test_unhiding_reopens_the_page(client, monkeypatch):
    monkeypatch.setattr(terminal, '_cached_stock', lambda t: ({'ticker': t}, False))
    hide(client, HIDDEN)
    assert client.get(f'/api/stock?ticker={HIDDEN}').status_code == 403
    client.delete(f'/api/blocked/{HIDDEN}')
    assert client.get(f'/api/stock?ticker={HIDDEN}').status_code == 200


# ---------------------------------------------------------------------------
# The line: the ledger does not move
# ---------------------------------------------------------------------------

def test_a_hidden_holding_still_counts_in_full(client):
    """The invariant this whole feature is bounded by.

    Hiding is not selling. If a block reached `_compute_invested()` the
    portfolio would report a return computed over capital it no longer admits
    to having deployed — right-looking numbers, silently wrong.
    """
    client.post('/api/holdings', json={'ticker': HIDDEN, 'name': 'Hidden Co',
                                       'shares': 10, 'price': 25.0,
                                       'date_acquired': '2026-01-05'})
    before = client.get('/api/portfolio/invested').get_json()['invested']
    assert before == pytest.approx(250.0)

    hide(client, HIDDEN)

    assert client.get('/api/portfolio/invested').get_json()['invested'] == before
    assert [h['ticker'] for h in client.get('/api/holdings').get_json()] == [HIDDEN]
    assert [t['ticker'] for t in client.get('/api/transactions').get_json()] == [HIDDEN]
    assert [p['ticker'] for p in
            client.get('/api/portfolio/performance').get_json()['positions']] == [HIDDEN]


def test_hiding_a_holding_says_so(client):
    """Reported rather than left to be discovered on the Holdings tab."""
    client.post('/api/holdings', json={'ticker': HIDDEN, 'name': 'Hidden Co',
                                       'shares': 1, 'price': 5.0,
                                       'date_acquired': '2026-01-05'})
    assert hide(client, HIDDEN)['held'] is True
    assert hide(client, KEPT)['held'] is False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_a_block_is_exact_and_does_not_spread_across_venues(client, monkeypatch):
    """Blocking ZKEEP.TO must not take a different company's ZKEEP with it.

    A root-symbol rule reads as helpful right up until NA.TO takes NA with it,
    and a filter that hides more than it was asked to is indistinguishable from
    a bug.
    """
    monkeypatch.setitem(terminal._movers_cache, 'TSX', {
        'gainers': [_row('ZKEEP.TO'), _row('ZKEEP')],
        'losers': [], 'ts': terminal._time_mod.time(),
    })
    hide(client, 'ZKEEP.TO')

    results = client.get('/api/movers?exchange=TSX').get_json()['results']
    assert [r['full_ticker'] for r in results] == ['ZKEEP']


def test_matching_keys_on_the_full_symbol_not_the_display_one(client, monkeypatch):
    """`ticker` on these rows is the display spelling — 'ZKEEP' for ZKEEP.TO."""
    monkeypatch.setitem(terminal._movers_cache, 'TSX', {
        'gainers': [_row('ZKEEP.TO')], 'losers': [],
        'ts': terminal._time_mod.time(),
    })
    hide(client, 'ZKEEP')          # the display spelling, deliberately
    assert len(client.get('/api/movers?exchange=TSX').get_json()['results']) == 1


def test_a_lowercase_ticker_is_normalised_on_the_way_in(client, monkeypatch):
    monkeypatch.setitem(terminal._movers_cache, 'TSX', {
        'gainers': [_row('ZHIDE.TO')], 'losers': [],
        'ts': terminal._time_mod.time(),
    })
    assert hide(client, 'zhide.to')['items'][0]['ticker'] == 'ZHIDE.TO'
    assert client.get('/api/movers?exchange=TSX').get_json()['results'] == []


def test_a_hand_edited_junk_row_cannot_break_the_filter(client):
    """blocked.json sits in the data directory and gets edited by hand."""
    terminal.save_blocked([{'ticker': 'not a ticker'}, {}, {'ticker': None},
                           {'ticker': HIDDEN}], owner='testrunner')
    assert terminal._blocked_set(owner='testrunner') == frozenset({HIDDEN})


def test_the_filter_is_empty_outside_a_request(client):
    """Background builders have no session; a raise there would break the cache."""
    assert terminal._blocked_set() == frozenset()
