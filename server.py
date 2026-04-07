from __future__ import annotations

import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import numpy as np
import requests
import yfinance as yf
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app, allow_headers=["Content-Type", "Authorization", "X-Sb-Url", "X-Sb-Key"])

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_KEY")
    or ""
).strip()

LIVE_PRICE_CACHE_TTL_SECONDS = 20
_LIVE_PRICE_CACHE: Dict[str, Dict[str, Any]] = {}


def _req_supabase_url() -> str:
    return ((request.headers.get("X-Sb-Url") or "").strip().rstrip("/")) or SUPABASE_URL


def _req_supabase_key() -> str:
    return (request.headers.get("X-Sb-Key") or "").strip() or SUPABASE_SERVICE_ROLE_KEY


def _supabase_error() -> Optional[str]:
    url = _req_supabase_url()
    key = _req_supabase_key()
    if not url:
        return "SUPABASE_URL이 설정되지 않았습니다."
    if not key:
        return "SUPABASE_SERVICE_ROLE_KEY가 설정되지 않았습니다."
    lowered = key.lower()
    if lowered.startswith("sb_publishable_"):
        return "publishable 키는 서버 쓰기용이 아닙니다. service_role 키를 사용하세요."
    return None


def _supabase_headers() -> Dict[str, str]:
    key = _req_supabase_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_request(method: str, path: str, *, json_body: Any = None, timeout: int = 12) -> requests.Response:
    url = f"{_req_supabase_url()}/rest/v1/{path.lstrip('/')}"
    return requests.request(method, url, headers=_supabase_headers(), json=json_body, timeout=timeout)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _normalize_ticker(raw: str) -> str:
    tk = (raw or "").strip().upper()
    if not tk:
        return ""
    if tk.isdigit() and len(tk) == 6:
        return f"{tk}.KS"
    if "." in tk and not tk.startswith("^"):
        # Yahoo Finance는 일부 종목(예: BRK.B)을 하이픈 표기(BRK-B)로만 인식한다.
        base, suffix = tk.rsplit(".", 1)
        if suffix in {"A", "B", "C", "D"} and base:
            return f"{base}-{suffix}"
    return tk


def _ticker_candidates(raw_ticker: str) -> List[str]:
    normalized = _normalize_ticker(raw_ticker)
    if not normalized:
        return []

    candidates: List[str] = [normalized]

    if "-" in normalized and not normalized.startswith("^"):
        candidates.append(normalized.replace("-", "."))
    if "." in normalized and not normalized.startswith("^"):
        candidates.append(normalized.replace(".", "-"))

    if normalized.isdigit() and len(normalized) == 6:
        candidates.append(f"{normalized}.KS")

    uniq: List[str] = []
    seen = set()
    for tk in candidates:
        t = (tk or "").strip().upper()
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _to_krw(amount: float, currency: str, fx_rates: Dict[str, float], usd_krw: float) -> float:
    cur = (currency or "USD").upper()
    if cur == "KRW":
        return amount
    denom = _safe_float(fx_rates.get(cur), 1.0)
    if denom == 0:
        denom = 1.0
    return (amount / denom) * usd_krw


def _ticker_name(ticker: str) -> str:
    try:
        info = yf.Ticker(ticker).fast_info or {}
        return str(info.get("shortName") or info.get("longName") or ticker)
    except Exception:
        return ticker


def _fetch_prev_close_from_supabase(ticker: str) -> Optional[Dict[str, Any]]:
    try:
        tk = quote(ticker)
        r = _sb_request(
            "GET",
            f"market_prev_close?select=ticker,prev_close,close_price,change,change_pct,market_date&ticker=eq.{tk}&order=market_date.desc&limit=1",
            timeout=8,
        )
        if not r.ok:
            return None
        rows = r.json() or []
        return rows[0] if rows else None
    except Exception:
        return None


