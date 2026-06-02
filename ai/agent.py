import asyncio
import logging
from collections.abc import Callable

from ai.memory import AgentMemory
from ai.prompts import build_event_prompt, build_morning_brief_prompt
from ai.provider import BaseProvider
from ai.tools import ToolExecutor
from events.types import MarketEvent

logger = logging.getLogger(__name__)

_CHAT_HISTORY_LIMIT = 20


class AIAgent:
    def __init__(
        self,
        provider: BaseProvider,
        tool_executor: ToolExecutor,
        memory: AgentMemory,
        on_message: Callable[[str, str], None] | None = None,
    ):
        self._provider = provider
        self._executor = tool_executor
        self._memory = memory
        self._on_message = on_message
        self._chat_history: list[dict] = []
        self._current_session_id: int | None = None

    async def initialize(self) -> None:
        self._chat_history = await self._memory.load_today_history()
        logger.info("히스토리 복원: %d개 항목", len(self._chat_history))

    def _push_history(self, role: str, source: str, content: str) -> None:
        self._chat_history.append({"role": role, "content": content})
        if len(self._chat_history) > _CHAT_HISTORY_LIMIT * 2:
            self._chat_history = self._chat_history[-_CHAT_HISTORY_LIMIT * 2:]
        asyncio.ensure_future(self._memory.save_history_entry(role, source, content))

    async def handle_event(self, event: MarketEvent) -> None:
        logger.info("AI 이벤트 처리: %s", event)
        today_plan = await self._memory.get_today_plan()
        recent = await self._memory.get_recent_decisions(limit=5)

        plan_str = (
            f"전망: {today_plan['market_outlook']}\n전략: {today_plan['strategy']}"
            if today_plan else "아직 오늘 계획 없음"
        )
        recent_str = "\n".join(
            f"- [{r['decided_at']}] {r['stock_code']} {r['action']}: {r['reason']}"
            for r in recent
        ) or "없음"

        user_msg = build_event_prompt(_format_event(event), plan_str, recent_str)
        self._push_history("user", "event", user_msg)
        await self._run_loop(source="event")

    async def morning_brief(self) -> None:
        logger.info("아침 브리핑 시작")
        self._notify("system", "장 시작 전 브리핑을 시작합니다...")
        prompt = build_morning_brief_prompt()
        self._push_history("user", "morning_brief", prompt)
        await self._run_loop(source="morning_brief")

    async def chat(self, user_input: str) -> str:
        self._push_history("user", "chat", user_input)
        return await self._run_loop(source="chat")

    async def _run_loop(self, source: str) -> str:
        final_text = await self._provider.run_loop(
            history=list(self._chat_history),
            on_text=lambda text: self._notify(source, text),
            on_tool=lambda name, result: self._notify("tool", f"{name} → {result[:200]}"),
            execute_tool=self._executor.execute,
        )

        if final_text:
            self._push_history("assistant", source, final_text)

        if source in ("event", "morning_brief") and final_text:
            await self._memory.save_memo(f"[{source}] {final_text[:500]}")

        return final_text

    def _notify(self, source: str, message: str) -> None:
        if self._on_message:
            self._on_message(source, message)
        logger.info("[%s] %s", source, message[:200])


def _format_event(event: MarketEvent) -> str:
    payload = event.payload
    lines = [f"종류: {event.kind}", f"시각: {event.market}"]
    if event.stock_code:
        lines.append(f"종목: {event.stock_name} ({event.stock_code})")
    for key, val in payload.items():
        lines.append(f"{key}: {val}")
    return "\n".join(lines)
