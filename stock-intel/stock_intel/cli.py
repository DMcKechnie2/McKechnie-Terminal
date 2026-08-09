"""Command-line interface.

    python -m stock_intel AAPL
    python -m stock_intel AAPL --json
    python -m stock_intel AAPL --sections quote valuation dividends
    python -m stock_intel --compare RY.TO TD.TO BNS.TO
    python -m stock_intel --search "royal bank of canada"

Useful for testing the package without an MCP client, and for shelling out to
it from agents in other languages.
"""

from __future__ import annotations

import argparse
import json
import sys

from .render import render_comparison, render_report
from .report import ALL_SECTIONS, compare_stocks, get_stock, search_ticker


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stock_intel",
        description="Structured equity data for AI agents.",
    )
    p.add_argument("tickers", nargs="*", help="one or more ticker symbols")
    p.add_argument("--compare", action="store_true",
                   help="compare the given tickers side by side")
    p.add_argument("--search", metavar="QUERY",
                   help="resolve a company name to candidate symbols")
    p.add_argument("--sections", nargs="+", metavar="NAME",
                   help=f"limit sections. available: {', '.join(ALL_SECTIONS)}, all")
    p.add_argument("--json", action="store_true",
                   help="emit raw JSON instead of the compact digest")
    p.add_argument("--no-cache", action="store_true", help="bypass the 5-minute cache")
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy codepage that cannot encode the
    # typographic characters used in the digest. Force UTF-8 rather than
    # degrading the output for everyone.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    if args.search:
        results = search_ticker(args.search)
        if args.json:
            print(json.dumps(results, indent=2, default=str))
        elif not results:
            print(f'No matches for "{args.search}".')
        else:
            for r in results:
                if r.get("error"):
                    print(f"Search failed: {r['error']}", file=sys.stderr)
                    return 1
                print(f"  {r.get('symbol', '?'):<12} {(r.get('name') or '')[:44]:<46}"
                      f" {(r.get('exchange') or ''):<16} {r.get('type') or ''}")
        return 0

    if not args.tickers:
        build_parser().print_help()
        return 2

    if args.compare or len(args.tickers) > 1:
        result = compare_stocks(args.tickers, use_cache=not args.no_cache)
        print(json.dumps(result, indent=2, default=str) if args.json
              else render_comparison(result))
        return 0

    report = get_stock(args.tickers[0], sections=args.sections,
                       use_cache=not args.no_cache)
    print(json.dumps(report, indent=2, default=str) if args.json
          else render_report(report))
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
