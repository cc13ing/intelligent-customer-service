const API_KEY_STORAGE = "kefu_admin_api_key";
const ADMIN_TOKEN_STORAGE = "kefu_admin_token";

export function getApiKey() {
  return localStorage.getItem(API_KEY_STORAGE) || "";
}

export function setApiKey(key) {
  localStorage.setItem(API_KEY_STORAGE, key);
}

export function getAdminToken() {
  return localStorage.getItem(ADMIN_TOKEN_STORAGE) || "";
}

export function setAdminToken(token) {
  if (token) localStorage.setItem(ADMIN_TOKEN_STORAGE, token);
  else localStorage.removeItem(ADMIN_TOKEN_STORAGE);
}

export async function apiFetch(path, options = {}) {
  const headers = {
    ...(options.headers || {}),
  };
  const apiKey = getApiKey();
  if (apiKey) headers["X-API-Key"] = apiKey;
  const adminToken = getAdminToken();
  if (adminToken) headers["Authorization"] = `Bearer ${adminToken}`;
  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, { ...options, headers, credentials: "include" });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const detail = err.detail;
    throw new Error(typeof detail === "string" ? detail : resp.statusText);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

export async function adminLogin(username, password) {
  const resp = await fetch("/api/v1/admin/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ username, password }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  setAdminToken(data.access_token);
  return data;
}

export async function adminLogout() {
  try {
    await apiFetch("/api/v1/admin/auth/logout", { method: "POST" });
  } catch {
    /* ignore */
  }
  setAdminToken("");
}

export async function adminMe() {
  const headers = {};
  const token = getAdminToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch("/api/v1/admin/auth/me", {
    credentials: "include",
    headers,
  });
  return resp.json();
}

export async function adminChangePassword(oldPassword, newPassword) {
  return apiFetch("/api/v1/admin/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
}

export async function fetchHealth() {
  const resp = await fetch("/health");
  return resp.json();
}

export async function fetchHealthDeep() {
  const resp = await fetch("/health/deep");
  return resp.json();
}

export async function fetchMetricsText() {
  const headers = {};
  if (getApiKey()) headers["X-API-Key"] = getApiKey();
  if (getAdminToken()) headers["Authorization"] = `Bearer ${getAdminToken()}`;
  const resp = await fetch("/metrics", { headers });
  if (!resp.ok) throw new Error("Failed to load metrics");
  return resp.text();
}

export function parsePrometheusMetrics(text) {
  const counters = {};
  const lines = text.split("\n");
  for (const line of lines) {
    if (!line || line.startsWith("#")) continue;
    const m = line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9.eE+-]+)/);
    if (!m) continue;
    const name = m[1];
    const value = parseFloat(m[3]);
    if (!counters[name]) counters[name] = 0;
    counters[name] += value;
  }
  return counters;
}

export async function listKnowledge() {
  return apiFetch("/api/v1/admin/knowledge");
}

export async function deleteKnowledge(filename) {
  return apiFetch(`/api/v1/admin/knowledge/${encodeURIComponent(filename)}`, {
    method: "DELETE",
  });
}

export async function rebuildKnowledge() {
  return apiFetch("/api/v1/knowledge/rebuild", { method: "POST" });
}

export async function uploadKnowledge(file) {
  const form = new FormData();
  form.append("file", file);
  const headers = {};
  if (getApiKey()) headers["X-API-Key"] = getApiKey();
  if (getAdminToken()) headers["Authorization"] = `Bearer ${getAdminToken()}`;
  const resp = await fetch("/api/v1/knowledge/upload", {
    method: "POST",
    headers,
    body: form,
    credentials: "include",
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || resp.statusText);
  }
  return resp.json();
}

export async function listComplaints({ kind = "all", status, limit = 50, offset = 0 } = {}) {
  const qs = new URLSearchParams({ kind, limit: String(limit), offset: String(offset) });
  if (status) qs.set("status", status);
  return apiFetch(`/api/v1/admin/complaints?${qs}`);
}

export async function updateComplaintStatus(id, status) {
  return apiFetch(`/api/v1/admin/complaints/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
}

export async function runInference(messages, maxNewTokens) {
  return apiFetch("/inference", {
    method: "POST",
    body: JSON.stringify({ messages, max_new_tokens: maxNewTokens }),
  });
}

export async function listOrders() {
  return apiFetch("/api/v1/admin/orders");
}

export async function patchOrder(orderId, body) {
  return apiFetch(`/api/v1/admin/orders/${encodeURIComponent(orderId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function deleteOrder(orderId) {
  return apiFetch(`/api/v1/admin/orders/${encodeURIComponent(orderId)}`, {
    method: "DELETE",
  });
}

export async function reloadOrders() {
  return apiFetch("/api/v1/admin/orders/reload", { method: "POST" });
}

export async function deleteOrdersFile(filename) {
  return apiFetch(`/api/v1/admin/orders/files/${encodeURIComponent(filename)}`, {
    method: "DELETE",
  });
}

export async function uploadOrdersCsv(file, merge = true) {
  const form = new FormData();
  form.append("file", file);
  const headers = {};
  if (getApiKey()) headers["X-API-Key"] = getApiKey();
  if (getAdminToken()) headers["Authorization"] = `Bearer ${getAdminToken()}`;
  const qs = merge ? "?merge=true" : "?merge=false";
  const resp = await fetch(`/api/v1/admin/orders/upload${qs}`, {
    method: "POST",
    headers,
    body: form,
    credentials: "include",
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || resp.statusText);
  }
  return resp.json();
}

export async function listSessions({ status, user_id, limit = 50, offset = 0 } = {}) {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) qs.set("status", status);
  if (user_id) qs.set("user_id", user_id);
  return apiFetch(`/api/v1/admin/sessions?${qs}`);
}

export async function archiveSession(id) {
  return apiFetch(`/api/v1/admin/sessions/${id}/archive`, { method: "POST" });
}

export async function deleteSession(id) {
  return apiFetch(`/api/v1/admin/sessions/${id}`, { method: "DELETE" });
}

export async function clearSessions({ include_active = true, only_archived = false } = {}) {
  return apiFetch("/api/v1/admin/sessions/clear", {
    method: "POST",
    body: JSON.stringify({ include_active, only_archived }),
  });
}

export async function listUsers({ limit = 50, offset = 0 } = {}) {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return apiFetch(`/api/v1/admin/users?${qs}`);
}

export async function deactivateUser(userId) {
  return apiFetch(`/api/v1/admin/users/${userId}`, { method: "DELETE" });
}

export async function listUserOrders(userId) {
  return apiFetch(`/api/v1/admin/users/${userId}/orders`);
}

export async function listUserSessions(userId) {
  return apiFetch(`/api/v1/admin/users/${userId}/sessions`);
}
