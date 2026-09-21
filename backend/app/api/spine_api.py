"""Spine 资源导出 API：序列帧 → Spine JSON 骨架 + atlas 图集。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.api.deps import get_session
from app.config import get_settings
from app.core.spine_export import (DEFAULT_ANIM_MAP, SPINE_VERSION, build_skeleton,
                                   fit_canvas, frame_name, pack_atlas)
from app.services.job_manager import job_manager
from app.services.sprite_store import SpriteStoreError, sprite_store
from app.services.template_store import template_store

router = APIRouter(prefix="/sprites/{sprite_id}/spine", tags=["spine"])

_BAD_CHARS = set('\\/:*?"<>|')


class SpineExportRequest(BaseModel):
    name: Optional[str] = Field(default=None, description="骨架/图集名，缺省用精灵名")
    action_ids: Optional[List[str]] = Field(default=None, description="导出的动作（缺省全部）")
    # 参考工程反解出来的导出约定：版本/帧名格式/画布/渲染尺寸/逐动画对齐偏移。
    # 指定后，下面同名的字段以模板为准（请求里显式给的仍然优先）。
    template_id: Optional[str] = Field(default=None, description="Spine 导出模板")
    anim_names: Optional[Dict[str, str]] = Field(
        default=None,
        description="逐动作改动画名：action_id → 名字（缺省用推断值；空串＝取消自定义）")
    remember_names: bool = Field(
        True, description="把改过的名字记到动作上，下次导出与流水线自动导出都用它")
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


def clean_anim(name: str) -> str:
    """动画名会拿去当帧文件名和图集区域名，路径字符与多余空白都得去掉。"""
    # 制表/换行换成空格再折叠，别把前后两个词粘成一个
    s = "".join(" " if c < " " else c
                for c in (name or "") if c not in _BAD_CHARS)
    return " ".join(s.split())


def derived_anim(action: dict) -> str:
    """没在动作上改过名时的推断值：模板设定 > 变体名推断 > 动作名。"""
    tpl = template_store.get(action.get("template_id") or "") or {}
    if (tpl.get("spine_anim") or "").strip():
        return clean_anim(tpl["spine_anim"])
    variant = (tpl.get("variant") or "").strip()
    name = (action.get("name") or "").strip()
    return clean_anim(DEFAULT_ANIM_MAP.get(variant) or DEFAULT_ANIM_MAP.get(name)
                      or name) or "anim"


def anim_name_of(action: dict) -> str:
    """动作 → Spine 动画名：动作上单独取的名字最大，其次才是推断值。"""
    return clean_anim(action.get("spine_anim") or "") or derived_anim(action)


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
        custom = clean_anim(action.get("spine_anim") or "")
        suggest = derived_anim(action)
        anim = custom or suggest
        used[anim] = used.get(anim, 0) + 1
        summary = action.get("summary") or {}
        items.append({
            "action_id": action["id"], "name": action.get("name"), "anim": anim,
            "custom": custom, "suggest": suggest,
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

    # 导出模板：参考工程反解出来的约定；请求里显式给的字段仍然优先
    tpl = {}
    if req.template_id:
        from app.services.spine_template_store import spine_template_store
        tpl = spine_template_store.get(req.template_id) or {}
        if not tpl:
            raise RuntimeError("导出模板不存在: %s" % req.template_id)
    given = req.model_fields_set

    def pick(field, tpl_key=None, default=None):
        if field in given:
            return getattr(req, field)
        v = tpl.get(tpl_key or field)
        return v if v is not None else (getattr(req, field) if default is None
                                        else default)

    canvas = pick("canvas")
    frame_pattern = pick("frame_pattern")
    start_index = pick("start_index")
    version = tpl.get("spine_version") or SPINE_VERSION
    images_path = tpl.get("images_path") or "./images/"
    by_anim = {a["name"]: a for a in (tpl.get("animations") or [])}
    # 图集压缩比：贴图按此缩小，附件仍按渲染尺寸画（既有工程就是 320 画 / 128 贴）
    atlas_scale = float((tpl.get("atlas") or {}).get("scale") or 1.0)

    # 描边参数：请求里给了就用，否则取精灵预设；两边都没有就不描
    from app.models.export_config import ExportOutlineConfig
    ocfg = req.outline
    if ocfg is None:
        ocfg = ((sprite_store.get_sprite(sprite_id).get("preset") or {})
                .get("outline") or {})
    outline_cfg = ExportOutlineConfig(**{k: v for k, v in ocfg.items()
                                         if k in ExportOutlineConfig.model_fields})

    # 这次导出单独取的名字（界面上逐行改的）；没给的动作仍按动作/模板的设定
    overrides = {k: clean_anim(v) for k, v in (req.anim_names or {}).items()}

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
        anim = overrides.get(aid) or anim_name_of(action)
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
            imgs.append(fit_canvas(img, canvas, 1.0))
        session.clear_frame_arrays()

        # 描边在压进画布之后：填几 px，成品里就是几 px
        if outline_cfg.enabled and outline_cfg.width > 0 and imgs:
            from app.api.export_api import apply_size_ops
            imgs = apply_size_ops(imgs, None, outline_cfg)

        names, size = [], None
        for img in imgs:
            fname = frame_name(anim, start_index + len(names), frame_pattern)
            ok, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGBA2BGRA))
            if not ok:
                continue
            (out / "images" / (fname + ".png")).write_bytes(buf.tobytes())
            names.append(fname)
            size = (img.shape[1], img.shape[0])
            if req.atlas:
                atlas_items.append(
                    (fname, img if atlas_scale == 1.0 else fit_canvas(img, None, atlas_scale)))
        if not names:
            skipped.append({"action_id": aid, "name": action.get("name"),
                            "reason": "帧图像不可读"})
            continue
        # 模板里有这个动画就照抄它的约定：插槽名、对齐偏移、渲染尺寸、帧率
        t = by_anim.get(anim) or {}
        entry = {"name": anim, "frames": names,
                 "fps": t.get("fps") or _action_fps(session, req.fps),
                 "loop": t.get("loop", req.loop) if t else req.loop,
                 "width": size[0], "height": size[1]}
        if t:
            if t.get("slot"):
                entry["slot"] = t["slot"]
            if t.get("offset"):
                entry["bone_offset"] = t["offset"]
            if t.get("scale"):
                entry["bone_scale"] = t["scale"]
            if t.get("att_offset"):
                entry["att_offset"] = t["att_offset"]
            if t.get("render"):
                entry["render"] = t["render"]
        anims.append(entry)

    if not anims:
        raise RuntimeError("没有导出任何动画：" +
                           json.dumps(skipped, ensure_ascii=False))

    ctx.report(85, "生成骨架 JSON...")
    (out / (name + ".json")).write_text(
        json.dumps(build_skeleton(anims, images_path, version),
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    atlas_info = None
    if req.atlas and atlas_items:
        ctx.report(90, "打包图集（%d 帧）..." % len(atlas_items))
        text, png = pack_atlas(atlas_items, name)
        (out / (name + ".atlas")).write_text(text, encoding="utf-8")
        (out / (name + ".png")).write_bytes(png)
        atlas_info = {"regions": len(atlas_items),
                      "size": text.splitlines()[2].split(":")[1].strip(),
                      "bytes": len(png), "scale": atlas_scale}

    frames_total = sum(len(a["frames"]) for a in anims)
    ctx.report(100, "导出完成：%d 个动画 / %d 帧" % (len(anims), frames_total))
    return {
        "name": name, "dir": str(out), "frames": frames_total,
        "animations": [{"name": a["name"], "frames": len(a["frames"]),
                        "fps": a["fps"], "size": [a["width"], a["height"]]}
                       for a in anims],
        "atlas": atlas_info, "skipped": skipped,
        "canvas": canvas, "template": tpl.get("name"),
        "spine_version": version,
        "missing": sorted(set(by_anim) - {a["name"] for a in anims}) if tpl else [],
        "extra": sorted({a["name"] for a in anims} - set(by_anim)) if tpl else [],
        "outline": ({"width": outline_cfg.width, "color": list(outline_cfg.color)}
                    if outline_cfg.enabled and outline_cfg.width > 0 else None),
        "files": sorted(p.name for p in out.iterdir() if p.is_file()),
    }


def _resolve_names(sprite_id: str, ids: List[str],
                   req: SpineExportRequest) -> None:
    """定下这次导出每个动作用的动画名：清洗、查重，改过的按需记到动作上。

    同名的两个动作会写同一批帧文件、在骨架里互相覆盖，所以这里直接拦掉，
    不让它跑完了才发现少了一个动画。
    """
    given = {k: clean_anim(v) for k, v in (req.anim_names or {}).items()
             if k in set(ids)}
    for aid, nm in given.items():
        if not nm and (req.anim_names or {}).get(aid, "").strip():
            raise HTTPException(status_code=400,
                                detail="动画名里只剩下不能用的字符了：%s" %
                                       req.anim_names[aid])

    used, final = {}, {}
    for aid in ids:
        try:
            action = sprite_store.get_action(sprite_id, aid)
        except Exception:
            continue
        if aid in given:
            # 传了空串＝要取消自定义，这一趟就直接按推断值导
            nm = given[aid] or derived_anim(action)
        else:
            nm = anim_name_of(action)
        final[aid] = (action, nm)
        used.setdefault(nm, []).append(action.get("name") or aid)
    dup = {k: v for k, v in used.items() if len(v) > 1}
    if dup:
        detail = "；".join("%s ← %s" % (k, "、".join(v)) for k, v in dup.items())
        raise HTTPException(
            status_code=400,
            detail="动画名重复，同名会互相覆盖，请改掉其中一个：" + detail)

    # 定下来的名字回填进请求，任务体照着导（省得两处各算一遍还可能不一样）
    req.anim_names = {aid: nm for aid, (_, nm) in final.items()}

    if not req.remember_names:
        return
    for aid, nm in given.items():
        action = (final.get(aid) or (None, ""))[0]
        if action is None:
            continue
        # 改回推断值就把自定义清掉——让模板继续说了算，而不是把当前值冻在动作上
        keep = "" if not nm or nm == derived_anim(action) else nm
        if clean_anim(action.get("spine_anim") or "") != keep:
            sprite_store.update_action(sprite_id, aid, {"spine_anim": keep})


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

    _resolve_names(sprite_id, ids, req)

    job = job_manager.submit(
        "spine_export",
        lambda ctx: run_spine_export(sprite_id, name, ids, req, ctx), pool="cpu")
    return {"job_id": job.id, "name": name}


def _describe_export(d: Path) -> dict:
    """读一个导出目录的概况（骨架 JSON 里有什么就报什么）。"""
    files = [p for p in d.rglob("*") if p.is_file()]
    rec = {"name": d.name, "files": len(files),
           "bytes": sum(p.stat().st_size for p in files),
           "updated_at": max((p.stat().st_mtime for p in files), default=0),
           "images": sum(1 for p in files if p.parent.name == "images"),
           "has_atlas": (d / (d.name + ".atlas")).is_file()}
    skel = d / (d.name + ".json")
    if skel.is_file():
        try:
            data = json.loads(skel.read_text(encoding="utf-8"))
            anims = data.get("animations") or {}
            atts = data.get("skins", [{}])[0].get("attachments") or {}
            rec.update({
                "animations": len(anims),
                "animation_names": sorted(anims),
                "frames": sum(len(v) for v in atts.values()),
                "spine_version": (data.get("skeleton") or {}).get("spine"),
                "canvas": (data.get("skeleton") or {}).get("width") or None,
            })
        except (json.JSONDecodeError, OSError, IndexError):
            rec["error"] = "骨架 JSON 读不出来"
    else:
        rec["error"] = "缺少骨架 JSON"
    return rec


@router.get("/exports")
def list_spine_exports(sprite_id: str):
    """已有的 Spine 导出产物——手动导的和流水线收口导的都在这里。"""
    root = get_settings().resolved_data_dir / "spine_exports" / sprite_id
    if not root.is_dir():
        return {"exports": []}
    items = [_describe_export(d) for d in root.iterdir() if d.is_dir()]
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"exports": items}


@router.delete("/exports/{name}")
def delete_spine_export(sprite_id: str, name: str):
    """删掉一份导出产物（序列帧很占地方，导完拿走就可以清理）。"""
    out = _export_dir(sprite_id, name)
    root = (get_settings().resolved_data_dir / "spine_exports" / sprite_id).resolve()
    # 名字里带 .. 之类的东西时别让它跳出导出根目录
    if not out.is_dir() or root not in out.resolve().parents:
        raise HTTPException(status_code=404, detail="导出结果不存在")
    shutil.rmtree(out, ignore_errors=True)
    return {"deleted": name}


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
