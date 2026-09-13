from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_session_service
from src.core.rate_limit import limiter
from src.db import get_db
from src.models.complaint import Complaint
from src.models.session import ChatSession
from src.models.user import User
from src.services.complaint_service import ticket_id_for
from src.services.session_service import SessionService
from src.tools import order

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/me/orders")
@limiter.limit("30/minute")
async def my_orders(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await order.list_my_orders(user_id=user.id, email=user.email)
    return result.to_dict()


@router.get("/me/sessions")
@limiter.limit("30/minute")
async def my_sessions(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    limit: int = 50,
    offset: int = 0,
):
    rows, total = await sessions.list_sessions_for_user(
        db, user.id, limit=min(max(limit, 1), 100), offset=max(offset, 0)
    )
    return {
        "items": [
            {
                "id": s.id,
                "status": s.status,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in rows
        ],
        "total": total,
    }


@router.get("/me/tickets")
@limiter.limit("30/minute")
async def my_tickets(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
):
    session_ids = list(
        (
            await db.scalars(
                select(ChatSession.id).where(ChatSession.user_id == user.id)
            )
        ).all()
    )
    if not session_ids:
        return {"items": [], "total": 0}

    rows = list(
        (
            await db.scalars(
                select(Complaint)
                .where(Complaint.session_id.in_(session_ids))
                .order_by(Complaint.created_at.desc())
                .limit(min(max(limit, 1), 100))
            )
        ).all()
    )
    items = [
        {
            "ticket_id": ticket_id_for(c.id),
            "id": c.id,
            "status": c.status,
            "details": (c.details or "")[:200],
            "is_handoff": (c.details or "").startswith("[转人工]"),
            "session_id": c.session_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in rows
    ]
    return {"items": items, "total": len(items)}


@router.get("/me/tickets/{ticket_id}")
@limiter.limit("30/minute")
async def my_ticket_detail(
    ticket_id: str,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from src.tools.complaint import query_work_order

    result = await query_work_order(
        ticket_id, db=db, user_id=user.id, require_owner=True
    )
    if not result.success:
        raise HTTPException(status_code=404, detail=result.error or "工单不存在")
    return result.to_dict()
