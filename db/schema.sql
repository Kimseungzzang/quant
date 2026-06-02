-- ============================================================
-- Quant Trading System — PostgreSQL Schema
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── 1. 분석 실행 세션 ──────────────────────────────────────────
-- 매번 /analyze 호출 시 1개 row 생성. 추천 종목들이 이 ID를 참조.
CREATE TABLE analysis_runs (
    id          BIGSERIAL PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    market      VARCHAR(10)  NOT NULL CHECK (market IN ('domestic', 'overseas')),
    horizon     VARCHAR(20)  NOT NULL DEFAULT 'swing'
                             CHECK (horizon IN ('long', 'swing', 'daytrade')),
    top_n       INT          NOT NULL DEFAULT 10,
    status      VARCHAR(20)  NOT NULL DEFAULT 'running'
                             CHECK (status IN ('running', 'completed', 'failed')),
    error_msg   TEXT
);

-- ── 2. 분석 결과 (추천 종목) ────────────────────────────────────
CREATE TABLE analysis_results (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT       NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    rank            INT          NOT NULL,
    stock_code      VARCHAR(20)  NOT NULL,
    stock_name      VARCHAR(100) NOT NULL,
    market          VARCHAR(10)  NOT NULL CHECK (market IN ('domestic', 'overseas')),
    horizon         VARCHAR(20)  NOT NULL DEFAULT 'swing'
                                    CHECK (horizon IN ('long', 'swing', 'daytrade')),
    current_price   NUMERIC(18,4) NOT NULL,
    change_pct      NUMERIC(8,4),
    trading_value   NUMERIC(20,4),
    final_score     NUMERIC(8,4),
    -- 백테스트 요약 (분석 시점 스냅샷)
    win_rate_pct    NUMERIC(8,4),
    backtest_return NUMERIC(8,4),
    max_drawdown    NUMERIC(8,4),
    trade_count     INT,
    exchange        VARCHAR(10),                    -- KRX | NAS | NYS 등
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_analysis_results_run_id ON analysis_results(run_id);
CREATE INDEX idx_analysis_results_created_at ON analysis_results(created_at DESC);
CREATE INDEX idx_analysis_results_market_horizon ON analysis_results(market, horizon);

-- ── 3. 백테스트 결과 ────────────────────────────────────────────
-- /backtest 호출 시 저장. analysis_run_id는 장전 분석에서 연동 시 채움.
CREATE TABLE backtest_results (
    id              BIGSERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stock_code      VARCHAR(20)  NOT NULL,
    stock_name      VARCHAR(100) NOT NULL,
    market          VARCHAR(10)  NOT NULL CHECK (market IN ('domestic', 'overseas')),
    period_days     INT          NOT NULL,
    start_date      DATE,
    end_date        DATE,
    initial_capital NUMERIC(18,4),
    final_capital   NUMERIC(18,4),
    total_return_pct NUMERIC(8,4),
    win_rate_pct    NUMERIC(8,4),
    max_drawdown_pct NUMERIC(8,4),
    trade_count     INT,
    avg_hold_days   NUMERIC(8,4),
    sharpe_ratio    NUMERIC(8,4),
    analysis_run_id BIGINT REFERENCES analysis_runs(id)
);

CREATE INDEX idx_backtest_stock_code ON backtest_results(stock_code);
CREATE INDEX idx_backtest_run_at ON backtest_results(run_at DESC);

-- ── 4. 매매 이력 ────────────────────────────────────────────────
CREATE TABLE trades (
    id              BIGSERIAL PRIMARY KEY,
    traded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stock_code      VARCHAR(20)  NOT NULL,
    stock_name      VARCHAR(100) NOT NULL,
    market          VARCHAR(10)  NOT NULL CHECK (market IN ('domestic', 'overseas')),
    side            VARCHAR(4)   NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity        INT          NOT NULL,
    price           NUMERIC(18,4) NOT NULL,
    amount          NUMERIC(18,4) NOT NULL,  -- quantity * price
    currency        VARCHAR(3)    NOT NULL DEFAULT 'KRW',
    commission      NUMERIC(18,4) DEFAULT 0,
    mode            VARCHAR(10)  NOT NULL DEFAULT 'paper' CHECK (mode IN ('paper', 'live', 'mock')),
    strategy        VARCHAR(50),
    reason          TEXT,
    -- SELL 시 손익 (매수 평균가 기준)
    realized_pnl    NUMERIC(18,4),
    pnl_pct         NUMERIC(8,4),
    -- 주문 연결 (BUY → SELL 매칭)
    order_group_id  UUID DEFAULT uuid_generate_v4(),
    kis_order_no    VARCHAR(50)
);

CREATE INDEX idx_trades_traded_at ON trades(traded_at DESC);
CREATE INDEX idx_trades_stock_code ON trades(stock_code);
CREATE INDEX idx_trades_market ON trades(market);
CREATE INDEX idx_trades_mode ON trades(mode);

-- ── 5. 현재 포지션 ──────────────────────────────────────────────
-- 보유 중인 종목만 존재. SELL 완료 시 삭제 또는 quantity=0.
CREATE TABLE positions (
    id              BIGSERIAL PRIMARY KEY,
    stock_code      VARCHAR(20)  NOT NULL,
    stock_name      VARCHAR(100) NOT NULL,
    market          VARCHAR(10)  NOT NULL CHECK (market IN ('domestic', 'overseas')),
    quantity        INT          NOT NULL DEFAULT 0,
    avg_price       NUMERIC(18,4) NOT NULL,
    currency        VARCHAR(3)    NOT NULL DEFAULT 'KRW',
    current_price   NUMERIC(18,4),
    unrealized_pnl  NUMERIC(18,4),
    unrealized_pct  NUMERIC(8,4),
    mode            VARCHAR(10)  NOT NULL DEFAULT 'paper',
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (stock_code, market, mode)
);

-- ── 6. 일별 포트폴리오 스냅샷 (P&L 곡선용) ─────────────────────
CREATE TABLE portfolio_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_date   DATE         NOT NULL,
    mode            VARCHAR(10)  NOT NULL DEFAULT 'paper',
    total_value     NUMERIC(18,4) NOT NULL,  -- 현금 + 평가금
    cash_amount     NUMERIC(18,4) NOT NULL,
    position_value  NUMERIC(18,4) NOT NULL,
    realized_pnl    NUMERIC(18,4) DEFAULT 0, -- 당일 실현 손익
    cumulative_pnl  NUMERIC(18,4) DEFAULT 0, -- 누적 실현 손익
    UNIQUE (snapshot_date, mode)
);

