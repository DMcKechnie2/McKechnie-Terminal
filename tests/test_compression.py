"""Response compression.

This exists for one payload in particular. The index and exchange browser ships
a whole universe in a single response so that sorting and filtering cost no
further requests — 503 rows for the S&P 500, 3,402 for Nasdaq. Uncompressed that
is ~876KB of JSON; gzipped it is ~171KB, because the body is overwhelmingly
repeated key names and digits. Flask does not compress on its own and the
loopback default has no proxy in front of it to do it instead.

The failure worth catching here is a client that gets a gzipped body it never
asked for, which does not look like a bug in any log — it looks like corrupt
JSON at the far end.
"""
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402

GZIP = {'Accept-Encoding': 'gzip, deflate'}


@pytest.fixture
def big(monkeypatch):
    """A universe payload comfortably over the compression threshold."""
    rows = [{'full_ticker': f'SYM{i}', 'name': f'Test Company Number {i}, Inc.'}
            for i in range(400)]
    monkeypatch.setattr(terminal, '_index_members', lambda k: rows)
    monkeypatch.setattr(terminal, '_index_quotes', lambda syms: {
        r['full_ticker']: {'symbol': r['full_ticker'], 'regularMarketPrice': 150.0,
                           'regularMarketChangePercent': 1.25,
                           'fiftyTwoWeekLow': 100.0, 'fiftyTwoWeekHigh': 200.0,
                           'trailingPE': 30.0, 'marketCap': 3.0e11,
                           'currency': 'USD', 'quoteType': 'EQUITY',
                           'longName': r['name']} for r in rows})
    return terminal.app.test_client()


def test_a_large_json_body_is_compressed(big):
    resp = big.get('/api/index/sp500', headers=GZIP)

    assert resp.headers['Content-Encoding'] == 'gzip'
    body = json.loads(gzip.decompress(resp.data))
    assert len(body['rows']) == 400


def test_compression_actually_saves_most_of_the_payload(big):
    plain = big.get('/api/index/sp500')
    terminal._index_payload_cache.clear()
    zipped = big.get('/api/index/sp500', headers=GZIP)

    assert len(zipped.data) < len(plain.data) / 3


def test_content_length_matches_the_compressed_body(big):
    """A Content-Length left describing the uncompressed body truncates the
    response at the client, which surfaces as unparseable JSON rather than as
    anything naming compression."""
    resp = big.get('/api/index/sp500', headers=GZIP)
    assert int(resp.headers['Content-Length']) == len(resp.data)


def test_a_client_that_did_not_ask_gets_plain_json(big):
    """The half that is silently wrong rather than merely slow: a gzipped body
    handed to a client that never advertised gzip is corrupt JSON at the far
    end, and nothing in the log says so."""
    resp = big.get('/api/index/sp500')

    assert 'Content-Encoding' not in resp.headers
    assert resp.get_json()['count'] == 400


def test_vary_is_announced_either_way(big):
    """The representation varies by request header whether or not this response
    was compressed. A shared cache that missed that would serve one client's
    gzipped copy to a client that cannot read it."""
    for headers in ({}, GZIP):
        terminal._index_payload_cache.clear()
        resp = big.get('/api/index/sp500', headers=headers)
        assert 'accept-encoding' in resp.headers.get('Vary', '').lower()


def test_a_small_body_is_left_alone():
    """Below about a kilobyte the gzip header and trailer cost more than the
    compression saves, and most responses here are a two-field object."""
    resp = terminal.app.test_client().get('/api/listing/nope', headers=GZIP)

    assert resp.status_code == 404
    assert 'Content-Encoding' not in resp.headers
    assert 'error' in resp.get_json()


def test_an_error_response_is_not_compressed(monkeypatch, big):
    """Only 200s are compressed, so a redirect to the login form or a 429 stays
    a body the browser can read without negotiating anything."""
    monkeypatch.setattr(terminal, '_index_members',
                        lambda k: (_ for _ in ()).throw(RuntimeError('upstream')))
    resp = big.get('/api/index/dow', headers=GZIP)

    assert resp.status_code == 502
    assert 'Content-Encoding' not in resp.headers
