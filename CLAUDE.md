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
ticker into its own output directory, and `_run_report` passes the requester's
theses in through `STOCKBOX_THESES` — so serving that shared path handed one
user's private notes to whoever asked for the same ticker next. The PDF is
copied to `users/<name>/reports/` on completion and `/api/report-file` serves
only that copy. `_report_jobs` entries carry an `owner` and answer 404 to anyone
else: a uuid4 job id is unguessable, but unguessable is not an access check, and
the error `detail` is a build log that can quote the theses.

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
it. Macrotrends serves fourteen, which is why it exists here at all — don't spend
time trying to coax more years out of yfinance.

**A scraped series is taken or dropped whole, never spliced.** `_mt_check`
compares Macrotrends against yfinance on the overlapping years and
`_merge_macrotrends` gap-fills only if it passes. The tolerance is deliberately
loose (10%, one divergent year forgiven when there are three or more to compare)
because yfinance carries restatements where Macrotrends is as-reported: this is a
check for gross errors — wrong company, thousandfold unit slip, split-adjusted
pasted onto as-filed, the v1 year shift — not a reconciliation. Measured over a
15-ticker basket it accepts 44 of 45 series and adds ten years to each. The one
rejection is Amazon's FCF, where the two sources net capital leases differently
on all four overlap years; splicing those would put a definitional step change
mid-chart that reads as a real swing in the business. A rejection is logged, not
silent, because a dropped series looks exactly like a ticker Macrotrends doesn't
carry.

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

## Important quirks
- yfinance dividendYield is already a % (0.41 = 0.41%, not 0.41%)
- ...but `shortPercentOfFloat` and the `growth` column of the estimate frames
  are **decimals** (0.01 = 1%, 0.2054 = 20.54%) — the opposite convention.
  Yahoo also omits `shortPercentOfFloat` for most non-US listings, so
  `_build_short_interest` recomputes it from sharesShort / floatShares
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
- `/api/chat` expects a local Ollama server (`OLLAMA_MODEL`, default
  `qwen2-math:7b`) which is not in requirements.txt, while the app also holds
  Groq and Anthropic keys. Pick one path.
- Portfolio data (holdings/sales/transactions/watchlist) is committed to git.
  Fine while the repo is private; move it out before sharing. `users.json` and
  `settings.json` (at every level, including `users/<name>/settings.json`) and
  `app_secret.json` are gitignored and must stay that way — between them they
  hold the password hashes, every account's API keys, and the session signing
  key. A leaked `SECRET_KEY` lets anyone mint a valid cookie for any account.
- There is no password reset by email and no MFA. `python app.py passwd <user>`
  from the machine itself is the whole recovery story, which is proportionate
  while this binds to loopback and would not be if it were ever exposed.
