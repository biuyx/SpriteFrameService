"""Same-key template families stay separate; no real generation requests."""
import io
import uuid

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.core import pipeline
from app.core.first_frame_generator import resolve_refs
from app.services.ffset_store import ffset_store
from app.services.sprite_store import sprite_store
from app.services.template_store import template_store
from app.services.template_bindings import TemplateBindingError, select_first_frame_set


def png(color):
    out = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(out, format='PNG')
    return out.getvalue()


@pytest.fixture
def families(tmp_path):
    groups = ['shop-' + uuid.uuid4().hex, 'npc-' + uuid.uuid4().hex]
    sets, templates = [], []
    for index, group in enumerate(groups):
        directory = tmp_path / str(index)
        directory.mkdir()
        (directory / 'idle.png').write_bytes(png('red' if index == 0 else 'blue'))
        sets.append(ffset_store.import_dir(directory, group, group))
        templates.append(template_store.add_video(f'idle({group}).mp4', b'video', group))
    roles = []
    for index in range(2):
        sp = sprite_store.create_sprite('role-' + uuid.uuid4().hex)
        action = sprite_store.create_action(sp['id'], 'idle')
        sprite_store.update_action(sp['id'], action['id'], {'template_id': templates[index]['id']})
        art = sprite_store.add_ref(sp['id'], 'art.png', png('green' if index == 0 else 'white'))
        sprite_store.update_ref(sp['id'], art['id'], {'role': 'front'})
        binding = {'group': groups[index], 'first_frame_set_id': sets[index]['id']}
        sprite_store.update_sprite(sp['id'], {'template_binding': binding})
        roles.append((sp, sprite_store.get_action(sp['id'], action['id'])))
    return groups, sets, templates, roles


def test_two_roles_with_same_key_use_own_set_and_portrait(families):
    _, sets, _, roles = families
    results = []
    for index, (sp, action) in enumerate(roles):
        chosen = pipeline._pick_ffset(action, 'idle', None)
        assert chosen['id'] == sets[index]['id']
        art, pose, _, _ = resolve_refs(sp['id'], action, chosen['id'])
        assert pose == ffset_store.frame_path(sets[index]['id'], 'idle').read_bytes()
        results.append((art, pose))
    assert results[0][0] != results[1][0] and results[0][1] != results[1][1]


def test_unbound_automatic_selection_never_falls_back(families):
    _, sets, _, roles = families
    sp, action = roles[0]
    sprite_store.update_sprite(sp['id'], {'template_binding': {}})
    with pytest.raises(TemplateBindingError, match='禁止自动选择'):
        pipeline._pick_ffset(action, 'idle', None)
    assert pipeline._pick_ffset(action, 'idle', sets[0]['id'])['id'] == sets[0]['id']
    with pytest.raises(TemplateBindingError, match='分组不一致'):
        resolve_refs(sp['id'], action, sets[1]['id'])


def test_bound_role_rejects_explicit_override_and_deleted_set(families):
    _, sets, _, roles = families
    sp, action = roles[0]
    with pytest.raises(TemplateBindingError, match='绑定不一致'):
        resolve_refs(sp['id'], action, sets[1]['id'])
    ffset_store.delete(sets[0]['id'])
    with pytest.raises(TemplateBindingError, match='已删除'):
        pipeline.check_firstframe(sp['id'], action, {})


def test_missing_key_does_not_borrow_from_other_group(families):
    _, sets, _, roles = families
    sp, action = roles[0]
    assert ffset_store.frame_path(sets[1]['id'], 'idle').is_file()
    record = ffset_store.get(sets[0]['id'])
    record['frames'] = []
    ffset_store._write(record)
    with pytest.raises(TemplateBindingError, match='不会从其他分组'):
        select_first_frame_set(sp['id'], action, 'idle')


def test_bound_set_group_changes_are_detected(families):
    groups, sets, _, roles = families
    sp, action = roles[0]
    record = ffset_store.get(sets[0]['id'])
    record['group'] = groups[1]
    ffset_store._write(record)
    with pytest.raises(TemplateBindingError, match='分组已变化'):
        resolve_refs(sp['id'], action, sets[0]['id'])


