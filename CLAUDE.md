# McKechnie Terminal

Flask + vanilla JS stock fundamental analysis dashboard.

## Stack
- Backend: Python/Flask, yfinance, requests (scraping Macrotrends)
- Frontend: vanilla JS, HTML, Chart.js with datalabels plugin
- Data: yfinance fast_info for quotes, bulk yf.download for movers

## Key files
- app.py — all Flask routes and data fetching
- templates/index.html — entire frontend (single file)
- templates/login.html — the sign-in page; the only view served to a visitor
  with no session
- tests/ — offline regression tests, plus live smoke tests behind `-m network`
- tests/conftest.py — hands every test a signed-in client and redirects the
  whole data layer at a temp directory
- tests/test_isolation.py — two accounts, one server: proves neither can see the
  other's portfolio
- users/&lt;username&gt;/ — one directory per account holding that account's
  portfolio JSON and generated reports. `DATA_DIR` moves the whole tree.
- stock-intel/ — standalone equity-data package for AI agents. Not imported by
  app.py; it is the reference implementation for data-correctness rules.

## Running locally
```
python app.py create-admin   # first run only — creates the first account
python app.py                # serves on 127.0.0.1:5000, opens a browser
pytest                       # offline tests
pytest -m network            # live Yahoo Finance smoke tests
```
`python app.py list-users` and `python app.py passwd <user>` are the recovery
path; there is no email reset, so `passwd` is the only way back in after a
forgotten admin password.

## Invariants — do not regress these

**The gate is deny-by-default, and it is not a decorator.**
`_require_login()` is a `before_request` hook that closes every endpoint except
the three in `_PUBLIC_ENDPOINTS` (`static`, `login_page`, `api_login`). There are
~90 routes here; a `@login_required` you can forget to apply is a hole that looks
exactly like working code, and the one you forget will be the new one. A route
added after this line is protected because it exists, and opening one up takes an
explicit edit to a set that a test asserts on.

`admin_required` re-checks the session rather than trusting the gate, so an admin
route stays closed even if its endpoint is ever added to the allowlist.

**The rate limiter is the same shape as the gate, and for the same reason.**
`_rate_limit_gate()` is a second `before_request` hook, registered ahead of
`_require_login` so a refusal lands in front of `_current_user()` — which
re-reads the account file on every request. Every route draws on a default
bucket (`_RATE_DEFAULT`) because it exists; `_RATE_LIMITS` holds the tighter
per-endpoint numbers. A route added later is limited without anyone remembering
to limit it, which matters because the expensive routes here do not look
expensive from outside: `/api/stock` fans out into five Macrotrends scrapes,
`/api/news` spends an LLM key across ~135 threads, `/api/generate-report` starts
a six-minute subprocess. A named endpoint spends from **both** its own bucket and
the default, so the default stays a real ceiling rather than something an
expensive route steps around. A typo in a `_RATE_LIMITS` key is silent — the
route just keeps the default — so a test asserts every key names a live endpoint.

**The bucket is per device, and the device id is deliberately not in the
session.** `session.clear()` runs on both login *and* logout, so an id stored
there would refund the whole allowance to anyone who signed out and back in —
and the login route, which has no account yet, is where a limit is worth the
most. `did` is an opaque random cookie, HttpOnly, shape-checked on the way in
(`_DEVICE_RE`) because it reaches a dict key. It is not signed: the value
carries no claim, so forging one only moves you to a different bucket.

Bounding *that* is the second, coarser per-address bucket — `_ADDR_FACTOR` times
the device allowance, so several real devices behind one address never meet it
while a loop minting a fresh cookie per request meets it after that factor. Both
are consulted and the stricter wins, the same "worst of these keys" rule
`_login_locked()` uses. A refusal spends **nothing**: charging the device bucket
for a request the address bucket already refused lets one noisy client drain
every other device on that address without a single request getting through.

Behind a proxy the address half only means anything with `TRUSTED_PROXIES` set —
see the ProxyFix note. Unset, every client shares one address bucket, which is
why that one is sized as a backstop and the device bucket does the real work.

Token buckets, not counters per fixed window: a window boundary lets two full
allowances through back to back, and it cannot answer "how long until I may
retry" without being wrong by up to a whole window. `Retry-After` is on every
429, and the frontend's `fetch` wrapper surfaces it — same argument as the 401
it already handles, since a silently refused call renders as an empty panel.

**A rate limit cannot bound `/api/generate-report`; `REPORT_MAX_CONCURRENT`
does.** The cost of a report is that it lives for up to six minutes, not that it
is asked for often — three requests a minute is polite and still stacks eighteen
python subprocesses. The cap is per account so one user cannot starve another,
and the slot is claimed under `_report_jobs_lock` in the same critical section
that registers the job: counting first and registering after leaves a window
where two requests both see the same free slot. Outcomes go through
`_finish_report()` rather than assigning a fresh dict, because that dropped the
`started` stamp the TTL prune reads — and nothing else ever removes an entry from
a table the concurrency check walks on every request.

**Tests reset the buckets; they do not switch the limiter off.** `conftest.py`
clears `_rate_buckets` around every test, because the test client keeps its
cookies and is always `127.0.0.1` — so without it the whole suite shares one
bucket and the 448th request pays for the first, surfacing as an unrelated test
going red once somebody adds a few more. Same rule as the auth bypass: it lives
entirely in test code, and `app.py` has no flag that disables the limiter.
`tests/test_rate_limit.py` is the file that opts back in and drives it.

**Per-user data is separated by directory, not by an `owner` column.**
Every portfolio file lives at `<data dir>/users/<username>/<file>.json` and is
reached through `_user_store()`; `load_holdings()` and friends resolve the
account themselves. The alternative — one file with an owner field, filtered on
read — puts a filter at every read site, and there are dozens here:
`_compute_invested`, `_replay_ledger`, the chart walk, the dividend walk, the
positions feed, both rebuilds. Miss one and it reports another account's money,
which is the failure that looks most like working code. `load_holdings()` cannot
return the wrong rows because it never opens the wrong file, and nothing
downstream needs to know accounts exist.

`_current_username()` **raises** when there is no session rather than naming a
default account. A per-user file that quietly falls back to a shared one is the
exact leak this layout exists to prevent, so background work that legitimately
has no request passes `owner=` explicitly — `_run_report` (a worker thread) and
`_backfill_transaction_gains` (startup) are the two, and both would otherwise be
guessing whose portfolio to touch.

Adding a per-user file means calling `_register_user_file()` at its definition
site — which is also what puts it in the isolation sweep's coverage check.

**A report belongs to the account that built it.** StockBox writes one PDF per
ticker into its own output directory, so two accounts building AAPL overwrite
each other's file and serving that shared path hands back whichever build
finished most recently. The PDF is copied to `users/<name>/reports/` on
completion and `/api/report-file` serves only that copy. `_report_jobs` entries
carry an `owner` and answer 404 to anyone else: a uuid4 job id is unguessable,
but unguessable is not an access check, and the error `detail` is a build log
from another account's run.

