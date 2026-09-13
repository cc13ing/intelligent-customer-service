from __future__ import annotations

from typing import TYPE_CHECKING

from src.rag.prompts import detect_user_language
from src.tools import ToolResult

if TYPE_CHECKING:
    from src.rag.retriever import Retriever
    from src.services.llm.kimi_client import KimiClient
    from src.services.llm.local_qwen import LocalQwenService


def _policy_answer(query: str, order_id: str | None) -> str:
    """退换货固定政策模板（不走投诉工单、不与投诉话术混用）。"""
    lang = detect_user_language(query)
    oid = (order_id or "").strip()
    oid_line_zh = f"已关联订单 **{oid}**。" if oid else "请先提供订单号（如 ORD-1001）。"
    oid_line_en = (
        f"Linked order **{oid}**."
        if oid
        else "Please provide your order ID (e.g. ORD-1001)."
    )

    if lang == "en":
        return (
            "Return / refund (not a complaint ticket):\n"
            f"- {oid_line_en}\n"
            "- Window: within **15 days** of delivery for unopened items with accessories.\n"
            "- Quality issues: exchange/repair within warranty; describe the defect.\n"
            "- Shipping: buyer pays for non-quality returns; we cover quality-related returns.\n"
            "- Refund: **5–10 business days** after warehouse inspection (original payment method).\n"
            "- Not eligible: activated devices, physical damage, missing accessories.\n"
            "Next: confirm order ID + reason (unwanted / quality / wrong item). "
            "This is a **return request**, not a service complaint."
        )

    return (
        "【退货 / 退款指引】（这不是投诉工单）\n"
        f"- {oid_line_zh}\n"
        "- 无理由退货：签收后 **15 天内**，未拆封且配件齐全。\n"
        "- 质量问题：质保期内可换货/维修，请说明故障现象。\n"
        "- 运费：非质量问题由买家承担；质量问题由平台承担。\n"
        "- 退款：仓库验收后 **5–10 个工作日** 原路退回。\n"
        "- 不支持：已激活设备、人为损坏、缺少原装配件。\n"
        "下一步：确认订单号 + 退货原因（不想要 / 质量问题 / 发错货）。"
        "本流程只处理退换货，服务质量投诉请另说「我要投诉」。"
    )


async def create_return_request(
    query: str,
    retriever: Retriever | None = None,
    order_id: str | None = None,
    qwen: LocalQwenService | None = None,
    kimi: KimiClient | None = None,
) -> ToolResult:
    """退换货/退款：输出明确政策模板，与投诉工单完全分离。"""
    oid = (order_id or "").strip() or None
    answer = _policy_answer(query or "", oid)
    return ToolResult(
        success=True,
        data={
            "answer": answer,
            "order_id": oid,
            "return_request": True,
            "kind": "return_refund",
            "message": answer,
        },
        citations=["returns_refund.txt"],
    )
