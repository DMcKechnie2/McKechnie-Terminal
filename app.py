from flask import Flask, render_template, request, jsonify
import yfinance as yf
import pandas as pd
import os

from groq import Groq

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))

_groq   = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))

def groq_call(system, user, max_tokens=80):
    """Single reusable Groq call. Returns stripped text or empty string on failure."""
    try:
        resp = _groq.chat.completions.create(
            model='llama-3.1-8b-instant',
            messages=[{'role': 'system', 'content': system},
                      {'role': 'user',   'content': user}],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip().strip('"')
    except Exception:
        return ''


def format_large_number(value):
    if value is None:
        return 'N/A'
    elif value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    elif value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    else:
        return f"${value / 1_000_000:.2f}M"



def scrape_macrotrends_fcf(ticker, company_name):
    """Fetch annual FCF from Macrotrends production API. Returns {year: raw_value}."""
    import requests as req
    import re, json

    base_ticker = ticker.split('.')[0].upper()

    url = 'https://www.macrotrends.net/production/stocks/desktop/fundamental_iframe.php'
    params = {
        't':         base_ticker,
        'type':      'free-cash-flow',
        'statement': 'cash-flow-statement',
        'freq':      'A',
        'sub':       '',
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': f'https://www.macrotrends.net/stocks/charts/{base_ticker.lower()}/stock/free-cash-flow',
    }

    r = req.get(url, params=params, headers=headers, timeout=10)

    # Data is in: var chartData = [{...}, ...]
    match = re.search(r'var\s+chartData\s*=\s*(\[.*?\])\s*[;\n]', r.text, re.DOTALL)
    if not match:
        return {}

    rows = json.loads(match.group(1))

    result = {}
    for row in rows:
        date_str = row.get('date', '')
        if not date_str:
            continue
        try:
            year = int(str(date_str)[:4])
            # v1 is the FCF value in millions for this year
            fcf_millions = float(row.get('v1', 0) or 0)
            result[year] = fcf_millions * 1_000_000
        except (ValueError, TypeError):
            continue

    return result


def scrape_macrotrends_profit_margin(ticker):
    """Fetch annual net profit margin % from Macrotrends. Returns {year: margin_pct}. US only."""
    import requests as req
    import re, json

    base_ticker = ticker.split('.')[0].upper()
    url = 'https://www.macrotrends.net/production/stocks/desktop/fundamental_iframe.php'
    params = {
        't':         base_ticker,
        'type':      'net-profit-margin',
        'statement': 'income-statement',
        'freq':      'A',
        'sub':       '',
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': f'https://www.macrotrends.net/stocks/charts/{base_ticker.lower()}/stock/net-profit-margin',
    }

    r = req.get(url, params=params, headers=headers, timeout=10)
    match = re.search(r'var\s+chartData\s*=\s*(\[.*?\])\s*[;\n]', r.text, re.DOTALL)
    if not match:
        return {}

    rows = json.loads(match.group(1))
    result = {}
    for row in rows:
        date_str = row.get('date', '')
        if not date_str:
            continue
        try:
            year = int(str(date_str)[:4])
            # v1 = net profit margin as a percentage
            val = float(row.get('v1', 0) or 0)
            result[year] = round(val, 2)
        except (ValueError, TypeError):
            continue

    return result



    return render_template('index.html')


# Cache canada drops so we don't re-fetch on every batch request
_tape_cache = {'data': [], 'ts': 0}
TAPE_BATCH_SIZE = 15

# Large list of TSX-listed tickers to pull quotes for
CA_TICKERS = [
    'RY.TO','TD.TO','BNS.TO','BMO.TO','CM.TO','NA.TO','MFC.TO','SLF.TO','GWO.TO','IAG.TO',
    'CNR.TO','CP.TO','CNQ.TO','SU.TO','ENB.TO','TRP.TO','CVE.TO','MEG.TO','PEY.TO','ARX.TO',
    'ATD.TO','L.TO','MRU.TO','WN.TO','EMP-A.TO','DOL.TO','CTC-A.TO','GIL.TO','PIF.TO','QSR.TO',
    'BCE.TO','T.TO','RCI-B.TO','SHOP.TO','CSU.TO','OTEX.TO','BB.TO','KXS.TO','DSG.TO','ENGH.TO',
    'ABX.TO','AEM.TO','K.TO','FNV.TO','WPM.TO','FM.TO','TECK-B.TO','LUN.TO','CS.TO','HBM.TO',
    'BAM.TO','BIP-UN.TO','BEP-UN.TO','BPY-UN.TO','IFC.TO','ELF.TO','FFH.TO','POW.TO','SFC.TO','BRW.TO',
    'SNC.TO','WSP.TO','STN.TO','ATA.TO','BDT.TO','TF.TO','CAE.TO','HII.TO','NFI.TO','MDA.TO',
    'NTR.TO','AGU.TO','VET.TO','PSK.TO','PKI.TO','TPX-B.TO','SAP.TO','WFG.TO','IFP.TO','CFP.TO',
    'H.TO','CHP-UN.TO','REI-UN.TO','SRU-UN.TO','CRT-UN.TO','DIR-UN.TO','AP-UN.TO','GRT-UN.TO','KMP-UN.TO','NWH-UN.TO',
    'PZA.TO','MTY.TO','BPF-UN.TO','ACO-X.TO','MFI.TO','TIH.TO','GFL.TO','BYD.TO','RBA.TO','MG.TO',
]

# Static fallback shown immediately while live data loads
TAPE_STATIC = [
    {'ticker':'RY','full_ticker':'RY.TO'},{'ticker':'TD','full_ticker':'TD.TO'},
    {'ticker':'BNS','full_ticker':'BNS.TO'},{'ticker':'BMO','full_ticker':'BMO.TO'},
    {'ticker':'CNR','full_ticker':'CNR.TO'},{'ticker':'ENB','full_ticker':'ENB.TO'},
    {'ticker':'SU','full_ticker':'SU.TO'},{'ticker':'SHOP','full_ticker':'SHOP.TO'},
    {'ticker':'CP','full_ticker':'CP.TO'},{'ticker':'BCE','full_ticker':'BCE.TO'},
    {'ticker':'CNQ','full_ticker':'CNQ.TO'},{'ticker':'ABX','full_ticker':'ABX.TO'},
    {'ticker':'MFC','full_ticker':'MFC.TO'},{'ticker':'ATD','full_ticker':'ATD.TO'},
    {'ticker':'TRP','full_ticker':'TRP.TO'},
]

def scrape_macrotrends_capex(ticker):
    """Fetch annual capex from Macrotrends. Returns {year: raw_value}. US only."""
    import requests as req
    import re, json

    base_ticker = ticker.split('.')[0].upper()
    url = 'https://www.macrotrends.net/production/stocks/desktop/fundamental_iframe.php'
    params = {
        't':         base_ticker,
        'type':      'capital-expenditures',
        'statement': 'cash-flow-statement',
        'freq':      'A',
        'sub':       '',
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': f'https://www.macrotrends.net/stocks/charts/{base_ticker.lower()}/stock/capital-expenditures',
    }

    r = req.get(url, params=params, headers=headers, timeout=10)
    match = re.search(r'var\s+chartData\s*=\s*(\[.*?\])\s*[;\n]', r.text, re.DOTALL)
    if not match:
        return {}

    rows = json.loads(match.group(1))
    result = {}
    for row in rows:
        date_str = row.get('date', '')
        if not date_str:
            continue
        try:
            year = int(str(date_str)[:4])
            val = float(row.get('v1', 0) or 0)
            result[year] = abs(val) * 1_000_000
        except (ValueError, TypeError):
            continue

    return result


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/crosslist', methods=['GET'])
def crosslist():
    """Find the same company listed on other exchanges."""
    tkkr = request.args.get('ticker', '').strip().upper()
    if not tkkr:
        return jsonify([])
    try:
        import requests as req
        # Search Yahoo for the company name to find cross-listings
        t = yf.Ticker(tkkr)
        info = t.info
        name = info.get('longName') or info.get('shortName', '')
        if not name:
            return jsonify([])

        # Use Yahoo search to find related listings
        url = 'https://query2.finance.yahoo.com/v1/finance/search'
        params = {'q': name, 'quotesCount': 10, 'newsCount': 0}
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = req.get(url, params=params, headers=headers, timeout=5)
        results = r.json().get('quotes', [])

        listings = []
        seen = set()
        current_exchange = info.get('exchange', '')

        for item in results:
            sym      = item.get('symbol', '')
            exchDisp = item.get('exchDisp', '') or item.get('exchange', '')
            exchCode = item.get('exchange', '')
            qtype    = item.get('quoteType', '')
            iname    = item.get('longname') or item.get('shortname', '')

            if qtype not in ('EQUITY',):
                continue
            if sym in seen or sym == tkkr:
                continue
            # Only include if name is similar enough
            if not iname or not any(w.lower() in iname.lower() for w in name.split()[:2] if len(w) > 3):
                continue

            seen.add(sym)
            listings.append({
                'ticker':      sym,
                'exchange':    exchDisp,
                'exchange_code': exchCode,
                'current':     False,
            })

        # Add current listing first
        listings.insert(0, {
            'ticker':   tkkr,
            'exchange': info.get('exchDisp') or current_exchange,
            'current':  True,
        })

        return jsonify(listings[:15])  # max 15 listings
    except Exception as e:
        return jsonify([{'ticker': tkkr, 'exchange': '', 'current': True}])


@app.route('/api/quote', methods=['GET'])
def single_quote():
    """Returns live price and daily change for a single ticker."""
    sym = request.args.get('ticker', '').strip().upper()
    if not sym:
        return jsonify({'error': 'No ticker'}), 400
    try:
        t  = yf.Ticker(sym)
        fi = t.fast_info
        last = getattr(fi, 'last_price', None)
        prev = getattr(fi, 'previous_close', None)
        if last and prev and prev != 0:
            chg = round(((last - prev) / prev) * 100, 2)
            return jsonify({'price': round(float(last), 2), 'change': chg})
        return jsonify({'price': None, 'change': None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tape', methods=['GET'])
def ticker_tape():
    """Returns Canadian stocks sorted by daily % drop, in batches."""
    import time
    try:
        batch = int(request.args.get('batch', 0))
        static = request.args.get('static') == '1'

        # Static mode — return placeholder immediately with no prices
        if static:
            return jsonify({
                'items': [{'ticker': s['ticker'], 'full_ticker': s['full_ticker'],
                           'price': 0, 'change': 0, 'loading': True} for s in TAPE_STATIC],
                'has_more': False, 'next_batch': 0, 'total_batches': 1,
            })

        if not _tape_cache['data'] or time.time() - _tape_cache['ts'] > 300:
            items = []
            try:
                # Bulk fetch — much faster than one-by-one
                symbols_str = ' '.join(CA_TICKERS)
                bulk = yf.download(
                    tickers=symbols_str,
                    period='2d',
                    interval='1d',
                    group_by='ticker',
                    auto_adjust=True,
                    progress=False,
                    threads=True,
                )
                for sym in CA_TICKERS:
                    try:
                        if sym in bulk.columns.get_level_values(0):
                            closes = bulk[sym]['Close'].dropna()
                        else:
                            closes = bulk['Close'][sym].dropna() if 'Close' in bulk else None
                        if closes is None or len(closes) < 2:
                            continue
                        prev  = float(closes.iloc[-2])
                        last  = float(closes.iloc[-1])
                        if prev == 0:
                            continue
                        chg = round(((last - prev) / prev) * 100, 2)
                        items.append({
                            'ticker':      sym.replace('.TO', ''),
                            'full_ticker': sym,
                            'price':       round(last, 2),
                            'change':      chg,
                        })
                    except Exception:
                        pass
            except Exception:
                # Fallback to one-by-one if bulk fails
                for sym in CA_TICKERS:
                    try:
                        t = yf.Ticker(sym)
                        fi = t.fast_info
                        last = getattr(fi, 'last_price', None)
                        prev = getattr(fi, 'previous_close', None)
                        if last and prev and prev != 0:
                            chg = round(((last - prev) / prev) * 100, 2)
                            items.append({
                                'ticker':      sym.replace('.TO', ''),
                                'full_ticker': sym,
                                'price':       round(float(last), 2),
                                'change':      chg,
                            })
                    except Exception:
                        pass

            _tape_cache['data'] = sorted(items, key=lambda x: x['change'])
            _tape_cache['ts'] = time.time()

        all_items = _tape_cache['data']
        total_batches = max(1, -(-len(all_items) // TAPE_BATCH_SIZE))
        start = batch * TAPE_BATCH_SIZE
        end   = start + TAPE_BATCH_SIZE

        return jsonify({
            'items': all_items[start:end],
            'has_more': end < len(all_items),
            'next_batch': batch + 1,
            'total_batches': total_batches,
        })
    except Exception as e:
        return jsonify({'items': [], 'has_more': False, 'next_batch': 0, 'error': str(e)})


@app.route('/api/screener/canada-drops', methods=['GET'])
def canada_drops():
    try:
        import requests as req
        url = 'https://query2.finance.yahoo.com/v1/finance/screener'
        payload = {
            "size": 100,
            "offset": 0,
            "sortField": "regularMarketChangePercent",
            "sortType": "ASC",
            "quoteType": "EQUITY",
            "query": {
                "operator": "AND",
                "operands": [
                    {"operator": "eq", "operands": ["exchange", "TOR"]},
                    {"operator": "gt", "operands": ["regularMarketVolume", 10000]}
                ]
            },
            "userId": "",
            "userIdType": "guid"
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/json',
        }
        r = req.post(url, json=payload, headers=headers, timeout=10)
        data = r.json()
        quotes = data.get('finance', {}).get('result', [{}])[0].get('quotes', [])
        results = []
        for q in quotes:
            chg = q.get('regularMarketChangePercent', 0) or 0
            results.append({
                'ticker':  q.get('symbol', ''),
                'name':    q.get('longName') or q.get('shortName', ''),
                'price':   round(float(q.get('regularMarketPrice', 0) or 0), 2),
                'change':  round(float(chg), 2),
                'volume':  q.get('regularMarketVolume', 0),
                'mkt_cap': format_large_number(q.get('marketCap')),
            })
        return jsonify({'results': results, 'count': len(results)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ── Daily Movers ──────────────────────────────────────────────────────────────
import threading as _threading
from yfinance import EquityQuery as _EQ

_movers_cache = {
    'TSX':    {'gainers': [], 'losers': [], 'ts': 0},
    'NYSE':   {'gainers': [], 'losers': [], 'ts': 0},
    'NASDAQ': {'gainers': [], 'losers': [], 'ts': 0},
}
MOVERS_TTL = 300

_EXCHANGE_CODE = {'TSX': 'TOR', 'NYSE': 'NYQ', 'NASDAQ': 'NMS'}
# Min market cap / volume to filter out illiquid micro-caps
_EXCHANGE_FILTERS = {
    'TSX':    (100_000_000, 50_000),
    'NYSE':   (500_000_000, 100_000),
    'NASDAQ': (500_000_000, 100_000),
}


def _screen_movers(exchange, asc, count=100):
    """Return top movers for an exchange via yfinance screener. asc=True → losers.
    Paginates in batches of 25 (API maximum) until count is reached."""
    ex_code = _EXCHANGE_CODE.get(exchange, 'NYQ')
    min_cap, min_vol = _EXCHANGE_FILTERS.get(exchange, (500_000_000, 100_000))
    query = _EQ('AND', [
        _EQ('eq',  ['exchange', ex_code]),
        _EQ('gte', ['intradaymarketcap', min_cap]),
        _EQ('gte', ['dayvolume', min_vol]),
    ])
    PAGE = 25
    all_quotes = []
    for offset in range(0, count, PAGE):
        try:
            result = yf.screen(query, sortField='percentchange', sortAsc=asc,
                               count=PAGE, offset=offset)
            page = result.get('quotes', []) if result else []
            all_quotes.extend(page)
            if len(page) < PAGE:
                break  # no more results
        except Exception:
            break
    return all_quotes


def _quotes_to_items(quotes, exchange):
    items = []
    for q in quotes:
        sym = q.get('symbol', '')
        if not sym:
            continue
        display = sym.replace('.TO', '').replace('-', '.')
        items.append({
            'ticker':      display,
            'full_ticker': sym,
            'name':        q.get('longName') or q.get('shortName') or q.get('displayName') or display,
            'price':       round(float(q.get('regularMarketPrice', 0)), 2),
            'change':      round(float(q.get('regularMarketChangePercent', 0)), 2),
            'volume':      q.get('regularMarketVolume', 0),
            'mkt_cap':     format_large_number(q.get('marketCap')),
            'exchange':    exchange,
            'enriched':    True,
        })
    return items


def _refresh_movers(exchange):
    import time

    raw_gainers, raw_losers = [], []
    tg = _threading.Thread(target=lambda: raw_gainers.extend(_screen_movers(exchange, asc=False, count=100)), daemon=True)
    tl = _threading.Thread(target=lambda: raw_losers.extend(_screen_movers(exchange, asc=True,  count=100)), daemon=True)
    tg.start(); tl.start()
    tg.join(timeout=30); tl.join(timeout=30)

    gainers = sorted([i for i in _quotes_to_items(raw_gainers, exchange) if i['change'] > 0], key=lambda x: x['change'], reverse=True)
    losers  = sorted([i for i in _quotes_to_items(raw_losers,  exchange) if i['change'] < 0], key=lambda x: x['change'])

    if not gainers and not losers:
        return

    _movers_cache[exchange]['gainers'] = gainers
    _movers_cache[exchange]['losers']  = losers
    _movers_cache[exchange]['ts']      = time.time()


@app.route('/api/movers', methods=['GET'])
def movers():
    import time
    exchange  = request.args.get('exchange', 'TSX').upper()
    direction = request.args.get('direction', 'gainers').lower()
    limit     = min(int(request.args.get('limit', 25)), 100)
    if exchange not in _movers_cache:
        exchange = 'TSX'
    cache = _movers_cache[exchange]
    if not cache['gainers'] and not cache['losers'] or time.time() - cache['ts'] > MOVERS_TTL:
        _refresh_movers(exchange)
    results = cache[direction][:limit]
    return jsonify({
        'results':   results,
        'count':     len(results),
        'exchange':  exchange,
        'direction': direction,
        'enriched':  True,
    })


@app.route('/api/debug/dividend', methods=['GET'])
def debug_dividend():
    tkkr = request.args.get('ticker', 'AAPL').strip().upper()
    t = yf.Ticker(tkkr)
    info = t.info
    return jsonify({
        'dividendYield':               info.get('dividendYield'),
        'trailingAnnualDividendYield': info.get('trailingAnnualDividendYield'),
        'dividendRate':                info.get('dividendRate'),
        'trailingAnnualDividendRate':  info.get('trailingAnnualDividendRate'),
    })


@app.route('/api/debug/cashflow', methods=['GET'])
def debug_cashflow():
    tkkr = request.args.get('ticker', '').strip().upper()
    if not tkkr:
        return jsonify({'error': 'No ticker'}), 400
    try:
        t = yf.Ticker(tkkr)
        cf = t.cashflow
        return jsonify({
            'rows_available': cf.index.tolist() if not cf.empty else [],
            'columns': [str(c) for c in cf.columns.tolist()] if not cf.empty else [],
            'empty': cf.empty,
        })
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/debug/macrotrends', methods=['GET'])
def debug_macrotrends():
    tkkr = request.args.get('ticker', '').strip().upper()
    name = request.args.get('name', 'unknown').strip()
    if not tkkr:
        return jsonify({'error': 'No ticker'}), 400
    try:
        fcf_result    = scrape_macrotrends_fcf(tkkr, name)
        capex_result  = scrape_macrotrends_capex(tkkr)
        pm_result     = scrape_macrotrends_profit_margin(tkkr)
        return jsonify({
            'ticker': tkkr,
            'fcf':   {str(k): round(v/1e9, 2) for k, v in sorted(fcf_result.items())},
            'capex': {str(k): round(v/1e9, 2) for k, v in sorted(capex_result.items())},
            'pm':    {str(k): v for k, v in sorted(pm_result.items())},
        })
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/debug/capex-raw', methods=['GET'])
def debug_capex_raw():
    import requests as req, re, json
    tkkr = request.args.get('ticker', 'AAPL').strip().upper()
    url = 'https://www.macrotrends.net/production/stocks/desktop/fundamental_iframe.php'
    params = {'t': tkkr, 'type': 'cash-flow-from-investing-activities', 'statement': 'cash-flow-statement', 'freq': 'A', 'sub': ''}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': f'https://www.macrotrends.net/stocks/charts/{tkkr.lower()}/stock/free-cash-flow',
    }
    r = req.get(url, params=params, headers=headers, timeout=10)
    match = re.search(r'var\s+chartData\s*=\s*(\[.*?\])\s*[;\n]', r.text, re.DOTALL)
    if match:
        rows = json.loads(match.group(1))
        return jsonify({'sample': rows[:3], 'total': len(rows)})
    return jsonify({'found': False})


OLLAMA_URL   = 'http://localhost:11434/api/chat'
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2-math:7b')

@app.route('/api/chat', methods=['POST'])
def chat():
    import requests as req
    data     = request.json or {}
    messages = data.get('messages', [])
    context  = data.get('context', '')
    if not messages:
        return jsonify({'error': 'No messages provided'}), 400

    system_prompt = (
        'You are a financial analysis assistant inside McKechnie Terminal, a personal stock research tool. '
        'Help with DCF valuations, financial ratios, investment math, and portfolio analysis. '
        'Be concise and direct. Show working for calculations. Use $ and % where relevant.'
    )
    if context:
        system_prompt += f'\n\nCurrent context: {context}'

    payload = {
        'model':    OLLAMA_MODEL,
        'messages': [{'role': 'system', 'content': system_prompt}] + messages,
        'stream':   False,
        'options':  {'temperature': 0.2, 'num_predict': 512},
    }
    try:
        r = req.post(OLLAMA_URL, json=payload, timeout=60)
        r.raise_for_status()
        reply = r.json()['message']['content'].strip()
        return jsonify({'reply': reply})
    except req.exceptions.ConnectionError:
        return jsonify({'error': 'Ollama is not running. Start it with: ollama serve'}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/search', methods=['GET'])
def search_tickers():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    try:
        import requests as req
        url = 'https://query2.finance.yahoo.com/v1/finance/search'
        params = {'q': query, 'quotesCount': 8, 'newsCount': 0, 'enableFuzzyQuery': True}
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = req.get(url, params=params, headers=headers, timeout=5)
        data = r.json()
        results = []
        for item in data.get('quotes', []):
            qtype = item.get('quoteType', '')
            if qtype not in ('EQUITY', 'ETF', 'MUTUALFUND', 'INDEX'):
                continue
            results.append({
                'ticker':   item.get('symbol', ''),
                'name':     item.get('longname') or item.get('shortname', ''),
                'sector':   item.get('sector') or qtype.title(),
                'exchange': item.get('exchDisp', ''),
            })
        return jsonify(results)
    except Exception:
        return jsonify([])


@app.route('/api/chart', methods=['GET'])
def get_chart():
    tkkr     = request.args.get('ticker', '').strip().upper()
    range_   = request.args.get('range', '1mo')
    interval = request.args.get('interval', '1d')
    if not tkkr:
        return jsonify({'error': 'No ticker'}), 400
    try:
        ticker = yf.Ticker(tkkr)
        hist   = ticker.history(period=range_, interval=interval)
        if hist.empty:
            return jsonify({'prices': [], 'dates': []})
        prices = [round(float(v), 4) for v in hist['Close'].tolist()]
        dates  = [str(d.date()) if hasattr(d, 'date') else str(d)[:10] for d in hist.index]

        # For 1D, use previous close as the baseline for % change
        prev_close = None
        if range_ == '1d':
            try:
                fi = ticker.fast_info
                prev_close = round(float(getattr(fi, 'previous_close', None) or 0), 4) or None
            except Exception:
                pass

        return jsonify({'prices': prices, 'dates': dates, 'prev_close': prev_close})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _do_get_stock(tkkr):
    """All yfinance work for a stock lookup — runs in a daemon thread with a hard timeout."""
    ticker = yf.Ticker(tkkr)
    info = ticker.info

    try:
        if not info or len(info) < 5:
            raise ValueError(f"Could not retrieve data for '{tkkr}'. Check the symbol.")

        # --- Core fields ---
        name     = info.get('longName') or info.get('shortName') or info.get('symbol', 'N/A')
        price    = info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose') or None
        currency = info.get('currency', 'USD')

        # Override price with fast_info for real-time consistency with watchlist quotes
        try:
            fi = ticker.fast_info
            fast_price = getattr(fi, 'last_price', None)
            if fast_price:
                price = float(fast_price)
        except Exception:
            pass
        sector   = info.get('sector') or info.get('industry') or 'N/A'
        exchange = info.get('exchange') or info.get('fullExchangeName') or 'N/A'
        pe_ratio     = info.get('trailingPE') or None
        forward_pe   = info.get('forwardPE')  or None
        week52_high  = info.get('fiftyTwoWeekHigh') or info.get('52WeekHigh') or None
        week52_low   = info.get('fiftyTwoWeekLow')  or info.get('52WeekLow')  or None
        market_cap = format_large_number(info.get('marketCap') or info.get('regularMarketCap'))
        raw_debt   = info.get('totalDebt') or info.get('longTermDebt') or 0
        raw_cash   = info.get('totalCash') or info.get('cash') or 0
        net_debt   = raw_debt - raw_cash
        debt       = format_large_number(raw_debt)
        cash       = format_large_number(raw_cash) if raw_cash else 'N/A'

        # --- Capex ---
        capex_ttm_str = 'N/A'
        capex_by_year = []
        try:
            cashflow = ticker.cashflow
            yf_capex = {}
            if 'Capital Expenditure' in cashflow.index:
                capex_row = cashflow.loc['Capital Expenditure']
                ttm_val = capex_row.iloc[0]
                if ttm_val is not None and not pd.isna(ttm_val):
                    capex_ttm_str = format_large_number(abs(float(ttm_val)))
                for date, val in sorted(capex_row.items()):
                    if val is not None and not pd.isna(val):
                        yr = date.year if date.month >= 4 else date.year - 1
                        yf_capex[yr] = abs(float(val))

            # Quarterly cashflow aggregated by year for extended history
            try:
                q_cashflow = ticker.quarterly_cashflow
                if 'Capital Expenditure' in q_cashflow.index:
                    q_capex_row = q_cashflow.loc['Capital Expenditure']
                    q_by_year = {}
                    for date, val in q_capex_row.items():
                        if val is not None and not pd.isna(val):
                            yr = date.year if date.month >= 4 else date.year - 1
                            q_by_year[yr] = q_by_year.get(yr, 0) + abs(float(val))
                    for yr, val in q_by_year.items():
                        if yr not in yf_capex:
                            yf_capex[yr] = val
            except Exception:
                pass

            capex_by_year = [{'year': yr, 'raw': v, 'value': format_large_number(v)} for yr, v in sorted(yf_capex.items())]
        except Exception:
            pass

        # Daily change — pulled directly so it matches Yahoo exactly
        day_change     = info.get('regularMarketChange') or info.get('currentPrice', 0) - info.get('previousClose', 0) or None
        day_change_pct = info.get('regularMarketChangePercent') or None
        if day_change_pct is None and day_change and info.get('previousClose'):
            day_change_pct = (day_change / info.get('previousClose')) * 100

        # --- Profit Margin ---
        profit_margin = info.get('profitMargins') or info.get('netProfitMargin') or None
        profit_margin_str = 'N/A'
        if profit_margin:
            val = profit_margin if profit_margin > 1 else profit_margin * 100
            profit_margin_str = f"{val:.2f}%"
        else:
            try:
                net_income = ticker.financials.loc['Net Income'].iloc[0]
                revenue    = ticker.financials.loc['Total Revenue'].iloc[0]
                if revenue and revenue != 0:
                    profit_margin_str = f"{(net_income / revenue) * 100:.2f}% (calc)"
            except Exception:
                pass

        # --- Price to Book ---
        pb_ratio = info.get('priceToBook') or None
        pb_str = 'N/A'
        if pb_ratio:
            pb_str = f"{pb_ratio:.2f}"
        elif info.get('bookValue') and price:
            pb_str = f"{price / info.get('bookValue'):.2f} (calc)"

        # --- Dividend Yield ---
        # dividendYield = already a % value (0.41 = 0.41%)
        # trailingAnnualDividendYield = decimal (0.0041 = 0.41%)
        raw_dy      = info.get('dividendYield')
        trailing_dy = info.get('trailingAnnualDividendYield')
        dividend_rate = info.get('dividendRate') or info.get('trailingAnnualDividendRate') or None
        div_yield_str = 'N/A'
        if raw_dy is not None:
            div_yield_str = f"{float(raw_dy):.2f}%"
        elif trailing_dy is not None:
            div_yield_str = f"{float(trailing_dy) * 100:.2f}%"
        elif dividend_rate and price:
            div_yield_str = f"{(dividend_rate / price) * 100:.2f}% (calc)"

        # --- Dividend History (every payment) ---
        dividend_history = []
        try:
            divs = ticker.dividends
            if not divs.empty:
                divs.index = divs.index.tz_localize(None)
                for date, amount in divs.items():
                    dividend_history.append({
                        'date': str(date.date()),
                        'amount': round(float(amount), 4)
                    })
        except Exception:
            pass


        # --- Free Cash Flow (Annual) — yfinance + Macrotrends merge ---
        fcf_annual = []
        try:
            cashflow  = ticker.cashflow
            # Use Free Cash Flow row directly if available (most accurate)
            if 'Free Cash Flow' in cashflow.index:
                fcf = cashflow.loc['Free Cash Flow']
                for date, value in sorted(fcf.items()):
                    if value is not None and not pd.isna(value):
                        fcf_annual.append({'year': date.year, 'raw': float(value), 'value': format_large_number(value)})
            else:
                operating = cashflow.loc['Operating Cash Flow'] if 'Operating Cash Flow' in cashflow.index else None
                capex     = cashflow.loc['Capital Expenditure']  if 'Capital Expenditure'  in cashflow.index else None
                if operating is not None:
                    fcf = (operating + capex) if capex is not None else operating
                    for date, value in sorted(fcf.items()):
                        if value is not None and not pd.isna(value):
                            label = format_large_number(value) + ('' if capex is not None else ' (OCF)')
                            fcf_annual.append({'year': date.year, 'raw': float(value), 'value': label})
        except Exception:
            pass

        # Fallback: aggregate quarterly cashflow into annual to get more years
        try:
            yf_years = {r['year'] for r in fcf_annual}
            qcf = ticker.quarterly_cashflow
            if 'Free Cash Flow' in qcf.index:
                q_fcf = qcf.loc['Free Cash Flow']
                by_year = {}
                for date, value in q_fcf.items():
                    if value is not None and not pd.isna(value):
                        yr = date.year
                        by_year[yr] = by_year.get(yr, 0) + float(value)
                for yr, total in by_year.items():
                    if yr not in yf_years:
                        fcf_annual.append({'year': yr, 'raw': total, 'value': format_large_number(total)})
            else:
                q_operating = qcf.loc['Operating Cash Flow'] if 'Operating Cash Flow' in qcf.index else None
                q_capex     = qcf.loc['Capital Expenditure']  if 'Capital Expenditure'  in qcf.index else None
                if q_operating is not None:
                    q_fcf = (q_operating + q_capex) if q_capex is not None else q_operating
                    by_year = {}
                    for date, value in q_fcf.items():
                        if value is not None and not pd.isna(value):
                            yr = date.year
                            by_year[yr] = by_year.get(yr, 0) + float(value)
                    for yr, total in by_year.items():
                        if yr not in yf_years:
                            label = format_large_number(total) + ('' if q_capex is not None else ' (OCF)')
                            fcf_annual.append({'year': yr, 'raw': total, 'value': label})
            fcf_annual.sort(key=lambda r: r['year'])
        except Exception:
            pass

        # Macrotrends scrape — only for US tickers, fills in older years not in yfinance
        try:
            if '.' not in tkkr:
                mt_data = scrape_macrotrends_fcf(tkkr, name)
                yf_years = {r['year'] for r in fcf_annual}
                for year, raw in mt_data.items():
                    if year not in yf_years:
                        fcf_annual.append({'year': year, 'raw': raw, 'value': format_large_number(raw)})
                fcf_annual.sort(key=lambda r: r['year'])
        except Exception:
            pass

        # Build a quick lookup from fcf_annual for use in payout ratio
        fcf_by_year = {r['year']: r['raw'] for r in fcf_annual}
        fcf_fmt_by_year = {r['year']: r['value'] for r in fcf_annual}

        # --- Payout Ratio by Year (FCF-based) ---
        payout_by_year = []
        try:
            divs2 = ticker.dividends
            divs2.index = divs2.index.tz_localize(None)
            annual_divs = divs2.groupby(divs2.index.year).sum()

            shares = info.get('sharesOutstanding') or info.get('impliedSharesOutstanding') or None

            # Get total dividends paid from cashflow statement
            total_divs_by_year = {}
            try:
                cf = ticker.cashflow
                if 'Common Stock Dividend Paid' in cf.index:
                    div_row = cf.loc['Common Stock Dividend Paid']
                elif 'Cash Dividends Paid' in cf.index:
                    div_row = cf.loc['Cash Dividends Paid']
                else:
                    div_row = None
                if div_row is not None:
                    for date, val in div_row.items():
                        if val is not None and not pd.isna(val):
                            yr = date.year if date.month >= 4 else date.year - 1
                            total_divs_by_year[yr] = abs(float(val))
            except Exception:
                pass

            all_years = sorted(set(list(fcf_by_year.keys()) + list(annual_divs.index.tolist())))

            for year in all_years:
                fcf_val = fcf_by_year.get(year, None)
                fcf_fmt = fcf_fmt_by_year.get(year, None)
                div_per_share = annual_divs.get(year, None)

                total_div = total_divs_by_year.get(year, None)
                if total_div is None and div_per_share is not None and shares:
                    total_div = float(div_per_share) * shares

                if div_per_share is None or pd.isna(div_per_share):
                    if fcf_val is not None:
                        payout_by_year.append({'year': year, 'payout': None, 'note': 'No dividend data', 'fcf': fcf_fmt})
                elif fcf_val is None:
                    payout_by_year.append({'year': year, 'payout': None, 'note': 'No FCF data', 'div': round(float(div_per_share), 4)})
                elif fcf_val <= 0:
                    payout_by_year.append({'year': year, 'payout': None, 'note': 'FCF negative', 'div': round(float(div_per_share), 4), 'fcf': fcf_fmt})
                else:
                    payout = round((total_div / fcf_val) * 100, 2) if total_div else None
                    payout_by_year.append({
                        'year': year,
                        'payout': payout,
                        'div': round(float(div_per_share), 4),
                        'fcf': fcf_fmt
                    })
        except Exception:
            pass

        news = []   # loaded separately via /api/news

        # --- Profit Margin by Year ---
        profit_margin_by_year = []
        try:
            fin = ticker.financials
            yf_pm = {}
            if 'Net Income' in fin.index and 'Total Revenue' in fin.index:
                net_income_row = fin.loc['Net Income']
                revenue_row    = fin.loc['Total Revenue']
                for date in sorted(net_income_row.index):
                    yr  = date.year if date.month >= 4 else date.year - 1
                    ni  = net_income_row.get(date)
                    rev = revenue_row.get(date)
                    if ni is not None and rev is not None and not pd.isna(ni) and not pd.isna(rev) and float(rev) != 0:
                        yf_pm[yr] = round((float(ni) / float(rev)) * 100, 2)

            # Macrotrends — US tickers only, yfinance years take priority
            try:
                if '.' not in tkkr:
                    mt_pm = scrape_macrotrends_profit_margin(tkkr)
                    for yr, margin in mt_pm.items():
                        if yr not in yf_pm:  # never overwrite yfinance data
                            yf_pm[yr] = margin
            except Exception:
                pass

            profit_margin_by_year = [{'year': yr, 'margin': m} for yr, m in sorted(yf_pm.items())]
        except Exception:
            pass

        # --- Revenue by Year ---
        revenue_ttm_str = 'N/A'
        revenue_by_year = []
        try:
            fin = ticker.financials
            if 'Total Revenue' in fin.index:
                rev_row = fin.loc['Total Revenue']
                ttm_val = rev_row.iloc[0]
                if ttm_val is not None and not pd.isna(ttm_val):
                    revenue_ttm_str = format_large_number(float(ttm_val))
                yf_rev = {}
                for date, val in sorted(rev_row.items()):
                    if val is not None and not pd.isna(val):
                        yr = date.year if date.month >= 4 else date.year - 1
                        yf_rev[yr] = float(val)
                try:
                    q_fin = ticker.quarterly_financials
                    if 'Total Revenue' in q_fin.index:
                        q_rev_row = q_fin.loc['Total Revenue']
                        q_by_year = {}
                        for date, val in q_rev_row.items():
                            if val is not None and not pd.isna(val):
                                yr = date.year if date.month >= 4 else date.year - 1
                                q_by_year[yr] = q_by_year.get(yr, 0) + float(val)
                        for yr, val in q_by_year.items():
                            if yr not in yf_rev:
                                yf_rev[yr] = val
                except Exception:
                    pass
                revenue_by_year = [{'year': yr, 'raw': v, 'value': format_large_number(v)} for yr, v in sorted(yf_rev.items())]
        except Exception:
            pass

        # Shares outstanding (for calculator hint)
        shares_outstanding = info.get('sharesOutstanding') or info.get('impliedSharesOutstanding') or None


        return {
            'ticker': tkkr,
            'name': name,
            'price': f"{price:.2f}" if price else 'N/A',
            'currency': currency,
            'market_cap': market_cap,
            'sector': sector,
            'exchange': exchange,
            'pe_ratio': f"{pe_ratio:.2f}" if pe_ratio else 'N/A',
            'forward_pe': f"{forward_pe:.2f}" if forward_pe else 'N/A',
            'week52_high': f"{week52_high:.2f}" if week52_high else 'N/A',
            'week52_low':  f"{week52_low:.2f}"  if week52_low  else 'N/A',
            'day_change_pct': round(float(day_change_pct), 2) if day_change_pct else None,
            'debt': debt,
            'cash': cash,
            'capex_ttm': capex_ttm_str,
            'capex_by_year': capex_by_year,
            'revenue_ttm': revenue_ttm_str,
            'revenue_by_year': revenue_by_year,
            'profit_margin': profit_margin_str,
            'profit_margin_by_year': profit_margin_by_year,
            'pb_ratio': pb_str,
            'div_yield': div_yield_str,
            'dividend_history': dividend_history,
            'payout_by_year': payout_by_year,
            'fcf_annual': fcf_annual,
            'news': news,
            'shares': shares_outstanding,
        }
    except Exception:
        raise

@app.route('/api/stock', methods=['GET'])
def get_stock():
    import threading as _threading, time as _time
    tkkr = request.args.get('ticker', '').strip().upper()
    if not tkkr:
        return jsonify({'error': 'No ticker provided'}), 400
    result  = [None]
    exc     = [None]
    done    = _threading.Event()
    def _run():
        try:
            result[0] = _do_get_stock(tkkr)
        except Exception as e:
            exc[0] = e
        finally:
            done.set()
    _threading.Thread(target=_run, daemon=True).start()
    if not done.wait(timeout=25):
        return jsonify({'error': 'Request timed out — try again.'}), 504
    if exc[0]:
        return jsonify({'error': str(exc[0])}), 500
    return jsonify(result[0])


@app.route('/api/news', methods=['GET'])
def get_news():
    import time as _time
    tkkr = request.args.get('ticker', '').strip().upper()
    name = request.args.get('name', '').strip()
    if not tkkr:
        return jsonify({'news': []})

    try:
        import datetime as _dt, re as _re, requests as _req
        from bs4 import BeautifulSoup
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

        # Run the entire news pipeline in a thread with a hard 25-second deadline
        import threading
        _result = [None]
        _done   = threading.Event()
        _t0 = _time.time()
        print(f'[NEWS] {tkkr}: starting pipeline', flush=True)

        def _run():
            try:
                _result[0] = _build_news(tkkr, name)
            except Exception as e:
                print(f'[NEWS] {tkkr}: pipeline exception: {e}', flush=True)
                _result[0] = []
            finally:
                _done.set()

        threading.Thread(target=_run, daemon=True).start()
        finished = _done.wait(timeout=25)
        elapsed = _time.time() - _t0
        if not finished:
            print(f'[NEWS] {tkkr}: TIMED OUT after {elapsed:.1f}s', flush=True)
        else:
            print(f'[NEWS] {tkkr}: done in {elapsed:.1f}s, {len(_result[0] or [])} items', flush=True)
        return jsonify({'news': _result[0] or []})

    except Exception as e:
        print(f'[NEWS] {tkkr}: outer exception: {e}', flush=True)
        return jsonify({'news': []})


def _build_news(tkkr, name=''):
    import datetime as _dt, re as _re, requests as _req, time as _time
    from bs4 import BeautifulSoup
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

    _t = _time.time
    _t0 = _t()

    ticker = yf.Ticker(tkkr)
    name   = name or tkkr
    print(f'[NEWS] {tkkr}: ticker created in {_t()-_t0:.2f}s', flush=True)

    def _fetch_article_text(url):
        try:
            r = _req.get(url, timeout=3, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                return ''
            soup = BeautifulSoup(r.text, 'html.parser')
            for tag in soup(['script','style','nav','header','footer','aside','figure','noscript']):
                tag.decompose()
            body = soup.find('article') or soup.find(attrs={'class': lambda c: c and 'article-body' in ' '.join(c)}) or soup.find('main') or soup.body
            if not body:
                return ''
            paras = [p.get_text(' ', strip=True) for p in body.find_all('p') if len(p.get_text(strip=True)) > 40]
            return ' '.join(paras[:40])
        except Exception:
            return ''

    def _smart_summary(text, name_words, ticker_sym, budget=180):
        action = {'announced','announces','launches','launched','reports','reported','beats','beat',
                  'misses','missed','cuts','cut','raises','raised','acquires','acquired','expands',
                  'expanded','files','filed','settles','settled','recalls','recalled','approves',
                  'approved','rejects','rejected','wins','won','loses','lost','appoints','appointed',
                  'resigns','resigned','partners','agreed','completes','completed','suspends',
                  'suspended','withdraws','withdrew','fined','charged','investigated','awarded'}
        filler = {'click here','read more','sign up','subscribe','according to sources',
                  'sources familiar','people familiar','it is unclear','remains to be seen',
                  'did not respond','could not be reached','no comment'}
        sents = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', text) if 25 < len(s.strip()) < 250]
        if not sents:
            return text[:budget]
        scored = []
        for i, s in enumerate(sents):
            sl = s.lower()
            score  = sum(8  for w in name_words if w in sl)
            score += 10 if ticker_sym.lower() in sl else 0
            score += sum(7  for a in action if a in sl)
            score += len(_re.findall(r'\d+\.?\d*\s*(%|\$|bn|million|billion)', sl)) * 6
            score += 4 if _re.search(r'\b(q[1-4]|fy\d{2,4}|full.year|fiscal)', sl) else 0
            score += max(0, 3 - i)
            score -= 15 if len(s) > 220 else 0
            score -= sum(8 for f in filler if f in sl)
            score -= 8  if s.startswith('"') else 0
            scored.append((score, i, s))
        scored.sort(key=lambda x: (-x[0], x[1]))
        best = scored[0][2]
        return best if len(best) <= budget else best[:budget].rsplit(' ', 1)[0] + '…'

    def _to_headline(text, short_name=''):
        text = _re.sub(r'^\([\w\.\-]+\)\s*[-–—]?\s*', '', text).strip()
        text = _re.sub(r'^[\"\u201c]', '', text).strip()
        text = _re.sub(r',?\s+according to [^.]{0,80}$', '', text, flags=_re.I).strip()
        text = _re.sub(r'[,;:\s]+$', '', text).strip()
        if not text:
            return ''
        system = (
            'You rewrite financial news sentences into concise, accurate headlines. '
            'Rules: max 15 words, title case, active voice, no attribution (remove "said/according to"), '
            'keep all specific numbers and figures, replace vague pronouns with the company name, '
            'output only the headline with no quotes or explanation.'
        )
        result = groq_call(system, f'Company: {short_name}\nSentence: {text}', max_tokens=60)
        return result if result else (text[0].upper() + text[1:])

    _now   = _dt.datetime.utcnow()
    _tier1 = {'reuters','bloomberg','wall street journal','wsj','financial times','ft','cnbc',"barron's",'the economist'}
    _tier2 = {'yahoo finance','marketwatch','seeking alpha','motley fool','business insider','fortune','forbes','associated press'}
    _action_kw = ['earnings','revenue','profit','loss','beat','miss','guidance','dividend','merger','acquisition',
                  'buyout','ipo','sec','fda','layoff','recall','settlement','buyback','split','bankruptcy',
                  'default','launch','partnership','contract','deal','appoints','resigns','expands','cuts',
                  'investigation','approved','rejected','fine','penalty','charges','files','reports',
                  'announces','raises','lowers','suspends','withdraws','completes','agrees','wins','loses']
    _hard_skip = [
        r'\bshould (you |i )?(buy|sell|hold|invest|own|avoid)\b',
        r'\b(top|best|worst)\s+\d*\s*(stocks?|picks?|buys?|investments?)\b',
        r'\bwhy\s+(i\s+)?(bought|sold|own|like|hate|am (buying|selling))\b',
        r'\b\d+\s+reason(s)?\s+(to|why)\b',
        r'\b(is|are)\s+[\w\s]{2,30}\s+(a\s+)?(good|great|bad|terrible)\s+(buy|investment|stock|bet)\b',
        r'\bhere.s why\b', r'\b(my|our)\s+(top|best|favourite|portfolio)\b',
        r'\bpassive income\b', r'\b(millionaire|retire\s+early|financial freedom)\b',
        r'\bprice target\b', r'\b(bull|bear)\s+case\b', r'\bsent(iment)?\s+anal(ysis)?\b',
        r'\bweek(ly)?\s+(wrap|recap|roundup|picks?)\b', r'\bmarket\s+(wrap|recap|roundup|movers?)\b',
        r'\bwhat\s+(analysts?|wall\s+street)\s+(think|say|expect)\b',
        r'\brated\s+(buy|sell|hold|overweight|underweight|outperform|underperform)\b',
        r'\b(overweight|underweight|outperform|underperform|neutral)\s+rating\b',
    ]
    _opinion_sources = {'seeking alpha','motley fool','investopedia','the motley fool'}
    _stop       = {'inc','corp','ltd','llc','plc','the','and','group'}
    _name_words = {w.lower() for w in (name or '').split() if len(w) > 2 and w.lower() not in _stop}
    _short_name = name or tkkr

    _t1 = _t()
    raw_news = ticker.news or []
    print(f'[NEWS] {tkkr}: ticker.news fetched in {_t()-_t1:.2f}s, {len(raw_news)} raw items', flush=True)

    news = []
    for item in raw_news[:20]:
        c       = item.get('content', {}) or {}
        title   = c.get('title', item.get('title', ''))
        url     = c.get('canonicalUrl', {}).get('url', item.get('link', ''))
        if not (title and url):
            continue
        source  = c.get('provider', {}).get('displayName', item.get('publisher', ''))
        summary = c.get('summary', '') or c.get('description', '') or ''
        thumb   = ''
        try:
            res = (c.get('thumbnail') or item.get('thumbnail') or {}).get('resolutions') or []
            if res: thumb = max(res, key=lambda r: r.get('width', 0)).get('url', '')
        except Exception: pass

        raw_t  = c.get('pubDate', '') or item.get('providerPublishTime', '')
        pub_ts = None
        if isinstance(raw_t, int):
            pub_ts = _dt.datetime.utcfromtimestamp(raw_t)
            date   = pub_ts.strftime('%b %d, %Y')
        elif raw_t:
            try: pub_ts = _dt.datetime.strptime(str(raw_t)[:19], '%Y-%m-%dT%H:%M:%S')
            except Exception: pass
            date = str(raw_t)[:10]
        else:
            date = ''

        t_low = title.lower()
        src   = (source or '').lower()
        if any(_re.search(p, t_low) for p in _hard_skip): continue
        if src in _opinion_sources and not any(kw in t_low for kw in _action_kw): continue

        score  = 40 if src in _tier1 else 20 if src in _tier2 else 0
        score += sum(20 for kw in _action_kw if kw in t_low)
        if pub_ts:
            score += max(0, 30 * (1 - max(0, (_now - pub_ts).total_seconds() / 3600) / 72))

        haystack = (title + ' ' + summary).lower()
        if tkkr.lower() not in haystack and not any(w in haystack for w in _name_words):
            continue

        news.append({'title': title, 'url': url, 'source': source, 'thumb': thumb,
                     'date': date, 'pub_ts': pub_ts.isoformat() if pub_ts else '',
                     'score': score, '_summary_fallback': summary})

    news.sort(key=lambda x: x['score'], reverse=True)
    print(f'[NEWS] {tkkr}: filtered to {len(news)} items after scoring', flush=True)

    to_fetch  = news[:10]
    raw_texts = {id(n): n.pop('_summary_fallback', '') for n in news}
    _t2 = _t()
    with ThreadPoolExecutor(max_workers=5) as pool:
        fetch_futures = {pool.submit(_fetch_article_text, n['url']): n for n in to_fetch}
        try:
            for future in as_completed(fetch_futures, timeout=8):
                n = fetch_futures[future]
                try:
                    t = future.result()
                    if t: raw_texts[id(n)] = t
                except Exception: pass
        except FuturesTimeout: pass
    print(f'[NEWS] {tkkr}: article fetch done in {_t()-_t2:.2f}s', flush=True)

    raws = {}
    for n in news:
        src = raw_texts.get(id(n), '') or n['title']
        raws[id(n)] = _smart_summary(src, _name_words, tkkr) or n['title']

    _t3 = _t()
    with ThreadPoolExecutor(max_workers=10) as pool:
        headline_futures = {pool.submit(_to_headline, raws[id(n)], _short_name): n for n in news}
        try:
            for future in as_completed(headline_futures, timeout=10):
                n = headline_futures[future]
                try:
                    r = future.result()
                    if r: n['title'] = r
                except Exception: pass
        except FuturesTimeout: pass
    print(f'[NEWS] {tkkr}: Groq headlines done in {_t()-_t3:.2f}s', flush=True)

    for i, n in enumerate(news):
        n['top'] = i < 3
        del n['score']

    return news


import json, os

WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'watchlist.json')

def load_watchlist():
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_watchlist(items):
    try:
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(items, f, indent=2)
        return True
    except Exception:
        return False

@app.route('/api/watchlist', methods=['GET'])
def get_watchlist():
    return jsonify(load_watchlist())

@app.route('/api/watchlist', methods=['POST'])
def add_to_watchlist():
    data = request.json
    ticker = (data.get('ticker') or '').strip().upper()
    if not ticker:
        return jsonify({'error': 'No ticker'}), 400
    items = load_watchlist()
    if not any(i['ticker'] == ticker for i in items):
        items.append({
            'ticker': ticker,
            'name':   data.get('name', ''),
            'added':  data.get('added', ''),
        })
        save_watchlist(items)
    return jsonify(items)

@app.route('/api/watchlist/<ticker>', methods=['DELETE'])
def remove_from_watchlist(ticker):
    ticker = ticker.strip().upper()
    items = load_watchlist()
    items = [i for i in items if i['ticker'] != ticker]
    save_watchlist(items)
    return jsonify(items)


# ── Holdings ───────────────────────────────────────────────────────────────────
HOLDINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'holdings.json')

def load_holdings():
    try:
        if os.path.exists(HOLDINGS_FILE):
            with open(HOLDINGS_FILE, 'r') as f:
                items = json.load(f)
            # Migrate: ensure lots and avg_price fields exist
            for item in items:
                if 'lots' not in item:
                    item['lots'] = [{'shares': float(item['shares']),
                                     'price':  float(item['price']),
                                     'date':   item.get('date_acquired', '')}]
                if 'avg_price' not in item:
                    item['avg_price'] = float(item['price'])
            return items
    except Exception:
        pass
    return []

def save_holdings(items):
    try:
        with open(HOLDINGS_FILE, 'w') as f:
            json.dump(items, f, indent=2)
    except Exception:
        pass

# ── Dividend helpers ───────────────────────────────────────────────────────────
def _build_div_events(tkr):
    """Return list of (ex_date, pay_date, amount_per_share) for all historical dividends.

    yfinance indexes dividends by ex-date.  Pay date is derived from the lag between
    the upcoming exDividendDate and dividendDate in ticker.info — this lag is applied
    uniformly to all historical ex-dates.  Falls back to ex-date if unavailable.
    """
    import datetime as _dt_mod
    try:
        t = yf.Ticker(tkr)
        divs = t.dividends
        if divs is None or divs.empty:
            return []
        divs.index = divs.index.tz_localize(None) if divs.index.tz is not None else divs.index

        pay_lag = None
        try:
            info   = t.info
            ex_ts  = info.get('exDividendDate')
            pay_ts = info.get('dividendDate')
            if ex_ts and pay_ts and int(ex_ts) > 0 and int(pay_ts) > 0:
                ex_dt  = _dt_mod.datetime.fromtimestamp(int(ex_ts)).date()
                pay_dt = _dt_mod.datetime.fromtimestamp(int(pay_ts)).date()
                lag    = (pay_dt - ex_dt).days
                if 0 < lag < 180:
                    pay_lag = lag
        except Exception:
            pass

        events = []
        for dt, val in divs.items():
            ex_date  = dt.date() if hasattr(dt, 'date') else dt
            pay_date = (ex_date + _dt_mod.timedelta(days=pay_lag)) if pay_lag is not None else ex_date
            events.append((ex_date, pay_date, float(val)))
        return events
    except Exception:
        return []


def _build_div_pay_map(tkr):
    """Return {pay_date: amount_per_share} — for crediting cash on actual pay date."""
    return {pay_date: amount for _, pay_date, amount in _build_div_events(tkr)}


# ── Cash ───────────────────────────────────────────────────────────────────────
CASH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cash.json')

def load_cash():
    try:
        if os.path.exists(CASH_FILE):
            with open(CASH_FILE) as f:
                return float(json.load(f).get('balance', 0.0))
    except Exception:
        pass
    return 0.0

def save_cash(balance):
    try:
        with open(CASH_FILE, 'w') as f:
            json.dump({'balance': round(float(balance), 2)}, f)
    except Exception:
        pass

@app.route('/api/cash', methods=['GET'])
def get_cash():
    return jsonify({'balance': load_cash()})

@app.route('/api/portfolio/invested', methods=['GET'])
def portfolio_invested():
    """Cash-pool model: sell proceeds + dividends go into a pool; buys draw from pool first.
    Only the shortfall (cost exceeding the pool) counts as new external money."""
    from datetime import datetime as _dt
    txns = load_transactions()
    parsed = []
    for t in txns:
        try:
            parsed.append({
                'date':   _dt.strptime(t['date'], '%Y-%m-%d'),
                'type':   t['type'],
                'ticker': t.get('ticker', ''),
                'shares': float(t.get('shares', 0)),
                'price':  float(t.get('price', 0)),
            })
        except Exception:
            pass
    # sells before buys on same day so proceeds are available
    parsed.sort(key=lambda x: (x['date'], 0 if x['type'] == 'sell' else 1))

    import datetime as _dt_mod
    today = _dt_mod.date.today()

    positions = {}
    cash = 0.0
    invested = 0.0

    # Fetch full dividend events (ex_date, pay_date, amount) for all tickers
    tickers = list({t['ticker'] for t in parsed if t['ticker']})
    div_events_map = {}  # tkr -> [(ex_date, pay_date, amount)]
    try:
        from concurrent.futures import ThreadPoolExecutor
        def _get_div_events(tkr):
            return tkr, _build_div_events(tkr)
        with ThreadPoolExecutor(max_workers=8) as ex:
            for tkr, events in ex.map(_get_div_events, tickers):
                if events:
                    div_events_map[tkr] = events
    except Exception:
        pass

    # Two-phase dividend processing — eligibility is based on shares at EX-DATE,
    # but cash is only received on PAY-DATE.
    #
    # Event sort order within a day:
    #   0 = div_pay   : credit cash before same-day buys (so dividend funds same-day purchases)
    #   1 = txn       : sells then buys (stable sort preserves parsed order: sells first)
    #   2 = div_snap  : snapshot shares AFTER all trades on ex-date (for eligibility check)
    #
    # div_snapshots[(tkr, ex_date)] = shares held at close of ex-date
    div_snapshots = {}

    all_events = []
    for t in parsed:
        all_events.append((t['date'].date(), 1, 'txn', t))

    for tkr, events in div_events_map.items():
        for ex_date, pay_date, amount in events:
            # Snapshot shares at close of ex-date (always, whether received or pending)
            all_events.append((ex_date, 2, 'div_snap', (tkr, ex_date)))
            if pay_date <= today:
                # Credit cash at start of pay-date using the ex-date snapshot
                all_events.append((pay_date, 0, 'div_pay', (tkr, ex_date, amount)))
            # else: ex_date <= today handled below via div_snap; pay_date > today → pending

    all_events.sort(key=lambda x: (x[0], x[1]))

    for _date, _order, etype, data in all_events:
        if etype == 'txn':
            t = data
            tk, sh, pr = t['ticker'], t['shares'], t['price']
            if t['type'] == 'buy':
                positions[tk] = positions.get(tk, 0) + sh
                cost = sh * pr
                invested += max(0.0, cost - cash)
                cash = max(0.0, cash - cost)
            elif t['type'] == 'sell':
                positions[tk] = positions.get(tk, 0) - sh
                cash += sh * pr
        elif etype == 'div_snap':
            tkr, ex_date = data
            div_snapshots[(tkr, ex_date)] = positions.get(tkr, 0)
        elif etype == 'div_pay':
            tkr, ex_date, amount = data
            shares = div_snapshots.get((tkr, ex_date), 0)
            if shares > 0:
                cash += shares * amount

    # Pending: ex-date passed (snapshot recorded), pay-date hasn't arrived yet.
    pending_cash = 0.0
    for tkr, events in div_events_map.items():
        for ex_date, pay_date, amount in events:
            if ex_date <= today < pay_date:
                shares = div_snapshots.get((tkr, ex_date), 0)
                if shares > 0:
                    pending_cash += shares * amount

    return jsonify({
        'invested':     round(invested,     2),
        'cash_pool':    round(cash,         2),
        'pending_cash': round(pending_cash, 2),
    })

@app.route('/api/cash', methods=['POST'])
def update_cash():
    data    = request.json or {}
    amount  = float(data.get('amount', 0))
    kind    = data.get('type', 'deposit')
    balance = load_cash()
    if kind == 'deposit':
        balance += amount
    else:
        balance = max(0.0, balance - amount)
    save_cash(balance)
    return jsonify({'balance': round(balance, 2)})

@app.route('/api/holdings', methods=['GET'])
def get_holdings():
    return jsonify(load_holdings())

@app.route('/api/holdings', methods=['POST'])
def add_to_holdings():
    data   = request.json
    ticker = (data.get('ticker') or '').strip().upper()
    if not ticker:
        return jsonify({'error': 'No ticker'}), 400
    try:
        shares = float(data.get('shares', 0))
        price  = float(data.get('price',  0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid shares or price'}), 400

    date_acquired = (data.get('date_acquired') or '').strip()
    items = load_holdings()

    for item in items:
        if item['ticker'] == ticker:
            # Accumulate new lot into existing holding
            lots = item.get('lots', [])
            if not lots:
                lots = [{'shares': float(item['shares']), 'price': float(item['price']),
                         'date': item.get('date_acquired', '')}]
            lots.append({'shares': shares, 'price': price, 'date': date_acquired})
            item['lots']      = lots
            total_shares      = sum(l['shares'] for l in lots)
            avg_price         = sum(l['shares'] * l['price'] for l in lots) / total_shares if total_shares > 0 else 0
            item['shares']    = round(total_shares, 10)
            item['avg_price'] = round(avg_price, 4)
            item['price']     = item['avg_price']
            item['name']      = data.get('name', item.get('name', ''))
            dates = [l['date'] for l in lots if l.get('date')]
            item['date_acquired'] = min(dates) if dates else date_acquired
            save_holdings(items)
            _record_transaction('buy', ticker, item['name'], shares, price, date_acquired)
            return jsonify(items)

    # New holding
    lots = [{'shares': shares, 'price': price, 'date': date_acquired}]
    items.append({'ticker': ticker, 'name': data.get('name', ''), 'shares': shares,
                  'price': price, 'avg_price': price, 'lots': lots,
                  'date_acquired': date_acquired, 'added': data.get('added', '')})
    save_holdings(items)
    _record_transaction('buy', ticker, data.get('name', ''), shares, price, date_acquired)
    return jsonify(items)

@app.route('/api/holdings/<ticker>', methods=['DELETE'])
def remove_from_holdings(ticker):
    ticker = ticker.strip().upper()
    items  = [i for i in load_holdings() if i['ticker'] != ticker]
    save_holdings(items)
    return jsonify(items)

# ── Sales ──────────────────────────────────────────────────────────────────────
SALES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sales.json')

def load_sales():
    try:
        if os.path.exists(SALES_FILE):
            with open(SALES_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_sales(items):
    try:
        with open(SALES_FILE, 'w') as f:
            json.dump(items, f, indent=2)
    except Exception:
        pass

@app.route('/api/sales', methods=['GET'])
def get_sales():
    return jsonify(load_sales())

# ── Transactions ───────────────────────────────────────────────────────────────
TRANSACTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'transactions.json')

def load_transactions():
    try:
        if os.path.exists(TRANSACTIONS_FILE):
            with open(TRANSACTIONS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_transactions(items):
    try:
        with open(TRANSACTIONS_FILE, 'w') as f:
            json.dump(items, f, indent=2)
    except Exception:
        pass

def rebuild_holdings_from_transactions():
    """Replay all transactions FIFO to rebuild holdings.json from scratch."""
    from datetime import datetime as _dt
    def _parse(d):
        try: return _dt.strptime(d, '%Y-%m-%d')
        except Exception: return _dt.min

    txns = sorted(load_transactions(), key=lambda t: _parse(t.get('date', '')))

    positions = {}  # ticker -> {name, lots:[{shares,price,date}]}
    for t in txns:
        ticker = (t.get('ticker') or '').upper()
        if not ticker:
            continue
        shares = float(t.get('shares') or 0)
        price  = float(t.get('price')  or 0)
        date   = t.get('date', '')
        name   = t.get('name', ticker)
        if t.get('type') == 'buy':
            if ticker not in positions:
                positions[ticker] = {'name': name, 'lots': []}
            positions[ticker]['lots'].append({'shares': shares, 'price': price, 'date': date})
        elif t.get('type') == 'sell':
            if ticker not in positions:
                continue
            remaining = shares
            new_lots = []
            for lot in positions[ticker]['lots']:
                if remaining <= 0:
                    new_lots.append(lot)
                elif lot['shares'] <= remaining + 1e-9:
                    remaining -= lot['shares']
                else:
                    new_lots.append({'shares': lot['shares'] - remaining, 'price': lot['price'], 'date': lot['date']})
                    remaining = 0
            positions[ticker]['lots'] = new_lots

    holdings = []
    for ticker, data in positions.items():
        lots = [l for l in data['lots'] if l['shares'] > 1e-9]
        if not lots:
            continue
        total_shares = sum(l['shares'] for l in lots)
        avg_price    = sum(l['shares'] * l['price'] for l in lots) / total_shares
        dates        = [l['date'] for l in lots if l.get('date')]
        holdings.append({
            'ticker':        ticker,
            'name':          data['name'],
            'shares':        round(total_shares, 6),
            'price':         round(avg_price, 4),
            'avg_price':     round(avg_price, 4),
            'lots':          [{'shares': round(l['shares'], 6), 'price': round(l['price'], 4), 'date': l['date']} for l in lots],
            'date_acquired': min(dates) if dates else '',
            'added':         '',
        })
    save_holdings(holdings)
    return holdings

def _record_transaction(txn_type, ticker, name, shares, price, date, gain_loss=None):
    import time as _t
    txns = load_transactions()
    txn = {
        'id':     str(int(_t.time() * 1000)),
        'type':   txn_type,
        'ticker': ticker,
        'name':   name,
        'shares': shares,
        'price':  price,
        'date':   date,
    }
    if gain_loss is not None:
        txn['gain_loss'] = gain_loss
    txns.insert(0, txn)
    save_transactions(txns)

@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    txns  = load_transactions()
    sales = load_sales()
    # Build a lookup: (ticker, date, rounded_shares) -> gain_loss
    sales_lookup = {}
    for s in sales:
        key = (s.get('ticker',''), s.get('sale_date',''), round(float(s.get('shares_sold', 0)), 4))
        sales_lookup[key] = s.get('gain_loss')
    for t in txns:
        if t.get('type') == 'sell' and t.get('gain_loss') is None:
            key = (t.get('ticker',''), t.get('date',''), round(float(t.get('shares', 0)), 4))
            gl = sales_lookup.get(key)
            if gl is not None:
                t['gain_loss'] = gl
    return jsonify(txns)

@app.route('/api/transactions/<txn_id>', methods=['DELETE'])
def delete_transaction(txn_id):
    txns = [t for t in load_transactions() if t.get('id') != txn_id]
    save_transactions(txns)
    holdings = rebuild_holdings_from_transactions()
    return jsonify({'transactions': txns, 'holdings': holdings})

@app.route('/api/transactions/<txn_id>', methods=['PUT'])
def update_transaction(txn_id):
    data = request.json or {}
    try:
        shares = float(data.get('shares', 0))
        price  = float(data.get('price',  0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid number values'}), 400

    txn_type = (data.get('type') or '').strip().lower()
    date     = (data.get('date') or '').strip()
    ticker   = (data.get('ticker') or '').strip().upper()
    name     = (data.get('name') or '').strip()

    if txn_type not in ('buy', 'sell'):
        return jsonify({'error': 'Type must be buy or sell'}), 400
    if shares <= 0 or price <= 0 or not date or not ticker:
        return jsonify({'error': 'Shares, price, date, and ticker are required'}), 400

    txns = load_transactions()
    for t in txns:
        if t.get('id') == txn_id:
            t['type']   = txn_type
            t['ticker'] = ticker
            t['name']   = name
            t['shares'] = round(shares, 6)
            t['price']  = round(price,  4)
            t['date']   = date
            break
    else:
        return jsonify({'error': 'Transaction not found'}), 404

    save_transactions(txns)
    holdings = rebuild_holdings_from_transactions()
    return jsonify({'transactions': txns, 'holdings': holdings})

@app.route('/api/transactions/quick', methods=['POST'])
def quick_transaction():
    data      = request.json or {}
    ticker    = (data.get('ticker') or '').strip().upper()
    buy_date  = (data.get('buy_date') or '').strip()
    sell_date = (data.get('sell_date') or '').strip()
    try:
        shares     = float(data.get('shares', 0))
        sell_price = float(data.get('sell_price', 0))
        gain       = float(data.get('gain', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid number values'}), 400

    if not ticker or not buy_date or not sell_date or shares <= 0 or sell_price <= 0:
        return jsonify({'error': 'Ticker, shares, dates, and sell price are required'}), 400

    buy_price = round(sell_price - (gain / shares), 4)
    sell_price = round(sell_price, 4)
    gain_loss  = round(gain, 4)

    # Look up company name via Yahoo Finance
    name = ticker
    try:
        import requests as req
        url = 'https://query2.finance.yahoo.com/v1/finance/search'
        params = {'q': ticker, 'quotesCount': 1, 'newsCount': 0}
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = req.get(url, params=params, headers=headers, timeout=5)
        quotes = r.json().get('quotes', [])
        if quotes:
            name = quotes[0].get('longname') or quotes[0].get('shortname') or ticker
    except Exception:
        pass

    import time as _t
    ts = int(_t.time() * 1000)
    txns = load_transactions()
    txns.insert(0, {'id': str(ts + 1), 'type': 'sell', 'ticker': ticker,
                    'name': name, 'shares': shares, 'price': sell_price, 'date': sell_date, 'gain_loss': gain_loss})
    txns.insert(1, {'id': str(ts),     'type': 'buy',  'ticker': ticker,
                    'name': name, 'shares': shares, 'price': buy_price,  'date': buy_date})
    save_transactions(txns)

    sales = load_sales()
    sales.append({'ticker': ticker, 'name': name, 'shares_sold': shares,
                  'avg_cost': buy_price, 'sale_price': sell_price,
                  'sale_date': sell_date, 'date_acquired': buy_date, 'gain_loss': gain_loss})
    save_sales(sales)
    rebuild_holdings_from_transactions()

    return jsonify({'transactions': txns, 'buy_price': buy_price, 'sell_price': sell_price, 'gain_loss': gain_loss})

@app.route('/api/holdings/<ticker>/sell', methods=['POST'])
def sell_holding(ticker):
    ticker = ticker.strip().upper()
    data   = request.json
    try:
        shares_sold = float(data.get('shares_sold', 0))
        sale_price  = float(data.get('sale_price',  0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid shares or price'}), 400
    if shares_sold <= 0 or sale_price <= 0:
        return jsonify({'error': 'Shares and price must be positive'}), 400

    holdings = load_holdings()
    holding  = next((h for h in holdings if h['ticker'] == ticker), None)
    if not holding:
        return jsonify({'error': 'Holding not found'}), 404

    shares_sold = min(shares_sold, holding['shares'])

    # FIFO cost basis from lots
    lots = holding.get('lots', [])
    if not lots:
        lots = [{'shares': float(holding['shares']), 'price': float(holding['price']),
                 'date': holding.get('date_acquired', '')}]
    lots_sorted       = sorted(lots, key=lambda l: l.get('date') or '')
    remaining_to_sell = shares_sold
    cost_basis_total  = 0.0
    for lot in lots_sorted:
        if remaining_to_sell <= 0:
            break
        take = min(lot['shares'], remaining_to_sell)
        cost_basis_total  += take * lot['price']
        lot['shares']     -= take
        remaining_to_sell -= take
    avg_cost  = round(cost_basis_total / shares_sold, 4)
    gain_loss = round((sale_price - avg_cost) * shares_sold, 4)

    # Record the sale
    sale_date = data.get('sale_date', '')
    name      = holding.get('name', '')
    sales = load_sales()
    sales.append({
        'ticker':        ticker,
        'name':          name,
        'shares_sold':   shares_sold,
        'avg_cost':      avg_cost,
        'sale_price':    sale_price,
        'sale_date':     sale_date,
        'date_acquired': holding.get('date_acquired', ''),
        'gain_loss':     gain_loss,
    })
    save_sales(sales)
    _record_transaction('sell', ticker, name, shares_sold, sale_price, sale_date, gain_loss=gain_loss)

    # Update holding lots and totals
    remaining_lots  = [l for l in lots_sorted if l['shares'] > 1e-9]
    remaining_total = sum(l['shares'] for l in remaining_lots)
    if remaining_total <= 1e-9:
        holdings = [h for h in holdings if h['ticker'] != ticker]
    else:
        holding['lots']   = remaining_lots
        holding['shares'] = round(remaining_total, 10)
        new_avg = sum(l['shares'] * l['price'] for l in remaining_lots) / remaining_total
        holding['avg_price'] = round(new_avg, 4)
        holding['price']     = holding['avg_price']
        dates = [l['date'] for l in remaining_lots if l.get('date')]
        holding['date_acquired'] = min(dates) if dates else holding.get('date_acquired', '')
    save_holdings(holdings)

    return jsonify({'holdings': holdings, 'sales': sales})


@app.route('/api/dividends', methods=['GET'])
def get_dividends():
    import pandas as pd
    ticker     = request.args.get('ticker', '').strip().upper()
    from_date  = request.args.get('from',   '')
    to_date    = request.args.get('to',     '')
    if not ticker:
        return jsonify({'total_per_share': 0})
    try:
        divs = yf.Ticker(ticker).dividends
        if divs.empty:
            return jsonify({'total_per_share': 0})
        # Normalize index to tz-naive dates for comparison
        divs.index = divs.index.tz_localize(None) if divs.index.tz is not None else divs.index
        if from_date:
            divs = divs[divs.index >= pd.Timestamp(from_date)]
        if to_date:
            divs = divs[divs.index <= pd.Timestamp(to_date)]
        return jsonify({'total_per_share': round(float(divs.sum()), 6)})
    except Exception as e:
        return jsonify({'total_per_share': 0})


@app.route('/api/dividends/history', methods=['GET'])
def dividends_history():
    from datetime import datetime
    from collections import defaultdict
    import pandas as pd

    transactions = load_transactions()
    if not transactions:
        return jsonify({'payments': [], 'by_month': {}, 'total': 0})

    parsed = []
    for t in transactions:
        try:
            parsed.append({
                'date':   datetime.strptime(t['date'], '%Y-%m-%d'),
                'type':   t['type'],
                'ticker': t['ticker'],
                'name':   t.get('name', t['ticker']),
                'shares': float(t['shares']),
            })
        except Exception:
            pass
    if not parsed:
        return jsonify({'payments': [], 'by_month': {}, 'total': 0})
    parsed.sort(key=lambda x: (x['date'], 0 if x['type'] == 'sell' else 1))

    ticker_events = defaultdict(list)
    ticker_names  = {}
    for t in parsed:
        delta = t['shares'] if t['type'] == 'buy' else -t['shares']
        ticker_events[t['ticker']].append((t['date'], delta))
        if t['name']:
            ticker_names[t['ticker']] = t['name']

    payments = []

    for ticker, events in ticker_events.items():
        events_sorted = sorted(events, key=lambda x: x[0])
        div_events = _build_div_events(ticker)
        if not div_events:
            continue

        name = ticker_names.get(ticker, ticker)

        import datetime as _dt_mod
        today_d = _dt_mod.date.today()

        for ex_date, pay_date, div_per_share in div_events:
            ex_dt = datetime.combine(ex_date, datetime.min.time())

            shares_held = 0.0
            for ev_date, delta in events_sorted:
                if ev_date <= ex_dt:
                    shares_held += delta
                else:
                    break

            if shares_held <= 1e-6:
                continue

            # Only show dividends where ex-date has passed (eligible) or already paid
            if ex_date > today_d:
                continue

            total = round(shares_held * float(div_per_share), 2)
            if total <= 0:
                continue

            status = 'paid' if pay_date <= today_d else 'pending'

            payments.append({
                'date':      str(pay_date),
                'ex_date':   str(ex_date),
                'status':    status,
                'ticker':    ticker,
                'name':      name,
                'shares':    round(shares_held, 4),
                'per_share': round(float(div_per_share), 4),
                'total':     total,
            })

    payments.sort(key=lambda x: x['date'])

    by_month = {}
    for p in payments:
        if p['status'] == 'paid':
            month = p['date'][:7]
            by_month[month] = round(by_month.get(month, 0.0) + p['total'], 2)

    grand_total    = round(sum(p['total'] for p in payments if p['status'] == 'paid'), 2)
    pending_total  = round(sum(p['total'] for p in payments if p['status'] == 'pending'), 2)

    return jsonify({'payments': payments, 'by_month': by_month, 'total': grand_total, 'pending_total': pending_total})


@app.route('/api/holdings/chart', methods=['GET'])
def holdings_chart():
    from datetime import datetime, timedelta
    from collections import defaultdict
    import pandas as pd

    range_param = request.args.get('range', '1Y')
    transactions = load_transactions()
    if not transactions:
        return jsonify({'dates': [], 'values': [], 'invested': []})

    # Parse and sort transactions ascending by date
    parsed = []
    for t in transactions:
        try:
            parsed.append({
                'date':   datetime.strptime(t['date'], '%Y-%m-%d'),
                'type':   t['type'],
                'ticker': t['ticker'],
                'shares': float(t['shares']),
                'price':  float(t['price']),
            })
        except Exception:
            pass
    if not parsed:
        return jsonify({'dates': [], 'values': [], 'invested': []})
    parsed.sort(key=lambda x: (x['date'], 0 if x['type'] == 'sell' else 1))

    today      = datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
    first_date = parsed[0]['date']
    range_days = {'1W': 7, '1M': 30, '3M': 90, '6M': 180, '1Y': 365}
    days       = range_days.get(range_param)
    start      = max(today - timedelta(days=days), first_date) if days else first_date

    tickers = list(set(t['ticker'] for t in parsed))

    # Download historical prices — yfinance 1.x always returns (Price, Ticker) MultiIndex
    try:
        dl_start  = start.strftime('%Y-%m-%d')
        dl_end    = (today + timedelta(days=1)).strftime('%Y-%m-%d')
        # Pass list always so column structure is consistent
        raw = yf.download(tickers, start=dl_start, end=dl_end,
                          auto_adjust=False, progress=False)
        if raw.empty:
            return jsonify({'dates': [], 'values': [], 'invested': []})
        # With auto_adjust=False, use 'Close' (unadjusted actual price, not retroactively scaled)
        close = raw['Close']
        prices = close.to_frame(name=tickers[0]) if isinstance(close, pd.Series) else close
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if prices.empty:
        return jsonify({'dates': [], 'values': [], 'invested': []})

    # Fetch dividends and stock splits for all tickers over the chart range.
    # Dividends flow through the cash pool so reinvestment buys don't inflate invested.
    # Splits adjust position sizes on the ex-date so unadjusted prices stay consistent.
    from concurrent.futures import ThreadPoolExecutor

    div_map   = {}  # ticker -> {date -> div_per_share}
    split_map = {}  # ticker -> {date -> ratio}  e.g. 2-for-1 split => ratio=2.0

    def _get_corporate_actions(tkr):
        t_obj = yf.Ticker(tkr)
        # div_events: list of (ex_date, pay_date, amount) filtered to pay_date in chart range
        div_events_out = []
        splits = {}
        try:
            range_start = pd.Timestamp(dl_start).date()
            range_end   = pd.Timestamp(dl_end).date()
            for ex_date, pay_date, amount in _build_div_events(tkr):
                if range_start <= pay_date <= range_end:
                    div_events_out.append((ex_date, pay_date, amount))
        except Exception:
            pass
        try:
            s = t_obj.splits
            if s is not None and not s.empty:
                tz  = s.index.tz
                s_ts = pd.Timestamp(dl_start, tz=tz)
                e_ts = pd.Timestamp(dl_end,   tz=tz)
                s = s[(s.index >= s_ts) & (s.index <= e_ts) & (s != 0) & (s != 1)]
                splits = {dt.date(): float(v) for dt, v in s.items()}
        except Exception:
            pass
        return tkr, div_events_out, splits

    # div_map[tkr] = [(ex_date, pay_date, amount), ...]
    with ThreadPoolExecutor(max_workers=8) as ex:
        for tkr, div_events_out, splits in ex.map(_get_corporate_actions, tickers):
            if div_events_out:
                div_map[tkr] = div_events_out
            if splits:
                split_map[tkr] = splits

    # Build pay-date lookup: pay_date -> [(tkr, ex_date, amount)]
    # and ex-date set for snapshot recording: ex_date -> [tkr]
    from collections import defaultdict as _dd
    pay_lookup  = _dd(list)   # pay_date  -> [(tkr, ex_date, amount)]
    ex_snap_map = _dd(list)   # ex_date   -> [tkr]
    for tkr, events in div_map.items():
        for ex_date, pay_date, amount in events:
            pay_lookup[pay_date].append((tkr, ex_date, amount))
            ex_snap_map[ex_date].append(tkr)
    # Snapshot positions at each ex-date; pre-populate with chart-start positions
    # for any ex-date that falls before the chart range (approximation).
    div_snapshots_chart = {}   # (tkr, ex_date) -> shares

    # Build starting positions from transactions before `start`.
    # invested = cumulative external cash that funded buys.
    # Sell proceeds and dividends go into a cash pool; buys draw from that pool
    # first — only the shortfall counts as new external money.
    positions = {}   # ticker -> shares
    cash      = 0.0  # internal cash pool: sell proceeds + unspent dividends
    invested  = 0.0  # external capital only

    def _apply_txn(t, positions, cash, invested):
        tk, sh, pr = t['ticker'], t['shares'], t['price']
        if t['type'] == 'buy':
            positions[tk] = positions.get(tk, 0) + sh
            cost      = sh * pr
            new_money = max(0.0, cost - cash)
            invested += new_money
            cash      = max(0.0, cash - cost)
        elif t['type'] == 'sell':
            positions[tk] = positions.get(tk, 0) - sh
            cash += sh * pr
        return cash, invested

    for t in parsed:
        if t['date'] >= start:
            break
        cash, invested = _apply_txn(t, positions, cash, invested)

    # Group in-range transactions by calendar date
    txns_by_date = defaultdict(list)
    for t in parsed:
        if t['date'] >= start:
            txns_by_date[t['date'].date()].append(t)

    dates_out, values_out, invested_out = [], [], []

    for idx_date in prices.index:
        d = idx_date.date() if hasattr(idx_date, 'date') else idx_date

        # Apply stock splits: adjust position size so unadjusted prices stay consistent.
        for tkr in list(positions):
            if tkr in split_map:
                ratio = split_map[tkr].get(d, 0.0)
                if ratio > 0 and ratio != 1.0:
                    positions[tkr] = positions[tkr] * ratio

        # Credit dividends whose pay-date == today using the ex-date share snapshot.
        # Snapshot must already exist (recorded when we iterated over that ex-date).
        # For ex-dates before the chart range we fall back to current positions.
        for tkr, ex_date, amount in pay_lookup.get(d, []):
            snap_key = (tkr, ex_date)
            shares = div_snapshots_chart.get(snap_key, positions.get(tkr, 0))
            if shares > 0:
                cash += shares * amount

        for t in txns_by_date.get(d, []):
            cash, invested = _apply_txn(t, positions, cash, invested)

        # Snapshot shares at close of this ex-date for any dividends whose ex-date == today.
        for tkr in ex_snap_map.get(d, []):
            div_snapshots_chart[(tkr, d)] = positions.get(tkr, 0)

        total_value = cash
        for tkr, shares in positions.items():
            if shares <= 0 or tkr not in prices.columns:
                continue
            p = prices.loc[idx_date, tkr]
            if pd.notna(p):
                total_value += shares * float(p)

        dates_out.append(str(d))
        values_out.append(round(total_value, 2))
        invested_out.append(round(invested, 2))

    return jsonify({'dates': dates_out, 'values': values_out, 'invested': invested_out})


def _backfill_transaction_gains():
    """Backfill gain_loss from sales.json into any sell transactions missing it, then save permanently."""
    txns  = load_transactions()
    sales = load_sales()
    sales_lookup = {}
    for s in sales:
        key = (s.get('ticker',''), s.get('sale_date',''), round(float(s.get('shares_sold', 0)), 4))
        sales_lookup[key] = s.get('gain_loss')
    changed = False
    for t in txns:
        if t.get('type') == 'sell' and t.get('gain_loss') is None:
            key = (t.get('ticker',''), t.get('date',''), round(float(t.get('shares', 0)), 4))
            gl = sales_lookup.get(key)
            if gl is not None:
                t['gain_loss'] = gl
                changed = True
    if changed:
        save_transactions(txns)


if __name__ == '__main__':
    import os
    import threading
    import webbrowser

    _backfill_transaction_gains()

    port = int(os.environ.get('PORT', 5000))

    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f'http://127.0.0.1:{port}')

    threading.Thread(target=open_browser, daemon=True).start()

    def _prewarm_movers():
        import time as _time
        _time.sleep(0.5)
        for ex in ('TSX', 'NYSE', 'NASDAQ'):
            try:
                _refresh_movers(ex)
            except Exception:
                pass

    threading.Thread(target=_prewarm_movers, daemon=True).start()
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
