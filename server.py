from flask import Flask, jsonify, request
from flask_cors import CORS
import FinanceDataReader as fdr
import numpy as np
import pandas as pd
import requests
import yfinance as yf
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import time

app = Flask(__name__)
CORS(app)
ALPHA_VANTAGE_API_KEY = os.environ.get('ALPHA_VANTAGE_API_KEY', 'M23I4O2KN7VDGIL8')
ALPHA_VANTAGE_BASE = 'https://www.alphavantage.co/query'
SUPABASE_URL = (os.environ.get('SUPABASE_URL') or '').rstrip('/')
SUPABASE_SERVICE_ROLE_KEY = (
    os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    or os.environ.get('SUPABASE_KEY')
    or ''
)

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
PREV_CLOSE_CACHE_TTL_SECONDS = 20
_PREV_CLOSE_CACHE = {}
LIVE_PRICE_CACHE_TTL_SECONDS = 5
_LIVE_PRICE_CACHE = {}


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
    # 1순위: yfinance history
    try:
        hist = yf.Ticker(ticker).history(period=period, interval='1d', auto_adjust=False)
        if hist is not None and not hist.empty and 'Close' in hist.columns:
            close = hist['Close'].dropna()
            if close is not None and not close.empty:
                if getattr(close.index, 'tz', None) is not None:
                    close.index = close.index.tz_localize(None)
                close = _latest_market_close(close)
                start = _period_to_start_date(period)
                close = close[close.index.date >= start]
                if close is not None and not close.empty:
                    close.name = ticker
                    return close
    except Exception:
        pass

    # 2순위: Yahoo Finance chart (직접 API)
    yahoo_data = _fetch_yahoo_chart_json(ticker, range_param=period, interval='1d')
    yahoo_series = _parse_yahoo_close_series(yahoo_data, ticker) if yahoo_data else None
    if yahoo_series is not None and not yahoo_series.empty:
        start = _period_to_start_date(period)
        yahoo_series = yahoo_series[yahoo_series.index.date >= start]
        if yahoo_series is not None and not yahoo_series.empty:
            yahoo_series.name = ticker
            return yahoo_series

    # 3순위: Alpha Vantage daily
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
    # 1순위: yfinance
    try:
        hist = yf.Ticker(ticker).history(period='10d', interval='1d', auto_adjust=False)
        if hist is not None and not hist.empty and 'Close' in hist.columns:
            close = hist['Close'].dropna()
            if close is not None and len(close) >= 1:
                if getattr(close.index, 'tz', None) is not None:
                    close.index = close.index.tz_localize(None)
                close = _latest_market_close(close)
                if close is not None and len(close) >= 1:
                    price = float(close.iloc[-1])
                    prev = float(close.iloc[-2]) if len(close) >= 2 else price
                    change = price - prev
                    change_pct = (change / prev * 100) if prev else 0
                    currency = _infer_currency(ticker)
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

    # 2순위: Yahoo Finance chart API
    yahoo_result = _yahoo_price_data(ticker)
    if yahoo_result is not None:
        return yahoo_result

    # 3순위: Alpha Vantage Daily 기반 전일 종가
    fdr_result = _fdr_price_data(ticker)
    if fdr_result is not None:
        return fdr_result

    # 4순위: Global Quote (응답 축소 시)
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


def _fetch_prev_close_prices(tickers):
    normalized = sorted({(t or '').strip().upper() for t in tickers if str(t or '').strip()})
    if not normalized:
        return {}
    in_clause = '(' + ','.join([f'"{t}"' for t in normalized]) + ')'
    rows, err = _supabase_request(
        'GET',
        'market_prev_close',
        params={
            'ticker': f'in.{in_clause}',
            'select': 'ticker,market_date,close_price,prev_close,change,change_pct',
            'order': 'ticker.asc,market_date.desc',
            'limit': len(normalized) * 10,
        }
    )
    if err:
        return {}

    by_ticker = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        ticker = (row.get('ticker') or '').upper()
        if not ticker or ticker in by_ticker:
            continue
        close_price = _to_float(row.get('close_price'), 0)
        prev_close = _to_float(row.get('prev_close'), 0)
        change = _to_float(row.get('change'), close_price - prev_close)
        change_pct = _to_float(row.get('change_pct'), (change / prev_close * 100) if prev_close else 0)
        by_ticker[ticker] = {
            'price': close_price,
            'change': change,
            'changePct': change_pct,
            'ok': close_price > 0,
            'marketDate': row.get('market_date'),
            'source': 'supabase',
        }
    return by_ticker


