import { useState } from "react";
import { getApiKey, setApiKey } from "../api";

export default function SettingsBar() {
  const [key, setKey] = useState(getApiKey());
  const [saved, setSaved] = useState(false);

  function save() {
    setApiKey(key.trim());
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  return (
    <div className="settings-bar">
      <label>
        API Key
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="填写 .env 中的 API_KEY"
        />
      </label>
      <button type="button" onClick={save}>
        保存
      </button>
      {saved && <span className="hint">已保存</span>}
      <p className="hint" style={{ marginTop: 6, fontSize: 12 }}>
        管理接口需要密钥；聊天前台默认免 Key（CHAT_PUBLIC_ACCESS）
      </p>
    </div>
  );
}
