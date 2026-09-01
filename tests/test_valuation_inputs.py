"""The valuation calculator's margin of safety must not depend on a share count.

MoS used to be `(equity/shares - price) / (equity/shares)`, which put Yahoo's
`sharesOutstanding` between the model and the answer. That input was wrong in two
independent ways:

  * The prefill stored `(shares / 1e6).toFixed(0)` — the nearest million. Invisible
    on a mega-cap, ruinous below one: NVR's 2,678,153 shares became 3,000,000
    (+12.0%), MKL's 12,389,958 became 12,000,000 (-3.1%), and anything under
    500,000 shares rounded to **zero**, so the field the page had just filled in
    read back as empty and MoS printed '(enter shares)'.

  * On a dual-class issuer it is not the same figure the cap is built from at all,
    so no amount of precision fixes it. Yahoo reports BRK-B's 1,408,035,161 B
    shares against a `marketCap` covering both classes: measured live, shares x
    price came to $718.1B against a reported $1,091.8B, a 34.2% gap that lands
    directly on the margin of safety.

Market cap is reported directly and needs no reconstruction, so MoS is computed
against it, and the per-share line is derived back out of the same two market
facts (implied shares = cap / price) so the two cannot disagree.
"""
import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal  # noqa: E402

INDEX_HTML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'templates', 'index.html',
)


@pytest.fixture(scope='module')
def index_src():
    with open(INDEX_HTML, encoding='utf-8') as fh:
        return fh.read()


def _fn(src, name):
    """The body of a top-level JS function, up to the next one."""
    start = src.index(f'function {name}(')
    return src[start:src.index('\nfunction ', start + 1)]


def _code(body):
    """`body` with its // comments removed.

    These assertions are about what the code computes, and the comments here
    quote the formulas they replaced — an unstripped search finds the commentary
    rather than the arithmetic.
    """
    return '\n'.join(re.sub(r'//.*$', '', line) for line in body.splitlines())


# ---------------------------------------------------------------------------
# The payload carries the raw cap
# ---------------------------------------------------------------------------

def test_stock_payload_sends_market_cap_as_a_number_not_only_a_string():
    """The raw/value rule. The calculator compares an equity value against the
    cap, so parsing '$1.09T' back would quantise every mega-cap to three digits."""
    src = open(terminal.__file__, encoding='utf-8').read()
    assert "'market_cap_raw': market_cap_raw," in src
    assert 'market_cap_raw = _finite(' in src


# ---------------------------------------------------------------------------
# A cached payload keeps its cap consistent with its refreshed price
# ---------------------------------------------------------------------------

class _FakeFastInfo:
    def __init__(self, last, prev):
        self.last_price = last
        self.previous_close = prev


class _FakeTicker:
    def __init__(self, last, prev):
        self.fast_info = _FakeFastInfo(last, prev)


def _payload(price='100.00', cap=1_000e9, symbol='$'):
    return {
        'ticker': 'TEST',
        'price': price,
        'market_cap_raw': cap,
        'market_cap': terminal.format_large_number(cap, symbol),
        'currency_symbol': symbol,
    }


def test_refresh_quote_rescales_the_cap_with_the_price(monkeypatch):
    """An entry is servable for STOCK_STALE_TTL — an hour. Refreshing the price
    and leaving the cap behind would put a live price beside an hour-old cap and
    bias every margin of safety by whatever the stock did in between. Cap is
    price x shares and the share count does not move intraday, so the rescale is
    exact rather than an approximation."""
    monkeypatch.setattr(terminal.yf, 'Ticker', lambda t: _FakeTicker(110.0, 100.0))

    out = terminal._refresh_quote(_payload(price='100.00', cap=1_000e9))

    assert out['price'] == '110.00'
    assert out['market_cap_raw'] == pytest.approx(1_100e9)
    assert out['market_cap'] == '$1.10T'


