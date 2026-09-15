"""图像处理 API：缩放、空白裁剪、边缘优化、RealESRGAN 增强、魔棒编辑。"""
from __future__ import annotations

from typing import Optional, Tuple

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException, Response
from PIL import Image

from app.api.deps import get_session, require_indices
from app.api.schemas import (
    CropRequest, EnhanceRequest, ImageOutlineRequest, OptimizeEdgesRequest,
    OutlineTestRequest, ScaleRequest, WandApplyRequest, WandSelectRequest,
)
from app.core.magic_wand import MagicWand
from app.core.realesrgan_processor import RealESRGANProcessor
from app.services.job_manager import job_manager
from app.utils.image_utils import encode_preview

router = APIRouter(prefix="/sessions/{session_id}/image", tags=["image-ops"])

_ALGO_MAP = {
    "nearest": Image.Resampling.NEAREST,
    "box": Image.Resampling.BOX,
    "bilinear": Image.Resampling.BILINEAR,
    "hamming": Image.Resampling.HAMMING,
    "bicubic": Image.Resampling.BICUBIC,
    "lanczos": Image.Resampling.LANCZOS,
}


def _push_history(session, indices, name: str, desc: str):
    session.history.push_snapshot(name, desc, indices, session.frame_manager)


# ---------- 描边 ----------
@router.post("/outline/test")
def test_outline(session_id: str, req: OutlineTestRequest):
    """单帧预览（不落盘）：返回描边后的 PNG。"""
    from app.core.outline import add_outline, pad_rgba

    session = get_session(session_id)
    img = session.load_display_array(req.frame_index)
    session.clear_frame_arrays()
    if img is None:
        raise HTTPException(status_code=404, detail="帧图像不存在")
    if img.ndim != 3 or img.shape[2] != 4:
        raise HTTPException(status_code=400, detail="该帧不是 RGBA——请先完成抠图")

    p = req.params
    src = img
    if p.position in ("outer", "center"):
        src = pad_rgba(src, int(p.width) + 1)     # 预览统一留白，避免贴边被裁
    out = add_outline(src, p.width, p.color, p.opacity, p.position,
                      p.corner, p.antialias, p.alpha_threshold)
    return Response(content=encode_preview(out, transparent_checker=True),
                    media_type="image/png")


def run_outline(session, indices: list, p, auto_pad: bool, ctx) -> dict:
    """描边任务体（端点与自动流水线共用）。调用方需持有会话锁。"""
    from app.core.outline import add_outline, pad_rgba, required_pad

    ctx.report(0, "开始描边...")
    arrays = session.load_display_arrays(indices)
    rgba_idx = [(i, a) for i, a in zip(indices, arrays)
                if a is not None and a.ndim == 3 and a.shape[2] == 4]
    if not rgba_idx:
        session.clear_frame_arrays()
        return {"processed": 0, "total": len(indices),
                "error": "没有可描边的帧——描边要求 RGBA，请先完成抠图"}

    # 外描边可能超出画布：所有帧扩同一个量，保持帧间尺寸与基线一致
    pad = 0
    if auto_pad and p.position in ("outer", "center"):
        pad = required_pad([a[:, :, 3] for _, a in rgba_idx], p.width,
                           p.alpha_threshold)

    _push_history(session, indices, "描边",
                  f"{p.position} {p.width}px RGB{tuple(p.color)}"
                  + (f" | 扩边 {pad}px" if pad else "")
                  + f" | {len(rgba_idx)}帧")

    processed = 0
    for i, (idx, img) in enumerate(rgba_idx):
        if ctx.cancelled():
            break
        src = pad_rgba(img, pad) if pad else img
        out = add_outline(src, p.width, p.color, p.opacity, p.position,
                          p.corner, p.antialias, p.alpha_threshold)
        session.save_processed(idx, out)
        processed += 1
        ctx.report((i + 1) / len(rgba_idx) * 100, f"描边 {i+1}/{len(rgba_idx)}")

    session.clear_frame_arrays()
    session.persist_metadata()
    from app.services import recipe
    recipe.record_step(session, "outline",
                       {"width": p.width, "color": list(p.color),
                        "opacity": p.opacity, "position": p.position,
                        "corner": p.corner, "antialias": p.antialias,
                        "pad": pad},
                       {"processed": processed})
    ctx.report(100, f"描边完成: {processed}/{len(rgba_idx)} 帧"
                    + (f"（已扩边 {pad}px）" if pad else ""))
    return {"processed": processed, "total": len(indices), "pad": pad,
            "skipped": len(indices) - len(rgba_idx)}


