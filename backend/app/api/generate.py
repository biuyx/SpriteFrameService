"""视频生成 API：Seedance 生成、take 版本管理、每日配额、重挂。"""
from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import get_session
from app.config import get_settings
from app.services.job_manager import job_manager
from app.services.take_store import TakeStore

router = APIRouter(tags=["generate"])


# ------------------------------------------------------------ 配额
def _quota_path():
    return get_settings().resolved_data_dir / "generate_quota.json"

def _today() -> str:
    return time.strftime("%Y-%m-%d")

def quota_state() -> dict:
    limit = get_settings().generate_daily_limit
    p = _quota_path()
    used = 0
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("date") == _today():
                used = int(d.get("count", 0))
        except (ValueError, OSError):
            pass
    return {"limit": limit, "used": used,
            "remaining": None if limit <= 0 else max(0, limit - used)}

def quota_consume() -> None:
    st = quota_state()
    if st["limit"] > 0 and st["used"] >= st["limit"]:
        raise HTTPException(status_code=429,
                            detail=f"今日生成额度已用完（{st['limit']} 次），明天再试或调大 SPRITE_GENERATE_DAILY_LIMIT")
    p = _quota_path()
    p.write_text(json.dumps({"date": _today(), "count": st["used"] + 1}),
                 encoding="utf-8")


# ------------------------------------------------------------ 能力
@router.get("/generate/capabilities")
def generate_capabilities():
    s = get_settings()
    return {
        "configured": s.generate_enabled,
        "model": s.ark_model,
        "quota": quota_state(),
        "params": {
            "resolution": ["480p", "720p", "1080p"],
            "ratio": ["adaptive", "1:1", "16:9", "9:16", "4:3", "3:4"],
            "duration": [3, 4, 5, 6, 8, 10, 12],
        },
    }


# ------------------------------------------------------------ 生成
class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)
    params: dict = Field(default_factory=dict, description="resolution/ratio/duration/seed")
    first_frame: Optional[dict] = Field(
        default=None, description='{"kind":"action"} 或 {"kind":"frame","frame_index":N}')


@router.post("/sessions/{session_id}/generate")
def generate_video(session_id: str, req: GenerateRequest):
    if not get_settings().generate_enabled:
        raise HTTPException(status_code=400,
                            detail="未配置 Ark API Key，无法生成（SPRITE_ARK_API_KEY）")
    session = get_session(session_id)
    quota_consume()

    payload = req.model_dump()

    def _job(ctx):
        from app.core.video_generator import run_generate
        return run_generate(session, payload, ctx)

    # io 池 + 不占会话锁：纯网络等待，只写新 take 文件，不碰帧数据
    job = job_manager.submit("generate", _job, pool="io")
    return {"job_id": job.id}


# ------------------------------------------------------------ take 管理
@router.get("/sessions/{session_id}/takes")
def list_takes(session_id: str):
    session = get_session(session_id)
    return TakeStore(session.storage).list()


@router.get("/sessions/{session_id}/takes/{take_id}/video")
def take_video(session_id: str, take_id: str):
    session = get_session(session_id)
    p = TakeStore(session.storage).video_file(take_id)
    if p is None:
        raise HTTPException(status_code=404, detail="该版本视频不存在")
    return FileResponse(str(p), media_type="video/mp4")


@router.post("/sessions/{session_id}/takes/{take_id}/select")
def select_take(session_id: str, take_id: str):
    """切换当前使用的版本；之后抽帧/预览针对它。"""
    session = get_session(session_id)
    ts = TakeStore(session.storage)
    take = ts.get(take_id)
    if take is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    if take.get("status") != "succeeded":
        raise HTTPException(status_code=400, detail="只有已成功的版本可以使用")

    # 释放解码句柄再切换（Windows 文件占用）
    session.release_video_handle()
    if not ts.set_current(take_id):
        raise HTTPException(status_code=400, detail="切换失败")

    try:
        session.video_info = session.video_processor.load_video(
            str(session.storage.video_path))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"视频不可解析: {e}")

    # 工序记录：素材来源切换（新链）
    from app.services import recipe
    recipe.set_source(session, {
        "kind": take.get("source", "generate"),
        "take_id": take_id,
        "model": take.get("model"),
        "prompt": (take.get("prompt") or "")[:300],
        "seed": take.get("seed"),
    })
    return {"current": take_id, "video_info": session.video_info.model_dump()}


@router.delete("/sessions/{session_id}/takes/{take_id}")
def delete_take(session_id: str, take_id: str):
    session = get_session(session_id)
    ts = TakeStore(session.storage)
    take = ts.get(take_id)
    if take is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    was_current = ts.current_id() == take_id
    if was_current:
        session.release_video_handle()
    ok = ts.delete(take_id)
    if was_current:
        # current 变了，重载（可能为 None）
        session.video_info = None
        p = session.storage.video_path
        if p:
            try:
                session.video_info = session.video_processor.load_video(str(p))
            except Exception:
                pass
    return {"deleted": ok, "id": take_id,
            "was_generated": take.get("source") == "generate"}


@router.post("/sessions/{session_id}/takes/reconcile")
def reconcile_takes(session_id: str):
    """重挂未完结的远端生成任务（重启/超时后取回结果）。"""
    if not get_settings().generate_enabled:
        return {"reconciled": 0, "results": []}
    session = get_session(session_id)

    def _job(ctx):
        from app.core.video_generator import run_reconcile
        return run_reconcile(session, ctx)

    job = job_manager.submit("reconcile", _job, pool="io")
    return {"job_id": job.id}
