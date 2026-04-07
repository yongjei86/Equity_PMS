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
- 프론트엔드는 `/api/market/prices`로 외부 시세 API(Yahoo/Alpha Vantage)에서 현재가를 조회
- Supabase에는 사용자가 입력한 종목/거래/관심종목/설정 데이터만 저장 (자동 종가 누적 저장 비활성)
- 프론트엔드/백엔드 설정(appSettings, 벤치마크 등)도 `portfolio_settings`에 저장
- 즉, **시장 데이터는 외부 API**, **사용자 입력 데이터(매수가/수량/매수일 등)는 Supabase**에 저장

## 5) Supabase 연결 주의사항

서버(`server.py`)는 반드시 아래 환경 변수가 설정되어야 Supabase에 연결됩니다.

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY` (또는 `SUPABASE_KEY`)

`publishable/anon` 키로는 `holdings`, `trades`, `watchlist` 쓰기/삭제가 RLS 정책에 의해 차단될 수 있으므로, 백엔드에서는 서비스 롤 키 사용을 권장합니다.

최신 `server.py`는 시작 시/요청 시점에 아래 케이스를 자동 검사하고, 잘못된 키면 원인을 포함한 에러를 반환합니다.

- `SUPABASE_URL` 누락
- `SUPABASE_SERVICE_ROLE_KEY`(또는 `SUPABASE_KEY`) 누락
- `sb_publishable_...` 키 사용
- JWT role이 `anon` 또는 `service_role`이 아닌 키 사용

즉, 백엔드 환경 변수는 반드시 **service_role 키**로 설정해야 합니다.

## 4) 종가 자동 적재 기능 상태

현재 기본 동작에서는 Supabase에 종가/일별 스냅샷을 자동 누적 저장하지 않습니다.

- 사용자 입력 기반 데이터(`holdings`, `trades`, `watchlist`, `portfolio_settings`)만 동기화
- 시세 데이터는 화면 계산용으로 외부 API에서 조회
- `scripts/update_prev_close.py`는 레거시 유틸로 남아있지만 기본 워크플로우에서는 사용하지 않음
