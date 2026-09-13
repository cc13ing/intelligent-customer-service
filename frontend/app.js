const API_BASE = window.location.origin;
const WS_BASE = API_BASE.replace(/^http/, "ws");
const CONFIG = window.__KEFU_CONFIG__ || { apiKey: "", wsPath: "/api/v1/ws/chat" };

const SESSION_STORAGE_KEY = "kefu_session_id";
const HISTORY_STORAGE_KEY = "kefu_session_history";
const MAX_HISTORY = 30;

let sessionId = localStorage.getItem(SESSION_STORAGE_KEY);
let ws = null;
let isConnected = false;
let connState = "connecting";
let reconnectAttempts = 0;
const WS_MAX_RECONNECT = 8;
const WS_REPLY_TIMEOUT_MS = 90000;
let wsReplyTimer = null;
let authToken = localStorage.getItem("kefu_token") || "";
let currentUser = null;
let lastUserMessage = "";
let socketGeneration = 0;
let reconnectTimer = null;
let heartbeatTimer = null;
let requestInFlight = false;
let chatAbortController = null;
let lastMoodAt = 0;

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send");
const cancelBtn = document.getElementById("cancel");
const newSessionBtn = document.getElementById("newSession");
const sessionInfoEl = document.getElementById("sessionInfo");
const sessionListEl = document.getElementById("sessionList");
const authStatusEl = document.getElementById("authStatus");
const loginEmailEl = document.getElementById("loginEmail");
const loginPasswordEl = document.getElementById("loginPassword");
const loginBtn = document.getElementById("loginBtn");
const registerBtn = document.getElementById("registerBtn");
const changePwdBox = document.getElementById("changePwdBox");
const changePwdBtn = document.getElementById("changePwdBtn");
const ticketsPanelEl = document.getElementById("ticketsPanel");
const ticketsListEl = document.getElementById("ticketsList");
const logoutBtn = document.getElementById("logoutBtn");
const ordersPanelEl = document.getElementById("ordersPanel");
const ordersListEl = document.getElementById("ordersList");
const connStatusEl = document.getElementById("connStatus");
const quickPromptsEl = document.getElementById("quickPrompts");

const TOOL_LABELS = {
  fetch_logistics_information: "物流查询",
  record_user_complaint: "投诉记录",
  create_return_request: "退换货",
  customer_chat: "知识库",
  query_order: "订单查询",
  query_my_orders: "我的订单",
};

function authHeaders() {
  const headers = { "Content-Type": "application/json" };
  if (CONFIG.apiKey) headers["X-API-Key"] = CONFIG.apiKey;
  if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
  return headers;
}

function updateConnStatus() {
  if (!connStatusEl) return;
  const labels = {
    connected: t("connConnected"),
    connecting: t("connConnecting"),
    disconnected: t("connDisconnected"),
    failed: t("connFailed"),
  };
  connStatusEl.textContent = labels[connState] || connState;
  connStatusEl.dataset.state = connState;
  connStatusEl.setAttribute("aria-live", "polite");
}

function showToast(message, ok = false) {
  let el = document.getElementById("appToast");
  if (!el) {
    el = document.createElement("div");
    el.id = "appToast";
    el.className = "app-toast";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.toggle("ok", !!ok);
  el.classList.add("show");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.remove("show"), 3200);
}

function clearWsReplyTimer() {
  if (wsReplyTimer) {
    clearTimeout(wsReplyTimer);
    wsReplyTimer = null;
  }
}

function armWsReplyTimer() {
  clearWsReplyTimer();
  wsReplyTimer = setTimeout(() => {
    if (!requestInFlight) return;
    finishPendingAssistant(t("wsTimeout") || "回复超时，请重试");
    setRequestInFlight(false);
    showToast(t("wsTimeout") || "回复超时，请重试");
    forceReconnectWebSocket();
  }, WS_REPLY_TIMEOUT_MS);
}

function getSessionHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveSessionHistory(history) {
  localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(history.slice(0, MAX_HISTORY)));
}

function upsertSessionHistory(id, title) {
  if (!id) return;
  const preview = (title || "").trim().slice(0, 40) || id.slice(0, 8);
  const history = getSessionHistory().filter((item) => item.id !== id);
  history.unshift({ id, title: preview, updatedAt: Date.now() });
  saveSessionHistory(history);
  renderSessionList();
}

function persistSessionId(id) {
  sessionId = id;
  if (id) {
    localStorage.setItem(SESSION_STORAGE_KEY, id);
  } else {
    localStorage.removeItem(SESSION_STORAGE_KEY);
  }
  if (sessionInfoEl) {
    sessionInfoEl.textContent = id ? `${t("session")}: ${id}` : "";
  }
  renderSessionList();
  // 会话切换后重新绑定，保证人工消息能推到当前页
  if (id && ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({ type: "bind", session_id: id }));
    } catch {
      /* ignore */
    }
  }
}

function removeSessionFromHistory(id) {
  const history = getSessionHistory().filter((item) => item.id !== id);
  saveSessionHistory(history);
  if (sessionId === id) {
    persistSessionId(null);
    clearMessages();
  }
  renderSessionList();
}

async function deleteSession(id, event) {
  if (event) {
    event.stopPropagation();
    event.preventDefault();
  }
  if (!window.confirm(t("confirmDeleteSession"))) return;

  try {
    const resp = await fetch(`${API_BASE}/api/v1/sessions/${id}`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || resp.statusText);
    }
  } catch (e) {
    console.warn("deleteSession failed", e);
  }
  removeSessionFromHistory(id);
}

function renderSessionList() {
  if (!sessionListEl) return;
  const history = getSessionHistory();
  sessionListEl.innerHTML = "";

  if (history.length === 0) {
    const empty = document.createElement("li");
    empty.className = "session-list-empty";
    empty.textContent = t("noHistory");
    sessionListEl.appendChild(empty);
    return;
  }

  history.forEach((item) => {
    const li = document.createElement("li");
    li.className = "session-list-item";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `session-open-btn${item.id === sessionId ? " active" : ""}`;
    btn.innerHTML = `
      <span class="session-preview">${escapeHtml(item.title)}</span>
      <span class="session-id">${escapeHtml(item.id.slice(0, 8))}…</span>
    `;
    btn.addEventListener("click", () => loadSession(item.id));

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "session-delete-btn";
    delBtn.title = t("deleteSession");
    delBtn.setAttribute("aria-label", t("deleteSession"));
    delBtn.textContent = "×";
    delBtn.addEventListener("click", (e) => deleteSession(item.id, e));

    li.appendChild(btn);
    li.appendChild(delBtn);
    sessionListEl.appendChild(li);
  });
}

