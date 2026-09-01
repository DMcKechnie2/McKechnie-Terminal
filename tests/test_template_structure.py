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


# ── The .chart-empty overlays need a containing block ─────────────────────────
# `.chart-empty` is `position: absolute; inset: 0` — an overlay that covers a
# chart canvas so the box keeps its height while the message shows. Six of them
# live inside a `.table-box` (earnings, FCF, shares outstanding, dividend
# history, P/E, and the balance sheet's own box), so that box has to be their
# containing block.
#
# It was, by accident: `.table-box` used to carry `animation: fadeUp 0.35s ease
# both`, whose keyframes end on `transform: translateY(0)`, and `fill-mode:
# both` leaves that transform applied forever — which makes the element a
# containing block. The design pass swapped the animation for a radius and a
# clip and took the containing block with it. Every overlay reparented to
# `#wrap` and drew as a full-width band at the *top of the page*, on top of the
# header — and because `inset: 0` also makes it a hit target, the "+ Holdings"
# hero button underneath could not be clicked at all on any ticker with no
# dividend history.
#
# Nothing about that failure looks like a missing declaration, which is why it
# is asserted rather than left to be noticed: the rule that lost it was editing
# the radius, and the symptom appeared four sections away.
CSS_RULE_RE     = re.compile(r'([^{}]*)\{([^{}]*)\}')
CSS_COMMENT_RE  = re.compile(r'/\*.*?\*/', re.S)


def css_block(text, selector):
    """The declarations of the rule whose selector list is exactly `selector`.

    Comments are stripped first: the chunk before a `{` runs back to the
    previous `}`, so a rule documented with a `/* ... */` above it — which every
    load-bearing rule in this file is — would otherwise never match its own
    selector.
    """
    for m in CSS_RULE_RE.finditer(CSS_COMMENT_RE.sub('', text)):
        if m.group(1).strip() == selector:
            return m.group(2)
    return None


def test_table_box_establishes_a_containing_block():
    body = css_block(read_template('index.html'), '.table-box')
    assert body is not None, '.table-box rule not found in templates/index.html'
    assert re.search(r'position\s*:\s*relative', body), (
        '.table-box has lost `position: relative`. The `.chart-empty` overlays '
        'inside it are `position: absolute; inset: 0`, so without it they '
        'reparent to #wrap, render as full-width bands across the top of the '
        'page, and swallow clicks on everything under them — including the '
        '"+ Holdings" button.'
    )


def test_chart_empty_is_an_overlay():
    """The other half of the pair: if this stops being absolute, the rule above
    is guarding nothing and can be dropped along with it."""
    body = css_block(read_template('index.html'), '.chart-loading, .chart-empty')
    assert body is not None, '.chart-loading/.chart-empty rule not found'
    assert re.search(r'position\s*:\s*absolute', body)


def test_table_box_chart_empty_lays_out_in_flow():
    """#bs-empty carries both classes — it *replaces* the balance-sheet grid
    rather than covering a canvas, so the rule above cannot reach it: the
    absolute positioning is on the element itself, not an ancestor."""
    body = css_block(read_template('index.html'), '.table-box.chart-empty')
    assert body is not None, (
        'the .table-box.chart-empty rule is gone, so #bs-empty is absolutely '
        'positioned against #wrap again and renders across the top of the page.'
    )
    assert re.search(r'position\s*:\s*static', body)


# ── Navigation ────────────────────────────────────────────────────────────────

# The top-level pages: the ones _hideAllPages() is responsible for. The account
# sub-sections (holdings, dividends, performance, valuations) are not here —
# they live inside #account-page and are switched by ACCOUNT_SECTIONS, so
# hiding their container hides them.
#
# Adding a page means adding it here and to _hideAllPages(). That is the whole
# point of there being one page-hide site: fetchStock() used to inline its own
# copy, which had already drifted — it never hid settings-page, so searching a
# ticker from Settings left it on screen underneath the result.
TOP_LEVEL_PAGES = ['account-page', 'settings-page', 'news-page',
                   'index-page', 'admin-page', 'blocked-page']


