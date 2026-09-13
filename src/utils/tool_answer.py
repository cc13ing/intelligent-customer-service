"""Format tool results into user-facing text when LLM synthesis is empty."""

from __future__ import annotations

from typing import Any

from src.rag.prompts import detect_user_language


def _step_data(step: dict) -> dict[str, Any]:
    result = step.get("result") or {}
    return result.get("data") or {}


def format_tool_steps_answer(question: str, steps: list[dict]) -> str:
    """Build a concise answer from tool step payloads (no LLM)."""
    if not steps:
        return ""

    lang = detect_user_language(question)
    order_step = next((s for s in steps if s.get("tool") == "query_order"), None)
    logistics_step = next(
        (s for s in steps if s.get("tool") == "fetch_logistics_information"), None
    )
    my_orders_step = next((s for s in steps if s.get("tool") == "query_my_orders"), None)
    complaint_step = next((s for s in steps if s.get("tool") == "record_user_complaint"), None)
    work_order_step = next((s for s in steps if s.get("tool") == "query_work_order"), None)

    if order_step and logistics_step:
        od = _step_data(order_step)
        ld = _step_data(logistics_step)
        if lang == "en":
            return (
                f"Behold—intelligence gathered.\n"
                f"Order {od.get('order_id', '')}: status {od.get('status', '')}, "
                f"carrier {od.get('carrier', '')}, tracking {od.get('tracking_number', '')}.\n"
                f"Logistics: {ld.get('status', '')}. "
                f"Now at {ld.get('current_location', '')}. "
                f"ETA {ld.get('estimated_delivery', '')}. Victory is mine."
            )
        return (
            f"情报到手。本饺子已查清。\n"
            f"订单 {od.get('order_id', '')}：状态 {od.get('status', '')}，"
            f"承运商 {od.get('carrier', '')}，运单号 {od.get('tracking_number', '')}。\n"
            f"物流状态：{ld.get('status', '')}，"
            f"当前位置 {ld.get('current_location', '')}，"
            f"预计送达 {ld.get('estimated_delivery', '')}。胜利属于饺子。"
        )

    if logistics_step and not order_step:
        ld = _step_data(logistics_step)
        tracking = ld.get("logistics_number", "")
        if lang == "en":
            return (
                f"Your parcel, dissected. Tracking {tracking}: {ld.get('status', '')}. "
                f"Carrier {ld.get('carrier', '')}. "
                f"Location: {ld.get('current_location', '')}. "
                f"ETA: {ld.get('estimated_delivery', '')}."
            )
        return (
            f"包裹行踪已在本饺子掌握之中。运单 {tracking}：{ld.get('status', '')}，"
            f"承运商 {ld.get('carrier', '')}，"
            f"当前位置 {ld.get('current_location', '')}，"
            f"预计送达 {ld.get('estimated_delivery', '')}。"
            + (
                f" 已写入我的订单 {ld.get('my_order', {}).get('order_id', '')}。"
                if ld.get("my_order")
                else ""
            )
        )

    if order_step:
        od = _step_data(order_step)
        if lang == "en":
            return (
                f"As predicted. Order {od.get('order_id', '')}: "
                f"status {od.get('status', '')}, "
                f"carrier {od.get('carrier', '')}, "
                f"tracking {od.get('tracking_number', '')}."
            )
        return (
            f"正如本饺子所料。订单 {od.get('order_id', '')}："
            f"状态 {od.get('status', '')}，"
            f"承运商 {od.get('carrier', '')}，运单号 {od.get('tracking_number', '')}。"
        )

    if my_orders_step:
        data = _step_data(my_orders_step)
        result = my_orders_step.get("result") or {}
        if result.get("login_required") or result.get("error"):
            return str(result.get("error") or (
                "请先登录后再查订单。"
                if lang != "en"
                else "Please sign in to view your orders."
            ))
        orders = data.get("orders") or []
        if not orders:
            return (
                "荒唐——订单库空空如也。先登录，再让本饺子施展才华。"
                if lang != "en"
                else "What the deuce—no orders. Sign in first, then let Jiaozi work."
            )
        lines = []
        for o in orders[:5]:
            lines.append(
                f"{o.get('order_id', '')} | {o.get('status', '')} | "
                f"{o.get('carrier', '')} {o.get('tracking_number', '')}"
            )
        header = "本饺子翻出的订单如下：" if lang != "en" else "Behold—your orders:"
        return header + "\n" + "\n".join(lines)

    if complaint_step:
        data = _step_data(complaint_step)
        return data.get("message", "") or (
            "您的投诉已受理，请留意工单进度通知。"
            if lang != "en"
            else "Your complaint has been accepted. Please check the ticket status for updates."
        )

    return_step = next((s for s in steps if s.get("tool") == "create_return_request"), None)
    if return_step:
        data = _step_data(return_step)
        return data.get("answer") or data.get("message") or (
            "【退货/退款】请提供订单号以便继续。"
            if lang != "en"
            else "[Return/refund] Please provide your order ID."
        )

    if work_order_step:
        data = _step_data(work_order_step)
        msg = data.get("message") or ""
        if msg:
            return msg
        if lang == "en":
            return (
                f"Ticket {data.get('ticket_id', '')}: status {data.get('status', '')}. "
                f"{'Handoff.' if data.get('is_handoff') else ''}"
            )
        return (
            f"工单 {data.get('ticket_id', '')}：状态 {data.get('status', '')}。"
            f"{'（转人工）' if data.get('is_handoff') else ''}"
        )

    # Generic: use embedded answer from RAG-like tools
    for step in steps:
        data = _step_data(step)
        if data.get("answer"):
            return str(data["answer"])

    # Failed structured tools: surface error
    for step in steps:
        result = step.get("result") or {}
        if result.get("error"):
            return str(result["error"])

    return ""
