"""Tests for accounts, sessions and the request gate.

These run offline. The point of most of them is not "does login work" but the
properties that are easy to regress silently: the gate is deny-by-default, the
password never exists in storage, a failure cannot be told apart from an unknown
user, and nobody can remove the last administrator.
"""
import json
import os
import sys

import pytest
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402

GOOD_PW  = 'correct-horse-battery'
OTHER_PW = 'another-good-password'


@pytest.fixture
def users_file(tmp_path, monkeypatch):
    """Point the users store at a temp file so no test touches the real one."""
    store = terminal.JsonStore.__new__(terminal.JsonStore)
    store.path     = str(tmp_path / 'users.json')
    store._default = list
    store._migrate = None
    store._lock    = terminal.threading.RLock()
    monkeypatch.setattr(terminal, '_users_store', store)
    monkeypatch.setattr(terminal, '_login_fails', {})
    return store


@pytest.fixture
def client(users_file, monkeypatch):
    """A client with no session — the opposite of conftest's default.

    conftest hands every other test a pre-authenticated client so the suite can
    reach the routes it is testing. This file is testing the door itself, so it
    opts back out and starts each test signed out.
    """
    terminal.app.config['TESTING'] = True
    monkeypatch.setattr(terminal.app, 'test_client_class', FlaskClient)
    # Deliberately not `with app.test_client()`: that form preserves the request
    # context between calls, and Flask reuses a live app context rather than
    # pushing a new one — so a second client's request would see the first's
    # cached `g.auth_user` and appear authenticated. Real requests never share an
    # app context, and a test that does cannot observe a session being revoked.
    return terminal.app.test_client()


def _login(client, username='admin', password=GOOD_PW):
    res = client.post('/api/auth/login',
                      json={'username': username, 'password': password})
    token = res.get_json().get('csrf_token', '') if res.status_code == 200 else ''
    return res, token


@pytest.fixture
def admin(client):
    """A signed-in administrator. Returns the CSRF token for its session."""
    terminal.create_user('admin', GOOD_PW, 'admin')
    _, token = _login(client)
    return token


# ---------------------------------------------------------------------------
# Password storage
# ---------------------------------------------------------------------------

def test_password_is_hashed_never_stored(users_file):
    """The password must not survive anywhere in the file, in any form."""
    terminal.create_user('dylan', GOOD_PW, 'admin')
    raw = open(users_file.path, encoding='utf-8').read()
    assert GOOD_PW not in raw
    record = json.loads(raw)[0]
    assert 'password' not in record
    assert record['password_hash'].startswith('scrypt:')
    assert GOOD_PW not in record['password_hash']


def test_hash_is_salted(users_file):
    """Two accounts sharing a password must not share a hash."""
    terminal.create_user('alpha1', GOOD_PW)
    terminal.create_user('alpha2', GOOD_PW)
    users = terminal.load_users()
    assert users[0]['password_hash'] != users[1]['password_hash']


def test_public_user_never_carries_the_hash(users_file):
    terminal.create_user('dylan', GOOD_PW, 'admin')
    public = terminal._public_user(terminal._find_user('dylan'))
    assert 'password_hash' not in public
    assert set(public) == {'username', 'role', 'disabled', 'created_at', 'last_login'}


@pytest.mark.parametrize('password', [
    'short',                 # under the minimum
    'exactly11c',
    'x' * 129,               # over the maximum: scrypt cost is a DoS lever
    None,
    12345678901234,
])
def test_weak_passwords_are_refused(users_file, password):
    with pytest.raises(ValueError):
        terminal.create_user('someone', password)


def test_password_may_not_be_the_username(users_file):
    with pytest.raises(ValueError):
        terminal.create_user('averylongusername', 'averylongusername')


@pytest.mark.parametrize('username', [
    'ab',                        # too short
    'x' * 33,                    # too long
    '_leading',                  # must start alphanumeric
    'has space',
    'has/slash',
    '../../etc/passwd',
    'user@host',
    '',
    None,
    123,
    ['admin'],
])
def test_bad_usernames_are_refused(users_file, username):
    with pytest.raises(ValueError):
        terminal.create_user(username, GOOD_PW)


def test_usernames_are_case_folded(users_file):
    terminal.create_user('Dylan', GOOD_PW)
    assert terminal._find_user('dylan') is not None
    with pytest.raises(ValueError):
        terminal.create_user('DYLAN', OTHER_PW)     # not a second account


# ---------------------------------------------------------------------------
# The gate: closed by default
# ---------------------------------------------------------------------------

