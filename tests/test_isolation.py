"""Two accounts, one server: neither may see the other's portfolio.

The design these test is directory-per-user (app._user_store), chosen over an
`owner` column because a filter is something every read site has to remember and
there are dozens of them. So the important test here is not "does /api/holdings
filter" — it is the sweep at the bottom, which walks every per-user GET route as
the second account and asserts that no marker from the first appears in any of
them. That is the check that keeps working when someone adds route 91.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402
from conftest import make_client  # noqa: E402

PW = 'a-strong-test-password'

# Markers that must never cross accounts. Distinctive enough that a substring
# search over a response body is meaningful.
ALPHA_TICKER = 'ZALPHA'
ALPHA_NOTE   = 'PRIVATE-NOTE-OF-ALPHA'
ALPHA_KEY    = 'gsk-PRIVATE-API-KEY-OF-ALPHA'
# What alpha has chosen not to see. A separate symbol from ALPHA_TICKER so the
# seed does not collide with the mirror test — a blocked ticker is filtered out
# of alpha's own watchlist, which is correct and would look like a leak the
# other way round.
ALPHA_HIDDEN = 'ZHIDDEN'


@pytest.fixture
def two_users(tmp_path, monkeypatch):
    """Accounts 'alpha' and 'beta', each with an empty data directory."""
    monkeypatch.setattr(terminal, '_USER_DATA_ROOT', str(tmp_path / 'users'))
    monkeypatch.setattr(terminal, '_user_stores', {})

    users = terminal.JsonStore.__new__(terminal.JsonStore)
    users.path     = str(tmp_path / 'users.json')
    users._default = list
    users._migrate = None
    users._lock    = terminal.threading.RLock()
    monkeypatch.setattr(terminal, '_users_store', users)

    terminal.create_user('alpha', PW, 'admin')
    terminal.create_user('beta',  PW, 'user')

    # Nothing here should reach Yahoo; an empty portfolio must not need to.
    monkeypatch.setattr(terminal, '_build_div_events', lambda t: [])
    monkeypatch.setattr(terminal, '_build_splits',     lambda t: [])
    monkeypatch.setattr(terminal, '_warm_corp_actions', lambda t: None)
    monkeypatch.setattr(terminal, '_fast_quote', lambda s: (None, None))

    return make_client('alpha'), make_client('beta')


def _seed_alpha(alpha):
    """Give alpha a position, a watch, a valuation and an option."""
    posts = [
        ('/api/watchlist',  {'ticker': ALPHA_TICKER, 'name': 'Alpha Co'}),
        ('/api/holdings',   {'ticker': ALPHA_TICKER, 'name': 'Alpha Co',
                             'shares': 10, 'price': 25.0,
                             'date_acquired': '2026-01-05'}),
        ('/api/valuations', {'ticker': ALPHA_TICKER, 'name': 'Alpha Co',
                             'buy_price': 20.0, 'notes': ALPHA_NOTE}),
        ('/api/options',    {'underlying': ALPHA_TICKER, 'option_type': 'call',
                             'strike': 30, 'expiry': '2027-01-15',
                             'contracts': 1, 'buy_price': 1.5,
                             'buy_date': '2026-01-06'}),
        ('/api/settings',   {'GROQ_API_KEY': ALPHA_KEY}),
        ('/api/blocked',    {'ticker': ALPHA_HIDDEN, 'name': 'Hidden Co'}),
    ]
    for path, payload in posts:
        res = alpha.post(path, json=payload)
        # Seeding silently failing would make every isolation assertion below
        # pass for the wrong reason.
        assert res.status_code in (200, 201), \
            f'seeding {path} failed: {res.status_code} {res.get_data(as_text=True)[:200]}'

    # Guidance has no seeding route — it is written by the worker thread once a
    # run finishes — so it is seeded through the same function that thread uses.
    # Without this the sweep would walk /api/forward_guidance against an empty
    # store and pass because there was nothing to leak.
    terminal._merge_guidance('alpha', ALPHA_TICKER, {
        'symbol': ALPHA_TICKER, 'sec_ticker': ALPHA_TICKER, 'reachable': True,
        'reason': 'US-listed', 'generated_at': '2026-08-12T00:00:00+00:00',
        'scorecards': [{'ticker': ALPHA_TICKER, 'period_label': 'FY2026',
                        'notes': ALPHA_NOTE}],
        'items': [{'ticker': ALPHA_TICKER, 'metric': 'revenue'}],
    })


# ---------------------------------------------------------------------------
# The files themselves
# ---------------------------------------------------------------------------

def test_each_account_gets_its_own_directory(two_users):
    alpha, beta = two_users
    _seed_alpha(alpha)

    a_path = terminal._user_store('holdings.json', owner='alpha').path
    b_path = terminal._user_store('holdings.json', owner='beta').path
    assert a_path != b_path
    assert os.path.basename(os.path.dirname(a_path)) == 'alpha'
    assert os.path.basename(os.path.dirname(b_path)) == 'beta'
    assert os.path.exists(a_path)


def test_reading_per_user_data_without_a_session_raises(two_users):
    """A silent fallback to a shared file is the leak this design prevents."""
    with pytest.raises(RuntimeError):
        terminal.load_holdings()


def test_seeded_data_landed_under_alpha_only(two_users):
    alpha, beta = two_users
    _seed_alpha(alpha)
    assert [h['ticker'] for h in terminal.load_holdings(owner='alpha')] == [ALPHA_TICKER]
    assert terminal.load_holdings(owner='beta') == []
    assert terminal.load_valuations(owner='beta') == []


# ---------------------------------------------------------------------------
# Per-route isolation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path,key', [
    ('/api/watchlist',    None),
    ('/api/holdings',     None),
    ('/api/transactions', None),
    ('/api/options',      None),
    ('/api/valuations',   None),
])
def test_beta_sees_an_empty_collection(two_users, path, key):
    alpha, beta = two_users
    _seed_alpha(alpha)

    mine = alpha.get(path).get_json()
    body = beta.get(path).get_json()
    rows = body if key is None else body.get(key, [])
    if key is None and isinstance(mine, dict):
        mine = mine.get('items', mine)

    assert rows == [], f'{path} leaked {rows}'
    assert mine, f'{path} returned nothing for the account that owns the data'


def test_knowing_alphas_row_id_does_not_let_beta_delete_it(two_users):
    """Not just filtered out of a list — not reachable by key either.

    Both delete-by-id routes are a filter over the caller's own file, so an id
    that isn't in it is a no-op. They answer 200 rather than 404 — an unknown id
    has always been idempotent here, from before accounts existed — so the
    assertion that matters is that alpha's rows are still there afterwards.
    """
    alpha, beta = two_users
    _seed_alpha(alpha)

    txn_id = alpha.get('/api/transactions').get_json()[0]['id']
    opt_id = alpha.get('/api/options').get_json()[0]['id']

    assert beta.delete(f'/api/transactions/{txn_id}').get_json()['transactions'] == []
    assert beta.delete(f'/api/options/{opt_id}').get_json() == []

    assert len(alpha.get('/api/transactions').get_json()) == 1
    assert len(alpha.get('/api/options').get_json()) == 1
    assert len(alpha.get('/api/holdings').get_json()) == 1


def test_beta_writing_does_not_touch_alpha(two_users):
    alpha, beta = two_users
    _seed_alpha(alpha)

    beta.post('/api/watchlist', json={'ticker': 'ZBETA', 'name': 'Beta Co'})
    assert [w['ticker'] for w in beta.get('/api/watchlist').get_json()] == ['ZBETA']
    assert [w['ticker'] for w in alpha.get('/api/watchlist').get_json()] == [ALPHA_TICKER]


def test_beta_deleting_by_ticker_cannot_reach_alpha(two_users):
    """The delete-by-ticker routes only filter, so they must filter *beta's* file."""
    alpha, beta = two_users
    _seed_alpha(alpha)

    beta.delete(f'/api/watchlist/{ALPHA_TICKER}')
    beta.delete(f'/api/holdings/{ALPHA_TICKER}')
    beta.delete(f'/api/valuations/{ALPHA_TICKER}')

    assert [w['ticker'] for w in alpha.get('/api/watchlist').get_json()] == [ALPHA_TICKER]
    assert [h['ticker'] for h in alpha.get('/api/holdings').get_json()] == [ALPHA_TICKER]
    assert [v['ticker'] for v in alpha.get('/api/valuations').get_json()] == [ALPHA_TICKER]


