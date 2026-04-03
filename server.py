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

ALPHA_VANTAGE_API_KEY = os.environ.get('ALPHA_VANTAGE_API_KEY', 'M23I4O2KN7VDGIL8')
ALPHA_VANTAGE_BASE = 'https://www.alphavantage.co/query'

# 거래소 코드 → 통화 매핑
EXCHANGE_CURRENCY = {
    'KS': 'KRW', 'KQ': 'KRW', 'KRX': 'KRW',
    'HK': 'HKD', 'HKG': 'HKD',
    'T': 'JPY', 'TRT': 'JPY', 'TYO': 'JPY',
    'TW': 'TWD', 'TWO': 'TWD', 'TAI': 'TWD',
    'L': 'GBP',
    'PA': 'EUR', 'DE': 'EUR', 'MI': 'EUR',
    'SS': 'CNY', 'SZ': 'CNY', 'SHH': 'CNY', 'SHZ': 'CNY',
    'BSE': 'INR', 'BOM': 'INR', 'NSE': 'INR', 'NSI': 'INR',
    'SET': 'THB', 'BKK': 'THB',
    'KLS': 'MYR', 'KLSE': 'MYR',
    'JKT': 'IDR',
    'SGX': 'SGD', 'SES': 'SGD', 'ST': 'SGD',
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
YAHOO_CHART_HOSTS = ('query1.finance.yahoo.com', 'query2.finance.yahoo.com')


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


def _ticker_to_alpha_symbol(ticker):
    """Yahoo/FDR 친화 티커를 Alpha Vantage 심볼로 변환."""
    ticker = _normalize_ticker(ticker)
    if ticker.startswith('^') or '=X' in ticker:
        return ticker
    if '.' not in ticker:
        return ticker
    base, exchange = ticker.rsplit('.', 1)
    alpha_exchange_map = {
        'KS': 'KRX', 'KQ': 'KRX',
        'HK': 'HKG',
        'T': 'TRT',
        'TW': 'TWO',
        'SS': 'SHH', 'SZ': 'SHZ',
    }
    alpha_exchange = alpha_exchange_map.get(exchange, exchange)
    return f'{base}.{alpha_exchange}'


def _alpha_symbol_to_yf(symbol):
    symbol = (symbol or '').strip().upper()
    if '.' not in symbol:
        return _normalize_ticker(symbol)
    base, exchange = symbol.rsplit('.', 1)
    yf_exchange_map = {
        'KRX': 'KS',
        'HKG': 'HK',
        'TRT': 'T',
        'TYO': 'T',
        'TWO': 'TW',
        'TAI': 'TW',
        'SHH': 'SS',
        'SHZ': 'SZ',
    }
    yf_exchange = yf_exchange_map.get(exchange, exchange)
    return _normalize_ticker(f'{base}.{yf_exchange}')


def _alpha_vantage_query(params):
    if not ALPHA_VANTAGE_API_KEY:
        return None
    try:
        merged = dict(params)
        merged['apikey'] = ALPHA_VANTAGE_API_KEY
        r = requests.get(ALPHA_VANTAGE_BASE, params=merged, timeout=8)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return None
        if data.get('Error Message') or data.get('Note'):
            return None
        return data
    except Exception:
        return None


def _latest_market_close(series):
    if series is None or series.empty:
        return series
    today = datetime.utcnow().date()
    filtered = series[series.index.date < today]
    if filtered is not None and not filtered.empty:
        return filtered
    return series


def _parse_alpha_daily_series(data, ticker):
    ts = data.get('Time Series (Daily)') if isinstance(data, dict) else None
    if not ts or not isinstance(ts, dict):
        return None
    rows = []
    for d, values in ts.items():
        if not isinstance(values, dict):
            continue
        close_val = values.get('4. close') or values.get('5. adjusted close')
        try:
            rows.append((pd.to_datetime(d), float(close_val)))
        except Exception:
            continue
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    idx = [x[0] for x in rows]
    vals = [x[1] for x in rows]
    s = pd.Series(vals, index=idx, name=ticker, dtype='float64')
    s = s.dropna()
    if s.empty:
        return None
    s = _latest_market_close(s)
    return s if s is not None and not s.empty else None


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


def _fetch_yahoo_chart_json(ticker, range_param='3mo', interval='1d'):
    ticker = _normalize_ticker(ticker)
    params = {'interval': interval, 'range': range_param}
    for host in YAHOO_CHART_HOSTS:
        try:
            r = requests.get(
                f'https://{host}/v8/finance/chart/{ticker}',
                params=params,
                timeout=8
            )
            r.raise_for_status()
            data = r.json()
            chart = data.get('chart', {}) if isinstance(data, dict) else {}
            if chart.get('error') is None and isinstance(chart.get('result'), list) and chart.get('result'):
                return data
        except Exception:
            continue
    return None


def _parse_yahoo_close_series(data, ticker):
    try:
        result = data.get('chart', {}).get('result', [])[0]
        ts = result.get('timestamp') or []
        quote = (result.get('indicators') or {}).get('quote', [{}])[0]
        closes = quote.get('close') or []
        rows = []
        for t, c in zip(ts, closes):
            if c is None:
                continue
            rows.append((pd.to_datetime(int(t), unit='s', utc=True).tz_localize(None), float(c)))
        if not rows:
            return None
        rows.sort(key=lambda x: x[0])
        s = pd.Series([v for _, v in rows], index=[d for d, _ in rows], name=ticker, dtype='float64')
        s = s.dropna()
        if s.empty:
            return None
        return _latest_market_close(s)
    except Exception:
        return None


def _yahoo_price_data(ticker):
    ticker = _normalize_ticker(ticker)
    data = _fetch_yahoo_chart_json(ticker, range_param='5d', interval='1d')
    if not data:
        return None
    try:
        result = data.get('chart', {}).get('result', [])[0]
        meta = result.get('meta', {}) or {}
        price = _to_float(meta.get('regularMarketPrice'))
        prev = _to_float(meta.get('chartPreviousClose') or meta.get('previousClose'))
        if price <= 0:
            close = _parse_yahoo_close_series(data, ticker)
            if close is None or close.empty:
                return None
            price = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) >= 2 else price
        change = price - prev
        change_pct = (change / prev * 100) if prev else 0
        currency = (meta.get('currency') or _infer_currency(ticker) or 'USD').upper()
        return {
            'price': round(price, 6),
            'change': round(change, 6),
            'changePct': round(change_pct, 4),
            'name': meta.get('longName') or meta.get('shortName') or ticker,
            'currency': currency,
            'ok': True,
        }
    except Exception:
        return None


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
    if ticker.endswith('.TW'):
        return 'TWD'
    if ticker.endswith('.L'):
        return 'GBP'
    if ticker.endswith('.HK'):
        return 'HKD'
    if ticker.endswith('.SS') or ticker.endswith('.SZ'):
        return 'CNY'
    if ticker.endswith('.NS') or ticker.endswith('.BO'):
        return 'INR'
    if ticker.endswith('.SI'):
        return 'SGD'
    return 'USD'