**API keys are per account, and there is no shared fallback — including the
environment.** `settings.json` is a per-user file like the portfolio ones, and
`_resolve_api_key(name, owner)` reads only that account's copy. The environment
fallback was removed deliberately: an exported `GROQ_API_KEY` is one key serving
every account, which is the thing per-user keys exist to prevent. An account
with none configured gets the degraded path every caller already has, which is
honest, rather than quietly spending someone else's key. `/api/settings` takes
the account from the session and never from the payload, so no request shape
reads or writes another user's keys — and an administrator has no more access
here than anyone else. Administering accounts is not reading their credentials.

**On `POST /api/settings`, `''` and `null` are not the same thing.** The
Settings page posts all four key fields on one Save with usually one of them
filled in, so an empty string has to mean "leave this one alone" — read as
"clear it" instead, saving a DeepSeek key wipes the three the user did not
retype. Clearing therefore needs a signal no untouched form field can produce,
and that is JSON `null`, which the Remove control on each key card sends. Do
not collapse the two into one falsy test; a key that can be set but never unset
is what that asymmetry buys out of.

**A key is passed to the code that spends it; it is never reached for.**
`groq_call(..., key=)` and `_filter_relevant(items, backend)` take the
credential as an argument, resolved by the route. This is not ceremony: the news
pipeline runs on a worker thread with no request context (`/api/news` gives it a
25-second deadline), so a function resolving an ambient key from there would
find none and silently degrade — the same shape as the original import-time
binding bug, which went unnoticed for exactly that reason. `_run_report` takes
`owner` for the same reason and passes that account's keys into the subprocess
environment.

The market-news payload stays a *shared* cache — it is market-wide, nobody's
private data, and rebuilding it per account would multiply the upstream work for
an identical answer. What is not shared is the key that pays for the relevance
pass: whoever triggers a rebuild spends their own, and an account with no key
still triggers one and lands on the regex filter.

**`SECRET_KEY` is the one genuinely app-level secret, and it has its own file.**
It signs the session cookie, so it must be identical for everybody —
infrastructure, not a credential belonging to an account. It lives in
`app_secret.json` precisely so that making API keys per-user could not drag it
into one user's directory, which would stop every other account's session from
verifying. `_split_secret_from_legacy_settings()` lifts it out of the
pre-accounts `settings.json` before that file migrates.

**The app shell is `no-store`.** `/` embeds the CSRF token and the signed-in
username, so it must not sit in the browser cache after sign-out. Observed while
switching accounts: the previous user's page came back from cache with their
name still in it. Every data call 401'd and bounced to the login form, so
nothing leaked — but the name and token should not have been there at all.

**The first account claims the pre-accounts portfolio.** `_claim_legacy_data()`
moves any per-user file still loose in the data directory into the first
account's directory, from `create-admin` and again at startup if anything was
left behind. Moved rather than copied, so a second account created later cannot
find the originals and inherit them too, and skipped per file when the
destination already has one — it must be safe to run twice.

**Bind to loopback anyway.** Routes require a session now, but that is a reason
to keep the default, not to relax it: it would expose the login form itself, and
Flask's development server has no business facing a network. The default host is
`127.0.0.1`; `HOST` can override it for a real deployment behind TLS. Never
`0.0.0.0`. `SESSION_COOKIE_SECURE` follows the bind address automatically — it
turns on the moment `HOST` leaves loopback, because a `Secure` cookie over plain
`http://127.0.0.1` would never come back and login would silently never stick.

**Passwords exist only as a scrypt hash.** `generate_password_hash` (Werkzeug's
default, `scrypt:32768:8:1`) in `_make_user`, `create_user` and the two reset
paths — nowhere else. A password is never logged, never returned, and never put
in argv: that is why the first account is made by `create-admin` reading
`getpass` rather than by a first-run web page, which would be public signup with
extra steps. `_public_user()` is the only thing that serialises a user, because
returning the record directly puts an offline-crackable hash on the wire.

**There is no public signup, and the account routes are the boundary.** Accounts
come from `POST /api/admin/users` (admin only) or the CLI. `clean_username()`
guards the shape the way `clean_ticker()` guards a symbol, and for the same
reason — a username reaches JSON keys, log lines and URL paths.

**Administration is its own tab, and the server decides whether it exists.**
Managing other people's accounts used to be a third section on Settings, hidden
with `display:none` for four accounts in five — so the page had a different
shape depending on who was looking, and a plain user still received the user
table and the create-account form and could unhide both from the console. It is
`#admin-page` now, behind an `{% if is_admin %}` in the template along with its
nav button, so a non-admin's document does not contain it at all.

This is not the access check and must not be mistaken for one: `admin_required`
on every `/api/admin/*` route is, and it re-reads the session rather than
trusting anything the page says. The Jinja gate is about not shipping a door to
someone who cannot open it. The four JS functions behind it (`loadUsers`,
`createUser`, `setUserDisabled`, `deleteUser`) are deliberately *not* gated —
they are inert without the markup, every route they call answers 403, and
hiding them would add the appearance of a check rather than a check.

Two consequences. `_hideAllPages()` runs on every navigation and is the one
page-hide site, so `#admin-page` is the single element there that needs a null
guard — a bare `getElementById(...).style` would be correct for an admin and a
TypeError that breaks all navigation for everyone else. And `goAdmin()` returns
early when the page is absent, since the console is the only way to reach it
without a button.

Settings keeps what belongs to *you* — identity, password, your own API keys.
That division is the same one `/api/settings` already enforces by taking the
account from the session: administering an account is not reading its
credentials.

**A session is checked against the record, not just the cookie.** The cookie is
signed, so its contents are authentic — but they are a snapshot. `_current_user()`
re-reads the user every request and matches `token_version`, so disabling an
account or changing a password ends every *other* live session for it. Without
that, "change my password" does not evict whoever you changed it because of.
Login calls `session.clear()` first: a token planted before login must not
survive the privilege change.

**Anything that writes needs the CSRF header.** `SameSite=Lax` is one browser
setting between an attacker's page and a route that sells a position, so
`_check_csrf()` also requires `X-CSRF-Token` on every non-GET. The token is
rendered into `index.html` as a meta tag rather than fetched, because
`/api/auth/me` would race the calls that fire on page load.

The frontend attaches it in **one place** — the `fetch` wrapper installed at the
top of the first `<script>`, before anything can call it. There are ~50 `fetch`
sites in that file and no central helper, so a header added per call site is a
403 waiting for whoever writes the next one. The wrapper also turns a 401 into a
trip to the login form instead of a page of silently empty panels. It sends the
token to same-origin URLs only; a future cross-origin `fetch` must not be handed
a credential that authorises writes here.

**Login failures are counted, and they all look alike.** `_note_login_failure`
locks a username *and* a client address after `LOGIN_MAX_FAILS`. Expiry keys on
the last-failure timestamp: an earlier version tested `locked_until`, which is
`0.0` on an unlocked entry and therefore always read as expired, so the counter
reset on every attempt and the lockout could never engage at all. A wrong
password and an unknown user return one message from one code path, and an
unknown user still pays for a `check_password_hash` against `_dummy_hash()` —
otherwise response time turns the form into a "does this account exist" oracle.