def _fetch_price_single(raw_ticker: str) -> Dict[str, Any]:
    candidates = _ticker_candidates(raw_ticker)
    if not candidates:
        return {"ok": False, "price": 0, "change": 0, "changePct": 0, "name": raw_ticker or "", "currency": "USD"}
    ticker = candidates[0]

    now = time.time()
    cached = _LIVE_PRICE_CACHE.get(ticker)
    if cached and now - cached.get("ts", 0) < LIVE_PRICE_CACHE_TTL_SECONDS:
        return cached["value"]

    result = {"ok": False, "price": 0, "change": 0, "changePct": 0, "name": ticker, "currency": "USD"}

    for candidate in candidates:
        try:
            yf_ticker = yf.Ticker(candidate)
            hist = yf_ticker.history(period="5d", interval="1d", auto_adjust=False)
            info = yf_ticker.fast_info or {}

            currency = str(info.get("currency") or "USD")
            name = str(info.get("shortName") or info.get("longName") or candidate)

            closes = []
            if hist is not None and not hist.empty:
                closes = [float(x) for x in hist["Close"].dropna().tolist() if x is not None]

            if closes:
                price = closes[-1]
                prev = closes[-2] if len(closes) >= 2 else price
                change = price - prev
                change_pct = (change / prev * 100.0) if prev else 0.0
                result = {
                    "ok": True,
                    "price": round(price, 6),
                    "change": round(change, 6),
                    "changePct": round(change_pct, 6),
                    "name": name,
                    "currency": currency,
                }
                ticker = candidate
                break
            sb_prev = _fetch_prev_close_from_supabase(candidate)
            if sb_prev:
                cp = _safe_float(sb_prev.get("close_price"), 0)
                ch = _safe_float(sb_prev.get("change"), 0)
                result = {
                    "ok": cp > 0,
                    "price": cp,
                    "change": ch,
                    "changePct": _safe_float(sb_prev.get("change_pct"), 0),
                    "name": name,
                    "currency": currency,
                }
                ticker = candidate
                if result["ok"]:
                    break
        except Exception:
            continue

    _LIVE_PRICE_CACHE[ticker] = {"ts": now, "value": result}
    return result


@app.get("/api/health")
def health() -> Any:
    err = _supabase_error()
    return jsonify({"ok": err is None, "error": err})


@app.get("/api/search")
def search_ticker() -> Any:
    q = (request.args.get("q") or "").strip().upper()
    if not q:
        return jsonify({"items": []})

    items = []
    try:
        tk = _normalize_ticker(q)
        nm = _ticker_name(tk)
        items.append({"ticker": tk, "name": nm, "exchange": "AUTO"})
    except Exception:
        pass

    if q.isdigit() and len(q) == 6:
        items.append({"ticker": f"{q}.KS", "name": "Korea Stock", "exchange": "KRX"})

    uniq = []
    seen = set()
    for item in items:
        t = item.get("ticker")
        if t and t not in seen:
            seen.add(t)
            uniq.append(item)

    return jsonify({"items": uniq[:8]})


