from flask import Flask, jsonify, request
from flask_cors import CORS
import FinanceDataReader as fdr
import numpy as np
import pandas as pd
import requests
import os
import json
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)
STATE_FILE = os.path.join(os.path.dirname(__file__), 'portfolio_state.json')
STATE_LOCK = Lock()

FINNHUB_API_KEY = os.environ.get('FINNHUB_API_KEY', '')
FINNHUB_BASE = 'https://finnhub.io/api/v1'

# 거래소 코드 → 통화 매핑
EXCHANGE_CURRENCY = {
    'KS': 'KRW', 'KQ': 'KRW',
    'HK': 'HKD',
    'T': 'JPY',
    'L': 'GBP',
    'PA': 'EUR', 'DE': 'EUR', 'MI': 'EUR',
    'SS': 'CNY', 'SZ': 'CNY',
}

INDEX_SYMBOL_MAP = {
    '^GSPC': 'US500',
    '^IXIC': 'IXIC',
    '^DJI': 'DJI',
    '^KS11': 'KS11',
    '^KQ11': 'KQ11',
    '^N225': 'JP225',
    '^HSI': 'HSI',
}

KOREA_EXCHANGE_ALIASES = {'KRX', 'KOSPI', 'KO', 'KS', 'KOSDAQ', 'KQ'}


def _normalize_ticker(ticker):
    """입력 티커를 조회 친화적인 Yahoo/FDR 형태로 정규화."""
    tk = (ticker or '').strip().upper()
    if not tk:
        return tk
    if ':' in tk and not tk.startswith('^') and '=X' not in tk:
        exchange, base = tk.split(':', 1)
        if base.isdigit() and len(base) == 6 and exchange in KOREA_EXCHANGE_ALIASES:
            if exchange in {'KQ', 'KOSDAQ'}:
                return f'{base}.KQ'
            return f'{base}.KS'
        return f'{base}.{exchange}'
    if '.' in tk and not tk.startswith('^') and '=X' not in tk:
        base, exchange = tk.rsplit('.', 1)
        if base.isdigit() and len(base) == 6 and exchange in {'KRX', 'KOSPI', 'KO'}:
            return f'{base}.KS'
    if tk.isdigit() and len(tk) == 6:
        return f'{tk}.KS'
    return tk


def yf_to_finnhub(ticker):
    """Yahoo Finance 형식 → Finnhub 형식: 005930.KS → KS:005930"""
    ticker = _normalize_ticker(ticker)
    if '.' in ticker and not ticker.startswith('^') and '=X' not in ticker:
        base, exchange = ticker.rsplit('.', 1)
        return f"{exchange}:{base}"
    return ticker


def finnhub_to_yf(symbol):
    """Finnhub 형식 → Yahoo Finance 형식: KS:005930 → 005930.KS"""
    symbol = (symbol or '').strip().upper()
    if ':' in symbol:
        exchange, base = symbol.split(':', 1)
        if base.isdigit() and len(base) == 6 and exchange in KOREA_EXCHANGE_ALIASES:
            if exchange in {'KQ', 'KOSDAQ'}:
                return f'{base}.KQ'
            return f'{base}.KS'
        return f"{base}.{exchange}"
    return _normalize_ticker(symbol)


def _period_to_start_date(period):
    now = datetime.utcnow().date()
    if period == '5d':
        return now - timedelta(days=10)
    if period == '1mo':
        return now - timedelta(days=45)
    if period == '3mo':
        return now - timedelta(days=120)
    if period == '6mo':
        return now - timedelta(days=240)
    if period == '1y':
        return now - timedelta(days=380)
    if period == '2y':
        return now - timedelta(days=760)
    if period == '5y':
        return now - timedelta(days=1900)
    if period == '10y':
        return now - timedelta(days=3800)
    if period == 'ytd':
        return datetime(now.year, 1, 1).date()
    if period == 'max':
        return datetime(1990, 1, 1).date()
    return now - timedelta(days=380)


