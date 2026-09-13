"""LLM 客户端：接通「大脑」（OpenAI 兼容 API，可接 Kimi / DeepSeek）。"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from src.core.config import Settings, get_settings
from src.core.metrics import KIMI_TOKENS, LLM_LATENCY
from src.rag.prompts import (
    TOOL_SELECTION_HINT,
    detect_user_language,
    is_greeting,
    language_reply_instruction,
    JIAOZI_PERSONA,
)
from src.tools.registry import get_openai_tools


def sanitize_messages_for_api(messages: list[dict]) -> list[dict]:
    """清理发给 API 的消息：去掉空 content 的 assistant 消息（部分厂商会拒收）。"""
    cleaned: list[dict] = []
    for msg in messages:
        m = dict(msg)
        role = m.get("role")
        content = m.get("content")
        if role == "assistant" and (content is None or content == ""):
            if m.get("tool_calls"):
                m.pop("content", None)
            else:
                continue
        cleaned.append(m)
    return cleaned


def _tool_call_payload(call) -> dict[str, Any]:
    """把模型返回的 tool_call 转成标准 JSON 结构。"""
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.function.name,
            "arguments": call.function.arguments,
        },
    }


def build_assistant_tool_message(message) -> dict:
    """构造「助手发起单工具调用」的消息，用于多轮工具对话。"""
    call = message.tool_calls[0]
    payload: dict[str, Any] = {
        "role": "assistant",
        "tool_calls": [_tool_call_payload(call)],
    }
    if message.content:
        payload["content"] = message.content
    return payload


def build_assistant_tool_message_multi(message) -> dict:
    """构造「助手发起多工具并行调用」的消息。"""
    payload: dict[str, Any] = {
        "role": "assistant",
        "tool_calls": [_tool_call_payload(call) for call in message.tool_calls],
    }
    if message.content:
        payload["content"] = message.content
    return payload


class KimiClient:
    """大模型客户端（OpenAI 兼容）。类名历史遗留，实际可接 DeepSeek 等。"""

    def __init__(self, settings: Settings | None = None):
        """用 .env 里的 key / base_url 接通 LLM API。"""
        self.settings = settings or get_settings()
        self._client = AsyncOpenAI(
            api_key=self.settings.moonshot_api_key,
            base_url=self.settings.moonshot_base_url,
            timeout=120.0,
            max_retries=2,
        )

    @staticmethod
    def _record_usage(usage: Any, operation: str) -> None:
        """把 token 用量记入监控指标。"""
        if not usage:
            return
        prompt = getattr(usage, "prompt_tokens", None) or 0
        completion = getattr(usage, "completion_tokens", None) or 0
        total = getattr(usage, "total_tokens", None) or (prompt + completion)
        if prompt:
            KIMI_TOKENS.labels(token_type="prompt").inc(prompt)
        if completion:
            KIMI_TOKENS.labels(token_type="completion").inc(completion)
        if total:
            KIMI_TOKENS.labels(token_type="total").inc(total)

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int = 1024,
        stream: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        *,
        operation: str = "chat",
    ):
        """调用大模型聊天接口；可带 tools 做函数调用。"""
        kwargs: dict[str, Any] = {
            "model": self.settings.moonshot_model,
            "messages": sanitize_messages_for_api(messages),
            "temperature": 1,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        start = time.perf_counter()
        response = await self._client.chat.completions.create(**kwargs)
        LLM_LATENCY.labels(operation=operation).observe(time.perf_counter() - start)

        if stream:
            return response
        self._record_usage(getattr(response, "usage", None), operation)
        message = response.choices[0].message
        if message.tool_calls:
            return message
        return message.content or ""

    async def chat_stream(
        self,
        messages: list[dict],
        max_tokens: int = 1024,
        *,
        operation: str = "chat_stream",
    ) -> AsyncIterator[str]:
        """流式聊天：逐个 yield 文本片段。"""
        start = time.perf_counter()
        stream = await self.chat(
            messages, max_tokens=max_tokens, stream=True, operation=operation
        )
        first_token = True
        async for chunk in stream:
            if first_token:
                LLM_LATENCY.labels(operation=operation).observe(time.perf_counter() - start)
                first_token = False
            if not chunk.choices:
                continue
            if getattr(chunk, "usage", None):
                self._record_usage(chunk.usage, operation)
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    async def select_tool(
        self,
        question: str,
        context: list[dict[str, str]] | None = None,
    ) -> tuple[str | None, dict]:
        """让模型选择要调用的工具；问候则不选。"""
        if is_greeting(question):
            return None, {}

        system_prompt = (
            f"{TOOL_SELECTION_HINT}\n"
            "根据用户问题选择合适的工具。若无需工具（如纯问候），不要调用任何工具。"
        )
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]
        if context:
            messages.extend(context[-4:])
        messages.append({"role": "user", "content": question})

        message = await self.chat(
            messages,
            tools=get_openai_tools(),
            tool_choice="auto",
        )
        if isinstance(message, str):
            return self._parse_legacy_tool_json(message)

        if hasattr(message, "tool_calls") and message.tool_calls:
            call = message.tool_calls[0]
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            return name, args
        return None, {}

    @staticmethod
    def _parse_legacy_tool_json(raw: str) -> tuple[str | None, dict]:
        """兼容旧格式：模型用 JSON 文本返回 tool / tool_input。"""
        cleaned = raw.strip()
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            data = json.loads(cleaned)
            tool = data.get("tool")
            if tool:
                return tool, data.get("tool_input", {})
        except json.JSONDecodeError:
            pass
        return None, {}

    async def synthesize_answer(
        self,
        question: str,
        tool_name: str,
        tool_result: dict,
        context: list[dict[str, str]] | None = None,
    ) -> str:
        """把单个工具结果组织成对用户友好的自然语言回答。"""
        lang_instruction = language_reply_instruction(detect_user_language(question))
        prompt = (
            f"User question: {question}\n"
            f"Tool {tool_name} returned: {tool_result}\n\n"
            f"{lang_instruction}\n"
            "Answer as Jiaozi (饺子) in Stewie Griffin style: theatrical, posh, mildly snarky, concise. "
            "If the tool result is in a different language than the user's question, "
            "translate it before replying."
        )
        messages: list[dict] = [
            {"role": "system", "content": f"{JIAOZI_PERSONA}\n{lang_instruction}"},
        ]
        if context:
            messages.extend(context[-4:])
        messages.append({"role": "user", "content": prompt})
        result = await self.chat(messages)
        return result if isinstance(result, str) else ""

    async def synthesize_answer_stream(
        self,
        question: str,
        tool_name: str,
        tool_result: dict,
        context: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[str]:
        """流式版：单工具结果 → 自然语言回答。"""
        lang_instruction = language_reply_instruction(detect_user_language(question))
        prompt = (
            f"User question: {question}\n"
            f"Tool {tool_name} returned: {tool_result}\n\n"
            f"{lang_instruction}\n"
            "Answer as Jiaozi (饺子) in Stewie Griffin style: theatrical, posh, mildly snarky, concise."
        )
        messages: list[dict] = [
            {"role": "system", "content": f"{JIAOZI_PERSONA}\n{lang_instruction}"},
        ]
        if context:
            messages.extend(context[-4:])
        messages.append({"role": "user", "content": prompt})
        async for token in self.chat_stream(messages):
            yield token

    async def synthesize_multi_answer(
        self,
        question: str,
        steps: list[dict],
        context: list[dict[str, str]] | None = None,
    ) -> str:
        """把多个工具步骤结果合成一句完整回答。"""
        lang_instruction = language_reply_instruction(detect_user_language(question))
        prompt = (
            f"User question: {question}\n"
            f"Tools executed (in order):\n{json.dumps(steps, ensure_ascii=False)}\n\n"
            f"{lang_instruction}\n"
            "Synthesize one concise answer as Jiaozi (饺子) using all tool results. "
            "Stewie-like: theatrical, witty, mildly arrogant—but helpful and accurate. "
            "If logistics was queried after an order lookup, combine order and tracking info. "
            "Reply with plain text only; do not use thinking or reasoning tags."
        )
        messages: list[dict] = [
            {"role": "system", "content": f"{JIAOZI_PERSONA}\n{lang_instruction}"},
        ]
        if context:
            messages.extend(context[-4:])
        messages.append({"role": "user", "content": prompt})
        result = await self.chat(messages)
        return result if isinstance(result, str) else ""

    async def synthesize_multi_stream(
        self,
        question: str,
        steps: list[dict],
        context: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[str]:
        """流式版：多工具结果合成回答。"""
        lang_instruction = language_reply_instruction(detect_user_language(question))
        prompt = (
            f"User question: {question}\n"
            f"Tools executed (in order):\n{json.dumps(steps, ensure_ascii=False)}\n\n"
            f"{lang_instruction}\n"
            "Synthesize one concise answer as Jiaozi (饺子) using all tool results. "
            "Stewie-like theatrical, witty tone—still clear and useful. "
            "Reply with plain text only; do not use thinking or reasoning tags."
        )
        messages: list[dict] = [
            {"role": "system", "content": f"{JIAOZI_PERSONA}\n{lang_instruction}"},
        ]
        if context:
            messages.extend(context[-4:])
        messages.append({"role": "user", "content": prompt})
        async for token in self.chat_stream(messages):
            yield token