def _fetch_live_prices(tickers):
    normalized = sorted({(t or '').strip().upper() for t in tickers if str(t or '').strip()})
    if not normalized:
        return {}

    def fetch_one(ticker):
        data = get_price_data(ticker)
        if data is None:
            return ticker, {
                'ok': False,
                'price': 0,
                'change': 0,
                'changePct': 0,
                'name': ticker,
                'currency': _infer_currency(ticker),
                'source': 'live',
            }
        result = dict(data)
        result['source'] = 'live'
        return ticker, result

    with ThreadPoolExecutor(max_workers=min(len(normalized), 8)) as ex:
        return dict(ex.map(fetch_one, normalized))


def _supabase_enabled():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _supabase_headers():
    return {
        'apikey': SUPABASE_SERVICE_ROLE_KEY,
        'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}',
        'Content-Type': 'application/json',
    }


def _supabase_request(method, path, *, params=None, payload=None, prefer=None):
    if not _supabase_enabled():
        return None, 'Supabase 환경변수가 설정되지 않았습니다.'
    headers = _supabase_headers()
    if prefer:
        headers['Prefer'] = prefer
    try:
        resp = requests.request(
            method.upper(),
            f'{SUPABASE_URL}/rest/v1/{path.lstrip("/")}',
            params=params,
            json=payload,
            headers=headers,
            timeout=10
        )
        if not resp.ok:
            hint = ''
            if resp.status_code in (401, 403):
                hint = ' (SUPABASE_SERVICE_ROLE_KEY가 누락/오류이거나 RLS 정책 문제일 수 있습니다)'
            return None, f'Supabase API 오류({resp.status_code}): {resp.text[:200]}{hint}'
        if not resp.text:
            return None, None
        return resp.json(), None
    except Exception as e:
        return None, str(e)


def _supabase_load_portfolio_state(key):
    holdings_params = {
        'portfolio_key': f'eq.{key}',
        'select': 'holding_id,ticker,name,avg_price,qty,currency,sector,current,change,change_pct,sort_order',
        'order': 'sort_order.asc,holding_id.asc',
        'limit': 5000,
    }
    holdings_data, holdings_err = _supabase_request('GET', 'holdings', params=holdings_params)
    if holdings_err:
        return None, holdings_err

    trades_params = {
        'portfolio_key': f'eq.{key}',
        'select': 'trade_id,holding_id,trade_date,side,price,qty,memo',
        'order': 'trade_date.desc',
        'limit': 20000,
    }
    trades_data, trades_err = _supabase_request('GET', 'trades', params=trades_params)
    if trades_err:
        return None, trades_err

    watchlist_params = {
        'portfolio_key': f'eq.{key}',
        'select': 'ticker',
        'order': 'ticker.asc',
        'limit': 1000,
    }
    watchlist_data, watchlist_err = _supabase_request('GET', 'watchlist', params=watchlist_params)
    if watchlist_err:
        return None, watchlist_err

    settings_data, settings_err = _supabase_request(
        'GET',
        'portfolio_settings',
        params={
            'portfolio_key': f'eq.{key}',
            'select': 'settings_json',
            'limit': 1,
        }
    )
    if settings_err:
        return None, settings_err

    holdings = []
    for row in holdings_data if isinstance(holdings_data, list) else []:
        if not isinstance(row, dict):
            continue
        holdings.append({
            'id': _to_float(row.get('holding_id'), 0),
            'ticker': (row.get('ticker') or '').upper(),
            'name': row.get('name') or '',
            'avgPrice': _to_float(row.get('avg_price'), 0),
            'qty': _to_float(row.get('qty'), 0),
            'currency': (row.get('currency') or 'USD').upper(),
            'sector': row.get('sector') or '',
            'current': _to_float(row.get('current'), 0),
            'change': _to_float(row.get('change'), 0),
            'changePct': _to_float(row.get('change_pct'), 0),
            'sortOrder': int(_to_float(row.get('sort_order'), 0)),
        })

    trade_map = {}
    for row in trades_data if isinstance(trades_data, list) else []:
        if not isinstance(row, dict):
            continue
        holding_id = str(row.get('holding_id') or '').strip()
        if not holding_id:
            continue
        if holding_id not in trade_map:
            trade_map[holding_id] = []
        trade_map[holding_id].append({
            'id': _to_float(row.get('trade_id'), 0),
            'date': row.get('trade_date') or '',
            'type': (row.get('side') or 'buy').lower(),
            'price': _to_float(row.get('price'), 0),
            'qty': _to_float(row.get('qty'), 0),
            'memo': row.get('memo') or '',
        })

    watchlist = []
    for row in watchlist_data if isinstance(watchlist_data, list) else []:
        if not isinstance(row, dict):
            continue
        ticker = (row.get('ticker') or '').strip().upper()
        if ticker:
            watchlist.append(ticker)

    app_settings = {}
    if isinstance(settings_data, list) and settings_data:
        app_settings = settings_data[0].get('settings_json') or {}
        if not isinstance(app_settings, dict):
            app_settings = {}

    return {
        'holdings': holdings,
        'trades': trade_map,
        'watchlist': watchlist,
        'appSettings': app_settings,
    }, None


