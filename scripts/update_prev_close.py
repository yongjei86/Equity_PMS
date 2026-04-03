#!/usr/bin/env python3
"""거래소 마감 시간 기준으로 전일 종가를 Supabase에 적재하는 스크립트.

기본 동작
- `--mode once`: 즉시 1회 실행
- `--mode daemon`: 거래소별 마감 시간(현지 기준) 이후 자동 실행

필수 환경 변수
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY (또는 SUPABASE_KEY)

선택 환경 변수
- PREV_CLOSE_SOURCE_TABLES (기본: holdings,watchlist)
- PREV_CLOSE_EXTRA_TICKERS (예: AAPL,MSFT,005930.KS)
- PREV_CLOSE_TARGET_TABLE (기본: market_prev_close)
- PREV_CLOSE_DELAY_MINUTES (기본: 15)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set

import yfinance as yf
from supabase import Client, create_client
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ExchangeSchedule:
    name: str
    timezone: str
    close_hour: int
    close_minute: int


EXCHANGE_SCHEDULES: Dict[str, ExchangeSchedule] = {
    "US": ExchangeSchedule("US", "America/New_York", 16, 0),
    "KR": ExchangeSchedule("KR", "Asia/Seoul", 15, 30),
    "JP": ExchangeSchedule("JP", "Asia/Tokyo", 15, 0),
    "HK": ExchangeSchedule("HK", "Asia/Hong_Kong", 16, 0),
    "UK": ExchangeSchedule("UK", "Europe/London", 16, 30),
    "EU": ExchangeSchedule("EU", "Europe/Paris", 17, 30),
}


def infer_exchange_group(ticker: str) -> str:
    tk = (ticker or "").upper().strip()
    if not tk:
        return "US"
    if tk.endswith((".KS", ".KQ")):
        return "KR"
    if tk.endswith(".T"):
        return "JP"
    if tk.endswith(".HK"):
        return "HK"
    if tk.endswith(".L"):
        return "UK"
    if tk.endswith((".PA", ".DE", ".MI")):
        return "EU"
    return "US"


def build_supabase_client() -> Client:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 환경 변수가 필요합니다.")
    return create_client(url, key)


def _safe_select_table(client: Client, table: str, columns: str) -> List[dict]:
    try:
        resp = client.table(table).select(columns).execute()
        return list(resp.data or [])
    except Exception:
        return []


def load_tickers(client: Client) -> List[str]:
    source_tables = [
        t.strip() for t in (os.getenv("PREV_CLOSE_SOURCE_TABLES") or "holdings,watchlist").split(",") if t.strip()
    ]

    tickers: Set[str] = set()
    for table in source_tables:
        for row in _safe_select_table(client, table, "ticker"):
            ticker = (row.get("ticker") or "").strip().upper()
            if ticker:
                tickers.add(ticker)

    extra = (os.getenv("PREV_CLOSE_EXTRA_TICKERS") or "").strip()
    if extra:
        for tk in extra.split(","):
            tk = tk.strip().upper()
            if tk:
                tickers.add(tk)

    return sorted(tickers)


def fetch_prev_close(ticker: str) -> Optional[dict]:
    try:
        history = yf.Ticker(ticker).history(period="10d", interval="1d", auto_adjust=False)
        if history is None or history.empty or len(history) < 2:
            return None

        valid = history.dropna(subset=["Close"])
        if valid.empty:
            return None

        last = valid.iloc[-1]
        prev = valid.iloc[-2] if len(valid) >= 2 else last

        market_date = valid.index[-1].date().isoformat()
        prev_close = float(prev["Close"])
        close_price = float(last["Close"])
        change = close_price - prev_close
        change_pct = (change / prev_close * 100.0) if prev_close else 0.0

        return {
            "ticker": ticker,
            "market_date": market_date,
            "prev_close": round(prev_close, 8),
            "close_price": round(close_price, 8),
            "change": round(change, 8),
            "change_pct": round(change_pct, 6),
            "exchange_group": infer_exchange_group(ticker),
            "updated_at": datetime.utcnow().isoformat(),
        }
    except Exception:
        return None


def upsert_prev_close_rows(client: Client, rows: Iterable[dict]) -> int:
    row_list = [r for r in rows if r]
    if not row_list:
        return 0

    target_table = (os.getenv("PREV_CLOSE_TARGET_TABLE") or "market_prev_close").strip()
    client.table(target_table).upsert(row_list, on_conflict="ticker,market_date").execute()
    return len(row_list)


def run_once(client: Client) -> int:
    tickers = load_tickers(client)
    if not tickers:
        print("[INFO] 대상 ticker가 없습니다. holdings/watchlist 또는 PREV_CLOSE_EXTRA_TICKERS를 확인하세요.")
        return 0

    rows: List[dict] = []
    for ticker in tickers:
        row = fetch_prev_close(ticker)
        if row:
            rows.append(row)
        else:
            print(f"[WARN] 전일 종가 조회 실패: {ticker}")

    saved = upsert_prev_close_rows(client, rows)
    print(f"[INFO] 저장 완료: {saved}건")
    return saved


def should_run_now(schedule: ExchangeSchedule, delay_minutes: int) -> bool:
    now_local = datetime.now(ZoneInfo(schedule.timezone))
    if now_local.weekday() >= 5:
        return False

    target = now_local.replace(
        hour=schedule.close_hour,
        minute=schedule.close_minute,
        second=0,
        microsecond=0,
    ) + timedelta(minutes=delay_minutes)

    delta = abs((now_local - target).total_seconds())
    return delta <= 90


def run_daemon(client: Client, delay_minutes: int, loop_sleep: int) -> None:
    print("[INFO] daemon 시작: 거래소 마감 시간 기반 자동 갱신")
    last_run_by_exchange: Dict[str, str] = {}

    while True:
        for name, schedule in EXCHANGE_SCHEDULES.items():
            now_local = datetime.now(ZoneInfo(schedule.timezone))
            local_day = now_local.date().isoformat()
            if last_run_by_exchange.get(name) == local_day:
                continue

            if should_run_now(schedule, delay_minutes):
                print(f"[INFO] {name}({schedule.timezone}) 마감 후 배치 실행")
                run_once(client)
                last_run_by_exchange[name] = local_day

        time.sleep(loop_sleep)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="yfinance 전일 종가 수집 후 Supabase 업서트")
    parser.add_argument("--mode", choices=["once", "daemon"], default="once")
    parser.add_argument("--delay-minutes", type=int, default=int(os.getenv("PREV_CLOSE_DELAY_MINUTES", "15")))
    parser.add_argument("--loop-sleep", type=int, default=60, help="daemon 모드 루프 sleep(초)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        client = build_supabase_client()
    except Exception as exc:
        print(f"[ERROR] Supabase 클라이언트 생성 실패: {exc}")
        return 1

    if args.mode == "once":
        run_once(client)
        return 0

    run_daemon(client, delay_minutes=args.delay_minutes, loop_sleep=args.loop_sleep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
