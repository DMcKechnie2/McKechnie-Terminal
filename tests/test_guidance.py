"""Forward guidance: what triggers a run, what it costs, and who can see it.

The feature is a subprocess that spends the account's Anthropic key, so the
tests that matter here are about the *trigger* and the *boundary*, not about
extraction quality — that lives in the Forward Guide project.

  - GET starts nothing. It is what the page reads when the button is first
    pressed, and if it could ever begin a run then every press would spend
    money that a stored answer had already paid for.
  - A run needs a key, an unhidden symbol and a well-shaped ticker, and it
    refuses each on its own rather than letting the subprocess find out.
  - The subprocess environment carries this account's keys and nothing
    inherited, because the whole point of per-user keys is that one exported
    variable cannot bill everybody. Both launchers are covered here, including
    the StockBox report one: they share `_account_env`, and the invariant is a
    property of the keys rather than of either feature, so it is tested once in
    the file that explains it.
  - A second ticker adds to the stored payload rather than replacing it.

Nothing here spawns a real subprocess: _run_guidance is patched wherever a
route would start one, and the tests that exercise a worker drive it directly
with a stubbed Popen.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402

TICKER = 'ZFG'


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A signed-in client whose per-user files live in a fresh temp directory."""
    monkeypatch.setattr(terminal, '_USER_DATA_ROOT', str(tmp_path / 'users'))
    monkeypatch.setattr(terminal, '_user_stores', {})
    monkeypatch.setattr(terminal, '_guidance_jobs', {})
    return terminal.app.test_client()


@pytest.fixture
def no_spawn(monkeypatch):
    """Record what would have been run, and run nothing."""
    started = []
    monkeypatch.setattr(terminal.threading, 'Thread',
                        lambda **kw: started.append(kw) or _NoThread())
    return started


class _NoThread:
    def start(self):
        pass


def _with_key(client, key='sk-ant-test'):
    assert client.post('/api/settings', json={'ANTHROPIC_API_KEY': key}).status_code == 200


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------

def test_get_starts_nothing_and_is_empty_for_a_fresh_account(client, no_spawn):
    """The read half must stay free, or the button costs money to look at."""
    res = client.get('/api/forward_guidance')
    assert res.status_code == 200
    assert res.get_json()['tickers'] == {}

    res = client.get(f'/api/forward_guidance?ticker={TICKER}')
    assert res.status_code == 200
    assert res.get_json()['entry'] is None
    assert no_spawn == [], 'a GET started a run'


def test_a_run_without_a_key_is_refused_before_anything_is_spawned(client, no_spawn):
    """The subprocess would exit 1 a second later with the reason in a log tail.

    Answering here makes it a sentence the UI can show, and costs nothing.
    """
    res = client.post('/api/forward_guidance/run', json={'ticker': TICKER})
    assert res.status_code == 400
    assert res.get_json()['needs_key'] == 'ANTHROPIC_API_KEY'
    assert no_spawn == []


def test_a_hidden_symbol_is_refused_independently(client, no_spawn):
    """/api/stock already 403s a hidden symbol, and that is not enough.

    This route is reachable on its own and spends the account's key, which is
    the same reason /api/news does its own check instead of trusting that the
    detail page refused first.
    """
    _with_key(client)
    assert client.post('/api/blocked', json={'ticker': TICKER}).status_code in (200, 201)

    res = client.post('/api/forward_guidance/run', json={'ticker': TICKER})
    assert res.status_code == 403
    assert res.get_json()['blocked'] is True
    assert no_spawn == []


@pytest.mark.parametrize('bad', ['', 'AAPL & calc', '../../etc/passwd', None])
def test_a_ticker_that_is_not_a_symbol_never_reaches_argv(client, no_spawn, bad):
    _with_key(client)
    res = client.post('/api/forward_guidance/run', json={'ticker': bad})
    assert res.status_code == 400
    assert no_spawn == []


def test_a_run_with_a_key_starts_exactly_one_job(client, no_spawn):
    _with_key(client)
    res = client.post('/api/forward_guidance/run', json={'ticker': TICKER})
    assert res.status_code == 200
    assert res.get_json()['job_id']
    assert len(no_spawn) == 1
    assert no_spawn[0]['args'][1] == TICKER


