"""Regression tests for the fixes in app.py.

Every test here corresponds to a defect that was live in the code. They run
offline — no test touches Yahoo Finance. Live checks belong in
test_live.py behind the `network` marker.

    pytest                  # these
    pytest -m network       # live smoke tests
"""
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402
from conftest import TEST_USER  # noqa: E402


# ---------------------------------------------------------------------------
# clean_ticker — the guard that makes it safe to put a ticker on a command line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('AAPL',        'AAPL'),
    ('brk.b',       'BRK.B'),
    ('  msft  ',    'MSFT'),
    ('TECK-B.TO',   'TECK-B.TO'),
    ('BIP-UN.TO',   'BIP-UN.TO'),
])
def test_clean_ticker_accepts_real_symbols(raw, expected):
    assert terminal.clean_ticker(raw) == expected


@pytest.mark.parametrize('payload', [
    'AAPL & calc',              # cmd.exe command separator
    'AAPL && whoami',
    'AAPL | more',
    'AAPL > out.txt',
    'AAPL; calc',
    'AAPL ^& calc',             # cmd.exe escape
    'AAPL `whoami`',
    'AAPL $(id)',
    'AAPL %PATH%',
    '..\\..\\Windows\\win.ini',  # path traversal
    '../../etc/passwd',
    'AAPL"x',
    '',
    '   ',
    None,
    'A' * 20,                   # over length
    123,                        # a JSON body can carry a number, or an object
    12.5,
    True,
    ['AAPL'],
    {'ticker': 'AAPL'},
])
def test_clean_ticker_rejects_injection(payload):
    """A ticker reaches a subprocess argv and a filesystem path. Anything that
    isn't a bare symbol must be refused, not sanitised."""
    assert terminal.clean_ticker(payload) is None


def test_report_routes_reject_bad_ticker():
    c = terminal.app.test_client()
    r = c.post('/api/generate-report', json={'ticker': 'AAPL & calc'})
    assert r.status_code == 400
    r = c.get('/api/report-file/AAPL%20%26%20calc')
    assert r.status_code == 400


def test_run_report_refuses_bad_ticker_without_spawning():
    """_run_report is the last line of defence: it must not reach Popen."""
    called = []
    original = terminal.subprocess.Popen
    terminal.subprocess.Popen = lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
        AssertionError('Popen must not be called'))
    try:
        terminal._run_report('job-1', 'AAPL & calc', TEST_USER)
    finally:
        terminal.subprocess.Popen = original
    assert called == []
    assert terminal._report_jobs['job-1']['status'] == 'error'


# ---------------------------------------------------------------------------
# The write boundary — a symbol in a portfolio file has to be a symbol
#
# add_to_watchlist and add_to_holdings stored `(ticker or '').strip().upper()`,
# so watchlist.json and holdings.json could legally hold a value that was never
# a symbol. That pushed the check onto every reader — _portfolio_symbols() had
# to filter the stored file — and readers are the wrong place for it.
# ---------------------------------------------------------------------------

# One payload per shape the guard has to refuse. The full battery is in
# test_clean_ticker_rejects_injection above; these run against every route.
BAD_TICKERS = ['AAPL & calc', 'AAPL | more', '../../etc/passwd',
               '..\\..\\Windows\\win.ini', 'AAPL $(id)', '', '   ', 'A' * 20,
               123, {'ticker': 'AAPL'}]   # a non-string is a 400, not a 500

# Path-param routes need the payload to survive routing first: an empty segment
# is a 404 rather than a 400, and %2F would decode into a path separator.
BAD_PATH_TICKERS = ['AAPL%20%26%20calc', 'AAPL%7Cmore', 'AAPL%24(id)', 'A' * 20]


@pytest.fixture
def portfolio_client(tmp_path, monkeypatch):
    """Give this test its own per-user data root.

    conftest already redirects the root away from the repo; this narrows it to
    one directory per test so writes from different tests cannot see each other.
    """
    monkeypatch.setattr(terminal, '_USER_DATA_ROOT', str(tmp_path / 'users'))
    monkeypatch.setattr(terminal, '_user_stores', {})
    return terminal.app.test_client()


def _stored_symbols():
    """Every symbol value the portfolio files hold, under each route's key.

    owner= is explicit because these run outside a request: the per-user stores
    resolve the account from the session, and refuse rather than guess when
    there isn't one.
    """
    me = TEST_USER
    rows = ([(w, 'ticker')     for w in terminal.load_watchlist(owner=me)] +
            [(h, 'ticker')     for h in terminal.load_holdings(owner=me)] +
            [(t, 'ticker')     for t in terminal.load_transactions(owner=me)] +
            [(s, 'ticker')     for s in terminal.load_sales(owner=me)] +
            [(o, 'underlying') for o in terminal.load_options(owner=me)] +
            [(v, 'ticker')     for v in terminal.load_valuations(owner=me)])
    return [r.get(k) for r, k in rows if r.get(k) is not None]


@pytest.mark.parametrize('bad', BAD_TICKERS)
def test_watchlist_post_refuses_a_non_symbol(portfolio_client, bad):
    assert portfolio_client.post('/api/watchlist',
                                 json={'ticker': bad}).status_code == 400
    assert terminal.load_watchlist(owner=TEST_USER) == []


@pytest.mark.parametrize('bad', BAD_TICKERS)
def test_holdings_post_refuses_a_non_symbol(portfolio_client, bad):
    """This one writes the ledger and rebuilds holdings.json off it, so a single
    accepted bad symbol reaches two files."""
    r = portfolio_client.post('/api/holdings',
                              json={'ticker': bad, 'shares': 10, 'price': 100})
    assert r.status_code == 400
    assert terminal.load_transactions(owner=TEST_USER) == []
    assert terminal.load_holdings(owner=TEST_USER) == []


@pytest.mark.parametrize('bad', BAD_TICKERS)
def test_quick_transaction_refuses_a_non_symbol(portfolio_client, bad):
    r = portfolio_client.post('/api/transactions/quick', json={
        'ticker': bad, 'shares': 10, 'sell_price': 50,
        'buy_date': '2026-01-01', 'sell_date': '2026-02-01', 'gain': 100})
    assert r.status_code == 400
    assert terminal.load_transactions(owner=TEST_USER) == []
    assert terminal.load_sales(owner=TEST_USER) == []


def test_quick_transaction_validates_before_it_calls_out(portfolio_client,
                                                         monkeypatch):
    """The name lookup sends the symbol to Yahoo as a query term. Validation
    runs first, so a rejected body never leaves the process."""
    import requests
    calls = []
    monkeypatch.setattr(requests, 'get',
                        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
                            AssertionError('must not be reached')))
    r = portfolio_client.post('/api/transactions/quick', json={
        'ticker': 'AAPL & calc', 'shares': 10, 'sell_price': 50,
        'buy_date': '2026-01-01', 'sell_date': '2026-02-01', 'gain': 100})
    assert r.status_code == 400
    assert calls == []


@pytest.mark.parametrize('bad', BAD_TICKERS)
def test_editing_a_transaction_refuses_a_non_symbol(portfolio_client, bad):
    """An edit rewrites the ledger row, and both rebuilds run off it."""
    terminal.save_transactions([
        {'id': '1782960300693-aa', 'type': 'buy', 'ticker': 'MFI.TO',
         'name': 'Maple Leaf', 'shares': 10, 'price': 25, 'date': '2026-01-05'}],
        owner=TEST_USER)

    r = portfolio_client.put('/api/transactions/1782960300693-aa', json={
        'type': 'buy', 'ticker': bad, 'shares': 10, 'price': 25,
        'date': '2026-01-05'})
    assert r.status_code == 400
    assert [t['ticker'] for t in terminal.load_transactions(owner=TEST_USER)] == ['MFI.TO']


@pytest.mark.parametrize('bad', BAD_PATH_TICKERS)
def test_selling_a_holding_refuses_a_non_symbol(portfolio_client, bad):
    """The path segment is written into the sale row and the ledger, so it is a
    write boundary even though the holding lookup would 404 on it anyway."""
    r = portfolio_client.post(f'/api/holdings/{bad}/sell',
                              json={'shares_sold': 1, 'sale_price': 10})
    assert r.status_code == 400
    assert terminal.load_sales(owner=TEST_USER) == []
    assert terminal.load_transactions(owner=TEST_USER) == []


@pytest.mark.parametrize('bad', BAD_TICKERS)
def test_buying_an_option_refuses_a_non_symbol_underlying(portfolio_client, bad):
    r = portfolio_client.post('/api/options', json={
        'underlying': bad, 'option_type': 'call', 'strike': 100,
        'contracts': 1, 'buy_price': 2.5})
    assert r.status_code == 400
    assert terminal.load_options(owner=TEST_USER) == []


@pytest.mark.parametrize('bad', BAD_TICKERS)
def test_editing_an_option_refuses_a_non_symbol_underlying(portfolio_client, bad):
    """The edit route copies the request body into the record field by field,
    so it must not be the way around the check the buy route makes."""
    portfolio_client.post('/api/options', json={
        'underlying': 'MSFT', 'option_type': 'call', 'strike': 100,
        'contracts': 1, 'buy_price': 2.5})
    opt_id = terminal.load_options(owner=TEST_USER)[0]['id']

    r = portfolio_client.put(f'/api/options/{opt_id}', json={'underlying': bad})
    assert r.status_code == 400
    assert terminal.load_options(owner=TEST_USER)[0]['underlying'] == 'MSFT'


@pytest.mark.parametrize('bad', BAD_TICKERS)
def test_valuations_post_refuses_a_non_symbol(portfolio_client, bad):
    r = portfolio_client.post('/api/valuations',
                              json={'ticker': bad, 'buy_price': 100})
    assert r.status_code == 400
    assert terminal.load_valuations(owner=TEST_USER) == []


def test_no_mutating_route_can_store_a_non_symbol(portfolio_client):
    """The invariant itself: drive an injection through every route that
    persists a symbol, then read the files back."""
    payload = 'AAPL & calc'
    attempts = [
        ('post', '/api/watchlist', {'ticker': payload}),
        ('post', '/api/holdings',  {'ticker': payload, 'shares': 10, 'price': 100}),
        ('post', '/api/transactions/quick',
         {'ticker': payload, 'shares': 10, 'sell_price': 50,
          'buy_date': '2026-01-01', 'sell_date': '2026-02-01', 'gain': 100}),
        ('put',  '/api/transactions/1782960300693-aa',
         {'type': 'buy', 'ticker': payload, 'shares': 10, 'price': 25,
          'date': '2026-01-05'}),
        ('post', '/api/holdings/AAPL%20%26%20calc/sell',
         {'shares_sold': 1, 'sale_price': 10}),
        ('post', '/api/options',
         {'underlying': payload, 'option_type': 'call', 'strike': 100,
          'contracts': 1, 'buy_price': 2.5}),
        ('post', '/api/valuations', {'ticker': payload, 'buy_price': 100}),
    ]
    for method, url, body in attempts:
        r = getattr(portfolio_client, method)(url, json=body)
        assert r.status_code == 400, f'{method.upper()} {url} accepted {payload!r}'

    stored = _stored_symbols()
    assert stored == []
    assert [s for s in stored if terminal.clean_ticker(s) is None] == []


