"""API 路由聚合。"""
from __future__ import annotations

from fastapi import APIRouter

from . import (
    analysis,
    auth,
    background,
    capabilities,
    export_api,
    frames,
    generate,
    history_api,
    image_ops,
    jobs,
    sessions,
    settings_api,
    sprites,
    videos,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(capabilities.router)
api_router.include_router(sprites.router)
api_router.include_router(sessions.router)
api_router.include_router(videos.router)
api_router.include_router(frames.router)
api_router.include_router(analysis.router)
api_router.include_router(background.router)
api_router.include_router(image_ops.router)
api_router.include_router(export_api.router)
api_router.include_router(history_api.router)
api_router.include_router(generate.router)
api_router.include_router(settings_api.router)
api_router.include_router(jobs.router)
