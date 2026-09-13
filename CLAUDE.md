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
- ../Forward Guide/ — sibling project behind the Guidance button. Run as a
  subprocess (`report.py --tickers X --fetch --brief --json <path>`), never
  imported, so its dependencies stay out of this app's import graph. Its
  `--push` flag is deliberately unused here; see the guidance invariants below.
  `FORWARD_GUIDE_DIR` overrides the location. The subprocess runs on
  `sys.executable`, so its `requirements.txt` (only `anthropic` is not already
  here) has to be installed into this app's venv wherever it is deployed. On the
  live box it sits at `mckechnie-terminal/forward-guide` — inside the app
  directory, because the service unit's `ReadWritePaths` covers that and not a
  new sibling, and Forward Guide writes its filing cache under its own `data/`.
  Ship it without `.env` (keys come from the account, via `_account_env`) and
  without `data/` (a re-derivable cache).

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

```
python app.py guest enable    # "Continue as guest" on the login page, seeded demo portfolio
python app.py guest disable   # hides the button and signs out every live guest
python app.py guest reset     # puts the demo portfolio back
python app.py guest status
```

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

**Guest access is one shared account, and read-only is a property of the gate.**
`python app.py guest enable` creates a user record named `guest` with role
`guest` and **no password hash**, so the password form can never open it —
`_verify_password(None, ...)` is False, one message, same timing — and the only
way in is `POST /api/auth/guest`, the fourth entry in `_PUBLIC_ENDPOINTS`, which
starts a session while the record is enabled and answers 404 otherwise. The
login page renders the "Continue as guest" button only when `_guest_enabled()`,
so a deployment without one shows the form and nothing else. The switch is the
existing `disabled` flag: the Admin tab's Enable/Disable is the on/off control,
and disabling bumps `token_version`, which ends every live guest session the
same way it does for anyone else. `admin_update_user` refuses a role change or
a password reset on it — a promoted guest is a passwordless account with write
access, and a guest with a password is a second door into the shared demo.

A guest reads everything in its own directory and writes none of it, and that
rule lives in `_require_login` — after the CSRF check, before the route — as
`_GUEST_WRITABLE`, a set holding `api_logout` alone. Same shape and same
argument as `_PUBLIC_ENDPOINTS`: there are ~40 mutating routes, several
strangers share the account at once, and the route you forgot to mark is the
one that lets a visitor rewrite the demo everyone else is looking at or paste an
API key that every later visitor would spend. The refusal is a 403 carrying
`guest: true`, which the `fetch` wrapper turns into a status-line notice for the
same reason it surfaces 401 and 429; the template also drops the password form
and the key cards for a guest (`{% if is_guest %}`), which is not the check but
a door not shipped. `tests/test_guest.py` walks the url_map and asserts every
non-GET `/api/` route refuses a guest.

The guest's portfolio is `_GUEST_LEDGER`, invented trades on real Canadian
listings, written by `_seed_guest_portfolio()` into `users/guest/` as
`transactions.json` and `watchlist.json` only — holdings and sales come from
the two rebuilds, which take `owner=` for this (the seed runs from the CLI, with
no request). `enable` seeds only an empty ledger; `reset` returns the directory
to exactly the seed — stray per-user files and any `reports/` removed, then the
four seed files rewritten. It has no `settings.json`, so every LLM path degrades
the way an account with no key already does, and Report and Guidance are POSTs
the gate refuses before a subprocess starts.

**Every guest login lands on the demo book, and on a healthy server that is a
read.** `api_guest_login` calls `_guest_portfolio_drifted()` — four small reads
compared against what the seed produces, plus a check for files the seed never
writes — and reseeds with `force=True` only when they differ. The gate already
makes a guest write impossible, so this fires for what the gate cannot see: a
file edited by hand on the box, a corrupt file (a read that raises counts as
drift), or a mutating route some later change lets through. It is deliberately
*not* an unconditional rewrite on login: several reviewers share the account at
once, and rewriting four files under a concurrent reader for no reason is a
blank panel waiting to happen. A test pins the mtime of an untouched ledger
across a login.

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

**A block is a discovery filter, and the ledger is on the other side of that
line.** `blocked.json` hides a stock from every route that *offers* one you did
not ask for by name — the movers strip, the ticker tape, the market browser, the
typeahead, the Canadian screener, the positions news feed, the exchange
switcher, the watchlist — and `/api/stock` answers **403** for one, because the
detail page is the single surface a symbol typed in full would otherwise walk
straight to. `/api/news` refuses independently rather than relying on that: it
fires in parallel with `/api/stock`, so leaning on the other route's refusal
would still spend the account's API key scraping ten articles about a hidden
company on every search.

What it must never reach is `holdings`, `transactions`, `sales`, `options` or
anything derived from them. `realized + unrealized + dividends + option P/L ==
(cash_pool + market value) − invested` holds to a penny, and it holds because
nothing filters those walks. A hidden position dropped from `_compute_invested()`
would not hide a stock — it would report a return computed over capital the book
no longer admits to having deployed, which is the failure here that looks most
like working code. So a hidden holding keeps counting, and `POST /api/blocked`
returns `held` so the UI can say so rather than leave the user to notice that
Holdings still lists what they just hid. A test asserts the whole ledger
projection is byte-identical across a block.

Nothing is deleted, anywhere. The watchlist GET *filters*; the stored row keeps
its name and its added date, so unhiding restores the entry rather than a blank.
That is what makes the control safe to reach for, and it is why the watchlist
POST answers 409 for a hidden symbol — a write the GET would filter straight
back out is an Add button that silently does nothing.

The caches this filters — `_movers_cache`, `_tape_cache`, `_index_payload_cache`
— are **shared market data built once for everybody**, so the filtering happens
per reader on the way out of the route, never in the builder. `_blocked_set()`
returns empty rather than raising outside a request for exactly that reason: a
background builder has no owner and nothing to hide. Filtering happens *before*
the slice on movers and before the batch arithmetic on the tape, so hiding three
names does not leave a 22-row "top 25" or a `has_more` that describes a payload
this account will never be sent. `/api/index` sends `hidden` and subtracts it
from `members`, because the frontend derives "unpriced" as `members − funds −
rows` — leave `members` at the universe's size and every hidden name is reported
as one Yahoo would not price, which is a different and untrue claim about the
data.

Matching is **exact, on the full symbol** (`_row_symbol`: `full_ticker` first,
`ticker` only as a fallback). `ticker` on a movers or index row is the display
spelling — `SHOP` for SHOP.TO — so keying on it would hide a different company's
listing on another venue. A root rule reads as helpful right up until NA.TO
takes National Bank's NA with it, and a filter that hides more than it was asked
to is indistinguishable from a bug.

**A report belongs to the account that built it.** StockBox writes one PDF per
ticker into its own output directory, so two accounts building AAPL overwrite
each other's file and serving that shared path hands back whichever build
finished most recently. The PDF is copied to `users/<name>/reports/` on
completion and `/api/report-file` serves only that copy. `_report_jobs` entries
carry an `owner` and answer 404 to anyone else: a uuid4 job id is unguessable,
but unguessable is not an access check, and the error `detail` is a build log
from another account's run.