def _yf_to_fdr_symbol(ticker):
    ticker = _normalize_ticker(ticker)
    if ticker.startswith('^'):
        return INDEX_SYMBOL_MAP.get(ticker, ticker.replace('^', ''))
    if ticker.endswith('.KS') or ticker.endswith('.KQ'):
        return ticker.split('.')[0]
    if '.' in ticker:
        base = ticker.split('.')[0]
        if base.isdigit() and len(base) == 6:
            return base
    if '=X' in ticker:
        return None
    return ticker


def _infer_currency(ticker):
    ticker = _normalize_ticker(ticker)
    if ticker.endswith('.KS') or ticker.endswith('.KQ'):
        return 'KRW'
    if ticker.startswith('^KS') or ticker.startswith('^KQ'):
        return 'KRW'
    if ticker.endswith('.T'):
        return 'JPY'
    if ticker.endswith('.L'):
        return 'GBP'
    if ticker.endswith('.HK'):
        return 'HKD'
    return 'USD'


def _fetch_close_series_fdr(ticker, period='1y'):
    ticker = _normalize_ticker(ticker)
    fdr_symbol = _yf_to_fdr_symbol(ticker)
    if not fdr_symbol:
        return None
    start = _period_to_start_date(period)
    end = datetime.utcnow().date()
    df = fdr.DataReader(fdr_symbol, start=start, end=end)
    if df is None or df.empty or 'Close' not in df.columns:
        return None
    s = df['Close'].dropna()
    if s.empty:
        return None
    s.name = ticker
    return s


def _fdr_price_data(ticker):
    try:
        close = _fetch_close_series_fdr(ticker, period='1mo')
        if close is None or len(close) < 1:
            return None
        price = float(close.iloc[-1])
        if len(close) >= 2:
            prev = float(close.iloc[-2])
            change = price - prev
            change_pct = (change / prev) * 100 if prev else 0
        else:
            prev = price
            change = 0
            change_pct = 0
        return {
            'price': round(price, 6),
            'change': round(change, 6),
            'changePct': round(change_pct, 4),
            'name': ticker,
            'currency': _infer_currency(ticker),
            'ok': True,
        }
    except Exception:
        return None


def get_price_data(ticker):
    ticker = _normalize_ticker(ticker)
    # 1순위: FinanceDataReader
    fdr_result = _fdr_price_data(ticker)
    if fdr_result is not None:
        return fdr_result

    # 2순위: Finnhub (FDR 미지원 티커 폴백)
    use_finnhub = FINNHUB_API_KEY and '=X' not in ticker and not ticker.startswith('^')
    if use_finnhub:
        try:
            fh_symbol = yf_to_finnhub(ticker)
            r = requests.get(
                f'{FINNHUB_BASE}/quote',
                params={'symbol': fh_symbol, 'token': FINNHUB_API_KEY},
                timeout=5
            )
            if r.ok:
                d = r.json()
                price = d.get('c', 0)
                prev = d.get('pc', 0)
                if price and price > 0:
                    change = price - prev
                    change_pct = (change / prev * 100) if prev else 0
                    exchange = fh_symbol.split(':')[0] if ':' in fh_symbol else 'US'
                    currency = EXCHANGE_CURRENCY.get(exchange, 'USD')
                    return {
                        'price': round(price, 6),
                        'change': round(change, 6),
                        'changePct': round(change_pct, 4),
                        'name': ticker,
                        'currency': currency,
                        'ok': True,
                    }
        except Exception:
            pass

    return None


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state_obj):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state_obj, f, ensure_ascii=False, indent=2)