function renderQuickPrompts() {
  if (!quickPromptsEl) return;
  quickPromptsEl.innerHTML = "";
  const label = document.createElement("span");
  label.className = "quick-label";
  label.textContent = t("quickPrompts");
  quickPromptsEl.appendChild(label);

  getPrompts().forEach((p) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "quick-btn";
    btn.textContent = p.label;
    btn.title = p.text;
    btn.addEventListener("click", () => {
      if (inputEl) {
        inputEl.value = p.text;
        inputEl.focus();
      }
    });
    quickPromptsEl.appendChild(btn);
  });
}

async function loadOrders() {
  if (!ordersPanelEl || !ordersListEl) return;
  if (!currentUser || !authToken) {
    ordersPanelEl.hidden = true;
    return;
  }
  ordersPanelEl.hidden = false;
  ordersListEl.innerHTML = `<li class="orders-loading">…</li>`;
  try {
    const resp = await fetch(`${API_BASE}/api/v1/users/me/orders`, {
      headers: authHeaders(),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    const orders = data.data?.orders || [];
    ordersListEl.innerHTML = "";
    if (orders.length === 0) {
      ordersListEl.innerHTML = `<li class="orders-empty">${t("noOrders")}</li>`;
      return;
    }
    orders.forEach((o) => {
      const li = document.createElement("li");
      li.className = "order-item";
      li.innerHTML = `
        <span class="order-id">${escapeHtml(o.order_id || "")}</span>
        <span class="order-status">${escapeHtml(o.status || "")}</span>
        <span class="order-meta">${escapeHtml(o.carrier || "")} ${escapeHtml(o.tracking_number || "")}</span>
      `;
      li.addEventListener("click", () => {
        if (inputEl) {
          inputEl.value = `查询订单 ${o.order_id} 的物流信息`;
          inputEl.focus();
        }
      });
      ordersListEl.appendChild(li);
    });
  } catch {
    ordersListEl.innerHTML = `<li class="orders-empty">${t("ordersLoadFailed")}</li>`;
  }
}

function clearMessages() {
  if (!messagesEl) return;
  messagesEl.innerHTML = "";
  ensureWelcomeState();
}

function ensureWelcomeState() {
  if (!messagesEl) return;
  const hasMessages = messagesEl.querySelector(".message, .message-row");
  let welcome = document.getElementById("welcomeState");
  if (hasMessages) {
    if (welcome) welcome.remove();
    return;
  }
  if (!welcome) {
    welcome = document.createElement("div");
    welcome.className = "welcome";
    welcome.id = "welcomeState";
    welcome.innerHTML = `
      <div class="welcome-stage">
        <img src="/assets/avatar-cs.png" alt="您的专属客服——饺子" class="welcome-avatar" />
        <div class="welcome-burst" aria-hidden="true"></div>
        <span class="welcome-online" aria-hidden="true"></span>
        <span class="welcome-sticker welcome-sticker-a">热乎</span>
        <span class="welcome-sticker welcome-sticker-b">在线</span>
      </div>
      <p class="welcome-agent"></p>
      <p class="welcome-brand" id="welcomeBrand"></p>
      <p class="welcome-lead"></p>
      <div class="scene-grid" id="sceneGridWelcome"></div>
    `;
    messagesEl.appendChild(welcome);
  }
  paintWelcomeType(welcome);
  renderSceneButtons();
}

function paintWelcomeType(welcome) {
  if (!welcome) return;
  const lang = typeof currentLang !== "undefined" ? currentLang : "zh";
  const brand = welcome.querySelector(".welcome-brand");
  const lead = welcome.querySelector(".welcome-lead");
  const agent = welcome.querySelector(".welcome-agent");
  if (brand) {
    if (lang === "en") {
      brand.innerHTML = `<span class="type-ink">Jia</span><span class="type-pink">ozi</span>`;
    } else if (lang === "ar") {
      brand.innerHTML = `<span class="type-pink">جياوزي</span>`;
    } else {
      brand.innerHTML = `<span class="type-ink">饺</span><span class="type-pink">子</span>`;
    }
  }
  if (lead) {
    if (lang === "en") {
      lead.innerHTML = `
        <span class="type-lead-a">Orders</span>
        <span class="type-dot">·</span>
        <span class="type-lead-b">Tracking</span>
        <span class="type-dot">·</span>
        <span class="type-lead-c">Returns</span>
        <br />
        <span class="type-lead-tail">Dump it on Jiaozi.</span>`;
    } else if (lang === "ar") {
      lead.innerHTML = `<span class="type-lead-tail">${t("welcomeLead")}</span>`;
    } else {
      lead.innerHTML = `
        <span class="type-lead-a">查订单</span>
        <span class="type-dot">·</span>
        <span class="type-lead-b">追物流</span>
        <span class="type-dot">·</span>
        <span class="type-lead-c">办退换</span>
        <br />
        <span class="type-lead-tail">丢给饺子就行。</span>`;
    }
  }
  if (agent) {
    const agentText =
      lang === "en"
        ? "Your dedicated agent — Jiaozi"
        : lang === "ar"
          ? "وكيلك الخاص — جياوزي"
          : "您的专属客服——饺子";
    agent.innerHTML = `<span class="type-chip type-chip-soft">${agentText}</span>`;
  }
}

function hideWelcomeState() {
  const welcome = document.getElementById("welcomeState");
  if (welcome) welcome.remove();
}

function renderMessages(messages) {
  clearMessages();
  messages.forEach((m) => {
    if (m.role === "user" || m.role === "assistant" || m.role === "agent") {
      appendMessage(m.role, m.content || "", m.citations || [], m.tools_used || []);
    }
  });
}

async function loadSession(id) {
  if (!id) return;
  try {
    const resp = await fetch(`${API_BASE}/api/v1/sessions/${id}`, {
      headers: authHeaders(),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    persistSessionId(id);
    renderMessages(data.messages || []);
    const firstUser = (data.messages || []).find((m) => m.role === "user");
    upsertSessionHistory(id, firstUser?.content || id);
  } catch (e) {
    removeSessionFromHistory(id);
    renderSessionList();
    console.warn("loadSession failed", e);
  }
}

function updateAuthUI() {
  if (!authStatusEl) return;
  if (currentUser) {
    authStatusEl.textContent = `${t("loggedInAs")}: ${currentUser.email}`;
    if (loginBtn) loginBtn.hidden = true;
    if (registerBtn) registerBtn.hidden = true;
    if (logoutBtn) logoutBtn.hidden = false;
    if (loginEmailEl) loginEmailEl.hidden = true;
    if (loginPasswordEl) loginPasswordEl.hidden = true;
    if (changePwdBox) changePwdBox.hidden = false;
  } else {
    authStatusEl.textContent = t("notLoggedIn");
    if (loginBtn) loginBtn.hidden = false;
    if (registerBtn) registerBtn.hidden = false;
    if (logoutBtn) logoutBtn.hidden = true;
    if (loginEmailEl) loginEmailEl.hidden = false;
    if (loginPasswordEl) loginPasswordEl.hidden = false;
    if (changePwdBox) changePwdBox.hidden = true;
    if (ordersPanelEl) ordersPanelEl.hidden = true;
    if (ticketsPanelEl) ticketsPanelEl.hidden = true;
  }
}

async function fetchMe() {
  try {
    const resp = await fetch(`${API_BASE}/api/v1/auth/me`, { headers: authHeaders() });
    const data = await resp.json();
    currentUser = data.authenticated ? data.user : null;
  } catch {
    currentUser = null;
  }
  updateAuthUI();
  await loadOrders();
  await loadTickets();
  await loadServerSessions();
}

async function login() {
  const email = (loginEmailEl?.value || "").trim();
  const password = (loginPasswordEl?.value || "").trim();
  if (!email || !password) {
    showToast("请输入邮箱和密码");
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/api/v1/auth/login`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ email, password }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    authToken = data.access_token;
    localStorage.setItem("kefu_token", authToken);
    currentUser = data.user;
    localStorage.removeItem(HISTORY_STORAGE_KEY);
    updateAuthUI();
    await loadOrders();
    await loadTickets();
    await loadServerSessions();
    forceReconnectWebSocket();
  } catch (e) {
    showToast(`${t("error")}: ${e.message}`);
  }
}

async function register() {
  const email = (loginEmailEl?.value || "").trim();
  const password = (loginPasswordEl?.value || "").trim();
  if (!email || !password) {
    showToast("请输入邮箱和密码（至少6位）");
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/api/v1/auth/register`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ email, password, name: email.split("@")[0] }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    authToken = data.access_token;
    localStorage.setItem("kefu_token", authToken);
    currentUser = data.user;
    localStorage.removeItem(HISTORY_STORAGE_KEY);
    updateAuthUI();
    await loadOrders();
    await loadTickets();
    await loadServerSessions();
    forceReconnectWebSocket();
    showToast("注册成功");
  } catch (e) {
    showToast(`${t("error")}: ${e.message}`);
  }
}

async function changePassword() {
  const oldPassword = (document.getElementById("oldPassword")?.value || "").trim();
  const newPassword = (document.getElementById("newPassword")?.value || "").trim();
  if (!oldPassword || !newPassword) {
    showToast("请填写原密码和新密码");
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/api/v1/auth/change-password`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    showToast(data.message || "密码已更新");
    const oldEl = document.getElementById("oldPassword");
    const newEl = document.getElementById("newPassword");
    if (oldEl) oldEl.value = "";
    if (newEl) newEl.value = "";
  } catch (e) {
    showToast(`${t("error")}: ${e.message}`);
  }
}

async function logout() {
  try {
    await fetch(`${API_BASE}/api/v1/auth/logout`, { method: "POST", headers: authHeaders() });
  } catch {
    /* ignore */
  }
  authToken = "";
  localStorage.removeItem("kefu_token");
  localStorage.removeItem(HISTORY_STORAGE_KEY);
  currentUser = null;
  persistSessionId("");
  updateAuthUI();
  renderSessionList();
  forceReconnectWebSocket();
}

async function loadTickets() {
  if (!ticketsPanelEl || !ticketsListEl) return;
  if (!authToken || !currentUser) {
    ticketsPanelEl.hidden = true;
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/api/v1/users/me/tickets`, { headers: authHeaders() });
    if (!resp.ok) {
      ticketsPanelEl.hidden = true;
      return;
    }
    const data = await resp.json();
    const items = data.items || [];
    ticketsListEl.innerHTML = "";
    if (!items.length) {
      ticketsPanelEl.hidden = false;
      ticketsListEl.innerHTML = "<li class='muted'>暂无工单</li>";
      return;
    }
    ticketsPanelEl.hidden = false;
    for (const tkt of items) {
      const li = document.createElement("li");
      li.className = "order-item";
      li.innerHTML = `<strong>${tkt.ticket_id}</strong> · ${tkt.status}${tkt.is_handoff ? " · 转人工" : ""}`;
      li.title = tkt.details || "";
      li.addEventListener("click", () => {
        const input = document.getElementById("messageInput");
        if (input) {
          input.value = `查询工单 ${tkt.ticket_id}`;
          input.focus();
        }
      });
      ticketsListEl.appendChild(li);
    }
  } catch {
    ticketsPanelEl.hidden = true;
  }
}

async function loadServerSessions() {
  if (!authToken || !currentUser) {
    renderSessionList();
    return;
  }
  try {
    const resp = await fetch(`${API_BASE}/api/v1/users/me/sessions`, { headers: authHeaders() });
    if (!resp.ok) {
      renderSessionList();
      return;
    }
    const data = await resp.json();
    const items = (data.items || []).map((s) => ({
      id: s.id,
      title: s.id.slice(0, 8),
      updatedAt: s.updated_at || s.created_at || Date.now(),
    }));
    localStorage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(items.slice(0, MAX_HISTORY)));
    renderSessionList();
  } catch {
    renderSessionList();
  }
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function sanitizeAssistantContent(text) {
  if (!text) return "";
  let s = String(text);
  // Strip model thinking / leak blocks only — keep Markdown code fences intact
  s = s.replace(/<think>[\s\S]*?<\/think>/gi, "");
  s = s.replace(/<\/?think>/gi, "");
  s = s.replace(/<redacted_thinking>[\s\S]*?<\/redacted_thinking>/gi, "");
  s = s.replace(/<\/?redacted_thinking>/gi, "");
  s = s.replace(/<think>[\s\S]*$/i, "");
  s = s.replace(/<redacted_thinking>[\s\S]*$/i, "");
  const leak = s.search(/\n(?:user|assistant)\s*(?:\n|$)/i);
  if (leak >= 0) s = s.slice(0, leak);
  const chatml = s.search(/<\|im_start\|>(?:user|assistant)\b/i);
  if (chatml >= 0) s = s.slice(0, chatml);
  return s.trim();
}

function renderMarkdown(text) {
  const raw = String(text || "");
  if (!raw) return "";
  let html;
  if (typeof marked !== "undefined" && typeof marked.parse === "function") {
    try {
      marked.setOptions({ breaks: true, gfm: true });
      html = marked.parse(raw);
    } catch (_) {
      html = escapeHtml(raw).replace(/\n/g, "<br>");
    }
  } else {
    html = escapeHtml(raw).replace(/\n/g, "<br>");
  }
  if (typeof DOMPurify !== "undefined" && typeof DOMPurify.sanitize === "function") {
    html = DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      ADD_ATTR: ["target", "rel"],
    });
  }
  // Open external links in a new tab
  html = html.replace(
    /<a\s+([^>]*href=(["'])(?!#|javascript:)[^"']*\2[^>]*)>/gi,
    (match, attrs) => {
      if (/\btarget=/i.test(attrs)) return match;
      return `<a ${attrs} target="_blank" rel="noopener noreferrer">`;
    }
  );
  return html;
}

function formatMessageContent(text, role = "user") {
  if (role === "assistant") {
    return renderMarkdown(sanitizeAssistantContent(text));
  }
  return escapeHtml(text || "").replace(/\n/g, "<br>");
}

function formatToolLabel(name) {
  return TOOL_LABELS[name] || name;
}

function buildWsUrl() {
  const base = `${WS_BASE}${CONFIG.wsPath}`;
  const params = new URLSearchParams();
  if (CONFIG.apiKey) params.set("api_key", CONFIG.apiKey);
  if (authToken) params.set("token", authToken);
  const qs = params.toString();
  return qs ? `${base}?${qs}` : base;
}

function setRequestInFlight(busy) {
  requestInFlight = busy;
  // 未连接时仍允许点发送（走 HTTP 回退）；仅在 busy 时禁用
  if (sendBtn) sendBtn.disabled = !!busy;
  if (cancelBtn) cancelBtn.disabled = !busy;
  if (!busy) clearWsReplyTimer();
}

function finishPendingAssistant(text) {
  if (!pendingAssistantEl) return;
  pendingAssistantEl.classList.remove("typing");
  const body = pendingAssistantEl.querySelector(".message-body");
  if (body) {
    body.innerHTML = formatMessageContent(text, "assistant");
  }
  pendingAssistantEl = null;
}

function cancelInFlight() {
  if (!requestInFlight && !pendingAssistantEl) return;

  if (chatAbortController) {
    chatAbortController.abort();
    chatAbortController = null;
  }

  if (isConnected && ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify({ type: "cancel" }));
    } catch {
      forceReconnectWebSocket();
    }
  } else {
    forceReconnectWebSocket();
  }

  finishPendingAssistant(t("cancelled") || "已取消");
  setRequestInFlight(false);
}

function clearHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function startHeartbeat(socket) {
  clearHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (socket.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify({ type: "ping" }));
      } catch {
        /* ignore */
      }
    }
  }, 20000);
}

