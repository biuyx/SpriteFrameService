"""Spine 导出模板 API：从参考工程导入、列出、改名、删除。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.spine_import import SpineImportError, load_reference
from app.services.spine_template_store import spine_template_store

router = APIRouter(prefix="/spine-templates", tags=["spine-templates"])


class ImportRequest(BaseModel):
    path: str = Field(..., description="参考工程目录，或 .json / .skel 文件")
    name: Optional[str] = Field(default=None, description="模板名，缺省用文件名")
    dry_run: bool = Field(False, description="只解析看结果，不落库")


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


@router.get("")
def list_templates():
    """模板列表（去掉逐动画明细，列表页用不上）。"""
    items = []
    for t in spine_template_store.list():
        items.append({k: v for k, v in t.items() if k != "animations"} |
                     {"animation_count": len(t.get("animations") or []),
                      "animation_names": [a["name"] for a in
                                          (t.get("animations") or [])]})
    items.sort(key=lambda t: t.get("updated_at") or 0, reverse=True)
    return {"templates": items}


@router.get("/{template_id}")
def get_template(template_id: str):
    t = spine_template_store.get(template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return t


@router.post("/import")
def import_template(req: ImportRequest):
    """解析参考工程，反解出导出约定并存为模板。"""
    p = Path(req.path.strip().strip('"'))
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"路径不存在: {p}")
    try:
        tpl = load_reference(p, (req.name or "").strip())
    except SpineImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析失败: {e}")

    warnings = []
    if not tpl.get("canvas"):
        warnings.append("没读出画布尺寸，导出时需手动指定")
    no_fps = [a["name"] for a in tpl["animations"] if not a.get("fps")]
    if no_fps:
        warnings.append(f"{len(no_fps)} 个动画没读出帧率，导出时用默认值")
    if tpl.get("source_kind") == "skel":
        warnings.append("参考来自 .skel 二进制；如有 Spine 导出的 .json，"
                        "用它做参考更可靠")

    if req.dry_run:
        return {"template": tpl, "warnings": warnings, "saved": False}
    rec = spine_template_store.add(tpl)
    return {"template": rec, "warnings": warnings, "saved": True}


@router.patch("/{template_id}")
def rename_template(template_id: str, req: RenameRequest):
    try:
        return spine_template_store.rename(template_id, req.name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{template_id}")
def delete_template(template_id: str):
    if not spine_template_store.delete(template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"deleted": template_id}
