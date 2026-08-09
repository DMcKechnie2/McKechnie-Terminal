"""Tests for the per-device rate limiter.

conftest resets the buckets around every other test in the suite so the limiter
stays invisible to them; this file is the one that drives it on purpose.

The properties worth holding onto are not "does a 429 come back" but the ones
that are easy to regress into something that looks like it works: the default
covers a route nobody remembered to list, a refusal spends nothing, the device
bucket survives signing out, and dropping the cookie does not hand back an
unlimited allowance.
"""
import os
import sys

import pytest
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402

from conftest import TEST_CSRF, TEST_USER, make_client  # noqa: E402


@pytest.fixture
def client():
    """The signed-in client conftest installs, with an empty bucket table."""
    terminal._rate_buckets.clear()
    return terminal.app.test_client()


@pytest.fixture
def no_thread(monkeypatch):
    """Register report jobs without starting a subprocess behind them."""
    class _Stub:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(terminal.threading, 'Thread', _Stub)
    terminal._report_jobs.clear()
    yield
    terminal._report_jobs.clear()


@pytest.fixture
def limited(monkeypatch):
    """Shrink the limits so a test can exhaust one in a few requests.

    Patching the table rather than looping to 150 keeps these tests fast and
    keeps them about the *mechanism*; the real numbers are asserted separately.

    The burst is what these tests assert; the sustained rate is pinned at 1/min
    so that it cannot. At 60/min a drained bucket holds a token again one second
    later, which turns "the next request is refused" into a race against however
    long the test client takes to get there — green on an idle machine, red
    under load. A token a minute takes refill out of the picture without moving
    a single burst.
    """
    monkeypatch.setattr(terminal, '_RATE_DEFAULT', (3, 1))
    monkeypatch.setattr(terminal, '_RATE_LIMITS', {'get_watchlist': (2, 1)})
    monkeypatch.setattr(terminal, '_ADDR_FACTOR', 1000)   # device bucket decides
    terminal._rate_buckets.clear()
    return None


def _drain(client, path, tries=12):
    """Request `path` until it is refused. Returns (ok_count, final_response)."""
    ok = 0
    for _ in range(tries):
        res = client.get(path)
        if res.status_code == 429:
            return ok, res
        ok += 1
    raise AssertionError(f'{path} was never refused after {tries} requests')


# ---------------------------------------------------------------------------
# The default covers everything
# ---------------------------------------------------------------------------

def test_an_unlisted_route_still_has_a_limit(client, limited):
    """The point of the before_request hook: no route opts in."""
    assert 'get_holdings' not in terminal._RATE_LIMITS
    ok, res = _drain(client, '/api/holdings')
    assert ok == 3                                  # _RATE_DEFAULT burst
    assert res.status_code == 429
    assert res.get_json()['rate_limited'] is True


def test_every_named_limit_points_at_a_real_endpoint():
    """A typo here is silent — the route just keeps the default allowance.

    Exactly the failure the hook exists to avoid, one level up: nothing errors,
    the expensive route simply is not limited the way the table claims.
    """
    endpoints = {rule.endpoint for rule in terminal.app.url_map.iter_rules()}
    unknown = sorted(set(terminal._RATE_LIMITS) - endpoints)
    assert unknown == [], f'_RATE_LIMITS names routes that do not exist: {unknown}'


def test_the_expensive_routes_are_all_named():
    """A checklist, so adding a scrape or an LLM call without a limit is a
    failing test rather than a bill."""
    for endpoint in ('get_stock', 'get_news', 'get_market_news',
                     'generate_report', 'api_login', 'api_change_password',
                     'insider_buying', 'crosslist'):
        assert endpoint in terminal._RATE_LIMITS, f'{endpoint} has no named limit'
        burst, per_min = terminal._RATE_LIMITS[endpoint]
        default_burst, default_rate = terminal._RATE_DEFAULT
        assert burst > 0 and per_min > 0
        # Both halves, explicitly: a tuple compare only reaches the second
        # element when the first ties, so it would wave through a named limit
        # with a smaller burst and a *looser* sustained rate.
        assert burst < default_burst and per_min < default_rate, \
            f'{endpoint} is not actually tighter than the default'