function forceReconnectWebSocket() {
  socketGeneration += 1;
  reconnectAttempts = 0;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  clearHeartbeat();
  clearWsReplyTimer();
  const old = ws;
  ws = null;
  if (old && old.readyState < WebSocket.CLOSING) {
    try {
      old.close();
    } catch {
      /* ignore */
    }
  }
  connectWebSocket();
}

function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (reconnectAttempts >= WS_MAX_RECONNECT) {
    connState = "failed";
    updateConnStatus();
    return;
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  connState = reconnectAttempts === 0 ? "connecting" : "disconnected";
  updateConnStatus();
  const gen = ++socketGeneration;
  const socket = new WebSocket(buildWsUrl());
  ws = socket;

  socket.onopen = () => {
    if (gen !== socketGeneration) return;
    isConnected = true;
    reconnectAttempts = 0;
    connState = "connected";
    updateConnStatus();
    setRequestInFlight(requestInFlight);
    startHeartbeat(socket);
    // 立即绑定当前会话，人工回复才能推到本页
    if (sessionId) {
      try {
        socket.send(JSON.stringify({ type: "bind", session_id: sessionId }));
      } catch {
        /* ignore */
      }
    }
  };

  socket.onmessage = (event) => {
    if (gen !== socketGeneration) return;
    let data;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }
    if (data.type === "ping" || data.type === "pong") return;
    if (data.type === "done" || data.type === "error" || data.type === "cancelled") {
      clearWsReplyTimer();
    }
    handleWsMessage(data);
  };

  socket.onclose = (ev) => {
    if (gen !== socketGeneration) return;
    clearHeartbeat();
    isConnected = false;
    // 鉴权失败等致命关闭码：停止空转重连
    if (ev && (ev.code === 1008 || ev.code === 4001 || ev.code === 4401)) {
      connState = "failed";
      updateConnStatus();
      if (requestInFlight || pendingAssistantEl) {
        finishPendingAssistant(t("connFailed") || "连接失败");
        setRequestInFlight(false);
      }
      if (ws === socket) ws = null;
      return;
    }
    reconnectAttempts += 1;
    if (reconnectAttempts >= WS_MAX_RECONNECT) {
      connState = "failed";
      updateConnStatus();
      if (requestInFlight || pendingAssistantEl) {
        finishPendingAssistant(t("connFailed") || "连接失败");
        setRequestInFlight(false);
      }
      if (ws === socket) ws = null;
      return;
    }
    connState = "disconnected";
    updateConnStatus();
    if (requestInFlight || pendingAssistantEl) {
      finishPendingAssistant(t("disconnected") || "连接已断开，请重试");
      setRequestInFlight(false);
    }
    if (ws === socket) ws = null;
    const delay = Math.min(1500 * reconnectAttempts, 8000);
    reconnectTimer = setTimeout(connectWebSocket, delay);
  };

  socket.onerror = () => {
    if (gen !== socketGeneration) return;
    isConnected = false;
    connState = reconnectAttempts >= WS_MAX_RECONNECT ? "failed" : "disconnected";
    updateConnStatus();
  };
}

