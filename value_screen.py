"""Numbers-only undervaluation screen for TSX stocks, using the same data source
(yfinance) as the McKechnie Terminal. Pulls fundamentals, computes valuation
metrics, and ranks by a composite value score."""
import yfinance as yf
import pandas as pd
import json, math, sys

CA_TICKERS = [
    'RY.TO','TD.TO','BNS.TO','BMO.TO','CM.TO','NA.TO','MFC.TO','SLF.TO','GWO.TO','IAG.TO',
    'CNR.TO','CP.TO','CNQ.TO','SU.TO','ENB.TO','TRP.TO','CVE.TO','MEG.TO','PEY.TO','ARX.TO',
    'ATD.TO','L.TO','MRU.TO','WN.TO','EMP-A.TO','DOL.TO','CTC-A.TO','GIL.TO','PIF.TO','QSR.TO',
    'BCE.TO','T.TO','RCI-B.TO','SHOP.TO','CSU.TO','OTEX.TO','BB.TO','KXS.TO','DSG.TO','ENGH.TO',
    'ABX.TO','AEM.TO','K.TO','FNV.TO','WPM.TO','FM.TO','TECK-B.TO','LUN.TO','CS.TO','HBM.TO',
    'BAM.TO','BIP-UN.TO','BEP-UN.TO','IFC.TO','ELF.TO','FFH.TO','POW.TO','SFC.TO',
    'WSP.TO','STN.TO','ATA.TO','BDT.TO','TF.TO','CAE.TO','NFI.TO','MDA.TO',
    'NTR.TO','VET.TO','PSK.TO','PKI.TO','TPX-B.TO','SAP.TO','WFG.TO','IFP.TO','CFP.TO',
    'H.TO','CHP-UN.TO','REI-UN.TO','SRU-UN.TO','CRT-UN.TO','DIR-UN.TO','AP-UN.TO','GRT-UN.TO','NWH-UN.TO',
    'PZA.TO','MTY.TO','BPF-UN.TO','ACO-X.TO','MFI.TO','TIH.TO','GFL.TO','BYD.TO','RBA.TO','MG.TO',
]

def num(x):
    try:
        if x is None: return None
        f = float(x)
        if math.isnan(f) or math.isinf(f): return None
        return f
    except Exception:
        return None

rows = []
for i, sym in enumerate(CA_TICKERS):
    try:
        info = yf.Ticker(sym).info
        if not info or len(info) < 5:
            continue
        r = {
            'ticker': sym.replace('.TO',''),
            'name': (info.get('shortName') or info.get('longName') or '')[:26],
            'sector': info.get('sector') or '',
            'price': num(info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')),
            'mktcap': num(info.get('marketCap')),
            'pe': num(info.get('trailingPE')),
            'fpe': num(info.get('forwardPE')),
            'pb': num(info.get('priceToBook')),
            'peg': num(info.get('pegRatio') or info.get('trailingPegRatio')),
            'ev_ebitda': num(info.get('enterpriseToEbitda')),
            'roe': num(info.get('returnOnEquity')),
            'margin': num(info.get('profitMargins')),
            'd2e': num(info.get('debtToEquity')),
            'divy': num(info.get('dividendYield')),
            'payout': num(info.get('payoutRatio')),
            'trail_eps': num(info.get('trailingEps')),
            'fwd_eps': num(info.get('forwardEps')),
            'fcf': num(info.get('freeCashflow')),
            'ocf': num(info.get('operatingCashflow')),
            'rev_growth': num(info.get('revenueGrowth')),
            'earn_growth': num(info.get('earningsGrowth')),
            'tgt_mean': num(info.get('targetMeanPrice')),
            'rec': info.get('recommendationKey') or '',
            'w52hi': num(info.get('fiftyTwoWeekHigh')),
            'w52lo': num(info.get('fiftyTwoWeekLow')),
        }
        # FCF yield = FCF / market cap
        if r['fcf'] and r['mktcap']:
            r['fcf_yield'] = r['fcf'] / r['mktcap'] * 100
        else:
            r['fcf_yield'] = None
        # upside to analyst target (a number, not an opinion narrative)
        if r['tgt_mean'] and r['price']:
            r['upside'] = (r['tgt_mean'] - r['price']) / r['price'] * 100
        else:
            r['upside'] = None
        # % off 52w high
        if r['w52hi'] and r['price']:
            r['off_hi'] = (r['price'] - r['w52hi']) / r['w52hi'] * 100
        else:
            r['off_hi'] = None
        rows.append(r)
        print(f"  [{i+1}/{len(CA_TICKERS)}] {r['ticker']:<8} ok", file=sys.stderr)
    except Exception as e:
        print(f"  [{i+1}/{len(CA_TICKERS)}] {sym} FAIL {e}", file=sys.stderr)

df = pd.DataFrame(rows)
df.to_json('value_screen_raw.json', orient='records', indent=2)
print(f"\nCollected {len(df)} tickers -> value_screen_raw.json")
