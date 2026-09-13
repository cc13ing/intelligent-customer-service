from __future__ import annotations

import datetime
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.tools import ToolResult

logger = structlog.get_logger()


def _mock_logistics_data(logistics_number: str) -> dict:
    return {
        "logistics_number": logistics_number,
        "status": "In Transit",
        "carrier": "DHL Express",
        "destination_country": "AE",
        "customs_status": "Cleared",
        "estimated_delivery": "2024-07-15",
        "current_location": "Dubai Sorting Center, UAE",
        "last_update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "_mock": True,
        "events": [
            {
                "event_type": "Pickup",
                "time": "2024-07-10 09:00:00",
                "location": "Shenzhen Warehouse, CN",
            },
            {
                "event_type": "Export Customs Cleared",
                "time": "2024-07-10 18:00:00",
                "location": "Shenzhen Port, CN",
            },
            {
                "event_type": "In Transit",
                "time": "2024-07-11 14:30:00",
                "location": "Dubai Sorting Center, UAE",
            },
        ],
    }


async def _upsert_into_my_orders(
    *,
    logistics_number: str,
    data: dict[str, Any],
    email: str | None,
    db: AsyncSession | None,
) -> dict[str, Any] | None:
    """将物流查询结果写入「我的订单」（内存 + 可选落库）。"""
    if not email:
        return None
    from src.services.order_persist import upsert_orders
    from src.services.order_store import get_order_store

    store = get_order_store()
    existing = store.find_by_tracking(logistics_number)
    carrier = data.get("carrier") or (existing or {}).get("carrier") or ""
    status = data.get("status") or (existing or {}).get("status") or "In Transit"
    if existing:
        updated = store.update_order(
            existing["order_id"],
            {
                "tracking_number": logistics_number,
                "carrier": carrier,
                "status": status,
            },
        )
        order = updated or existing
        order["_logistics_synced"] = True
    else:
        order = {
            "order_id": f"TRK-{logistics_number}",
            "email": email.lower(),
            "status": status,
            "destination_country": data.get("destination_country") or "",
            "currency": "USD",
            "total": 0,
            "tracking_number": logistics_number,
            "carrier": carrier,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": [{"name": "物流查询新增", "quantity": 1}],
            "_source": "logistics",
        }
        store.merge_order_dict(order)
        order["_logistics_synced"] = True
        order["_created_from_logistics"] = True

    if db is not None:
        try:
            await upsert_orders(db, [order])
        except Exception as exc:
            logger.warning("logistics_order_persist_failed", error=str(exc))
    return order


async def fetch_logistics_information(
    logistics_number: str,
    *,
    email: str | None = None,
    user_id: str | None = None,
    db: AsyncSession | None = None,
) -> ToolResult:
    settings = get_settings()
    number = (logistics_number or "").strip()
    if not number:
        return ToolResult(success=False, error="请提供物流单号")

    data: dict[str, Any] | None = None
    if settings.logistics_api_url:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    settings.logistics_api_url,
                    params={"number": number},
                    headers={"Authorization": f"Bearer {settings.logistics_api_key}"},
                )
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict):
                    data = payload
                else:
                    return ToolResult(success=False, error="物流接口返回格式异常")
        except Exception:
            return ToolResult(
                success=False,
                error="物流查询失败（部分快递暂不可查询）。请核对单号或稍后再试。",
            )
    elif settings.env == "production":
        return ToolResult(
            success=False,
            error="Logistics API is not configured. Contact support.",
        )
    else:
        data = _mock_logistics_data(number)

    assert data is not None
    if "logistics_number" not in data:
        data["logistics_number"] = number

    synced = await _upsert_into_my_orders(
        logistics_number=number,
        data=data,
        email=email,
        db=db,
    )
    if synced:
        data["my_order"] = {
            "order_id": synced.get("order_id"),
            "tracking_number": synced.get("tracking_number"),
            "carrier": synced.get("carrier"),
            "status": synced.get("status"),
            "created_from_logistics": bool(synced.get("_created_from_logistics")),
        }

    return ToolResult(success=True, data=data)