def test_an_accepted_write_stores_the_normalised_symbol(portfolio_client):
    """Rejecting is half the guard; the other half is that what does get through
    is stored in clean_ticker()'s normal form, so a reader's == compare works."""
    portfolio_client.post('/api/watchlist', json={'ticker': ' brk.b '})
    assert [w['ticker'] for w in terminal.load_watchlist(owner=TEST_USER)] == ['BRK.B']

    portfolio_client.post('/api/holdings',
                          json={'ticker': 'mfi.to', 'shares': 10, 'price': 25})
    assert [t['ticker'] for t in terminal.load_transactions(owner=TEST_USER)] == ['MFI.TO']
    assert [h['ticker'] for h in terminal.load_holdings(owner=TEST_USER)]     == ['MFI.TO']

    portfolio_client.post('/api/options', json={
        'underlying': 'teck-b.to', 'option_type': 'call', 'strike': 50,
        'contracts': 1, 'buy_price': 1.5})
    assert [o['underlying'] for o in terminal.load_options(owner=TEST_USER)] == ['TECK-B.TO']

    assert [s for s in _stored_symbols() if terminal.clean_ticker(s) is None] == []


def test_portfolio_symbols_still_filters_what_it_reads(monkeypatch):
    """The write routes validate now, but these files are hand-editable and the
    read-side filter is what keeps a pasted row out of a yfinance call."""
    monkeypatch.setattr(terminal, 'load_holdings',
                        lambda: [{'ticker': 'AAPL & calc'}, {'ticker': 'MSFT'}])
    monkeypatch.setattr(terminal, 'load_watchlist', lambda: [])
    assert terminal._portfolio_symbols() == ['MSFT']


# ---------------------------------------------------------------------------
# format_large_number — negatives used to be scaled against the wrong threshold
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value,expected', [
    (2.8e12,   '$2.80T'),
    (5.6e9,    '$5.60B'),
    (4.5e6,    '$4.50M'),
    (None,     'N/A'),
    (0,        '$0.00M'),
    (-4.5e9,   '-$4.50B'),   # was '$-4500.00M'
    (-1.2e12,  '-$1.20T'),   # was '$-1200000.00M'
    (-5e5,     '-$0.50M'),
])
def test_format_large_number(value, expected):
    assert terminal.format_large_number(value) == expected


def test_negative_billions_not_reported_as_millions():
    """The specific failure: a -$4.5B loss rendered as '$-4500.00M', which reads
    as millions and understates the magnitude by three orders of magnitude."""
    out = terminal.format_large_number(-4.5e9)
    assert out.endswith('B') and out.startswith('-')


# ---------------------------------------------------------------------------
# classify_insider — ported from stock-intel
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('text,kind', [
    ('Sale at price 214.50 per share.',                    'sell'),
    ('Purchase at price 45.10 per share.',                 'buy'),
    # Canadian phrasing: the old `'Purchase' in text` test dropped all of these,
    # which left the insider panel completely empty for RY.TO and TD.TO.
    ('Disposition in the public market at price 206.49 per share.', 'sell'),
    ('Disposition under a purchase/ownership plan',        'sell'),
    ('Acquisition under a purchase/ownership plan',        'buy'),
    # Not conviction trades.
    ('Redemption, retraction, cancelation, repurchase',    'exclude'),
    ('Grant/award of equity instruments',                  'exclude'),
    ('Vesting of restricted share units',                  'exclude'),
    ('Compensation for services at price 119.81 per share.', 'exclude'),
    ('Exercise of options',                                'exclude'),
    ('Bona fide gift',                                     'exclude'),
    ('',                                                   'exclude'),
    (None,                                                 'exclude'),
])
def test_classify_insider(text, kind):
    assert terminal.classify_insider(text)[0] == kind


def test_exclusions_tested_before_buy_sell():
    """Order matters: 'Disposition under a purchase/ownership plan' contains
    'purchase' but is a sale, and a buyback contains 'repurchase'."""
    assert terminal.classify_insider('Disposition under a purchase/ownership plan')[0] == 'sell'
    assert terminal.classify_insider('Redemption, retraction, cancelation, repurchase')[0] == 'exclude'


def test_blank_description_is_labelled_not_silently_dropped():
    kind, label = terminal.classify_insider('')
    assert (kind, label) == ('exclude', 'undisclosed')


# ---------------------------------------------------------------------------
# _build_ownership — missing is not zero, and >100% is not clampable
# ---------------------------------------------------------------------------

def test_absent_ownership_is_dropped_not_read_as_zero():
    """Yahoo returns None for both fields on most non-US listings, and there is
    no fallback — major_holders is empty for them too. The old `or 0` turned
    that into 0% institutional, so retail fell out of `1 - 0 - 0` at 100% and
    the page drew a full doughnut claiming GOOG.TO is entirely retail-held.
    """
    assert terminal._build_ownership({}) == {}
    assert terminal._build_ownership(
        {'heldPercentInstitutions': None, 'heldPercentInsiders': None}) == {}
    # NaN survives an `is None` check and would serialise as invalid JSON.
    assert terminal._build_ownership(
        {'heldPercentInstitutions': float('nan'),
         'heldPercentInsiders': float('nan')}) == {}


def test_a_genuine_zero_still_reports():
    """The point is telling missing from zero, so a real 0% must survive."""
    own = terminal._build_ownership(
        {'heldPercentInstitutions': 0.0, 'heldPercentInsiders': 0.0})
    assert own['institutional'] == 0.0
    assert own['retail'] == 100.0


def test_ordinary_split_sums_to_one_hundred():
    own = terminal._build_ownership(          # AAPL, as reported
        {'heldPercentInstitutions': 0.66289, 'heldPercentInsiders': 0.01647})
    assert own['institutional'] == 66.29
    assert own['insider'] == 1.65
    assert own['exceeds_outstanding'] is False
    assert abs(own['institutional'] + own['insider'] + own['retail'] - 100) < 0.01


@pytest.mark.parametrize('inst,insider', [
    (1.2325, 0.0074),   # WING
    (1.1224, 0.0304),   # CARG
    (1.0570, 0.0211),   # CVNA
    (0.8175, 0.1899),   # TREE — only just over
])
def test_over_one_hundred_percent_suppresses_retail_rather_than_clamping(inst, insider):
    """13F filings double-count lent shares, so institutional legitimately
    passes 100%. Clamping retail to 0 asserts "no retail float", which the data
    does not say; the flag lets the frontend drop the doughnut instead of
    letting Chart.js renormalise 123% into a slice drawn as 99%.
    """
    own = terminal._build_ownership(
        {'heldPercentInstitutions': inst, 'heldPercentInsiders': insider})
    assert own['exceeds_outstanding'] is True
    assert own['retail'] is None
    assert own['institutional'] == round(inst * 100, 2)   # reported as-is


def test_one_half_present_reports_without_implying_the_rest():
    own = terminal._build_ownership({'heldPercentInstitutions': 0.5})
    assert own['institutional'] == 50.0
    assert own['insider'] is None
    assert own['retail'] is None


# ---------------------------------------------------------------------------
# JsonStore — atomic writes, loud failures, no lost updates
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = terminal.JsonStore.__new__(terminal.JsonStore)
    s.path = str(tmp_path / 'data.json')
    s._default = list
    s._migrate = None
    s._lock = threading.RLock()
    return s


def test_missing_file_returns_default(store):
    assert store.load() == []


def test_save_is_atomic_and_leaves_no_temp_file(store):
    store.save([{'ticker': 'AAPL'}])
    with open(store.path) as f:
        assert json.load(f) == [{'ticker': 'AAPL'}]
    assert not os.path.exists(store.path + '.tmp')


def test_corrupt_file_raises_instead_of_reporting_empty(store):
    """The old loader returned [] on a parse error, so a corrupt holdings.json
    showed an empty portfolio — and the next save wrote that emptiness back."""
    with open(store.path, 'w') as f:
        f.write('{"truncated": ')
    with pytest.raises(RuntimeError, match='corrupt'):
        store.load()


def test_concurrent_mutate_loses_no_writes(store):
    """load -> append -> save from two threads used to lose one of the two
    appends. mutate() holds the lock across the whole read-modify-write."""
    store.save([])
    workers, per_worker = 8, 40

    def work(w):
        for i in range(per_worker):
            store.mutate(lambda cur: cur + [f'{w}-{i}'])

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.load()
    assert len(final) == workers * per_worker
    assert len(set(final)) == workers * per_worker


