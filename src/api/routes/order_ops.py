"""前台订单导入 / 统计（免管理端 API Key，受 chat_public_access 与限流约束）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.app_state import refresh_app_order_store
from src.core.config import get_settings
from src.core.rate_limit import limiter
from src.core.security import verify_chat_access
from src.db import get_db
from src.services.order_persist import upsert_orders
from src.services.order_store import get_order_store

logger = structlog.get_logger()
router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


@router.get("/stats")
@limiter.limit("60/minute")
async def order_store_stats(
    request: Request,
    _: Annotated[str, Depends(verify_chat_access)],
):
    store = get_order_store()
    return {"stats": store.stats()}


@router.post("/upload")
@limiter.limit("10/minute")
async def upload_orders_csv_public(
    request: Request,
    file: UploadFile = File(...),
    db: Annotated[AsyncSession, Depends(get_db)] = ...,
    _: Annotated[str, Depends(verify_chat_access)] = ...,
    merge: bool = True,
):
    """聊天页批量导入 CSV（与管理端逻辑一致，权限走 chat_public_access）。"""
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
        "orders_csv_uploaded_public",
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