**Tests never touch the real data files.** `tests/conftest.py` redirects
`_USER_DATA_ROOT` (which moves every per-user file at once), redirects the two
remaining shared stores, and hands out a pre-authenticated client. All of it is
load-bearing: `_DATA_DIR` is the working directory, so the live portfolio sits
next to `app.py`, and a test that reaches a mutating route writes it. That is not
hypothetical — the CSRF test left an `AAPL` row in the real `watchlist.json`
before the redirect existed. The bypass lives entirely in test code; there is no
"skip auth" flag in `app.py` for someone to find later and set.
`tests/test_auth.py` opts back out and builds plain clients, since it is testing
the closed door.

**`tests/test_isolation.py` is the sweep, and it is the test that matters.**
Per-route assertions check the routes that exist today; the sweep signs in as a
second account, walks every per-user GET and greps the raw response bytes for
markers seeded into the first account's portfolio. It is paired with a mirror
test — isolation that hides data from its *owner* is also a bug — and with a
coverage check that fails when a file registered in `_PER_USER_FILES` has no
route in the sweep. Set `DATA_DIR` to run the whole app against a copy of the
real data; that is how the migration was verified without touching the live
files.

**Validate tickers at every boundary.** `clean_ticker()` returns `None` for
anything that isn't a bare symbol. A ticker reaches a subprocess argv
(`_run_report`) and a filesystem path (`report_file`), so a raw
`.strip().upper()` is not sufficient — that was a shell-injection hole.

**The write boundary is a boundary.** Every route that *persists* a symbol runs
it through `clean_ticker()` and answers 400 — POST watchlist, POST holdings,
PUT and quick POST on transactions, the holding sell, POST and PUT on options,
and the valuations upsert. They used to store a bare `.strip().upper()`, so
`watchlist.json` and `holdings.json` could legally hold a value that was never a
symbol and every reader had to defend itself. Validating on the way in is what
makes a stored symbol worth trusting; a route that skips it pushes the check
onto code that has no idea where the value came from. Delete-by-ticker routes
only filter, so they are the one place a raw compare is still fine.

**Never build a command line by string interpolation.** `_run_report` passes an
argv list with no shell. The old `cmd.exe /c f'...{ticker}...'` form let
`AAPL & calc` execute.

**All JSON persistence goes through `JsonStore`.** It writes to a temp file and
`os.replace`s it (so an interrupted write cannot truncate the file), raises on a
corrupt read instead of returning an empty default, and holds a lock across
read-modify-write via `.mutate()`. A bare `except: pass` around a save reports
success to the UI while losing the data.

**Mutating routes carry `@atomic`.** Holdings, transactions, cash, sales and
options are one logical record spread over five files; they share
`PORTFOLIO_LOCK` so a concurrent request can't observe a half-applied sell.

**`transactions.json` is the only ledger. Never compute P/L from `sales.json`.**
Both holdings and sales are *derived*: `rebuild_holdings_from_transactions()` and
`rebuild_sales_from_transactions()` regenerate them, and every route that reports
a gain replays the ledger through `_replay_ledger()`. A sell's cost basis is the
WAC at the moment it executed, which only a replay recovers — sell out, re-buy
higher, and the old average survives nowhere else.

`/api/portfolio/performance` read `sales.json` directly and was the only route
that did. DELETE and PUT on a transaction rebuild holdings but wrote nothing to
that file, so a deleted trade or a corrected fat-fingered price kept its original
gain: the file had drifted $5,872 (six phantom rows, four of them "sold at $1.00")
and the Performance tab reported −$4,136 realized while every other tab showed
+$1,736. If you add a route that writes `transactions.json`, it must call **both**
rebuilds.

`date_acquired` is the one field on a sale row that the ledger cannot recompute —
it sets the dividend window in `/api/transactions`. The rebuild carries it across
by `txn_id`, so stamp that on any new sale row.

**A transaction id is a string, and parsing it must never drop the row.**
`_new_txn_id()` returns `'<ms>-<hex>'`; the random suffix was added because a
bare millisecond stamp collided. `/api/portfolio/invested` and
`/api/holdings/chart` both parsed it with `int()` inside a bare
`except Exception: pass`, so every transaction recorded since that change
vanished from their walks — three buys worth $2,403.69 at the time, and all of
them going forward. The shares still reached `holdings.json` (rebuilt by a
different path), so their market value counted toward funds while the capital
that bought them never counted toward `invested`: the Holdings tab reported the
difference as profit, +13.28% against a true +8.97%. Sort on the string; the
ms-timestamp prefix makes lexicographic order match numeric, which is what
`rebuild_holdings_from_transactions()` already relies on.

**A dividend whose pay date equals its ex date must still be credited.**
yfinance reports one date per distribution, so `_build_div_events` falls back to
`pay_date = ex_date`. In `/api/portfolio/invested` the pay event sorted at
priority 0 and the ex-date snapshot it reads at priority 2, so on a shared date
the credit ran first, missed the snapshot and defaulted to **0 shares** —
dropping the distribution outright. T.TO, UNH.TO and ZMMK.TO have `pay == ex` on
every distribution and credited exactly nothing ($64.40). The pay event now
sorts last when `pay == ex`; a dividend paid on its own ex-date cannot fund an
earlier same-day buy anyway. `/api/holdings/chart` has the same ordering but
falls back to pre-trade positions instead of 0, so it was unaffected — keep that
fallback if you touch it.

After both fixes the Holdings tab reconciles to the ledger: realized + unrealized
+ dividends received, and `invested` == net deployed − dividends received exactly.
Those two identities are the cheapest regression check on this whole area.

**Holdings and Performance divide by the same denominator, and it is not cost
basis.** Both tabs report a portfolio-level return, and they used to compute
"how much did I put in" separately: Holdings against external capital, Performance
against `Σ (cost_basis_sold + held_cost)`. That sum re-books a dollar on every lap
it makes — $68,655 of "invested" under a book that took $24,556 of real money,
with a money-market parking round-trip alone accounting for 23% of it. The two
tabs printed +13.28% and +2.87% for one portfolio on one day.

`_compute_invested()` is now the single source of that number and rides on the
`/api/portfolio/performance` payload, so the Performance tab cannot drift from
Holdings by fetching its own. Per *position* the cost basis is still the right
denominator — it is the capital that position consumed — so the per-box
percentages keep using it; only the summary divides by external capital.

Performance also counts dividends now (`_dividends_received_by_ticker()`), which
Holdings always did. Income changes the sign on real positions: T.TO reads
+$3.45 rather than −$34.01 once its $37.46 of dividends is counted.

`_dividend_payments()` is the one walk behind both the Dividends tab and this,
for the same reason `sales.json` is not allowed a second opinion on P/L.

**The TWR tile is chain-linked daily, and Modified Dietz is not a substitute.**
`/api/holdings/chart` labelled a Dietz number "time-weighted". They answer
different questions: money-weighted asks what the *investor* earned including
the effect of when capital arrived, time-weighted strips that out and asks what
the *holdings* did. On this book over nine months they read +25.55% and +27.75%;
the gap grows without bound as contributions grow against the opening balance
(double $1k, add $100k, drop 10% — the two report +80% and −84% on one
portfolio on one day). A test drives one security through two funding schedules
and requires one answer; the staggered ledger returned −51.24% against a true
−10% before the fix.

