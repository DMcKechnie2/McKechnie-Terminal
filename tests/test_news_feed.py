"""Regression tests for the breaking-news feed.

Every assertion here corresponds to something that was actually wrong, or to a
contract that is easy to break silently. All offline — the live feed check lives
in test_live.py behind the `network` marker.

    pytest                  # these
    pytest -m network       # live feed reachability
"""
import datetime as dt
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402


# ---------------------------------------------------------------------------
# Feed parsing — three shapes, and a date contract that is easy to get wrong
# ---------------------------------------------------------------------------

RSS20 = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Fed holds rates steady</title>
    <link>https://example.com/a</link>
    <pubDate>Mon, 03 Aug 2026 14:23:00 GMT</pubDate>
    <description>The central bank left its policy rate unchanged.</description>
  </item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>ECB statement</title>
    <link href="https://example.com/b"/>
    <updated>2026-08-03T10:00:00+02:00</updated>
    <summary>Governing council decision.</summary>
  </entry>
</feed>"""


def test_parses_rss20():
    items = terminal._parse_feed(RSS20, 'Test', 'financial', 2)
    assert len(items) == 1
    assert items[0]['title'] == 'Fed holds rates steady'
    assert items[0]['url'] == 'https://example.com/a'
    assert items[0]['tier'] == 2


def test_parses_atom_link_href():
    """Atom puts the URL in an attribute, not in the element text."""
    items = terminal._parse_feed(ATOM, 'ECB', 'policy', 1)
    assert items[0]['url'] == 'https://example.com/b'


@pytest.mark.parametrize('raw,expected', [
    # RFC-822, the common case
    ('Mon, 03 Aug 2026 14:23:00 GMT',  dt.datetime(2026, 8, 3, 14, 23, 0)),
    # ECB publishes +0200. Left unconverted its releases land in the future and
    # pin themselves to the top of a recency-ranked feed.
    ('Thu, 30 Jul 2026 10:00:00 +0200', dt.datetime(2026, 7, 30, 8, 0, 0)),
    # Yahoo puts ISO-8601 inside a <pubDate> tag, so the parser cannot be
    # chosen from the tag name.
    ('2026-08-02T16:03:00Z',            dt.datetime(2026, 8, 2, 16, 3, 0)),
    ('2026-07-31T09:30:53+00:00',       dt.datetime(2026, 7, 31, 9, 30, 53)),
])
def test_dates_normalise_to_naive_utc(raw, expected):
    got = terminal._feed_datetime(raw)
    assert got == expected
    # The frontend does `new Date(pub_ts + 'Z')`; a tz-aware value would be
    # double-offset.
    assert got.tzinfo is None


def test_unparseable_date_is_none_not_an_exception():
    assert terminal._feed_datetime('not a date') is None
    assert terminal._feed_datetime('') is None


def test_date_tag_falls_back_past_pubdate():
    """Bank of Canada uses <date>, not <pubDate>."""
    xml = b"""<?xml version="1.0"?><rss><channel><item>
      <title>Rate decision</title><link>https://example.com/c</link>
      <date>2026-07-31T09:30:53+00:00</date>
    </item></channel></rss>"""
    items = terminal._parse_feed(xml, 'BoC', 'policy', 1)
    assert items[0]['pub_dt'] == dt.datetime(2026, 7, 31, 9, 30, 53)


def test_description_html_is_stripped():
    """AP ships an <img> tag inside <description>.

    Two things went wrong when it was not stripped: the markup reached the
    payload, and AP's image URLs contain the path segment "default", which
    scored every one of its sport and box-office stories as a debt default.
    """
    xml = b"""<?xml version="1.0"?><rss><channel><item>
      <title>Cycling result</title><link>https://apnews.com/article/x</link>
      <description>&lt;img src="https://dims.apnews.com/dims4/default/abc/"&gt;Race report.</description>
    </item></channel></rss>"""
    items = terminal._parse_feed(xml, 'AP', 'broad', 2)
    assert '<' not in items[0]['summary']
    assert 'img' not in items[0]['summary'].lower()
    assert 'Race report.' in items[0]['summary']


def test_item_without_title_or_link_is_skipped():
    xml = b"""<?xml version="1.0"?><rss><channel>
      <item><title>No link here</title></item>
      <item><link>https://example.com/d</link></item>
    </channel></rss>"""
    assert terminal._parse_feed(xml, 'Test', 'financial', 2) == []


def test_feed_with_no_items_raises(monkeypatch):
    """Raising keeps 'this feed is broken' distinct from 'it published nothing'
    — the same discipline _fetch_div_events uses, so a blank never gets cached."""
    class Resp:
        status_code = 200
        content = b'<?xml version="1.0"?><rss><channel></channel></rss>'

    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **k: Resp())
    with pytest.raises(RuntimeError):
        terminal._fetch_feed(('http://x', 'Test', 'financial', 2))


def test_non_200_raises(monkeypatch):
    class Resp:
        status_code = 503
        content = b''

    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **k: Resp())
    with pytest.raises(RuntimeError):
        terminal._fetch_feed(('http://x', 'Test', 'financial', 2))


# ---------------------------------------------------------------------------
# Keyword matching — the anchoring bug, measured against live feeds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('text', [
    'embassies warn americans of travel risk',
    'openai hack confirmed months of cyber warnings',
    'company warned investors about software licences',
])
def test_war_does_not_match_warn_or_software(text):
    """A '\\bwar' prefix match tagged these as war coverage. Both-side \\b is
    what fixes it; the existing _action_kw loop uses bare substring `in`."""
    assert not terminal._MARKET_KW_HIGH.search(text)


@pytest.mark.parametrize('text,expected', [
    ('russia invasion escalates',        'invasion'),
    ('fed announces rate cut',           'rate cut'),
    ('trump imposes new tariffs',        'tariffs'),
    ('war in the region deepens',        'war'),
])
def test_real_macro_terms_still_match(text, expected):
    assert expected in terminal._MARKET_KW_HIGH.findall(text)


def test_longest_alternative_wins():
    """'rate cut' must win over 'interest rate' fragments, so the alternation is
    ordered longest-first."""
    assert 'rate cut' in terminal._MARKET_KW_HIGH.findall('the fed signalled a rate cut')


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def _item(title, url, source, tier=2, score=10, hours=1.0):
    return {'title': title, 'url': url, 'source': source, 'group': 'financial',
            'tier': tier, 'summary': '', 'pub_dt': dt.datetime.utcnow(),
            'hours_old': hours, 'score': score}


def test_dedupe_collapses_url_variants():
    items = terminal._dedupe_news([
        _item('Fed holds rates', 'https://www.example.com/a?utm_source=rss', 'CNBC', score=20),
        _item('Fed holds rates', 'http://example.com/a/',                    'AP',   score=10),
    ])
    assert len(items) == 1
    assert items[0]['source'] == 'CNBC'        # the higher-scoring copy survived
    assert items[0]['also'] == ['AP']


def test_dedupe_collapses_reworded_titles():
    items = terminal._dedupe_news([
        _item('Federal Reserve holds interest rates steady at 4.25%',
              'https://a.com/1', 'Reuters', score=30),
        _item('Fed holds interest rates steady',
              'https://b.com/2', 'CNBC', score=20),
    ])
    assert len(items) == 1
    assert 'CNBC' in items[0]['also']


def test_distinct_stories_are_not_merged():
    items = terminal._dedupe_news([
        _item('Fed holds rates steady', 'https://a.com/1', 'CNBC'),
        _item('OPEC agrees production cut', 'https://b.com/2', 'AP'),
    ])
    assert len(items) == 2


def test_canon_url_ignores_scheme_www_and_query():
    a = terminal._canon_url('https://www.example.com/path?utm=1#frag')
    b = terminal._canon_url('http://example.com/path/')
    assert a == b


# ---------------------------------------------------------------------------
# Categories — single-valued, ordered, first match wins
# ---------------------------------------------------------------------------

def test_category_is_single_valued_and_ordered():
    """A headline hitting several rulesets gets the first, not a list — that is
    what lets the chips partition the feed."""
    item = {'title': 'Fed weighs rate cut as oil prices and inflation surge',
            'summary': '', 'group': 'financial', 'source': 'CNBC'}
    assert terminal._categorise(item) == 'central-bank'


def test_policy_group_short_circuits():
    """A central-bank press release is a central-bank item whatever the wording."""
    item = {'title': 'Minutes of the June meeting', 'summary': '',
            'group': 'policy', 'source': 'Federal Reserve'}
    assert terminal._categorise(item) == 'central-bank'


def test_white_house_is_policy_not_central_bank():
    item = {'title': 'Presidential proclamation on imports', 'summary': '',
            'group': 'policy', 'source': 'White House'}
    assert terminal._categorise(item) == 'policy'


def test_unmatched_headline_falls_back_to_markets():
    item = {'title': 'Acme Corp names new chief executive', 'summary': '',
            'group': 'financial', 'source': 'CNBC'}
    assert terminal._categorise(item) == 'markets'


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_fresh_high_impact_outranks_stale_low_impact():
    now = dt.datetime.utcnow()
    hot = {'title': 'Fed announces emergency rate cut', 'summary': '',
           'tier': 1, 'hours_old': 0.5}
    cold = {'title': 'Acme names new chief executive', 'summary': '',
            'tier': 3, 'hours_old': 40}
    assert terminal._impact_score(hot, now) > terminal._impact_score(cold, now)


def test_listicles_are_vetoed():
    now = dt.datetime.utcnow()
    junk = {'title': '3 Reasons To Buy This Dividend Stock', 'summary': '',
            'tier': 2, 'hours_old': 0.5}
    real = {'title': 'Acme reports quarterly results', 'summary': '',
            'tier': 2, 'hours_old': 0.5}
    assert terminal._impact_score(junk, now) < terminal._impact_score(real, now)


def test_hoisted_hard_skip_still_kills_listicles():
    """_hard_skip moved out of _build_news to module scope so both news paths
    share it. The four existing /api/news tests monkeypatch _build_news, so none
    of them would notice if the hoist broke."""
    killed = ['should you buy apple stock right now?',
              '3 reasons to buy this dividend king',
              'top 5 stocks for 2026',
              'here is why i sold my portfolio']
    kept = ['fed holds rates steady', 'apple announces q3 earnings beat']
    for t in killed:
        assert any(p.search(t) for p in terminal._HARD_SKIP_RE), t
    for t in kept:
        assert not any(p.search(t) for p in terminal._HARD_SKIP_RE), t


# ---------------------------------------------------------------------------
# Build, cache and failure behaviour
# ---------------------------------------------------------------------------

@pytest.fixture
def news_cache():
    """Process-global, like the corp-action caches — isolate each test."""
    terminal._market_news_cache.clear()
    terminal._market_last_good.update({'items': [], 'ts': 0, 'stale': True})
    yield
    terminal._market_news_cache.clear()


# Each headline uses disjoint vocabulary on purpose. The overlap pass in
# _dedupe_news correctly collapses near-identical titles, so a fixture that
# reused wording would dedupe itself below the thinness guard and make every
# build look like a failure. Every headline also carries a macro keyword, since
# broad-wire items without one are filtered by design.
_HEADLINES = [
    'Tariff schedule widens on imported steel',
    'Inflation accelerates beyond forecasts',
    'Payrolls miss badly again',
    'Crude slips as OPEC lifts output',
    'Sanctions target shipping insurers',
    'Treasury yields steepen sharply',
    'Recession odds climb among forecasters',
    'Stimulus package clears committee',
    'Shutdown deadline nears without agreement',
    'Embargo extended another six months',
    'Bailout terms agreed for regional lender',
    'GDP revised upward for the quarter',
    'Housing starts tumble in June',
    'Deficit widens on lower receipts',
    'Central bank holds its policy rate',
]
_FEED_INDEX = {name: i for i, (_u, name, _g, _t) in enumerate(terminal._MARKET_FEEDS)}


def _fake_feed_factory(broken=()):
    """Three stories per feed, windowed so neighbouring feeds share one.

    That overlap is deliberate — it exercises cross-feed dedupe — while the
    window still covers all 15 distinct headlines across the registry.
    """
    def fake(entry):
        url, name, group, tier = entry
        if name in broken:
            raise RuntimeError('feed down')
        slug = name.replace(' ', '')
        start = _FEED_INDEX[name]
        rows = []
        for n in range(3):
            i = (start + n) % len(_HEADLINES)
            rows.append({
                'title': _HEADLINES[i],
                'url': f'https://{slug}.com/{i}',
                'source': name, 'group': group, 'tier': tier,
                'summary': 'macro summary',
                'pub_dt': dt.datetime.utcnow() - dt.timedelta(minutes=30 + n),
            })
        return rows
    return fake


def test_partial_feed_failure_still_builds(news_cache, monkeypatch):
    """One or two feeds down out of fifteen is normal and must stay invisible —
    but observable on the payload."""
    broken = ('AP', 'ECB')
    monkeypatch.setattr(terminal, '_fetch_feed', _fake_feed_factory(broken))
    data = terminal._build_market_news()
    assert data['items']
    assert data['stale'] is False
    assert set(data['sources_failed']) == set(broken)
    assert data['sources_ok'] == len(terminal._MARKET_FEEDS) - len(broken)


def test_total_failure_is_not_cached_and_serves_last_good(news_cache, monkeypatch):
    """A transient blip must not pin an empty news page for the whole TTL."""
    monkeypatch.setattr(terminal, '_fetch_feed', _fake_feed_factory())
    good = terminal._build_market_news()
    assert good['items'] and good['stale'] is False

    terminal._market_news_cache.clear()          # force a rebuild
    def dead(entry):
        raise RuntimeError('all feeds down')
    monkeypatch.setattr(terminal, '_fetch_feed', dead)

    degraded = terminal._build_market_news()
    assert degraded['stale'] is True
    assert degraded['items'] == good['items']    # last good, not empty
    assert 'market' not in terminal._market_news_cache._data

    monkeypatch.setattr(terminal, '_fetch_feed', _fake_feed_factory())
    assert terminal._build_market_news()['stale'] is False


def test_concurrent_builds_collapse_into_one(news_cache, monkeypatch):
    """The property /api/news lacks: it uses a plain dict, so two tabs each run
    the whole pipeline."""
    calls = []
    base = _fake_feed_factory()

    def counting(entry):
        calls.append(entry[1])
        return base(entry)

    monkeypatch.setattr(terminal, '_fetch_feed', counting)
    threads = [threading.Thread(target=terminal._build_market_news) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # One fan-out, not eight.
    assert len(calls) == len(terminal._MARKET_FEEDS)


def test_route_returns_categories_and_respects_limit(news_cache, monkeypatch):
    monkeypatch.setattr(terminal, '_fetch_feed', _fake_feed_factory())
    client = terminal.app.test_client()
    body = client.get('/api/news/market?limit=5').get_json()
    assert len(body['items']) == 5
    assert body['categories']
    assert all('category' in i and 'pub_ts' in i for i in body['items'])


def test_items_older_than_max_age_are_dropped(news_cache, monkeypatch):
    stale_dt = dt.datetime.utcnow() - dt.timedelta(hours=terminal.MARKET_MAX_AGE_H + 10)

    def ancient(entry):
        url, name, group, tier = entry
        return [{'title': f'{name} tariff inflation story', 'url': f'https://{name}.com/x',
                 'source': name, 'group': group, 'tier': tier, 'summary': '',
                 'pub_dt': stale_dt}]

    monkeypatch.setattr(terminal, '_fetch_feed', ancient)
    data = terminal._build_market_news()
    # Everything aged out, so the build was too thin to cache and we get the
    # stale fallback rather than a page of three-day-old headlines.
    assert data['stale'] is True


def test_broad_wire_needs_macro_vocabulary(news_cache, monkeypatch):
    """AP and BBC World carry sport and box office in the same feed as the
    geopolitics we want, and neither exposes a filterable URL path."""
    base = _fake_feed_factory()

    def mixed(entry):
        url, name, group, tier = entry
        # Every feed also publishes a sport story with no macro vocabulary.
        rows = [{'title': 'Cy Young winner traded at the MLB deadline',
                 'url': f'https://{name}.com/sportball', 'source': name,
                 'group': group, 'tier': tier, 'summary': '',
                 'pub_dt': dt.datetime.utcnow()}]
        rows.extend(base(entry))
        return rows

    monkeypatch.setattr(terminal, '_fetch_feed', mixed)
    data = terminal._build_market_news()

    broad_names = {n for _, n, g, _t in terminal._MARKET_FEEDS if g == 'broad'}
    sport = [i for i in data['items'] if 'Cy Young' in i['title']]

    # The sport story must not survive under a broad wire. It may survive from a
    # financial feed — everything those publish is market news by definition, and
    # dedupe keeps a single copy attributed to the highest scorer.
    assert not [i for i in sport if i['source'] in broad_names]
    # Broad wires still contribute their macro stories.
    assert [i for i in data['items'] if i['source'] in broad_names]


# ---------------------------------------------------------------------------
# Positions feed
# ---------------------------------------------------------------------------

def test_portfolio_symbols_dedupes_overlap(monkeypatch):
    monkeypatch.setattr(terminal, 'load_holdings',
                        lambda: [{'ticker': 'MFI.TO'}, {'ticker': 'MSFT.TO'}])
    monkeypatch.setattr(terminal, 'load_watchlist',
                        lambda: [{'ticker': 'MFI.TO'}, {'ticker': 'PEP'}])
    assert terminal._portfolio_symbols() == ['MFI.TO', 'MSFT.TO', 'PEP']


def test_portfolio_symbols_rejects_unvalidated_stored_values(monkeypatch):
    """add_to_watchlist and add_to_holdings store a bare .strip().upper(), so
    these files can legally contain something that was never a symbol. Anything
    read back out is untrusted input."""
    monkeypatch.setattr(terminal, 'load_holdings',
                        lambda: [{'ticker': 'AAPL & calc'}, {'ticker': 'MSFT'}])
    monkeypatch.setattr(terminal, 'load_watchlist',
                        lambda: [{'ticker': '../../etc/passwd'}, {'ticker': ''}])
    assert terminal._portfolio_symbols() == ['MSFT']


def test_positions_route_ignores_query_string_tickers(monkeypatch):
    """The server decides what the portfolio is; the client sends nothing."""
    monkeypatch.setattr(terminal, 'load_holdings', lambda: [{'ticker': 'MSFT'}])
    monkeypatch.setattr(terminal, 'load_watchlist', lambda: [])
    seen = []
    monkeypatch.setattr(terminal, '_build_news',
                        lambda s, name='', lite=False: seen.append(s) or [])

    terminal._positions_news_cache.clear()
    client = terminal.app.test_client()
    body = client.get('/api/news/positions?tickers=EVIL&ticker=EVIL').get_json()

    assert seen == ['MSFT']
    assert body['symbols'] == ['MSFT']


def test_positions_never_scrapes_or_calls_groq(monkeypatch):
    """The lite path exists so a nine-symbol portfolio doesn't spawn ~135
    threads and ~100 LLM calls under one 25s deadline."""
    monkeypatch.setattr(terminal, 'load_holdings', lambda: [{'ticker': 'MSFT'}])
    monkeypatch.setattr(terminal, 'load_watchlist', lambda: [])

    def boom(*a, **k):
        raise AssertionError('positions feed must not enrich')

    monkeypatch.setattr(terminal, 'groq_call', boom)

    class FakeTicker:
        news = [{'content': {'title': 'Microsoft announces earnings beat',
                             'canonicalUrl': {'url': 'https://x.com/1'},
                             'provider': {'displayName': 'Reuters'},
                             'summary': 'Microsoft reported results.',
                             'pubDate': '2026-08-03T12:00:00Z'}}]

    monkeypatch.setattr(terminal.yf, 'Ticker', lambda t: FakeTicker())
    monkeypatch.setattr(terminal, '_resolve_company_name', lambda t: 'Microsoft')

    terminal._positions_news_cache.clear()
    client = terminal.app.test_client()
    body = client.get('/api/news/positions').get_json()

    assert body['items']
    assert body['items'][0]['tickers'] == ['MSFT']


def test_positions_merges_a_story_shared_by_two_holdings(monkeypatch):
    monkeypatch.setattr(terminal, 'load_holdings',
                        lambda: [{'ticker': 'MSFT'}, {'ticker': 'META'}])
    monkeypatch.setattr(terminal, 'load_watchlist', lambda: [])
    monkeypatch.setattr(terminal, '_build_news',
                        lambda s, name='', lite=False: [
                            {'title': 'Big Tech rallies', 'url': 'https://x.com/1',
                             'source': 'Reuters', 'date': '', 'pub_ts': '2026-08-03T12:00:00',
                             'thumb': ''}])

    terminal._positions_news_cache.clear()
    client = terminal.app.test_client()
    body = client.get('/api/news/positions').get_json()

    assert len(body['items']) == 1
    assert sorted(body['items'][0]['tickers']) == ['META', 'MSFT']


def test_build_news_lite_skips_enrichment(monkeypatch):
    """The seam itself: lite must return candidates without scraping or
    rewriting, and must not leak the internal scoring fields."""
    class FakeTicker:
        news = [{'content': {'title': 'Microsoft announces earnings beat',
                             'canonicalUrl': {'url': 'https://x.com/1'},
                             'provider': {'displayName': 'Reuters'},
                             'summary': 'Microsoft reported results.',
                             'pubDate': '2026-08-03T12:00:00Z'}}]

    monkeypatch.setattr(terminal.yf, 'Ticker', lambda t: FakeTicker())
    monkeypatch.setattr(terminal, '_resolve_company_name', lambda t: 'Microsoft')
    monkeypatch.setattr(terminal, 'groq_call',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('no groq')))

    rows = terminal._build_news('MSFT', lite=True)
    assert rows
    assert rows[0]['title'] == 'Microsoft announces earnings beat'   # not rewritten
    assert 'score' not in rows[0]
    assert '_summary_fallback' not in rows[0]


# ---------------------------------------------------------------------------
# Relevance filter
#
# Keyword scoring ranks what is already market news; it cannot tell "Boeing
# clears key hurdle and stock rallies" from "Why flights are so expensive".
# ---------------------------------------------------------------------------

@pytest.fixture
def llm_off(monkeypatch):
    """No backend configured, and a clean verdict memo."""
    terminal._llm_verdicts.clear()
    monkeypatch.setattr(terminal, '_resolve_api_key', lambda name, owner=None: '')
    yield
    terminal._llm_verdicts.clear()


@pytest.mark.parametrize('title', [
    'Analyst Report: Valero Energy Corp',
    'Market Update: EPD, GLW, LUV, SWK, VLO',
    'Form 144 Airbnb For: 3 August',
])
def test_boilerplate_is_dropped_without_any_key(llm_off, title):
    kept, mode = terminal._filter_relevant([{'title': title}])
    assert kept == []
    assert mode == 'regex'


@pytest.mark.parametrize('title', [
    'If I buy a house for $1 million in cash at 70, will I run out of money?',
    'I paid off $15,000 in credit card debt',
    'My ex-husband died so why is Fidelity asking me for a death certificate',
])
def test_first_person_money_stories_are_dropped(llm_off, title):
    kept, _ = terminal._filter_relevant([{'title': title}])
    assert kept == []


@pytest.mark.parametrize('title', [
    'Boeing clears key hurdle and stock rallies, leading the Dow',
    'Visa to buy cybersecurity firm BioCatch for $2.4 billion',
    'Fed holds rates steady',
    'Amazon tops $3 trillion market cap',
])
def test_real_market_news_survives_the_regex_layer(llm_off, title):
    """The cheap layer must be narrow. Anything ambiguous is the LLM's job."""
    kept, _ = terminal._filter_relevant([{'title': title}])
    assert len(kept) == 1


