"""Spine 资源导出 API：序列帧 → Spine JSON 骨架 + atlas 图集。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.api.deps import get_session
from app.config import get_settings
from app.core.spine_export import (DEFAULT_ANIM_MAP, build_skeleton, fit_canvas,
                                   frame_name, pack_atlas)
from app.services.job_manager import job_manager
from app.services.sprite_store import SpriteStoreError, sprite_store
from app.services.template_store import template_store

router = APIRouter(prefix="/sprites/{sprite_id}/spine", tags=["spine"])

_BAD_CHARS = set('\\/:*?"<>|')


class SpineExportRequest(BaseModel):
    name: Optional[str] = Field(default=None, description="骨架/图集名，缺省用精灵名")
    action_ids: Optional[List[str]] = Field(default=None, description="导出的动作（缺省全部）")
    fps: float = Field(12, gt=0, le=60, description="未记录抽帧帧率时的默认值")
    # 尺寸只由画布决定：帧等比压进画布，结果与源帧分辨率无关。
    # 不再提供额外缩放系数——它会和画布的兜底缩放叠乘，角色越缩越小。
    canvas: Optional[int] = Field(128, ge=16, le=4096, description="统一方形画布边长")
    outline: Optional[dict] = Field(
        default=None,
        description="描边（压进画布之后执行，宽度即成品实际像素宽）；缺省用精灵预设")
    frame_pattern: str = Field("{anim}_{i:04d}", description="帧命名格式")
    start_index: int = Field(1, ge=0, description="帧号起始值（既有工程从 1 开始）")
    loop: bool = Field(True, description="动画末尾回到首帧（循环衔接）")
    atlas: bool = Field(True, description="同时打包 .atlas + .png")
    use_processed: bool = Field(True, description="优先用抠图/描边后的帧")


def anim_name_of(action: dict) -> str:
    """动作 → Spine 动画名：模板设定 > 变体名推断 > 动作名。"""
    tpl = template_store.get(action.get("template_id") or "") or {}
    if (tpl.get("spine_anim") or "").strip():
        return tpl["spine_anim"].strip()
    variant = (tpl.get("variant") or "").strip()
    name = (action.get("name") or "").strip()
    return (DEFAULT_ANIM_MAP.get(variant) or DEFAULT_ANIM_MAP.get(name)
            or name or "anim")


def _export_dir(sprite_id: str, name: str) -> Path:
    safe = "".join(c for c in name if c not in _BAD_CHARS).strip() or sprite_id
    return get_settings().resolved_data_dir / "spine_exports" / sprite_id / safe


def _action_fps(session, fallback: float) -> float:
    """取该动作最近一次抽帧用的 fps，没有记录则用请求默认值。"""
    try:
        from app.services import recipe
        steps = [s for s in recipe.read(session).get("steps", [])
                 if s.get("op") == "extract"]
        if steps:
            v = float(steps[-1].get("params", {}).get("fps") or 0)
            if v > 0:
                return v
    except Exception:
        pass
    return fallback


@router.get("/preview")
def preview_spine(sprite_id: str):
    """导出前预览：每个动作将用的动画名、帧数、抠图进度。"""
    try:
        sprite = sprite_store.get_sprite(sprite_id)
    except SpriteStoreError as e:
        raise HTTPException(status_code=404, detail=str(e))
    items, used = [], {}
    for action in sprite_store.list_actions(sprite_id):
        anim = anim_name_of(action)
        used[anim] = used.get(anim, 0) + 1
        summary = action.get("summary") or {}
        items.append({
            "action_id": action["id"], "name": action.get("name"), "anim": anim,
            "frames": int(summary.get("frame_count") or 0),
            "processed": int(summary.get("processed_count") or 0),
            "status": action.get("status"),
        })
    for it in items:
        it["duplicate"] = used[it["anim"]] > 1
    return {"sprite": sprite.get("name"), "items": items,
            "conflicts": sorted(k for k, v in used.items() if v > 1)}


def run_spine_export(sprite_id: str, name: str, action_ids: List[str],
                     req: SpineExportRequest, ctx) -> dict:
    """导出任务体（端点与自动流水线收口共用）。"""
    out = _export_dir(sprite_id, name)
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    (out / "images").mkdir(parents=True, exist_ok=True)

    # 描边参数：请求里给了就用，否则取精灵预设；两边都没有就不描
    from app.models.export_config import ExportOutlineConfig
    ocfg = req.outline
    if ocfg is None:
        ocfg = ((sprite_store.get_sprite(sprite_id).get("preset") or {})
                .get("outline") or {})
    outline_cfg = ExportOutlineConfig(**{k: v for k, v in ocfg.items()
                                         if k in ExportOutlineConfig.model_fields})

    anims, atlas_items, skipped = [], [], []
    total = len(action_ids)
    for n, aid in enumerate(action_ids):
        if ctx.cancelled():
            break
        try:
            action = sprite_store.get_action(sprite_id, aid)
            session = get_session(aid)
        except Exception as e:
            skipped.append({"action_id": aid, "reason": "无法打开: %s" % e})
            continue
        anim = anim_name_of(action)
        ctx.report(n / total * 80, "[%d/%d] %s" % (n + 1, total, anim))

        frames = session.frame_manager.frames
        if not frames:
            skipped.append({"action_id": aid, "name": action.get("name"),
                            "reason": "无帧"})
            continue
        imgs = []
        for fr in frames:
            if req.use_processed:
                img = session.load_display_array(fr.index)
            else:
                img = session.frame_store.load_raw(fr.id)
            if img is None:
                continue
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGBA)
            elif img.shape[2] == 3:
                img = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
            imgs.append(fit_canvas(img, req.canvas, 1.0))
        session.clear_frame_arrays()

        # 描边在压进画布之后：填几 px，成品里就是几 px
        if outline_cfg.enabled and outline_cfg.width > 0 and imgs:
            from app.api.export_api import apply_size_ops
            imgs = apply_size_ops(imgs, None, outline_cfg)

        names, size = [], None
        for img in imgs:
            fname = frame_name(anim, req.start_index + len(names), req.frame_pattern)
            ok, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGBA2BGRA))
            if not ok:
                continue
            (out / "images" / (fname + ".png")).write_bytes(buf.tobytes())
            names.append(fname)
            size = (img.shape[1], img.shape[0])
            if req.atlas:
                atlas_items.append((fname, img))
        if not names:
            skipped.append({"action_id": aid, "name": action.get("name"),
                            "reason": "帧图像不可读"})
            continue
        anims.append({"name": anim, "frames": names,
                      "fps": _action_fps(session, req.fps), "loop": req.loop,
                      "width": size[0], "height": size[1]})

    if not anims:
        raise RuntimeError("没有导出任何动画：" +
                           json.dumps(skipped, ensure_ascii=False))

    ctx.report(85, "生成骨架 JSON...")
    (out / (name + ".json")).write_text(
        json.dumps(build_skeleton(anims), ensure_ascii=False, indent=2),
        encoding="utf-8")

    atlas_info = None
    if req.atlas and atlas_items:
        ctx.report(90, "打包图集（%d 帧）..." % len(atlas_items))
        text, png = pack_atlas(atlas_items, name)
        (out / (name + ".atlas")).write_text(text, encoding="utf-8")
        (out / (name + ".png")).write_bytes(png)
        atlas_info = {"regions": len(atlas_items),
                      "size": text.splitlines()[2].split(":")[1].strip(),
                      "bytes": len(png)}

    frames_total = sum(len(a["frames"]) for a in anims)
    ctx.report(100, "导出完成：%d 个动画 / %d 帧" % (len(anims), frames_total))
    return {
        "name": name, "dir": str(out), "frames": frames_total,
        "animations": [{"name": a["name"], "frames": len(a["frames"]),
                        "fps": a["fps"], "size": [a["width"], a["height"]]}
                       for a in anims],
        "atlas": atlas_info, "skipped": skipped,
        "canvas": req.canvas,
        "outline": ({"width": outline_cfg.width, "color": list(outline_cfg.color)}
                    if outline_cfg.enabled and outline_cfg.width > 0 else None),
        "files": sorted(p.name for p in out.iterdir() if p.is_file()),
    }


@router.post("/export")
def export_spine(sprite_id: str, req: SpineExportRequest):
    """导出 Spine 资源包（后台任务）：images/ + .json + .atlas + .png。"""
    try:
        sprite = sprite_store.get_sprite(sprite_id)
    except SpriteStoreError as e:
        raise HTTPException(status_code=404, detail=str(e))

    name = (req.name or sprite.get("name") or sprite_id).strip()
    ids = [r["id"] for r in sprite.get("actions", [])]
    if req.action_ids:
        want = set(req.action_ids)
        ids = [i for i in ids if i in want]
    if not ids:
        raise HTTPException(status_code=400, detail="没有可导出的动作")

    job = job_manager.submit(
        "spine_export",
        lambda ctx: run_spine_export(sprite_id, name, ids, req, ctx), pool="cpu")
    return {"job_id": job.id, "name": name}


@router.get("/download")
def download_spine(sprite_id: str, name: str):
    """把导出目录打包成 zip 下载。"""
    out = _export_dir(sprite_id, name)
    if not out.is_dir():
        raise HTTPException(status_code=404, detail="导出结果不存在，请先执行导出")
    tmp_dir = get_settings().resolved_data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, zpath = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
    os.close(fd)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out).as_posix())
    stamp = time.strftime("%Y%m%d_%H%M")
    return FileResponse(
        zpath, media_type="application/zip",
        filename="%s_spine_%s.zip" % (name, stamp),
        background=BackgroundTask(lambda: Path(zpath).unlink(missing_ok=True)))
