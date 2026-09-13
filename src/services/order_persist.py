"""订单落库：CSV 热更新后同步到 PostgreSQL，启动时回灌。"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.order import Order
from src.services.order_store import get_order_store

logger = structlog.get_logger()


def _to_row(order: dict[str, Any]) -> dict[str, Any]:
    total = order.get("total") or 0
    try:
        total_f = float(total)
    except (TypeError, ValueError):
        total_f = 0.0
    return {
        "order_id": order["order_id"],
        "email": (order.get("email") or "").lower(),
        "status": order.get("status") or "Unknown",
        "destination_country": order.get("destination_country") or "",
        "currency": order.get("currency") or "USD",
        "total": total_f,
        "tracking_number": order.get("tracking_number") or "",
        "carrier": order.get("carrier") or "",
        "created_at_biz": order.get("created_at") or "",
        "items": order.get("items") or [],
        "source": order.get("_source") or "csv",
    }


async def upsert_orders(db: AsyncSession, orders: list[dict[str, Any]]) -> int:
    """按 order_id 写入/更新。返回写入条数。"""
    if not orders:
        return 0
    count = 0
    for order in orders:
        if not order.get("order_id"):
            continue
        payload = _to_row(order)
        existing = await db.scalar(select(Order).where(Order.order_id == payload["order_id"]))
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
        else:
            db.add(Order(**payload))
        count += 1
    await db.flush()
    logger.info("orders_upserted", count=count)
    return count


async def load_orders_into_store(db: AsyncSession) -> int:
    """从数据库把订单合并进内存仓库。"""
    rows = (await db.scalars(select(Order))).all()
    store = get_order_store()
    merged = 0
    for row in rows:
        data = {
            "order_id": row.order_id,
            "email": row.email or "",
            "status": row.status,
            "destination_country": row.destination_country,
            "currency": row.currency,
            "total": row.total,
            "tracking_number": row.tracking_number,
            "carrier": row.carrier,
            "created_at": row.created_at_biz,
            "items": row.items or [],
            "_source": "db",
        }
        store.merge_order_dict(data)
        merged += 1
    logger.info("orders_loaded_from_db", count=merged)
    return merged


async def delete_order_db(db: AsyncSession, order_id: str) -> bool:
    row = await db.scalar(select(Order).where(Order.order_id == order_id))
    if not row:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def rename_order_id(db: AsyncSession, old_id: str, new_id: str) -> bool:
    if old_id == new_id:
        return True
    existing_new = await db.scalar(select(Order).where(Order.order_id == new_id))
    if existing_new:
        raise ValueError(f"订单号 {new_id} 已存在")
    row = await db.scalar(select(Order).where(Order.order_id == old_id))
    if not row:
        return False
    row.order_id = new_id
    await db.flush()
    return True
