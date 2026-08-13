"""会话管理 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import get_session
from app.services.session import session_manager

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("")
def create_session():
    """匿名会话已下线：工作单位改为精灵动作（见 /api/sprites）。"""
    raise HTTPException(
        status_code=410,
        detail="匿名会话已下线。请先创建精灵与动作（POST /api/sprites → "
               "POST /api/sprites/{id}/actions → .../open）。",
    )


@router.get("")
def list_sessions():
    """旧版匿名会话列表（仅供认领，正常入口在 /api/sprites）。"""
    return session_manager.list()


@router.get("/{session_id}")
def get_session_detail(session_id: str):
    session = get_session(session_id)
    return session.summary()


@router.delete("/{session_id}")
def delete_session(session_id: str):
    ok = session_manager.delete(session_id)
    return {"deleted": ok, "id": session_id}
