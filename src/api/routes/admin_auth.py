from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from src.core.auth import create_access_token, try_decode_token
from src.core.config import get_settings
from src.core.rate_limit import limiter
from src.core.security import check_api_key
from src.services.admin_auth_store import (
    admin_username,
    change_admin_password,
    verify_admin_password,
)

router = APIRouter(prefix="/api/v1/admin/auth", tags=["admin-auth"])


class AdminLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class AdminChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


def _extract_admin_token(request: Request) -> str | None:
    settings = get_settings()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(settings.admin_jwt_cookie_name)


def is_admin_authenticated(request: Request) -> bool:
    token = _extract_admin_token(request)
    payload = try_decode_token(token)
    return bool(payload and payload.get("role") == "admin")


@router.post("/login")
@limiter.limit("20/minute")
async def admin_login(request: Request, body: AdminLoginRequest, response: Response):
    settings = get_settings()
    if body.username != admin_username() or not verify_admin_password(body.password):
        raise HTTPException(status_code=401, detail="管理员账号或密码错误")
    token = create_access_token(
        "admin",
        f"{admin_username()}@admin.local",
        settings=settings,
        extra={"role": "admin"},
    )
    response.set_cookie(
        key=settings.admin_jwt_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "admin": {"username": admin_username()},
    }


@router.post("/logout")
async def admin_logout(response: Response):
    settings = get_settings()
    response.delete_cookie(settings.admin_jwt_cookie_name)
    return {"ok": True}


@router.get("/me")
async def admin_me(request: Request):
    if not is_admin_authenticated(request):
        return {"authenticated": False}
    return {"authenticated": True, "admin": {"username": admin_username()}}


@router.post("/change-password")
@limiter.limit("10/minute")
async def admin_change_password(request: Request, body: AdminChangePasswordRequest):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="需要管理员登录")
    try:
        change_admin_password(body.old_password, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "管理员密码已更新"}


async def verify_admin_access(request: Request) -> str:
    """管理员 JWT 或 API Key 均可访问管理接口。"""
    settings = get_settings()
    api_key = request.headers.get("X-API-Key")
    if check_api_key(api_key, settings):
        return "api_key"
    if is_admin_authenticated(request):
        return "admin_jwt"
    # 开发环境且未配置 api_key 时保持兼容
    if not settings.api_key and settings.env != "production":
        return "dev"
    raise HTTPException(status_code=401, detail="需要管理员登录或有效 API Key")
