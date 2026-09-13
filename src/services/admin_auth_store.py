"""管理员密码哈希存储（内存 + 可选落盘）。"""

from __future__ import annotations

from pathlib import Path

from src.core.config import PROJECT_ROOT, get_settings
from src.services.user_service import hash_password, verify_password

_HASH_FILE = PROJECT_ROOT / "data" / "admin_password.hash"
_runtime_hash: str | None = None


def _load_hash() -> str:
    global _runtime_hash
    if _runtime_hash:
        return _runtime_hash
    if _HASH_FILE.exists():
        text = _HASH_FILE.read_text(encoding="utf-8").strip()
        if text:
            _runtime_hash = text
            return _runtime_hash
    settings = get_settings()
    _runtime_hash = hash_password(settings.admin_password)
    return _runtime_hash


def verify_admin_password(password: str) -> bool:
    return verify_password(password, _load_hash())


def change_admin_password(old_password: str, new_password: str) -> None:
    if not verify_admin_password(old_password):
        raise ValueError("原密码不正确")
    if len(new_password) < 6:
        raise ValueError("新密码至少 6 位")
    global _runtime_hash
    _runtime_hash = hash_password(new_password)
    _HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HASH_FILE.write_text(_runtime_hash, encoding="utf-8")


def admin_username() -> str:
    return get_settings().admin_username
