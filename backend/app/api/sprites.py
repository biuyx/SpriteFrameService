"""精灵/动作管理 API。

工作台内的一切（帧/抠图/导出/历史/任务）继续走 /api/sessions/{action_id}/...，
本模块只负责实体层：精灵档案、动作看板、打开动作、旧会话认领。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional, Union

import cv2
import numpy as np
from fastapi import APIRouter, File, HTTPException, Response, UploadFile
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
    template_id: Optional[str] = None


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
    # 每精灵聚合各阶段进度（生成→抽帧→抠图→导出→定稿），列表视图用
    for sp in sprites:
        refs = sp.get("actions", [])
        sp["action_count"] = len(refs)
        prog = {"generated": 0, "extracted": 0, "processed": 0,
                "exported": 0, "final": 0}
        for ref in refs:
            try:
                a = sprite_store.get_action(sp["id"], ref["id"])
            except SpriteStoreError:
                continue
            s = sprite_store._action_summary(sp["id"], ref["id"])
            # 生成 = 有成功的生成版本（失败/进行中的尝试不算；上传视频另算素材）
            if s.get("generated_count"):
                prog["generated"] += 1
            if s.get("frame_count"):
                prog["extracted"] += 1
            if s.get("processed_count"):
                prog["processed"] += 1
            if s.get("export_count"):
                prog["exported"] += 1
            if a.get("status") == "final":
                prog["final"] += 1
        sp["progress"] = prog
        sp["final_count"] = prog["final"]
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


class AsRefRequest(BaseModel):
    role: str = Field(..., pattern="^(front|back)$")


@router.post("/{sprite_id}/actions/{action_id}/first-frame/as-ref")
def action_first_frame_as_ref(sprite_id: str, action_id: str, req: AsRefRequest):
    """把动作现有首帧登记为精灵的正面/背面立绘（入首帧图库并标记朝向，同朝向排他）。"""
    action = _wrap(lambda: sprite_store.get_action(sprite_id, action_id))
    p = sprite_store.action_dir(sprite_id, action_id) / "first_frame.png"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="该动作尚无首帧")
    label = "正面立绘" if req.role == "front" else "背面立绘"
    rec = _wrap(lambda: sprite_store.add_ref(
        sprite_id, f"{action.get('name', '')}·{label}", p.read_bytes()))
    rec = sprite_store.update_ref(sprite_id, rec["id"], {"role": req.role})
    return rec


@router.get("/{sprite_id}/actions/{action_id}/first-frame")
def action_first_frame(sprite_id: str, action_id: str):
    """动作当前首帧图（不加载会话，总览网格用）。"""
    _wrap(lambda: sprite_store.get_action(sprite_id, action_id))
    p = sprite_store.action_dir(sprite_id, action_id) / "first_frame.png"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="尚无首帧")
    return Response(content=p.read_bytes(), media_type="image/png")


# ---------- 首帧参考图库（精灵级共享：一张图可用于多个动作的生成） ----------
class RefApplyRequest(BaseModel):
    action_ids: List[str] = Field(..., min_length=1, max_length=200)


@router.get("/{sprite_id}/refs")
def list_sprite_refs(sprite_id: str):
    _wrap(lambda: sprite_store.get_sprite(sprite_id))
    return {"refs": sprite_store.list_refs(sprite_id)}


@router.post("/{sprite_id}/refs")
async def upload_sprite_ref(sprite_id: str, file: UploadFile = File(...)):
    """上传参考图入库（统一转 PNG，按内容去重）。"""
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片超过 10MB")
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise HTTPException(status_code=400, detail="不是可识别的图片文件")
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise HTTPException(status_code=400, detail="图片编码失败")
    name = Path(file.filename or "参考图").stem
    return _wrap(lambda: sprite_store.add_ref(sprite_id, name, buf.tobytes()))


@router.get("/{sprite_id}/refs/{ref_id}/image")
def sprite_ref_image(sprite_id: str, ref_id: str):
    ref = sprite_store.get_ref(sprite_id, ref_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="参考图不存在")
    p = sprite_store.ref_path(sprite_id, ref)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="参考图文件缺失")
    return Response(content=p.read_bytes(), media_type="image/png")


class RefPatch(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = Field(default=None, pattern="^(front|back|)$")


@router.patch("/{sprite_id}/refs/{ref_id}")
def patch_sprite_ref(sprite_id: str, ref_id: str, req: RefPatch):
    ref = sprite_store.update_ref(sprite_id, ref_id,
                                  req.model_dump(exclude_unset=True))
    if ref is None:
        raise HTTPException(status_code=404, detail="参考图不存在")
    return ref


@router.delete("/{sprite_id}/refs/{ref_id}")
def delete_sprite_ref(sprite_id: str, ref_id: str):
    return {"deleted": sprite_store.delete_ref(sprite_id, ref_id)}


@router.post("/{sprite_id}/refs/{ref_id}/apply")
def apply_sprite_ref(sprite_id: str, ref_id: str, req: RefApplyRequest):
    """把图库中的一张参考图设为多个动作的首帧（覆盖各动作现有首帧）。"""
    ref = sprite_store.get_ref(sprite_id, ref_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="参考图不存在")
    p = sprite_store.ref_path(sprite_id, ref)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="参考图文件缺失")
    data = p.read_bytes()

    applied, skipped = [], []
    for aid in req.action_ids:
        try:
            sprite_store.get_action(sprite_id, aid)
        except SpriteStoreError:
            skipped.append({"action_id": aid, "reason": "不属于该精灵"})
            continue
        dest = sprite_store.action_dir(sprite_id, aid) / "first_frame.png"
        try:
            dest.write_bytes(data)
        except OSError as e:
            skipped.append({"action_id": aid, "reason": f"写入失败: {e}"})
            continue
        sprite_store.update_action(sprite_id, aid, {
            "first_frame": {"kind": "sprite_ref", "ref_id": ref_id,
                            "file": "first_frame.png"},
        })
        applied.append(aid)
    return {"applied": applied, "skipped": skipped}


# ---------- 批量导入（角色×动作矩阵） ----------
class BatchScanRequest(BaseModel):
    frames_dir: str = Field(..., description="角色首帧根目录（每子目录一个角色）")
    templates_dir: Optional[str] = Field(default=None, description="动作模板视频目录（可选）")
    # 参考视频库分组名（非 None 时以该分组的动作为模板集，templates_dir 忽略）
    template_group: Optional[str] = None


class ActionImportSpec(BaseModel):
    """库分组模式的动作条目：动作名 + 首帧图 key + 精确绑定的模板。"""
    name: str
    key: str
    template_id: Optional[str] = None


class BatchImportSprite(BaseModel):
    dir_name: str
    name: Optional[str] = None          # 精灵名，缺省用目录名
    # 目录模式：key 字符串列表；库分组模式：ActionImportSpec 列表
    actions: List[Union[ActionImportSpec, str]]


class BatchImportRequest(BaseModel):
    frames_dir: str
    templates_dir: Optional[str] = None
    template_group: Optional[str] = None    # 参考视频库分组（非 None 时优先）
    project: Optional[str] = None           # 项目名称 → 精灵分类标签
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

    templates_preview = None

    # 模板来源 A：参考视频库分组——每个模板对应一个动作（变体名即动作名，
    # 同 key 多变体共用同一张首帧图）；缺图的动作也建（可后补首帧图库）
    if req.template_group is not None:
        from app.services.template_store import template_store
        group_tpls = template_store.by_group(req.template_group)
        if not group_tpls:
            raise HTTPException(status_code=400,
                                detail=f"参考视频库分组「{req.template_group or '未分组'}」下没有模板")
        group_keys = {t["key"] for t in group_tpls}
        for s in sprites:
            images = {a["key"]: a["file"] for a in s["actions"]}
            acts, used = [], set()
            for t in group_tpls:
                name = t["variant"] or t["key"]
                if name in used:                       # 变体名撞车时带上 key 保证唯一
                    name = f"{t['key']}_{t['variant']}" if t["variant"] else t["key"]
                used.add(name)
                acts.append({"name": name, "key": t["key"], "variant": t["variant"],
                             "template_id": t["id"], "file": images.get(t["key"]),
                             "has_image": t["key"] in images})
            s["actions"] = acts
            s["extra_images"] = sorted(k for k in images if k not in group_keys)
        templates_preview = {
            "group": req.template_group,
            "found": [{"key": t["key"], "variant": t["variant"],
                       "duration_hint": t.get("duration_hint")} for t in group_tpls],
            "new_count": 0,
        }

    # 模板来源 B：本地目录——只预览将导入的数量，不落库
    elif req.templates_dir and req.templates_dir.strip():
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

    project = (req.project or "").strip()
    library_mode = req.template_group is not None

    # 1) 模板集：库分组模式直接用分组；目录模式先导入目录（幂等）
    templates_result = None
    if library_mode:
        group_tpls = template_store.by_group(req.template_group)
        if not group_tpls:
            raise HTTPException(status_code=400,
                                detail=f"参考视频库分组「{req.template_group or '未分组'}」下没有模板")
        tpl_pool = group_tpls
    else:
        if req.templates_dir and req.templates_dir.strip():
            try:
                templates_result = template_store.scan_import(Path(req.templates_dir.strip()))
            except FileNotFoundError as e:
                raise HTTPException(status_code=400, detail=str(e))
        tpl_pool = template_store.list()

    # 动作 key → 默认模板（同 key 多变体取第一个，生成前可切换）
    tpl_by_key = {}
    for t in tpl_pool:
        tpl_by_key.setdefault(t["key"], t["id"])

    existing_sprites = {sp["name"]: sp["id"] for sp in sprite_store.list_sprites()}
    stats = {"sprites_created": 0, "sprites_reused": 0,
             "actions_created": 0, "actions_skipped": 0,
             "actions_no_frame": 0, "errors": []}

    for spec in req.sprites:
        src_dir = root / spec.dir_name
        if not src_dir.is_dir():
            stats["errors"].append(f"角色目录不存在: {spec.dir_name}")
            continue
        name = (spec.name or spec.dir_name).strip()

        if name in existing_sprites:
            sprite_id = existing_sprites[name]
            stats["sprites_reused"] += 1
            # 复用的精灵补挂项目标签（已有标签保留）
            if project:
                sp = sprite_store.get_sprite(sprite_id)
                tags = sp.get("tags") or []
                if project not in tags:
                    sprite_store.update_sprite(sprite_id, {"tags": tags + [project]})
        else:
            sprite = sprite_store.create_sprite(
                name, tags=[project] if project else None)
            sprite_id = sprite["id"]
            existing_sprites[name] = sprite_id
            stats["sprites_created"] += 1

        existing_actions = {r["name"] for r in
                            sprite_store.get_sprite(sprite_id).get("actions", [])}

        for item in spec.actions:
            if isinstance(item, str):
                a_name, img_key = item, item
                tid = tpl_by_key.get(item)
            else:                        # 库分组模式：模板即动作，精确绑定
                a_name, img_key = item.name, item.key
                tid = item.template_id or tpl_by_key.get(item.key)
            if a_name in existing_actions:
                stats["actions_skipped"] += 1
                continue
            # 找首帧图（img_key 即文件主干；同 key 多个动作共用同一张图）
            src_img = None
            for ext in IMAGE_EXTS:
                p = src_dir / f"{img_key}{ext}"
                if p.is_file():
                    src_img = p
                    break
            if src_img is None and not library_mode:
                # 目录模式：动作集来自图片，缺图视为异常
                stats["errors"].append(f"{name}/{a_name}: 首帧图缺失")
                continue

            first_frame = ({"kind": "batch_import", "source": str(src_img),
                            "file": "first_frame.png"} if src_img else None)
            action = sprite_store.create_action(sprite_id, a_name,
                                                first_frame=first_frame)
            if src_img is not None:
                # 物化首帧（统一转 PNG 名义；源已是 PNG 直接拷贝）
                dest = sprite_store.action_dir(sprite_id, action["id"]) / "first_frame.png"
                try:
                    import shutil as _sh
                    _sh.copyfile(src_img, dest)
                except OSError as e:
                    stats["errors"].append(f"{name}/{a_name}: 首帧拷贝失败 {e}")
            else:
                # 库分组模式：缺图也建动作，之后可从首帧图库补
                stats["actions_no_frame"] += 1
            # 关联动作模板
            if tid:
                sprite_store.update_action(sprite_id, action["id"],
                                           {"template_id": tid})
            existing_actions.add(a_name)
            stats["actions_created"] += 1

    return {"templates": templates_result, **stats}


# ---------- 首帧生成（立绘 + 参考首帧集 → Seedream 生图） ----------
class FfGenRequest(BaseModel):
    set_id: str = Field(..., description="参考首帧集 id")
    prompt: Optional[str] = Field(default=None, description="留空按提示词库解析")
    prompt_id: Optional[str] = None       # 提示词来自库时的追溯信息
    prompt_version: Optional[int] = None
    prompt_name: Optional[str] = None
    remember: bool = Field(default=True, description="记为该动作的首帧生成设定")


def _prompt_meta(req) -> dict:
    return {"prompt_id": req.prompt_id, "prompt_version": req.prompt_version,
            "prompt_name": req.prompt_name}


@router.post("/{sprite_id}/actions/{action_id}/gen-first-frame")
def gen_first_frame(sprite_id: str, action_id: str, req: FfGenRequest):
    """为单个动作 AI 生成首帧（按张计费）。"""
    from app.core.first_frame_generator import resolve_refs, run_gen_first_frame
    from app.services.job_manager import job_manager

    action = _wrap(lambda: sprite_store.get_action(sprite_id, action_id))
    try:
        resolve_refs(sprite_id, action, req.set_id)   # 预检，让错误同步返回
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = job_manager.submit(
        "gen_first_frame",
        lambda ctx: run_gen_first_frame(sprite_id, action_id, req.set_id,
                                        req.prompt, ctx,
                                        prompt_meta=_prompt_meta(req),
                                        remember=req.remember),
        pool="io")
    return {"job_id": job.id}


class BatchFfGenRequest(BaseModel):
    action_ids: List[str] = Field(..., min_length=1, max_length=200)
    set_id: str
    prompt: Optional[str] = Field(default=None, description="留空按各动作解析提示词库")
    prompt_id: Optional[str] = None
    prompt_version: Optional[int] = None
    prompt_name: Optional[str] = None
    remember: bool = True


@router.post("/{sprite_id}/batch-gen-first-frames")
def batch_gen_first_frames(sprite_id: str, req: BatchFfGenRequest):
    """为多个动作批量生成首帧（并发闸门 3，逐张计费）。"""
    from app.core.first_frame_generator import resolve_refs, run_gen_first_frame
    from app.services.job_manager import job_manager

    _wrap(lambda: sprite_store.get_sprite(sprite_id))
    submitted, skipped = [], []
    for aid in req.action_ids:
        try:
            action = sprite_store.get_action(sprite_id, aid)
        except SpriteStoreError:
            skipped.append({"action_id": aid, "name": aid, "reason": "不属于该精灵"})
            continue
        name = action.get("name", aid)
        try:
            resolve_refs(sprite_id, action, req.set_id)
        except ValueError as e:
            skipped.append({"action_id": aid, "name": name, "reason": str(e)})
            continue
        job = job_manager.submit(
            "gen_first_frame",
            (lambda _a: lambda ctx: run_gen_first_frame(
                sprite_id, _a, req.set_id, req.prompt, ctx,
                prompt_meta=_prompt_meta(req), remember=req.remember))(aid),
            pool="io")
        submitted.append({"action_id": aid, "name": name, "job_id": job.id})
    return {"submitted": submitted, "skipped": skipped}


# ---------- 批量抽帧（按模板的参考抽帧规则） ----------
class BatchExtractRequest(BaseModel):
    action_ids: List[str] = Field(..., min_length=1, max_length=200)


@router.post("/{sprite_id}/batch-extract")
def batch_extract(sprite_id: str, req: BatchExtractRequest):
    """为多个动作按各自模板的抽帧规则提交抽帧任务。

    规则查找顺序：当前视频版本(take)生成时用的模板 → 动作绑定的模板。
    end 钳制到实际视频时长（生成时长与模板推荐值可能有零点几秒偏差）。
    """
    _wrap(lambda: sprite_store.get_sprite(sprite_id))
    from app.api.deps import get_session as _get_session
    from app.api.frames import submit_extract_job
    from app.services.take_store import TakeStore
    from app.services.template_store import template_store

    submitted, skipped = [], []
    for aid in req.action_ids:
        try:
            action = sprite_store.get_action(sprite_id, aid)
        except SpriteStoreError:
            skipped.append({"action_id": aid, "name": aid, "reason": "不属于该精灵"})
            continue
        name = action.get("name", aid)
        try:
            session = _get_session(aid)
        except HTTPException:
            skipped.append({"action_id": aid, "name": name, "reason": "会话不可用"})
            continue
        if session.video_info is None:
            skipped.append({"action_id": aid, "name": name, "reason": "无视频素材"})
            continue

        # 规则来源：当前 take 的模板优先（该版本生成时实际用的参考视频）
        ts = TakeStore(session.storage)
        cur = ts.get(ts.current_id()) if ts.current_id() else None
        tid = (cur or {}).get("template_id") or action.get("template_id")
        tpl = template_store.get(tid) if tid else None
        rule = (tpl or {}).get("extract_rule")
        if not rule:
            skipped.append({"action_id": aid, "name": name, "reason": "模板无抽帧规则"})
            continue

        start = float(rule["start"])
        end = min(float(rule["end"]), session.video_info.duration)
        if end <= start:
            skipped.append({"action_id": aid, "name": name,
                            "reason": "规则与视频时长不符"})
            continue
        try:
            job = submit_extract_job(session, start, end, float(rule["fps"]),
                                     keep=rule.get("keep"),
                                     keep_total=rule.get("total"))
        except HTTPException as e:
            skipped.append({"action_id": aid, "name": name, "reason": str(e.detail)})
            continue
        submitted.append({"action_id": aid, "name": name, "job_id": job.id,
                          "rule": {"start": start, "end": round(end, 3),
                                   "fps": rule["fps"],
                                   "keep_count": len(rule["keep"]) if rule.get("keep") else None,
                                   "total": rule.get("total"),
                                   "template": tpl.get("variant") or tpl.get("key")}})
    return {"submitted": submitted, "skipped": skipped}


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