**Forward guidance is opt-in per ticker, and the read half is what makes that
affordable.** Forward Guide is a sibling project (`../Forward Guide`) that reads
a company's earnings 8-K press release — and, when the guidance was only ever
spoken, its earnings call — and returns what the company said it expects plus
the earnings, margin and cash figures those statements imply. It runs the same
way StockBox does: `_run_guidance` on a worker thread, a subprocess, this
account's keys, bounded by `GUIDANCE_MAX_CONCURRENT` rather than by a rate
limit, for the reason `REPORT_MAX_CONCURRENT` documents.

The trigger is the button and nothing else. Every other panel on the stock page
is filled from the one `/api/stock` payload a lookup already pays for; this one
costs an LLM call, and the figures move once a quarter on a filing — so a page
load draws no guidance and asks the server nothing about it. `GET
/api/forward_guidance` reads what is stored and **starts nothing**, which is
what lets the first press be free for a ticker already looked up; only
`POST .../run` spends. Keep that split. A read that could start a run turns
every press into a purchase of an answer the account already owns.

**The Terminal owns the write, not the subprocess.** `report.py --push` writes
one payload over the account's whole file, which is right for a batch run
across the portfolio and wrong here: the second ticker looked up would erase the
first. The run writes to a temp file (`--json`, added for this) and
`_merge_guidance` folds it in through `_user_store(...).mutate()` — the lock has
to span the read and the write, and a foreign process replacing the file cannot
hold it.

**An entry is keyed by the symbol asked about, never the filer.** You search
`MSFT.TO`; Forward Guide answers about `MSFT`. Keyed on the filer the page would
never find its own answer, so the resolution is recorded *inside* the entry and
shown ("filed by MSFT"). `_migrate_forward_guidance` folds the pre-button batch
payload into this shape rather than discarding it — that is guidance already
extracted and already paid for — and it is the one thing that cannot recover the
mapping, since the legacy payload records no resolution. Those rows land under
the filer and re-run on first press, which is cheap because the extraction
itself is cached upstream.

**`_account_env` sets every key in `_SETTINGS_KEYS` and deletes none**, and it
is the one environment builder for both subprocess launchers — the guidance run
and `_run_report`. Both halves matter and neither is ceremony. Set
unconditionally, because `ANTHROPIC_API_KEY` exported in the shell that started
the server would otherwise be one key billed to every account: `_run_report`
filled its environment with `if _k not in env` until this was made shared, so an
exported key beat the account's own and one person paid for everybody's reports.
And named-with-an-empty-value rather than absent, because Forward Guide loads a
`.env` beside itself and skips any key already in the environment: an empty one
reads as "none configured", where an absent one falls through to that file and
spends a shared key on behalf of an account that configured nothing. The user's
existing `.env` Alpha Vantage key is therefore *not* used; it belongs in
Settings now. Everything **outside** `_SETTINGS_KEYS` is inherited untouched,
which is what `STOCKBOX_THESES`, `MAX_FILING_CHARS` and `SEDAR_HEADED` need —
they configure a run rather than pay for one, so they are nobody's credential to
leak. A new launcher builds its environment through this helper; it is defined
up in the settings block beside `_SETTINGS_KEYS` rather than next to either
caller, because it belongs to the keys and not to the feature.

**The run route refuses a hidden symbol independently**, like `/api/news` and
for the same reason — it is reachable on its own and it spends the account's
key, so leaning on `/api/stock`'s 403 would still pay to extract guidance for a
company this account has hidden.

**A guidance figure says whether the company gave it.** `derived_*` fields are
reconstructions — a derived EPS is exactly the number a reader would otherwise
quote back as the filer's own — so the guided value wins wherever both exist and
the tile is marked and dashed when it does not. The listed lines are what was
actually said, rather than a fixed revenue/margin/EPS grid: a retailer guides
operating income in dollars and comparable sales in words and never mentions
either, so a complete outlook renders as a row of dashes against those columns.
And the value's **unit decides its shape, never the metric name** — the same
rule `NormalizedItem.is_rate` enforces upstream, since "organic revenue +2 to
4%" is named as revenue and quoted as a rate.

**A fiscal label is dated against the filing, never against the year-end month
alone.** `FY2027` does not say what is being projected, and the two ways of
reading it are twelve months apart. Most filers name a fiscal year for the
calendar year it *ends* in — Microsoft's FY2027 is Jul 2026 – Jun 2027 — but
retailers name it for the year it *begins*: Lululemon's FY2026 runs Feb 2026 –
Jan 2027. **NVIDIA and Lululemon both close in January and label it opposite
ways**, so no rule keyed on the year-end month can separate them, and a guess
prints a real-looking span that is a year wrong with nothing on screen to give
it away — the failure here that looks most like working code.

The filing separates them, because a company guides a period that has not
finished yet: of the two candidate spans `fgPeriodSpan` takes the earlier that
still ends on or after the filing month. That resolves NVDA (Q4 FY2026 guided
Nov 2025 — the near candidate ends Jan 2026) and LULU (Q2 FY2026 guided Jun 2026
— the near candidate ended Jul 2025, so it is the far one) without either being
told its own convention. December filers are exempt and date with no filing
date, since the fiscal year *is* the calendar year.

Everything else — an `OTHER` period like "over the next 5 years", an offset
filer with no filing date, guidance reaching more than a year past the filing —
renders the fiscal label with **no span**. That is what the panel looked like
before this existed; a wrong span reads as a figure that was looked up.

The month comes from `_fye_month_opt()` on the `/api/stock` payload — free,
since `financials` is already in `_PREFETCH_PROPS` and yfinance memoises it. It
is **not** `_fye_month()`, which defaults to December: harmless in the chart
caption that is its only other consumer, and a year wrong for every offset filer
here. `_fyeMonth` is reset from every stock payload, because carrying one
filer's calendar onto another's guidance is the cross-currency trap in a new
place.

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
binding bug, which went unnoticed for exactly that reason. `_run_report` and
`_run_guidance` take `owner` for the same reason, and both build the subprocess
environment through `_account_env(owner)` rather than each doing it by hand —
which is how one of them drifted into letting an exported key win.

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
gain: the file had drifted by thousands of dollars (six phantom rows, four of
them "sold at $1.00") and the Performance tab reported a four-figure realized
loss while every other tab showed a four-figure gain. If you add a route that writes `transactions.json`, it must call **both**
rebuilds.

`date_acquired` is the one field on a sale row that the ledger cannot recompute —
it sets the dividend window in `/api/transactions`. The rebuild carries it across
by `txn_id`, so stamp that on any new sale row.

