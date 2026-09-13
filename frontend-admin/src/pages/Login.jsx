import { useState } from "react";
import { adminLogin } from "../api";

export default function LoginPage({ onLoggedIn }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      await adminLogin(username.trim(), password);
      onLoggedIn?.();
    } catch (err) {
      setError(err.message || "登录失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>饺子管理台</h1>
        <p className="muted">请使用管理员账号登录</p>
        <label>
          账号
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label>
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error ? <p className="error">{error}</p> : null}
        <button type="submit" disabled={loading}>
          {loading ? "登录中…" : "登录"}
        </button>
        <p className="muted tiny">默认账号 admin / admin123（可在后台改密）</p>
      </form>
    </div>
  );
}
