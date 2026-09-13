from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.app_state import refresh_app_retriever
from src.core.config import get_settings
from src.api.routes.admin_auth import verify_admin_access
from src.db import get_db
from src.models.complaint import Complaint
from src.models.knowledge import KnowledgeDoc
from src.rag.indexer import rebuild_index_async
from src.services.complaint_service import (
    ticket_id_for,
    update_complaint_status as apply_complaint_status,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class ComplaintStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(Received|InReview|Resolved)$")


@router.get("/knowledge")
async def list_knowledge(
  _: Annotated[str, Depends(verify_admin_access)],
  db: Annotated[AsyncSession, Depends(get_db)],
):
    settings = get_settings()
    files: list[dict] = []
    if settings.knowledge_dir.exists():
        for path in sorted(settings.knowledge_dir.rglob("*")):
            if path.suffix.lower() not in {".txt", ".md"}:
                continue
            stat = path.stat()
            files.append(
                {
                    "filename": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )

    result = await db.execute(select(KnowledgeDoc).order_by(KnowledgeDoc.indexed_at.desc()))
    docs = result.scalars().all()
    return {
        "files": files,
        "indexed_docs": [
            {
                "filename": d.filename,
                "chunk_count": d.chunk_count,
                "indexed_at": d.indexed_at.isoformat() if d.indexed_at else None,
            }
            for d in docs
        ],
    }


@router.delete("/knowledge/{filename}")
async def delete_knowledge_file(
    filename: str,
    request: Request,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings = get_settings()
    safe_name = Path(filename).name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")

    target = settings.knowledge_dir / safe_name
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    target.unlink()
    chunk_count = await rebuild_index_async(settings)
    refresh_app_retriever(request.app)
    logger.info("admin_knowledge_deleted", filename=safe_name, chunk_count=chunk_count)
    return {"filename": safe_name, "chunk_count": chunk_count, "message": "Deleted and reindexed"}


@router.get("/complaints")
async def list_complaints(
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
    offset: int = 0,
    kind: str = "all",
    status: str | None = None,
):
    """列出投诉/转人工工单。kind=all|handoff|complaint。"""
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    kind = (kind or "all").lower()
    query = select(Complaint).order_by(Complaint.created_at.desc())
    if status in ("Received", "InReview", "Resolved"):
        query = query.where(Complaint.status == status)
    result = await db.execute(query)
    rows = result.scalars().all()

    def _is_handoff(details: str) -> bool:
        return (details or "").lstrip().startswith("[转人工]")

    filtered = []
    for c in rows:
        handoff = _is_handoff(c.details)
        if kind == "handoff" and not handoff:
            continue
        if kind == "complaint" and handoff:
            continue
        filtered.append(c)

    total = len(filtered)
    page = filtered[offset : offset + limit]
    return {
        "items": [
            {
                "id": c.id,
                "session_id": c.session_id,
                "details": c.details,
                "status": c.status,
                "ticket_id": ticket_id_for(c.id),
                "assigned_agent": c.assigned_agent,
                "is_handoff": _is_handoff(c.details),
                "kind": "handoff" if _is_handoff(c.details) else "complaint",
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in page
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "kind": kind,
    }


@router.patch("/complaints/{complaint_id}")
async def update_complaint_status(
    complaint_id: str,
    body: ComplaintStatusUpdate,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        complaint = await apply_complaint_status(db, complaint_id, body.status)
    except LookupError:
        raise HTTPException(status_code=404, detail="Complaint not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "id": complaint.id,
        "status": complaint.status,
        "ticket_id": ticket_id_for(complaint.id),
    }


@router.get("/orders")
async def list_orders(
    _: Annotated[str, Depends(verify_admin_access)],
    limit: int = 200,
):
    """列出当前内存中的订单（来自 CSV 热更新仓库）。"""
    from src.services.order_store import get_order_store

    settings = get_settings()
    store = get_order_store()
    files: list[dict] = []
    if settings.orders_dir.exists():
        for path in sorted(settings.orders_dir.glob("*.csv")):
            stat = path.stat()
            files.append(
                {
                    "filename": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    return {
        "stats": store.stats(),
        "files": files,
        "orders": store.list_all(limit=min(max(limit, 1), 500)),
    }


@router.post("/orders/upload")
async def upload_orders_csv(
    request: Request,
    file: UploadFile = File(...),
    db: Annotated[AsyncSession, Depends(get_db)] = ...,
    _: Annotated[str, Depends(verify_admin_access)] = ...,
    merge: bool = True,
):
    """上传 CSV 批量导入订单，并局部热更新内存仓库。

    merge=true（默认）：合并进现有数据，同 order_id 覆盖。
    merge=false：保存文件后全量从目录重载。
    """
    from src.core.app_state import refresh_app_order_store
    from src.services.order_persist import upsert_orders
    from src.services.order_store import get_order_store

    settings = get_settings()
    filename = Path(file.filename or "orders.csv").name
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="仅支持 .csv 文件")

    content = await file.read()
    if len(content) > settings.max_upload_bytes * 5:
        raise HTTPException(status_code=400, detail="文件过大")
    if not content.strip():
        raise HTTPException(status_code=400, detail="空文件")

    settings.orders_dir.mkdir(parents=True, exist_ok=True)
    target = settings.orders_dir / filename
    if target.exists():
        stem = target.stem
        target = settings.orders_dir / f"{stem}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    target.write_bytes(content)

    store = get_order_store()
    report: dict = {
        "imported": 0,
        "overwritten": 0,
        "skipped": [],
        "skipped_count": 0,
        "preview": [],
        "orders": [],
    }
    try:
        if merge:
            report = store.merge_csv_bytes(content, filename=target.name)
            imported = report["imported"]
            stats = refresh_app_order_store(request.app, full_reload=False)
            await upsert_orders(db, report.get("orders") or [])
        else:
            stats = refresh_app_order_store(request.app, full_reload=True)
            imported = stats.get("order_count", 0)
            report["imported"] = imported
            report["preview"] = [
                {
                    "order_id": o.get("order_id"),
                    "email": o.get("email") or "",
                    "status": o.get("status") or "",
                    "total": o.get("total"),
                    "tracking_number": o.get("tracking_number") or "",
                }
                for o in store.list_all(limit=8)
            ]
            await upsert_orders(db, store.list_all(limit=5000))
    except ValueError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "orders_csv_uploaded",
        filename=target.name,
        imported=imported,
        merge=merge,
        total=stats.get("order_count"),
    )
    return {
        "filename": target.name,
        "imported": imported,
        "overwritten": report.get("overwritten", 0),
        "skipped_count": report.get("skipped_count", 0),
        "skipped": report.get("skipped") or [],
        "preview": report.get("preview") or [],
        "merge": merge,
        "stats": stats,
        "message": (
            f"导入成功 {imported} 条"
            + (f"，覆盖 {report.get('overwritten', 0)} 条" if report.get("overwritten") else "")
            + (f"，跳过 {report.get('skipped_count', 0)} 行" if report.get("skipped_count") else "")
            + "，已热更新并落库"
        ),
    }


@router.post("/orders/reload")
async def reload_orders(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_admin_access)],
):
    """从 data/orders 目录全量重载 CSV（热更新），并同步落库。"""
    from src.core.app_state import refresh_app_order_store
    from src.services.order_persist import upsert_orders
    from src.services.order_store import get_order_store

    stats = refresh_app_order_store(request.app, full_reload=True)
    store = get_order_store()
    synced = await upsert_orders(db, store.list_all(limit=5000))
    return {
        "stats": stats,
        "synced": synced,
        "message": f"订单仓库已重载，并同步落库 {synced} 条",
    }


@router.delete("/orders/files/{filename}")
async def delete_orders_file(
    filename: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_admin_access)],
):
    """删除某个订单 CSV，全量热重载，并同步落库。"""
    from src.core.app_state import refresh_app_order_store
    from src.services.order_persist import upsert_orders
    from src.services.order_store import get_order_store

    settings = get_settings()
    safe_name = Path(filename).name
    if not safe_name.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    target = settings.orders_dir / safe_name
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    target.unlink()
    stats = refresh_app_order_store(request.app, full_reload=True)
    store = get_order_store()
    synced = await upsert_orders(db, store.list_all(limit=5000))
    return {
        "filename": safe_name,
        "stats": stats,
        "synced": synced,
        "message": f"已删除并重载，同步落库 {synced} 条",
    }


class ClearSessionsBody(BaseModel):
    include_active: bool = True
    only_archived: bool = False


@router.get("/sessions")
async def admin_list_sessions(
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    user_id: str | None = None,
):
    from sqlalchemy import func

    from src.models.message import Message
    from src.redis_client import get_redis
    from src.services.session_service import SessionService

    sessions = SessionService(redis_client=await get_redis())
    rows, total = await sessions.list_sessions(
        db,
        limit=min(max(limit, 1), 200),
        offset=max(offset, 0),
        status=status or None,
        user_id=user_id or None,
    )
    items = []
    for s in rows:
        msg_count = int(
            await db.scalar(
                select(func.count()).select_from(Message).where(Message.session_id == s.id)
            )
            or 0
        )
        items.append(
            {
                "id": s.id,
                "user_id": s.user_id,
                "status": s.status,
                "message_count": msg_count,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/sessions/{session_id}/archive")
async def admin_archive_session(
    session_id: str,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from src.core.security import validate_session_id
    from src.redis_client import get_redis
    from src.services.session_service import SessionService

    if not validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    sessions = SessionService(redis_client=await get_redis())
    ok = await sessions.archive_session(db, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.commit()
    return {"ok": True, "session_id": session_id, "status": "archived"}


@router.delete("/sessions/{session_id}")
async def admin_delete_session(
    session_id: str,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from src.core.security import validate_session_id
    from src.redis_client import get_redis
    from src.services.session_service import SessionService

    if not validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    sessions = SessionService(redis_client=await get_redis())
    ok = await sessions.delete_session(db, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.commit()
    return {"ok": True, "session_id": session_id}


@router.post("/sessions/clear")
async def admin_clear_sessions(
    body: ClearSessionsBody,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """清空会话（危险操作）。默认含进行中会话。"""
    from src.redis_client import get_redis
    from src.services.session_service import SessionService

    sessions = SessionService(redis_client=await get_redis())
    stats = await sessions.clear_sessions(
        db,
        include_active=body.include_active,
        only_archived=body.only_archived,
    )
    await db.commit()
    return {"ok": True, **stats, "message": f"已删除 {stats['deleted_sessions']} 个会话"}


class OrderPatchBody(BaseModel):
    order_id: str | None = Field(default=None, max_length=64)
    tracking_number: str | None = Field(default=None, max_length=128)
    carrier: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)


@router.get("/users")
async def admin_list_users(
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
    offset: int = 0,
):
    from src.services.user_service import list_users

    rows, total = await list_users(
        db, limit=min(max(limit, 1), 200), offset=max(offset, 0)
    )
    return {
        "items": [
            {
                "id": u.id,
                "email": u.email,
                "name": u.name,
                "is_active": bool(u.is_active),
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in rows
        ],
        "total": total,
    }


@router.delete("/users/{user_id}")
async def admin_deactivate_user(
    user_id: str,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from src.services.user_service import deactivate_user

    user = await deactivate_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    await db.commit()
    return {"ok": True, "user_id": user_id, "is_active": False, "message": "账号已注销"}


@router.get("/users/{user_id}/orders")
async def admin_user_orders(
    user_id: str,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from src.services.order_store import get_order_store
    from src.services.user_service import get_user_by_id

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    orders = get_order_store().list_by_email(user.email)
    return {"user_id": user_id, "email": user.email, "orders": orders}


@router.get("/users/{user_id}/sessions")
async def admin_user_sessions(
    user_id: str,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
    offset: int = 0,
):
    from src.redis_client import get_redis
    from src.services.session_service import SessionService
    from src.services.user_service import get_user_by_id

    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    sessions = SessionService(redis_client=await get_redis())
    rows, total = await sessions.list_sessions_for_user(
        db, user_id, limit=min(max(limit, 1), 200), offset=max(offset, 0)
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


@router.patch("/orders/{order_id}")
async def admin_patch_order(
    order_id: str,
    body: OrderPatchBody,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from src.services.order_persist import rename_order_id, upsert_orders
    from src.services.order_store import get_order_store

    store = get_order_store()
    existing = store.get(order_id)
    if not existing:
        raise HTTPException(status_code=404, detail="订单不存在")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    new_id = updates.get("order_id") or order_id
    if new_id != order_id:
        try:
            await rename_order_id(db, order_id, new_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated = store.update_order(order_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="订单不存在")
    await upsert_orders(db, [updated])
    await db.commit()
    return {"ok": True, "order": updated}


@router.delete("/orders/{order_id}")
async def admin_delete_order(
    order_id: str,
    _: Annotated[str, Depends(verify_admin_access)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from src.services.order_persist import delete_order_db
    from src.services.order_store import get_order_store

    store = get_order_store()
    ok_mem = store.delete_order(order_id)
    ok_db = await delete_order_db(db, order_id)
    if not ok_mem and not ok_db:
        raise HTTPException(status_code=404, detail="订单不存在")
    await db.commit()
    return {"ok": True, "order_id": order_id, "message": "订单已删除"}
