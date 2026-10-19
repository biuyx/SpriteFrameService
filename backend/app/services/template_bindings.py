"""Role-specific template contracts. Never pick another group's first frame."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TemplateBindingError(ValueError):
    pass


class TemplateBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    group: str = Field(min_length=1, max_length=120)
    first_frame_set_id: str | None = None
    shared_template_ids: list[str] = Field(default_factory=list, max_length=200)


def validate_reference(sprite_id, template_id, binding=None):
    from app.services.sprite_store import sprite_store
    from app.services.template_store import template_store
    if not template_id:
        return None
    template = template_store.get(template_id)
    if template is None:
        raise TemplateBindingError('绑定的参考动作不存在，请重新选择')
    if binding is None:
        binding = sprite_store.get_sprite(sprite_id).get('template_binding') or {}
    if binding and ((template.get('group') or '').strip() != binding['group']
                    and template_id not in binding.get('shared_template_ids', [])):
        raise TemplateBindingError('参考动作不属于角色模板分组；如需复用，请在「模板绑定」中明确授权共享参考')
    return template


def normalize_binding(sprite_id, value):
    from app.services.sprite_store import sprite_store
    from app.services.ffset_store import ffset_store
    if not value:
        return {}
    try:
        data = TemplateBinding(**value).model_dump()
    except ValueError as exc:
        raise TemplateBindingError(f'模板绑定配置无效：{exc}') from exc
    data['group'] = data['group'].strip()
    if not data['group']:
        raise TemplateBindingError('请选择角色模板分组')
    data['shared_template_ids'] = sorted(set(data['shared_template_ids']))
    fsid = data['first_frame_set_id']
    if fsid:
        fs = ffset_store.get(fsid)
        if not fs or (fs.get('group') or '').strip() != data['group']:
            raise TemplateBindingError('参考首帧集必须存在且属于角色绑定分组；请归档或导入该组的参考首帧集')
    for tid in data['shared_template_ids']:
        validate_reference(sprite_id, tid, data)
    for action in sprite_store.list_actions(sprite_id):
        try:
            validate_reference(sprite_id, action.get('template_id'), data)
        except TemplateBindingError as exc:
            raise TemplateBindingError(f"动作「{action['name']}」：{exc}") from exc
    return data


def select_first_frame_set(sprite_id, action, key, requested=None):
    from app.services.sprite_store import sprite_store
    from app.services.ffset_store import ffset_store
    binding = sprite_store.get_sprite(sprite_id).get('template_binding') or {}
    template = validate_reference(sprite_id, action.get('template_id'), binding)
    if binding:
        bound = binding.get('first_frame_set_id')
        if not bound:
            raise TemplateBindingError('角色尚未绑定参考首帧集，请在角色「模板绑定」中选择')
        if requested and requested != bound:
            raise TemplateBindingError('所选首帧集与角色绑定不一致，请先修改角色模板绑定')
        selected = bound
    else:
        # Legacy manual choices remain explicit; unattended workflows must bind.
        if not requested:
            raise TemplateBindingError('角色尚未配置模板绑定，禁止自动选择参考首帧集')
        selected = requested
    fs = ffset_store.get(selected)
    if not fs:
        raise TemplateBindingError('绑定的参考首帧集已删除，请重新绑定')
    group = (fs.get('group') or '').strip()
    if binding:
        if group != binding['group']:
            raise TemplateBindingError('参考首帧集分组已变化，与角色绑定不一致')
    elif template and (template.get('group') or '').strip() and group != template['group'].strip():
        raise TemplateBindingError('参考首帧集与动作模板分组不一致，请先配置角色模板绑定')
    if not any(f['key'] == key for f in fs.get('frames', [])):
        raise TemplateBindingError(f'角色绑定的参考首帧集缺少动作 {key}；不会从其他分组补取')
    return fs


def validate_video_reference(session_id, template_id):
    from app.services.sprite_store import sprite_store
    sid = sprite_store.sprite_of_action(session_id)
    if sid:
        return validate_reference(sid, template_id)