def test_no_key_keeps_everything_else(llm_off):
    items = [{'title': 'Fed holds rates steady'},
             {'title': 'OPEC agrees output cut'}]
    kept, mode = terminal._filter_relevant(items)
    assert len(kept) == 2
    assert mode == 'regex'


def test_llm_verdicts_are_applied(monkeypatch):
    terminal._llm_verdicts.clear()
    monkeypatch.setattr(terminal, '_resolve_api_key',
                        lambda name, owner=None: 'k' if name == 'DEEPSEEK_API_KEY' else '')
    items = [{'title': f'Macro story number {i} on tariffs'} for i in range(10)]
    monkeypatch.setattr(terminal, '_llm_keep_indices',
                        lambda titles, backend: {0, 1, 2, 3, 4, 5})

    kept, mode = terminal._filter_relevant(items, terminal._llm_backend())
    assert mode == 'deepseek'
    assert len(kept) == 6
    terminal._llm_verdicts.clear()


def test_deepseek_is_preferred_over_groq(monkeypatch):
    terminal._llm_verdicts.clear()
    monkeypatch.setattr(terminal, '_resolve_api_key', lambda name, owner=None: 'key-' + name)
    assert terminal._llm_backend()['name'] == 'DEEPSEEK_API_KEY'


def test_groq_is_used_when_only_groq_is_set(monkeypatch):
    monkeypatch.setattr(terminal, '_resolve_api_key',
                        lambda name, owner=None: 'k' if name == 'GROQ_API_KEY' else '')
    assert terminal._llm_backend()['name'] == 'GROQ_API_KEY'