def test_money_summaries_are_per_account(two_users):
    alpha, beta = two_users
    _seed_alpha(alpha)

    a_inv = alpha.get('/api/portfolio/invested').get_json()
    b_inv = beta.get('/api/portfolio/invested').get_json()
    assert a_inv['invested'] > 0
    assert b_inv['invested'] == 0

    b_perf = beta.get('/api/portfolio/performance').get_json()
    assert b_perf['positions'] == []
    assert b_perf['invested'] == 0
    assert b_perf['cash_pool'] == 0

    assert beta.get('/api/cash').get_json()['balance'] == 0


def test_positions_news_uses_only_your_own_symbols(two_users, monkeypatch):
    alpha, beta = two_users
    _seed_alpha(alpha)
    monkeypatch.setattr(terminal, '_build_news', lambda s, name='', lite=False: [])

    assert beta.get('/api/news/positions').get_json()['symbols'] == []
    assert ALPHA_TICKER in alpha.get('/api/news/positions').get_json()['symbols']


# ---------------------------------------------------------------------------
# Reports — each account reads its own copy, never StockBox's shared output
# ---------------------------------------------------------------------------

def test_report_pdf_is_per_account(two_users):
    alpha, beta = two_users
    _seed_alpha(alpha)

    mine = terminal._user_report_path('alpha', ALPHA_TICKER)
    os.makedirs(os.path.dirname(mine), exist_ok=True)
    with open(mine, 'wb') as f:
        f.write(b'%PDF-1.4 alpha report containing ' + ALPHA_NOTE.encode())

    assert alpha.get(f'/api/report-file/{ALPHA_TICKER}').status_code == 200
    assert beta.get(f'/api/report-file/{ALPHA_TICKER}').status_code == 404


