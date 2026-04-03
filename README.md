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
```

## 3) 동작 방식

- 프론트엔드의 보유종목/거래내역/관심종목 수정사항은 `/api/portfolio/state`를 통해 Supabase `holdings`, `trades`, `watchlist`에 저장
- 새로고침 시 `/api/portfolio/state`에서 같은 `portfolio_key`의 데이터를 다시 읽어서 렌더링하므로, 어떤 브라우저에서 접속해도 동일한 데이터 표시
- 프론트엔드가 가격 새로고침(`refreshAll`) 완료 후 `/api/portfolio/daily/snapshot` 호출
- 백엔드가 현재 보유 종목 상태로 KRW 기준 평가액/손익 계산
- Supabase `upsert`로 날짜 단위 누적 저장
- 기간별 수익률은 `/api/portfolio/period-returns`에서 Supabase의 포트폴리오 일별 기준가(`total_market_krw`)를 기반으로 계산
- 프론트엔드는 백엔드 DB 결과를 우선 사용하고, 백엔드 장애 시에만 로컬 백업 상태를 제한적으로 사용
- 일별 스냅샷 데이터는 Supabase에 누적 저장되며, 필요 시 별도 화면/리포트에서 조회 가능
