"""Taxonomy must not rewrite old assets or silently retarget references."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.services.library_catalog import CatalogError, library_catalog as catalog
from app.services.sprite_store import sprite_store as store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, '_path', tmp_path / 'library_catalog.json')
    monkeypatch.setattr(store, '_root', tmp_path / 'sprites')
    monkeypatch.setattr(store, '_action_index', {})
    monkeypatch.setattr(store, '_scanned', False)


@pytest.fixture
def client():
    from app.main import create_app
    return TestClient(create_app())


def project(name='Project A'):
    return catalog.save('projects', {'name': name})


def typ(p, name='Shop', **kwargs):
    return catalog.save('types', {'project_id': p['id'], 'name': name, **kwargs})


def category(p, name, parent=None):
    return catalog.save('categories', {'project_id': p['id'], 'name': name, 'parent_id': parent})


def test_legacy_reads_do_not_migrate_or_write(client):
    sp = store.create_sprite('Legacy', ['OldProject', 'NPC'])
    path = store.sprite_json(sp['id'])
    data = json.loads(path.read_text(encoding='utf-8'))
    data.pop('project_id'); data.pop('sprite_type_id')
    store._write_json(path, data)
    before = path.read_bytes()
    assert client.get('/api/library').json()['projects'] == []
    assert not catalog.path.exists()
    assert client.get('/api/sprites').json()['sprites'][0]['tags'] == ['OldProject', 'NPC']
    assert client.get('/api/sprites/' + sp['id']).json().get('project_id') is None
    assert path.read_bytes() == before


def test_nested_categories_rename_and_move_keep_ids_and_assets():
    p = project(); a = category(p, 'People'); b = category(p, 'Workers', a['id'])
    t = typ(p, category_id=b['id']); sp = store.create_sprite('Shop 1', project_id=p['id'], sprite_type_id=t['id'])
    action = store.create_action(sp['id'], 'idle')
    asset = store.action_dir(sp['id'], action['id']) / 'first_frame.png'; asset.write_bytes(b'original')
    before = store.sprite_json(sp['id']).read_bytes()
    catalog.save('categories', {'name': 'Employees', 'parent_id': None}, b['id'])
    catalog.save('projects', {'name': 'Renamed'}, p['id'])
    assert store.sprite_json(sp['id']).read_bytes() == before
    assert asset.read_bytes() == b'original'
    assert catalog.read()['types'][0]['id'] == t['id']


@pytest.mark.parametrize('target', ['self', 'descendant'])
def test_category_cycle_rejected_without_writing(target):
    p = project(); a = category(p, 'A'); b = category(p, 'B', a['id'])
    before = catalog.path.read_bytes()
    with pytest.raises(CatalogError, match='自身或子分类'):
        catalog.save('categories', {'parent_id': a['id'] if target == 'self' else b['id']}, a['id'])
    assert catalog.path.read_bytes() == before


def test_cross_project_parent_type_and_move_are_rejected():
    a = project(); b = project('Project B'); ca = category(a, 'People'); t = typ(a)
    with pytest.raises(CatalogError, match='同一项目'):
        category(b, 'Nested', ca['id'])
    with pytest.raises(CatalogError, match='跨项目'):
        catalog.save('types', {'project_id': b['id']}, t['id'])
    before = store.list_sprites()
    with pytest.raises(CatalogError, match='不属于'):
        store.create_sprite('Invalid', project_id=b['id'], sprite_type_id=t['id'])
    assert store.list_sprites() == before


def test_sibling_duplicate_names_but_distinct_parent_names_allowed():
    p = project(); a = category(p, 'A'); b = category(p, 'B')
    category(p, 'Children', a['id']); category(p, 'Children', b['id'])
    with pytest.raises(CatalogError, match='同名'):
        category(p, 'children', a['id'])


@pytest.mark.parametrize('kind', ['projects', 'categories', 'types'])
def test_delete_in_use_nodes_is_blocked(kind):
    p = project(); c = category(p, 'People'); t = typ(p, category_id=c['id'])
    sp = store.create_sprite('Existing', project_id=p['id'], sprite_type_id=t['id'])
    rid = {'projects': p['id'], 'categories': c['id'], 'types': t['id']}[kind]
    with pytest.raises(CatalogError, match='仍被使用'):
        catalog.delete(kind, rid)
    assert store.get_sprite(sp['id'])['sprite_type_id'] == t['id']


def test_delete_empty_nodes_child_first():
    p = project(); c = category(p, 'A'); t = typ(p, category_id=c['id'])
    catalog.delete('types', t['id']); catalog.delete('categories', c['id']); catalog.delete('projects', p['id'])
    assert not catalog.read()['projects']


def test_new_roles_take_independent_defaults_existing_roles_unchanged():
    p = project(); t = typ(p, template_binding={'group': 'Shop'})
    sp = store.create_sprite('New', project_id=p['id'], sprite_type_id=t['id'])
    before = store.sprite_json(sp['id']).read_bytes()
    catalog.save('types', {'name': 'Shop Renamed', 'template_binding': {'group': 'Shop', 'shared_template_ids': []}}, t['id'])
    assert store.sprite_json(sp['id']).read_bytes() == before
    assert store.get_sprite(sp['id'])['template_binding']['group'] == 'Shop'
    with pytest.raises(CatalogError, match='已有精灵'):
        catalog.save('types', {'template_binding': {'group': 'NPC'}}, t['id'])


def test_invalid_default_set_never_saved():
    p = project(); before = catalog.path.read_bytes()
    with pytest.raises(CatalogError, match='首帧集'):
        typ(p, template_binding={'group': 'Shop', 'first_frame_set_id': 'missing'})
    assert catalog.path.read_bytes() == before


def test_classification_does_not_apply_defaults_and_is_previewed(client):
    p = project(); t = typ(p, template_binding={'group': 'Shop'})
    sp = store.create_sprite('Legacy', ['Old tag']); a = store.create_action(sp['id'], 'idle')
    asset = store.action_dir(sp['id'], a['id']) / 'first_frame.png'; asset.write_bytes(b'original')
    payload = {'sprite_ids': [sp['id']], 'project_id': p['id'], 'sprite_type_id': t['id']}
    before = store.sprite_json(sp['id']).read_bytes()
    preview = client.post('/api/library/assignment/preview', json=payload).json()
    assert store.sprite_json(sp['id']).read_bytes() == before
    assert client.post('/api/library/assignment/apply', json=payload).status_code == 400
    payload['revisions'] = {r['id']: r['revision'] for r in preview['items']}
    assert client.post('/api/library/assignment/apply', json=payload).json()['assigned'] == 1
    result = store.get_sprite(sp['id'])
    assert result['tags'] == ['Old tag'] and not result.get('template_binding')
    assert result['sprite_type_id'] == t['id'] and asset.read_bytes() == b'original'


def test_assignment_revalidates_stale_preview(client):
    p = project(); sp = store.create_sprite('Legacy')
    body = {'sprite_ids': [sp['id']], 'project_id': p['id']}
    row = client.post('/api/library/assignment/preview', json=body).json()['items'][0]
    store.update_sprite(sp['id'], {'name': 'Changed'})
    r = client.post('/api/library/assignment/apply', json={**body, 'revisions': {sp['id']: row['revision']}})
    assert r.status_code == 400 and not store.get_sprite(sp['id']).get('project_id')


def test_batch_conflict_is_all_or_nothing(client):
    p = project(); t = typ(p, template_binding={'group': 'Shop'})
    a = store.create_sprite('Unbound'); b = store.create_sprite('NPC', template_binding={'group': 'NPC'})
    r = client.post('/api/library/assignment/preview', json={'sprite_ids': [a['id'], b['id']], 'project_id': p['id'], 'sprite_type_id': t['id']})
    assert r.status_code == 400
    assert all(not sp.get('project_id') for sp in store.list_sprites())


def test_assignment_transient_write_failure_rolls_back(client, monkeypatch):
    p = project(); a = store.create_sprite('A'); b = store.create_sprite('B')
    payload = {'sprite_ids': [a['id'], b['id']], 'project_id': p['id']}
    rows = client.post('/api/library/assignment/preview', json=payload).json()['items']
    originals = {sp['id']: store.get_sprite(sp['id']) for sp in (a, b)}
    original_write = store._write_json; calls = []
    def failing_write(path, data):
        calls.append(path)
        if len(calls) == 2: raise OSError('transient disk error')
        original_write(path, data)
    monkeypatch.setattr(store, '_write_json', failing_write)
    from app.api.library import apply_assignment, Assignment
    with pytest.raises(OSError):
        apply_assignment(Assignment(**payload, revisions={r['id']: r['revision'] for r in rows}))
    assert all(store.get_sprite(sid) == data for sid, data in originals.items())


def test_clear_ownership_explicit_null_and_preserve_reference(client):
    p = project(); t = typ(p, template_binding={'group': 'Shop'})
    sp = store.create_sprite('Role', project_id=p['id'], sprite_type_id=t['id'])
    r = client.patch('/api/sprites/' + sp['id'], json={'project_id': None, 'sprite_type_id': None})
    assert r.status_code == 200 and r.json()['project_id'] is None
    assert r.json()['template_binding']['group'] == 'Shop'


def test_copy_inherits_membership_and_target_type_conflict_leaves_no_orphan():
    p = project(); t = typ(p, template_binding={'group': 'Shop'})
    sp = store.create_sprite('Role', project_id=p['id'], sprite_type_id=t['id'])
    copied = store.fork_sprite(sp['id'], 'Copy')['sprite']
    assert copied['project_id'] == p['id'] and copied['sprite_type_id'] == t['id']
    npc = typ(p, name='NPC', template_binding={'group': 'NPC'})
    before = len(store.list_sprites())
    with pytest.raises(CatalogError, match='分组与类型不一致'):
        store.fork_sprite(sp['id'], 'Bad copy', ownership={'project_id': p['id'], 'sprite_type_id': npc['id']})
    assert len(store.list_sprites()) == before


def test_same_name_import_is_scoped_to_project_and_type(tmp_path, ctx):
    from app.api.sprites import BatchImportRequest, _run_batch_import
    a = project(); b = project('Project B'); ta = typ(a); tb = typ(b)
    root = tmp_path / 'incoming'; (root / 'Same').mkdir(parents=True)
    from PIL import Image
    Image.new('RGB', (8, 8), 'red').save(root / 'Same' / 'idle.png')
    existing = store.create_sprite('Same', project_id=a['id'], sprite_type_id=ta['id'])
    req = BatchImportRequest(frames_dir=str(root), project_id=b['id'], sprite_type_id=tb['id'], sprites=[{'dir_name': 'Same', 'actions': ['idle']}])
    result = _run_batch_import(req, ctx)
    assert result['sprites_created'] == 1
    assert not store.get_sprite(existing['id'])['actions']
    other = next(s for s in store.list_sprites() if s['project_id'] == b['id'])
    assert other['actions'][0]['name'] == 'idle'


def test_corrupt_catalog_fails_closed_without_overwrite(client):
    catalog.path.write_text('{bad', encoding='utf-8')
    assert client.get('/api/library').status_code == 400
    assert client.post('/api/library/projects', json={'name': 'A'}).status_code == 400
    assert catalog.path.read_text() == '{bad'


def test_crud_http_validation_and_scope(client):
    assert client.post('/api/library/projects', json={'name': ' '}).status_code == 400
    assert client.post('/api/library/projects', json={'name': 'A', 'unknown': True}).status_code == 422
    p = client.post('/api/library/projects', json={'name': 'A'}).json()
    c = client.post('/api/library/categories', json={'name': 'People', 'project_id': p['id']}).json()
    t = client.post('/api/library/types', json={'name': 'Shop', 'project_id': p['id'], 'category_id': c['id']}).json()
    role = client.post('/api/sprites', json={'name': 'Role', 'project_id': p['id'], 'sprite_type_id': t['id']})
    assert role.status_code == 200 and role.json()['sprite_type_id'] == t['id']
    assert client.delete('/api/library/types/' + t['id']).status_code == 400


def test_catalog_admin_endpoints_require_authentication(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'auth_token', 'catalog-test-token')
    for method, path, body in [
        ('GET', '/api/library', None),
        ('POST', '/api/library/projects', {'name': 'A'}),
        ('POST', '/api/library/assignment/preview', {'sprite_ids': ['missing']}),
    ]:
        assert client.request(method, path, json=body).status_code == 401
    assert client.get('/api/library', headers={'Authorization': 'Bearer catalog-test-token'}).status_code == 200


def test_schema_corruption_is_not_treated_as_an_empty_library():
    catalog.path.write_text(json.dumps({'version': 1, 'projects': [{}], 'categories': [], 'types': []}), encoding='utf-8')
    with pytest.raises(CatalogError, match='无法读取'):
        catalog.read()


def test_first_default_configuration_of_populated_unbound_type_is_safe():
    p = project(); t = typ(p)
    sp = store.create_sprite('Already classified', project_id=p['id'], sprite_type_id=t['id'])
    catalog.save('types', {'template_binding': {'group': 'Shop'}}, t['id'])
    assert not store.get_sprite(sp['id']).get('template_binding')
    new = store.create_sprite('Next', project_id=p['id'], sprite_type_id=t['id'])
    assert new['template_binding']['group'] == 'Shop'


def test_null_name_patch_keeps_legacy_behavior(client):
    sp = store.create_sprite('Name')
    assert client.patch('/api/sprites/' + sp['id'], json={'name': None}).json()['name'] == 'Name'
