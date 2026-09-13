import { useCallback, useEffect, useState } from "react";
import {
  deleteOrder,
  deleteOrdersFile,
  listOrders,
  patchOrder,
  reloadOrders,
  uploadOrdersCsv,
} from "../api";

const CSV_TEMPLATE = `order_id,email,status,destination_country,currency,total,tracking_number,carrier,created_at,items
ORD-3001,demo@gulf.ae,Shipped,AE,AED,199.00,TRK001,Aramex,2024-08-01 12:00:00,手机壳 x2
`;

export default function OrdersPage() {
  const [orders, setOrders] = useState([]);
  const [files, setFiles] = useState([]);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [merge, setMerge] = useState(true);
  const [message, setMessage] = useState("");
  const [importPreview, setImportPreview] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await listOrders();
      setOrders(data.orders || []);
      setFiles(data.files || []);
      setStats(data.stats || null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function onUpload(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError("");
    setMessage("");
    setImportPreview(null);
    try {
      const res = await uploadOrdersCsv(file, merge);
      const imported = res.imported ?? 0;
      const overwritten = res.overwritten ?? 0;
      const skipped = res.skipped_count ?? (Array.isArray(res.skipped) ? res.skipped.length : 0);
      setMessage(
        res.message ||
          `导入完成：成功 ${imported} · 覆盖 ${overwritten} · 跳过 ${skipped}`
      );
      setImportPreview({
        imported,
        overwritten,
        skipped,
        preview: res.preview || [],
        skipped_rows: Array.isArray(res.skipped) ? res.skipped : [],
      });
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
      e.target.value = "";
    }
  }

  async function onReload() {
    setReloading(true);
    setError("");
    setMessage("");
    try {
      const res = await reloadOrders();
      setMessage(res.message || "已重载");
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setReloading(false);
    }
  }

  async function onDelete(filename) {
    if (!window.confirm(`删除 ${filename} 并全量重载订单？`)) return;
    setError("");
    try {
      await deleteOrdersFile(filename);
      await load();
    } catch (err) {
      setError(err.message);
    }
  }

  function downloadTemplate() {
    const blob = new Blob([CSV_TEMPLATE], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "orders_template.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div>
      <header className="page-header">
        <h2>订单管理</h2>
        <div className="header-actions">
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginRight: 8 }}>
            <input
              type="checkbox"
              checked={merge}
              onChange={(e) => setMerge(e.target.checked)}
            />
            合并导入（同订单号覆盖）
          </label>
          <button type="button" onClick={downloadTemplate}>
            下载模板
          </button>
          <button type="button" onClick={onReload} disabled={reloading}>
            {reloading ? "重载中…" : "热重载"}
          </button>
          <label className="upload-btn">
            {uploading ? "导入中…" : "CSV 批量导入"}
            <input type="file" accept=".csv,text/csv" onChange={onUpload} hidden />
          </label>
        </div>
      </header>

      <p className="muted" style={{ marginBottom: 12 }}>
        上传 CSV 后立即写入内存并局部热更新，客服查询 / Agent 工具马上可用，无需重启服务。
        必填列：<code>order_id</code>；建议列：email, status, total, tracking_number, carrier, items…
      </p>

      {error && <p className="error">{error}</p>}
      {message && <p className="ok">{message}</p>}

      {importPreview && (
        <section style={{ marginBottom: 16 }}>
          <h3>
            本次导入明细 · 成功 {importPreview.imported} · 覆盖{" "}
            {importPreview.overwritten} · 跳过 {importPreview.skipped}
          </h3>
          {importPreview.preview?.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>订单号</th>
                  <th>邮箱</th>
                  <th>状态</th>
                  <th>结果</th>
                </tr>
              </thead>
              <tbody>
                {importPreview.preview.map((row, i) => (
                  <tr key={`${row.order_id || "row"}-${i}`}>
                    <td>{row.order_id || "-"}</td>
                    <td>{row.email || "-"}</td>
                    <td>{row.status || "-"}</td>
                    <td>{row.action || row.result || "ok"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {importPreview.skipped_rows?.length > 0 && (
            <p className="muted" style={{ marginTop: 8 }}>
              跳过行：
              {importPreview.skipped_rows
                .slice(0, 8)
                .map((r) =>
                  typeof r === "string"
                    ? r
                    : `行${r.row || "?"} ${r.reason || r.error || ""}`
                )
                .join("；")}
            </p>
          )}
        </section>
      )}

      {stats && (
        <p>
          当前订单 <strong>{stats.order_count}</strong> 条 · 用户邮箱{" "}
          <strong>{stats.email_count}</strong> · 最近加载{" "}
          {stats.loaded_at || "-"}
        </p>
      )}

      {loading ? (
        <p>加载中…</p>
      ) : (
        <>
          <section>
            <h3>CSV 文件 ({files.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>文件名</th>
                  <th>大小</th>
                  <th>修改时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.filename}>
                    <td>{f.filename}</td>
                    <td>{(f.size_bytes / 1024).toFixed(1)} KB</td>
                    <td>{f.modified_at}</td>
                    <td>
                      <button
                        type="button"
                        className="danger"
                        onClick={() => onDelete(f.filename)}
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
                {files.length === 0 && (
                  <tr>
                    <td colSpan={4}>暂无文件，请上传 CSV</td>
                  </tr>
                )}
              </tbody>
            </table>
          </section>

          <section>
            <h3>订单预览 ({orders.length})</h3>
            <table>
              <thead>
                <tr>
                  <th>订单号</th>
                  <th>邮箱</th>
                  <th>状态</th>
                  <th>金额</th>
                  <th>物流单号</th>
                  <th>承运商</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.order_id}>
                    <td>{o.order_id}</td>
                    <td>{o.email || "-"}</td>
                    <td>{o.status}</td>
                    <td>
                      {o.currency} {o.total}
                    </td>
                    <td>{o.tracking_number || "-"}</td>
                    <td>{o.carrier || "-"}</td>
                    <td>
                      <button
                        type="button"
                        onClick={async () => {
                          const orderId = window.prompt("订单号", o.order_id);
                          if (orderId == null) return;
                          const tracking = window.prompt(
                            "运单号",
                            o.tracking_number || ""
                          );
                          if (tracking == null) return;
                          try {
                            await patchOrder(o.order_id, {
                              order_id: orderId.trim() || o.order_id,
                              tracking_number: tracking.trim(),
                            });
                            setMessage(`已更新 ${o.order_id}`);
                            await load();
                          } catch (err) {
                            setError(err.message);
                          }
                        }}
                      >
                        编辑
                      </button>
                      <button
                        type="button"
                        className="danger"
                        onClick={async () => {
                          if (!window.confirm(`删除订单 ${o.order_id}？`)) return;
                          try {
                            await deleteOrder(o.order_id);
                            setMessage(`已删除 ${o.order_id}`);
                            await load();
                          } catch (err) {
                            setError(err.message);
                          }
                        }}
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
                {orders.length === 0 && (
                  <tr>
                    <td colSpan={7}>暂无订单数据</td>
                  </tr>
                )}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  );
}
