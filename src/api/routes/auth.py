from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_optional_user
from src.core.auth import create_access_token
from src.core.config import get_settings
from src.core.rate_limit import limiter
from src.db import get_db
from src.models.user import User
from src.services.user_service import (
    authenticate,
    change_password,
    get_or_create_user,
    register_user,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)
    name: str | None = Field(default=None, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=128)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


class OAuthLoginRequest(BaseModel):
    provider: str = Field(..., pattern="^(google|github|dev)$")
    email: EmailStr
    name: str | None = None
    subject: str = Field(..., min_length=1, max_length=128)
    id_token: str | None = None


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


def _user_dict(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "oauth_provider": user.oauth_provider,
        "is_active": bool(user.is_active),
        "has_password": bool(user.password_hash),
    }


def _set_auth_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.jwt_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
    )


@router.post("/register", response_model=AuthResponse)
@limiter.limit("10/minute")
async def register(
    request: Request,
    body: RegisterRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        user = await register_user(db, body.email, body.password, name=body.name)
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settings = get_settings()
    token = create_access_token(user.id, user.email, settings=settings)
    _set_auth_cookie(response, token)
    return AuthResponse(access_token=token, user=_user_dict(user))


@router.post("/login", response_model=AuthResponse)
@limiter.limit("20/minute")
async def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """邮箱 + 密码登录。"""
    # #region debug-point B:auth-login
    try:
        import json
        import urllib.request

        _p = ".dbg/login-chat-timeout.env"
        _u = "http://127.0.0.1:7778/event"
        _s = "login-chat-timeout"
        try:
            with open(_p, encoding="utf-8") as _f:
                _c = _f.read().splitlines()
                _u = next((line.split("=", 1)[1] for line in _c if line.startswith("DEBUG_SERVER_URL=")), _u)
                _s = next((line.split("=", 1)[1] for line in _c if line.startswith("DEBUG_SESSION_ID=")), _s)
        except Exception:
            pass

        urllib.request.urlopen(
            urllib.request.Request(
                _u,
                data=json.dumps(
                    {
                        "sessionId": _s,
                        "runId": "pre",
                        "hypothesisId": "B",
                        "location": "src/api/routes/auth.py:login",
                        "msg": "[DEBUG] auth_login_attempt",
                        "data": {
                            "email": str(body.email),
                            "password_len": len(body.password or ""),
                            "content_type": request.headers.get("content-type"),
                            "origin": request.headers.get("origin"),
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ),
            timeout=2,
        ).read()
    except Exception:
        pass
    # #endregion
    user = await authenticate(db, body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="邮箱或密码错误，或账号已注销")
    if body.name and not user.name:
        user.name = body.name
        await db.flush()
    await db.commit()

    settings = get_settings()
    token = create_access_token(user.id, user.email, settings=settings)
    _set_auth_cookie(response, token)
    return AuthResponse(access_token=token, user=_user_dict(user))


@router.post("/change-password")
@limiter.limit("10/minute")
async def change_password_endpoint(
    request: Request,
    body: ChangePasswordRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    try:
        await change_password(db, user, body.old_password, body.new_password)
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "密码已更新"}


@router.post("/oauth", response_model=AuthResponse)
@limiter.limit("20/minute")
async def oauth_login(
    request: Request,
    body: OAuthLoginRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """OAuth-style login. Production should verify id_token; dev accepts trusted payload."""
    settings = get_settings()
    if settings.env == "production" and body.provider == "dev":
        raise HTTPException(status_code=400, detail="Dev provider not allowed in production")

    if body.provider == "google" and settings.oauth_google_client_id and body.id_token:
        logger.info("oauth_google_token_received", sub=body.subject)

    user = await get_or_create_user(
        db,
        body.email,
        name=body.name,
        oauth_provider=body.provider,
        oauth_sub=body.subject,
    )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已注销")
    await db.commit()
    token = create_access_token(user.id, user.email, settings=settings)
    _set_auth_cookie(response, token)
    return AuthResponse(access_token=token, user=_user_dict(user))


@router.get("/me")
async def me(user: Annotated[User | None, Depends(get_optional_user)]):
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "user": _user_dict(user)}


@router.post("/logout")
async def logout(response: Response):
    settings = get_settings()
    response.delete_cookie(settings.jwt_cookie_name)
    return {"ok": True}