**A transaction id is a string, and parsing it must never drop the row.**
`_new_txn_id()` returns `'<ms>-<hex>'`; the random suffix was added because a
bare millisecond stamp collided. `/api/portfolio/invested` and
`/api/holdings/chart` both parsed it with `int()` inside a bare
`except Exception: pass`, so every transaction recorded since that change
vanished from their walks — three buys at the time, and all of them going
forward. The shares still reached `holdings.json` (rebuilt by a
different path), so their market value counted toward funds while the capital
that bought them never counted toward `invested`: the Holdings tab reported the
difference as profit — more than four points of return that did not exist.
Sort on the string; the
ms-timestamp prefix makes lexicographic order match numeric, which is what
`rebuild_holdings_from_transactions()` already relies on.

**A dividend whose pay date equals its ex date must still be credited.**
yfinance reports one date per distribution, so `_build_div_events` falls back to
`pay_date = ex_date`. In `/api/portfolio/invested` the pay event sorted at
priority 0 and the ex-date snapshot it reads at priority 2, so on a shared date
the credit ran first, missed the snapshot and defaulted to **0 shares** —
dropping the distribution outright. T.TO, UNH.TO and ZMMK.TO have `pay == ex` on
every distribution and credited exactly nothing. The pay event now
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
it makes — the "invested" figure came to nearly three times the external capital
the book had actually taken in, with a money-market parking round-trip alone
accounting for 23% of it. The two tabs disagreed by more than ten points of
return for one portfolio on one day.

`_compute_invested()` is now the single source of that number and rides on the
`/api/portfolio/performance` payload, so the Performance tab cannot drift from
Holdings by fetching its own. Per *position* the cost basis is still the right
denominator — it is the capital that position consumed — so the per-box
percentages keep using it; only the summary divides by external capital.

Performance also counts dividends now (`_dividends_received_by_ticker()`), which
Holdings always did. Income changes the sign on real positions: a Telus position
that reads a small loss on price alone reads a small gain once a year of
dividends is counted.

`_dividend_payments()` is the one walk behind both the Dividends tab and this,
for the same reason `sales.json` is not allowed a second opinion on P/L.

**The TWR tile is chain-linked daily, and Modified Dietz is not a substitute.**
`/api/holdings/chart` labelled a Dietz number "time-weighted". They answer
different questions: money-weighted asks what the *investor* earned including
the effect of when capital arrived, time-weighted strips that out and asks what
the *holdings* did. On a nine-month book with steady contributions they read
about two points apart; the gap grows without bound as contributions grow against the opening balance
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
days raised a 29-day return to the 12.6th power and printed a near-doubling as
the largest number on the page.

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

Both tabs now read the same figure on the same book, and
`realized + unrealized + dividends + option P/L == (cash_pool + market value) −
invested` holds to a penny. `cash.json` is dead and can be deleted.

**Escape anything from outside.** News headlines, insider names and company
names are scraped third-party text — some of it LLM-rewritten — and go into
`innerHTML`. Use `escHtml()`; use `safeUrl()` for any href.

**Send both `raw` and `value`.** `raw` is the true float, `value` the display
string. Charts must read `raw`; parsing the formatted string back into a number
quantises every data point to 2dp of its unit.

**Margin of safety compares two equity totals, and a share count is not allowed
between them.** `MoS = (equity_value − market_cap) / equity_value` on the
valuation panel. It used to be `(equity/shares − price) / (equity/shares)`, which
put Yahoo's `sharesOutstanding` between the model and the answer, and that input
is wrong in two independent ways.

*It was quantised on the way in.* Every field on that panel is denominated in
millions and every one was prefilled with `(raw / 1e6).toFixed(0)` — the nearest
million. Invisible on a mega-cap and ruinous below one: NVR's 2,678,153 shares
became 3,000,000 and moved its MoS 7.8 points (+27.0% → +34.8%), MKL's
12,389,958 became 12,000,000. Under 500,000 it rounds to a flat **zero**, which
the calculator reads as an empty field — so a small-cap's share count, FCF or net
cash silently vanished and the panel asked the user to fill in something it had
just filled in itself. `toMillions()` is the one prefill site now; nothing on this
panel rounds to a million.

*And on a dual-class issuer it is not the same figure the cap is built from, so
no precision fixes it.* Yahoo reports BRK-B's 1,408,035,161 B shares against a
`marketCap` covering both classes. `shares × price` came to $718.1B against a
reported $1,091.8B — measured, a 34.2% gap landing straight on the answer. At the
default rates the old panel printed **+5.7%** margin of safety on Berkshire where
the honest number is **−43.3%**: not merely off, the opposite sign, which is the
failure here that looks most like working code.

Market cap is reported directly, so it is what the payload carries
(`market_cap_raw`, per the raw/value rule above) and what MoS is computed
against. The per-share line is then derived back out of the *same two market
facts* — implied shares are `cap / price` — so `(ivps − price)/ivps` and
`(equity − mcap)/equity` are identically equal and the two readouts cannot drift
apart. On a single-class name this changes nothing at all: AAPL reads −31.4%
before and after. That is the point — it is not a different model, it is the same
model with an input that is not reconstructed.

**Except where Yahoo reports no cap at all, and then the share count is the only
input there is.** The same stable minority the index tables already back-fill —
Home Depot, Exxon, Salesforce, American Eagle — is missing `marketCap` from
`info` and from the v7 quote endpoint alike, with `sharesOutstanding` missing
beside it, so nothing in the response derives one. `/api/stock` had no
equivalent of `_index_backfill_caps` and simply printed `N/A`, which is worse on
the detail page than in a table: MoS divides by the cap, so the whole valuation
panel went blank rather than merely losing a column. `_do_get_stock` now falls
back to fast_info's count times the price — the definition, and the same figure
`_index_backfill_caps` was measured to reproduce to a ratio of 1.0000 on the 116
names reporting both.

It is a fallback and never a correction: the derivation runs only when
`market_cap_raw is None`, because a share count is not always the count the cap
is built from — BRK-B's covers the B class alone and lands 34% under the
reported figure, which is the whole reason MoS reads a cap in the first place.
Verified live: AAPL and BRK-B both keep Yahoo's own cap, AEO gains one.

The share count behind it is resolved **once**, at the top of `_do_get_stock`,
preferring `info` over fast_info for the same reason. That expression used to be
written out three times — the payout-ratio walk, the TTM earnings derivation and
the payload — each reading `info` directly, so one missing field quietly took
four unrelated figures with it (market cap, `price_to_tangible_book`, every
payout ratio, and the TTM earnings card, which then also stopped agreeing with
the EPS card it exists to match) and there was no single place to fix it.

Two consequences. **`_refresh_quote` must rescale the cap with the price.** An
entry is servable for `STOCK_STALE_TTL` (an hour), so refreshing the price and
leaving the cap behind puts a live price beside an hour-old cap and biases MoS by
whatever the stock did in between. Cap is price × shares and the count does not
move intraday, so the rescale is exact rather than an approximation.

