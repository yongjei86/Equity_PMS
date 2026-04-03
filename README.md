# portfolio-management

Supabase를 사용해 **포트폴리오 상태와 일별 스냅샷 데이터**를 저장하도록 구성되어 있습니다.

## 1) 환경 변수

`server.py` 실행 전에 아래 환경 변수를 설정하세요.

```bash
export SUPABASE_URL="https://<project-ref>.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="<service-role-key>"
```

> `SUPABASE_SERVICE_ROLE_KEY`는 서버에서만 사용해야 하며, 프론트엔드에 노출하면 안 됩니다.

## 2) Supabase 테이블 생성 SQL

Supabase SQL Editor에서 아래를 실행하세요.

```sql
create table if not exists public.holdings (
  portfolio_key text not null,
  holding_id text not null,
  ticker text not null,
  name text not null default '',
  avg_price numeric not null default 0,
  qty numeric not null default 0,
  currency text not null default 'USD',
  sector text not null default '',
  current numeric not null default 0,
  change numeric not null default 0,
  change_pct numeric not null default 0,
  sort_order int not null default 0,
  updated_at timestamptz not null default now(),
  primary key (portfolio_key, holding_id)
);

create table if not exists public.trades (
  portfolio_key text not null,
  trade_id text not null,
  holding_id text not null,
  trade_date text not null default '',
  side text not null default 'buy',
  price numeric not null default 0,
  qty numeric not null default 0,
  memo text not null default '',
  created_at timestamptz not null default now(),
  primary key (portfolio_key, trade_id)
);

create table if not exists public.watchlist (
  portfolio_key text not null,
  ticker text not null,
  created_at timestamptz not null default now(),
  primary key (portfolio_key, ticker)
);

create table if not exists public.portfolio_daily_snapshots (
  id bigint generated always as identity primary key,
  portfolio_key text not null,
  snapshot_date date not null,
  holdings_count int not null default 0,
  total_cost_krw numeric not null default 0,
  total_market_krw numeric not null default 0,
  total_pl_krw numeric not null default 0,
  total_return_pct numeric not null default 0,
  created_at timestamptz not null default now(),
  unique (portfolio_key, snapshot_date)
);

create table if not exists public.holding_daily_snapshots (
  id bigint generated always as identity primary key,
  portfolio_key text not null,
  snapshot_date date not null,
  holding_id text not null,
  ticker text not null,
  name text not null default '',
  currency text not null default 'USD',
  qty numeric not null default 0,
  avg_price numeric not null default 0,
  current_price numeric not null default 0,
  market_value_krw numeric not null default 0,
  pl_krw numeric not null default 0,
  return_pct numeric not null default 0,
  created_at timestamptz not null default now(),
  unique (portfolio_key, snapshot_date, holding_id, ticker)
);

create table if not exists public.portfolio_settings (
  portfolio_key text primary key,
  settings_json jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);
```

## 3) 동작 방식

- 프론트엔드의 보유종목/거래내역/관심종목 수정사항은 `/api/portfolio/state`를 통해 Supabase `holdings`, `trades`, `watchlist`에 저장
- 새로고침 시 `/api/portfolio/state`에서 같은 `portfolio_key`의 데이터를 다시 읽어서 렌더링하므로, 어떤 브라우저에서 접속해도 동일한 데이터 표시
- 프론트엔드는 `/api/market/prev-close`로 DB(`market_prev_close`)에 적재된 전일 종가만 읽어서 화면에 표시
- 프론트엔드가 가격 새로고침(`refreshAll`) 완료 후 `/api/portfolio/daily/snapshot` 호출
- 백엔드가 현재 보유 종목 상태로 KRW 기준 평가액/손익 계산
- Supabase `upsert`로 날짜 단위 누적 저장
- 기간별 수익률은 `/api/portfolio/period-returns`에서 Supabase의 포트폴리오 일별 기준가(`total_market_krw`)를 기반으로 계산
- 프론트엔드/백엔드 설정(appSettings, 벤치마크 등)도 `portfolio_settings`에 저장
- 일별 스냅샷 데이터는 Supabase에 누적 저장되며, 필요 시 별도 화면/리포트에서 조회 가능

## 5) 기본 Supabase 연결 정보

서버(`server.py`)는 환경변수가 없을 때 아래 기본값으로 접속합니다.

- URL: `https://iewzhfnalpqvlyaehvnq.supabase.co`
- KEY: `sb_publishable_jew0PizzOC9CBB7_APnSzg_lE1i1yrP`

운영 환경에서는 서비스 롤 키를 `SUPABASE_SERVICE_ROLE_KEY`로 별도 주입하는 방식을 권장합니다.

## 4) yfinance 전일 종가 → Supabase 적재 자동화

`supabase-py` + `yfinance`를 사용하는 배치 스크립트가 추가되었습니다.

- 파일: `scripts/update_prev_close.py`
- 기본 소스: `holdings`, `watchlist` 테이블의 `ticker`
- 타겟 테이블: `market_prev_close` (환경 변수로 변경 가능)

### 4-1. 타겟 테이블 생성

```sql
create table if not exists public.market_prev_close (
  ticker text not null,
  market_date date not null,
  prev_close numeric not null default 0,
  close_price numeric not null default 0,
  change numeric not null default 0,
  change_pct numeric not null default 0,
  exchange_group text not null default 'US',
  updated_at timestamptz not null default now(),
  primary key (ticker, market_date)
);
```

### 4-2. 의존성 설치

```bash
pip install yfinance supabase
```

### 4-3. 1회 실행

```bash
export SUPABASE_URL="https://<project-ref>.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="<service-role-key>"
python scripts/update_prev_close.py --mode once
```

### 4-4. 거래소 마감시간 자동 실행

스크립트 자체 daemon 모드(거래소 현지 마감시간 + 기본 15분)를 지원합니다.

```bash
python scripts/update_prev_close.py --mode daemon --delay-minutes 15
```

기본 매핑:
- US: 16:00 (America/New_York)
- KR: 15:30 (Asia/Seoul)
- JP: 15:00 (Asia/Tokyo)
- HK: 16:00 (Asia/Hong_Kong)
- UK: 16:30 (Europe/London)
- EU: 17:30 (Europe/Paris)

### 4-5. cron으로 단순 운영 (대안)

daemon 대신 운영 서버 시간 기준 cron으로 고정 실행도 가능합니다.

```cron
# 예시: 평일 UTC 21:20(미국장 마감 직후 근사치)
20 21 * * 1-5 cd /workspace/Equity_PMS && /usr/bin/python3 scripts/update_prev_close.py --mode once >> /tmp/prev_close.log 2>&1
```

### 4-6. 추가 환경 변수

- `PREV_CLOSE_SOURCE_TABLES`: 조회 대상 테이블(기본 `holdings,watchlist`)
- `PREV_CLOSE_EXTRA_TICKERS`: 강제 포함 ticker CSV
- `PREV_CLOSE_TARGET_TABLE`: 저장 테이블(기본 `market_prev_close`)
- `PREV_CLOSE_DELAY_MINUTES`: daemon 실행 지연(기본 `15`)
