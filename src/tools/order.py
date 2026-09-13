"""订单查询工具：优先 CSV 仓库，其次外部 API，开发环境可回退假数据。"""

from __future__ import annotations

import httpx

from src.core.config import get_settings
from src.services.order_store import get_order_store
from src.tools import ToolResult


def _fallback_mock_order(order_id: str) -> dict:
    return {
        "order_id": order_id,
        "status": "Shipped",
        "destination_country": "SA",
        "currency": "USD",
        "items": [{"name": "智能手机 / Smartphone", "quantity": 1}],
        "total": 2999.00,
        "created_at": "2024-07-01 10:00:00",
        "carrier": "Aramex",
        "tracking_number": "ARX123456789",
        "_mock": True,
    }


async def query_order(order_id: str, email: str | None = None) -> ToolResult:
    """按订单号查询；先查本地 CSV 热更新仓库。"""
    settings = get_settings()
    order_id = (order_id or "").strip()
    if not order_id:
        return ToolResult(success=False, error="Missing order_id")

    store = get_order_store()
    local = store.get(order_id)
    if local:
        data = dict(local)
        if email:
            data["email_verified"] = (local.get("email") or "").lower() == email.lower()
        return ToolResult(success=True, data=data)

    if settings.order_api_url:
        try:
            params = {"order_id": order_id}
            if email:
                params["email"] = email
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{settings.order_api_url}/{order_id}",
                    params=params,
                    headers={"Authorization": f"Bearer {settings.order_api_key}"},
                )
                resp.raise_for_status()
                return ToolResult(success=True, data=resp.json())
        except Exception:
            return ToolResult(success=False, error="Order lookup failed")

    if settings.env == "production":
        return ToolResult(
            success=False,
            error="Order not found. Import CSV in admin or configure ORDER_API_URL.",
        )

    data = _fallback_mock_order(order_id)
    if email:
        data["email_verified"] = True
    return ToolResult(success=True, data=data)


async def list_my_orders(
    user_id: str | None = None,
    email: str | None = None,
) -> ToolResult:
    """列出当前用户订单；优先按邮箱从 CSV 仓库取。"""
    settings = get_settings()
    email_key = (email or "").lower()

    store = get_order_store()
    local_orders = store.list_by_email(email_key) if email_key else []
    if local_orders:
        return ToolResult(
            success=True,
            data={
                "orders": local_orders,
                "user_id": user_id,
                "email": email_key,
                "count": len(local_orders),
                "_source": "csv",
            },
        )

    if settings.order_api_url and user_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{settings.order_api_url}/user/{user_id}",
                    headers={"Authorization": f"Bearer {settings.order_api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
                return ToolResult(success=True, data={"orders": data, "user_id": user_id})
        except Exception:
            return ToolResult(success=False, error="Failed to fetch orders")

    if settings.env == "production":
        return ToolResult(
            success=False,
            error="No local orders for this user. Import CSV in admin or configure ORDER_API_URL.",
        )

    return ToolResult(
        success=True,
        data={
            "orders": [],
            "user_id": user_id,
            "email": email_key,
            "count": 0,
            "_source": "empty",
        },
    )
