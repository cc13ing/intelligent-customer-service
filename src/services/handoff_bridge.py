"""人工客服桥接：会话模式 + 用户/坐席 WebSocket 连接登记 + 未读计数。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from fastapi import WebSocket

logger = structlog.get_logger()

MODE_WAITING = "waiting_human"
MODE_HUMAN = "human"
MODE_BOT = "active"


class HandoffBridge:
    """进程内桥接（单机部署够用；多实例可再换 Redis pub/sub）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._modes: dict[str, str] = {}
        self._agents: dict[str, str] = {}  # session_id -> agent_name
        self._tickets: dict[str, str] = {}  # session_id -> complaint_id
        self._user_ws: dict[str, WebSocket] = {}
        self._agent_ws: dict[str, WebSocket] = {}
        self._unread: dict[str, int] = {}  # session_id -> unread user msgs for agent
        self._last_user_msg_at: dict[str, float] = {}
        self._queue_version: int = 0

    @property
    def queue_version(self) -> int:
        return self._queue_version

    def bump_queue(self) -> None:
        self._queue_version += 1

    def get_mode(self, session_id: str) -> str:
        return self._modes.get(session_id) or MODE_BOT

    def is_human(self, session_id: str) -> bool:
        return self.get_mode(session_id) in {MODE_WAITING, MODE_HUMAN}

    def get_unread(self, session_id: str) -> int:
        return int(self._unread.get(session_id) or 0)

    def last_user_msg_at(self, session_id: str) -> float | None:
        return self._last_user_msg_at.get(session_id)

    def has_agent_online(self, session_id: str) -> bool:
        return session_id in self._agent_ws

    async def enqueue(self, session_id: str, complaint_id: str) -> None:
        async with self._lock:
            self._modes[session_id] = MODE_WAITING
            self._tickets[session_id] = complaint_id
            self._agents.pop(session_id, None)
            self._queue_version += 1
        logger.info("handoff_enqueued", session_id=session_id, complaint_id=complaint_id)

    async def claim(self, session_id: str, agent_name: str) -> None:
        async with self._lock:
            self._modes[session_id] = MODE_HUMAN
            self._agents[session_id] = agent_name
            self._queue_version += 1
        logger.info("handoff_claimed", session_id=session_id, agent=agent_name)

    async def release(self, session_id: str) -> None:
        async with self._lock:
            self._modes.pop(session_id, None)
            self._agents.pop(session_id, None)
            self._tickets.pop(session_id, None)
            self._agent_ws.pop(session_id, None)
            self._unread.pop(session_id, None)
            self._last_user_msg_at.pop(session_id, None)
            self._queue_version += 1
        logger.info("handoff_released", session_id=session_id)

    async def mark_user_message(self, session_id: str) -> int:
        async with self._lock:
            self._unread[session_id] = self.get_unread(session_id) + 1
            self._last_user_msg_at[session_id] = time.time()
            return self._unread[session_id]

    async def mark_read(self, session_id: str) -> None:
        async with self._lock:
            self._unread[session_id] = 0

    async def bind_user(self, session_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._user_ws[session_id] = ws

    async def unbind_user(self, session_id: str, ws: WebSocket) -> None:
        async with self._lock:
            if self._user_ws.get(session_id) is ws:
                self._user_ws.pop(session_id, None)

    async def bind_agent(self, session_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._agent_ws[session_id] = ws
            self._unread[session_id] = 0

    async def unbind_agent(self, session_id: str, ws: WebSocket) -> None:
        async with self._lock:
            if self._agent_ws.get(session_id) is ws:
                self._agent_ws.pop(session_id, None)

    def agent_name(self, session_id: str) -> str | None:
        return self._agents.get(session_id)

    def ticket_id(self, session_id: str) -> str | None:
        return self._tickets.get(session_id)

    async def push_user(self, session_id: str, payload: dict[str, Any]) -> bool:
        ws = self._user_ws.get(session_id)
        if not ws:
            logger.warning(
                "handoff_push_user_no_ws",
                session_id=session_id,
                payload_type=payload.get("type"),
            )
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception as exc:
            logger.warning("handoff_push_user_failed", session_id=session_id, error=str(exc))
            return False

    async def push_agent(self, session_id: str, payload: dict[str, Any]) -> bool:
        # 用户来信：先记未读
        if payload.get("type") == "user_message":
            await self.mark_user_message(session_id)
            payload = {**payload, "unread": self.get_unread(session_id)}
        ws = self._agent_ws.get(session_id)
        if not ws:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception as exc:
            logger.warning("handoff_push_agent_failed", session_id=session_id, error=str(exc))
            return False


_bridge: HandoffBridge | None = None


def get_handoff_bridge() -> HandoffBridge:
    global _bridge
    if _bridge is None:
        _bridge = HandoffBridge()
    return _bridge
