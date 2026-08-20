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
            "duration": [4, 5, 6, 7, 8, 10, 12],       # 默认 4s
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
        description="使用动作本地上传的参考视频（与 template_id 二选一）")
    template_id: Optional[str] = Field(
        default=None,
        description="使用模板库中的动作模板作为参考视频（优先于本地参考视频）")


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

    if req.template_id:
        from app.services.template_store import template_store
        if template_store.get(req.template_id) is None:
            raise HTTPException(status_code=400, detail="动作模板不存在")
    if req.template_id or req.use_reference_video:
        if (not req.template_id and
                not (session.storage.root / "reference_video.mp4").is_file()):
            raise HTTPException(status_code=400,
                                detail="尚未上传参考视频，请先上传或取消勾选")
        # Ark 参考媒体只接受公网 URL，需经 OSS 中转
        from app.core.oss_uploader import oss_configured
        if not oss_configured():
            raise HTTPException(
                status_code=400,
                detail="参考视频需要公网 URL（经 OSS 中转），但未配置 OSS。"
                       "请在「设置」中配置 OSS，或设置对应环境变量")

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


# ------------------------------------------------------------ 批量生成
class BatchGenerateRequest(BaseModel):
    action_ids: list[str] = Field(..., min_length=1, max_length=200)
    model: Optional[str] = None
    resolution: str = Field(default="480p")
    ratio: str = Field(default="adaptive")


def _default_generate_payload(session, action: dict, template: dict,
                              model: str, resolution: str, ratio: str) -> dict:
    """按动作的默认配置构造生成请求（与前端单动作面板的默认逻辑一致）。"""
    variant = template.get("variant") or action.get("name", "")
    prompt = f"{variant}：图片参考视频进行动作，固定镜头，无运镜，背景不变。"
    duration = template.get("duration_hint") or 4
    return {
        "prompt": prompt,
        "model": model,
        "params": {"resolution": resolution, "ratio": ratio, "duration": duration},
        "first_frame": {"kind": "action"},
        "template_id": template["id"],
        "use_reference_video": False,
    }


@router.post("/sprites/{sprite_id}/batch-generate")
def batch_generate(sprite_id: str, req: BatchGenerateRequest):
    """为多个动作按各自默认配置提交生成任务（并发闸门自动限流排队）。

    每个动作要求：首帧参考图已物化 + 已关联动作模板；不满足的跳过并给出
    原因，不影响其他动作。
    """
    s = get_settings()
    if not s.generate_enabled:
        raise HTTPException(status_code=400, detail="未配置 Ark API Key，无法生成")
    from app.core.oss_uploader import oss_configured
    if not oss_configured():
        raise HTTPException(status_code=400,
                            detail="批量生成使用动作模板（需 OSS 中转），请先在「设置」配置 OSS")

    model = req.model or s.ark_model
    if model not in {m["id"] for m in s.ark_models_list}:
        raise HTTPException(status_code=400, detail=f"不支持的模型: {model}")

    from app.services.sprite_store import sprite_store
    from app.services.template_store import template_store

    submitted, skipped = [], []
    for aid in req.action_ids:
        if sprite_store.sprite_of_action(aid) != sprite_id:
            skipped.append({"action_id": aid, "name": aid, "reason": "不属于该精灵"})
            continue
        try:
            action = sprite_store.get_action(sprite_id, aid)
        except Exception:
            skipped.append({"action_id": aid, "name": aid, "reason": "动作不存在"})
            continue
        name = action.get("name", aid)

        session = get_session(aid)
        if not (session.storage.root / "first_frame.png").is_file():
            skipped.append({"action_id": aid, "name": name, "reason": "缺少首帧参考图"})
            continue
        template = template_store.get(action.get("template_id") or "")
        if template is None:
            skipped.append({"action_id": aid, "name": name,
                            "reason": "未关联动作模板（请进入动作手动生成）"})
            continue

        payload = _default_generate_payload(session, action, template,
                                            model, req.resolution, req.ratio)

        def _job(ctx, _session=session, _payload=payload):
            from app.core.video_generator import run_generate
            generate_gate.acquire(ctx)
            try:
                return run_generate(_session, _payload, ctx)
            finally:
                generate_gate.release()

        job = job_manager.submit("generate", _job, pool="io")
        submitted.append({"action_id": aid, "name": name, "job_id": job.id,
                          "variant": template.get("variant"),
                          "duration": payload["params"]["duration"]})

    return {"submitted": submitted, "skipped": skipped,
            "model": model, "resolution": req.resolution}


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


# ------------------------------------------------------------ 动作模板库
class TemplateScanRequest(BaseModel):
    dir: str = Field(..., description="包含动作模板视频的本机目录")


@router.get("/templates")
def list_templates():
    from app.services.template_store import template_store
    return {"templates": template_store.list()}


@router.post("/templates/scan")
def scan_templates(req: TemplateScanRequest):
    """扫描目录导入动作模板（按 key+变体 去重，幂等）。"""
    from pathlib import Path as _P
    from app.services.template_store import template_store
    try:
        return template_store.scan_import(_P(req.dir.strip()))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/templates/{template_id}/video")
def template_video(template_id: str):
    from app.services.template_store import template_store
    t = template_store.get(template_id)
    if t is None or not template_store.path(t).is_file():
        raise HTTPException(status_code=404, detail="模板不存在")
    return FileResponse(str(template_store.path(t)), media_type="video/mp4")


@router.delete("/templates/{template_id}")
def delete_template(template_id: str):
    from app.services.template_store import template_store
    return {"deleted": template_store.delete(template_id)}


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