def _supabase_save_portfolio_state(key, state):
    holdings = state.get('holdings') if isinstance(state.get('holdings'), list) else []
    trades = state.get('trades') if isinstance(state.get('trades'), dict) else {}
    watchlist = state.get('watchlist') if isinstance(state.get('watchlist'), list) else []
    app_settings = state.get('appSettings') if isinstance(state.get('appSettings'), dict) else {}

    for table_name in ('holdings', 'trades', 'watchlist', 'portfolio_settings'):
        _, del_err = _supabase_request(
            'DELETE',
            table_name,
            params={'portfolio_key': f'eq.{key}'}
        )
        if del_err:
            return del_err

    holding_rows = []
    for order_idx, h in enumerate(holdings):
        if not isinstance(h, dict):
            continue
        holding_rows.append({
            'portfolio_key': key,
            'holding_id': str(h.get('id') or ''),
            'ticker': (h.get('ticker') or '').upper(),
            'name': h.get('name') or '',
            'avg_price': _to_float(h.get('avgPrice'), 0),
            'qty': _to_float(h.get('qty'), 0),
            'currency': (h.get('currency') or 'USD').upper(),
            'sector': h.get('sector') or '',
            'current': _to_float(h.get('current'), 0),
            'change': _to_float(h.get('change'), 0),
            'change_pct': _to_float(h.get('changePct'), 0),
            'sort_order': int(_to_float(h.get('sortOrder'), order_idx)),
        })
    if holding_rows:
        _, holdings_err = _supabase_request('POST', 'holdings', payload=holding_rows)
        if holdings_err:
            return holdings_err

    trade_rows = []
    for holding_id, trade_list in trades.items():
        if not isinstance(trade_list, list):
            continue
        for t in trade_list:
            if not isinstance(t, dict):
                continue
            trade_rows.append({
                'portfolio_key': key,
                'trade_id': str(t.get('id') or ''),
                'holding_id': str(holding_id),
                'trade_date': t.get('date') or '',
                'side': (t.get('type') or 'buy').lower(),
                'price': _to_float(t.get('price'), 0),
                'qty': _to_float(t.get('qty'), 0),
                'memo': t.get('memo') or '',
            })
    if trade_rows:
        _, trades_err = _supabase_request('POST', 'trades', payload=trade_rows)
        if trades_err:
            return trades_err

    watchlist_rows = []
    for t in watchlist:
        ticker = str(t or '').strip().upper()
        if ticker:
            watchlist_rows.append({
                'portfolio_key': key,
                'ticker': ticker,
            })
    if watchlist_rows:
        _, watchlist_err = _supabase_request('POST', 'watchlist', payload=watchlist_rows)
        if watchlist_err:
            return watchlist_err

    _, settings_err = _supabase_request(
        'POST',
        'portfolio_settings',
        payload=[{
            'portfolio_key': key,
            'settings_json': app_settings,
        }],
        prefer='resolution=merge-duplicates'
    )
    if settings_err:
        return settings_err

    return None


