"""视频生成 API：Seedance 生成、take 版本管理、并发闸门、重挂。"""
from __future__ import annotations

import threading
from typing import Optional

import cv2
import numpy as np
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import get_session
from app.config import get_settings
from app.services.job_manager import job_manager
from app.services.take_store import TakeStore

router = APIRouter(tags=["generate"])


# ------------------------------------------------------------ 生成并发闸门
class _ConcurrencyGate:
    """限制同时进行中的生成任务数。上限每次现取（设置修改立即生效）。

    超限的任务排队等待而不是拒绝——远端生成本来就要等几分钟，
    多等一会儿比让用户重试友好；等待中可取消。
    """

    def __init__(self):
        self._active = 0
        self._cond = threading.Condition()

    @property
    def active(self) -> int:
        return self._active

    def acquire(self, ctx) -> None:
        with self._cond:
            while self._active >= max(1, get_settings().generate_max_concurrent):
                ctx.report(1, f"排队中（生成并发已满 {self._active} 个）...")
                if ctx.cancelled():
                    raise RuntimeError("已取消")
                self._cond.wait(timeout=2)
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()


generate_gate = _ConcurrencyGate()


# ------------------------------------------------------------ 能力
@router.get("/generate/capabilities")
def generate_capabilities():
    s = get_settings()
    return {
        "configured": s.generate_enabled,
        "models": s.ark_models_list,
        "default_model": s.ark_model,
        "params": {
            "resolution": ["480p", "720p", "1080p"],   # 480p 优先（成本最低）
            "ratio": ["adaptive", "1:1", "16:9", "9:16", "4:3", "3:4"],
            "duration": [4, 5, 6, 8, 10, 12],          # 默认 4s（mini 下限档）
        },
        "defaults": {"resolution": "480p", "ratio": "adaptive", "duration": 4},
        "concurrent": {"limit": s.generate_max_concurrent,
                       "active": generate_gate.active},
        "prompt_templates": _prompt_templates(),
    }


def _prompt_templates() -> dict:
    from app.services.prompt_templates import template_payload
    return template_payload()


# ------------------------------------------------------------ 生成
class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)
    model: Optional[str] = Field(default=None, description="缺省用默认模型")
    params: dict = Field(default_factory=dict, description="resolution/ratio/duration/seed")
    first_frame: dict = Field(
        ..., description='必填：{"kind":"action"} 或 {"kind":"frame","frame_index":N}')
    use_reference_video: bool = Field(
        default=False,
        description="使用已上传的参考视频（动作/镜头节奏参考）。"
                    "注意：Ark 不允许 first_frame 与参考媒体混用，启用后首帧图"
                    "将自动改以 reference_image 角色传入")


@router.post("/sessions/{session_id}/generate")
def generate_video(session_id: str, req: GenerateRequest):
    s = get_settings()
    if not s.generate_enabled:
        raise HTTPException(status_code=400,
                            detail="未配置 Ark API Key，无法生成（SPRITE_ARK_API_KEY）")
    session = get_session(session_id)

    # 模型白名单
    model = req.model or s.ark_model
    if model not in {m["id"] for m in s.ark_models_list}:
        raise HTTPException(status_code=400, detail=f"不支持的模型: {model}")

    # 首帧必填且必须真实可用（角色一致性依赖参考图，不允许纯文生）
    from app.core.video_generator import resolve_first_frame
    if resolve_first_frame(session, req.first_frame) is None:
        raise HTTPException(
            status_code=400,
            detail="必须提供角色首帧参考图：上传参考图，或选择已有帧作为首帧")

    if req.use_reference_video:
        if not (session.storage.root / "reference_video.mp4").is_file():
            raise HTTPException(status_code=400,
                                detail="尚未上传参考视频，请先上传或取消勾选")
        # Ark 参考媒体只接受公网 URL，需经 OSS 中转
        from app.core.oss_uploader import oss_configured
        if not oss_configured():
            raise HTTPException(
                status_code=400,
                detail="参考视频需要公网 URL（经 OSS 中转），但未配置 OSS。"
                       "请设置环境变量 OSS_BUCKET、OSS_PUBLIC_DOMAIN、"
                       "ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET 后重启服务")

    payload = req.model_dump()
    payload["model"] = model

    def _job(ctx):
        from app.core.video_generator import run_generate
        generate_gate.acquire(ctx)   # 并发上限（可在设置中调整，默认 5）
        try:
            return run_generate(session, payload, ctx)
        finally:
            generate_gate.release()

    # io 池 + 不占会话锁：纯网络等待，只写新 take 文件，不碰帧数据
    job = job_manager.submit("generate", _job, pool="io")
    return {"job_id": job.id}