Dietz is the approximation for when you *lack* periodic valuations and have to
assume flows land at an average moment. `values_out` is a daily valuation
series, so there is nothing to approximate:

    r_i = V_i / (V_{i−1} + C_i)        TWR = Π r_i − 1

`C_i` is external capital only — sale proceeds, dividends and closed-option
gains stay inside the portfolio and belong to the return, which is what
`invested_out` already encodes. It belongs in the *denominator* because the walk
applies each day's transactions before valuing at that day's close: the new
money is already inside `V_i`, so leaving it out of the base books the
contribution itself as a gain. Day 0 sets the opening balance and carries no
return — there is no prior close to measure it against.

Flows are assumed to arrive at the start of their day, the one place a
convention is still needed, since transactions carry a date and no time. The
alternative — `(V_i − C_i) / V_{i−1}` — drops the flow's own intraday P/L out of
the return entirely rather than merely mistiming it.

**Sub-year returns are not annualized** (GIPS 5.A.4). `annualized_twr` is null
below 365 days, and the frontend falls back to the period return, so the gate
lives in `app.py` rather than the template — the old template threshold of 14
days raised a 29-day return to the 12.6th power and printed +96.22% as the
largest number on the page.

**Cash is derived. There is no cash store and no setter.** `load_cash()` returns
the `cash_pool` leg of `_compute_invested()`; `/api/cash` GET reports it and POST
answers 410.

It used to be `cash.json`, a running balance incremented by six write sites —
buy, sell, option buy, option sell, option edit, option delete — plus a manual
deposit modal. Nothing reversed it: DELETE and PUT on a transaction rebuild
holdings and sales and never touched it, and the option edit/delete paths
hand-rolled their own refund arithmetic to unwind the previous write. That is
the `sales.json` defect in a different file, and the Holdings tab was already
paying for it — the funds line added `cash.json` while `invested` had already
been *reduced* by the pool the model spent, so any un-redeployed proceeds fell
straight out of the P/L.

Two consequences if you touch this:

**Option legs must stay in the `_compute_invested()` walk.** They draw from and
return to the same pool as share trades (`opt_buy` / `opt_sell` at priority 1).
Before cash was derived they only moved `cash.json`, so omitting them here would
write the premium out of the portfolio entirely. Only *closed* contracts are
marked to market, matching `/api/holdings/chart` — the app never fetches a
contract's price, so counting an open one would book a loss equal to its premium.

**Realized option P/L rides on the performance payload as `options_pl`.** An
option has an underlying, not a share position, so it gets no box in the
per-ticker grid — but `invested` is net of the option legs, so the summary
numerator has to include it or the two tabs re-diverge by the size of the option
book. The summary card prints "incl. ±$X options" rather than carrying the
difference silently.

Both tabs now read +9.06% on this book, and
`realized + unrealized + dividends + option P/L == (cash_pool + market value) −
invested` holds to a penny. `cash.json` is dead and can be deleted.

**Escape anything from outside.** News headlines, insider names and company
names are scraped third-party text — some of it LLM-rewritten — and go into
`innerHTML`. Use `escHtml()`; use `safeUrl()` for any href.

**Send both `raw` and `value`.** `raw` is the true float, `value` the display
string. Charts must read `raw`; parsing the formatted string back into a number
quantises every data point to 2dp of its unit.

**A money figure carries the currency it is actually in, and the stock page has
two of them.** `currency` is what the *listing* trades in; `financialCurrency`
is what the company *files* in. Price, market cap, street targets, dividends per
share, insider trade values and `trailingEps` follow the listing. Revenue,
earnings, FCF, capex, the balance sheet and the by-year EPS series follow the
filing. They are the same for a domestic listing and different for **every ADR**
— TSM quotes in USD and reports in TWD, Sony in USD and JPY, BABA in USD and CNY.

`format_large_number(value, symbol='$')` takes the prefix and `_currency_symbol()`
maps the code; the default keeps portfolio code, which is always in the account's
own money, untouched. An unmapped currency falls back to its ISO code
(`SEK 4.50B`) and never to `$`, because a wrong unit reads as a real number.
CNY is `CN¥` so it cannot be mistaken for JPY, and `GBp` is pence — a real unit
1/100th of GBP, not a typo for it.

The hardcoded `$` is what surfaced this: SK hynix files in won, so its ₩189T of
TTM revenue rendered as "$189.17T" — larger than any company has ever billed, and
nothing on the page gave a reader a way to catch it. The figures were right; only
the symbol lied. The ADR case is worse, because two currencies appear on one page
under one symbol: TSM's revenue read "$4440.49B" against a real ~$140B.

**A ratio may not span the two.** This is the half that is silently wrong rather
than merely mislabelled, because a ratio carries no unit to give it away:

- `price_to_tangible_book` divided price (USD) by book per share (TWD) and landed
  ~31x off. It is now `None` when the currencies differ.
- Profit margin was overridden with `trailingEps × shares / totalRevenue` —
  USD earnings over TWD revenue, printing TSM's ~38% net margin as **1.33%** and
  Sony's as 0.05%. The override now requires `same_currency`; otherwise Yahoo's
  own `profitMargins`, which is unit-free, stands.

Suppressed rather than converted, deliberately: a live FX rate applied to a filed
balance sheet invents a figure that appeared on no statement, and applying today's
rate to a 2019 income statement is worse.

The two analyst estimate frames are **not** necessarily in the same currency and
each carries its own `currency` column — Yahoo returns TSM's EPS estimates in USD
per ADR and its revenue estimates in TWD, in one lookup. The frame's column wins;
the passed symbol is only the fallback.

The frontend builds its own chart labels off `raw`, so it gets the same split via
`_curSym = {trade, fin}`, set from the payload before anything renders. Portfolio
formatters are left on `$` — that is the account's money, not a filer's.

Not fixed, and upstream: Yahoo's TTM fields for 000660.KS disagree with its own
quarterlies (`totalRevenue` 189T KRW against 132T summed, `netIncomeToCommon`
162T against 75T), which is why its margin reads 85.68%. That is a Yahoo data
problem, not a formatting one — don't "correct" it by inventing a different
basis.

**Batch per-symbol requests.** The browser allows ~6 connections per host, so a
burst of single-symbol calls starves whatever the user is actually waiting for.
The ticker tape used to fire one `/api/quote` per item as it scrolled into view —
about 100 requests per load, which turned a 2.9s server-side lookup into 9.1s in
the browser. Symbols now queue for `TAPE_QUOTE_DEBOUNCE` and go out as one
`/api/quotes` call. Idle request volume went from ~125 per 22s to 9.

Callers that already know their full symbol list — holdings, performance,
watchlist, valuations — call `fetchQuotes(symbols)` directly instead of
debouncing. Those four pages fired 26 single-symbol requests between them; they
now fire four. **There is no reason left to call `/api/quote` from the frontend.**