def test_the_concurrency_cap_is_what_bounds_a_run(client, no_spawn, monkeypatch):
    """A rate limit bounds how often; it cannot bound how many are alive."""
    _with_key(client)
    monkeypatch.setattr(terminal, 'GUIDANCE_MAX_CONCURRENT', 1)
    assert client.post('/api/forward_guidance/run', json={'ticker': TICKER}).status_code == 200
    res = client.post('/api/forward_guidance/run', json={'ticker': 'ZFGB'})
    assert res.status_code == 429


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_another_accounts_job_reads_as_absent(client, monkeypatch):
    """A uuid4 is unguessable, and unguessable is not an access check.

    `detail` on a failed job is a run log from another account.
    """
    monkeypatch.setitem(terminal._guidance_jobs, 'job-of-someone-else', {
        'status': 'error', 'ticker': 'ZOTHER', 'owner': 'somebody',
        'started': 0, 'detail': 'PRIVATE-LOG-LINE',
    })
    res = client.get('/api/forward_guidance/status/job-of-someone-else')
    assert res.status_code == 404
    assert 'PRIVATE-LOG-LINE' not in res.get_data(as_text=True)


def test_the_subprocess_environment_carries_this_accounts_keys_and_no_others(
        client, monkeypatch):
    """An exported key is one key billed to everybody — the thing per-user keys
    exist to prevent. Naming the variable empty is also what stops Forward
    Guide's own .env from filling the gap for an account that configured none.
    """
    _with_key(client, 'sk-ant-belongs-to-this-account')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-EXPORTED-IN-THE-SHELL')
    monkeypatch.setenv('ALPHAVANTAGE_API_KEY', 'AV-EXPORTED-IN-THE-SHELL')

    env = terminal._account_env('testrunner')
    assert env['ANTHROPIC_API_KEY'] == 'sk-ant-belongs-to-this-account'
    # Configured by nobody, so it must be present-and-empty rather than
    # inherited: absent would let the .env beside report.py supply one.
    assert env['ALPHAVANTAGE_API_KEY'] == ''
    assert 'EXPORTED' not in env['ANTHROPIC_API_KEY']


def test_the_report_subprocess_is_held_to_the_same_rule(client, monkeypatch):
    """The other launcher, and the one that used to be weaker.

    `_run_report` filled its environment with `if _k not in env`, so a key
    exported in the shell that started the server beat the account's own and one
    person's key paid for everybody's reports — the same failure the guidance
    path is guarded against above. Driven through `_run_report` rather than
    asserted against `_account_env`, because the helper was already correct; the
    bug was a launcher not using it, and only a launcher can show that fixed.
    """
    monkeypatch.setattr(terminal, '_report_jobs', {})
    _with_key(client, 'sk-ant-belongs-to-this-account')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-EXPORTED-IN-THE-SHELL')
    monkeypatch.setenv('FRED_API_KEY', 'FRED-EXPORTED-IN-THE-SHELL')
    # Not a credential and not in _SETTINGS_KEYS: build_report.py reads it for
    # the thesis list, so the copied environment has to carry it through.
    monkeypatch.setenv('STOCKBOX_THESES', 'margins hold')

    seen = {}

    def _popen(argv, **kw):
        seen.update(kw['env'])
        return _FakePopen(None, returncode=1)   # the branch that touches no files

    monkeypatch.setattr(terminal.subprocess, 'Popen', _popen)
    terminal._report_jobs['r1'] = {'status': 'running', 'ticker': TICKER,
                                   'owner': 'testrunner', 'started': 0}
    terminal._run_report('r1', TICKER, 'testrunner')

    assert seen, 'the subprocess was never started'
    assert seen['ANTHROPIC_API_KEY'] == 'sk-ant-belongs-to-this-account'
    assert seen['FRED_API_KEY'] == ''
    assert seen['STOCKBOX_THESES'] == 'margins hold'


# ---------------------------------------------------------------------------
# The stored payload
# ---------------------------------------------------------------------------

