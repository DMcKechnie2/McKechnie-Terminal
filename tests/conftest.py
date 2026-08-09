"""Shared test setup: a signed-in client, and a users store that is not the real one.

app._require_login closes every route by default, which is the point of it — but
it also means the ~130 tests about dividends, cross-listings and ledger replay
would each need login boilerplate to reach the routes they are actually testing.
So `app.test_client()` returns a client that already holds a valid session and
sends the CSRF header.

The bypass lives entirely here. Nothing in app.py knows that tests exist, and
there is no "skip auth" flag in production code for someone to find later and
set. tests/test_auth.py opts back out — it builds plain clients so it can still
see the closed door.
"""
import os
import sys
import threading

import pytest
from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402

TEST_USER   = 'testrunner'
TEST_CSRF   = 'test-csrf-token'
TEST_SECRET = 'test-secret-key-not-used-anywhere-real'


class SignedInClient(FlaskClient):
    """A test client that arrives authenticated, with the CSRF header attached."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with self.session_transaction() as sess:
            sess['uid']  = TEST_USER
            sess['tv']   = 1
            sess['csrf'] = TEST_CSRF

    def open(self, *args, **kwargs):
        headers = Headers(kwargs.get('headers') or {})
        # setdefault, so a test that wants to send a wrong token still can.
        if 'X-CSRF-Token' not in headers:
            headers['X-CSRF-Token'] = TEST_CSRF
        kwargs['headers'] = headers
        return super().open(*args, **kwargs)


# The two stores that are still one file for the whole app: the account list and
# the session signing key. Everything else — portfolio *and* API keys — is
# per-user and resolved through _user_store(), which builds its paths under
# _USER_DATA_ROOT, so redirecting that root moves all of it at once.
_STORE_NAMES = ('_users_store', '_app_secret_store')


def _temp_store(original, directory):
    """A JsonStore with the same shape as `original`, backed by a temp file."""
    clone = terminal.JsonStore.__new__(terminal.JsonStore)
    clone.path     = os.path.join(directory, os.path.basename(original.path))
    clone._default = original._default
    clone._migrate = original._migrate
    clone._lock    = threading.RLock()
    return clone


def make_client(username):
    """A test client signed in as `username`, with a matching CSRF token.

    The default client is always TEST_USER; the isolation tests need two
    accounts at once to check that one cannot see the other's data.
    """
    token = f'csrf-{username}'

    class _Client(FlaskClient):
        def open(self, *args, **kwargs):
            headers = Headers(kwargs.get('headers') or {})
            if 'X-CSRF-Token' not in headers:
                headers['X-CSRF-Token'] = token
            kwargs['headers'] = headers
            return super().open(*args, **kwargs)

    previous = terminal.app.test_client_class
    terminal.app.test_client_class = _Client
    try:
        client = terminal.app.test_client()
    finally:
        terminal.app.test_client_class = previous

    user = terminal._find_user(username)
    with client.session_transaction() as sess:
        sess['uid']  = username
        sess['tv']   = int((user or {}).get('token_version', 1))
        sess['csrf'] = token
    return client


@pytest.fixture(autouse=True)
def _fresh_rate_buckets():
    """Start every test with a full rate-limit allowance.

    The limiter keys on a device cookie and a client address, and the test
    client keeps its cookies and is always 127.0.0.1 — so without this the whole
    suite shares one bucket and the 448th request pays for the first. That is a
    slow failure that shows up as an unrelated test going red once somebody adds
    a few more, so the reset is per test rather than per session.

    Like the auth bypass above, this lives entirely in test code: app.py has no
    flag that turns the limiter off. tests/test_rate_limit.py is the file that
    opts back in and drives it on purpose.
    """
    terminal._rate_buckets.clear()
    yield
    terminal._rate_buckets.clear()


@pytest.fixture(scope='session', autouse=True)
def _auth_test_harness(tmp_path_factory):
    """Redirect every store to a temp directory and seed the test account.

    Session-scoped and autouse because this is a containment boundary, not a
    convenience: a test that hits a mutating route through the authenticated
    client would otherwise write to the real watchlist.json and holdings.json
    sitting beside app.py. That is not hypothetical — the CSRF test did exactly
    that, and left an 'AAPL' row in the live watchlist. Tests that patch their
    own stores still override this; it is the floor, not the mechanism.
    """
    directory = str(tmp_path_factory.mktemp('data'))
    saved     = {name: getattr(terminal, name) for name in _STORE_NAMES}
    for name, original in saved.items():
        setattr(terminal, name, _temp_store(original, directory))

    real_user_root = terminal._USER_DATA_ROOT
    terminal._USER_DATA_ROOT = os.path.join(directory, 'users')
    terminal._user_stores.clear()      # drop anything resolved against the real root

    real_secret, real_class = terminal.app.secret_key, terminal.app.test_client_class
    terminal.app.secret_key        = TEST_SECRET
    terminal.app.test_client_class = SignedInClient
    terminal.app.config['TESTING'] = True

    terminal._users_store.save([{
        'username':      TEST_USER,
        'password_hash': 'x',       # never verified: these clients skip the form
        'role':          'admin',
        'disabled':      False,
        'created_at':    '2026-01-01T00:00:00+00:00',
        'last_login':    None,
        'token_version': 1,
    }])

    yield

    for name, original in saved.items():
        setattr(terminal, name, original)
    terminal._USER_DATA_ROOT = real_user_root
    terminal._user_stores.clear()
    terminal.app.secret_key        = real_secret
    terminal.app.test_client_class = real_class
