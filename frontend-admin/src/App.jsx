import { useEffect, useState } from "react";
import { NavLink, Route, Routes, Navigate } from "react-router-dom";
import KnowledgePage from "./pages/Knowledge";
import ComplaintsPage from "./pages/Complaints";
import DashboardPage from "./pages/Dashboard";
import MetricsPage from "./pages/Metrics";
import InferencePage from "./pages/Inference";
import OrdersPage from "./pages/Orders";
import AgentDeskPage from "./pages/AgentDesk";
import SessionsPage from "./pages/Sessions";
import UsersPage from "./pages/Users";
import LoginPage from "./pages/Login";
import SettingsBar from "./components/SettingsBar";
import { adminChangePassword, adminLogout, adminMe } from "./api";

export default function App() {
  const [authed, setAuthed] = useState(false);
  const [checking, setChecking] = useState(true);
  const [pwdMsg, setPwdMsg] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const me = await adminMe();
        if (!alive) return;
        if (me?.authenticated) {
          setAuthed(true);
        } else {
          // cookie/token 失效则清掉，强制走登录页
          await adminLogout();
          setAuthed(false);
        }
      } catch {
        if (!alive) return;
        await adminLogout();
        setAuthed(false);
      } finally {
        if (alive) setChecking(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  async function onLogout() {
    await adminLogout();
    setAuthed(false);
  }

  async function onChangeAdminPwd() {
    const oldPassword = window.prompt("原管理员密码");
    if (oldPassword == null) return;
    const newPassword = window.prompt("新密码（至少6位）");
    if (newPassword == null) return;
    try {
      const res = await adminChangePassword(oldPassword, newPassword);
      setPwdMsg(res.message || "密码已更新");
    } catch (e) {
      setPwdMsg(e.message);
    }
  }

  if (checking) {
    return <div className="login-page"><p>检查登录状态…</p></div>;
  }

  if (!authed) {
    return <LoginPage onLoggedIn={() => setAuthed(true)} />;
  }

  return (
    <div className="admin-layout">
      <aside className="admin-sidebar">
        <h1>饺子管理台</h1>
        <nav>
          <NavLink to="/" end>仪表盘</NavLink>
          <NavLink to="/users">注册账户</NavLink>
          <NavLink to="/agent-desk">人工客服台</NavLink>
          <NavLink to="/sessions">会话归档</NavLink>
          <NavLink to="/knowledge">知识库</NavLink>
          <NavLink to="/orders">订单导入</NavLink>
          <NavLink to="/complaints">投诉 / 转人工</NavLink>
          <NavLink to="/metrics">指标</NavLink>
          <NavLink to="/inference">推理测试</NavLink>
          <a href="/" target="_blank" rel="noreferrer">聊天前台 ↗</a>
        </nav>
        <div className="admin-account">
          <button type="button" onClick={onChangeAdminPwd}>修改管理员密码</button>
          <button type="button" onClick={onLogout}>退出登录</button>
          {pwdMsg ? <p className="muted tiny">{pwdMsg}</p> : null}
        </div>
        <SettingsBar />
      </aside>
      <main className="admin-main">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="/agent-desk" element={<AgentDeskPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/orders" element={<OrdersPage />} />
          <Route path="/complaints" element={<ComplaintsPage />} />
          <Route path="/metrics" element={<MetricsPage />} />
          <Route path="/inference" element={<InferencePage />} />
          <Route path="/login" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