And **the price field rescales the cap rather than sharing `calcDCF`'s
listener.** Neither leg of MoS carries a price, so a price wired straight to
`calcDCF` moves the per-share line and leaves MoS sitting exactly where it was —
a dead control on the one field someone reaches for to ask "what if it were
cheaper". A price move is a cap move and the share count is what stays put, so
the handler scales the cap by the same ratio against a tracked baseline.
`fillCalculators` seeds that baseline via `setDCFPriceBase()`, because assigning
`.value` fires no input event and the first hand-typed price would otherwise be
measured against whatever the previously viewed ticker traded at.

Not fixed, and pre-existing: the equity value is built from FCF and net debt,
which are **filing**-currency figures, while cap and price are the **listing's**.
For an ADR those differ, so the whole panel is a cross-currency ratio of the kind
suppressed everywhere else on this page. It was equally wrong before this change.

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

**Two callers wanting the same GET is one request.** `apiGet(url, {ttl})` is the
browser-side twin of `_stock_inflight`: concurrent callers share one promise, and
a settled entry answers later callers outright for `ttl`. It is keyed by URL, so
it can only ever join requests that are the same question, and it is GET-only —
two POSTs are two intents even when they are byte-identical.

It exists because the duplicates here are not a call-site mistake anyone can see
locally. `/api/holdings` has three legitimate consumers — `loadHoldings()` at page
load, `_prefetchAcctData`, and `_loadHoldingsData` when the tab opens — and which
pair overlaps depends on how fast the network is. Measured on the load path: two
identical `/api/holdings` before, one after.

**A failure is never memoised**, only successes: pinning a panel empty for the
whole TTL is the `_fetch_div_events` distinction again. And **any successful
write clears the whole map**, from the same `fetch` wrapper that attaches the
CSRF header — holdings, transactions, cash, sales and options are one record
across five files, so "which GETs did this POST invalidate" has no local answer,
and a per-route invalidation list is a stale-panel bug waiting for whoever adds
the next mutating route. `acctRefresh()` clears it too: Refresh means ask again,
and a memo hit would render the same numbers and look like a dead button.

`GET_MEMO_TTL` is 15s and is **not** a freshness policy — `ACCT_TTL` still governs
how old the data on screen may be. Fifteen seconds is one interaction: a prefetch
racing a tab click, or a flip to Performance and straight back.

**A write clears the account cache too, and that is the half that mattered.**
The same line in the `fetch` wrapper calls `clearAcctCache()`. The account cache
is the *other* copy of this data — a rendered snapshot in localStorage that
`_loadHoldingsData` paints from and, while the entry is inside `ACCT_TTL`,
**returns early on without refetching at all**. So a write that left it standing
did not show something stale for a moment; it showed the pre-write portfolio for
the next five minutes. Adding a holding wrote the ledger, navigated to the
Holdings tab and rendered the list from before the add — measured: the server
had the new position, the table did not, and the entry's `ts` never moved.
That is what "cannot enter multiple holdings quickly" was: the first add landed
on a cold cache and appeared, the second landed inside the five minutes the
first had just warmed and did not.

Clearing alone is not enough, because these payloads are assembled over seconds
of quote and dividend fetches. A refresh already in flight when the write lands
finishes *after* it and saves the pre-write portfolio back — with a fresh `ts`,
so it looks current. `acctGen()` is captured before the first fetch and handed
back to `saveAcctCache(tab, payload, gen)`, which drops a payload whose
generation a write has overtaken. In memory only and deliberately so: it is a
token for "the server state I read", meaningless across reloads, and nothing
persisted carries a generation to mismatch.

**A mutating route answers `{error: ...}`, not the collection.** `saveHolding()`
assigned `await res.json()` straight to `holdingsData`, so a 400 (invalid
ticker), a 429 or a 500 replaced the array with an object. The very next line
calls `.some()` on it and throws, `catch(e) {}` swallows that, and the modal
never closes — the failure surfaced as a Save button that did nothing, with the
reason in a console nobody has open. `holdingsData` stayed poisoned for the rest
of the session: `openAddHoldingModal`'s `.find` and `renderHoldingsPage`'s
`.length` were broken from then on, and the Holdings page rendered its empty
state over a full portfolio. Both it and `removeHolding()` now check
`res.ok && Array.isArray(data)` before assigning, and the modal has a
`#holdings-modal-error` line so a refusal says what it was.

**The account prefetch waits for the movers *fetch*, not its enrichment.**
`_prefetchAcctData` used to poll every 500ms until `moversFetching` and
`moversEnrichTimer` were both clear. Enrichment is a one-request-per-second
trickle that polls up to 25 times — it never saturates the connection pool, so
there was nothing to yield to, and waiting it out put the user's own portfolio as
late as t+28s. It gates on `_moversReady` now, resolved in a `finally` so a
movers *failure* still releases it, and raced against a 4s cap so a hung request
cannot hold the account data hostage. Measured: `/api/holdings` starts at 44ms
against 3568ms before, and that 3568 was the *good* case with movers already
enriched server-side.

`loadMovers()` also fires immediately rather than on a 1.5s timer. That delay was
there to let the ticker tape go first, back when the tape fired ~100 requests;
it is one batched `/api/quotes` now and two concurrent requests need no
staggering.

**Redrawing an unchanged holdings table is destructive, not just wasteful.**
`renderHoldingsPage` blanks every summary card and rebuilds `hp-tbody`, which
drops every computed cell back to "—". On the revalidation path that ran
*after* `_applyHoldingsCache` had just painted real numbers, so a warm load
visibly wiped itself and redrew the identical table a beat later — the reason a
cached page looked exactly like a cold one. It now returns early when
`_holdingsSignature(items)` matches what is already drawn. Positions change on a
trade and a trade clears the request memo, so a signature match means there is
genuinely nothing to draw.

`_dimHoldingsColors()` is likewise only called when there is no cache to show.
Blanking unconditionally threw away the payload applied two lines above it; the
"don't present stale green as current" intent is carried by
`setUpdatedBar(..., true)`, which says "Updating…" in words for the whole window.

**`/api/stock` serves stale while it revalidates.** Expiry used to be a cliff:
the first request after `STOCK_TTL` paid the full cold cost — five Macrotrends
scrapes and a whole yfinance walk — on behalf of everyone behind it, and the more
popular a ticker the more reliably some caller hit that edge. Between `STOCK_TTL`
and `STOCK_STALE_TTL` the payload is returned immediately and `_refresh_stock_async`
rebuilds behind the response; past `STOCK_STALE_TTL` the caller waits, as before.

The hour-long stale window is sized against what actually moves in that payload:
fundamentals are quarterly, the statement frames and Macrotrends series change on
a filing, and the one field that moves by the minute is the quote — which
`_refresh_quote` re-fetches on the way out regardless of the entry's age. A
40-minute-old body with a live quote on it is the same answer, three seconds
sooner.

The rebuild registers in `_stock_inflight` under the lock that reads it, so ten
stale hits start one rebuild and a genuinely cold request for that ticker waits
on it instead of racing it. **A failed rebuild leaves the stale entry in place** —
dropping it turns one transient upstream failure into a cold fetch for the next
caller, which is the cost this whole path exists to avoid.

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

