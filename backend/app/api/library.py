"""Admin taxonomy and explicit, previewed legacy classification."""
from __future__ import annotations

import hashlib
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.services.library_catalog import CatalogError, library_catalog
from app.services.sprite_store import sprite_store

router = APIRouter(prefix='/library', tags=['library'])


class CatalogPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, max_length=80)
    project_id: str | None = None
    parent_id: str | None = None
    category_id: str | None = None
    template_binding: dict | None = None


class Assignment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sprite_ids: list[str] = Field(min_length=1, max_length=200)
    project_id: str | None = None
    sprite_type_id: str | None = None
    revisions: dict[str, str] | None = None


@router.get('')
def catalog():
    return library_catalog.read()


def _patch(kind, req):
    allowed = {'projects': {'name'}, 'categories': {'name', 'project_id', 'parent_id'},
               'types': {'name', 'project_id', 'category_id', 'template_binding'}}[kind]
    data = req.model_dump(exclude_unset=True)
    if set(data) - allowed:
        raise CatalogError('该对象不支持这些字段')
    return data


@router.post('/{kind}')
def create(kind: Literal['projects', 'categories', 'types'], req: CatalogPatch):
    return library_catalog.save(kind, _patch(kind, req))


@router.patch('/{kind}/{rid}')
def update(kind: Literal['projects', 'categories', 'types'], rid: str, req: CatalogPatch):
    return library_catalog.save(kind, _patch(kind, req), rid)


@router.delete('/{kind}/{rid}')
def delete(kind: Literal['projects', 'categories', 'types'], rid: str):
    library_catalog.delete(kind, rid)
    return {'deleted': True}


def _preview(req):
    rows = []
    for sid in dict.fromkeys(req.sprite_ids):
        try:
            sp = sprite_store.get_sprite(sid)
        except Exception as exc:
            raise CatalogError('待归类精灵已不存在，请刷新') from exc
        library_catalog.ownership(req.project_id, req.sprite_type_id, sp.get('template_binding'))
        rows.append({'id': sid, 'name': sp['name'], 'from_project_id': sp.get('project_id'),
                     'from_type_id': sp.get('sprite_type_id'),
                     'revision': hashlib.sha256(sprite_store.sprite_json(sid).read_bytes()).hexdigest()})
    return {'items': rows, 'project_id': req.project_id, 'sprite_type_id': req.sprite_type_id,
            'preserves': ['素材路径', '参考绑定', '立绘标记', '原标签', '动作与工艺配置']}


@router.post('/assignment/preview')
def preview_assignment(req: Assignment):
    with library_catalog.lock, sprite_store._lock:
        return _preview(req)


@router.post('/assignment/apply')
def apply_assignment(req: Assignment):
    with library_catalog.lock, sprite_store._lock:
        preview = _preview(req)
        if not req.revisions or any(req.revisions.get(r['id']) != r['revision'] for r in preview['items']):
            raise CatalogError('精灵档案已变化或缺少预览，请重新预览后归类')
        original = {r['id']: sprite_store.get_sprite(r['id']) for r in preview['items']}
        try:
            for sid in original:
                sprite_store.update_sprite(sid, {'project_id': req.project_id,
                                                'sprite_type_id': req.sprite_type_id})
        except Exception:
            for sid, data in original.items():
                sprite_store._write_json(sprite_store.sprite_json(sid), data)
            raise
        return {'assigned': len(original)}
