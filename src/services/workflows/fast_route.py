"""规则快路径：常见意图直接选工具，跳过 ReAct 选工具的 LLM 往返。"""

from __future__ import annotations

import re

from src.services.workflows.return_workflow import extract_order_id, is_return_intent

# 工单号
_TICKET_RE = re.compile(r"\b(TKT-[A-Z0-9]+)\b", re.IGNORECASE)
_TICKET_QUERY_RE = re.compile(
    r"(查询?\s*工单|工单\s*(进度|状态|查询?)|query\s+ticket|ticket\s+status)",
    re.IGNORECASE,
)

# 物流单号：有明确前缀，或「运单/快递/物流」后跟较长编号
_TRACKING_LABELED_RE = re.compile(
    r"(?:运单|快递|物流|单号|tracking(?:\s*number)?|shipment)[:：\s#]*([A-Za-z0-9-]{8,32})",
    re.IGNORECASE,
)
_TRACKING_CARRIER_RE = re.compile(
    r"\b((?:SF|YT|YD|ZT|STO|YTO|JD|DHL|UPS|FEDEX)[A-Za-z0-9]{6,28})\b",
    re.IGNORECASE,
)

_LOGISTICS_ASK_RE = re.compile(
    r"(物流|快递|运单|包裹|到哪|到哪了|tracking|shipment|delivery|shipping)",
    re.IGNORECASE,
)
_KNOWLEDGE_RE = re.compile(
    r"(保修|质保|保固|warranty|guarantee|"
    r"怎么(用|设置|操作)|如何(用|设置|操作)|"
    r"支付方式|运费|发货|售后政策|使用说明|"
    r"how\s+to|shipping\s+fee|payment\s+method)",
    re.IGNORECASE,
)


def extract_ticket_id(message: str) -> str | None:
    match = _TICKET_RE.search(message or "")
    return match.group(1).upper() if match else None


def extract_tracking_number(message: str) -> str | None:
    text = message or ""
    labeled = _TRACKING_LABELED_RE.search(text)
    if labeled:
        return labeled.group(1).strip()
    carrier = _TRACKING_CARRIER_RE.search(text)
    if carrier:
        return carrier.group(1).strip()
    return None


def resolve_fast_route(question: str) -> list[tuple[str, dict]] | None:
    """
    若能用规则确定工具，返回 [(tool_name, args), ...]；
    否则返回 None，交给 ReAct。
    「我的订单」由 my_orders_intent 更早短路，这里不再重复。
    """
    text = (question or "").strip()
    if not text:
        return None

    # 退换货交给专用 workflow，这里不抢
    if is_return_intent(text) and not extract_order_id(text):
        return None

    ticket_id = extract_ticket_id(text)
    if ticket_id:
        return [("query_work_order", {"ticket_id": ticket_id})]
    if _TICKET_QUERY_RE.search(text) and not ticket_id:
        return None

    tracking = extract_tracking_number(text)
    order_id = extract_order_id(text)

    if tracking and not order_id:
        return [("fetch_logistics_information", {"logistics_number": tracking})]

    if order_id:
        steps: list[tuple[str, dict]] = [("query_order", {"order_id": order_id})]
        if tracking:
            steps.append(
                ("fetch_logistics_information", {"logistics_number": tracking})
            )
        elif _LOGISTICS_ASK_RE.search(text):
            pass
        return steps

    if _KNOWLEDGE_RE.search(text):
        return [("customer_chat", {"query": text})]

    return None