def test_cross_group_action_needs_explicit_shared_permission(families):
    groups, sets, templates, roles = families
    sp, action = roles[0]
    with pytest.raises(TemplateBindingError, match='明确授权'):
        sprite_store.update_action(sp['id'], action['id'], {'template_id': templates[1]['id']})
    sprite_store.update_sprite(sp['id'], {'template_binding': {
        'group': groups[0], 'first_frame_set_id': sets[0]['id'], 'shared_template_ids': [templates[1]['id']],
    }})
    action = sprite_store.update_action(sp['id'], action['id'], {'template_id': templates[1]['id']})
    # Intentional shared video still uses this role's own pose set and portrait.
    assert resolve_refs(sp['id'], action, sets[0]['id'])[1] == ffset_store.frame_path(sets[0]['id'], 'idle').read_bytes()
    with pytest.raises(TemplateBindingError, match='明确授权'):
        sprite_store.update_sprite(sp['id'], {'template_binding': {
            'group': groups[0], 'first_frame_set_id': sets[0]['id'], 'shared_template_ids': [],
        }})


def test_invalid_binding_is_400_and_leaves_role_unchanged(families):
    from app.main import app
    groups, sets, _, roles = families
    sp, _ = roles[0]
    before = sprite_store.get_sprite(sp['id'])['template_binding']
    with TestClient(app) as client:
        response = client.patch(f"/api/sprites/{sp['id']}", json={'template_binding': {
            'group': groups[0], 'first_frame_set_id': sets[1]['id'],
        }})
        assert response.status_code == 400
    assert sprite_store.get_sprite(sp['id'])['template_binding'] == before


def test_bound_role_without_set_blocks_firstframe_but_keeps_existing_assets(families):
    groups, _, _, roles = families
    sp, action = roles[0]
    existing = sprite_store.action_dir(sp['id'], action['id']) / 'first_frame.png'
    existing.write_bytes(png('yellow'))
    sprite_store.update_sprite(sp['id'], {'template_binding': {'group': groups[0]}})
    assert pipeline.plan_action(sp['id'], action, {'steps': ['firstframe']})['steps']['firstframe']['status'] == 'skip'
    forced = pipeline.plan_action(sp['id'], action, {'steps': ['firstframe'], 'force': ['firstframe']})
    assert forced['steps']['firstframe']['status'] == 'blocked'
    assert existing.read_bytes() == png('yellow')


def test_http_single_and_batch_firstframe_cannot_bypass_role_binding(families, monkeypatch):
    from app.main import app
    from app.services.job_manager import job_manager
    _, sets, _, roles = families
    sp, action = roles[0]
    monkeypatch.setattr(job_manager, 'submit', lambda *a, **k: pytest.fail('must not submit paid work'))
    with TestClient(app) as client:
        response = client.post(f"/api/sprites/{sp['id']}/actions/{action['id']}/gen-first-frame",
                               json={'set_id': sets[1]['id']})
        assert response.status_code == 400
        response = client.post(f"/api/sprites/{sp['id']}/batch-gen-first-frames",
                               json={'action_ids': [action['id']], 'set_id': sets[1]['id']})
        assert response.status_code == 200
        assert response.json()['submitted'] == []
        assert len(response.json()['skipped']) == 1


def test_video_runtime_rechecks_reference_group_before_provider(families, monkeypatch):
    from app.api.deps import get_session
    from app.core.video_generator import run_generate
    from app.core.ark_client import ArkClient
    from conftest import FakeCtx
    groups, _, templates, roles = families
    sp, action = roles[0]
    monkeypatch.setattr(ArkClient, 'create_task', lambda *a, **k: pytest.fail('must not call provider'))
    with pytest.raises(TemplateBindingError):
        run_generate(get_session(action['id']), {'template_id': templates[1]['id']}, FakeCtx())
    # Mutating a formerly allowed template's group is also caught at runtime.
    template_store.update(templates[0]['id'], {'group': groups[1]})
    with pytest.raises(TemplateBindingError):
        run_generate(get_session(action['id']), {'template_id': templates[0]['id']}, FakeCtx())


