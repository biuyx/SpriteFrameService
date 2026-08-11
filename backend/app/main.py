"""SpriteFrameService 应用入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.config import get_settings
from app.security import AuthMiddleware

logger = logging.getLogger(__name__)


def _warn_if_exposed(settings) -> None:
    """未启用认证却监听在非回环地址时给出告警。"""
    if settings.auth_enabled:
        logger.info("访问认证：已启用（SPRITE_AUTH_TOKEN）")
        return

    host = (settings.host or "").strip()
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "服务监听在 %s 且未启用认证，任何能访问该端口的人都可以读写你的数据。"
            "请设置 SPRITE_AUTH_TOKEN 或在反代层加认证。", host or "(未指定)"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    _warn_if_exposed(settings)
    yield
    # 优雅关闭：仅释放内存与文件句柄，磁盘数据保留（重启后按需恢复）
    from app.services.session import session_manager
    session_manager.release_all()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="SpriteFrameService",
        description="精灵帧工作室 - 后端服务 API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # 中间件顺序：后添加的更靠外。先加认证、再加 CORS，
    # 这样 401 响应也能带上 CORS 头，跨域前端才能正确读到错误。
    app.add_middleware(AuthMiddleware)

    # CORS：默认放开（本机/局域网自用）。通配来源下不能带凭证——浏览器会
    # 直接拒绝该组合，因此仅在显式配置了具体来源时才允许 credentials。
    origins = settings.cors_origins_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "app": "SpriteFrameService"}

    # 托管前端构建产物（若存在）
    frontend_dir = settings.resolved_frontend_dir
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    return app


app = create_app()
