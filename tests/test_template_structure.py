"""Structural checks on the Jinja templates: every <div> must be closed exactly once.

templates/index.html carried an unmatched </div> at the end of the dashboard for
the whole life of the repo — it was there in the initial commit. Browsers drop a
stray close tag that has nothing to close, so the page always rendered correctly
and nothing ever pointed at it. The cost was silence: with the region already at
-1, a genuine nesting mistake made anywhere in the dashboard or the stock detail
panels produced no signal at all.

A naive count of '<div' against '</div' does not work on this file. Most of it is
JS that builds table rows and cards out of template literals full of divs, and one
comment inside that JS contains a literal "<script>". So the walk below strips
<script>, <style> and HTML comment regions first, replacing them with spaces so
line numbers stay true, and only then counts tags.

Two properties are asserted, and they catch different mistakes:

  * the depth never goes negative — a close tag with nothing open to close
  * the depth ends at zero — nothing left open at the end of the file

A file can end at zero and still be wrong (one stray close plus one unclosed div
cancel out), which is why the negative check is not redundant.
"""
import os
import re

import pytest

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates'
)

TEMPLATE_FILES = sorted(
    f for f in os.listdir(TEMPLATES) if f.endswith('.html')
)

# <div ...> or </div>, capturing the slash and the attributes
DIV_RE = re.compile(r'<\s*(/?)\s*div\b([^>]*)>', re.I)
ID_RE  = re.compile(r'\bid\s*=\s*["\']([^"\']*)["\']', re.I)
CLS_RE = re.compile(r'\bclass\s*=\s*["\']([^"\']*)["\']', re.I)

RAW_TEXT_TAGS = ('script', 'style')


def strip_non_markup(text):
    """Blank out <script>, <style> and <!-- --> regions, preserving line numbers.

    Everything removed is replaced by spaces rather than deleted, so an offset in
    the result still maps to the same line in the original file.
    """
    out = list(text)
    low = text.lower()
    n = len(text)
    i = 0

    def blank(start, end):
        for k in range(start, end):
            if out[k] != '\n':
                out[k] = ' '

    while i < n:
        if low.startswith('<!--', i):
            end = low.find('-->', i + 4)
            end = n if end == -1 else end + 3
            blank(i, end)
            i = end
            continue

        region_end = None
        for tag in RAW_TEXT_TAGS:
            if not low.startswith('<' + tag, i):
                continue
            after = i + 1 + len(tag)
            # '<scriptable' is not a <script> tag
            if after >= n or low[after] not in ' \t\r\n>/':
                continue
            open_end = low.find('>', i)
            if open_end == -1:
                continue
            close = low.find('</' + tag, open_end)
            if close == -1:
                region_end = n
            else:
                gt = low.find('>', close)
                region_end = n if gt == -1 else gt + 1
            break

        if region_end is not None:
            blank(i, region_end)
            i = region_end
            continue
        i += 1

    return ''.join(out)


def describe(attrs):
    """A short, human-findable label for a div, e.g. '#dashboard .wrap'."""
    ident = ID_RE.search(attrs)
    cls   = CLS_RE.search(attrs)
    parts = []
    if ident:
        parts.append('#' + ident.group(1))
    if cls and cls.group(1).split():
        parts.append('.' + cls.group(1).split()[0])
    return ' '.join(parts) or '<div>'


def walk_divs(text):
    """Walk every div tag outside script/style/comments.

    Returns (unmatched_closes, still_open) where unmatched_closes is a list of
    line numbers holding a </div> that had nothing open to close, and still_open
    is a list of (line, label) for divs never closed.
    """
    stripped = strip_non_markup(text)
    line_starts = [0] + [i + 1 for i, ch in enumerate(stripped) if ch == '\n']

    def line_of(pos):
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    stack = []
    unmatched = []
    for m in DIV_RE.finditer(stripped):
        if m.group(1) == '/':
            if stack:
                stack.pop()
            else:
                unmatched.append(line_of(m.start()))
        else:
            stack.append((line_of(m.start()), describe(m.group(2))))
    return unmatched, stack


def read_template(name):
    with open(os.path.join(TEMPLATES, name), encoding='utf-8') as f:
        return f.read()


def test_templates_are_discovered():
    """Guard the guard: if the glob breaks, the tests below must not pass vacuously."""
    assert 'index.html' in TEMPLATE_FILES


@pytest.mark.parametrize('name', TEMPLATE_FILES)
def test_no_unmatched_closing_div(name):
    """No </div> may appear with nothing open to close it."""
    unmatched, _ = walk_divs(read_template(name))
    assert not unmatched, (
        f'templates/{name}: {len(unmatched)} closing </div> with nothing open to '
        f'close, at line(s) {unmatched}. A browser silently drops these, so the '
        f'page still renders — remove the surplus tag, or add the opening <div> '
        f'that was meant to pair with it.'
    )


@pytest.mark.parametrize('name', TEMPLATE_FILES)
def test_no_unclosed_div(name):
    """Every <div> must be closed before the end of the file."""
    _, still_open = walk_divs(read_template(name))
    detail = ', '.join(f'line {ln} {label}' for ln, label in still_open)
    assert not still_open, (
        f'templates/{name}: {len(still_open)} <div> never closed: {detail}'
    )


@pytest.mark.parametrize('name', TEMPLATE_FILES)
def test_stripper_consumed_every_raw_region(name):
    """If script/style/comment stripping silently failed, the walk above is junk.

    After stripping there must be no opening <script>/<style> and no '<!--' left,
    otherwise a region stayed in the text and its contents were counted as markup.
    """
    stripped = strip_non_markup(read_template(name)).lower()
    for tag in RAW_TEXT_TAGS:
        assert f'<{tag}' not in stripped, (
            f'templates/{name}: a <{tag}> region survived stripping — the div '
            f'walk cannot be trusted. Check for an unclosed <{tag}> tag.'
        )
    assert '<!--' not in stripped, (
        f'templates/{name}: an HTML comment survived stripping — check for an '
        f'unterminated <!-- .'
    )