@router.post("/outline")
def outline_frames(session_id: str, req: ImageOutlineRequest):
    """给已抠图的帧加纯色描边。抠图之后、导出之前执行。"""
    session = get_session(session_id)
    indices = require_indices(session, req.indices)
    job = job_manager.submit(
        "outline",
        lambda ctx: run_outline(session, indices, req.params, req.auto_pad, ctx),
        lock=session.lock)
    return {"job_id": job.id}


# ---------- 缩放 ----------
def run_scale(session, indices: list, mode: str, percent: float,
              width: int, height: int, algorithm: str, ctx) -> dict:
    """缩放任务体（端点与自动流水线共用）。调用方需持有会话锁。"""
    algo = _ALGO_MAP.get(algorithm, Image.Resampling.LANCZOS)
    ctx.report(0, "开始缩放...")
    arrays = session.load_display_arrays(indices)
    first = next((a for a in arrays if a is not None), None)
    if first is None:
        return {"processed": 0, "message": "没有可用的帧图像"}

    orig_h, orig_w = first.shape[:2]
    if mode == "percent":
        k = percent / 100.0
        target_w, target_h = int(orig_w * k), int(orig_h * k)
    else:
        target_w, target_h = width, height

    if target_w < 1 or target_h < 1:
        return {"processed": 0, "message": "目标尺寸非法"}
    if (target_w, target_h) == (orig_w, orig_h):
        session.clear_frame_arrays()
        return {"processed": 0, "total": len(indices), "message": "尺寸未变，跳过",
                "from": f"{orig_w}x{orig_h}", "to": f"{target_w}x{target_h}"}

    _push_history(session, indices, "批量缩放",
                  f"{len(indices)}帧 {orig_w}x{orig_h}→{target_w}x{target_h} {algorithm}")

    processed = 0
    for i, (idx, img) in enumerate(zip(indices, arrays)):
        if ctx.cancelled():
            break
        if img is None:
            continue
        pil = Image.fromarray(img)
        scaled = pil.resize((target_w, target_h), algo)
        session.save_processed(idx, np.array(scaled))
        processed += 1
        ctx.report((i + 1) / len(indices) * 100, f"缩放 {i+1}/{len(indices)}")

    session.clear_frame_arrays()
    session.persist_metadata()
    from app.services import recipe
    recipe.record_step(session, "scale",
                       {"mode": mode, "percent": percent,
                        "width": width, "height": height, "algorithm": algorithm},
                       {"processed": processed, "to": f"{target_w}x{target_h}"})
    ctx.report(100, f"缩放完成: {processed}/{len(indices)} 帧 → {target_w}x{target_h}")
    return {"processed": processed, "total": len(indices),
            "from": f"{orig_w}x{orig_h}", "to": f"{target_w}x{target_h}"}


@router.post("/scale")
def scale_frames(session_id: str, req: ScaleRequest):
    session = get_session(session_id)
    indices = require_indices(session, req.indices)
    job = job_manager.submit(
        "scale",
        lambda ctx: run_scale(session, indices, req.mode, req.percent,
                              req.width, req.height, req.algorithm, ctx),
        lock=session.lock)
    return {"job_id": job.id}