**Corporate actions are cached; go through `_build_div_events` / `_build_splits`.**
Four endpoints (`/api/portfolio/invested`, `/api/dividends/history`,
`/api/holdings/chart`, `/api/transactions`) all need the same dividend history,
and all four fire on one portfolio load. Uncached, that was 81 calls for 22
distinct tickers — each a `.dividends` fetch plus a `.info` fetch — and ~19s of
upstream work per load. Both helpers now go through `_TtlCache`
(`CORP_ACTIONS_TTL`, 6h), which also collapses concurrent misses on one ticker
into a single fetch. Measured after: 4.7s cold, 0.5s warm.

A failed lookup must not be cached. `_fetch_div_events` returns `[]` for a ticker
that genuinely pays no dividend and *raises* when the lookup failed — the caller
cannot tell those apart, so caching a transient blank would pin dividend income
to zero for six hours. Keep that distinction if you touch the fetchers.

Consume these caches from a sequential loop and you serialise ~20 fetches;
`/api/dividends/history` and `/api/transactions` call `_warm_corp_actions()`
first for exactly that reason (7.6s → 2.3s and 5.2s → 1.8s cold).

**A balance-sheet snapshot comes from one column.** `_build_balance_sheet`
picks the newest period that actually reports Total Assets — quarterly first,
annual as the fallback — and reads every metric from that one column. Taking
each row's own latest non-null cell instead would pair this quarter's current
assets with last year's current liabilities and report a ratio that appeared on
no filing. When a row is missing from the chosen column the metric is null; it
does not reach back a year for it.

Every key is present on every payload, null when the filer does not report it.
Banks and insurers file no current assets or current liabilities at all, so
absent rows are the normal case — a shape that varies by filer pushes "missing
or zero?" onto each consumer. Leverage and coverage bands are skipped for
Financial Services in the frontend for the same reason `Net Debt / FCF` opts
out: 4x debt-to-equity is unremarkable for a bank.

**The ownership split has no ambient zero, and no clamp.** `_build_ownership`
owns both halves. The three parts are the right shape — Yahoo's own
`institutionsFloatPercentHeld` is `heldPercentInstitutions / (1 -
heldPercentInsiders)` to five decimals on every ticker checked, so institutions
hold out of the float and insiders hold the rest, and the remainder is genuinely
retail. What broke was the arithmetic laid over it, in two directions.

*Missing was read as zero.* Yahoo reports neither field for most non-US
listings — the same gap `_build_short_interest` documents for
`shortPercentOfFloat`, and here there is no fallback either, since
`ticker.major_holders` comes back an empty frame for the same symbols. The old
`info.get(...) or 0` turned that into 0% institutional, so `retail` fell out of
`1 - 0 - 0` at **100%** and the frontend's `> 0` guard passed on the strength of
that fabricated 100. GOOG.TO, MSFT.TO, META.TO and LULU.TO each drew a full
doughnut saying they are entirely retail-held — four of the ten symbols in this
account's portfolio, and Alphabet is ~81% institutional. The builder returns
`{}` now and the existing empty state shows.

*Over 100% was clamped.* Institutional legitimately exceeds shares outstanding
because 13F filings double-count lent shares — the lender still reports a
position the short buyer now also reports — so WING reads 123%, CARG 112%, ZG
107%, CVNA 106%. Six of twelve US names sampled. That is real information about
share lending, so the figure is kept as reported; what cannot survive is
`retail`, which is `None` rather than 0 because a clamped 0 asserts "no retail
float", a claim the data does not make. `exceeds_outstanding` carries that to
the frontend, which drops the doughnut and shows a note. It has to: Chart.js
renormalises whatever it is handed, so the old code printed "123.2%" in the
legend beside a slice drawn as 99% — the error made invisible by the chart.

Suppressed rather than reconciled, the same call as a cross-currency ratio:
scaling the three to sum to 100 invents a number that appeared in no filing. The
frontend rule follows from it — a part is rendered only when it is a number, so
a null never reaches `toFixed()` as a fabricated `0.0%`.

**"Other exchanges" means a different exchange, and Yahoo's search will not
tell you.** `/api/crosslist` feeds the switcher under the company name. Yahoo
returns a company's preferred series with `quoteType: EQUITY` and the *same
longname* as the common share — `BCE Inc.` is the longname of BCE-PZ.TO as
much as of BCE.TO — so no name test can separate them. BCE rendered six
buttons, five labelled TSX, each loading a preferred.

Three rules, in this order: the candidate must sit on a **different exchange**
than the current listing (that alone kills every same-venue preferred and
second share class); **one listing per exchange**, first wins, since Yahoo
ranks the common above its preferreds; and only then the name match.

The shortname is the only field that marks a preferred, and it lies in both
directions. It is truncated at 30 characters (`BROOKFIELD RENEWABLE LP PREF
SE`), and Yahoo files the *common* NYSE listing of BNS under
`Bank Nova Scotia Halifax Pfd 3` — rejecting on that marker alone cost the
bank its NYSE button. So a longname marker settles it, a shortname marker only
counts against a symbol carrying a class suffix (`BEP-PA`). Units are not
markers: an LP's units *are* its common equity (BEP-UN.TO).

The name match is token-based, ignoring legal form. The old
`any(w in candidate for w in name.split()[:2] if len(w) > 3)` was a coin flip:
`Royal Bank of Canada` admitted anything containing "bank", `BCE Inc.`
degenerated to matching "inc", and an issuer whose first two words are all
three characters or shorter (`AZZ Inc`) hit `any()` over an empty generator
and dropped **every** listing. A candidate now has to contain all of the
subject's identifying words plus nothing but depositary-receipt boilerplate,
so `APPLE INC CEDEAR(REPR 1/20 SHR)` matches Apple and `Apple Hospitality
REIT` does not.

The search returns **at most seven quotes** however high `quotesCount` goes,
and preferreds compete for those seven slots — there is no larger pool to
filter down from, which is why BNS.TO can still miss a real listing. Adding a
second query is the only lever; searching the bare symbol is not it, as it
returns mostly unrelated issuers and mutual funds.

**Macrotrends scrapes go through `_start_macrotrends`.** Five scrapes at a
10-second timeout each, run one after another, is twice the 25-second route
deadline. They are submitted together before the yfinance prefetch so they
overlap it and each other, and read back with `_mt_result`. Adding a sixth
scrape inline would reintroduce the deadline problem.

Reads share **one absolute deadline**, set when the futures were submitted
(`_MtScrapes`). Per-key timeouts compound — five reads at 12s each is a minute —
and every scrape started at the same moment anyway.

**A Macrotrends value is field `v2`. `v1` is the previous year.** In each
chartData row v1 equals the row above's v2 and v3 is the change between them, so
only v2 belongs to the row's own `date`. All four original scrapers read v1, and
every backfilled year on the FCF, profit-margin and share-count charts was
therefore showing the prior year's number under the current year's label — AAPL's
2020 free cash flow read $58.90B, which is what it earned in 2019. There is now
one parse site (`scrape_macrotrends`), an offline fixture that fails if the
response shape changes, and a live canary that compares the scrape against
yfinance on the overlapping years.