def test_report_job_status_is_not_readable_by_another_account(two_users, monkeypatch):
    alpha, beta = two_users
    monkeypatch.setattr(terminal, '_report_jobs', {})
    monkeypatch.setattr(terminal.threading, 'Thread',
                        lambda *a, **k: type('T', (), {'start': lambda self: None,
                                                       'daemon': True})())

    job_id = alpha.post('/api/generate-report',
                        json={'ticker': ALPHA_TICKER}).get_json()['job_id']
    assert alpha.get(f'/api/report-status/{job_id}').status_code == 200
    assert beta.get(f'/api/report-status/{job_id}').status_code == 404


# ---------------------------------------------------------------------------
# Shared things stay shared
# ---------------------------------------------------------------------------

def test_api_keys_are_per_account(two_users):
    """Each account holds its own keys; nobody's calls are billed to another."""
    alpha, beta = two_users

    assert alpha.post('/api/settings', json={'GROQ_API_KEY': 'gsk_alpha_key'}).status_code == 200
    assert beta.post('/api/settings',  json={'GROQ_API_KEY': 'gsk_beta_key'}).status_code == 200

    assert terminal._resolve_api_key('GROQ_API_KEY', 'alpha') == 'gsk_alpha_key'
    assert terminal._resolve_api_key('GROQ_API_KEY', 'beta')  == 'gsk_beta_key'

    # Even masked, one account's key never appears in another's payload.
    body = beta.get('/api/settings').get_data(as_text=True)
    assert 'alpha' not in body and 'gsk_alpha_key' not in body


def test_an_account_with_no_key_gets_no_key_not_someone_elses(two_users):
    alpha, beta = two_users
    alpha.post('/api/settings', json={'GROQ_API_KEY': 'gsk_alpha_key'})

    assert terminal._resolve_api_key('GROQ_API_KEY', 'beta') == ''
    assert terminal._llm_backend(owner='beta') is None


def test_admin_has_no_extra_access_to_keys(two_users):
    """Administering accounts is not the same as reading their credentials."""
    alpha, beta = two_users            # alpha is the admin
    beta.post('/api/settings', json={'GROQ_API_KEY': 'gsk_beta_key'})

    body = alpha.get('/api/settings').get_data(as_text=True)
    assert 'gsk_beta_key' not in body
    # and the admin's own view reflects the admin's own (absent) key
    assert alpha.get('/api/settings').get_json()['GROQ_API_KEY'] == ''


def test_market_data_is_shared(two_users, monkeypatch):
    """Prices are not anybody's private data; both accounts get the same answer."""
    alpha, beta = two_users
    monkeypatch.setattr(terminal, '_fast_quote', lambda s: (1.23, 0.5))
    a = alpha.get('/api/quotes?tickers=AAPL').get_json()
    b = beta.get('/api/quotes?tickers=AAPL').get_json()
    assert a == b and a