**An exchange is an index with a different membership source, and nothing
else.** `/api/listing/<key>` and `/api/index/<key>` are one endpoint under two
names — the index spelling is what the four index keys were published under, the
listing spelling is the honest one for a venue. Membership comes from Wikipedia
for an index, from the Nasdaq Trader SymDir files for Nasdaq/NYSE/NYSE American,
and from TMX's own company directory for TSX/TSXV; everything downstream is
shared, so adding a venue is a `_EXCHANGE_SOURCES` entry and a button, not a
second code path. `_UNIVERSES` is the one registry both halves are looked up in,
and a test asserts the key sets do not collide — a shared key would make dispatch
depend on dict ordering.

NYSE Arca and Cboe BZX are deliberately absent: of 2,697 Arca listings 15 are
common stock, and of 1,578 BZX listings 4 are. A tab for either is a filter box
over an empty table.

**A listing file names every security on a venue, not every stock.** Rights,
units, warrants, preferreds and baby bonds each trade under their own symbol and
arrive in the same column — 918 of Nasdaq's 5,577. None of them has a P/E or a
market cap, so a row for one is a row of dashes that still takes a line of the
table and a slot in every sort. Two cases the word list alone gets wrong, both
measured: `American Depositary Shares` must survive the bare `Depositary Shares`
rule that marks a preferred, since an ADR *is* the common equity of a foreign
issuer and 168 Nasdaq listings are spelled that way; and a coupon in the name
(`Aegon Funding Company LLC 5.10%`) is the only tell for a baby bond spelled
without any of the words.

TMX has no security-type column at all, so that filter cannot run on it.
`quoteType` does that job instead, downstream and for every venue at once —
without it the TSX tab is 1,528 ETFs over 714 companies and 70% of the table has
no market cap, because a fund has no such figure. It is applied to exchanges
only: an index constituent is an equity by construction, and a missing
`quoteType` there would drop a real member.

**A depositary receipt gets no market cap.** Yahoo attaches the *underlying
company's* cap to a CDR: NVDA.TO comes back at C$6.81T, which is NVIDIA, against
a CDR program worth a few hundred million. 133 of the 2,266 TSX entries are one,
so left alone the six largest companies on the Toronto exchange are Nvidia,
Apple, Alphabet, Microsoft, Amazon and Meta, and Royal Bank is not on the first
screen — cap is the default sort. Suppressed rather than converted, the same call
the cross-currency ratios make: the figure for the program itself is nowhere in
the payload. The row stays, because a CAD-hedged NVDA is a real thing to buy in
Toronto and its price, day change and 52-week range are all its own; it sorts
last on cap where every other unreported figure already goes. TMX's own name
wins for these, because Yahoo calls NVDA.TO "NVIDIA Corporation" and in a list of
Toronto listings that reads as NVIDIA being listed there.

The flag rests entirely on TMX spelling "CDR" out in the company name, which is
why there is a live canary asserting it still does.

**Market cap is filled from a cached share count, not a cached cap.** Yahoo omits
`marketCap` for a stable minority — 256 of 2,180 NYSE equities, and the largest
of them by traded value are XOM, CRM, MCD, HD, MRK and TGT. `sharesOutstanding`
is omitted with it, so nothing in the response derives it. `fast_info` carries
both, but a cap held for a day is a day-stale number in the column the table
sorts by, while shares outstanding move on the slow clock membership does — so
the count is stored (`share_counts.json`, 24h) and multiplied by the live price
on every build. That is the definition, and it was checked: on the 116 sampled
names reporting both, price x shares reproduces Yahoo's own figure to a ratio of
1.0000.

That distinction is what makes it affordable. The fetch is one request per symbol
(~24ms at 16 threads), so without the cache every 120-second payload rebuild
would pay ~3s again. A run is bounded at `_INDEX_CAP_BACKFILL_MAX` and ordered by
traded value, and symbols already cached drop out of the wanted list — so
successive builds walk down the tail instead of refetching the head. Measured:
NYSE went from 256 uncapped to 18, with XOM ranking 7th of 2,179.

**The whole universe still ships in one response, and gzip is why.** A 3,402-row
Nasdaq payload is ~876KB of JSON and ~171KB gzipped — 5.1x, because the body is
overwhelmingly repeated key names and digits. Flask does not compress on its own
and the loopback default has no proxy in front of it, so `_compress_response` is
what keeps the design that makes sorting and filtering free from being paid for
on the wire instead. Measured over the tunnel-shaped path: 218KB transferred
against 937KB decoded. Paging server-side would put a request on every sort, and
a sort that only orders the page you can see is not a sort.

It is registered *before* `_issue_device_cookie` so that it runs *after* it —
Flask calls `after_request` handlers in reverse registration order, and the one
that rewrites the body has to see the final body. Only 200s, only compressible
types, only above a kilobyte, and `Vary: Accept-Encoding` goes out either way:
a gzipped body handed to a client that never advertised gzip is corrupt JSON at
the far end and nothing in the log says so.

**The table draws a window, not the result set.** Rendering all 3,402 Nasdaq rows
is 2MB of HTML and **994ms** — per sort click and per keystroke in the filter.
Drawing `IDX_CHUNK` and extending on scroll is **38ms**. `_idxRows` is what
arrived, `_idxSorted` is it in the current sort order, and `_idxView` is that
filtered: sorting once per sort *change* rather than once per keystroke is the
point of the split, since filtering preserves order and a re-sort carries a
`localeCompare`. Extending appends rather than re-rendering, so reaching row
3,000 stays the cost of 150 rows.

There are three ways to reach the next chunk — an IntersectionObserver, a
passive rAF-coalesced scroll listener, and a click on the "showing N of M" line
— which is two more than looks necessary. The observer is the right mechanism
and the cheap one; it is backed up because its failure mode is silent and total
(a table frozen at 150 rows under a line that says "scroll for more"), and
because the Browser pane this was verified in runs as a *hidden* document, where
scroll events, `requestAnimationFrame` and IntersectionObserver are all
suspended. The click path is the one that could be proven to work there, and it
is also the only one reachable from a keyboard.

**`.movers-ex-btn` is shared, so its handlers must be scoped.** The market
browser's selector and the home page's movers strip are the same control and
deliberately the same class. An unscoped `querySelectorAll('.movers-ex-btn')`
bound the movers handler to the market tabs as well: clicking "Nasdaq-100" set
`moversExchange` to `undefined`, cleared the active state on the real movers
tabs and fired a movers reload for a venue that is not one. Both handlers are
scoped now (`#home-movers` and `#index-page`) and a test asserts the unscoped
form is gone.

**An index is two facts on two clocks, and they fail separately.** `/api/index/<key>`
answers with every constituent priced. Membership changes a handful of times a
year and comes from Wikipedia; quotes change by the minute and come from Yahoo.
One combined fetch would either re-scrape a constituent list every two minutes
or serve yesterday's prices, and a Wikipedia outage would take the prices down
with it — so `_index_members()` and `_index_quotes()` are cached and failed
apart.

