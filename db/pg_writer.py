"""
PostgreSQL 쓰기/읽기 헬퍼.
asyncpg 기반 — FastAPI의 async 컨텍스트에서 직접 사용.
"""
import os
from datetime import datetime
from typing import Optional

import asyncpg

_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://kimseungzzang@localhost/quant_trading",
)


async def _conn() -> asyncpg.Connection:
    return await asyncpg.connect(_DSN)


class PGWriter:
    async def ensure_analysis_horizon_columns(self):
        conn = await _conn()
        try:
            await conn.execute(
                "ALTER TABLE analysis_runs "
                "ADD COLUMN IF NOT EXISTS horizon VARCHAR(20) NOT NULL DEFAULT 'swing'"
            )
            await conn.execute(
                "ALTER TABLE analysis_results "
                "ADD COLUMN IF NOT EXISTS horizon VARCHAR(20) NOT NULL DEFAULT 'swing'"
            )
            await conn.execute(
                "ALTER TABLE analysis_results "
                "ADD COLUMN IF NOT EXISTS trading_value NUMERIC(20,4)"
            )
        finally:
            await conn.close()

    # ── analysis_runs ──────────────────────────────────────────────

    async def has_running_analysis(self, market: str, horizon: str = "swing") -> bool:
        await self.ensure_analysis_horizon_columns()
        conn = await _conn()
        try:
            row = await conn.fetchrow(
                "SELECT id FROM analysis_runs "
                "WHERE market=$1 AND horizon=$2 AND status='running' LIMIT 1",
                market, horizon,
            )
            return row is not None
        finally:
            await conn.close()

    async def create_analysis_run(self, market: str, top_n: int, horizon: str = "swing") -> int:
        await self.ensure_analysis_horizon_columns()
        conn = await _conn()
        try:
            row = await conn.fetchrow(
                "INSERT INTO analysis_runs (market, horizon, top_n, status) "
                "VALUES ($1, $2, $3, 'running') RETURNING id",
                market, horizon, top_n,
            )
            return row["id"]
        finally:
            await conn.close()

    async def complete_analysis_run(
        self, run_id: int, status: str, error_msg: Optional[str] = None
    ):
        conn = await _conn()
        try:
            await conn.execute(
                "UPDATE analysis_runs SET status=$1, error_msg=$2 WHERE id=$3",
                status, error_msg, run_id,
            )
        finally:
            await conn.close()

    async def get_analysis_run_status(self, run_id: int) -> dict | None:
        conn = await _conn()
        try:
            row = await conn.fetchrow(
                """
                SELECT r.id, r.status, r.error_msg, r.top_n, COUNT(ar.id) AS result_count
                FROM analysis_runs r
                LEFT JOIN analysis_results ar ON ar.run_id = r.id
                WHERE r.id=$1
                GROUP BY r.id
                """,
                run_id,
            )
            return dict(row) if row else None
        finally:
            await conn.close()

    async def save_analysis_results(self, run_id: int, candidates: list):
        """Candidate 리스트를 analysis_results 테이블에 저장."""
        if not candidates:
            return
        await self.ensure_analysis_horizon_columns()
        conn = await _conn()
        try:
            rows = [
                (
                    run_id,
                    rank,
                    c.stock_code,
                    c.name,
                    _market_label(c.exchange),
                    c.horizon,
                    float(c.current_price),
                    float(c.change_pct),
                    float(getattr(c, "trading_value", 0.0) or 0.0),
                    float(c.final_score),
                    float(c.backtest.win_rate_pct),
                    float(c.backtest.total_return_pct),
                    float(c.backtest.max_drawdown_pct),
                    int(c.backtest.total_trades),
                    c.exchange,
                )
                for rank, c in enumerate(candidates, 1)
            ]
            await conn.executemany(
                """
                INSERT INTO analysis_results
	                  (run_id, rank, stock_code, stock_name, market,
	                   horizon,
	                   current_price, change_pct, trading_value, final_score,
	                   win_rate_pct, backtest_return, max_drawdown, trade_count, exchange)
	                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                """,
                rows,
            )
        finally:
            await conn.close()

    # ── backtest_results ───────────────────────────────────────────

    async def save_backtest_result(self, result, market: str) -> int:
        """BacktestResult dataclass를 DB에 저장."""
        conn = await _conn()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO backtest_results
                  (stock_code, stock_name, market, period_days,
                   total_return_pct, win_rate_pct, max_drawdown_pct,
                   trade_count, sharpe_ratio)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                RETURNING id
                """,
                result.get("stock_code", ""),
                result.get("stock_name", result.get("stock_code", "")),
                market,
                result.get("period_days", 60),
                result.get("total_return_pct", 0.0),
                result.get("win_rate_pct", 0.0),
                result.get("max_drawdown_pct", 0.0),
                result.get("total_trades", 0),
                result.get("sharpe_ratio", 0.0),
            )
            return row["id"]
        finally:
            await conn.close()

    # ── trades ─────────────────────────────────────────────────────

    async def save_trade(self, trade: dict):
        conn = await _conn()
        try:
            await conn.execute(
                """
                INSERT INTO trades
                  (stock_code, stock_name, market, side, quantity,
                   price, amount, commission, mode, strategy, reason,
                   realized_pnl, pnl_pct, kis_order_no)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                trade["stock_code"],
                trade.get("stock_name", trade["stock_code"]),
                trade.get("market", "domestic"),
                trade["side"],
                trade["quantity"],
                trade["price"],
                trade["quantity"] * trade["price"],
                trade.get("commission", 0),
                trade.get("mode", "paper"),
                trade.get("strategy"),
                trade.get("reason"),
                trade.get("realized_pnl"),
                trade.get("pnl_pct"),
                trade.get("kis_order_no"),
            )
        finally:
            await conn.close()

    # ── positions ──────────────────────────────────────────────────

    async def get_positions(self) -> list[dict]:
        conn = await _conn()
        try:
            rows = await conn.fetch(
                "SELECT * FROM positions WHERE quantity > 0 ORDER BY opened_at DESC"
            )
            return [dict(r) for r in rows]
        finally:
            await conn.close()

    async def upsert_position(self, pos: dict):
        conn = await _conn()
        try:
            await conn.execute(
                """
                INSERT INTO positions
                  (stock_code, stock_name, market, quantity, avg_price,
                   current_price, unrealized_pnl, unrealized_pct, mode)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (stock_code, market, mode) DO UPDATE SET
                  quantity       = EXCLUDED.quantity,
                  avg_price      = EXCLUDED.avg_price,
                  current_price  = EXCLUDED.current_price,
                  unrealized_pnl = EXCLUDED.unrealized_pnl,
                  unrealized_pct = EXCLUDED.unrealized_pct,
                  updated_at     = NOW()
                """,
                pos["stock_code"],
                pos.get("stock_name", pos["stock_code"]),
                pos.get("market", "domestic"),
                pos["quantity"],
                pos["avg_price"],
                pos.get("current_price"),
                pos.get("unrealized_pnl"),
                pos.get("unrealized_pct"),
                pos.get("mode", "paper"),
            )
        finally:
            await conn.close()


