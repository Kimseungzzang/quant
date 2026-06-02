import asyncio
import json
import logging
from collections.abc import Callable

import anthropic

from ai.memory import AgentMemory
from ai.prompts import SYSTEM_PROMPT, build_event_prompt, build_morning_brief_prompt
from ai.tools import TOOL_DEFINITIONS, ToolExecutor
from events.types import EventKind, MarketEvent

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_TOKENS = 4096
_CHAT_HISTORY_LIMIT = 20


class AIAgent:
    def __init__(
        self,
        api_key: str,
        tool_executor: ToolExecutor,
        memory: AgentMemory,
        on_message: Callable[[str, str], None] | None = None,
    ):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._executor = tool_executor
        self._memory = memory
        self._on_message = on_message
        self._chat_history: list[dict] = []
        self._current_session_id: int | None = None

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

        event_summary = _format_event(event)
        user_msg = build_event_prompt(event_summary, plan_str, recent_str)

        await self._run_agentic_loop(user_msg, source="event")

    async def morning_brief(self) -> None:
        logger.info("아침 브리핑 시작")
        self._notify("system", "장 시작 전 브리핑을 시작합니다...")
        await self._run_agentic_loop(build_morning_brief_prompt(), source="morning_brief")

    async def chat(self, user_input: str) -> str:
        self._chat_history.append({"role": "user", "content": user_input})
        if len(self._chat_history) > _CHAT_HISTORY_LIMIT * 2:
            self._chat_history = self._chat_history[-_CHAT_HISTORY_LIMIT * 2:]

        response = await self._run_agentic_loop(
            user_input,
            source="chat",
            use_history=True,
        )
        return response

    async def _run_agentic_loop(
        self,
        initial_message: str,
        source: str,
        use_history: bool = False,
    ) -> str:
        if use_history:
            messages = list(self._chat_history)
        else:
            messages = [{"role": "user", "content": initial_message}]

        final_text = ""

        while True:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda msgs=messages: self._client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_DEFINITIONS,
                    messages=msgs,
                ),
            )

            tool_calls = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            if text_blocks:
                final_text = text_blocks[-1].text
                self._notify(source, final_text)

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn" or not tool_calls:
                break

            tool_results = []
            for tool_call in tool_calls:
                logger.info("도구 호출: %s(%s)", tool_call.name, tool_call.input)
                result = await self._executor.execute(tool_call.name, tool_call.input)
                self._notify("tool", f"{tool_call.name} → {result[:200]}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        if use_history:
            self._chat_history.append({"role": "assistant", "content": final_text})

        if source in ("event", "morning_brief") and final_text:
            await self._memory.save_memo(f"[{source}] {final_text[:500]}")

        return final_text

    def _notify(self, source: str, message: str) -> None:
        if self._on_message:
            self._on_message(source, message)
        logger.info("[%s] %s", source, message[:200])


def _format_event(event: MarketEvent) -> str:
    payload = event.payload
    lines = [f"종류: {event.kind}", f"시장: {event.market}"]
    if event.stock_code:
        lines.append(f"종목: {event.stock_name} ({event.stock_code})")
    for key, val in payload.items():
        lines.append(f"{key}: {val}")
    return "\n".join(lines)
