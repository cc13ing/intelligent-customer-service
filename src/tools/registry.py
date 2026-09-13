from __future__ import annotations

from typing import Any


# Tool metadata for Agent orchestration
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "fetch_logistics_information",
        "description": "根据物流单号查询包裹运输状态和轨迹信息",
        "parameters": {
            "type": "object",
            "properties": {
                "logistics_number": {
                    "type": "string",
                    "description": "物流单号",
                }
            },
            "required": ["logistics_number"],
        },
    },
    {
        "name": "record_user_complaint",
        "description": (
            "记录服务质量投诉并创建工单（TKT-）。"
            "适用：态度差、要投诉/举报、欺诈等服务问题。"
            "不要用于退货、退款、换货申请或退换货政策咨询。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "complaint_details": {
                    "type": "string",
                    "description": "投诉详情描述",
                }
            },
            "required": ["complaint_details"],
        },
    },
    {
        "name": "create_return_request",
        "description": (
            "处理退货、退款、换货申请并说明退换货政策与下一步。"
            "适用：我要退货/退款/换货、退换货怎么弄。"
            "不要用于服务质量投诉（投诉请用 record_user_complaint）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户的退货/退款/换货诉求",
                },
                "order_id": {
                    "type": "string",
                    "description": "订单编号（若用户已提供）",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "customer_chat",
        "description": "回答用户关于产品、服务、政策等一般性咨询问题",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户咨询问题",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_order",
        "description": "根据订单号查询订单状态和详情（可选邮箱验证）",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "订单编号",
                },
                "email": {
                    "type": "string",
                    "description": "下单邮箱（可选，用于身份验证）",
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "query_my_orders",
        "description": "查询当前登录用户的全部订单（我的订单）。用户问「我的订单」「我买了什么」时使用，需已登录。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "query_work_order",
        "description": "根据工单号查询投诉/转人工工单详情与状态。用户提供 TKT- 开头工单号，或问「查工单」时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "工单号，例如 TKT-A1B2C3D4",
                }
            },
            "required": ["ticket_id"],
        },
    },
]


def get_openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in TOOL_DEFINITIONS
    ]


def format_tools_for_prompt() -> str:
    lines = ["可用工具列表:\n"]
    for tool in TOOL_DEFINITIONS:
        lines.append(f"工具名称: {tool['name']}")
        lines.append(f"工具描述: {tool['description']}")
        lines.append(f"输入参数: {tool['parameters']}\n")
    return "\n".join(lines)
