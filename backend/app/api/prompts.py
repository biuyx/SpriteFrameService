"""提示词库 API：多版本、绑定、按动作解析。"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.prompt_store import (LEVELS, SCOPE_LABEL, SCOPES, PromptStoreError,
                                       prompt_store, resolve_for_action)
from app.services.sprite_store import SpriteStoreError, sprite_store

router = APIRouter(prefix="/prompts", tags=["prompts"])


def _wrap(fn):
    try:
        return fn()
    except PromptStoreError as e:
        raise HTTPException(status_code=400, detail=str(e))


class Binding(BaseModel):
    level: str = Field(..., pattern="^(global|group|key|template)$")
    value: str = ""


class PromptCreate(BaseModel):
    scope: str = Field(..., pattern="^(first_frame|video_ref|video_i2v)$")
    name: str = ""
    text: str = Field(..., min_length=1, max_length=4000)
    notes: str = ""
    note: str = "初始版本"
    bindings: List[Binding] = Field(default_factory=list)


class PromptMeta(BaseModel):
    name: Optional[str] = None
    notes: Optional[str] = None


class VersionCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    note: str = ""


class CurrentSet(BaseModel):
    v: int = Field(..., ge=1)


class BindingsSet(BaseModel):
    bindings: List[Binding]


@router.get("")
def list_prompts(scope: Optional[str] = None):
    prompt_store.ensure_seeds()
    return {"prompts": prompt_store.list(scope), "scopes": SCOPES,
            "scope_labels": SCOPE_LABEL, "levels": LEVELS}


@router.post("")
def create_prompt(req: PromptCreate):
    return _wrap(lambda: prompt_store.create(
        req.scope, req.name, req.text, req.notes,
        [b.model_dump() for b in req.bindings], req.note))


@router.patch("/{prompt_id}")
def patch_prompt(prompt_id: str, req: PromptMeta):
    return _wrap(lambda: prompt_store.update_meta(prompt_id, req.name, req.notes))


@router.post("/{prompt_id}/versions")
def add_version(prompt_id: str, req: VersionCreate):
    return _wrap(lambda: prompt_store.add_version(prompt_id, req.text, req.note))


@router.post("/{prompt_id}/current")
def set_current(prompt_id: str, req: CurrentSet):
    return _wrap(lambda: prompt_store.set_current(prompt_id, req.v))


@router.put("/{prompt_id}/bindings")
def set_bindings(prompt_id: str, req: BindingsSet):
    return _wrap(lambda: prompt_store.set_bindings(
        prompt_id, [b.model_dump() for b in req.bindings]))


@router.delete("/{prompt_id}")
def delete_prompt(prompt_id: str):
    return {"deleted": prompt_store.delete(prompt_id)}


# ------------------------------------------------------------ 解析
@router.get("/resolve")
def resolve(scope: str, sprite_id: str, action_id: str):
    """动作在某作用域下将使用的提示词：记忆 > 模板 > key > 分组 > 全局 > 内置。"""
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail="未知作用域")
    prompt_store.ensure_seeds()
    try:
        action = sprite_store.get_action(sprite_id, action_id)
    except SpriteStoreError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return resolve_for_action(scope, action)


class ResolveMany(BaseModel):
    scope: str = Field(..., pattern="^(first_frame|video_ref|video_i2v)$")
    sprite_id: str
    action_ids: List[str] = Field(..., min_length=1, max_length=500)


@router.post("/resolve-many")
def resolve_many(req: ResolveMany):
    prompt_store.ensure_seeds()
    out = {}
    for aid in req.action_ids:
        try:
            action = sprite_store.get_action(req.sprite_id, aid)
        except SpriteStoreError:
            continue
        r = resolve_for_action(req.scope, action)
        out[aid] = {k: r[k] for k in ("prompt_id", "version", "name", "source")}
    return {"resolved": out}