def _to_krw(amount, currency, fx_rates, usd_krw):
    cur = (currency or 'USD').upper()
    if cur == 'KRW':
        return float(amount)
    denom = _to_float(fx_rates.get(cur), 0)
    if denom <= 0:
        denom = 1
    return (float(amount) / denom) * float(usd_krw)


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
    if not _supabase_enabled():
        return jsonify({'ok': False, 'error': 'Supabase 환경변수가 설정되지 않았습니다.', 'state': None}), 503
    state, err = _supabase_load_portfolio_state(key)
    if err:
        return jsonify({'ok': False, 'error': err, 'state': None}), 500
    return jsonify({'ok': True, 'state': state}), 200


@app.route('/api/market/prev-close', methods=['GET'])
def get_market_prev_close():
    key = (request.args.get('key') or 'default').strip() or 'default'
    tickers_param = (request.args.get('tickers') or '').strip()
    tickers = [t.strip().upper() for t in tickers_param.split(',') if t.strip()]
    if not tickers:
        state, err = _supabase_load_portfolio_state(key)
        if err:
            return jsonify({'ok': False, 'error': err, 'prices': {}}), 500
        tickers = sorted({
            (h.get('ticker') or '').upper()
            for h in state.get('holdings', [])
            if isinstance(h, dict) and (h.get('ticker') or '').strip()
        })
    if not tickers:
        return jsonify({'ok': True, 'prices': {}}), 200

    cache_key = f'{key}|' + ','.join(sorted(set(tickers)))
    now_ts = time.time()
    cached = _PREV_CLOSE_CACHE.get(cache_key)
    if cached and (now_ts - cached.get('ts', 0) <= PREV_CLOSE_CACHE_TTL_SECONDS):
        return jsonify({'ok': True, 'prices': cached.get('prices', {}), 'cached': True}), 200

    by_ticker = _fetch_prev_close_prices(tickers)
    _PREV_CLOSE_CACHE[cache_key] = {'ts': now_ts, 'prices': by_ticker}
    return jsonify({'ok': True, 'prices': by_ticker, 'cached': False}), 200


@app.route('/api/market/prices', methods=['GET'])
def get_market_prices():
    key = (request.args.get('key') or 'default').strip() or 'default'
    tickers_param = (request.args.get('tickers') or '').strip()
    tickers = [t.strip().upper() for t in tickers_param.split(',') if t.strip()]
    if not tickers:
        state, err = _supabase_load_portfolio_state(key)
        if err:
            return jsonify({'ok': False, 'error': err, 'prices': {}}), 500
        tickers = sorted({
            (h.get('ticker') or '').upper()
            for h in state.get('holdings', [])
            if isinstance(h, dict) and (h.get('ticker') or '').strip()
        })
    if not tickers:
        return jsonify({'ok': True, 'prices': {}}), 200

    cache_key = f'{key}|' + ','.join(sorted(set(tickers)))
    now_ts = time.time()
    cached = _LIVE_PRICE_CACHE.get(cache_key)
    if cached and (now_ts - cached.get('ts', 0) <= LIVE_PRICE_CACHE_TTL_SECONDS):
        return jsonify({'ok': True, 'prices': cached.get('prices', {}), 'cached': True}), 200

    live_prices = _fetch_live_prices(tickers)
    missing = [t for t in tickers if not live_prices.get(t, {}).get('ok')]
    fallback = _fetch_prev_close_prices(missing) if missing else {}

    merged = {}
    for ticker in tickers:
        live = live_prices.get(ticker)
        if live and live.get('ok'):
            merged[ticker] = live
            continue
        fallback_row = fallback.get(ticker)
        if fallback_row:
            merged[ticker] = fallback_row
        else:
            merged[ticker] = live or {
                'ok': False,
                'price': 0,
                'change': 0,
                'changePct': 0,
                'name': ticker,
                'currency': _infer_currency(ticker),
                'source': 'none',
            }

    _LIVE_PRICE_CACHE[cache_key] = {'ts': now_ts, 'prices': merged}
    return jsonify({'ok': True, 'prices': merged, 'cached': False}), 200


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
    if not _supabase_enabled():
        return jsonify({'ok': False, 'error': 'Supabase 환경변수가 설정되지 않았습니다.'}), 503
    err = _supabase_save_portfolio_state(key, safe_state)
    if err:
        return jsonify({'ok': False, 'error': err}), 500
    return jsonify({'ok': True}), 200


