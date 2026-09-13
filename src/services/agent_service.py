"""Agent 编排服务：项目经理，决定找哪个工具、如何回答用户。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.metrics import TOOL_CALLS, TOOL_LATENCY, TOOL_SELECTION
from src.rag.prompts import (
    AGENT_SYSTEM_PROMPT,
    REACT_SYSTEM_HINT,
    TOOL_SELECTION_HINT,
    build_greeting_messages,
    is_greeting,
)
from src.rag.retriever import Retriever
from src.services.llm.kimi_client import (
    KimiClient,
    build_assistant_tool_message_multi,
)
from src.services.llm.local_qwen import LocalQwenService
from src.services.session_service import SessionService
from src.tools import complaint, knowledge_chat, logistics, order, returns
from src.tools.registry import get_openai_tools
from src.core.tracing import trace_span
from src.services.workflows.return_workflow import try_handle_return_workflow
from src.services.workflows.complaint_intent import try_handle_complaint_intent
from src.services.workflows.fast_route import resolve_fast_route
from src.services.workflows.my_orders_intent import try_handle_my_orders_intent
from src.utils.text import sanitize_assistant_reply
from src.utils.tool_answer import format_tool_steps_answer
from src.utils.emotion import detect_jiaozi_emotion

logger = structlog.get_logger()

_STREAM_CHUNK_SIZE = 20

_STRUCTURED_TOOLS = frozenset(
    {
        "query_order",
        "query_my_orders",
        "fetch_logistics_information",
        "record_user_complaint",
        "query_work_order",
    }
)


def _all_structured_tools(steps: list) -> bool:
    """结构化工具（含失败）一律模板回复，禁止再调 LLM 合成。"""
    return bool(steps) and all(s.tool_name in _STRUCTURED_TOOLS for s in steps)


@dataclass
class ToolStep:
    """一次工具调用的记录：名字、入参、结果。"""

    tool_name: str
    tool_input: dict
    tool_result: dict


@dataclass
class AgentResponse:
    """Agent 最终回复结构（非流式）。"""

    answer: str
    tool_name: str | None = None
    tool_result: dict | None = None
    citations: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    emotion: str = "listen"


def _done_event(
    answer: str,
    tool_name: str | None = None,
    citations: list[str] | None = None,
    tools_used: list[str] | None = None,
    emotion: str | None = None,
    question: str = "",
) -> dict[str, Any]:
    """构造流式结束事件，告诉前端「回答完成了」。"""
    tools = tools_used or []
    return {
        "type": "done",
        "answer": answer,
        "tool_name": tool_name,
        "citations": citations or [],
        "tools_used": tools,
        "emotion": emotion
        or detect_jiaozi_emotion(question, tools_used=tools, answer=answer),
    }


def _with_emotion(
    response: AgentResponse,
    question: str,
) -> AgentResponse:
    response.emotion = detect_jiaozi_emotion(
        question,
        tools_used=response.tools_used,
        answer=response.answer,
    )
    return response


def _collect_citations(steps: list[ToolStep]) -> list[str]:
    """从各工具结果里收集引用/来源列表。"""
    citations: list[str] = []
    for step in steps:
        citations.extend(step.tool_result.get("citations", []))
    return citations


class AgentService:
    """智能客服的「项目经理」：选工具、调用工具、合成答案。"""

    def __init__(
        self,
        kimi: KimiClient,
        retriever: Retriever,
        qwen: LocalQwenService | None = None,
    ):
        """保存 LLM 客户端、检索器、可选本地生成模型。"""
        self.kimi = kimi
        self.retriever = retriever
        self.qwen = qwen

    async def _call_tool(
        self,
        tool_name: str,
        tool_input: dict,
        session_id: str | None = None,
        db: AsyncSession | None = None,
        user_id: str | None = None,
        user_email: str | None = None,
        *,
        defer_customer_chat: bool = False,
    ) -> dict:
        """按工具名分发到具体业务函数（物流/投诉/退换货/知识库/订单）。"""
        TOOL_CALLS.labels(tool_name=tool_name).inc()
        start = time.perf_counter()
        try:
            if tool_name == "fetch_logistics_information":
                result = await logistics.fetch_logistics_information(
                    tool_input.get("logistics_number", ""),
                    email=user_email,
                    user_id=user_id,
                    db=db,
                )
            elif tool_name == "record_user_complaint":
                result = await complaint.record_user_complaint(
                    tool_input.get("complaint_details", ""),
                    session_id=session_id,
                    db=db,
                )
            elif tool_name == "query_work_order":
                result = await complaint.query_work_order(
                    tool_input.get("ticket_id", ""),
                    db=db,
                    user_id=user_id,
                    require_owner=bool(user_id),
                )
            elif tool_name == "create_return_request":
                result = await returns.create_return_request(
                    tool_input.get("query", ""),
                    order_id=tool_input.get("order_id"),
                    retriever=self.retriever,
                    qwen=self.qwen,
                    kimi=self.kimi,
                )
            elif tool_name == "customer_chat":
                # 流式路径稍后一次生成，避免 ReAct 阶段先完整生成一遍
                if defer_customer_chat:
                    return {
                        "success": True,
                        "deferred": True,
                        "data": {"query": tool_input.get("query", "")},
                        "citations": [],
                    }
                result = await knowledge_chat.customer_chat(
                    tool_input.get("query", ""),
                    self.retriever,
                    qwen=self.qwen,
                    kimi=self.kimi,
                )
            elif tool_name == "query_order":
                result = await order.query_order(
                    tool_input.get("order_id", ""),
                    email=tool_input.get("email") or user_email,
                )
            elif tool_name == "query_my_orders":
                if not user_id:
                    return {
                        "success": False,
                        "error": "先登录。本饺子再天才，也翻不开没权限的订单。",
                        "login_required": True,
                    }
                result = await order.list_my_orders(user_id=user_id, email=user_email)
            else:
                return {"success": False, "error": "Unknown tool"}

            return result.to_dict()
        finally:
            TOOL_LATENCY.labels(tool_name=tool_name).observe(time.perf_counter() - start)

    async def _get_context(
        self,
        session_id: str | None,
        db: AsyncSession | None,
        sessions: SessionService | None,
    ) -> list[dict[str, str]]:
        """取出当前会话历史，给模型当上下文。"""
        if not session_id or not db or not sessions:
            return []
        return await sessions.get_context_messages(db, session_id)

    async def _run_tool_plan(
        self,
        plan: list[tuple[str, dict]],
        *,
        session_id: str | None = None,
        db: AsyncSession | None = None,
        user_id: str | None = None,
        user_email: str | None = None,
        defer_customer_chat: bool = False,
    ) -> list[ToolStep]:
        """按已确定的工具计划执行（规则快路径或 ReAct 结果）。"""

        async def _one(name: str, args: dict) -> ToolStep:
            logger.info("fast_or_planned_tool", tool=name, session_id=session_id)
            TOOL_SELECTION.labels(tool_name=name).inc()
            result = await self._call_tool(
                name,
                args,
                session_id=session_id,
                db=db,
                user_id=user_id,
                user_email=user_email,
                defer_customer_chat=defer_customer_chat,
            )
            return ToolStep(name, args, result)

        if len(plan) == 1:
            return [await _one(plan[0][0], plan[0][1])]
        gathered = await asyncio.gather(*[_one(n, a) for n, a in plan])
        return list(gathered)

    async def _run_react(
        self,
        question: str,
        context: list[dict[str, str]],
        *,
        session_id: str | None = None,
        db: AsyncSession | None = None,
        user_id: str | None = None,
        user_email: str | None = None,
        defer_customer_chat: bool = False,
    ) -> list[ToolStep]:
        """ReAct 循环：模型选工具 → 执行 → 结果回填，可多步。"""
        settings = get_settings()
        steps: list[ToolStep] = []
        react_messages: list[dict] = [
            {
                "role": "system",
                "content": f"{REACT_SYSTEM_HINT}\n{TOOL_SELECTION_HINT}",
            },
        ]
        if context:
            react_messages.extend(context[-4:])
        react_messages.append({"role": "user", "content": question})

        for _ in range(settings.agent_max_steps):
            message = await self.kimi.chat(
                react_messages,
                tools=get_openai_tools(),
                tool_choice="auto",
            )
            if isinstance(message, str) or not getattr(message, "tool_calls", None):
                break

            calls = message.tool_calls
            react_messages.append(build_assistant_tool_message_multi(message))

            async def _invoke(call):
                """执行单个工具调用。"""
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                logger.info("react_tool_step", tool=name, session_id=session_id)
                TOOL_SELECTION.labels(tool_name=name).inc()
                tool_result = await self._call_tool(
                    name,
                    args,
                    session_id=session_id,
                    db=db,
                    user_id=user_id,
                    user_email=user_email,
                    defer_customer_chat=defer_customer_chat,
                )
                return call, name, args, tool_result

            if len(calls) == 1:
                call, name, args, tool_result = await _invoke(calls[0])
                results = [(call, name, args, tool_result)]
            else:
                gathered = await asyncio.gather(*[_invoke(c) for c in calls])
                results = list(gathered)

            for call, name, args, tool_result in results:
                steps.append(ToolStep(name, args, tool_result))
                react_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )

            if any(
                r[1] in ("customer_chat", "create_return_request") for r in results
            ):
                break

        return steps

    async def _collect_steps(
        self,
        question: str,
        context: list[dict[str, str]],
        *,
        session_id: str | None = None,
        db: AsyncSession | None = None,
        user_id: str | None = None,
        user_email: str | None = None,
        defer_customer_chat: bool = False,
    ) -> list[ToolStep]:
        """规则快路径优先；否则走 ReAct。"""
        plan = resolve_fast_route(question)
        if plan:
            logger.info(
                "fast_route_hit",
                tools=[p[0] for p in plan],
                session_id=session_id,
            )
            return await self._run_tool_plan(
                plan,
                session_id=session_id,
                db=db,
                user_id=user_id,
                user_email=user_email,
                defer_customer_chat=defer_customer_chat,
            )
        return await self._run_react(
            question,
            context,
            session_id=session_id,
            db=db,
            user_id=user_id,
            user_email=user_email,
            defer_customer_chat=defer_customer_chat,
        )

    def _steps_payload(self, steps: list[ToolStep]) -> list[dict]:
        """把 ToolStep 转成便于模型阅读的字典列表。"""
        return [
            {
                "tool": s.tool_name,
                "input": s.tool_input,
                "result": s.tool_result,
            }
            for s in steps
        ]

    async def process(
        self,
        question: str,
        session_id: str | None = None,
        db: AsyncSession | None = None,
        sessions: SessionService | None = None,
        user_id: str | None = None,
        user_email: str | None = None,
    ) -> AgentResponse:
        """处理用户问题（一次性返回完整答案）。

        顺序：问候 → 退换货 Workflow → ReAct 工具 → 合成回答。
        """
        with trace_span(
            "agent.process",
            session_id=session_id or "",
            user_id=user_id or "",
        ):
            if is_greeting(question):
                answer = await self.kimi.chat(build_greeting_messages(question))
                if not isinstance(answer, str):
                    answer = getattr(answer, "content", "") or ""
                return _with_emotion(AgentResponse(answer=answer), question)

            my_orders_response = await try_handle_my_orders_intent(
                question,
                self,
                user_id=user_id,
                user_email=user_email,
                db=db,
            )
            if my_orders_response is not None:
                return _with_emotion(my_orders_response, question)

            context = await self._get_context(session_id, db, sessions)

            # 显式投诉优先于退货流程（避免「退款不到账要投诉」走进退货）
            complaint_response = await try_handle_complaint_intent(
                question,
                self,
                session_id=session_id,
                db=db,
                sessions=sessions,
            )
            if complaint_response is not None:
                return _with_emotion(complaint_response, question)

            workflow_response = await try_handle_return_workflow(
                question, self, sessions, session_id
            )
            if workflow_response is not None:
                return _with_emotion(workflow_response, question)

            steps = await self._collect_steps(
                question,
                context,
                session_id=session_id,
                db=db,
                user_id=user_id,
                user_email=user_email,
                defer_customer_chat=False,
            )

            if not steps:
                messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
                messages.extend(context[-6:])
                messages.append({"role": "user", "content": question})
                answer = await self.kimi.chat(messages)
                if not isinstance(answer, str):
                    answer = getattr(answer, "content", "") or ""
                return _with_emotion(AgentResponse(answer=answer), question)

            tools_used = [s.tool_name for s in steps]
            citations = _collect_citations(steps)
            payload = self._steps_payload(steps)

            if len(steps) == 1 and steps[0].tool_name in (
                "customer_chat",
                "create_return_request",
            ):
                data = steps[0].tool_result.get("data", {})
                answer = data.get("answer", "")
                if answer:
                    return _with_emotion(
                        AgentResponse(
                            answer=answer,
                            tool_name=steps[0].tool_name,
                            tool_result=steps[0].tool_result,
                            citations=citations,
                            tools_used=tools_used,
                        ),
                        question,
                    )

            # 订单/物流/工单等结构化结果：绕过 Kimi 合成，直接格式化输出
            if _all_structured_tools(steps):
                answer = format_tool_steps_answer(question, payload)
                if answer:
                    return _with_emotion(
                        AgentResponse(
                            answer=answer,
                            tool_name=tools_used[-1] if tools_used else None,
                            tool_result=steps[-1].tool_result if steps else None,
                            citations=citations,
                            tools_used=tools_used,
                        ),
                        question,
                    )

            answer = await self.kimi.synthesize_multi_answer(
                question, payload, context=context
            )
            answer = sanitize_assistant_reply(answer)
            if not answer:
                answer = format_tool_steps_answer(question, payload)
            return _with_emotion(
                AgentResponse(
                    answer=answer,
                    tool_name=tools_used[-1] if tools_used else None,
                    tool_result=steps[-1].tool_result if steps else None,
                    citations=citations,
                    tools_used=tools_used,
                ),
                question,
            )

    async def process_stream(
        self,
        question: str,
        session_id: str | None = None,
        db: AsyncSession | None = None,
        sessions: SessionService | None = None,
        user_id: str | None = None,
        user_email: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式处理用户问题：边生成边 yield chunk，最后 yield done。

        聊天页 WebSocket 主要走这个方法。
        """
        with trace_span(
            "agent.process_stream",
            session_id=session_id or "",
            user_id=user_id or "",
        ):
            parts: list[str] = []

            if is_greeting(question):
                greeting_messages = build_greeting_messages(question)
                async for token in self.kimi.chat_stream(greeting_messages):
                    parts.append(token)
                    yield {"type": "chunk", "content": token}
                if not parts:
                    fallback = await self.kimi.chat(greeting_messages)
                    if isinstance(fallback, str) and fallback:
                        parts.append(fallback)
                        yield {"type": "chunk", "content": fallback}
                yield _done_event("".join(parts), question=question)
                return

            # 「我的订单」最先短路：不读历史、不调模型
            my_orders_response = await try_handle_my_orders_intent(
                question,
                self,
                user_id=user_id,
                user_email=user_email,
                db=db,
            )
            if my_orders_response is not None:
                answer = my_orders_response.answer
                for chunk in [
                    answer[i : i + _STREAM_CHUNK_SIZE]
                    for i in range(0, len(answer), _STREAM_CHUNK_SIZE)
                ]:
                    parts.append(chunk)
                    yield {"type": "chunk", "content": chunk}
                yield _done_event(
                    answer,
                    tool_name=my_orders_response.tool_name,
                    citations=my_orders_response.citations,
                    tools_used=my_orders_response.tools_used,
                    question=question,
                )
                return

            context = await self._get_context(session_id, db, sessions)

            complaint_response = await try_handle_complaint_intent(
                question,
                self,
                session_id=session_id,
                db=db,
                sessions=sessions,
            )
            if complaint_response is not None:
                answer = complaint_response.answer
                for chunk in [
                    answer[i : i + _STREAM_CHUNK_SIZE]
                    for i in range(0, len(answer), _STREAM_CHUNK_SIZE)
                ]:
                    parts.append(chunk)
                    yield {"type": "chunk", "content": chunk}
                yield _done_event(
                    answer,
                    tool_name=complaint_response.tool_name,
                    citations=complaint_response.citations,
                    tools_used=complaint_response.tools_used,
                    question=question,
                )
                return

            workflow_response = await try_handle_return_workflow(
                question, self, sessions, session_id
            )
            if workflow_response is not None:
                answer = workflow_response.answer
                for chunk in [
                    answer[i : i + _STREAM_CHUNK_SIZE]
                    for i in range(0, len(answer), _STREAM_CHUNK_SIZE)
                ]:
                    parts.append(chunk)
                    yield {"type": "chunk", "content": chunk}
                yield _done_event(
                    answer,
                    tool_name=workflow_response.tool_name,
                    citations=workflow_response.citations,
                    tools_used=workflow_response.tools_used,
                    question=question,
                )
                return

            steps = await self._collect_steps(
                question,
                context,
                session_id=session_id,
                db=db,
                user_id=user_id,
                user_email=user_email,
                defer_customer_chat=True,
            )

            if not steps:
                messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
                messages.extend(context[-6:])
                messages.append({"role": "user", "content": question})
                async for token in self.kimi.chat_stream(messages):
                    parts.append(token)
                    yield {"type": "chunk", "content": token}
                if not parts:
                    fallback = await self.kimi.chat(messages)
                    if isinstance(fallback, str) and fallback:
                        parts.append(fallback)
                        yield {"type": "chunk", "content": fallback}
                yield _done_event("".join(parts), question=question)
                return

            tools_used = [s.tool_name for s in steps]
            citations = _collect_citations(steps)

            if len(steps) == 1 and steps[0].tool_name == "create_return_request":
                # 退换货已是固定政策模板，禁止再走 RAG
                data = steps[0].tool_result.get("data") or {}
                answer_text = data.get("answer") or data.get("message") or ""
                if steps[0].tool_result.get("citations"):
                    citations = steps[0].tool_result.get("citations") or citations
                for chunk in [
                    answer_text[i : i + _STREAM_CHUNK_SIZE]
                    for i in range(0, len(answer_text), _STREAM_CHUNK_SIZE)
                ]:
                    parts.append(chunk)
                    yield {"type": "chunk", "content": chunk}
            elif len(steps) == 1 and steps[0].tool_name == "customer_chat":
                query = steps[0].tool_input.get("query", question)
                # 若 ReAct/快路径已 defer，或结果里已有完整答案，优先流式生成一次
                existing = (steps[0].tool_result.get("data") or {}).get("answer")
                if existing and not steps[0].tool_result.get("deferred"):
                    for chunk in [
                        existing[i : i + _STREAM_CHUNK_SIZE]
                        for i in range(0, len(existing), _STREAM_CHUNK_SIZE)
                    ]:
                        parts.append(chunk)
                        yield {"type": "chunk", "content": chunk}
                else:
                    step_citations, token_stream = await knowledge_chat.customer_chat_stream(
                        query,
                        self.retriever,
                        qwen=self.qwen,
                        kimi=self.kimi,
                    )
                    if step_citations:
                        citations = step_citations
                    async for token in token_stream:
                        parts.append(token)
                        yield {"type": "chunk", "content": token}
            elif _all_structured_tools(steps):
                payload = self._steps_payload(steps)
                answer_text = format_tool_steps_answer(question, payload)
                if answer_text:
                    for chunk in [
                        answer_text[i : i + _STREAM_CHUNK_SIZE]
                        for i in range(0, len(answer_text), _STREAM_CHUNK_SIZE)
                    ]:
                        parts.append(chunk)
                        yield {"type": "chunk", "content": chunk}
                else:
                    parts.append("")
            else:
                payload = self._steps_payload(steps)
                async for token in self.kimi.synthesize_multi_stream(
                    question, payload, context=context
                ):
                    parts.append(token)
                    yield {"type": "chunk", "content": token}
                answer_text = sanitize_assistant_reply("".join(parts))
                if not answer_text:
                    fallback = format_tool_steps_answer(question, payload)
                    if fallback:
                        parts = [fallback]
                        yield {"type": "chunk", "content": fallback}

            yield _done_event(
                "".join(parts),
                tool_name=tools_used[-1] if tools_used else None,
                citations=citations,
                tools_used=tools_used,
                question=question,
            )
