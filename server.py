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
CORS(app, allow_headers=['Content-Type', 'Authorization', 'X-Sb-Url', 'X-Sb-Key'])
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



def _req_supabase_url():
    """요청 헤더(X-Sb-Url)에서 Supabase URL을 읽고, 없으면 환경변수로 폴백."""
    try:
        from flask import request as _r
        v = (_r.headers.get('X-Sb-Url') or '').strip().rstrip('/')
        if v:
            return v
    except RuntimeError:
        pass
    return SUPABASE_URL


def _req_supabase_key():
    """요청 헤더(X-Sb-Key)에서 Supabase 키를 읽고, 없으면 환경변수로 폴백."""
    try:
        from flask import request as _r
        v = (_r.headers.get('X-Sb-Key') or '').strip()
        if v:
            return v
    except RuntimeError:
        pass
    return SUPABASE_SERVICE_ROLE_KEY


def _decode_jwt_payload(token):
    token = (token or '').strip()
    parts = token.split('.')
    if len(parts) != 3:
        return None
    try:
        import base64
        padded = parts[1] + '=' * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(padded.encode('utf-8')).decode('utf-8')
        import json
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _supabase_config_error():
    url = _req_supabase_url()
    if not url:
        return 'Supabase URL이 설정되지 않았습니다. 설정 화면에서 URL을 입력하거나 SUPABASE_URL 환경변수를 설정하세요.'
    key = (_req_supabase_key() or '').strip()
    if not key:
        return 'Supabase API 키가 설정되지 않았습니다. 설정 화면에서 service_role 키를 입력하거나 SUPABASE_SERVICE_ROLE_KEY 환경변수를 설정하세요.'

    lowered = key.lower()
    if lowered.startswith('sb_publishable_'):
        return 'Supabase publishable 키는 쓰기/삭제에 사용할 수 없습니다. Supabase 대시보드 > Settings > API에서 service_role 키를 사용하세요.'

    payload = _decode_jwt_payload(key)
    if payload is not None:
        role = str(payload.get('role') or '').strip().lower()
        if role == 'anon':
            return 'Supabase anon 키는 RLS로 인해 데이터 쓰기·삭제가 차단됩니다. Supabase 대시보드 > Settings > API에서 service_role 키를 사용하세요.'
        if role and role != 'service_role':
            return f'Supabase 키의 role이 {role!r}입니다. service_role 키를 사용해야 합니다.'

    return None


_initial_supabase_config_error = _supabase_config_error()
if _initial_supabase_config_error:
    print(f'[WARN] {_initial_supabase_config_error}')


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

