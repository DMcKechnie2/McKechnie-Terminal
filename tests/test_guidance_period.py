"""The forward guidance panel's period dating.

`FY2027` on its own does not say what is being projected. Most filers name a
fiscal year for the calendar year it *ends* in — Microsoft's FY2027 ends June
2027 — but retailers name it for the year it *begins*: Lululemon's FY2026 runs
Feb 2026 to Jan 2027. NVIDIA and Lululemon both close their fiscal year in
January and label it the opposite way round, so nothing keyed on the year-end
month alone can separate them, and a guess prints a real-looking span that is
twelve months wrong.

The calibration that does separate them is the filing: a company guides a
period that has not finished yet, so of the two candidate spans the right one
is the earlier that still ends on or after the filing month. That is what
`fgPeriodSpan` implements, and the vectors below are the companies' actual
reported periods rather than that function's own output.

Two halves are tested here and they fail differently:

  * `_fye_month_opt` must answer None when the annual frame cannot say. The
    older `_fye_month` defaults to December, which is harmless in a chart
    caption and a whole year wrong when a *date* is computed from it.
  * the span arithmetic itself, which lives in the template's JS. It is driven
    through a real JS runtime when one is present, and skipped when not — a
    ported copy would only prove the copy right.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as terminal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, 'templates', 'index.html')


# ── The fiscal year-end month ────────────────────────────────────────────────

class _Frame:
    def __init__(self, columns):
        self.columns = columns


def test_fye_month_opt_reads_the_newest_annual_column():
    """Every annual column shares the month, so the newest one answers."""
    import pandas as pd
    fin = _Frame([pd.Timestamp('2024-06-30'), pd.Timestamp('2026-06-30'),
                  pd.Timestamp('2025-06-30')])
    assert terminal._fye_month_opt(fin) == 6


@pytest.mark.parametrize('frame', [None, _Frame([]), _Frame(['not-a-date'])])
def test_fye_month_opt_is_none_when_the_frame_cannot_say(frame):
    """None, never a December guess.

    The guidance panel dates a fiscal period against this. Turning "unknown"
    into December for an offset filer captions Microsoft's FY2027 as
    `Jan - Dec 2027` when it means Jul 2026 - Jun 2027, which reads exactly
    like a figure that was looked up.
    """
    assert terminal._fye_month_opt(frame) is None


@pytest.mark.parametrize('frame', [None, _Frame([]), _Frame(['not-a-date'])])
def test_fye_month_keeps_its_december_default(frame):
    """The older helper's contract is unchanged — `_quarter_label` relies on it."""
    assert terminal._fye_month(frame) == 12


def test_stock_payload_carries_the_fiscal_year_end():
    """The panel gets the month from `/api/stock`, which already holds the frame.

    Sourcing it anywhere else means a second upstream fetch on a route whose
    whole design is that it costs nothing to read.
    """
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert "'fiscal_year_end_month': _fye_month_opt(" in src, (
        'the stock payload must carry the fiscal year end, and must read it '
        'through the strict helper rather than the December-defaulting one'
    )


# ── The span arithmetic ──────────────────────────────────────────────────────

# (label, fye month, scorecard, expected span). Every expectation is the
# company's real reported period.
SPAN_CASES = [
    ('MSFT FY2027',    6,  2027, 'FY', '2026-07-29', 'Jul 2026 – Jun 2027'),
    ('MSFT FY2027 Q1', 6,  2027, 'Q1', '2026-07-29', 'Jul – Sep 2026'),
    ('PEP FY2026',     12, 2026, 'FY', '2026-07-09', 'Jan – Dec 2026'),
    ('NOW FY2026 Q3',  12, 2026, 'Q3', '2026-07-22', 'Jul – Sep 2026'),
    ('AAPL FY2026 Q4', 9,  2026, 'Q4', '2026-07-30', 'Jul – Sep 2026'),
    # Retail convention: the year is named for when it BEGINS.
    ('LULU FY2026',    1,  2026, 'FY', '2026-06-04', 'Feb 2026 – Jan 2027'),
    ('LULU FY2026 Q2', 1,  2026, 'Q2', '2026-06-04', 'May – Jul 2026'),
    # Same year-end month, OPPOSITE convention: NVDA's FY2026 ended Jan 2026.
    ('NVDA FY2026 Q4', 1,  2026, 'Q4', '2025-11-19', 'Nov 2025 – Jan 2026'),
    ('NVDA FY2026',    1,  2026, 'FY', '2025-11-19', 'Feb 2025 – Jan 2026'),
    # A December filer needs no calibration, so it dates without a filing date.
    ('Dec, no filing', 12, 2026, 'FY', '',           'Jan – Dec 2026'),
    # Refusals. A missing span is what this looked like before it existed; a
    # wrong one is indistinguishable from a real figure.
    ('unbounded',      12, None, 'OTHER', '2026-08-06', ''),
    ('OTHER w/ year',  12, 2026, 'OTHER', '2026-07-30', ''),
    ('no fye known',   None, 2027, 'FY',  '2026-07-29', ''),
    ('offset, unfiled', 6, 2027, 'FY',    '',           ''),
]