@app.get("/api/market/prices")
def market_prices() -> Any:
    raw = (request.args.get("tickers") or "").strip()
    tickers = [x.strip() for x in raw.split(",") if x.strip()]
    if not tickers:
        return jsonify({"ok": True, "prices": {}})

    prices: Dict[str, Any] = {}
    workers = min(8, max(1, len(tickers)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut_map = {ex.submit(_fetch_price_single, tk): tk for tk in tickers}
        for fut in as_completed(fut_map):
            orig = fut_map[fut]
            try:
                prices[orig] = fut.result()
            except Exception:
                prices[orig] = {"ok": False, "price": 0, "change": 0, "changePct": 0, "name": orig, "currency": "USD"}

    return jsonify({"ok": True, "prices": prices})


@app.get("/api/history/<path:ticker>")
def history_start_price(ticker: str) -> Any:
    range_key = (request.args.get("range") or "1mo").strip()
    tk = _normalize_ticker(ticker)
    try:
        hist = yf.Ticker(tk).history(period=range_key, interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return jsonify({"ok": False, "startPrice": None})
        closes = [float(v) for v in hist["Close"].dropna().tolist() if v is not None]
        start = closes[0] if closes else None
        return jsonify({"ok": start is not None, "startPrice": start})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "startPrice": None}), 500


@app.get("/api/news/<path:ticker>")
def news_for_ticker(ticker: str) -> Any:
    tk = _normalize_ticker(ticker)
    items: List[Dict[str, Any]] = []
    try:
        raw_news = yf.Ticker(tk).news or []
        for n in raw_news[:12]:
            provider_time = n.get("providerPublishTime")
            iso = None
            if provider_time:
                iso = datetime.fromtimestamp(int(provider_time), tz=timezone.utc).isoformat()
            items.append(
                {
                    "title": n.get("title") or "",
                    "titleOrig": n.get("title") or "",
                    "url": n.get("link") or "#",
                    "source": n.get("publisher") or "Yahoo",
                    "publishedAt": iso,
                    "time": "방금 전" if iso else "",
                    "sentiment": "neu",
                }
            )
    except Exception:
        pass
    return jsonify(items)


def _load_state_from_supabase(portfolio_key: str) -> Dict[str, Any]:
    holdings_r = _sb_request(
        "GET",
        f"holdings?select=holding_id,ticker,name,avg_price,qty,currency,sector,current,change,change_pct,sort_order&portfolio_key=eq.{quote(portfolio_key)}&order=sort_order.asc",
    )
    trades_r = _sb_request(
        "GET",
        f"trades?select=trade_id,holding_id,trade_date,side,price,qty,memo,created_at&portfolio_key=eq.{quote(portfolio_key)}&order=created_at.desc",
    )
    watch_r = _sb_request(
        "GET",
        f"watchlist?select=ticker&portfolio_key=eq.{quote(portfolio_key)}&order=created_at.asc",
    )
    settings_r = _sb_request(
        "GET",
        f"portfolio_settings?select=settings_json&portfolio_key=eq.{quote(portfolio_key)}&limit=1",
    )

    if not (holdings_r.ok and trades_r.ok and watch_r.ok and settings_r.ok):
        raise RuntimeError("Supabase state 조회 실패")

    holdings_rows = holdings_r.json() or []
    holdings = [
        {
            "id": row.get("holding_id"),
            "ticker": row.get("ticker") or "",
            "name": row.get("name") or row.get("ticker") or "",
            "avgPrice": _safe_float(row.get("avg_price"), 0),
            "qty": _safe_float(row.get("qty"), 0),
            "currency": row.get("currency") or "USD",
            "sector": row.get("sector") or "",
            "current": _safe_float(row.get("current"), 0),
            "change": _safe_float(row.get("change"), 0),
            "changePct": _safe_float(row.get("change_pct"), 0),
        }
        for row in holdings_rows
    ]

    trades_rows = trades_r.json() or []
    trades_map: Dict[str, List[Dict[str, Any]]] = {}
    for row in trades_rows:
        hid = row.get("holding_id")
        if hid is None:
            continue
        trades_map.setdefault(str(hid), []).append(
            {
                "id": row.get("trade_id"),
                "date": row.get("trade_date") or "",
                "type": row.get("side") or "buy",
                "price": _safe_float(row.get("price"), 0),
                "qty": _safe_float(row.get("qty"), 0),
                "memo": row.get("memo") or "",
            }
        )

    watch_rows = watch_r.json() or []
    watchlist = [row.get("ticker") for row in watch_rows if row.get("ticker")]

    settings_rows = settings_r.json() or []
    app_settings = settings_rows[0].get("settings_json") if settings_rows else {}
    if not isinstance(app_settings, dict):
        app_settings = {}

    return {
        "holdings": holdings,
        "trades": trades_map,
        "watchlist": watchlist,
        "appSettings": app_settings,
    }


def _replace_table_rows(portfolio_key: str, table: str, rows: List[Dict[str, Any]], pk_fields: str) -> None:
    _sb_request("DELETE", f"{table}?portfolio_key=eq.{quote(portfolio_key)}")
    if rows:
        _sb_request("POST", f"{table}?on_conflict={pk_fields}", json_body=rows)


@app.get("/api/portfolio/state")
def get_portfolio_state() -> Any:
    err = _supabase_error()
    if err:
        return jsonify({"ok": False, "error": err}), 400

    portfolio_key = (request.args.get("key") or "default").strip() or "default"
    try:
        state = _load_state_from_supabase(portfolio_key)
        return jsonify({"ok": True, "state": state})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "state": None}), 500


