from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from src.core.config import get_settings
from src.core.metrics import COMPLAINTS_RECORDED
from src.tools import ToolResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

COMPLAINT_STATUSES = ("Received", "InReview", "Resolved")


async def record_user_complaint(
    complaint_details: str,
    session_id: str | None = None,
    db: AsyncSession | None = None,
) -> ToolResult:
    settings = get_settings()
    record = {
        "complaint_details": complaint_details,
        "complaint_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "Received",
        "session_id": session_id,
    }

    if db is not None:
        from src.models.complaint import Complaint

        try:
            complaint = Complaint(
                session_id=session_id,
                details=complaint_details,
                status="Received",
            )
            db.add(complaint)
            await db.flush()
            record["id"] = str(complaint.id)
            record["ticket_id"] = f"TKT-{str(complaint.id).replace('-', '')[:8].upper()}"
        except Exception as exc:
            await db.rollback()
            logger.warning("complaint_db_failed", error=str(exc))

    if settings.complaint_webhook_url:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(settings.complaint_webhook_url, json=record)
        except Exception as exc:
            logger.warning("complaint_webhook_failed", error=str(exc))

    ticket_id = record.get("ticket_id", "TKT-PENDING")
    COMPLAINTS_RECORDED.inc()
    return ToolResult(
        success=True,
        data={
            "message": (
                f"您的投诉已受理。\n"
                f"工单编号：{ticket_id}\n"
                f"当前状态：Received（已接收）\n"
                f"处理说明：我们将在 24 小时内给出首次回复；"
                f"如需进一步核查，通常在 3–5 个工作日内反馈处理结果。\n"
                f"进度查询：请回复「查询工单 {ticket_id}」。"
            ),
            "ticket_id": ticket_id,
            "status": "Received",
            "kind": "complaint",
            "status_workflow": list(COMPLAINT_STATUSES),
            "record": record,
        },
    )


def _normalize_ticket_id(ticket_id: str) -> str:
    raw = (ticket_id or "").strip().upper()
    if raw.startswith("TKT-"):
        return raw
    return f"TKT-{raw}"


async def query_work_order(
    ticket_id: str,
    *,
    db: AsyncSession | None = None,
    user_id: str | None = None,
    require_owner: bool = False,
) -> ToolResult:
    """按工单号查询投诉/转人工工单详情。"""
    from sqlalchemy import select

    from src.models.complaint import Complaint
    from src.models.session import ChatSession
    from src.services.complaint_service import ticket_id_for

    if db is None:
        return ToolResult(success=False, error="工单查询服务暂不可用")

    wanted = _normalize_ticket_id(ticket_id)
    prefix = wanted.replace("TKT-", "")[:8]
    if len(prefix) < 4:
        return ToolResult(success=False, error="工单号格式不正确，示例：TKT-XXXXXXXX")

    rows = list((await db.scalars(select(Complaint).order_by(Complaint.created_at.desc()).limit(500))).all())
    match: Complaint | None = None
    for c in rows:
        if ticket_id_for(c.id) == wanted or str(c.id).replace("-", "")[:8].upper() == prefix:
            match = c
            break

    if not match:
        return ToolResult(success=False, error=f"未找到工单 {wanted}")

    if require_owner or user_id:
        if not match.session_id:
            if require_owner:
                return ToolResult(success=False, error="无权查看该工单")
        else:
            session = await db.scalar(
                select(ChatSession).where(ChatSession.id == match.session_id)
            )
            if user_id and session and session.user_id and session.user_id != user_id:
                return ToolResult(success=False, error="无权查看该工单")
            if require_owner and (not session or session.user_id != user_id):
                return ToolResult(success=False, error="无权查看该工单")

    details = match.details or ""
    return ToolResult(
        success=True,
        data={
            "ticket_id": ticket_id_for(match.id),
            "id": match.id,
            "status": match.status,
            "details": details,
            "is_handoff": details.startswith("[转人工]"),
            "assigned_agent": match.assigned_agent,
            "session_id": match.session_id,
            "created_at": match.created_at.isoformat() if match.created_at else None,
            "message": (
                f"工单 {ticket_id_for(match.id)} 当前状态：{match.status}。"
                f"{'（转人工）' if details.startswith('[转人工]') else ''}"
            ),
        },
    )
