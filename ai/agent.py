import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

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
        full = await self._memory.load_today_history()
        self._chat_history = full[-_CHAT_HISTORY_LIMIT * 2:]
        logger.info("히스토리 복원: %d개 항목 (전체 %d개)", len(self._chat_history), len(full))

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
        past = list(self._chat_history)
        self._push_history("user", "event", user_msg)
        self._executor.reset_executed_tools()
        self._executor.allow_orders = True
        final_text = await self._run_loop(source="event", past_history=past, current_message=user_msg)
        missing = [
            name for name in ("get_price", "save_memo")
            if name not in set(self._executor.executed_tools)
        ]
        if missing:
            logger.warning("이벤트 필수 도구 누락, 재시도: %s", missing)
            retry_prompt = f"""
The previous watch-trigger response is invalid because it did not call required tools: {", ".join(missing)}.

Triggered event:
{user_msg}

Previous response:
{(final_text or "")[:2000]}

Now call the missing tools as real tool calls before making a decision.
For this event, you must use get_price for the triggered symbol, then save_memo.
Use get_candles only if the trigger payload is insufficient for the decision.
Only call place_order if the fresh tool data justifies BUY or SELL. Otherwise HOLD or adjust the watch.
Respond in Korean only.
""".strip()
            await self._run_loop(source="event", past_history=[], current_message=retry_prompt)
            if "save_memo" not in set(self._executor.executed_tools):
                await self._executor.execute("save_memo", {
                    "content": (
                        "이벤트 처리 보정: watch_triggered 이벤트에서 모델이 필수 메모 저장을 누락하여 "
                        f"시스템이 자동 기록했다. 이벤트: {event.stock_code} {event.payload}"
                    )
                })

    async def morning_brief(self) -> None:
        logger.info("아침 브리핑 시작")
        self._notify("system", "장 시작 전 브리핑을 시작합니다...")
        prompt = build_morning_brief_prompt()
        past = list(self._chat_history)
        self._push_history("user", "morning_brief", prompt)
        self._executor.allow_orders = True
        await self._run_loop(source="morning_brief", past_history=past, current_message=prompt)

    async def chat(self, user_input: str, source: str = "chat") -> str:
        """source="chat"(기본)은 HTTP 응답으로 바로 렌더링되므로 브로드캐스트에서 제외된다.
        스케줄러 등 HTTP 요청자가 없는 호출은 source="schedule" 등으로 넘겨야
        응답이 어디에도 안 뜨고 증발하지 않는다."""
        past = list(self._chat_history)
        is_planning = self._is_planning_request(user_input)
        prompt = self._build_chat_prompt(user_input, is_planning)
        self._push_history("user", "chat", user_input)
        self._executor.reset_executed_tools()
        self._executor.allow_orders = is_planning or self._is_order_execution_request(user_input)
        final_text = await self._run_loop(source=source, past_history=past, current_message=prompt)
        if self._looks_like_raw_tool_output(final_text):
            logger.warning("원시 도구 출력 형태 응답 감지, 최종 답변 재작성")
            final_text = await self._rewrite_raw_response(user_input, final_text, past)
        elif not self._contains_korean(final_text):
            logger.warning("비한국어 최종 응답 감지, 한국어로 재작성")
            final_text = await self._rewrite_raw_response(user_input, final_text, past)

        # 재무 데이터 환각 방지: 툴 미호출 시 강제 재시도
        required_tool = self._requires_data_tool(user_input)
        if required_tool and required_tool not in self.executed_tools_set:
            logger.warning("재무 데이터 환각 가능성 — %s 미호출, 강제 재시도", required_tool)
            retry_prompt = (
                f"반드시 {required_tool} 툴을 먼저 호출해서 실제 데이터를 확인한 뒤 답변하세요. "
                f"숫자를 임의로 만들어내지 마세요.\n\n사용자 질문: {user_input}"
            )
            final_text = await self._run_loop(source="chat", past_history=past, current_message=retry_prompt)

        if is_planning:
            missing = self._missing_plan_tools()
            if missing:
                logger.warning("계획 요청 필수 도구 누락, 도구 강제 재시도: %s", missing)
                retry_text = await self._retry_missing_plan_tools(user_input, final_text, missing, past)
                retry_missing = self._missing_plan_tools()
                if retry_missing:
                    logger.warning("계획 요청 필수 도구 재시도 후에도 누락: %s", retry_missing)
                    final_text = await self._record_plan_tool_failure(user_input, retry_text or final_text, retry_missing)
                else:
                    final_text = retry_text or final_text

        return final_text

    async def _run_loop(self, source: str, past_history: list[dict], current_message: str) -> str:
        final_text = await self._provider.run_loop(
            past_history=past_history,
            current_message=current_message,
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

    def _is_planning_request(self, user_input: str) -> bool:
        normalized = user_input.lower()
        planning_keywords = (
            "브리핑", "매수 계획", "뭐 살", "뭘 살",
            "종목 추천", "매수 후보", "살만한", "살 만한",
            "섹터 분석", "감시 설정", "와치 설정",
            "detecting/watch", "set watches", "trading plan", "buy plan",
            "autonomous", "trading process", "market trading process",
        )
        return any(keyword in normalized for keyword in planning_keywords)

    def _is_order_execution_request(self, user_input: str) -> bool:
        normalized = user_input.lower()
        execution_keywords = (
            "주문해", "매수해", "매도해", "실행해",
            "사줘", "사봐", "사라", "사달라", "팔아줘", "팔아봐", "팔아라",
            "매수", "매도", "주문",
            "buy now", "sell now", "place real orders", "real orders automatically",
        )
        return any(keyword in normalized for keyword in execution_keywords)

    def _missing_plan_tools(self) -> list[str]:
        executed = set(self._executor.executed_tools)
        required = ("save_plan", "save_memo", "set_watch")
        return [name for name in required if name not in executed]

    async def _retry_missing_plan_tools(self, user_input: str, previous_text: str, missing: list[str], past_history: list[dict] | None = None) -> str:
        retry_prompt = f"""
이전 응답에서 필수 툴 호출이 누락되었습니다: {", ".join(missing)}

원래 요청: {user_input}

이전 응답:
{previous_text[:2000]}

실제 툴 호출로 누락된 툴을 모두 호출하세요. 텍스트로 툴 호출을 설명하지 마세요.
포트폴리오/현금은 get_portfolio로, 활성 감시는 list_watches로 확인한 뒤 계획을 저장하세요.
감시 규칙을 설정할 때는 직접 종목 수를 결정하고 해당 종목에만 set_watch를 호출하세요.
주문하지 않기로 결정한 경우 place_order를 호출하지 마세요.
툴 호출 후 실제로 저장/설정된 내용을 한국어로만 답변하세요.
""".strip()
        return await self._run_loop(source="chat", past_history=past_history or [], current_message=retry_prompt)

    @property
    def executed_tools_set(self) -> set[str]:
        return set(self._executor.executed_tools)

    def _requires_data_tool(self, user_input: str) -> str | None:
        """재무 데이터 질문에 필요한 툴 이름 반환. 없으면 None."""
        t = user_input.lower()
        if any(k in t for k in ("잔고", "현금", "포지션", "보유", "계좌", "자산")):
            return "get_portfolio"
        if any(k in t for k in ("현재가", "주가", "가격", "얼마야", "얼마에")):
            return "get_price"
        return None

    def _looks_like_raw_tool_output(self, text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return False
        raw_markers = (
            '[{"title":',
            '{"query":',
            '{"results":',
            '"body":',
            '"rank_type":',
            '"candles":',
        )
        return any(marker in stripped for marker in raw_markers)

    @staticmethod
    def _contains_korean(text: str) -> bool:
        return any("가" <= ch <= "힣" for ch in text or "")

    async def _rewrite_raw_response(self, user_input: str, raw_text: str, past_history: list[dict] | None = None) -> str:
        prompt = f"""
이전 응답에 JSON 또는 검색 결과 원문이 그대로 노출되었습니다. 한국어로 간결하게 다시 답변하세요.

사용자 질문: {user_input}

이전 응답 내용:
{raw_text[:2500]}

JSON, 검색 결과 원문, 툴 페이로드를 포함하지 마세요.
실제로 확인한 내용을 바탕으로 사용자 질문에만 답변하세요. 불필요한 계획 요약은 하지 마세요.
""".strip()
        return await self._run_loop(source="chat", past_history=past_history or [], current_message=prompt)

    async def _record_plan_tool_failure(self, user_input: str, final_text: str, missing: list[str]) -> str:
        """계획 요청 필수 도구를 재시도 후에도 호출하지 않은 경우.
        예전엔 하드코딩된 3종목(NVDA/AAPL/MSFT) 중 하나로 임의 감시를 걸어 땜빵했는데,
        AI가 실제로 판단한 적 없는 종목에 감시가 걸리는 문제가 있어 제거했다.
        지금은 실패 사실만 정직하게 메모로 남기고, 사용자가 다시 요청하도록 안내한다."""
        memo = (
            f"계획 요청 실패: 모델이 필수 도구({', '.join(missing)})를 호출하지 않아 "
            f"계획/감시가 저장되지 않았습니다. 원문 요청: {user_input}"
        )
        await self._executor.execute("save_memo", {"content": memo})
        logger.error("계획 요청 처리 실패 — 필수 도구 미호출로 계획/감시 저장 안 됨: %s", missing)
        return (
            (final_text or "").strip()
            + "\n\n⚠️ 계획/감시 설정이 저장되지 않았습니다 (필수 도구 호출 누락: "
            + ", ".join(missing)
            + "). 다시 요청해주세요."
        )

    def _build_chat_prompt(self, user_input: str, is_planning: bool | None = None) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S KST")
        if is_planning is None:
            is_planning = self._is_planning_request(user_input)
        if is_planning:
            return f"""
Current time: {now}
User request: {user_input}

This is an autonomous trading plan or briefing request.
Infer the market from recent context. If the recent conversation is about US stocks or US market, analyze overseas/US candidates unless the user explicitly asks for Korea.
Do not ask which information to check first. Call the required tools yourself and build the plan.
The plan must include the AI's chosen thesis, candidate symbols if any, action decision, concrete detecting/watch rules if any,
risk controls chosen by the AI, allocation or cash-hold rationale, and the items actually saved or configured.
You must call save_plan and save_memo as real tool calls. Printing JSON examples is not enough.
If you decide BUY_NOW, call place_order as a real tool call and include position_pct or quantity.
If you decide to wait, choose how many symbols deserve active watches and call set_watch only for those symbols.
Never output raw JSON, search result arrays, or tool payloads. Summarize tool results in Korean.
Respond to the user in Korean only.
""".strip()
        return (
            f"Current time: {now}\nUser request: {user_input}\n"
            "Respond to the user in Korean only. Never output raw JSON, search result arrays, or tool payloads."
        )


def _format_event(event: MarketEvent) -> str:
    payload = event.payload
    lines = [f"Kind: {event.kind}", f"Market: {event.market}"]
    if event.stock_code:
        lines.append(f"Symbol: {event.stock_name} ({event.stock_code})")
    for key, val in payload.items():
        lines.append(f"{key}: {val}")
    return "\n".join(lines)