def test_api_key_comes_from_the_accounts_own_settings(monkeypatch):
    """The import-time os.environ read on _groq is why a key saved through
    /api/settings never reached it. This resolver reads the account's settings,
    at call time, so a pasted key works on the very next request."""
    monkeypatch.setattr(terminal, '_load_settings',
                        lambda owner=None: {'DEEPSEEK_API_KEY': 'from-settings'})
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    assert terminal._resolve_api_key('DEEPSEEK_API_KEY') == 'from-settings'


def test_the_environment_is_not_a_source(monkeypatch):
    """Keys are per account, so there is no shared one — the shell included.

    An exported key would be a single key serving every account, which is the
    thing per-user keys exist to prevent. No key configured means the degraded
    path, not somebody else's credential.
    """
    monkeypatch.setattr(terminal, '_load_settings', lambda owner=None: {})
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'from-env')
    assert terminal._resolve_api_key('DEEPSEEK_API_KEY') == ''


def test_llm_failure_keeps_items_and_does_not_claim_credit(monkeypatch):
    """An outage must degrade to the regex layer, not empty the page — and the
    payload must not report a model pass that never happened."""
    terminal._llm_verdicts.clear()
    monkeypatch.setattr(terminal, '_resolve_api_key',
                        lambda name, owner=None: 'k' if name == 'GROQ_API_KEY' else '')

    def dead(titles, backend):
        raise RuntimeError('api down')

    monkeypatch.setattr(terminal, '_llm_keep_indices', dead)
    items = [{'title': f'Macro story {i} on tariffs'} for i in range(10)]
    kept, mode = terminal._filter_relevant(items, terminal._llm_backend())

    assert len(kept) == 10
    assert mode == 'regex'