# ---------- 空白裁剪 ----------
@router.post("/crop-whitespace")
def crop_whitespace(session_id: str, req: CropRequest):
    session = get_session(session_id)
    indices = require_indices(session, req.indices)

    def _job(ctx):
        ctx.report(0, "计算统一裁剪区域...")
        arrays = session.load_display_arrays(indices)

        min_x, min_y = float('inf'), float('inf')
        max_x, max_y = 0, 0
        valid_arrays = []
        for idx, img in zip(indices, arrays):
            if img is None:
                continue
            valid_arrays.append((idx, img))
            if len(img.shape) == 3 and img.shape[2] == 4:
                alpha = img[:, :, 3]
                rows = np.any(alpha > 0, axis=1)
                cols = np.any(alpha > 0, axis=0)
            else:
                gray = np.mean(img, axis=2) if len(img.shape) == 3 else img
                rows = np.any(gray > 10, axis=1)
                cols = np.any(gray > 10, axis=0)
            if np.any(rows) and np.any(cols):
                y_idx = np.where(rows)[0]
                x_idx = np.where(cols)[0]
                min_x = min(min_x, int(x_idx[0]))
                max_x = max(max_x, int(x_idx[-1]))
                min_y = min(min_y, int(y_idx[0]))
                max_y = max(max_y, int(y_idx[-1]))

        if min_x == float('inf'):
            return {"processed": 0, "message": "未找到有效内容区域"}

        h, w = valid_arrays[0][1].shape[:2]
        min_x = max(0, min_x - req.margin_left)
        max_x = min(w - 1, max_x + req.margin_right)
        min_y = max(0, min_y - req.margin_top)
        max_y = min(h - 1, max_y + req.margin_bottom)

        crop_w = max_x - min_x + 1
        crop_h = max_y - min_y + 1

        _push_history(session, indices, "空白裁剪",
                      f"裁剪 {w}x{h}→{crop_w}x{crop_h} 边距({req.margin_top},{req.margin_bottom},{req.margin_left},{req.margin_right}) | {len(indices)}帧")

        processed = 0
        for i, (idx, img) in enumerate(valid_arrays):
            if ctx.cancelled():
                break
            cropped = img[min_y:max_y + 1, min_x:max_x + 1].copy()
            session.save_processed(idx, cropped)
            processed += 1
            ctx.report((i + 1) / len(valid_arrays) * 100, f"裁剪 {i+1}/{len(valid_arrays)}")

        session.clear_frame_arrays()
        session.persist_metadata()
        from app.services import recipe
        recipe.record_step(session, "crop_whitespace",
                           {"margins": [req.margin_top, req.margin_bottom,
                                        req.margin_left, req.margin_right]},
                           {"processed": processed, "size": f"{crop_w}x{crop_h}"})
        return {"processed": processed, "total": len(indices), "size": f"{crop_w}x{crop_h}"}

    job = job_manager.submit("crop", _job, lock=session.lock)
    return {"job_id": job.id}


# ---------- 边缘优化 ----------
@router.post("/optimize-edges")
def optimize_edges(session_id: str, req: OptimizeEdgesRequest):
    session = get_session(session_id)
    indices = require_indices(session, req.indices)

    def _job(ctx):
        ctx.report(0, "开始边缘优化...")
        arrays = session.load_display_arrays(indices)

        _push_history(session, indices, "边缘优化",
                      f"边缘收缩 {req.erode}px | {len(indices)}帧")

        kernel_size = req.erode * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))

        processed = 0
        for i, (idx, img) in enumerate(zip(indices, arrays)):
            if ctx.cancelled():
                break
            if img is None or len(img.shape) != 3 or img.shape[2] != 4:
                continue
            alpha = img[:, :, 3]
            eroded = cv2.erode(alpha, kernel, iterations=1)
            optimized = img.copy()
            optimized[:, :, 3] = eroded
            session.save_processed(idx, optimized)
            processed += 1
            ctx.report((i + 1) / len(indices) * 100, f"边缘优化 {i+1}/{len(indices)}")

        session.clear_frame_arrays()
        session.persist_metadata()
        from app.services import recipe
        recipe.record_step(session, "optimize_edges", {"erode": req.erode},
                           {"processed": processed})
        return {"processed": processed, "total": len(indices)}

    job = job_manager.submit("optimize-edges", _job, lock=session.lock)
    return {"job_id": job.id}


# ---------- RealESRGAN 增强 ----------
@router.post("/enhance")
def enhance_frames(session_id: str, req: EnhanceRequest):
    session = get_session(session_id)
    indices = require_indices(session, req.indices)
    processor = RealESRGANProcessor()

    if not processor.is_available():
        raise HTTPException(status_code=400, detail="Real-ESRGAN 不可用（缺少可执行文件或模型）")

    def _job(ctx):
        ctx.report(0, "开始增强...")
        arrays = session.load_display_arrays(indices)

        _push_history(session, indices, "图像增强",
                      f"RealESRGAN {req.model} | {len(indices)}帧")

        processed = 0
        for i, (idx, img) in enumerate(zip(indices, arrays)):
            if ctx.cancelled():
                break
            if img is None:
                continue
            result = processor.process_image(img, model_name=req.model, tile=req.tile)
            if result is not None:
                session.save_processed(idx, result)
                processed += 1
            ctx.report((i + 1) / len(indices) * 100, f"增强 {i+1}/{len(indices)}")

        session.clear_frame_arrays()
        session.persist_metadata()
        from app.services import recipe
        recipe.record_step(session, "enhance",
                           {"model": req.model, "tile": req.tile},
                           {"processed": processed})
        return {"processed": processed, "total": len(indices), "model": req.model}

    job = job_manager.submit("enhance", _job, lock=session.lock)
    return {"job_id": job.id}