function appendToolsBadge(parent, tools) {
  if (!tools || tools.length === 0) return;
  const wrap = document.createElement("div");
  wrap.className = "tools-badge";
  wrap.textContent = `${t("toolsUsed")}: `;
  tools.forEach((name) => {
    const tag = document.createElement("span");
    tag.className = "tool-tag";
    tag.textContent = formatToolLabel(name);
    wrap.appendChild(tag);
  });
  parent.appendChild(wrap);
}

function appendCitations(parent, citations) {
  if (!citations || citations.length === 0) return;
  const ul = document.createElement("ul");
  ul.className = "citations";
  citations.forEach((c) => {
    const li = document.createElement("li");
    li.textContent = `${t("ref")}: ${c}`;
    ul.appendChild(li);
  });
  parent.appendChild(ul);
}

function appendMessage(role, content, citations = [], toolsUsed = []) {
  if (!messagesEl) return null;
  hideWelcomeState();

  const row = document.createElement("div");
  row.className = `message-row ${role}`;

  if (role === "assistant" || role === "agent") {
    const avatar = document.createElement("img");
    avatar.className = "message-avatar";
    avatar.src =
      role === "agent"
        ? "/assets/avatar-hood.png"
        : window.__jiaoziAvatarSrc || "/assets/avatar-cs.png";
    avatar.alt = role === "agent" ? "人工客服" : "您的专属客服——饺子";
    if (window.__jiaoziEmotion) {
      avatar.dataset.emotion = window.__jiaoziEmotion;
    }
    row.appendChild(avatar);
  }

  const div = document.createElement("div");
  div.className = `message ${role}`;

  const body = document.createElement("div");
  body.className = "message-body";
  body.innerHTML = formatMessageContent(content, role === "agent" ? "assistant" : role);
  div.appendChild(body);
  if (role === "assistant") appendToolsBadge(div, toolsUsed);
  appendCitations(div, citations);

  row.appendChild(div);
  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

/** 饺子情绪：按用户问题触发「拍一拍」反应 */
const JIAOZI_AVATARS = {
  default: "/assets/avatar-default.png",
  hood: "/assets/avatar-hood.png",
  rain: "/assets/avatar-rain.png",
  couch: "/assets/avatar-couch.png",
  bunny: "/assets/avatar-bunny.png",
  dog: "/assets/avatar-dog.png",
  sideeye: "/assets/avatar-sideeye.png",
  cry: "/assets/avatar-cry.png",
};

const JIAOZI_EMOTIONS = {
  regret: {
    pat: "饺子拍了拍自己，表示很遗憾",
    label: "很遗憾",
    face: "sad",
    avatar: "cry",
  },
  sorry: {
    pat: "饺子拍了拍你，表示非常抱歉",
    label: "很抱歉",
    face: "sorry",
    avatar: "rain",
  },
  happy: {
    pat: "饺子拍了拍你，表示超开心见到你",
    label: "很开心",
    face: "happy",
    avatar: "bunny",
  },
  thanks: {
    pat: "饺子拍了拍你，表示不客气呀",
    label: "害羞",
    face: "shy",
    avatar: "hood",
  },
  curious: {
    pat: "饺子拍了拍脑袋，表示马上帮你查",
    label: "认真查",
    face: "focus",
    avatar: "dog",
  },
  cheer: {
    pat: "饺子拍了拍胸口，表示包在饺子身上",
    label: "有把握",
    face: "cheer",
    avatar: "sideeye",
  },
  listen: {
    pat: "饺子拍了拍耳朵，表示在认真听",
    label: "在听",
    face: "listen",
    avatar: "couch",
  },
};

function jiaoziAvatarUrl(emotion) {
  const key = (emotion && emotion.avatar) || "default";
  return JIAOZI_AVATARS[key] || JIAOZI_AVATARS.default;
}

function applyJiaoziAvatar(emotionKey) {
  const emotion = JIAOZI_EMOTIONS[emotionKey] || JIAOZI_EMOTIONS.listen;
  const url = jiaoziAvatarUrl(emotion);
  const cacheBust = `${url}?v=emotion`;
  document
    .querySelectorAll(
      ".mascot-avatar, .agent-chip-avatar, .welcome-avatar, .brand-mark .avatar-img, .message-avatar"
    )
    .forEach((img) => {
      if (img && img.tagName === "IMG") {
        img.src = cacheBust;
      }
    });
  // 同步默认资源路径，后续新消息头像也用当前情绪图
  window.__jiaoziAvatarSrc = cacheBust;
}

function detectJiaoziEmotion(text) {
  const t = (text || "").toLowerCase();
  if (
    /退货|退款|换货|退换|不想要|return|refund|exchange|money\s*back|إرجاع|استرداد/.test(t)
  ) {
    return "regret";
  }
  if (/投诉|态度差|欺诈|骗子|生气|愤怒|complaint|angry|terrible|awful|شكوى|غاضب/.test(t)) {
    return "sorry";
  }
  if (/谢谢|感谢|thanks|thank\s*you|xiexie|شكرا/.test(t)) {
    return "thanks";
  }
  if (
    /^(你好|您好|哈喽|嗨|hello|hi|hey|nihao|مرحبا|أهلا)[\s!.?，,~！؟]*$/i.test(text.trim())
  ) {
    return "happy";
  }
  if (/订单|物流|运单|快递|tracking|order|shipment|到哪|طلب|شحن|تتبع/.test(t)) {
    return "curious";
  }
  if (/帮我|麻烦|可以吗|怎么办|怎么弄|please|help|ساعد|مساعدة/.test(t)) {
    return "cheer";
  }
  return "listen";
}

function appendPatReaction(emotionKey) {
  if (!messagesEl) return;
  const emotion = JIAOZI_EMOTIONS[emotionKey] || JIAOZI_EMOTIONS.listen;
  const el = document.createElement("div");
  el.className = `pat-reaction pat-${emotion.face}`;
  el.setAttribute("role", "status");
  el.innerHTML = `<span class="pat-reaction-text">${escapeHtml(emotion.pat)}</span>`;
  messagesEl.appendChild(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  // 短暂高亮后淡成常态微信灰条
  requestAnimationFrame(() => el.classList.add("pat-show"));
}

function setJiaoziMood(emotionKey) {
  lastMoodAt = Date.now();
  const emotion = JIAOZI_EMOTIONS[emotionKey] || JIAOZI_EMOTIONS.listen;
  window.__jiaoziEmotion = emotionKey;
  document.body.dataset.jiaoziMood = emotion.face;

  const chip = document.querySelector(".agent-chip");
  if (chip) {
    chip.dataset.mood = emotion.face;
    let badge = chip.querySelector(".agent-mood-badge");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "agent-mood-badge";
      chip.appendChild(badge);
    }
    badge.textContent = emotion.label;
    badge.classList.remove("mood-pop");
    void badge.offsetWidth;
    badge.classList.add("mood-pop");
  }

  const stage = document.getElementById("mascotStage");
  const mascotAvatar = document.getElementById("mascotAvatar");
  const moodLabel = document.getElementById("mascotMoodLabel");
  const bubble = document.getElementById("mascotBubble");
  const bubbleText = document.getElementById("mascotBubbleText");
  if (stage) stage.dataset.mood = emotion.face;
  if (moodLabel) moodLabel.textContent = emotion.label;
  if (bubbleText) bubbleText.textContent = emotion.pat;
  if (bubble) {
    bubble.classList.remove("bubble-pop");
    void bubble.offsetWidth;
    bubble.classList.add("bubble-pop");
  }

  document
    .querySelectorAll(".agent-chip-avatar, .welcome-avatar, .brand-mark, .mascot-avatar")
    .forEach((node) => {
      node.classList.remove(
        "mood-sad",
        "mood-sorry",
        "mood-happy",
        "mood-shy",
        "mood-focus",
        "mood-cheer",
        "mood-listen"
      );
      node.classList.add(`mood-${emotion.face}`);
    });

  if (mascotAvatar) {
    void mascotAvatar.offsetWidth;
  }

  applyJiaoziAvatar(emotionKey);
}

function reactAsJiaozi(userText, serverEmotion) {
  const key =
    serverEmotion && JIAOZI_EMOTIONS[serverEmotion]
      ? serverEmotion
      : detectJiaoziEmotion(userText || "");
  setJiaoziMood(key);
  appendPatReaction(key);
  return key;
}

function syncMascotBubbleFromReply(answer) {
  const bubbleText = document.getElementById("mascotBubbleText");
  const bubble = document.getElementById("mascotBubble");
  if (!bubbleText || !answer) return;
  const plain = String(answer)
    .replace(/[#>*_`~\-\[\]\(\)]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  const snip = plain.slice(0, 36) + (plain.length > 36 ? "…" : "");
  if (!snip) return;
  bubbleText.textContent = snip;
  if (bubble) {
    bubble.classList.remove("bubble-pop");
    void bubble.offsetWidth;
    bubble.classList.add("bubble-pop");
  }
}

const SCENE_ACTIONS = [
  {
    id: "orders",
    titleKey: "sceneOrdersTitle",
    descKey: "sceneOrdersDesc",
    textKey: "sceneOrdersText",
    emotion: "curious",
    tone: "sky",
  },
  {
    id: "track",
    titleKey: "sceneTrackTitle",
    descKey: "sceneTrackDesc",
    textKey: "sceneTrackText",
    emotion: "curious",
    tone: "lime",
  },
  {
    id: "return",
    titleKey: "sceneReturnTitle",
    descKey: "sceneReturnDesc",
    textKey: "sceneReturnText",
    emotion: "regret",
    tone: "pink",
  },
];

function getSceneActions() {
  return SCENE_ACTIONS.map((scene) => ({
    ...scene,
    title: t(scene.titleKey),
    desc: t(scene.descKey),
    text: t(scene.textKey),
  }));
}

function renderSceneButtons() {
  const targets = [
    document.getElementById("sceneGridWelcome"),
    document.getElementById("sceneGridBar"),
  ].filter(Boolean);
  const actions = getSceneActions();
  targets.forEach((grid) => {
    grid.innerHTML = "";
    actions.forEach((scene) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `scene-card scene-${scene.tone}`;
      btn.innerHTML = `
        <span class="scene-card-title">${escapeHtml(scene.title)}</span>
        <span class="scene-card-desc">${escapeHtml(scene.desc)}</span>
      `;
      btn.addEventListener("click", () => runSceneAction(scene));
      grid.appendChild(btn);
    });
  });
}

function runSceneAction(scene) {
  if (requestInFlight) return;
  if (!inputEl) return;
  inputEl.value = scene.text;
  window.__sceneEmotionHint = scene.emotion;
  sendMessage();
}

function formatApiError(detail, fallback) {
  if (!detail) return fallback || "请求失败";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === "string" ? d : d.msg || JSON.stringify(d)))
      .join("；");
  }
  if (typeof detail === "object" && detail.msg) return detail.msg;
  try {
    return JSON.stringify(detail);
  } catch {
    return fallback || "请求失败";
  }
}

