"""帧管理 API：抽帧、列表、图像服务、选择、删除、重排、循环过渡、首尾补帧。"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from fastapi import (APIRouter, File, Form, HTTPException, Query, Response,
                     UploadFile)
from pydantic import BaseModel

from app.api.deps import get_session, require_indices
from app.config import get_settings
from app.api.schemas import (
    ExtractRequest, LoopTransitionRequest, ReorderRequest,
    SelectionRequest, SupplementRequest,
)
from app.core.crossfade import apply_loop_transition
from app.core.frame_extractor import FrameExtractor
from app.core.frame_supplement import interpolate_frames
from app.models.frame_data import FrameData
from app.services.job_manager import job_manager
from app.utils.image_utils import encode_preview, read_image

router = APIRouter(prefix="/sessions/{session_id}/frames", tags=["frames"])


# ---------- 抽帧 ----------
def submit_extract_job(session, start_time: float, end_time: float, fps: float,
                       keep: Optional[List[int]] = None,
                       keep_total: Optional[int] = None):
    """校验参数并提交抽帧任务（单动作端点与批量抽帧共用）。

    keep/keep_total：模板规则的选帧保留集——抽帧后自动删除保留集之外的帧；
    实际抽出帧数与校准时（keep_total）不符则跳过，全部保留并在消息里说明。
    """
    if session.video_info is None:
        raise HTTPException(status_code=400, detail="请先上传视频")
    if end_time > session.video_info.duration + 0.001:
        raise HTTPException(status_code=400, detail=f"结束时间超出视频时长 {session.video_info.duration:.2f}s")
    if end_time <= start_time:
        raise HTTPException(status_code=400, detail="结束时间必须大于开始时间")

    # 每帧一张 PNG 落盘，长视频 × 高帧率足以撑满磁盘，这里给出上限
    limit = get_settings().max_extract_frames
    planned = int((end_time - start_time) * fps) + 1
    if limit and planned > limit:
        raise HTTPException(
            status_code=400,
            detail=f"本次将抽取约 {planned} 帧，超过上限 {limit}。请缩短时间范围或降低帧率。",
        )

    return job_manager.submit(
        "extract",
        lambda ctx: run_extract(session, start_time, end_time, fps, keep, keep_total, ctx),
        lock=session.lock)


def run_extract(session, start_time: float, end_time: float, fps: float,
                keep: Optional[List[int]], keep_total: Optional[int], ctx) -> Optional[dict]:
    """抽帧任务体（端点与自动流水线共用）。调用方需持有会话锁。"""
    extractor = FrameExtractor()
    ctx.register_cancel(extractor.cancel)
    ctx.report(0, "开始抽帧...")

    video_path = session.storage.video_path
    frames = extractor.extract_frames(
        str(video_path),
        start_time, end_time, fps,
        session.video_info,
        progress_callback=lambda cur, total, pct: ctx.report(pct, f"抽帧 {cur}/{total}")
    )

    if ctx.cancelled():
        return None

    session.frame_manager.clear()
    session.frame_store.save_raw_frames(frames)
    session.frame_manager.add_frames(frames)
    session.persist_metadata()

    # 按模板规则的保留集删除多余帧（校准帧数不符则安全跳过）
    msg = f"抽帧完成: {len(frames)} 帧"
    kept = None
    if keep is not None and keep_total:
        if len(frames) == keep_total:
            fm = session.frame_manager
            keep_set = set(keep)
            for idx in range(len(frames) - 1, -1, -1):
                if idx not in keep_set:
                    fr = fm.get_frame(idx)
                    if fr:
                        session.frame_store.remove_frame_files(fr.id)
                    fm.remove_frame(idx)
            session.persist_metadata()
            kept = fm.frame_count
            msg = f"抽帧完成: {len(frames)} 帧，按规则保留 {kept} 帧"
        else:
            msg = (f"抽帧完成: {len(frames)} 帧"
                   f"（与校准时 {keep_total} 帧不符，未应用选帧结果）")
    ctx.report(100, msg)
    from app.services import recipe
    recipe.record_step(session, "extract",
                       {"start_time": start_time, "end_time": end_time,
                        "fps": fps},
                       {"extracted": len(frames), "kept": kept})
    return {"extracted": len(frames), "kept": kept,
            "start_time": start_time, "end_time": end_time}


@router.post("/extract")
def extract_frames(session_id: str, req: ExtractRequest):
    session = get_session(session_id)
    keep = keep_total = None
    if req.template_rule_id:
        from app.services.template_store import template_store
        rule = (template_store.get(req.template_rule_id) or {}).get("extract_rule") or {}
        keep, keep_total = rule.get("keep"), rule.get("total")
    job = submit_extract_job(session, req.start_time, req.end_time, req.fps,
                             keep=keep, keep_total=keep_total)
    return {"job_id": job.id}


# ---------- 参考抽帧规则：从当前会话状态计算并保存到模板 ----------
class SaveExtractRuleRequest(BaseModel):
    template_id: str


@router.post("/extract-rule")
def save_extract_rule(session_id: str, req: SaveExtractRuleRequest):
    """把最近一次抽帧的参数 + 当前保留的帧，存为模板的参考抽帧规则。

    保留集按帧 timestamp 映射回抽帧序号（删帧/重排不影响），补帧等
    特殊帧（tag 非空）不计入；没删过帧则只存抽帧三参数。
    """
    import time as _time

    from app.services import recipe
    from app.services.template_store import template_store

    session = get_session(session_id)
    steps = [s for s in recipe.read(session).get("steps", []) if s.get("op") == "extract"]
    if not steps:
        raise HTTPException(status_code=400, detail="尚未抽帧——先抽帧并确认效果，再保存规则")
    last = steps[-1]
    p = last.get("params", {})
    start, end, fps = p.get("start_time"), p.get("end_time"), p.get("fps")
    if start is None or end is None or not fps:
        raise HTTPException(status_code=400, detail="抽帧记录不完整，无法保存规则")
    total = (last.get("result") or {}).get("extracted")

    fm = session.frame_manager
    if fm.frame_count == 0:
        raise HTTPException(status_code=400, detail="当前没有帧，请先抽帧")

    # 当前留存帧 → 原始抽帧序号（timestamp 精确落在 start + i/fps 网格上）
    keep, unmapped = set(), 0
    for i in range(fm.frame_count):
        fr = fm.get_frame(i)
        if fr is None:
            continue
        if fr.tag:                       # 补帧/过渡帧不属于抽帧网格
            unmapped += 1
            continue
        orig = round((fr.timestamp - start) * fps)
        on_grid = abs(fr.timestamp - (start + orig / fps)) < 0.5 / fps
        if on_grid and 0 <= orig < (total or 1 << 30):
            keep.add(orig)
        else:
            unmapped += 1

    rule = {"start": start, "end": end, "fps": fps,
            "updated_at": _time.time(), "calibrated_on": session_id}
    if total:
        rule["total"] = total
        if 0 < len(keep) < total:        # 有删帧才记录保留集
            rule["keep"] = sorted(keep)
    try:
        tpl = template_store.update(req.template_id, {"extract_rule": rule})
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"rule": rule, "kept": len(keep), "total": total, "unmapped": unmapped,
            "template": tpl.get("variant") or tpl.get("key")}


# ---------- 帧包导出/导入（外部工具加工与替换） ----------
@router.get("/export-zip")
def export_frames_zip(session_id: str,
                      type: str = Query("raw", pattern="^(raw|processed)$")):
    """把当前帧打包下载：文件名为帧序号（0000.png），外部加工后可按序号导回。"""
    import tempfile
    import time as _time
    import zipfile

    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    session = get_session(session_id)
    fm = session.frame_manager
    if fm.frame_count == 0:
        raise HTTPException(status_code=400, detail="当前没有帧")

    tmp_dir = get_settings().resolved_data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    import os
    fd, zpath = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
    os.close(fd)
    manifest, packed = [], 0
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
            for i in range(fm.frame_count):
                fr = fm.get_frame(i)
                if fr is None:
                    continue
                p = fr.processed_path if type == "processed" else fr.image_path
                if p and Path(p).is_file():
                    z.write(p, f"{i:04d}.png")
                    packed += 1
                manifest.append({"file": f"{i:04d}.png", "id": fr.id,
                                 "timestamp": fr.timestamp,
                                 "included": bool(p and Path(p).is_file())})
            import json as _json
            z.writestr("manifest.json",
                       _json.dumps({"type": type, "session": session_id,
                                    "frames": manifest}, ensure_ascii=False, indent=1))
    except Exception:
        Path(zpath).unlink(missing_ok=True)
        raise
    if not packed:
        Path(zpath).unlink(missing_ok=True)
        raise HTTPException(status_code=400,
                            detail="没有可导出的帧" + ("（尚无处理图）" if type == "processed" else ""))

    stamp = _time.strftime("%Y%m%d_%H%M")
    filename = f"frames_{type}_{session_id[:8]}_{stamp}.zip"
    return FileResponse(zpath, media_type="application/zip", filename=filename,
                        background=BackgroundTask(lambda: Path(zpath).unlink(missing_ok=True)))


@router.post("/import-zip")
async def import_frames_zip(session_id: str, mode: str = Form("processed"),
                            file: UploadFile = File(...)):
    """导入帧包（按文件名序号匹配）。

    mode=processed：作为处理图替换到对应帧（外部抠图/修图回填）；
    mode=replace  ：清空现有帧，把包内图片按序号顺序作为新的原始帧。
    """
    import io
    import re
    import zipfile

    import cv2
    import numpy as np

    from app.models.frame_data import FrameStatus
    from app.services import recipe

    if mode not in ("processed", "replace"):
        raise HTTPException(status_code=400, detail="mode 须为 processed 或 replace")
    session = get_session(session_id)

    raw = await file.read()
    if len(raw) > 800 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="帧包超过 800MB")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="不是有效的 zip 文件")

    name_re = re.compile(r"^(\d+)\.(png|jpg|jpeg|webp)$", re.IGNORECASE)
    entries = {}
    ignored = 0
    with z:
        for n in z.namelist():
            base = n.rsplit("/", 1)[-1]
            m = name_re.match(base)
            if not m:
                if base and base != "manifest.json" and not n.endswith("/"):
                    ignored += 1
                continue
            entries[int(m.group(1))] = z.read(n)
    if not entries:
        raise HTTPException(status_code=400,
                            detail="包内没有「序号.png」命名的图片（如 0000.png）")

    def decode(data: bytes):
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None:
            return None
        if img.ndim == 3 and img.shape[2] == 3:
            return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        if img.ndim == 3 and img.shape[2] == 4:
            return cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
        return img

    fm = session.frame_manager
    with session.lock:
        if mode == "processed":
            updated, missing, bad = 0, 0, 0
            for idx in sorted(entries):
                fr = fm.get_frame(idx)
                if fr is None:
                    missing += 1
                    continue
                img = decode(entries[idx])
                if img is None:
                    bad += 1
                    continue
                fr.processed_path = session.frame_store.save_processed(fr.id, img)
                fr.status = FrameStatus.BACKGROUND_REMOVED
                updated += 1
            session.persist_metadata()
            recipe.record_step(session, "frames_import",
                               {"mode": mode}, {"updated": updated})
            return {"mode": mode, "updated": updated, "missing": missing,
                    "bad": bad, "ignored": ignored,
                    "frame_count": fm.frame_count}
        else:
            # 整组替换：删旧帧文件后按序号顺序导入为新原始帧
            for i in range(fm.frame_count - 1, -1, -1):
                fr = fm.get_frame(i)
                if fr:
                    session.frame_store.remove_frame_files(fr.id)
                fm.remove_frame(i)
            frames, bad = [], 0
            for n, idx in enumerate(sorted(entries)):
                img = decode(entries[idx])
                if img is None:
                    bad += 1
                    continue
                frames.append(FrameData(index=len(frames), timestamp=float(len(frames)),
                                        image=img))
            if not frames:
                session.persist_metadata()
                raise HTTPException(status_code=400, detail="包内图片全部无法解码")
            session.frame_store.save_raw_frames(frames)
            fm.add_frames(frames)
            session.persist_metadata()
            recipe.record_step(session, "frames_import",
                               {"mode": mode}, {"imported": len(frames)})
            return {"mode": mode, "imported": len(frames), "bad": bad,
                    "ignored": ignored, "frame_count": fm.frame_count}


# ---------- 列表 ----------
def _frame_summary(frame) -> dict:
    return {
        "id": frame.id,
        "index": frame.index,
        "timestamp": frame.timestamp,
        "status": frame.status.value,
        "is_selected": frame.is_selected,
        "has_raw": frame.has_image,
        "has_processed": frame.has_processed,
        "tag": frame.tag,
        "analysis": {
            "pose": frame.pose_id is not None,
            "contour": frame.contour_id is not None,
            "image": frame.image_feature_id is not None,
            "regional": frame.regional_feature_id is not None,
        },
    }


@router.get("")
def list_frames(session_id: str):
    session = get_session(session_id)
    return {
        "frames": [_frame_summary(f) for f in session.frame_manager.frames],
        "frame_count": session.frame_manager.frame_count,
        "selected_count": session.frame_manager.selected_count,
    }


@router.get("/{index}/info")
def frame_info(session_id: str, index: int):
    session = get_session(session_id)
    frame = session.frame_manager.get_frame(index)
    if frame is None:
        raise HTTPException(status_code=404, detail="帧不存在")
    return _frame_summary(frame)


# ---------- 图像服务 ----------
def _resolve_frame_path(session, frame, kind: str):
    """解析帧图像文件路径：优先 FrameData 记录的路径，其次按 id 计算。"""
    if kind == "processed":
        if frame.processed_path and frame.processed_path.exists():
            return frame.processed_path
        p = session.frame_store.proc_path(frame.id)
        return p if p.exists() else None
    if kind == "preview":
        # 预览优先显示处理结果（若有），使处理后能即时看到最新效果
        if frame.processed_path and frame.processed_path.exists():
            return frame.processed_path
        p = session.frame_store.proc_path(frame.id)
        if p.exists():
            return p
    # raw / preview 回退到 raw
    if frame.image_path and frame.image_path.exists():
        return frame.image_path
    p = session.frame_store.raw_path(frame.id)
    return p if p.exists() else None


@router.get("/{index}/image")
def get_frame_image(
    session_id: str,
    index: int,
    type: str = Query("preview", description="raw/processed/preview"),
    checker: int = Query(1, description="透明图合成棋盘格(1/0)"),
    fit: int = Query(0, description="最大边长缩放(0为原尺寸)"),
):
    session = get_session(session_id)
    frame = session.frame_manager.get_frame(index)
    if frame is None:
        raise HTTPException(status_code=404, detail="帧不存在")

    if type == "processed":
        path = _resolve_frame_path(session, frame, "processed")
        if path is None:
            raise HTTPException(status_code=404, detail="该帧尚无处理结果")
    elif type == "preview":
        path = _resolve_frame_path(session, frame, "preview")
        if path is None:
            raise HTTPException(status_code=404, detail="图像不存在")
    else:
        path = _resolve_frame_path(session, frame, "raw")
        if path is None:
            raise HTTPException(status_code=404, detail="图像不存在")

    img = read_image(path)
    if img is None:
        raise HTTPException(status_code=404, detail="图像读取失败")

    bytes_data = encode_preview(
        img,
        transparent_checker=bool(checker) and type == "preview",
        max_w=fit if fit > 0 else 0,
        max_h=fit if fit > 0 else 0,
    )
    return Response(content=bytes_data, media_type="image/png")


# ---------- 选择 ----------
@router.post("/selection")
def update_selection(session_id: str, req: SelectionRequest):
    session = get_session(session_id)
    fm = session.frame_manager

    if req.mode == "all":
        fm.select_all()
    elif req.mode in ("clear", "none"):
        fm.deselect_all()
    elif req.mode == "invert":
        for f in fm.frames:
            f.is_selected = not f.is_selected
    elif req.mode == "range":
        start = req.range_start if req.range_start is not None else 0
        end = req.range_end if req.range_end is not None else fm.frame_count - 1
        fm.select_range(start, end)
    else:  # set
        fm.deselect_all()
        for i in (req.indices or []):
            if 0 <= i < fm.frame_count:
                fm.select_frame(i, True)

    session.persist_metadata()
    return {"frame_count": fm.frame_count, "selected_count": fm.selected_count}


# ---------- 删除 ----------
@router.delete("/{index}")
def delete_frame(session_id: str, index: int):
    session = get_session(session_id)
    fm = session.frame_manager

    if not (0 <= index < fm.frame_count):
        raise HTTPException(status_code=404, detail="帧不存在")

    frame = fm.get_frame(index)
    if frame:
        session.frame_store.remove_frame_files(frame.id)
    fm.remove_frame(index)
    session.persist_metadata()
    return {"frame_count": fm.frame_count, "selected_count": fm.selected_count}


@router.post("/delete")
def delete_frames(session_id: str, indices: List[int]):
    """批量删除指定帧。"""
    session = get_session(session_id)
    fm = session.frame_manager

    for index in sorted(set(indices), reverse=True):
        if 0 <= index < fm.frame_count:
            frame = fm.get_frame(index)
            if frame:
                session.frame_store.remove_frame_files(frame.id)
            fm.remove_frame(index)

    session.persist_metadata()
    return {"frame_count": fm.frame_count, "selected_count": fm.selected_count}


# ---------- 重排 ----------
@router.post("/reorder")
def reorder_frames(session_id: str, req: ReorderRequest):
    session = get_session(session_id)
    fm = session.frame_manager

    if len(req.new_order) != fm.frame_count:
        raise HTTPException(status_code=400, detail="顺序长度与帧数不符")

    if sorted(req.new_order) != list(range(fm.frame_count)):
        raise HTTPException(status_code=400, detail="顺序必须为 0..N-1 的排列")

    fm.reorder_frames(req.new_order)
    session.persist_metadata()
    return {"frame_count": fm.frame_count}


# ---------- 循环过渡 ----------
@router.post("/loop-transition")
def loop_transition(session_id: str, req: LoopTransitionRequest):
    """对选中帧应用循环过渡（非破坏性），生成 GIF 预览供查看。"""
    session = get_session(session_id)
    indices = require_indices(session, req.indices)
    if len(indices) < 2:
        raise HTTPException(status_code=400, detail="至少需要 2 帧才能进行循环过渡")
    if req.mode not in ("blend", "align"):
        raise HTTPException(status_code=400, detail="mode 仅支持 blend / align")

    def _job(ctx):
        ctx.report(10, "加载帧图像...")
        images = session.load_display_arrays(indices)
        images = [img for img in images if img is not None]
        if len(images) < 2:
            return {"error": "可用帧不足 2 张"}

        ctx.report(40, "应用循环过渡...")
        result_images = apply_loop_transition(images, req.count, req.mode)
        session.clear_frame_arrays()

        # 生成 GIF 预览（透明帧合成到棋盘格）
        from PIL import Image
        from app.utils.image_utils import composite_checkerboard

        pil_frames = []
        for img in result_images:
            if len(img.shape) == 3 and img.shape[2] == 4:
                img = composite_checkerboard(img)
            pil_frames.append(Image.fromarray(img))

        duration = int(1000 / req.fps)
        out_path = session.storage.preview_dir / "loop_transition.gif"
        pil_frames[0].save(
            str(out_path), save_all=True, append_images=pil_frames[1:],
            duration=duration, loop=0,
        )

        ctx.report(100, "循环过渡预览已生成")
        return {
            "original_count": len(indices),
            "result_count": len(result_images),
            "gif": "/api/sessions/{}/frames/preview/loop_transition.gif".format(session_id),
        }

    job = job_manager.submit("loop-transition", _job, lock=session.lock)
    return {"job_id": job.id}


@router.get("/preview/{name}")
def get_preview_file(session_id: str, name: str):
    """返回预览产物（如循环过渡 GIF）。"""
    session = get_session(session_id)
    safe = Path(name).name
    path = session.storage.preview_dir / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="预览文件不存在")
    from fastapi.responses import FileResponse
    media = "image/gif" if safe.endswith(".gif") else "application/octet-stream"
    return FileResponse(str(path), media_type=media)


# ---------- 首尾补帧 ----------
@router.post("/supplement")
def supplement_frames(session_id: str, req: SupplementRequest):
    """在选中帧的尾帧与首帧之间生成中间帧（用于循环衔接），追加到帧管理。"""
    session = get_session(session_id)
    indices = require_indices(session, req.indices)
    if len(indices) < 2:
        raise HTTPException(status_code=400, detail="至少需要 2 帧（首帧和尾帧）")

    def _job(ctx):
        fm = session.frame_manager
        first = fm.get_frame(indices[0])
        last = fm.get_frame(indices[-1])
        if first is None or last is None:
            return {"error": "帧不存在"}

        ctx.report(10, "加载首尾帧...")
        first_img = session.load_display_array(first.index)
        last_img = session.load_display_array(last.index)
        if first_img is None or last_img is None:
            return {"error": "首尾帧图像不可用"}

        ctx.report(30, "生成中间帧...")
        middle_frames = interpolate_frames(
            first_img, last_img, req.num_frames,
            progress_callback=lambda cur, total, msg: ctx.report(
                30 + cur / total * 60, msg
            ),
        )

        # 追加到帧管理（标签：补），时间戳接在最后一帧之后
        base_index = fm.frame_count
        base_ts = last.timestamp if last.timestamp is not None else 0.0
        new_frames = []
        for i, img in enumerate(middle_frames):
            frame = FrameData(
                index=base_index + i,
                timestamp=base_ts + (i + 1) * 0.001,
                image=img,
                is_selected=True,
                tag="补",
            )
            new_frames.append(frame)

        session.frame_store.save_raw_frames(new_frames)
        fm.add_frames(new_frames)
        session.clear_frame_arrays()
        session.persist_metadata()

        ctx.report(100, f"补帧完成，新增 {len(new_frames)} 帧")
        from app.services import recipe
        recipe.record_step(session, "supplement",
                           {"num_frames": req.num_frames, "indices": indices[:20]},
                           {"added": len(new_frames)})
        return {"added": len(new_frames), "total": fm.frame_count}

    job = job_manager.submit("supplement", _job, lock=session.lock)
    return {"job_id": job.id}