def test_shared_lock_keeps_related_stores_consistent(tmp_path):
    """A sell writes holdings, transactions and cash. They share PORTFOLIO_LOCK
    so another request can't observe or interleave a half-applied sell."""
    lock = threading.RLock()

    def mk(name, default):
        s = terminal.JsonStore.__new__(terminal.JsonStore)
        s.path = str(tmp_path / name)
        s._default, s._migrate, s._lock = default, None, lock
        return s

    holdings, cash = mk('h.json', list), mk('c.json', dict)
    holdings.save([])
    cash.save({'balance': 0})

    def sell(i):
        with lock:
            holdings.mutate(lambda cur: cur + [i])
            cash.save({'balance': cash.load()['balance'] + 1})

    threads = [threading.Thread(target=sell, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(holdings.load()) == 50
    assert cash.load()['balance'] == 50


# ---------------------------------------------------------------------------
# Transaction ids — a bare millisecond stamp collided
# ---------------------------------------------------------------------------

def test_txn_ids_unique_under_tight_loop():
    """Two transactions recorded in the same millisecond got the same id, and
    the id is what DELETE and PUT match on."""
    ids = set()
    for _ in range(2000):
        ids.add(terminal._new_txn_id(ids))
    assert len(ids) == 2000


def test_txn_id_avoids_supplied_existing_ids():
    existing = {terminal._new_txn_id(set()) for _ in range(20)}
    assert terminal._new_txn_id(existing) not in existing


def test_txn_id_keeps_sortable_timestamp_prefix():
    """rebuild_holdings_from_transactions() sorts on (date, id) and relies on
    the id to order same-day transactions by insertion."""
    a = terminal._new_txn_id(set())
    b = terminal._new_txn_id({a})
    assert a.split('-')[0].isdigit()
    assert len(a.split('-')[0]) == 13
    assert a.split('-')[0] <= b.split('-')[0]


# ---------------------------------------------------------------------------
# Stock cache
# ---------------------------------------------------------------------------

def test_cache_serves_second_call_without_refetching(monkeypatch):
    calls = []

    def fake(tkkr):
        calls.append(tkkr)
        return {'ticker': tkkr, 'price': '100.00', 'name': 'Test'}

    monkeypatch.setattr(terminal, '_do_get_stock', fake)
    monkeypatch.setattr(terminal, '_refresh_quote', lambda d: d)
    terminal._stock_cache.clear()

    d1, cached1 = terminal._cached_stock('TEST')
    d2, cached2 = terminal._cached_stock('TEST')

    assert calls == ['TEST']
    assert (cached1, cached2) == (False, True)
    assert d1 == d2


def test_cache_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal, '_do_get_stock',
                        lambda t: calls.append(t) or {'ticker': t})
    terminal._stock_cache.clear()

    terminal._cached_stock('TEST')
    # Age the entry past the TTL.
    terminal._stock_cache['TEST']['ts'] -= (terminal.STOCK_TTL + 1)
    terminal._cached_stock('TEST')

    assert len(calls) == 2


def test_concurrent_requests_share_one_upstream_fetch(monkeypatch):
    """Five simultaneous lookups of the same ticker must not fire five sets of
    ~9 upstream calls each."""
    import time
    calls = []

    def slow(tkkr):
        calls.append(tkkr)
        time.sleep(0.3)
        return {'ticker': tkkr}

    monkeypatch.setattr(terminal, '_do_get_stock', slow)
    monkeypatch.setattr(terminal, '_refresh_quote', lambda d: d)
    terminal._stock_cache.clear()

    results = []
    threads = [threading.Thread(target=lambda: results.append(terminal._cached_stock('TEST')))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert len(results) == 5


def test_refresh_quote_preserves_payload_types(monkeypatch):
    """price is a 2dp string and day_change_pct a rounded float. A refreshed
    cached payload must be indistinguishable from a fresh one."""
    class FakeFast:
        last_price = 123.456
        previous_close = 120.0

    monkeypatch.setattr(terminal.yf, 'Ticker', lambda t: type('T', (), {'fast_info': FakeFast()})())
    out = terminal._refresh_quote({'ticker': 'TEST', 'price': '100.00', 'day_change_pct': 1.0})

    assert out['price'] == '123.46'
    assert isinstance(out['price'], str)
    assert isinstance(out['day_change_pct'], float)
    assert out['day_change_pct'] == round((123.456 - 120.0) / 120.0 * 100, 2)


def test_refresh_quote_survives_upstream_failure(monkeypatch):
    def boom(t):
        raise RuntimeError('yahoo down')

    monkeypatch.setattr(terminal.yf, 'Ticker', boom)
    original = {'ticker': 'TEST', 'price': '100.00'}
    assert terminal._refresh_quote(original) == original


def test_stock_route_rejects_invalid_ticker():
    c = terminal.app.test_client()
    r = c.get('/api/stock?ticker=AAPL%20%26%20calc')
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Corporate-action cache
#
# One portfolio load called _build_div_events 81 times for 22 distinct tickers
# across four endpoints, each call a .dividends plus a .info fetch — ~19s of
# upstream work per load.
# ---------------------------------------------------------------------------

@pytest.fixture
def corp_caches():
    """These caches are process-global; isolate each test from the last."""
    terminal._div_events_cache.clear()
    terminal._splits_cache.clear()
    yield


def test_div_events_fetched_once_across_endpoints(corp_caches, monkeypatch):
    calls = []
    monkeypatch.setattr(terminal, '_fetch_div_events',
                        lambda t: calls.append(t) or [])

    for _ in range(4):          # what the four portfolio endpoints do
        terminal._build_div_events('TEST.TO')

    assert calls == ['TEST.TO']


def test_div_events_cache_expires(corp_caches, monkeypatch):
    calls = []
    monkeypatch.setattr(terminal, '_fetch_div_events',
                        lambda t: calls.append(t) or [])

    terminal._build_div_events('TEST.TO')
    # Age the entry past the TTL.
    value, ts = terminal._div_events_cache._data['TEST.TO']
    terminal._div_events_cache._data['TEST.TO'] = (value, ts - (terminal.CORP_ACTIONS_TTL + 1))
    terminal._build_div_events('TEST.TO')

    assert len(calls) == 2


def test_failed_lookup_is_not_cached(corp_caches, monkeypatch):
    """A transient Yahoo failure must not pin dividend income to zero for the
    whole TTL — the caller cannot tell [] from 'pays no dividend'."""
    state = {'fail': True}

    def flaky(tkr):
        if state['fail']:
            raise RuntimeError('yahoo down')
        return [('2026-01-01', '2026-02-01', 0.25)]

    monkeypatch.setattr(terminal, '_fetch_div_events', flaky)

    assert terminal._build_div_events('TEST.TO') == []
    state['fail'] = False
    assert terminal._build_div_events('TEST.TO') == [('2026-01-01', '2026-02-01', 0.25)]


def test_genuinely_empty_result_is_cached(corp_caches, monkeypatch):
    """A ticker that pays no dividend returns [] too, but that answer is real
    and must not be re-fetched every request."""
    calls = []
    monkeypatch.setattr(terminal, '_fetch_div_events',
                        lambda t: calls.append(t) or [])

    assert terminal._build_div_events('NODIV.TO') == []
    assert terminal._build_div_events('NODIV.TO') == []
    assert len(calls) == 1


def test_concurrent_lookups_share_one_fetch(corp_caches, monkeypatch):
    """The endpoints fan out 8 threads wide over the same tickers and run
    concurrently with each other."""
    import time
    calls = []

    def slow(tkr):
        calls.append(tkr)
        time.sleep(0.3)
        return []

    monkeypatch.setattr(terminal, '_fetch_div_events', slow)

    threads = [threading.Thread(target=lambda: terminal._build_div_events('TEST.TO'))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == ['TEST.TO']


def test_splits_drop_noop_ratios(corp_caches, monkeypatch):
    """Ratios of 0 and 1 are no-ops that would only complicate the position
    adjustment in /api/holdings/chart."""
    import datetime

    class FakeSeries:
        empty = False
        def items(self):
            return [
                (datetime.datetime(2022, 6, 29), 10.0),
                (datetime.datetime(2023, 1, 3),  1.0),
                (datetime.datetime(2023, 5, 9),  0.0),
            ]

    monkeypatch.setattr(terminal.yf, 'Ticker',
                        lambda t: type('T', (), {'splits': FakeSeries()})())

    assert terminal._build_splits('TEST.TO') == {datetime.date(2022, 6, 29): 10.0}


# ---------------------------------------------------------------------------
# Batched tape quotes — replaces ~100 single-symbol requests per tape load
# ---------------------------------------------------------------------------

@pytest.fixture
def quotes_client(monkeypatch):
    """Stub the upstream so these test the endpoint, not Yahoo."""
    monkeypatch.setattr(terminal, '_fast_quote', lambda s: (100.0, 1.5))
    return terminal.app.test_client()


def test_batch_quotes_returns_all_symbols(quotes_client):
    r = quotes_client.get('/api/quotes?tickers=RY.TO,TD.TO,BNS.TO')
    assert r.status_code == 200
    d = r.get_json()
    assert set(d) == {'RY.TO', 'TD.TO', 'BNS.TO'}
    assert d['RY.TO'] == {'price': 100.0, 'change': 1.5}


def test_batch_quotes_dedupes(quotes_client):
    d = quotes_client.get('/api/quotes?tickers=AAPL,AAPL,AAPL,aapl').get_json()
    assert list(d) == ['AAPL']


def test_batch_quotes_drops_invalid_symbols_keeps_valid(quotes_client):
    """One bad symbol must not poison the whole batch."""
    d = quotes_client.get('/api/quotes?tickers=AAPL %26 calc,MSFT').get_json()
    assert 'MSFT' in d
    assert not any('calc' in k for k in d)


def test_batch_quotes_caps_batch_size(monkeypatch):
    seen = []

    def counting(sym):
        seen.append(sym)
        return 1.0, 0.0

    monkeypatch.setattr(terminal, '_fast_quote', counting)
    c = terminal.app.test_client()
    many = ','.join(f'SYM{i}' for i in range(120))
    c.get(f'/api/quotes?tickers={many}')
    assert len(seen) == terminal.MAX_BATCH_QUOTES


def test_batch_quotes_empty_input(quotes_client):
    assert quotes_client.get('/api/quotes?tickers=').get_json() == {}
    assert quotes_client.get('/api/quotes').get_json() == {}


def test_batch_quotes_omits_failed_symbols(monkeypatch):
    """A symbol with no quote is absent, not present-with-null — the tape keeps
    the price it already had rather than blanking the row."""
    monkeypatch.setattr(terminal, '_fast_quote',
                        lambda s: (None, None) if s == 'BAD' else (50.0, 2.0))
    d = terminal.app.test_client().get('/api/quotes?tickers=GOOD,BAD').get_json()
    assert 'GOOD' in d and 'BAD' not in d


# ---------------------------------------------------------------------------
# Parallel prefetch
# ---------------------------------------------------------------------------

def test_prefetch_touches_every_property_concurrently():
    touched = []

    class FakeTicker:
        def __getattr__(self, name):
            touched.append(name)
            return f'value-{name}'

    terminal._prefetch_ticker(FakeTicker())
    assert set(touched) == set(terminal._PREFETCH_PROPS)


def test_prefetch_swallows_property_errors():
    """A property that raises must not abort the lookup — the sections below
    have their own error handling."""
    class BrokenTicker:
        def __getattr__(self, name):
            raise RuntimeError(f'{name} unavailable')

    terminal._prefetch_ticker(BrokenTicker())  # must not raise


def test_prefetch_list_matches_what_the_lookup_reads():
    """If _do_get_stock starts reading a new property, it should be prefetched
    too, or it silently reverts to a sequential round trip."""
    import re
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'app.py'), encoding='utf-8') as f:
        src = f.read()
    body = src[src.index('def _do_get_stock'):src.index("@app.route('/api/stock'")]
    read = set(re.findall(r'\bticker\.([a-z_]+)\b', body))
    # history() takes arguments so it can't be prefetched blind; info is read
    # directly after the prefetch call.
    ignorable = {'history'}
    missed = read - set(terminal._PREFETCH_PROPS) - ignorable
    assert not missed, f'read but not prefetched: {sorted(missed)}'


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def test_news_starts_without_a_name_from_the_client(monkeypatch):
    """The frontend no longer passes name, so the backend must resolve it —
    otherwise headline filtering drops everything that doesn't contain the
    literal ticker string."""
    captured = {}
    monkeypatch.setattr(terminal, '_resolve_company_name', lambda t: 'Royal Bank of Canada')

    def fake_build(tkkr, name='', lite=False, groq_key=''):
        captured['name'] = name or terminal._resolve_company_name(tkkr)
        return [{'title': 'x', 'url': 'https://e.com'}]

    monkeypatch.setattr(terminal, '_build_news', fake_build)
    terminal._news_cache.clear()
    terminal.app.test_client().get('/api/news?ticker=RY.TO')
    assert captured['name'] == 'Royal Bank of Canada'


def test_resolve_company_name_prefers_warm_stock_cache(monkeypatch):
    """Must not hit the network when a lookup already holds the name."""
    def boom(t):
        raise AssertionError('should not touch yfinance')

    monkeypatch.setattr(terminal.yf, 'Ticker', boom)
    terminal._name_cache.clear()
    with terminal._stock_cache_lock:
        terminal._stock_cache['ZZ'] = {'data': {'name': 'Zed Corp'}, 'ts': 1e12}
    try:
        assert terminal._resolve_company_name('ZZ') == 'Zed Corp'
    finally:
        terminal._stock_cache.pop('ZZ', None)
        terminal._name_cache.clear()


def test_news_cache_hit_avoids_rebuilding(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal, '_build_news',
                        lambda t, n='', lite=False, groq_key='':
                        calls.append(t) or [{'title': 'a', 'url': 'https://e.com'}])
    terminal._news_cache.clear()
    c = terminal.app.test_client()
    c.get('/api/news?ticker=KO')
    c.get('/api/news?ticker=KO')
    assert len(calls) == 1


def test_empty_news_is_not_cached(monkeypatch):
    """Caching [] would pin an empty panel for the whole TTL after one hiccup."""
    monkeypatch.setattr(terminal, '_build_news',
                        lambda t, n='', lite=False, groq_key='': [])
    terminal._news_cache.clear()
    terminal.app.test_client().get('/api/news?ticker=ZZZZ')
    assert 'ZZZZ' not in terminal._news_cache


# ---------------------------------------------------------------------------
# Server binding
# ---------------------------------------------------------------------------

def test_server_does_not_bind_all_interfaces_by_default():
    """Every route is unauthenticated and several mutate the portfolio or the
    stored API keys, so the default bind must be loopback."""
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'app.py'), encoding='utf-8') as f:
        src = f.read()
    assert "host='0.0.0.0'" not in src
    assert "os.environ.get('HOST', '127.0.0.1')" in src