# ------------------------------------------------------------ 首帧参考图
@router.post("/sessions/{session_id}/first-frame")
async def upload_first_frame(session_id: str, file: UploadFile = File(...)):
    """上传角色首帧参考图（生成的必要输入），存为动作的 first_frame.png。"""
    session = get_session(session_id)
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="参考图超过 10MB")
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise HTTPException(status_code=400, detail="不是可识别的图片文件")

    dest = session.storage.root / "first_frame.png"
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise HTTPException(status_code=400, detail="图片编码失败")
    dest.write_bytes(buf.tobytes())

    # 同步到动作实体（血统来源标记为上传）
    from app.services.sprite_store import sprite_store
    sprite_id = sprite_store.sprite_of_action(session_id)
    if sprite_id:
        sprite_store.update_action(sprite_id, session_id, {
            "first_frame": {"kind": "upload", "file": "first_frame.png",
                            "filename": file.filename},
        })
    return {"ok": True, "file": "first_frame.png"}


@router.get("/sessions/{session_id}/first-frame")
def get_first_frame(session_id: str):
    """返回当前动作的首帧参考图（预览用）。"""
    session = get_session(session_id)
    p = session.storage.root / "first_frame.png"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="尚未设置首帧参考图")
    return Response(content=p.read_bytes(), media_type="image/png")


# ------------------------------------------------------------ 参考视频
REFERENCE_VIDEO_MAX_MB = 20   # base64 后随请求体走，保守限制


@router.post("/sessions/{session_id}/reference-video")
async def upload_reference_video(session_id: str, file: UploadFile = File(...)):
    """上传参考视频（动作/镜头节奏参考，生成时可选启用）。"""
    session = get_session(session_id)
    raw = await file.read()
    if len(raw) > REFERENCE_VIDEO_MAX_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"参考视频超过 {REFERENCE_VIDEO_MAX_MB}MB——参考视频只取动作节奏，"
                   f"请裁短或压缩后上传")
    # 校验确实是视频（cv2 能开且能读到帧）
    import tempfile, os
    fd, tmp = tempfile.mkstemp(suffix=".mp4")
    try:
        os.write(fd, raw)
        os.close(fd)
        cap = cv2.VideoCapture(tmp)
        ok = cap.isOpened() and cap.read()[0]
        cap.release()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if not ok:
        raise HTTPException(status_code=400, detail="不是可识别的视频文件")

    dest = session.storage.root / "reference_video.mp4"
    dest.write_bytes(raw)
    return {"ok": True, "bytes": len(raw), "filename": file.filename}


@router.get("/sessions/{session_id}/reference-video")
def get_reference_video(session_id: str):
    """返回参考视频（预览用）。"""
    session = get_session(session_id)
    p = session.storage.root / "reference_video.mp4"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="尚未上传参考视频")
    return FileResponse(str(p), media_type="video/mp4")


@router.delete("/sessions/{session_id}/reference-video")
def delete_reference_video(session_id: str):
    session = get_session(session_id)
    p = session.storage.root / "reference_video.mp4"
    existed = p.is_file()
    if existed:
        p.unlink()
    return {"deleted": existed}


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