def test_every_route_is_closed_unless_explicitly_public():
    """The allowlist is the whole attack surface — keep it reviewable.

    This fails whenever an endpoint is added to _PUBLIC_ENDPOINTS, which is the
    intent: opening a route to the internet should not pass unnoticed.
    """
    assert terminal._PUBLIC_ENDPOINTS == {'static', 'login_page', 'api_login'}


@pytest.mark.parametrize('path', [
    '/api/holdings',
    '/api/watchlist',
    '/api/transactions',
    '/api/portfolio/performance',
    '/api/settings',
    '/api/stock?ticker=AAPL',
    '/api/admin/users',
    '/api/auth/me',
])
def test_api_requires_a_session(client, path):
    res = client.get(path)
    assert res.status_code == 401
    assert res.get_json()['auth'] == 'required'


def test_page_redirects_to_login(client):
    res = client.get('/')
    assert res.status_code == 302
    assert '/login' in res.headers['Location']


def test_login_page_is_reachable_without_a_session(client):
    assert client.get('/login').status_code == 200


def test_mutating_route_is_closed_too(client):
    res = client.post('/api/watchlist', json={'ticker': 'AAPL'})
    assert res.status_code == 401
    assert terminal.load_watchlist.__name__          # store untouched


def test_every_api_route_is_covered_by_the_gate(client):
    """Walk the real url_map: no /api/ route may answer without a session.

    A per-route decorator would make this test the only thing standing between a
    forgotten @login_required and an open portfolio; with a before_request gate
    it is a belt-and-braces check that the gate is actually installed.
    """
    seen = 0
    for rule in terminal.app.url_map.iter_rules():
        if not rule.rule.startswith('/api/') or rule.endpoint in terminal._PUBLIC_ENDPOINTS:
            continue
        if '<' in rule.rule:            # needs a real argument; covered elsewhere
            continue
        method = 'GET' if 'GET' in rule.methods else sorted(rule.methods - {'HEAD', 'OPTIONS'})[0]
        res = client.open(rule.rule, method=method)
        assert res.status_code in (401, 405), f'{rule.rule} answered {res.status_code}'
        seen += 1
    assert seen > 20, 'expected to have walked the real route table'


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def test_login_succeeds_and_opens_the_app(client, admin):
    assert client.get('/api/holdings').status_code == 200
    assert client.get('/').status_code == 200


def test_login_rejects_a_wrong_password(client, users_file):
    terminal.create_user('admin', GOOD_PW, 'admin')
    res, _ = _login(client, password='wrong-password-here')
    assert res.status_code == 401
    assert client.get('/api/holdings').status_code == 401


def test_failure_does_not_reveal_whether_the_user_exists(client, users_file):
    terminal.create_user('admin', GOOD_PW, 'admin')
    wrong_pw   = client.post('/api/auth/login',
                             json={'username': 'admin', 'password': 'nope-nope-nope'})
    no_such    = client.post('/api/auth/login',
                             json={'username': 'ghost', 'password': 'nope-nope-nope'})
    assert wrong_pw.status_code == no_such.status_code == 401
    assert wrong_pw.get_json() == no_such.get_json()


def test_disabled_account_cannot_log_in(client, users_file):
    terminal.create_user('temp', GOOD_PW)
    terminal._users_store.mutate(
        lambda users: [dict(u, disabled=True) for u in users])
    res, _ = _login(client, 'temp', GOOD_PW)
    assert res.status_code == 401


def test_disabling_ends_a_live_session(client, users_file):
    """The cookie is authentic but it is a snapshot; the record is the truth."""
    terminal.create_user('temp', GOOD_PW)
    _login(client, 'temp', GOOD_PW)
    assert client.get('/api/holdings').status_code == 200

    terminal._users_store.mutate(
        lambda users: [dict(u, disabled=True) for u in users])
    assert client.get('/api/holdings').status_code == 401


def test_lockout_after_repeated_failures(client, users_file):
    terminal.create_user('admin', GOOD_PW, 'admin')
    for _ in range(terminal.LOGIN_MAX_FAILS):
        client.post('/api/auth/login',
                    json={'username': 'admin', 'password': 'bad-password-xx'})
    blocked = client.post('/api/auth/login',
                          json={'username': 'admin', 'password': GOOD_PW})
    assert blocked.status_code == 429       # correct password, still refused


def test_logout_ends_the_session(client, admin):
    assert client.post('/api/auth/logout',
                       headers={'X-CSRF-Token': admin}).status_code == 200
    assert client.get('/api/holdings').status_code == 401


