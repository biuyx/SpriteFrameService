"""项目包导出/导入：把精灵（可按分类筛选）连同引用的动作模板打成 zip，
供另一台机器上的 SpriteFrameService 导入。

包结构：
    manifest.json              {format, version, project, sprites, templates}
    sprites/{sp_id}/...        精灵目录（精简模式只含档案+首帧+参考图库）
    templates/{file}           模板视频本体

导入策略（幂等、保引用）：
    模板按 (key, variant) 合并——已存在则复用其 id，动作绑定自动改写；
    精灵按 id 去重——同 id 已存在则跳过（视为同一份数据）。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.config import get_settings
from app.services.sprite_store import sprite_store
from app.services.template_store import template_store

router = APIRouter(prefix="/project", tags=["project"])

FORMAT = "sprite-project"
VERSION = 1

# 精简导出（不含工作数据）时，动作目录里保留的文件
_LITE_ACTION_FILES = {"action.json", "first_frame.png"}


class ExportRequest(BaseModel):
    project: Optional[str] = Field(default=None, description="按分类标签筛选；空=不筛")
    sprite_ids: Optional[List[str]] = Field(default=None, description="指定精灵；空=全部")
    include_workdata: bool = Field(
        default=False, description="是否包含帧/抠图/生成视频/导出等工作数据（体积大）")


def _pick_sprites(req: ExportRequest) -> List[dict]:
    sprites = sprite_store.list_sprites()
    if req.sprite_ids:
        wanted = set(req.sprite_ids)
        sprites = [s for s in sprites if s["id"] in wanted]
    if req.project:
        sprites = [s for s in sprites if req.project in (s.get("tags") or [])]
    return sprites


def _iter_sprite_files(sp_dir: Path, include_workdata: bool):
    """(绝对路径, 包内相对路径) 生成器。"""
    for p in sp_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(sp_dir)
        parts = rel.parts
        if not include_workdata:
            # 保留：sprite.json / reference/** / actions/{id}/(action.json|first_frame.png)
            if parts[0] == "actions":
                if len(parts) != 3 or parts[2] not in _LITE_ACTION_FILES:
                    continue
            elif parts[0] not in ("reference",) and rel.name != "sprite.json":
                continue
        yield p, rel.as_posix()


@router.post("/export")
def export_project(req: ExportRequest):
    sprites = _pick_sprites(req)
    if not sprites:
        raise HTTPException(status_code=400, detail="没有匹配的精灵可导出")

    # 收集引用到的模板
    tpl_ids = set()
    sprite_metas = []
    for sp in sprites:
        sprite_metas.append({"id": sp["id"], "name": sp["name"]})
        for ref in sp.get("actions", []):
            try:
                a = sprite_store.get_action(sp["id"], ref["id"])
            except Exception:
                continue
            if a.get("template_id"):
                tpl_ids.add(a["template_id"])
    templates = [t for t in template_store.list() if t["id"] in tpl_ids]

    from app.services.prompt_store import prompt_store
    manifest = {
        "format": FORMAT, "version": VERSION,
        "exported_at": time.time(),
        "project": req.project or None,
        "include_workdata": req.include_workdata,
        "sprites": sprite_metas,
        "templates": templates,
        "prompts": prompt_store.list(),     # 提示词库整体随包（按 id 合并）
    }

    tmp_dir = get_settings().resolved_data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, zpath = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
    import os
    os.close(fd)
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=1))
            for sp in sprites:
                sp_dir = sprite_store.sprite_dir(sp["id"])
                for abs_p, rel in _iter_sprite_files(sp_dir, req.include_workdata):
                    z.write(abs_p, f"sprites/{sp['id']}/{rel}")
            for t in templates:
                p = template_store.path(t)
                if p.is_file():
                    z.write(p, f"templates/{t['file']}")
    except Exception:
        Path(zpath).unlink(missing_ok=True)
        raise

    stamp = time.strftime("%Y%m%d_%H%M")
    label = (req.project or "全部精灵").replace("/", "_")
    filename = f"sprite_project_{label}_{stamp}.zip"
    return FileResponse(zpath, media_type="application/zip", filename=filename,
                        background=BackgroundTask(lambda: Path(zpath).unlink(missing_ok=True)))


def _safe_rel(name: str, prefix: str) -> Optional[Path]:
    """zip 成员名安全校验（防 zip-slip），返回去掉前缀的相对路径。"""
    if not name.startswith(prefix):
        return None
    rel = name[len(prefix):]
    p = Path(rel)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        return None
    return p


@router.post("/import")
async def import_project(file: UploadFile = File(...)):
    # 落盘再解析（包可能很大，不整体读入内存）
    tmp_dir = get_settings().resolved_data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, zpath = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
    import os
    os.close(fd)
    try:
        with open(zpath, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            z = zipfile.ZipFile(zpath)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="不是有效的 zip 文件")
        with z:
            try:
                manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            except KeyError:
                raise HTTPException(status_code=400, detail="包内缺少 manifest.json")
            if manifest.get("format") != FORMAT:
                raise HTTPException(status_code=400, detail="不是精灵项目包")
            if manifest.get("version", 0) > VERSION:
                raise HTTPException(status_code=400,
                                    detail="包版本过新，请先升级本服务")

            stats = {"templates_imported": 0, "templates_reused": 0,
                     "sprites_imported": 0, "sprites_skipped": 0,
                     "actions_imported": 0, "prompts_imported": 0, "errors": []}

            # 0) 提示词库：同 id 跳过
            from app.services.prompt_store import prompt_store
            for rec in manifest.get("prompts", []) or []:
                if prompt_store.import_record(rec):
                    stats["prompts_imported"] += 1

            # 1) 模板：按 (key,variant) 合并，记录 id 改写映射
            tid_map = {}
            for rec in manifest.get("templates", []):
                member = f"templates/{rec.get('file', '')}"
                try:
                    data = z.read(member)
                except KeyError:
                    stats["errors"].append(f"模板文件缺失: {rec.get('filename', member)}")
                    continue
                old_id, new_id, added = template_store.resolve_or_add(rec, data)
                tid_map[old_id] = new_id
                stats["templates_imported" if added else "templates_reused"] += 1

            # 2) 精灵：同 id 已存在则跳过，否则解包目录
            names = z.namelist()
            for sp in manifest.get("sprites", []):
                sp_id = sp.get("id", "")
                if not sp_id or "/" in sp_id or "\\" in sp_id or ".." in sp_id:
                    stats["errors"].append(f"非法精灵 id: {sp_id}")
                    continue
                dest_dir = sprite_store.sprite_dir(sp_id)
                if dest_dir.exists():
                    stats["sprites_skipped"] += 1
                    continue
                prefix = f"sprites/{sp_id}/"
                members = [n for n in names
                           if n.startswith(prefix) and not n.endswith("/")]
                if not members:
                    stats["errors"].append(f"包内缺少精灵数据: {sp.get('name', sp_id)}")
                    continue
                for n in members:
                    rel = _safe_rel(n, prefix)
                    if rel is None:
                        stats["errors"].append(f"跳过可疑路径: {n}")
                        continue
                    target = dest_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(n) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                # 改写动作的模板绑定到本机 id
                actions_dir = dest_dir / "actions"
                if actions_dir.is_dir():
                    for aj in actions_dir.glob("*/action.json"):
                        try:
                            a = json.loads(aj.read_text(encoding="utf-8"))
                        except (json.JSONDecodeError, OSError):
                            continue
                        tid = a.get("template_id")
                        if tid and tid_map.get(tid) and tid_map[tid] != tid:
                            a["template_id"] = tid_map[tid]
                            aj.write_text(json.dumps(a, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
                        stats["actions_imported"] += 1
                stats["sprites_imported"] += 1

        # 让 action → sprite 索引重扫，新精灵立即可用
        with sprite_store._lock:
            sprite_store._scanned = False
        return stats
    finally:
        Path(zpath).unlink(missing_ok=True)