Membership is **persisted** (`index_constituents.json`, a shared store — public
market-wide data identical for every account, the same reasoning as the shared
market-news payload) rather than merely memoised, because the failure it guards
against outlives the process. A scrape that raises or comes back thin leaves the
stored list exactly where it was, so a Wikipedia redesign degrades to a slightly
stale membership list instead of an empty page — and it degrades that way after
a restart too, which an in-memory fallback would not. `_scrape_index_members`
**raises rather than returning `[]`** on a thin parse, the distinction
`_fetch_div_events` keeps: the caller cannot tell "this index is empty" from
"the table moved", and only one of those may overwrite a good list. Every one of
these pages carries other tables under the same column names — the Dow article
has thirty-odd — so the row count is what picks the components table out, not
the column names.

**`keep_default_na=False` on that `read_html` is load-bearing.** pandas treats
`'NA'` as a missing value by default, and NA is National Bank of Canada's
symbol: the TSX 60 came back with 59 members, the bank simply absent, and
nothing anywhere reported a problem. `NULL`, `NaN` and `None` are on the same
default list and are all plausible symbols. `_index_symbol` still refuses a
`'NAN'` string as a backstop, because `clean_ticker` would accept it as a
well-shaped ticker and price a missing cell as a real listing.

**One quote endpoint carries every column this page shows.** `v7/finance/quote`
— the same endpoint behind the screener — returns price, daily change, the
52-week range, trailing P/E and market cap together, so a 500-name index is five
requests rather than five hundred `.info` lookups (measured: 503 symbols in
0.37s). It goes through yfinance's `YfData` because that endpoint needs a crumb
and cookie pair yfinance already negotiates and refreshes; a second copy of that
here would be the part most likely to break.

**Except market cap, which Yahoo omits outright for a stable minority.** Home
Depot, Exxon, Lowe's, McDonald's, Merck, Salesforce — 26 of the S&P 500 and 4 of
the Dow. `sharesOutstanding` is missing with it, so nothing in the response
derives it, and naming the field explicitly does not bring it back. This one is
worth a second request where the other gaps are not, because market cap is the
table's **default sort**: left null those names sort below every company in the
index, so the first thing anyone sees is Home Depot beneath a $5B utility, which
reads as a broken table rather than one missing figure. `_index_backfill_caps`
fills them from `fast_info` (price x shares — the definition, in the listing's
own currency), bounded and logged, and asked only for symbols that were priced
but uncapped so a total quote failure cannot become 500 extra requests.

Everything else missing stays **null, never 0**. Yahoo reports no trailing P/E
for a loss-making company — ~5% of the S&P 500 — and a `0.0` there reads as a
real and extraordinarily cheap valuation. The frontend sorts nulls **last in
both directions** for the same reason, and because `null - 5` is NaN, which
leaves the comparator inconsistent and the order undefined rather than merely
wrong.

**A row carries the currency its listing trades in.** The TSX 60 is priced in
`C$` and its caps formatted with `_currency_symbol(quote['currency'])` — the
portfolio's `$` on a C$126B bank is the SK hynix defect in a new place. Market
cap sends `mkt_cap_raw` alongside the display string, the same rule charts
follow: the column sorts on it, and parsing `'$3.45T'` back into a number would
quantise every mega-cap to three digits.

The whole index ships in one response (503 rows is ~90KB) so the filter box and
the sortable headers work in memory and cost no requests — the call the news
category chips already make. `week52_pos` is computed server-side and is `None`
on a degenerate range: a listing younger than a year divides by zero, and a
marker parked at the left edge would assert it is sitting on its low.

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
doughnut saying they are entirely retail-held — and Alphabet is ~81%
institutional. The builder returns
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

**Macrotrends scrapes go through `_start_macrotrends`.** Six scrapes at a
10-second timeout each, run one after another, is more than twice the 25-second
route deadline. They are submitted together before the yfinance prefetch so they
overlap it and each other, and read back with `_mt_result`. Adding a seventh
scrape inline would reintroduce the deadline problem — the count in
`_MT_LOOKUP_METRICS` is free to grow only because they run concurrently.

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
and all five series then in the lookup, every value the old request returned is
byte-identical in the wider one. `yb` only prepends older rows, so it cannot move a year the gate
validates against. **Losing this parameter is silent** — every chart shortens
back to fourteen with no error, no empty series and no log line, and the merge
still passes because the years it checks come back either way. A test asserts
the request carries it.

**`freq=Q` is the quarterly view, and v1 is a different field there.** The same
endpoint serves the same series by quarter, and `yb` bounds that window too —
157 quarters for Apple, back to 1987, against 40 annual columns. One scraper
still serves both because **v2 is the labelled period's own value at either
frequency**. Nothing else about the row is stable:

    annual     v1 = the prior fiscal year, v3 = the change from v1 to v2
    quarterly  v1 = the TRAILING TWELVE MONTHS, v3 = the change on the same
               quarter a year earlier

That was measured, not assumed — the trailing four v2 sum to v1 to the last
digit on every quarter checked. Reading v1 here shifted every annual bar back a
year once already; the same read on the quarterly path puts a TTM figure on a
quarter's bar, four times too large and rising smoothly where the real series is
seasonal. Four quarters sum exactly to the annual column on the other tab, over
every fiscal year compared, which is the identity the live test asserts and the
thing that makes the two views one dataset at two resolutions rather than two
sources sitting near each other.

**The quarterly gap is the cash-flow statement, and half of it is silent.**
`free-cash-flow` under `freq=Q` does not error — it returns the **annual payload
byte for byte**, 39 rows twelve months apart. Only `capital-expenditures` has
the decency to 404. Measured across ten tickers spanning mega-caps, a bank, a
small-cap and two recent IPOs, the split is a property of the statement rather
than of the company. So `_MT_QUARTERLY_METRICS` names the five income-statement
series and nothing asks for the other two, and FCF and capex go quarterly on
yfinance's five or six columns with a caption saying why they are short — a
five-bar chart beside a 157-bar one otherwise reads as a failure.

`_mt_assert_quarterly` is the backstop for the five, and it **raises** rather
than returning empty, so `_TtlCache` cannot hold a broken series for six hours.
It exists because that failure is silent and total: forty annual bars relabelled
Q1..Q4 draw a company that grew for four decades without one down quarter, every
number on it real, and neither the merge gate nor the chart nor a reader has any
way to notice. A live canary asserts the cash-flow series is *still* annual, so
if Macrotrends ever publishes one the omission gets revisited instead of
outliving its reason.

**A quarter is keyed `YYYY-MM`, and the day is dropped deliberately.** Both
sources normalise a quarter end to month end today — checked on COST, TGT, NKE,
CSCO and DE, every one a 52/53-week filer whose quarters really end on a weekday,
and the two agree on all five to seven columns each. Keying on the full date
would stake the merge on that continuing to match on both sides at once, and the
failure is silent: the overlap falls to zero, `_mt_check` sees no evidence either
way and drops every series, and the charts shorten to five bars with nothing on
screen saying why. Nothing downstream wants the day, and the zero-padded key
sorts chronologically as a string — the property `_new_txn_id` already relies on.