def calculate_portfolio_metrics(tickers, weights=None, period='1y', benchmark='^GSPC', risk_free_rate=0.03):
    if not tickers:
        return {'error': '최소 1개 이상의 티커가 필요합니다.'}, 400

    raw_tickers = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not raw_tickers:
        return {'error': '유효한 티커가 없습니다.'}, 400

    if weights is None or len(weights) == 0:
        raw_weights = np.array([1 / len(raw_tickers)] * len(raw_tickers), dtype=float)
    else:
        if len(weights) != len(raw_tickers):
            return {'error': 'weights 길이는 tickers 길이와 같아야 합니다.'}, 400
        raw_weights = np.array([_to_float(w) for w in weights], dtype=float)
        if np.any(raw_weights < 0):
            return {'error': 'weights는 0 이상이어야 합니다.'}, 400
        weight_sum = raw_weights.sum()
        if weight_sum == 0:
            return {'error': 'weights 합이 0일 수 없습니다.'}, 400
        raw_weights = raw_weights / weight_sum

    # merge duplicate tickers (same asset split by multiple holdings)
    merged = {}
    for ticker, weight in zip(raw_tickers, raw_weights):
        merged[ticker] = merged.get(ticker, 0.0) + float(weight)
    clean_tickers = list(merged.keys())
    weights_arr = np.array([merged[t] for t in clean_tickers], dtype=float)
    weights_arr = weights_arr / weights_arr.sum()

    valid_periods = {'1mo', '3mo', '6mo', '1y', '2y', '5y', '10y', 'ytd', 'max'}
    if period not in valid_periods:
        return {'error': f'invalid period. allowed: {sorted(valid_periods)}'}, 400

    try:
        close_map = {}
        for t in clean_tickers:
            series = _fetch_close_series_fdr(t, period=period)
            if series is not None and len(series) > 2:
                close_map[t] = series
        if not close_map:
            return {'error': '가격 데이터를 가져오지 못했습니다.'}, 500
        close_prices = pd.concat(close_map.values(), axis=1)
        close_prices.columns = list(close_map.keys())
        available_tickers = list(close_map.keys())
    except Exception:
        return {'error': '가격 데이터를 가져오지 못했습니다.'}, 500

    if close_prices is None or len(close_prices) == 0:
        return {'error': '가격 데이터가 비어 있습니다.'}, 404

    if set(clean_tickers).issubset(set(close_prices.columns)):
        available_tickers = clean_tickers
        close_prices = close_prices[clean_tickers]

    ticker_to_weight = dict(zip(clean_tickers, weights_arr))
    aligned_weights = np.array([ticker_to_weight[t] for t in available_tickers], dtype=float)
    aligned_weights = aligned_weights / aligned_weights.sum()
    close_prices = close_prices.dropna(how='any')
    if close_prices.empty or len(close_prices) < 3:
        return {'error': '지표 계산을 위한 데이터가 충분하지 않습니다.'}, 422

    returns = close_prices.pct_change().dropna(how='any')
    portfolio_returns = (returns * aligned_weights).sum(axis=1)

    benchmark_returns = None
    benchmark_period_return = None
    try:
        bench_close = _fetch_close_series_fdr(benchmark, period=period)
        if bench_close is not None and len(bench_close) > 2:
            benchmark_returns = bench_close.pct_change().dropna()
            benchmark_period_return = _to_float(bench_close.iloc[-1] / bench_close.iloc[0] - 1)
    except Exception:
        benchmark_returns = None

    trading_days = 252
    total_days = max((portfolio_returns.index[-1] - portfolio_returns.index[0]).days, 1)
    annual_rf = _to_float(risk_free_rate, 0.03)
    daily_rf = annual_rf / trading_days

    cumulative = (1 + portfolio_returns).cumprod()
    rolling_max = cumulative.cummax()
    drawdown = (cumulative / rolling_max) - 1
    max_drawdown = _to_float(drawdown.min(), 0)

    annual_return = _to_float(portfolio_returns.mean() * trading_days)
    annual_volatility = _to_float(portfolio_returns.std(ddof=1) * np.sqrt(trading_days))
    downside = portfolio_returns[portfolio_returns < daily_rf] - daily_rf
    downside_deviation = _to_float(np.sqrt((downside ** 2).mean()) * np.sqrt(trading_days), 0)

    sharpe_ratio = None
    if annual_volatility > 0:
        sharpe_ratio = (annual_return - annual_rf) / annual_volatility

    sortino_ratio = None
    if downside_deviation > 0:
        sortino_ratio = (annual_return - annual_rf) / downside_deviation

    period_return = _to_float(cumulative.iloc[-1] - 1)
    cagr = (1 + period_return) ** (365 / total_days) - 1 if total_days > 0 else None

    var_95 = _to_float(np.percentile(portfolio_returns, 5))
    tail = portfolio_returns[portfolio_returns <= var_95]
    cvar_95 = _to_float(tail.mean()) if len(tail) > 0 else var_95

    beta = None
    alpha = None
    information_ratio = None
    correlation = None
    if benchmark_returns is not None and len(benchmark_returns) > 2:
        aligned = portfolio_returns.to_frame('p').join(benchmark_returns.to_frame('b'), how='inner').dropna()
        if len(aligned) > 2:
            cov = np.cov(aligned['p'], aligned['b'], ddof=1)[0][1]
            var_b = np.var(aligned['b'], ddof=1)
            if var_b > 0:
                beta = cov / var_b
                annual_bench_return = _to_float(aligned['b'].mean() * trading_days)
                alpha = annual_return - (annual_rf + beta * (annual_bench_return - annual_rf))
            active_return = aligned['p'] - aligned['b']
            tracking_error = _to_float(active_return.std(ddof=1) * np.sqrt(trading_days))
            if tracking_error > 0:
                information_ratio = _to_float(active_return.mean() * trading_days / tracking_error)
            correlation = _to_float(aligned['p'].corr(aligned['b']))

    result = {
        'ok': True,
        'inputs': {
            'tickers': available_tickers,
            'weights': [round(float(x), 6) for x in aligned_weights.tolist()],
            'period': period,
            'benchmark': benchmark,
            'riskFreeRate': round(annual_rf, 6),
        },
        'metrics': {
            'periodReturn': round(period_return, 6),
            'benchmarkPeriodReturn': round(benchmark_period_return, 6) if benchmark_period_return is not None else None,
            'cagr': round(float(cagr), 6) if cagr is not None else None,
            'annualReturn': round(annual_return, 6),
            'volatility': round(annual_volatility, 6),
            'sharpeRatio': round(float(sharpe_ratio), 6) if sharpe_ratio is not None else None,
            'sortinoRatio': round(float(sortino_ratio), 6) if sortino_ratio is not None else None,
            'maxDrawdown': round(max_drawdown, 6),
            'beta': round(float(beta), 6) if beta is not None else None,
            'alpha': round(float(alpha), 6) if alpha is not None else None,
            'informationRatio': round(float(information_ratio), 6) if information_ratio is not None else None,
            'correlationWithBenchmark': round(float(correlation), 6) if correlation is not None else None,
            'var95Daily': round(var_95, 6),
            'cvar95Daily': round(cvar_95, 6),
        },
    }
    return result, 200


