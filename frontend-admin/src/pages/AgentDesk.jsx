import { useCallback, useEffect, useRef, useState } from "react";
import { getApiKey } from "../api";

const WS_PATH = "/api/v1/agent/ws/desk";

async function agentFetch(path, options = {}) {
  const headers = {
    "X-API-Key": getApiKey(),
    ...(options.headers || {}),
  };
  if (!(options.body instanceof FormData) && options.body) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, { ...options, headers });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(
      typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail || err)
    );
  }
  return resp.json();
}

function formatWait(sec) {
  if (!sec || sec < 60) return `${sec || 0}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}分${s}s`;
}

export default function AgentDeskPage() {
  const [agentName, setAgentName] = useState(
    () => localStorage.getItem("kefu_agent_name") || "人工客服小周"
  );
  const [queue, setQueue] = useState([]);
  const [waitingCount, setWaitingCount] = useState(0);
  const [active, setActive] = useState(null);
  const [messages, setMessages] = useState([]);
  const [context, setContext] = useState(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("idle");
  const [loading, setLoading] = useState(false);
  const [flash, setFlash] = useState("");
  const wsRef = useRef(null);
  const listRef = useRef(null);
  const queueVersionRef = useRef(0);
  const titleBase = useRef(document.title);

  const saveName = () => {
    localStorage.setItem("kefu_agent_name", agentName.trim() || "人工客服");
  };

  const loadContext = useCallback(async (sessionId) => {
    if (!sessionId) {
      setContext(null);
      return;
    }
    try {
      const ctx = await agentFetch(`/api/v1/agent/sessions/${sessionId}/context`);
      setContext(ctx);
    } catch {
      setContext(null);
    }
  }, []);

  const loadQueue = useCallback(async () => {
    try {
      const waiting = await agentFetch("/api/v1/agent/queue?status=Received");
      const mine = await agentFetch("/api/v1/agent/queue?status=InReview");
      const mineItems = (mine.items || []).filter(
        (i) => !i.assigned_agent || i.assigned_agent === agentName
      );
      const next = [...(waiting.items || []), ...mineItems];
      setQueue(next);
      setWaitingCount(waiting.waiting_count || (waiting.items || []).length);

      if (waiting.auto_released?.length) {
        setFlash(`已自动退回超时认领：${waiting.auto_released.join(", ")}`);
      }
      const ver = waiting.queue_version || 0;
      if (queueVersionRef.current && ver > queueVersionRef.current && (waiting.items || []).length) {
        setFlash(`新转人工 ${(waiting.items || []).length} 单待认领`);
        document.title = `(${waiting.items.length}) 人工客服台`;
        try {
          const ctx = new (window.AudioContext || window.webkitAudioContext)();
          const o = ctx.createOscillator();
          const g = ctx.createGain();
          o.connect(g);
          g.connect(ctx.destination);
          o.frequency.value = 880;
          g.gain.value = 0.04;
          o.start();
          setTimeout(() => {
            o.stop();
            ctx.close();
          }, 180);
        } catch {
          /* ignore audio */
        }
      }
      queueVersionRef.current = ver;
    } catch (e) {
      setError(e.message);
    }
  }, [agentName]);

  useEffect(() => {
    loadQueue();
    const t = setInterval(loadQueue, 4000);
    return () => clearInterval(t);
  }, [loadQueue]);

  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [messages]);

  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => {
      setFlash("");
      document.title = titleBase.current || "智能客服管理后台";
    }, 4000);
    return () => clearTimeout(t);
  }, [flash]);

  function disconnectWs() {
    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch {
        /* ignore */
      }
      wsRef.current = null;
    }
    setStatus("idle");
  }

  function connectWs(sessionId) {
    disconnectWs();
    const key = getApiKey();
    if (!key) {
      setError("请先在侧栏保存 API Key");
      return;
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}${WS_PATH}?api_key=${encodeURIComponent(key)}&agent=${encodeURIComponent(agentName)}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;
    setStatus("connecting");

    ws.onopen = () => {
      setStatus("online");
      ws.send(JSON.stringify({ type: "attach", session_id: sessionId }));
    };
    ws.onclose = () => {
      setStatus("offline");
      wsRef.current = null;
    };
    ws.onerror = () => setStatus("offline");
    ws.onmessage = (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (data.type === "user_message") {
        setMessages((prev) => [
          ...prev,
          { role: "user", content: data.content, created_at: new Date().toISOString() },
        ]);
        setFlash("用户发来新消息");
        document.title = "● 新消息 · 人工客服台";
        loadQueue();
        if (ws.readyState === 1) {
          ws.send(JSON.stringify({ type: "read", session_id: sessionId }));
        }
      } else if (data.type === "error") {
        setError(data.error || "坐席通道错误");
      }
    };
  }

  async function claim(item) {
    setLoading(true);
    setError("");
    saveName();
    try {
      const res = await agentFetch(`/api/v1/agent/complaints/${item.id}/claim`, {
        method: "POST",
        body: JSON.stringify({ agent_name: agentName }),
      });
      const ticket = {
        ...item,
        status: "InReview",
        assigned_agent: res.agent_name,
        session_id: res.session_id,
        claimed_at: res.claimed_at,
      };
      setActive(ticket);
      const hist = await agentFetch(`/api/v1/agent/sessions/${res.session_id}/messages`);
      setMessages(hist.messages || []);
      await loadContext(res.session_id);
      connectWs(res.session_id);
      await loadQueue();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function openMine(item) {
    if (!item.session_id) {
      setError("该工单没有会话 ID，无法聊天");
      return;
    }
    setActive(item);
    setError("");
    try {
      const hist = await agentFetch(`/api/v1/agent/sessions/${item.session_id}/messages`);
      setMessages(hist.messages || []);
      await loadContext(item.session_id);
      connectWs(item.session_id);
      await agentFetch(`/api/v1/agent/sessions/${item.session_id}/read`, { method: "POST" });
      await loadQueue();
    } catch (e) {
      setError(e.message);
    }
  }

  function send() {
    const text = draft.trim();
    if (!text || !active?.session_id || !wsRef.current || wsRef.current.readyState !== 1) {
      return;
    }
    wsRef.current.send(
      JSON.stringify({
        type: "message",
        session_id: active.session_id,
        content: text,
        agent_name: agentName,
      })
    );
    setMessages((prev) => [
      ...prev,
      { role: "agent", content: text, created_at: new Date().toISOString() },
    ]);
    setDraft("");
  }

  async function resolve() {
    if (!active) return;
    setLoading(true);
    setError("");
    try {
      await agentFetch(`/api/v1/agent/complaints/${active.id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ agent_name: agentName, note: "已人工处理完毕" }),
      });
      disconnectWs();
      setActive(null);
      setMessages([]);
      setContext(null);
      await loadQueue();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function release() {
    if (!active) return;
    if (!window.confirm("退回排队？用户会继续等待其他坐席。")) return;
    setLoading(true);
    try {
      await agentFetch(`/api/v1/agent/complaints/${active.id}/release`, { method: "POST" });
      disconnectWs();
      setActive(null);
      setMessages([]);
      setContext(null);
      await loadQueue();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  const unreadTotal = queue.reduce((n, i) => n + (i.unread || 0), 0);

  return (
    <div className="agent-desk">
      <header className="page-header">
        <h2>
          人工客服台
          {waitingCount > 0 && <span className="badge-count">{waitingCount}</span>}
          {unreadTotal > 0 && <span className="badge-count unread">{unreadTotal} 未读</span>}
        </h2>
        <div className="header-actions" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label>
            坐席名
            <input
              value={agentName}
              onChange={(e) => setAgentName(e.target.value)}
              onBlur={saveName}
              style={{ marginLeft: 6 }}
            />
          </label>
          <span className={`badge badge-${status === "online" ? "Resolved" : "Received"}`}>
            {status}
          </span>
          <button type="button" onClick={loadQueue}>
            刷新队列
          </button>
        </div>
      </header>

      <p className="muted">
        认领超时 {15} 分钟且坐席离线会自动退回排队。右侧为用户上下文卡片。
      </p>
      {flash && <p className="ok">{flash}</p>}
      {error && <p className="error">{error}</p>}

      <div className="desk-grid desk-grid-3">
        <section className="desk-queue">
          <h3>排队 / 进行中 ({queue.length})</h3>
          <ul>
            {queue.map((item) => (
              <li key={item.id} className={item.unread ? "has-unread" : ""}>
                <div>
                  <strong>{item.ticket_id}</strong>
                  <span className={`badge badge-${item.status}`}>{item.status}</span>
                  {item.unread > 0 && <span className="badge-count unread">{item.unread}</span>}
                </div>
                <p>{item.details}</p>
                <p className="muted">
                  等待 {formatWait(item.wait_seconds)} · {item.session_id?.slice(0, 8) || "无会话"}
                  {item.agent_online ? " · 在线" : ""}
                </p>
                {item.status === "Received" ? (
                  <button type="button" disabled={loading} onClick={() => claim(item)}>
                    认领并对话
                  </button>
                ) : (
                  <button type="button" onClick={() => openMine(item)}>
                    继续对话
                  </button>
                )}
              </li>
            ))}
            {queue.length === 0 && <li className="muted">暂无转人工工单</li>}
          </ul>
        </section>

        <section className="desk-chat">
          {active ? (
            <>
              <div className="desk-chat-head">
                <div>
                  <strong>{active.ticket_id}</strong>
                  <span className="muted"> · 会话 {active.session_id?.slice(0, 8)}</span>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <button type="button" onClick={release} disabled={loading}>
                    退回排队
                  </button>
                  <button type="button" className="danger" disabled={loading} onClick={resolve}>
                    结束并交回
                  </button>
                </div>
              </div>
              <div className="desk-messages" ref={listRef}>
                {messages.map((m, i) => (
                  <div key={`${m.role}-${i}`} className={`desk-msg desk-msg-${m.role}`}>
                    <span className="desk-role">
                      {m.role === "user" ? "用户" : m.role === "agent" ? "我" : "系统/饺子"}
                    </span>
                    <div className="desk-bubble">{m.content}</div>
                  </div>
                ))}
              </div>
              <div className="desk-composer">
                <textarea
                  rows={3}
                  value={draft}
                  placeholder="输入回复… Enter 发送"
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      send();
                    }
                  }}
                />
                <button type="button" className="btn-primary" onClick={send}>
                  发送
                </button>
              </div>
            </>
          ) : (
            <div className="desk-empty">
              <h3>还没有接入会话</h3>
              <p>从左侧认领一张转人工工单开始。</p>
            </div>
          )}
        </section>

        <section className="desk-context">
          <h3>上下文卡片</h3>
          {!context ? (
            <p className="muted">认领会话后显示用户、订单与对话摘要。</p>
          ) : (
            <>
              <div className="ctx-block">
                <h4>用户</h4>
                <p>{context.user?.email || "未登录 / 无邮箱"}</p>
                {context.user?.name && <p className="muted">{context.user.name}</p>}
                <p className="muted">会话状态：{context.session?.status}</p>
              </div>
              <div className="ctx-block">
                <h4>工单</h4>
                <p>{context.ticket?.ticket_id || "—"}</p>
                <p className="muted">{context.ticket?.details}</p>
              </div>
              <div className="ctx-block">
                <h4>订单 ({context.orders?.length || 0})</h4>
                {(context.orders || []).length === 0 && <p className="muted">暂无关联订单</p>}
                <ul className="ctx-orders">
                  {(context.orders || []).map((o) => (
                    <li key={o.order_id}>
                      <strong>{o.order_id}</strong> {o.status}
                      <br />
                      <span className="muted">
                        {o.currency} {o.total} · {o.carrier || "-"} {o.tracking_number || ""}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
              <div className="ctx-block">
                <h4>AI 已用工具</h4>
                <p>{(context.tools_used || []).join("、") || "无"}</p>
              </div>
              <div className="ctx-block">
                <h4>对话摘要</h4>
                <pre className="ctx-summary">{context.summary || "暂无"}</pre>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
