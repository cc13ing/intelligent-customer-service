import { useCallback, useEffect, useState } from "react";
import {
  archiveSession,
  clearSessions,
  deleteSession,
  listSessions,
} from "../api";

export default function SessionsPage() {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 50;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await listSessions({
        status: status || undefined,
        limit,
        offset,
      });
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [status, offset]);

  useEffect(() => {
    load();
  }, [load]);

  async function onArchive(id) {
    setError("");
    try {
      await archiveSession(id);
      setMessage(`已归档 ${id.slice(0, 8)}`);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function onDelete(id) {
    if (!window.confirm(`永久删除会话 ${id.slice(0, 8)}…？`)) return;
    setError("");
    try {
      await deleteSession(id);
      setMessage(`已删除 ${id.slice(0, 8)}`);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function onClear(onlyArchived) {
    const tip = onlyArchived
      ? "清空全部已归档会话？"
      : "危险：将删除几乎所有对话（含进行中）。确定？";
    if (!window.confirm(tip)) return;
    if (!onlyArchived && !window.confirm("再次确认：清空全部会话不可恢复。")) return;
    setError("");
    try {
      const res = await clearSessions({
        include_active: !onlyArchived,
        only_archived: onlyArchived,
      });
      setMessage(res.message || `已删除 ${res.deleted_sessions} 个`);
      setOffset(0);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div>
      <header className="page-header">
        <h2>会话归档 / 清数据</h2>
        <div className="header-actions" style={{ display: "flex", gap: 8 }}>
          <button type="button" onClick={load}>
            刷新
          </button>
          <button type="button" onClick={() => onClear(true)}>
            清空已归档
          </button>
          <button type="button" className="danger" onClick={() => onClear(false)}>
            清空全部对话
          </button>
        </div>
      </header>

      <p className="muted">
        归档会保留会话记录但标记为 archived；清空会永久删除消息与会话。
      </p>

      <div style={{ marginBottom: 12 }}>
        <select
          value={status}
          onChange={(e) => {
            setOffset(0);
            setStatus(e.target.value);
          }}
        >
          <option value="">全部状态</option>
          <option value="active">active</option>
          <option value="waiting_human">waiting_human</option>
          <option value="human">human</option>
          <option value="archived">archived</option>
        </select>
      </div>

      {error && <p className="error">{error}</p>}
      {message && <p className="ok">{message}</p>}
      <p>
        共 <strong>{total}</strong> 条
      </p>

      {loading ? (
        <p>加载中…</p>
      ) : (
        <>
          <table>
            <thead>
              <tr>
                <th>会话 ID</th>
                <th>状态</th>
                <th>消息数</th>
                <th>用户</th>
                <th>更新时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id}>
                  <td>
                    <code>{s.id.slice(0, 8)}</code>
                  </td>
                  <td>
                    <span className="badge badge-Received">{s.status}</span>
                  </td>
                  <td>{s.message_count}</td>
                  <td>{s.user_id ? s.user_id.slice(0, 8) : "—"}</td>
                  <td>{s.updated_at || s.created_at || "—"}</td>
                  <td style={{ display: "flex", gap: 6 }}>
                    {s.status !== "archived" && (
                      <button type="button" onClick={() => onArchive(s.id)}>
                        归档
                      </button>
                    )}
                    <button type="button" className="danger" onClick={() => onDelete(s.id)}>
                      删除
                    </button>
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr>
                  <td colSpan={6}>暂无会话</td>
                </tr>
              )}
            </tbody>
          </table>
          <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
            <button
              type="button"
              disabled={offset <= 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              上一页
            </button>
            <button
              type="button"
              disabled={offset + limit >= total}
              onClick={() => setOffset(offset + limit)}
            >
              下一页
            </button>
          </div>
        </>
      )}
    </div>
  );
}
