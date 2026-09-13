import { useCallback, useEffect, useState } from "react";
import { listComplaints, updateComplaintStatus } from "../api";

const STATUSES = ["Received", "InReview", "Resolved"];
const KINDS = [
  { id: "all", label: "全部" },
  { id: "handoff", label: "转人工队列" },
  { id: "complaint", label: "普通投诉" },
];

export default function ComplaintsPage() {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [kind, setKind] = useState("all");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 50;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await listComplaints({ kind, status: status || undefined, limit, offset });
      setItems(data.items || []);
      setTotal(data.total ?? (data.items || []).length);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [kind, status, offset]);

  useEffect(() => {
    load();
  }, [load]);

  async function onStatusChange(id, next) {
    setError("");
    try {
      await updateComplaintStatus(id, next);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div>
      <header className="page-header">
        <h2>投诉 / 转人工</h2>
        <button type="button" onClick={load}>
          刷新
        </button>
      </header>

      <div className="filter-row" style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        {KINDS.map((k) => (
          <button
            key={k.id}
            type="button"
            className={kind === k.id ? "btn-primary" : ""}
            onClick={() => {
              setOffset(0);
              setKind(k.id);
            }}
          >
            {k.label}
          </button>
        ))}
        <select
          value={status}
          onChange={(e) => {
            setOffset(0);
            setStatus(e.target.value);
          }}
        >
          <option value="">全部状态</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      <p className="muted">
        共 {total} 条 · Received=待接手 · InReview=人工处理中 · Resolved=已结案
      </p>

      {error && <p className="error">{error}</p>}
      {loading ? (
        <p>加载中…</p>
      ) : (
        <>
          <table>
            <thead>
              <tr>
                <th>类型</th>
                <th>工单号</th>
                <th>状态</th>
                <th>详情</th>
                <th>会话</th>
                <th>时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id} className={c.is_handoff ? "row-handoff" : ""}>
                  <td>{c.is_handoff ? "转人工" : "投诉"}</td>
                  <td>{c.ticket_id}</td>
                  <td>
                    <span className={`badge badge-${c.status}`}>{c.status}</span>
                  </td>
                  <td className="details-cell">{c.details}</td>
                  <td>{c.session_id?.slice(0, 8) || "—"}</td>
                  <td>{c.created_at}</td>
                  <td>
                    <select
                      value={c.status}
                      onChange={(e) => onStatusChange(c.id, e.target.value)}
                    >
                      {STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr>
                  <td colSpan={7}>暂无工单</td>
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