def test_refresh_quote_does_not_compound_across_repeated_cache_hits(monkeypatch):
    """Every request inside STOCK_TTL runs this on the same cached entry. The
    rescale is only correct because the copy is what gets mutated and the result
    is never written back, so each pass applies one ratio from the stored
    baseline. Mutating in place would compound the ratio on every hit and walk a
    popular ticker's cap away from reality over an hour."""
    monkeypatch.setattr(terminal.yf, 'Ticker', lambda t: _FakeTicker(110.0, 100.0))

    cached = _payload(price='100.00', cap=1_000e9)
    first  = terminal._refresh_quote(cached)
    second = terminal._refresh_quote(cached)

    assert first['market_cap_raw'] == pytest.approx(second['market_cap_raw'])
    assert second['market_cap_raw'] == pytest.approx(1_100e9)
    # The entry the cache holds is untouched.
    assert cached['market_cap_raw'] == pytest.approx(1_000e9)
    assert cached['price'] == '100.00'


def test_refresh_quote_keeps_the_cap_in_the_listings_currency(monkeypatch):
    """The formatted half has to be rebuilt with the same symbol it went out
    with — a C$126B bank relabelled '$' is the SK hynix defect in a new place."""
    monkeypatch.setattr(terminal.yf, 'Ticker', lambda t: _FakeTicker(200.0, 190.0))

    out = terminal._refresh_quote(_payload(price='100.00', cap=50e9, symbol='C$'))

    assert out['market_cap_raw'] == pytest.approx(100e9)
    assert out['market_cap'] == 'C$100.00B'


def test_refresh_quote_leaves_the_cap_alone_when_the_prior_price_is_unusable(monkeypatch):
    """`price` is 'N/A' when the fresh build had no quote. There is no ratio to
    apply, and inventing one would be worse than a slightly stale cap."""
    monkeypatch.setattr(terminal.yf, 'Ticker', lambda t: _FakeTicker(110.0, 100.0))

    out = terminal._refresh_quote(_payload(price='N/A', cap=1_000e9))

    assert out['price'] == '110.00'
    assert out['market_cap_raw'] == pytest.approx(1_000e9)


def test_refresh_quote_survives_a_payload_with_no_cap(monkeypatch):
    """Yahoo omits marketCap for a stable minority of listings. A missing cap must
    not cost the payload its price refresh."""
    monkeypatch.setattr(terminal.yf, 'Ticker', lambda t: _FakeTicker(110.0, 100.0))

    data = _payload()
    data['market_cap_raw'] = None
    data['market_cap'] = 'N/A'

    out = terminal._refresh_quote(data)

    assert out['price'] == '110.00'
    assert out['market_cap_raw'] is None


# ---------------------------------------------------------------------------
# The calculator asks for a cap, not a share count
# ---------------------------------------------------------------------------

def test_the_dcf_panel_asks_for_market_cap(index_src):
    assert 'id="dcf-mcap"' in index_src
    assert 'Market Cap (millions)' in index_src


def test_no_share_count_input_survives_anywhere(index_src):
    """Input, listener list and calcDCF all had to move together. A leftover
    getElementById('dcf-shares') is a TypeError that kills the whole handler."""
    assert 'dcf-shares' not in index_src


def test_margin_of_safety_is_computed_against_the_cap(index_src):
    body = _code(_fn(index_src, 'calcDCF'))
    assert '(equity - mcap) / equity' in body
    # The old per-share form, in any spacing.
    assert not re.search(r'ivps\s*-\s*price', body), \
        'MoS is back to comparing a derived per-share value against price'


def test_the_per_share_line_is_derived_from_the_same_two_facts(index_src):
    """implied shares = cap / price, so (ivps - price)/ivps and
    (equity - mcap)/equity are the same number and the two readouts cannot drift
    apart."""
    body = _code(_fn(index_src, 'calcDCF'))
    assert 'mcap / price' in body
    assert 'equity / impliedSh' in body


def test_no_dcf_prefill_rounds_to_the_nearest_million(index_src):
    """Not just the share count. FCF and net debt were prefilled the same way, on
    the same panel, into the same equity value — so a small-cap's whole model was
    quantised, and a sub-$500k figure landed as a zero the calculator reads as an
    empty field."""
    body = _code(_fn(index_src, 'fillCalculators'))
    assert 'toFixed(0)' not in body
    for field in ('dcf-fcf', 'dcf-mcap', 'dcf-netdebt'):
        assert field in body, f'{field} lost its prefill'


def test_zero_net_debt_is_still_written_to_the_field(index_src):
    """A debt-free company has net_debt_raw == 0, and net cash is negative. A
    truthiness test would drop the first and a missing field defaults to 0
    anyway — but only until someone types in it."""
    body = _code(_fn(index_src, 'fillCalculators'))
    assert 'ndM !== null' in body


