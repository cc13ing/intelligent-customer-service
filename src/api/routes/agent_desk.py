"""人工客服工作台 API + WebSocket。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_session_service
from src.api.routes.admin_auth import verify_admin_access
from src.core.security import validate_session_id
from src.db import get_db
from src.models.complaint import Complaint
from src.models.session import ChatSession
from src.models.user import User
from src.services.complaint_service import ticket_id_for, update_complaint_status
from src.services.handoff_bridge import get_handoff_bridge
from src.services.order_store import get_order_store
from src.services.session_service import SessionService

logger = structlog.get_logger()
router = APIRouter(prefix="/api/v1/agent", tags=["agent-desk"])

CLAIM_TIMEOUT_MINUTES = 15


class ClaimRequest(BaseModel):
    agent_name: str = Field(default="人工客服", max_length=64)


class ResolveRequest(BaseModel):
    agent_name: str = Field(default="人工客服", max_length=64)
    note: str = Field(default="", max_length=500)


def _is_handoff(details: str) -> bool:
    return (details or "").lstrip().startswith("[转人工]")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _wait_seconds(created_at: datetime | None) -> int:
    dt = _aware(created_at)
    if not dt:
        return 0
    return max(0, int((_utcnow() - dt).total_seconds()))


async def _auto_release_stale(db: AsyncSession) -> list[str]:
    """超时未活跃的认领自动退回队列。"""
    bridge = get_handoff_bridge()
    result = await db.execute(select(Complaint).where(Complaint.status == "InReview"))
    released: list[str] = []
    now = _utcnow()
    for c in result.scalars().all():
        if not _is_handoff(c.details or ""):
            continue
        claimed = _aware(c.claimed_at) or _aware(c.created_at)
        if not claimed:
            continue
        age_min = (now - claimed).total_seconds() / 60
        sid = c.session_id or ""
        # 坐席仍在线则不释放；超时且离线才退回
        if age_min < CLAIM_TIMEOUT_MINUTES:
            continue
        if sid and bridge.has_agent_online(sid):
            continue
        c.status = "Received"
        c.assigned_agent = None
        c.claimed_at = None
        if sid and validate_session_id(sid):
            session = await db.scalar(select(ChatSession).where(ChatSession.id == sid))
            if session and session.status == "human":
                session.status = "waiting_human"
            await bridge.enqueue(sid, c.id)
        released.append(ticket_id_for(c.id))
        logger.info("handoff_auto_released", ticket=ticket_id_for(c.id), age_min=round(age_min, 1))
    if released:
        await db.commit()
        bridge.bump_queue()
    return released


@router.get("/queue")
async def agent_queue(
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str = "Received",
):
    """转人工排队列表（含等待时长、未读、自动释放超时认领）。"""
    released = await _auto_release_stale(db)
    bridge = get_handoff_bridge()
    result = await db.execute(select(Complaint).order_by(Complaint.created_at.desc()).limit(200))
    rows = result.scalars().all()
    items = []
    waiting_count = 0
    for c in rows:
        if not _is_handoff(c.details):
            continue
        if c.status == "Received":
            waiting_count += 1
        if status and c.status != status:
            continue
        sid = c.session_id or ""
        items.append(
            {
                "id": c.id,
                "ticket_id": ticket_id_for(c.id),
                "session_id": c.session_id,
                "details": c.details,
                "status": c.status,
                "assigned_agent": c.assigned_agent,
                "claimed_at": c.claimed_at.isoformat() if c.claimed_at else None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "wait_seconds": _wait_seconds(c.created_at),
                "unread": bridge.get_unread(sid) if sid else 0,
                "agent_online": bridge.has_agent_online(sid) if sid else False,
            }
        )
    return {
        "items": items,
        "total": len(items),
        "waiting_count": waiting_count,
        "queue_version": bridge.queue_version,
        "claim_timeout_minutes": CLAIM_TIMEOUT_MINUTES,
        "auto_released": released,
    }


@router.post("/complaints/{complaint_id}/claim")
async def claim_ticket(
    complaint_id: str,
    body: ClaimRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    _: Annotated[str, Depends(verify_admin_access)],
):
    """坐席认领工单，进入人工对话模式。"""
    await _auto_release_stale(db)
    complaint = await db.scalar(select(Complaint).where(Complaint.id == complaint_id))
    if not complaint or not _is_handoff(complaint.details or ""):
        raise HTTPException(status_code=404, detail="转人工工单不存在")
    if not complaint.session_id or not validate_session_id(complaint.session_id):
        raise HTTPException(status_code=400, detail="工单缺少有效会话，无法对接聊天")
    if complaint.status == "InReview" and complaint.assigned_agent:
        if complaint.assigned_agent != body.agent_name:
            raise HTTPException(
                status_code=409,
                detail=f"已被 {complaint.assigned_agent} 接手",
            )

    if complaint.status == "Received":
        await update_complaint_status(db, complaint_id, "InReview")
    complaint.assigned_agent = body.agent_name.strip() or "人工客服"
    complaint.claimed_at = _utcnow()

    session = await db.scalar(select(ChatSession).where(ChatSession.id == complaint.session_id))
    if not session:
        # 会话被清档但工单仍引用：补建空会话，避免写消息外键失败
        session = ChatSession(id=complaint.session_id, user_id=None, status="human")
        db.add(session)
        await db.flush()
    else:
        session.status = "human"

    bridge = get_handoff_bridge()
    await bridge.enqueue(complaint.session_id, complaint.id)
    await bridge.claim(complaint.session_id, complaint.assigned_agent)

    await sessions.add_message(
        db,
        complaint.session_id,
        "assistant",
        f"人工客服「{complaint.assigned_agent}」已接入，接下来由真人帮你处理。",
    )
    await db.commit()

    await bridge.push_user(
        complaint.session_id,
        {
            "type": "agent_joined",
            "session_id": complaint.session_id,
            "agent_name": complaint.assigned_agent,
            "ticket_id": ticket_id_for(complaint.id),
            "message": f"人工客服「{complaint.assigned_agent}」已接入",
        },
    )

    return {
        "ok": True,
        "complaint_id": complaint.id,
        "ticket_id": ticket_id_for(complaint.id),
        "session_id": complaint.session_id,
        "agent_name": complaint.assigned_agent,
        "status": complaint.status,
        "claimed_at": complaint.claimed_at.isoformat() if complaint.claimed_at else None,
    }


@router.post("/complaints/{complaint_id}/release")
async def release_ticket(
    complaint_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_admin_access)],
):
    """主动退回排队（未解决）。"""
    complaint = await db.scalar(select(Complaint).where(Complaint.id == complaint_id))
    if not complaint:
        raise HTTPException(status_code=404, detail="工单不存在")
    complaint.status = "Received"
    complaint.assigned_agent = None
    complaint.claimed_at = None
    sid = complaint.session_id
    if sid and validate_session_id(sid):
        session = await db.scalar(select(ChatSession).where(ChatSession.id == sid))
        if session:
            session.status = "waiting_human"
        await get_handoff_bridge().enqueue(sid, complaint.id)
    await db.commit()
    return {"ok": True, "status": "Received"}


@router.post("/complaints/{complaint_id}/resolve")
async def resolve_ticket(
    complaint_id: str,
    body: ResolveRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    _: Annotated[str, Depends(verify_admin_access)],
):
    """结束人工，交回机器人。"""
    complaint = await db.scalar(select(Complaint).where(Complaint.id == complaint_id))
    if not complaint:
        raise HTTPException(status_code=404, detail="工单不存在")
    await update_complaint_status(db, complaint_id, "Resolved")
    complaint.claimed_at = None

    note = (body.note or "").strip()
    sid = complaint.session_id
    if sid and validate_session_id(sid):
        session = await db.scalar(select(ChatSession).where(ChatSession.id == sid))
        if not session:
            session = ChatSession(id=sid, user_id=None, status="active")
            db.add(session)
            await db.flush()
        else:
            session.status = "active"
        closing = f"人工客服「{body.agent_name}」已结束服务，饺子继续为你效劳。"
        if note:
            closing += f"\n备注：{note}"
        await sessions.add_message(db, sid, "assistant", closing)
        await db.commit()
        bridge = get_handoff_bridge()
        await bridge.push_user(
            sid,
            {
                "type": "agent_left",
                "session_id": sid,
                "message": closing,
            },
        )
        await bridge.release(sid)
    else:
        await db.commit()

    return {"ok": True, "status": "Resolved"}


@router.get("/sessions/{session_id}/messages")
async def agent_session_messages(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    _: Annotated[str, Depends(verify_admin_access)],
):
    if not validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    msgs = await sessions.get_messages(db, session_id, limit=100)
    await get_handoff_bridge().mark_read(session_id)
    return {
        "session_id": session_id,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in msgs
        ],
    }


@router.get("/sessions/{session_id}/context")
async def agent_session_context(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    _: Annotated[str, Depends(verify_admin_access)],
):
    """坐席上下文卡片：用户、订单、近期对话与工具摘要。"""
    if not validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    session = await sessions.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    user_info = None
    email = None
    if session.user_id:
        user = await db.scalar(select(User).where(User.id == session.user_id))
        if user:
            email = user.email
            user_info = {"id": user.id, "email": user.email, "name": user.name}

    orders: list[dict] = []
    store = get_order_store()
    if email:
        for o in store.list_by_email(email)[:8]:
            orders.append(
                {
                    "order_id": o.get("order_id"),
                    "status": o.get("status"),
                    "total": o.get("total"),
                    "currency": o.get("currency"),
                    "tracking_number": o.get("tracking_number"),
                    "carrier": o.get("carrier"),
                }
            )

    msgs = await sessions.get_messages(db, session_id, limit=80)
    tools: list[str] = []
    for m in msgs:
        tc = m.tool_calls or {}
        if isinstance(tc.get("tools"), list):
            for t in tc["tools"]:
                if t and t not in tools:
                    tools.append(str(t))
        elif tc.get("tool") and str(tc["tool"]) not in tools:
            tools.append(str(tc["tool"]))

    recent = [
        {
            "role": m.role,
            "content": (m.content or "")[:220],
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in msgs[-10:]
    ]
    summary = await sessions._get_session_summary(session_id)
    if not summary and msgs:
        bits = []
        for m in msgs[-6:]:
            bits.append(f"{m.role}: {(m.content or '')[:80]}")
        summary = "\n".join(bits)

    complaint = await db.scalar(
        select(Complaint)
        .where(Complaint.session_id == session_id)
        .order_by(Complaint.created_at.desc())
        .limit(1)
    )

    return {
        "session": {
            "id": session.id,
            "status": session.status,
            "user_id": session.user_id,
            "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        },
        "user": user_info,
        "orders": orders,
        "tools_used": tools,
        "summary": summary,
        "recent_messages": recent,
        "ticket": (
            {
                "id": complaint.id,
                "ticket_id": ticket_id_for(complaint.id),
                "details": complaint.details,
                "status": complaint.status,
                "assigned_agent": complaint.assigned_agent,
            }
            if complaint
            else None
        ),
        "unread": get_handoff_bridge().get_unread(session_id),
    }


@router.post("/sessions/{session_id}/read")
async def mark_session_read(
    session_id: str,
    _: Annotated[str, Depends(verify_admin_access)],
):
    if not validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    await get_handoff_bridge().mark_read(session_id)
    return {"ok": True, "unread": 0}


@router.websocket("/ws/desk")
async def agent_desk_ws(websocket: WebSocket):
    """坐席 WebSocket：attach 会话后收发消息。"""
    from src.core.config import get_settings
    from src.core.security import check_api_key

    api_key = websocket.query_params.get("api_key") or websocket.headers.get("x-api-key")
    settings = get_settings()
    if not check_api_key(api_key, settings):
        await websocket.close(code=1008, reason="Unauthorized")
        return

    await websocket.accept()
    bridge = get_handoff_bridge()
    attached: set[str] = set()
    agent_name = websocket.query_params.get("agent") or "人工客服"

    from src.db import async_session_factory

    sessions: SessionService = websocket.app.state.session_service

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "error": "Invalid JSON"})
                continue

            msg_type = data.get("type")
            if msg_type in {"ping", "pong"}:
                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                continue

            if msg_type == "attach":
                sid = data.get("session_id")
                if not sid or not validate_session_id(sid):
                    await websocket.send_json({"type": "error", "error": "Invalid session_id"})
                    continue
                await bridge.bind_agent(sid, websocket)
                attached.add(sid)
                await websocket.send_json(
                    {"type": "attached", "session_id": sid, "unread": 0}
                )
                continue

            if msg_type == "detach":
                sid = data.get("session_id")
                if sid in attached:
                    await bridge.unbind_agent(sid, websocket)
                    attached.discard(sid)
                await websocket.send_json({"type": "detached", "session_id": sid})
                continue

            if msg_type == "read":
                sid = data.get("session_id")
                if sid and validate_session_id(sid):
                    await bridge.mark_read(sid)
                    await websocket.send_json({"type": "read", "session_id": sid, "unread": 0})
                continue

            if msg_type == "message":
                sid = data.get("session_id")
                content = (data.get("content") or "").strip()
                if not sid or not validate_session_id(sid) or not content:
                    await websocket.send_json({"type": "error", "error": "session_id/content required"})
                    continue
                if sid not in attached:
                    await bridge.bind_agent(sid, websocket)
                    attached.add(sid)

                async with async_session_factory() as db:
                    await sessions.add_message(db, sid, "agent", content)
                    # 刷新认领时间，避免超时误释放
                    complaint = await db.scalar(
                        select(Complaint)
                        .where(Complaint.session_id == sid, Complaint.status == "InReview")
                        .order_by(Complaint.created_at.desc())
                        .limit(1)
                    )
                    if complaint:
                        complaint.claimed_at = _utcnow()
                    await db.commit()

                payload = {
                    "type": "agent_message",
                    "session_id": sid,
                    "agent_name": data.get("agent_name") or agent_name,
                    "content": content,
                }
                await bridge.push_user(sid, payload)
                await bridge.mark_read(sid)
                await websocket.send_json({"type": "sent", "session_id": sid})
                continue

            await websocket.send_json({"type": "error", "error": f"Unknown type: {msg_type}"})
    except WebSocketDisconnect:
        pass
    finally:
        for sid in list(attached):
            await bridge.unbind_agent(sid, websocket)