def test_login_replaces_any_existing_session_token(client, users_file):
    """Session fixation: the token planted before login must not survive it."""
    terminal.create_user('admin', GOOD_PW, 'admin')
    with client.session_transaction() as sess:
        sess['csrf'] = 'planted-token'
        sess['uid']  = 'attacker'
    _, token = _login(client)
    assert token and token != 'planted-token'
    with client.session_transaction() as sess:
        assert sess['uid'] == 'admin'


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def test_write_without_the_csrf_header_is_refused(client, admin):
    res = client.post('/api/watchlist', json={'ticker': 'AAPL'})
    assert res.status_code == 403


def test_write_with_a_wrong_csrf_token_is_refused(client, admin):
    res = client.post('/api/watchlist', json={'ticker': 'AAPL'},
                      headers={'X-CSRF-Token': 'not-the-token'})
    assert res.status_code == 403


def test_write_with_the_csrf_header_is_allowed(client, admin):
    res = client.post('/api/watchlist', json={'ticker': 'AAPL'},
                      headers={'X-CSRF-Token': admin})
    assert res.status_code == 200


def test_reads_do_not_need_a_csrf_token(client, admin):
    assert client.get('/api/watchlist').status_code == 200


# ---------------------------------------------------------------------------
# No public signup
# ---------------------------------------------------------------------------

def test_there_is_no_public_signup_route():
    paths = {r.rule for r in terminal.app.url_map.iter_rules()}
    for tempting in ('/api/auth/signup', '/api/auth/register',
                     '/api/signup', '/api/register', '/signup', '/register'):
        assert tempting not in paths


def test_account_creation_requires_a_session(client, users_file):
    terminal.create_user('admin', GOOD_PW, 'admin')
    res = client.post('/api/admin/users',
                      json={'username': 'intruder', 'password': GOOD_PW})
    assert res.status_code == 401
    assert terminal._find_user('intruder') is None


def test_account_creation_requires_an_admin(client, users_file):
    terminal.create_user('admin', GOOD_PW, 'admin')
    terminal.create_user('plain', OTHER_PW, 'user')
    _, token = _login(client, 'plain', OTHER_PW)
    res = client.post('/api/admin/users',
                      json={'username': 'intruder', 'password': GOOD_PW},
                      headers={'X-CSRF-Token': token})
    assert res.status_code == 403
    assert terminal._find_user('intruder') is None


def test_a_plain_user_cannot_list_or_delete_accounts(client, users_file):
    terminal.create_user('admin', GOOD_PW, 'admin')
    terminal.create_user('plain', OTHER_PW, 'user')
    _, token = _login(client, 'plain', OTHER_PW)
    assert client.get('/api/admin/users').status_code == 403
    assert client.delete('/api/admin/users/admin',
                         headers={'X-CSRF-Token': token}).status_code == 403
    assert terminal._find_user('admin') is not None


def test_admin_can_create_an_account(client, admin):
    res = client.post('/api/admin/users',
                      json={'username': 'colleague', 'password': GOOD_PW,
                            'role': 'user'},
                      headers={'X-CSRF-Token': admin})
    assert res.status_code == 201
    assert res.get_json()['username'] == 'colleague'
    assert 'password_hash' not in res.get_json()
    assert terminal._find_user('colleague')['role'] == 'user'


def test_created_account_can_sign_in(client, admin):
    client.post('/api/admin/users',
                json={'username': 'colleague', 'password': GOOD_PW},
                headers={'X-CSRF-Token': admin})
    client.post('/api/auth/logout', headers={'X-CSRF-Token': admin})
    res, _ = _login(client, 'colleague', GOOD_PW)
    assert res.status_code == 200


def test_admin_cannot_create_a_duplicate_or_weak_account(client, admin):
    dup = client.post('/api/admin/users',
                      json={'username': 'admin', 'password': GOOD_PW},
                      headers={'X-CSRF-Token': admin})
    assert dup.status_code == 400
    weak = client.post('/api/admin/users',
                       json={'username': 'colleague', 'password': 'short'},
                       headers={'X-CSRF-Token': admin})
    assert weak.status_code == 400
    assert terminal._find_user('colleague') is None


# ---------------------------------------------------------------------------
# Administration guardrails
# ---------------------------------------------------------------------------