# ---------------------------------------------------------------------------
# Secrets must not be committed
#
# `Launch McKechnie Terminal.bat` set a live Groq key on a `set GROQ_API_KEY=`
# line, and the file is tracked — so the key went to GitHub with the initial
# commit. .gitignore covered settings.json, which is where keys are supposed to
# live, but nothing covered the launcher that shadowed it.
#
# These scan what git actually tracks rather than the working tree: settings.json
# is untracked and legitimately holds real keys, so walking the directory would
# fail on the one file that is doing the right thing.
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Vendor-prefixed key formats. Each requires a long secret body after the
# prefix, so the patterns below do not match their own source text.
_SECRET_PATTERNS = (
    ('Groq',      r'gsk_[A-Za-z0-9]{20,}'),
    ('Anthropic', r'sk-ant-[A-Za-z0-9\-_]{20,}'),
    ('OpenAI-compatible (DeepSeek etc.)', r'sk-[A-Za-z0-9]{24,}'),
)


def _tracked_text_files():
    """(path, text) for every git-tracked file that reads as text.

    Skipped entirely when git is unavailable — the check is about what is
    committed, and without git there is no way to ask that question.
    """
    import subprocess
    try:
        out = subprocess.run(['git', 'ls-files', '-z'], cwd=_REPO_ROOT,
                             capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip('git not available')
    if out.returncode != 0:
        pytest.skip('not a git working tree')

    for rel in out.stdout.decode('utf-8', 'replace').split('\0'):
        if not rel:
            continue
        full = os.path.join(_REPO_ROOT, rel.replace('/', os.sep))
        try:
            with open(full, encoding='utf-8') as f:
                yield rel, f.read()
        except (OSError, UnicodeDecodeError):
            continue        # binary or deleted-but-staged; nothing to scan


def test_no_tracked_file_contains_a_vendor_api_key():
    """A committed key is a published key, whether or not the repo is public."""
    import re
    found = []
    for rel, text in _tracked_text_files():
        for vendor, pattern in _SECRET_PATTERNS:
            if re.search(pattern, text):
                found.append(f'{rel}: looks like a {vendor} key')
    assert not found, 'secret committed to the repo:\n  ' + '\n  '.join(found)


def test_no_tracked_file_sets_a_settings_key_in_the_environment():
    """The format scan above only catches prefixes it already knows; this is the
    general rule for the shape the leak actually took.

    Scoped to environment-assignment syntax — `set NAME=`, `export NAME=`, or a
    bare `NAME=` at the start of a line — because that is how a key gets pasted
    into a launcher, a shell script or a CI file. Deliberately not matching
    `NAME:` or a quoted key: templates/index.html maps every settings key to the
    id of the input that collects it (`'GROQ_API_KEY': 'set-groq'`), which is the
    UI doing its job, and a scan that cries wolf on it is a scan that gets
    deleted.

    `set GROQ_API_KEY=` with nothing after it passes: the empty assignment is
    what documents that the key does not live there.
    """
    import re
    patterns = [
        (k, re.compile(r'^[ \t]*(?:set[ \t]+|export[ \t]+)?' + k +
                       r'[ \t]*=[ \t]*(\S+)', re.M))
        for k in terminal._SETTINGS_KEYS
    ]
    found = []
    for rel, text in _tracked_text_files():
        for key, pattern in patterns:
            for m in pattern.finditer(text):
                value = m.group(1)
                # %VAR% / $VAR / ${VAR} is indirection, not a pasted secret.
                if re.fullmatch(r'%\w+%|\$\{?\w+\}?', value):
                    continue
                found.append(f'{rel}: {key} is given a literal value')
    assert not found, ('an API key value is committed; move it to settings.json:'
                       '\n  ' + '\n  '.join(found))


# ---------------------------------------------------------------------------
# Performance tab — it read sales.json, which nothing keeps in sync
# ---------------------------------------------------------------------------

@pytest.fixture
def perf_ledger(monkeypatch):
    """Install a transaction ledger plus a stale sales.json alongside it."""
    def install(txns, holdings, sales):
        monkeypatch.setattr(terminal, 'load_transactions', lambda: txns)
        monkeypatch.setattr(terminal, 'load_holdings',     lambda: holdings)
        monkeypatch.setattr(terminal, 'load_sales',        lambda: sales)
        res = terminal.app.test_client().get('/api/portfolio/performance')
        return {p['ticker']: p for p in res.get_json()['positions']}
    return install


def test_performance_ignores_sales_records_with_no_transaction(perf_ledger):
    """Deleting a fat-fingered sell rebuilds holdings but leaves its row in
    sales.json. The tab counted that phantom trade's loss forever — a $1 sale of
    157 shares showed as a five-figure realized loss that no longer existed."""
    txns = [
        {'id': '1', 'type': 'buy',  'ticker': 'LULU.TO', 'name': 'Lululemon',
         'shares': 100.0, 'price': 10.0, 'date': '2026-01-15'},
    ]
    holdings = [{'ticker': 'LULU.TO', 'name': 'Lululemon', 'shares': 100.0,
                 'avg_price': 10.0, 'date_acquired': '2026-01-15'}]
    stale = [{'ticker': 'LULU.TO', 'name': 'Lululemon', 'shares_sold': 157.5821,
              'avg_cost': 8.7207, 'sale_price': 1.0, 'sale_date': '2026-05-30',
              'date_acquired': '2026-01-15', 'gain_loss': -1216.6441}]

    pos = perf_ledger(txns, holdings, stale)['LULU.TO']
    assert pos['realized_pl']     == 0.0
    assert pos['cost_basis_sold'] == 0.0
    assert pos['held_cost']       == 1000.0
    assert pos['still_held'] is True


def test_performance_does_not_double_count_a_duplicated_sale(perf_ledger):
    """One sell, two sales.json rows (an edit appended rather than replaced).
    Realized P/L must follow the ledger's single trade."""
    txns = [
        {'id': '1', 'type': 'buy',  'ticker': 'PZA.TO', 'shares': 50.0,
         'price': 14.0, 'date': '2025-11-06'},
        {'id': '2', 'type': 'sell', 'ticker': 'PZA.TO', 'shares': 50.0,
         'price': 16.0, 'date': '2026-01-08'},
    ]
    dupes = [
        {'ticker': 'PZA.TO', 'shares_sold': 50.0, 'avg_cost': 14.0,
         'sale_price': 16.0, 'sale_date': '2026-01-08', 'gain_loss': 100.0},
        {'ticker': 'PZA.TO', 'shares_sold': 50.0, 'avg_cost': 14.4443,
         'sale_price': 16.0, 'sale_date': '2026-01-08', 'gain_loss': 77.8},
    ]

    pos = perf_ledger(txns, [], dupes)['PZA.TO']
    assert pos['realized_pl']     == 100.0
    assert pos['cost_basis_sold'] == 700.0
    assert pos['still_held'] is False


def test_performance_uses_wac_at_sale_not_the_rebought_average(perf_ledger):
    """Sell out, re-buy higher. sales.json stores the average recorded at sale
    time, but only a replay recovers it once the position has been rebuilt."""
    txns = [
        {'id': '1', 'type': 'buy',  'ticker': 'T.TO', 'shares': 100.0,
         'price': 20.0, 'date': '2025-11-07'},
        {'id': '2', 'type': 'sell', 'ticker': 'T.TO', 'shares': 100.0,
         'price': 25.0, 'date': '2026-02-01'},
        {'id': '3', 'type': 'buy',  'ticker': 'T.TO', 'shares': 40.0,
         'price': 30.0, 'date': '2026-04-09'},
    ]
    holdings = [{'ticker': 'T.TO', 'shares': 40.0, 'avg_price': 30.0,
                 'date_acquired': '2026-04-09'}]

    pos = perf_ledger(txns, holdings, [])['T.TO']
    assert pos['realized_pl']     == 500.0    # (25 - 20) * 100
    assert pos['cost_basis_sold'] == 2000.0   # not 40 * 30
    assert pos['first_date']      == '2025-11-07'
    assert pos['last_sale_date']  == '2026-02-01'
    assert pos['still_held'] is True


def test_performance_and_transactions_report_the_same_realized_pl(perf_ledger,
                                                                  monkeypatch):
    """Both tabs replay the same ledger, so their totals must agree exactly."""
    txns = [
        {'id': '1', 'type': 'buy',  'ticker': 'MFI.TO', 'shares': 20.0,
         'price': 25.0, 'date': '2025-12-10'},
        {'id': '2', 'type': 'buy',  'ticker': 'MFI.TO', 'shares': 30.0,
         'price': 28.0, 'date': '2026-02-20'},
        {'id': '3', 'type': 'sell', 'ticker': 'MFI.TO', 'shares': 25.0,
         'price': 31.0, 'date': '2026-06-05'},
    ]
    monkeypatch.setattr(terminal, '_build_div_events', lambda t: [])
    monkeypatch.setattr(terminal, '_warm_corp_actions', lambda syms: None)
    monkeypatch.setattr(terminal, 'load_transactions', lambda: json.loads(json.dumps(txns)))
    monkeypatch.setattr(terminal, 'load_sales', lambda: [])

    client   = terminal.app.test_client()
    from_txn = sum(t.get('gain_loss') or 0
                   for t in client.get('/api/transactions').get_json()
                   if t.get('type') == 'sell')

    monkeypatch.setattr(terminal, 'load_holdings', lambda: [])
    from_perf = sum(p['realized_pl'] for p in
                    client.get('/api/portfolio/performance').get_json()['positions'])

    assert round(from_txn, 2) == round(from_perf, 2)


# ---------------------------------------------------------------------------
# sales.json reconciliation — DELETE and PUT used to leave the sale row behind
# ---------------------------------------------------------------------------

@pytest.fixture
def reconcile(monkeypatch):
    """Run rebuild_sales_from_transactions() over an in-memory pair of ledgers."""
    def run(txns, sales):
        written = []
        monkeypatch.setattr(terminal, 'load_transactions', lambda: txns)
        monkeypatch.setattr(terminal, 'load_sales',        lambda: sales)
        monkeypatch.setattr(terminal, 'save_sales',        written.append)
        out = terminal.rebuild_sales_from_transactions()
        assert written and written[0] == out
        return out
    return run


BUY  = {'id': 'b1', 'type': 'buy',  'ticker': 'T.TO', 'name': 'Telus',
        'shares': 100.0, 'price': 20.0, 'date': '2025-11-07'}
SELL = {'id': 's1', 'type': 'sell', 'ticker': 'T.TO', 'name': 'Telus',
        'shares': 40.0,  'price': 25.0, 'date': '2026-02-01'}


def test_reconcile_drops_sales_whose_transaction_is_gone(reconcile):
    """The row a deleted sell left behind must not survive the rebuild."""
    stale = [
        {'ticker': 'T.TO', 'shares_sold': 40.0, 'avg_cost': 20.0, 'sale_price': 25.0,
         'sale_date': '2026-02-01', 'date_acquired': '2025-11-07', 'gain_loss': 200.0},
        {'ticker': 'MFI.TO', 'shares_sold': 134.0987, 'avg_cost': 26.8928,
         'sale_price': 2.0, 'sale_date': '2026-03-30',
         'date_acquired': '2026-03-10', 'gain_loss': -3338.0921},
    ]
    out = reconcile([BUY, SELL], stale)
    assert [r['ticker'] for r in out] == ['T.TO']
    assert sum(r['gain_loss'] for r in out) == 200.0


def test_reconcile_recomputes_gain_from_the_ledger_not_the_stored_row(reconcile):
    """Editing a sell's price left the pre-edit gain in sales.json."""
    stale = [{'ticker': 'T.TO', 'shares_sold': 40.0, 'avg_cost': 18.0,
              'sale_price': 99.0, 'sale_date': '2026-02-01',
              'date_acquired': '2025-11-07', 'gain_loss': 3240.0}]
    row = reconcile([BUY, SELL], stale)[0]
    assert row['avg_cost']   == 20.0
    assert row['sale_price'] == 25.0
    assert row['gain_loss']  == 200.0


def test_reconcile_carries_acquisition_date_across(reconcile):
    """date_acquired drives the dividend window and cannot be recomputed from
    the ledger, so a rebuild must preserve it rather than regenerate it."""
    prior = [{'txn_id': 's1', 'ticker': 'T.TO', 'shares_sold': 40.0, 'avg_cost': 20.0,
              'sale_price': 25.0, 'sale_date': '2026-02-01',
              'date_acquired': '2025-06-30', 'gain_loss': 200.0}]
    assert reconcile([BUY, SELL], prior)[0]['date_acquired'] == '2025-06-30'


def test_reconcile_rejects_an_acquisition_date_after_the_sale(reconcile):
    """Two rows carried a year that hadn't happened yet. A date later than the
    sale is a typo, not history — fall back to the FIFO lot date."""
    typo = [{'ticker': 'T.TO', 'shares_sold': 40.0, 'avg_cost': 20.0,
             'sale_price': 25.0, 'sale_date': '2026-02-01',
             'date_acquired': '2026-11-14', 'gain_loss': 200.0}]
    assert reconcile([BUY, SELL], typo)[0]['date_acquired'] == '2025-11-07'


def test_reconcile_derives_acquisition_date_fifo(reconcile):
    """With nothing to inherit, the shares sold are the oldest open lot's."""
    txns = [
        dict(BUY, id='b1', shares=10.0, price=20.0, date='2025-11-07'),
        dict(BUY, id='b2', shares=10.0, price=30.0, date='2026-01-05'),
        dict(SELL, id='s1', shares=5.0, price=40.0, date='2026-03-01'),
    ]
    assert reconcile(txns, [])[0]['date_acquired'] == '2025-11-07'


def test_reconcile_pairs_identical_sells_with_distinct_prior_rows(reconcile):
    """Two sells of the same size and price on different days must not both
    inherit the same acquisition date."""
    txns = [
        dict(BUY, id='b1', shares=100.0, price=20.0, date='2025-11-07'),
        dict(SELL, id='s1', shares=14.0, price=41.4, date='2026-01-15'),
        dict(SELL, id='s2', shares=14.0, price=41.4, date='2026-01-16'),
    ]
    prior = [
        {'ticker': 'T.TO', 'shares_sold': 14.0, 'sale_price': 41.4,
         'sale_date': '2026-01-15', 'date_acquired': '2025-11-07', 'avg_cost': 20.0},
        {'ticker': 'T.TO', 'shares_sold': 14.0, 'sale_price': 41.4,
         'sale_date': '2026-01-16', 'date_acquired': '2025-11-12', 'avg_cost': 20.0},
    ]
    got = {r['sale_date']: r['date_acquired'] for r in reconcile(txns, prior)}
    assert got == {'2026-01-15': '2025-11-07', '2026-01-16': '2025-11-12'}


def test_reconcile_stamps_txn_id_on_every_row(reconcile):
    """The id is what lets a later rebuild find the row it is replacing."""
    out = reconcile([BUY, SELL], [])
    assert [r['txn_id'] for r in out] == ['s1']


def test_deleting_a_sell_transaction_rebuilds_sales(monkeypatch, tmp_path):
    """End to end: the drift that made the performance tab wrong."""
    txns  = [dict(BUY), dict(SELL)]
    state = {'sales': [], 'holdings': []}
    monkeypatch.setattr(terminal, 'load_transactions', lambda: list(txns))
    monkeypatch.setattr(terminal, 'save_transactions', lambda v: (txns.clear(), txns.extend(v)))
    monkeypatch.setattr(terminal, 'load_sales',    lambda: list(state['sales']))
    monkeypatch.setattr(terminal, 'save_sales',    lambda v: state.__setitem__('sales', v))
    monkeypatch.setattr(terminal, 'load_holdings', lambda: list(state['holdings']))
    monkeypatch.setattr(terminal, 'save_holdings', lambda v: state.__setitem__('holdings', v))

    client = terminal.app.test_client()
    terminal.rebuild_sales_from_transactions()
    assert len(state['sales']) == 1

    client.delete('/api/transactions/s1')
    assert state['sales'] == []

    perf = client.get('/api/portfolio/performance').get_json()['positions']
    assert sum(p['realized_pl'] for p in perf) == 0.0


# ---------------------------------------------------------------------------
# Transaction ids are strings — routes that parse them must not drop rows
# ---------------------------------------------------------------------------

@pytest.fixture
def bare_portfolio(monkeypatch):
    """Isolate the cash-pool walk: no upstream corporate actions, no options.

    These routes fan out to dividend/split history, so keep them offline. And
    `_compute_invested()` reads options.json now that cash is derived from the
    ledger — a test that stubs only the transactions would otherwise pick up the
    real option book and see its gain in the pool.
    """
    monkeypatch.setattr(terminal, '_build_div_events', lambda t: [])
    monkeypatch.setattr(terminal, '_build_splits',     lambda t: {})
    monkeypatch.setattr(terminal, 'load_options',      lambda: [])


def test_invested_counts_transactions_with_suffixed_ids(monkeypatch, bare_portfolio):
    """`_new_txn_id()` returns '<ms>-<hex>'. /api/portfolio/invested parsed the
    id with int(), which raised and was swallowed by a bare `except: pass` — the
    whole transaction disappeared from the cash-pool walk.

    The shares still reached holdings.json (rebuilt by a different code path),
    so their market value counted toward funds while the capital that bought
    them never counted toward `invested`. The Holdings tab reported the
    difference as profit. Three live buys totalling $2,403.69 were being
    dropped this way, inflating the reported return by ~6 points."""
    txns = [
        {'id': '1782960300693',        'type': 'buy', 'ticker': 'A.TO',
         'name': 'A', 'shares': 10, 'price': 100, 'date': '2026-01-05'},
        {'id': '1785189226799-12ebf0', 'type': 'buy', 'ticker': 'B.TO',
         'name': 'B', 'shares': 10, 'price': 100, 'date': '2026-01-06'},
    ]
    monkeypatch.setattr(terminal, 'load_transactions', lambda: txns)

    d = terminal.app.test_client().get('/api/portfolio/invested').get_json()
    assert d['invested'] == 2000.0, 'suffixed-id buy was dropped from the pool'


def test_invested_pool_identity_holds_across_id_formats(monkeypatch, bare_portfolio):
    """invested = buys - sells - leftover pool. A dropped row breaks it."""
    txns = [
        {'id': '1782960300693',        'type': 'buy',  'ticker': 'A.TO',
         'name': 'A', 'shares': 10, 'price': 100, 'date': '2026-01-05'},
        {'id': '1785189226799-12ebf0', 'type': 'sell', 'ticker': 'A.TO',
         'name': 'A', 'shares': 10, 'price': 150, 'date': '2026-02-05'},
        {'id': '1785189226800-9ab23d', 'type': 'buy',  'ticker': 'B.TO',
         'name': 'B', 'shares': 10, 'price': 100, 'date': '2026-03-05'},
    ]
    monkeypatch.setattr(terminal, 'load_transactions', lambda: txns)

    d = terminal.app.test_client().get('/api/portfolio/invested').get_json()
    # $1000 in, sold for $1500, redeployed $1000 from the pool: no new capital.
    assert d['invested']  == 1000.0
    assert d['cash_pool'] == 500.0


def test_holdings_chart_counts_transactions_with_suffixed_ids(monkeypatch, bare_portfolio):
    """Same int(id) drop bent both series on /api/holdings/chart."""
    import datetime as _d
    seen = {}

    def _fake_download(tickers, **kw):
        import pandas as pd
        seen['tickers'] = sorted(tickers)
        idx = pd.date_range('2026-01-05', periods=3, freq='D')
        return pd.concat({'Close': pd.DataFrame(
            {t: [100.0] * 3 for t in sorted(tickers)}, index=idx)}, axis=1)

    monkeypatch.setattr(terminal.yf, 'download', _fake_download)
    monkeypatch.setattr(terminal, '_build_div_events', lambda t: [])
    monkeypatch.setattr(terminal, '_build_splits',     lambda t: {})
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        {'id': '1785189226799-12ebf0', 'type': 'buy', 'ticker': 'ONLY.TO',
         'name': 'Only', 'shares': 5, 'price': 100, 'date': '2026-01-05'},
    ])

    r = terminal.app.test_client().get('/api/holdings/chart?range=1Y')
    assert r.status_code == 200
    assert seen.get('tickers') == ['ONLY.TO'], 'suffixed-id trade never reached the chart'