async function requestHumanHandoff() {
  const btn = document.getElementById("handoffBtn");
  if (btn?.dataset.busy === "1") return;

  // 转人工优先：打断当前进行中的对话，避免被 requestInFlight 静默吞掉
  if (requestInFlight) {
    cancelInFlight();
  }

  if (btn) {
    btn.dataset.busy = "1";
    btn.disabled = true;
    btn.textContent = t("handoffBusy") || "转接中…";
  }

  setJiaoziMood("sorry");
  appendPatReaction("sorry");
  appendMessage("user", t("handoff") || "转人工");
  try {
    const payload = { reason: t("handoffReason") || "用户点击转人工" };
    if (sessionId) payload.session_id = sessionId;
    const resp = await fetch(`${API_BASE}/api/v1/handoff`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(formatApiError(data.detail, resp.statusText || t("handoffFail")));
    }
    if (data.session_id) {
      persistSessionId(data.session_id);
      upsertSessionHistory(data.session_id, t("handoff") || "转人工");
    }
    appendMessage("assistant", data.message || t("handoffOk"));
    if (data.emotion) setJiaoziMood(data.emotion);
    syncMascotBubbleFromReply(data.message || "");
  } catch (err) {
    appendMessage("assistant", `${t("handoffFail")}：${err.message || err}`);
  } finally {
    if (btn) {
      btn.dataset.busy = "0";
      btn.disabled = false;
      btn.textContent = t("handoff") || "转人工";
    }
  }
}