def test_template_role_fork_preserves_binding_not_character_images_by_default(families):
    from app.main import app
    _, _, _, roles = families
    sp, action = roles[0]
    (sprite_store.action_dir(sp['id'], action['id']) / 'first_frame.png').write_bytes(png('yellow'))
    with TestClient(app) as client:
        response = client.post('/api/sprites', json={'name': 'new-character', 'from_sprite_id': sp['id']})
        assert response.status_code == 200
        clone = response.json()
    assert clone['template_binding'] == sprite_store.get_sprite(sp['id'])['template_binding']
    assert clone['copied']['first_frames'] == clone['copied']['refs'] == 0
    copied_action = sprite_store.list_actions(clone['id'])[0]
    assert copied_action['id'] != action['id']
    assert not (sprite_store.action_dir(clone['id'], copied_action['id']) / 'first_frame.png').exists()


def test_directory_import_does_not_pick_first_template_with_ambiguous_key(families, tmp_path):
    from app.api.sprites import _run_batch_import, BatchImportRequest
    from conftest import FakeCtx
    directory = tmp_path / 'import-character'
    directory.mkdir()
    (directory / 'idle.png').write_bytes(png('yellow'))
    result = _run_batch_import(BatchImportRequest(frames_dir=str(tmp_path), sprites=[
        {'dir_name': directory.name, 'name': 'import-' + uuid.uuid4().hex, 'actions': ['idle']},
    ]), FakeCtx())
    assert result['actions_created'] == 1
    assert any('不唯一' in e for e in result['errors'])


def test_group_import_rejects_explicit_foreign_template(families, tmp_path):
    from app.api.sprites import _run_batch_import, BatchImportRequest
    from conftest import FakeCtx
    groups, _, templates, _ = families
    directory = tmp_path / 'import-character'
    directory.mkdir()
    result = _run_batch_import(BatchImportRequest(frames_dir=str(tmp_path), template_group=groups[0], sprites=[
        {'dir_name': directory.name, 'name': 'import-' + uuid.uuid4().hex,
         'actions': [{'name': 'idle', 'key': 'idle', 'template_id': templates[1]['id']}]},
    ]), FakeCtx())
    assert result['actions_created'] == 0
    assert any('不属于' in e for e in result['errors'])


def test_queued_free_extract_rechecks_snapshot_reference_permission(families, monkeypatch):
    from types import SimpleNamespace
    from app.api.deps import get_session
    from app.api import frames
    from app.mcp_server import operations
    from conftest import FakeCtx
    groups, sets, templates, roles = families
    sp, action = roles[0]
    sprite_store.update_sprite(sp['id'], {'template_binding': {
        'group': groups[0], 'first_frame_set_id': sets[0]['id'], 'shared_template_ids': [templates[1]['id']],
    }})
    sprite_store.update_action(sp['id'], action['id'], {'template_id': templates[1]['id']})
    template_store.update(templates[1]['id'], {'extract_rule': {'start': 0, 'end': .5, 'fps': 2}})
    session = get_session(action['id'])
    session.video_info = SimpleNamespace(duration=1, width=8, height=8)
    plan = operations.plan_processing(sp['id'], [action['id']], ['extract'])
    assert plan['actions'][0]['plan']['steps']['extract']['status'] == 'run'
    sprite_store.update_action(sp['id'], action['id'], {'template_id': templates[0]['id']})
    sprite_store.update_sprite(sp['id'], {'template_binding': {'group': groups[0], 'first_frame_set_id': sets[0]['id']}})
    monkeypatch.setattr(frames, 'run_extract', lambda *a: pytest.fail('revoked reference must not run'))
    with pytest.raises(TemplateBindingError, match='明确授权'):
        operations._run_batch(plan, FakeCtx())