@app.route('/api/portfolio/daily/snapshot', methods=['POST'])
def upsert_daily_snapshot():
    payload = request.get_json(silent=True) or {}
    key = (payload.get('key') or 'default').strip() or 'default'
    state = payload.get('state') if isinstance(payload.get('state'), dict) else {}
    fx_rates = payload.get('fxRates') if isinstance(payload.get('fxRates'), dict) else {}
    usd_krw = _to_float(payload.get('usdKrw'), 1360)
    snapshot_date = (payload.get('snapshotDate') or datetime.utcnow().date().isoformat()).strip()

    holdings = state.get('holdings') if isinstance(state.get('holdings'), list) else []
    total_cost = 0.0
    total_market = 0.0
    holding_rows = []

    for h in holdings:
        if not isinstance(h, dict):
            continue
        qty = _to_float(h.get('qty'), 0)
        avg_price = _to_float(h.get('avgPrice'), 0)
        current = _to_float(h.get('current'), 0)
        change = _to_float(h.get('change'), 0)
        reference_close = current - change if current > 0 else avg_price
        if reference_close <= 0:
            reference_close = avg_price
        currency = (h.get('currency') or 'USD').upper()

        cost_krw = _to_krw(avg_price * qty, currency, fx_rates, usd_krw)
        market_krw = _to_krw(reference_close * qty, currency, fx_rates, usd_krw)
        pl_krw = market_krw - cost_krw
        return_pct = (pl_krw / cost_krw * 100) if cost_krw > 0 else 0
        total_cost += cost_krw
        total_market += market_krw

        holding_rows.append({
            'portfolio_key': key,
            'snapshot_date': snapshot_date,
            'holding_id': str(h.get('id') or ''),
            'ticker': (h.get('ticker') or '').upper(),
            'name': h.get('name') or '',
            'currency': currency,
            'qty': qty,
            'avg_price': avg_price,
            'current_price': reference_close,
            'market_value_krw': round(market_krw, 4),
            'pl_krw': round(pl_krw, 4),
            'return_pct': round(return_pct, 6),
        })

    total_pl = total_market - total_cost
    total_return_pct = (total_pl / total_cost * 100) if total_cost > 0 else 0
    portfolio_row = {
        'portfolio_key': key,
        'snapshot_date': snapshot_date,
        'holdings_count': len(holding_rows),
        'total_cost_krw': round(total_cost, 4),
        'total_market_krw': round(total_market, 4),
        'total_pl_krw': round(total_pl, 4),
        'total_return_pct': round(total_return_pct, 6),
    }

    p_data, p_err = _supabase_request(
        'POST',
        'portfolio_daily_snapshots',
        payload=[portfolio_row],
        prefer='resolution=merge-duplicates,return=representation'
    )
    if p_err:
        return jsonify({'ok': False, 'error': p_err}), 500

    h_data = []
    if holding_rows:
        h_data, h_err = _supabase_request(
            'POST',
            'holding_daily_snapshots',
            payload=holding_rows,
            prefer='resolution=merge-duplicates,return=representation'
        )
        if h_err:
            return jsonify({'ok': False, 'error': h_err}), 500

    return jsonify({
        'ok': True,
        'snapshotDate': snapshot_date,
        'portfolio': p_data[0] if isinstance(p_data, list) and p_data else portfolio_row,
        'holdingCount': len(h_data) if isinstance(h_data, list) else len(holding_rows),
    }), 200


@app.route('/api/portfolio/daily/portfolio', methods=['GET'])
def get_portfolio_daily_history():
    key = (request.args.get('key') or 'default').strip() or 'default'
    days = max(1, min(_to_float(request.args.get('days'), 30), 365))
    params = {
        'portfolio_key': f'eq.{key}',
        'select': 'snapshot_date,total_cost_krw,total_market_krw,total_pl_krw,total_return_pct,holdings_count',
        'order': 'snapshot_date.desc',
        'limit': int(days),
    }
    data, err = _supabase_request('GET', 'portfolio_daily_snapshots', params=params)
    if err:
        return jsonify({'ok': False, 'error': err, 'items': []}), 500
    items = list(reversed(data if isinstance(data, list) else []))
    return jsonify({'ok': True, 'items': items}), 200


