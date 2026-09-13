"""「我的订单」意图：直接查单并模板回复，绝不走 ReAct / LLM。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from src.services.workflows.fast_route import (
    extract_ticket_id,
    extract_tracking_number,
)
from src.services.workflows.return_workflow import extract_order_id
from src.utils.tool_answer import format_tool_steps_answer

if TYPE_CHECKING:
    from src.services.agent_service import AgentService
    from sqlalchemy.ext.asyncio import AsyncSession

_MY_ORDERS_RE = re.compile(
    r"(我的订单|全部订单|订单列表|有哪些订单|看看订单|查(一?下)?(我的)?订单|"
    r"订单(呢|到哪|状态)|买过什么|下过的单|"
    r"my\s+orders?|list\s+(my\s+)?orders?|what\s+orders?|"
    r"show\s+(my\s+)?orders?|order\s+list)",
    re.IGNORECASE,
)


def is_my_orders_intent(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    # 带具体单号/运单/工单时交给更精确的路由
    if extract_order_id(text) or extract_tracking_number(text) or extract_ticket_id(text):
        return False
    return bool(_MY_ORDERS_RE.search(text))


async def try_handle_my_orders_intent(
    message: str,
    agent: "AgentService",
    *,
    user_id: str | None,
    user_email: str | None,
    db: AsyncSession | None = None,
) -> Any | None:
    """检测到「我的订单」时直接查单，跳过上下文加载与模型调用。"""
    from src.services.agent_service import AgentResponse

    if not is_my_orders_intent(message):
        return None

    result = await agent._call_tool(
        "query_my_orders",
        {},
        db=db,
        user_id=user_id,
        user_email=user_email,
    )
    answer = format_tool_steps_answer(
        message,
        [{"tool": "query_my_orders", "input": {}, "result": result}],
    )
    if not answer:
        answer = (result or {}).get("error") or "暂时查不到订单，请稍后重试或先登录。"

    return AgentResponse(
        answer=answer,
        tool_name="query_my_orders",
        tool_result=result,
        tools_used=["query_my_orders"],
    )
