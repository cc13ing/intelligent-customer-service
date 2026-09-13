"""聊天接口：HTTP 与 WebSocket，把用户消息交给 Agent。"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_agent_service, get_optional_user, get_session_service
from src.core.auth import try_decode_token
from src.core.input_guard import validate_user_message
from src.core.metrics import CHAT_REQUESTS, RESPONSE_LATENCY
from src.core.rate_limit import limiter
from src.core.security import validate_session_id, verify_chat_access, verify_ws_api_key
from src.db import get_db
from src.utils.text import sanitize_assistant_reply
from src.models.user import User
from src.services.agent_service import AgentService
from src.services.session_service import SessionService

logger = structlog.get_logger()
router = APIRouter(prefix="/api/v1", tags=["chat"])

_ws_message_times: dict[str, list[float]] = defaultdict(list)
_WS_RATE_LIMIT = 30
_WS_KEEPALIVE_SECONDS = 15.0
_WS_RATE_WINDOW = 60.0


class ChatRequest(BaseModel):
    """HTTP 聊天请求体。"""

    message: str = Field(..., min_length=1, max_length=2000)
    session_id: str | None = None


class ChatResponse(BaseModel):
    """HTTP 聊天响应体。"""

    session_id: str
    answer: str
    tool_name: str | None = None
    citations: list[str] = []
    tools_used: list[str] = []
    emotion: str = "listen"


class HandoffRequest(BaseModel):
    """转人工请求。"""

    reason: str = Field(default="用户请求转人工", max_length=500)
    session_id: str | None = None


@router.post("/handoff")
@limiter.limit("20/minute")
async def request_human_handoff(
    request: Request,
    body: HandoffRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    _: Annotated[str, Depends(verify_chat_access)],
):
    """创建转人工工单，并进入等待人工队列。"""
    from sqlalchemy import select

    from src.models.complaint import Complaint
    from src.models.session import ChatSession
    from src.services.handoff_bridge import get_handoff_bridge
    from src.tools.complaint import record_user_complaint

    # 确保一定有可对接的会话，否则坐席无法聊天
    sid = body.session_id if body.session_id and validate_session_id(body.session_id) else None
    if sid:
        existing = await sessions.get_session(db, sid)
        if not existing:
            sid = None
    if not sid:
        created = await sessions.create_session(db, user_id=None)
        sid = created.id
        await db.flush()

    reason = (body.reason or "用户请求转人工").strip()
    details = f"[转人工] {reason}"
    result = await record_user_complaint(
        complaint_details=details,
        session_id=sid,
        db=db,
    )
    data = result.data or {}
    record = data.get("record") or {}
    ticket_id = data.get("ticket_id") or record.get("ticket_id") or "TKT-PENDING"
    complaint_id = record.get("id")

    session = await db.scalar(select(ChatSession).where(ChatSession.id == sid))
    if session:
        session.status = "waiting_human"
    if complaint_id:
        complaint = await db.scalar(select(Complaint).where(Complaint.id == complaint_id))
        if complaint and not complaint.session_id:
            complaint.session_id = sid
        await get_handoff_bridge().enqueue(sid, str(complaint_id))
    await db.commit()
    await get_handoff_bridge().push_user(
        sid,
        {
            "type": "handoff_queued",
            "session_id": sid,
            "ticket_id": ticket_id,
            "message": "已进入人工队列，请稍候…",
        },
    )

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "session_id": sid,
        "status": data.get("status", "Received"),
        "emotion": "sorry",
        "message": (
            f"饺子已经帮你叫人了～工单 {ticket_id} 已进入人工队列。"
            "请稍等，真人客服会在「人工客服台」接手并和你对话。"
        ),
        "queue": "human_handoff",
        "is_handoff": True,
    }


async def _resolve_user_context(
    db: AsyncSession,
    sessions: SessionService,
    session_id: str,
    user: User | None,
) -> tuple[str | None, str | None]:
    """从登录用户解析 user_id / email，并绑定到会话。"""
    user_id = user.id if user else None
    user_email = user.email if user else None
    if user_id:
        await sessions.bind_user(db, session_id, user_id)
    return user_id, user_email


class SessionResponse(BaseModel):
    """会话历史响应。"""

    id: str
    status: str
    messages: list[dict]


async def _resolve_websocket_session(
    db: AsyncSession,
    sessions: SessionService,
    *,
    client_session_id: str | None,
    connection_session_id: str | None,
    user_id: str | None,
) -> tuple[str, bool]:
    """为 WebSocket 消息选定有效会话；过期则新建。返回 (session_id, 是否新建)。"""
    candidates: list[str] = []
    if client_session_id and validate_session_id(client_session_id):
        candidates.append(client_session_id)
    if connection_session_id and validate_session_id(connection_session_id):
        if connection_session_id not in candidates:
            candidates.append(connection_session_id)

    for sid in candidates:
        session = await sessions.get_session(db, sid)
        if not session:
            continue
        if user_id and session.user_id and session.user_id != user_id:
            continue
        return sid, False

    session = await sessions.create_session(db, user_id=user_id)
    await db.flush()
    return session.id, True


def _check_ws_rate(ip: str) -> bool:
    """WebSocket 简易限流：同一 IP 每分钟消息数不超过上限。"""
    now = time.monotonic()
    times = _ws_message_times[ip]
    _ws_message_times[ip] = [t for t in times if now - t < _WS_RATE_WINDOW]
    if len(_ws_message_times[ip]) >= _WS_RATE_LIMIT:
        return False
    _ws_message_times[ip].append(now)
    return True


async def _ws_send_json(websocket: WebSocket, payload: dict) -> bool:
    """向客户端发 JSON；连接已断开则返回 False。"""
    if websocket.client_state != WebSocketState.CONNECTED:
        return False
    try:
        await websocket.send_json(payload)
        return True
    except WebSocketDisconnect:
        return False
    except RuntimeError as exc:
        if "close message" in str(exc).lower():
            return False
        raise


@router.post("/chat", response_model=ChatResponse)
@limiter.limit("30/minute")
async def chat(
    request: Request,
    body: ChatRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    agent: Annotated[AgentService, Depends(get_agent_service)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    user: Annotated[User | None, Depends(get_optional_user)],
    _: Annotated[str, Depends(verify_chat_access)],
):
    """HTTP 聊天：一次性返回完整答案（非流式）。"""
    CHAT_REQUESTS.labels(method="rest").inc()

    if body.session_id and not validate_session_id(body.session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format")

    try:
        message = validate_user_message(body.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.session_id:
        session = await sessions.get_session(db, body.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.user_id and user and session.user_id != user.id:
            raise HTTPException(status_code=403, detail="无权使用该会话")
        if session.user_id and not user:
            raise HTTPException(status_code=403, detail="请先登录")
        session_id = body.session_id
    else:
        session = await sessions.create_session(db, user_id=user.id if user else None)
        session_id = session.id

    user_id, user_email = await _resolve_user_context(db, sessions, session_id, user)

    await sessions.add_message(db, session_id, "user", message)
    try:
        start = time.perf_counter()
        response = await agent.process(
            message,
            session_id=session_id,
            db=db,
            sessions=sessions,
            user_id=user_id,
            user_email=user_email,
        )
        RESPONSE_LATENCY.labels(method="rest").observe(time.perf_counter() - start)
    except Exception:
        logger.exception("chat_process_failed", session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to process message")

    clean_answer = sanitize_assistant_reply(response.answer)
    await sessions.add_message(
        db,
        session_id,
        "assistant",
        clean_answer,
        tool_calls={"tools": response.tools_used} if response.tools_used else (
            {"tool": response.tool_name} if response.tool_name else None
        ),
        citations=response.citations or None,
    )
    await sessions.update_session_summary(session_id, message, clean_answer)

    return ChatResponse(
        session_id=session_id,
        answer=clean_answer,
        tool_name=response.tool_name,
        citations=response.citations,
        tools_used=response.tools_used,
        emotion=getattr(response, "emotion", None) or "listen",
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
@limiter.limit("60/minute")
async def get_session_history(
    request: Request,
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    user: Annotated[User | None, Depends(get_optional_user)],
    _: Annotated[str, Depends(verify_chat_access)],
):
    """查询某个会话的历史消息（仅本人或未绑定会话）。"""
    if not validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format")

    session = await sessions.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id:
        if not user or session.user_id != user.id:
            raise HTTPException(status_code=403, detail="无权访问该会话")

    messages = await sessions.get_messages(db, session_id)
    return SessionResponse(
        id=session.id,
        status=session.status,
        messages=[
            {
                "role": m.role,
                "content": m.content,
                "citations": m.citations,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    )


@router.delete("/sessions/{session_id}")
@limiter.limit("30/minute")
async def delete_session_history(
    request: Request,
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    user: Annotated[User | None, Depends(get_optional_user)],
    _: Annotated[str, Depends(verify_chat_access)],
):
    """删除某个会话及其消息。"""
    if not validate_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format")

    session = await sessions.get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id and (not user or session.user_id != user.id):
        raise HTTPException(status_code=403, detail="无权删除该会话")

    deleted = await sessions.delete_session(db, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True, "session_id": session_id}


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket 聊天入口：流式推送、心跳 ping、支持 cancel 取消生成。"""
    api_key = websocket.query_params.get("api_key") or websocket.headers.get("x-api-key")
    try:
        verify_ws_api_key(api_key)
    except HTTPException:
        # #region debug-point C:ws-unauthorized
        try:
            import json as _json, urllib.request as _ur

            _p = ".dbg/login-chat-timeout.env"
            _u = "http://127.0.0.1:7778/event"
            _s = "login-chat-timeout"
            try:
                with open(_p, encoding="utf-8") as _f:
                    _c = _f.read().splitlines()
                    _u = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SERVER_URL=")), _u)
                    _s = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SESSION_ID=")), _s)
            except Exception:
                pass
            _ur.urlopen(
                _ur.Request(
                    _u,
                    data=_json.dumps(
                        {
                            "sessionId": _s,
                            "runId": "pre",
                            "hypothesisId": "C",
                            "location": "src/api/routes/chat.py:ws_chat",
                            "msg": "[DEBUG] ws_unauthorized",
                            "data": {
                                "has_api_key": bool(api_key),
                                "client": websocket.client.host if websocket.client else None,
                            },
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=2,
            ).read()
        except Exception:
            pass
        # #endregion
        await websocket.close(code=1008, reason="Unauthorized")
        return

    await websocket.accept()
    # #region debug-point C:ws-accepted
    try:
        import json as _json, urllib.request as _ur

        _p = ".dbg/login-chat-timeout.env"
        _u = "http://127.0.0.1:7778/event"
        _s = "login-chat-timeout"
        try:
            with open(_p, encoding="utf-8") as _f:
                _c = _f.read().splitlines()
                _u = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SERVER_URL=")), _u)
                _s = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SESSION_ID=")), _s)
        except Exception:
            pass
        _ur.urlopen(
            _ur.Request(
                _u,
                data=_json.dumps(
                    {
                        "sessionId": _s,
                        "runId": "pre",
                        "hypothesisId": "C",
                        "location": "src/api/routes/chat.py:ws_chat",
                        "msg": "[DEBUG] ws_accepted",
                        "data": {
                            "client": websocket.client.host if websocket.client else None,
                            "has_api_key": bool(api_key),
                            "has_token": bool(websocket.query_params.get("token")),
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ),
            timeout=2,
        ).read()
    except Exception:
        pass
    # #endregion
    app = websocket.app
    agent: AgentService = app.state.agent_service
    sessions: SessionService = app.state.session_service

    from src.db import async_session_factory

    session_id: str | None = None
    client_ip = websocket.client.host if websocket.client else "unknown"
    ws_token = websocket.query_params.get("token")
    ws_payload = try_decode_token(ws_token)
    ws_user_id = ws_payload.get("sub") if ws_payload else None
    ws_user_email = ws_payload.get("email") if ws_payload else None

    incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def _pump_incoming() -> None:
        """后台持续读取客户端消息，放入队列（便于生成中也能收到 cancel）。"""
        try:
            while True:
                incoming.put_nowait(await websocket.receive_text())
        except WebSocketDisconnect:
            incoming.put_nowait(None)
        except Exception:
            incoming.put_nowait(None)

    pump_task = asyncio.create_task(_pump_incoming())

    try:
        while True:
            raw = await incoming.get()
            if raw is None:
                break
            if not _check_ws_rate(client_ip):
                if not await _ws_send_json(
                    websocket,
                    {"type": "error", "error": "Rate limit exceeded. Please slow down."},
                ):
                    break
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                if not await _ws_send_json(
                    websocket, {"type": "error", "error": "Invalid JSON payload"}
                ):
                    break
                continue

            msg_type = data.get("type")
            if msg_type in {"ping", "pong", "cancel"}:
                if msg_type == "ping":
                    if not await _ws_send_json(websocket, {"type": "pong"}):
                        break
                continue

            # 登记用户 WS，便于人工坐席推送（不必等用户发消息）
            if msg_type == "bind":
                bind_sid = data.get("session_id")
                if bind_sid and validate_session_id(bind_sid):
                    from src.services.handoff_bridge import get_handoff_bridge

                    async with async_session_factory() as db:
                        # 校验会话存在且归属
                        sess = await sessions.get_session(db, bind_sid)
                        if sess and (
                            not ws_user_id
                            or not sess.user_id
                            or sess.user_id == ws_user_id
                        ):
                            session_id = bind_sid
                            await get_handoff_bridge().bind_user(bind_sid, websocket)
                            await _ws_send_json(
                                websocket,
                                {"type": "bound", "session_id": bind_sid},
                            )
                        else:
                            await _ws_send_json(
                                websocket,
                                {"type": "error", "error": "无法绑定该会话"},
                            )
                continue

            message = data.get("message", "").strip()
            if not message:
                continue

            CHAT_REQUESTS.labels(method="websocket").inc()

            try:
                message = validate_user_message(message)
            except ValueError as exc:
                if not await _ws_send_json(websocket, {"type": "error", "error": str(exc)}):
                    break
                continue

            async with async_session_factory() as db:
                client_sid = data.get("session_id")
                if client_sid and not validate_session_id(client_sid):
                    if not await _ws_send_json(
                        websocket, {"type": "error", "error": "Invalid session_id"}
                    ):
                        break
                    continue

                session_id, is_new_session = await _resolve_websocket_session(
                    db,
                    sessions,
                    client_session_id=client_sid,
                    connection_session_id=session_id,
                    user_id=ws_user_id,
                )
                if is_new_session:
                    await db.commit()
                    if not await _ws_send_json(
                        websocket, {"type": "session", "session_id": session_id}
                    ):
                        break

                if ws_user_id:
                    await sessions.bind_user(db, session_id, ws_user_id)

                await sessions.add_message(db, session_id, "user", message)
                await db.commit()

                from src.services.handoff_bridge import get_handoff_bridge

                bridge = get_handoff_bridge()
                await bridge.bind_user(session_id, websocket)
                mode = bridge.get_mode(session_id)
                # DB 状态兜底（进程重启后 bridge 可能空）
                if mode == "active":
                    from sqlalchemy import select as _sel
                    from src.models.session import ChatSession as _CS

                    row = await db.scalar(_sel(_CS).where(_CS.id == session_id))
                    if row and row.status in {"waiting_human", "human"}:
                        mode = row.status
                        if mode == "waiting_human":
                            await bridge.enqueue(session_id, bridge.ticket_id(session_id) or "")
                        elif mode == "human":
                            await bridge.claim(session_id, "人工客服")

                if mode in {"waiting_human", "human"}:
                    await bridge.push_agent(
                        session_id,
                        {
                            "type": "user_message",
                            "session_id": session_id,
                            "content": message,
                        },
                    )
                    if mode == "waiting_human":
                        tip = "人工客服还在赶来的路上，请稍候～你的消息已同步到排队队列。"
                        await sessions.add_message(db, session_id, "assistant", tip)
                        await db.commit()
                        if not await _ws_send_json(
                            websocket,
                            {
                                "type": "done",
                                "session_id": session_id,
                                "answer": tip,
                                "emotion": "listen",
                                "human_mode": "waiting",
                            },
                        ):
                            break
                    else:
                        # 真人模式：不跑 AI，等坐席回复（agent_message 经 bridge 推送）
                        if not await _ws_send_json(
                            websocket,
                            {
                                "type": "human_ack",
                                "session_id": session_id,
                                "message": "已送达人工客服",
                            },
                        ):
                            break
                    continue

                answer = ""
                tool_name = None
                citations: list[str] = []
                tools_used: list[str] = []
                emotion = "listen"
                client_disconnected = False
                cancelled = False
                keepalive_task: asyncio.Task | None = None
                # #region debug-point D:ws-agent-start
                _trace_id = f"{int(time.time() * 1000)}-{session_id}"
                try:
                    import json as _json, urllib.request as _ur

                    _p = ".dbg/login-chat-timeout.env"
                    _u = "http://127.0.0.1:7778/event"
                    _s = "login-chat-timeout"
                    try:
                        with open(_p, encoding="utf-8") as _f:
                            _c = _f.read().splitlines()
                            _u = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SERVER_URL=")), _u)
                            _s = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SESSION_ID=")), _s)
                    except Exception:
                        pass
                    _ur.urlopen(
                        _ur.Request(
                            _u,
                            data=_json.dumps(
                                {
                                    "sessionId": _s,
                                    "runId": "pre",
                                    "hypothesisId": "D",
                                    "traceId": _trace_id,
                                    "location": "src/api/routes/chat.py:ws_chat",
                                    "msg": "[DEBUG] ws_agent_start",
                                    "data": {
                                        "session_id": session_id,
                                        "message_len": len(message),
                                        "user_id": bool(ws_user_id),
                                    },
                                },
                                ensure_ascii=False,
                            ).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                        ),
                        timeout=2,
                    ).read()
                except Exception:
                    pass
                # #endregion
                stream = agent.process_stream(
                    message,
                    session_id=session_id,
                    db=db,
                    sessions=sessions,
                    user_id=ws_user_id,
                    user_email=ws_user_email,
                )
                stream_iter = stream.__aiter__()

                async def _keepalive() -> None:
                    while True:
                        await asyncio.sleep(_WS_KEEPALIVE_SECONDS)
                        if not await _ws_send_json(websocket, {"type": "ping"}):
                            return

                async def _next_event():
                    return await stream_iter.__anext__()

                try:
                    start = time.perf_counter()
                    keepalive_task = asyncio.create_task(_keepalive())
                    chunk_count = 0
                    while True:
                        event_task = asyncio.create_task(_next_event())
                        incoming_task = asyncio.create_task(incoming.get())
                        done, pending = await asyncio.wait(
                            {event_task, incoming_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

                        if incoming_task in done:
                            if event_task in done:
                                try:
                                    event_task.result()
                                except StopAsyncIteration:
                                    pass
                            raw_msg = incoming_task.result()
                            if raw_msg is None:
                                client_disconnected = True
                                break
                            try:
                                control = json.loads(raw_msg)
                            except json.JSONDecodeError:
                                control = {}
                            ctype = control.get("type")
                            if ctype == "cancel":
                                cancelled = True
                                break
                            if ctype == "ping":
                                if not await _ws_send_json(websocket, {"type": "pong"}):
                                    client_disconnected = True
                                    break
                                continue
                            if ctype == "pong":
                                continue
                            # Queue non-control messages for the next turn.
                            await incoming.put(raw_msg)
                            continue

                        try:
                            event = event_task.result()
                        except StopAsyncIteration:
                            break

                        if event["type"] == "chunk":
                            chunk_count += 1
                            if not await _ws_send_json(
                                websocket,
                                {"type": "chunk", "content": event["content"]},
                            ):
                                client_disconnected = True
                                break
                        elif event["type"] == "done":
                            answer = event.get("answer", "")
                            tool_name = event.get("tool_name")
                            citations = event.get("citations") or []
                            tools_used = event.get("tools_used") or []
                            emotion = event.get("emotion") or "listen"
                    RESPONSE_LATENCY.labels(method="websocket").observe(
                        time.perf_counter() - start
                    )
                    # #region debug-point D:ws-agent-done
                    try:
                        import json as _json, urllib.request as _ur

                        _p = ".dbg/login-chat-timeout.env"
                        _u = "http://127.0.0.1:7778/event"
                        _s = "login-chat-timeout"
                        try:
                            with open(_p, encoding="utf-8") as _f:
                                _c = _f.read().splitlines()
                                _u = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SERVER_URL=")), _u)
                                _s = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SESSION_ID=")), _s)
                        except Exception:
                            pass
                        _ur.urlopen(
                            _ur.Request(
                                _u,
                                data=_json.dumps(
                                    {
                                        "sessionId": _s,
                                        "runId": "pre",
                                        "hypothesisId": "D",
                                        "traceId": _trace_id,
                                        "location": "src/api/routes/chat.py:ws_chat",
                                        "msg": "[DEBUG] ws_agent_done",
                                        "data": {
                                            "session_id": session_id,
                                            "chunk_count": chunk_count,
                                            "answer_len": len(answer or ""),
                                            "tool_name": tool_name,
                                        },
                                    },
                                    ensure_ascii=False,
                                ).encode("utf-8"),
                                headers={"Content-Type": "application/json"},
                            ),
                            timeout=2,
                        ).read()
                    except Exception:
                        pass
                    # #endregion
                except WebSocketDisconnect:
                    client_disconnected = True
                except Exception:
                    logger.exception("ws_chat_process_failed", session_id=session_id)
                    # #region debug-point D:ws-agent-exception
                    try:
                        import json as _json, urllib.request as _ur, traceback as _tb

                        _p = ".dbg/login-chat-timeout.env"
                        _u = "http://127.0.0.1:7778/event"
                        _s = "login-chat-timeout"
                        try:
                            with open(_p, encoding="utf-8") as _f:
                                _c = _f.read().splitlines()
                                _u = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SERVER_URL=")), _u)
                                _s = next((l.split("=", 1)[1] for l in _c if l.startswith("DEBUG_SESSION_ID=")), _s)
                        except Exception:
                            pass
                        _ur.urlopen(
                            _ur.Request(
                                _u,
                                data=_json.dumps(
                                    {
                                        "sessionId": _s,
                                        "runId": "pre",
                                        "hypothesisId": "D",
                                        "traceId": _trace_id,
                                        "location": "src/api/routes/chat.py:ws_chat",
                                        "msg": "[DEBUG] ws_agent_exception",
                                        "data": {"session_id": session_id, "error": _tb.format_exc()[-1200:]},
                                    },
                                    ensure_ascii=False,
                                ).encode("utf-8"),
                                headers={"Content-Type": "application/json"},
                            ),
                            timeout=2,
                        ).read()
                    except Exception:
                        pass
                    # #endregion
                    if not await _ws_send_json(
                        websocket,
                        {"type": "error", "error": "Failed to process message"},
                    ):
                        break
                    continue
                finally:
                    if keepalive_task is not None:
                        keepalive_task.cancel()
                        try:
                            await keepalive_task
                        except asyncio.CancelledError:
                            pass
                    await stream.aclose()

                if client_disconnected:
                    break

                if cancelled:
                    if not await _ws_send_json(websocket, {"type": "cancelled"}):
                        break
                    continue

                answer = sanitize_assistant_reply(answer)
                await sessions.add_message(
                    db,
                    session_id,
                    "assistant",
                    answer,
                    tool_calls={"tools": tools_used} if tools_used else (
                        {"tool": tool_name} if tool_name else None
                    ),
                    citations=citations or None,
                )
                await sessions.update_session_summary(session_id, message, answer)
                await db.commit()

                if not await _ws_send_json(
                    websocket,
                    {
                        "type": "done",
                        "session_id": session_id,
                        "answer": answer,
                        "tool_name": tool_name,
                        "tools_used": tools_used,
                        "citations": citations,
                        "emotion": emotion,
                    },
                ):
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("ws_unhandled_error")
        await _ws_send_json(
            websocket, {"type": "error", "error": "Internal server error"}
        )
    finally:
        if session_id:
            try:
                from src.services.handoff_bridge import get_handoff_bridge

                await get_handoff_bridge().unbind_user(session_id, websocket)
            except Exception:
                pass
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