@app.route('/api/portfolio/daily/holdings', methods=['GET'])
def get_holding_daily_history():
    key = (request.args.get('key') or 'default').strip() or 'default'
    ticker = (request.args.get('ticker') or '').strip().upper()
    if not ticker:
        return jsonify({'ok': False, 'error': 'ticker 파라미터가 필요합니다.', 'items': []}), 400
    days = max(1, min(_to_float(request.args.get('days'), 30), 365))
    params = {
        'portfolio_key': f'eq.{key}',
        'ticker': f'eq.{ticker}',
        'select': 'snapshot_date,ticker,name,qty,avg_price,current_price,market_value_krw,pl_krw,return_pct,currency',
        'order': 'snapshot_date.desc',
        'limit': int(days),
    }
    data, err = _supabase_request('GET', 'holding_daily_snapshots', params=params)
    if err:
        return jsonify({'ok': False, 'error': err, 'items': []}), 500
    items = list(reversed(data if isinstance(data, list) else []))
    return jsonify({'ok': True, 'items': items}), 200


@app.route('/api/portfolio/period-returns', methods=['GET'])
def get_portfolio_period_returns():
    key = (request.args.get('key') or 'default').strip() or 'default'
    params = {
        'portfolio_key': f'eq.{key}',
        'select': 'snapshot_date,total_cost_krw,total_market_krw,total_pl_krw',
        'order': 'snapshot_date.asc',
        'limit': 5000,
    }
    data, err = _supabase_request('GET', 'portfolio_daily_snapshots', params=params)
    if err:
        return jsonify({'ok': False, 'error': err, 'periods': {}}), 500

    rows = []
    for row in (data if isinstance(data, list) else []):
        if not isinstance(row, dict):
            continue
        d = row.get('snapshot_date')
        try:
            d_obj = datetime.strptime(str(d), '%Y-%m-%d').date()
        except Exception:
            continue
        rows.append({
            'snapshot_date': d_obj,
            'snapshot_date_str': str(d_obj),
            'total_market_krw': _to_float(row.get('total_market_krw'), 0),
            'total_cost_krw': _to_float(row.get('total_cost_krw'), 0),
            'total_pl_krw': _to_float(row.get('total_pl_krw'), 0),
        })

    if not rows:
        return jsonify({'ok': True, 'asOfDate': None, 'periods': {}}), 200

    latest = rows[-1]
    latest_date = latest['snapshot_date']
    latest_market = latest['total_market_krw']

    def find_on_or_before(target_date):
        found = None
        for r in rows:
            if r['snapshot_date'] <= target_date:
                found = r
            else:
                break
        return found

    def as_period(base_row):
        if not base_row:
            return {'hasData': False, 'gainKrw': None, 'pct': None, 'baseDate': None}
        base_market = _to_float(base_row.get('total_market_krw'), 0)
        if base_market <= 0:
            return {'hasData': False, 'gainKrw': None, 'pct': None, 'baseDate': base_row.get('snapshot_date_str')}
        gain = latest_market - base_market
        pct = (gain / base_market * 100) if base_market > 0 else None
        return {
            'hasData': True,
            'gainKrw': round(gain, 4),
            'pct': round(pct, 6) if pct is not None else None,
            'baseDate': base_row.get('snapshot_date_str'),
        }

    one_day_base = rows[-2] if len(rows) >= 2 else None
    period_targets = {
        '1w': latest_date - timedelta(days=7),
        '1m': latest_date - timedelta(days=30),
        '3m': latest_date - timedelta(days=90),
        'ytd': datetime(latest_date.year, 1, 1).date(),
    }
    periods = {
        '1d': as_period(one_day_base),
    }
    for p, target in period_targets.items():
        periods[p] = as_period(find_on_or_before(target))

    latest_cost = _to_float(latest.get('total_cost_krw'), 0)
    if latest_cost > 0:
        cost_gain = latest_market - latest_cost
        periods['cost'] = {
            'hasData': True,
            'gainKrw': round(cost_gain, 4),
            'pct': round(cost_gain / latest_cost * 100, 6),
            'baseDate': latest.get('snapshot_date_str'),
        }
    else:
        periods['cost'] = {'hasData': False, 'gainKrw': None, 'pct': None, 'baseDate': latest.get('snapshot_date_str')}

    return jsonify({
        'ok': True,
        'asOfDate': latest.get('snapshot_date_str'),
        'periods': periods,
        'samples': len(rows),
    }), 200


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
