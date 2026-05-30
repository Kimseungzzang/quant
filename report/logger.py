import sqlite3
import logging
from datetime import datetime
from pathlib import Path

from kis.constants import OrderSide, CloseReason

logger = logging.getLogger(__name__)

DB_PATH = Path("data/trades.db")

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code  TEXT NOT NULL,
    name        TEXT,
    exchange    TEXT,
    side        TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    price       REAL NOT NULL,
    order_no    TEXT,
    created_at  TEXT NOT NULL
)
"""

_CREATE_POSITIONS = """
CREATE TABLE IF NOT EXISTS closed_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code    TEXT NOT NULL,
    name          TEXT,
    exchange      TEXT,
    qty           INTEGER,
    entry_price   REAL,
    exit_price    REAL,
    pnl_pct       REAL,
    close_reason  TEXT,
    entry_at      TEXT,
    exit_at       TEXT NOT NULL,
    buy_order_no  TEXT,
    sell_order_no TEXT
)
"""


class TradeLogger:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        cur = self._conn.cursor()
        cur.execute(_CREATE_TRADES)
        cur.execute(_CREATE_POSITIONS)
        self._conn.commit()

    def log_buy(
        self,
        stock_code: str,
        name: str,
        exchange: str,
        qty: int,
        price: float,
        order_no: str = "",
    ):
        self._insert_trade(OrderSide.BUY, stock_code, name, exchange, qty, price, order_no)

    def log_sell(
        self,
        stock_code: str,
        name: str,
        exchange: str,
        qty: int,
        entry_price: float,
        exit_price: float,
        pnl_pct: float,
        reason: CloseReason | str,
        order_no: str = "",
    ):
        self._insert_trade(OrderSide.SELL, stock_code, name, exchange, qty, exit_price, order_no)
        now = datetime.now().isoformat()
        self._conn.execute(
            """INSERT INTO closed_positions
               (stock_code, name, exchange, qty, entry_price, exit_price,
                pnl_pct, close_reason, exit_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (stock_code, name, exchange, qty, entry_price, exit_price,
             pnl_pct, str(reason), now),
        )
        self._conn.commit()

    def get_closed_positions(self, date_from: str | None = None) -> list[dict]:
        query = "SELECT * FROM closed_positions"
        params: list = []
        if date_from:
            query += " WHERE exit_at >= ?"
            params.append(date_from)
        query += " ORDER BY exit_at DESC"
        cur = self._conn.execute(query, params)
        return [dict(row) for row in cur.fetchall()]

    def get_daily_summary(self, date_str: str) -> dict:
        rows = self.get_closed_positions(date_from=f"{date_str}T00:00:00")
        rows = [r for r in rows if r["exit_at"].startswith(date_str)]
        if not rows:
            return {"trades": 0, "wins": 0, "total_pnl": 0.0, "win_rate": 0.0}

        total_pnl = sum(r["pnl_pct"] for r in rows)
        wins = sum(1 for r in rows if r["pnl_pct"] > 0)
        return {
            "trades":    len(rows),
            "wins":      wins,
            "total_pnl": round(total_pnl, 2),
            "win_rate":  round(wins / len(rows) * 100, 1),
            "details":   rows,
        }

    def _insert_trade(
        self,
        side: OrderSide,
        stock_code: str,
        name: str,
        exchange: str,
        qty: int,
        price: float,
        order_no: str,
    ):
        now = datetime.now().isoformat()
        self._conn.execute(
            """INSERT INTO trades (stock_code, name, exchange, side, qty, price, order_no, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (stock_code, name, exchange, str(side), qty, price, order_no, now),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