def test_a_named_limit_binds_before_the_default(client, limited):
    """get_watchlist is capped at 2 while the default allows 3."""
    ok, _ = _drain(client, '/api/watchlist')
    assert ok == 2


def test_a_named_route_also_spends_the_default_bucket(client, limited):
    """Otherwise the default stops being a ceiling: an expensive route with its
    own allowance would spend on top of the global one rather than out of it."""
    assert client.get('/api/watchlist').status_code == 200      # default: 3 -> 2
    assert client.get('/api/watchlist').status_code == 200      # default: 2 -> 1
    assert client.get('/api/watchlist').status_code == 429      # own bucket empty
    ok, _ = _drain(client, '/api/holdings')
    assert ok == 1, 'the watchlist calls did not draw on the default bucket'


# ---------------------------------------------------------------------------
# The response
# ---------------------------------------------------------------------------

def test_refusal_carries_retry_after(client, limited):
    _, res = _drain(client, '/api/holdings')
    assert res.headers['Retry-After'].isdigit()
    assert int(res.headers['Retry-After']) >= 1


def test_a_page_refusal_is_not_json(client, limited):
    """/ is HTML; answering it with a JSON body would render as garbage."""
    for _ in range(4):
        res = client.get('/')
    assert res.status_code == 429
    assert res.mimetype == 'text/plain'
    assert res.headers['Retry-After']


def test_refusal_spends_nothing(monkeypatch, client):
    """A refused request must not charge the buckets that did have room.

    Otherwise one client hitting the address ceiling drains every other device
    on that address without a single request getting through.
    """
    # 1/min rather than 60: five refusals have to land before the watchlist
    # bucket refills, and a token a second is not long enough to promise that
    # under load. See the `limited` fixture.
    monkeypatch.setattr(terminal, '_RATE_DEFAULT', (50, 1))
    monkeypatch.setattr(terminal, '_RATE_LIMITS', {'get_watchlist': (1, 1)})
    monkeypatch.setattr(terminal, '_ADDR_FACTOR', 1000)
    terminal._rate_buckets.clear()

    assert client.get('/api/watchlist').status_code == 200
    for _ in range(5):
        assert client.get('/api/watchlist').status_code == 429

    # Five refusals, so the default bucket should have paid for exactly the one
    # request that got through.
    device_default = [v for k, v in terminal._rate_buckets.items()
                      if k.startswith('d:*:')]
    assert len(device_default) == 1
    assert device_default[0][0] == pytest.approx(49.0, abs=0.1)


def test_tokens_refill_over_time(monkeypatch, client):
    monkeypatch.setattr(terminal, '_RATE_DEFAULT', (2, 1))
    monkeypatch.setattr(terminal, '_RATE_LIMITS', {})
    monkeypatch.setattr(terminal, '_ADDR_FACTOR', 1000)
    terminal._rate_buckets.clear()

    ok, _ = _drain(client, '/api/holdings')
    assert ok == 2

    # Rewind the bucket's clock rather than sleeping: 1/min is one token a
    # minute, so two minutes back is two tokens. Rewinding by hand is also what
    # keeps the drain above honest — at 60/min the wall clock would hand back a
    # token on its own and the test would pass without this line doing anything.
    for entry in terminal._rate_buckets.values():
        entry[1] -= 120.0
    assert client.get('/api/holdings').status_code == 200


# ---------------------------------------------------------------------------
# The device key
# ---------------------------------------------------------------------------

