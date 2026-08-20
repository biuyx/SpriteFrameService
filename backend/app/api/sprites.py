"""精灵/动作管理 API。

工作台内的一切（帧/抠图/导出/历史/任务）继续走 /api/sessions/{action_id}/...，
本模块只负责实体层：精灵档案、动作看板、打开动作、旧会话认领。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.session import session_manager
from app.services.sprite_store import (
    COMMON_ACTION_NAMES, SpriteStoreError, sprite_store,
)
from app.utils.image_utils import encode_preview, read_image

router = APIRouter(prefix="/sprites", tags=["sprites"])


# ---------- 请求体 ----------
class SpriteCreate(BaseModel):
    name: str = Field(..., description="精灵名称")
    tags: Optional[List[str]] = Field(default=None)


class SpritePatch(BaseModel):
    name: Optional[str] = None
    tags: Optional[List[str]] = None
    preset: Optional[dict] = None


class ActionCreate(BaseModel):
    name: str = Field(..., description="动作名称，如 walk")
    first_frame: Optional[dict] = Field(
        default=None,
        description='首帧来源，如 {"kind":"action_frame","action":"...","frame_index":7}',
    )


class ActionPatch(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    first_frame: Optional[dict] = None
    preset_override: Optional[dict] = None


class ClaimRequest(BaseModel):
    session_id: str = Field(..., description="旧会话 ID")
    name: str = Field(..., description="认领为动作的名称")


def _wrap(fn):
    try:
        return fn()
    except SpriteStoreError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------- 精灵 ----------
@router.get("")
def list_sprites():
    sprites = sprite_store.list_sprites()
    # 卡片进度：动作数与完成数
    for sp in sprites:
        refs = sp.get("actions", [])
        sp["action_count"] = len(refs)
        done = 0
        for ref in refs:
            try:
                a = sprite_store.get_action(sp["id"], ref["id"])
                if a.get("status") == "final":
                    done += 1
            except SpriteStoreError:
                pass
        sp["final_count"] = done
    return {"sprites": sprites, "common_action_names": COMMON_ACTION_NAMES}


@router.post("")
def create_sprite(req: SpriteCreate):
    return sprite_store.create_sprite(req.name, req.tags)


@router.get("/legacy-sessions")
def legacy_sessions():
    """旧版匿名会话列表（供认领）。"""
    return {"sessions": session_manager.list()}


@router.get("/{sprite_id}")
def get_sprite(sprite_id: str):
    return _wrap(lambda: sprite_store.get_sprite(sprite_id))


@router.patch("/{sprite_id}")
def patch_sprite(sprite_id: str, req: SpritePatch):
    patch = req.model_dump(exclude_none=True)
    return _wrap(lambda: sprite_store.update_sprite(sprite_id, patch))


@router.delete("/{sprite_id}")
def delete_sprite(sprite_id: str):
    """删除精灵及全部动作（不可恢复）。先释放各动作的句柄。"""
    sp = _wrap(lambda: sprite_store.get_sprite(sprite_id))
    for ref in sp.get("actions", []):
        session_manager.release(ref["id"])
    ok = sprite_store.delete_sprite(sprite_id)
    return {"deleted": ok, "id": sprite_id}


# ---------- 动作 ----------
@router.get("/{sprite_id}/actions")
def list_actions(sprite_id: str):
    return {"actions": _wrap(lambda: sprite_store.list_actions(sprite_id))}


@router.post("/{sprite_id}/actions")
def create_action(sprite_id: str, req: ActionCreate):
    _wrap(lambda: sprite_store.get_sprite(sprite_id))  # 校验存在
    action = sprite_store.create_action(sprite_id, req.name, req.first_frame)
    # 首帧来源为其他动作的帧时，物化为文件（血统引用 + 物理拷贝）
    if req.first_frame and req.first_frame.get("kind") == "action_frame":
        ff = sprite_store.materialize_first_frame(sprite_id, action["id"], req.first_frame)
        if ff is not req.first_frame:
            action = sprite_store.update_action(sprite_id, action["id"], {"first_frame": ff})
    return action


@router.patch("/{sprite_id}/actions/{action_id}")
def patch_action(sprite_id: str, action_id: str, req: ActionPatch):
    patch = req.model_dump(exclude_none=True)
    return _wrap(lambda: sprite_store.update_action(sprite_id, action_id, patch))


@router.delete("/{sprite_id}/actions/{action_id}")
def delete_action(sprite_id: str, action_id: str):
    session_manager.release(action_id)
    ok = sprite_store.delete_action(sprite_id, action_id)
    return {"deleted": ok, "id": action_id}


@router.post("/{sprite_id}/actions/{action_id}/open")
def open_action(sprite_id: str, action_id: str):
    """打开动作：建立/恢复工作态，返回给前端当 session 使用。"""
    if sprite_store.sprite_of_action(action_id) != sprite_id:
        raise HTTPException(status_code=404, detail="动作不存在")
    session = session_manager.get(action_id)
    if session is None:
        raise HTTPException(status_code=404, detail="动作工作目录缺失")

    # 首次打开：new → active
    action = _wrap(lambda: sprite_store.get_action(sprite_id, action_id))
    if action.get("status") == "new":
        action = sprite_store.update_action(sprite_id, action_id, {"status": "active"})

    sprite = _wrap(lambda: sprite_store.get_sprite(sprite_id))
    return {
        "session": session.summary(),
        "action": action,
        "sprite": {"id": sprite["id"], "name": sprite["name"],
                   "preset": sprite.get("preset", {})},
    }


@router.get("/{sprite_id}/actions/{action_id}/cover")
def action_cover(sprite_id: str, action_id: str):
    """看板封面（第一帧，处理图优先）。无帧时 404，前端显示占位。"""
    p = sprite_store.cover_path(sprite_id, action_id)
    if p is None:
        raise HTTPException(status_code=404, detail="暂无封面")
    img = read_image(p)
    if img is None:
        raise HTTPException(status_code=404, detail="封面不可读")
    data = encode_preview(img, transparent_checker=True, max_w=320, max_h=320)
    return Response(content=data, media_type="image/png")


# ---------- 批量导入（角色×动作矩阵） ----------
class BatchScanRequest(BaseModel):
    frames_dir: str = Field(..., description="角色首帧根目录（每子目录一个角色）")
    templates_dir: Optional[str] = Field(default=None, description="动作模板视频目录（可选）")


class BatchImportSprite(BaseModel):
    dir_name: str
    name: Optional[str] = None          # 精灵名，缺省用目录名
    actions: List[str]                  # 动作 key 列表


class BatchImportRequest(BaseModel):
    frames_dir: str
    templates_dir: Optional[str] = None
    sprites: List[BatchImportSprite]


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _scan_frames_dir(root: Path) -> List[dict]:
    result = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        actions = []
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                actions.append({"key": f.stem.strip(), "file": f.name})
        if actions:
            result.append({"dir_name": d.name, "actions": actions})
    return result


@router.post("/batch-scan")
def batch_scan(req: BatchScanRequest):
    """扫描素材目录，返回 角色×动作 矩阵预览（不写任何数据）。"""
    root = Path(req.frames_dir.strip())
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"首帧目录不存在: {root}")

    sprites = _scan_frames_dir(root)
    if not sprites:
        raise HTTPException(status_code=400,
                            detail="目录下没有找到「子目录/图片」结构的角色首帧")

    # 模板目录（可选）：只预览将导入的数量，不落库
    templates_preview = None
    if req.templates_dir and req.templates_dir.strip():
        tdir = Path(req.templates_dir.strip())
        if not tdir.is_dir():
            raise HTTPException(status_code=400, detail=f"模板目录不存在: {tdir}")
        from app.services.template_store import (VIDEO_EXTS, parse_template_name,
                                                 template_store)
        existing = {(t["key"], t["variant"]) for t in template_store.list()}
        found, new = [], 0
        for f in sorted(tdir.iterdir()):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                key, variant, duration = parse_template_name(f.stem)
                found.append({"key": key, "variant": variant,
                              "duration_hint": duration, "file": f.name})
                if (key, variant) not in existing:
                    new += 1
        templates_preview = {"found": found, "new_count": new}

    # 已有精灵（同名将复用而不是重复创建）
    existing_sprites = {sp["name"]: sp["id"] for sp in sprite_store.list_sprites()}
    for s in sprites:
        s["existing_sprite_id"] = existing_sprites.get(s["dir_name"])

    return {"sprites": sprites, "templates": templates_preview}


@router.post("/batch-import")
def batch_import(req: BatchImportRequest):
    """执行批量建档：建精灵与动作、物化首帧、按 key 关联动作模板。幂等。"""
    root = Path(req.frames_dir.strip())
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"首帧目录不存在: {root}")

    from app.services.template_store import template_store

    # 1) 模板目录先导入（幂等）
    templates_result = None
    if req.templates_dir and req.templates_dir.strip():
        try:
            templates_result = template_store.scan_import(Path(req.templates_dir.strip()))
        except FileNotFoundError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # 动作 key → 默认模板（同 key 多变体取第一个，生成前可切换）
    tpl_by_key = {}
    for t in template_store.list():
        tpl_by_key.setdefault(t["key"], t["id"])

    existing_sprites = {sp["name"]: sp["id"] for sp in sprite_store.list_sprites()}
    stats = {"sprites_created": 0, "sprites_reused": 0,
             "actions_created": 0, "actions_skipped": 0, "errors": []}

    for spec in req.sprites:
        src_dir = root / spec.dir_name
        if not src_dir.is_dir():
            stats["errors"].append(f"角色目录不存在: {spec.dir_name}")
            continue
        name = (spec.name or spec.dir_name).strip()

        if name in existing_sprites:
            sprite_id = existing_sprites[name]
            stats["sprites_reused"] += 1
        else:
            sprite = sprite_store.create_sprite(name)
            sprite_id = sprite["id"]
            existing_sprites[name] = sprite_id
            stats["sprites_created"] += 1

        existing_actions = {r["name"] for r in
                            sprite_store.get_sprite(sprite_id).get("actions", [])}

        for key in spec.actions:
            if key in existing_actions:
                stats["actions_skipped"] += 1
                continue
            # 找首帧图（key 即文件主干）
            src_img = None
            for ext in IMAGE_EXTS:
                p = src_dir / f"{key}{ext}"
                if p.is_file():
                    src_img = p
                    break
            if src_img is None:
                stats["errors"].append(f"{name}/{key}: 首帧图缺失")
                continue

            action = sprite_store.create_action(
                sprite_id, key,
                first_frame={"kind": "batch_import", "source": str(src_img),
                             "file": "first_frame.png"})
            # 物化首帧（统一转 PNG 名义；源已是 PNG 直接拷贝）
            dest = sprite_store.action_dir(sprite_id, action["id"]) / "first_frame.png"
            try:
                import shutil as _sh
                _sh.copyfile(src_img, dest)
            except OSError as e:
                stats["errors"].append(f"{name}/{key}: 首帧拷贝失败 {e}")
            # 关联动作模板
            tid = tpl_by_key.get(key)
            if tid:
                sprite_store.update_action(sprite_id, action["id"],
                                           {"template_id": tid})
            existing_actions.add(key)
            stats["actions_created"] += 1

    return {"templates": templates_result, **stats}


# ---------- 旧会话认领 ----------
@router.post("/{sprite_id}/claim")
def claim_legacy_session(sprite_id: str, req: ClaimRequest):
    """把旧匿名会话认领为该精灵的动作（目录整体移动，ID 不变）。"""
    _wrap(lambda: sprite_store.get_sprite(sprite_id))

    src = get_settings().sessions_dir / req.session_id
    if not src.is_dir():
        raise HTTPException(status_code=404, detail="旧会话不存在")
    if sprite_store.sprite_of_action(req.session_id) is not None:
        raise HTTPException(status_code=400, detail="该会话已被认领")

    # 释放可能存在的句柄（Windows 下文件被占用则移动失败）
    session_manager.release(req.session_id)

    dest = sprite_store.action_dir(sprite_id, req.session_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dest))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"目录移动失败: {e}")

    # 写实体记录（复用 create_action 的索引维护，但目录已就位）
    action = sprite_store.register_claimed_action(sprite_id, req.session_id, req.name)
    return {"action": action}
