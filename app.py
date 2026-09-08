from flask import (Flask, g, jsonify, redirect, render_template, request,
                   session)
import yfinance as yf
import pandas as pd
import collections
import functools
import math
import os
import sys
import threading
import time as _time_mod
from datetime import (date as _date, datetime as _datetime,
                      timedelta as _timedelta, timezone as _timezone)
from urllib.parse import quote as _url_quote

from groq import Groq

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))

# The Groq key is stored in settings.json (written by POST /api/settings) and
# only sometimes in the environment. Binding the client to os.environ at import
# time meant a key configured through the UI never reached it: every call raised
# on an empty key, groq_call()'s blanket `except` turned that into '', and the
# /api/news headline rewrite silently degraded to sentence-casing the extracted
# summary. Nothing anywhere reported that the LLM path was dead.
#
# So the key is resolved per call and the client rebuilt when it changes. Keying
# the cache on the key itself is what makes a save through POST /api/settings
# take effect on the next call — without the route having to know this state
# exists, and without a restart.
_groq_lock   = threading.Lock()
_groq        = None     # client for _groq_key; None while no key is configured
_groq_key    = None     # the key _groq built with; None means "not yet resolved"
_groq_warned = False    # "no key" is logged once, not once per call


def _groq_client(key):
    """Client for `key`, or None when there isn't one.

    The key is passed in rather than resolved here. API keys are per account
    (see _resolve_api_key), and the news pipeline runs on a worker thread with
    no request context — so the only place that can say whose key this is, is
    the route that started the work. A function reaching for ambient credentials
    from a thread would silently find none, which is the failure mode that hid
    the original import-time-binding bug for so long.

    Still cached on the key itself, so one account's repeated calls reuse a
    client and a key change takes effect on the next call with no restart.
    """
    global _groq, _groq_key
    key = (key or '').strip()
    if not key:
        return None
    with _groq_lock:
        if key != _groq_key:
            _groq     = Groq(api_key=key)
            _groq_key = key
        return _groq


def groq_call(system, user, max_tokens=80, key=''):
    """Single reusable Groq call. Returns stripped text or empty string on failure.

    Both paths still degrade to '' — every caller has a non-LLM fallback — but
    they are logged apart. An absent key is a setup problem that stays true until
    someone fixes it; an exception is a live one. Reporting neither is how this
    went unnoticed, so "no key" is logged once and a failure every time.
    """
    global _groq_warned
    client = _groq_client(key)
    if client is None:
        if not _groq_warned:
            _groq_warned = True
            print('[GROQ] no API key for this account — paste one into Settings; '
                  'LLM features are degraded', flush=True)
        return ''
    try:
        resp = client.chat.completions.create(
            model='llama-3.1-8b-instant',
            messages=[{'role': 'system', 'content': system},
                      {'role': 'user',   'content': user}],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip().strip('"')
    except Exception as e:
        print(f'[GROQ] call failed: {type(e).__name__}: {e}', flush=True)
        return ''


# A money figure is meaningless without the unit it is denominated in, and a
# hardcoded '$' is a wrong answer rather than a missing one: SK hynix files in
# won, so its 189T KRW of revenue rendered as "$189.17T" — a number larger than
# any company on earth has ever billed, and one a reader has no way to spot as
# a unit error. The figures were right; only the symbol lied.
#
# CNY and JPY both use ¥, so CNY is disambiguated rather than left to collide.
# An unmapped currency falls back to its ISO code ('SEK 4.50B'), which is
# unambiguous — never to '$', because a wrong unit reads as a real number.
_CURRENCY_SYMBOLS = {
    'USD': '$',   'CAD': 'C$',  'EUR': '€',   'GBP': '£',   'JPY': '¥',
    'KRW': '₩',   'CNY': 'CN¥', 'TWD': 'NT$', 'HKD': 'HK$', 'AUD': 'A$',
    'NZD': 'NZ$', 'INR': '₹',   'BRL': 'R$',  'MXN': 'Mex$','SGD': 'S$',
    'ILS': '₪',   'ZAR': 'R',   'CHF': 'CHF ','SEK': 'SEK ','NOK': 'NOK ',
    'DKK': 'DKK ','PLN': 'PLN ','TRY': '₺',   'THB': '฿',   'IDR': 'Rp',
}


def _currency_symbol(code):
    """Display prefix for an ISO currency code.

    'GBp' is Yahoo's marker for a pence-quoted London listing — a real unit,
    not a typo for GBP, and 1/100th of it. Mapping it onto '£' would overstate
    every quoted price a hundredfold, so it keeps its own prefix.
    """
    if not code:
        return '$'
    if code == 'GBp':
        return 'p'
    return _CURRENCY_SYMBOLS.get(code.upper(), f'{code.upper()} ')


def format_large_number(value, symbol='$'):
    """Format a money value as <symbol>X.XXT/B/M.

    The sign is pulled out and applied outside the symbol so that a negative
    scales correctly: -4.5e9 renders '-$4.50B', not '$-4500.00M'.

    `symbol` defaults to '$' so that portfolio code — which is always in the
    account's own currency — is unaffected. Anything reading a *filer's*
    statements must pass the currency that filer reports in.
    """
    if value is None:
        return 'N/A'
    sign = '-' if value < 0 else ''
    mag  = abs(value)
    if mag >= 1_000_000_000_000:
        return f"{sign}{symbol}{mag / 1_000_000_000_000:.2f}T"
    elif mag >= 1_000_000_000:
        return f"{sign}{symbol}{mag / 1_000_000_000:.2f}B"
    else:
        return f"{sign}{symbol}{mag / 1_000_000:.2f}M"


def _finite(value):
    """float(value), or None for anything that isn't a real number.

    pandas hands back NaN for a missing cell, and NaN survives an `is None`
    check — it then serialises as a bare `NaN`, which is not valid JSON.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _eps_str(value):
    """EPS as display text: cents, widened only when cents would read as zero."""
    return f"{value:.4f}" if 0 < abs(value) < 0.01 else f"{value:.2f}"


# Tickers only ever contain letters, digits, dot and hyphen (BRK.B, TECK-B.TO).
# Validating against this at every boundary is what makes it safe to interpolate
# a ticker into a path or a command line.
_TICKER_RE = __import__('re').compile(r'^[A-Za-z0-9][A-Za-z0-9.\-]{0,11}$')


def clean_ticker(raw):
    """Return the normalised ticker, or None if it isn't a plausible symbol.

    Anything that isn't a string is refused rather than coerced: a JSON body can
    carry `{"ticker": 123}` or a whole object, and `(raw or '').strip()` raised
    on those — a 500 where the route means to answer 400.
    """
    if not isinstance(raw, str):
        return None
    t = raw.strip().upper()
    return t if _TICKER_RE.match(t) else None



# ---------------------------------------------------------------------------
# Macrotrends
#
# yfinance returns five annual columns, and Yahoo's own fundamentals-timeseries
# endpoint caps at four however wide a window you ask it for, so Macrotrends is
# the only way to chart more than five years. It serves every metric from one
# endpoint in one response shape — hence one scraper for all of them rather than
# one per metric, which is how the same parsing bug came to be copied four times.
# ---------------------------------------------------------------------------

_MT_URL = 'https://www.macrotrends.net/production/stocks/desktop/fundamental_iframe.php'
_MT_UA  = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# How far back to ask for. Unset, the endpoint serves fourteen years — which is
# not a limit on what it holds, only its default, and reads exactly like one.
# Apple's chart page embeds this iframe with `yb=15`, which is how the parameter
# surfaced at all; it is honoured well past that, and 40 reaches Apple's 1987.
#
# The extra years are free: the response is the same ~10KB and the same ~0.15s
# whether it carries fourteen rows or thirty-nine, so there is no reason to ask
# per metric or to tune this per chart. Macrotrends truncates at its own
# coverage rather than padding — NVDA starts at fiscal 1999, the year it listed —
# so a high number costs nothing on a young company.
#
# Verified before raising it: across ten tickers and all five series, every
# value the old request returned is byte-identical in the wider one. `yb` only
# prepends older rows, so it cannot move a year `_mt_check` validates against.
_MT_YEARS_BACK = 40

# Macrotrends quantises the share count to the nearest million, which turns a
# small-cap series into flat runs of identical values. Drop a series that coarse
# rather than charting "no change" where the count actually moved.
_MT_SHARES_MIN = 100_000_000

_MtSeries = collections.namedtuple(
    '_MtSeries',
    'type statement scale places tol_rel tol_abs min_scale absolute positive_only',
    defaults=(None, 0.10, 0.0, 0.0, False, False),
)

# `type` doubles as the Referer slug. Units are NOT consistent between metrics:
# revenue, net income and the share count come in billions, FCF and capex in
# millions, margin and EPS in their own units. Same endpoint, same field,
# different scale — checked per series rather than assumed, because a company
# with $17M of revenue serves `0.017` on the same line Apple serves `416.161`.
_MT_SERIES = {
    'fcf':      _MtSeries('free-cash-flow',                 'cash-flow-statement', 1e6, tol_abs=5e6),
    'capex':    _MtSeries('capital-expenditures',           'cash-flow-statement', 1e6, tol_abs=5e6, absolute=True),
    'margin':   _MtSeries('net-profit-margin',              'income-statement',    1.0, places=2, tol_abs=0.5),
    'shares':   _MtSeries('shares-outstanding',             'income-statement',    1e9, tol_abs=1e6,
                          min_scale=_MT_SHARES_MIN, positive_only=True),
    'revenue':  _MtSeries('revenue',                        'income-statement',    1e9, tol_abs=5e6),
    'earnings': _MtSeries('net-income',                     'income-statement',    1e9, tol_abs=5e6),
    'eps':      _MtSeries('eps-earnings-per-share-diluted', 'income-statement',    1.0, places=4, tol_abs=0.02),
}

# What a stock lookup actually merges. `capex` is scraped only by the debug
# routes — the capex chart is pure yfinance.
_MT_LOOKUP_METRICS = ('fcf', 'margin', 'shares', 'revenue', 'earnings', 'eps')

# Which of those have a quarterly page at all. The split is by statement, not by
# company: every income-statement series serves real quarters on `freq=Q`, and
# neither cash-flow-statement one does — `free-cash-flow` hands back the annual
# payload unchanged and `capital-expenditures` 404s. Checked across ten tickers
# spanning mega-caps, a bank, a small-cap and two recent IPOs; identical every
# time. So FCF and capex go quarterly on yfinance's five or six columns and say
# in the UI why they are short, and nothing here wastes a request discovering
# that again on every lookup.
_MT_QUARTERLY_METRICS = ('revenue', 'earnings', 'margin', 'shares', 'eps')


def _mt_chart_rows(ticker, series_type, statement, timeout=10, freq='A'):
    """The parsed `var chartData` list for one Macrotrends series.

    `freq` is 'A' for annual columns or 'Q' for quarterly ones — the same
    endpoint and the same response shape either way, which is why there is still
    one scraper. `yb` bounds both: at 40 it reaches 157 quarters for Apple, back
    to 1987, and Macrotrends truncates at its own coverage rather than padding.

    Raises on a transport or parse failure, and returns [] only when the page
    carries a genuinely empty series. The caller caches this, and caching a
    transient blank would pin a chart to five years for the whole TTL — the same
    distinction `_fetch_div_events` draws for the same reason.
    """
    import requests as req
    import re, json

    base = ticker.split('.')[0].upper()
    r = req.get(
        _MT_URL,
        params={'t': base, 'type': series_type, 'statement': statement,
                'freq': freq, 'sub': '', 'yb': _MT_YEARS_BACK},
        headers={'User-Agent': _MT_UA,
                 'Referer': f'https://www.macrotrends.net/stocks/charts/{base.lower()}/stock/{series_type}'},
        timeout=timeout,
    )
    r.raise_for_status()
    match = re.search(r'var\s+chartData\s*=\s*(\[.*?\])\s*[;\n]', r.text, re.DOTALL)
    if match is None:
        raise ValueError(f'no chartData in the Macrotrends {series_type} page for {base}')
    return json.loads(match.group(1))


def scrape_macrotrends(ticker, metric, timeout=10, freq='A'):
    """{'YYYY-MM-DD': value} in base units, for one key of `_MT_SERIES`.

    Keyed by period-end date rather than by year. Macrotrends dates a column the
    same way yfinance does — Walmart's fiscal 2025 is '2026-01-31' to both — so
    the caller re-keys it with `_mt_years` (or `_mt_quarters`) under the same
    rule as the series it is merging into. That is the only thing that makes the
    two line up.

    Reads **v2**, and v2 is the labelled period's own value at BOTH frequencies —
    which is the only reason one parser serves both. The other two fields are not
    stable across `freq`, so do not reach for them:

      annual     v1 = the prior fiscal year, v3 = the change from v1 to v2
      quarterly  v1 = the TRAILING TWELVE MONTHS, v3 = the change on the same
                 quarter a year earlier

    v1 was measured, not assumed: the sum of each row's trailing four v2 equals
    its v1 to the last digit on every quarter checked. Reading v1 here on the
    annual path shifted every scraped bar back a year once already — Apple's 2020
    free cash flow read $58.90B, which is what it earned in 2019 — and the same
    read on the quarterly path would put a TTM figure on a quarter's bar, roughly
    four times too large and rising smoothly where the real series is seasonal.
    """
    spec = _MT_SERIES[metric]
    out  = {}
    for row in _mt_chart_rows(ticker, spec.type, spec.statement, timeout, freq=freq):
        date_str = str(row.get('date') or '')
        if len(date_str) < 10:
            continue
        try:
            value = float(row.get('v2', 0) or 0) * spec.scale
        except (ValueError, TypeError):
            continue
        if spec.absolute:
            value = abs(value)
        if spec.positive_only and value <= 0:
            continue
        out[date_str] = round(value, spec.places) if spec.places is not None else value
    if freq == 'Q':
        _mt_assert_quarterly(out, ticker, metric)
    return out


def _mt_assert_quarterly(by_date, ticker, metric):
    """Raise unless `by_date` really does step by quarters.

    Asking this endpoint for a cash-flow-statement series by quarter does not
    fail. `free-cash-flow` with freq=Q returns the **annual** payload byte for
    byte — thirty-nine rows twelve months apart — and only `capital-expenditures`
    has the decency to 404. Measured across ten tickers, that split is a property
    of the statement, not of the company, which is why `_MT_QUARTERLY_METRICS`
    simply does not ask for those two.

    This is the backstop for the other five. The failure it exists to catch is
    silent and total: forty annual bars relabelled Q1..Q4 draw a company that
    grew for four decades without one down quarter, and every number on it is
    real, so nothing downstream — not the merge gate, not the chart, not a
    reader — has any way to notice. Raising also keeps it out of `_TtlCache`,
    which caches returns and not exceptions, so a Macrotrends change that broke
    one series would not pin it broken for six hours.
    """
    dates = sorted(by_date)
    if len(dates) < 3:
        # Too short to judge, and too short to chart. A genuine quarterly series
        # is 31 rows for the youngest company checked.
        raise ValueError(f'Macrotrends returned {len(dates)} quarterly rows for '
                         f'{ticker} {metric}; expected a series')
    gaps = []
    for a, b in zip(dates, dates[1:]):
        gaps.append((int(b[:4]) - int(a[:4])) * 12 + (int(b[5:7]) - int(a[5:7])))
    gaps.sort()
    median = gaps[len(gaps) // 2]
    # Quarterly steps 3; annual steps 12. Five leaves room for a filer that skips
    # or restates a period without admitting a twelve-month series.
    if median > 5:
        raise ValueError(f'Macrotrends served an annual series for {ticker} '
                         f'{metric} under freq=Q ({median}-month median step)')


def _mt_years(mt_by_date, fiscal=True):
    """Re-key a `scrape_macrotrends` result from period-end date to year.

    Buckets with `_fiscal_year`, the same rule the statement frames go through,
    so a January close lands on the same label from both sources.
    """
    out = {}
    for date_str, value in (mt_by_date or {}).items():
        try:
            d = _date(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]))
        except (TypeError, ValueError):
            continue
        out[_fiscal_year(d) if fiscal else d.year] = value
    return out


def _quarter_key(ts):
    """The merge key for one quarterly column: 'YYYY-MM' of its period end.

    The month and not the full date, deliberately. Both sources normalise a
    quarter end to month end today — checked on COST, TGT, NKE, CSCO and DE,
    every one a 52/53-week filer whose quarters genuinely end on a weekday, and
    the two agree on all five to seven columns for each. Keying on the day would
    stake the whole feature on that normalisation continuing to match on both
    sides at once, and the failure if it ever stopped is silent rather than
    loud: the overlap falls to zero, `_mt_check` sees no evidence either way and
    drops every series, and the charts quietly shorten to five bars with nothing
    on screen saying why. Nothing downstream wants the day.
    """
    return f'{ts.year:04d}-{ts.month:02d}'


def _mt_quarters(mt_by_date):
    """Re-key a quarterly `scrape_macrotrends` result from period-end date to
    `_quarter_key`, so it lines up with the yfinance quarterly frame."""
    out = {}
    for date_str, value in (mt_by_date or {}).items():
        try:
            d = _date(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]))
        except (TypeError, ValueError):
            continue
        out[_quarter_key(d)] = value
    return out


def _fiscal_quarter(year, month, fye_month):
    """(fiscal_year, quarter_number) for a period ending in `month` of `year`.

    `fye_month` is the month the company's fiscal year ends, read off the annual
    frame where every column shares it.

    Not a reuse of `_fiscal_year`, and it cannot be one: that rule buckets a
    whole year on a fixed April cut, which is right for an annual column and
    wrong for three quarters in four of any non-calendar filer. Apple's FY2026
    runs October 2025 to September 2026, so its December 2025 quarter is FY2026
    Q1 — `_fiscal_year` calls that 2025 and would file it a year early, next to
    a bar it precedes.
    """
    fy = year if month <= fye_month else year + 1
    q  = ((month - fye_month - 1) % 12) // 3 + 1
    return fy, q


def _quarter_label(qkey, fye_month):
    """A quarterly bar's axis label: "Q3 '26"."""
    try:
        year, month = int(qkey[:4]), int(qkey[5:7])
    except (TypeError, ValueError):
        return str(qkey)
    fy, q = _fiscal_quarter(year, month, fye_month)
    return f"Q{q} '{fy % 100:02d}"


def _mt_check(mt_by_year, yf_by_year, spec, min_overlap=2, max_bad=None):
    """Whether a whole Macrotrends series agrees with yfinance well enough to use.

    Taken or dropped whole. A wrong stretch spliced onto a right one is
    indistinguishable from real data once it is a bar on a chart, which is the
    same reason `min_scale` drops a quantised share count rather than charting
    its flat runs.

    The tolerance is deliberately loose. This catches gross errors — a different
    company under the same symbol, a thousandfold unit slip, a split-adjusted
    series pasted onto an as-filed one, the year shift that reading v1 caused —
    it is not a reconciliation. yfinance carries restated figures where
    Macrotrends is as-reported, and Macrotrends' net income line is
    attributable-to-parent where yfinance's `Net Income` row is not, so small
    disagreements are normal and one bad year is allowed before the series goes.

    Measured over a fifteen-ticker basket spanning banks, January filers, recent
    splits and loss-makers, this accepts 44 of 45 series. What an accepted series
    adds is bounded by `_MT_YEARS_BACK` and then by the company's own age: 35
    years for Apple or Coca-Cola, 24 for NVIDIA, which listed in 1999.

    Rejections are all the same shape — a source that disagrees by a steady
    offset rather than at one year. Amazon's free cash flow runs 12-31% above
    yfinance's on all four overlapping years because the two net capital leases
    differently, and Exxon's runs 11-14% above on three of four. Those are the
    cases worth rejecting: splicing the older years on would put a definitional
    step change mid-chart and read as a real swing in the business.

    Note the asymmetry this leaves, and that widening the window deepened: the
    gate only ever sees the four or five years yfinance also carries, so it
    validates the series *as a whole* on its most recent tail and admits
    everything behind it on that evidence. That holds up because the errors it
    exists to catch — wrong company, unit slip, year shift, a definitional gap
    like Amazon's — are properties of the whole series and show up on any
    overlap. A one-off bad year deep in the unvalidated past would not be caught,
    and never was.
    """
    overlap = sorted(set(mt_by_year) & set(yf_by_year))
    bad     = []
    for yr in overlap:
        a, b = _finite(yf_by_year[yr]), _finite(mt_by_year[yr])
        if a is None or b is None:
            continue
        # Symmetric: a near-zero yfinance value paired with a huge Macrotrends
        # one has to fail, and scaling the limit by `a` alone would let it pass.
        if abs(a - b) > max(spec.tol_abs, spec.tol_rel * max(abs(a), abs(b))):
            bad.append({'year': yr, 'yf': a, 'mt': b})
    # One divergent year among four or five is a restatement worth tolerating.
    # One among two is half the evidence there is, so it isn't.
    if max_bad is None:
        max_bad = 1 if len(overlap) >= 3 else 0
    biggest  = max((abs(v) for v in mt_by_year.values()), default=0.0)
    scale_ok = biggest >= spec.min_scale
    return {
        'ok':       bool(mt_by_year) and scale_ok and len(overlap) >= min_overlap and len(bad) <= max_bad,
        'overlap':  len(overlap),
        'bad':      bad,
        'scale_ok': scale_ok,
    }


def _merge_macrotrends(yf_by_year, mt_by_year, metric, min_overlap=2, tol_rel=None):
    """Fill the years yfinance doesn't reach from Macrotrends.

    Returns (merged, src, report). A Macrotrends year never overwrites a yfinance
    one, and every year carries a 'yf' or 'mt' tag so the chart can fade the
    scraped ones. `report` is for the debug route: a rejected series is simply
    absent from the payload, which is otherwise indistinguishable from a ticker
    Macrotrends has never heard of.
    """
    spec = _MT_SERIES[metric]
    if tol_rel is not None:
        spec = spec._replace(tol_rel=tol_rel)
    merged = dict(yf_by_year)
    src    = {yr: 'yf' for yr in yf_by_year}
    report = _mt_check(mt_by_year or {}, yf_by_year, spec, min_overlap)
    if report['ok']:
        for yr, value in (mt_by_year or {}).items():
            if yr not in merged:
                merged[yr] = value
                src[yr]    = 'mt'
    elif mt_by_year:
        # Rejecting drops the chart back to yfinance's five years, which looks
        # exactly like a ticker Macrotrends does not carry. Say which it was.
        why = ('quantised below the resolution floor' if not report['scale_ok']
               else f"only {report['overlap']} overlapping years" if report['overlap'] < min_overlap
               else 'disagrees with yfinance on ' + ', '.join(
                   f"{b['year']} (yf {b['yf']:.4g} vs mt {b['mt']:.4g})" for b in report['bad']))
        print(f'[MT] dropped {metric}: {why}', flush=True)
    return merged, src, report


# Cache canada drops so we don't re-fetch on every batch request
_tape_cache = {'data': [], 'ts': 0}
TAPE_BATCH_SIZE = 15

# Large list of TSX-listed tickers to pull quotes for
CA_TICKERS = [
    'RY.TO','TD.TO','BNS.TO','BMO.TO','CM.TO','NA.TO','MFC.TO','SLF.TO','GWO.TO','IAG.TO',
    'CNR.TO','CP.TO','CNQ.TO','SU.TO','ENB.TO','TRP.TO','CVE.TO','MEG.TO','PEY.TO','ARX.TO',
    'ATD.TO','L.TO','MRU.TO','WN.TO','EMP-A.TO','DOL.TO','CTC-A.TO','GIL.TO','PIF.TO','QSR.TO',
    'BCE.TO','T.TO','RCI-B.TO','SHOP.TO','CSU.TO','OTEX.TO','BB.TO','KXS.TO','DSG.TO','ENGH.TO',
    'ABX.TO','AEM.TO','K.TO','FNV.TO','WPM.TO','FM.TO','TECK-B.TO','LUN.TO','CS.TO','HBM.TO',
    'BAM.TO','BIP-UN.TO','BEP-UN.TO','BPY-UN.TO','IFC.TO','ELF.TO','FFH.TO','POW.TO','SFC.TO','BRW.TO',
    'SNC.TO','WSP.TO','STN.TO','ATA.TO','BDT.TO','TF.TO','CAE.TO','HII.TO','NFI.TO','MDA.TO',
    'NTR.TO','AGU.TO','VET.TO','PSK.TO','PKI.TO','TPX-B.TO','SAP.TO','WFG.TO','IFP.TO','CFP.TO',
    'H.TO','CHP-UN.TO','REI-UN.TO','SRU-UN.TO','CRT-UN.TO','DIR-UN.TO','AP-UN.TO','GRT-UN.TO','KMP-UN.TO','NWH-UN.TO',
    'PZA.TO','MTY.TO','BPF-UN.TO','ACO-X.TO','MFI.TO','TIH.TO','GFL.TO','BYD.TO','RBA.TO','MG.TO',
]

# Static fallback shown immediately while live data loads
TAPE_STATIC = [
    {'ticker':'RY','full_ticker':'RY.TO'},{'ticker':'TD','full_ticker':'TD.TO'},
    {'ticker':'BNS','full_ticker':'BNS.TO'},{'ticker':'BMO','full_ticker':'BMO.TO'},
    {'ticker':'CNR','full_ticker':'CNR.TO'},{'ticker':'ENB','full_ticker':'ENB.TO'},
    {'ticker':'SU','full_ticker':'SU.TO'},{'ticker':'SHOP','full_ticker':'SHOP.TO'},
    {'ticker':'CP','full_ticker':'CP.TO'},{'ticker':'BCE','full_ticker':'BCE.TO'},
    {'ticker':'CNQ','full_ticker':'CNQ.TO'},{'ticker':'ABX','full_ticker':'ABX.TO'},
    {'ticker':'MFC','full_ticker':'MFC.TO'},{'ticker':'ATD','full_ticker':'ATD.TO'},
    {'ticker':'TRP','full_ticker':'TRP.TO'},
]

class _MtScrapes:
    """Macrotrends futures read back against one shared, absolute deadline.

    Per-key timeouts compound: five reads at twelve seconds each is a minute
    against a twenty-five second route deadline. Every scrape started at the same
    moment, so the budget runs from when they were submitted rather than from
    whenever the first result is asked for.
    """

    def __init__(self, futures, budget=14):
        import time as _time
        self._futures  = futures
        self._deadline = _time.monotonic() + budget

    def get(self, key):
        import time as _time
        fut = self._futures.get(key)
        if fut is None:
            return {}
        try:
            return fut.result(timeout=max(0.0, self._deadline - _time.monotonic())) or {}
        except Exception:
            return {}


def _start_macrotrends(tkkr):
    """Kick off every Macrotrends scrape a lookup needs, concurrently.

    Each scrape carries a 10-second timeout. Run one after another they cost more
    than the whole 25-second route deadline; run together they cost one.
    Tickers with a dot are not on Macrotrends, so nothing is started for them.

    Returns an `_MtScrapes` — read it with `_mt_result`.
    """
    from concurrent.futures import ThreadPoolExecutor

    if '.' in tkkr:
        return _MtScrapes({})

    ex = ThreadPoolExecutor(max_workers=len(_MT_LOOKUP_METRICS))
    try:
        return _MtScrapes({m: ex.submit(_mt_cached, tkkr, m) for m in _MT_LOOKUP_METRICS})
    finally:
        # Threads already submitted keep running; this just releases the pool
        # once they finish instead of blocking here.
        ex.shutdown(wait=False)


def _mt_result(scrapes, key):
    """The scrape result for `key`, or {} if it failed or was never started."""
    return scrapes.get(key) if scrapes is not None else {}


@app.route('/')
def index():
    # The CSRF token is rendered into the page rather than fetched, so the
    # frontend's fetch wrapper has it before the first request goes out. Asking
    # /api/auth/me for it would race every call that fires on page load.
    user = _current_user() or {}
    html = render_template('index.html',
                           csrf_token=session.get('csrf', ''),
                           username=user.get('username', ''),
                           is_admin=(user.get('role') == 'admin'),
                           is_guest=_is_guest(user))
    resp = app.make_response(html)
    # The shell carries the CSRF token and the signed-in username, so it must not
    # sit in the browser cache after sign-out. Observed: after switching accounts
    # the previous user's page came back from cache with their name still in it —
    # every data call 401'd and bounced to the login form, but the stale name and
    # token should not have been there to begin with.
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    return resp


_NAME_SPLIT_RE = __import__('re').compile(r'[^a-z0-9]+')

# Yahoo's search returns a company's preferred series with the same quoteType
# and the *same* longname as its common shares — 'BCE Inc.' is the longname of
# BCE-PZ.TO just as much as of BCE.TO. Only the shortname gives them away, and
# it is truncated at 30 characters ('BROOKFIELD RENEWABLE LP PREF SE'), so
# match on tokens rather than phrases.
_NOT_COMMON_TOKENS = {
    'pr', 'prf', 'pfd', 'pref', 'prefd', 'preferred', 'ser', 'series',
    'warrant', 'warrants', 'wt', 'wts', 'right', 'rights', 'rt',
    'debenture', 'debentures', 'note', 'notes', 'cvr',
}

# Legal form and filler carry no identity: 'Enbridge Inc' and 'Enbridge Inc.'
# are one company. Dropping them is also what keeps 'Bank of Montreal' from
# looking like 'Royal Bank of Canada'.
_NAME_NOISE_TOKENS = {
    'the', 'and', 'of', 'inc', 'incorporated', 'corp', 'corporation', 'co',
    'company', 'companies', 'ltd', 'limited', 'plc', 'llc', 'lp', 'llp',
    'nv', 'sa', 'ag', 'se', 'spa', 'ab', 'as', 'asa', 'oyj', 'class', 'cl',
}

# A depositary receipt keeps the issuer's name and wraps boilerplate around it:
# 'APPLE INC CEDEAR(REPR 1/20 SHR)' is Apple on Buenos Aires.
_RECEIPT_TOKENS = {
    'adr', 'adrs', 'ads', 'gdr', 'cdr', 'bdr', 'cedear', 'cedears', 'drc',
    'repr', 'representing', 'sponsored', 'unsponsored', 'sponsrd', 'shs',
    'shr', 'shrs', 'share', 'shares', 'ord', 'ordinary', 'common', 'reg',
    'registered', 'bearer', 'br', 'each', 'ratio', 'new',
}

# The '-PZ' of BCE-PZ.TO, the '-PA' of BEP-PA: a class suffix, which a plain
# common listing does not carry.
_CLASS_SUFFIX_RE = __import__('re').compile(r'^[A-Z0-9]+-[A-Z0-9]{1,3}(\.[A-Z]+)?$')


def _name_tokens(name):
    """The identity-carrying words of a company name, lowercased."""
    return [w for w in _NAME_SPLIT_RE.split((name or '').lower())
            if w and w not in _NAME_NOISE_TOKENS]


def _is_common_share(symbol, longname, shortname):
    """False when this row is a preferred, warrant or note rather than the
    common share.

    A marker in the longname settles it. A marker in the shortname does not:
    Yahoo files the common NYSE listing of BNS under the shortname
    'Bank Nova Scotia Halifax Pfd 3', so that only counts against a symbol
    carrying a class suffix as well. Units are deliberately absent from the
    token list — an LP's units *are* its common equity (BEP-UN.TO).
    """
    if any(w in _NOT_COMMON_TOKENS for w in _name_tokens(longname)):
        return False
    if _CLASS_SUFFIX_RE.match(symbol or ''):
        return not any(w in _NOT_COMMON_TOKENS for w in _name_tokens(shortname))
    return True


def _is_same_issuer(subject, *names):
    """True when one of `names` names the issuer whose tokens are `subject`.

    Every identifying word of the subject has to be present and anything extra
    has to be depositary-receipt boilerplate, so 'Apple Inc' matches
    'APPLE INC CEDEAR(REPR 1/20 SHR)' but not 'Apple Hospitality REIT'.
    """
    if not subject:
        return False
    for n in names:
        cand = set(_name_tokens(n))
        if not cand or not subject <= cand:
            continue
        if all(w in _RECEIPT_TOKENS or w.isdigit() for w in cand - subject):
            return True
    return False


@app.route('/api/crosslist', methods=['GET'])
def crosslist():
    """Find the same company listed on other exchanges."""
    tkkr = clean_ticker(request.args.get('ticker', ''))
    if not tkkr:
        return jsonify([])
    try:
        import requests as req
        # Search Yahoo for the company name to find cross-listings
        t = yf.Ticker(tkkr)
        info = t.info
        name = info.get('longName') or info.get('shortName', '')
        if not name:
            return jsonify([])

        # Use Yahoo search to find related listings. It returns at most seven
        # quotes however high quotesCount goes, and a company's preferred
        # series compete for those seven slots — so the filtering below has to
        # earn its keep, there is no larger pool to fall back on.
        url = 'https://query2.finance.yahoo.com/v1/finance/search'
        params = {'q': name, 'quotesCount': 10, 'newsCount': 0}
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = req.get(url, params=params, headers=headers, timeout=5)
        results = r.json().get('quotes', [])

        current_exchange = (info.get('exchange') or '').upper()
        if not current_exchange:
            # info didn't say, but the search saw the symbol we were asked for
            current_exchange = next(
                ((q.get('exchange') or '').upper() for q in results
                 if (q.get('symbol') or '').upper() == tkkr), '')

        subject        = set(_name_tokens(name))
        listings       = []
        seen_symbols   = {tkkr}
        seen_exchanges = {current_exchange} if current_exchange else set()

        for item in results:
            sym       = (item.get('symbol') or '').upper()
            exch_code = (item.get('exchange') or '').upper()

            if item.get('quoteType') != 'EQUITY' or not sym or not exch_code:
                continue
            if sym in seen_symbols:
                continue
            # "Other exchanges" means a *different* exchange. A preferred
            # series or a second share class sits on the same one, and a second
            # hit on an exchange already taken is another security, not another
            # listing of this one.
            if exch_code in seen_exchanges:
                continue

            longname  = item.get('longname') or ''
            shortname = item.get('shortname') or ''
            if not _is_common_share(sym, longname, shortname):
                continue
            if not _is_same_issuer(subject, longname, shortname):
                continue

            seen_symbols.add(sym)
            seen_exchanges.add(exch_code)
            listings.append({
                'ticker':        sym,
                'exchange':      item.get('exchDisp') or exch_code,
                'exchange_code': exch_code,
                'current':       False,
            })

        # The switcher is a way to reach another listing, so a hidden one is
        # dropped like any other offer. Filtered before the current listing is
        # prepended: that row is the page you are already on, and /api/stock
        # would have refused it if it were hidden.
        listings = _drop_blocked(listings)

        # Add current listing first
        listings.insert(0, {
            'ticker':        tkkr,
            'exchange':      info.get('fullExchangeName') or current_exchange,
            'exchange_code': current_exchange,
            'current':       True,
        })

        return jsonify(listings[:15])  # max 15 listings
    except Exception:
        return jsonify([{'ticker': tkkr, 'exchange': '', 'current': True}])


@app.route('/api/quote', methods=['GET'])
def single_quote():
    """Returns live price and daily change for a single ticker."""
    sym = request.args.get('ticker', '').strip().upper()
    if not sym:
        return jsonify({'error': 'No ticker'}), 400
    try:
        t  = yf.Ticker(sym)
        fi = t.fast_info
        last = getattr(fi, 'last_price', None)
        prev = getattr(fi, 'previous_close', None)
        if last and prev and prev != 0:
            chg = round(((last - prev) / prev) * 100, 2)
            return jsonify({'price': round(float(last), 2), 'change': chg})
        return jsonify({'price': None, 'change': None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _fast_quote(sym):
    """(price, change_pct) for one symbol, or (None, None)."""
    try:
        fi   = yf.Ticker(sym).fast_info
        last = getattr(fi, 'last_price', None)
        prev = getattr(fi, 'previous_close', None)
        if last and prev and float(prev) != 0:
            return round(float(last), 2), round((float(last) - float(prev)) / float(prev) * 100, 2)
    except Exception:
        pass
    return None, None


# Symbols per /api/quotes call. The scrolling tape used to fire one /api/quote
# per symbol as each item came into view — ~100 requests that saturated the
# browser's 6-connections-per-host limit and queued real lookups behind them.
MAX_BATCH_QUOTES = 40


@app.route('/api/quotes', methods=['GET'])
def batch_quotes():
    """Live price + daily change for many symbols in one request.

    Returns {symbol: {price, change}}. Symbols that fail are simply absent —
    the caller keeps whatever it already had rather than blanking the row.
    """
    from concurrent.futures import ThreadPoolExecutor

    raw = (request.args.get('tickers') or '').split(',')
    syms, seen = [], set()
    for r in raw:
        s = clean_ticker(r)
        if s and s not in seen:
            seen.add(s)
            syms.append(s)
    syms = syms[:MAX_BATCH_QUOTES]
    if not syms:
        return jsonify({})

    out = {}
    with ThreadPoolExecutor(max_workers=min(12, len(syms))) as ex:
        for sym, (price, chg) in zip(syms, ex.map(_fast_quote, syms)):
            if price is not None:
                out[sym] = {'price': price, 'change': chg}
    return jsonify(out)


@app.route('/api/tape', methods=['GET'])
def ticker_tape():
    """Returns Canadian stocks sorted by daily % drop, in batches."""
    import time
    try:
        batch = int(request.args.get('batch', 0))
        static = request.args.get('static') == '1'

        # Static mode — return placeholder immediately with no prices
        if static:
            return jsonify({
                'items': [{'ticker': s['ticker'], 'full_ticker': s['full_ticker'],
                           'price': 0, 'change': 0, 'loading': True}
                          for s in _drop_blocked(TAPE_STATIC)],
                'has_more': False, 'next_batch': 0, 'total_batches': 1,
            })

        if not _tape_cache['data'] or time.time() - _tape_cache['ts'] > 300:
            items = []
            try:
                # Bulk fetch — much faster than one-by-one
                symbols_str = ' '.join(CA_TICKERS)
                bulk = yf.download(
                    tickers=symbols_str,
                    period='2d',
                    interval='1d',
                    group_by='ticker',
                    auto_adjust=True,
                    progress=False,
                    threads=True,
                )
                for sym in CA_TICKERS:
                    try:
                        if sym in bulk.columns.get_level_values(0):
                            closes = bulk[sym]['Close'].dropna()
                        else:
                            closes = bulk['Close'][sym].dropna() if 'Close' in bulk else None
                        if closes is None or len(closes) < 2:
                            continue
                        prev  = float(closes.iloc[-2])
                        last  = float(closes.iloc[-1])
                        if prev == 0:
                            continue
                        chg = round(((last - prev) / prev) * 100, 2)
                        items.append({
                            'ticker':      sym.replace('.TO', ''),
                            'full_ticker': sym,
                            'price':       round(last, 2),
                            'change':      chg,
                        })
                    except Exception:
                        pass
            except Exception:
                # Fallback to one-by-one if bulk fails
                for sym in CA_TICKERS:
                    try:
                        t = yf.Ticker(sym)
                        fi = t.fast_info
                        last = getattr(fi, 'last_price', None)
                        prev = getattr(fi, 'previous_close', None)
                        if last and prev and prev != 0:
                            chg = round(((last - prev) / prev) * 100, 2)
                            items.append({
                                'ticker':      sym.replace('.TO', ''),
                                'full_ticker': sym,
                                'price':       round(float(last), 2),
                                'change':      chg,
                            })
                    except Exception:
                        pass

            _tape_cache['data'] = sorted(items, key=lambda x: x['change'])
            _tape_cache['ts'] = time.time()

        # Same rule as the movers cache above: _tape_cache is shared market data
        # built once, and the hidden names are removed per reader. Before the
        # batch arithmetic, so has_more and total_batches describe what this
        # account will actually be sent.
        all_items = _drop_blocked(_tape_cache['data'])
        total_batches = max(1, -(-len(all_items) // TAPE_BATCH_SIZE))
        start = batch * TAPE_BATCH_SIZE
        end   = start + TAPE_BATCH_SIZE

        return jsonify({
            'items': all_items[start:end],
            'has_more': end < len(all_items),
            'next_batch': batch + 1,
            'total_batches': total_batches,
        })
    except Exception as e:
        return jsonify({'items': [], 'has_more': False, 'next_batch': 0, 'error': str(e)})


@app.route('/api/screener/canada-drops', methods=['GET'])
def canada_drops():
    try:
        import requests as req
        url = 'https://query2.finance.yahoo.com/v1/finance/screener'
        payload = {
            "size": 100,
            "offset": 0,
            "sortField": "regularMarketChangePercent",
            "sortType": "ASC",
            "quoteType": "EQUITY",
            "query": {
                "operator": "AND",
                "operands": [
                    {"operator": "eq", "operands": ["exchange", "TOR"]},
                    {"operator": "gt", "operands": ["regularMarketVolume", 10000]}
                ]
            },
            "userId": "",
            "userIdType": "guid"
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/json',
        }
        r = req.post(url, json=payload, headers=headers, timeout=10)
        data = r.json()
        quotes = data.get('finance', {}).get('result', [{}])[0].get('quotes', [])
        results = []
        for q in quotes:
            chg = q.get('regularMarketChangePercent', 0) or 0
            results.append({
                'ticker':  q.get('symbol', ''),
                'name':    q.get('longName') or q.get('shortName', ''),
                'price':   round(float(q.get('regularMarketPrice', 0) or 0), 2),
                'change':  round(float(chg), 2),
                'volume':  q.get('regularMarketVolume', 0),
                # A screen spans exchanges, so the rows are in mixed currencies
                # — the quote says which, and every one of them is not '$'.
                'mkt_cap': format_large_number(
                    q.get('marketCap'), _currency_symbol(q.get('currency'))),
            })
        results = _drop_blocked(results)
        return jsonify({'results': results, 'count': len(results)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ── Daily Movers ──────────────────────────────────────────────────────────────
import threading as _threading
from yfinance import EquityQuery as _EQ

_movers_cache = {
    'TSX':    {'gainers': [], 'losers': [], 'ts': 0},
    'NYSE':   {'gainers': [], 'losers': [], 'ts': 0},
    'NASDAQ': {'gainers': [], 'losers': [], 'ts': 0},
}
MOVERS_TTL = 300

_EXCHANGE_CODE = {'TSX': 'TOR', 'NYSE': 'NYQ', 'NASDAQ': 'NMS'}
# Min market cap / volume to filter out illiquid micro-caps
_EXCHANGE_FILTERS = {
    'TSX':    (100_000_000, 50_000),
    'NYSE':   (500_000_000, 100_000),
    'NASDAQ': (500_000_000, 100_000),
}


def _screen_movers(exchange, asc, count=100):
    """Return top movers for an exchange via yfinance screener. asc=True → losers.
    Paginates in batches of 25 (API maximum) until count is reached."""
    ex_code = _EXCHANGE_CODE.get(exchange, 'NYQ')
    min_cap, min_vol = _EXCHANGE_FILTERS.get(exchange, (500_000_000, 100_000))
    query = _EQ('AND', [
        _EQ('eq',  ['exchange', ex_code]),
        _EQ('gte', ['intradaymarketcap', min_cap]),
        _EQ('gte', ['dayvolume', min_vol]),
    ])
    PAGE = 25
    all_quotes = []
    for offset in range(0, count, PAGE):
        try:
            result = yf.screen(query, sortField='percentchange', sortAsc=asc,
                               count=PAGE, offset=offset)
            page = result.get('quotes', []) if result else []
            all_quotes.extend(page)
            if len(page) < PAGE:
                break  # no more results
        except Exception:
            break
    return all_quotes


def _quotes_to_items(quotes, exchange):
    items = []
    for q in quotes:
        sym = q.get('symbol', '')
        if not sym:
            continue
        display = sym.replace('.TO', '').replace('-', '.')
        items.append({
            'ticker':      display,
            'full_ticker': sym,
            'name':        q.get('longName') or q.get('shortName') or q.get('displayName') or display,
            'price':       round(float(q.get('regularMarketPrice', 0)), 2),
            'change':      round(float(q.get('regularMarketChangePercent', 0)), 2),
            'volume':      q.get('regularMarketVolume', 0),
            # The TSX tables are in CAD; only the US ones are in dollars.
            'mkt_cap':     format_large_number(
                q.get('marketCap'), _currency_symbol(q.get('currency'))),
            'exchange':    exchange,
            'enriched':    True,
        })
    return items


def _refresh_movers(exchange):
    import time

    raw_gainers, raw_losers = [], []
    tg = _threading.Thread(target=lambda: raw_gainers.extend(_screen_movers(exchange, asc=False, count=100)), daemon=True)
    tl = _threading.Thread(target=lambda: raw_losers.extend(_screen_movers(exchange, asc=True,  count=100)), daemon=True)
    tg.start(); tl.start()
    tg.join(timeout=30); tl.join(timeout=30)

    gainers = sorted([i for i in _quotes_to_items(raw_gainers, exchange) if i['change'] > 0], key=lambda x: x['change'], reverse=True)
    losers  = sorted([i for i in _quotes_to_items(raw_losers,  exchange) if i['change'] < 0], key=lambda x: x['change'])

    if not gainers and not losers:
        return

    _movers_cache[exchange]['gainers'] = gainers
    _movers_cache[exchange]['losers']  = losers
    _movers_cache[exchange]['ts']      = time.time()


@app.route('/api/movers', methods=['GET'])
def movers():
    import time
    exchange  = request.args.get('exchange', 'TSX').upper()
    direction = request.args.get('direction', 'gainers').lower()
    limit     = min(int(request.args.get('limit', 25)), 100)
    if exchange not in _movers_cache:
        exchange = 'TSX'
    cache = _movers_cache[exchange]
    if not cache['gainers'] and not cache['losers'] or time.time() - cache['ts'] > MOVERS_TTL:
        _refresh_movers(exchange)
    # Hidden names come out here rather than in _refresh_movers: the cache is
    # market data shared by every account, so it is built once and filtered per
    # reader. Filtered before the slice, so hiding three names does not leave a
    # 22-row "top 25".
    results = _drop_blocked(cache[direction])[:limit]
    return jsonify({
        'results':   results,
        'count':     len(results),
        'exchange':  exchange,
        'direction': direction,
        'enriched':  True,
    })


@app.route('/api/debug/dividend', methods=['GET'])
def debug_dividend():
    tkkr = request.args.get('ticker', 'AAPL').strip().upper()
    t = yf.Ticker(tkkr)
    info = t.info
    return jsonify({
        'dividendYield':               info.get('dividendYield'),
        'trailingAnnualDividendYield': info.get('trailingAnnualDividendYield'),
        'dividendRate':                info.get('dividendRate'),
        'trailingAnnualDividendRate':  info.get('trailingAnnualDividendRate'),
    })


@app.route('/api/debug/cashflow', methods=['GET'])
def debug_cashflow():
    tkkr = request.args.get('ticker', '').strip().upper()
    if not tkkr:
        return jsonify({'error': 'No ticker'}), 400
    try:
        t = yf.Ticker(tkkr)
        cf = t.cashflow
        return jsonify({
            'rows_available': cf.index.tolist() if not cf.empty else [],
            'columns': [str(c) for c in cf.columns.tolist()] if not cf.empty else [],
            'empty': cf.empty,
        })
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/debug/macrotrends', methods=['GET'])
def debug_macrotrends():
    """Every Macrotrends series for a ticker, keyed by period-end date.

    Dates rather than years because that is what the scraper returns and what
    makes a fiscal-year misalignment visible; `_mt_years` is what collapses them.
    A series the gate rejected is simply absent from /api/stock, which looks the
    same as a ticker Macrotrends has never heard of — the reject reason is logged
    by `_merge_macrotrends`, and this is where you check the data behind it.
    """
    from concurrent.futures import ThreadPoolExecutor

    tkkr = clean_ticker(request.args.get('ticker', ''))
    if not tkkr:
        return jsonify({'error': 'No ticker'}), 400

    with ThreadPoolExecutor(max_workers=len(_MT_SERIES)) as ex:
        futures = {m: ex.submit(scrape_macrotrends, tkkr, m) for m in _MT_SERIES}
        out = {}
        for metric, fut in futures.items():
            try:
                by_date = fut.result(timeout=20)
            except Exception as e:
                out[metric] = {'error': f'{type(e).__name__}: {e}'}
                continue
            by_year = _mt_years(by_date, fiscal=(metric != 'fcf'))
            out[metric] = {
                'years':   len(by_year),
                'scale':   _MT_SERIES[metric].scale,
                'by_date': dict(sorted(by_date.items())),
                'by_year': {str(y): v for y, v in sorted(by_year.items())},
            }
    return jsonify({'ticker': tkkr, 'series': out})


@app.route('/api/debug/raw-series', methods=['GET'])
def debug_raw_series():
    """Untouched chartData rows, so v1/v2/v3 can be read as Macrotrends sends them.

    This is the evidence behind `scrape_macrotrends` reading v2: in every row v1
    repeats the previous row's v2 and v3 is the change between them, so v1 is the
    prior year's value and only v2 belongs to the row's own `date`.
    """
    tkkr = clean_ticker(request.args.get('ticker', 'AAPL'))
    if not tkkr:
        return jsonify({'error': 'No ticker'}), 400
    metric = request.args.get('metric', 'fcf')
    spec   = _MT_SERIES.get(metric)
    if spec is None:
        return jsonify({'error': f'unknown metric; try one of {sorted(_MT_SERIES)}'}), 400
    try:
        rows = _mt_chart_rows(tkkr, spec.type, spec.statement)
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 502
    return jsonify({'ticker': tkkr, 'metric': metric, 'type': spec.type,
                    'total': len(rows), 'sample': rows[-5:]})


@app.route('/api/search', methods=['GET'])
def search_tickers():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    try:
        import requests as req
        url = 'https://query2.finance.yahoo.com/v1/finance/search'
        params = {'q': query, 'quotesCount': 8, 'newsCount': 0, 'enableFuzzyQuery': True}
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = req.get(url, params=params, headers=headers, timeout=5)
        data = r.json()
        results = []
        for item in data.get('quotes', []):
            qtype = item.get('quoteType', '')
            if qtype not in ('EQUITY', 'ETF', 'MUTUALFUND', 'INDEX'):
                continue
            results.append({
                'ticker':   item.get('symbol', ''),
                'name':     item.get('longname') or item.get('shortname', ''),
                'sector':   item.get('sector') or qtype.title(),
                'exchange': item.get('exchDisp', ''),
            })
        # The typeahead is the one surface where a hidden name would otherwise
        # be offered by the app itself. Typing the symbol in full still reaches
        # /api/stock, which answers with the block rather than the page.
        return jsonify(_drop_blocked(results))
    except Exception:
        return jsonify([])


@app.route('/api/chart', methods=['GET'])
def get_chart():
    tkkr     = request.args.get('ticker', '').strip().upper()
    range_   = request.args.get('range', '1mo')
    interval = request.args.get('interval', '1d')
    if not tkkr:
        return jsonify({'error': 'No ticker'}), 400
    try:
        ticker = yf.Ticker(tkkr)
        hist   = ticker.history(period=range_, interval=interval)
        if hist.empty:
            return jsonify({'prices': [], 'dates': []})
        prices = [round(float(v), 4) for v in hist['Close'].tolist()]
        dates  = [str(d.date()) if hasattr(d, 'date') else str(d)[:10] for d in hist.index]

        # For 1D, use previous close as the baseline for % change
        prev_close = None
        if range_ == '1d':
            try:
                fi = ticker.fast_info
                prev_close = round(float(getattr(fi, 'previous_close', None) or 0), 4) or None
            except Exception:
                pass

        return jsonify({'prices': prices, 'dates': dates, 'prev_close': prev_close})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _quarterly_ttm(q_row, fiscal_adj=True):
    """Returns (partial_fiscal_year, ttm_value) when the most recent fiscal year has < 4 quarters.
    ttm_value = sum of the 4 most recent quarters. Returns (None, None) otherwise."""
    pairs = sorted(
        [(date, float(val)) for date, val in q_row.items() if val is not None and not pd.isna(val)],
        reverse=True
    )
    if len(pairs) < 4:
        return None, None
    yr_counts = {}
    for date, _ in pairs:
        yr = (date.year if date.month >= 4 else date.year - 1) if fiscal_adj else date.year
        yr_counts[yr] = yr_counts.get(yr, 0) + 1
    max_yr = max(yr_counts)
    if yr_counts[max_yr] < 4:
        return max_yr, sum(v for _, v in pairs[:4])
    return None, None


# ---------------------------------------------------------------------------
# Balance sheet / share count / analyst estimates / short interest
#
# These four take already-fetched frames and dicts rather than a Ticker. That
# keeps them pure — testable with no network and no monkeypatching — and keeps
# _do_get_stock a wiring layer for them instead of growing another 200 lines.
# ---------------------------------------------------------------------------

def _fmt_count(value):
    """Format a share count as X.XXB/M/K — format_large_number without the $."""
    if value is None:
        return 'N/A'
    sign = '-' if value < 0 else ''
    mag  = abs(float(value))
    if mag >= 1_000_000_000:
        return f"{sign}{mag / 1_000_000_000:.2f}B"
    if mag >= 1_000_000:
        return f"{sign}{mag / 1_000_000:.2f}M"
    if mag >= 1_000:
        return f"{sign}{mag / 1_000:.2f}K"
    return f"{sign}{mag:.0f}"


def _fiscal_year(ts):
    """Fiscal year of a statement column, matching the rest of the module: a
    period ending before April belongs to the previous year."""
    return ts.year if ts.month >= 4 else ts.year - 1


def _df_row(df, *names):
    """The first of `names` present in `df`'s index, or None.

    Yahoo renames rows between filers — 'Stockholders Equity' on one, 'Common
    Stock Equity' on another — so every read here is a list of candidates.
    """
    if df is None or getattr(df, 'empty', True):
        return None
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def _build_balance_sheet(bs, qbs, fin, shares_out=None, price=None,
                         symbol='$', price_matches_filing=True):
    """Balance-sheet snapshot plus a per-year series.

    The snapshot is read from ONE column. Mixing this quarter's assets with last
    year's liabilities would produce a current ratio that never existed on any
    filing, so the newest quarter that actually reports Total Assets wins and the
    annual statement is the fallback; `period` says which was used.

    Every metric is independently optional. Banks and insurers file no current
    assets or current liabilities at all — RY.TO has neither — so a missing row
    is the normal case, not an error.
    """
    # Every key is present on every payload, null when the filer does not
    # report it. A shape that varies by filer pushes the "is this missing or
    # is it zero" question onto each consumer.
    result = {
        'by_year': [], 'period': None, 'as_of': None,
        'current_ratio': None, 'quick_ratio': None, 'debt_to_equity': None,
        'interest_coverage': None, 'interest_coverage_year': None,
        'tangible_book_per_share': None, 'price_to_tangible_book': None,
    }
    for key in ('assets', 'liabilities', 'equity', 'current_assets',
                'current_liabilities', 'debt', 'net_debt', 'cash',
                'goodwill', 'tangible_book'):
        result[key] = None
        result[key + '_str'] = format_large_number(None, symbol)

    frame, col, period = None, None, None
    for df, label in ((qbs, 'MRQ'), (bs, 'FY')):
        assets_row = _df_row(df, 'Total Assets')
        if assets_row is None:
            continue
        for ts in sorted(df.columns, reverse=True):
            if _finite(assets_row.get(ts)) is not None:
                frame, col, period = df, ts, label
                break
        if frame is not None:
            break

    if frame is not None:
        def cell(*names):
            row = _df_row(frame, *names)
            return _finite(row.get(col)) if row is not None else None

        def money(key, value):
            result[key] = value
            result[key + '_str'] = format_large_number(value, symbol)

        assets   = cell('Total Assets')
        liabs    = cell('Total Liabilities Net Minority Interest', 'Total Liabilities')
        equity   = cell('Stockholders Equity', 'Common Stock Equity',
                        'Total Equity Gross Minority Interest')
        cur_a    = cell('Current Assets', 'Total Current Assets')
        cur_l    = cell('Current Liabilities', 'Total Current Liabilities')
        inventory = cell('Inventory')
        debt     = cell('Total Debt')
        net_debt = cell('Net Debt')
        tbv      = cell('Tangible Book Value', 'Net Tangible Assets')
        goodwill = cell('Goodwill', 'Goodwill And Other Intangible Assets')
        cash     = cell('Cash Cash Equivalents And Short Term Investments',
                        'Cash And Cash Equivalents')

        # Yahoo omits the total-liabilities row for some filers but never omits
        # both of the pieces it is made of.
        if liabs is None and assets is not None and equity is not None:
            liabs = assets - equity

        result['period'] = period
        result['as_of']  = str(pd.Timestamp(col).date())
        money('assets', assets)
        money('liabilities', liabs)
        money('equity', equity)
        money('current_assets', cur_a)
        money('current_liabilities', cur_l)
        money('debt', debt)
        money('net_debt', net_debt)
        money('cash', cash)
        money('goodwill', goodwill)
        money('tangible_book', tbv)

        if cur_a is not None and cur_l:
            result['current_ratio'] = round(cur_a / cur_l, 2)
            result['quick_ratio']   = round((cur_a - (inventory or 0)) / cur_l, 2)

        # A company with negative equity has no meaningful debt-to-equity ratio
        # — the arithmetic returns a negative number that reads like low
        # leverage when it means the opposite.
        if debt is not None and equity is not None and equity > 0:
            result['debt_to_equity'] = round(debt / equity, 2)

        if tbv is not None and shares_out:
            tbvps = tbv / float(shares_out)
            result['tangible_book_per_share'] = round(tbvps, 2)
            # An ADR trades in one currency and files in another — TSM quotes in
            # USD and reports in TWD — so price / book-per-share divides dollars
            # by New Taiwan dollars and lands ~31x off. The result looks like an
            # ordinary ratio, which is what makes it dangerous. Suppressed rather
            # than converted: a live FX rate applied to a filed balance sheet
            # invents a figure that appeared on no statement.
            if price and tbvps > 0 and price_matches_filing:
                result['price_to_tangible_book'] = round(float(price) / tbvps, 2)

    # Interest coverage comes off the income statement, so it is an annual
    # figure and carries its own year rather than the snapshot's as_of.
    ebit_row = _df_row(fin, 'EBIT', 'Operating Income')
    int_row  = _df_row(fin, 'Interest Expense', 'Interest Expense Non Operating')
    if ebit_row is not None and int_row is not None:
        for ts in sorted(fin.columns, reverse=True):
            ebit     = _finite(ebit_row.get(ts))
            interest = _finite(int_row.get(ts))
            if ebit is not None and interest:
                result['interest_coverage']      = round(ebit / abs(interest), 2)
                result['interest_coverage_year'] = _fiscal_year(pd.Timestamp(ts))
                break

    if bs is not None and not getattr(bs, 'empty', True):
        a_row  = _df_row(bs, 'Total Assets')
        l_row  = _df_row(bs, 'Total Liabilities Net Minority Interest', 'Total Liabilities')
        e_row  = _df_row(bs, 'Stockholders Equity', 'Common Stock Equity',
                         'Total Equity Gross Minority Interest')
        ca_row = _df_row(bs, 'Current Assets', 'Total Current Assets')
        cl_row = _df_row(bs, 'Current Liabilities', 'Total Current Liabilities')
        d_row  = _df_row(bs, 'Total Debt')
        t_row  = _df_row(bs, 'Tangible Book Value', 'Net Tangible Assets')

        for ts in sorted(bs.columns):
            def at(row):
                return _finite(row.get(ts)) if row is not None else None

            assets = at(a_row)
            equity = at(e_row)
            liabs  = at(l_row)
            if liabs is None and assets is not None and equity is not None:
                liabs = assets - equity
            if assets is None and liabs is None and equity is None:
                continue

            cur_a, cur_l, debt = at(ca_row), at(cl_row), at(d_row)
            row = {
                'year':          _fiscal_year(pd.Timestamp(ts)),
                'assets':        assets,
                'assets_str':    format_large_number(assets, symbol),
                'liabilities':      liabs,
                'liabilities_str':  format_large_number(liabs, symbol),
                'equity':        equity,
                'equity_str':    format_large_number(equity, symbol),
                'tangible_book': at(t_row),
                'current_ratio': round(cur_a / cur_l, 2) if cur_a is not None and cur_l else None,
                'debt_to_equity': round(debt / equity, 2) if debt is not None and equity and equity > 0 else None,
            }
            result['by_year'].append(row)

    return result


def _build_shares_history(bs, mt_shares=None):
    """Share count by fiscal year, plus the change across the window.

    yfinance's balance sheet is exact but reaches back only ~5 years;
    Macrotrends reaches ~14 at lower resolution. A Macrotrends year is only ever
    used to fill a gap — it never overwrites a yfinance year — and is tagged
    `src: 'mt'` so the chart can mark it approximate.

    `min_overlap=0` here where every other series wants 2: Macrotrends' share
    history routinely starts where yfinance's balance sheet stops, so there is
    nothing to agree on and `_MT_SHARES_MIN` is the only gate that applies.

    `change_pct` is positive for dilution and negative for a net buyback.
    """
    by_year, sources = {}, {}

    row = _df_row(bs, 'Ordinary Shares Number', 'Share Issued')
    if row is not None:
        for ts in row.index:
            count = _finite(row.get(ts))
            if count and count > 0:
                yr = _fiscal_year(pd.Timestamp(ts))
                by_year[yr] = count
                sources[yr] = 'yf'

    mt_shares = {y: float(c) for y, c in (mt_shares or {}).items() if c and c > 0}
    by_year, sources, _ = _merge_macrotrends(by_year, mt_shares, 'shares', min_overlap=0)

    if not by_year:
        return {'by_year': []}

    years = sorted(by_year)
    result = {
        'by_year': [{'year': y, 'raw': by_year[y], 'value': _fmt_count(by_year[y]),
                     'src': sources[y]} for y in years],
        'latest': by_year[years[-1]],
        'latest_str': _fmt_count(by_year[years[-1]]),
    }
    first, last = by_year[years[0]], by_year[years[-1]]
    if len(years) > 1 and first:
        result['change_pct'] = round((last - first) / first * 100, 2)
        result['from_year']  = years[0]
        result['to_year']    = years[-1]
    return result


# Yahoo's estimate frames are indexed by these period codes.
_EST_PERIODS = (
    ('0q',  'Current Qtr'),
    ('+1q', 'Next Qtr'),
    ('0y',  'Current Year'),
    ('+1y', 'Next Year'),
)

_RATING_LABELS = {
    'strong_buy': 'Strong Buy', 'buy': 'Buy', 'hold': 'Hold',
    'underperform': 'Underperform', 'sell': 'Sell',
}


def _build_analyst(info, eps_est=None, rev_est=None, price=None,
                   price_symbol='$', money_symbol='$'):
    """Street price targets, consensus rating and forward estimates.

    Targets and the rating come out of `info`, which the lookup already holds,
    so they add no network cost — only the two estimate frames are separate
    endpoints.

    yfinance reports estimate `growth` as a **decimal** (0.2054 = 20.54%), the
    opposite convention to `dividendYield`. It is converted here, once.

    The two frames are **not** necessarily in the same currency, and each says
    which it is in a `currency` column. TSM's EPS estimates come back in USD
    per ADR while its revenue estimates come back in TWD — so the frame's own
    column wins over anything inferred from the listing, and the passed
    symbols are only the fallback for a frame that omits it.
    """
    def rows(df, year_ago_col, money, default_symbol):
        if df is None or getattr(df, 'empty', True):
            return []
        symbol = default_symbol
        if 'currency' in getattr(df, 'columns', []):
            codes = [c for c in df['currency'].dropna().unique() if c]
            if len(codes) == 1:
                symbol = _currency_symbol(str(codes[0]))
        out = []
        for period, label in _EST_PERIODS:
            if period not in df.index:
                continue
            r = df.loc[period]
            avg = _finite(r.get('avg'))
            if avg is None:
                continue
            growth   = _finite(r.get('growth'))
            analysts = _finite(r.get('numberOfAnalysts'))
            out.append({
                'period':     period,
                'label':      label,
                'avg':        avg,
                'avg_str':    (format_large_number(avg, symbol) if money
                               else f"{symbol}{avg:.2f}"),
                'currency_symbol': symbol,
                'low':        _finite(r.get('low')),
                'high':       _finite(r.get('high')),
                'year_ago':   _finite(r.get(year_ago_col)),
                'analysts':   int(analysts) if analysts else None,
                'growth_pct': round(growth * 100, 2) if growth is not None else None,
            })
        return out

    result = {
        # EPS is quoted per traded share, revenue comes off the income
        # statement — different currencies for an ADR, hence different defaults.
        'eps_estimates': rows(eps_est, 'yearAgoEps',     money=False,
                              default_symbol=price_symbol),
        'rev_estimates': rows(rev_est, 'yearAgoRevenue', money=True,
                              default_symbol=money_symbol),
        'price_symbol':  price_symbol,
    }

    target_mean = _finite(info.get('targetMeanPrice'))
    for key, field in (('target_mean',   'targetMeanPrice'),
                       ('target_high',   'targetHighPrice'),
                       ('target_low',    'targetLowPrice'),
                       ('target_median', 'targetMedianPrice')):
        value = _finite(info.get(field))
        result[key] = round(value, 2) if value is not None else None

    n = _finite(info.get('numberOfAnalystOpinions'))
    result['n_analysts'] = int(n) if n else None

    key = (info.get('recommendationKey') or '').lower()
    result['rating']      = _RATING_LABELS.get(key) or (key.replace('_', ' ').title() or None)
    result['rating_mean'] = _finite(info.get('recommendationMean'))

    if target_mean and price:
        result['upside_pct'] = round((target_mean - float(price)) / float(price) * 100, 2)

    return result


# A float larger than the share count is impossible on one basis — the float is
# a subset of the count. Yahoo reports it anyway on depositary receipts, where
# the two are quoted on different bases; see `_build_short_interest`. The 2%
# margin covers the two figures being dated a few days apart. Measured
# 2026-08-08, the smallest real mismatch clears it by two orders of magnitude:
# ASML 55.5x, TSM 7.3x, HDB 5.0x, INFY 1.9x, against 0.99-1.00x on AAPL, SONY,
# SHOP and RY.TO.
_FLOAT_BASIS_MAX = 1.02


def _build_short_interest(info):
    """Short-interest snapshot. Every field is already in `info` — no extra call.

    `shortPercentOfFloat` is a **decimal** (0.01 = 1%), the opposite of
    `dividendYield`, which Yahoo hands over already scaled. Yahoo also omits it
    for most non-US listings — RY.TO has a share count but no percentage — so it
    is recomputed from sharesShort / floatShares when missing.

    **That fallback cannot run on a depositary receipt.** Yahoo quotes
    `floatShares` for an ADR on the *ordinary share* basis while `sharesShort`
    and `sharesOutstanding` count receipts, so the division mixes units and
    understates the answer by the whole deposit ratio — ASML reads 0.006% where
    0.33% of the receipts are short, TSM 0.09% against 0.64%. The two
    conditions are not independent: Yahoo omits the percentage for non-US
    listings, and non-US is exactly where the mixed basis lives, so the
    fallback fires precisely where it is wrong.

    `_FLOAT_BASIS_MAX` catches it, and the percentage is then None — suppressed
    rather than converted, the same call as a cross-currency ratio, because the
    deposit ratio appears nowhere in the payload and any conversion would be a
    number Yahoo never reported. Two things deliberately keep their value.
    Yahoo's own `shortPercentOfFloat` is already on the receipt basis (TSM 0.69%
    against 0.64% of shares outstanding, not the 0.09% an ordinary-share float
    would give), so only the fallback is guarded. And `pct_of_outstanding` is
    sound throughout: both of its terms count receipts.
    """
    shares_short = _finite(info.get('sharesShort'))
    if not shares_short:
        return {}

    prior    = _finite(info.get('sharesShortPriorMonth'))
    float_sh = _finite(info.get('floatShares'))
    out_sh   = (_finite(info.get('sharesOutstanding'))
                or _finite(info.get('impliedSharesOutstanding')))

    mixed_basis = bool(float_sh and out_sh and float_sh > out_sh * _FLOAT_BASIS_MAX)

    pct_float = _finite(info.get('shortPercentOfFloat'))
    if pct_float is not None:
        pct_float = round(pct_float * 100, 2)
    elif float_sh and not mixed_basis:
        pct_float = round(shares_short / float_sh * 100, 2)

    result = {
        'shares_short':     shares_short,
        'shares_short_str': _fmt_count(shares_short),
        'pct_of_float':     pct_float,
        'pct_of_outstanding': round(shares_short / out_sh * 100, 2) if out_sh else None,
        'days_to_cover':    _finite(info.get('shortRatio')),
        'change_pct':       round((shares_short - prior) / prior * 100, 2) if prior else None,
        'prior_month':      prior,
        'as_of':            None,
    }

    # Yahoo dates this in epoch seconds at UTC midnight; reading it in local
    # time would roll it back a day for anyone west of Greenwich.
    ts = info.get('dateShortInterest')
    try:
        if ts:
            result['as_of'] = _datetime.fromtimestamp(int(ts), tz=_timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        pass

    return result


def _build_ownership(info):
    """Institutional / insider / retail split, or {} when Yahoo reports neither.

    Institutional and insider are disjoint by construction, so the remainder is
    genuinely retail: Yahoo's own `institutionsFloatPercentHeld` equals
    `heldPercentInstitutions / (1 - heldPercentInsiders)` to five decimals on
    every ticker checked (GOOGL, SIRI, BYND), which means institutions hold out
    of the float and insiders hold the rest. The split is the right shape. Two
    Yahoo behaviours break the arithmetic on top of it, and both are common
    enough to hit this portfolio.

    **The fields are absent for most non-US listings**, the same gap
    `_build_short_interest` documents for `shortPercentOfFloat`. GOOG.TO,
    MSFT.TO, META.TO, LULU.TO and ZMMK.TO return None for both, and there is no
    fallback — `ticker.major_holders` is an empty frame for all of them. The old
    `info.get(...) or 0` read that as 0% institutional, so `retail` fell out of
    `1 - 0 - 0` at **100%** and the page drew a full doughnut claiming Alphabet
    is entirely retail-held. Four of the ten symbols in this account's portfolio
    rendered that way. Missing is not zero, so the section is dropped instead
    and the frontend's existing empty state shows.

    **Institutional can legitimately exceed 100%.** 13F filings double-count
    lent shares — the lender still reports a position the short buyer now also
    reports — so WING reads 123%, CARG 112%, ZG 107%, CVNA 106%. That is real
    information about share lending rather than a Yahoo typo, so the figure is
    reported as-is. What cannot survive is `retail`: it is None here rather than
    clamped to 0, because a clamped 0 asserts "no retail float", which is a
    claim the data does not make. `exceeds_outstanding` tells the frontend the
    three parts no longer form a whole, so it can drop the doughnut rather than
    let Chart.js renormalise 123% into a slice drawn as 99%.

    Suppressed rather than reconciled, for the same reason a cross-currency
    ratio is: scaling the three to sum to 100 would invent a number that
    appeared in no filing.
    """
    inst    = _finite(info.get('heldPercentInstitutions'))
    insider = _finite(info.get('heldPercentInsiders'))
    if inst is None and insider is None:
        return {}

    result = {
        'institutional':       round(inst * 100, 2) if inst is not None else None,
        'insider':             round(insider * 100, 2) if insider is not None else None,
        'retail':              None,
        'exceeds_outstanding': False,
    }

    # The remainder needs both halves. One field present and the other missing
    # is still worth showing on its own; it just cannot imply the rest.
    if inst is not None and insider is not None:
        if inst + insider <= 1.0:
            result['retail'] = round((1.0 - inst - insider) * 100, 2)
        else:
            result['exceeds_outstanding'] = True

    return result


# Properties _do_get_stock reads. Each is a separate Yahoo endpoint, and
# yfinance memoises them on the Ticker instance — so touching them all up front
# in parallel means the ~570 lines below hit warm attributes instead of paying
# for 12 sequential round trips.
_PREFETCH_PROPS = (
    'info', 'fast_info', 'financials', 'quarterly_financials',
    'cashflow', 'quarterly_cashflow',
    'balance_sheet', 'quarterly_balance_sheet',
    'dividends', 'earnings_dates',
    'earnings_estimate', 'revenue_estimate',
)


def _prefetch_ticker(ticker):
    """Warm every property the lookup needs, concurrently.

    Exceptions are swallowed: a property that fails here fails again when the
    code below reads it, where the existing per-section error handling deals with
    it. The point is only to overlap the network waits.
    """
    from concurrent.futures import ThreadPoolExecutor

    def touch(prop):
        try:
            getattr(ticker, prop)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=len(_PREFETCH_PROPS)) as ex:
        list(ex.map(touch, _PREFETCH_PROPS))


def _do_get_stock(tkkr):
    """All yfinance work for a stock lookup — runs in a daemon thread with a hard timeout."""
    ticker = yf.Ticker(tkkr)
    # Started before the yfinance prefetch so the scrapes overlap it too.
    mt = _start_macrotrends(tkkr)
    _prefetch_ticker(ticker)
    info = ticker.info

    try:
        if not info or len(info) < 5:
            raise ValueError(f"Could not retrieve data for '{tkkr}'. Check the symbol.")

        # --- Core fields ---
        name     = info.get('longName') or info.get('shortName') or info.get('symbol', 'N/A')
        price    = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose') or None
        currency = info.get('currency', 'USD')

        # Two currencies, and conflating them is a real error rather than a
        # cosmetic one. `currency` is what the *listing* trades in; the
        # statements are filed in `financialCurrency`, and for an ADR those
        # differ — TSM quotes in USD and files in TWD, Sony in USD and JPY.
        # Market cap, price and street targets follow the listing; revenue,
        # earnings, FCF, capex and the balance sheet follow the filing. Reading
        # them under one symbol put a 31x error on TSM's revenue and a 150x one
        # on Sony's, both rendered as ordinary dollars.
        financial_currency = info.get('financialCurrency') or currency
        trade_sym = _currency_symbol(currency)
        fin_sym   = _currency_symbol(financial_currency)
        # GBp is GBP quoted in pence, so a London listing is the one case where
        # the codes differ but the money is the same money — 100:1 apart, which
        # still bars any cross-currency ratio.
        same_currency = (currency or '').upper() == (financial_currency or '').upper()

        # Override price with fast_info for real-time consistency with watchlist
        # quotes. The share count comes off the same object because `info` omits
        # it for exactly the listings it omits `marketCap` for — see below.
        fast_shares = None
        try:
            fi = ticker.fast_info
            fast_price = getattr(fi, 'last_price', None)
            if fast_price:
                price = float(fast_price)
            fast_shares = _finite(getattr(fi, 'shares', None))
        except Exception:
            pass

        # One definition, read by everything below it. This expression used to be
        # written out three separate times — the payout-ratio walk, the TTM
        # earnings derivation and the payload — so a listing Yahoo reports no
        # count for lost four unrelated figures at once and there was nowhere
        # single to fix it.
        shares_outstanding = (_finite(info.get('sharesOutstanding'))
                              or _finite(info.get('impliedSharesOutstanding'))
                              or fast_shares)

        sector   = info.get('sector') or info.get('industry') or 'N/A'
        exchange = info.get('exchange') or info.get('fullExchangeName') or 'N/A'
        pe_ratio     = info.get('trailingPE') or None
        forward_pe   = info.get('forwardPE')  or None
        week52_high  = info.get('fiftyTwoWeekHigh') or info.get('52WeekHigh') or None
        week52_low   = info.get('fiftyTwoWeekLow')  or info.get('52WeekLow')  or None
        # Both halves, per the raw/value rule. The valuation calculator compares
        # its equity value against the cap directly rather than dividing by a
        # share count, so the float has to survive the trip — parsing '$1.09T'
        # back would quantise a mega-cap to three digits.
        market_cap_raw = _finite(info.get('marketCap') or info.get('regularMarketCap'))
        if market_cap_raw is None and shares_outstanding and price:
            # Yahoo omits `marketCap` outright for a stable minority of listings —
            # Home Depot and American Eagle among them — and omits
            # `sharesOutstanding` with it, so nothing in `info` derives it and
            # naming the field explicitly does not bring it back. The index tables
            # already fill this the same way (`_index_backfill_caps`: fast_info's
            # count times the live price, verified to reproduce Yahoo's own figure
            # to a ratio of 1.0000 on the 116 sampled names reporting both). The
            # detail page had no equivalent and printed 'N/A', which took the
            # valuation panel's margin of safety down with it — MoS divides by the
            # cap, so a missing one is a blank readout rather than a wrong number.
            #
            # A fallback and not the rule, because a share count is not always the
            # count the cap is built from: BRK-B's covers the B class alone and
            # lands 34% under the reported figure. That case cannot reach here —
            # this only fires where Yahoo reports no cap at all, and there the
            # alternative is nothing.
            market_cap_raw = shares_outstanding * float(price)
        market_cap = format_large_number(market_cap_raw, trade_sym)
        raw_debt   = info.get('totalDebt') or info.get('longTermDebt') or 0
        raw_cash   = info.get('totalCash') or info.get('cash') or 0
        net_debt   = raw_debt - raw_cash
        debt       = format_large_number(raw_debt, fin_sym)
        cash       = format_large_number(raw_cash, fin_sym) if raw_cash else 'N/A'

        # --- Capex ---
        capex_ttm_str = 'N/A'
        capex_by_year = []
        try:
            cashflow = ticker.cashflow
            yf_capex = {}
            if 'Capital Expenditure' in cashflow.index:
                capex_row = cashflow.loc['Capital Expenditure']
                ttm_val = capex_row.iloc[0]
                if ttm_val is not None and not pd.isna(ttm_val):
                    capex_ttm_str = format_large_number(abs(float(ttm_val)), fin_sym)
                for date, val in sorted(capex_row.items()):
                    if val is not None and not pd.isna(val):
                        yr = date.year if date.month >= 4 else date.year - 1
                        yf_capex[yr] = abs(float(val))

            # Quarterly cashflow aggregated by year for extended history
            capex_ttm_entry = None
            try:
                q_cashflow = ticker.quarterly_cashflow
                if 'Capital Expenditure' in q_cashflow.index:
                    q_capex_row = q_cashflow.loc['Capital Expenditure']
                    partial_yr, ttm_capex_raw = _quarterly_ttm(q_capex_row)
                    q_by_year = {}
                    for date, val in q_capex_row.items():
                        if val is not None and not pd.isna(val):
                            yr = date.year if date.month >= 4 else date.year - 1
                            q_by_year[yr] = q_by_year.get(yr, 0) + abs(float(val))
                    for yr, val in q_by_year.items():
                        if yr not in yf_capex and yr != partial_yr:
                            yf_capex[yr] = val
                    if ttm_capex_raw is not None and partial_yr not in yf_capex:
                        ttm_capex = abs(ttm_capex_raw)
                        capex_ttm_entry = {'year': 'TTM', 'raw': ttm_capex, 'value': format_large_number(ttm_capex, fin_sym)}
            except Exception:
                pass

            capex_by_year = [{'year': yr, 'raw': v, 'value': format_large_number(v, fin_sym)} for yr, v in sorted(yf_capex.items())]
            if capex_ttm_entry:
                capex_by_year.append(capex_ttm_entry)
        except Exception:
            pass

        # Daily change — pulled directly so it matches Yahoo exactly
        day_change     = info.get('regularMarketChange') or info.get('currentPrice', 0) - info.get('previousClose', 0) or None
        day_change_pct = info.get('regularMarketChangePercent') or None
        if day_change_pct is None and day_change and info.get('previousClose'):
            day_change_pct = (day_change / info.get('previousClose')) * 100

        # --- Profit Margin ---
        profit_margin = info.get('profitMargins') or info.get('netProfitMargin') or None
        profit_margin_str = 'N/A'
        if profit_margin:
            val = profit_margin if profit_margin > 1 else profit_margin * 100
            profit_margin_str = f"{val:.2f}%"
        else:
            try:
                net_income = ticker.financials.loc['Net Income'].iloc[0]
                revenue    = ticker.financials.loc['Total Revenue'].iloc[0]
                if revenue and revenue != 0:
                    profit_margin_str = f"{(net_income / revenue) * 100:.2f}% (calc)"
            except Exception:
                pass

        # --- Price to Book ---
        pb_ratio = info.get('priceToBook') or None
        pb_str = 'N/A'
        if pb_ratio:
            pb_str = f"{pb_ratio:.2f}"
        elif info.get('bookValue') and price:
            pb_str = f"{price / info.get('bookValue'):.2f} (calc)"

        # --- Dividend Yield ---
        # dividendYield = already a % value (0.41 = 0.41%)
        # trailingAnnualDividendYield = decimal (0.0041 = 0.41%)
        raw_dy      = info.get('dividendYield')
        trailing_dy = info.get('trailingAnnualDividendYield')
        dividend_rate = info.get('dividendRate') or info.get('trailingAnnualDividendRate') or None
        div_yield_str = 'N/A'
        if raw_dy is not None:
            div_yield_str = f"{float(raw_dy):.2f}%"
        elif trailing_dy is not None:
            div_yield_str = f"{float(trailing_dy) * 100:.2f}%"
        elif dividend_rate and price:
            div_yield_str = f"{(dividend_rate / price) * 100:.2f}% (calc)"

        # --- Dividend History (every payment) ---
        dividend_history = []
        try:
            divs = ticker.dividends
            if not divs.empty:
                divs.index = divs.index.tz_localize(None)
                for date, amount in divs.items():
                    dividend_history.append({
                        'date': str(date.date()),
                        'amount': round(float(amount), 4)
                    })
        except Exception:
            pass


        # --- Free Cash Flow (Annual) — yfinance + Macrotrends merge ---
        fcf_annual = []
        try:
            cashflow  = ticker.cashflow
            # Use Free Cash Flow row directly if available (most accurate)
            if 'Free Cash Flow' in cashflow.index:
                fcf = cashflow.loc['Free Cash Flow']
                for date, value in sorted(fcf.items()):
                    if value is not None and not pd.isna(value):
                        fcf_annual.append({'year': date.year, 'raw': float(value), 'value': format_large_number(value, fin_sym)})
            else:
                operating = cashflow.loc['Operating Cash Flow'] if 'Operating Cash Flow' in cashflow.index else None
                capex     = cashflow.loc['Capital Expenditure']  if 'Capital Expenditure'  in cashflow.index else None
                if operating is not None:
                    fcf = (operating + capex) if capex is not None else operating
                    for date, value in sorted(fcf.items()):
                        if value is not None and not pd.isna(value):
                            label = format_large_number(value, fin_sym) + ('' if capex is not None else ' (OCF)')
                            fcf_annual.append({'year': date.year, 'raw': float(value), 'value': label})
        except Exception:
            pass

        # Fallback: aggregate quarterly cashflow into annual to get more years
        fcf_ttm_entry = None
        try:
            yf_years = {r['year'] for r in fcf_annual}
            qcf = ticker.quarterly_cashflow
            if 'Free Cash Flow' in qcf.index:
                q_fcf = qcf.loc['Free Cash Flow']
                partial_yr, ttm_fcf = _quarterly_ttm(q_fcf, fiscal_adj=False)
                by_year = {}
                for date, value in q_fcf.items():
                    if value is not None and not pd.isna(value):
                        yr = date.year
                        by_year[yr] = by_year.get(yr, 0) + float(value)
                for yr, total in by_year.items():
                    if yr not in yf_years and yr != partial_yr:
                        fcf_annual.append({'year': yr, 'raw': total, 'value': format_large_number(total, fin_sym)})
                if ttm_fcf is not None and partial_yr not in yf_years:
                    fcf_ttm_entry = {'year': 'TTM', 'raw': ttm_fcf, 'value': format_large_number(ttm_fcf, fin_sym)}
            else:
                q_operating = qcf.loc['Operating Cash Flow'] if 'Operating Cash Flow' in qcf.index else None
                q_capex     = qcf.loc['Capital Expenditure']  if 'Capital Expenditure'  in qcf.index else None
                if q_operating is not None:
                    q_fcf = (q_operating + q_capex) if q_capex is not None else q_operating
                    partial_yr, ttm_fcf = _quarterly_ttm(q_fcf, fiscal_adj=False)
                    by_year = {}
                    for date, value in q_fcf.items():
                        if value is not None and not pd.isna(value):
                            yr = date.year
                            by_year[yr] = by_year.get(yr, 0) + float(value)
                    for yr, total in by_year.items():
                        if yr not in yf_years and yr != partial_yr:
                            label = format_large_number(total, fin_sym) + ('' if q_capex is not None else ' (OCF)')
                            fcf_annual.append({'year': yr, 'raw': total, 'value': label})
                    if ttm_fcf is not None and partial_yr not in yf_years:
                        label = format_large_number(ttm_fcf, fin_sym) + ('' if q_capex is not None else ' (OCF)')
                        fcf_ttm_entry = {'year': 'TTM', 'raw': ttm_fcf, 'value': label}
            fcf_annual.sort(key=lambda r: r['year'])
        except Exception:
            pass

        # Macrotrends scrape — only for US tickers, fills in older years not in yfinance
        try:
            yf_fcf     = {r['year']: r['raw']   for r in fcf_annual}
            fcf_labels = {r['year']: r['value'] for r in fcf_annual}
            # fiscal=False: this series alone keys on the bare calendar year of
            # the period end, so the scrape has to be bucketed the same way or
            # the two sources land on different labels for one fiscal year.
            merged, fcf_src, _ = _merge_macrotrends(
                yf_fcf, _mt_years(_mt_result(mt, 'fcf'), fiscal=False), 'fcf')
            # A Macrotrends year is a real free cash flow figure, so it must not
            # inherit the ' (OCF)' marker from a yfinance year that had to fall
            # back to operating cash flow — the frontend reads that marker out of
            # this string to paint the bar amber and warn about it.
            fcf_annual = [{'year': y, 'raw': v,
                           'value': fcf_labels.get(y) or format_large_number(v, fin_sym),
                           'src': fcf_src[y]} for y, v in sorted(merged.items())]
        except Exception:
            pass

        if fcf_ttm_entry:
            # Last, after the sort: `fillCalculators` reads fcf_annual[-1] for the
            # TTM figure, and 'TTM' is a string that won't order against the ints.
            fcf_ttm_entry.setdefault('src', 'yf')
            fcf_annual.append(fcf_ttm_entry)

        # Build a quick lookup from fcf_annual for use in payout ratio
        fcf_by_year = {r['year']: r['raw'] for r in fcf_annual}
        fcf_fmt_by_year = {r['year']: r['value'] for r in fcf_annual}

        # --- Payout Ratio by Year (FCF-based) ---
        payout_by_year = []
        try:
            divs2 = ticker.dividends
            divs2.index = divs2.index.tz_localize(None)
            annual_divs = divs2.groupby(divs2.index.year).sum()

            shares = shares_outstanding

            # Get total dividends paid from cashflow statement
            total_divs_by_year = {}
            try:
                cf = ticker.cashflow
                if 'Common Stock Dividend Paid' in cf.index:
                    div_row = cf.loc['Common Stock Dividend Paid']
                elif 'Cash Dividends Paid' in cf.index:
                    div_row = cf.loc['Cash Dividends Paid']
                else:
                    div_row = None
                if div_row is not None:
                    for date, val in div_row.items():
                        if val is not None and not pd.isna(val):
                            yr = date.year if date.month >= 4 else date.year - 1
                            total_divs_by_year[yr] = abs(float(val))
            except Exception:
                pass

            all_years = sorted(set(list(fcf_by_year.keys()) + list(annual_divs.index.tolist())))

            for year in all_years:
                fcf_val = fcf_by_year.get(year, None)
                fcf_fmt = fcf_fmt_by_year.get(year, None)
                div_per_share = annual_divs.get(year, None)

                total_div = total_divs_by_year.get(year, None)
                if total_div is None and div_per_share is not None and shares:
                    total_div = float(div_per_share) * shares

                if div_per_share is None or pd.isna(div_per_share):
                    if fcf_val is not None:
                        payout_by_year.append({'year': year, 'payout': None, 'note': 'No dividend data', 'fcf': fcf_fmt})
                elif fcf_val is None:
                    payout_by_year.append({'year': year, 'payout': None, 'note': 'No FCF data', 'div': round(float(div_per_share), 4)})
                elif fcf_val <= 0:
                    payout_by_year.append({'year': year, 'payout': None, 'note': 'FCF negative', 'div': round(float(div_per_share), 4), 'fcf': fcf_fmt})
                else:
                    payout = round((total_div / fcf_val) * 100, 2) if total_div else None
                    payout_by_year.append({
                        'year': year,
                        'payout': payout,
                        'div': round(float(div_per_share), 4),
                        'fcf': fcf_fmt
                    })
        except Exception:
            pass

        news = []   # loaded separately via /api/news

        # --- Profit Margin by Year ---
        profit_margin_by_year = []
        try:
            fin = ticker.financials
            yf_pm = {}
            if 'Net Income' in fin.index and 'Total Revenue' in fin.index:
                net_income_row = fin.loc['Net Income']
                revenue_row    = fin.loc['Total Revenue']
                for date in sorted(net_income_row.index):
                    yr  = date.year if date.month >= 4 else date.year - 1
                    ni  = net_income_row.get(date)
                    rev = revenue_row.get(date)
                    if ni is not None and rev is not None and not pd.isna(ni) and not pd.isna(rev) and float(rev) != 0:
                        yf_pm[yr] = round((float(ni) / float(rev)) * 100, 2)

            # Macrotrends — US tickers only, yfinance years take priority
            yf_pm, pm_src, _ = _merge_macrotrends(
                yf_pm, _mt_years(_mt_result(mt, 'margin')), 'margin')

            profit_margin_by_year = [{'year': yr, 'margin': m, 'src': pm_src[yr]}
                                     for yr, m in sorted(yf_pm.items())]
        except Exception:
            pass

        # --- Revenue by Year ---
        # Built exactly like the earnings series below: annual columns first, the
        # quarterly frame filling any year yfinance omits from the annual one, a
        # TTM bar off the trailing four quarters, and Macrotrends behind all of
        # it. The quarterly fallback sits outside the `Total Revenue in
        # fin.index` test rather than nested inside it — a filer missing from the
        # annual frame is exactly the one whose quarters have to answer.
        revenue_ttm_str = 'N/A'
        revenue_by_year = []
        try:
            fin = ticker.financials
            yf_rev = {}
            if 'Total Revenue' in fin.index:
                rev_row = fin.loc['Total Revenue']
                # The fallback for the box, not the answer: this is the newest
                # *annual* column. See the override below the quarterly block.
                ttm_val = rev_row.iloc[0]
                if ttm_val is not None and not pd.isna(ttm_val):
                    revenue_ttm_str = format_large_number(float(ttm_val), fin_sym)
                for date, val in sorted(rev_row.items()):
                    if val is not None and not pd.isna(val):
                        yr = date.year if date.month >= 4 else date.year - 1
                        yf_rev[yr] = float(val)
            rev_ttm_entry = None
            ttm_rev = None
            try:
                q_fin = ticker.quarterly_financials
                if 'Total Revenue' in q_fin.index:
                    q_rev_row = q_fin.loc['Total Revenue']
                    partial_yr, ttm_rev = _quarterly_ttm(q_rev_row)
                    q_by_year = {}
                    for date, val in q_rev_row.items():
                        if val is not None and not pd.isna(val):
                            yr = date.year if date.month >= 4 else date.year - 1
                            q_by_year[yr] = q_by_year.get(yr, 0) + float(val)
                    for yr, val in q_by_year.items():
                        if yr not in yf_rev and yr != partial_yr:
                            yf_rev[yr] = val
                    if ttm_rev is not None and partial_yr not in yf_rev:
                        rev_ttm_entry = {'year': 'TTM', 'raw': ttm_rev,
                                         'value': format_large_number(ttm_rev, fin_sym), 'src': 'yf'}
            except Exception:
                pass
            # The box is labelled "Revenue (TTM)" and was showing the newest
            # annual column, which is a fiscal year and not a trailing twelve
            # months — Apple read $416.16B against a real $466.82B. Nothing on
            # the page contradicted it while the by-year chart was a popup; the
            # chart is a section now and prints its own TTM bar directly below,
            # so the two would have disagreed in plain sight.
            #
            # `_quarterly_ttm` sums the four most recent quarters and returns
            # None rather than a short sum when it has fewer, so the annual
            # column stays the fallback for a filer yfinance gives no quarters
            # for — a stale-by-one-quarter figure beats a blank card.
            if ttm_rev is not None:
                revenue_ttm_str = format_large_number(ttm_rev, fin_sym)
            # yfinance reaches back five years; Macrotrends carries the rest, and
            # a scraped year only ever fills a gap.
            yf_rev, rev_src, _ = _merge_macrotrends(
                yf_rev, _mt_years(_mt_result(mt, 'revenue')), 'revenue')
            revenue_by_year = [{'year': yr, 'raw': v, 'value': format_large_number(v, fin_sym),
                                'src': rev_src[yr]} for yr, v in sorted(yf_rev.items())]
            if rev_ttm_entry:
                # Appended after the sort: 'TTM' is a string and won't order
                # against the int years.
                revenue_by_year.append(rev_ttm_entry)
        except Exception:
            pass

        # --- Earnings by Year & TTM ---
        # TTM earnings card is derived from trailingEps × sharesOutstanding so it is
        # arithmetically identical to what Yahoo Finance uses for the P/E ratio, keeping
        # Earnings, EPS, and P/E all consistent. (Summing raw quarterly Net Income fails
        # for companies like MFI that had a large discontinued-operations gain in one quarter,
        # and yfinance often has missing quarters that compound the error.)
        earnings_ttm_str = 'N/A'
        earnings_ttm_raw = None
        earnings_by_year = []
        try:
            fin = ticker.financials
            yf_ni = {}
            if 'Net Income' in fin.index:
                ni_row = fin.loc['Net Income']
                for date, val in sorted(ni_row.items()):
                    if val is not None and not pd.isna(val):
                        yr = date.year if date.month >= 4 else date.year - 1
                        yf_ni[yr] = float(val)
            # Fill recent years from quarterly totals
            ni_ttm_entry = None
            try:
                q_fin = ticker.quarterly_financials
                if 'Net Income' in q_fin.index:
                    q_ni_row = q_fin.loc['Net Income']
                    partial_yr, ttm_ni = _quarterly_ttm(q_ni_row)
                    q_by_year = {}
                    for date, val in q_ni_row.items():
                        if val is not None and not pd.isna(val):
                            yr = date.year if date.month >= 4 else date.year - 1
                            q_by_year[yr] = q_by_year.get(yr, 0) + float(val)
                    for yr, val in q_by_year.items():
                        if yr not in yf_ni and yr != partial_yr:
                            yf_ni[yr] = val
                    if ttm_ni is not None and partial_yr not in yf_ni:
                        ni_ttm_entry = {'year': 'TTM', 'raw': ttm_ni,
                                        'value': format_large_number(ttm_ni, fin_sym), 'src': 'yf'}
            except Exception:
                pass
            # yfinance reaches back five years; Macrotrends carries fourteen, and
            # a scraped year only ever fills a gap.
            yf_ni, ni_src, _ = _merge_macrotrends(
                yf_ni, _mt_years(_mt_result(mt, 'earnings')), 'earnings')
            # No abs(): a loss-making year must not render as a positive figure.
            # format_large_number now scales negatives correctly, so the sign can
            # be carried through to the label.
            earnings_by_year = [{'year': yr, 'raw': v, 'value': format_large_number(v, fin_sym),
                                 'src': ni_src[yr]} for yr, v in sorted(yf_ni.items())]
            if ni_ttm_entry:
                # Appended after the sort: 'TTM' is a string and won't order
                # against the int years.
                earnings_by_year.append(ni_ttm_entry)
        except Exception:
            pass

        # EPS (TTM) & EPS by Year
        eps_ttm_str = 'N/A'
        eps_raw = None
        eps_by_year = []
        try:
            eps = info.get('trailingEps') or info.get('epsTrailingTwelveMonths')
            if eps is not None and not pd.isna(float(eps)):
                eps_raw = float(eps)
                # Per traded share, so the listing's currency — unlike the
                # by-year series below, which comes off the income statement.
                eps_ttm_str = f"{trade_sym}{eps_raw:.2f}"
        except Exception:
            pass
        try:
            fin = ticker.financials
            yf_eps = {}
            eps_reported = False
            # Prefer Diluted EPS row, fall back to Basic EPS
            for row_name in ('Diluted EPS', 'Basic EPS'):
                if row_name in fin.index:
                    eps_row = fin.loc[row_name]
                    for date, val in sorted(eps_row.items()):
                        if val is not None and not pd.isna(val):
                            yr = date.year if date.month >= 4 else date.year - 1
                            yf_eps[yr] = float(val)
                    eps_reported = bool(yf_eps)
                    break
            # Fall back: calculate from Net Income / Diluted Shares
            if not yf_eps and 'Net Income' in fin.index:
                ni_row  = fin.loc['Net Income']
                sh_row  = None
                for sh_name in ('Diluted Average Shares', 'Basic Average Shares', 'Ordinary Shares Number'):
                    if sh_name in fin.index:
                        sh_row = fin.loc[sh_name]
                        break
                if sh_row is not None:
                    for date, ni_val in sorted(ni_row.items()):
                        sh_val = sh_row.get(date)
                        if ni_val is not None and sh_val is not None and not pd.isna(ni_val) and not pd.isna(sh_val) and float(sh_val) != 0:
                            yr = date.year if date.month >= 4 else date.year - 1
                            yf_eps[yr] = float(ni_val) / float(sh_val)
            # A derived EPS is net income over an average share count, which can
            # sit several percent off Macrotrends' as-reported diluted figure
            # without either being wrong — so the gate is only tightened when
            # yfinance gave us the reported row to compare against.
            yf_eps, eps_src, _ = _merge_macrotrends(
                yf_eps, _mt_years(_mt_result(mt, 'eps')), 'eps',
                tol_rel=None if eps_reported else 0.15)
            eps_by_year = [{'year': yr, 'raw': v,
                            'value': ('-' if v < 0 else '') + fin_sym + _eps_str(abs(v)),
                            'src': eps_src[yr]} for yr, v in sorted(yf_eps.items())]
        except Exception:
            pass

        # --- P/E History (weekly, up to 10 years) ---
        # Primary: quarterly TTM for recent accuracy; fallback: annual EPS for older years.
        pe_history = []
        try:
            _a_eps = {e['year']: e['raw'] for e in eps_by_year if e.get('raw') and e['raw'] > 0}
            _q_eps_list = []
            try:
                _qfin = ticker.quarterly_financials
                if _qfin is not None and not _qfin.empty:
                    _qni = next((_qfin.loc[n] for n in ['Net Income', 'Net Income Common Stockholders'] if n in _qfin.index), None)
                    _qsh = next((_qfin.loc[n] for n in ['Diluted Average Shares', 'Basic Average Shares'] if n in _qfin.index), None)
                    if _qni is not None and _qsh is not None:
                        for _qdt in sorted(_qni.index):
                            _ni, _sh = _qni.get(_qdt), _qsh.get(_qdt)
                            if (_ni is not None and _sh is not None
                                    and not pd.isna(_ni) and not pd.isna(_sh)
                                    and float(_sh) != 0):
                                _q_eps_list.append((pd.Timestamp(_qdt).date(), float(_ni) / float(_sh)))
            except Exception:
                pass
            if _a_eps or len(_q_eps_list) >= 4:
                _weekly = ticker.history(period='10y', interval='1wk')
                for _wdt, _wrow in _weekly.iterrows():
                    _wprice = _wrow.get('Close')
                    if _wprice is None or pd.isna(_wprice):
                        continue
                    _wd = pd.Timestamp(_wdt).date()
                    _ttm_eps = None
                    if _q_eps_list:
                        _rq = [_e for (_d, _e) in _q_eps_list if _d <= _wd]
                        if len(_rq) >= 4:
                            _ttm_eps = sum(_rq[-4:])
                    if not _ttm_eps or _ttm_eps <= 0:
                        _ey = max((_y for _y in _a_eps if _y <= _wd.year), default=None)
                        if _ey is not None:
                            _ttm_eps = _a_eps[_ey]
                    if not _ttm_eps or _ttm_eps <= 0:
                        continue
                    _pe = round(float(_wprice) / _ttm_eps, 2)
                    if 0 < _pe < 500:
                        pe_history.append({'date': str(_wd), 'pe': _pe})
        except Exception:
            pass

        # Derive TTM earnings from trailingEps × shares — guaranteed consistent with
        # the EPS card and P/E ratio (both sourced from the same Yahoo Finance fields).
        # Also recalculate profit margin so it uses the same earnings basis.
        try:
            _shares = shares_outstanding
            if eps_raw is not None and _shares is not None:
                # trailingEps is per *traded* share, so this lands in the
                # listing's currency — USD per ADR for TSM, not the TWD its
                # income statement is filed in.
                earnings_ttm_raw = eps_raw * float(_shares)
                earnings_ttm_str = format_large_number(earnings_ttm_raw, trade_sym)
                # Override profit margin with the same earnings basis / TTM
                # revenue — but only when both legs are the same money.
                # `totalRevenue` is a filing figure, so for an ADR this divided
                # USD earnings by TWD revenue and printed TSM's 38% net margin
                # as 1.33%. A ratio carries no unit to give the error away.
                # Yahoo's own `profitMargins` is already unit-free and correct,
                # so the fallback above stands rather than being replaced.
                _rev = info.get('totalRevenue')
                if _rev and float(_rev) != 0 and same_currency:
                    profit_margin_str = f"{(earnings_ttm_raw / float(_rev)) * 100:.2f}%"
        except Exception:
            pass

        # --- Earnings hit/miss history ---
        # Newest first. yfinance's Surprise(%) is already a percent (-10.88 means
        # -10.88%), and it can be NaN even when both EPS columns are present —
        # NaN is not None, so it has to be filtered explicitly or jsonify emits a
        # bare `NaN` literal that JSON.parse rejects, losing the whole payload.
        # The bubble chart positions dots from `estimate`/`reported`, so those
        # keep 4dp; `*_str` carries the 2dp display text (see the raw/value rule).
        earnings_history = []
        try:
            ed = ticker.earnings_dates
            if ed is not None and not ed.empty:
                ed = ed.dropna(subset=['EPS Estimate', 'Reported EPS'])
                for dt, row in ed.head(12).iterrows():
                    estimate = _finite(row.get('EPS Estimate'))
                    reported = _finite(row.get('Reported EPS'))
                    if estimate is None or reported is None:
                        continue
                    surprise_pct = _finite(row.get('Surprise(%)'))
                    if surprise_pct is None and estimate != 0:
                        surprise_pct = ((reported - estimate) / abs(estimate)) * 100
                    earnings_history.append({
                        'date': dt.strftime('%b %Y'),
                        'date_full': dt.strftime('%d %b %Y'),
                        'estimate': round(estimate, 4),
                        'reported': round(reported, 4),
                        'estimate_str': _eps_str(estimate),
                        'reported_str': _eps_str(reported),
                        'surprise_pct': round(surprise_pct, 2) if surprise_pct is not None else None,
                        'beat': reported >= estimate,
                    })
        except Exception:
            pass

        # --- Ownership ---
        ownership = {}
        try:
            ownership = _build_ownership(info)
        except Exception:
            pass

        # --- Balance sheet, share count, analyst view, short interest ---
        # Each builder is independently optional: a filer that reports none of
        # the rows one of them wants yields {} or [], and the frontend hides
        # that section rather than the whole page failing.
        balance_sheet = {}
        try:
            balance_sheet = _build_balance_sheet(
                ticker.balance_sheet, ticker.quarterly_balance_sheet,
                ticker.financials, shares_outstanding, price,
                symbol=fin_sym, price_matches_filing=same_currency)
        except Exception:
            pass

        shares_history = {}
        try:
            shares_history = _build_shares_history(
                ticker.balance_sheet, _mt_years(_mt_result(mt, 'shares')))
        except Exception:
            pass

        analyst = {}
        try:
            analyst = _build_analyst(
                info, ticker.earnings_estimate, ticker.revenue_estimate, price,
                price_symbol=trade_sym, money_symbol=fin_sym)
        except Exception:
            pass

        short_interest = {}
        try:
            short_interest = _build_short_interest(info)
        except Exception:
            pass

        return {
            'ticker': tkkr,
            'name': name,
            'price': f"{price:.2f}" if price else 'N/A',
            'currency': currency,
            # The frontend formats its own chart labels off `raw`, so it needs
            # the same split the strings above already carry: statement charts
            # are in the filing currency, quote-derived boxes in the listing's.
            'financial_currency': financial_currency,
            'currency_symbol': trade_sym,
            'financial_currency_symbol': fin_sym,
            'same_currency': same_currency,
            # Which month this filer's fiscal year ends in, so the forward
            # guidance panel can say what calendar span an `FY2027` covers.
            # Free here: `financials` is in `_PREFETCH_PROPS` and yfinance
            # memoises it per Ticker, so this is a warm attribute read.
            # None when the frame does not say — the panel then shows the
            # fiscal label alone, which is what it did before this existed.
            'fiscal_year_end_month': _fye_month_opt(_frame(ticker, 'financials')),
            'market_cap': market_cap,
            'market_cap_raw': market_cap_raw,
            'sector': sector,
            'exchange': exchange,
            'pe_ratio': f"{pe_ratio:.2f}" if pe_ratio else 'N/A',
            'forward_pe': f"{forward_pe:.2f}" if forward_pe else 'N/A',
            'week52_high': f"{week52_high:.2f}" if week52_high else 'N/A',
            'week52_low':  f"{week52_low:.2f}"  if week52_low  else 'N/A',
            'day_change_pct': round(float(day_change_pct), 2) if day_change_pct else None,
            'debt': debt,
            'cash': cash,
            'capex_ttm': capex_ttm_str,
            'capex_by_year': capex_by_year,
            'revenue_ttm': revenue_ttm_str,
            'revenue_by_year': revenue_by_year,
            'profit_margin': profit_margin_str,
            'profit_margin_by_year': profit_margin_by_year,
            'pb_ratio': pb_str,
            'div_yield': div_yield_str,
            'dividend_history': dividend_history,
            'payout_by_year': payout_by_year,
            'fcf_annual': fcf_annual,
            'earnings_ttm': earnings_ttm_str,
            'earnings_by_year': earnings_by_year,
            'eps_ttm': eps_ttm_str,
            'eps_raw': eps_raw,
            'eps_by_year': eps_by_year,
            'pe_history': pe_history,
            'earnings_history': earnings_history,
            'news': news,
            'shares': shares_outstanding,
            'ownership': ownership,
            'net_debt_raw': net_debt,
            'balance_sheet': balance_sheet,
            'shares_history': shares_history,
            'analyst': analyst,
            'short_interest': short_interest,
        }
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Stock lookup cache
#
# One lookup makes ~9 sequential upstream fetches (info, financials, quarterly
# financials, cashflow, quarterly cashflow, dividends, history, fast_info,
# earnings_dates) plus two Macrotrends scrapes at a 10s timeout each. That is
# why the route needs a 25-second deadline, and without a cache viewing the same
# ticker twice paid the whole cost twice.
#
# Fundamentals change quarterly, so a 10-minute TTL is conservative. The live
# price is refreshed on the way out of the cache so a cached entry never shows a
# stale quote — that is the one field a user notices going stale.
# ---------------------------------------------------------------------------
STOCK_TTL = 600

# Past STOCK_TTL a payload is stale but still servable; past this it is not.
# The gap between the two is the stale-while-revalidate window — see
# _cached_stock. An hour is chosen against what actually moves in this payload:
# fundamentals are quarterly, the statement frames and Macrotrends series change
# on a filing, and the one field that moves by the minute is the quote, which
# `_refresh_quote` re-fetches on the way out regardless of the entry's age. So a
# 40-minute-old body with a live quote on it is not a worse answer than a fresh
# one — it is the same answer, three seconds sooner.
STOCK_STALE_TTL = 3600

_stock_cache: dict = {}          # ticker -> {'data': dict, 'ts': float}
_stock_cache_lock = threading.RLock()
_stock_inflight: dict = {}       # ticker -> Event, so concurrent hits share one fetch


def _refresh_quote(data):
    """Update just the quote fields on a cached payload. Cheap: fast_info only.

    Keeps the exact shape _do_get_stock produces — `price` is a 2dp string and
    `day_change_pct` a rounded float — so a refreshed payload is indistinguishable
    from a fresh one to the frontend.

    Market cap rides along, rescaled by the same ratio. It is price x shares by
    definition and the share count does not move intraday, so this is exact — and
    it has to happen, because the valuation calculator now compares an equity
    value against the cap while showing the price beside it. An entry is servable
    for STOCK_STALE_TTL (an hour), so leaving the cap behind would put a live
    price next to an hour-old cap and quietly bias every margin of safety by
    whatever the stock did in between.
    """
    try:
        fi   = yf.Ticker(data['ticker']).fast_info
        last = getattr(fi, 'last_price', None)
        prev = getattr(fi, 'previous_close', None)
        if last:
            prior = _finite(data.get('price'))
            data = dict(data)
            data['price'] = f'{float(last):.2f}'
            if prev and float(prev):
                data['day_change_pct'] = round(
                    (float(last) - float(prev)) / float(prev) * 100, 2)
            cap = _finite(data.get('market_cap_raw'))
            if cap is not None and prior:
                cap = cap * (float(last) / prior)
                data['market_cap_raw'] = cap
                data['market_cap'] = format_large_number(
                    cap, data.get('currency_symbol') or '$')
    except Exception:
        pass
    return data


def _refresh_stock_async(tkkr):
    """Rebuild a stale cache entry in the background. No-op if one is running.

    Registration happens under the same lock that reads `_stock_inflight`, so two
    stale hits arriving together start one rebuild rather than two, and a
    genuinely cold request for the same ticker waits on this one instead of
    racing it — the same coalescing `_cached_stock` already does for cold
    fetches, reused rather than reimplemented.
    """
    import time as _time

    with _stock_cache_lock:
        if tkkr in _stock_inflight:
            return
        waiting = threading.Event()
        _stock_inflight[tkkr] = waiting

    def _work():
        try:
            data = _do_get_stock(tkkr)
            with _stock_cache_lock:
                _stock_cache[tkkr] = {'data': data, 'ts': _time.time()}
        except Exception as e:
            # The stale entry stays. It is still servable, and dropping it would
            # turn one transient upstream failure into a cold fetch for whoever
            # asks next — the exact cost this path exists to avoid. It ages out
            # through STOCK_STALE_TTL on its own if the failure is not transient.
            print(f'[STOCK] background refresh failed for {tkkr}: '
                  f'{type(e).__name__}: {e}', flush=True)
        finally:
            with _stock_cache_lock:
                _stock_inflight.pop(tkkr, None)
            waiting.set()

    threading.Thread(target=_work, daemon=True).start()


def _cached_stock(tkkr):
    """Return (payload, was_cached). Raises whatever _do_get_stock raises."""
    import time as _time
    now = _time.time()

    with _stock_cache_lock:
        hit = _stock_cache.get(tkkr)
        age = (now - hit['ts']) if hit else None
        if hit and age < STOCK_TTL:
            return hit['data'], True
        # Stale but inside the SWR window: answer from it now and rebuild
        # behind the response. Expiry used to be a cliff — the first request
        # after ten minutes paid the full cold cost (five Macrotrends scrapes
        # plus a whole yfinance walk, ~3s) on behalf of everyone after it, and
        # the more popular a ticker was the more reliably some unlucky caller
        # hit that edge. Nobody waits for a rebuild now.
        if hit and age < STOCK_STALE_TTL:
            _refresh_stock_async(tkkr)
            return hit['data'], True
        # Another request is already fetching this ticker: wait for it rather
        # than starting a second identical set of upstream calls.
        waiting = _stock_inflight.get(tkkr)
        if waiting is None:
            waiting = threading.Event()
            _stock_inflight[tkkr] = waiting
            owner = True
        else:
            owner = False

    if not owner:
        waiting.wait(timeout=25)
        with _stock_cache_lock:
            hit = _stock_cache.get(tkkr)
        if hit:
            return hit['data'], True
        raise RuntimeError('Request timed out — try again.')

    try:
        data = _do_get_stock(tkkr)
        with _stock_cache_lock:
            _stock_cache[tkkr] = {'data': data, 'ts': _time.time()}
        return data, False
    finally:
        with _stock_cache_lock:
            _stock_inflight.pop(tkkr, None)
        waiting.set()


@app.route('/api/stock', methods=['GET'])
def get_stock():
    import threading as _threading
    tkkr = clean_ticker(request.args.get('ticker'))
    if not tkkr:
        return jsonify({'error': 'No valid ticker provided'}), 400

    # Here in the route rather than in _do_get_stock: the payload cache below is
    # keyed by ticker and shared by every account, so the account-specific
    # question has to be asked before it is consulted. Refused rather than
    # served, because the detail page is the one place a hidden symbol can still
    # be reached — the filters elsewhere only stop the app from offering it, and
    # a ticker typed in full would otherwise walk straight past all of them.
    if tkkr in _blocked_set():
        return jsonify({'error': f'{tkkr} is hidden.',
                        'blocked': True, 'ticker': tkkr}), 403

    result  = [None]
    cached  = [False]
    exc     = [None]
    done    = _threading.Event()

    def _run():
        try:
            result[0], cached[0] = _cached_stock(tkkr)
        except Exception as e:
            exc[0] = e
        finally:
            done.set()

    _threading.Thread(target=_run, daemon=True).start()
    if not done.wait(timeout=25):
        return jsonify({'error': 'Request timed out — try again.'}), 504
    if exc[0]:
        return jsonify({'error': str(exc[0])}), 500

    payload = _refresh_quote(result[0]) if cached[0] else result[0]
    return jsonify(payload)


# Headlines go stale far faster than fundamentals, so this gets a much shorter
# TTL than STOCK_TTL. Without any cache, flipping back to a ticker re-scraped
# every article body and paid for the Groq rewrite again — about 2.2s of pure
# repeat work.
NEWS_TTL = 300

_news_cache: dict = {}
_news_cache_lock = threading.RLock()


@app.route('/api/news', methods=['GET'])
def get_news():
    import time as _time
    tkkr = clean_ticker(request.args.get('ticker'))
    name = request.args.get('name', '').strip()
    if not tkkr:
        return jsonify({'news': []})
    # Fires in parallel with /api/stock, so it has to refuse independently —
    # otherwise hiding a stock still spends the account's API key scraping and
    # rewriting ten articles about it every time the symbol is searched.
    if tkkr in _blocked_set():
        return jsonify({'news': []})

    with _news_cache_lock:
        hit = _news_cache.get(tkkr)
        if hit and _time.time() - hit['ts'] < NEWS_TTL:
            print(f'[NEWS] {tkkr}: cache hit', flush=True)
            return jsonify({'news': hit['data']})

    # Resolved here, in the request, because the pipeline below runs on a thread
    # where there is no session to resolve it from.
    _groq_key_for_request = _resolve_api_key('GROQ_API_KEY')

    try:
        import datetime as _dt, re as _re, requests as _req
        from bs4 import BeautifulSoup
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

        # Run the entire news pipeline in a thread with a hard 25-second deadline
        import threading
        _result = [None]
        _done   = threading.Event()
        _t0 = _time.time()
        print(f'[NEWS] {tkkr}: starting pipeline', flush=True)

        def _run():
            try:
                _result[0] = _build_news(tkkr, name, groq_key=_groq_key_for_request)
            except Exception as e:
                print(f'[NEWS] {tkkr}: pipeline exception: {e}', flush=True)
                _result[0] = []
            finally:
                _done.set()

        threading.Thread(target=_run, daemon=True).start()
        finished = _done.wait(timeout=25)
        elapsed = _time.time() - _t0
        if not finished:
            print(f'[NEWS] {tkkr}: TIMED OUT after {elapsed:.1f}s', flush=True)
        else:
            print(f'[NEWS] {tkkr}: done in {elapsed:.1f}s, {len(_result[0] or [])} items', flush=True)

        items = _result[0] or []
        # Only cache a real result: caching [] would pin an empty news panel for
        # the whole TTL after one upstream hiccup.
        if items:
            with _news_cache_lock:
                _news_cache[tkkr] = {'data': items, 'ts': _time.time()}
        return jsonify({'news': items})

    except Exception as e:
        print(f'[NEWS] {tkkr}: outer exception: {e}', flush=True)
        return jsonify({'news': []})


_name_cache: dict = {}   # ticker -> longName, so this is resolved at most once


def _resolve_company_name(tkkr):
    """Best-effort company name, cheapest source first. Returns '' if unknown.

    Order matters for latency: a warm stock lookup already holds the name, so
    the common case costs nothing and never touches the network.
    """
    if tkkr in _name_cache:
        return _name_cache[tkkr]

    name = ''
    with _stock_cache_lock:
        hit = _stock_cache.get(tkkr)
    if hit:
        name = hit['data'].get('name') or ''

    if not name:
        try:
            info = yf.Ticker(tkkr).info or {}
            name = info.get('longName') or info.get('shortName') or ''
        except Exception:
            name = ''

    if name:
        _name_cache[tkkr] = name
    return name


# Listicle/opinion headlines, killed on sight. Hoisted to module level and
# precompiled because the market-news feed applies the same veto — see
# _impact_score. Measured against 319 live wire items it matched none of them:
# this pattern set is tuned for Yahoo's per-ticker listicles ("3 Reasons to Buy
# X"), which the wires don't publish. It stays as a cheap guard against a feed
# drifting downmarket, not as the filter that carries the market feed.
_HARD_SKIP_RE = tuple(__import__('re').compile(p) for p in [
    r'\bshould (you |i )?(buy|sell|hold|invest|own|avoid)\b',
    r'\b(top|best|worst)\s+\d*\s*(stocks?|picks?|buys?|investments?)\b',
    r'\bwhy\s+(i\s+)?(bought|sold|own|like|hate|am (buying|selling))\b',
    r'\b\d+\s+reason(s)?\s+(to|why)\b',
    r'\b(is|are)\s+[\w\s]{2,30}\s+(a\s+)?(good|great|bad|terrible)\s+(buy|investment|stock|bet)\b',
    r'\bhere.s why\b', r'\b(my|our)\s+(top|best|favourite|portfolio)\b',
    r'\bpassive income\b', r'\b(millionaire|retire\s+early|financial freedom)\b',
    r'\bprice target\b', r'\b(bull|bear)\s+case\b', r'\bsent(iment)?\s+anal(ysis)?\b',
    r'\bweek(ly)?\s+(wrap|recap|roundup|picks?)\b', r'\bmarket\s+(wrap|recap|roundup|movers?)\b',
    r'\bwhat\s+(analysts?|wall\s+street)\s+(think|say|expect)\b',
    r'\brated\s+(buy|sell|hold|overweight|underweight|outperform|underperform)\b',
    r'\b(overweight|underweight|outperform|underperform|neutral)\s+rating\b',
])


def _build_news(tkkr, name='', lite=False, groq_key=''):
    """Per-ticker news. `lite` stops after filtering and scoring — see below.

    groq_key is passed in because this runs on a worker thread: the account
    whose key should pay for the headline rewrite is only knowable in the
    request that started it. Empty means no rewrite, which is what the
    sentence-case fallback below is for. `lite` returns before the rewrite, so
    it never needs one.
    """
    import datetime as _dt, re as _re, requests as _req, time as _time
    from bs4 import BeautifulSoup
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

    _t = _time.time
    _t0 = _t()

    ticker = yf.Ticker(tkkr)

    # The company name drives headline scoring and the relevance filter below,
    # so it can't just be defaulted to the symbol — "Royal Bank of Canada"
    # articles don't contain the string "RY.TO" and would be dropped.
    #
    # The frontend used to supply it, which meant news could not start until
    # /api/stock had returned. Resolving it here instead lets the two run
    # concurrently. Cost is normally zero: a lookup for this ticker has almost
    # always populated _stock_cache already.
    name = name or _resolve_company_name(tkkr) or tkkr
    print(f'[NEWS] {tkkr}: ticker created in {_t()-_t0:.2f}s, name={name!r}', flush=True)

    def _fetch_article_text(url):
        try:
            r = _req.get(url, timeout=3, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                return ''
            soup = BeautifulSoup(r.text, 'html.parser')
            for tag in soup(['script','style','nav','header','footer','aside','figure','noscript']):
                tag.decompose()
            body = soup.find('article') or soup.find(attrs={'class': lambda c: c and 'article-body' in ' '.join(c)}) or soup.find('main') or soup.body
            if not body:
                return ''
            paras = [p.get_text(' ', strip=True) for p in body.find_all('p') if len(p.get_text(strip=True)) > 40]
            return ' '.join(paras[:40])
        except Exception:
            return ''

    def _smart_summary(text, name_words, ticker_sym, budget=180):
        action = {'announced','announces','launches','launched','reports','reported','beats','beat',
                  'misses','missed','cuts','cut','raises','raised','acquires','acquired','expands',
                  'expanded','files','filed','settles','settled','recalls','recalled','approves',
                  'approved','rejects','rejected','wins','won','loses','lost','appoints','appointed',
                  'resigns','resigned','partners','agreed','completes','completed','suspends',
                  'suspended','withdraws','withdrew','fined','charged','investigated','awarded'}
        filler = {'click here','read more','sign up','subscribe','according to sources',
                  'sources familiar','people familiar','it is unclear','remains to be seen',
                  'did not respond','could not be reached','no comment'}
        sents = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', text) if 25 < len(s.strip()) < 250]
        if not sents:
            return text[:budget]
        scored = []
        for i, s in enumerate(sents):
            sl = s.lower()
            score  = sum(8  for w in name_words if w in sl)
            score += 10 if ticker_sym.lower() in sl else 0
            score += sum(7  for a in action if a in sl)
            score += len(_re.findall(r'\d+\.?\d*\s*(%|\$|bn|million|billion)', sl)) * 6
            score += 4 if _re.search(r'\b(q[1-4]|fy\d{2,4}|full.year|fiscal)', sl) else 0
            score += max(0, 3 - i)
            score -= 15 if len(s) > 220 else 0
            score -= sum(8 for f in filler if f in sl)
            score -= 8  if s.startswith('"') else 0
            scored.append((score, i, s))
        scored.sort(key=lambda x: (-x[0], x[1]))
        best = scored[0][2]
        return best if len(best) <= budget else best[:budget].rsplit(' ', 1)[0] + '…'

    def _to_headline(text, short_name=''):
        text = _re.sub(r'^\([\w\.\-]+\)\s*[-–—]?\s*', '', text).strip()
        text = _re.sub(r'^[\"\u201c]', '', text).strip()
        text = _re.sub(r',?\s+according to [^.]{0,80}$', '', text, flags=_re.I).strip()
        text = _re.sub(r'[,;:\s]+$', '', text).strip()
        if not text:
            return ''
        system = (
            'You rewrite financial news sentences into concise, accurate headlines. '
            'Rules: max 15 words, title case, active voice, no attribution (remove "said/according to"), '
            'keep all specific numbers and figures, replace vague pronouns with the company name, '
            'output only the headline with no quotes or explanation.'
        )
        result = groq_call(system, f'Company: {short_name}\nSentence: {text}',
                           max_tokens=60, key=groq_key)
        return result if result else (text[0].upper() + text[1:])

    _now   = _dt.datetime.utcnow()
    _tier1 = {'reuters','bloomberg','wall street journal','wsj','financial times','ft','cnbc',"barron's",'the economist'}
    _tier2 = {'yahoo finance','marketwatch','seeking alpha','motley fool','business insider','fortune','forbes','associated press'}
    _action_kw = ['earnings','revenue','profit','loss','beat','miss','guidance','dividend','merger','acquisition',
                  'buyout','ipo','sec','fda','layoff','recall','settlement','buyback','split','bankruptcy',
                  'default','launch','partnership','contract','deal','appoints','resigns','expands','cuts',
                  'investigation','approved','rejected','fine','penalty','charges','files','reports',
                  'announces','raises','lowers','suspends','withdraws','completes','agrees','wins','loses']
    _opinion_sources = {'seeking alpha','motley fool','investopedia','the motley fool'}
    _stop       = {'inc','corp','ltd','llc','plc','the','and','group'}
    _name_words = {w.lower() for w in (name or '').split() if len(w) > 2 and w.lower() not in _stop}
    _short_name = name or tkkr

    _t1 = _t()
    raw_news = ticker.news or []
    print(f'[NEWS] {tkkr}: ticker.news fetched in {_t()-_t1:.2f}s, {len(raw_news)} raw items', flush=True)

    news = []
    for item in raw_news[:20]:
        c       = item.get('content', {}) or {}
        title   = c.get('title', item.get('title', ''))
        url     = c.get('canonicalUrl', {}).get('url', item.get('link', ''))
        if not (title and url):
            continue
        source  = c.get('provider', {}).get('displayName', item.get('publisher', ''))
        summary = c.get('summary', '') or c.get('description', '') or ''
        thumb   = ''
        try:
            res = (c.get('thumbnail') or item.get('thumbnail') or {}).get('resolutions') or []
            if res: thumb = max(res, key=lambda r: r.get('width', 0)).get('url', '')
        except Exception: pass

        raw_t  = c.get('pubDate', '') or item.get('providerPublishTime', '')
        pub_ts = None
        if isinstance(raw_t, int):
            pub_ts = _dt.datetime.utcfromtimestamp(raw_t)
            date   = pub_ts.strftime('%b %d, %Y')
        elif raw_t:
            try: pub_ts = _dt.datetime.strptime(str(raw_t)[:19], '%Y-%m-%dT%H:%M:%S')
            except Exception: pass
            date = str(raw_t)[:10]
        else:
            date = ''

        t_low = title.lower()
        src   = (source or '').lower()
        if any(p.search(t_low) for p in _HARD_SKIP_RE): continue
        if src in _opinion_sources and not any(kw in t_low for kw in _action_kw): continue

        score  = 40 if src in _tier1 else 20 if src in _tier2 else 0
        score += sum(20 for kw in _action_kw if kw in t_low)
        if pub_ts:
            score += max(0, 30 * (1 - max(0, (_now - pub_ts).total_seconds() / 3600) / 72))

        haystack = (title + ' ' + summary).lower()
        if tkkr.lower() not in haystack and not any(w in haystack for w in _name_words):
            continue

        news.append({'title': title, 'url': url, 'source': source, 'thumb': thumb,
                     'date': date, 'pub_ts': pub_ts.isoformat() if pub_ts else '',
                     'score': score, '_summary_fallback': summary})

    news.sort(key=lambda x: x['score'], reverse=True)
    print(f'[NEWS] {tkkr}: filtered to {len(news)} items after scoring', flush=True)

    # Everything above is one yfinance call. Everything below scrapes up to ten
    # article bodies and pays for a Groq rewrite per item — fine for the one
    # ticker the user is looking at, ruinous across a whole portfolio (nine
    # symbols would be ~135 threads and ~100 LLM calls under one 25s deadline).
    # The positions feed takes this exit; /api/news does not.
    if lite:
        for n in news:
            n.pop('_summary_fallback', None)
            n.pop('score', None)
        return news[:3]

    to_fetch  = news[:10]
    raw_texts = {id(n): n.pop('_summary_fallback', '') for n in news}
    _t2 = _t()
    with ThreadPoolExecutor(max_workers=5) as pool:
        fetch_futures = {pool.submit(_fetch_article_text, n['url']): n for n in to_fetch}
        try:
            for future in as_completed(fetch_futures, timeout=8):
                n = fetch_futures[future]
                try:
                    t = future.result()
                    if t: raw_texts[id(n)] = t
                except Exception: pass
        except FuturesTimeout: pass
    print(f'[NEWS] {tkkr}: article fetch done in {_t()-_t2:.2f}s', flush=True)

    raws = {}
    for n in news:
        src = raw_texts.get(id(n), '') or n['title']
        raws[id(n)] = _smart_summary(src, _name_words, tkkr) or n['title']

    _t3 = _t()
    with ThreadPoolExecutor(max_workers=10) as pool:
        headline_futures = {pool.submit(_to_headline, raws[id(n)], _short_name): n for n in news}
        try:
            for future in as_completed(headline_futures, timeout=10):
                n = headline_futures[future]
                try:
                    r = future.result()
                    if r: n['title'] = r
                except Exception: pass
        except FuturesTimeout: pass
    print(f'[NEWS] {tkkr}: Groq headlines done in {_t()-_t3:.2f}s', flush=True)

    for i, n in enumerate(news):
        n['top'] = i < 3
        del n['score']

    return news


import json, os

# ---------------------------------------------------------------------------
# JSON persistence
#
# Portfolio state lives in flat JSON files. Three things have to hold:
#
#   1. A write must never leave a half-written file. Opening with 'w' truncates
#      first, so a crash mid-write destroyed the data. Writes now go to a temp
#      file in the same directory and are swapped in with os.replace(), which
#      is atomic on Windows and POSIX alike.
#   2. Concurrent requests must not clobber each other. The server runs
#      threaded, so two overlapping load/append/save sequences would each read
#      the same list and the second would overwrite the first. mutate() holds a
#      per-file lock across the whole read-modify-write.
#   3. A failed write must be loud. The old helpers swallowed the exception and
#      the route still returned 200 OK while the data was gone.
# ---------------------------------------------------------------------------
# Overridable so a test or a sandbox can run against a copy of the data instead
# of the live portfolio. Defaults to the directory app.py sits in, which is where
# the files have always been.
_DATA_DIR = os.environ.get('DATA_DIR') or os.path.dirname(os.path.abspath(__file__))

# One lock shared by every portfolio store. Selling a holding writes holdings,
# sales, transactions and cash in one logical operation; per-file locks would
# make each write individually safe but leave the group interleavable, so a
# concurrent read could see cash already debited but the holding not yet gone.
# Sharing one re-entrant lock makes any group of portfolio writes atomic with
# respect to any other, and removes lock-ordering as a concern entirely.
PORTFOLIO_LOCK = threading.RLock()


class JsonStore:
    """Atomic, lock-protected JSON file backing one piece of portfolio state."""

    def __init__(self, filename, default, migrate=None, lock=None, directory=None):
        self.path     = os.path.join(directory or _DATA_DIR, filename)
        self._default = default
        self._migrate = migrate
        self._lock    = lock or threading.RLock()

    def load(self):
        """Parsed contents, or a fresh default if the file is absent.

        A file that exists but does not parse is an error, not an empty
        portfolio: raising here is what stops corruption from being silently
        reinterpreted as "you own nothing".
        """
        with self._lock:
            if not os.path.exists(self.path):
                return self._default() if callable(self._default) else self._default
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (ValueError, UnicodeDecodeError) as e:
                raise RuntimeError(f'{os.path.basename(self.path)} is corrupt: {e}') from e
            return self._migrate(data) if self._migrate else data

    def save(self, data):
        """Write atomically. Raises on failure rather than reporting success."""
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f'{self.path}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return data

    def mutate(self, fn):
        """Run fn(current) -> new under the lock and persist the result.

        This is the only safe way to do a read-modify-write: holding the lock
        across both halves is what prevents two requests from interleaving.
        """
        with self._lock:
            data = self.load()
            result = fn(data)
            new = data if result is None else result
            self.save(new)
            return new


# ---------------------------------------------------------------------------
# Per-user data
#
# The portfolio files are per account, and the separation is by *directory*
# rather than by an `owner` column filtered on read:
#
#     <data dir>/users/<username>/holdings.json
#
# A filter is something every read site has to remember, and there are dozens of
# them here — `_compute_invested`, `_replay_ledger`, the chart walk, the dividend
# walk, the positions news feed. Miss one and it silently reports another
# account's money, which is the failure mode that looks most like working code.
# With a directory per user, `load_holdings()` cannot return the wrong rows
# because it never reads the wrong file. Nothing downstream needs to know that
# accounts exist at all.
#
# The cost is that the owner must be resolved somewhere, and that is
# `_current_username()` — which raises rather than falling back to a shared
# file. A silent fallback is the leak this design exists to prevent.
# ---------------------------------------------------------------------------

_USER_DATA_ROOT = os.path.join(_DATA_DIR, 'users')

# filename -> (default, migrate). Populated at each store's definition site
# below, so a migration function is registered where it is defined.
_PER_USER_FILES: dict = {}

_user_stores: dict = {}          # (username, filename) -> JsonStore
_user_stores_lock = threading.Lock()


def _register_user_file(filename, default, migrate=None):
    _PER_USER_FILES[filename] = (default, migrate)
    return filename


def _user_data_dir(username):
    return os.path.join(_USER_DATA_ROOT, username)


def _current_username():
    """The signed-in username, or a hard failure.

    Every per-user store resolves through this. It raises outside a request, or
    with no session, instead of naming some default account: a per-user file
    that quietly falls back to a shared one is exactly the cross-account leak
    the directory layout is meant to make impossible. Background work that
    legitimately has no request — the report builder, the startup backfill —
    passes its owner in explicitly.
    """
    try:
        user = _current_user()
    except RuntimeError as e:        # no request context at all
        raise RuntimeError('per-user data was read outside a request; pass '
                           'owner= explicitly') from e
    if not user:
        raise RuntimeError('per-user data was read with no signed-in user')
    return user['username']


def _user_store(filename, owner=None):
    """The store holding `filename` for `owner`, defaulting to the current user."""
    username = owner or _current_username()
    key = (username, filename)
    with _user_stores_lock:
        store = _user_stores.get(key)
        if store is None:
            default, migrate = _PER_USER_FILES[filename]
            store = JsonStore(filename, default, migrate=migrate,
                              lock=PORTFOLIO_LOCK,
                              directory=_user_data_dir(username))
            _user_stores[key] = store
        return store


def _forget_user_stores(username):
    """Drop cached stores for an account, so a delete cannot be served stale."""
    with _user_stores_lock:
        for key in [k for k in _user_stores if k[0] == username]:
            del _user_stores[key]


def atomic(fn):
    """Hold PORTFOLIO_LOCK for the whole handler.

    Every mutating route does load -> modify -> save. Locking only inside save()
    would still let two requests both read the same list and have the second
    overwrite the first; the lock has to span the read and the write. Applied as
    a decorator so route bodies stay unindented.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with PORTFOLIO_LOCK:
            return fn(*args, **kwargs)
    return wrapper


@app.errorhandler(RuntimeError)
def _handle_store_error(e):
    """Surface a corrupt/unwritable data file instead of returning empty data."""
    print(f'[STORE] {e}', flush=True)
    return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Hidden stocks
#
# A block is a *discovery* filter, and that boundary is the whole design. Every
# route that offers a stock you did not ask for by name drops a hidden symbol on
# the way out — the tape, the movers strip, the market browser, the typeahead,
# the Canadian screener, the positions news feed, the exchange switcher, the
# watchlist — and /api/stock refuses the detail page outright, so searching a
# hidden symbol says so rather than quietly serving it.
#
# What it deliberately does not touch is the ledger. Holdings, transactions,
# sales, options and every figure derived from them are one arithmetic record:
# `realized + unrealized + dividends + option P/L == (cash_pool + market value)
# − invested` holds to a penny, and it holds because nothing filters those
# walks. Dropping a hidden position from `_compute_invested()` would not hide a
# stock — it would silently misreport the portfolio's return, which is the
# failure mode here that looks most like working code. So a hidden holding keeps
# counting, and POST /api/blocked reports `held` so the UI can say so out loud
# instead of leaving the user to notice.
#
# Nothing is deleted. A block hides rows; unblocking brings back the watchlist
# entry, the valuation and the notes exactly as they were. That is what makes
# this safe to reach for — the alternative, deleting on block, turns a change of
# mind into lost work.
#
# Matching is exact, on the full symbol. A root rule (block SHOP, hide SHOP.TO)
# reads as helpful right up until NA.TO takes NA with it; every store here is
# keyed by the full symbol, and a filter that hides more than it was asked to is
# indistinguishable from a bug.
# ---------------------------------------------------------------------------

BLOCKED_FILE = _register_user_file('blocked.json', list)

def load_blocked(owner=None):
    return _user_store(BLOCKED_FILE, owner).load()

def save_blocked(items, owner=None):
    _user_store(BLOCKED_FILE, owner).save(items)
    return True


def _blocked_set(owner=None):
    """This account's hidden symbols, as a set.

    Empty rather than raising when there is no session: the movers, tape and
    index caches are built on background threads and are shared across accounts
    anyway, so a builder with no owner has nothing to hide. The filtering
    happens per request, on the way out of the route that serves those caches —
    one shared payload, filtered differently for each reader.
    """
    try:
        rows = load_blocked(owner)
    except RuntimeError:
        return frozenset()
    return frozenset(t for t in (clean_ticker(r.get('ticker')) for r in rows) if t)


def _row_symbol(row):
    """The symbol a discovery row is identified by.

    `full_ticker` first because `ticker` is the display spelling on the movers
    and index rows — 'SHOP' for SHOP.TO — and matching that would hide a
    different company's listing on a different venue.
    """
    return (row.get('full_ticker') or row.get('ticker') or '').strip().upper()


def _drop_blocked(rows, owner=None):
    """`rows` minus anything this account has hidden."""
    hidden = _blocked_set(owner)
    if not hidden:
        return list(rows)
    return [r for r in rows if _row_symbol(r) not in hidden]


@app.route('/api/blocked', methods=['GET'])
def get_blocked():
    return jsonify({'items': load_blocked()})


@app.route('/api/blocked', methods=['POST'])
@atomic
def add_blocked():
    data   = request.json or {}
    ticker = clean_ticker(data.get('ticker'))
    if not ticker:
        return jsonify({'error': 'Invalid ticker'}), 400

    items = load_blocked()
    if not any(i.get('ticker') == ticker for i in items):
        items.append({
            'ticker':  ticker,
            'name':    str(data.get('name') or '')[:120],
            'blocked': _now_iso(),
        })
        save_blocked(items)

    # Said out loud rather than left to be discovered. Hiding a stock does not
    # unwind a position in it, and a Holdings tab still listing something you
    # just hid reads as the block having failed.
    held = any(h.get('ticker') == ticker for h in load_holdings())
    return jsonify({'items': items, 'held': held})


@app.route('/api/blocked/<ticker>', methods=['DELETE'])
@atomic
def remove_blocked(ticker):
    # Delete-by-ticker only filters this account's own file, so a raw compare is
    # fine here — the same rule the watchlist and valuations deletes follow.
    want  = (ticker or '').strip().upper()
    items = [i for i in load_blocked() if i.get('ticker') != want]
    save_blocked(items)
    return jsonify({'items': items})


WATCHLIST_FILE = _register_user_file('watchlist.json', list)

def load_watchlist(owner=None):
    return _user_store(WATCHLIST_FILE, owner).load()

def save_watchlist(items, owner=None):
    _user_store(WATCHLIST_FILE, owner).save(items)
    return True

@app.route('/api/watchlist', methods=['GET'])
def get_watchlist():
    # Filtered, not pruned: the stored row survives a block untouched, so
    # unhiding puts the entry back with the name and date it was added under.
    return jsonify(_drop_blocked(load_watchlist()))

@app.route('/api/watchlist', methods=['POST'])
@atomic
def add_to_watchlist():
    data = request.json or {}
    # Validate on the way in rather than on every read back out: a bare
    # .strip().upper() let watchlist.json hold a value that was never a symbol,
    # which is why _portfolio_symbols() has to re-filter the stored file.
    ticker = clean_ticker(data.get('ticker'))
    if not ticker:
        return jsonify({'error': 'Invalid ticker'}), 400
    # Unreachable from the UI — the stock page a watch is added from already
    # refuses a hidden symbol — but a route that accepts a write the GET then
    # filters out is an Add button that silently does nothing.
    if ticker in _blocked_set():
        return jsonify({'error': f'{ticker} is hidden. Unhide it first.',
                        'blocked': True}), 409
    items = load_watchlist()
    if not any(i['ticker'] == ticker for i in items):
        items.append({
            'ticker': ticker,
            'name':   data.get('name', ''),
            'added':  data.get('added', ''),
        })
        save_watchlist(items)
    return jsonify(items)

@app.route('/api/watchlist/<ticker>', methods=['DELETE'])
@atomic
def remove_from_watchlist(ticker):
    ticker = ticker.strip().upper()
    items = load_watchlist()
    items = [i for i in items if i['ticker'] != ticker]
    save_watchlist(items)
    return jsonify(items)


# ── Holdings ───────────────────────────────────────────────────────────────────

def _migrate_holdings(items):
    """Ensure the lots/avg_price fields added after the first schema exist."""
    for item in items:
        if 'lots' not in item:
            item['lots'] = [{'shares': float(item['shares']),
                             'price':  float(item['price']),
                             'date':   item.get('date_acquired', '')}]
        if 'avg_price' not in item:
            item['avg_price'] = float(item['price'])
    return items


HOLDINGS_FILE = _register_user_file('holdings.json', list, migrate=_migrate_holdings)

def load_holdings(owner=None):
    return _user_store(HOLDINGS_FILE, owner).load()

def save_holdings(items, owner=None):
    _user_store(HOLDINGS_FILE, owner).save(items)

# ── Corporate-action helpers ───────────────────────────────────────────────────
# ---------------------------------------------------------------------------
# Corporate actions are the dominant cost of a portfolio page load, and none of
# it used to be cached.
#
# One load calls _build_div_events 81 times for 22 distinct tickers:
# /api/portfolio/invested, /api/dividends/history, /api/holdings/chart and
# /api/transactions each re-derive the identical history from scratch. Each call
# is a .dividends fetch plus a .info fetch (the latter used only to derive the
# ex-date → pay-date lag). Splits were re-fetched per chart request on top of
# that. Measured: ~19s of upstream calls per load, ~4.8s of it splits.
#
# Dividend history and splits change quarterly at most, so the TTL is hours
# rather than the minutes used for quotes. Both caches hold ticker market data
# only — nothing derived from the portfolio — so the mutating routes have no
# reason to invalidate them.
# ---------------------------------------------------------------------------
CORP_ACTIONS_TTL = 6 * 3600


class _TtlCache:
    """TTL cache that collapses concurrent misses on one key into a single fetch.

    Same shape as the _stock_cache/_stock_inflight pair, factored out because the
    corporate-action endpoints all want the same ~20 tickers at the same moment:
    without the in-flight check a cold load fires every upstream call several
    times over.

    A fetch that raises is not cached. `_fetch_div_events` returns [] for a
    ticker that genuinely pays no dividend and raises when the lookup failed, so
    one bad response cannot pin dividend income to zero for the whole TTL.
    """

    def __init__(self, ttl):
        self.ttl       = ttl
        self._data     = {}   # key -> (value, ts)
        self._inflight = {}   # key -> Event
        self._lock     = threading.RLock()

    def get(self, key, fetch):
        import time as _time
        with self._lock:
            hit = self._data.get(key)
            if hit and _time.time() - hit[1] < self.ttl:
                return hit[0]
            waiting = self._inflight.get(key)
            if waiting is None:
                waiting = threading.Event()
                self._inflight[key] = waiting
                owner = True
            else:
                owner = False

        if not owner:
            waiting.wait(timeout=30)
            with self._lock:
                hit = self._data.get(key)
            if hit:
                return hit[0]
            # The owner failed or timed out. Fetch it ourselves rather than
            # raising — the caller treats a miss as "no corporate actions",
            # which would silently drop real dividend income.
            return fetch(key)

        try:
            value = fetch(key)
            with self._lock:
                self._data[key] = (value, _time.time())
            return value
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            waiting.set()

    def clear(self):
        with self._lock:
            self._data.clear()


_div_events_cache = _TtlCache(CORP_ACTIONS_TTL)
_splits_cache     = _TtlCache(CORP_ACTIONS_TTL)

# Annual fundamentals move once a quarter at most, and a lookup now fires five
# Macrotrends requests at one host instead of three. `scrape_macrotrends` raises
# rather than returning {} on a failed fetch, so a transient block is retried
# next time instead of pinning every chart to five years for the next six hours.
_mt_cache = _TtlCache(CORP_ACTIONS_TTL)


def _mt_cached(tkkr, metric, freq='A'):
    """`scrape_macrotrends` behind the shared TTL cache.

    `freq` is part of the key. The annual and quarterly series for one metric are
    different data under the same name, so sharing a key would serve whichever
    frequency happened to be asked for first — and since the annual lookup runs
    on every stock page and the quarterly one only on a toggle, that would
    reliably be the annual one.
    """
    return _mt_cache.get((tkkr.split('.')[0].upper(), metric, freq),
                         lambda k: scrape_macrotrends(k[0], k[1], freq=k[2]))


# ---------------------------------------------------------------------------
# Quarterly view
#
# `_do_get_stock` builds the annual charts and stays the load path. This builds
# the same five income-statement series by quarter — up to 157 of them, back to
# 1987 for Apple — plus FCF and capex, which have no quarterly page anywhere and
# so get yfinance's five or six columns and a caption saying why.
#
# A second route rather than five more scrapes on `/api/stock`, because only the
# toggle wants this. A lookup that never flips it pays nothing; the first flip
# per ticker costs one round trip and every flip after that is free on both
# sides. Same argument the account prefetch makes about the load path: what
# everybody waits for should not carry what only some people use.
#
# The merge gate is reused rather than reimplemented. `_merge_macrotrends` and
# `_mt_check` never look at what a key *means* — they intersect, sort and
# compare — so quarter keys pass through the same take-or-drop-whole rule the
# annual series go through, with the same tolerances and the same logging.
# ---------------------------------------------------------------------------

# Quarterly fundamentals change on a filing, so this rides the same six hours as
# the scrapes behind it. `_mt_cache` already holds the expensive half; this saves
# re-walking four yfinance frames and re-running five merges on every toggle.
_quarterly_cache = _TtlCache(CORP_ACTIONS_TTL)

# Narrower than `_PREFETCH_PROPS`: this payload reads four frames and `info`, and
# has no use for dividends, earnings dates or the two estimate frames.
_Q_PREFETCH_PROPS = ('info', 'financials', 'quarterly_financials',
                     'quarterly_cashflow', 'quarterly_balance_sheet')

# Why the two cash-flow series are short. Shown under those charts only, because
# a five-bar chart beside a 157-bar one otherwise reads as a data failure.
_Q_CASHFLOW_NOTE = ('Yahoo Finance reports five to six quarters. Macrotrends '
                    'publishes no quarterly cash-flow statement, so there is '
                    'nothing deeper to merge in.')


def _frame(ticker, name):
    """One yfinance frame, or None if it failed. Warmed by the prefetch above."""
    try:
        df = getattr(ticker, name)
        return None if df is None or getattr(df, 'empty', True) else df
    except Exception:
        return None


def _fye_month_opt(fin):
    """The month a filer's fiscal year ends, or None when the frame can't say.

    Every annual column shares it, so the newest one answers. The one parse
    site; `_fye_month` is this with a December default laid over it.

    The distinction is not pedantry. `_quarter_label` only ever puts the month
    in a *label*, so guessing December there is wrong in a caption and nowhere
    else. The forward-guidance panel dates a fiscal period against it — turn
    "unknown" into December for an offset filer there and `FY2027` is captioned
    `Jan – Dec 2027` when it means Jul 2026 – Jun 2027, which is a whole year
    wrong and reads exactly like a figure that was looked up. That consumer
    takes this one and shows no span rather than a guessed one.
    """
    try:
        cols = sorted(fin.columns)
        if cols:
            return int(pd.Timestamp(cols[-1]).month)
    except Exception:
        pass
    return None


def _fye_month(fin):
    """The month a filer's fiscal year ends, defaulting to December.

    Right for the large majority, and wrong only in a label rather than in a
    value, since `_quarter_label` is the sole consumer. Anything that computes
    a *date* from this wants `_fye_month_opt`.
    """
    fye = _fye_month_opt(fin)
    return 12 if fye is None else fye


def _q_row_map(row):
    """{quarter key: float} from one row of a yfinance quarterly frame."""
    out = {}
    if row is None:
        return out
    for ts, val in row.items():
        v = _finite(val)
        if v is not None:
            out[_quarter_key(pd.Timestamp(ts))] = float(v)
    return out


def _q_rows(merged, src, fye, fmt, extra_key=None):
    """A merged {quarter key: value} dict as ordered chart rows, oldest first.

    Carries `period` (the merge key) and `label` (the fiscal quarter) rather than
    the annual shape's `year`, so nothing downstream can mistake one shape for
    the other. The frontend reads `label ?? year` and draws either.

    Sorting on the key works because `_quarter_key` is zero-padded 'YYYY-MM', so
    lexicographic order is chronological — the same property `_new_txn_id` relies
    on, and the same reason neither needs parsing to order correctly.
    """
    rows = []
    for qk in sorted(merged):
        v = merged[qk]
        if v is None:
            continue
        row = {'period': qk, 'label': _quarter_label(qk, fye),
               'raw': v, 'value': fmt(v), 'src': src.get(qk, 'yf')}
        if extra_key:
            # The annual margin chart reads `margin`, not `raw`. Carrying both
            # lets one frontend code path draw either frequency.
            row[extra_key] = v
        rows.append(row)
    return rows


def _fetch_quarterly(tkkr):
    """Every quarterly series for one ticker. Raises if the lookup itself failed.

    Behind `_TtlCache`, so the distinction `_fetch_div_events` draws applies
    here too: a transport failure has to raise rather than return an empty
    payload, or one bad moment pins every quarterly chart empty for six hours.
    A series that is legitimately absent — FCF on a filer yfinance has no
    cash-flow frame for — is a real answer inside a payload that built, and is
    cached with the rest.
    """
    from concurrent.futures import ThreadPoolExecutor

    ticker = yf.Ticker(tkkr)

    # Submitted before the yfinance prefetch so the scrapes overlap it and each
    # other — the ordering `_do_get_stock` uses, for the same reason. The budget
    # in `_MtScrapes` starts here, at submission, not at the first read.
    mt = _MtScrapes({})
    if '.' not in tkkr:
        ex = ThreadPoolExecutor(max_workers=len(_MT_QUARTERLY_METRICS))
        try:
            mt = _MtScrapes({m: ex.submit(_mt_cached, tkkr, m, 'Q')
                             for m in _MT_QUARTERLY_METRICS})
        finally:
            ex.shutdown(wait=False)

    def _touch(prop):
        try:
            getattr(ticker, prop)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=len(_Q_PREFETCH_PROPS)) as pex:
        list(pex.map(_touch, _Q_PREFETCH_PROPS))

    info = ticker.info or {}
    if not info or len(info) < 5:
        raise ValueError(f"Could not retrieve data for '{tkkr}'. Check the symbol.")

    # Statement figures follow the filing currency, never the listing's — the
    # split every money figure on the stock page already observes.
    fin_sym = _currency_symbol(info.get('financialCurrency') or info.get('currency') or 'USD')

    q_fin = _frame(ticker, 'quarterly_financials')
    q_cf  = _frame(ticker, 'quarterly_cashflow')
    q_bs  = _frame(ticker, 'quarterly_balance_sheet')
    fye   = _fye_month(_frame(ticker, 'financials'))

    money  = lambda v: format_large_number(v, fin_sym)
    eps_fm = lambda v: ('-' if v < 0 else '') + fin_sym + _eps_str(abs(v))

    yf_rev = _q_row_map(_df_row(q_fin, 'Total Revenue'))
    yf_ni  = _q_row_map(_df_row(q_fin, 'Net Income', 'Net Income Common Stockholders'))

    out = {'ticker': tkkr, 'fiscal_year_end_month': fye,
           'financial_currency': info.get('financialCurrency') or info.get('currency') or 'USD',
           'notes': {}}

    # --- Revenue / net income -------------------------------------------------
    rev_m, rev_s, _ = _merge_macrotrends(yf_rev, _mt_quarters(_mt_result(mt, 'revenue')), 'revenue')
    out['revenue_by_q'] = _q_rows(rev_m, rev_s, fye, money)

    ni_m, ni_s, _ = _merge_macrotrends(yf_ni, _mt_quarters(_mt_result(mt, 'earnings')), 'earnings')
    out['earnings_by_q'] = _q_rows(ni_m, ni_s, fye, money)

    # --- Net profit margin ----------------------------------------------------
    # Computed per quarter from that quarter's own two rows, never by pairing a
    # quarter's earnings with a trailing revenue — the balance-sheet rule in a
    # different place.
    yf_pm = {}
    for qk, ni in yf_ni.items():
        rev = yf_rev.get(qk)
        if rev:
            yf_pm[qk] = round(ni / rev * 100, 2)
    pm_m, pm_s, _ = _merge_macrotrends(yf_pm, _mt_quarters(_mt_result(mt, 'margin')), 'margin')
    out['margin_by_q'] = _q_rows(pm_m, pm_s, fye, lambda v: f'{v:.2f}%', extra_key='margin')

    # --- EPS ------------------------------------------------------------------
    yf_eps = _q_row_map(_df_row(q_fin, 'Diluted EPS', 'Basic EPS'))
    eps_reported = bool(yf_eps)
    if not yf_eps:
        sh = _q_row_map(_df_row(q_fin, 'Diluted Average Shares', 'Basic Average Shares'))
        for qk, ni in yf_ni.items():
            if sh.get(qk):
                yf_eps[qk] = ni / sh[qk]
    # Same widening as the annual path: a derived EPS sits several percent off
    # Macrotrends' as-reported diluted figure without either being wrong, so the
    # gate only tightens when yfinance handed us the reported row.
    eps_m, eps_s, _ = _merge_macrotrends(
        yf_eps, _mt_quarters(_mt_result(mt, 'eps')), 'eps',
        tol_rel=None if eps_reported else 0.15)
    out['eps_by_q'] = _q_rows(eps_m, eps_s, fye, eps_fm)

    # --- Share count ----------------------------------------------------------
    # `min_overlap=0` matches `_build_shares_history`: Macrotrends' share history
    # routinely starts where yfinance's balance sheet stops, leaving nothing to
    # agree on, and `_MT_SHARES_MIN` is the gate that actually applies.
    yf_sh = {k: v for k, v in _q_row_map(_df_row(q_bs, 'Ordinary Shares Number', 'Share Issued')).items() if v > 0}
    mt_sh = {k: float(v) for k, v in _mt_quarters(_mt_result(mt, 'shares')).items() if v and v > 0}
    sh_m, sh_s, _ = _merge_macrotrends(yf_sh, mt_sh, 'shares', min_overlap=0)
    out['shares_by_q'] = _q_rows(sh_m, sh_s, fye, _fmt_count)

    # --- Free cash flow / capex ----------------------------------------------
    # yfinance alone. Nothing to merge and nothing to gate, so these skip
    # `_merge_macrotrends` entirely rather than calling it with an empty dict.
    yf_fcf = _q_row_map(_df_row(q_cf, 'Free Cash Flow'))
    out['fcf_by_q'] = _q_rows(yf_fcf, {}, fye, money)

    yf_capex = {k: abs(v) for k, v in
                _q_row_map(_df_row(q_cf, 'Capital Expenditure', 'Capital Expenditures')).items()}
    out['capex_by_q'] = _q_rows(yf_capex, {}, fye, money)

    if out['fcf_by_q']:
        out['notes']['fcf'] = _Q_CASHFLOW_NOTE
    if out['capex_by_q']:
        out['notes']['capex'] = _Q_CASHFLOW_NOTE

    return out


@app.route('/api/stock/quarterly', methods=['GET'])
def get_stock_quarterly():
    """The quarterly twin of `/api/stock`, fetched when the toggle is first flipped."""
    import threading as _threading

    tkkr = clean_ticker(request.args.get('ticker'))
    if not tkkr:
        return jsonify({'error': 'No valid ticker provided'}), 400

    # Refused independently rather than leaning on `/api/stock` having already
    # refused: this route is reachable on its own, and the cache below is shared
    # by every account, so the account-specific question comes first.
    if tkkr in _blocked_set():
        return jsonify({'error': f'{tkkr} is hidden.',
                        'blocked': True, 'ticker': tkkr}), 403

    result, exc = [None], [None]
    done = _threading.Event()

    def _run():
        try:
            result[0] = _quarterly_cache.get(tkkr, _fetch_quarterly)
        except Exception as e:
            exc[0] = e
        finally:
            done.set()

    _threading.Thread(target=_run, daemon=True).start()
    if not done.wait(timeout=25):
        return jsonify({'error': 'Request timed out — try again.'}), 504
    if exc[0]:
        return jsonify({'error': str(exc[0])}), 500
    return jsonify(result[0])


def _fetch_div_events(tkr):
    """Uncached dividend lookup. Raises if the lookup itself failed.

    Returns [] only for a ticker that pays no dividend — the distinction is what
    lets _TtlCache store a genuine empty result without also storing a transient
    network failure.

    yfinance indexes dividends by ex-date.  Pay date is derived from the lag between
    the upcoming exDividendDate and dividendDate in ticker.info — this lag is applied
    uniformly to all historical ex-dates.  Falls back to ex-date if unavailable.
    """
    import datetime as _dt_mod
    t = yf.Ticker(tkr)
    divs = t.dividends
    if divs is None or divs.empty:
        return []
    divs.index = divs.index.tz_localize(None) if divs.index.tz is not None else divs.index

    pay_lag = None
    try:
        info   = t.info
        ex_ts  = info.get('exDividendDate')
        pay_ts = info.get('dividendDate')
        if ex_ts and pay_ts and int(ex_ts) > 0 and int(pay_ts) > 0:
            ex_dt  = _dt_mod.datetime.fromtimestamp(int(ex_ts)).date()
            pay_dt = _dt_mod.datetime.fromtimestamp(int(pay_ts)).date()
            lag    = (pay_dt - ex_dt).days
            if 0 < lag < 180:
                pay_lag = lag
    except Exception:
        pass

    events = []
    for dt, val in divs.items():
        ex_date  = dt.date() if hasattr(dt, 'date') else dt
        pay_date = (ex_date + _dt_mod.timedelta(days=pay_lag)) if pay_lag is not None else ex_date
        events.append((ex_date, pay_date, float(val)))
    return events


def _build_div_events(tkr):
    """Return list of (ex_date, pay_date, amount_per_share) for all historical dividends.

    Cached for CORP_ACTIONS_TTL. Never raises: a failed lookup yields [] and is
    not cached, so the next request retries.
    """
    try:
        return _div_events_cache.get(tkr, _fetch_div_events)
    except Exception:
        return []


def _fetch_splits(tkr):
    """Uncached split lookup: {ex_date: ratio} over all history. Raises on failure.

    Ratios of 0 and 1 are dropped — they are no-ops that only complicate the
    position adjustment downstream.
    """
    s = yf.Ticker(tkr).splits
    if s is None or s.empty:
        return {}
    return {dt.date(): float(v) for dt, v in s.items() if v and float(v) != 1.0}


def _build_splits(tkr):
    """{ex_date: ratio} for every split on record, e.g. 2-for-1 => 2.0.

    Cached for CORP_ACTIONS_TTL. Never raises — see _build_div_events.
    """
    try:
        return _splits_cache.get(tkr, _fetch_splits)
    except Exception:
        return {}


def _warm_corp_actions(tickers):
    """Prime the dividend cache for many tickers concurrently.

    /api/dividends/history and /api/transactions consume dividend events inside
    plain sequential loops, which on a cold cache serialises ~20 upstream
    fetches (measured: 7.6s and 5.2s). The other two portfolio endpoints already
    fan out 8 threads wide. Warming first lets those loops run against a hot
    cache without restructuring them.

    Best-effort: a failure here just means the loop fetches as it goes.
    """
    tickers = [t for t in dict.fromkeys(tickers) if t]
    if len(tickers) < 2:
        return
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_build_div_events, tickers))
    except Exception:
        pass


def _build_div_pay_map(tkr):
    """Return {pay_date: amount_per_share} — for crediting cash on actual pay date."""
    return {pay_date: amount for _, pay_date, amount in _build_div_events(tkr)}


# ── Cash ───────────────────────────────────────────────────────────────────────
# Cash is DERIVED, never stored. It is the leftover of the same cash-pool walk
# that produces `invested`: sell proceeds and dividends in, buy costs out.
#
# It used to live in cash.json as a running balance, incremented on every buy,
# sell, option leg and manual deposit. That is the `sales.json` mistake again —
# six write sites maintained it forward and nothing reversed it, so deleting or
# editing a transaction rebuilt holdings and sales and left the cash balance
# reporting money that trade no longer explained. A derived number cannot drift
# from the ledger, because it *is* the ledger.
#
# Consequence for the Holdings tab: `invested` was already being reduced by the
# pool the model spent, so displaying cash.json instead of the pool dropped
# whatever proceeds were sitting un-redeployed straight out of the P/L.

def load_cash():
    """The un-redeployed cash the ledger implies. Derived; there is no setter."""
    return _compute_invested()['cash_pool']

@app.route('/api/cash', methods=['GET'])
def get_cash():
    return jsonify({'balance': load_cash()})

def _compute_invested():
    """Cash-pool model: sell proceeds + dividends go into a pool; buys draw from pool first.
    Only the shortfall (cost exceeding the pool) counts as new external money.

    Returns {'invested', 'cash_pool', 'pending_cash'}.

    Extracted from the route so /api/portfolio/performance can report its return
    against the same denominator the Holdings tab uses. The two tabs were each
    computing "how much did I put in" their own way — Holdings against external
    capital, Performance against gross cost basis — and reporting +13.28% and
    +2.87% for one portfolio on the same day. Whatever the model, both tabs
    divide by this number now, so they can only disagree if the numerators do.
    """
    from datetime import datetime as _dt
    txns = load_transactions()
    parsed = []
    for t in txns:
        try:
            parsed.append({
                'date':   _dt.strptime(t['date'], '%Y-%m-%d'),
                'type':   t['type'],
                'ticker': t.get('ticker', ''),
                'shares': float(t.get('shares', 0)),
                'price':  float(t.get('price', 0)),
                # Ids are STRINGS: _new_txn_id() returns '<ms>-<hex>'. int() on
                # that raises, and the except below swallows it — the whole
                # transaction vanished from the cash pool. Every trade recorded
                # since the id got its random suffix was silently dropped here,
                # so `invested` under-counted the capital while the shares those
                # buys created still showed up in holdings: pure phantom gain.
                # The ms-timestamp prefix makes a lexicographic sort match the
                # numeric one, which is what rebuild_holdings_from_transactions
                # already relies on.
                'id':     str(t.get('id') or ''),
            })
        except Exception:
            pass
    parsed.sort(key=lambda x: (x['date'], x['id']))

    import datetime as _dt_mod
    today = _dt_mod.date.today()

    positions = {}
    cash = 0.0
    invested = 0.0

    # Fetch full dividend events (ex_date, pay_date, amount) for all tickers
    tickers = list({t['ticker'] for t in parsed if t['ticker']})
    div_events_map = {}  # tkr -> [(ex_date, pay_date, amount)]
    try:
        from concurrent.futures import ThreadPoolExecutor
        def _get_div_events(tkr):
            return tkr, _build_div_events(tkr)
        with ThreadPoolExecutor(max_workers=8) as ex:
            for tkr, events in ex.map(_get_div_events, tickers):
                if events:
                    div_events_map[tkr] = events
    except Exception:
        pass

    # Two-phase dividend processing — eligibility is based on shares at EX-DATE,
    # but cash is only received on PAY-DATE.
    #
    # Event sort order within a day:
    #   0 = div_pay   : credit cash before same-day buys (so dividend funds same-day purchases)
    #   1 = txn       : sells then buys (stable sort preserves parsed order: sells first)
    #   2 = div_snap  : snapshot shares AFTER all trades on ex-date (for eligibility check)
    #   3 = div_pay   : ...but when pay_date == ex_date the credit MUST follow the
    #                   snapshot it reads, or it reads a snapshot that does not
    #                   exist yet and the lookup below defaults to 0 shares.
    #
    # yfinance reports one date per distribution, so _build_div_events falls back
    # to pay_date = ex_date whenever a separate pay date is unknown — the whole
    # dividend then silently vanished from the cash pool. That was $64.40 across
    # T.TO, UNH.TO and ZMMK.TO here: every one of their distributions has
    # pay == ex, so each ticker credited exactly nothing. Dropping the credit
    # inflates `invested`, which is the denominator of the Holdings tab return.
    #
    # A dividend paid on its own ex-date cannot fund a buy earlier that day
    # anyway, so ordering it last costs nothing the priority-0 case was buying.
    #
    # div_snapshots[(tkr, ex_date)] = shares held at close of ex-date
    div_snapshots = {}

    all_events = []
    for t in parsed:
        all_events.append((t['date'].date(), 1, 'txn', t))

    for tkr, events in div_events_map.items():
        for ex_date, pay_date, amount in events:
            # Snapshot shares at close of ex-date (always, whether received or pending)
            all_events.append((ex_date, 2, 'div_snap', (tkr, ex_date)))
            if pay_date <= today:
                # Credit cash at start of pay-date using the ex-date snapshot
                order = 0 if pay_date > ex_date else 3
                all_events.append((pay_date, order, 'div_pay', (tkr, ex_date, amount)))
            # else: ex_date <= today handled below via div_snap; pay_date > today → pending

    # Option legs draw from and return to the same pool as share trades. They
    # have to be in this walk now that cash is derived from it: the premium on
    # an open contract is capital that has left the pool, and a closed contract's
    # proceeds are cash sitting in it. Omitting them would silently write those
    # dollars out of the portfolio the moment cash.json stopped being read.
    #
    # Priority 1 alongside share trades — a buy and a sell on one day resolve in
    # the order the list gives them, same as the stock legs.
    for opt in load_options():
        try:
            contracts = int(opt.get('contracts', 1))
            buy_price = float(opt.get('buy_price') or 0)
            if opt.get('buy_date'):
                all_events.append((_dt.strptime(opt['buy_date'], '%Y-%m-%d').date(), 1,
                                   'opt_buy', buy_price * contracts * 100))
            if opt.get('status') == 'closed' and opt.get('sell_price') is not None and opt.get('sell_date'):
                all_events.append((_dt.strptime(opt['sell_date'], '%Y-%m-%d').date(), 1,
                                   'opt_sell', float(opt['sell_price']) * contracts * 100))
        except Exception:
            pass

    all_events.sort(key=lambda x: (x[0], x[1]))

    for _date, _order, etype, data in all_events:
        if etype == 'txn':
            t = data
            tk, sh, pr = t['ticker'], t['shares'], t['price']
            if t['type'] == 'buy':
                positions[tk] = positions.get(tk, 0) + sh
                cost = sh * pr
                invested += max(0.0, cost - cash)
                cash = max(0.0, cash - cost)
            elif t['type'] == 'sell':
                positions[tk] = positions.get(tk, 0) - sh
                cash += sh * pr
        elif etype == 'opt_buy':
            invested += max(0.0, data - cash)
            cash = max(0.0, cash - data)
        elif etype == 'opt_sell':
            cash += data
        elif etype == 'div_snap':
            tkr, ex_date = data
            div_snapshots[(tkr, ex_date)] = positions.get(tkr, 0)
        elif etype == 'div_pay':
            tkr, ex_date, amount = data
            shares = div_snapshots.get((tkr, ex_date), 0)
            if shares > 0:
                cash += shares * amount

    # Pending: ex-date passed (snapshot recorded), pay-date hasn't arrived yet.
    pending_cash = 0.0
    for tkr, events in div_events_map.items():
        for ex_date, pay_date, amount in events:
            if ex_date <= today < pay_date:
                shares = div_snapshots.get((tkr, ex_date), 0)
                if shares > 0:
                    pending_cash += shares * amount

    return {
        'invested':     round(invested,     2),
        'cash_pool':    round(cash,         2),
        'pending_cash': round(pending_cash, 2),
    }


@app.route('/api/portfolio/invested', methods=['GET'])
def portfolio_invested():
    return jsonify(_compute_invested())

@app.route('/api/cash', methods=['POST'])
def update_cash():
    """Gone: cash is derived from the ledger, so there is nothing to set.

    A deposit that funds no trade does not change the portfolio — the cash-pool
    model already treats external capital as arriving exactly when a buy needs
    it. Kept as an explicit 410 rather than deleted so an old cached page gets
    an answer it can't mistake for success.
    """
    return jsonify({
        'error': 'Cash is derived from transactions and is no longer settable.',
        'balance': load_cash(),
    }), 410

@app.route('/api/holdings', methods=['GET'])
def get_holdings():
    return jsonify(load_holdings())

@app.route('/api/holdings', methods=['POST'])
@atomic
def add_to_holdings():
    data   = request.json or {}
    # This writes the ledger and rebuild_holdings_from_transactions() copies the
    # symbol into holdings.json, so one unvalidated POST lands in two files.
    ticker = clean_ticker(data.get('ticker'))
    if not ticker:
        return jsonify({'error': 'Invalid ticker'}), 400
    try:
        shares = float(data.get('shares', 0))
        price  = float(data.get('price',  0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid shares or price'}), 400
    if shares <= 0 or price <= 0:
        return jsonify({'error': 'Shares and price must be positive'}), 400

    date_acquired = (data.get('date_acquired') or '').strip()
    name          = (data.get('name') or ticker).strip()

    # Record the transaction first, then rebuild holdings from the complete
    # transaction history — this ensures a full-sell-then-re-buy correctly
    # resets avg_price rather than blending with the old position.
    _record_transaction('buy', ticker, name, shares, price, date_acquired)
    holdings = rebuild_holdings_from_transactions()
    # No cash write: the buy is in the ledger, which is what cash is derived from.
    return jsonify(holdings)

@app.route('/api/holdings/<ticker>', methods=['DELETE'])
@atomic
def remove_from_holdings(ticker):
    ticker = ticker.strip().upper()
    items  = [i for i in load_holdings() if i['ticker'] != ticker]
    save_holdings(items)
    return jsonify(items)

# ── Sales ──────────────────────────────────────────────────────────────────────
SALES_FILE = _register_user_file('sales.json', list)

def load_sales(owner=None):
    return _user_store(SALES_FILE, owner).load()

def save_sales(items, owner=None):
    _user_store(SALES_FILE, owner).save(items)

@app.route('/api/sales', methods=['GET'])
def get_sales():
    return jsonify(load_sales())

def _replay_ledger(txns):
    """Chronological weighted-average-cost replay of transactions.json.

    transactions.json is the canonical record: holdings.json is rebuilt from it,
    and a sell's cost basis is the WAC *at the moment it executed*, which only a
    replay can recover — a sell-everything-then-rebuy cycle leaves no trace of
    the old average anywhere else.

    sales.json is a parallel ledger that DELETE and PUT on a transaction never
    update. Nothing may compute P/L from it; see portfolio_performance().

    Returns (state, sell_wac): state maps ticker -> running aggregate, sell_wac
    maps a sell transaction's id -> the WAC in force when it executed.
    """
    from datetime import datetime as _dtmod

    def _parse_d(d):
        try:    return _dtmod.strptime(d, '%Y-%m-%d')
        except: return _dtmod.min

    state, sell_wac = {}, {}

    def _slot(tkr, name):
        s = state.setdefault(tkr, {
            'name': tkr, 'shares': 0.0, 'avg_price': 0.0,
            'realized_pl': 0.0, 'cost_basis_sold': 0.0,
            'first_buy_date': None, 'last_sale_date': None,
        })
        if name and s['name'] == tkr:
            s['name'] = name
        return s

    # Sells sort before buys on a shared date — same key /api/transactions used,
    # so the Transactions and Performance tabs cannot disagree.
    for t in sorted(txns, key=lambda t: (_parse_d(t.get('date', '')),
                                         0 if t.get('type') == 'sell' else 1)):
        tkr = (t.get('ticker') or '').upper()
        if not tkr:
            continue
        s    = _slot(tkr, t.get('name'))
        date = t.get('date') or ''
        sh   = float(t.get('shares') or 0)
        pr   = float(t.get('price')  or 0)

        if t.get('type') == 'buy':
            total          = s['shares'] + sh
            s['avg_price'] = (s['shares'] * s['avg_price'] + sh * pr) / total if total > 0 else pr
            s['shares']    = total
            if date and (s['first_buy_date'] is None or date < s['first_buy_date']):
                s['first_buy_date'] = date
        elif t.get('type') == 'sell':
            wac = s['avg_price']
            sell_wac[t.get('id')] = wac
            s['realized_pl']     += (pr - wac) * sh
            s['cost_basis_sold'] += wac * sh
            s['shares'] = max(0.0, s['shares'] - sh)
            if s['shares'] <= 1e-9:
                s['avg_price'] = 0.0
            if date and (s['last_sale_date'] is None or date > s['last_sale_date']):
                s['last_sale_date'] = date

    return state, sell_wac


@app.route('/api/portfolio/performance', methods=['GET'])
def portfolio_performance():
    """Per-ticker all-time performance across every stock ever invested in.

    Realized P/L and the cost basis of sold shares come from `_replay_ledger()`
    over transactions.json. They must **not** come from sales.json: deleting or
    editing a transaction rebuilds holdings but leaves that file untouched, so a
    corrected fat-fingered price or a removed trade keeps reporting its original
    gain here forever. That drift is invisible — every other tab replays the
    ledger, so only this one goes wrong.

    Currently-held shares come from holdings.json, which is what the Holdings tab
    renders, so the unrealized leg matches it. The frontend fetches live quotes
    to add that leg.

    Aligned with the Holdings tab. Two things used to make this tab disagree
    with it about the same portfolio on the same day (+2.87% here against
    +13.28%):

    `dividends` — cash actually received per ticker, from the same walk the
    Dividends tab renders. Holdings has always counted dividends in its return;
    this tab counted only price movement, which understates a book held for
    income. A ticker sold at a loss that paid its way is not a losing position.

    `invested` — the portfolio-level denominator, external capital only, from
    the same `_compute_invested()` the Holdings tab divides by. Summing
    `cost_basis_sold + held_cost` instead counts every dollar again each time it
    was recycled: $68,655 of "invested" against $24,556 that ever left the bank,
    because a money-market parking round-trip re-books its own basis on every
    lap. Per position that sum is still the right denominator — it is the
    capital that position consumed — so the per-box percentages keep using it.
    """
    txns      = load_transactions()
    holdings  = load_holdings()
    ledger, _ = _replay_ledger(txns)
    div_by_tkr = _dividends_received_by_ticker(txns)

    agg = {}  # ticker -> aggregate

    def _ensure(tkr, name):
        if tkr not in agg:
            led = ledger.get(tkr, {})
            agg[tkr] = {
                'name':            name or led.get('name') or tkr,
                'held_shares': 0.0, 'avg_cost': 0.0, 'held_cost': 0.0,
                'realized_pl':     led.get('realized_pl', 0.0),
                'cost_basis_sold': led.get('cost_basis_sold', 0.0),
                'first_date':      led.get('first_buy_date'),
                'last_sale_date':  led.get('last_sale_date'),
                'still_held':      False,
            }
        return agg[tkr]

    # Every ticker ever traded, closed positions included
    for tkr in ledger:
        _ensure(tkr, None)

    # Currently-held positions supply the held leg
    for h in holdings:
        tkr = (h.get('ticker') or '').upper()
        if not tkr:
            continue
        a      = _ensure(tkr, h.get('name'))
        shares = float(h.get('shares') or 0)
        avg    = float(h.get('avg_price') or h.get('price') or 0)
        a['held_shares'] = shares
        a['avg_cost']    = avg
        a['held_cost']   = shares * avg
        a['still_held']  = shares > 1e-9
        if h.get('name'):
            a['name'] = h['name']
        # A holding recorded outside the ledger still needs an acquisition date
        if not a['first_date']:
            dates = [d for d in [h.get('date_acquired')] +
                                [l.get('date') for l in (h.get('lots') or [])] if d]
            a['first_date'] = min(dates) if dates else None

    out = []
    for tkr, a in agg.items():
        out.append({
            'ticker':          tkr,
            'name':            a['name'],
            'held_shares':     round(a['held_shares'], 6),
            'avg_cost':        round(a['avg_cost'], 4),
            'held_cost':       round(a['held_cost'], 2),
            'realized_pl':     round(a['realized_pl'], 2),
            'cost_basis_sold': round(a['cost_basis_sold'], 2),
            'dividends':       round(div_by_tkr.get(tkr, 0.0), 2),
            'first_date':      a['first_date'],
            'last_sale_date':  a['last_sale_date'],
            'still_held':      a['still_held'],
        })

    # The Holdings tab's denominator, so the two summaries agree by construction
    # rather than by two frontends happening to compute the same thing.
    inv = _compute_invested()

    # Realized option P/L is portfolio-level: an option has an underlying, not a
    # share position, so it gets no box in the per-ticker grid. It still has to
    # reach the summary — `invested` is net of the option legs now that cash is
    # derived, so leaving it out of the numerator would reopen exactly the gap
    # this payload closed, by the size of the option book.
    #
    # Closed contracts only, matching /api/holdings/chart: an open contract's
    # premium has left the pool but the app never fetches a contract's market
    # value, so counting it would book an unrealized loss equal to the premium.
    options_pl = 0.0
    for opt in load_options():
        if opt.get('status') == 'closed' and opt.get('gain_loss') is not None:
            try:
                options_pl += float(opt['gain_loss'])
            except (TypeError, ValueError):
                pass

    return jsonify({
        'positions':    out,
        'invested':     inv['invested'],
        'cash_pool':    inv['cash_pool'],
        'pending_cash': inv['pending_cash'],
        'options_pl':   round(options_pl, 2),
    })

# ── Transactions ───────────────────────────────────────────────────────────────
TRANSACTIONS_FILE = _register_user_file('transactions.json', list)

def load_transactions(owner=None):
    return _user_store(TRANSACTIONS_FILE, owner).load()

def save_transactions(items, owner=None):
    _user_store(TRANSACTIONS_FILE, owner).save(items)

def rebuild_holdings_from_transactions(owner=None):
    """Replay all transactions FIFO to rebuild holdings.json from scratch.

    `owner` is for callers with no request — the guest seed runs from the CLI.
    Inside a request it is left None and the account comes from the session,
    the same rule every other per-user reader follows.
    """
    from datetime import datetime as _dt
    def _parse(d):
        try: return _dt.strptime(d, '%Y-%m-%d')
        except Exception: return _dt.min

    # Positional when there is no owner, so the zero-argument doubles the
    # ledger tests install for these loaders keep working.
    txns = sorted(load_transactions(owner=owner) if owner else load_transactions(),
                  key=lambda t: (_parse(t.get('date', '')), t.get('id', '')))

    positions = {}  # ticker -> {name, shares, avg_price, lots:[{shares,price,date}]}
    for t in txns:
        ticker = (t.get('ticker') or '').upper()
        if not ticker:
            continue
        shares = float(t.get('shares') or 0)
        price  = float(t.get('price')  or 0)
        date   = t.get('date', '')
        name   = t.get('name', ticker)
        if t.get('type') == 'buy':
            if ticker not in positions:
                positions[ticker] = {'name': name, 'shares': 0.0, 'avg_price': 0.0, 'lots': []}
            pos       = positions[ticker]
            old_s     = pos['shares']
            old_a     = pos['avg_price']
            new_total = old_s + shares
            pos['avg_price'] = (old_s * old_a + shares * price) / new_total if new_total > 0 else price
            pos['shares']    = new_total
            pos['lots'].append({'shares': shares, 'price': price, 'date': date})
        elif t.get('type') == 'sell':
            if ticker not in positions:
                continue
            pos = positions[ticker]
            pos['shares'] = max(0.0, pos['shares'] - shares)
            # WAC: avg_price unchanged on sell — only share count drops.
            # FIFO lot removal is kept for display purposes only.
            remaining = shares
            new_lots  = []
            for lot in pos['lots']:
                if remaining <= 0:
                    new_lots.append(lot)
                elif lot['shares'] <= remaining + 1e-9:
                    remaining -= lot['shares']
                else:
                    new_lots.append({'shares': lot['shares'] - remaining, 'price': lot['price'], 'date': lot['date']})
                    remaining = 0
            pos['lots'] = new_lots
            if pos['shares'] <= 1e-9:
                pos['avg_price'] = 0.0

    holdings = []
    for ticker, data in positions.items():
        if data['shares'] <= 1e-9:
            continue
        lots  = [l for l in data['lots'] if l['shares'] > 1e-9]
        dates = [l['date'] for l in lots if l.get('date')]
        holdings.append({
            'ticker':        ticker,
            'name':          data['name'],
            'shares':        round(data['shares'], 6),
            'price':         round(data['avg_price'], 4),
            'avg_price':     round(data['avg_price'], 4),
            'lots':          [{'shares': round(l['shares'], 6), 'price': round(l['price'], 4), 'date': l['date']} for l in lots],
            'date_acquired': min(dates) if dates else '',
            'added':         '',
        })
    if owner:
        save_holdings(holdings, owner=owner)
    else:
        save_holdings(holdings)
    return holdings

def rebuild_sales_from_transactions(owner=None):
    """Rewrite sales.json so it matches the transaction ledger exactly.

    sales.json is a denormalised view of the sell transactions: every field in it
    except `date_acquired` is derivable from the ledger. It used to be written
    only on the way in, so DELETE and PUT on a transaction left the sale row
    behind — a corrected fat-fingered price or a deleted trade kept its original
    gain forever. The file had accumulated $5.8k of P/L for trades that no longer
    existed, plus rows under tickers that had since been renamed.

    `date_acquired` is the one field the ledger cannot recompute — it sets the
    dividend window in /api/transactions — so it is carried across rather than
    regenerated: by `txn_id` first, then by an exact (ticker, date, shares, price)
    match for rows written before ids were stamped, then by the same match
    ignoring the date, since the date is what tended to be wrong. Only when all
    three miss is it derived FIFO from the oldest lot the sell consumed."""
    txns  = load_transactions(owner=owner) if owner else load_transactions()
    prior = load_sales(owner=owner)        if owner else load_sales()

    # Prior acquisition dates, indexed three ways. Lists, popped as they are
    # claimed, so two identical sells can't both inherit the same row.
    by_id, by_exact, by_loose = {}, {}, {}
    for s in prior:
        acq = (s.get('date_acquired') or '').strip()
        if not acq:
            continue
        tkr = (s.get('ticker') or '').upper()
        sh  = round(float(s.get('shares_sold') or 0), 4)
        pr  = round(float(s.get('sale_price')  or 0), 4)
        if s.get('txn_id'):
            by_id.setdefault(s['txn_id'], []).append(acq)
        by_exact.setdefault((tkr, s.get('sale_date') or '', sh, pr), []).append(acq)
        by_loose.setdefault((tkr, sh, pr), []).append(acq)

    def _claim(sale_date, *keys):
        """First unclaimed prior date for this sell that could actually be one.

        A stored date_acquired later than the sale is a typo, not history — two
        rows carried a year that hadn't happened yet ('2026-11-14' against a sale
        on 2026-01-08). Skip those and let the FIFO derivation fill in."""
        for idx, key in keys:
            bucket = idx.get(key) or []
            for i, acq in enumerate(bucket):
                if not sale_date or acq <= sale_date:
                    return bucket.pop(i)
        return None

    # The WAC each sell actually executed at, so avg_cost here and the cost basis
    # on the performance tab come from the same replay and cannot disagree.
    _, sell_wac = _replay_ledger(txns)

    from datetime import datetime as _dtmod
    def _parse_d(d):
        try:    return _dtmod.strptime(d, '%Y-%m-%d')
        except: return _dtmod.min

    lots = {}  # ticker -> [[shares, date], ...] — FIFO, only to date the shares sold
    out  = []
    for t in sorted(txns, key=lambda t: (_parse_d(t.get('date', '')),
                                         0 if t.get('type') == 'sell' else 1)):
        tkr = (t.get('ticker') or '').upper()
        if not tkr:
            continue
        date = t.get('date') or ''
        sh   = round(float(t.get('shares') or 0), 6)
        pr   = round(float(t.get('price')  or 0), 4)

        if t.get('type') == 'buy':
            lots.setdefault(tkr, []).append([sh, date])
            continue
        if t.get('type') != 'sell':
            continue

        # Consume FIFO; the first lot touched is when these shares started being held
        queue, remaining, fifo_acq = lots.get(tkr) or [], sh, ''
        while remaining > 1e-9 and queue:
            if not fifo_acq:
                fifo_acq = queue[0][1]
            take            = min(queue[0][0], remaining)
            queue[0][0]    -= take
            remaining      -= take
            if queue[0][0] <= 1e-9:
                queue.pop(0)

        wac = round(sell_wac.get(t.get('id'), 0.0), 4)
        acq = _claim(date,
                     (by_id,    t.get('id')),
                     (by_exact, (tkr, date, round(sh, 4), pr)),
                     (by_loose, (tkr, round(sh, 4), pr))) or fifo_acq

        out.append({
            'txn_id':        t.get('id'),
            'ticker':        tkr,
            'name':          t.get('name') or tkr,
            'shares_sold':   sh,
            'avg_cost':      wac,
            'sale_price':    pr,
            'sale_date':     date,
            'date_acquired': acq,
            'gain_loss':     round((pr - wac) * sh, 4),
        })

    if owner:
        save_sales(out, owner=owner)
    else:
        save_sales(out)
    return out

def _new_txn_id(existing_ids):
    """Millisecond timestamp plus a random suffix.

    A bare millisecond stamp collided when two transactions were recorded in the
    same millisecond, and the id is what DELETE/PUT match on — so a collision
    meant editing one row and hitting the other. The timestamp prefix is kept
    because rebuild_holdings_from_transactions() sorts on (date, id) and relies
    on the id to order same-day transactions by insertion.
    """
    import time as _t, secrets as _secrets
    while True:
        candidate = f'{int(_t.time() * 1000)}-{_secrets.token_hex(3)}'
        if candidate not in existing_ids:
            return candidate


def _record_transaction(txn_type, ticker, name, shares, price, date, gain_loss=None):
    """Append a transaction and return its id, so a sale row can point back at it."""
    new_id = []
    def _add(txns):
        txn = {
            'id':     _new_txn_id({t.get('id') for t in txns}),
            'type':   txn_type,
            'ticker': ticker,
            'name':   name,
            'shares': shares,
            'price':  price,
            'date':   date,
        }
        if gain_loss is not None:
            txn['gain_loss'] = gain_loss
        new_id.append(txn['id'])
        txns.insert(0, txn)
        return txns
    _user_store(TRANSACTIONS_FILE).mutate(_add)
    return new_id[0]

@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    txns  = load_transactions()
    sales = load_sales()

    # Build enriched sales lookup: (ticker, sale_date, rounded_shares) -> {gain_loss, avg_cost, date_acquired}
    sales_lookup = {}
    for s in sales:
        key = (s.get('ticker',''), s.get('sale_date',''), round(float(s.get('shares_sold', 0)), 4))
        sales_lookup[key] = {
            'gain_loss':     s.get('gain_loss'),
            'avg_cost':      float(s.get('avg_cost') or 0),
            'date_acquired': s.get('date_acquired', ''),
        }

    # Share-count timeline per ticker — used to know shares held on each dividend ex-date
    txn_events = {}  # ticker -> [(date_str, delta)]
    for t in txns:
        tkr = (t.get('ticker') or '').upper()
        d   = t.get('date', '')
        sh  = float(t.get('shares') or 0)
        if t.get('type') == 'buy':
            txn_events.setdefault(tkr, []).append((d, +sh))
        elif t.get('type') == 'sell':
            txn_events.setdefault(tkr, []).append((d, -sh))

    div_cache = {}  # ticker -> [(ex_date, pay_date, per_share)] — request-local view
                    # of the process-wide _div_events_cache, warmed below

    def _shares_at(events, target_date_str):
        total = 0.0
        for d, delta in sorted(events):
            if d <= target_date_str:
                total += delta
        return max(0.0, total)

    def _dividends_in_period(ticker, date_acquired, sale_date):
        if not date_acquired or not sale_date:
            return 0.0
        if ticker not in div_cache:
            div_cache[ticker] = _build_div_events(ticker)
        events = txn_events.get(ticker, [])
        total  = 0.0
        for ex_date, _pay_date, per_share in div_cache[ticker]:
            ex_str = ex_date.isoformat()
            if date_acquired <= ex_str <= sale_date:
                held   = _shares_at(events, ex_str)
                total += held * per_share
        return round(total, 4)

    # Replay transaction history chronologically to compute correct WAC at each sell.
    # This handles the full-sell-then-re-buy case correctly regardless of what is
    # stored in sales.json (which may have been computed with stale lot data).
    # Shared with /api/portfolio/performance so the two tabs report the same P/L.
    _, _sell_wac = _replay_ledger(txns)  # txn_id -> wac at time of sell

    # Only sells with a known acquisition date need dividend history; warm just
    # those, concurrently, so the enrichment loop below doesn't serialise them.
    _warm_corp_actions(
        (t.get('ticker') or '').upper()
        for t in txns
        if t.get('type') == 'sell' and sales_lookup.get(
            (t.get('ticker', ''), t.get('date', ''), round(float(t.get('shares', 0)), 4)), {}
        ).get('date_acquired')
    )

    # Enrich sell transactions with gain_loss, cost_basis, dividends_earned
    for t in txns:
        if t.get('type') == 'sell':
            key          = (t.get('ticker',''), t.get('date',''), round(float(t.get('shares', 0)), 4))
            sale_info    = sales_lookup.get(key)
            shares_sold  = float(t.get('shares') or 0)
            sale_price   = float(t.get('price')  or 0)
            date_acquired = sale_info['date_acquired'] if sale_info else ''

            wac = _sell_wac.get(t.get('id'))
            if wac is not None and wac > 0:
                t['gain_loss']  = round((sale_price - wac) * shares_sold, 4)
                t['cost_basis'] = round(wac * shares_sold, 4)
            elif sale_info:
                avg_cost = sale_info['avg_cost']
                if t.get('gain_loss') is None:
                    t['gain_loss'] = sale_info['gain_loss']
                t['cost_basis'] = round(avg_cost * shares_sold, 4)

            if date_acquired:
                t['dividends_earned'] = _dividends_in_period(
                    (t.get('ticker') or '').upper(), date_acquired, t.get('date', '')
                )

    return jsonify(txns)

@app.route('/api/transactions/<txn_id>', methods=['DELETE'])
@atomic
def delete_transaction(txn_id):
    txns = [t for t in load_transactions() if t.get('id') != txn_id]
    save_transactions(txns)
    holdings = rebuild_holdings_from_transactions()
    rebuild_sales_from_transactions()  # or the deleted sell keeps reporting its gain
    return jsonify({'transactions': txns, 'holdings': holdings})

@app.route('/api/transactions/<txn_id>', methods=['PUT'])
@atomic
def update_transaction(txn_id):
    data = request.json or {}
    try:
        shares = float(data.get('shares', 0))
        price  = float(data.get('price',  0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid number values'}), 400

    txn_type = (data.get('type') or '').strip().lower()
    date     = (data.get('date') or '').strip()
    # An edit rewrites the ledger row and both rebuilds run off it, so an
    # unvalidated symbol here reaches transactions.json, holdings.json and
    # sales.json together.
    ticker   = clean_ticker(data.get('ticker'))
    name     = (data.get('name') or '').strip()

    if txn_type not in ('buy', 'sell'):
        return jsonify({'error': 'Type must be buy or sell'}), 400
    if shares <= 0 or price <= 0 or not date or not ticker:
        return jsonify({'error': 'Shares, price, date, and a valid ticker are required'}), 400

    txns = load_transactions()
    for t in txns:
        if t.get('id') == txn_id:
            t['type']   = txn_type
            t['ticker'] = ticker
            t['name']   = name
            t['shares'] = round(shares, 6)
            t['price']  = round(price,  4)
            t['date']   = date
            break
    else:
        return jsonify({'error': 'Transaction not found'}), 404

    save_transactions(txns)
    holdings = rebuild_holdings_from_transactions()
    rebuild_sales_from_transactions()  # or the pre-edit gain survives the correction
    return jsonify({'transactions': txns, 'holdings': holdings})

@app.route('/api/transactions/quick', methods=['POST'])
@atomic
def quick_transaction():
    data      = request.json or {}
    # Writes a buy and a sell into the ledger plus a row into sales.json, and
    # the symbol also goes out as a Yahoo search term below.
    ticker    = clean_ticker(data.get('ticker'))
    buy_date  = (data.get('buy_date') or '').strip()
    sell_date = (data.get('sell_date') or '').strip()
    try:
        shares     = float(data.get('shares', 0))
        sell_price = float(data.get('sell_price', 0))
        gain       = float(data.get('gain', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid number values'}), 400

    if not ticker or not buy_date or not sell_date or shares <= 0 or sell_price <= 0:
        return jsonify({'error': 'A valid ticker, shares, dates, and sell price are required'}), 400

    buy_price = round(sell_price - (gain / shares), 4)
    sell_price = round(sell_price, 4)
    gain_loss  = round(gain, 4)

    # Look up company name via Yahoo Finance
    name = ticker
    try:
        import requests as req
        url = 'https://query2.finance.yahoo.com/v1/finance/search'
        params = {'q': ticker, 'quotesCount': 1, 'newsCount': 0}
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = req.get(url, params=params, headers=headers, timeout=5)
        quotes = r.json().get('quotes', [])
        if quotes:
            name = quotes[0].get('longname') or quotes[0].get('shortname') or ticker
    except Exception:
        pass

    txns = load_transactions()
    _ids = {t.get('id') for t in txns}
    _buy_id  = _new_txn_id(_ids)
    _sell_id = _new_txn_id(_ids | {_buy_id})
    txns.insert(0, {'id': _sell_id, 'type': 'sell', 'ticker': ticker,
                    'name': name, 'shares': shares, 'price': sell_price, 'date': sell_date, 'gain_loss': gain_loss})
    txns.insert(1, {'id': _buy_id,  'type': 'buy',  'ticker': ticker,
                    'name': name, 'shares': shares, 'price': buy_price,  'date': buy_date})
    save_transactions(txns)

    sales = load_sales()
    sales.append({'txn_id': _sell_id, 'ticker': ticker, 'name': name, 'shares_sold': shares,
                  'avg_cost': buy_price, 'sale_price': sell_price,
                  'sale_date': sell_date, 'date_acquired': buy_date, 'gain_loss': gain_loss})
    save_sales(sales)
    rebuild_holdings_from_transactions()

    return jsonify({'transactions': txns, 'buy_price': buy_price, 'sell_price': sell_price, 'gain_loss': gain_loss})

@app.route('/api/holdings/<ticker>/sell', methods=['POST'])
@atomic
def sell_holding(ticker):
    # The holding lookup below would 404 on a bad symbol anyway, but this one is
    # written into both the sale row and the ledger — validate it as a write.
    ticker = clean_ticker(ticker)
    if not ticker:
        return jsonify({'error': 'Invalid ticker'}), 400
    data   = request.json or {}
    try:
        shares_sold = float(data.get('shares_sold', 0))
        sale_price  = float(data.get('sale_price',  0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid shares or price'}), 400
    if shares_sold <= 0 or sale_price <= 0:
        return jsonify({'error': 'Shares and price must be positive'}), 400

    holdings = load_holdings()
    holding  = next((h for h in holdings if h['ticker'] == ticker), None)
    if not holding:
        return jsonify({'error': 'Holding not found'}), 404

    shares_sold = min(shares_sold, holding['shares'])

    # FIFO cost basis from lots
    lots = holding.get('lots', [])
    if not lots:
        lots = [{'shares': float(holding['shares']), 'price': float(holding['price']),
                 'date': holding.get('date_acquired', '')}]
    lots_sorted       = sorted(lots, key=lambda l: l.get('date') or '')
    remaining_to_sell = shares_sold
    for lot in lots_sorted:
        if remaining_to_sell <= 0:
            break
        take = min(lot['shares'], remaining_to_sell)
        lot['shares']     -= take
        remaining_to_sell -= take
    # WAC: use holding's avg_price (not FIFO lot prices) to match holdings display
    avg_cost  = round(float(holding.get('avg_price') or holding.get('price')), 4)
    gain_loss = round((sale_price - avg_cost) * shares_sold, 4)

    # Record the sale
    sale_date = data.get('sale_date', '')
    name      = holding.get('name', '')
    # Record the transaction first: its id is what ties the sale row to the
    # ledger, so rebuild_sales_from_transactions() can carry date_acquired
    # across a later edit or delete instead of losing it.
    txn_id = _record_transaction('sell', ticker, name, shares_sold, sale_price,
                                 sale_date, gain_loss=gain_loss)
    sales = load_sales()
    sales.append({
        'txn_id':        txn_id,
        'ticker':        ticker,
        'name':          name,
        'shares_sold':   shares_sold,
        'avg_cost':      avg_cost,
        'sale_price':    sale_price,
        'sale_date':     sale_date,
        'date_acquired': holding.get('date_acquired', ''),
        'gain_loss':     gain_loss,
    })
    save_sales(sales)

    # Update holding lots and totals
    remaining_lots  = [l for l in lots_sorted if l['shares'] > 1e-9]
    remaining_total = sum(l['shares'] for l in remaining_lots)
    # Also treat as fully sold if the user sold all their reported shares (prevents
    # stale lots from surviving when lot totals drift from holding['shares'])
    sold_all = shares_sold >= holding['shares'] - 1e-6
    if remaining_total <= 1e-9 or sold_all:
        holdings = [h for h in holdings if h['ticker'] != ticker]
    else:
        holding['lots']   = remaining_lots
        holding['shares'] = round(remaining_total, 10)
        # WAC: avg_price does not change on a partial sell — only share count reduces.
        # avg_price stays as-is; it will blend correctly when new buys are added.
    save_holdings(holdings)
    # No cash write: the sell is in the ledger, which is what cash is derived from.

    return jsonify({'holdings': holdings, 'sales': sales})


@app.route('/api/dividends', methods=['GET'])
def get_dividends():
    import pandas as pd
    ticker     = request.args.get('ticker', '').strip().upper()
    from_date  = request.args.get('from',   '')
    to_date    = request.args.get('to',     '')
    if not ticker:
        return jsonify({'total_per_share': 0})
    try:
        divs = yf.Ticker(ticker).dividends
        if divs.empty:
            return jsonify({'total_per_share': 0})
        # Normalize index to tz-naive dates for comparison
        divs.index = divs.index.tz_localize(None) if divs.index.tz is not None else divs.index
        if from_date:
            divs = divs[divs.index >= pd.Timestamp(from_date)]
        if to_date:
            divs = divs[divs.index <= pd.Timestamp(to_date)]
        return jsonify({'total_per_share': round(float(divs.sum()), 6)})
    except Exception as e:
        return jsonify({'total_per_share': 0})


def _dividend_payments(transactions):
    """Every dividend payment the transaction ledger entitles the portfolio to.

    One walk, shared by /api/dividends/history and /api/portfolio/performance.
    Both tabs report dividend income, and computing it twice is exactly how the
    Dividends tab and the Performance tab would drift apart on the same ticker —
    the lesson `sales.json` already taught.

    Eligibility is shares held at the EX-date. `status` is 'paid' once the pay
    date has arrived and 'pending' until then; a payment whose ex-date is still
    in the future, or that the portfolio held no shares for, is omitted.

    Returns the payment rows the Dividends tab renders, unsorted.
    """
    from datetime import datetime
    from collections import defaultdict
    import datetime as _dt_mod

    parsed = []
    for t in transactions:
        try:
            parsed.append({
                'date':   datetime.strptime(t['date'], '%Y-%m-%d'),
                'type':   t['type'],
                'ticker': t['ticker'],
                'name':   t.get('name', t['ticker']),
                'shares': float(t['shares']),
            })
        except Exception:
            pass
    if not parsed:
        return []
    parsed.sort(key=lambda x: (x['date'], 0 if x['type'] == 'sell' else 1))

    ticker_events = defaultdict(list)
    ticker_names  = {}
    for t in parsed:
        delta = t['shares'] if t['type'] == 'buy' else -t['shares']
        ticker_events[t['ticker']].append((t['date'], delta))
        if t['name']:
            ticker_names[t['ticker']] = t['name']

    payments = []
    today_d  = _dt_mod.date.today()

    # Concurrently, or the loop below serialises one fetch per ticker.
    _warm_corp_actions(ticker_events.keys())

    for ticker, events in ticker_events.items():
        events_sorted = sorted(events, key=lambda x: x[0])
        div_events = _build_div_events(ticker)
        if not div_events:
            continue

        name = ticker_names.get(ticker, ticker)

        for ex_date, pay_date, div_per_share in div_events:
            ex_dt = datetime.combine(ex_date, datetime.min.time())

            shares_held = 0.0
            for ev_date, delta in events_sorted:
                if ev_date <= ex_dt:
                    shares_held += delta
                else:
                    break

            if shares_held <= 1e-6:
                continue

            # Only show dividends where ex-date has passed (eligible) or already paid
            if ex_date > today_d:
                continue

            total = round(shares_held * float(div_per_share), 2)
            if total <= 0:
                continue

            status = 'paid' if pay_date <= today_d else 'pending'

            payments.append({
                'date':      str(pay_date),
                'ex_date':   str(ex_date),
                'status':    status,
                'ticker':    ticker,
                'name':      name,
                'shares':    round(shares_held, 4),
                'per_share': round(float(div_per_share), 4),
                'total':     total,
            })

    return payments


def _dividends_received_by_ticker(transactions):
    """Ticker -> dividend cash actually received (pay date passed)."""
    out = {}
    for p in _dividend_payments(transactions):
        if p['status'] == 'paid':
            out[p['ticker']] = round(out.get(p['ticker'], 0.0) + p['total'], 2)
    return out


@app.route('/api/dividends/history', methods=['GET'])
def dividends_history():
    transactions = load_transactions()
    if not transactions:
        return jsonify({'payments': [], 'by_month': {}, 'total': 0})

    payments = _dividend_payments(transactions)
    payments.sort(key=lambda x: x['date'])

    by_month = {}
    for p in payments:
        if p['status'] == 'paid':
            month = p['date'][:7]
            by_month[month] = round(by_month.get(month, 0.0) + p['total'], 2)

    grand_total    = round(sum(p['total'] for p in payments if p['status'] == 'paid'), 2)
    pending_total  = round(sum(p['total'] for p in payments if p['status'] == 'pending'), 2)

    return jsonify({'payments': payments, 'by_month': by_month, 'total': grand_total, 'pending_total': pending_total})


@app.route('/api/holdings/chart', methods=['GET'])
def holdings_chart():
    from datetime import datetime, timedelta
    from collections import defaultdict
    import pandas as pd

    range_param = request.args.get('range', '1Y')
    transactions = load_transactions()
    if not transactions:
        return jsonify({'dates': [], 'values': [], 'invested': []})

    # Parse and sort transactions ascending by date
    parsed = []
    for t in transactions:
        try:
            parsed.append({
                'date':   datetime.strptime(t['date'], '%Y-%m-%d'),
                'type':   t['type'],
                'ticker': t['ticker'],
                'shares': float(t['shares']),
                'price':  float(t['price']),
                # String ids — see the note in portfolio_invested(). int() here
                # dropped every transaction carrying a random suffix, which bent
                # both series on this chart.
                'id':     str(t.get('id') or ''),
            })
        except Exception:
            pass
    if not parsed:
        return jsonify({'dates': [], 'values': [], 'invested': []})
    parsed.sort(key=lambda x: (x['date'], x['id']))

    today      = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    first_date = parsed[0]['date']
    range_days = {'1W': 7, '1M': 30, '3M': 90, '6M': 180, '1Y': 365}
    days       = range_days.get(range_param)
    start      = max(today - timedelta(days=days), first_date) if days else first_date

    tickers = list(set(t['ticker'] for t in parsed))

    # Download historical prices — yfinance 1.x always returns (Price, Ticker) MultiIndex
    try:
        dl_start  = start.strftime('%Y-%m-%d')
        dl_end    = (today + timedelta(days=1)).strftime('%Y-%m-%d')
        # Pass list always so column structure is consistent
        raw = yf.download(tickers, start=dl_start, end=dl_end,
                          auto_adjust=False, progress=False)
        if raw.empty:
            return jsonify({'dates': [], 'values': [], 'invested': []})
        # With auto_adjust=False, use 'Close' (unadjusted actual price, not retroactively scaled)
        close = raw['Close']
        prices = close.to_frame(name=tickers[0]) if isinstance(close, pd.Series) else close
        # Forward-fill then back-fill so NaN gaps (holidays, thin volume, NEO/TSX timing
        # mismatches) don't silently zero out positions in the portfolio value.
        prices = prices.ffill().bfill()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if prices.empty:
        return jsonify({'dates': [], 'values': [], 'invested': []})

    # Fetch dividends and stock splits for all tickers over the chart range.
    # Dividends flow through the cash pool so reinvestment buys don't inflate invested.
    # Splits adjust position sizes on the ex-date so unadjusted prices stay consistent.
    from concurrent.futures import ThreadPoolExecutor

    div_map   = {}  # ticker -> {date -> div_per_share}
    split_map = {}  # ticker -> {date -> ratio}  e.g. 2-for-1 split => ratio=2.0

    range_start = pd.Timestamp(dl_start).date()
    range_end   = pd.Timestamp(dl_end).date()

    def _get_corporate_actions(tkr):
        # Both lookups are cached for CORP_ACTIONS_TTL and return whole history,
        # so the range filter happens here rather than in the fetch.
        div_events_out = [(ex_date, pay_date, amount)
                          for ex_date, pay_date, amount in _build_div_events(tkr)
                          if range_start <= pay_date <= range_end]
        splits = {d: ratio for d, ratio in _build_splits(tkr).items()
                  if range_start <= d <= range_end}
        return tkr, div_events_out, splits

    # div_map[tkr] = [(ex_date, pay_date, amount), ...]
    with ThreadPoolExecutor(max_workers=8) as ex:
        for tkr, div_events_out, splits in ex.map(_get_corporate_actions, tickers):
            if div_events_out:
                div_map[tkr] = div_events_out
            if splits:
                split_map[tkr] = splits

    # Build pay-date lookup: pay_date -> [(tkr, ex_date, amount)]
    # and ex-date set for snapshot recording: ex_date -> [tkr]
    from collections import defaultdict as _dd
    pay_lookup  = _dd(list)   # pay_date  -> [(tkr, ex_date, amount)]
    ex_snap_map = _dd(list)   # ex_date   -> [tkr]
    for tkr, events in div_map.items():
        for ex_date, pay_date, amount in events:
            pay_lookup[pay_date].append((tkr, ex_date, amount))
            ex_snap_map[ex_date].append(tkr)
    # Snapshot positions at each ex-date; pre-populate with chart-start positions
    # for any ex-date that falls before the chart range (approximation).
    div_snapshots_chart = {}   # (tkr, ex_date) -> shares

    # Build starting positions from transactions before `start`.
    # invested = cumulative external cash that funded buys.
    # Sell proceeds and dividends go into a cash pool; buys draw from that pool
    # first — only the shortfall counts as new external money.
    positions = {}   # ticker -> shares
    cash      = 0.0  # internal cash pool: sell proceeds + unspent dividends
    invested  = 0.0  # external capital only

    def _apply_txn(t, positions, cash, invested):
        tk, sh, pr = t['ticker'], t['shares'], t['price']
        if t['type'] == 'buy':
            positions[tk] = positions.get(tk, 0) + sh
            cost      = sh * pr
            new_money = max(0.0, cost - cash)
            invested += new_money
            cash      = max(0.0, cash - cost)
        elif t['type'] == 'sell':
            positions[tk] = positions.get(tk, 0) - sh
            cash += sh * pr
        return cash, invested

    for t in parsed:
        if t['date'] >= start:
            break
        cash, invested = _apply_txn(t, positions, cash, invested)

    # Group in-range transactions by calendar date
    txns_by_date = defaultdict(list)
    for t in parsed:
        if t['date'] >= start:
            txns_by_date[t['date'].date()].append(t)

    # Closed options: add gain_loss to cash pool on sell_date (after sell)
    # Open options are invisible to the chart until sold.
    option_gains_by_date = defaultdict(float)
    for opt in load_options():
        if opt.get('status') == 'closed' and opt.get('sell_date') and opt.get('gain_loss') is not None:
            try:
                sd = datetime.strptime(opt['sell_date'], '%Y-%m-%d').date()
                option_gains_by_date[sd] += float(opt['gain_loss'])
            except Exception:
                pass

    dates_out, values_out, invested_out = [], [], []

    for idx_date in prices.index:
        d = idx_date.date() if hasattr(idx_date, 'date') else idx_date

        # Apply stock splits: adjust position size so unadjusted prices stay consistent.
        for tkr in list(positions):
            if tkr in split_map:
                ratio = split_map[tkr].get(d, 0.0)
                if ratio > 0 and ratio != 1.0:
                    positions[tkr] = positions[tkr] * ratio

        # Credit dividends whose pay-date == today using the ex-date share snapshot.
        # Snapshot must already exist (recorded when we iterated over that ex-date).
        # For ex-dates before the chart range we fall back to current positions.
        for tkr, ex_date, amount in pay_lookup.get(d, []):
            snap_key = (tkr, ex_date)
            shares = div_snapshots_chart.get(snap_key, positions.get(tkr, 0))
            if shares > 0:
                cash += shares * amount

        for t in txns_by_date.get(d, []):
            cash, invested = _apply_txn(t, positions, cash, invested)

        # Add closed option gains/losses to cash on their sell date
        if d in option_gains_by_date:
            cash += option_gains_by_date[d]

        # Snapshot shares at close of this ex-date for any dividends whose ex-date == today.
        for tkr in ex_snap_map.get(d, []):
            div_snapshots_chart[(tkr, d)] = positions.get(tkr, 0)

        total_value = cash
        for tkr, shares in positions.items():
            if shares <= 0 or tkr not in prices.columns:
                continue
            p = prices.loc[idx_date, tkr]
            if pd.notna(p):
                total_value += shares * float(p)

        dates_out.append(str(d))
        values_out.append(round(total_value, 2))
        invested_out.append(round(invested, 2))

    # ── Time-weighted return ──────────────────────────────────────────────────
    #   r_i = V_i / (V_{i−1} + C_i)          TWR = Π r_i − 1
    #
    # Daily chain-linking, which is what "time-weighted" means. This was Modified
    # Dietz, and Dietz is a *money-weighted* return — it answers a different
    # question than the tile asks. Money-weighted measures what the investor
    # earned including the effect of when they added capital; time-weighted
    # strips that effect out and measures what the holdings themselves did. On
    # this book over nine months they differ by 2.2 points (+25.55% against a
    # true +27.75%), and they diverge without bound as contributions grow
    # relative to the opening balance: double your money on $1k, add $100k, drop
    # 10%, and the two read +80% and −84% for one portfolio on one day.
    #
    # Dietz is the approximation you reach for when you *lack* periodic
    # valuations and have to assume flows arrive at an average moment.
    # values_out is a daily valuation series, so there is nothing to approximate
    # — chain the days and the flow-timing effect cancels exactly.
    #
    # C_i is external capital only; sale proceeds, dividends and closed-option
    # gains stay inside the portfolio and belong to the return, which is what
    # invested_out already encodes. It sits in the *denominator* because the
    # loop above applies the day's transactions before valuing at that day's
    # close: the new money is already inside V_i, so leaving it out of the base
    # would book the contribution itself as a gain.
    twr = annualized_twr = None
    twr_reliable = False

    if len(values_out) >= 2:
        from datetime import date as _dc
        D = (_dc.fromisoformat(dates_out[-1]) - _dc.fromisoformat(dates_out[0])).days

        growth  = 1.0
        linked  = 0   # days that contributed a return
        skipped = 0   # days with no capital at risk

        # Day 0 sets the opening balance and cannot itself carry a return —
        # there is no prior close to measure it against.
        for i in range(1, len(values_out)):
            cf   = invested_out[i] - invested_out[i - 1]
            base = values_out[i - 1] + cf
            if base <= 0:
                skipped += 1      # empty account: no return to link
                continue
            growth *= values_out[i] / base
            linked += 1

        if linked:
            twr = round(growth - 1.0, 6)
            # A gap means the window contains days the return does not cover.
            twr_reliable = skipped == 0
            # GIPS 5.A.4: a period shorter than a year is not annualized.
            # Raising a 29-day return to the 12.6th power printed +96.22% on the
            # 1M view of this book — an extrapolation rendered as a measurement,
            # and the largest number on the page.
            #
            # growth > 0 because a closed option can post a loss into the cash
            # pool, so a day's value is not structurally positive. A negative
            # base under a fractional exponent returns a *complex* number in
            # Python rather than raising, and round() then 500s the route.
            if D >= 365 and growth > 0:
                annualized_twr = round(growth ** (365.0 / D) - 1.0, 6)

    return jsonify({
        'dates': dates_out, 'values': values_out, 'invested': invested_out,
        'twr': twr, 'annualized_twr': annualized_twr, 'twr_reliable': twr_reliable,
    })


# ── Options ────────────────────────────────────────────────────────────────────
OPTIONS_FILE = _register_user_file('options.json', list)

def load_options(owner=None):
    return _user_store(OPTIONS_FILE, owner).load()

def save_options(items, owner=None):
    _user_store(OPTIONS_FILE, owner).save(items)

@app.route('/api/options', methods=['GET'])
def get_options():
    return jsonify(load_options())

@app.route('/api/options', methods=['POST'])
@atomic
def buy_option():
    import time as _t
    data = request.json or {}
    # `underlying` is a ticker stored in a portfolio file like any other.
    underlying   = clean_ticker(data.get('underlying'))
    option_type  = (data.get('option_type') or '').strip().lower()
    if not underlying or option_type not in ('call', 'put'):
        return jsonify({'error': 'a valid underlying and option_type (call/put) are required'}), 400
    try:
        strike    = float(data.get('strike', 0))
        contracts = int(data.get('contracts', 1))
        buy_price = float(data.get('buy_price', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid numeric values'}), 400
    if strike <= 0 or contracts <= 0 or buy_price <= 0:
        return jsonify({'error': 'Strike, contracts, and buy_price must be positive'}), 400
    expiry   = (data.get('expiry')   or '').strip()
    buy_date = (data.get('buy_date') or '').strip()
    option = {
        'id':          _new_txn_id({o.get('id') for o in load_options()}),
        'status':      'open',
        'underlying':  underlying,
        'option_type': option_type,
        'strike':      strike,
        'expiry':      expiry,
        'contracts':   contracts,
        'buy_price':   buy_price,
        'buy_date':    buy_date,
        'sell_price':  None,
        'sell_date':   None,
        'gain_loss':   None,
    }
    options = load_options()
    options.insert(0, option)
    save_options(options)
    return jsonify(options)

@app.route('/api/options/<option_id>/sell', methods=['PUT'])
@atomic
def sell_option(option_id):
    data = request.json or {}
    try:
        sell_price = float(data.get('sell_price', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid sell_price'}), 400
    if sell_price < 0:
        return jsonify({'error': 'sell_price must be >= 0'}), 400
    sell_date = (data.get('sell_date') or '').strip()
    options = load_options()
    opt = next((o for o in options if o.get('id') == option_id), None)
    if not opt:
        return jsonify({'error': 'Option not found'}), 404
    contracts  = int(opt.get('contracts', 1))
    buy_price  = float(opt.get('buy_price', 0))
    gain_loss  = round((sell_price - buy_price) * contracts * 100, 2)
    opt['status']     = 'closed'
    opt['sell_price'] = sell_price
    opt['sell_date']  = sell_date
    opt['gain_loss']  = gain_loss
    save_options(options)
    return jsonify(options)

@app.route('/api/options/<option_id>', methods=['PUT'])
@atomic
def edit_option(option_id):
    data = request.json or {}
    options = load_options()
    opt = next((o for o in options if o.get('id') == option_id), None)
    if not opt:
        return jsonify({'error': 'Option not found'}), 404
    # Validate the incoming numbers before writing anything. The old buy/sell
    # values used to be read here to hand-compute a cash delta that unwound the
    # previous edit; cash is derived from options.json now, so re-reading this
    # record after the write is the whole reversal.
    try:
        float(data.get('buy_price',  opt.get('buy_price', 0)))
        int(data.get('contracts',    opt.get('contracts', 1)))
        float(data.get('sell_price') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid numeric values'}), 400

    # The copy loop writes whatever arrives straight into options.json, so the
    # symbol has to clear clean_ticker() first — the buy route validates it and
    # an edit must not be the way around that.
    updates = dict(data)
    if 'underlying' in updates:
        underlying = clean_ticker(updates['underlying'])
        if not underlying:
            return jsonify({'error': 'Invalid underlying'}), 400
        updates['underlying'] = underlying

    updatable = ['underlying', 'option_type', 'strike', 'expiry', 'contracts',
                 'buy_price', 'buy_date', 'sell_price', 'sell_date', 'status']
    for key in updatable:
        if key in updates:
            opt[key] = updates[key]
    # Recalculate gain_loss if closed
    if opt.get('status') == 'closed' and opt.get('sell_price') is not None:
        opt['gain_loss'] = round((float(opt['sell_price']) - float(opt['buy_price'])) * int(opt['contracts']) * 100, 2)
    else:
        opt['gain_loss'] = None

    save_options(options)
    return jsonify(options)

@app.route('/api/options/<option_id>', methods=['DELETE'])
@atomic
def delete_option(option_id):
    # Dropping the record is the whole operation — cash is derived from the list
    # that no longer contains it. The removed hand-rolled refund (restore the buy
    # cost, then subtract the proceeds if it had been closed) is the exact class
    # of reversal that left cash.json describing trades that no longer existed.
    options = [o for o in load_options() if o.get('id') != option_id]
    save_options(options)
    return jsonify(options)


# ---------------------------------------------------------------------------
# App settings (API keys stored in settings.json)
# ---------------------------------------------------------------------------
_SETTINGS_PATH = os.path.join(_DATA_DIR, 'settings.json')
_SETTINGS_KEYS = ('ANTHROPIC_API_KEY', 'FRED_API_KEY', 'GROQ_API_KEY',
                  'DEEPSEEK_API_KEY', 'ALPHAVANTAGE_API_KEY')

# API keys are per account: each person pastes their own, and nobody's call is
# billed to somebody else's key. So this is a per-user file like the portfolio
# ones, and there is no shared fallback — not settings.json at the root, and not
# the environment either. An account with no key configured gets the degraded
# path every caller already has, which is the honest outcome; silently borrowing
# a key from the environment would be "shared" wearing a different hat.
SETTINGS_FILE = _register_user_file('settings.json', dict)


def _load_settings(owner=None) -> dict:
    # Unlike the portfolio stores, an unreadable settings file must not break
    # the app — a missing key just disables the feature that needs it.
    try:
        return _user_store(SETTINGS_FILE, owner).load()
    except Exception:
        return {}


def _save_settings(data: dict, owner=None) -> None:
    _user_store(SETTINGS_FILE, owner).save(data)


def _resolve_api_key(name, owner=None):
    """The named API key for one account, or ''. Read at call time.

    `_groq` at the top of this file used to bind its key at import from
    os.environ, so a key saved through POST /api/settings never reached it and
    every call failed silently. Resolving per call is what makes a key pasted
    into Settings work on the very next request, with no restart.
    """
    try:
        return str((_load_settings(owner) or {}).get(name, '') or '').strip()
    except Exception:
        return ''


def _account_env(owner: str) -> dict:
    """The environment for a subprocess run on `owner`'s behalf: this account's
    keys and no others.

    Used by both launchers — `_run_report` and `_run_guidance` — because the
    rule is a property of the keys, not of either feature. Every name in
    `_SETTINGS_KEYS` is set unconditionally, and set to `''` rather than left
    out when the account has none. Both halves matter.

    Unconditionally, because `if name not in env` lets an `ANTHROPIC_API_KEY`
    exported in the shell that started the server beat the account's own — one
    key billed to everybody, which is the thing per-user keys exist to prevent.
    `_run_report` had exactly that shape and was the weaker of the two.

    Present-but-empty rather than absent, because Forward Guide loads a `.env`
    beside itself and skips any key already in the environment: an empty one
    reads as "none configured", where an absent one falls through to that file
    and spends a shared key for an account that configured nothing.

    Everything outside `_SETTINGS_KEYS` is inherited untouched. That is what
    `STOCKBOX_THESES` and `MAX_FILING_CHARS` need — they configure a run rather
    than pay for one, so they are nobody's credential to leak.
    """
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    cfg = _load_settings(owner) or {}
    for name in _SETTINGS_KEYS:
        env[name] = str(cfg.get(name, '') or '').strip()
    return env


# The /api/settings routes live in the administration block below only because
# that is where the auth helpers are defined — they are *not* admin-only. Every
# account manages its own keys.


# ---------------------------------------------------------------------------
# Accounts, sessions and the request gate
#
# Every route used to be unauthenticated, which is why the server was pinned to
# loopback. It now requires a signed-in user, and the gate is *deny by default*
# (_require_login below) rather than a decorator per route: there are ~90 routes
# here and a decorator you can forget to apply is a hole that looks like working
# code. A new route is protected the moment it exists; opening one up takes an
# explicit entry in _PUBLIC_ENDPOINTS.
#
# There is no public signup. Accounts are created by an administrator through
# POST /api/admin/users, or from the command line with `python app.py
# create-admin` — which is also the only way the first account can exist.
# ---------------------------------------------------------------------------
import hmac
import secrets as _secrets

from werkzeug.security import check_password_hash, generate_password_hash


# The one genuinely app-level secret. It signs the session cookie, so it has to
# be the same for everybody — it is infrastructure, not a credential belonging to
# an account, and it gets its own file precisely so that making API keys per-user
# could not drag it along. Nothing reads or writes it through /api/settings.
_app_secret_store = JsonStore('app_secret.json', dict)


def _split_secret_from_legacy_settings() -> str:
    """Lift SECRET_KEY out of the pre-accounts settings.json, if it is there.

    That file held the API keys and the signing key together. The keys are now
    per account and the file moves into the first account's directory with the
    rest of the legacy data, so the signing key has to come out first or it
    would become one user's private property — and every other account's session
    would stop verifying.
    """
    legacy = os.path.join(_DATA_DIR, 'settings.json')
    if not os.path.exists(legacy):
        return ''
    try:
        with open(legacy, encoding='utf-8') as f:
            cfg = json.load(f)
        key = str(cfg.pop('SECRET_KEY', '') or '').strip()
        if not key:
            return ''
        _app_secret_store.save({'SECRET_KEY': key})
        with open(legacy, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
        print('[AUTH] moved SECRET_KEY out of settings.json into '
              'app_secret.json', flush=True)
        return key
    except Exception as e:
        print(f'[AUTH] could not split SECRET_KEY from settings.json: {e}',
              flush=True)
        return ''


def _resolve_secret_key() -> str:
    """The session-cookie signing key: environment, then app_secret.json, then new.

    Persisted on first use because a key regenerated per restart would
    invalidate every session on every restart.
    """
    env = os.environ.get('SECRET_KEY', '').strip()
    if env:
        return env
    try:
        key = (_app_secret_store.load().get('SECRET_KEY') or '').strip()
    except Exception:
        key = ''
    if key:
        return key
    key = _split_secret_from_legacy_settings()
    if key:
        return key
    key = _secrets.token_urlsafe(64)
    try:
        _app_secret_store.save({'SECRET_KEY': key})
    except Exception as e:
        # An unwritable file must not stop the app from serving; the cost is
        # that sessions do not survive this process.
        print(f'[AUTH] could not persist SECRET_KEY ({e}); sessions will not '
              f'survive a restart', flush=True)
    return key


# Secure cookies over plain http://127.0.0.1 would never be sent back, so the
# flag follows the bind address: on by default the moment HOST leaves loopback,
# and overridable for the case where TLS terminates in front of us either way.
_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}
_SECURE_COOKIE_ENV = os.environ.get('SESSION_COOKIE_SECURE', '').strip().lower()
_SECURE_COOKIE = (_SECURE_COOKIE_ENV in ('1', 'true', 'yes') if _SECURE_COOKIE_ENV
                  else os.environ.get('HOST', '127.0.0.1') not in _LOOPBACK_HOSTS)

# How long a signed-in session outlives the tab.
#
# SESSION_PERMANENT=0 makes it a *browser-session* cookie: it carries no expiry,
# so closing the browser ends the session and the next visit asks for a password
# again. PERMANENT_SESSION_LIFETIME does not apply in that mode — Flask only
# consults it for permanent sessions — which is why this is a separate switch
# rather than "set the lifetime very low".
#
# The default stays permanent so that running this locally behaves as it always
# has; a deployment that wants re-authentication turns it off in the env file.
_SESSION_PERMANENT = (os.environ.get('SESSION_PERMANENT', '1').strip().lower()
                      not in ('0', 'false', 'no'))
_SESSION_HOURS = float(os.environ.get('SESSION_HOURS', '12') or 12)

app.secret_key = _resolve_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,      # keeps the cookie out of document.cookie
    SESSION_COOKIE_SAMESITE='Lax',     # first line of defence against CSRF
    SESSION_COOKIE_SECURE=_SECURE_COOKIE,
    PERMANENT_SESSION_LIFETIME=_timedelta(hours=_SESSION_HOURS),
    SESSION_REFRESH_EACH_REQUEST=True,  # makes the window idle-based, not a cap
)

# Behind a reverse proxy every request arrives *from the proxy*, so
# request.remote_addr reads 127.0.0.1 for the entire internet and the per-IP
# half of _note_login_failure() collapses into one bucket — one attacker's
# failures then lock out every other client, and the address in the log lines
# names the proxy rather than anyone. ProxyFix reads the real client out of
# X-Forwarded-For instead.
#
# Off by default, and that default is load-bearing. The header is client
# supplied: trusting it with no proxy in front lets anyone forge an address and
# walk around the lockout entirely, which is strictly worse than not having it.
# TRUSTED_PROXIES is the number of hops that actually rewrite the header — 1 for
# a single cloudflared/Caddy hop. Claiming more hops than exist is the same hole,
# because the extra ones are read from whatever the client sent.
#
# x_host stays 0 deliberately. The Host header decides where redirects point,
# and taking it from a forwarded header is where cache poisoning and redirect
# hijacking start; the tunnel passes the real Host through anyway.
_TRUSTED_PROXIES = int(os.environ.get('TRUSTED_PROXIES', '0') or 0)
if _TRUSTED_PROXIES > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_TRUSTED_PROXIES,
                            x_proto=_TRUSTED_PROXIES, x_host=0, x_prefix=0)
    print(f'[AUTH] trusting {_TRUSTED_PROXIES} proxy hop(s) for the client '
          f'address; lockout keys on X-Forwarded-For', flush=True)

_USERS_LOCK = threading.RLock()
_users_store = JsonStore('users.json', list, lock=_USERS_LOCK)

# Same shape as clean_ticker's guard, and for the same reason: a username lands
# in JSON keys, log lines and URL paths, so it is validated on the way in rather
# than defended against on the way out.
_USERNAME_RE = __import__('re').compile(r'^[a-z0-9][a-z0-9._-]{2,31}$')

PASSWORD_MIN_LEN = 12
# An upper bound is a denial-of-service guard, not a policy: scrypt is meant to
# be slow, and it is slow proportional to what you feed it.
PASSWORD_MAX_LEN = 128

LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT_SEC = 300

# key -> [fail_count, locked_until_epoch, last_fail_epoch]. The third field is
# what expires a stale counter; keying that decision off locked_until instead
# meant an un-locked entry (locked_until 0.0) always read as expired, so the
# count reset on every failure and the lockout could never engage at all.
_login_fails: dict = {}
_login_fails_lock = threading.Lock()

# An attacker can mint a new key per attempt by varying the username, so the
# table needs a ceiling or it is a memory leak with a login form attached.
_LOGIN_FAILS_MAX = 4096

_dummy_hash_cache = None
_dummy_hash_lock = threading.Lock()


def _dummy_hash() -> str:
    """A real hash to verify against when the username does not exist.

    Returning early for an unknown user makes the two cases distinguishable by
    response time, which turns the login form into a "does this account exist"
    oracle. Built lazily — it costs a full scrypt round, which does not belong in
    import time.
    """
    global _dummy_hash_cache
    with _dummy_hash_lock:
        if _dummy_hash_cache is None:
            _dummy_hash_cache = generate_password_hash(_secrets.token_urlsafe(32))
        return _dummy_hash_cache


def _verify_password(stored_hash, password):
    """check_password_hash that answers False instead of raising.

    users.json is a plain file in the working directory and gets hand-edited, so
    a stored hash is untrusted input the same way a stored ticker is. Werkzeug
    raises on a malformed one ('not$a$hash' -> ValueError, None -> AttributeError)
    rather than returning False, which would turn a corrupt record into a 500 —
    and a 500 that a 401 does not produce is exactly the "does this account
    exist" oracle the single error message elsewhere is there to prevent.
    """
    try:
        return check_password_hash(stored_hash, password)
    except Exception:
        return False


def clean_username(raw):
    """The normalised username, or None if it isn't a plausible one.

    Case-folded, so 'Dylan' and 'dylan' cannot become two accounts.
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip().lower()
    return name if _USERNAME_RE.match(name) else None


def check_password_policy(password, username=''):
    """Return an error string, or None when the password is acceptable."""
    if not isinstance(password, str):
        return 'Password must be text.'
    if len(password) < PASSWORD_MIN_LEN:
        return f'Password must be at least {PASSWORD_MIN_LEN} characters.'
    if len(password) > PASSWORD_MAX_LEN:
        return f'Password must be at most {PASSWORD_MAX_LEN} characters.'
    if password.strip().lower() == (username or '').strip().lower():
        return 'Password must not be the username.'
    return None


def load_users():
    return _users_store.load()


def _find_user(username, users=None):
    name = (username or '').strip().lower()
    for u in (users if users is not None else load_users()):
        if u.get('username') == name:
            return u
    return None


def _public_user(u):
    """A user record with the credential material removed.

    Every route that returns a user goes through this. Serialising the record
    directly would put the password hash on the wire — offline-crackable, and
    invisible in a payload nobody reads closely.
    """
    return {
        'username':   u.get('username', ''),
        'role':       u.get('role', 'user'),
        'disabled':   bool(u.get('disabled')),
        'created_at': u.get('created_at', ''),
        'last_login': u.get('last_login'),
    }


def _now_iso():
    return _datetime.now(_timezone.utc).replace(microsecond=0).isoformat()


def _make_user(username, password, role='user'):
    """Build a new user record. The password is hashed here and nowhere else."""
    return {
        'username':      username,
        'password_hash': generate_password_hash(password),
        'role':          role,
        'disabled':      False,
        'created_at':    _now_iso(),
        'last_login':    None,
        # Bumped by a password change or an admin reset. A session carries the
        # value it was issued under, so bumping it signs out every other session
        # for that user — which is the whole point of changing a password you
        # think someone else has.
        'token_version': 1,
    }


def create_user(username, password, role='user'):
    """Create an account. Returns the public record; raises ValueError on refusal.

    Shared by POST /api/admin/users and the create-admin CLI so that the rules —
    username shape, password policy, uniqueness — cannot differ between them.
    """
    name = clean_username(username)
    if not name:
        raise ValueError('Username must be 3-32 characters: letters, digits, '
                         'dot, hyphen or underscore, starting with a letter or digit.')
    if role not in ('user', 'admin'):
        raise ValueError('Role must be "user" or "admin".')
    err = check_password_policy(password, name)
    if err:
        raise ValueError(err)

    created = {}

    def _add(users):
        if _find_user(name, users):
            raise ValueError('That username is already taken.')
        record = _make_user(name, password, role)
        created.update(record)
        return users + [record]

    _users_store.mutate(_add)
    return _public_user(created)


def _admin_count(users, excluding=None):
    """Enabled admins, optionally ignoring one username."""
    return sum(1 for u in users
               if u.get('role') == 'admin' and not u.get('disabled')
               and u.get('username') != excluding)


# --- guest access -----------------------------------------------------------
#
# One shared, read-only account for people reviewing the project who have no
# account of their own. It is an ordinary user record — same directory layout,
# same gate, same session mechanics — with three differences, each of which is a
# single check rather than a second code path:
#
#   * role is 'guest', and `_require_login` refuses every non-GET for that role
#     except logout. Read-only is a property of the gate, not of the routes, for
#     the same reason the gate itself is not a decorator: the mutating route you
#     forget to mark is the one that lets a stranger rewrite the demo ledger
#     everyone else is looking at.
#   * password_hash is None, so the password form can never sign in as it —
#     `_verify_password(None, ...)` is False — and the only way in is the button,
#     which starts a session for it while it is enabled and 404s otherwise.
#   * it has no API keys and cannot save any: a key saved into a shared account
#     would be spent by every stranger who pressed the button.
#
# The switch is the existing `disabled` flag, so the Admin tab's Enable/Disable
# control is the on/off toggle and disabling it ends every live guest session the
# same way it does for anyone else. `python app.py guest enable` creates it.

GUEST_USERNAME = 'guest'
GUEST_ROLE     = 'guest'


def _guest_record(users=None):
    """The guest account's record, or None if it has never been enabled."""
    u = _find_user(GUEST_USERNAME, users)
    return u if u is not None and u.get('role') == GUEST_ROLE else None


def _guest_enabled():
    u = _guest_record()
    return u is not None and not u.get('disabled')


def _is_guest(user):
    return bool(user) and user.get('role') == GUEST_ROLE


def _current_user():
    """The signed-in user, or None.

    Re-read per request rather than trusted from the cookie: the cookie is signed,
    so its contents are authentic, but they are also a snapshot. A user disabled
    or deleted a minute ago would otherwise keep working until their session
    expired. Cached on `g` so the gate and the route body share one read.
    """
    if 'auth_user' in g:
        return g.auth_user
    g.auth_user = None
    name = session.get('uid')
    if name:
        u = _find_user(name)
        if u and not u.get('disabled') and \
                int(u.get('token_version', 1)) == session.get('tv'):
            g.auth_user = u
    return g.auth_user


def _wants_json():
    return (request.path.startswith('/api/')
            or request.accept_mimetypes.best == 'application/json')


def _safe_next(raw):
    """A path inside this app, or '/'.

    Reflecting ?next= into a redirect without this is an open redirect:
    /login?next=https://evil.example walks the user off-site straight from our
    own login form. '//host' is the same trick without a scheme.
    """
    if not isinstance(raw, str) or not raw.startswith('/') or raw.startswith('//'):
        return '/'
    if any(c in raw for c in ('\\', '\r', '\n')):
        return '/'
    return raw


def _auth_challenge():
    """401 for the API, a trip to the login form for a page."""
    if _wants_json():
        return jsonify({'error': 'Authentication required', 'auth': 'required'}), 401
    nxt = _safe_next(request.full_path.rstrip('?') if request.query_string else request.path)
    return redirect(f'/login?next={_url_quote(nxt)}')


def admin_required(fn):
    """Refuse anyone who is not an enabled administrator.

    The gate has already established that *someone* is signed in; this is the
    second half, and it re-checks the session rather than trusting the first —
    an admin route must not become public if its endpoint is ever added to
    _PUBLIC_ENDPOINTS by mistake.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user = _current_user()
        if user is None:
            return _auth_challenge()
        if user.get('role') != 'admin':
            return jsonify({'error': 'Administrator access required'}), 403
        return fn(*args, **kwargs)
    return wrapper


# The only endpoints reachable without a session. Everything else — including
# every route added after this comment — is closed.
_PUBLIC_ENDPOINTS = {'static', 'login_page', 'api_login', 'api_guest_login'}

# The only non-GET endpoints a guest session may reach. Same shape as the set
# above and for the same reason: a route added later is read-only for a guest
# because it exists, and letting one write takes an explicit edit here.
_GUEST_WRITABLE = {'api_logout'}

_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}


def _check_csrf():
    """Require a header token on anything that changes state.

    SameSite=Lax already stops a cross-site form POST from carrying the session
    cookie, but it is one browser setting between an attacker's page and a route
    that sells a position. The token lives in the session and is echoed into the
    page, so only same-origin script can read it; `fetch` from another origin
    cannot set the header without CORS approval this app never gives.
    """
    if request.method in _SAFE_METHODS:
        return None
    sent = request.headers.get('X-CSRF-Token', '')
    want = session.get('csrf', '')
    if not want or not sent or not hmac.compare_digest(str(sent), str(want)):
        return jsonify({'error': 'Invalid or missing CSRF token'}), 403
    return None


# --- rate limiting --------------------------------------------------------
#
# Same argument as the gate above, and deliberately the same shape: a
# before_request hook with a default that covers every route, not a decorator
# somebody has to remember. The expensive routes here do not look expensive from
# the outside — /api/stock fans out into five Macrotrends scrapes, /api/news
# spends an LLM key across ~135 threads under a 25s deadline, and
# /api/generate-report starts a six-minute subprocess — so a route added after
# this line is limited *because it exists*, and loosening one takes an explicit
# edit to _RATE_LIMITS that a test asserts on.
#
# The bucket is per device rather than per account. Two reasons: the login route
# has no account yet, and that is exactly where a limit is worth having; and
# keying on the account would mean signing out handed back a fresh allowance. So
# the id is an opaque random cookie kept deliberately *outside* the session —
# session.clear() runs on both login and logout, and anything living there
# resets with it.
#
# A cookie is client state, so a client that drops it draws a new bucket. That
# is what the second, coarser per-address bucket is for: _ADDR_FACTOR times the
# device allowance, so several real devices behind one address never meet it,
# while a loop minting a fresh cookie per request meets it after that factor.
# Both are consulted and the stricter answer wins — the same "worst of these
# keys" rule _login_locked() already uses.
#
# Behind a reverse proxy the address half only means anything with
# TRUSTED_PROXIES set; see the ProxyFix note above. Left unset, every client
# shares one address bucket, which is why that bucket is sized as a backstop and
# the device bucket is the limit that actually does the work.

_DEVICE_COOKIE  = 'did'
_DEVICE_MAX_AGE = int(_timedelta(days=365).total_seconds())
# Shape-checked because it reaches a dict key: an unbounded cookie value is an
# unbounded key. Same reasoning as clean_username(), one layer down.
_DEVICE_RE = __import__('re').compile(r'^[A-Za-z0-9_-]{16,64}$')

# (burst, per minute). The burst is what a page load spends at once; the rate is
# what a script settles down to. The default is generous on purpose — it is a
# ceiling on total volume, not a throttle a human should ever notice — while the
# named entries below are sized against what one request actually costs.
_RATE_DEFAULT = (150, 180)

# How much more the whole address may spend than one device on it.
_ADDR_FACTOR = 4

_RATE_LIMITS = {
    # Password hashing. scrypt at 32768:8:1 is ~100ms and ~32MB by design, so
    # these are the routes where a plain loop is a memory-and-CPU DoS. The login
    # lockout above stops a *guessing* run; this stops the cost of one.
    'api_login':            (10, 10),
    # No hash to pay for, but it mints a session per call and stamps the users
    # file — priced like login so a loop on the button cannot do either freely.
    'api_guest_login':      (10, 10),
    'api_change_password':   (5, 10),
    'admin_create_user':    (10, 20),
    'admin_update_user':    (20, 40),   # can reset a password, so it hashes too

    # A subprocess with a six-minute timeout. See REPORT_MAX_CONCURRENT below:
    # a rate limit alone still lets a handful of them stack up, because the cost
    # is in how long each one lives, not in how often it is asked for.
    'generate_report':       (3,  2),
    # EDGAR, an LLM extraction and a yfinance walk in a subprocess, on the
    # account's own key. Priced like a report and bounded the same way, by
    # GUIDANCE_MAX_CONCURRENT — the status route stays on the default bucket,
    # since it is polled for the life of the run exactly as report-status is.
    'forward_guidance_run':  (3,  2),

    # Third-party work per request. Held below what the upstream would notice:
    # being rate-limited by Yahoo degrades every other tab, not just this one.
    'get_stock':            (15, 30),
    # Five Macrotrends scrapes and four yfinance frames, so it is priced like
    # get_stock rather than like a quote. Lower burst: a page load fires one
    # get_stock, but only a deliberate toggle fires this, and never more than
    # once per ticker — the browser memo and `_quarterly_cache` absorb the rest.
    'get_stock_quarterly':  (10, 20),
    'get_chart':            (20, 40),
    'crosslist':            (20, 40),
    'insider_buying':       (10, 20),
    'canada_drops':          (5, 10),
    'movers':               (10, 20),
    'ticker_tape':          (20, 40),
    'search_tickers':       (30, 90),   # typeahead, debounced at 200ms
    'single_quote':         (30, 60),
    'batch_quotes':         (30, 60),
    # A cold S&P 500 is a Wikipedia scrape plus five Yahoo quote batches, and
    # the answer is shared — every account browsing the same index inside
    # INDEX_QUOTES_TTL is served from one build.
    'market_index':         (10, 20),

    # Spends an API key — the account's own, but still money.
    'get_news':              (8, 15),
    'get_market_news':       (6, 10),
    'get_positions_news':    (6, 10),

    # Scrapes with no cache in front of them.
    'debug_dividend':       (10, 20),
    'debug_cashflow':       (10, 20),
    'debug_macrotrends':    (10, 20),
    'debug_raw_series':     (10, 20),
}

# key -> [tokens, last_refill_epoch, capacity, tokens_per_sec]. Capacity and
# rate are stored rather than looked back up so that eviction can tell an idle
# bucket from a drained one exactly, instead of guessing with a timeout.
_rate_buckets: dict = {}
_rate_buckets_lock = threading.Lock()

# A new device cookie is a new key, so the table needs a ceiling for the same
# reason _login_fails does.
_RATE_BUCKETS_MAX = 8192


def _device_id():
    """This browser's opaque bucket key, minted on first sight.

    The value carries no claim, so it is not signed: forging one only moves you
    to a different bucket, and bounding *that* is the per-address bucket's job.
    """
    got = getattr(g, 'device_id', None)
    if got:
        return got
    raw = request.cookies.get(_DEVICE_COOKIE, '')
    if not _DEVICE_RE.match(raw or ''):
        raw = _secrets.token_urlsafe(16)
        g.device_new = True
    g.device_id = raw
    return raw


def _rate_tokens(key, capacity, rate, now):
    entry = _rate_buckets.get(key)
    if entry is None:
        return float(capacity)
    # max(0.0, ...) so a clock stepping backwards cannot mint tokens.
    return min(float(capacity), entry[0] + max(0.0, now - entry[1]) * rate)


def _rate_evict(now):
    """Drop buckets that have fully refilled.

    A full bucket answers every question identically to one that was never
    created, so forgetting it is not the same as forgetting a limit.
    """
    for key, (tokens, last, capacity, rate) in list(_rate_buckets.items()):
        if tokens + max(0.0, now - last) * rate >= capacity:
            _rate_buckets.pop(key, None)


def _rate_consume(specs):
    """Spend one token from every bucket in `specs`, or from none of them.

    Returns seconds to wait, or 0 to proceed. A refusal spends nothing: charging
    the device bucket for a request the address bucket already refused would let
    one noisy client drain every other device on that address without a single
    request getting through.

    A token bucket rather than a counter per fixed window — a window boundary
    lets two full allowances through back to back, and it cannot say how long
    the caller should wait without being wrong by up to a whole window.
    """
    now = _time_mod.time()
    with _rate_buckets_lock:
        if len(_rate_buckets) >= _RATE_BUCKETS_MAX:
            _rate_evict(now)

        wait = 0
        for key, capacity, per_min in specs:
            rate = per_min / 60.0
            tokens = _rate_tokens(key, capacity, rate, now)
            if tokens < 1.0:
                wait = max(wait, int((1.0 - tokens) / rate) + 1)
        if wait:
            return wait

        for key, capacity, per_min in specs:
            rate = per_min / 60.0
            tokens = _rate_tokens(key, capacity, rate, now)
            _rate_buckets[key] = [tokens - 1.0, now, float(capacity), rate]
        return 0


def _rate_specs(endpoint):
    """The buckets one request draws on: its own endpoint's, and the default.

    A named endpoint spends from both, so the default stays a true ceiling on
    total volume rather than something an expensive route can step around.
    """
    device = _device_id()
    addr   = request.remote_addr or '?'
    scopes = [('*', _RATE_DEFAULT)]
    named  = _RATE_LIMITS.get(endpoint)
    if named:
        scopes.append((endpoint, named))

    specs = []
    for scope, (capacity, per_min) in scopes:
        specs.append((f'd:{scope}:{device}', capacity, per_min))
        specs.append((f'a:{scope}:{addr}',
                      capacity * _ADDR_FACTOR, per_min * _ADDR_FACTOR))
    return specs


@app.before_request
def _rate_limit_gate():
    """Bound how fast one device can spend server time.

    Registered before the login gate, and that order matters: _current_user()
    re-reads the account file on every request, so a refusal belongs in front of
    that work rather than behind it.
    """
    endpoint = request.endpoint
    if endpoint is None or endpoint == 'static':
        return None
    wait = _rate_consume(_rate_specs(endpoint))
    if not wait:
        return None
    print(f'[RATE] {endpoint} refused for {request.remote_addr} '
          f'(retry in {wait}s)', flush=True)
    return _rate_limited(wait)


def _rate_limited(wait):
    """429 with a Retry-After the caller can actually act on."""
    if _wants_json():
        resp = jsonify({'error': f'Too many requests. Try again in {wait} second(s).',
                        'rate_limited': True})
    else:
        resp = app.make_response(f'Too many requests. Try again in {wait} second(s).\n')
        resp.mimetype = 'text/plain'
    resp.status_code = 429
    resp.headers['Retry-After'] = str(wait)
    return resp


import gzip as _gzip

# Compression is not a general nicety here; it is what keeps "the whole universe
# in one response" affordable. A 3,402-row Nasdaq payload is ~876KB of JSON and
# ~171KB gzipped — 5.1x, because the body is overwhelmingly repeated key names
# and digits. Flask does not do this on its own and the loopback default has no
# proxy in front of it to do it instead, so without this the design that makes
# sorting and filtering free would simply move the cost onto the wire.
#
# Registered *before* _issue_device_cookie so that it runs *after* it: Flask
# calls after_request handlers in reverse registration order, and the one that
# rewrites the body has to see the final body.
_COMPRESS_MIN_BYTES = 1024
_COMPRESSIBLE_TYPES = ('application/json', 'text/', 'application/javascript',
                       'image/svg+xml')


@app.after_request
def _compress_response(resp):
    """gzip a sizable text body when the client said it would take one."""
    ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip()
    if not ctype.startswith(_COMPRESSIBLE_TYPES):
        return resp

    # Announced whether or not this particular response was compressed, because
    # the representation varies by request header either way — a shared cache
    # that missed it would hand a gzipped body to a client that never asked for
    # one.
    if 'accept-encoding' not in (resp.headers.get('Vary') or '').lower():
        resp.headers.add('Vary', 'Accept-Encoding')

    if (resp.status_code != 200
            # A streamed response has no body to read here, and get_data() on
            # one would buffer the whole thing to compress it.
            or resp.direct_passthrough
            or resp.headers.get('Content-Encoding')
            or 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower()):
        return resp

    body = resp.get_data()
    # Below about a kilobyte the gzip header and trailer cost more than the
    # compression saves, and most responses here are a two-field JSON object.
    if len(body) < _COMPRESS_MIN_BYTES:
        return resp

    resp.set_data(_gzip.compress(body, 6))
    resp.headers['Content-Encoding'] = 'gzip'
    resp.headers['Content-Length']   = str(resp.calculate_content_length())
    return resp


@app.after_request
def _issue_device_cookie(resp):
    """Hand out the device id, including on the 429 that refused the request —
    otherwise a limited client never gets one and every retry draws a fresh
    bucket."""
    if getattr(g, 'device_new', False):
        resp.set_cookie(_DEVICE_COOKIE, g.device_id,
                        max_age=_DEVICE_MAX_AGE, httponly=True,
                        samesite='Lax', secure=_SECURE_COOKIE)
    return resp


@app.before_request
def _require_login():
    """Deny by default. See the section header for why this is not a decorator."""
    endpoint = request.endpoint
    if endpoint is None:
        return None                     # unrouted path: let Flask 404 it
    if endpoint in _PUBLIC_ENDPOINTS:
        return None
    user = _current_user()
    if user is None:
        return _auth_challenge()
    refusal = _check_csrf()
    if refusal is not None:
        return refusal
    # A guest reads everything its own directory holds and writes none of it.
    # Checked after CSRF so a forged cross-site write still gets the 403 that
    # names the real reason; checked here rather than per route because there
    # are ~40 mutating routes and the one without the check would be the hole.
    if _is_guest(user) and request.method not in _SAFE_METHODS \
            and endpoint not in _GUEST_WRITABLE:
        return jsonify({'error': 'Guest access is read-only.', 'guest': True}), 403
    return None


# --- login / logout -------------------------------------------------------

def _throttle_keys():
    body = request.get_json(silent=True) or {}
    name = (body.get('username') or '').strip().lower()
    return [f'u:{name}', f'i:{request.remote_addr or "?"}']


def _login_locked(keys):
    """Seconds remaining on a lockout, or 0.

    Counted per username *and* per client address: the username counter protects
    one account from a password list, the address counter protects every account
    from one client walking the user list.
    """
    now = _time_mod.time()
    with _login_fails_lock:
        worst = 0
        for k in keys:
            entry = _login_fails.get(k)
            if entry and entry[1] > now:
                worst = max(worst, int(entry[1] - now))
        return worst


def _note_login_failure(keys):
    now = _time_mod.time()
    with _login_fails_lock:
        if len(_login_fails) >= _LOGIN_FAILS_MAX:
            for k, v in list(_login_fails.items()):
                if now - v[2] > LOGIN_LOCKOUT_SEC:
                    _login_fails.pop(k, None)
        for k in keys:
            entry = _login_fails.get(k)
            # A run of failures counts as one run only while it stays fresh:
            # five failures spread over a week are somebody mistyping.
            if not entry or now - entry[2] > LOGIN_LOCKOUT_SEC:
                entry = [0, 0.0, now]
            entry[0] += 1
            entry[2] = now
            if entry[0] >= LOGIN_MAX_FAILS:
                entry[1] = now + LOGIN_LOCKOUT_SEC
            _login_fails[k] = entry


def _clear_login_failures(keys):
    with _login_fails_lock:
        for k in keys:
            _login_fails.pop(k, None)


def _start_session(user):
    """Issue a session for `user`, replacing anything already in the cookie.

    session.clear() is the session-fixation defence: without it a token planted
    before login survives the privilege change and keeps working afterwards.
    """
    session.clear()
    session['uid']  = user['username']
    session['tv']   = int(user.get('token_version', 1))
    session['csrf'] = _secrets.token_urlsafe(32)
    session.permanent = _SESSION_PERMANENT


@app.route('/login', methods=['GET'])
def login_page():
    if _current_user() is not None:
        return redirect(_safe_next(request.args.get('next')))
    return render_template('login.html',
                           next_url=_safe_next(request.args.get('next')),
                           guest_enabled=_guest_enabled())


@app.route('/api/auth/guest', methods=['POST'])
def api_guest_login():
    """Open a session on the shared guest account, if one is enabled.

    Public, like api_login, because it *is* a login. 404 rather than 403 when
    there is no enabled guest: the button that calls this is only rendered when
    there is one, so a caller reaching it otherwise is probing, and there is
    nothing here to be forbidden from.
    """
    guest = _guest_record()
    if guest is None or guest.get('disabled'):
        return jsonify({'error': 'Guest access is not enabled.'}), 404

    _start_session(guest)

    # Every guest login lands on the demo book. Normally a read that finds
    # nothing to do — a guest cannot write — but if the directory has drifted
    # by any other route it is put back before this visitor sees it.
    try:
        if _guest_portfolio_drifted():
            _seed_guest_portfolio(force=True)
            print('[AUTH] guest portfolio had drifted; reset on login', flush=True)
    except Exception as e:
        print(f'[AUTH] guest portfolio reset failed: {e}', flush=True)

    def _stamp(users):
        for u in users:
            if u.get('username') == GUEST_USERNAME:
                u['last_login'] = _now_iso()
        return users

    try:
        _users_store.mutate(_stamp)
    except Exception:
        pass        # bookkeeping; must not be able to fail the sign-in

    print(f'[AUTH] guest login from {request.remote_addr}', flush=True)
    return jsonify({'ok': True, 'user': _public_user(guest),
                    'csrf_token': session['csrf']})


@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data     = request.get_json(silent=True) or {}
    username = data.get('username')
    password = data.get('password')
    keys     = _throttle_keys()

    wait = _login_locked(keys)
    if wait:
        return jsonify({'error': f'Too many failed attempts. Try again in '
                                 f'{max(1, wait // 60 + 1)} minute(s).'}), 429

    name = clean_username(username)
    user = _find_user(name) if name else None

    # One message and one code path for every failure. "No such user" and "wrong
    # password" told apart is a free list of valid usernames; a disabled account
    # that says so is the same leak.
    ok = False
    if isinstance(password, str) and password:
        if user is not None and not user.get('disabled'):
            ok = _verify_password(user.get('password_hash', ''), password)
        else:
            _verify_password(_dummy_hash(), password)      # equalise the timing

    if not ok:
        _note_login_failure(keys)
        print(f'[AUTH] failed login for {name or "<invalid>"} '
              f'from {request.remote_addr}', flush=True)
        return jsonify({'error': 'Invalid username or password.'}), 401

    _clear_login_failures(keys)
    _start_session(user)

    def _stamp(users):
        for u in users:
            if u.get('username') == user['username']:
                u['last_login'] = _now_iso()
        return users

    try:
        _users_store.mutate(_stamp)
    except Exception:
        pass        # a bookkeeping field must not be able to fail a valid login

    print(f'[AUTH] login: {user["username"]} from {request.remote_addr}', flush=True)
    return jsonify({'ok': True, 'user': _public_user(user),
                    'csrf_token': session['csrf']})


@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/auth/me', methods=['GET'])
def api_me():
    user = _current_user()
    return jsonify({'user': _public_user(user), 'csrf_token': session.get('csrf', '')})


@app.route('/api/auth/password', methods=['POST'])
def api_change_password():
    """Change your own password. Requires the current one."""
    user = _current_user()
    data = request.get_json(silent=True) or {}
    current = data.get('current_password')
    new     = data.get('new_password')

    if not isinstance(current, str) or \
            not _verify_password(user.get('password_hash', ''), current):
        return jsonify({'error': 'Current password is incorrect.'}), 403

    err = check_password_policy(new, user['username'])
    if err:
        return jsonify({'error': err}), 400
    if new == current:
        return jsonify({'error': 'New password must differ from the current one.'}), 400

    def _apply(users):
        for u in users:
            if u.get('username') == user['username']:
                u['password_hash'] = generate_password_hash(new)
                u['token_version'] = int(u.get('token_version', 1)) + 1
        return users

    _users_store.mutate(_apply)

    # Every other session for this account is now invalid, including whichever
    # one an attacker might be holding. Re-issue this one so the user who just
    # changed it isn't the only one signed out.
    _start_session(_find_user(user['username']))
    print(f'[AUTH] password changed: {user["username"]}', flush=True)
    return jsonify({'ok': True, 'csrf_token': session['csrf']})


# --- administration -------------------------------------------------------

# Your own keys, nobody else's. Both routes resolve the account from the session
# and never take one from the request, so there is no shape of payload that
# reads or writes another user's keys — and an administrator has no more access
# here than anyone else. Even masked, a key belongs to one person.
@app.route('/api/settings', methods=['GET'])
def get_settings():
    cfg = _load_settings()
    masked = {}
    for k in _SETTINGS_KEYS:
        v = str(cfg.get(k, '') or '')
        masked[k] = ('•' * (len(v) - 4) + v[-4:]) if len(v) > 4 else ('•' * len(v))
    return jsonify(masked)


@app.route('/api/settings', methods=['POST'])
@atomic
def save_settings():
    # No client needs invalidating here: the key is re-read per call by
    # _resolve_api_key(), and _groq_client() rebuilds when the value changes.
    #
    # Three cases, and the difference between the last two is the point. A
    # string sets the key. An empty string leaves it alone — that is what lets
    # the Settings page post all four fields on one Save with only one of them
    # filled in, without the three blanks wiping what is stored. Clearing is
    # therefore an explicit JSON null, which no untouched form field produces:
    # a key you could set but never unset was the gap that left.
    data = request.get_json(silent=True) or {}
    cfg  = _load_settings()
    for k in _SETTINGS_KEYS:
        if k not in data:
            continue
        if data[k] is None:
            cfg.pop(k, None)
        elif isinstance(data[k], str) and data[k].strip():
            cfg[k] = data[k].strip()
    _save_settings(cfg)
    return jsonify({'ok': True})


@app.route('/api/admin/users', methods=['GET'])
@admin_required
def admin_list_users():
    return jsonify([_public_user(u) for u in load_users()])


@app.route('/api/admin/users', methods=['POST'])
@admin_required
def admin_create_user():
    """The only way to create an account over HTTP, and it requires an admin."""
    data = request.get_json(silent=True) or {}
    try:
        created = create_user(data.get('username'), data.get('password'),
                              data.get('role') or 'user')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    print(f'[AUTH] account created: {created["username"]} ({created["role"]}) '
          f'by {_current_user()["username"]}', flush=True)
    return jsonify(created), 201


@app.route('/api/admin/users/<username>', methods=['PUT'])
@admin_required
def admin_update_user(username):
    """Change a role, enable/disable, or reset a password."""
    actor  = _current_user()
    name   = clean_username(username)
    data   = request.get_json(silent=True) or {}
    if not name:
        return jsonify({'error': 'Unknown user.'}), 404

    role     = data.get('role')
    disabled = data.get('disabled')
    new_pw   = data.get('password')

    if role is not None and role not in ('user', 'admin'):
        return jsonify({'error': 'Role must be "user" or "admin".'}), 400
    if new_pw is not None:
        err = check_password_policy(new_pw, name)
        if err:
            return jsonify({'error': err}), 400

    error = {}

    def _apply(users):
        target = _find_user(name, users)
        if target is None:
            error['msg'], error['code'] = 'Unknown user.', 404
            return users
        # The guest account has no password to reset and no role to change:
        # promoting it makes a passwordless account with write access, and
        # giving it a password makes the password form a second door into the
        # shared demo. Enable/disable is the whole of its administration.
        if target.get('role') == GUEST_ROLE and (role is not None or new_pw is not None):
            error['msg'] = 'The guest account can only be enabled or disabled.'
            error['code'] = 400
            return users
        # Locking yourself out is the one mistake this UI can make that no
        # amount of clicking undoes, so the last enabled admin cannot be
        # demoted or disabled — by anyone, including themselves.
        losing_admin = ((role == 'user' and target.get('role') == 'admin')
                        or (disabled is True and target.get('role') == 'admin'))
        if losing_admin and _admin_count(users, excluding=name) == 0:
            error['msg'] = 'This is the last administrator; promote another first.'
            error['code'] = 409
            return users
        if role is not None:
            target['role'] = role
        if disabled is not None:
            target['disabled'] = bool(disabled)
        if new_pw is not None:
            target['password_hash'] = generate_password_hash(new_pw)
            target['token_version'] = int(target.get('token_version', 1)) + 1
        if disabled is True:
            target['token_version'] = int(target.get('token_version', 1)) + 1
        return users

    _users_store.mutate(_apply)
    if error:
        return jsonify({'error': error['msg']}), error['code']

    updated = _find_user(name)
    # An admin who reset their own password just invalidated their own session.
    if name == actor['username'] and (new_pw is not None):
        _start_session(updated)
    print(f'[AUTH] account updated: {name} by {actor["username"]}', flush=True)
    return jsonify(_public_user(updated))


@app.route('/api/admin/users/<username>', methods=['DELETE'])
@admin_required
def admin_delete_user(username):
    actor = _current_user()
    name  = clean_username(username)
    if not name:
        return jsonify({'error': 'Unknown user.'}), 404
    if name == actor['username']:
        return jsonify({'error': 'You cannot delete your own account.'}), 409

    error = {}

    def _apply(users):
        target = _find_user(name, users)
        if target is None:
            error['msg'], error['code'] = 'Unknown user.', 404
            return users
        if target.get('role') == 'admin' and _admin_count(users, excluding=name) == 0:
            error['msg'] = 'This is the last administrator; promote another first.'
            error['code'] = 409
            return users
        return [u for u in users if u.get('username') != name]

    _users_store.mutate(_apply)
    if error:
        return jsonify({'error': error['msg']}), error['code']
    # Their data directory is left on disk — deleting an account should not
    # silently destroy a portfolio, and it is the only copy. Drop the cached
    # store handles so a later account reusing the name starts clean rather
    # than inheriting an open handle to those files.
    _forget_user_stores(name)
    print(f'[AUTH] account deleted: {name} by {actor["username"]} '
          f'(data left in {_user_data_dir(name)})', flush=True)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# StockBox report generation
# ---------------------------------------------------------------------------
import shutil as _shutil
import subprocess
import threading
import uuid as _uuid

_STOCKBOX_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'StockBox', 'StockBox')
)
_report_jobs: dict = {}  # job_id -> {status, ticker, owner, started, pdf_path?, error?}
_report_jobs_lock = threading.Lock()

# A rate limit bounds how *often* a report is asked for; it cannot bound how
# many are running, because the cost of one is that it lives for up to six
# minutes. Three requests a minute is polite and still stacks eighteen python
# subprocesses. This is the cap that matters, and it is per account so one user
# cannot starve another.
REPORT_MAX_CONCURRENT = int(os.environ.get('REPORT_MAX_CONCURRENT', '2') or 2)

# Finished jobs are kept far longer than the frontend polls for them, but not
# forever: nothing else ever removes an entry, and the concurrency check walks
# this dict on every request.
_REPORT_JOB_TTL = 6 * 3600


def _finish_report(job_id: str, **fields) -> None:
    """Record a job's outcome without losing what generate_report() stamped on it.

    Assigning a fresh dict here drops `started`, which is what the TTL prune
    reads — the entry would then never be collected, and the concurrency check
    walks this dict on every request.
    """
    with _report_jobs_lock:
        job = dict(_report_jobs.get(job_id) or {})
        job.update(fields)
        _report_jobs[job_id] = job


def _report_stem(ticker: str) -> str:
    """Filename stem StockBox writes its PDF under. Shared by the writer and the
    reader so the two can't drift apart."""
    return ticker.replace('.', '_').replace('/', '_').replace(' ', '_')


def _user_report_path(owner: str, ticker: str) -> str:
    """Where `owner`'s copy of the report for `ticker` lives.

    StockBox writes one PDF per ticker into its own output directory, so two
    accounts building AAPL overwrite each other's file and whoever reads that
    shared path last gets whichever build finished most recently. Each account
    gets its own copy instead, taken as soon as the build completes.
    """
    return os.path.join(_user_data_dir(owner), 'reports',
                        f'{_report_stem(ticker)}_report.pdf')


def _run_report(job_id: str, ticker: str, owner: str) -> None:
    proc = None
    try:
        # Defence in depth: callers already validate, but this string reaches a
        # command line, so refuse anything that isn't a bare symbol.
        if clean_ticker(ticker) is None:
            _finish_report(job_id, status='error', ticker=ticker,
                           error=f'Invalid ticker: {ticker!r}')
            return

        # This account's own keys — the report is built for them and any LLM
        # spend inside build_report.py should land on their key, not a shared one.
        # owner= is required: this runs on a worker thread with no request, so
        # _current_username() has no session to resolve and would (correctly)
        # refuse rather than guess whose keys to spend. Shared with the guidance
        # launcher: this used to fill the environment with `if _k not in env`,
        # which let an exported key win over the account's own.
        env = _account_env(owner)

        # No shell: argv is passed through verbatim, so nothing in `ticker` can
        # be read as a command separator. Uses this interpreter rather than
        # whatever `python` happens to resolve to on PATH.
        #
        # Output is piped rather than given its own console window: the old
        # `|| pause` trick needed a shell, and a window that closes on failure
        # loses the error anyway. Progress is echoed to the server log and the
        # tail is handed back to the UI, so a failure is diagnosable in place.
        proc = subprocess.Popen(
            [sys.executable, '-u', 'build_report.py', ticker],
            env=env,
            cwd=_STOCKBOX_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
        )

        tail = collections.deque(maxlen=40)
        for line in proc.stdout:
            line = line.rstrip()
            tail.append(line)
            print(f'[REPORT] {ticker}: {line}', flush=True)

        proc.wait(timeout=360)
        if proc.returncode == 0:
            built = os.path.join(_STOCKBOX_DIR, 'output', f'{_report_stem(ticker)}_report.pdf')
            # Take a private copy immediately: the shared path is overwritten by
            # the next account to build this ticker.
            mine = _user_report_path(owner, ticker)
            os.makedirs(os.path.dirname(mine), exist_ok=True)
            _shutil.copyfile(built, mine)
            _finish_report(job_id, status='done', ticker=ticker,
                           owner=owner, pdf_path=mine)
        else:
            detail = '\n'.join(tail).strip() or '(no output)'
            _finish_report(
                job_id,
                status='error',
                ticker=ticker,
                owner=owner,
                error=f'build_report.py exited with code {proc.returncode}.',
                detail=detail,
            )
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
        _finish_report(job_id, status='error', ticker=ticker, owner=owner,
                       error='Timed out after 6 minutes.')
    except Exception as e:
        _finish_report(job_id, status='error', ticker=ticker,
                       owner=owner, error=str(e))


@app.route('/api/generate-report', methods=['POST'])
def generate_report():
    data = request.get_json(silent=True) or {}
    ticker = clean_ticker(data.get('ticker'))
    if not ticker:
        return jsonify({'error': 'valid ticker required'}), 400
    owner  = _current_username()
    job_id = str(_uuid.uuid4())

    # Claim the slot and register the job under one lock. Counting first and
    # registering after leaves a window where two requests both see the same
    # free slot and both take it.
    now = _time_mod.time()
    with _report_jobs_lock:
        for jid, job in list(_report_jobs.items()):
            if job.get('status') != 'running' and \
                    now - job.get('started', now) > _REPORT_JOB_TTL:
                _report_jobs.pop(jid, None)
        running = sum(1 for job in _report_jobs.values()
                      if job.get('status') == 'running' and job.get('owner') == owner)
        if running >= REPORT_MAX_CONCURRENT:
            return jsonify({'error': f'{running} report(s) already building. '
                                     f'Wait for one to finish.'}), 429
        _report_jobs[job_id] = {'status': 'running', 'ticker': ticker,
                                'owner': owner, 'started': now}

    threading.Thread(target=_run_report, args=(job_id, ticker, owner),
                     daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/report-status/<job_id>', methods=['GET'])
def report_status(job_id):
    job = _report_jobs.get(job_id)
    # A job id is a uuid4 and so unguessable, but "unguessable" is not an access
    # check — and the error `detail` is a build log from another account's run.
    # Someone else's job reads as absent rather than forbidden.
    if not job or job.get('owner') != _current_username():
        return jsonify({'error': 'unknown job'}), 404
    return jsonify({k: v for k, v in job.items() if k != 'pdf_path'})


@app.route('/api/report-file/<path:ticker>', methods=['GET'])
def report_file(ticker):
    from flask import send_file as _send_file
    tkkr = clean_ticker(ticker)
    if not tkkr:
        return jsonify({'error': 'invalid ticker'}), 400
    # Serve this account's own copy, never StockBox's shared output file, which
    # belongs to whichever account built this ticker most recently.
    pdf_path = _user_report_path(_current_username(), tkkr)
    if not os.path.exists(pdf_path):
        return jsonify({'error': 'PDF not found'}), 404
    return _send_file(pdf_path, mimetype='application/pdf', as_attachment=False)


# ---------------------------------------------------------------------------
# Forward guidance
#
# Forward Guide is a separate project that reads a company's earnings 8-K press
# release and, when the guidance was only ever spoken, its earnings call, and
# returns what the company said it expects — plus the earnings, margin and cash
# figures those statements imply. It runs here the same way StockBox does: a
# subprocess, on a worker thread, spending *this account's* keys.
#
# Two things about the trigger are deliberate. It never runs on page load —
# a stock page opening would otherwise spend an LLM key on every lookup, and the
# figures move once a quarter, on a filing. And it is not started by rendering
# the panel either: `GET /api/forward_guidance` reads what is already stored and
# starts nothing, so a ticker looked up before shows instantly and for free.
# Only POST .../run spends anything, and only the button reaches it.
# ---------------------------------------------------------------------------
_FORWARD_GUIDE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Forward Guide')
)

# Keyed by the symbol the user asked about, not the filer Forward Guide
# resolved it to: the page knows MSFT.TO and would never find an entry filed
# under MSFT. The resolution is recorded *inside* the entry, because "this is
# the US filer behind your CDR" is worth saying out loud.
#
#   {'version': 1, 'tickers': {'MSFT.TO': {...}}}
#
# A dict rather than a list for the same reason the report copy is per account:
# a second lookup of a ticker replaces its entry and touches nothing else.
FORWARD_GUIDANCE_VERSION = 1


def _migrate_forward_guidance(data):
    """Fold a pre-button `report.py --push` payload into the per-ticker shape.

    That file was written by a batch run across the whole portfolio and carried
    one flat list of scorecards and one of items covering every company at
    once. The button keys on the symbol asked about, so the flat lists are
    regrouped and kept rather than dropped: this is guidance already extracted
    and already paid for, and discarding it would make the first press of every
    button spend to learn what the file already knew.

    Any per-ticker entry already present wins — it was written by a run through
    this route, which is newer than the batch that produced the flat lists.

    The one thing this cannot recover is the TSX mapping. The legacy payload
    records no resolution, so a row filed by MSFT lands under `MSFT` and a
    lookup of MSFT.TO re-runs — which is cheap, because the extraction itself
    is cached upstream. Filing Microsoft's guidance under a symbol the user
    never asked about would be the worse guess.
    """
    if not isinstance(data, dict):
        return data
    cards = data.get('scorecards')
    items = data.get('items')
    if not isinstance(cards, list) and not isinstance(items, list):
        return data                      # already the per-ticker shape

    stamp = data.get('generated_at') or ''
    tickers: dict = {}

    def _slot(sym):
        return tickers.setdefault(sym, {
            'symbol': sym, 'sec_ticker': sym, 'reachable': True, 'reason': '',
            'generated_at': stamp, 'scorecards': [], 'items': [],
        })

    for row in cards if isinstance(cards, list) else []:
        sym = str((row or {}).get('ticker') or '').upper()
        if sym:
            _slot(sym)['scorecards'].append(row)
    for row in items if isinstance(items, list) else []:
        sym = str((row or {}).get('ticker') or '').upper()
        if sym:
            _slot(sym)['items'].append(row)
    for row in data.get('unreachable') or []:
        sym = str((row or {}).get('symbol') or '').upper()
        if sym:
            entry = _slot(sym)
            entry['reachable'] = False
            entry['reason'] = row.get('reason') or ''

    existing = data.get('tickers')
    if isinstance(existing, dict):
        tickers.update(existing)
    return {'version': FORWARD_GUIDANCE_VERSION, 'tickers': tickers}


FORWARD_GUIDANCE_FILE = _register_user_file('forward_guidance.json', dict,
                                            migrate=_migrate_forward_guidance)

_guidance_jobs: dict = {}   # job_id -> {status, ticker, owner, started, error?}
_guidance_jobs_lock = threading.Lock()

# Same argument as REPORT_MAX_CONCURRENT: the cost of a run is that it lives for
# a minute or two — EDGAR, an LLM extraction and a yfinance walk — not that it
# is asked for often, and a rate limit cannot bound how many are alive at once.
# Per account, so one user cannot starve another.
GUIDANCE_MAX_CONCURRENT = int(os.environ.get('GUIDANCE_MAX_CONCURRENT', '2') or 2)
_GUIDANCE_JOB_TTL = 6 * 3600
_GUIDANCE_TIMEOUT = 300


def _finish_guidance(job_id: str, **fields) -> None:
    """Record an outcome without dropping `started`, which the TTL prune reads.

    Assigning a fresh dict here is the bug _finish_report() documents: nothing
    else removes an entry, and the concurrency check walks this dict on every
    request.
    """
    with _guidance_jobs_lock:
        job = dict(_guidance_jobs.get(job_id) or {})
        job.update(fields)
        _guidance_jobs[job_id] = job


def _merge_guidance(owner: str, symbol: str, entry: dict) -> None:
    """Fold one ticker's result into this account's stored payload.

    Through the store rather than written from the subprocess: `JsonStore`
    holds its lock across the read and the write, and a foreign process
    replacing the file wholesale would erase every other ticker in it — which
    is exactly what `report.py --push` does, and why the run writes to a temp
    file and this does the merging.
    """
    def _apply(data):
        data = data if isinstance(data, dict) else {}
        tickers = data.get('tickers')
        data['tickers'] = tickers if isinstance(tickers, dict) else {}
        data['version'] = FORWARD_GUIDANCE_VERSION
        data['tickers'][symbol] = entry
        return data

    _user_store(FORWARD_GUIDANCE_FILE, owner).mutate(_apply)


def _guidance_entry(symbol: str, payload: dict) -> dict:
    """One stored entry, built from what the run returned.

    Forward Guide answers about the filer; `resolved` carries the mapping back
    to the symbol that was asked about. An unreachable symbol still produces an
    entry, carrying its reason — "files 40-F/6-K, so never an 8-K item 2.02" is
    a different fact from "this company gave no guidance", needs a different
    fix, and only one of them is worth retrying.
    """
    resolved = next(
        (r for r in payload.get('resolved') or []
         if str(r.get('symbol', '')).upper() == symbol.upper()),
        {},
    )
    return {
        'symbol': symbol,
        'sec_ticker': resolved.get('sec_ticker') or symbol,
        'reachable': bool(resolved.get('reachable', True)),
        'reason': resolved.get('reason') or '',
        'generated_at': payload.get('generated_at') or '',
        'scorecards': payload.get('scorecards') or [],
        'items': payload.get('items') or [],
    }


def _run_guidance(job_id: str, symbol: str, owner: str) -> None:
    import tempfile

    proc = None
    out_path = None
    try:
        # Defence in depth: the route validates, but this string reaches an
        # argv, and _run_report documents what a raw .strip().upper() cost.
        if clean_ticker(symbol) is None:
            _finish_guidance(job_id, status='error', ticker=symbol, owner=owner,
                             error=f'Invalid ticker: {symbol!r}')
            return

        fd, out_path = tempfile.mkstemp(prefix='fg_', suffix='.json')
        os.close(fd)

        # No shell, and this interpreter rather than whatever `python` resolves
        # to. --json is the machine-readable handoff; --push is deliberately not
        # passed, because it would write this one ticker's payload over the
        # account's whole file. --brief keeps the piped log to the outlook
        # itself, which is what makes a failure diagnosable in the tail.
        proc = subprocess.Popen(
            [sys.executable, '-u', 'report.py',
             '--tickers', symbol, '--fetch', '--brief', '--json', out_path],
            env=_account_env(owner),
            cwd=_FORWARD_GUIDE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
        )

        tail = collections.deque(maxlen=40)
        for line in proc.stdout:
            line = line.rstrip()
            tail.append(line)
            print(f'[GUIDANCE] {symbol}: {line}', flush=True)

        proc.wait(timeout=_GUIDANCE_TIMEOUT)
        detail = '\n'.join(tail).strip() or '(no output)'

        if proc.returncode != 0:
            _finish_guidance(job_id, status='error', ticker=symbol, owner=owner,
                             error=f'report.py exited with code {proc.returncode}.',
                             detail=detail)
            return

        try:
            with open(out_path, encoding='utf-8') as f:
                payload = json.load(f)
        except Exception:
            # Exit 0 with no payload is a real outcome, not a crash: nothing
            # was extracted and nothing was written. Say so rather than
            # reporting a failure the log does not explain.
            _finish_guidance(job_id, status='error', ticker=symbol, owner=owner,
                             error='The run produced no payload.', detail=detail)
            return

        entry = _guidance_entry(symbol, payload)
        _merge_guidance(owner, symbol, entry)
        _finish_guidance(job_id, status='done', ticker=symbol, owner=owner,
                         items=len(entry['items']),
                         periods=len(entry['scorecards']),
                         reachable=entry['reachable'])
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
        _finish_guidance(job_id, status='error', ticker=symbol, owner=owner,
                         error=f'Timed out after {_GUIDANCE_TIMEOUT // 60} minutes.')
    except Exception as e:
        _finish_guidance(job_id, status='error', ticker=symbol, owner=owner,
                         error=str(e))
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass


@app.route('/api/forward_guidance', methods=['GET'])
def forward_guidance():
    """What is already stored for this account. Starts nothing, spends nothing.

    This is what the stock page reads when it opens a ticker it has looked up
    before, so it must stay free — the moment a read could trigger a run, every
    page load would spend an API key.
    """
    data = _user_store(FORWARD_GUIDANCE_FILE).load() or {}
    tickers = data.get('tickers') if isinstance(data, dict) else {}
    tickers = tickers if isinstance(tickers, dict) else {}

    wanted = request.args.get('ticker')
    if wanted:
        tkkr = clean_ticker(wanted)
        if not tkkr:
            return jsonify({'error': 'invalid ticker'}), 400
        entry = tickers.get(tkkr)
        return jsonify({'version': FORWARD_GUIDANCE_VERSION,
                        'ticker': tkkr, 'entry': entry})
    return jsonify({'version': FORWARD_GUIDANCE_VERSION, 'tickers': tickers})


@app.route('/api/forward_guidance/run', methods=['POST'])
def forward_guidance_run():
    data   = request.get_json(silent=True) or {}
    symbol = clean_ticker(data.get('ticker'))
    if not symbol:
        return jsonify({'error': 'valid ticker required'}), 400

    # Refused independently of /api/stock rather than leaning on it. This route
    # spends the account's Anthropic key, and it is reachable on its own — the
    # same reason /api/news does its own check instead of trusting that the
    # detail page already 403'd.
    if symbol in _blocked_set():
        return jsonify({'error': f'{symbol} is hidden.',
                        'blocked': True, 'ticker': symbol}), 403

    owner = _current_username()

    # Checked here so the answer is a sentence rather than a subprocess that
    # exits 1 a second later with the reason buried in a log tail.
    if not _resolve_api_key('ANTHROPIC_API_KEY', owner):
        return jsonify({'error': 'No Anthropic API key configured. Add one on '
                                 'the Settings tab — guidance extraction runs '
                                 'on your own key.',
                        'needs_key': 'ANTHROPIC_API_KEY'}), 400

    job_id = str(_uuid.uuid4())
    now    = _time_mod.time()
    with _guidance_jobs_lock:
        for jid, job in list(_guidance_jobs.items()):
            if job.get('status') != 'running' and \
                    now - job.get('started', now) > _GUIDANCE_JOB_TTL:
                _guidance_jobs.pop(jid, None)
        running = sum(1 for job in _guidance_jobs.values()
                      if job.get('status') == 'running' and job.get('owner') == owner)
        if running >= GUIDANCE_MAX_CONCURRENT:
            return jsonify({'error': f'{running} guidance run(s) already going. '
                                     f'Wait for one to finish.'}), 429
        _guidance_jobs[job_id] = {'status': 'running', 'ticker': symbol,
                                  'owner': owner, 'started': now}

    threading.Thread(target=_run_guidance, args=(job_id, symbol, owner),
                     daemon=True).start()
    # Told up front so the UI can say the transcript fallback is off rather than
    # leave "no guidance" standing for a company that only ever said it aloud.
    return jsonify({'job_id': job_id,
                    'transcripts': bool(_resolve_api_key('ALPHAVANTAGE_API_KEY', owner))})


@app.route('/api/forward_guidance/status/<job_id>', methods=['GET'])
def forward_guidance_status(job_id):
    job = _guidance_jobs.get(job_id)
    # Someone else's job reads as absent rather than forbidden: a uuid4 is
    # unguessable, but unguessable is not an access check, and `detail` is a
    # run log from another account.
    if not job or job.get('owner') != _current_username():
        return jsonify({'error': 'unknown job'}), 404
    return jsonify(job)


# Insider transaction classification.
#
# Ported from stock-intel, which rebuilt these rules from the actual phrase
# vocabulary Yahoo returns. A naive `'Purchase' in text` / `'Sale' in text`
# test gets this wrong in both directions:
#
#   "Disposition in the public market"            a sale, matches neither
#   "Disposition under a purchase/ownership plan" a sale, contains "purchase"
#   "Redemption, retraction, cancelation, repurchase"  a corporate buyback
#   ""                                            RSU vesting, ~half of all rows
#
# Order matters: exclusions are tested before buy/sell so that a row reading
# "Disposition under a purchase/ownership plan" cannot be scored as a purchase.
_INSIDER_RULES = (
    (r"repurchase|redemption|retraction|cancell?ation", 'exclude', 'corporate_buyback'),
    # "Compensation for services" is payment in shares, not a conviction trade.
    # Found on TD.TO; stock-intel's copy of these rules does not cover it yet.
    (r"\baward\b|\bgrant\b|\bvest|compensation",        'exclude', 'award_or_vesting'),
    (r"\bgift\b",                                       'exclude', 'gift'),
    (r"exercise|conversion",                            'exclude', 'option_exercise'),
    (r"\bsale\b|\bsold\b|disposition|disposed",         'sell',    'sale'),
    (r"\bpurchase\b|\bbought\b|\bacquisition\b|\bacquired\b", 'buy', 'purchase'),
)

_INSIDER_COMPILED = tuple(
    (__import__('re').compile(pat, __import__('re').I), kind, label)
    for pat, kind, label in _INSIDER_RULES
)


def classify_insider(text):
    """Return (kind, label) where kind is 'buy', 'sell' or 'exclude'."""
    cleaned = (text or '').strip()
    if not cleaned:
        return 'exclude', 'undisclosed'
    for pattern, kind, label in _INSIDER_COMPILED:
        if pattern.search(cleaned):
            return kind, label
    return 'exclude', 'unrecognized'


@app.route('/api/insider-buying/<ticker>', methods=['GET'])
def insider_buying(ticker):
    import pandas as _pd
    import datetime as _dt
    tkkr = clean_ticker(ticker)
    if not tkkr:
        return jsonify({'error': 'invalid ticker'}), 400

    _empty = {'dates': [], 'cumulative': [], 'cumulative_pct': [],
              'pct_available': False, 'period_summary': {}, 'transactions': [],
              'excluded': {}}
    try:
        t  = yf.Ticker(tkkr)
        df = t.insider_transactions
        if df is None or (hasattr(df, 'empty') and df.empty):
            return jsonify(_empty)

        # Shares outstanding for % calculations
        shares_outstanding = None
        try:
            fi = t.fast_info
            shares_outstanding = getattr(fi, 'shares', None)
        except Exception:
            pass
        if not shares_outstanding:
            try:
                info = t.info
                shares_outstanding = (info.get('sharesOutstanding')
                                      or info.get('impliedSharesOutstanding'))
            except Exception:
                pass

        rows = []
        excluded = {}
        for _, row in df.iterrows():
            text   = str(row.get('Text', '') or '')
            shares = row.get('Shares', None)

            if shares is None or (isinstance(shares, float) and _pd.isna(shares)):
                continue
            try:
                shares = int(shares)
            except (ValueError, TypeError):
                continue

            kind_raw, label = classify_insider(text)
            if kind_raw == 'exclude':
                # Counted and reported rather than dropped in silence, so the
                # chart can say what it left out.
                excluded[label] = excluded.get(label, 0) + 1
                if label == 'unrecognized':
                    print(f'[INSIDER] {tkkr}: unrecognised text: {text!r}', flush=True)
                continue
            if kind_raw == 'buy':
                kind, net = 'Buy',  shares
            else:
                kind, net = 'Sell', -shares

            date_val = row.get('Start Date')
            if date_val is None or (isinstance(date_val, float) and _pd.isna(date_val)):
                date_val = row.get('Date', None)
            if date_val is None:
                continue
            try:
                date_str = str(_pd.Timestamp(date_val).date())
            except Exception:
                continue

            insider_name = str(row.get('Insider', '') or '').strip()
            raw_val = row.get('Value', None)
            try:
                value = int(raw_val) if raw_val is not None and not _pd.isna(raw_val) else None
            except (ValueError, TypeError):
                value = None
            rows.append({'date': date_str, 'insider': insider_name,
                         'type': kind, 'shares': shares, 'net': net, 'value': value})

        if not rows:
            return jsonify({**_empty, 'excluded': excluded})

        rows.sort(key=lambda r: r['date'])

        pct_ok = bool(shares_outstanding and shares_outstanding > 0)
        running = 0
        dates, cumulative, cumulative_pct = [], [], []
        for r in rows:
            running += r['net']
            dates.append(r['date'])
            cumulative.append(running)
            cumulative_pct.append(round(running / shares_outstanding * 100, 4) if pct_ok else None)

        # Period summaries: net shares (and %) over trailing windows
        today = _dt.date.today()
        period_summary = {}
        for label, days in [('1M', 30), ('3M', 90), ('6M', 180), ('1Y', 365), ('All', None)]:
            cutoff = str(today - _dt.timedelta(days=days)) if days else '0000-00-00'
            net_sh = sum(r['net'] for r in rows if r['date'] >= cutoff)
            period_summary[label] = {
                'net_shares': net_sh,
                'net_pct': round(net_sh / shares_outstanding * 100, 4) if pct_ok else None,
            }

        transactions = []
        for r in rows:
            transactions.append({
                'date':    r['date'],
                'insider': r['insider'],
                'type':    r['type'],
                'shares':  r['shares'],
                'value':   r.get('value'),
                'pct':     round(r['shares'] / shares_outstanding * 100, 4) if pct_ok else None,
            })

        return jsonify({
            'dates': dates,
            'cumulative': cumulative,
            'cumulative_pct': cumulative_pct,
            'pct_available': pct_ok,
            'period_summary': period_summary,
            'transactions': transactions,
            'excluded': excluded,
        })

    except Exception as e:
        print(f'[INSIDER] {tkkr}: error: {e}', flush=True)
        return jsonify(_empty)


# ── Valuations ─────────────────────────────────────────────────────────────────
def _migrate_valuations(items):
    """Carry the pre-split schema's single intrinsic_value onto buy_price.

    A row used to hold one number, and the UI derived an upside percentage from
    it; it now holds two thresholds. The old number was the price the user was
    willing to pay, so it becomes buy_price. sell_price stays None on purpose —
    deriving one (intrinsic x some multiple) would fire a SELL signal the user
    never set.
    """
    for item in items:
        if 'buy_price' not in item:
            old = item.pop('intrinsic_value', None)
            item['buy_price'] = round(float(old), 4) if old else None
        item.setdefault('sell_price', None)
    return items


VALUATIONS_FILE = _register_user_file('valuations.json', list,
                                      migrate=_migrate_valuations)

def load_valuations(owner=None):
    return _user_store(VALUATIONS_FILE, owner).load()

def save_valuations(items, owner=None):
    _user_store(VALUATIONS_FILE, owner).save(items)


def _parse_threshold(data, key, current):
    """Resolve one optional price field against what is already stored.

    Both thresholds are independently optional, and the table edits them one
    input at a time, so an absent key has to mean "leave it alone" rather than
    "clear it" — otherwise saving a buy price would wipe the sell price. An
    explicit null or empty string is the clear. Returns (value, error).
    """
    if key not in data:
        return current, None
    raw = data.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, f'invalid {key}'
    if v <= 0:
        return None, f'{key} must be positive'
    return round(v, 4), None


@app.route('/api/valuations', methods=['GET'])
def get_valuations():
    return jsonify(load_valuations())

@app.route('/api/valuations', methods=['POST'])
@atomic
def upsert_valuation():
    data   = request.json or {}
    ticker = clean_ticker(data.get('ticker'))
    if not ticker:
        return jsonify({'error': 'ticker required'}), 400

    items    = load_valuations()
    existing = next((i for i in items if i['ticker'] == ticker), None)

    buy, err = _parse_threshold(data, 'buy_price',
                                existing.get('buy_price') if existing else None)
    if err:
        return jsonify({'error': err}), 400
    sell, err = _parse_threshold(data, 'sell_price',
                                 existing.get('sell_price') if existing else None)
    if err:
        return jsonify({'error': err}), 400

    if buy is None and sell is None:
        return jsonify({'error': 'buy_price or sell_price required'}), 400
    # Overlapping bands would make a single quote both a BUY and a SELL.
    if buy is not None and sell is not None and sell <= buy:
        return jsonify({'error': 'sell_price must be above buy_price'}), 400

    today = str(_date.today())
    if existing:
        existing['buy_price']  = buy
        existing['sell_price'] = sell
        existing['name']  = data.get('name', existing.get('name', ticker))
        existing['notes'] = data.get('notes', existing.get('notes', ''))
        existing['added'] = existing.get('added') or today
    else:
        items.append({
            'ticker':     ticker,
            'name':       data.get('name', ticker),
            'buy_price':  buy,
            'sell_price': sell,
            'notes':      data.get('notes', ''),
            'added':      today,
        })
    save_valuations(items)
    return jsonify(items)

@app.route('/api/valuations/<ticker>', methods=['DELETE'])
@atomic
def delete_valuation(ticker):
    ticker = clean_ticker(ticker)
    if not ticker:
        return jsonify({'error': 'invalid ticker'}), 400
    items = [i for i in load_valuations() if i['ticker'] != ticker]
    save_valuations(items)
    return jsonify(items)


# ═══════════════════════════════════════════════════════════════════════════════
# Breaking market news
#
# /api/news is per-ticker: it answers "what happened to AAPL". Nothing answered
# "what happened that moves markets" — an oil shock, a rate decision, a tariff
# order — until the user happened to look up an affected company.
#
# Sources are RSS, not yfinance, for two reasons. yfinance only serves news per
# symbol, so it cannot carry a non-ticker story at all; and measured against the
# real endpoints, all 15 feeds together take 0.58s and yield ~330 items, where
# the per-ticker pipeline takes 2-20s for ONE symbol. No API key and no new
# dependency: requests and lxml are already required.
#
# Deliberately absent: Reuters discontinued its public RSS (feeds.reuters.com no
# longer resolves) and Bloomberg publishes none. Both would need a paid API.
#
# Nothing here calls Groq. The per-ticker path rewrites headlines through an LLM
# because a Yahoo summary is not a headline; an RSS <title> already is. That also
# keeps this path working with no key configured at all, which is the state
# groq_call() degrades to.
# ═══════════════════════════════════════════════════════════════════════════════
import html as _html
import re as _re_mod
from urllib.parse import urlsplit as _urlsplit

MARKET_NEWS_TTL      = 300   # same cadence as MOVERS_TTL and NEWS_TTL
MARKET_MAX_AGE_H     = 48    # a breaking page showing 3-day-old items is broken
MARKET_FEED_TIMEOUT  = (3, 5)
MARKET_FEED_POOL     = 10
MARKET_BUILD_TIMEOUT = 14    # inside the 25s budget the other slow routes use
MARKET_NEWS_LIMIT    = 80

# (url, display_name, group, tier)
#   tier 1 = primary source — a central bank publishing its own decision
#   tier 2 = major wire or broadcaster
#   tier 3 = aggregator
# Tier is a registry field rather than the display-name lookup _build_news uses
# (_tier1/_tier2), because here we control the names instead of matching whatever
# string Yahoo happens to emit.
_MARKET_FEEDS = (
    ('https://www.cnbc.com/id/100003114/device/rss/rss.html',         'CNBC',              'financial', 2),
    ('https://www.cnbc.com/id/20910258/device/rss/rss.html',          'CNBC Economy',      'financial', 2),
    ('https://www.cnbc.com/id/10000664/device/rss/rss.html',          'CNBC Finance',      'financial', 2),
    ('https://feeds.content.dowjones.io/public/rss/mw_topstories',    'MarketWatch',       'financial', 2),
    ('https://finance.yahoo.com/news/rssindex',                       'Yahoo Finance',     'financial', 3),
    ('https://www.investing.com/rss/news.rss',                        'Investing.com',     'financial', 3),
    ('https://www.cbc.ca/webfeed/rss/rss-business',                   'CBC Business',      'financial', 3),
    ('https://www.federalreserve.gov/feeds/press_all.xml',            'Federal Reserve',   'policy',    1),
    ('https://www.bankofcanada.ca/content_type/press-releases/feed/', 'Bank of Canada',    'policy',    1),
    ('https://www.ecb.europa.eu/rss/press.html',                      'ECB',               'policy',    1),
    ('https://www.whitehouse.gov/presidential-actions/feed/',         'White House',       'policy',    1),
    ('https://feeds.bbci.co.uk/news/business/rss.xml',                'BBC Business',      'broad',     2),
    ('https://feeds.bbci.co.uk/news/world/rss.xml',                   'BBC World',         'broad',     2),
    ('https://feedx.net/rss/ap.xml',                                  'AP',                'broad',     2),
    ('https://www.theguardian.com/uk/business/rss',                   'Guardian Business', 'broad',     2),
)

# The broad feeds carry a lot of sport and showbiz. The URL path discriminates
# far more reliably than the headline text does.
_SKIP_PATH_RE = _re_mod.compile(
    r'/(sport|sports|football|soccer|cricket|rugby|tennis|nfl|nba|olympics|'
    r'entertainment|celebrity|lifestyle|culture|tv-and-radio|music|film|'
    r'fashion|food|travel|games|weather|obituaries)(/|$)')


def _kw_re(words):
    """One anchored alternation, longest-first so 'rate cut' wins over 'rate'.

    Both-side \\b matters: a bare '\\bwar' prefix match tags "embassies WARN
    americans" and "cyber WARNINGS" as war coverage. Measured against live feeds,
    not hypothetical.
    """
    ordered = sorted(set(words), key=len, reverse=True)
    return _re_mod.compile(r'\b(?:' + '|'.join(_re_mod.escape(w) for w in ordered) + r')\b')


# Company-event words (_action_kw in _build_news) are useless here — a macro feed
# needs its own vocabulary. Weighted in three bands, each capped so one keyword-
# stuffed headline can't dominate the ranking.
_MARKET_KW_HIGH = _kw_re((
    'federal reserve', 'fomc', 'fed chair', 'rate cut', 'rate hike', 'rate decision',
    'interest rate', 'interest rates', 'central bank', 'basis points', 'inflation',
    'cpi', 'pce', 'jobs report', 'nonfarm', 'payrolls', 'unemployment', 'gdp',
    'recession', 'tariff', 'tariffs', 'sanction', 'sanctions', 'opec', 'embargo',
    # 'default' alone is unusable — it is a path segment in half the image URLs
    # on the internet, so it tagged every AP sports story as a sovereign default.
    'debt ceiling', 'shutdown', 'debt default', 'sovereign default',
    'bank failure', 'bailout', 'stimulus',
    'war', 'invasion', 'treasury yield', 'bond yield', 'yield curve',
))
_MARKET_KW_MED = _kw_re((
    'trade deal', 'trade war', 'export control', 'crude', 'oil price', 'natural gas',
    'gold', 'dollar', 'yuan', 'euro', 'boj', 'ecb', 'bank of england',
    'bank of canada', 'imf', 'credit', 'housing', 'retail sales', 'pmi', 'ppi',
    'layoffs', 'antitrust', 'regulation', 'regulator', 'election', 'ceasefire',
    'supply chain', 'semiconductor', 'semiconductors', 'chips', 'strike',
    'blockade', 'missile', 'airstrike', 'drone', 'nato', 'deficit',
))
_MARKET_KW_VERB = _kw_re((
    'breaking', 'announces', 'unveils', 'signs', 'imposes', 'halts', 'suspends',
    'bans', 'slashes', 'surges', 'plunges', 'tumbles', 'soars', 'warns',
    'escalates', 'collapses', 'downgrades', 'freezes', 'seizes',
))

# Ordered, first match wins — the _INSIDER_RULES discipline. A single-valued
# category is what lets the frontend chips partition the feed cleanly.
_CATEGORY_RULES = (
    ('central-bank', _kw_re(('federal reserve', 'fed', 'fomc', 'ecb', 'boj', 'boe',
                             'bank of england', 'bank of canada', 'central bank',
                             'monetary policy', 'rate cut', 'rate hike', 'rate decision',
                             'basis points', 'quantitative easing', 'policy rate'))),
    ('policy',       _kw_re(('tariff', 'tariffs', 'sanction', 'sanctions', 'executive order',
                             'congress', 'senate', 'shutdown', 'tax bill', 'antitrust',
                             'regulator', 'regulation', 'trade deal', 'trade war',
                             'export control', 'subsidy', 'stimulus', 'debt ceiling'))),
    ('geopolitics',  _kw_re(('war', 'invasion', 'missile', 'airstrike', 'ceasefire', 'nato',
                             'coup', 'election', 'border', 'strait', 'drone', 'troops',
                             'embargo', 'blockade', 'terror', 'hostage', 'militia'))),
    ('commodities',  _kw_re(('oil', 'crude', 'opec', 'brent', 'wti', 'natural gas', 'lng',
                             'gold', 'copper', 'wheat', 'barrel', 'pipeline', 'refinery',
                             'commodity', 'commodities'))),
    ('economy',      _kw_re(('cpi', 'pce', 'inflation', 'gdp', 'jobs', 'payrolls', 'nonfarm',
                             'unemployment', 'pmi', 'ppi', 'retail sales', 'housing',
                             'recession', 'consumer', 'deficit', 'wages'))),
)

_MARKET_DATE_TAGS = ('pubDate', 'published', 'updated', 'date', 'dc:date')


def _feed_datetime(raw):
    """RFC-822 or ISO-8601 -> naive UTC datetime, or None.

    Two traps, both measured against the live feeds:

      * The tag is not always <pubDate> — Bank of Canada uses <date>.
      * You cannot pick the parser from the tag name — Yahoo Finance puts
        ISO-8601 inside a <pubDate> tag while everyone else puts RFC-822 there.

    Naive UTC is a contract, not a preference: the frontend does
    `new Date(pub_ts + 'Z')`, so returning a tz-aware value double-applies the
    offset. ECB is the feed that proves it — it publishes +0200, and left
    unconverted its releases land two hours in the future and pin themselves to
    the top of a recency-ranked feed.
    """
    raw = (raw or '').strip()
    if not raw:
        return None
    from email.utils import parsedate_to_datetime
    for parse in (parsedate_to_datetime,
                  lambda s: _datetime.fromisoformat(s.replace('Z', '+00:00'))):
        try:
            dt = parse(raw)
        except Exception:
            continue
        if dt is None:
            continue
        if dt.tzinfo is not None:
            dt = dt.astimezone(_timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _parse_feed(xml_bytes, name, group, tier):
    """Parse one feed body into raw item dicts. Handles RSS 2.0, RDF and Atom."""
    from bs4 import BeautifulSoup
    # r.content, never r.text: letting lxml read the XML declaration avoids the
    # mojibake you get when requests guesses the charset.
    soup = BeautifulSoup(xml_bytes, 'xml')
    out = []
    for node in soup.find_all(['item', 'entry'])[:30]:
        t = node.find('title')
        title = _html.unescape(t.get_text(strip=True)) if t else ''
        link_node = node.find('link')
        url = ''
        if link_node is not None:
            url = (link_node.get('href') or link_node.get_text(strip=True) or '').strip()
        if not (title and url):
            continue
        raw_date = ''
        for tag in _MARKET_DATE_TAGS:
            node_d = node.find(tag)
            if node_d is not None and node_d.get_text(strip=True):
                raw_date = node_d.get_text(strip=True)
                break
        # <description> is not plain text. Several feeds put escaped HTML in it —
        # AP ships a full <img> tag — so it has to be unescaped and then stripped
        # of markup. Skipping the strip put raw tags in the payload and, because
        # AP's image URLs contain the path segment "default", scored every one of
        # its sport and box-office stories as a debt default.
        desc = node.find('description') or node.find('summary')
        summary = ''
        if desc is not None:
            unescaped = _html.unescape(desc.get_text(' ', strip=True))
            summary = BeautifulSoup(unescaped, 'html.parser').get_text(' ', strip=True)[:300]
        out.append({'title': title, 'url': url, 'source': name, 'group': group,
                    'tier': tier, 'summary': summary, 'pub_dt': _feed_datetime(raw_date)})
    return out


def _fetch_feed(entry):
    """Fetch and parse one feed. Raises on failure so the caller can count it.

    Returning [] instead would erase the difference between "this feed published
    nothing" and "this feed is broken" — the same distinction _fetch_div_events
    keeps, and for the same reason: a silent blank gets cached.
    """
    import requests as _req
    url, name, group, tier = entry
    r = _req.get(url, timeout=MARKET_FEED_TIMEOUT,
                 headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    if r.status_code != 200:
        raise RuntimeError(f'{name}: HTTP {r.status_code}')
    items = _parse_feed(r.content, name, group, tier)
    if not items:
        raise RuntimeError(f'{name}: no items parsed')
    return items


def _canon_url(url):
    """Scheme/host/query-insensitive key, so ?utm_source variants collapse."""
    try:
        parts = _urlsplit(url)
        host = parts.netloc.lower().split(':')[0]
        if host.startswith('www.'):
            host = host[4:]
        return host + parts.path.rstrip('/').lower()
    except Exception:
        return url.lower()


_TITLE_STOP = {'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'on', 'for', 'at',
               'as', 'by', 'is', 'are', 'was', 'were', 'be', 'with', 'from', 'that',
               'this', 'it', 'its', 'after', 'over', 'says', 'said', 'amid', 'new'}


def _title_tokens(title):
    """Meaningful words of a headline, for both dedupe passes."""
    cleaned = _re_mod.sub(r'^[^:|-]{0,24}[:|-]\s+', '', title)          # drop "Source - " prefix
    words = _re_mod.findall(r'[a-z0-9]+', cleaned.lower())
    return {w for w in words if len(w) >= 4 and w not in _TITLE_STOP}


def _title_signature(title):
    """Order-independent exact fingerprint: the 6 longest meaningful tokens.

    Cheap O(1) key that catches re-punctuated and re-ordered reprints of the
    same headline. It does NOT catch a genuine rewording — "Fed holds rates
    steady" and "Federal Reserve holds interest rates steady at 4.25%" produce
    different signatures, which is what the overlap pass in _dedupe_news is for.
    """
    toks = _title_tokens(title)
    return '-'.join(sorted(sorted(toks, key=len, reverse=True)[:6]))


def _impact_score(item, now):
    """How likely is this to move a market, and how fresh is it."""
    hay = (item['title'] + ' ' + item.get('summary', '')).lower()
    score = {1: 45, 2: 30}.get(item['tier'], 15)
    score += 25 * min(len(set(_MARKET_KW_HIGH.findall(hay))), 2)
    score += 12 * min(len(set(_MARKET_KW_MED.findall(hay))), 3)
    score += 10 * min(len(set(_MARKET_KW_VERB.findall(hay))), 2)

    hours = item.get('hours_old')
    if hours is not None:
        score += max(0, 45 * (1 - hours / 24))
        if hours < 2:
            score += 25
    if any(p.search(item['title'].lower()) for p in _HARD_SKIP_RE):
        score -= 100
    return score


def _categorise(item):
    """Single category, first rule wins. Policy-group feeds short-circuit."""
    hay = (item['title'] + ' ' + item.get('summary', '')).lower()
    if item['group'] == 'policy':
        return 'central-bank' if item['source'] != 'White House' else 'policy'
    for label, pattern in _CATEGORY_RULES:
        if pattern.search(hay):
            return label
    return 'markets'


def _dedupe_news(items):
    """Collapse the same story across feeds, keeping the best-scoring copy.

    The survivor records the others in `also`. That is not decoration: how many
    independent outlets carry a story within the hour is the strongest "this is
    breaking" signal available without a paid wire, so it feeds back into the
    flag below.
    """
    def absorb(winner, loser):
        if loser['source'] != winner['source'] and loser['source'] not in winner['also']:
            winner['also'].append(loser['source'])

    # Pass 1 — exact keys. O(n), catches the common case: the identical story
    # syndicated under utm-tagged URLs, or reprinted with different punctuation.
    best, survivors = {}, []
    for item in sorted(items, key=lambda i: i['score'], reverse=True):
        key = _canon_url(item['url'])
        sig = _title_signature(item['title'])
        hit = best.get(key) or (best.get(sig) if sig else None)
        if hit is not None:
            absorb(hit, item)
            continue
        item['also'] = []
        item['_tokens'] = _title_tokens(item['title'])
        best[key] = item
        if sig:
            best[sig] = item
        survivors.append(item)

    # Pass 2 — token overlap, for a genuine rewording that pass 1 cannot key on
    # ("Fed holds rates steady" vs "Federal Reserve holds interest rates steady
    # at 4.25%"). O(n^2) over ~330 items is ~55k set intersections; the exact
    # pass has already removed the bulk.
    merged = []
    for item in survivors:                       # already in descending score order
        dupe_of = None
        for kept in merged:
            a, b = item['_tokens'], kept['_tokens']
            if len(a) >= 3 and len(b) >= 3:
                if len(a & b) / min(len(a), len(b)) >= 0.7:
                    dupe_of = kept
                    break
        if dupe_of is not None:
            absorb(dupe_of, item)
        else:
            merged.append(item)

    for item in merged:
        item.pop('_tokens', None)
    return merged


# ── Relevance filtering ───────────────────────────────────────────────────────
# Keyword scoring ranks what is already market news; it cannot tell "Boeing
# clears key hurdle and stock rallies" from "Why flights are so expensive".
# Measured on a live build, ~30 of the 55 items in the 'markets' catch-all were
# advice columns, pundit picks, rating boilerplate or human interest.
#
# Two layers. The regexes below take the unambiguous boilerplate for free and
# work with no API key at all; an LLM judges the rest.

# Anchored at the start because these are feed-generated title prefixes, not
# phrases that occur mid-headline.
_NEWS_BOILERPLATE_RE = _re_mod.compile(
    r'^\s*(analyst report|market update|form \d+|earnings call transcript|'
    r'daily briefing|week ahead|what to watch)\b[:\s]', _re_mod.I)

# First-person money stories. The pronoun is the signal — a headline about the
# reader's own finances is never about a tradable event.
_NEWS_ADVICE_RE = _re_mod.compile(
    r'\b(should|can|will|must)\s+i\b'
    r'|\bi\s+(paid|bought|sold|inherited|owe|earn|retired|invested)\b'
    r'|\bmy\s+(wife|husband|ex|ex-\w+|mother|father|son|daughter|sister|brother|'
    r'parents|in-laws|boss|landlord|401|ira|pension|savings|nest egg)\b'
    r'|\bhere.s how (much )?(you|i|we)\b', _re_mod.I)

_LLM_FILTER_TIMEOUT = 20
_LLM_FILTER_BATCH   = 60

# Both APIs are OpenAI-compatible, so one request shape drives either. DeepSeek
# leads because it judges the borderline cases better; Groq is the fallback
# because a key for it is usually already configured.
_LLM_BACKENDS = (
    ('DEEPSEEK_API_KEY', 'https://api.deepseek.com/chat/completions',       'deepseek-chat'),
    ('GROQ_API_KEY',     'https://api.groq.com/openai/v1/chat/completions', 'llama-3.1-8b-instant'),
)

_NEWS_FILTER_SYSTEM = (
    "You filter a markets news feed for a professional investor.\n"
    "KEEP a headline only if it reports something that could move a market or a "
    "specific security: company events, earnings, M&A, products, regulation, "
    "legal action, central-bank decisions, macro data, commodities, trade "
    "policy, or geopolitics with an economic channel.\n"
    "DROP: personal-finance or retirement advice, first-person money stories, "
    "pundit opinion and stock picks, analyst-rating boilerplate, SEC form "
    "notices, sport, celebrity, box office, human interest, and health or crime "
    "stories with no market angle.\n"
    "Reply with ONLY the numbers to keep, comma-separated. Nothing else."
)

# Headline -> keep/drop, so a rebuild re-judges only what is new. Also stops the
# feed flickering when the model decides a borderline item differently.
_llm_verdicts: dict = {}
_LLM_VERDICT_CAP = 4000


def _llm_backend(owner=None):
    """This account's first configured backend, or None. Order is preference."""
    for key_name, url, model in _LLM_BACKENDS:
        key = _resolve_api_key(key_name, owner)
        if key:
            return {'name': key_name, 'key': key, 'url': url, 'model': model}
    return None


def _llm_keep_indices(titles, backend):
    """Ask the model which headlines to keep. Raises so the caller can degrade."""
    import requests as _req

    numbered = '\n'.join(f'{i}. {t}' for i, t in enumerate(titles))
    r = _req.post(
        backend['url'],
        headers={'Authorization': f"Bearer {backend['key']}",
                 'Content-Type': 'application/json'},
        json={'model': backend['model'], 'temperature': 0, 'max_tokens': 400,
              'messages': [{'role': 'system', 'content': _NEWS_FILTER_SYSTEM},
                           {'role': 'user', 'content': numbered}]},
        timeout=_LLM_FILTER_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"{backend['name']} HTTP {r.status_code}: {r.text[:120]}")
    text = r.json()['choices'][0]['message']['content']
    keep = {int(n) for n in _re_mod.findall(r'\d+', text) if int(n) < len(titles)}
    if not keep:
        raise RuntimeError('no usable indices in response')
    return keep


def _filter_relevant(items, backend=None):
    """Drop what isn't market news. Returns (items, mode).

    Never drops on failure: an unreachable model or a missing key degrades to
    the regex layer rather than emptying the page. Same instinct as the thinness
    guard — a filter that silently eats the feed is worse than no filter.

    `backend` is resolved by the caller, in the request, because this runs
    under a cache fetcher that can be entered from anywhere. An account with no
    key still gets the regex layer, which needs no key at all.
    """
    kept = [i for i in items
            if not _NEWS_BOILERPLATE_RE.search(i['title'])
            and not _NEWS_ADVICE_RE.search(i['title'])]

    if not backend:
        print('[MKTNEWS] no LLM key configured; regex filter only', flush=True)
        return kept, 'regex'

    undecided = [i for i in kept if i['title'] not in _llm_verdicts]
    batches = ok_batches = 0
    for batch_start in range(0, len(undecided), _LLM_FILTER_BATCH):
        batch = undecided[batch_start:batch_start + _LLM_FILTER_BATCH]
        titles = [i['title'] for i in batch]
        batches += 1
        try:
            keep_idx = _llm_keep_indices(titles, backend)
        except Exception as e:
            print(f'[MKTNEWS] LLM filter failed ({e}); keeping batch', flush=True)
            continue
        ok_batches += 1
        for n, item in enumerate(batch):
            _llm_verdicts[item['title']] = n in keep_idx

    # Don't credit the model for a pass it didn't make. The payload drives a UI
    # label, and "filtered by groq" over an unfiltered page is a lie that hides
    # an outage.
    if batches and not ok_batches:
        return kept, 'regex'

    if len(_llm_verdicts) > _LLM_VERDICT_CAP:
        _llm_verdicts.clear()

    # Default True: anything the model never ruled on survives.
    out = [i for i in kept if _llm_verdicts.get(i['title'], True)]

    # A degenerate response should not empty the page.
    if len(out) < max(5, len(kept) // 8):
        print(f'[MKTNEWS] LLM dropped {len(kept)}->{len(out)}; ignoring', flush=True)
        return kept, 'regex'
    return out, backend['name'].replace('_API_KEY', '').lower()


def _fetch_market_news(_key, backend=None):
    """Build the ranked feed. Raises when the result is too thin to cache."""
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FT

    now = _datetime.utcnow()
    raw, ok, failed = [], 0, []
    with ThreadPoolExecutor(max_workers=MARKET_FEED_POOL) as pool:
        futures = {pool.submit(_fetch_feed, f): f for f in _MARKET_FEEDS}
        try:
            for fut in as_completed(futures, timeout=MARKET_BUILD_TIMEOUT):
                name = futures[fut][1]
                try:
                    raw.extend(fut.result())
                    ok += 1
                except Exception as e:
                    failed.append(name)
                    print(f'[MKTNEWS] {name}: {e}', flush=True)
        except _FT:
            pending = [f[1] for fut, f in futures.items() if not fut.done()]
            failed.extend(pending)
            print(f'[MKTNEWS] timed out waiting on {pending}', flush=True)

    items = []
    for item in raw:
        if _SKIP_PATH_RE.search(_urlsplit(item['url']).path.lower()):
            continue
        # A general-wire item matching no macro vocabulary is not market news.
        # AP and BBC World carry sport, box office and human interest in the same
        # feed as the geopolitics we want, and neither exposes a filterable path
        # (AP is /article/<slug>-<hash>, BBC is /news/articles/<opaque-id>), so
        # the path rule above only reaches Guardian-style URLs. Without this gate
        # the feed was 58/80 cycling results and MLB trades. Financial and policy
        # feeds are exempt: everything they publish is market news by definition.
        if item['group'] == 'broad':
            hay = (item['title'] + ' ' + item.get('summary', '')).lower()
            if not (_MARKET_KW_HIGH.search(hay) or _MARKET_KW_MED.search(hay)):
                continue
        if item['pub_dt'] is None:
            item['hours_old'] = None
        else:
            item['hours_old'] = max(0.0, (now - item['pub_dt']).total_seconds() / 3600)
            if item['hours_old'] > MARKET_MAX_AGE_H:
                continue
        item['score'] = _impact_score(item, now)
        items.append(item)

    items = _dedupe_news(items)

    # Thinness is a statement about the FEEDS, so it is measured before the
    # relevance filter — an LLM legitimately dropping half the page is not an
    # upstream failure and must not be treated as one.
    if ok < 3 or len(items) < 10:
        raise RuntimeError(f'market news too thin: {ok} feeds ok, {len(items)} items')

    items, filter_mode = _filter_relevant(items, backend)

    for item in items:
        item['score'] += min(8 * len(item['also']), 24)
        hours = item.get('hours_old')
        item['breaking'] = bool(
            hours is not None and hours < 3
            and (len(item['also']) >= 2 or item['tier'] == 1
                 or _MARKET_KW_HIGH.search(item['title'].lower()))
        )
        item['category'] = _categorise(item)
    items.sort(key=lambda i: i['score'], reverse=True)

    payload = []
    for i, item in enumerate(items[:MARKET_NEWS_LIMIT]):
        payload.append({
            'title':    item['title'],
            'url':      item['url'],
            'source':   item['source'],
            'category': item['category'],
            'tier':     item['tier'],
            'summary':  item['summary'][:200],
            'breaking': item['breaking'],
            'top':      i < 3,
            'also':     item['also'],
            'pub_ts':   item['pub_dt'].isoformat() if item['pub_dt'] else '',
            'date':     item['pub_dt'].strftime('%b %d, %Y') if item['pub_dt'] else '',
        })

    categories = {}
    for p in payload:
        categories[p['category']] = categories.get(p['category'], 0) + 1

    result = {'items': payload, 'categories': categories, 'sources_ok': ok,
              'sources_failed': failed, 'filter': filter_mode,
              'ts': _time.time(), 'stale': False}
    _market_last_good.update(result)
    return result


_market_news_cache = _TtlCache(MARKET_NEWS_TTL)
# Last successful build, so a total upstream failure serves something rather than
# an empty page. Only ever written by a build that passed the thinness check.
_market_last_good  = {'items': [], 'categories': {}, 'sources_ok': 0,
                      'sources_failed': [], 'filter': 'none',
                      'ts': 0, 'stale': True}


def _build_market_news(backend=None):
    """Never raises. Falls back to the last good payload, flagged stale.

    The payload is market-wide, so the cache stays shared — this is nobody's
    private data and rebuilding it per account would multiply the upstream work
    by the number of users for an identical answer. What is *not* shared is the
    key that pays for the relevance pass: whoever triggers a rebuild spends
    their own. An account with no key still triggers a rebuild, it just lands on
    the regex filter.
    """
    try:
        return _market_news_cache.get(
            'market', lambda k: _fetch_market_news(k, backend))
    except Exception as e:
        print(f'[MKTNEWS] build failed: {e}', flush=True)
        return {**_market_last_good, 'stale': True}


@app.route('/api/news/market', methods=['GET'])
def get_market_news():
    """Breaking news that could move markets. Takes no ticker and no user input.

    Returns every category in one payload so the frontend chips filter in memory
    — switching chips costs no requests, which is the same reasoning that pushed
    the ticker tape onto a single batched call.
    """
    data = _build_market_news(_llm_backend())
    try:
        limit = min(int(request.args.get('limit', MARKET_NEWS_LIMIT)), MARKET_NEWS_LIMIT)
    except (TypeError, ValueError):
        limit = MARKET_NEWS_LIMIT
    return jsonify({**data, 'items': data['items'][:limit]})


_positions_news_cache = _TtlCache(600)


def _portfolio_symbols():
    """Every symbol the user tracks: holdings + watchlist, deduped, validated.

    The write routes validate now, so the stored files should already be clean —
    this filter stays because it is not the only thing that can write them. The
    files sit in the working directory and are edited by hand; a row that
    predates the write-side fix, or one pasted in, is untrusted input like any
    other. Dropping a bad symbol here costs one comprehension.
    """
    raw = [h.get('ticker', '') for h in load_holdings()] + \
          [w.get('ticker', '') for w in load_watchlist()]
    clean = [clean_ticker(t) for t in dict.fromkeys(raw) if t]
    # A hidden holding still counts in every money figure — that arithmetic is
    # not a discovery surface — but a feed of headlines about it is exactly
    # what a block is for.
    hidden = _blocked_set()
    return [t for t in dict.fromkeys(clean) if t and t not in hidden]


@app.route('/api/news/positions', methods=['GET'])
def get_positions_news():
    """News for the user's own holdings and watchlist, as one batched call.

    Takes no symbols from the query string — the server decides what the
    portfolio is. One request for ~9 symbols rather than nine, for the same
    reason the ticker tape batches its quotes.
    """
    from concurrent.futures import ThreadPoolExecutor

    symbols = _portfolio_symbols()
    if not symbols:
        return jsonify({'items': [], 'symbols': []})

    def one(sym):
        try:
            # lite=True returns before the Groq rewrite, so no key is needed —
            # that exit is why this feed costs one yfinance call per symbol
            # instead of ten scrapes and an LLM call per item.
            return sym, _positions_news_cache.get(
                sym, lambda s: _build_news(s, lite=True))
        except Exception:
            return sym, []

    items = []
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for sym, rows in pool.map(one, symbols):
                for r in rows:
                    r = dict(r)
                    r['tickers']  = [sym]
                    r['category'] = 'positions'
                    r['breaking'] = False
                    r['also']     = []
                    r['tier']     = 3
                    r['summary']  = ''
                    items.append(r)
    except Exception as e:
        print(f'[POSNEWS] {e}', flush=True)

    # The same story can surface under two holdings; keep one copy per URL.
    merged = {}
    for item in items:
        key = _canon_url(item['url'])
        if key in merged:
            for t in item['tickers']:
                if t not in merged[key]['tickers']:
                    merged[key]['tickers'].append(t)
        else:
            merged[key] = item

    out = sorted(merged.values(), key=lambda i: i.get('pub_ts') or '', reverse=True)
    return jsonify({'items': out, 'symbols': symbols})


# ── Index browser ─────────────────────────────────────────────────────────────
#
# An index is two independent facts: who is in it, and what those names are
# doing today. They move on completely different clocks — membership changes a
# handful of times a year, quotes change by the minute — so they are fetched,
# cached and *failed* separately. One combined fetch would either re-scrape a
# constituent list every two minutes or serve yesterday's prices, and a
# Wikipedia outage would take the prices down with it.

_INDEX_SOURCES = {
    'sp500': {
        'label':  'S&P 500',
        'url':    'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
        'symbol': 'Symbol',
        'name':   'Security',
        'suffix': '',
        'min':    400,
    },
    'ndx': {
        'label':  'Nasdaq-100',
        # Not /wiki/Nasdaq-100 — the components table lives on its own page,
        # and the index article keeps nothing but a navbox link to it.
        'url':    'https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies',
        'symbol': 'Ticker',
        'name':   'Company',
        'suffix': '',
        'min':    80,
    },
    'dow': {
        'label':  'Dow 30',
        'url':    'https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average',
        'symbol': 'Symbol',
        'name':   'Company',
        'suffix': '',
        'min':    25,
    },
    'tsx60': {
        'label':  'S&P/TSX 60',
        'url':    'https://en.wikipedia.org/wiki/S%26P/TSX_60',
        'symbol': 'Symbol',
        'name':   'Company',
        # Wikipedia lists the bare TSX symbol; Yahoo wants the venue on it.
        'suffix': '.TO',
        'min':    50,
    },
}

# An index is a curated list of a few hundred names; an exchange is everything
# that trades on a venue. They differ in exactly one place — where the membership
# comes from — so everything downstream is shared: the daily membership cache,
# the batch quote fetch, the row builder, the market-cap fill, the payload cache
# and the route. Adding a venue is a table entry, not a second code path.
#
# The listing files below are the canonical free sources and are republished
# daily. Nasdaq Trader's SymDir covers every US venue in two pipe-delimited
# files; TMX's own company directory covers Toronto. Both move on the same slow
# clock as index membership, which is why they share its cache and its
# raise-rather-than-return discipline.
#
# NYSE Arca and Cboe BZX are deliberately absent. They are ETF venues: of 2,697
# Arca listings 15 are common stock, and of 1,578 BZX listings 4 are. A tab for
# either would be a filter box over an empty table.
_SYMDIR_URLS = {
    'nasdaq': 'https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt',
    'other':  'https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt',
}
_TMX_DIRECTORY = 'https://www.tsx.com/json/company-directory/search/{board}/%5E*'

# `min` is the same gate _INDEX_SOURCES uses, sized well under the real count
# (measured 2026-08-10: nasdaq 3402, nyse 2184, amex 268, tsx 2266, tsxv 1431).
# It is what tells a parser that broke from a venue that shrank.
_EXCHANGE_SOURCES = {
    'nasdaq': {'label': 'Nasdaq',        'file': 'nasdaq',               'min': 2000},
    'nyse':   {'label': 'NYSE',          'file': 'other', 'venue': 'N',  'min': 1200},
    'amex':   {'label': 'NYSE American', 'file': 'other', 'venue': 'A',  'min':  120},
    'tsx':    {'label': 'TSX',           'board': 'tsx',  'suffix': '.TO', 'min': 1000},
    'tsxv':   {'label': 'TSX Venture',   'board': 'tsxv', 'suffix': '.V',  'min':  600},
}

# The one registry both halves are looked up in. Keys must not collide — an
# index and an exchange answering to the same name would make the route's
# dispatch depend on dict ordering — and a test asserts they don't.
_UNIVERSES = {
    **{k: {'label': v['label'], 'kind': 'index'}
       for k, v in _INDEX_SOURCES.items()},
    **{k: {'label': v['label'], 'kind': 'exchange'}
       for k, v in _EXCHANGE_SOURCES.items()},
}

INDEX_MEMBERS_TTL = 24 * 3600   # membership changes a few times a year
INDEX_QUOTES_TTL  = 120         # one trading minute, near enough

# Membership is persisted, not just memoised, because the failure it guards
# against outlives the process. A shared store rather than a per-user one for
# the same reason the market-news payload is shared: it is public, market-wide
# data that is identical for every account, and rebuilding it per user would
# multiply the upstream work for a byte-identical answer.
_index_members_store = JsonStore('index_constituents.json', dict)
_index_members_lock  = threading.RLock()


def _index_symbol(raw, suffix):
    """A Wikipedia symbol in Yahoo's spelling, or None if it isn't one.

    A share class is a dot on the exchange and a hyphen at Yahoo — BRK.B is
    BRK-B, TECK.B is TECK-B.TO. The movers table already does this conversion
    in the other direction for display, and `_index_row` reverses it back.

    Runs through clean_ticker() like every other boundary here. This is scraped
    third-party text: a footnote marker, a merged header row or an em dash for
    a pending addition all arrive looking like a symbol, and this one reaches a
    URL query.
    """
    text = str(raw).replace('\xa0', ' ').strip().upper()
    text = text.split('[')[0].strip()      # 'BRK.B[a]' — Wikipedia footnotes
    if not text or text == 'NAN':
        return None
    return clean_ticker(text.replace('.', '-') + suffix)


def _scrape_index_members(key):
    """Constituents of one index from Wikipedia, or raise.

    Raises rather than returning [] on a thin parse, the same distinction
    `_fetch_div_events` keeps: the caller cannot tell "this index is empty"
    from "the table moved", and only one of those may be allowed to overwrite a
    good stored list. Every one of these pages carries other tables with the
    same column names — the Dow article alone has thirty-odd — so the row count
    is what picks the components table out, not the column names alone.
    """
    import io
    import requests as _req

    src = _INDEX_SOURCES[key]
    resp = _req.get(src['url'], headers={'User-Agent': _MT_UA}, timeout=15)
    resp.raise_for_status()

    # keep_default_na=False is load-bearing, not tidiness. pandas treats 'NA'
    # as a missing value by default, and NA is National Bank of Canada's
    # symbol — so the TSX 60 came back with 59 members and no error anywhere,
    # the bank simply absent from the table. 'NULL', 'NaN' and 'None' are on
    # the same default list and are all plausible symbols. An empty cell then
    # arrives as '' rather than NaN, which _index_symbol already refuses.
    for frame in pd.read_html(io.StringIO(resp.text), keep_default_na=False):
        cols = [str(c) for c in frame.columns]
        if src['symbol'] not in cols or src['name'] not in cols:
            continue
        rows, seen = [], set()
        for sym, name in zip(frame[src['symbol']], frame[src['name']]):
            full = _index_symbol(sym, src['suffix'])
            if not full or full in seen:
                continue
            seen.add(full)
            rows.append({'full_ticker': full, 'name': str(name).strip()})
        if len(rows) >= src['min']:
            return rows

    raise RuntimeError(
        f"no table at {src['url']} with >= {src['min']} usable rows under "
        f"columns {src['symbol']!r}/{src['name']!r}")


# A listing file names every *security* on a venue, not every stock: rights,
# units, warrants, preferreds and baby bonds each trade under their own symbol
# and arrive in the same column as the common shares. They are excluded here
# rather than left in, because none of them has a P/E or a market cap — a row
# for one is a row of dashes that still takes a line of the table and a slot in
# every sort. On Nasdaq alone that is 918 of 5,577 symbols.
#
# Two cases the word list alone gets wrong, both measured:
#
#   'American Depositary Shares' has to survive the bare 'Depositary Shares'
#   rule that marks a preferred. An ADR *is* the common equity of a foreign
#   issuer, and 168 Nasdaq listings are spelled exactly that way.
#
#   A coupon in the name — 'Aegon Funding Company LLC 5.10% Subordinated Notes'
#   — is the tell for a baby bond. Some are spelled without any of the words
#   below, so the percentage is what catches them.
_NONCOMMON_RE = _re_mod.compile(
    r'\b(warrants?|rights?|units?|debentures?|notes?|bonds?|preferred|'
    r'preference|pfd|convertible|subordinated|when[-\s]issued|'
    r'contingent\s+value|liquidating\s+trust|'
    r'depositary\s+shares?|depository\s+shares?)\b', _re_mod.I)
_ADR_RE    = _re_mod.compile(r'\bamerican\s+depositar', _re_mod.I)
_COUPON_RE = _re_mod.compile(r'\d\s*%')

# TMX names a Canadian Depositary Receipt outright — 'Nvidia CDR (CAD Hedged)'.
# That is the only marker there is, and it is a reliable one.
_CDR_RE = _re_mod.compile(r'\bCDR\b|canadian\s+depositar', _re_mod.I)


def _is_common_stock(name):
    """Whether a listing-file security name describes common equity."""
    text = str(name or '')
    if _ADR_RE.search(text):
        return True
    return not (_COUPON_RE.search(text) or _NONCOMMON_RE.search(text))


def _symdir_display_name(raw):
    """'Apple Inc. - Common Stock' -> 'Apple Inc.'

    Only ever a fallback: `_index_row` prefers the quote's own longName and
    reaches for this when Yahoo sends neither name.
    """
    text = str(raw or '').strip()
    return text.split(' - ')[0].strip() or text


def _symdir_rows(which):
    """Every row of one Nasdaq Trader listing file, keyed by its own header.

    The files are pipe-delimited with a header line and a 'File Creation Time'
    footer. The footer is dropped here — left in, it parses as a security whose
    symbol is that literal text, which `_index_symbol` would then refuse one
    layer too late to be readable.
    """
    import requests as _req

    resp = _req.get(_SYMDIR_URLS[which], headers={'User-Agent': _MT_UA}, timeout=20)
    resp.raise_for_status()
    lines = [ln for ln in resp.text.splitlines()
             if ln.strip() and not ln.startswith('File Creation Time')]
    if len(lines) < 2:
        raise RuntimeError(f'{which} listing file came back with no rows')
    header = lines[0].split('|')
    return [dict(zip(header, ln.split('|'))) for ln in lines[1:]]


def _scrape_symdir_members(src):
    """Common stock on one US venue."""
    rows, seen = [], set()
    for raw in _symdir_rows(src['file']):
        # nasdaqlisted.txt calls the column 'Symbol'. otherlisted.txt calls it
        # 'ACT Symbol' and adds 'Exchange', because one file carries five venues
        # (N NYSE, A NYSE American, P Arca, Z Cboe BZX, V IEX).
        if src.get('venue') and raw.get('Exchange') != src['venue']:
            continue
        # A test issue is a symbol the venue reserves for its own systems
        # checks. It is not a security and Yahoo does not price it.
        if raw.get('Test Issue') == 'Y' or raw.get('ETF') == 'Y':
            continue
        if not _is_common_stock(raw.get('Security Name')):
            continue
        full = _index_symbol(raw.get('Symbol') or raw.get('ACT Symbol') or '', '')
        if not full or full in seen:
            continue
        seen.add(full)
        rows.append({'full_ticker': full,
                     'name': _symdir_display_name(raw.get('Security Name'))})
    return rows


def _scrape_tmx_members(src):
    """Every issuer on one TMX board, from its own company directory.

    Only the company-level symbol is taken, never the `instruments` array under
    it. That array carries an issuer's other series — the USD-denominated class
    of a fund (BTCQ and BTCQ.U), separate unit classes — which are the same
    company twice in a table that is one row per company. Measured: 2,266 TSX
    companies expand to 2,999 instruments, and the extra 733 are duplicates of
    names already in the list.

    There is no security-type field here, so the common-stock filter above
    cannot run on this source. `quoteType` does that job instead, downstream and
    for every venue at once — see `_build_index`.

    What the directory does name outright is a depositary receipt, and 133 of
    the 2,266 TSX entries are one. They are flagged rather than dropped; see
    `_index_row` for what the flag suppresses and why the row stays.
    """
    import requests as _req

    resp = _req.get(_TMX_DIRECTORY.format(board=src['board']),
                    headers={'User-Agent': _MT_UA}, timeout=20)
    resp.raise_for_status()
    rows, seen = [], set()
    for company in ((resp.json() or {}).get('results') or []):
        full = _index_symbol(company.get('symbol'), src['suffix'])
        if not full or full in seen:
            continue
        seen.add(full)
        name = str(company.get('name') or '').strip()
        row  = {'full_ticker': full, 'name': name}
        if _CDR_RE.search(name):
            row['dr'] = True
        rows.append(row)
    return rows


def _scrape_exchange_members(key):
    """Every listing on one exchange, or raise.

    Raises on a thin parse for the same reason `_scrape_index_members` does: a
    venue that comes back with forty names is a parser that broke, not a venue
    that delisted three thousand companies — and only one of those may be
    allowed to overwrite a good stored list.
    """
    src  = _EXCHANGE_SOURCES[key]
    rows = (_scrape_tmx_members(src) if src.get('board')
            else _scrape_symdir_members(src))
    if len(rows) < src['min']:
        raise RuntimeError(
            f'{key}: listing source returned {len(rows)} usable rows, '
            f'expected at least {src["min"]}')
    return rows


def _index_members(key):
    """Constituents, refreshed at most daily and persisted across restarts.

    A scrape that fails or comes back thin leaves the stored list exactly where
    it was, so a Wikipedia redesign degrades to a slightly stale membership
    list instead of an empty page — and it degrades that way after a restart
    too, which is the whole reason this is a file and not a dict. A failure is
    logged rather than swallowed, because a silently stale index looks exactly
    like a working one.

    Serves both kinds of universe. An exchange's listing file is a different
    source with the same properties — public, market-wide, republished daily,
    and worse to lose than to serve a day stale — so it gets the same cache
    rather than a parallel one.
    """
    with _index_members_lock:
        cached = (_index_members_store.load() or {}).get(key) or {}
        rows   = cached.get('rows')
        if rows and _time_mod.time() - (cached.get('ts') or 0) < INDEX_MEMBERS_TTL:
            return rows

        try:
            # Resolved through the module globals rather than a callable stored
            # in _UNIVERSES, so that patching either scraper — which the tests
            # covering the degrade-to-stale path do — still takes effect.
            fresh = (_scrape_exchange_members(key)
                     if _UNIVERSES.get(key, {}).get('kind') == 'exchange'
                     else _scrape_index_members(key))
        except Exception as e:
            print(f'[INDEX] {key}: membership refresh failed, keeping '
                  f'{len(rows or [])} stored: {type(e).__name__}: {e}', flush=True)
            if rows:
                return rows
            raise

        _index_members_store.mutate(
            lambda data: {**data, key: {'rows': fresh, 'ts': _time_mod.time()}})
        return fresh


# Symbols per Yahoo quote call. 100 keeps the URL far inside any length limit
# while putting the whole S&P 500 in five requests.
_INDEX_QUOTE_CHUNK = 100


def _index_quotes(symbols):
    """{symbol: quote} from Yahoo's batch quote endpoint.

    This is the endpoint the screener behind /api/movers already reads, and it
    carries price, daily change, the 52-week range, trailing P/E and market cap
    in one response — so a 500-name index costs five requests rather than five
    hundred `.info` lookups. Measured: 503 symbols in 0.37s.

    It goes through yfinance's YfData because the endpoint requires a crumb and
    cookie pair that yfinance already negotiates, caches and refreshes on
    expiry. Re-implementing that here would be a second copy of precisely the
    part most likely to break.

    A chunk that fails is skipped, not raised on: the caller renders the names
    it did get, the same way /api/quotes leaves a failed symbol absent rather
    than blanking its row. Symbols are clean_ticker()'d upstream, so joining
    them into the query needs no escaping.
    """
    from concurrent.futures import ThreadPoolExecutor
    from yfinance.data import YfData

    fetcher = YfData()
    chunks  = [symbols[i:i + _INDEX_QUOTE_CHUNK]
               for i in range(0, len(symbols), _INDEX_QUOTE_CHUNK)]
    if not chunks:
        return {}

    def grab(chunk):
        try:
            resp = fetcher.cache_get(
                'https://query2.finance.yahoo.com/v7/finance/quote?symbols='
                + ','.join(chunk))
            return (resp.json().get('quoteResponse') or {}).get('result') or []
        except Exception as e:
            print(f'[INDEX] quote batch of {len(chunk)} failed: '
                  f'{type(e).__name__}: {e}', flush=True)
            return []

    out = {}
    with ThreadPoolExecutor(max_workers=min(6, len(chunks))) as ex:
        for result in ex.map(grab, chunks):
            for quote in result:
                if quote.get('symbol'):
                    out[quote['symbol']] = quote
    return out


# Yahoo's quote endpoint omits marketCap outright for a stable minority of large
# listings — Home Depot, Exxon, Lowe's, McDonald's, Merck and Salesforce among
# them, 26 of the S&P 500 and 4 of the Dow. It omits sharesOutstanding with it,
# so there is nothing in the response to derive it from, and naming the field
# explicitly does not bring it back. Not flaky: the same symbols come back empty
# on every request, alone or in a batch.
#
# It matters more than the other gaps because market cap is this table's default
# sort. Left null those names sort below every company in the index, so the
# first thing anyone sees is Home Depot beneath a $5B utility — which reads as a
# broken table rather than as one missing figure. At exchange scale the same gap
# is 256 of 2,180 NYSE equities, and the largest of them by traded value are
# XOM, CRM, MCD, B, HD, SE, TGT, MDT and MRK.
#
# What is cached to fill it is the *share count*, not the cap. fast_info carries
# both, but a cap held for a day is a day-stale number in the column the table
# sorts by, while shares outstanding move on the slow clock membership does. So
# the count is stored and multiplied by the current price on every build, which
# is the definition of market cap and keeps the figure live between fetches.
# Verified against Yahoo's own marketCap on the 116 sampled names that report
# both: price x shares reproduces it to a ratio of 1.0000.
#
# That distinction is what makes this affordable at all. The fetch is one
# request per symbol — ~24ms at 16 threads, so ~3s for a cold NYSE — and without
# the cache every 120-second payload rebuild would pay it again.
SHARE_COUNT_TTL         = 24 * 3600
_INDEX_CAP_BACKFILL_MAX = 150

# Shared, not per-user: a share count is public market data identical for every
# account, the same reasoning as index_constituents.json and the market-news
# payload. It must not be registered with _register_user_file().
_share_counts_store = JsonStore('share_counts.json', dict)

# Symbols are only ever added here, so without a prune the file grows for the
# life of the install — roughly 8,000 entries across the five venues, plus every
# symbol that has ever been delisted. A week is comfortably longer than the TTL,
# so pruning only ever drops entries that would be refetched anyway.
_SHARE_COUNT_KEEP = 7 * 24 * 3600


def _prune_share_counts(data, now):
    return {sym: entry for sym, entry in (data or {}).items()
            if isinstance(entry, dict)
            and (entry.get('ts') or 0) > now - _SHARE_COUNT_KEEP}


def _index_backfill_caps(symbols, quotes):
    """{symbol: market cap} for the symbols the quote endpoint priced but capped
    at nothing, as a cached share count times the live price.

    Only ever asked for the symbols actually missing one, so a total quote
    failure cannot turn into one request per member behind it.
    """
    from concurrent.futures import ThreadPoolExecutor

    def price_of(sym):
        return _finite(quotes.get(sym, {}).get('regularMarketPrice'))

    now    = _time_mod.time()
    stored = _share_counts_store.load() or {}
    caps, wanted = {}, []
    for sym in symbols:
        entry  = stored.get(sym) or {}
        shares = _finite(entry.get('shares'))
        if shares and now - (entry.get('ts') or 0) < SHARE_COUNT_TTL:
            price = price_of(sym)
            if price:
                caps[sym] = price * shares
        else:
            wanted.append(sym)
    if not wanted:
        return caps

    # Ordered by traded value so a bounded run spends itself on the names a
    # reader would notice missing. Symbols already cached above are not in this
    # list, so successive builds walk further down the tail instead of refetching
    # the same head — the whole venue fills in over a few refreshes.
    wanted.sort(key=lambda s: -((price_of(s) or 0)
                                * (_finite(quotes.get(s, {}).get('regularMarketVolume')) or 0)))
    if len(wanted) > _INDEX_CAP_BACKFILL_MAX:
        # Logged rather than silently truncated: a sudden jump here means Yahoo
        # dropped the field wholesale, and a quiet cap would hide that behind a
        # column of dashes.
        print(f'[INDEX] {len(wanted)} symbols missing marketCap, backfilling '
              f'the {_INDEX_CAP_BACKFILL_MAX} largest by traded value', flush=True)
        wanted = wanted[:_INDEX_CAP_BACKFILL_MAX]

    def shares_of(sym):
        try:
            return _finite(getattr(yf.Ticker(sym).fast_info, 'shares', None))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=min(16, len(wanted))) as ex:
        found = {s: n for s, n in zip(wanted, ex.map(shares_of, wanted)) if n}

    if found:
        stamped = {s: {'shares': n, 'ts': now} for s, n in found.items()}
        _share_counts_store.mutate(
            lambda data: _prune_share_counts({**data, **stamped}, now))
    for sym, shares in found.items():
        price = price_of(sym)
        if price:
            caps[sym] = price * shares
    return caps


def _index_row(member, quote):
    """One priced table row, or None if Yahoo would not price it.

    Every figure is None when it is not reported rather than 0. A loss-making
    company has no trailing P/E, and a 0.0 in that column reads as a real and
    extraordinarily cheap valuation — the same call `_build_ownership` makes
    about a missing institutional holding.
    """
    price = _finite(quote.get('regularMarketPrice'))
    if price is None:
        return None

    full = member['full_ticker']
    low  = _finite(quote.get('fiftyTwoWeekLow'))
    high = _finite(quote.get('fiftyTwoWeekHigh'))

    # Where the price sits in its own year: 0 at the low, 1 at the high. A
    # degenerate range — a listing younger than a year, or a halted name —
    # divides by zero, so it gets no position rather than a fabricated 0 that
    # would draw the marker hard against the left edge.
    pos = None
    if low is not None and high is not None and high > low:
        pos = max(0.0, min(1.0, (price - low) / (high - low)))

    # A depositary receipt is a bank-created wrapper around a share listed
    # somewhere else, and Yahoo attaches the *underlying company's* market cap
    # to it: NVDA.TO comes back at C$6.81T, which is NVIDIA, against a CDR
    # program worth a few hundred million. Left in, the six largest companies on
    # the Toronto exchange are Nvidia, Apple, Alphabet, Microsoft, Amazon and
    # Meta, and Royal Bank is not on the first screen — cap is the default sort.
    #
    # Suppressed rather than converted, the same call the cross-currency ratios
    # make: the figure for the program itself appears nowhere in the payload, so
    # there is nothing to put here instead. The row itself stays, because a
    # CAD-hedged NVDA is a real thing to buy in Toronto and its price, day change
    # and 52-week range are all its own. It sorts last on cap, where every other
    # unreported figure already goes.
    cap      = None if member.get('dr') else _finite(quote.get('marketCap'))
    currency = quote.get('currency') or ''
    return {
        # Yahoo's spelling back to the exchange's, as the movers table shows it.
        'ticker':      full.replace('.TO', '').replace('-', '.'),
        'full_ticker': full,
        # The venue's own name wins for a receipt. Yahoo calls NVDA.TO 'NVIDIA
        # Corporation', which in a list of Toronto listings reads as NVIDIA
        # being listed in Toronto; TMX calls it 'Nvidia CDR (CAD Hedged)'.
        'name':        (member.get('name') if member.get('dr') else None)
                       or quote.get('longName') or quote.get('shortName')
                       or member.get('name') or full,
        'price':       price,
        'change':      _finite(quote.get('regularMarketChangePercent')),
        'week52_low':  low,
        'week52_high': high,
        'week52_pos':  pos,
        'pe':          _finite(quote.get('trailingPE')),
        # Both, for the reason charts read `raw`: the table sorts on this
        # column, and parsing '$3.45T' back into a number to do it would
        # quantise every mega-cap to the same three digits.
        'mkt_cap':     format_large_number(cap, _currency_symbol(currency)),
        'mkt_cap_raw': cap,
        'volume':      quote.get('regularMarketVolume'),
        'currency':    currency,
        'cur_symbol':  _currency_symbol(currency),
    }


def _build_index(key):
    """The whole universe — an index or an exchange — priced, as one payload."""
    kind    = _UNIVERSES[key]['kind']
    members = _index_members(key)
    quotes  = _index_quotes([m['full_ticker'] for m in members])

    # An exchange's listing file has no security-type column worth trusting for
    # this — TMX has none at all — so the type comes from the quote, which knows
    # it for every venue at once. Without it the TSX tab is 1,528 ETFs over 714
    # companies, and 70% of the table has no market cap because a fund has no
    # such figure. Applied to exchanges only: an index constituent is an equity
    # by construction, and a missing quoteType there should not drop a member.
    funds = 0
    if kind == 'exchange':
        for sym, quote in list(quotes.items()):
            if quote.get('regularMarketPrice') is None:
                continue
            if quote.get('quoteType') != 'EQUITY':
                del quotes[sym]
                funds += 1

    # Only the priced-but-uncapped symbols, so a total quote failure does not
    # turn into 500 fast_info requests behind it.
    # A depositary receipt is skipped here too: its cap is deliberately null (see
    # _index_row), so asking fast_info for one would spend a request to produce a
    # number the row then throws away.
    uncapped = [m['full_ticker'] for m in members
                if not m.get('dr')
                and quotes.get(m['full_ticker'], {}).get('regularMarketPrice')
                and not quotes[m['full_ticker']].get('marketCap')]
    for sym, cap in _index_backfill_caps(uncapped, quotes).items():
        quotes[sym]['marketCap'] = cap

    rows = [r for r in (_index_row(m, quotes.get(m['full_ticker'], {}))
                        for m in members) if r]
    if not rows:
        raise RuntimeError(f'no rows priced out of {len(members)} members')
    return {
        'key':     key,
        'kind':    kind,
        'label':   _UNIVERSES[key]['label'],
        'rows':    rows,
        'count':   len(rows),
        # Reported rather than hidden: an index Yahoo prices 498 of 503 names
        # in is a fact about the data, and a bare "503 stocks" over a 498-row
        # table is the kind of quiet mismatch nothing else here would catch.
        # `funds` is counted apart from that so the page can say why a 2,266-name
        # venue draws 714 rows — those are excluded, not missing.
        'members': len(members),
        'funds':   funds,
    }


_index_payload_cache = _TtlCache(INDEX_QUOTES_TTL)


@app.route('/api/listing/<key>', methods=['GET'])
@app.route('/api/index/<key>', methods=['GET'])
def market_index(key):
    """Every constituent of one index or exchange, priced, in a single response.

    The whole universe ships at once — 503 rows is ~90KB, a 3,402-name Nasdaq
    is ~876KB and gzips to ~171KB — because the sorting and filtering this page
    exists for are then instant and cost no further requests. Same call the
    market-news feed makes in shipping every category together so the chips
    filter in memory. Paging it server-side would put a request on every sort,
    and a sort that only orders the page you can see is not a sort.

    Two URLs, one implementation: /api/index/<key> is what the four index keys
    were published under and still answer on, /api/listing/<key> is the honest
    spelling for a venue. One endpoint name, so there is still one rate-limit
    bucket and one entry in _RATE_LIMITS.
    """
    key = (key or '').strip().lower()
    if key not in _UNIVERSES:
        return jsonify({'error': 'Unknown index'}), 404
    try:
        data = _index_payload_cache.get(key, _build_index)
        # One built payload, filtered per reader — the same split the movers and
        # tape caches make, and for the same reason: an index is public market
        # data and rebuilding it per account would multiply the upstream work
        # for an identical answer.
        #
        # `members` comes down with it because the frontend derives "unpriced"
        # as members − funds − rows. Leave it at the universe's size and every
        # hidden name is reported as a listing Yahoo would not price, which is a
        # different and untrue statement about the data.
        rows = _drop_blocked(data['rows'])
        hidden = len(data['rows']) - len(rows)
        return jsonify({**data, 'rows': rows, 'count': len(rows),
                        'members': data['members'] - hidden, 'hidden': hidden})
    except Exception as e:
        print(f'[INDEX] {key}: build failed: {type(e).__name__}: {e}', flush=True)
        return jsonify({'error': 'Could not load this index right now.'}), 502


def _legacy_data_files():
    """Per-user files still sitting loose in the data directory, pre-accounts."""
    return [name for name in _PER_USER_FILES
            if os.path.exists(os.path.join(_DATA_DIR, name))]


def _claim_legacy_data(username):
    """Move the pre-accounts portfolio into `username`'s directory.

    Everything predates accounts, so it belongs to whoever the first account is —
    that is the only answer that does not silently discard a real portfolio.
    Moved rather than copied, so a second account created later cannot find the
    originals and inherit them too, and skipped per file if the destination
    already has one (this must be safe to run twice).
    """
    moved = []
    for name in _legacy_data_files():
        src = os.path.join(_DATA_DIR, name)
        dst = os.path.join(_user_data_dir(username), name)
        if os.path.exists(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _shutil.move(src, dst)
        moved.append(name)
    return moved


def _backfill_transaction_gains(owner):
    """Backfill gain_loss from sales.json into any sell transactions missing it, then save permanently."""
    txns  = load_transactions(owner=owner)
    sales = load_sales(owner=owner)
    sales_lookup = {}
    for s in sales:
        key = (s.get('ticker',''), s.get('sale_date',''), round(float(s.get('shares_sold', 0)), 4))
        sales_lookup[key] = s.get('gain_loss')
    changed = False
    for t in txns:
        if t.get('type') == 'sell' and t.get('gain_loss') is None:
            key = (t.get('ticker',''), t.get('date',''), round(float(t.get('shares', 0)), 4))
            gl = sales_lookup.get(key)
            if gl is not None:
                t['gain_loss'] = gl
                changed = True
    if changed:
        save_transactions(txns, owner=owner)


# --- the guest's demo portfolio ---------------------------------------------
#
# A reviewer who signs in as guest and finds an empty Holdings tab has seen the
# login form and nothing else, so the guest directory is seeded with a small
# illustrative ledger on real listings. Real symbols, so every quote, dividend
# and chart resolves live; invented trades, because the account holder's own
# portfolio is theirs. Only transactions.json and watchlist.json are written —
# holdings and sales are derived, here as everywhere, by the two rebuilds.
#
# Canadian listings for the book, because the portfolio formatters print the
# account's own money as `$` and a CAD book keeps every total in one currency.
# US names on the watchlist, because that is where the forty-year Macrotrends
# charts are — a `.TO` symbol skips the scrape.

_GUEST_LEDGER = [
    # (date, type, ticker, name, shares, price)
    ('2025-09-15', 'buy',  'T.TO',    'TELUS Corporation',               120, 21.80),
    ('2025-09-22', 'buy',  'BCE.TO',  'BCE Inc.',                         50, 32.40),
    ('2025-10-01', 'buy',  'XEQT.TO', 'iShares Core Equity ETF Portfolio', 80, 36.90),
    ('2025-10-06', 'buy',  'ENB.TO',  'Enbridge Inc.',                    60, 64.25),
    ('2025-11-12', 'buy',  'RY.TO',   'Royal Bank of Canada',             25, 172.40),
    ('2025-11-20', 'buy',  'AAPL.TO', 'Apple CDR (CAD Hedged)',           40, 33.10),
    ('2025-12-02', 'buy',  'CNR.TO',  'Canadian National Railway',        15, 148.60),
    ('2026-01-20', 'buy',  'SHOP.TO', 'Shopify Inc.',                     12, 198.50),
    ('2026-02-10', 'buy',  'MSFT.TO', 'Microsoft CDR (CAD Hedged)',       30, 41.75),
    ('2026-03-04', 'buy',  'RY.TO',   'Royal Bank of Canada',             10, 181.10),
    ('2026-04-14', 'sell', 'BCE.TO',  'BCE Inc.',                         50, 35.10),
]

_GUEST_WATCHLIST = [
    ('AAPL',  'Apple Inc.'),
    ('MSFT',  'Microsoft Corporation'),
    ('NVDA',  'NVIDIA Corporation'),
    ('COST',  'Costco Wholesale Corporation'),
    ('BRK-B', 'Berkshire Hathaway Inc.'),
]


def _guest_txn_id(date, n):
    """An id whose millisecond prefix is the trade date, so the lexicographic
    order `_new_txn_id` promises still matches chronology on seeded rows."""
    ms = int(_datetime.strptime(date, '%Y-%m-%d')
             .replace(tzinfo=_timezone.utc).timestamp() * 1000)
    return f'{ms}-{n:06x}'


def _guest_seed_rows():
    """The ledger exactly as the seed stores it: newest first, like the routes."""
    rows = []
    for n, (date, kind, ticker, name, shares, price) in enumerate(_GUEST_LEDGER):
        rows.append({'id': _guest_txn_id(date, n), 'type': kind, 'ticker': ticker,
                     'name': name, 'shares': float(shares), 'price': float(price),
                     'date': date})
    rows.reverse()
    return rows


def _guest_seed_watchlist():
    return [{'ticker': t, 'name': n, 'added': _GUEST_LEDGER[0][0]}
            for t, n in _GUEST_WATCHLIST]


# What the seed writes. Everything else registered in _PER_USER_FILES — options,
# valuations, blocked, guidance, settings — is absent from a clean guest
# directory, and a reset removes it rather than leaving it to be found later.
_GUEST_SEED_FILES = ('transactions.json', 'watchlist.json',
                     'holdings.json', 'sales.json')


def _guest_stray_paths(owner=GUEST_USERNAME):
    """Files in the guest directory that a clean seed never writes."""
    d = _user_data_dir(owner)
    out = [os.path.join(d, name) for name in _PER_USER_FILES
           if name not in _GUEST_SEED_FILES and os.path.exists(os.path.join(d, name))]
    reports = os.path.join(d, 'reports')
    if os.path.isdir(reports):
        out.append(reports)
    return out


def _guest_portfolio_drifted(owner=GUEST_USERNAME):
    """True when the guest directory is not exactly what the seed produces.

    The gate already makes a guest write impossible, so on a healthy server this
    answers False on every login and costs four small reads. It exists for the
    cases the gate cannot see — a file edited by hand on the box, a corrupt
    file, or a mutating route that some future change lets through — so that
    the next visitor still lands on the demo book and not on whatever the last
    one left. A read that raises counts as drift: a corrupt ledger is the one
    case where reseeding is unambiguously right.
    """
    try:
        if load_transactions(owner=owner) != _guest_seed_rows():
            return True
        if load_watchlist(owner=owner) != _guest_seed_watchlist():
            return True
        bought = {t for _, k, t, *_ in _GUEST_LEDGER if k == 'buy'}
        sold   = {t for _, k, t, *_ in _GUEST_LEDGER if k == 'sell'}
        if {h.get('ticker') for h in load_holdings(owner=owner)} != bought - sold:
            return True
        if len(load_sales(owner=owner)) != len(sold):
            return True
    except Exception:
        return True
    return bool(_guest_stray_paths(owner))


def _seed_guest_portfolio(owner=GUEST_USERNAME, force=False):
    """Write the demo ledger into `owner`'s directory and derive the rest.

    Skipped when the ledger already has rows unless `force`, so enabling guest
    access twice does not reset what is there. With `force` the directory is
    returned to exactly the seed: stray per-user files and any reports are
    removed first, so `guest reset` and the drift check on login both mean the
    same thing. Returns True if it wrote.
    """
    if load_transactions(owner=owner) and not force:
        return False
    if force:
        for path in _guest_stray_paths(owner):
            if os.path.isdir(path):
                _shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
    save_transactions(_guest_seed_rows(), owner=owner)
    save_sales([], owner=owner)          # a reset must not inherit stale sale rows
    save_watchlist(_guest_seed_watchlist(), owner=owner)
    rebuild_holdings_from_transactions(owner=owner)
    rebuild_sales_from_transactions(owner=owner)
    return True


def enable_guest_access():
    """Create the guest account if needed, enable it, seed it if empty.

    Returns (created, seeded). Raises ValueError if the name is taken by a real
    account — a person called 'guest' must not silently become the demo.
    """
    created = {}

    def _apply(users):
        existing = _find_user(GUEST_USERNAME, users)
        if existing is not None and existing.get('role') != GUEST_ROLE:
            raise ValueError(f'"{GUEST_USERNAME}" is an ordinary account; '
                             f'rename or delete it before enabling guest access.')
        if existing is None:
            record = {
                'username':      GUEST_USERNAME,
                'password_hash': None,      # no password: the button is the only door
                'role':          GUEST_ROLE,
                'disabled':      False,
                'created_at':    _now_iso(),
                'last_login':    None,
                'token_version': 1,
            }
            created.update(record)
            return users + [record]
        existing['disabled'] = False
        return users

    _users_store.mutate(_apply)
    seeded = _seed_guest_portfolio()
    return bool(created), seeded


def disable_guest_access():
    """Disable the guest account and end every live guest session. Returns
    False if there is no guest account to disable."""
    found = []

    def _apply(users):
        for u in users:
            if u.get('username') == GUEST_USERNAME and u.get('role') == GUEST_ROLE:
                u['disabled'] = True
                u['token_version'] = int(u.get('token_version', 1)) + 1
                found.append(u)
        return users

    _users_store.mutate(_apply)
    return bool(found)


def _prompt_new_account(role):
    """Interactively create an account. Returns the username, or None if aborted.

    The password is read with getpass and passed straight to create_user(), so it
    is never echoed to the terminal, never lands in argv (where `ps` and the
    shell history would both keep it) and is never written anywhere but the
    scrypt hash in users.json. That is why the first admin is made here and not
    through a first-run web page: a setup page reachable before anyone has signed
    in is public signup with extra steps.
    """
    import getpass

    default = (os.environ.get('USERNAME') or os.environ.get('USER') or '').lower()
    prompt  = f'Username [{default}]: ' if clean_username(default) else 'Username: '
    try:
        username = input(prompt).strip() or default
        name = clean_username(username)
        if not name:
            print('Invalid username: 3-32 characters, letters/digits/._- , '
                  'starting with a letter or digit.')
            return None
        if _find_user(name):
            print(f'"{name}" already exists.')
            return None

        print(f'Password (at least {PASSWORD_MIN_LEN} characters; input is hidden)')
        password = getpass.getpass('Password: ')
        err = check_password_policy(password, name)
        if err:
            print(err)
            return None
        if getpass.getpass('Confirm password: ') != password:
            print('Passwords did not match.')
            return None

        first = not load_users()
        create_user(name, password, role)
    except (KeyboardInterrupt, EOFError):
        print('\nAborted.')
        return None
    except ValueError as e:
        print(str(e))
        return None

    print(f'Created {role} account "{name}".')
    if first:
        moved = _claim_legacy_data(name)
        if moved:
            print(f'Moved the existing portfolio into this account '
                  f'({len(moved)} files): {", ".join(sorted(moved))}')
            print(f'  -> {_user_data_dir(name)}')
    return name


def _cli(argv):
    """Account management from the command line. Returns a process exit code."""
    cmd = argv[0]

    if cmd == 'create-admin':
        return 0 if _prompt_new_account('admin') else 1

    if cmd == 'create-user':
        return 0 if _prompt_new_account('user') else 1

    if cmd == 'list-users':
        users = load_users()
        if not users:
            print('No accounts yet. Run:  python app.py create-admin')
            return 0
        for u in users:
            flags = ' (disabled)' if u.get('disabled') else ''
            print(f'{u.get("username", "?"):<24} {u.get("role", "user"):<6}'
                  f'  last login: {u.get("last_login") or "never"}{flags}')
        return 0

    if cmd == 'passwd':
        # The recovery path. There is no email reset here, so an admin who
        # forgets their password would otherwise have no way back in.
        import getpass
        name = clean_username(argv[1]) if len(argv) > 1 else None
        if not name or not _find_user(name):
            print(f'Usage: python app.py passwd <username>')
            return 1
        try:
            password = getpass.getpass('New password: ')
            err = check_password_policy(password, name)
            if err:
                print(err)
                return 1
            if getpass.getpass('Confirm password: ') != password:
                print('Passwords did not match.')
                return 1
        except (KeyboardInterrupt, EOFError):
            print('\nAborted.')
            return 1

        def _apply(users):
            for u in users:
                if u.get('username') == name:
                    u['password_hash'] = generate_password_hash(password)
                    u['token_version'] = int(u.get('token_version', 1)) + 1
            return users

        _users_store.mutate(_apply)
        print(f'Password updated for "{name}". Other sessions have been signed out.')
        return 0

    if cmd == 'guest':
        # Guest access for reviewers. `enable` is idempotent; `reset` puts the
        # demo portfolio back — visitors cannot change it, but the account
        # holder can edit _GUEST_LEDGER and reseed.
        sub = argv[1] if len(argv) > 1 else 'status'
        if sub == 'enable':
            try:
                created, seeded = enable_guest_access()
            except ValueError as e:
                print(str(e))
                return 1
            print(f'Guest access {"created and " if created else ""}enabled'
                  f'{" with a demo portfolio" if seeded else ""}. The login page '
                  f'now shows a "Continue as guest" button.')
            return 0
        if sub == 'disable':
            if not disable_guest_access():
                print('There is no guest account.')
                return 1
            print('Guest access disabled. Live guest sessions have been signed out.')
            return 0
        if sub == 'reset':
            if _guest_record() is None:
                print('There is no guest account. Run:  python app.py guest enable')
                return 1
            _seed_guest_portfolio(force=True)
            print('Guest demo portfolio reset.')
            return 0
        if sub == 'status':
            g = _guest_record()
            state = ('not created' if g is None
                     else 'disabled' if g.get('disabled') else 'enabled')
            print(f'Guest access: {state}')
            return 0
        print('Usage: python app.py guest [enable|disable|reset|status]')
        return 1

    print(f'Unknown command: {cmd}\n'
          f'Usage: python app.py [create-admin|create-user|list-users|passwd <user>'
          f'|guest enable|disable|reset|status]')
    return 1


if __name__ == '__main__':
    import webbrowser

    if len(sys.argv) > 1:
        sys.exit(_cli(sys.argv[1:]))

    # Refuse to serve with no accounts rather than opening a hole to fix it.
    # With an empty users.json nobody can sign in, so serving anyway would only
    # invite a bypass; one command fixes it.
    if not load_users():
        print('No accounts exist yet, so nobody could sign in.\n'
              '\n'
              '  Create the first administrator:\n'
              '      python app.py create-admin\n'
              '\n'
              'After that, further accounts are created from Settings > Users\n'
              'inside the app. There is no public signup.', flush=True)
        sys.exit(1)

    # Legacy data that never got claimed — the account was created before this
    # existed, or users.json was rebuilt. The oldest admin is the closest thing
    # to "whoever this portfolio belongs to".
    if _legacy_data_files():
        admins = [u['username'] for u in load_users()
                  if u.get('role') == 'admin' and not u.get('disabled')]
        if admins:
            moved = _claim_legacy_data(admins[0])
            if moved:
                print(f'[DATA] moved {", ".join(sorted(moved))} into '
                      f'{admins[0]}/', flush=True)

    for _u in load_users():
        _backfill_transaction_gains(_u['username'])

    port = int(os.environ.get('PORT', 5000))

    # Loopback by default. Routes require a session now, but that is a reason to
    # keep this rather than to relax it: the login form itself would be exposed,
    # and Flask's development server has no business facing a network. Set HOST
    # explicitly for a real deployment — behind TLS, where SESSION_COOKIE_SECURE
    # turns itself on the moment this leaves loopback.
    host = os.environ.get('HOST', '127.0.0.1')

    # SERVER=waitress is the deployed path; the default stays app.run() so that
    # a developer double-clicking the launcher gets the same behaviour as before.
    _server = os.environ.get('SERVER', '').strip().lower()
    _headless = (_server == 'waitress'
                 or os.environ.get('NO_BROWSER', '').strip().lower()
                 in ('1', 'true', 'yes'))

    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f'http://127.0.0.1:{port}')

    # There is no browser to open on a headless server, and under systemd with
    # Restart=always a crash loop would fire this once per restart.
    if not _headless:
        threading.Thread(target=open_browser, daemon=True).start()

    def _prewarm_movers():
        import time as _time
        _time.sleep(0.5)
        for ex in ('TSX', 'NYSE', 'NASDAQ'):
            try:
                _refresh_movers(ex)
            except Exception:
                pass

    threading.Thread(target=_prewarm_movers, daemon=True).start()

    # One process, many threads — never multiple worker processes. PORTFOLIO_LOCK,
    # _TtlCache, _report_jobs and _market_last_good are all per-process state, so
    # a second worker means two locks guarding the five JSON files that make up
    # one logical record, two copies of every corporate-actions cache paying the
    # upstream cost twice, and a report job that 404s depending on which worker
    # happens to field the poll. waitress is single-process by design, which is
    # exactly why it is the right swap here and `gunicorn -w 2` is not.
    if _server == 'waitress':
        from waitress import serve as _waitress_serve
        _threads = int(os.environ.get('THREADS', '16') or 16)
        print(f'[BOOT] waitress on {host}:{port} — one process, {_threads} threads',
              flush=True)
        _waitress_serve(app, host=host, port=port, threads=_threads,
                        ident='McKechnie Terminal')
    else:
        app.run(host=host, port=port, debug=False, threaded=True)