let pendingAssistantEl = null;

function handleWsMessage(data) {
  if (data.type === "session") {
    persistSessionId(data.session_id);
    upsertSessionHistory(data.session_id, lastUserMessage);
  } else if (data.type === "chunk" && pendingAssistantEl) {
    const current = pendingAssistantEl.dataset.full || "";
    const full = current + data.content;
    pendingAssistantEl.dataset.full = full;
    const body = pendingAssistantEl.querySelector(".message-body");
    if (body) {
      body.innerHTML = formatMessageContent(full, "assistant");
    }
    messagesEl.scrollTop = messagesEl.scrollHeight;
  } else if (data.type === "handoff_queued") {
    appendMessage("assistant", data.message || "已进入人工队列，请稍候…");
    setJiaoziMood("listen");
  } else if (data.type === "agent_joined") {
    appendMessage("assistant", data.message || "人工客服已接入");
    setJiaoziMood("cheer");
    showToast(data.message || "人工客服已接入", true);
  } else if (data.type === "agent_message") {
    if (pendingAssistantEl) {
      finishPendingAssistant("");
      setRequestInFlight(false);
    }
    appendMessage("agent", data.content || "");
    setJiaoziMood("listen");
    syncMascotBubbleFromReply(data.content || "");
  } else if (data.type === "agent_left") {
    appendMessage("assistant", data.message || "人工客服已结束，饺子继续为你效劳");
    setJiaoziMood("happy");
  } else if (data.type === "human_ack") {
    setRequestInFlight(false);
    if (pendingAssistantEl) {
      pendingAssistantEl.remove();
      pendingAssistantEl = null;
    }
  } else if (data.type === "done") {
    persistSessionId(data.session_id);
    upsertSessionHistory(data.session_id, lastUserMessage);
    if (pendingAssistantEl) {
      pendingAssistantEl.classList.remove("typing");
      const body = pendingAssistantEl.querySelector(".message-body");
      if (body) {
        body.innerHTML = formatMessageContent(data.answer || "", "assistant");
      }
      const tools = data.tools_used?.length
        ? data.tools_used
        : data.tool_name
          ? [data.tool_name]
          : [];
      appendToolsBadge(pendingAssistantEl, tools);
      appendCitations(pendingAssistantEl, data.citations);
      pendingAssistantEl = null;
    } else if (data.answer) {
      appendMessage("assistant", data.answer);
    }
    if (data.emotion) {
      setJiaoziMood(data.emotion);
    }
    syncMascotBubbleFromReply(data.answer || "");
    setRequestInFlight(false);
  } else if (data.type === "cancelled") {
    finishPendingAssistant(t("cancelled") || "已取消");
    setRequestInFlight(false);
  } else if (data.type === "error" && pendingAssistantEl) {
    pendingAssistantEl.classList.remove("typing");
    const body = pendingAssistantEl.querySelector(".message-body");
    if (body) {
      body.textContent = `${t("error")}: ${data.error || "Unknown error"}`;
    }
    pendingAssistantEl = null;
    setRequestInFlight(false);
  }
}