@app.route('/api/price/<path:ticker>')
def price(ticker):
    data = get_price_data(ticker)
    if data is None:
        return jsonify({'ok': False, 'price': 0, 'change': 0, 'changePct': 0,
                        'name': ticker, 'currency': 'USD'}), 200
    return jsonify(data)


@app.route('/api/prices')
def prices():
    tickers = request.args.get('tickers', '')
    if not tickers:
        return jsonify({'error': 'tickers 파라미터가 필요합니다'}), 400
    ticker_list = [t.strip() for t in tickers.split(',') if t.strip()]

    def fetch_one(ticker):
        data = get_price_data(ticker)
        return ticker, data if data else {
            'ok': False, 'price': 0, 'change': 0, 'changePct': 0,
            'name': ticker, 'currency': 'USD'
        }

    with ThreadPoolExecutor(max_workers=min(len(ticker_list), 8)) as ex:
        result = dict(ex.map(lambda tk: fetch_one(tk), ticker_list))
    return jsonify(result)


@app.route('/api/history/<path:ticker>')
def history(ticker):
    range_param = request.args.get('range', '1mo')
    if range_param not in {'5d', '1mo', '3mo', 'ytd', '1y'}:
        return jsonify({'error': 'invalid range'}), 400
    try:
        close = _fetch_close_series_fdr(ticker, period=range_param)
        if close is None or close.empty:
            return jsonify({'startPrice': None})
        start_price = float(close.iloc[0])
        return jsonify({'startPrice': round(start_price, 6)})
    except Exception:
        return jsonify({'startPrice': None})


