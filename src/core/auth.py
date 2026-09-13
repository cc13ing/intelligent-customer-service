"""登录鉴权：签发 / 解析 JWT「临时工牌」。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt

from src.core.config import Settings, get_settings


def create_access_token(
    user_id: str,
    email: str,
    *,
    settings: Settings | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """登录成功后签发 JWT：写入用户 id、邮箱和过期时间。"""
    settings = settings or get_settings()
    expire = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": user_id,
        "email": email,
        "exp": expire,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    """解析 JWT；无效或过期则抛出 401。"""
    settings = settings or get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc


def try_decode_token(token: str | None, settings: Settings | None = None) -> dict[str, Any] | None:
    """尝试解析 JWT；失败返回 None（不抛错，适合可选登录场景）。"""
    if not token:
        return None
    settings = settings or get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


def extract_bearer_token(request: Request) -> str | None:
    """从请求里取出 token：优先 Authorization 头，其次 Cookie，再其次 query。"""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    settings = get_settings()
    cookie = request.cookies.get(settings.jwt_cookie_name)
    if cookie:
        return cookie
    return request.query_params.get("token")