# ---------------------------------------------------------------------------
# Time-weighted return — /api/holdings/chart
# ---------------------------------------------------------------------------

def _twr_fixture(monkeypatch, prices, txns):
    """Drive the chart route over a hand-built price series.

    `prices` is a list of closes for one ticker on consecutive days ending
    yesterday, so the window always sits inside the 1Y range no matter when the
    suite runs. Returns the parsed payload.
    """
    import datetime as _d
    import pandas as pd

    day0 = _d.date.today() - _d.timedelta(days=len(prices))
    days = [day0 + _d.timedelta(days=i) for i in range(len(prices))]

    def _fake_download(tickers, **kw):
        idx = pd.DatetimeIndex(days)
        cols = sorted(tickers) if not isinstance(tickers, str) else [tickers]
        return pd.concat({'Close': pd.DataFrame(
            {t: list(prices) for t in cols}, index=idx)}, axis=1)

    monkeypatch.setattr(terminal.yf, 'download', _fake_download)
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        dict(t, date=days[t['day']].isoformat(), ticker='X.TO', name='X',
             id=str(1780000000000 + i))
        for i, t in enumerate(txns)
    ])
    return terminal.app.test_client().get('/api/holdings/chart?range=1Y').get_json()


def test_twr_is_unmoved_by_contribution_timing(monkeypatch, bare_portfolio):
    """The defining property of a time-weighted return, and the one the old
    code did not have: it measures what the holdings did, not what the investor
    did. Two accounts in the same security over the same window must report the
    same return however differently they funded it.

    This was Modified Dietz — a *money-weighted* return — reported and labelled
    as TWR. Dietz is what you use when you lack periodic valuations and have to
    assume flows arrive at an average moment; this route builds a daily
    valuation series, so there was nothing to approximate.

    Prices move only on days with no contribution, so the two conventions for an
    intraday flow agree and the expected answer is exactly the price return:
    +20% then -25%, so -10%. On the staggered ledger the old formula returned
    -51.24%, five times the loss, on a book that held one security throughout.
    """
    prices = [100.0, 100.0, 120.0, 120.0, 90.0]

    steady = _twr_fixture(monkeypatch, prices, [
        {'day': 0, 'type': 'buy', 'shares': 10, 'price': 100.0},
    ])
    staggered = _twr_fixture(monkeypatch, prices, [
        {'day': 0, 'type': 'buy', 'shares': 1,   'price': 100.0},
        {'day': 1, 'type': 'buy', 'shares': 50,  'price': 100.0},
        {'day': 3, 'type': 'buy', 'shares': 100, 'price': 120.0},
    ])

    assert steady['twr']    == pytest.approx(-0.10, abs=1e-6)
    assert staggered['twr'] == pytest.approx(-0.10, abs=1e-6), (
        'contribution schedule moved the time-weighted return — this is Dietz, '
        'not TWR')