# ---------- 魔棒选区 ----------
@router.post("/wand/select")
def wand_select(session_id: str, req: WandSelectRequest):
    """魔棒选区：存储 mask 并返回选区信息（mask 图由 /wand/mask 提供）。"""
    session = get_session(session_id)
    img = session.load_display_array(req.frame_index)
    if img is None:
        raise HTTPException(status_code=404, detail="帧图像不存在")

    try:
        selection = MagicWand().select(
            img,
            req.x, req.y,
            tolerance=req.tolerance,
            contiguous=req.contiguous,
            anti_alias=req.anti_alias,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 存储 mask（会话级，每帧一个，超出上限自动淘汰最早的）
    session.set_wand_mask(req.frame_index, selection.mask)

    session.clear_frame_arrays()

    return {
        "frame_index": req.frame_index,
        "area": selection.area,
        "bounds": {
            "x": selection.x, "y": selection.y,
            "width": selection.width, "height": selection.height,
        },
    }


@router.get("/wand/mask")
def wand_mask(session_id: str, frame_index: int):
    """返回已存储的选区 mask 图（PNG，蓝色高亮）。"""
    session = get_session(session_id)
    mask = session.wand_masks.get(frame_index)
    if mask is None:
        raise HTTPException(status_code=404, detail="请先对该帧执行选区操作")

    mask_img = (mask * 255).astype(np.uint8) if mask.max() <= 1 else mask
    rgba = np.zeros((*mask_img.shape, 4), dtype=np.uint8)
    rgba[:, :, 2] = mask_img  # 蓝色通道显示选区
    rgba[:, :, 3] = mask_img

    # 保留透明通道（选区外透明），前端叠加到画布上时不遮挡原图
    bytes_data = encode_preview(rgba, transparent_checker=False)
    return Response(content=bytes_data, media_type="image/png")


@router.post("/wand/apply")
def wand_apply(session_id: str, req: WandApplyRequest):
    """应用魔棒选区：delete 清除选区，fill 用颜色填充。

    同步端点但会改写帧数据，必须与后台任务互斥。用带超时的锁获取，
    避免在长任务（批量抠图等）期间把请求线程挂死。
    """
    session = get_session(session_id)
    mask = session.wand_masks.get(req.frame_index)
    if mask is None:
        raise HTTPException(status_code=400, detail="请先对该帧执行选区操作")

    if not session.lock.acquire(timeout=5):
        raise HTTPException(status_code=409,
                            detail="该动作有任务正在进行，请等它完成后再应用魔棒")
    try:
        img = session.load_display_array(req.frame_index)
        if img is None:
            raise HTTPException(status_code=404, detail="帧图像不存在")

        _push_history(session, [req.frame_index], "魔棒编辑",
                      f"{'删除选区' if req.operation == 'delete' else '填充选区'} | 帧 {req.frame_index}")

        from app.core.magic_wand import Selection
        wand = MagicWand()
        wand._selection = Selection(
            mask=mask,
            bounds=(0, 0, img.shape[1], img.shape[0]),
            area=int(np.count_nonzero(mask > 0)),
            seed_point=(0, 0),
            tolerance=0,
            contiguous=True,
        )

        result = wand.apply_to_image(
            img,
            operation=req.operation,
            fill_color=tuple(req.fill_color) if req.fill_color else None,
        )
        session.save_processed(req.frame_index, result)
        session.clear_frame_arrays()
        session.persist_metadata()

        from app.services import recipe
        recipe.record_step(session, "wand_apply",
                           {"frame_index": req.frame_index, "operation": req.operation})
    finally:
        session.lock.release()

    return {"frame_index": req.frame_index, "operation": req.operation}