def _market_label(exchange: str) -> str:
    """exchange 코드 → 'domestic' / 'overseas'"""
    return "domestic" if exchange in ("KRX", "KOSPI", "KOSDAQ") else "overseas"


# ── 동기 버전 (OrderManager 등 sync 컨텍스트에서 사용) ─────────────────

import psycopg2
import psycopg2.extras

_SYNC_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://kimseungzzang@localhost/quant_trading",
)


class PGWriterSync:
    """psycopg2 기반 동기 DB 쓰기 — trading 루프(sync 스레드)에서 사용."""

    def _conn(self):
        return psycopg2.connect(_SYNC_DSN)

    def save_buy(self, stock_code: str, stock_name: str, exchange: str,
                 qty: int, price: float, order_no: str = "", mode: str = "live"):
        market = _market_label(exchange)
        amount = qty * price
        try:
            conn = self._conn()
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trades
                      (stock_code, stock_name, market, side, quantity,
                       price, amount, mode, kis_order_no)
                    VALUES (%s,%s,%s,'BUY',%s,%s,%s,%s,%s)
                    """,
                    (stock_code, stock_name, market, qty, price, amount, mode, order_no or None),
                )
            # 포지션 upsert
            conn2 = self._conn()
            with conn2, conn2.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO positions
                      (stock_code, stock_name, market, quantity, avg_price, mode)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (stock_code, market, mode) DO UPDATE SET
                      quantity  = EXCLUDED.quantity,
                      avg_price = EXCLUDED.avg_price,
                      updated_at = NOW()
                    """,
                    (stock_code, stock_name, market, qty, price, mode),
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("PGWriterSync.save_buy 실패: %s", e)

    def save_sell(self, stock_code: str, stock_name: str, exchange: str,
                  qty: int, entry_price: float, exit_price: float,
                  pnl_pct: float, order_no: str = "", mode: str = "live"):
        market = _market_label(exchange)
        amount = qty * exit_price
        realized_pnl = qty * (exit_price - entry_price)
        try:
            conn = self._conn()
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trades
                      (stock_code, stock_name, market, side, quantity,
                       price, amount, mode, realized_pnl, pnl_pct, kis_order_no)
                    VALUES (%s,%s,%s,'SELL',%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (stock_code, stock_name, market, qty, exit_price, amount,
                     mode, realized_pnl, pnl_pct, order_no or None),
                )
            # 포지션 제거
            conn2 = self._conn()
            with conn2, conn2.cursor() as cur:
                cur.execute(
                    "DELETE FROM positions WHERE stock_code=%s AND market=%s AND mode=%s",
                    (stock_code, market, mode),
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error("PGWriterSync.save_sell 실패: %s", e)
