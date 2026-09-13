from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.agent_service import AgentService
from src.services.workflows.my_orders_intent import is_my_orders_intent


def test_is_my_orders_intent():
    assert is_my_orders_intent("我的订单有哪些？")
    assert is_my_orders_intent("看看订单")
    assert not is_my_orders_intent("查订单 ORD-1001")
    assert not is_my_orders_intent("今天天气")


@pytest.mark.asyncio
async def test_my_orders_skips_llm():
    kimi = MagicMock()
    kimi.chat = AsyncMock()
    retriever = MagicMock()
    agent = AgentService(kimi, retriever, None)
    agent._call_tool = AsyncMock(
        return_value={
            "success": True,
            "data": {
                "orders": [
                    {
                        "order_id": "ORD-1001",
                        "status": "Shipped",
                        "carrier": "DHL",
                        "tracking_number": "DHL1",
                    }
                ]
            },
            "error": None,
            "citations": [],
        }
    )

    response = await agent.process(
        "我的订单有哪些？",
        user_id="u1",
        user_email="demo@gulf.ae",
    )

    kimi.chat.assert_not_awaited()
    assert response.tools_used == ["query_my_orders"]
    assert "ORD-1001" in response.answer