def test_degenerate_response_is_ignored(monkeypatch):
    """A model that drops nearly everything is far more likely to be broken than
    right."""
    terminal._llm_verdicts.clear()
    monkeypatch.setattr(terminal, '_resolve_api_key',
                        lambda name, owner=None: 'k' if name == 'GROQ_API_KEY' else '')
    monkeypatch.setattr(terminal, '_llm_keep_indices', lambda titles, backend: {0})

    items = [{'title': f'Macro story {i} on tariffs'} for i in range(40)]
    kept, mode = terminal._filter_relevant(items, terminal._llm_backend())

    assert len(kept) == 40
    assert mode == 'regex'
    terminal._llm_verdicts.clear()


def test_verdicts_are_memoised_across_builds(monkeypatch):
    """A rebuild every 5 minutes must not re-pay for headlines already judged —
    and must not let a borderline item flicker in and out of the feed."""
    terminal._llm_verdicts.clear()
    monkeypatch.setattr(terminal, '_resolve_api_key',
                        lambda name, owner=None: 'k' if name == 'GROQ_API_KEY' else '')
    asked = []

    def judge(titles, backend):
        asked.append(len(titles))
        return set(range(len(titles)))

    monkeypatch.setattr(terminal, '_llm_keep_indices', judge)
    items = [{'title': f'Macro story {i} on tariffs'} for i in range(10)]

    backend = terminal._llm_backend()
    terminal._filter_relevant(items, backend)
    terminal._filter_relevant(items, backend)

    assert asked == [10], 'second pass should have re-used the memo'
    terminal._llm_verdicts.clear()


def test_thinness_guard_measures_feeds_not_filter_output(news_cache, monkeypatch):
    """An LLM legitimately dropping most of the page is not an upstream failure
    and must not be reported as stale."""
    monkeypatch.setattr(terminal, '_fetch_feed', _fake_feed_factory())
    monkeypatch.setattr(terminal, '_resolve_api_key',
                        lambda name, owner=None: 'k' if name == 'GROQ_API_KEY' else '')
    terminal._llm_verdicts.clear()
    monkeypatch.setattr(terminal, '_llm_keep_indices',
                        lambda titles, backend: set(range(len(titles)))
                        if len(titles) < 3 else {0, 1, 2, 3, 4, 5})

    data = terminal._build_market_news(terminal._llm_backend())
    assert data['stale'] is False
    assert data['filter'] == 'groq'
    terminal._llm_verdicts.clear()