def _hide_all_pages_body(text):
    """The source of _hideAllPages(), from its opening line to its closing brace."""
    start = text.index('function _hideAllPages()')
    depth, i = 0, text.index('{', start)
    for pos in range(i, len(text)):
        if text[pos] == '{':
            depth += 1
        elif text[pos] == '}':
            depth -= 1
            if depth == 0:
                return text[start:pos + 1]
    raise AssertionError('_hideAllPages() is never closed')


@pytest.mark.parametrize('page_id', TOP_LEVEL_PAGES)
def test_every_top_level_page_is_hidden_on_navigation(page_id):
    body = _hide_all_pages_body(read_template('index.html'))
    assert page_id in body, (
        f'#{page_id} is not hidden by _hideAllPages(), so it stays on screen '
        f'underneath whatever you navigate to next.'
    )


@pytest.mark.parametrize('page_id', TOP_LEVEL_PAGES)
def test_every_top_level_page_exists_in_the_markup(page_id):
    """A page named in _hideAllPages() but absent from the document is a
    TypeError on the first click, which takes navigation down for everyone.
    #admin-page is the one that is conditionally rendered, and it is the one
    with a null guard."""
    text = read_template('index.html')
    assert f'id="{page_id}"' in text


def test_the_index_page_ids_the_js_binds_all_exist():
    """The JS binds these by id and never by layout class, so the markup can be
    recomposed freely — as long as the ids survive."""
    text = read_template('index.html')
    for element_id in ('idx-meta', 'idx-filter', 'idx-refresh-btn', 'idx-loading',
                       'idx-empty', 'idx-table-scroll', 'idx-tbody'):
        assert f'id="{element_id}"' in text, f'#{element_id} is gone'


def test_index_nav_button_reaches_the_page():
    text = read_template('index.html')
    assert 'onclick="goIndexes()"' in text
    assert 'data-nav="indexes"' in text
    assert "_setActiveNav('indexes')" in text, (
        'goIndexes() does not light its own nav button, so the header shows no '
        'active section while the page is open.'
    )


def test_every_index_selector_button_names_a_backend_index():
    """A typo in data-idx is silent — the button just 404s on click. Same
    argument as the test that every _RATE_LIMITS key names a live endpoint."""
    import app as terminal

    text = read_template('index.html')
    page = text[text.index('id="index-page"'):text.index('<!-- end index-page -->')]
    buttons = set(re.findall(r'data-idx="([^"]+)"', page))
    assert buttons, 'the index page has no selector buttons'
    unknown = sorted(buttons - set(terminal._UNIVERSES))
    assert unknown == [], f'data-idx names universes the server does not serve: {unknown}'


def test_every_backend_universe_has_a_button():
    """The other direction: a venue the server can price with no way to reach it
    is dead code that looks like a feature in app.py."""
    import app as terminal

    text = read_template('index.html')
    page = text[text.index('id="index-page"'):text.index('<!-- end index-page -->')]
    buttons = set(re.findall(r'data-idx="([^"]+)"', page))
    missing = sorted(set(terminal._UNIVERSES) - buttons)
    assert missing == [], f'no selector button reaches: {missing}'


def test_hidden_nav_button_reaches_the_page():
    text = read_template('index.html')
    assert 'onclick="goHidden()"' in text
    assert 'data-nav="hidden"' in text
    assert "_setActiveNav('hidden')" in text, (
        'goHidden() does not light its own nav button, so the header shows no '
        'active section while the page is open.'
    )


def test_the_blocked_page_ids_the_js_binds_all_exist():
    text = read_template('index.html')
    for element_id in ('blk-meta', 'blk-empty', 'blk-table-scroll', 'blk-tbody',
                       'blocked-notice', 'blocked-notice-title',
                       'blocked-notice-unhide', 'block-hero-btn'):
        assert f'id="{element_id}"' in text, f'#{element_id} is gone'


