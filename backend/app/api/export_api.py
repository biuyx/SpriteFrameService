"""导出 API：精灵图/GIF/帧/WebP/Godot 导出任务与结果下载。"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.api.deps import get_session, require_indices
from app.api.schemas import ExportRequest
from app.core.crossfade import apply_transition_to_frame_data
from app.core.exporter import Exporter
from app.services.job_manager import job_manager

router = APIRouter(prefix="/sessions/{session_id}/export", tags=["export"])


def _sanitize_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-一-鿿]", "_", name) or "export"


def _safe_child(base: Path, *parts: str) -> Path:
    """在 base 下解析子路径，拒绝越界（.. / 分隔符 / 绝对路径 / 符号链接逃逸）。

    显式拒绝而非静默改写，避免 "..%2F.." 这类输入在反代解码后产生意外语义。
    """
    target = base
    for part in parts:
        if not part or part in (".", "..") or "/" in part or "\\" in part:
            raise HTTPException(status_code=400, detail="非法的名称")
        target = target / part

    base_r = base.resolve()
    target_r = target.resolve()
    if target_r != base_r and base_r not in target_r.parents:
        raise HTTPException(status_code=400, detail="非法的路径")
    return target_r


def apply_size_ops(images: list, scale_cfg, outline_cfg, log=None) -> list:
    """导出时的 缩放 → 描边，返回新图像列表，不改原数组。

    顺序固定：先定稿尺寸再描边，所以描边宽度填多少，成品里就是多少像素。
    描边在缩放之后做，一批帧统一扩同一个边量，帧间尺寸与基线保持一致。
    """
    import numpy as np
    from PIL import Image

    from app.api.image_ops import _ALGO_MAP

    out = list(images)

    if scale_cfg is not None and getattr(scale_cfg, "enabled", False):
        first = next((im for im in out if im is not None), None)
        if first is not None:
            algo = _ALGO_MAP.get(scale_cfg.algorithm, Image.Resampling.LANCZOS)
            h0, w0 = first.shape[:2]

            def target(im):
                # 按比例：每帧各按自身尺寸缩，帧间尺寸有差异时不会被强行拉齐；
                # 固定尺寸：全部拉到同一个目标，保证精灵图每格一致
                if scale_cfg.mode == "percent":
                    k = scale_cfg.percent / 100.0
                    h, w = im.shape[:2]
                    return max(1, int(w * k)), max(1, int(h * k))
                return max(1, scale_cfg.width), max(1, scale_cfg.height)

            changed = False
            new = []
            for im in out:
                if im is None:
                    new.append(im)
                    continue
                tw, th = target(im)
                if (tw, th) == (im.shape[1], im.shape[0]):
                    new.append(im)
                    continue
                new.append(np.array(Image.fromarray(im).resize((tw, th), algo)))
                changed = True
            if changed:
                out = new
                t0 = target(first)
                if log:
                    log(f"缩放 {w0}x{h0}→{t0[0]}x{t0[1]}")

    if (outline_cfg is not None and getattr(outline_cfg, "enabled", False)
            and outline_cfg.width > 0):
        from app.core.outline import add_outline, pad_rgba, required_pad
        rgba = [im for im in out if im is not None and im.ndim == 3 and im.shape[2] == 4]
        if rgba:
            pad = 0
            if outline_cfg.auto_pad and outline_cfg.position in ("outer", "center"):
                pad = required_pad([im[:, :, 3] for im in rgba], outline_cfg.width,
                                   outline_cfg.alpha_threshold)
            new = []
            for im in out:
                if im is None or im.ndim != 3 or im.shape[2] != 4:
                    new.append(im)
                    continue
                src = pad_rgba(im, pad) if pad else im
                new.append(add_outline(src, outline_cfg.width, tuple(outline_cfg.color),
                                       outline_cfg.opacity, outline_cfg.position,
                                       outline_cfg.corner, outline_cfg.antialias,
                                       outline_cfg.alpha_threshold))
            out = new
            if log:
                log(f"描边 {outline_cfg.width}px" + (f"（扩边 {pad}px）" if pad else ""))
        elif log:
            log("跳过描边：没有 RGBA 帧（需先抠图）")
    return out


def _apply_to_frames(frames: list, cfg, ctx):
    """把导出缩放/描边应用到帧副本上（原帧对象与磁盘文件都不动）。"""
    scale_cfg, outline_cfg = getattr(cfg, "scale", None), getattr(cfg, "outline", None)
    on = (scale_cfg is not None and scale_cfg.enabled) or \
         (outline_cfg is not None and outline_cfg.enabled and outline_cfg.width > 0)
    if not on:
        return frames, []
    notes = []
    images = [f.display_image if f.display_image is not None else f.image for f in frames]
    result = apply_size_ops(images, scale_cfg, outline_cfg, notes.append)
    if notes:
        ctx.report(25, " / ".join(notes))
    copies = []
    for f, im in zip(frames, result):
        c = f.model_copy()
        c.processed_image = im
        copies.append(c)
    return copies, notes


def run_export(session, config, indices: list, export_name: str, ctx) -> dict:
    """导出任务体（端点与自动流水线共用）。调用方需持有会话锁。"""
    export_dir = session.storage.export_dir(export_name)

    frames = []
    session.load_display_arrays(indices)
    for idx in indices:
        f = session.frame_manager.get_frame(idx)
        if f is not None:
            frames.append(f)

    if not frames:
        return {"error": "没有可导出的帧"}

    cfg = config.model_copy()
    cfg.output_path = export_dir

    # 循环过渡：非破坏性应用到导出帧（使首尾无缝衔接）
    lt = cfg.loop_transition
    if lt.enabled and len(frames) > 1:
        frames = apply_transition_to_frame_data(frames, lt.count, lt.mode)

    # 缩放 / 描边：同样只作用于导出副本，帧文件保持原样
    frames, size_notes = _apply_to_frames(frames, cfg, ctx)

    ctx.report(30, "开始导出...")
    exporter = Exporter()
    main_path, info = exporter.export(frames, cfg)

    session.clear_frame_arrays()

    files = [f.name for f in sorted(Path(main_path).parent.iterdir()) if f.is_file()]
    ctx.report(100, "导出完成")
    from app.services import recipe
    recipe.record_step(session, "export",
                       {"format": config.format.value
                        if hasattr(config.format, "value") else str(config.format),
                        "name": export_name,
                        "loop_transition": lt.enabled,
                        "size_ops": size_notes,
                        "frame_indices": indices[:50]},
                       {"files": files})
    return {
        "name": export_name,
        "main": Path(main_path).name,
        "info": info,
        "files": files,
        "dir": str(export_dir),
    }


@router.post("")
def create_export(session_id: str, req: ExportRequest):
    session = get_session(session_id)
    indices = require_indices(session, req.indices)
    export_name = _sanitize_name(req.config.output_name)

    job = job_manager.submit(
        "export", lambda ctx: run_export(session, req.config, indices, export_name, ctx),
        lock=session.lock)
    return {"job_id": job.id, "export_name": export_name}


@router.get("/list")
def list_exports(session_id: str):
    session = get_session(session_id)
    return session.storage.list_exports()


@router.get("/{name}/download")
def download_export(session_id: str, name: str):
    """下载导出结果（打包为 zip）。"""
    session = get_session(session_id)
    export_dir = _safe_child(session.storage.exports_dir, name)
    if not export_dir.is_dir():
        raise HTTPException(status_code=404, detail="导出不存在")

    files = [f for f in sorted(export_dir.iterdir()) if f.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="导出为空")

    if len(files) == 1:
        return FileResponse(str(files[0]), filename=files[0].name)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        # 用解析后的目录名，避免请求参数直接进响应头
        headers={"Content-Disposition": f'attachment; filename="{export_dir.name}.zip"'},
    )


@router.get("/{name}/files/{filename}")
def download_export_file(session_id: str, name: str, filename: str):
    session = get_session(session_id)
    path = _safe_child(session.storage.exports_dir, name, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(path), filename=path.name)