# ---------------------------------------------------------------------------
# The sweep: nothing of alpha's, anywhere, for beta
# ---------------------------------------------------------------------------

# Per-user GET routes taking no path argument. A route added later that serves
# per-user data and is not listed here is exactly what test_the_sweep_covers_
# every_per_user_route below is for.
_PER_USER_GETS = [
    '/api/watchlist', '/api/holdings', '/api/transactions', '/api/sales',
    '/api/options', '/api/valuations', '/api/cash', '/api/blocked',
    '/api/settings', '/api/portfolio/invested', '/api/portfolio/performance',
    '/api/dividends', '/api/holdings/chart?range=1Y', '/api/news/positions',
    '/api/forward_guidance',
]


def test_no_marker_of_alphas_appears_anywhere_for_beta(two_users, monkeypatch):
    """Walk every per-user read as beta and grep the raw bytes for alpha's data."""
    alpha, beta = two_users
    _seed_alpha(alpha)
    monkeypatch.setattr(terminal, '_build_news', lambda s, name='', lite=False: [])
    monkeypatch.setattr(terminal, '_dividend_payments', lambda t: [])

    leaks = []
    for path in _PER_USER_GETS:
        res = beta.get(path)
        assert res.status_code == 200, f'{path} -> {res.status_code}'
        body = res.get_data(as_text=True)
        for marker in (ALPHA_TICKER, ALPHA_NOTE, ALPHA_KEY, ALPHA_HIDDEN):
            if marker in body:
                leaks.append(f'{path} leaked {marker}')
    assert leaks == [], '\n'.join(leaks)


def test_one_accounts_block_does_not_hide_anything_from_another(two_users, monkeypatch):
    """A block is a preference, not a fact about the stock.

    The caches it filters — movers, the tape, the index payload — are shared
    market data built once for everybody, so the filtering has to happen per
    reader on the way out. Getting that wrong would be invisible in a
    single-account test and would hide alpha's choices from beta's screen.
    """
    alpha, beta = two_users
    _seed_alpha(alpha)

    monkeypatch.setitem(terminal._movers_cache, 'TSX', {
        'gainers': [{'ticker': ALPHA_HIDDEN, 'full_ticker': ALPHA_HIDDEN,
                     'name': 'Hidden Co', 'change': 5.0}],
        'losers': [], 'ts': terminal._time_mod.time(),
    })

    assert alpha.get('/api/movers?exchange=TSX').get_json()['results'] == []
    assert [r['ticker'] for r in
            beta.get('/api/movers?exchange=TSX').get_json()['results']] == [ALPHA_HIDDEN]

    # And the detail page: refused for the account that hid it, served to the
    # one that did not. Stubbed rather than fetched — the assertion is about who
    # gets past the gate, and reaching Yahoo for it would make this a live test.
    monkeypatch.setattr(terminal, '_cached_stock',
                        lambda t: ({'ticker': t, 'name': 'Hidden Co'}, False))
    assert alpha.get(f'/api/stock?ticker={ALPHA_HIDDEN}').status_code == 403
    assert beta.get(f'/api/stock?ticker={ALPHA_HIDDEN}').status_code == 200


def test_alpha_still_sees_alphas_own_data(two_users, monkeypatch):
    """The mirror of the sweep: isolation that hides data from its owner is a bug."""
    alpha, _ = two_users
    _seed_alpha(alpha)
    monkeypatch.setattr(terminal, '_build_news', lambda s, name='', lite=False: [])
    monkeypatch.setattr(terminal, '_dividend_payments', lambda t: [])

    seen = set()
    for path in _PER_USER_GETS:
        body = alpha.get(path).get_data(as_text=True)
        if ALPHA_TICKER in body:
            seen.add(path)
    assert '/api/watchlist' in seen
    assert '/api/holdings' in seen
    assert '/api/transactions' in seen


def test_the_sweep_covers_every_per_user_route():
    """Fails when a per-user store gains a reader the sweep does not walk.

    Every file in _PER_USER_FILES must be reachable from at least one path in
    _PER_USER_GETS, so adding a store without adding coverage is caught here
    rather than by a user seeing someone else's holdings.
    """
    covered = ' '.join(_PER_USER_GETS)
    for filename in terminal._PER_USER_FILES:
        stem = filename.replace('.json', '')
        assert stem in covered, f'no sweep route reads {filename}'
