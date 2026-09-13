# McKechnie Terminal

A self-hosted stock research and portfolio terminal. Flask on the back, one
file of vanilla JS on the front, Yahoo Finance and Macrotrends behind it.
Multi-user, with every account's portfolio and API keys kept in its own
directory and never shared.

## What it does

**Stock page** — quote and key ratios; up to four decades of revenue, earnings,
free cash flow, margins, share count and EPS (Yahoo's five years back-filled
from Macrotrends, behind a gate that drops a series whole when the two sources
disagree); an annual/quarterly toggle; a balance-sheet snapshot; ownership split
and short interest; insider transactions; analyst estimates; dividend history;
a DCF and margin-of-safety calculator; cross-listings on other exchanges;
per-ticker news; and, through the optional Forward Guide integration, the
guidance a company gave in its latest earnings release.

**Markets** — the S&P 500, Nasdaq-100, Dow and TSX 60 as sortable, filterable
tables; the full Nasdaq, NYSE, NYSE American, TSX and TSXV listings; a movers
strip and a ticker tape; a market-news feed built from fifteen RSS sources with
optional LLM relevance filtering.

**Portfolio** — a transactions ledger that is the single source of truth
(holdings, realized gains and cash are all rebuilt from it), options, dividends
received, a daily chain-linked time-weighted return, per-position performance,
a watchlist, theses and valuation notes. Every tab reconciles to one identity:

    realized + unrealized + dividends + option P/L == (cash + market value) − invested

**Accounts** — sign-in required for everything; an admin tab to manage users;
API keys stored per account; an optional read-only guest account seeded with a
demo portfolio, for showing the app to someone without handing them a login.

## Stack

Python 3.10+, Flask, yfinance, pandas, requests, BeautifulSoup. The frontend is
`templates/index.html` — vanilla JS and Chart.js, no build step.

## Quick start

```
pip install -r requirements.txt
python app.py create-admin   # first run only: prompts for a username and password
python app.py                # serves on http://127.0.0.1:5000 and opens a browser
```

On Windows, `setup.bat`, `Create Account.bat` and `Launch McKechnie Terminal.bat`
do the same three things by double-click.

There is no public signup. Accounts come from the Admin tab or from
`create-admin`; `python app.py list-users` and `python app.py passwd <user>` are
the recovery path if an admin password is lost.

### Guest mode

```
python app.py guest enable    # adds "Continue as guest" to the login page
python app.py guest reset     # puts the demo portfolio back
python app.py guest disable   # hides the button and signs out every live guest
```

A guest can read everything in the demo account and write nothing. The demo
book is invented trades on real listings.

### API keys

All optional, all per account, pasted into the Settings tab: Groq or DeepSeek
(news relevance filtering and headline rewriting), Anthropic (guidance
extraction through Forward Guide), FRED and Alpha Vantage (used by the report
integration). Nothing is read from the environment or from a shared file — an
account with no key configured gets the non-LLM path, which every feature has.

## Configuration

Everything is optional and set through environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Stay on loopback unless the app sits behind TLS; the session cookie becomes `Secure` automatically the moment this leaves loopback. |
| `PORT` | `5000` | |
| `SERVER` | | `waitress` to serve with a production WSGI server. Single-process by design — the portfolio lock and the rate-limit buckets live in memory. |
| `NO_BROWSER` | | `1` to skip opening a browser on start. |
| `DATA_DIR` | working directory | Where `users/`, `users.json` and `app_secret.json` live. |
| `SECRET_KEY` | generated once, persisted to `app_secret.json` | Session signing key. |
| `TRUSTED_PROXIES` | `0` | Number of reverse-proxy hops that rewrite `X-Forwarded-For`. Needed for per-address rate limiting and login lockout behind a proxy. |
| `SESSION_HOURS` | `12` | Session lifetime. |
| `REPORT_MAX_CONCURRENT`, `GUIDANCE_MAX_CONCURRENT` | `2` | Per-account cap on subprocess-backed jobs. |
| `FORWARD_GUIDE_DIR` | `../Forward Guide` | Location of the optional Forward Guide project. |

### Optional integrations

Two sibling projects are run as subprocesses when present and simply
unavailable otherwise:

- **Forward Guide** (`FORWARD_GUIDE_DIR`) reads a company's earnings 8-K and,
  when the guidance was only spoken, its earnings call, and returns what the
  company said it expects. Needs an Anthropic key in the account's settings.
- **StockBox** (`../StockBox/StockBox`) builds a PDF research report per ticker.

`stock-intel/` is a standalone package that exposes the same equity data to AI
agents as structured numerics with declared units, including as an MCP server.
It is not imported by the app; see its own README.

## Tests

```
pytest              # ~800 offline tests, no network
pytest -m network   # live canaries against Yahoo, Macrotrends, Wikipedia and the exchanges
```

Tests never touch real data: `tests/conftest.py` redirects every store to a
temp directory and hands each test a signed-in client. `tests/test_isolation.py`
signs in as a second account, walks every per-user route and greps the raw
response bytes for markers seeded into the first account's portfolio.

## Data sources and caveats

Quotes and fundamentals come from Yahoo Finance through yfinance; long-run
history from Macrotrends; index membership from Wikipedia; exchange listings
from Nasdaq Trader and TMX. All of it is read from public pages with no API
agreement behind it, so any of it can change or break without notice. The merge
gate, the persisted last-good membership lists and the stale-while-revalidate
caches exist so that it degrades rather than blanks the page. Figures are as
reported by those sources. Nothing here is investment advice.

`CLAUDE.md` is the engineering log: the invariants the code depends on, and the
measured reason behind each one.

## Layout

```
app.py                 every route and every data fetch
templates/index.html   the whole frontend
templates/login.html   the sign-in page
tests/                 offline regression tests; live smoke tests behind -m network
stock-intel/           standalone equity-data package for agents (own README)
value_screen.py        numbers-only TSX undervaluation screen
value_rank.py          sector-aware ranking of the screen's output
users/<name>/          one directory per account — gitignored, created at runtime
```

## License

MIT — see [LICENSE](LICENSE).