def test_twr_counts_a_contribution_as_capital_not_as_gain(monkeypatch,
                                                          bare_portfolio):
    """New money is already inside the day's closing value, so it has to be in
    the denominator too. Leave it out and the deposit reads as a profit: here a
    flat price with a contribution that quadruples the account would print
    +300%."""
    payload = _twr_fixture(monkeypatch, [100.0, 100.0, 100.0], [
        {'day': 0, 'type': 'buy', 'shares': 10, 'price': 100.0},
        {'day': 1, 'type': 'buy', 'shares': 30, 'price': 100.0},
    ])
    assert payload['values'][-1] == pytest.approx(4000.0)
    assert payload['twr'] == pytest.approx(0.0, abs=1e-9)


def test_twr_under_a_year_is_not_annualized(monkeypatch, bare_portfolio):
    """GIPS 5.A.4. Raising a 29-day return to the 12.6th power printed +96.22%
    on the live book's 1M view — an extrapolation rendered as a measurement, and
    the largest number on the page. The frontend falls back to the period return
    when this is null, so the gate lives here rather than in the template."""
    payload = _twr_fixture(monkeypatch, [100.0, 110.0], [
        {'day': 0, 'type': 'buy', 'shares': 10, 'price': 100.0},
    ])
    assert payload['twr'] == pytest.approx(0.10, abs=1e-6)
    assert payload['annualized_twr'] is None


def test_twr_over_a_year_is_annualized(monkeypatch, bare_portfolio):
    """The other side of that gate: a multi-year window is scaled *down* to a
    per-annum figure, which is a restatement rather than a forecast."""
    prices  = [100.0] + [200.0] * 730          # 731 days, D = 730
    payload = _twr_fixture(monkeypatch, prices, [
        {'day': 0, 'type': 'buy', 'shares': 10, 'price': 100.0},
    ])
    assert payload['twr'] == pytest.approx(1.0, abs=1e-6)
    # 2x over two years is not 100%/yr.
    assert payload['annualized_twr'] == pytest.approx(2.0 ** (365 / 730) - 1,
                                                      abs=1e-6)


def test_invested_credits_dividend_paid_on_its_own_ex_date(monkeypatch, bare_portfolio):
    """yfinance gives one date per distribution, so _build_div_events falls back
    to pay_date == ex_date. The pay event sorted at priority 0 and the snapshot
    it reads at priority 2, so on a shared date the credit ran first and read a
    snapshot that did not exist yet — defaulting to 0 shares and dropping the
    dividend outright. Three closed positions lost every distribution that way."""
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        {'id': '1', 'type': 'buy', 'ticker': 'D.TO', 'name': 'D',
         'shares': 100, 'price': 10, 'date': '2026-01-05'},
    ])
    # ex == pay, well after the buy
    monkeypatch.setattr(terminal, '_build_div_events',
                        lambda t: [(__import__('datetime').date(2026, 3, 10),
                                    __import__('datetime').date(2026, 3, 10), 0.50)])

    d = terminal.app.test_client().get('/api/portfolio/invested').get_json()
    assert d['cash_pool'] == 50.0, 'dividend with pay==ex was dropped'


def test_invested_still_lets_a_dividend_fund_a_later_same_day_buy(monkeypatch, bare_portfolio):
    """The priority-0 ordering exists so a dividend paid on the morning of a buy
    funds it. That must survive the pay==ex fix, which only reorders the
    degenerate case."""
    import datetime as _d
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        {'id': '1', 'type': 'buy', 'ticker': 'D.TO', 'name': 'D',
         'shares': 100, 'price': 10, 'date': '2026-01-05'},
        {'id': '2', 'type': 'buy', 'ticker': 'D.TO', 'name': 'D',
         'shares': 5,   'price': 10, 'date': '2026-03-20'},
    ])
    # ex-date in March, pay-date lands on the second buy
    monkeypatch.setattr(terminal, '_build_div_events',
                        lambda t: [(_d.date(2026, 3, 10), _d.date(2026, 3, 20), 0.50)])

    d = terminal.app.test_client().get('/api/portfolio/invested').get_json()
    # $1000 external for the first buy; the $50 dividend covers the $50 second buy
    assert d['invested']  == 1000.0
    assert d['cash_pool'] == 0.0


# ---------------------------------------------------------------------------
# Performance tab aligned with Holdings tab
# ---------------------------------------------------------------------------

def test_performance_reports_dividends_per_position(monkeypatch, bare_portfolio):
    """The Performance tab counted price movement only, so a position held for
    income read as a loser whenever the payouts more than covered a price drift.
    Holdings has always counted them."""
    import datetime as _d
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        {'id': '1', 'type': 'buy', 'ticker': 'D.TO', 'name': 'D',
         'shares': 100, 'price': 10, 'date': '2026-01-05'},
    ])
    monkeypatch.setattr(terminal, 'load_holdings', lambda: [
        {'ticker': 'D.TO', 'name': 'D', 'shares': 100, 'avg_price': 10},
    ])
    monkeypatch.setattr(terminal, '_build_div_events',
                        lambda t: [(_d.date(2026, 2, 10), _d.date(2026, 2, 20), 0.25)])

    d = terminal.app.test_client().get('/api/portfolio/performance').get_json()
    assert d['positions'][0]['dividends'] == 25.0


def test_performance_ships_the_holdings_denominator(monkeypatch, bare_portfolio):
    """Both summaries divide by external capital. They used to each compute
    "how much did I put in" their own way and disagree by 10 points."""
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        {'id': '1', 'type': 'buy',  'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 10, 'date': '2026-01-05'},
        {'id': '2', 'type': 'sell', 'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 12, 'date': '2026-02-05'},
        {'id': '3', 'type': 'buy',  'ticker': 'B.TO', 'name': 'B',
         'shares': 100, 'price': 12, 'date': '2026-03-05'},
    ])
    monkeypatch.setattr(terminal, 'load_holdings', lambda: [
        {'ticker': 'B.TO', 'name': 'B', 'shares': 100, 'avg_price': 12},
    ])
    client = terminal.app.test_client()
    perf = client.get('/api/portfolio/performance').get_json()
    inv  = client.get('/api/portfolio/invested').get_json()

    assert perf['invested'] == inv['invested'] == 1000.0
    # Gross cost basis would be $2,200 — the recycled $1,000 booked twice.
    gross = sum(p['cost_basis_sold'] + p['held_cost'] for p in perf['positions'])
    assert gross == 2200.0


def test_performance_and_holdings_summaries_reconcile(monkeypatch, bare_portfolio):
    """The identity the two tabs now share, at a fixed price so it is checkable:

        realized + unrealized + dividends  ==  (cash + market value) - invested

    It holds whenever the un-redeployed cash pool matches the recorded cash
    balance, which is the case the Holdings tab displays."""
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        {'id': '1', 'type': 'buy',  'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 10, 'date': '2026-01-05'},
        {'id': '2', 'type': 'sell', 'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 12, 'date': '2026-02-05'},
        {'id': '3', 'type': 'buy',  'ticker': 'B.TO', 'name': 'B',
         'shares': 100, 'price': 12, 'date': '2026-03-05'},
    ])
    monkeypatch.setattr(terminal, 'load_holdings', lambda: [
        {'ticker': 'B.TO', 'name': 'B', 'shares': 100, 'avg_price': 12},
    ])
    perf = terminal.app.test_client().get('/api/portfolio/performance').get_json()

    market_price = 15.0
    positions = {p['ticker']: p for p in perf['positions']}
    unrealized = positions['B.TO']['held_shares'] * market_price - positions['B.TO']['held_cost']
    ledger_pl  = sum(p['realized_pl'] + p['dividends'] for p in perf['positions']) + unrealized

    market_value  = 100 * market_price
    holdings_pl   = (perf['cash_pool'] + market_value) - perf['invested']

    # realized $200 (A: 100 x $2) + unrealized $300 (B: 100 x $3), no dividends.
    # $1,000 of external capital funded both legs; the sale of A funded the buy
    # of B in full. Gross cost basis would say $2,200 in and 22.7%.
    assert round(ledger_pl, 2) == round(holdings_pl, 2) == 500.0
    assert round(100 * ledger_pl / perf['invested'], 2) == 50.0


# ---------------------------------------------------------------------------
# Valuations — a buy threshold and a sell threshold, either one optional
# ---------------------------------------------------------------------------

@pytest.fixture
def valuations_client(tmp_path, monkeypatch):
    """Give this test its own per-user root so it never touches the real one."""
    monkeypatch.setattr(terminal, '_USER_DATA_ROOT', str(tmp_path / 'users'))
    monkeypatch.setattr(terminal, '_user_stores', {})
    return terminal.app.test_client()


def _valuations_path():
    return terminal._user_store(terminal.VALUATIONS_FILE, owner=TEST_USER).path