def test_a_device_cookie_is_issued_and_reused(client):
    res = client.get('/api/holdings')
    assert res.status_code == 200
    issued = res.headers.get('Set-Cookie', '')
    assert terminal._DEVICE_COOKIE in issued
    assert 'HttpOnly' in issued, 'script must not be able to read or set it'
    # The client now holds one, so a second request must not be issued another —
    # a cookie reissued per response is a new bucket per request.
    again = client.get('/api/holdings')
    assert terminal._DEVICE_COOKIE not in again.headers.get('Set-Cookie', '')


def test_the_cookie_is_issued_even_on_a_refusal(client, limited):
    """A limited client that never receives an id draws a fresh bucket on every
    retry, which is the same as having no device limit at all."""
    plain = terminal.app.test_client()
    with plain.session_transaction() as sess:
        sess['uid'], sess['tv'], sess['csrf'] = TEST_USER, 1, TEST_CSRF
    terminal._rate_buckets.clear()

    # Drain the address bucket from a different device so this one's first
    # request is refused before it ever holds a cookie.
    #
    # The refill stamp is set in the *future* so that the refusal does not
    # depend on how quickly the request arrives: _rate_tokens adds
    # max(0.0, now - last) * rate, so a stamp ahead of now contributes exactly
    # zero tokens however long the client takes — the same clamp
    # test_a_backwards_clock_cannot_mint_tokens pins down. Stamping it at `now`
    # left a one-millisecond window instead, because the refill uses the rate
    # _rate_specs derives (_ADDR_FACTOR 1000 x 60/min = 1000 tokens/sec), not
    # the 1.0 stored in the entry, which only _rate_evict reads.
    terminal._rate_buckets['a:*:127.0.0.1'] = [
        0.0, terminal._time_mod.time() + 3600.0, 3.0, 1.0,
    ]
    res = plain.get('/api/holdings')
    assert res.status_code == 429
    assert terminal._DEVICE_COOKIE in res.headers.get('Set-Cookie', '')


@pytest.mark.parametrize('forged', ['', 'x', 'short', 'a' * 65, 'has spaces',
                                    '../../etc', 'a;b', '<script>'])
def test_a_malformed_device_cookie_is_replaced(forged):
    """The value reaches a dict key, so it is shape-checked like a username."""
    plain = terminal.app.test_client()
    plain.set_cookie(terminal._DEVICE_COOKIE, forged)
    with plain.session_transaction() as sess:
        sess['uid'], sess['tv'], sess['csrf'] = TEST_USER, 1, TEST_CSRF
    terminal._rate_buckets.clear()

    res = plain.get('/api/holdings')
    assert res.status_code == 200
    assert terminal._DEVICE_COOKIE in res.headers.get('Set-Cookie', '')
    # The device is the last component of a bucket key, so this is exact —
    # a substring test would trip over a random id that happens to contain it.
    assert not any(k.endswith(f':{forged}') for k in terminal._rate_buckets)
    assert any(k.startswith('d:*:') for k in terminal._rate_buckets), \
        'the request drew on no device bucket at all'


def test_the_device_bucket_survives_logout(client):
    """The id lives outside the session on purpose: session.clear() runs on both
    login and logout, so an id stored there would refund the allowance to
    anyone who signed out and back in."""
    terminal._rate_buckets.clear()
    client.get('/api/holdings')
    before = {k: v[0] for k, v in terminal._rate_buckets.items()
              if k.startswith('d:*:')}
    assert before

    client.post('/api/auth/logout')
    after = {k: v[0] for k, v in terminal._rate_buckets.items()
             if k.startswith('d:*:')}
    assert set(after) == set(before), 'logout moved the device to a new bucket'


