import json
import logging
from datetime import datetime

import asyncpg

logger = logging.getLogger(__name__)


class AgentMemory:
    """AI의 현재 계획, 판단 이력, thesis를 PostgreSQL에 저장/조회."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def save_plan(self, market_outlook: str, watch_stocks: list[dict], strategy: str) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ai_sessions (market_outlook, watch_stocks, strategy, created_at)
                VALUES ($1, $2, $3, NOW())
                RETURNING id
                """,
                market_outlook,
                json.dumps(watch_stocks, ensure_ascii=False),
                strategy,
            )
            return row["id"]

    async def save_decision(
        self,
        session_id: int | None,
        event_kind: str,
        stock_code: str,
        action: str,
        reason: str,
        confidence: float,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_decisions
                    (session_id, event_kind, stock_code, action, reason, confidence, decided_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                """,
                session_id, event_kind, stock_code, action, reason, confidence,
            )

    async def get_today_plan(self) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM ai_sessions
                WHERE created_at::date = CURRENT_DATE
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            if not row:
                return None
            return {
                "id": row["id"],
                "market_outlook": row["market_outlook"],
                "watch_stocks": json.loads(row["watch_stocks"] or "[]"),
                "strategy": row["strategy"],
                "created_at": row["created_at"].isoformat(),
            }

    async def get_recent_decisions(self, limit: int = 10) -> list[dict]:
        return await self.get_decisions(limit=limit)

    async def get_decisions(
        self,
        stock_code: str | None = None,
        limit: int = 20,
        action_filter: str | None = None,
    ) -> list[dict]:
        async with self._pool.acquire() as conn:
            conditions = []
            params: list = []
            if stock_code:
                params.append(stock_code)
                conditions.append(f"stock_code = ${len(params)}")
            if action_filter:
                params.append(action_filter.upper())
                conditions.append(f"UPPER(action) = ${len(params)}")
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            params.append(limit)
            rows = await conn.fetch(
                f"SELECT * FROM ai_decisions {where} ORDER BY decided_at DESC LIMIT ${len(params)}",
                *params,
            )
            return [dict(r) for r in rows]

    async def save_memo(self, content: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ai_memos (content, created_at) VALUES ($1, NOW())",
                content,
            )

    async def get_recent_memos(self, limit: int = 5) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content FROM ai_memos ORDER BY created_at DESC LIMIT $1",
                limit,
            )
            return [r["content"] for r in rows]

    async def save_history_entry(self, role: str, source: str, content: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_chat_history (role, source, content, created_at)
                VALUES ($1, $2, $3, NOW())
                """,
                role, source, content,
            )

    async def get_chat_history(
        self,
        date: str | None = None,
        source: str | None = None,
        limit: int = 40,
    ) -> list[dict]:
        async with self._pool.acquire() as conn:
            conditions = []
            params: list = []
            if date:
                params.append(date)
                conditions.append(f"created_at::date = ${len(params)}::date")
            else:
                conditions.append("created_at::date = CURRENT_DATE")
            if source:
                params.append(source)
                conditions.append(f"source = ${len(params)}")
            params.append(limit)
            where = " AND ".join(conditions)
            rows = await conn.fetch(
                f"""
                SELECT role, source, content, created_at
                FROM ai_chat_history
                WHERE {where}
                ORDER BY created_at ASC
                LIMIT ${len(params)}
                """,
                *params,
            )
            return [dict(r) for r in rows]

    async def load_today_history(self) -> list[dict]:
        """서버 시작 시 오늘 대화 히스토리를 복원합니다."""
        rows = await self.get_chat_history()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
