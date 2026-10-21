"""导出 API：精灵图/GIF/帧/WebP/Godot 导出任务与结果下载。"""
from __future__ import annotations

import io
import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

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


# 默认就叫动作名：一个角色下每个动作各自一个名字，导出后放一起也分得清
DEFAULT_NAME_PATTERN = "{action}"


def _sprite_action_names(session_id: str) -> tuple:
    """会话 → (角色名, 动作名, Spine 动画名, 该动作存的导出别名)。

    旧会话没建过档就都是空的。
    """
    from app.services.sprite_store import sprite_store
    try:
        sprite_id = sprite_store.sprite_of_action(session_id)
        if sprite_id:
            action = sprite_store.get_action(sprite_id, session_id)
            from app.api.spine_api import anim_name_of
            return ((sprite_store.get_sprite(sprite_id).get("name") or "").strip(),
                    (action.get("name") or "").strip(),
                    anim_name_of(action),
                    (action.get("export_alias") or "").strip())
    except Exception:
        pass
    return "", "", "", ""


def _resolve_name(session_id: str, raw: str) -> str:
    """定下导出名：支持 {sprite} {action} {anim} 占位，留空即动作名。

    优先级：这次填的 > 该动作存的导出别名 > 默认（动作名）。
    别名挂在动作上：动作名是固定的，导出时对外叫什么可以随时改，改了不动数据。
    """
    sprite_name, action_name, anim, alias = _sprite_action_names(session_id)
    text = (raw or "").strip() or alias or DEFAULT_NAME_PATTERN
    name = (text.replace("{sprite}", sprite_name)
                .replace("{action}", action_name)
                .replace("{anim}", anim)).strip(" _")
    # 旧会话没建过档，{action} 展开成空，退到角色名、再退到通用名
    return _sanitize_name(name or sprite_name or "sprite")


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


def _publish_export(staged: Path, destination: Path) -> None:
    """发布整份新产物；替换失败时恢复旧目录，回滚失败也保留备份供恢复。"""
    backup = destination.parent.parent / ('.export-previous-' + uuid4().hex)
    had_previous = destination.exists()
    if had_previous:
        destination.replace(backup)
    try:
        staged.replace(destination)
    except BaseException:
        if had_previous:
            backup.replace(destination)
        raise
    if had_previous:
        try:
            shutil.rmtree(backup)
        except OSError:
            logging.getLogger(__name__).warning('旧导出备份清理失败，保留于 %s', backup)


def run_export(session, config, indices: list, export_name: str, ctx) -> dict:
    """导出任务体（端点与自动流水线共用）。调用方需持有会话锁。"""
    from app.services.resources import resources, estimate_export_mb
    resources.check_estimate(session.id, estimate_export_mb(session, indices, config))
    export_dir = _safe_child(session.storage.exports_dir, export_name)
    try:
        frames = []
        session.load_display_arrays(indices)
        for idx in indices:
            f = session.frame_manager.get_frame(idx)
            if f is not None:
                frames.append(f)

        if not frames:
            raise ValueError('没有可导出的帧，请重新选择帧后导出')

        cfg = config.model_copy()
        # 文件名跟目录名用同一个已清洗的值：既跟着「角色_动作」走，
        # 也不让 ../ 之类的输入顺着 f"{output_name}.png" 跑出导出目录
        cfg.output_name = export_name

        # 循环过渡：非破坏性应用到导出帧（使首尾无缝衔接）
        lt = cfg.loop_transition
        if lt.enabled and len(frames) > 1:
            frames = apply_transition_to_frame_data(frames, lt.count, lt.mode)

        # 缩放 / 描边：同样只作用于导出副本，帧文件保持原样
        frames, size_notes = _apply_to_frames(frames, cfg, ctx)

        ctx.report(30, "开始导出...")
        # 暂存目录不放在 exports/，列表和下载始终只看到已发布的完整版本。
        with tempfile.TemporaryDirectory(prefix='.export-work-', dir=session.storage.root) as work:
            staged = Path(work) / export_name
            staged.mkdir()
            cfg.output_path = staged
            if ctx.cancelled():
                raise RuntimeError('已取消')
            main_path, info = Exporter().export(frames, cfg)
            files = [f.name for f in sorted(staged.iterdir())
                     if f.is_file() and not f.name.startswith('.')]
            if not files:
                raise ValueError('导出没有生成文件，请检查帧图像是否完整')
            main_name = Path(main_path).name
            if isinstance(info, str):
                info = info.replace(str(staged), str(export_dir))
            if ctx.cancelled():
                raise RuntimeError('已取消')
            _publish_export(staged, export_dir)
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
            "main": main_name,
            "info": info,
            "files": files,
            "dir": str(export_dir),
        }
    finally:
        session.clear_frame_arrays()