def test_dropping_the_cookie_still_meets_the_address_ceiling(monkeypatch):
    """A fresh cookie per request is the obvious way around a device bucket, so
    the address bucket has to be the thing that stops it."""
    # 1/min rather than 60: the address bucket refills at _ADDR_FACTOR x that,
    # and at 60/min a token came back every half second — less than the twenty
    # clients below take to run, so `allowed` counted the refills too.
    monkeypatch.setattr(terminal, '_RATE_DEFAULT', (2, 1))
    monkeypatch.setattr(terminal, '_RATE_LIMITS', {})
    monkeypatch.setattr(terminal, '_ADDR_FACTOR', 2)
    terminal._rate_buckets.clear()

    allowed = 0
    for _ in range(20):
        fresh = terminal.app.test_client()          # new client, no cookie jar
        with fresh.session_transaction() as sess:
            sess['uid'], sess['tv'], sess['csrf'] = TEST_USER, 1, TEST_CSRF
        if fresh.get('/api/holdings').status_code == 200:
            allowed += 1
    # 2 burst x _ADDR_FACTOR, and nothing beyond it however many cookies are minted.
    assert allowed == 4


# ---------------------------------------------------------------------------
# Bucket bookkeeping
# ---------------------------------------------------------------------------

def test_full_buckets_are_evicted(monkeypatch):
    """A refilled bucket answers every question the same as an absent one, so
    dropping it is not the same as forgetting a limit."""
    terminal._rate_buckets.clear()
    now = terminal._time_mod.time()
    terminal._rate_buckets['d:*:drained'] = [0.0, now, 10.0, 1.0]
    terminal._rate_buckets['d:*:refilled'] = [0.0, now - 3600, 10.0, 1.0]
    terminal._rate_evict(now)
    assert 'd:*:drained' in terminal._rate_buckets
    assert 'd:*:refilled' not in terminal._rate_buckets


def test_the_bucket_table_has_a_ceiling():
    """A new cookie is a new key, so an attacker mints rows unless this is capped."""
    assert terminal._RATE_BUCKETS_MAX > 0


def test_a_backwards_clock_cannot_mint_tokens():
    now = terminal._time_mod.time()
    terminal._rate_buckets.clear()
    terminal._rate_buckets['k'] = [0.0, now + 600, 10.0, 1.0]
    assert terminal._rate_tokens('k', 10.0, 1.0, now) == 0.0


# ---------------------------------------------------------------------------
# The gate runs before the login gate
# ---------------------------------------------------------------------------

def test_limiting_happens_before_authentication(monkeypatch):
    """_current_user() re-reads the account file every request; a refusal should
    land in front of that work, not behind it."""
    order = [f.__name__ for f in terminal.app.before_request_funcs[None]]
    assert order.index('_rate_limit_gate') < order.index('_require_login')


def test_an_unauthenticated_flood_is_refused_not_authenticated(monkeypatch):
    monkeypatch.setattr(terminal, '_RATE_DEFAULT', (2, 1))   # see `limited`
    monkeypatch.setattr(terminal, '_RATE_LIMITS', {})
    monkeypatch.setattr(terminal, '_ADDR_FACTOR', 1000)
    monkeypatch.setattr(terminal.app, 'test_client_class', FlaskClient)
    terminal._rate_buckets.clear()

    anon = terminal.app.test_client()
    assert anon.get('/api/holdings').status_code == 401
    assert anon.get('/api/holdings').status_code == 401
    assert anon.get('/api/holdings').status_code == 429


def test_login_is_limited_independently_of_the_lockout(monkeypatch):
    """The lockout counts *failures*; this counts requests, so a flood of valid
    ones still cannot be used to burn scrypt time."""
    assert 'api_login' in terminal._RATE_LIMITS
    monkeypatch.setattr(terminal.app, 'test_client_class', FlaskClient)
    monkeypatch.setattr(terminal, '_login_fails', {})
    # 1/min rather than 60: each of the three logins below spends a scrypt hash,
    # so the third can easily arrive more than a second after the second one.
    monkeypatch.setattr(terminal, '_RATE_LIMITS', {'api_login': (2, 1)})
    monkeypatch.setattr(terminal, '_RATE_DEFAULT', (50, 1))
    monkeypatch.setattr(terminal, '_ADDR_FACTOR', 1000)
    terminal._rate_buckets.clear()

    anon = terminal.app.test_client()
    codes = [anon.post('/api/auth/login',
                       json={'username': 'nobody', 'password': 'x' * 20}).status_code
             for _ in range(3)]
    assert codes == [401, 401, 429]


