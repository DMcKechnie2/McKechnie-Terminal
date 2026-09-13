# stock-intel

Structured equity data for AI agents. Extracted and rebuilt from the McKechnie
Terminal data layer.

An agent making a decision needs different things from a dashboard rendering a
page. This package returns raw numerics with a declared unit convention,
computes the derived metrics an agent would otherwise get wrong, flags the
places where a number does not mean what it appears to mean, and reports what it
could not retrieve instead of returning a confident-looking empty result.

## Install

```bash
pip install -e .
```

Requires Python 3.10+. For the MCP server, also `pip install mcp`.

## Register as an MCP server

Add to your MCP client config (`claude_desktop_config.json`, or `.mcp.json` for
Claude Code):

```json
{
  "mcpServers": {
    "stock-intel": {
      "command": "python",
      "args": ["-m", "stock_intel.server"],
      "cwd": "/path/to/McKechnie Terminal/stock-intel"
    }
  }
}
```

### Tools exposed

| Tool | Purpose |
|---|---|
| `get_stock` | Full lookup: quote, valuation, financials, balance sheet, cash flow, dividends, earnings, analysts, ownership, insiders, flags |
| `compare_stocks` | Up to 20 tickers side by side on identical fields, with a mixed-currency warning |
| `search_ticker` | Resolve a company name to candidate symbols, so agents stop guessing tickers |
| `get_statement` | Raw statement line items (R&D, inventory, goodwill…) not in the curated sections |

## Use as a library

```python
from stock_intel import get_stock, compare_stocks, render_report

report = get_stock("AAPL")                      # full dict
print(render_report(report))                    # compact digest

get_stock("RY.TO", sections=["quote", "dividends"])   # cheaper subset
compare_stocks(["RY.TO", "TD.TO", "BNS.TO"])
```

## CLI

```bash
python -m stock_intel AAPL
python -m stock_intel AAPL --json
python -m stock_intel AAPL --sections quote valuation dividends
python -m stock_intel --compare RY.TO TD.TO BNS.TO
python -m stock_intel --search "royal bank of canada"
```

## The five rules

These are the design contract. Everything else follows from them.

**1. `None` means unavailable. It never means zero.**
The single most important property for an agent. A missing dividend and a zero
dividend lead to opposite conclusions. Sections that fail to fetch are listed
under `data_quality.failed_sections` with the reason, never silently emptied.

**2. Units are declared and consistent.**
`*_pct` is a percentage (`12.5` means 12.5%). `*_ratio` is a bare multiple.
Money is unscaled reporting currency. yfinance is internally inconsistent here —
`dividendYield` arrives as a percent while `returnOnEquity` arrives as a
fraction — so values are normalized on the way in. Dividend yield is *computed*
from rate ÷ price rather than trusted, because that field's convention has
changed between library versions.

**3. A metric with insufficient support returns `None`, not a plausible number.**
No CAGR from a negative base (a swing from −100M to +50M is not "150% growth").
No percentile from four observations. No P/E from a loss-making quarter.

**4. Failures are reported, never swallowed.**

**5. Facts and mechanical flags only.** Nothing here recommends an action.

## What it adds over raw yfinance

- **Valuation in context.** "P/E 28" is not decision-grade. "P/E 28, 85th
  percentile of its own 5-year range, median 19" is. Reconstructed from weekly
  prices against as-then-reported trailing EPS.
- **Derived metrics computed once, correctly**: CAGRs, FCF and earnings yields,
  ROIC, interest coverage, net-debt/EBITDA, shareholder yield, dividend growth
  streak, earnings beat rate, margin and share-count trends.
- **Mechanical flags with cited evidence** — each carries the numbers that
  triggered it, so the agent can verify rather than trust.
- **One-call comparison** across tickers on identical fields.
- **A compact renderer** costing roughly a fifth of the tokens of the JSON.

## Traps this handles

Each of these was found by running the tool against live data and checking the
output, and each has a regression test.

**Bank "free cash flow" is an artefact.** Royal Bank screens at a ~17% FCF
yield. Operating cash flow for a lender tracks deposit and loan flows, not
distributable cash. An agent would read that as extraordinarily cheap. Financial
and property issuers get a `sector_metrics_not_applicable` warning naming
exactly which fields to disregard.

**Corporate buybacks look like insider buying.** Yahoo's insider feed labels
share repurchases `"Redemption, retraction, cancelation, repurchase"` — which
contains the substring "purchase". Matching naively turned Royal Bank's buyback
programme into 50 insider *buys* and flipped the signal from net selling to net
buying.

**RSU vesting is not conviction.** Roughly half of all insider rows have a blank
description; they are compensation events with six-figure share counts and a
null value. Counting them as purchases misrepresents scheduled pay as a signal.
They are excluded from the net figure and reported separately.

**A 10% holder is not a director.** Volkswagen's $1B purchase of Rivian stock
moves the aggregate by 4.3% of shares outstanding while Rivian's own officers
were net *sellers*. Net figures are split by role.

**`"Disposition in the public market"`** is the Canadian term for a sale, and
`"Disposition under a purchase/ownership plan"` contains "purchase" but is also
a sale. Both were misread before the classifier was rebuilt from the actual
phrase vocabulary.

**Balance sheet period mixing.** `info.totalDebt` is most-recent-quarter while
the annual statement is fiscal-year-end; for Apple they differ by $14B. Reading
FY debt against MRQ cash produces a net debt figure matching neither. All
balance sheet figures now come from one statement, and the period is reported.

**Sentinel zeros.** yfinance returns `grossMargins == 0.0` for every bank,
because banks report no gross profit line. Passed through, that renders as a
real "0.0%" — exactly the zero-versus-missing confusion rule 1 exists to
prevent.

## Layout

```
stock_intel/
  core.py       yfinance access: lazy loading, caching, timeouts, error capture
  metrics.py    derived math; refuses degenerate inputs
  sections.py   section builders + insider classification
  flags.py      threshold rules with cited evidence
  render.py     compact text renderer
  report.py     orchestration: get_stock / compare_stocks / search_ticker
  server.py     MCP stdio server
  cli.py        command line
```

## Tests

```bash
pytest              # offline logic, 41 tests
pytest -m network   # live Yahoo Finance smoke tests, 4 tests
```

## Limitations

- **One upstream source.** Everything comes from Yahoo Finance via yfinance,
  which is an unofficial API: fields move between names, coverage is thinner
  outside North America, and it can rate-limit. There is no second source to
  cross-check against, so a wrong upstream value is a wrong output value.
- **ROIC assumes a 21% tax rate** when the effective rate is unavailable.
- **Fiscal year mapping is a heuristic** — period ends in Jan–Mar are attributed
  to the prior calendar year.
- **Flag thresholds are conventional rules of thumb**, not tuned to any
  strategy. Each flag states its threshold so you can disagree with it.
- **No intraday, options, or futures data.**
- Not investment advice.
