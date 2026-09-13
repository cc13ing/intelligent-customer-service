import { useCallback, useEffect, useState } from "react";
import {
  deactivateUser,
  deleteOrder,
  deleteSession,
  listUserOrders,
  listUserSessions,
  listUsers,
  patchOrder,
} from "../api";

export default function UsersPage() {
  const [users, setUsers] = useState([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [orders, setOrders] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await listUsers();
      setUsers(data.items || []);
      setTotal(data.total || 0);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function openUser(user) {
    setSelected(user);
    setMessage("");
    setError("");
    try {
      const [o, s] = await Promise.all([
        listUserOrders(user.id),
        listUserSessions(user.id),
      ]);
      setOrders(o.orders || []);
      setSessions(s.items || []);
    } catch (e) {
      setError(e.message);
    }
  }

  async function onDeactivate(user) {
    if (!window.confirm(`确认注销账户 ${user.email}？`)) return;
    try {
      await deactivateUser(user.id);
      setMessage(`已注销 ${user.email}`);
      if (selected?.id === user.id) setSelected(null);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function onSaveOrder(order) {
    const orderId = order.order_id;
    const nextOrderId = window.prompt("订单号", order.order_id);
    if (nextOrderId == null) return;
    const tracking = window.prompt("运单号", order.tracking_number || "");
    if (tracking == null) return;
    try {
      await patchOrder(orderId, {
        order_id: nextOrderId.trim() || orderId,
        tracking_number: tracking.trim(),
      });
      setMessage(`已更新订单 ${orderId}`);
      if (selected) await openUser(selected);
    } catch (e) {
      setError(e.message);
    }
  }

  async function onDeleteOrder(orderId) {
    if (!window.confirm(`删除订单 ${orderId}？`)) return;
    try {
      await deleteOrder(orderId);
      setMessage(`已删除 ${orderId}`);
      if (selected) await openUser(selected);
    } catch (e) {
      setError(e.message);
    }
  }

  async function onDeleteSession(id) {
    if (!window.confirm(`删除会话 ${id}？`)) return;
    try {
      await deleteSession(id);
      setMessage(`已删除会话`);
      if (selected) await openUser(selected);
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="page">
      <h2>注册账户</h2>
      <p className="muted">共 {total} 个账户。可查看订单/会话，注销账号或改订单。</p>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="ok">{message}</p> : null}
      {loading ? <p>加载中…</p> : null}
      <div className="users-grid">
        <table className="data-table">
          <thead>
            <tr>
              <th>邮箱</th>
              <th>姓名</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className={selected?.id === u.id ? "active" : ""}>
                <td>
                  <button type="button" className="linkish" onClick={() => openUser(u)}>
                    {u.email}
                  </button>
                </td>
                <td>{u.name || "—"}</td>
                <td>{u.is_active ? "正常" : "已注销"}</td>
                <td>
                  {u.is_active ? (
                    <button type="button" onClick={() => onDeactivate(u)}>
                      注销
                    </button>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {selected ? (
          <div className="user-detail">
            <h3>{selected.email}</h3>
            <h4>订单</h4>
            {!orders.length ? <p className="muted">无订单</p> : null}
            <ul>
              {orders.map((o) => (
                <li key={o.order_id}>
                  {o.order_id} · {o.status} · {o.tracking_number || "无运单"}
                  <button type="button" onClick={() => onSaveOrder(o)}>
                    编辑
                  </button>
                  <button type="button" onClick={() => onDeleteOrder(o.order_id)}>
                    删除
                  </button>
                </li>
              ))}
            </ul>
            <h4>会话</h4>
            {!sessions.length ? <p className="muted">无会话</p> : null}
            <ul>
              {sessions.map((s) => (
                <li key={s.id}>
                  {s.id.slice(0, 8)}… · {s.status}
                  <button type="button" onClick={() => onDeleteSession(s.id)}>
                    删除
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </div>
  );
}