**Yahoo is a hard ceiling at ~5 annual columns.** `ticker.financials` and
`ticker.cashflow` return five, and Yahoo's own `fundamentals-timeseries` endpoint
returns four even with `period1` set to 1985. There is no parameter that widens
it. Macrotrends is why there is a longer chart at all — don't spend time trying
to coax more years out of yfinance.

**Macrotrends' fourteen years were its default, not its limit — `yb` is the
parameter.** Unset, the endpoint returns fourteen, which reads exactly like a
ceiling and was taken for one. Apple's own chart page embeds the iframe with
`yb=15`; it is honoured far past that, and `_MT_YEARS_BACK = 40` reaches Apple's
1987. The extra years are free — same ~10KB response, same ~0.15s, whether it
carries fourteen rows or thirty-nine — so it is sent for every metric rather
than tuned per chart, and Macrotrends truncates at its own coverage instead of
padding (NVIDIA starts at fiscal 1999, the year it listed).

Checked before raising it, and worth rechecking if it moves: across ten tickers
and all five series, every value the old request returned is byte-identical in
the wider one. `yb` only prepends older rows, so it cannot move a year the gate
validates against. **Losing this parameter is silent** — every chart shortens
back to fourteen with no error, no empty series and no log line, and the merge
still passes because the years it checks come back either way. A test asserts
the request carries it.

**A scraped series is taken or dropped whole, never spliced.** `_mt_check`
compares Macrotrends against yfinance on the overlapping years and
`_merge_macrotrends` gap-fills only if it passes. The tolerance is deliberately
loose (10%, one divergent year forgiven when there are three or more to compare)
because yfinance carries restatements where Macrotrends is as-reported: this is a
check for gross errors — wrong company, thousandfold unit slip, split-adjusted
pasted onto as-filed, the v1 year shift — not a reconciliation. Measured over a
15-ticker basket it accepts 44 of 45 series; how much an accepted one adds is
bounded by `_MT_YEARS_BACK` and then by the company's age — 35 years for AAPL or
KO, 24 for NVDA. Rejections all have one shape, a steady offset rather than one
bad year: AMZN's FCF runs 12-31% above yfinance's on all four overlap years
because the two net capital leases differently, XOM's 11-14% above on three of
four. Splicing those would put a definitional step change mid-chart that reads
as a real swing in the business. A rejection is logged, not silent, because a
dropped series looks exactly like a ticker Macrotrends doesn't carry.

The gate only ever sees the four or five years yfinance also carries, so it
judges a whole series on its most recent tail and admits everything behind it on
that evidence — an asymmetry that widening the window deepened. It holds because
every error it exists to catch is a property of the whole series and shows on
any overlap. A one-off bad year deep in the unvalidated past would not be
caught, and never was.

**Macrotrends dates a column the same way yfinance does**, so `_mt_years` buckets
the scrape with `_fiscal_year` — the same rule the statement frames go through —
and Walmart's fiscal 2025 lands on one label from both sources rather than two.
Free cash flow is the exception: it keys on the bare calendar year, so its merge
passes `fiscal=False`. That inconsistency predates this and is confined to that
one series; unifying it would move the displayed payout ratios for January filers.

**Nothing expensive on tab refocus.** `pollWhenVisible(fn, ms, {refreshOnFocus})`
re-runs `fn` immediately when the tab is shown. That's right for a few watchlist
quotes and wrong for the tape, which re-walks 100 symbols — pass
`refreshOnFocus: false` for anything costly.

**The Groq key lives in settings.json, so the client cannot be built at import
time.** `_groq = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))` bound the
client to the environment once, at import — but the key is written by
`POST /api/settings` into settings.json, and `GROQ_API_KEY` is not exported on
this machine. Every call raised on the empty key, `groq_call()`'s blanket
`except` returned `''`, and `/api/news` quietly fell back to sentence-casing the
extracted summary. The whole LLM path was dead and nothing said so.

`_resolve_groq_key()` now reads settings first and the environment as a fallback
— settings wins because that is the copy the UI writes, and a key pasted there
has to beat a stale one from the shell. `_groq_client()` caches on the key
itself, so a save through `POST /api/settings` takes effect on the next call with
no restart and no invalidation hook in the route. Keep that direction: an
import-time read of `os.environ` for any of `_SETTINGS_KEYS` is the bug.

Graceful degradation stays — callers all have a non-LLM fallback — but the two
failure modes are logged apart, "no key configured" once per key and "call
failed" every time. Returning `''` for both is what made this invisible.

**The breaking-news feed is RSS, and must stay independent of Groq.** `/api/news`
is per-ticker; `/api/news/market` is the market-wide feed behind the News tab,
built from the 15 RSS sources in `_MARKET_FEEDS`. Measured: all 15 in parallel is
0.58s for ~330 items, against 2-20s for a *single* ticker through `_build_news`.
Nothing on this path calls Groq — an RSS `<title>` is already a headline, so the
feed keeps working with no key configured at all. Reuters and Bloomberg are
absent because neither publishes usable RSS, not by oversight.

`pub_ts` is **naive UTC** on both news paths. The frontend does
`new Date(pub_ts + 'Z')`, so a tz-aware value is double-offset. Two traps in
`_feed_datetime`, both measured: the tag is not always `<pubDate>` (Bank of Canada
uses `<date>`), and the parser cannot be chosen from the tag name (Yahoo puts
ISO-8601 inside a `pubDate` tag). ECB is the feed that punishes a mistake here —
it publishes `+0200`, so unconverted its releases land in the future and pin
themselves to the top of a recency-ranked feed.

**Macro keywords need `\b` on both sides.** A `\bwar` prefix match tagged
"embassies **warn** americans" and "cyber **warnings**" as war coverage. `_kw_re`
anchors both ends and orders alternatives longest-first so `rate cut` beats
`rate`. Related: `default` cannot be a keyword at all — it is a path segment in
AP's image URLs, which scored every AP sports story as a sovereign default.

**`<description>` is not plain text.** AP ships an `<img>` tag inside it, so
`_parse_feed` unescapes *then* strips markup. Skipping the strip puts raw tags in
the payload and feeds URL text to the scorer.

**A thin build raises rather than returning.** `_TtlCache` caches whatever the
fetcher returns, including `[]`, so `_fetch_market_news` raises when fewer than 3
feeds succeeded or under 10 items survived — same distinction `_fetch_div_events`
keeps. `_build_market_news` then serves `_market_last_good` flagged `stale`, so a
transient blip degrades instead of pinning an empty page for the TTL.

**Broad wires are gated on macro vocabulary.** AP and BBC World carry sport, box
office and human interest in the same feed as the geopolitics we want, and
neither exposes a filterable URL path (AP is `/article/<slug>-<hash>`, BBC is an
opaque id), so `_SKIP_PATH_RE` only reaches Guardian-style URLs. Without the gate
the feed was 58 of 80 items cycling results and MLB trades. Financial and policy
feeds are exempt.