def _entry(symbol, **over):
    base = {'symbol': symbol, 'sec_ticker': symbol, 'reachable': True,
            'reason': 'US-listed', 'generated_at': '2026-08-12T00:00:00+00:00',
            'scorecards': [{'ticker': symbol, 'period_label': 'FY2026'}],
            'items': [{'ticker': symbol, 'metric': 'revenue'}]}
    base.update(over)
    return base


def test_a_second_ticker_adds_rather_than_replaces(client):
    """`report.py --push` writes the whole payload over the file, which is why
    the run hands back a temp file and the merge happens here instead."""
    terminal._merge_guidance('testrunner', 'ZAAA', _entry('ZAAA'))
    terminal._merge_guidance('testrunner', 'ZBBB', _entry('ZBBB'))

    stored = client.get('/api/forward_guidance').get_json()['tickers']
    assert set(stored) == {'ZAAA', 'ZBBB'}

    # And looking one up again replaces only its own entry.
    terminal._merge_guidance('testrunner', 'ZAAA', _entry('ZAAA', reason='updated'))
    stored = client.get('/api/forward_guidance').get_json()['tickers']
    assert set(stored) == {'ZAAA', 'ZBBB'}
    assert stored['ZAAA']['reason'] == 'updated'


def test_an_entry_is_keyed_by_the_symbol_asked_about_not_the_filer():
    """You asked about MSFT.TO; Forward Guide answered about MSFT. Keyed on the
    filer, the page would never find its own answer."""
    payload = {
        'generated_at': '2026-08-12T00:00:00+00:00',
        'scorecards': [], 'items': [],
        'resolved': [{'symbol': 'MSFT.TO', 'sec_ticker': 'MSFT',
                      'reachable': True, 'reason': 'CDR of a US filer'}],
    }
    entry = terminal._guidance_entry('MSFT.TO', payload)
    assert entry['symbol'] == 'MSFT.TO'
    assert entry['sec_ticker'] == 'MSFT'
    assert entry['reachable'] is True


def test_an_unreachable_symbol_keeps_its_reason():
    """"No EDGAR presence" and "files 40-F/6-K, so never an 8-K item 2.02" need
    different fixes, so neither may be flattened into "unavailable"."""
    payload = {
        'generated_at': '2026-08-12T00:00:00+00:00',
        'scorecards': [], 'items': [],
        'resolved': [{'symbol': 'PZA.TO', 'sec_ticker': 'PZA.TO',
                      'reachable': False,
                      'reason': 'Canadian issuer, files with SEDAR not EDGAR'}],
    }
    entry = terminal._guidance_entry('PZA.TO', payload)
    assert entry['reachable'] is False
    assert 'SEDAR' in entry['reason']


# ---------------------------------------------------------------------------
# The legacy batch payload
# ---------------------------------------------------------------------------

_LEGACY = {
    'version': 1,
    'generated_at': '2026-08-09T00:00:00+00:00',
    'scorecards': [{'ticker': 'NFLX', 'period_label': 'FY2026'},
                   {'ticker': 'MSFT', 'period_label': 'FY2026'}],
    'items': [{'ticker': 'NFLX', 'metric': 'revenue'},
              {'ticker': 'MSFT', 'metric': 'revenue'},
              {'ticker': 'MSFT', 'metric': 'eps_diluted'}],
    'unreachable': [{'symbol': 'PZA.TO',
                     'reason': 'files with SEDAR+ only -- no EDGAR presence'}],
}


def test_a_batch_payload_is_regrouped_rather_than_discarded():
    """It is guidance already extracted and already paid for; dropping it would
    make the first press of every button spend to learn what the file knew."""
    out = terminal._migrate_forward_guidance(dict(_LEGACY))
    assert set(out['tickers']) == {'NFLX', 'MSFT', 'PZA.TO'}
    assert len(out['tickers']['MSFT']['items']) == 2
    assert len(out['tickers']['NFLX']['scorecards']) == 1
    # The flat lists do not survive alongside the regrouped copy.
    assert set(out) == {'version', 'tickers'}


def test_the_unreachable_reason_survives_the_migration():
    out = terminal._migrate_forward_guidance(dict(_LEGACY))
    entry = out['tickers']['PZA.TO']
    assert entry['reachable'] is False
    assert 'SEDAR' in entry['reason']