_JS_RUNTIME = shutil.which('deno') or shutil.which('node')

_HARNESS = r'''
const html = %(html)s;
const js = html.slice(html.indexOf('const _FG_MONTHS'),
                      html.indexOf('function fgItemLabel'));
const fgPeriodSpan = new Function(
  'return (function(){' + js + '; return fgPeriodSpan;})()')();
const cases = %(cases)s;
const out = cases.map(c => fgPeriodSpan(
  { fiscal_year: c[0], fiscal_period: c[1], filing_date: c[2] }, c[3]));
console.log(JSON.stringify(out));
'''


@pytest.mark.skipif(not _JS_RUNTIME, reason='no JS runtime (deno or node) available')
def test_period_span_dates_both_fiscal_year_conventions(tmp_path):
    """Drive the real template function over known fiscal calendars.

    The pair that matters is LULU and NVDA: identical year-end month, opposite
    labelling, and a formula keyed on the month alone gets one of them a full
    year wrong with nothing on screen to give it away.
    """
    html = open(INDEX, encoding='utf-8').read()
    cases = [[fy, fp, filed, fye] for _, fye, fy, fp, filed, _ in SPAN_CASES]
    script = tmp_path / 'span.js'
    script.write_text(
        _HARNESS % {'html': json.dumps(html), 'cases': json.dumps(cases)},
        encoding='utf-8')

    argv = ([_JS_RUNTIME, 'run', '--allow-read', str(script)]
            if 'deno' in os.path.basename(_JS_RUNTIME)
            else [_JS_RUNTIME, str(script)])
    # encoding is explicit: the spans carry an en-dash, and on Windows the
    # locale default decodes it to a replacement char, failing the compare
    # against a span that is in fact correct.
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                          encoding='utf-8')
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    for (label, _fye, _fy, _fp, _filed, want), actual in zip(SPAN_CASES, got):
        assert actual == want, f'{label}: got {actual!r}, want {want!r}'


# ── The wiring that makes it visible ─────────────────────────────────────────

def test_panel_renders_the_span_through_the_helper():
    """The period head must consult `fgPeriodSpan`, not print the raw month."""
    text = open(INDEX, encoding='utf-8').read()
    assert 'fgPeriodSpan(c, _fyeMonth)' in text, (
        'the guidance card must date its period through fgPeriodSpan'
    )
    assert re.search(r'\.fg-period-span\s*\{', text), (
        'markup uses .fg-period-span but no CSS rule defines it'
    )


def test_fye_month_is_reset_from_every_stock_payload():
    """It is per company. Carried across a search it dates one filer's guidance
    by another's calendar, which is the CDR/currency trap in a new place."""
    text = open(INDEX, encoding='utf-8').read()
    assert 'data.fiscal_year_end_month' in text, (
        '_fyeMonth must be set from the stock payload on every lookup'
    )


def test_section_header_names_the_periods():
    """The whole point of the panel is what the company said about *when*, and
    that should not need a scroll to find."""
    text = open(INDEX, encoding='utf-8').read()
    assert 'sorted.map(c => c.period_label)' in text, (
        'the Forward Guidance header should list the periods it covers'
    )