**Relevance filtering is two layers, and neither may empty the page.** Keyword
scoring ranks what is *already* market news; it cannot separate "Boeing clears
key hurdle and stock rallies" from "Why flights are so expensive". Measured on a
live build, ~30 of the 55 items in the `markets` catch-all were advice columns,
pundit picks, rating boilerplate or human interest.

`_NEWS_BOILERPLATE_RE` and `_NEWS_ADVICE_RE` take the unambiguous cases
(`Analyst Report:`, `Form 144`, first-person money stories) and work with no API
key. Keep them narrow — anything arguable is the LLM's job, and a greedy regex
here silently eats real news. `_filter_relevant` then judges the rest in one
batched call and **degrades rather than drops**: no key, a failed call, or a
response that would cut more than 7/8 of the page all fall back to keeping the
items. A filter that empties the feed on an outage is worse than no filter. It
also reports `regex` rather than the model name when no batch succeeded, so the
UI cannot claim a pass that never happened.

`_llm_verdicts` memoises by headline. Without it a rebuild every 5 minutes
re-pays for the same items and borderline stories flicker in and out as the model
decides them differently.

**The thinness guard runs before the filter.** It is a statement about the
*feeds* — an LLM legitimately dropping half the page is not an upstream failure
and must not be cached as `stale`.

**`_resolve_api_key()` reads settings first, environment second, at call time.**
DeepSeek is preferred over Groq because it judges the borderline headlines
better; both APIs are OpenAI-compatible so one request shape drives either. Do
not bind a key at import the way `_groq` does (app.py:16) — that is exactly why a
key saved through `POST /api/settings` never reaches `groq_call()`. Resolving per
call means pasting a key into Settings takes effect on the next build with no
restart.