def test_a_hybrid_file_keeps_the_newer_per_ticker_entry():
    """A file written by this route after a batch push holds both shapes. The
    per-ticker entry came from the newer run, so it must win."""
    hybrid = dict(_LEGACY, tickers={'NFLX': _entry('NFLX', reason='from the route')})
    out = terminal._migrate_forward_guidance(hybrid)
    assert out['tickers']['NFLX']['reason'] == 'from the route'
    assert 'MSFT' in out['tickers'], 'folding the batch half was skipped'


def test_migrating_the_current_shape_changes_nothing():
    current = {'version': 1, 'tickers': {'NFLX': _entry('NFLX')}}
    assert terminal._migrate_forward_guidance(current) == current


# ---------------------------------------------------------------------------
# The worker, with the subprocess stubbed out
# ---------------------------------------------------------------------------

class _FakePopen:
    def __init__(self, payload, returncode=0, lines=('working…',)):
        self._payload, self.returncode, self._lines = payload, returncode, lines
        self.stdout = iter(lines)

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def _run_with(monkeypatch, payload, returncode=0, write=True):
    """Drive _run_guidance against a stubbed subprocess, returning the job."""
    def _popen(argv, **kw):
        if write and payload is not None:
            out = argv[argv.index('--json') + 1]
            with open(out, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
        return _FakePopen(payload, returncode)

    monkeypatch.setattr(terminal.subprocess, 'Popen', _popen)
    terminal._guidance_jobs['j1'] = {'status': 'running', 'ticker': TICKER,
                                     'owner': 'testrunner', 'started': 0}
    terminal._run_guidance('j1', TICKER, 'testrunner')
    return terminal._guidance_jobs['j1']


def test_a_successful_run_stores_the_payload_and_reports_what_it_found(
        client, monkeypatch):
    payload = {
        'generated_at': '2026-08-12T00:00:00+00:00',
        'scorecards': [{'ticker': TICKER, 'period_label': 'FY2026'}],
        'items': [{'ticker': TICKER, 'metric': 'revenue'},
                  {'ticker': TICKER, 'metric': 'eps_diluted'}],
        'resolved': [{'symbol': TICKER, 'sec_ticker': TICKER,
                      'reachable': True, 'reason': 'US-listed'}],
    }
    job = _run_with(monkeypatch, payload)
    assert job['status'] == 'done'
    assert job['items'] == 2 and job['periods'] == 1

    entry = client.get(f'/api/forward_guidance?ticker={TICKER}').get_json()['entry']
    assert entry['items'][1]['metric'] == 'eps_diluted'


def test_exit_zero_with_no_payload_is_an_error_not_a_silent_empty(
        client, monkeypatch):
    """Nothing extracted and nothing written is a real outcome, but it is not
    "this company gave no guidance" — reporting it as one caches a false
    negative into the UI."""
    job = _run_with(monkeypatch, None, returncode=0, write=False)
    assert job['status'] == 'error'
    assert 'no payload' in job['error'].lower()
    assert client.get(f'/api/forward_guidance?ticker={TICKER}').get_json()['entry'] is None


def test_a_nonzero_exit_carries_the_log_tail_for_diagnosis(client, monkeypatch):
    job = _run_with(monkeypatch, None, returncode=1, write=False)
    assert job['status'] == 'error'
    assert 'exited with code 1' in job['error']


def test_the_temp_payload_file_is_removed_even_on_failure(monkeypatch):
    """It holds a copy of the account's extracted guidance; nothing should be
    left behind in the system temp directory."""
    seen = []

    def _popen(argv, **kw):
        seen.append(argv[argv.index('--json') + 1])
        raise RuntimeError('upstream blew up')

    monkeypatch.setattr(terminal.subprocess, 'Popen', _popen)
    terminal._guidance_jobs['j2'] = {'status': 'running', 'ticker': TICKER,
                                     'owner': 'testrunner', 'started': 0}
    terminal._run_guidance('j2', TICKER, 'testrunner')

    assert terminal._guidance_jobs['j2']['status'] == 'error'
    assert seen and not os.path.exists(seen[0])
