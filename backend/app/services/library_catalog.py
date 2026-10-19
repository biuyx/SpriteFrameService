"""Project/type taxonomy. IDs are stable; organization never moves asset files."""
from __future__ import annotations

import json
import threading
import time
import uuid

from app.config import get_settings


class CatalogError(ValueError):
    pass


class LibraryCatalog:
    def __init__(self, path=None):
        self._path = path
        self.lock = threading.RLock()

    @property
    def path(self):
        return self._path or get_settings().resolved_data_dir / 'library_catalog.json'

    def read(self):
        with self.lock:
            if not self.path.exists():
                return {'version': 1, 'projects': [], 'categories': [], 'types': []}
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if data.get('version') != 1 or any(not isinstance(data.get(k), list) for k in ('projects', 'categories', 'types')):
                    raise ValueError('unknown catalog schema')
                for kind in ('projects', 'categories', 'types'):
                    ids = set()
                    for row in data[kind]:
                        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                                or not isinstance(row.get('name'), str) or row['id'] in ids
                                or (kind != 'projects' and not isinstance(row.get('project_id'), str))):
                            raise ValueError('invalid catalog record')
                        ids.add(row['id'])
                return data
            except (ValueError, OSError) as exc:
                raise CatalogError('分类数据无法读取，请恢复备份；不会覆盖原文件') from exc

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.json.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    @staticmethod
    def find(data, kind, rid):
        record = next((r for r in data[kind] if r['id'] == rid), None)
        if record is None:
            raise CatalogError('项目、分类或精灵类型不存在，请刷新后重试')
        return record

    @staticmethod
    def _binding(value):
        if not value:
            return {}
        from app.services.template_bindings import TemplateBinding
        from app.services.ffset_store import ffset_store
        from app.services.template_store import template_store
        try:
            b = TemplateBinding(**value).model_dump()
        except ValueError as exc:
            raise CatalogError('类型默认参考绑定格式无效') from exc
        b['group'] = b['group'].strip()
        if not b['group']:
            raise CatalogError('默认参考分组不能为空')
        b['shared_template_ids'] = sorted(set(b['shared_template_ids']))
        if b['first_frame_set_id']:
            fs = ffset_store.get(b['first_frame_set_id'])
            if not fs or (fs.get('group') or '').strip() != b['group']:
                raise CatalogError('类型默认首帧集必须存在且属于所选分组')
        if any(template_store.get(tid) is None for tid in b['shared_template_ids']):
            raise CatalogError('共享参考动作不存在')
        return b

    def save(self, kind, patch, rid=None):
        if kind not in ('projects', 'categories', 'types'):
            raise CatalogError('未知分类对象')
        with self.lock:
            data = self.read()
            old = self.find(data, kind, rid) if rid else None
            record = {**(old or {}), **patch}
            record['name'] = str(record.get('name') or '').strip()
            if not record['name'] or len(record['name']) > 80:
                raise CatalogError('名称需为 1 到 80 个字符')
            if old and kind != 'projects' and record.get('project_id') != old['project_id']:
                raise CatalogError('分类和类型不能跨项目移动，请在目标项目新建')
            if kind != 'projects':
                self.find(data, 'projects', record.get('project_id'))
                parent = record.get('parent_id') if kind == 'categories' else record.get('category_id')
                if parent:
                    category = self.find(data, 'categories', parent)
                    if category['project_id'] != record['project_id']:
                        raise CatalogError('父分类必须属于同一项目')
                if kind == 'categories':
                    record['parent_id'] = parent or None
                    visited = {rid} if rid else set()
                    while parent:
                        if parent in visited:
                            raise CatalogError('分类不能移入自身或子分类')
                        visited.add(parent)
                        parent = self.find(data, 'categories', parent).get('parent_id')
                else:
                    record['category_id'] = parent or None
                    record['template_binding'] = self._binding(record.get('template_binding'))
                    # A changed default affects only future roles; prohibit a group
                    # change on populated types to keep current roles compatible.
                    if old and (old.get('template_binding') or {}).get('group') != (record.get('template_binding') or {}).get('group'):
                        from app.services.sprite_store import sprite_store
                        linked = [s for s in sprite_store.list_sprites() if s.get('sprite_type_id') == rid]
                        new_group = (record.get('template_binding') or {}).get('group')
                        if linked and ((old.get('template_binding') or {}).get('group') or any(
                                s.get('template_binding') and s['template_binding'].get('group') != new_group for s in linked)):
                            raise CatalogError('类型已有精灵，不能直接更换参考分组；请新建类型')
            parent_field = {'projects': None, 'categories': 'parent_id', 'types': 'category_id'}[kind]
            if any(r['id'] != rid and r['name'].casefold() == record['name'].casefold()
                   and (kind == 'projects' or r['project_id'] == record['project_id'])
                   and (not parent_field or r.get(parent_field) == record.get(parent_field)) for r in data[kind]):
                raise CatalogError('同一位置已有同名条目')
            if not old:
                record['id'] = {'projects': 'pr_', 'categories': 'cat_', 'types': 'st_'}[kind] + uuid.uuid4().hex[:12]
                record['created_at'] = time.time()
                data[kind].append(record)
            else:
                old.update(record)
            self._write(data)
            return record

    def delete(self, kind, rid):
        with self.lock:
            data = self.read()
            self.find(data, kind, rid)
            from app.services.sprite_store import sprite_store
            sprites = sprite_store.list_sprites()
            if kind == 'projects':
                used = any(r['project_id'] == rid for k in ('categories', 'types') for r in data[k]) or any(s.get('project_id') == rid for s in sprites)
            elif kind == 'categories':
                used = any(r.get('parent_id') == rid for r in data['categories']) or any(r.get('category_id') == rid for r in data['types'])
            else:
                used = any(s.get('sprite_type_id') == rid for s in sprites)
            if used:
                raise CatalogError('条目仍被使用，请先移出精灵、类型或子分类')
            data[kind] = [r for r in data[kind] if r['id'] != rid]
            self._write(data)

    def ownership(self, project_id, sprite_type_id, binding=None):
        """Validate membership without guessing from labels or changing bindings."""
        data = self.read()
        if not project_id:
            if sprite_type_id:
                raise CatalogError('选择精灵类型前须选择项目')
            return None
        self.find(data, 'projects', project_id)
        if sprite_type_id:
            typ = self.find(data, 'types', sprite_type_id)
            if typ['project_id'] != project_id:
                raise CatalogError('精灵类型不属于所选项目')
            group = (typ.get('template_binding') or {}).get('group')
            if group and binding and (binding.get('group') or '').strip() != group:
                raise CatalogError('角色参考分组与类型不一致，请先明确调整模板绑定或选择其他类型')
            return typ
        return None


library_catalog = LibraryCatalog()