`_fiscal_quarter` is **not** `_fiscal_year` and cannot be. That rule buckets a
year on a fixed April cut, which is right for an annual column and wrong for
three quarters in four of any non-calendar filer: Apple's December 2025 quarter
is FY2026 Q1, and `_fiscal_year` files it as 2025 — a year early, beside a bar it
actually follows. The fiscal year-end month is read off the annual frame, where
every column shares it.

**The merge gate is reused, not reimplemented.** `_merge_macrotrends` and
`_mt_check` never look at what a key *means* — they intersect, sort and compare —
so quarter keys pass through the same take-or-drop-whole rule, the same
tolerances and the same logging as the annual series. yfinance's five quarterly
columns are the overlap they judge on.

**Quarterly is a second route, fetched on the toggle.** `/api/stock/quarterly`
rather than five more scrapes on `/api/stock`: a lookup that never flips the
switch pays nothing, and the first flip per ticker costs one round trip that
`_quarterly_cache` and the browser's `apiGet` memo absorb thereafter. Same
argument the account prefetch makes — what everybody waits for should not carry
what only some people use. It refuses a blocked symbol independently, because it
is reachable on its own and its cache is shared by every account.

**A quarterly chart's YoY compares four bars back, not one.** `makeYoyPlugin`
takes the lag as an argument, and this is the half that is wrong rather than
merely ugly if it is missed. On a quarterly series the previous bar is the
previous *quarter*, so lag 1 measures seasonality and prints it as growth:
driven through a synthetic series growing a flat 10% a year, lag 4 reads +10% on
every bar and lag 1 reads −30%, −3%, +6%, +53% repeating forever. The lag is
computed **per chart**, since `periodRows` falls back to the annual series
independently for each — FCF can be annual while revenue is quarterly, and lag 4
on annual bars compares 2026 against 2022 and calls it year-over-year. The
dividend chart stays annual whatever the toggle says and names `makeYoyPlugin(1)`
explicitly rather than inheriting.

**`.period-btn` was already taken, and sharing it broke both controls.** It
belongs to the price chart's range selector (1D/1W/1M/1Y/5Y/10Y/All), with its
own CSS rule and its own handler. Measured while sharing it: their handler
clears `.active` across every match and wiped the frequency toggle's state, and
a click on any range button reached the frequency listener with `dataset.period`
undefined — which reads as "annual" and silently snapped the fundamental charts
back out of quarterly. The `.movers-ex-btn` lesson one class over. The toggle is
`.freq-btn` now and both of its handlers are scoped to `#period-toggle`, with a
test asserting the collision cannot come back.

Two consequences in the frontend. Series are read through `periodRows(data, ...)`
— the single place the toggle is consulted, so reading `data.<x>_by_year`
directly in a chart block is what makes one chart ignore the switch. And a bar's
label comes from `periodLabel(r)`, because a quarterly row carries `label` where
an annual one carries `year`: the scraped-range captions used `Math.min` over
those values, which is fine on a number and `NaN` on `"Q3 '26"`.

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

**Revenue is a section, not a popup, and it shares the earnings chart's
treatment.** The three money series on the income statement — revenue, net
earnings, free cash flow — now read as one family: a `.section-label`, a
`.table-box`, value above each bar, YoY under it, `makeDragAvgPlugin` for the
average across a swept range, `srcColor` fading the scraped years and
`markScrapedYears` captioning what the fade means.

Revenue used to be the odd one out — a `.pm-expand` popup hanging off the
Revenue (TTM) box, drawn by ChartDataLabels with a flat green fill and no source
marking, and pure yfinance, so it was five bars where earnings was forty. It is
one `_MT_SERIES` entry (`revenue`, billions, `income-statement`) plus the same
`_merge_macrotrends` call the other two make: AAPL goes from 5 years to 40,
back to 1987.

Two consequences of promoting it. The build **must sit after `yoyPlugin`** — a
`const` in `fetchStock`'s scope, so constructing the chart above its definition
is a TDZ throw rather than a hoist, which is why the block moved down beside the
earnings one rather than staying where the popup was. And the three remaining
popups (`pm`, `capex`, `eps`) each close the others by id on click, so removing
one means removing it from the other three handlers; a leftover
`getElementById('revenue-expand').classList` is a TypeError that kills the
handler it sits in.

**`revenue_ttm` is the trailing four quarters, not the newest annual column.**
The box is labelled "Revenue (TTM)" and was reading `financials.loc['Total
Revenue'].iloc[0]` — a fiscal year. Apple read $416.16B against a real
$466.82B. Nothing contradicted it while the by-year chart was a popup; the
chart is a section now and prints its own TTM bar directly below the box, so
the two would have disagreed in plain sight. `_quarterly_ttm` returns None
rather than a short sum when it has fewer than four quarters, so the annual
column stays the fallback — a figure one quarter stale beats a blank card.

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

**`.table-box` is `position: relative`, and that line is load-bearing.** The
empty states inside it — `.chart-empty` on earnings, FCF, shares outstanding,
dividend history and P/E — are `position: absolute; inset: 0` overlays that
cover a chart canvas so the box keeps its height while the message shows. They
need that box to be their containing block.

It used to be, by accident. The rule carried `animation: fadeUp 0.35s ease
both`, whose keyframes end on `transform: translateY(0)`; with `fill-mode:
both` that transform stays applied forever, and a non-`none` transform makes an
element a containing block for absolutely-positioned descendants. The radius
pass above swapped the animation for `border-radius` + `overflow: hidden` and
took the containing block with it. All five overlays reparented to `#wrap` and
drew as full-width 200px bands across the **top of the page**.

`inset: 0` also makes them a transparent hit target, so this was not cosmetic.
On any ticker with no dividend history — AMZN, TSLA, most growth names —
`elementFromPoint()` at the centre of the "+ Holdings" hero button returned
`#div-empty`, and the button could not be clicked at all. "No dividend history
found." printed near the top of the page and a dead Add-to-Holdings button were
one bug.

`overflow: hidden` does not contain this: it clips only descendants whose
containing block is the box or inside it, which is exactly what these had
stopped being. Relative positioning at `z-index: auto` creates no stacking
context, so the sticky `<th>` strips and the clip are unaffected — they were
measured after the fix.

`#bs-empty` carries **both** classes: it replaces the balance-sheet grid rather
than covering a canvas, so the rule above cannot reach it — the absolute
positioning is on the element itself. `.table-box.chart-empty { position:
static }` puts it back in flow. Three tests in `tests/test_template_structure.py`
assert the pair, because nothing about this failure looks like a missing
declaration: the edit that lost it was about corner radius, and the symptom
appeared four sections away.

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