@app.post("/api/portfolio/state")
def post_portfolio_state() -> Any:
    err = _supabase_error()
    if err:
        return jsonify({"ok": False, "error": err}), 400

    payload = request.get_json(silent=True) or {}
    portfolio_key = (payload.get("key") or "default").strip() or "default"
    state = payload.get("state") or {}

    holdings = state.get("holdings") if isinstance(state.get("holdings"), list) else []
    trades_map = state.get("trades") if isinstance(state.get("trades"), dict) else {}
    watchlist = state.get("watchlist") if isinstance(state.get("watchlist"), list) else []
    app_settings = state.get("appSettings") if isinstance(state.get("appSettings"), dict) else {}

    holding_rows = []
    for idx, h in enumerate(holdings):
        holding_rows.append(
            {
                "portfolio_key": portfolio_key,
                "holding_id": str(h.get("id")),
                "ticker": _normalize_ticker(h.get("ticker") or ""),
                "name": h.get("name") or h.get("ticker") or "",
                "avg_price": _safe_float(h.get("avgPrice"), 0),
                "qty": _safe_float(h.get("qty"), 0),
                "currency": (h.get("currency") or "USD").upper(),
                "sector": h.get("sector") or "",
                "current": _safe_float(h.get("current"), 0),
                "change": _safe_float(h.get("change"), 0),
                "change_pct": _safe_float(h.get("changePct"), 0),
                "sort_order": idx,
                "updated_at": datetime.utcnow().isoformat(),
            }
        )

    trade_rows = []
    for holding_id, items in trades_map.items():
        if not isinstance(items, list):
            continue
        for t in items:
            trade_rows.append(
                {
                    "portfolio_key": portfolio_key,
                    "trade_id": str(t.get("id")),
                    "holding_id": str(holding_id),
                    "trade_date": t.get("date") or "",
                    "side": t.get("type") or "buy",
                    "price": _safe_float(t.get("price"), 0),
                    "qty": _safe_float(t.get("qty"), 0),
                    "memo": t.get("memo") or "",
                    "created_at": datetime.utcnow().isoformat(),
                }
            )

    watch_rows = [
        {
            "portfolio_key": portfolio_key,
            "ticker": _normalize_ticker(t),
            "created_at": datetime.utcnow().isoformat(),
        }
        for t in watchlist
        if t
    ]

    try:
        _replace_table_rows(portfolio_key, "holdings", holding_rows, "portfolio_key,holding_id")
        _replace_table_rows(portfolio_key, "trades", trade_rows, "portfolio_key,trade_id")
        _replace_table_rows(portfolio_key, "watchlist", watch_rows, "portfolio_key,ticker")
        _sb_request(
            "POST",
            "portfolio_settings?on_conflict=portfolio_key",
            json_body=[
                {
                    "portfolio_key": portfolio_key,
                    "settings_json": app_settings,
                    "updated_at": datetime.utcnow().isoformat(),
                }
            ],
        )
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/portfolio/daily/snapshot")
def save_daily_snapshot() -> Any:
    err = _supabase_error()
    if err:
        return jsonify({"ok": False, "error": err}), 400

    payload = request.get_json(silent=True) or {}
    portfolio_key = (payload.get("key") or "default").strip() or "default"
    state = payload.get("state") or {}
    fx_rates = payload.get("fxRates") if isinstance(payload.get("fxRates"), dict) else {"USD": 1}
    usd_krw = _safe_float(payload.get("usdKrw"), 1360)
    snapshot_date = (payload.get("snapshotDate") or date.today().isoformat()).strip()

    holdings = state.get("holdings") if isinstance(state.get("holdings"), list) else []

    total_cost = 0.0
    total_market = 0.0
    holding_rows = []

    for h in holdings:
        qty = _safe_float(h.get("qty"), 0)
        avg = _safe_float(h.get("avgPrice"), 0)
        cur = _safe_float(h.get("current"), avg)
        ccy = (h.get("currency") or "USD").upper()

        cost_krw = _to_krw(avg * qty, ccy, fx_rates, usd_krw)
        mkt_krw = _to_krw(cur * qty, ccy, fx_rates, usd_krw)
        pl_krw = mkt_krw - cost_krw
        pct = (pl_krw / cost_krw * 100.0) if cost_krw else 0.0

        total_cost += cost_krw
        total_market += mkt_krw

        holding_rows.append(
            {
                "portfolio_key": portfolio_key,
                "snapshot_date": snapshot_date,
                "holding_id": str(h.get("id")),
                "ticker": _normalize_ticker(h.get("ticker") or ""),
                "name": h.get("name") or h.get("ticker") or "",
                "currency": ccy,
                "qty": qty,
                "avg_price": avg,
                "current_price": cur,
                "market_value_krw": mkt_krw,
                "pl_krw": pl_krw,
                "return_pct": pct,
                "created_at": datetime.utcnow().isoformat(),
            }
        )

    total_pl = total_market - total_cost
    total_ret = (total_pl / total_cost * 100.0) if total_cost else 0.0

    try:
        _sb_request(
            "POST",
            "portfolio_daily_snapshots?on_conflict=portfolio_key,snapshot_date",
            json_body=[
                {
                    "portfolio_key": portfolio_key,
                    "snapshot_date": snapshot_date,
                    "holdings_count": len(holdings),
                    "total_cost_krw": total_cost,
                    "total_market_krw": total_market,
                    "total_pl_krw": total_pl,
                    "total_return_pct": total_ret,
                    "created_at": datetime.utcnow().isoformat(),
                }
            ],
        )
        _sb_request("DELETE", f"holding_daily_snapshots?portfolio_key=eq.{quote(portfolio_key)}&snapshot_date=eq.{quote(snapshot_date)}")
        if holding_rows:
            _sb_request(
                "POST",
                "holding_daily_snapshots?on_conflict=portfolio_key,snapshot_date,holding_id,ticker",
                json_body=holding_rows,
            )
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/portfolio/period-returns")
def portfolio_period_returns() -> Any:
    err = _supabase_error()
    if err:
        return jsonify({"ok": False, "error": err}), 400

    portfolio_key = (request.args.get("key") or "default").strip() or "default"
    try:
        r = _sb_request(
            "GET",
            f"portfolio_daily_snapshots?select=snapshot_date,total_market_krw&portfolio_key=eq.{quote(portfolio_key)}&order=snapshot_date.asc",
        )
        rows = r.json() if r.ok else []
        if not rows:
            return jsonify({"ok": True, "periods": {}})

        data = []
        for row in rows:
            d = row.get("snapshot_date")
            if not d:
                continue
            data.append((datetime.fromisoformat(d).date(), _safe_float(row.get("total_market_krw"), 0)))

        if not data:
            return jsonify({"ok": True, "periods": {}})

        cur_date, cur_val = data[-1]

        def at_or_before(target: date) -> Optional[float]:
            chosen = None
            for d, v in data:
                if d <= target:
                    chosen = v
                else:
                    break
            return chosen

        targets = {
            "1d": cur_date - timedelta(days=1),
            "1w": cur_date - timedelta(days=7),
            "1m": cur_date - timedelta(days=30),
            "3m": cur_date - timedelta(days=90),
            "ytd": date(cur_date.year, 1, 1),
        }

        periods: Dict[str, Dict[str, Any]] = {}
        for k, t in targets.items():
            base = at_or_before(t)
            if base and base > 0:
                gain = cur_val - base
                pct = gain / base * 100.0
                periods[k] = {"hasData": True, "gainKrw": gain, "pct": pct}
            else:
                periods[k] = {"hasData": False, "gainKrw": 0, "pct": None}

        return jsonify({"ok": True, "periods": periods})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/portfolio/daily/holdings")