def test_the_blocked_notice_is_cleared_on_navigation():
    """It is not a page, so the page loop above does not cover it — and it sits
    above the movers strip, so left standing it says the stock you searched
    twenty minutes ago is hidden, over whatever you navigated to since."""
    body = _hide_all_pages_body(read_template('index.html'))
    assert 'blocked-notice' in body


def test_the_hide_control_claims_its_click_before_the_row():
    """Both discovery tables navigate on a row click, and Hide lives inside the
    row. Without stopPropagation, hiding a stock also opens the stock — the last
    thing that should happen to something you just asked not to see."""
    text = read_template('index.html')
    hits = re.findall(r"closest\('\.row-hide-btn'\)", text)
    assert len(hits) >= 2, 'the movers table and the market browser each need one'
    for block in re.split(r"closest\('\.row-hide-btn'\)", text)[1:]:
        assert 'stopPropagation' in block[:300], (
            'a .row-hide-btn handler does not stop the row click behind it'
        )


def test_the_movers_selector_is_scoped_to_its_own_page():
    """The market browser's tabs share .movers-ex-btn with the movers strip —
    deliberately, they are the same control. An unscoped querySelectorAll bound
    the movers handler to those too, so clicking an index tab also set
    moversExchange to undefined and fired a reload for a venue that is not one.
    """
    text = read_template('index.html')
    assert "querySelectorAll('.movers-ex-btn')" not in text, (
        'an unscoped .movers-ex-btn query binds the movers handler to the '
        'market browser tabs as well'
    )
    assert "querySelectorAll('#home-movers .movers-ex-btn')" in text


# --------------------------------------------------------------------------
# The annual / quarterly toggle
# --------------------------------------------------------------------------

def test_period_section_headings_all_exist():
    """The headings say "— Annual" in the markup, so syncPeriodLabels rewrites
    them when the toggle flips. A renamed or dropped id fails silently: the
    chart still redraws with quarterly bars, only the heading above it keeps
    claiming Annual, which is worse than having no toggle at all."""
    text = read_template('index.html')
    for element_id in ('sec-revenue', 'sec-earnings', 'sec-fcf', 'sec-margin',
                       'sec-eps', 'sec-capex', 'sec-shares', 'sec-shares-period',
                       'period-toggle', 'period-status'):
        assert f'id="{element_id}"' in text, f'#{element_id} is gone'


def test_the_period_toggle_does_not_reuse_the_price_range_class():
    """`.period-btn` already belongs to the price chart's range selector
    (1D/1W/1M/1Y/5Y/10Y/All), which has its own CSS rule and its own handler.

    Sharing it collided both ways, and was measured doing so: their handler
    clears .active across every match and wiped this control's state, and a
    click on any range button reached this control's listener with
    `dataset.period` undefined — which reads as "annual" and silently snapped
    the fundamental charts back out of quarterly. The `.movers-ex-btn` lesson
    one class over, which is why this is asserted rather than remembered.
    """
    text = read_template('index.html')
    assert 'class="freq-btn" data-period=' in text
    assert 'class="period-btn" data-period=' not in text, (
        'the frequency toggle is reusing the price range selector\'s class'
    )
    # And the handlers are scoped, so a future third user of either class
    # cannot re-create the collision.
    assert "querySelectorAll('#period-toggle .freq-btn')" in text
    assert "closest('#period-toggle .freq-btn')" in text


def test_the_dividend_chart_asks_for_an_annual_yoy_lag():
    """Dividend history is by year whatever the fundamental charts are set to.
    It shares makeYoyPlugin with them, so it has to name lag 1 rather than
    inherit the 4 a quarterly series needs — otherwise flipping the toggle
    silently re-bases the dividend chart's growth figures four years apart."""
    text = read_template('index.html')
    assert 'makeYoyPlugin(1)' in text


# ── Responsive layout ────────────────────────────────────────────────────────
#
# The app is one template at every width; there is no separate phone build to
# drift. What makes that hold is a small set of mechanical rules, and each of
# the four below was broken at the time it was written — every one of them
# silently, on a machine wide enough never to show it.

PHONE_BP = '@media (max-width: 640px)'