def _seed(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(rows, f)


def test_old_schema_row_migrates_intrinsic_onto_buy(valuations_client):
    """The pre-split rows held one number. It was the price the user was
    willing to pay, so it becomes buy_price — and sell_price stays unset
    rather than being derived, which would fire a SELL nobody asked for."""
    _seed(_valuations_path(),
          [{'ticker': 'MFI.TO', 'name': 'Maple Leaf Foods Inc.',
            'intrinsic_value': 38.0, 'notes': '', 'added': '2026-06-11'}])

    row = valuations_client.get('/api/valuations').get_json()[0]
    assert row['buy_price']  == 38.0
    assert row['sell_price'] is None
    assert 'intrinsic_value' not in row
    assert row['added'] == '2026-06-11'    # migration must not restamp the date


def test_migration_leaves_a_new_schema_row_alone(valuations_client):
    _seed(_valuations_path(),
          [{'ticker': 'AAPL', 'name': 'Apple', 'buy_price': 150.0,
            'sell_price': 260.0, 'notes': '', 'added': '2026-01-01'}])

    row = valuations_client.get('/api/valuations').get_json()[0]
    assert (row['buy_price'], row['sell_price']) == (150.0, 260.0)


def test_post_creates_a_row_with_both_thresholds(valuations_client):
    r = valuations_client.post('/api/valuations', json={
        'ticker': 'msft', 'name': 'Microsoft', 'buy_price': 400,
        'sell_price': 520, 'notes': 'DCF at 10%'})
    assert r.status_code == 200
    row = r.get_json()[0]
    assert row['ticker'] == 'MSFT'          # normalised through clean_ticker
    assert (row['buy_price'], row['sell_price']) == (400.0, 520.0)


def test_editing_one_threshold_leaves_the_other_intact(valuations_client):
    """The table edits one input at a time, so a body carrying only buy_price
    must not be read as 'clear the sell price'."""
    valuations_client.post('/api/valuations', json={
        'ticker': 'AAPL', 'buy_price': 150, 'sell_price': 260})

    row = valuations_client.post('/api/valuations', json={
        'ticker': 'AAPL', 'buy_price': 160}).get_json()[0]
    assert (row['buy_price'], row['sell_price']) == (160.0, 260.0)


def test_explicit_null_clears_one_threshold(valuations_client):
    valuations_client.post('/api/valuations', json={
        'ticker': 'AAPL', 'buy_price': 150, 'sell_price': 260})

    row = valuations_client.post('/api/valuations', json={
        'ticker': 'AAPL', 'sell_price': None}).get_json()[0]
    assert row['buy_price']  == 150.0
    assert row['sell_price'] is None


def test_sell_at_or_below_buy_is_rejected(valuations_client):
    """Overlapping bands would make one quote both a BUY and a SELL."""
    for sell in (150, 149.99):
        r = valuations_client.post('/api/valuations', json={
            'ticker': 'AAPL', 'buy_price': 150, 'sell_price': sell})
        assert r.status_code == 400
        assert 'above' in r.get_json()['error']
    assert valuations_client.get('/api/valuations').get_json() == []


def test_a_row_with_neither_threshold_is_rejected(valuations_client):
    r = valuations_client.post('/api/valuations', json={'ticker': 'AAPL'})
    assert r.status_code == 400
    assert valuations_client.get('/api/valuations').get_json() == []


@pytest.mark.parametrize('bad', [0, -5, 'abc'])
def test_non_positive_or_unparseable_price_is_rejected(valuations_client, bad):
    r = valuations_client.post('/api/valuations',
                               json={'ticker': 'AAPL', 'buy_price': bad})
    assert r.status_code == 400


def test_delete_validates_the_ticker(valuations_client):
    assert valuations_client.delete('/api/valuations/AAPL %26 calc').status_code == 400


def test_delete_removes_a_dotted_ticker(valuations_client):
    valuations_client.post('/api/valuations',
                           json={'ticker': 'MFI.TO', 'buy_price': 38})
    assert valuations_client.delete('/api/valuations/MFI.TO').get_json() == []


# ---------------------------------------------------------------------------
# Cash is derived, never stored
# ---------------------------------------------------------------------------

def test_cash_is_the_derived_pool(monkeypatch, bare_portfolio):
    """cash.json was a running balance maintained forward by six write sites and
    reversed by none, so a deleted or edited trade left it describing money that
    trade no longer explained — the `sales.json` mistake in another file."""
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        {'id': '1', 'type': 'buy',  'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 10, 'date': '2026-01-05'},
        {'id': '2', 'type': 'sell', 'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 12, 'date': '2026-02-05'},
    ])
    d = terminal.app.test_client().get('/api/cash').get_json()
    assert d['balance'] == 1200.0


def test_deleting_a_transaction_moves_cash(monkeypatch, bare_portfolio, tmp_path):
    """The point of deriving it: cash follows a correction with no reversal code.
    The old store had no path back from a DELETE at all."""
    txns = [
        {'id': '1', 'type': 'buy',  'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 10, 'date': '2026-01-05'},
        {'id': '2', 'type': 'sell', 'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 12, 'date': '2026-02-05'},
    ]
    state = {'sales': [], 'holdings': []}
    monkeypatch.setattr(terminal, 'load_transactions', lambda: list(txns))
    monkeypatch.setattr(terminal, 'save_transactions', lambda v: (txns.clear(), txns.extend(v)))
    monkeypatch.setattr(terminal, 'load_sales',    lambda: list(state['sales']))
    monkeypatch.setattr(terminal, 'save_sales',    lambda v: state.__setitem__('sales', v))
    monkeypatch.setattr(terminal, 'load_holdings', lambda: list(state['holdings']))
    monkeypatch.setattr(terminal, 'save_holdings', lambda v: state.__setitem__('holdings', v))

    client = terminal.app.test_client()
    assert client.get('/api/cash').get_json()['balance'] == 1200.0

    client.delete('/api/transactions/2')          # undo the sale
    assert client.get('/api/cash').get_json()['balance'] == 0.0


def test_option_legs_move_the_derived_pool(monkeypatch):
    """Options draw from and return to the same pool as share trades. Before cash
    was derived they only moved cash.json, so omitting them from the walk would
    have written the premium out of the portfolio."""
    monkeypatch.setattr(terminal, '_build_div_events', lambda t: [])
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        {'id': '1', 'type': 'buy',  'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 10, 'date': '2026-01-05'},
        {'id': '2', 'type': 'sell', 'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 12, 'date': '2026-02-05'},
    ])
    monkeypatch.setattr(terminal, 'load_options', lambda: [
        {'id': 'o1', 'status': 'closed', 'contracts': 1,
         'buy_price': 1.00, 'buy_date':  '2026-03-01',
         'sell_price': 1.50, 'sell_date': '2026-03-10'},
    ])
    # $1,200 pool, minus $100 premium, plus $150 proceeds
    assert terminal.app.test_client().get('/api/cash').get_json()['balance'] == 1250.0

    # An open contract is capital that has left the pool and not come back
    monkeypatch.setattr(terminal, 'load_options', lambda: [
        {'id': 'o1', 'status': 'open', 'contracts': 1,
         'buy_price': 1.00, 'buy_date': '2026-03-01',
         'sell_price': None, 'sell_date': None},
    ])
    assert terminal.app.test_client().get('/api/cash').get_json()['balance'] == 1100.0


def test_cash_cannot_be_set(monkeypatch, bare_portfolio):
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [])
    r = terminal.app.test_client().post('/api/cash', json={'amount': 500, 'type': 'deposit'})
    assert r.status_code == 410


# ---------------------------------------------------------------------------
# /api/crosslist — the "other exchanges" switcher under the company name
# ---------------------------------------------------------------------------

def _hit(symbol, exchange, longname, shortname=None, quote_type='EQUITY'):
    """One row shaped the way Yahoo's search endpoint returns it."""
    return {'symbol': symbol, 'exchange': exchange, 'exchDisp': exchange,
            'longname': longname, 'shortname': shortname or longname,
            'quoteType': quote_type}


def _syms(payload):
    return [l['ticker'] for l in payload]


@pytest.fixture
def crosslist(monkeypatch):
    """Run the route against a canned info dict and a canned search result."""
    def run(ticker, info, quotes):
        monkeypatch.setattr(terminal.yf, 'Ticker',
                            lambda t: type('T', (), {'info': info})())
        monkeypatch.setattr(
            'requests.get',
            lambda *a, **k: type('R', (), {'json': lambda self: {'quotes': quotes}})())
        return terminal.app.test_client().get(
            f'/api/crosslist?ticker={ticker}').get_json()
    return run


def test_preferred_series_are_not_other_exchanges(crosslist):
    """Yahoo gives a preferred series the same quoteType *and the same
    longname* as the common share, so the old name check waved all five
    through. BCE rendered six buttons, five of them labelled TSX, each one
    loading a preferred instead of the company."""
    out = crosslist('BCE.TO',
                    {'longName': 'BCE Inc.', 'exchange': 'TOR',
                     'fullExchangeName': 'Toronto'},
                    [_hit('BCE.TO',    'TOR', 'BCE Inc.', 'BCE INC.'),
                     _hit('BCE',       'NYQ', 'BCE Inc.', 'BCE, Inc.'),
                     _hit('BCE-PY.TO', 'TOR', 'BCE Inc.', 'BCE INC SER Y PR'),
                     _hit('BCE-PZ.TO', 'TOR', 'BCE Inc.', 'BCE INC SERIES Z'),
                     _hit('BCE-PQ.TO', 'TOR', 'BCE Inc.', 'BCE INC PREFERRED SHARES SERIES')])
    assert _syms(out) == ['BCE.TO', 'BCE']


def test_a_preferred_alone_on_an_exchange_is_still_rejected(crosslist):
    """The different-exchange rule cannot see this one — BEP-PA trades on NYSE
    while the common trades in Toronto. The class suffix plus a shortname
    marker is what catches it."""
    out = crosslist('BEP-UN.TO',
                    {'longName': 'Brookfield Renewable Partners L.P.', 'exchange': 'TOR'},
                    [_hit('BEP-UN.TO', 'TOR', 'Brookfield Renewable Partners L.P.'),
                     _hit('BEP-PM.TO', 'TOR', 'Brookfield Renewable Partners L.P.',
                          'BROOKFIELD RENEWABLE LP PREF SE'),
                     _hit('BEP-PA',    'NYQ', 'Brookfield Renewable Partners L.P.',
                          'Brookfield Renewable Pfd Series A')])
    assert _syms(out) == ['BEP-UN.TO']


def test_a_shortname_marker_alone_does_not_drop_a_common_listing(crosslist):
    """Yahoo files BNS — the *common* NYSE listing — under the shortname
    'Bank Nova Scotia Halifax Pfd 3'. Rejecting on a shortname marker by itself
    cost the bank its NYSE button, so it only counts against a symbol that
    carries a class suffix too."""
    out = crosslist('BNS.TO',
                    {'longName': 'The Bank of Nova Scotia', 'exchange': 'TOR'},
                    [_hit('BNS.TO', 'TOR', 'The Bank of Nova Scotia', 'BANK OF NOVA SCOTIA'),
                     _hit('BNS',    'NYQ', 'The Bank of Nova Scotia',
                          'Bank Nova Scotia Halifax Pfd 3')])
    assert _syms(out) == ['BNS.TO', 'BNS']


def test_a_different_bank_is_not_a_cross_listing(crosslist):
    """The old check kept anything containing one of the first two words over
    three characters long, so every issuer with 'Bank' in its name counted as
    another listing of Royal Bank of Canada."""
    out = crosslist('RY.TO',
                    {'longName': 'Royal Bank of Canada', 'exchange': 'TOR'},
                    [_hit('RY.TO',   'TOR', 'Royal Bank of Canada'),
                     _hit('RY',      'NYQ', 'Royal Bank of Canada'),
                     _hit('BMO.MX',  'MEX', 'Bank of Montreal'),
                     _hit('RYCEY',   'PNK', 'Rolls-Royce Holdings plc'),
                     _hit('AAKONXX', 'NAS', 'Royal Bank of Canada Point to Point',
                          quote_type='MUTUALFUND')])
    assert _syms(out) == ['RY.TO', 'RY']


def test_a_short_company_name_does_not_wipe_out_every_listing(crosslist):
    """`any(... for w in name.split()[:2] if len(w) > 3)` is False over an
    empty generator, so an issuer whose first two words are all three
    characters or shorter dropped every one of its listings and the switcher
    vanished."""
    out = crosslist('AZZ',
                    {'longName': 'AZZ Inc', 'exchange': 'NYQ'},
                    [_hit('AZZ',   'NYQ', 'AZZ Inc'),
                     _hit('AZZ.F', 'FRA', 'AZZ Inc')])
    assert _syms(out) == ['AZZ', 'AZZ.F']


def test_a_depositary_receipt_is_the_same_issuer(crosslist):
    """Buenos Aires wraps the issuer's name in receipt boilerplate, so an exact
    name match would drop a real listing — but 'Apple' leading a different
    company's name is not one."""
    out = crosslist('AAPL',
                    {'longName': 'Apple Inc.', 'exchange': 'NMS'},
                    [_hit('AAPL',     'NMS', 'Apple Inc.'),
                     _hit('AAPLC.BA', 'BUE', 'APPLE INC CEDEAR(REPR 1/20 SHR)'),
                     _hit('APLE',     'NYQ', 'Apple Hospitality REIT, Inc.')])
    assert _syms(out) == ['AAPL', 'AAPLC.BA']


def test_one_listing_per_exchange(crosslist):
    """A second hit on an exchange already taken is a different security, not a
    second listing of this one."""
    out = crosslist('X.TO',
                    {'longName': 'Example Mining', 'exchange': 'TOR'},
                    [_hit('X.TO',  'TOR', 'Example Mining'),
                     _hit('XMPL',  'NYQ', 'Example Mining'),
                     _hit('XMPLB', 'NYQ', 'Example Mining')])
    assert _syms(out) == ['X.TO', 'XMPL']


def test_current_listing_carries_its_exchange_code(crosslist):
    """yfinance's info has no exchDisp key at all, so the old label silently
    fell back to the raw code on every ticker. The frontend labels from
    exchange_code, so it has to be there."""
    out = crosslist('AAPL',
                    {'longName': 'Apple Inc.', 'exchange': 'NMS',
                     'fullExchangeName': 'NasdaqGS'},
                    [_hit('AAPL', 'NMS', 'Apple Inc.')])
    assert out[0] == {'ticker': 'AAPL', 'exchange': 'NasdaqGS',
                      'exchange_code': 'NMS', 'current': True}


def test_crosslist_validates_the_ticker():
    """The route reaches an outbound request; it takes clean_ticker like every
    other boundary rather than a bare strip/upper."""
    assert terminal.app.test_client().get(
        '/api/crosslist?ticker=AAPL %26 calc').get_json() == []


def test_performance_summary_counts_option_pl(monkeypatch):
    """`invested` is net of the option legs now that cash is derived from them,
    so the numerator has to be too — otherwise the Performance summary drifts
    from Holdings by the size of the option book."""
    monkeypatch.setattr(terminal, '_build_div_events', lambda t: [])
    monkeypatch.setattr(terminal, 'load_transactions', lambda: [
        {'id': '1', 'type': 'buy', 'ticker': 'A.TO', 'name': 'A',
         'shares': 100, 'price': 10, 'date': '2026-01-05'},
    ])
    monkeypatch.setattr(terminal, 'load_holdings', lambda: [
        {'ticker': 'A.TO', 'name': 'A', 'shares': 100, 'avg_price': 10},
    ])
    monkeypatch.setattr(terminal, 'load_options', lambda: [
        {'id': 'o1', 'status': 'closed', 'contracts': 1, 'gain_loss': 20.8,
         'buy_price': 1.00, 'buy_date': '2026-03-01',
         'sell_price': 1.208, 'sell_date': '2026-03-10'},
        {'id': 'o2', 'status': 'open', 'contracts': 1, 'gain_loss': None,
         'buy_price': 2.00, 'buy_date': '2026-04-01',
         'sell_price': None, 'sell_date': None},
    ])
    d = terminal.app.test_client().get('/api/portfolio/performance').get_json()
    assert d['options_pl'] == 20.8    # closed only; the open contract is not marked


# ---------------------------------------------------------------------------
# Groq key resolution
#
# The client was built once at import time from os.environ alone, but the key is
# stored in settings.json. On a machine with no GROQ_API_KEY exported — which is
# this one — every call raised on an empty key, groq_call() swallowed it and
# returned '', and the /api/news headline rewrite fell back to sentence-casing
# the summary. Nothing reported it. These tests pin both halves: the key reaches
# the client, and a dead LLM path is distinguishable in the log.
# ---------------------------------------------------------------------------

@pytest.fixture
def groq(tmp_path, monkeypatch):
    """Recording Groq client plus a scratch per-user settings directory.

    Keys are per account now, so this redirects _USER_DATA_ROOT rather than
    standing in for one shared store — the real settings.json holds live API
    keys and no test in this section is allowed near it.

    The client cache is process-global, so every field of it is restored after
    each test; `built` is the list of clients constructed, newest last.
    """
    built = []

    class FakeCompletions:
        def __init__(self, client):
            self._client = client

        def create(self, **kwargs):
            self._client.calls.append(kwargs)
            if self._client.error:
                raise self._client.error
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='"Rewritten Headline"'))])

    class FakeGroq:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.calls   = []
            self.error   = None
            self.chat    = SimpleNamespace(completions=FakeCompletions(self))
            built.append(self)

    monkeypatch.setattr(terminal, '_USER_DATA_ROOT', str(tmp_path / 'users'))
    monkeypatch.setattr(terminal, '_user_stores', {})
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    monkeypatch.setattr(terminal, 'Groq', FakeGroq)
    monkeypatch.setattr(terminal, '_groq', None)
    monkeypatch.setattr(terminal, '_groq_key', None)
    monkeypatch.setattr(terminal, '_groq_warned', False)

    def set_key(value, owner=TEST_USER):
        terminal._save_settings({'GROQ_API_KEY': value} if value else {},
                                owner=owner)

    def call(owner=TEST_USER):
        """One groq_call as `owner` would make it, key resolved from settings."""
        return terminal.groq_call(
            'system', 'user', key=terminal._resolve_api_key('GROQ_API_KEY', owner))

    return SimpleNamespace(clients=built, set_key=set_key, call=call)