def test_the_panel_shows_the_equity_value_mos_is_computed_from(index_src):
    """MoS compares equity value against market cap, and equity value was the one
    figure in that chain the panel never printed. The only total on screen was
    *enterprise* value, which differs from equity by net debt — so tuning the model
    to MoS = 0 and checking it against market cap came up short by exactly the net
    cash, with nothing on the panel to reconcile the two. Alphabet holds $121.7B of
    it."""
    assert 'id="dcf-equity"' in index_src
    assert '>Equity Value<' in index_src
    body = _code(_fn(index_src, 'calcDCF'))
    assert 'dcf-equity' in body
    # It has to be cleared with the rest, or a stale total survives an invalid model.
    assert "'dcf-ev','dcf-equity'" in body


def test_a_break_even_margin_of_safety_is_not_printed_as_negative_zero(index_src):
    """At an exact break-even the float lands a hair either side of zero, which
    rendered '-0.0%' in the loss colour on a figure displayed as zero."""
    body = _code(_fn(index_src, 'calcDCF'))
    assert 'Math.abs(mos) < 0.05' in body
    assert 'shown.toFixed(1)' in body


@pytest.mark.parametrize('fcf, net_debt', [
    (50_000e6, -121_683e6),   # net cash, Alphabet-shaped
    (50_000e6, 0.0),          # neither
    (50_000e6, 250_000e6),    # net debt
    (300e6,    12e6),         # small cap
])
def test_at_zero_mos_equity_equals_market_cap_and_ivps_equals_price(fcf, net_debt):
    """The identity the new row makes legible, asserted rather than eyeballed.

    Enterprise value ties out to the cap only for a company holding no cash and
    owing nothing, which is why reading it as the total was misleading on every
    real balance sheet.
    """
    growth = wacc = 0.10
    tgr, years, price = 0.03, 10, 100.0

    pv1, f = 0.0, fcf
    for t in range(1, years + 1):
        f *= (1 + growth)
        pv1 += f / (1 + wacc) ** t
    ev = pv1 + ((f * (1 + tgr)) / (wacc - tgr)) / (1 + wacc) ** years
    equity = ev - net_debt

    # Set the cap to the equity value: that is what MoS == 0 means.
    mcap = equity
    mos = (equity - mcap) / equity
    assert mos == pytest.approx(0.0, abs=1e-12)

    # And the per-share line lands exactly on the traded price at the same moment.
    implied_shares = mcap / price
    assert equity / implied_shares == pytest.approx(price, rel=1e-12)

    # Enterprise value does NOT tie out unless net debt is zero — the whole point.
    if net_debt != 0:
        assert abs(ev - mcap) == pytest.approx(abs(net_debt), rel=1e-9)


def test_price_rescales_the_cap_rather_than_sharing_calcdcfs_listener(index_src):
    """MoS is cap against equity and neither leg carries a price, so a price field
    wired straight to calcDCF would move the per-share line and leave MoS exactly
    where it was — a dead control on the one field a user reaches for to ask "what
    if it were cheaper". A price move is a cap move; the share count stays put."""
    # It must no longer be in the plain forwarding list...
    assert "['dcf-mcap','dcf-netdebt']" in index_src
    # ...and must rescale the cap against a tracked baseline.
    assert "cap * (px / _dcfPriceBase)" in index_src


def test_the_price_baseline_is_seeded_when_a_stock_loads(index_src):
    """Assigning .value fires no input event, so without this the first hand-typed
    price is measured against whatever the previously viewed ticker traded at."""
    body = _code(_fn(index_src, 'fillCalculators'))
    assert 'setDCFPriceBase(' in body


def test_market_cap_raw_is_preferred_over_the_derivation(index_src):
    """shares x price is the fallback for the listings Yahoo gives no cap for, not
    the rule — it is the figure that is 34% wrong on BRK-B."""
    body = _code(_fn(index_src, 'fillCalculators'))
    assert body.index('data.market_cap_raw') < body.index('data.shares * _px')