**There is one LLM path, and it is hosted.** `/api/chat` and its local Ollama
backend are gone — a second integration with a different shape (an unversioned
model pulled by `setup.bat`, a key that was an environment variable rather than
an account's, an error string telling the user to run `ollama serve`) was the
"pick one path" gap, and the path picked is `_resolve_api_key()` above. Anything
new that wants a model asks for a backend through `_llm_backend(owner)` so it
degrades the same way everything else does when an account has no key.

**The positions feed uses `_build_news(..., lite=True)`.** That exit returns after
filtering and scoring — one yfinance call — instead of scraping ten article bodies
and paying for a Groq rewrite per item. Across nine symbols the full path would be
~135 threads and ~100 LLM calls under one 25s deadline; lite measures 1.24s cold,
0.008s warm. `_portfolio_symbols()` still runs every stored symbol through
`clean_ticker()` even though the write routes validate now — the JSON files sit
in the working directory and get hand-edited, so they stay untrusted input.

**There is one page-hide site.** `_hideAllPages()`. `fetchStock` used to inline
its own copy which had already drifted (it never hid `settings-page`, so searching
a ticker from Settings left it on screen). Adding a page means touching that one
function.

**A canvas hit test is in CSS pixels, and there is one of them.**
`makeBarDragger()` is the press-sweep-read-back mechanic behind both drag
plugins — the average on the earnings and FCF charts, the average YoY growth on
the dividend chart. It used to be a copy inside each, and the copy is where the
bug lived.

`bar.x` is a CSS pixel offset. Chart.js sizes the backing store at
`canvas.width = CSS width x devicePixelRatio` and scales the context to match,
so scaling the pointer by `canvas.width / rect.width` — which both copies did —
measures the cursor in device pixels and compares it against bars in CSS pixels.
The selection lands `devicePixelRatio` times too far right and drifts further
the further right you press. At 150% Windows display scaling, the default on
most laptops, pressing 2018 on a twelve-year dividend chart selected 2020 and
the right-hand third of the chart could not be reached at all. **At dpr 1 the
factor is 1 and it is exact**, which is why it looked correct wherever it was
written and only broke on the machines it shipped to. Verified across 78
press/sweep pairs at each of dpr 1, 1.25, 1.5 and 2.

**A plugin that adds DOM listeners must remove them in `afterDestroy`.** The
same fix's other half. These listeners sit on the canvas, and the canvas is a
fixed element `makeScrollableChart()` looks up by id and reuses — so `destroy()`
does not take them with it. Every ticker search left another set attached, each
closed over a destroyed chart and throwing out of `chart.update()` on every
mousemove of every later drag, and each one pinning its dead chart in memory.
The live handler is registered last and still ran, so the feature kept working
while the console filled up — which is why this survived. Chart.js does fire
`afterDestroy`; `detach()` is called from there.

**A corner radius is a token, the same way a colour is.** `--r-sm` / `--r` /
`--r-lg` / `--r-pill` — chip, control, container, bar — and no rule anywhere
writes a literal. The tokens already existed and exactly one rule read them, so
the file had drifted to 2px, 3px and 4px chosen per site: three buttons doing
the same job at three radii, and nothing for a new one to copy. The scale is
`login.html`'s, because that view was written later and was already rounded —
you signed in through a 10px card and landed on square panels.

Two things move with the radius rather than after it. `.grid`'s gap went 2px →
8px: at 2px the metric boxes read as one slab ruled into cells, which is why
square corners suited them, and rounding at that spacing only punches four
notches of page background into every junction. And `.table-box` gained
`overflow: hidden`, because it is full of children that paint their own
background into the corner — the sticky `<th>` strip, `.eh-header`,
`.ocf-note`, the first and last news row — each of which squares the box off
again. The clip is safe for the sticky headers: `.table-scroll` is still the
nearest scrollport, so they keep sticking to it.

Grouped controls round on the outside only, or the seam stops reading as a
seam: `.search-input`/`.search-btn` and the `.settings-input-wrap` pair each
take half a radius. `.search-input.open` also drops its bottom-left, since open
it is the top half of one shape with the suggestions dropdown.

The clip has a cost that only shows on a narrow viewport: a table wider than
its box is not merely cramped, it is cut off, because there is nowhere to
scroll to. `.table-scroll.wide` adds `overflow-x` for that case. The users list
is what surfaced it — five columns at 561px inside a 319px box, with Delete off
the end of the page and unreachable.

**Type is a scale, and caps are furniture.** `--fs-0`…`--fs-7` live in the
token block and `--fs-0` (10px) is the floor: nothing renders below it. Before
the scale existed the whole app sat at 9–13px — the portfolio page's own total
was 18px in a page holding 122 nine-pixel badges — and hierarchy was attempted
with weight, uppercase and letterspacing alone, at nine distinct tracking
values. That is why every page read as a flat wall of small bold labels.
Two voices now: numbers are data and read Inter with `tabular-nums` (columns
must not jiggle); Jakarta is identity — wordmark, tickers, panel titles,
buttons. Uppercase + tracking survives in exactly three shapes — section
labels, table headers, status pills — and a control says what it does in
sentence case. A new label that wants to shout should get a size step, not a
letter-spacing.

**The header is one sticky app bar, and ids are the JS contract.** Brand, nav,
search and the settings gear share `.appbar`, sticky below the ticker tape.
The JS binds `#logoMark`, `#homeLink`, `#mainnav`, `#tickerInput`,
`#suggestions`, `#searchBtn`, `#settingsBtn` and never the layout classes, so
the bar can be recomposed freely as long as those ids survive. `#logoMark`
(theme toggle) and `#homeLink` (go home) must stay siblings — nesting one in
the other makes a theme toggle also navigate.

**A chart panel is a theme surface, and a canvas cannot resolve `var()`.**
`.hero-chart-wrap` was a hardcoded white card in the dark page, styled around
Chart.js defaults. It is `--surface` now, and the price chart reads
`--chart-grid` and `--muted` through `_cssVar()` at build time — same rule the
doughnut's `pieColors()` already followed, and build time matters because the
theme can toggle between two builds. A canvas gradient must fade to its own
hue at alpha 0, not to transparent white: canvas interpolates unpremultiplied,
so `rgba(255,255,255,0)` washes the fill milky on a dark panel.

**Settings uses the page's own furniture.** It used to draw its own: a section
heading at 0.18em against the `.section-label` every other page uses at 0.3em,
fields laid straight onto the page background where everything else lives in a
`--surface` panel at `--r-lg`, and a users list hand-built from flex rows with
inline styles instead of the `<table>` that gets sticky headers and hover for
free. It read as a different application bolted on, and a new field had two
conflicting things to copy. Headers are `.section-label` + `.section-rule`,
bodies are `.settings-panel`, and the only thing left in the settings CSS block
is form layout.

`.settings-btn` is one shape with three weights — `.primary` for the single
action a panel exists for, plain for everything else, `.danger` for destructive
— because the old `settings-reveal-btn` was Show, Sign Out, Disable *and*
Delete, which gave "delete this account permanently" exactly the weight of
"reveal this field".

An API key's state is a pill and a masked line inside its card, not the input's
placeholder. Placeholder colour is hint colour, so a configured key looked like
an empty field; and a placeholder disappears the moment you type, which is
precisely when you want to see what you are replacing.

## Important quirks
- yfinance dividendYield is already a % (0.41 = 0.41%, not 0.41%)
- ...but `shortPercentOfFloat` and the `growth` column of the estimate frames
  are **decimals** (0.01 = 1%, 0.2054 = 20.54%) — the opposite convention.
  Yahoo also omits `shortPercentOfFloat` for most non-US listings, so
  `_build_short_interest` recomputes it from sharesShort / floatShares
- **`floatShares` and `sharesOutstanding` are not always the same share.** On a
  depositary receipt Yahoo counts the float in *ordinary shares* and everything
  else in receipts, so the recompute above mixes units — ASML would read 0.006%
  where 0.33% of the receipts are short. A float above the share count is the
  tell (impossible on one basis); `_FLOAT_BASIS_MAX` catches it and the
  percentage goes to None, suppressed rather than converted since the deposit
  ratio is nowhere in the payload. Yahoo's own `shortPercentOfFloat` is already
  on the receipt basis and is left alone, as is `pct_of_outstanding`
- Macrotrends units differ per metric: `net-income` and `shares-outstanding` are
  in **billions**, FCF and capex in **millions**, margin and EPS in their own
  units. Same endpoint, same field name, different scale — `_MT_SERIES` holds it.
  The share count is also quantised to the nearest million, which turns a
  small-cap series into flat runs of identical values; `_MT_SHARES_MIN` drops a
  series that coarse rather than charting "no change" where the count moved.
  A Macrotrends year never overwrites a yfinance year and is tagged `src: 'mt'`
- Interest coverage is an income-statement figure, so it carries its own
  `interest_coverage_year` rather than the balance sheet's `as_of`
- FCF comes from cashflow "Free Cash Flow" row, not manual calculation
- A Macrotrends value is in var chartData → field **v2**, never v1. See below
- Tickers with "." in them skip Macrotrends (non-US)
- `_tape_cache` shared between ticker tape and movers
- `format_large_number()` for M/B/T formatting — carries the sign outside the `$`
  so a negative scales correctly (`-$4.50B`, not `$-4500.00M`)
- Earnings must not be wrapped in `abs()`: a loss-making year has to render
  negative
- `/api/stock` is cached for `STOCK_TTL` (10 min). Concurrent hits on the same
  ticker share one fetch. The live quote is refreshed on the way out of the
  cache, keeping `price` a 2dp **string** and `day_change_pct` a rounded float —
  a cached payload must be indistinguishable from a fresh one.
- `_do_get_stock` calls `_prefetch_ticker()` first, which warms every property
  in `_PREFETCH_PROPS` concurrently. yfinance memoises per Ticker instance, so
  the body below then reads warm attributes instead of paying for one sequential
  round trip each (~2.8x faster cold). **If you read a new `ticker.<prop>` in
  that function, add it to `_PREFETCH_PROPS`** or it silently reverts to a
  sequential fetch — there is a test that enforces this.
- `/api/news` resolves the company name itself via `_resolve_company_name()`
  (warm stock cache first, then one `info` call). Do not make the frontend pass
  it: that coupling is what forced news to wait for `/api/stock` to finish.
  The name is not optional — it drives the headline relevance filter, and
  defaulting it to the symbol drops every article that doesn't contain the
  literal ticker string. Cached for `NEWS_TTL` (5 min); empty results are
  deliberately not cached.
- Insider classification: match against the ordered `_INSIDER_RULES`, never
  `'Purchase' in text`. Yahoo's Canadian phrasing is "Disposition in the public
  market"; "Disposition under a purchase/ownership plan" is a *sale* containing
  the word "purchase"; buybacks read "Redemption, retraction, cancelation,
  repurchase"; ~half of all rows have a blank description and are RSU vesting.
  Excluded rows are counted and reported to the UI, not silently dropped.

## Known gaps
- Chart.js loads from cdnjs without an `integrity` hash, so charts need network
  on first paint. Vendoring both files under `static/vendor/` would fix the
  offline case and remove the third-party runtime dependency.
- `_do_get_stock` is a single ~570-line function. `stock-intel` already
  implements the same data layer correctly and with tests; migrating the route
  onto it needs a field-by-field mapping against the frontend's ~50 reads.
- Portfolio data (holdings/sales/transactions/watchlist) is committed to git.
  Fine while the repo is private; move it out before sharing. `users.json` and
  `settings.json` (at every level, including `users/<name>/settings.json`) and
  `app_secret.json` are gitignored and must stay that way — between them they
  hold the password hashes, every account's API keys, and the session signing
  key. A leaked `SECRET_KEY` lets anyone mint a valid cookie for any account.
- There is no password reset by email and no MFA. `python app.py passwd <user>`
  from the machine itself is the whole recovery story, which is proportionate
  while this binds to loopback and would not be if it were ever exposed.
- Rate-limit buckets live in process memory, so they reset on restart and are
  not shared between workers. Fine for one Flask process on loopback; a real
  multi-worker deployment would divide every limit by the worker count and hand
  a restart loop a way to clear them. Redis or an equivalent shared counter is
  the fix, and `_rate_consume()` is the one function that would change.
  `_report_jobs` has the same property, which is why `/api/report-status` for an
  unknown job is already a 404 rather than an error.
