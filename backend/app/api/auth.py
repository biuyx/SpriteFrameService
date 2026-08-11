"""认证 API：登录（校验令牌并下发 Cookie）、登出、状态查询。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import get_settings
from app.security import (
    COOKIE_NAME, cookie_value, is_authenticated, login_throttled,
    record_login_failure, reset_login_failures, use_secure_cookie, verify_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    token: str = Field(..., description="访问令牌")


@router.get("/status")
def auth_status(request: Request):
    """是否需要认证 / 当前是否已认证。前端据此决定是否显示登录页。"""
    settings = get_settings()
    return {
        "required": settings.auth_enabled,
        "authenticated": is_authenticated(request),
    }


@router.post("/login")
def login(request: Request, response: Response, req: LoginRequest):
    settings = get_settings()
    if not settings.auth_enabled:
        return {"ok": True, "required": False}

    if login_throttled(request):
        raise HTTPException(
            status_code=429, detail="失败次数过多，请稍后再试"
        )

    if not verify_token(req.token.strip()):
        record_login_failure(request)
        raise HTTPException(status_code=401, detail="令牌不正确")

    reset_login_failures(request)
    response.set_cookie(
        key=COOKIE_NAME,
        value=cookie_value(settings.auth_token_value),
        max_age=settings.auth_session_hours * 3600,
        httponly=True,       # 前端 JS 读不到，降低 XSS 窃取风险
        samesite="lax",
        secure=use_secure_cookie(request),
        path="/",
    )
    return {"ok": True, "required": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}
