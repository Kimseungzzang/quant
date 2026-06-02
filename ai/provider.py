import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

import anthropic
import openai

from ai.prompts import SYSTEM_PROMPT
from ai.tools import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

_MAX_TOKENS = 4096


class BaseProvider(ABC):
    @abstractmethod
    async def run_loop(
        self,
        history: list[dict],
        on_text: Callable[[str], None],
        on_tool: Callable[[str, str], None],
        execute_tool: Callable[[str, dict], Awaitable[str]],
    ) -> str: ...


class AnthropicProvider(BaseProvider):
    MODEL = "claude-haiku-4-5-20251001"

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    async def run_loop(
        self,
        history: list[dict],
        on_text: Callable[[str], None],
        on_tool: Callable[[str, str], None],
        execute_tool: Callable[[str, dict], Awaitable[str]],
    ) -> str:
        messages = list(history)
        final_text = ""

        while True:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda msgs=messages: self._client.messages.create(
                    model=self.MODEL,
                    max_tokens=_MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_DEFINITIONS,
                    messages=msgs,
                ),
            )

            tool_calls = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            if text_blocks:
                final_text = text_blocks[-1].text
                on_text(final_text)

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn" or not tool_calls:
                break

            tool_results = []
            for tc in tool_calls:
                logger.info("도구 호출: %s(%s)", tc.name, tc.input)
                result = await execute_tool(tc.name, tc.input)
                on_tool(tc.name, result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})

        return final_text


class OpenAICompatibleProvider(BaseProvider):
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    @staticmethod
    def _to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in anthropic_tools
        ]

    async def run_loop(
        self,
        history: list[dict],
        on_text: Callable[[str], None],
        on_tool: Callable[[str, str], None],
        execute_tool: Callable[[str, dict], Awaitable[str]],
    ) -> str:
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
        tools = self._to_openai_tools(TOOL_DEFINITIONS)
        final_text = ""

        while True:
            for attempt in range(3):
                try:
                    response = await self._client.chat.completions.create(
                        model=self.model,
                        max_tokens=_MAX_TOKENS,
                        messages=messages,
                        tools=tools,
                    )
                    break
                except openai.RateLimitError as e:
                    retry_after = getattr(e, "retry_after", None) or 20
                    if attempt == 2 or retry_after > 60:
                        logger.warning("429 한도 초과 (재시도 불가): %s", e)
                        return "API 일일 요청 한도를 초과했습니다. 내일 다시 사용 가능합니다."
                    logger.warning("429 — %ds 후 재시도 (%d/2)", retry_after, attempt + 1)
                    await asyncio.sleep(retry_after)
            else:
                return "API 요청 한도 초과입니다."

            choice = response.choices[0]
            msg = choice.message
            text = msg.content or ""
            tool_calls = msg.tool_calls or []

            if text:
                final_text = text
                on_text(final_text)

            assistant_msg: dict = {"role": "assistant", "content": msg.content}
            if tool_calls:
                assistant_msg["tool_calls"] = [tc.model_dump() for tc in tool_calls]
            messages.append(assistant_msg)

            if choice.finish_reason == "stop" or not tool_calls:
                break

            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                logger.info("도구 호출: %s(%s)", tc.function.name, args)
                result = await execute_tool(tc.function.name, args)
                on_tool(tc.function.name, result)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

        return final_text


_PROVIDERS = {
    "anthropic": lambda key: AnthropicProvider(key),
    "gemini":    lambda key: OpenAICompatibleProvider(
        key,
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-2.5-flash-lite",
    ),
    "groq":      lambda key: OpenAICompatibleProvider(
        key,
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
    ),
}


def create_provider(provider_name: str, api_key: str) -> BaseProvider:
    factory = _PROVIDERS.get(provider_name.lower())
    if not factory:
        raise ValueError(f"알 수 없는 provider: {provider_name} (사용 가능: {list(_PROVIDERS)})")
    return factory(api_key)