def test_key_stored_only_in_settings_reaches_the_client(groq):
    """The original defect. The client was built from os.environ at import time,
    so a key configured through the UI never got anywhere near it."""
    groq.set_key('gsk_only_in_settings')

    assert groq.call() == 'Rewritten Headline'
    assert [c.api_key for c in groq.clients] == ['gsk_only_in_settings']
    assert groq.clients[0].calls[0]['model'] == 'llama-3.1-8b-instant'


def test_the_environment_is_not_a_fallback(groq, monkeypatch):
    """Keys are per account, so there is no shared source — including the shell.

    An exported GROQ_API_KEY would be one key serving every account, which is
    exactly what per-user keys are for. An account with none configured gets the
    degraded path rather than quietly spending someone else's key.
    """
    monkeypatch.setenv('GROQ_API_KEY', 'gsk_exported_in_the_shell')

    assert groq.call() == ''
    assert groq.clients == []
    assert terminal._resolve_api_key('GROQ_API_KEY', TEST_USER) == ''


def test_each_account_resolves_its_own_key(groq):
    groq.set_key('gsk_belongs_to_test_user', owner=TEST_USER)
    groq.set_key('gsk_belongs_to_someone_else', owner='otheruser')

    assert terminal._resolve_api_key('GROQ_API_KEY', TEST_USER) == 'gsk_belongs_to_test_user'
    assert terminal._resolve_api_key('GROQ_API_KEY', 'otheruser') == 'gsk_belongs_to_someone_else'
    assert terminal._resolve_api_key('GROQ_API_KEY', 'nobody') == ''


def test_saving_a_key_through_the_route_reaches_the_next_call(groq):
    """POST /api/settings must take effect without a restart — and without the
    route having to know the client cache exists."""
    assert groq.call() == ''        # nothing configured yet
    assert groq.clients == []       # so no client is built

    r = terminal.app.test_client().post(
        '/api/settings', json={'GROQ_API_KEY': 'gsk_saved_via_ui'})
    assert r.status_code == 200

    assert groq.call() == 'Rewritten Headline'
    assert [c.api_key for c in groq.clients] == ['gsk_saved_via_ui']


def test_the_settings_route_writes_only_the_callers_own_keys(groq):
    """The account comes from the session; no payload can redirect the write."""
    groq.set_key('gsk_theirs', owner='otheruser')
    terminal.app.test_client().post('/api/settings', json={
        'GROQ_API_KEY': 'gsk_mine', 'username': 'otheruser', 'owner': 'otheruser'})

    assert terminal._resolve_api_key('GROQ_API_KEY', TEST_USER) == 'gsk_mine'
    assert terminal._resolve_api_key('GROQ_API_KEY', 'otheruser') == 'gsk_theirs'


def test_the_settings_route_masks_and_never_returns_the_key(groq):
    groq.set_key('gsk_1234567890abcd')
    body = terminal.app.test_client().get('/api/settings').get_json()
    assert body['GROQ_API_KEY'].endswith('abcd')
    assert 'gsk_1234567890abcd' not in json.dumps(body)


def test_a_blank_field_leaves_a_stored_key_alone(groq):
    """The Settings page posts all four fields on one Save with usually one of
    them filled in, so '' has to mean "untouched" — otherwise saving a DeepSeek
    key wipes the three keys the user did not retype."""
    groq.set_key('gsk_keep_me')

    r = terminal.app.test_client().post('/api/settings', json={
        'GROQ_API_KEY': '', 'DEEPSEEK_API_KEY': 'sk_new', 'FRED_API_KEY': '   '})
    assert r.status_code == 200

    assert terminal._resolve_api_key('GROQ_API_KEY', TEST_USER) == 'gsk_keep_me'
    assert terminal._resolve_api_key('DEEPSEEK_API_KEY', TEST_USER) == 'sk_new'


def test_null_clears_a_key(groq):
    """Because '' means "untouched", clearing needs its own signal — and it has
    to be one no untouched form field can produce. That is what the Remove
    control on each key card sends; without it a key could be set and never
    unset."""
    groq.set_key('gsk_remove_me')

    r = terminal.app.test_client().post('/api/settings', json={'GROQ_API_KEY': None})
    assert r.status_code == 200

    assert terminal._resolve_api_key('GROQ_API_KEY', TEST_USER) == ''
    assert terminal.app.test_client().get('/api/settings').get_json()['GROQ_API_KEY'] == ''
    # Removing one key is not removing the account's settings file.
    assert 'GROQ_API_KEY' not in terminal._load_settings(TEST_USER)


def test_client_is_rebuilt_only_when_the_key_changes(groq):
    """Resolution is per call, but a rebuild is not: the cache is keyed on the
    key itself, which is what lets the route stay uncoupled from it."""
    groq.set_key('gsk_one')
    groq.call()
    groq.call()
    assert len(groq.clients) == 1

    groq.set_key('gsk_two')
    groq.call()
    assert [c.api_key for c in groq.clients] == ['gsk_one', 'gsk_two']


def test_an_unusable_settings_file_yields_no_key_rather_than_raising(groq):
    """_load_settings() returns whatever parsed, so .get can fail on a file that
    is valid JSON but not an object. That must degrade, not 500."""
    terminal._user_store(terminal.SETTINGS_FILE, owner=TEST_USER).save(['not', 'a', 'dict'])

    assert terminal._resolve_api_key('GROQ_API_KEY', TEST_USER) == ''
    assert groq.call() == ''


def test_no_key_and_failed_call_are_logged_apart(groq, capsys):
    """Both degrade to '', which is how a dead LLM path hid for so long. The log
    line is the only thing that separates "never configured" from "broken"."""
    assert groq.call() == ''
    assert 'no API key' in capsys.readouterr().out

    groq.set_key('gsk_live')
    groq.call()
    capsys.readouterr()

    groq.clients[0].error = RuntimeError('429 rate limited')
    assert groq.call() == ''
    out = capsys.readouterr().out
    assert 'call failed' in out and '429 rate limited' in out
    assert 'no API key' not in out


def test_the_no_key_warning_does_not_repeat_per_call(groq, capsys):
    """_build_news submits one _to_headline per article, so a per-call warning
    would print ten identical lines per ticker and train the reader to skip it."""
    for _ in range(10):
        groq.call()

    assert capsys.readouterr().out.count('no API key') == 1