@router.post("")
def create_export(session_id: str, req: ExportRequest):
    session = get_session(session_id)
    indices = require_indices(session, req.indices)
    export_name = _resolve_name(session_id, req.config.output_name)
    from app.services.resources import estimate_export_mb

    job = job_manager.submit(
        "export", lambda ctx: run_export(session, req.config, indices, export_name, ctx),
        lock=session.lock, target=session.id, resource='export',
        params={"config": req.config.model_dump(mode='json'), "indices": indices},
        memory_mb=estimate_export_mb(session, indices, req.config))
    return {"job_id": job.id, "export_name": export_name}


@router.get("/name-parts")
def export_name_parts(session_id: str):
    """文件名占位符各自会展开成什么——界面上照着显示「这次会导成什么名字」。"""
    sprite_name, action_name, anim, alias = _sprite_action_names(session_id)
    return {"sprite": sprite_name, "action": action_name, "anim": anim,
            "alias": alias, "default": DEFAULT_NAME_PATTERN}


@router.get("/list")
def list_exports(session_id: str):
    session = get_session(session_id)
    return session.storage.list_exports()


def _attachment(filename: str) -> dict:
    """下载响应头。

    中文名直接塞进 Content-Disposition 会炸——响应头只吃 latin-1，
    非 ASCII 的按 RFC 5987 走 filename*（FileResponse 内部也是这么干的）。
    """
    quoted = quote(filename)
    if quoted == filename:
        return {"Content-Disposition": 'attachment; filename="%s"' % filename}
    return {"Content-Disposition": "attachment; filename*=utf-8''%s" % quoted}


def _download_names(session_id: str, export_name: str) -> tuple:
    """下载时的 (单文件前缀, zip 文件名, zip 内目录)——都带上角色名。

    盘上的产物本来就按角色/动作分开存，但下载下来会挤进同一个下载目录：
    动作名在不同角色之间是会重复的（谁都有「走路」），不分开就会互相盖。
    """
    raw = _sprite_action_names(session_id)[0]
    sprite_name = _sanitize_name(raw) if raw else ""
    if not sprite_name:
        return "", export_name + ".zip", export_name
    return (sprite_name + "_",
            "%s_%s.zip" % (sprite_name, export_name),
            "%s/%s" % (sprite_name, export_name))


@router.get("/{name}/download")
def download_export(session_id: str, name: str):
    """下载导出结果（打包为 zip）。"""
    session = get_session(session_id)
    export_dir = _safe_child(session.storage.exports_dir, name)
    if not export_dir.is_dir():
        raise HTTPException(status_code=404, detail="导出不存在")
    if (export_dir / '.incomplete').exists():
        raise HTTPException(status_code=409, detail='该导出尚未完成或已中断，请完成后再下载')

    files = [f for f in sorted(export_dir.iterdir()) if f.is_file()]
    if not files:
        raise HTTPException(status_code=404, detail="导出为空")

    # 用解析后的目录名，避免请求参数直接进响应头
    prefix, zip_name, arc_root = _download_names(session_id, export_dir.name)

    if len(files) == 1:
        # 单个文件不套目录，改在文件名上分开（GIF 这类直接拿走的场景不想再解一层 zip）
        return FileResponse(str(files[0]), filename=prefix + files[0].name)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            # 解到哪都自带「角色/动作」两层，几个角色的产物不会混进同一个文件夹
            zf.write(f, arcname="%s/%s" % (arc_root, f.name))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers=_attachment(zip_name))


@router.get("/{name}/files/{filename}")
def download_export_file(session_id: str, name: str, filename: str):
    session = get_session(session_id)
    path = _safe_child(session.storage.exports_dir, name, filename)
    if (path.parent / '.incomplete').exists():
        raise HTTPException(status_code=409, detail='该导出尚未完成或已中断，请完成后再下载')
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(str(path), filename=path.name)