# ---------------------------------------------------------------------------
# The server fills a cap Yahoo does not report
# ---------------------------------------------------------------------------
#
# The frontend fallback above only works if the payload carries a share count,
# and for these listings it did not: Yahoo omits `sharesOutstanding` from the
# same responses it omits `marketCap` from, so both legs went null together and
# the box read 'N/A' with the margin of safety blank beside it.

def _stock_src():
    """`_do_get_stock` with its # comments stripped.

    Same reason `_code` strips // from the JS: the commentary here quotes the
    expressions it is about, so an unstripped search matches the prose.
    """
    src = inspect.getsource(terminal._do_get_stock)
    return '\n'.join(re.sub(r'#.*$', '', line) for line in src.splitlines())


def test_a_cap_yahoo_does_not_report_is_derived_from_the_share_count():
    """Yahoo omits `marketCap` outright for a stable minority of listings — Home
    Depot and American Eagle among them — from `info` and the v7 quote endpoint
    alike, and omits `sharesOutstanding` with it, so nothing in the response
    derives one. The index tables already fill this from fast_info's count times
    the live price; the detail page had no equivalent."""
    src = _stock_src()
    assert 'fast_shares = _finite(getattr(fi,' in src
    assert 'market_cap_raw = shares_outstanding * float(price)' in src


def test_the_reported_cap_still_wins_over_the_derivation():
    """The BRK-B rule, on the server side. A share count is not always the count
    the cap is built from — B shares alone against a cap covering both classes,
    34% apart — so the derivation may only run where Yahoo reports no cap at
    all, and never as a correction to one it does report."""
    src = _stock_src()
    assert 'if market_cap_raw is None and shares_outstanding and price:' in src
    assert (src.index("market_cap_raw = _finite(info.get('marketCap')")
            < src.index('market_cap_raw = shares_outstanding * float(price)'))


def test_yahoos_own_share_count_is_preferred_over_fast_infos():
    """Same argument one level down: where both exist they are the same number,
    and where they differ `info` is the basis the rest of the payload is built
    against."""
    src   = _stock_src()
    start = src.index('shares_outstanding = (')
    defn  = src[start:src.index('\n\n', start)]
    assert defn.index("info.get('sharesOutstanding')") < defn.index('fast_shares')


def test_the_share_count_is_resolved_in_one_place():
    """It used to be written out three times — the payout-ratio walk, the TTM
    earnings derivation and the payload — reading `info` directly at each site.
    So a listing Yahoo reports no count for lost four unrelated figures at once
    (market cap, price-to-tangible-book, the payout ratios and the TTM earnings
    card) and there was no single place to fix it."""
    src = _stock_src()
    assert src.count("info.get('sharesOutstanding')") == 1
    assert src.count("info.get('impliedSharesOutstanding')") == 1


# ---------------------------------------------------------------------------
# The algebra the two readouts rest on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('equity, mcap, price', [
    (1_500e9, 1_091.762e9, 510.00),   # BRK-B shaped: cap over both share classes
    (20e9,    16.856e9,    6_294.0),  # NVR shaped: 2.68M shares, four-digit price
    (500e6,   1.564e9,     32.28),    # overvalued, negative MoS
])
def test_cap_based_mos_equals_the_per_share_form_it_replaces(equity, mcap, price):
    """The change is not a different answer, it is the same answer reached without
    the share count. With implied shares = cap/price the two forms are identically
    equal, which is why the per-share line can still be shown beside it."""
    implied_shares = mcap / price
    ivps = equity / implied_shares

    per_share = (ivps - price) / ivps
    cap_based = (equity - mcap) / equity

    assert cap_based == pytest.approx(per_share, rel=1e-12)


def test_a_wrong_share_count_no_longer_reaches_the_answer():
    """The regression this exists to stop. Under the old form a share count 34%
    light — BRK-B's B-class count against a two-class cap — moved MoS by 19
    points. The cap-based form does not read the share count at all."""
    equity, mcap, price = 1_500e9, 1_091.762e9, 510.00

    honest = (equity - mcap) / equity

    # What equity/shares produced when `shares` was Yahoo's B-class count.
    b_class_shares = 1_408_035_161
    old_ivps = equity / b_class_shares
    old_mos  = (old_ivps - price) / old_ivps

    assert honest == pytest.approx(0.2722, abs=5e-4)
    assert old_mos == pytest.approx(0.5213, abs=5e-4)
    assert abs(old_mos - honest) > 0.19
