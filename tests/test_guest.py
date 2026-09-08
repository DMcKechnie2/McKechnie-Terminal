"""Guest access: one shared, read-only account behind a button on the login page.

The properties worth pinning are the ones that fail silently. A guest that can
write rewrites the demo everyone else is looking at; a guest with a password is
a second door into it; a guest button that renders when nothing is enabled is a
404 with a nice label. Reuses test_auth's fixtures so every test here starts
signed out, against a temp users file.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402
from test_auth import users_file, client, admin, _login, GOOD_PW  # noqa: E402,F401


def _guest_in(client):
    """Sign the client in as guest; returns the CSRF token for the session."""
    res = client.post('/api/auth/guest')
    assert res.status_code == 200, res.get_json()
    return res.get_json()['csrf_token']


def _fresh_guest():
    terminal.enable_guest_access()
    terminal._seed_guest_portfolio(force=True)


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def test_guest_login_is_public_but_answers_404_until_enabled(client):
    assert 'api_guest_login' in terminal._PUBLIC_ENDPOINTS
    res = client.post('/api/auth/guest')
    assert res.status_code == 404
    assert client.get('/api/holdings').status_code == 401     # no session was opened


def test_enable_creates_a_passwordless_guest(users_file):
    created, _ = terminal.enable_guest_access()
    assert created
    g = terminal._guest_record()
    assert g['role'] == 'guest'
    assert g['password_hash'] is None
    assert not g['disabled']
    # Idempotent: a second enable neither duplicates nor recreates.
    created, _ = terminal.enable_guest_access()
    assert not created
    assert sum(1 for u in terminal.load_users() if u['username'] == 'guest') == 1


def test_the_button_is_rendered_only_while_enabled(client):
    assert b'id="guestBtn"' not in client.get('/login').data
    terminal.enable_guest_access()
    assert b'id="guestBtn"' in client.get('/login').data
    terminal.disable_guest_access()
    assert b'id="guestBtn"' not in client.get('/login').data


def test_a_real_account_called_guest_is_not_turned_into_the_demo(users_file):
    terminal.create_user('guest', GOOD_PW, 'user')
    with pytest.raises(ValueError):
        terminal.enable_guest_access()
    assert terminal._guest_record() is None
    assert terminal._find_user('guest')['role'] == 'user'


def test_guest_login_has_a_named_rate_limit():
    assert 'api_guest_login' in terminal._RATE_LIMITS


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------

def test_guest_signs_in_and_can_read_its_own_portfolio(client):
    _fresh_guest()
    res = client.post('/api/auth/guest')
    assert res.status_code == 200
    body = res.get_json()
    assert body['user']['role'] == 'guest'
    assert 'password_hash' not in res.data.decode()
    assert body['csrf_token']

    holdings = client.get('/api/holdings')
    assert holdings.status_code == 200
    tickers = {h['ticker'] for h in holdings.get_json()}
    assert 'RY.TO' in tickers and 'BCE.TO' not in tickers      # BCE was sold

    page = client.get('/')
    assert page.status_code == 200
    assert b'name="is-guest" content="1"' in page.data
    assert b'Guest \xc2\xb7 read-only' in page.data           # the app-bar pill


def test_the_password_form_cannot_open_the_guest(client):
    _fresh_guest()
    for pw in ('', 'guest', GOOD_PW, 'None'):
        res = client.post('/api/auth/login', json={'username': 'guest', 'password': pw})
        assert res.status_code == 401, pw
    assert client.get('/api/holdings').status_code == 401


def test_disabling_ends_live_guest_sessions(client):
    _fresh_guest()
    _guest_in(client)
    assert client.get('/api/holdings').status_code == 200
    assert terminal.disable_guest_access()
    assert client.get('/api/holdings').status_code == 401
    assert client.post('/api/auth/guest').status_code == 404


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------

def test_a_guest_write_is_refused_and_changes_nothing(client):
    _fresh_guest()
    token = _guest_in(client)
    before = client.get('/api/watchlist').get_json()

    res = client.post('/api/watchlist', json={'ticker': 'AAPL'},
                      headers={'X-CSRF-Token': token})
    assert res.status_code == 403
    assert res.get_json()['guest'] is True
    assert client.get('/api/watchlist').get_json() == before


@pytest.mark.parametrize('path, payload', [
    ('/api/auth/password',     {'current_password': 'x', 'new_password': GOOD_PW}),
    ('/api/settings',          {'GROQ_API_KEY': 'not-a-real-key'}),
    ('/api/generate-report',   {'ticker': 'AAPL'}),
    ('/api/transactions/quick', {'ticker': 'AAPL', 'shares': 1, 'buy_price': 1,
                                 'sell_price': 2, 'buy_date': '2026-01-01',
                                 'sell_date': '2026-02-01'}),
])
def test_the_expensive_and_credential_routes_are_closed_to_a_guest(client, path, payload):
    _fresh_guest()
    token = _guest_in(client)
    res = client.post(path, json=payload, headers={'X-CSRF-Token': token})
    assert res.status_code == 403
    assert res.get_json()['guest'] is True
    assert terminal._load_settings(owner='guest') == {}


def test_a_guest_cannot_write_anywhere(client):
    """Walk the real url_map: every non-GET /api/ route refuses a guest.

    The rule lives in the gate, so this is the check that it is installed in
    front of everything — the same shape as test_auth's walk of the closed door.
    """
    _fresh_guest()
    token = _guest_in(client)
    seen = 0
    for rule in terminal.app.url_map.iter_rules():
        if not rule.rule.startswith('/api/'):
            continue
        if rule.endpoint in terminal._PUBLIC_ENDPOINTS | terminal._GUEST_WRITABLE:
            continue
        methods = sorted(rule.methods - {'GET', 'HEAD', 'OPTIONS'})
        if not methods:
            continue
        path = re.sub(r'<[^>]+>', 'AAPL', rule.rule)
        for method in methods:
            res = client.open(path, method=method, json={},
                              headers={'X-CSRF-Token': token})
            # 404 only when a converter refused the placeholder — the gate
            # never ran, and the route was not reached either.
            assert res.status_code in (403, 404, 405), \
                f'{method} {rule.rule} answered {res.status_code} for a guest'
            if res.status_code == 403:
                assert res.get_json().get('guest') is True, f'{method} {rule.rule}'
                seen += 1
    assert seen > 20, 'expected to have walked the real route table'


def test_logout_is_the_one_write_a_guest_may_make(client):
    _fresh_guest()
    token = _guest_in(client)
    res = client.post('/api/auth/logout', headers={'X-CSRF-Token': token})
    assert res.status_code == 200
    assert client.get('/api/holdings').status_code == 401


def test_csrf_is_still_checked_ahead_of_the_guest_rule(client):
    _fresh_guest()
    _guest_in(client)
    res = client.post('/api/watchlist', json={'ticker': 'AAPL'})   # no token
    assert res.status_code == 403
    assert res.get_json().get('guest') is not True


def test_a_guest_is_not_an_administrator(client):
    _fresh_guest()
    _guest_in(client)
    assert client.get('/api/admin/users').status_code == 403


def test_a_guest_page_carries_no_password_or_key_forms(client, admin):
    admin_page = client.get('/').data
    assert b'id="pw-current"' in admin_page and b'id="set-anthropic"' in admin_page

    _fresh_guest()
    client.post('/api/auth/logout', headers={'X-CSRF-Token': admin})
    _guest_in(client)
    guest_page = client.get('/').data
    assert b'id="pw-current"' not in guest_page
    assert b'id="set-anthropic"' not in guest_page
    assert b'id="admin-page"' not in guest_page
    assert b'id="settings-page"' in guest_page             # the page itself remains


# ---------------------------------------------------------------------------
# Administering the guest
# ---------------------------------------------------------------------------

def test_an_admin_can_only_toggle_the_guest(client, admin):
    _fresh_guest()
    hdr = {'X-CSRF-Token': admin}

    for payload in ({'role': 'user'}, {'role': 'admin'}, {'password': GOOD_PW}):
        res = client.put('/api/admin/users/guest', json=payload, headers=hdr)
        assert res.status_code == 400, payload
    g = terminal._guest_record()
    assert g['role'] == 'guest' and g['password_hash'] is None

    assert client.put('/api/admin/users/guest', json={'disabled': True},
                      headers=hdr).status_code == 200
    assert not terminal._guest_enabled()
    assert client.put('/api/admin/users/guest', json={'disabled': False},
                      headers=hdr).status_code == 200
    assert terminal._guest_enabled()


def test_the_guest_is_listed_without_credential_material(client, admin):
    _fresh_guest()
    res = client.get('/api/admin/users')
    rows = {u['username']: u for u in res.get_json()}
    assert rows['guest']['role'] == 'guest'
    assert b'password_hash' not in res.data


# ---------------------------------------------------------------------------
# The demo portfolio
# ---------------------------------------------------------------------------

def test_the_seed_derives_holdings_and_sales_from_the_ledger(users_file):
    _fresh_guest()
    txns     = terminal.load_transactions(owner='guest')
    holdings = terminal.load_holdings(owner='guest')
    sales    = terminal.load_sales(owner='guest')

    assert len(txns) == len(terminal._GUEST_LEDGER)
    bought = {t for _, k, t, *_ in terminal._GUEST_LEDGER if k == 'buy'}
    sold   = {t for _, k, t, *_ in terminal._GUEST_LEDGER if k == 'sell'}
    assert {h['ticker'] for h in holdings} == bought - sold
    assert [s['ticker'] for s in sales] == sorted(sold)
    assert sales[0]['gain_loss'] == pytest.approx((35.10 - 32.40) * 50)
    # Two RY.TO buys average into one position.
    ry = next(h for h in holdings if h['ticker'] == 'RY.TO')
    assert ry['shares'] == 35
    assert ry['avg_price'] == pytest.approx((25 * 172.40 + 10 * 181.10) / 35, abs=1e-3)
    # Every stored symbol passes the same check the write routes apply.
    for t in txns:
        assert terminal.clean_ticker(t['ticker']) == t['ticker']
    assert len(terminal.load_watchlist(owner='guest')) == len(terminal._GUEST_WATCHLIST)


def test_seeded_ids_sort_chronologically_as_strings(users_file):
    _fresh_guest()
    txns = terminal.load_transactions(owner='guest')
    by_id   = [t['date'] for t in sorted(txns, key=lambda t: t['id'])]
    by_date = sorted(t['date'] for t in txns)
    assert by_id == by_date


def test_enabling_again_does_not_reset_the_ledger_but_reset_does(users_file):
    _fresh_guest()
    marker = dict(terminal.load_transactions(owner='guest')[0], id='9999999999999-marker')
    terminal.save_transactions([marker], owner='guest')

    terminal.enable_guest_access()
    assert [t['id'] for t in terminal.load_transactions(owner='guest')] == [marker['id']]

    assert terminal._seed_guest_portfolio(force=True)
    ids = [t['id'] for t in terminal.load_transactions(owner='guest')]
    assert marker['id'] not in ids and len(ids) == len(terminal._GUEST_LEDGER)


def test_the_guest_directory_is_its_own(users_file):
    _fresh_guest()
    assert terminal.load_transactions(owner='guest')
    assert terminal.load_transactions(owner='someone_else') == []


# ---------------------------------------------------------------------------
# What happens when a guest tries to trade, and what a new login lands on
# ---------------------------------------------------------------------------

def _snapshot(owner='guest'):
    return (terminal.load_transactions(owner=owner), terminal.load_holdings(owner=owner),
            terminal.load_sales(owner=owner), terminal.load_watchlist(owner=owner))


def test_a_guest_sell_is_refused_and_the_book_is_byte_identical(client):
    _fresh_guest()
    token = _guest_in(client)
    before = _snapshot()
    hdr = {'X-CSRF-Token': token}

    sell = client.post('/api/holdings/RY.TO/sell',
                       json={'shares': 10, 'price': 200, 'date': '2026-09-01'}, headers=hdr)
    assert sell.status_code == 403 and sell.get_json()['guest'] is True

    txn_id = before[0][0]['id']
    assert client.delete(f'/api/transactions/{txn_id}', headers=hdr).status_code == 403
    assert client.put(f'/api/transactions/{txn_id}', json={'price': 1}, headers=hdr).status_code == 403
    assert client.post('/api/holdings', json={'ticker': 'AAPL', 'shares': 1, 'price': 1},
                       headers=hdr).status_code == 403

    assert _snapshot() == before
    assert not terminal._guest_portfolio_drifted()


def test_an_untouched_book_is_not_rewritten_on_login(client):
    _fresh_guest()
    path = os.path.join(terminal._user_data_dir('guest'), 'transactions.json')
    os.utime(path, (1_600_000_000, 1_600_000_000))       # a recognisable mtime
    _guest_in(client)
    assert os.path.getmtime(path) == 1_600_000_000        # read, not reseeded


def test_a_drifted_book_is_reset_by_the_next_guest_login(client):
    """The backstop for a write that reached the files by some route other than
    the gate — a hand edit on the box, or a route a later change lets through."""
    _fresh_guest()
    clean = _snapshot()

    # Simulate a sell that somehow landed: drop RY.TO from the ledger and holdings.
    terminal.save_transactions([t for t in clean[0] if t['ticker'] != 'RY.TO'], owner='guest')
    terminal.save_holdings([h for h in clean[1] if h['ticker'] != 'RY.TO'], owner='guest')
    assert terminal._guest_portfolio_drifted()

    _guest_in(client)
    assert _snapshot() == clean
    assert not terminal._guest_portfolio_drifted()


def test_a_corrupt_guest_file_is_reseeded_not_served(client):
    _fresh_guest()
    clean = _snapshot()
    path = os.path.join(terminal._user_data_dir('guest'), 'holdings.json')
    with open(path, 'w') as f:
        f.write('{not json')
    assert terminal._guest_portfolio_drifted()
    _guest_in(client)
    assert _snapshot() == clean
    assert client.get('/api/holdings').status_code == 200


def test_reset_removes_files_the_seed_never_writes(client):
    _fresh_guest()
    d = terminal._user_data_dir('guest')
    terminal._save_settings({'GROQ_API_KEY': 'planted'}, owner='guest')
    with open(os.path.join(d, 'options.json'), 'w') as f:
        f.write('[]')
    os.makedirs(os.path.join(d, 'reports'), exist_ok=True)
    with open(os.path.join(d, 'reports', 'AAPL.pdf'), 'wb') as f:
        f.write(b'%PDF')
    assert terminal._guest_portfolio_drifted()

    _guest_in(client)                                     # login performs the reset
    assert not os.path.exists(os.path.join(d, 'settings.json'))
    assert not os.path.exists(os.path.join(d, 'options.json'))
    assert not os.path.exists(os.path.join(d, 'reports'))
    assert terminal._load_settings(owner='guest') == {}
    assert not terminal._guest_portfolio_drifted()