def test_no_inline_grid_template_columns():
    """A column count is a class. An inline `grid-template-columns` outranks
    every media query in the file, so the five rows that carried one could not
    respond to anything: at 375px the stock page held seven fixed columns and
    the document scrolled sideways to 999px, dragging the fixed ticker tape out
    with it. Two of those rows were also classed `.g4` while the inline style
    said seven.

    The rules they were ignoring existed the whole time — `.g3, .g4` collapsed
    at 680px and everything collapsed at 420px — which is the point: the
    responsive CSS looked present and was decorative. Nothing about that reads
    as a bug in the file.
    """
    for name in TEMPLATE_FILES:
        text = strip_non_markup(read_template(name))
        assert 'grid-template-columns' not in text, (
            f'{name} sets grid-template-columns inline. Media queries cannot '
            f'override an inline style, so that element is outside the '
            f'responsive layout at every width. Use a .gN class.'
        )


def test_phone_rules_live_in_exactly_one_block():
    """Phone rules go in one place, for the same reason there is one
    `_hideAllPages()`.

    This file reached eight breakpoints — 1150, 1100, 900, 780, 680, 640, 620
    and 420 — with no rule about which meant what, and *two* of them were the
    same 640px width setting `.wrap` padding to different values, where
    whichever sat lower in the file silently won. The widths above the phone one
    do desktop-to-narrow-desktop work and are left alone; 640 means phone and
    appears once.
    """
    text = read_template('index.html')
    style_only = text[text.index('<style>'):text.index('</style>')]
    style_only = CSS_COMMENT_RE.sub('', style_only)
    assert style_only.count(PHONE_BP) == 1, (
        f'expected exactly one `{PHONE_BP}` block, found '
        f'{style_only.count(PHONE_BP)}. Two blocks at one width means the '
        f'lower one silently wins on any property they share.'
    )


@pytest.mark.parametrize('name', TEMPLATE_FILES)
def test_every_template_carries_the_viewport_meta(name):
    """Without it a phone lays the page out at ~980px and scales it down, so
    every media query below sees a desktop width and none of them fire."""
    text = read_template(name)
    assert re.search(
        r'<meta\s+name=["\']viewport["\']\s+content=["\'][^"\']*width=device-width',
        text, re.I,
    ), f'{name} has no width=device-width viewport meta'


def test_every_grid_column_class_used_has_a_rule():
    """A `.gN` in the markup with no rule behind it is a grid that silently
    falls back to one column — no error, no warning, just a page that looks
    wrong on the one screen nobody tested. The scale runs to 7 because the stock
    page has rows of 5, 6 and 7 metrics."""
    text = read_template('index.html')
    markup = strip_non_markup(text)
    used = set()
    for m in CLS_RE.finditer(markup):
        used.update(c for c in m.group(1).split() if re.fullmatch(r'g\d+', c))
    assert used, 'no .gN grid classes found in the markup at all'
    for cls in sorted(used):
        assert re.search(r'^\.%s\s*\{' % cls, text, re.M), (
            f'markup uses .{cls} but no `.{cls} {{ ... }}` rule defines it, so '
            f'that grid collapses to a single column at every width'
        )


def test_phone_column_hiding_is_scoped_to_a_container():
    """`.hp-table` is worn by the holdings, options, closed-options and
    transactions tables; `.movers-table` by the movers, Markets and Hidden
    tables. Their nth-child positions mean something different in each, so an
    unscoped rule that drops column 5 hides Volume on one table and Expiry on
    another — the `.movers-ex-btn` lesson, which cost this file a working
    frequency toggle once already.
    """
    text = CSS_COMMENT_RE.sub('', read_template('index.html'))
    phone = text[text.index(PHONE_BP):text.index('</style>')]
    for sel in re.findall(r'[^{};]*nth-child[^{]*\{', phone):
        for part in sel.split(','):
            part = part.strip()
            if 'nth-child' not in part:
                continue
            assert part.startswith('#'), (
                f'phone rule `{part}` hides a table column without scoping to a '
                f'container id. Both table classes are shared by four tables '
                f'with different column orders.'
            )