**A column count is a class, and an inline style is outside the responsive
system.** This is the one that made everything else here decorative. Five metric
rows on the stock page carried `style="grid-template-columns: repeat(7, 1fr)"`
— two of them on an element also classed `.g4`, which said four — and an inline
declaration outranks every media query in the file. So those rows held seven
fixed columns at every width: at 375px the stock page laid out to 999px and
scrolled sideways, dragging the *fixed* ticker tape out to 999px with it. The
rules they were ignoring (`.g3, .g4` at 680px, everything at 420px) had been
there the whole time.

That is the failure mode to watch for, because nothing about it reads as a bug:
the responsive CSS is present, it is just unreachable, and it looks correct on
any machine wide enough. `.g2`…`.g7` now cover every row on the page and
`.hp-summary-3` covers Performance's three-card summary, which had the same
inline override. A test asserts no template sets `grid-template-columns` inline,
and a second asserts every `.gN` used in the markup has a rule behind it — a
`.g8` typo is a grid that silently collapses to one column.

**The desktop layout did not change, and that is a constraint rather than a
happy accident.** Moving a declaration out of a `style=` attribute is only safe
if the rule that replaces it resolves to the same value, so each of these was
copied verbatim and then measured: `.g5`/`.g6`/`.g7` against the `repeat(N, 1fr)`
they replaced, `.mini-btn` and `.mini-input` against the ~10px/11px header
controls, `.insider-chip` against its 68px, `.section-head` against the flex row
written inline three times. The `@media (max-width: 680px)` rule is deliberately
still `.g3, .g4` only — a 5/6/7-column row was inline and therefore fixed at
*every* width, so collapsing it anywhere above the phone breakpoint would be a
new behaviour, not a restoration. The wide rows collapse in the 640 block and
nowhere else.

Two traps found while checking that. `.hp-remove` is the same `×` control as the
valuations row's, but at 16px against that one's 15px, so the valuations button
gets `.val-remove` rather than sharing — the point of moving it to CSS is to
reach it from the phone block, not to restyle the desktop by a pixel. And a
`gap` added to `.section-head` is inert under `justify-content: space-between`
at desktop width and load-bearing once the row wraps, so it lives in the phone
block; "inert here" is not the same as "identical", and only one of those can be
verified by measurement.

Verified by fingerprinting ~40 elements at 1440px — geometry, font size,
padding, colours, radius, tracking, plus every grid's resolved column list and
every table's visible column set — before and after. One field differed, which
was the 16px/15px above, and it is now the 40th that matches. **A HEAD baseline
will not reproduce this**: most of this app is uncommitted, so `git stash` drops
features whole (`goIndexes` is simply undefined there). Compare against the
working tree.

**Phone rules live in one `@media (max-width: 640px)` block.** Same argument as
`_hideAllPages()`. The file had drifted to eight breakpoints — 1150, 1100, 900,
780, 680, 640, 620 and 420 — with no rule about which meant what, and *two* of
them were the same 640px width setting `.wrap` padding to different values,
where whichever sat lower silently won. The wider ones do
desktop-to-narrow-desktop work and are left alone; 640 means phone, appears
once, and a test enforces that.

It is not a second UI. Everything in it re-lays out markup that already exists
— no phone-only element, nothing hidden that carries information the wide
layout carries — so there is nothing to drift the way a separate mobile
template would. What it does: the app bar drops the wordmark and goes to two
rows (169px of chrome to 130px, and the search input from **72px** to 197px);
metric tiles and summary cards go 2-up; the portfolio chart row stacks; every
control clears a 40px touch target; and `input { font-size: 16px }`, because iOS
zooms the viewport on a focused field under 16px and does not zoom back out —
on `login.html` that landed on the password field.

**A phone column-hide is scoped to a container id, never the shared class.**
`#hp-table-wrap` had no scroll container at all, so ten columns ran off the page
and took the document with them (843px at 375px) — the same way the users list
lost its Delete button. Every wide table scrolls now, and then the columns a
phone cannot afford come out. `.hp-table` is also the options, closed-options
and transactions tables and `.movers-table` is also Markets and Hidden, so an
unscoped `nth-child(5)` hides Volume on one table and Expiry on another. That is
the `.movers-ex-btn` lesson one selector over, and a test asserts every
column-hiding selector in the phone block starts with `#`.

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
- Macrotrends units differ per metric: `revenue`, `net-income` and
  `shares-outstanding` are in **billions**, FCF and capex in **millions**, margin
  and EPS in their own units. Same endpoint, same field name, different scale —
  `_MT_SERIES` holds it. The unit does not vary with the company: a filer with
  $17M of revenue serves `0.017` on the line Apple serves `416.161`, so a new
  series' scale has to be read off a small-cap as well as a mega-cap.
  The share count is also quantised to the nearest million, which turns a
  small-cap series into flat runs of identical values; `_MT_SHARES_MIN` drops a
  series that coarse rather than charting "no change" where the count moved.
  A Macrotrends year never overwrites a yfinance year and is tagged `src: 'mt'`
- Interest coverage is an income-statement figure, so it carries its own
  `interest_coverage_year` rather than the balance sheet's `as_of`
- FCF comes from cashflow "Free Cash Flow" row, not manual calculation
- A Macrotrends value is in var chartData → field **v2**, never v1 — at both
  frequencies. v1 is the prior year annually and the **TTM** quarterly. See below
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
- Portfolio data is no longer tracked: `users/` is gitignored as a whole, and
  no commit reachable from `main` has ever carried a ledger. `users.json` and
  `settings.json` (at every level, including `users/<name>/settings.json`) and
  `app_secret.json` are gitignored and must stay that way — between them they
  hold the password hashes, every account's API keys, and the session signing
  key. A leaked `SECRET_KEY` lets anyone mint a valid cookie for any account.
  The history was rewritten twice to get here, and a force-push does not delete
  anything from GitHub: the pre-rewrite commits stayed retrievable by SHA and
  through `refs/pull/1/head` until the remote repository was recreated. If a
  secret or a ledger ever lands in a commit again, treat the secret as burned
  the moment it is pushed and recreate the remote rather than force-pushing
  over it.
- There is no password reset by email and no MFA. `python app.py passwd <user>`
  from the machine itself is the whole recovery story, which is proportionate
  while this binds to loopback and would not be if it were ever exposed.
- Index membership depends on four Wikipedia articles keeping a components
  table with the column names in `_INDEX_SOURCES`. The persisted last-good list
  and the row-count gate mean a change there degrades rather than empties the
  page, but nobody would notice a slowly stale index without the live canary in
  `test_live.py`, which asserts each one still parses to a plausible size. There
  is no free API that serves these lists; the paid index providers licence them.
- Rate-limit buckets live in process memory, so they reset on restart and are
  not shared between workers. Fine for one Flask process on loopback; a real
  multi-worker deployment would divide every limit by the worker count and hand
  a restart loop a way to clear them. Redis or an equivalent shared counter is
  the fix, and `_rate_consume()` is the one function that would change.
  `_report_jobs` has the same property, which is why `/api/report-status` for an
  unknown job is already a 404 rather than an error.
