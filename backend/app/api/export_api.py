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