def _fetch_close_series_fdr(ticker, period='1y'):
    ticker = _normalize_ticker(ticker)
    # 1순위: Yahoo Finance chart (한국/해외 공통, 안정적)
    yahoo_data = _fetch_yahoo_chart_json(ticker, range_param=period, interval='1d')
    yahoo_series = _parse_yahoo_close_series(yahoo_data, ticker) if yahoo_data else None
    if yahoo_series is not None and not yahoo_series.empty:
        start = _period_to_start_date(period)
        yahoo_series = yahoo_series[yahoo_series.index.date >= start]
        if yahoo_series is not None and not yahoo_series.empty:
            yahoo_series.name = ticker
            return yahoo_series

    # 2순위: Alpha Vantage daily
    alpha_symbol = _ticker_to_alpha_symbol(ticker)
    if not alpha_symbol:
        return None
    output_size = 'full' if period in {'2y', '5y', '10y', 'max'} else 'compact'
    data = _alpha_vantage_query({
        'function': 'TIME_SERIES_DAILY_ADJUSTED',
        'symbol': alpha_symbol,
        'outputsize': output_size,
    })
    s = _parse_alpha_daily_series(data, ticker)
    if s is None or s.empty:
        return None
    start = _period_to_start_date(period)
    s = s[s.index.date >= start]
    if s.empty:
        return None
    s.name = ticker
    return s


def _fdr_price_data(ticker):
    try:
        close = _fetch_close_series_fdr(ticker, period='3mo')
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
    # 1순위: Yahoo Finance chart API
    yahoo_result = _yahoo_price_data(ticker)
    if yahoo_result is not None:
        return yahoo_result

    # 2순위: Alpha Vantage Daily 기반 전일 종가
    fdr_result = _fdr_price_data(ticker)
    if fdr_result is not None:
        return fdr_result

    # 3순위: Global Quote (응답 축소 시)
    try:
        alpha_symbol = _ticker_to_alpha_symbol(ticker)
        data = _alpha_vantage_query({'function': 'GLOBAL_QUOTE', 'symbol': alpha_symbol})
        quote = data.get('Global Quote', {}) if isinstance(data, dict) else {}
        if isinstance(quote, dict) and quote:
            price = _to_float(quote.get('05. price'))
            prev = _to_float(quote.get('08. previous close'))
            if price > 0:
                change = price - prev
                change_pct = (change / prev * 100) if prev else 0
                exchange = alpha_symbol.split('.')[-1] if '.' in alpha_symbol else 'US'
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
    q_upper = query.upper()
    q_digits = ''.join(ch for ch in q_upper if ch.isdigit())

    # 0순위: 한국 6자리 코드 직접 입력 시 즉시 반환
    if q_digits and len(q_digits) == 6:
        quick_items = []
        for suffix, market in (('.KS', 'KOSPI'), ('.KQ', 'KOSDAQ')):
            quick_items.append({
                'symbol': f'{q_digits}{suffix}',
                'name': q_digits,
                'exchange': market,
                'currency': 'KRW',
                'type': 'EQUITY',
            })
        return jsonify({'items': quick_items})

    # 1순위: Alpha Vantage SYMBOL_SEARCH
    data = _alpha_vantage_query({'function': 'SYMBOL_SEARCH', 'keywords': query})
    if data and isinstance(data.get('bestMatches'), list):
        items = []
        for item in data.get('bestMatches', []):
            symbol = (item.get('1. symbol') or '').strip().upper()
            if not symbol:
                continue
            region = (item.get('4. region') or '').strip()
            item_type = (item.get('3. type') or '').strip().upper()
            yf_symbol = _alpha_symbol_to_yf(symbol)
            exchange = symbol.rsplit('.', 1)[-1] if '.' in symbol else (region or 'US')
            currency = EXCHANGE_CURRENCY.get(exchange, (item.get('8. currency') or 'USD').upper())
            items.append({
                'symbol': yf_symbol,
                'name': item.get('2. name') or symbol,
                'exchange': region or exchange,
                'currency': currency,
                'type': item_type or 'EQUITY',
            })
            if len(items) >= 10:
                break
        if items:
            return jsonify({'items': items})

    # 2순위: 한국 주식 코드/한글 검색 (FDR KRX 상장종목)
    # 예) 005930, 삼성, 카카오
    is_korean_hint = any('\uac00' <= ch <= '\ud7a3' for ch in query) or q_digits != ''
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
