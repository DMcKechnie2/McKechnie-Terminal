"""Rank the collected TSX fundamentals on hard valuation numbers.
Uses sector-aware scoring: financials/REITs judged on P/B, P/E, yield, ROE;
others on P/E, forward P/E, EV/EBITDA, FCF yield, PEG, growth, leverage."""
import pandas as pd, json

df = pd.DataFrame(json.load(open('value_screen_raw.json')))

FIN = {'Financial Services'}
REIT = {'Real Estate'}

def score(r):
    s, reasons, flags = 0.0, [], []
    sector = r.get('sector') or ''
    pe, fpe, pb, peg = r.get('pe'), r.get('fpe'), r.get('pb'), r.get('peg')
    ev = r.get('ev_ebitda'); fcfy = r.get('fcf_yield'); roe = r.get('roe')
    divy = r.get('divy'); d2e = r.get('d2e'); eg = r.get('earn_growth')
    teps, feps = r.get('trail_eps'), r.get('fwd_eps')

    # --- Correct one-time-gain distortion: if trailing EPS >> forward EPS,
    #     trailing P/E is artificially low. Use forward P/E as the real gauge. ---
    if teps and feps and feps > 0 and teps > 1.5*feps and pe:
        flags.append(f"trailing P/E {pe:.1f} inflated by one-time gain; use fwd")
        pe = None  # don't reward the fake-cheap trailing P/E

    # --- Cheapness on earnings ---
    if pe and pe > 0:
        if pe < 8:   s += 2.5; reasons.append(f"P/E {pe:.1f} (very low)")
        elif pe < 12: s += 1.5; reasons.append(f"P/E {pe:.1f} (low)")
        elif pe < 16: s += 0.5
        elif pe > 30: s -= 1.0; flags.append(f"P/E {pe:.1f} rich")
    if fpe and fpe > 0:
        if fpe < 10: s += 1.5; reasons.append(f"fwd P/E {fpe:.1f}")
        elif fpe < 14: s += 0.5
        # forward cheaper than trailing => improving earnings
        if pe and fpe < pe * 0.9: s += 0.5; reasons.append("earnings improving (fwd<trail)")

    # --- Book value (weighted heavily for financials/REITs) ---
    if pb and pb > 0:
        w = 2.0 if (sector in FIN or sector in REIT) else 1.0
        if pb < 1.0: s += 2.0*w; reasons.append(f"P/B {pb:.2f} (below book)")
        elif pb < 1.5: s += 1.0*w; reasons.append(f"P/B {pb:.2f}")
        elif pb < 2.5: s += 0.3*w
        elif pb > 5:   s -= 0.5

    # --- EV/EBITDA (skip financials, meaningless there) ---
    if ev and ev > 0 and sector not in FIN:
        if ev < 6:   s += 1.5; reasons.append(f"EV/EBITDA {ev:.1f} (cheap)")
        elif ev < 9: s += 0.7
        elif ev > 16: s -= 0.7; flags.append(f"EV/EBITDA {ev:.1f} rich")

    # --- FCF yield ---
    if fcfy is not None:
        if fcfy > 10: s += 2.0; reasons.append(f"FCF yield {fcfy:.1f}%")
        elif fcfy > 6: s += 1.0; reasons.append(f"FCF yield {fcfy:.1f}%")
        elif fcfy < 0: s -= 0.5; flags.append("negative FCF")

    # --- PEG (cheap relative to growth) ---
    if peg and peg > 0:
        if peg < 1: s += 1.0; reasons.append(f"PEG {peg:.2f}")
        elif peg > 3: s -= 0.5

    # --- Quality: ROE ---
    if roe is not None:
        if roe > 0.18: s += 1.0; reasons.append(f"ROE {roe*100:.0f}%")
        elif roe > 0.12: s += 0.5
        elif roe < 0: s -= 1.0; flags.append("negative ROE")

    # --- Dividend: yfinance already returns dividendYield as a percent (3.9 = 3.9%) ---
    if divy is not None:
        dv = divy
        payout = r.get('payout')
        if 3 <= dv <= 8:
            s += 0.7; reasons.append(f"div {dv:.1f}%")
            if payout and payout > 0.95:
                flags.append(f"payout {payout*100:.0f}% (stretched)")
        elif dv > 8 and payout and payout > 1.0:
            flags.append(f"div {dv:.1f}% w/ payout {payout*100:.0f}% (unsustainable)")

    # --- Growth ---
    if eg is not None and eg > 0.10:
        s += 0.5; reasons.append(f"EPS growth +{eg*100:.0f}%")

    # --- Leverage penalty ---
    if d2e is not None and d2e > 200 and sector not in FIN and sector not in REIT:
        s -= 1.0; flags.append(f"high debt/equity {d2e:.0f}")

    return round(s,2), reasons, flags

out = []
for _, r in df.iterrows():
    sc, reasons, flags = score(r)
    out.append({**r, 'score': sc, 'reasons': '; '.join(reasons), 'flags': '; '.join(flags)})

res = pd.DataFrame(out).sort_values('score', ascending=False)

def fmt(v, f, suf=''):
    return (f.format(v)+suf) if v is not None and pd.notna(v) else '—'

print("="*120)
print("TSX VALUE SCREEN — ranked by composite value score (data: yfinance via McKechnie Terminal)")
print("="*120)
hdr = f"{'#':<3}{'TICK':<8}{'SCORE':>6}  {'P/E':>6}{'fP/E':>6}{'P/B':>6}{'EV/EB':>6}{'FCF%':>6}{'ROE':>6}{'DIV%':>6}{'UPS%':>6}  {'SECTOR':<22}"
print(hdr); print("-"*120)
for i,(_,r) in enumerate(res.head(25).iterrows(),1):
    divy = r['divy']
    dv = (divy if (divy is None or divy<1) else divy/100)
    print(f"{i:<3}{r['ticker']:<8}{r['score']:>6.2f}  "
          f"{fmt(r['pe'],'{:.1f}'):>6}{fmt(r['fpe'],'{:.1f}'):>6}{fmt(r['pb'],'{:.2f}'):>6}"
          f"{fmt(r['ev_ebitda'],'{:.1f}'):>6}{fmt(r['fcf_yield'],'{:.1f}'):>6}"
          f"{fmt(r['roe']*100 if r['roe'] is not None else None,'{:.0f}'):>6}"
          f"{fmt(dv*100 if dv is not None else None,'{:.1f}'):>6}"
          f"{fmt(r['upside'],'{:.0f}'):>6}  {(r['sector'] or '')[:22]:<22}")

print("\n\nTOP 12 — EVIDENCE DETAIL")
print("="*120)
for i,(_,r) in enumerate(res.head(12).iterrows(),1):
    print(f"\n{i}. {r['ticker']} — {r['name']}  [{r['sector']}]   score {r['score']}")
    print(f"   price ${fmt(r['price'],'{:.2f}')}  mktcap {fmt(r['mktcap']/1e9 if r['mktcap'] else None,'{:.1f}','B')}  analyst target ${fmt(r['tgt_mean'],'{:.2f}')} ({fmt(r['upside'],'{:+.0f}','%')})  rec={r['rec']}")
    if r['reasons']: print(f"   + {r['reasons']}")
    if r['flags']:   print(f"   ! {r['flags']}")

res.to_json('value_screen_ranked.json', orient='records', indent=2)
print("\nFull ranking -> value_screen_ranked.json")