# ---------------------------------------------------------------------------
# Report generation: a rate limit is not enough on its own
# ---------------------------------------------------------------------------

def test_concurrent_reports_are_capped_per_account(client, no_thread, monkeypatch):
    """Each job is a subprocess that lives for up to six minutes, so the limit
    that matters is how many run at once, not how often one is asked for."""
    monkeypatch.setattr(terminal, 'REPORT_MAX_CONCURRENT', 2)

    for _ in range(2):
        res = client.post('/api/generate-report', json={'ticker': 'AAPL'})
        assert res.status_code == 200

    third = client.post('/api/generate-report', json={'ticker': 'MSFT'})
    assert third.status_code == 429
    assert 'already building' in third.get_json()['error']
    assert len(terminal._report_jobs) == 2


def test_the_cap_is_per_account_not_global(no_thread, monkeypatch):
    """One user filling their own slots must not block everyone else."""
    monkeypatch.setattr(terminal, 'REPORT_MAX_CONCURRENT', 1)
    terminal._rate_buckets.clear()

    if terminal._find_user('second') is None:
        terminal._users_store.mutate(lambda users: users + [{
            'username': 'second', 'password_hash': 'x', 'role': 'user',
            'disabled': False, 'created_at': '2026-01-01T00:00:00+00:00',
            'last_login': None, 'token_version': 1,
        }])

    first = make_client(TEST_USER)
    other = make_client('second')
    assert first.post('/api/generate-report', json={'ticker': 'AAPL'}).status_code == 200
    assert first.post('/api/generate-report', json={'ticker': 'MSFT'}).status_code == 429
    assert other.post('/api/generate-report', json={'ticker': 'AAPL'}).status_code == 200


def test_a_finished_job_frees_its_slot(client, no_thread, monkeypatch):
    monkeypatch.setattr(terminal, 'REPORT_MAX_CONCURRENT', 1)

    job_id = client.post('/api/generate-report',
                         json={'ticker': 'AAPL'}).get_json()['job_id']
    assert client.post('/api/generate-report',
                       json={'ticker': 'MSFT'}).status_code == 429

    terminal._finish_report(job_id, status='done', pdf_path='x.pdf')
    assert client.post('/api/generate-report',
                       json={'ticker': 'MSFT'}).status_code == 200


def test_finishing_a_job_keeps_the_started_stamp(client, no_thread):
    """The TTL prune reads `started`; replacing the dict wholesale dropped it and
    the entry then lived forever."""
    job_id = client.post('/api/generate-report',
                         json={'ticker': 'AAPL'}).get_json()['job_id']
    terminal._finish_report(job_id, status='error', error='boom')
    assert 'started' in terminal._report_jobs[job_id]


def test_old_jobs_are_pruned(client, no_thread):
    terminal._report_jobs['ancient'] = {
        'status': 'done', 'ticker': 'AAPL', 'owner': TEST_USER,
        'started': terminal._time_mod.time() - terminal._REPORT_JOB_TTL - 60,
    }
    terminal._report_jobs['recent'] = {
        'status': 'done', 'ticker': 'MSFT', 'owner': TEST_USER,
        'started': terminal._time_mod.time(),
    }
    client.post('/api/generate-report', json={'ticker': 'NVDA'})
    assert 'ancient' not in terminal._report_jobs
    assert 'recent' in terminal._report_jobs


def test_a_running_job_is_never_pruned(client, no_thread):
    """However long a build has been going, its slot is still occupied."""
    terminal._report_jobs['stuck'] = {
        'status': 'running', 'ticker': 'AAPL', 'owner': TEST_USER,
        'started': terminal._time_mod.time() - terminal._REPORT_JOB_TTL - 60,
    }
    client.post('/api/generate-report', json={'ticker': 'NVDA'})
    assert 'stuck' in terminal._report_jobs