CREATE INDEX idx_snapshots_date ON portfolio_snapshots(snapshot_date DESC);

-- ── 7. 트레이딩 세션 ────────────────────────────────────────────
-- /trade/start, /trade/stop 추적
CREATE TABLE trading_sessions (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stopped_at  TIMESTAMPTZ,
    market      VARCHAR(10)  NOT NULL,
    mode        VARCHAR(10)  NOT NULL DEFAULT 'paper',
    status      VARCHAR(20)  NOT NULL DEFAULT 'running'
                             CHECK (status IN ('running', 'stopped', 'error')),
    config_snapshot JSONB
);

-- ── 8. AI 세션 (아침 브리핑 계획) ────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_sessions (
    id              BIGSERIAL PRIMARY KEY,
    market_outlook  TEXT         NOT NULL,
    watch_stocks    JSONB        NOT NULL DEFAULT '[]',
    strategy        TEXT         NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_sessions_date ON ai_sessions(created_at DESC);

-- ── 9. AI 판단 이력 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_decisions (
    id          BIGSERIAL PRIMARY KEY,
    session_id  BIGINT REFERENCES ai_sessions(id),
    event_kind  VARCHAR(30)  NOT NULL,
    stock_code  VARCHAR(20)  NOT NULL DEFAULT '',
    action      VARCHAR(20)  NOT NULL,
    reason      TEXT         NOT NULL,
    confidence  NUMERIC(4,2) NOT NULL DEFAULT 0,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_decisions_at ON ai_decisions(decided_at DESC);

-- ── 10. AI 메모 ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_memos (
    id          BIGSERIAL PRIMARY KEY,
    content     TEXT         NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_memos_at ON ai_memos(created_at DESC);