def test_the_last_admin_cannot_be_deleted_or_demoted(client, admin):
    client.post('/api/admin/users',
                json={'username': 'plain', 'password': OTHER_PW, 'role': 'user'},
                headers={'X-CSRF-Token': admin})

    demote = client.put('/api/admin/users/admin', json={'role': 'user'},
                        headers={'X-CSRF-Token': admin})
    assert demote.status_code == 409

    disable = client.put('/api/admin/users/admin', json={'disabled': True},
                         headers={'X-CSRF-Token': admin})
    assert disable.status_code == 409

    assert terminal._find_user('admin')['role'] == 'admin'
    assert terminal._find_user('admin')['disabled'] is False


def test_an_admin_can_be_demoted_once_another_exists(client, admin):
    client.post('/api/admin/users',
                json={'username': 'second', 'password': OTHER_PW, 'role': 'admin'},
                headers={'X-CSRF-Token': admin})
    res = client.put('/api/admin/users/second', json={'role': 'user'},
                     headers={'X-CSRF-Token': admin})
    assert res.status_code == 200
    assert terminal._find_user('second')['role'] == 'user'


def test_you_cannot_delete_yourself(client, admin):
    res = client.delete('/api/admin/users/admin', headers={'X-CSRF-Token': admin})
    assert res.status_code == 409
    assert terminal._find_user('admin') is not None


def test_deleting_an_unknown_user_is_404_not_a_silent_success(client, admin):
    res = client.delete('/api/admin/users/ghost', headers={'X-CSRF-Token': admin})
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------

def test_password_change_requires_the_current_one(client, admin):
    res = client.post('/api/auth/password',
                      json={'current_password': 'wrong-password-x',
                            'new_password': OTHER_PW},
                      headers={'X-CSRF-Token': admin})
    assert res.status_code == 403


def test_password_change_rotates_the_credential(client, admin):
    res = client.post('/api/auth/password',
                      json={'current_password': GOOD_PW, 'new_password': OTHER_PW},
                      headers={'X-CSRF-Token': admin})
    assert res.status_code == 200

    client.post('/api/auth/logout', headers={'X-CSRF-Token': res.get_json()['csrf_token']})
    assert _login(client, 'admin', GOOD_PW)[0].status_code == 401
    assert _login(client, 'admin', OTHER_PW)[0].status_code == 200


def test_password_change_signs_out_other_sessions(client, users_file):
    """token_version is what makes 'change my password' mean 'lock them out'."""
    terminal.create_user('admin', GOOD_PW, 'admin')
    other = terminal.app.test_client()
    other.post('/api/auth/login', json={'username': 'admin', 'password': GOOD_PW})
    assert other.get('/api/holdings').status_code == 200

    _, token = _login(client)
    client.post('/api/auth/password',
                json={'current_password': GOOD_PW, 'new_password': OTHER_PW},
                headers={'X-CSRF-Token': token})

    assert other.get('/api/holdings').status_code == 401     # the stale session
    assert client.get('/api/holdings').status_code == 200    # the one that changed it


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    ('/holdings',                  '/holdings'),
    ('https://evil.example',       '/'),
    ('//evil.example',             '/'),
    ('http://evil.example/x',      '/'),
    ('/\\evil.example',            '/'),
    ('/ok\r\nSet-Cookie: x=1',     '/'),
    ('', '/'), (None, '/'), (123, '/'),
])
def test_next_cannot_leave_the_app(raw, expected):
    assert terminal._safe_next(raw) == expected


def test_login_redirect_carries_a_safe_next(client):
    res = client.get('/api/holdings', headers={'Accept': 'text/html'})
    assert res.status_code == 401       # /api/ is JSON regardless of Accept


# ---------------------------------------------------------------------------
# Storage failure must fail closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('stored', ['', 'x', 'not$a$hash', None, 12345])
def test_a_corrupt_password_hash_is_a_refusal_not_a_crash(client, users_file, stored):
    """A 500 here is a leak: it tells an attacker the account exists."""
    terminal.create_user('admin', GOOD_PW, 'admin')
    terminal._users_store.mutate(
        lambda users: [dict(u, password_hash=stored) for u in users])
    res = client.post('/api/auth/login',
                      json={'username': 'admin', 'password': GOOD_PW})
    assert res.status_code == 401
    assert res.get_json() == {'error': 'Invalid username or password.'}


def test_a_corrupt_users_file_denies_rather_than_admits(client, users_file):
    terminal.create_user('admin', GOOD_PW, 'admin')
    _login(client)
    with open(users_file.path, 'w', encoding='utf-8') as f:
        f.write('{ not json')
    res = client.get('/api/holdings')
    assert res.status_code == 500        # not 200, and not an empty portfolio