def portfolio_daily_holdings() -> Any:
    err = _supabase_error()
    if err:
        return jsonify({"ok": False, "error": err}), 400

    portfolio_key = (request.args.get("key") or "default").strip() or "default"
    ticker = _normalize_ticker(request.args.get("ticker") or "")
    days = max(7, min(2000, int(request.args.get("days") or 365)))
    since = (date.today() - timedelta(days=days)).isoformat()

    try:
        r = _sb_request(
            "GET",
            f"holding_daily_snapshots?select=snapshot_date,return_pct,market_value_krw,pl_krw,ticker&portfolio_key=eq.{quote(portfolio_key)}&ticker=eq.{quote(ticker)}&snapshot_date=gte.{since}&order=snapshot_date.asc",
        )
        items = r.json() if r.ok else []
        return jsonify({"ok": True, "items": items})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "items": []}), 500


def _annualized_stats(returns: np.ndarray) -> Dict[str, float]:
    if returns.size == 0:
        return {"mean": 0.0, "vol": 0.0}
    mean_daily = float(np.mean(returns))
    vol_daily = float(np.std(returns, ddof=1)) if returns.size > 1 else 0.0
    return {"mean": mean_daily * 252.0, "vol": vol_daily * math.sqrt(252.0)}


@app.post("/api/portfolio/metrics")
def portfolio_metrics() -> Any:
    payload = request.get_json(silent=True) or {}
    holdings = payload.get("holdings") if isinstance(payload.get("holdings"), list) else []
    benchmark = _normalize_ticker(payload.get("benchmark") or "^GSPC")

    if not holdings:
        return jsonify({"ok": True, "metrics": {}})

    returns = []
    for h in holdings:
        avg = _safe_float(h.get("avgPrice"), 0)
        cur = _safe_float(h.get("current"), avg)
        if avg > 0:
            returns.append((cur - avg) / avg)

    if not returns:
        return jsonify({"ok": True, "metrics": {}})

    port_returns = np.array(returns, dtype=float)
    stats = _annualized_stats(port_returns)
    rf = 0.03
    excess = stats["mean"] - rf
    sharpe = excess / stats["vol"] if stats["vol"] > 0 else 0.0

    # 간단 benchmark 추정
    bench_ret = 0.0
    try:
        hist = yf.Ticker(benchmark).history(period="6mo", interval="1d", auto_adjust=False)
        closes = hist["Close"].dropna().tolist() if hist is not None and not hist.empty else []
        if len(closes) >= 2 and closes[0] > 0:
            bench_ret = (float(closes[-1]) - float(closes[0])) / float(closes[0])
    except Exception:
        pass

    var95 = float(np.percentile(port_returns, 5)) if port_returns.size else 0.0
    max_dd = float(np.min(port_returns)) if port_returns.size else 0.0

    metrics = {
        "sharpeRatio": float(sharpe),
        "sortinoRatio": float(sharpe),
        "maxDrawdown": float(max_dd),
        "beta": 1.0,
        "alpha": float(np.mean(port_returns) - bench_ret),
        "volatility": float(stats["vol"]),
        "informationRatio": float((np.mean(port_returns) - bench_ret) / (np.std(port_returns) + 1e-9)),
        "correlationWithBenchmark": 0.0,
        "var95Daily": float(var95),
    }
    return jsonify({"ok": True, "metrics": metrics})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