@app.route('/api/news/<path:ticker>')
def news(ticker):
    try:
        # FinanceDataReader는 뉴스 API를 제공하지 않아 Yahoo 검색 API 사용
        resp = requests.get(
            'https://query2.finance.yahoo.com/v1/finance/search',
            params={'q': ticker, 'quotesCount': 0, 'newsCount': 10,
                    'enableFuzzyQuery': 'true', 'lang': 'en-US'},
            timeout=6
        )
        resp.raise_for_status()
        raw_news = resp.json().get('news', []) or []
        articles = []
        for item in raw_news[:10]:
            title = item.get('title', '')
            url = item.get('link', '')
            provider = item.get('publisher', '')
            pub_time = item.get('providerPublishTime')
            if pub_time:
                try:
                    dt = datetime.fromtimestamp(pub_time)
                    diff = datetime.utcnow() - dt
                    hours = int(diff.total_seconds() // 3600)
                    time_str = f'{hours}시간 전' if hours < 24 else f'{hours // 24}일 전'
                except Exception:
                    time_str = ''
            else:
                time_str = ''
            if title:
                articles.append({
                    'title': title,
                    'titleOrig': title,
                    'url': url,
                    'source': provider,
                    'time': time_str,
                    'sentiment': 'neu',
                })
        return jsonify(articles)
    except Exception:
        return jsonify([])


@app.route('/api/search')
def search_ticker():
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify({'items': []})

    # 1순위: Finnhub 검색
    if FINNHUB_API_KEY:
        try:
            r = requests.get(
                f'{FINNHUB_BASE}/search',
                params={'q': query, 'token': FINNHUB_API_KEY},
                timeout=5
            )
            r.raise_for_status()
            results = r.json().get('result', []) or []
            items = []
            for item in results:
                fh_symbol = (item.get('symbol') or '').strip()
                if not fh_symbol:
                    continue
                item_type = (item.get('type') or '').upper()
                if item_type and item_type not in {'COMMON STOCK', 'ETP', 'ADR', 'GDR', ''}:
                    continue
                yf_symbol = finnhub_to_yf(fh_symbol)
                exchange = fh_symbol.split(':')[0] if ':' in fh_symbol else 'US'
                currency = EXCHANGE_CURRENCY.get(exchange, 'USD')
                items.append({
                    'symbol': yf_symbol,
                    'name': item.get('description', fh_symbol),
                    'exchange': item.get('displaySymbol', fh_symbol),
                    'currency': currency,
                    'type': item_type,
                })
            if items:
                return jsonify({'items': items[:10]})
        except Exception:
            pass

    # 2순위: 한국 주식 코드/한글 검색 (FDR KRX 상장종목)
    # 예) 005930, 삼성, 카카오
    is_korean_hint = any('\uac00' <= ch <= '\ud7a3' for ch in query) or query.isdigit()
    if is_korean_hint:
        try:
            krx = fdr.StockListing('KRX')
            q = query.upper()
            q_digits = ''.join(ch for ch in q if ch.isdigit())
            code_col = 'Code' if 'Code' in krx.columns else 'Symbol'
            name_col = 'Name' if 'Name' in krx.columns else None
            market_col = 'Market' if 'Market' in krx.columns else None
            matched = []
            for _, row in krx.iterrows():
                code = str(row.get(code_col, '')).strip()
                name = str(row.get(name_col, '')).strip() if name_col else ''
                market = str(row.get(market_col, '')).strip() if market_col else ''
                if not code:
                    continue
                if q_digits and q_digits in code:
                    pass
                elif query.lower() in name.lower():
                    pass
                else:
                    continue
                suffix = '.KQ' if market.upper() == 'KOSDAQ' else '.KS'
                matched.append({
                    'symbol': f'{code}{suffix}',
                    'name': name or code,
                    'exchange': market or 'KRX',
                    'currency': 'KRW',
                    'type': 'EQUITY',
                })
                if len(matched) >= 10:
                    break
            if matched:
                return jsonify({'items': matched})
        except Exception:
            pass

    # 3순위: Yahoo Finance 검색 (폴백)
    try:
        resp = requests.get(
            'https://query2.finance.yahoo.com/v1/finance/search',
            params={'q': query, 'quotesCount': 12, 'newsCount': 0,
                    'enableFuzzyQuery': 'true', 'lang': 'en-US'},
            timeout=6
        )
        resp.raise_for_status()
        quotes = resp.json().get('quotes', []) or []
        items = []
        for q in quotes:
            symbol = (q.get('symbol') or '').strip().upper()
            if not symbol:
                continue
            qtype = (q.get('quoteType') or '').upper()
            if qtype and qtype not in {'EQUITY', 'ETF', 'INDEX'}:
                continue
            items.append({
                'symbol': symbol,
                'name': q.get('longname') or q.get('shortname') or symbol,
                'exchange': q.get('exchDisp') or q.get('exchange') or '',
                'currency': (q.get('currency') or '').upper(),
                'type': qtype or '',
            })
        return jsonify({'items': items[:10]})
    except Exception:
        return jsonify({'items': []})


@app.route('/api/portfolio/metrics', methods=['POST'])
def portfolio_metrics():
    payload = request.get_json(silent=True) or {}
    tickers = payload.get('tickers', [])
    weights = payload.get('weights')
    period = payload.get('period', '1y')
    benchmark = payload.get('benchmark', '^GSPC')
    risk_free_rate = payload.get('riskFreeRate', 0.03)

    result, status = calculate_portfolio_metrics(
        tickers=tickers,
        weights=weights,
        period=period,
        benchmark=benchmark,
        risk_free_rate=risk_free_rate
    )
    return jsonify(result), status


@app.route('/api/portfolio/state', methods=['GET'])
def get_portfolio_state():
    key = (request.args.get('key') or 'default').strip() or 'default'
    with STATE_LOCK:
        all_states = load_state()
        state = all_states.get(key)
    return jsonify({'ok': True, 'state': state}), 200


@app.route('/api/portfolio/state', methods=['POST'])
def set_portfolio_state():
    payload = request.get_json(silent=True) or {}
    key = (payload.get('key') or 'default').strip() or 'default'
    state = payload.get('state')
    if not isinstance(state, dict):
        return jsonify({'ok': False, 'error': 'state는 객체여야 합니다.'}), 400
    safe_state = {
        'holdings': state.get('holdings') if isinstance(state.get('holdings'), list) else [],
        'trades': state.get('trades') if isinstance(state.get('trades'), dict) else {},
        'watchlist': state.get('watchlist') if isinstance(state.get('watchlist'), list) else [],
        'appSettings': state.get('appSettings') if isinstance(state.get('appSettings'), dict) else {},
    }
    with STATE_LOCK:
        all_states = load_state()
        all_states[key] = safe_state
        save_state(all_states)
    return jsonify({'ok': True}), 200


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
