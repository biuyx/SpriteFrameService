"""访问认证：共享令牌（Bearer Header + HttpOnly Cookie 双通道）。

默认关闭：未配置 SPRITE_AUTH_TOKEN 时行为与不带认证完全一致，
本机 127.0.0.1 自用不受影响。

为什么需要 Cookie：前端有大量 `<img src>` / `<video src>` / 下载链接直接
指向 API（帧图像、姿势叠加、魔棒 mask、导出下载），这类请求无法携带自定义
请求头。登录时下发 HttpOnly Cookie 可让它们自动通过，同时保留 Bearer
Header 供脚本与 CLI 使用。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

COOKIE_NAME = "sprite_auth"

# 无需认证即可访问的 API（登录流程自身与健康检查）
EXEMPT_PATHS = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/logout",
}

# 登录失败节流：同一 IP 在窗口内失败次数超限则拒绝
_FAIL_WINDOW_SECONDS = 300
_FAIL_MAX_ATTEMPTS = 10
_login_failures: Dict[str, Deque[float]] = defaultdict(deque)


def cookie_value(token: str) -> str:
    """由令牌派生 Cookie 值，避免把原始令牌直接存进浏览器 Cookie。"""
    return hashlib.sha256(f"spriteframe:{token}".encode("utf-8")).hexdigest()


def verify_token(candidate: str) -> bool:
    """常量时间比对访问令牌。"""
    expected = get_settings().auth_token_value
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


def verify_cookie(candidate: str) -> bool:
    """常量时间比对 Cookie 值。"""
    expected = get_settings().auth_token_value
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, cookie_value(expected))


def is_authenticated(request: Request) -> bool:
    """请求是否已通过认证（Header 或 Cookie 任一即可）。"""
    settings = get_settings()
    if not settings.auth_enabled:
        return True

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        if verify_token(auth_header[7:].strip()):
            return True

    return verify_cookie(request.cookies.get(COOKIE_NAME, ""))


# MCP（给 AI 用的接口）：挂在 /api 下面，才会经过本中间件——挂到别处就等于不设防
MCP_PATH = "/api/mcp"


def is_mcp_path(path: str) -> bool:
    return path == MCP_PATH or path.startswith(MCP_PATH + "/")


def bearer_of(headers) -> str:
    auth = headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def agent_of(headers) -> dict | None:
    """MCP 调用方身份：平台口令 = 管理员（全部权限）；AI 密钥 = 该密钥的权限。

    只认 Bearer，不认 Cookie：浏览器自动带的 Cookie 不该能驱动 AI 接口。
    认证关闭时（本机自用）谁都放行，记为匿名。
    """
    from app.services.agent_keys import SCOPES, agent_key_store
    token = bearer_of(headers)
    if token:
        if verify_token(token):
            return {"id": "admin", "name": "平台口令", "scopes": list(SCOPES)}
        rec = agent_key_store.verify(token)
        if rec:
            return rec
    if not get_settings().auth_enabled:
        return {"id": "anonymous", "name": "匿名（未启用认证）", "scopes": list(SCOPES)}
    return None


def use_secure_cookie(request: Request) -> bool:
    """是否给 Cookie 加 Secure 标志。"""
    mode = (get_settings().auth_cookie_secure or "auto").strip().lower()
    if mode in ("true", "1", "yes", "on"):
        return True
    if mode in ("false", "0", "no", "off"):
        return False
    # auto：按当前请求协议判断（反代场景依赖 X-Forwarded-Proto）
    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",")[0].strip() or request.url.scheme
    return scheme == "https"


# --- 登录节流 ---
def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_throttled(request: Request) -> bool:
    """该 IP 是否因失败次数过多被暂时拒绝。"""
    now = time.time()
    bucket = _login_failures[_client_ip(request)]
    while bucket and now - bucket[0] > _FAIL_WINDOW_SECONDS:
        bucket.popleft()
    return len(bucket) >= _FAIL_MAX_ATTEMPTS


def record_login_failure(request: Request) -> None:
    _login_failures[_client_ip(request)].append(time.time())


def reset_login_failures(request: Request) -> None:
    _login_failures.pop(_client_ip(request), None)


def generate_token(nbytes: int = 32) -> str:
    """生成一个可直接用作 SPRITE_AUTH_TOKEN 的随机令牌。"""
    return secrets.token_urlsafe(nbytes)


class AuthMiddleware(BaseHTTPMiddleware):
    """对 /api/* 强制认证；静态前端放行，以便登录页本身能加载。"""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()

        if not settings.auth_enabled:
            return await call_next(request)

        # CORS 预检不带凭证，必须放行，否则浏览器拿不到 CORS 响应头
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api") or path in EXEMPT_PATHS:
            return await call_next(request)

        if is_mcp_path(path):
            if agent_of(request.headers) is None:
                return JSONResponse(
                    {"detail": "MCP 需要 Bearer 密钥：平台访问口令，"
                               "或 scripts/agent_keys.py 发的 AI 密钥"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

        if not is_authenticated(request):
            return JSONResponse(
                {"detail": "未认证或登录已过期，请重新登录"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)
