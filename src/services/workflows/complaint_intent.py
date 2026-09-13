"""投诉意图：强制走建工单，避免落到 customer_chat / 退款话术。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from src.rag.prompts import detect_user_language

if TYPE_CHECKING:
    from src.services.agent_service import AgentService
    from src.services.session_service import SessionService
    from sqlalchemy.ext.asyncio import AsyncSession

# 明确投诉/服务态度；不含「退款」「退货」主意图
_COMPLAINT_RE = re.compile(
    r"(投诉|举报|态度\s*差|服务\s*差|骗子|欺诈|差评|"
    r"complaint|complain|terrible\s+service|bad\s+service|fraud)",
    re.IGNORECASE,
)

# 若同时强退换货意图，交给退货流程优先
_RETURN_PRIORITY_RE = re.compile(
    r"(退货|退款|换货|退换货|return|refund|exchange|money\s*back)",
    re.IGNORECASE,
)


def is_complaint_intent(message: str) -> bool:
    text = message or ""
    if not _COMPLAINT_RE.search(text):
        return False
    # 「退款失败要投诉」仍算投诉；纯「我要退款」不算
    if _RETURN_PRIORITY_RE.search(text) and not re.search(
        r"(投诉|举报|complaint|complain)", text, re.IGNORECASE
    ):
        return False
    return True


async def try_handle_complaint_intent(
    message: str,
    agent: "AgentService",
    *,
    session_id: str | None,
    db: AsyncSession | None,
    sessions: SessionService | None = None,
) -> Any | None:
    """检测到投诉意图时直接建工单，跳过 ReAct / RAG。"""
    from src.services.agent_service import AgentResponse

    if not is_complaint_intent(message):
        return None

    result = await agent._call_tool(
        "record_user_complaint",
        {"complaint_details": message.strip()},
        session_id=session_id,
        db=db,
    )
    data = (result or {}).get("data") or {}
    answer = data.get("message") or ""
    if not answer:
        lang = detect_user_language(message)
        ticket = data.get("ticket_id", "TKT-PENDING")
        answer = (
            f"Your complaint has been accepted.\n"
            f"Ticket ID: {ticket}\n"
            f"Status: Received\n"
            f"We will provide an initial response within 24 hours. "
            f"Complex cases are typically resolved within 3–5 business days.\n"
            f"To check progress, reply: query ticket {ticket}"
            if lang == "en"
            else (
                f"您的投诉已受理。\n"
                f"工单编号：{ticket}\n"
                f"当前状态：Received（已接收）\n"
                f"处理说明：我们将在 24 小时内给出首次回复；"
                f"如需进一步核查，通常在 3–5 个工作日内反馈处理结果。\n"
                f"进度查询：请回复「查询工单 {ticket}」。"
            )
        )

    return AgentResponse(
        answer=answer,
        tool_name="record_user_complaint",
        tool_result=result,
        tools_used=["record_user_complaint"],
    )