async function sendMessage() {
  const message = inputEl.value.trim();
  if (!message || requestInFlight) return;

  lastUserMessage = message;
  inputEl.value = "";
  setRequestInFlight(true);
  appendMessage("user", message);
  const hint = window.__sceneEmotionHint;
  window.__sceneEmotionHint = null;
  reactAsJiaozi(message, hint);

  pendingAssistantEl = appendMessage("assistant", "");
  pendingAssistantEl.classList.add("typing");
  pendingAssistantEl.dataset.full = "";

  if (isConnected && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ message, session_id: sessionId }));
    armWsReplyTimer();
  } else {
    chatAbortController = new AbortController();
    try {
      const resp = await fetch(`${API_BASE}/api/v1/chat`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ message, session_id: sessionId }),
        signal: chatAbortController.signal,
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || resp.statusText);
      persistSessionId(data.session_id);
      upsertSessionHistory(data.session_id, message);
      if (pendingAssistantEl) {
        pendingAssistantEl.classList.remove("typing");
        pendingAssistantEl.remove();
        pendingAssistantEl = null;
      }
      const tools = data.tools_used?.length
        ? data.tools_used
        : data.tool_name
          ? [data.tool_name]
          : [];
      appendMessage("assistant", data.answer, data.citations, tools);
      if (data.emotion) setJiaoziMood(data.emotion);
      syncMascotBubbleFromReply(data.answer || "");
    } catch (e) {
      if (e.name === "AbortError") {
        finishPendingAssistant(t("cancelled") || "已取消");
      } else if (pendingAssistantEl) {
        pendingAssistantEl.classList.remove("typing");
        const body = pendingAssistantEl.querySelector(".message-body");
        if (body) {
          body.textContent = `${t("requestFailed")}: ${e.message}`;
        }
        pendingAssistantEl = null;
      }
    } finally {
      chatAbortController = null;
      setRequestInFlight(false);
    }
  }
}

function startNewSession() {
  persistSessionId(null);
  clearMessages();
  lastUserMessage = "";
  forceReconnectWebSocket();
}

if (newSessionBtn) {
  newSessionBtn.addEventListener("click", startNewSession);
}

if (loginBtn) loginBtn.addEventListener("click", login);
if (registerBtn) registerBtn.addEventListener("click", register);
if (changePwdBtn) changePwdBtn.addEventListener("click", changePassword);
if (logoutBtn) logoutBtn.addEventListener("click", logout);
if (loginEmailEl) {
  loginEmailEl.value = "demo@gulf.ae";
  loginEmailEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") login();
  });
}
if (loginPasswordEl) {
  loginPasswordEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") login();
  });
}

if (sendBtn) {
  sendBtn.addEventListener("click", sendMessage);
}
if (cancelBtn) {
  cancelBtn.addEventListener("click", cancelInFlight);
}
if (inputEl) {
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
}

async function initApp() {
  renderSessionList();
  renderQuickPrompts();
  updateConnStatus();
  initOrdersImportUi();
  initSidebarToggle();
  ensureWelcomeState();
  renderSceneButtons();
  applyJiaoziAvatar("listen");
  initIdleMascot();
  initHandoffButton();
  checkDegradedMode();
  await fetchMe();
  if (sessionId) {
    await loadSession(sessionId);
  }
  connectWebSocket();
}

async function checkDegradedMode() {
  try {
    const resp = await fetch(`${API_BASE}/health/deep`);
    const data = await resp.json().catch(() => ({}));
    const redis = data?.components?.redis || data?.checks?.redis || data?.redis;
    const status = typeof redis === "string" ? redis : redis?.status;
    if (status && status !== "ok") {
      showToast(t("redisDegraded") || "缓存服务暂不可用，会话可能不稳定", false);
      const banner = document.getElementById("degradedBanner");
      if (banner) {
        banner.hidden = false;
        banner.textContent = t("redisDegraded") || "缓存服务暂不可用，会话可能不稳定";
      }
    }
  } catch {
    /* ignore */
  }
}

function initSidebarToggle() {
  const toggle = document.getElementById("sidebarToggle");
  const scrim = document.getElementById("sidebarScrim");
  const sidebar = document.getElementById("sidebar");
  if (!toggle || !sidebar) return;

  function open() {
    document.body.classList.add("sidebar-open");
    if (scrim) scrim.hidden = false;
  }
  function close() {
    document.body.classList.remove("sidebar-open");
    if (scrim) scrim.hidden = true;
  }
  toggle.addEventListener("click", () => {
    if (document.body.classList.contains("sidebar-open")) close();
    else open();
  });
  if (scrim) scrim.addEventListener("click", close);
  sidebar.querySelectorAll("button, a").forEach((el) => {
    el.addEventListener("click", () => {
      if (window.matchMedia("(max-width: 860px)").matches) close();
    });
  });
}

