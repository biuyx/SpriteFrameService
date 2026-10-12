"""Spine 导出模板 API：从参考工程导入（服务器路径或上传 zip）、列出、改名、删除。"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.spine_import import SpineImportError, load_reference
from app.services.spine_template_store import spine_template_store

router = APIRouter(prefix="/spine-templates", tags=["spine-templates"])


class ImportRequest(BaseModel):
    path: str = Field(..., description="参考工程目录，或 .json / .skel 文件")
    name: Optional[str] = Field(default=None, description="模板名，缺省用文件名")
    dry_run: bool = Field(False, description="只解析看结果，不落库")


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


class AnimMapRequest(BaseModel):
    map: dict = Field(..., description="动作名 → 动画名；值为空表示删掉这一条")
    replace: bool = Field(False, description="整表替换（缺省是合并）")


@router.get("")
def list_templates():
    """模板列表（去掉逐动画明细，列表页用不上）。"""
    items = []
    for t in spine_template_store.list():
        items.append({k: v for k, v in t.items() if k != "animations"} |
                     {"animation_count": len(t.get("animations") or []),
                      "animation_names": [a["name"] for a in
                                          (t.get("animations") or [])],
                      "mapped_count": len(t.get("anim_map") or {})})
    items.sort(key=lambda t: t.get("updated_at") or 0, reverse=True)
    return {"templates": items}


@router.get("/{template_id}")
def get_template(template_id: str):
    t = spine_template_store.get(template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return t


# 只解骨架与图集索引：工程里的 png 可能很大，解析用不上
WANTED_SUFFIX = (".json", ".skel", ".atlas")
MAX_MEMBERS = 5000
MAX_BYTES = 200 * 1024 * 1024


def _safe_member(name: str) -> Optional[Path]:
    """zip 成员名安全校验（防 zip-slip）。"""
    p = Path(name)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        return None
    return p


def _extract_reference(src: Path, dest: Path) -> None:
    """把上传的包解到 dest：zip 只取骨架/图集，单文件直接放进去。"""
    if src.suffix.lower() != ".zip":
        shutil.copy2(src, dest / src.name)
        return
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="不是有效的 zip 文件")
    with z:
        members = [i for i in z.infolist()
                   if not i.is_dir()
                   and Path(i.filename).suffix.lower() in WANTED_SUFFIX]
        if len(members) > MAX_MEMBERS:
            raise HTTPException(status_code=400, detail="包内文件过多")
        if sum(i.file_size for i in members) > MAX_BYTES:
            raise HTTPException(status_code=400, detail="解压后体积过大（上限 200 MB）")
        got = 0
        for info in members:
            rel = _safe_member(info.filename)
            if rel is None:
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as fsrc, open(out, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
            got += 1
        if not got:
            raise HTTPException(status_code=400,
                                detail="包里没有 .json / .skel 骨架数据")


def _parse_reference(path: Path, name: str) -> dict:
    try:
        return load_reference(path, (name or "").strip())
    except SpineImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析失败: {e}")


def _describe(tpl: dict, dry_run: bool) -> dict:
    """解析结果 → 接口返回（顺带把需要人留意的地方说清楚）。"""
    warnings = []
    if not tpl.get("canvas"):
        warnings.append("没读出画布尺寸，导出时需手动指定")
    no_fps = [a["name"] for a in tpl["animations"] if not a.get("fps")]
    if no_fps:
        warnings.append(f"{len(no_fps)} 个动画没读出帧率，导出时用默认值")
    if (tpl.get("skin_count") or 1) > 1:
        names = "、".join(n for n in (tpl.get("skin_names") or []) if n)
        warnings.append(f"参考工程有 {tpl['skin_count']} 套皮肤（{names}），"
                        "导出只产出 default 一套——换装需在 Spine 里另行处理")
    if tpl.get("source_kind") == "skel":
        warnings.append("参考来自 .skel 二进制；如有 Spine 导出的 .json，"
                        "用它做参考更可靠")

    if dry_run:
        return {"template": tpl, "warnings": warnings, "saved": False}
    rec = spine_template_store.add(tpl)
    return {"template": rec, "warnings": warnings, "saved": True}


@router.post("/import")
def import_template(req: ImportRequest):
    """解析服务器上的参考工程（目录或 .json/.skel），反解出导出约定并存为模板。"""
    p = Path(req.path.strip().strip('"'))
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"路径不存在: {p}")
    return _describe(_parse_reference(p, req.name or ""), req.dry_run)


@router.post("/upload")
async def upload_template(file: UploadFile = File(...),
                          name: str = Form(""),
                          dry_run: bool = Form(False)):
    """上传参考工程导入：整个工程打成 zip，或单个 .json / .skel。

    服务跑在别的机器上时没法填本地路径，这条走上传。
    """
    tmp_root = get_settings().resolved_data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="spine_ref_", dir=str(tmp_root)))
    try:
        raw = work / "上传"
        raw.mkdir()
        src = raw / (Path(file.filename or "reference.zip").name or "reference.zip")
        with open(src, "wb") as f:          # 包可能很大，别整体读进内存
            shutil.copyfileobj(file.file, f)
        unpacked = work / "工程"
        unpacked.mkdir()
        _extract_reference(src, unpacked)
        tpl = _parse_reference(unpacked, name or Path(src.name).stem)
        return _describe(tpl, dry_run)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@router.patch("/{template_id}")
def rename_template(template_id: str, req: RenameRequest):
    try:
        return spine_template_store.rename(template_id, req.name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{template_id}/anim-map")
def set_anim_map(template_id: str, req: AnimMapRequest):
    """存这套模板下的动作关联：换个角色、同样的动作名就不用再对一遍。"""
    try:
        t = spine_template_store.set_anim_map(template_id, req.map, req.replace)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"id": t["id"], "anim_map": t.get("anim_map") or {}}


@router.delete("/{template_id}")
def delete_template(template_id: str):
    if not spine_template_store.delete(template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"deleted": template_id}
