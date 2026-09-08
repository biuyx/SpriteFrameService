"""参考首帧集 API：某完成角色的全套动作首帧库（新角色首帧生图的参考源）。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.services.ffset_store import FfSetError, ffset_store

router = APIRouter(prefix="/ffsets", tags=["ffsets"])


@router.get("")
def list_sets():
    return {"sets": ffset_store.list()}


class ImportDirRequest(BaseModel):
    dir: str = Field(..., description="首帧图片目录（文件名主干 = 动作 key）")
    name: str = Field(default="", description="参考集名称，缺省用目录名")
    group: str = Field(default="", description="关联的模板分组（可空）")


@router.post("/import-dir")
def import_dir(req: ImportDirRequest):
    try:
        return ffset_store.import_dir(Path(req.dir.strip()), req.name, req.group)
    except FfSetError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ArchiveRequest(BaseModel):
    sprite_id: str
    name: Optional[str] = None
    group: Optional[str] = None


@router.post("/archive-sprite")
def archive_sprite(req: ArchiveRequest):
    from app.services.sprite_store import SpriteStoreError
    try:
        return ffset_store.archive_sprite(req.sprite_id, req.name, req.group)
    except SpriteStoreError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FfSetError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{set_id}/frames/{key}/image")
def frame_image(set_id: str, key: str):
    p = ffset_store.frame_path(set_id, key)
    if p is None:
        raise HTTPException(status_code=404, detail="参考首帧不存在")
    return Response(content=p.read_bytes(), media_type="image/png")


@router.delete("/{set_id}")
def delete_set(set_id: str):
    return {"deleted": ffset_store.delete(set_id)}
