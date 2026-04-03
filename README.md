# portfolio-management

Supabase를 사용해 **포트폴리오 전체/개별 보유 종목의 일별 스냅샷을 누적 저장**하고, 프론트엔드에서 이를 조회할 수 있도록 구성되어 있습니다.

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

create table if not exists public.daily_market_prices (
  id bigint generated always as identity primary key,
  portfolio_key text not null,
  snapshot_date date not null,
  ticker text not null,
  name text not null default '',
  currency text not null default 'USD',
  close_price numeric not null default 0,
  change_value numeric not null default 0,
  change_pct numeric not null default 0,
  is_ok boolean not null default true,
  created_at timestamptz not null default now(),
  unique (portfolio_key, snapshot_date, ticker)
);

create table if not exists public.trade_history (
  id bigint generated always as identity primary key,
  portfolio_key text not null,
  holding_id text not null,
  trade_id text not null,
  ticker text not null default '',
  trade_date date,
  trade_type text not null default '',
  price numeric not null default 0,
  qty numeric not null default 0,
  memo text not null default '',
  created_at timestamptz not null default now(),
  unique (portfolio_key, trade_id)
);

create table if not exists public.portfolio_states (
  id bigint generated always as identity primary key,
  portfolio_key text not null unique,
  state_json jsonb not null,
  updated_at timestamptz not null default now()
);
```

## 3) 동작 방식

- 프론트엔드가 가격 새로고침(`refreshAll`) 완료 후 `/api/portfolio/daily/snapshot` 호출
- 같은 시점에 `/api/portfolio/daily/prices`로 당일 전체 주가 스냅샷 저장
- 거래 저장 시 `/api/portfolio/trades/sync`로 거래내역 누적 저장
- 백엔드가 현재 보유 종목 상태로 KRW 기준 평가액/손익 계산
- Supabase `upsert`로 날짜 단위 누적 저장
- 일별 누적 데이터는 대시보드에 직접 노출하지 않고 서버에 누적 저장(조회 API로 활용)