function initHandoffButton() {
  const btn = document.getElementById("handoffBtn");
  if (!btn) return;
  btn.addEventListener("click", () => requestHumanHandoff());
}

const IDLE_ROTATION = ["listen", "cheer", "happy", "curious"];
let idleTimer = null;
let idleIndex = 0;

function initIdleMascot() {
  if (idleTimer) clearInterval(idleTimer);
  idleTimer = setInterval(() => {
    if (requestInFlight) return;
    if (Date.now() - lastMoodAt < 12000) return;
    idleIndex = (idleIndex + 1) % IDLE_ROTATION.length;
    const key = IDLE_ROTATION[idleIndex];
    // 待机只切形象与标签，不刷拍一拍刷屏
    setJiaoziMood(key);
    const moodLabel = document.getElementById("mascotMoodLabel");
    if (moodLabel) moodLabel.textContent = t("idleStandby") || "待机中";
    const bubbleText = document.getElementById("mascotBubbleText");
    if (bubbleText) {
      const lines = [
        t("idleLine1") || "有事直说，饺子在这～",
        t("idleLine2") || "本饺子正在待机充电…",
        t("idleLine3") || "点下面大按钮也行哦",
        t("idleLine4") || "订单物流退货，随便问",
      ];
      bubbleText.textContent = lines[idleIndex % lines.length];
    }
  }, 9000);
}

const ORDERS_CSV_TEMPLATE = `order_id,email,status,destination_country,currency,total,tracking_number,carrier,created_at,items
ORD-3001,demo@gulf.ae,Shipped,AE,AED,199.00,TRK001,Aramex,2024-08-01 12:00:00,手机壳 x2
`;

function initOrdersImportUi() {
  const openBtn = document.getElementById("importOrdersBtn");
  const modal = document.getElementById("importOrdersModal");
  const fileInput = document.getElementById("ordersCsvInput");
  const mergeCheck = document.getElementById("ordersMergeCheck");
  const msgEl = document.getElementById("importOrdersMsg");
  const statsEl = document.getElementById("importOrdersStats");
  const uploadLabel = document.getElementById("uploadOrdersLabel");
  const templateBtn = document.getElementById("downloadOrdersTemplate");
  if (!openBtn || !modal) return;

  function setMsg(text, ok) {
    if (!msgEl) return;
    if (!text) {
      msgEl.hidden = true;
      msgEl.textContent = "";
      msgEl.className = "modal-msg";
      return;
    }
    msgEl.hidden = false;
    msgEl.textContent = text;
    msgEl.className = "modal-msg " + (ok ? "ok" : "err");
  }

  function openModal() {
    modal.hidden = false;
    setMsg("");
    refreshOrderStats();
  }

  function closeModal() {
    modal.hidden = true;
  }

  async function refreshOrderStats() {
    if (!statsEl) return;
    try {
      const headers = authHeaders();
      const resp = await fetch(`${API_BASE}/api/v1/orders/stats`, { headers });
      if (!resp.ok) throw new Error(t("ordersLoadFailed") || "无法读取订单");
      const data = await resp.json();
      const n = data.stats?.order_count ?? 0;
      const emails = data.stats?.email_count ?? 0;
      statsEl.textContent = `${t("ordersStats") || "当前订单"} ${n} · ${t("ordersEmails") || "邮箱"} ${emails}`;
    } catch (err) {
      statsEl.textContent = `${t("ordersLoadFailed") || "订单统计加载失败"}：${err.message || err}`;
    }
  }

  openBtn.addEventListener("click", openModal);
  modal.querySelectorAll("[data-close-import]").forEach((el) => {
    el.addEventListener("click", closeModal);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) closeModal();
  });

  if (templateBtn) {
    templateBtn.addEventListener("click", () => {
      const blob = new Blob([ORDERS_CSV_TEMPLATE], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "orders_template.csv";
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  if (fileInput) {
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      if (uploadLabel) uploadLabel.textContent = "上传中…";
      setMsg("");
      try {
        const form = new FormData();
        form.append("file", file);
        const headers = {};
        if (CONFIG.apiKey) headers["X-API-Key"] = CONFIG.apiKey;
        if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
        const merge = mergeCheck ? mergeCheck.checked : true;
        const resp = await fetch(
          `${API_BASE}/api/v1/orders/upload?merge=${merge ? "true" : "false"}`,
          { method: "POST", headers, body: form }
        );
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          throw new Error(data.detail || resp.statusText || "上传失败");
        }
        const imported = data.imported ?? 0;
        const total = data.stats?.order_count ?? "";
        const overwritten = data.overwritten ?? 0;
        const skipped = data.skipped_count ?? 0;
        setMsg(
          data.message ||
            `导入成功：本次 ${imported} 条，当前共 ${total} 条（已热更新）`,
          true
        );
        const previewEl = document.getElementById("importOrdersPreview");
        if (previewEl) {
          const rows = data.preview || [];
          const skipRows = data.skipped || [];
          if (rows.length || skipRows.length) {
            previewEl.hidden = false;
            previewEl.innerHTML = `
              <p class="import-preview-title">导入预览 · 成功 ${imported} · 覆盖 ${overwritten} · 跳过 ${skipped}</p>
              <table class="import-preview-table">
                <thead><tr><th>订单号</th><th>邮箱</th><th>状态</th><th>金额</th><th>物流</th></tr></thead>
                <tbody>
                  ${rows
                    .map(
                      (o) => `<tr>
                      <td>${escapeHtml(o.order_id || "")}</td>
                      <td>${escapeHtml(o.email || "")}</td>
                      <td>${escapeHtml(o.status || "")}</td>
                      <td>${escapeHtml(String(o.total ?? ""))}</td>
                      <td>${escapeHtml(o.tracking_number || "")}</td>
                    </tr>`
                    )
                    .join("")}
                </tbody>
              </table>
              ${
                skipRows.length
                  ? `<p class="import-preview-skip">跳过：${skipRows
                      .slice(0, 5)
                      .map((s) => `第${s.row}行(${s.reason})`)
                      .join("；")}</p>`
                  : ""
              }
            `;
          } else {
            previewEl.hidden = true;
            previewEl.innerHTML = "";
          }
        }
        await refreshOrderStats();
        if (currentUser) {
          try {
            await loadOrders();
          } catch {
            /* ignore */
          }
        }
      } catch (err) {
        setMsg(err.message || "上传失败", false);
      } finally {
        if (uploadLabel) uploadLabel.textContent = "选择 CSV 上传";
        fileInput.value = "";
      }
    });
  }
}

initLanguageSwitcher();
initApp();
