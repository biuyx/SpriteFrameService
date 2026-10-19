"""真实小图导出、批次幂等与 HTTP 权限；不联网、不调计费接口。"""
import hashlib
import json
import time
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.mcp_server import operations, queries
from app.models.frame_data import FrameData
from app.services.job_manager import JobManager, JobSubmissionError
from app.services.sprite_store import sprite_store


@pytest.fixture
def exportable():
    sp = sprite_store.create_sprite('mcp_phase2_' + uuid.uuid4().hex[:8])
    action = sprite_store.create_action(sp['id'], 'walk')
    session = get_session(action['id'])
    image = np.full((32, 32, 4), 255, np.uint8)
    frame = FrameData(index=0, timestamp=0, image=image)
    session.frame_store.save_raw_frames([frame])
    session.frame_manager.add_frames([frame])
    session.save_processed(0, image)
    session.persist_metadata()
    return sp, action, session


def wait_batch(manager, batch_id):
    deadline = time.monotonic() + 10
    while manager.get(batch_id).finished_at is None and time.monotonic() < deadline:
        time.sleep(.02)
    assert manager.get(batch_id).finished_at is not None


def test_real_export_persists_batch_and_duplicate_does_not_reexport(exportable, tmp_path, monkeypatch):
    from app.core.ark_client import ArkClient
    monkeypatch.setattr(ArkClient, 'create_task', lambda *a, **kw: pytest.fail('不能调用付费接口'))
    sp, action, session = exportable
    manager = JobManager(storage_path=tmp_path / 'jobs.db')
    monkeypatch.setattr(operations, 'job_manager', manager)
    original = session.frame_store.proc_path(session.frame_manager.frames[0].id)
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    plan = operations.plan_processing(sp['id'], [action['id']], ['export'])
    assert plan['paid_calls'] == 0
    assert plan['actions'][0]['plan']['steps']['export']['status'] == 'run'
    batch = operations.submit_processing(sp['id'], [action['id']], ['export'], 'request-one')
    wait_batch(manager, batch['batch_id'])
    done = operations.batch_view(batch['batch_id'])
    assert done['status'] == 'done', done
    assert list(session.storage.exports_dir.rglob('*.png'))
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    manager.close()
    restored = JobManager(storage_path=tmp_path / 'jobs.db')
    monkeypatch.setattr(operations, 'job_manager', restored)
    same = operations.submit_processing(sp['id'], [action['id']], ['export'], 'request-one')
    assert same['batch_id'] == batch['batch_id'] and same['status'] == 'done'
    assert same['result'] == done['result']
    with pytest.raises(JobSubmissionError):
        operations.submit_processing(sp['id'], [action['id']], ['export'], 'request-one', force=True)
    restored.close()


def test_paid_steps_rejected_before_submission(exportable):
    sp, action, _ = exportable
    with pytest.raises(queries.ToolInputError, match='免费工序'):
        operations.submit_processing(sp['id'], [action['id']], ['generate'], 'paid-is-forbidden')


def test_blocked_batch_does_not_partially_submit(exportable, tmp_path, monkeypatch):
    sp, action, _ = exportable
    missing = sprite_store.create_action(sp['id'], 'no_video')
    manager = JobManager(storage_path=tmp_path / 'jobs.db')
    monkeypatch.setattr(operations, 'job_manager', manager)
    with pytest.raises(queries.ToolInputError, match='批次未提交'):
        operations.submit_processing(sp['id'], [action['id'], missing['id']], ['export'], 'blocked')
    assert manager.list() == []
    manager.close()


def test_http_read_key_cannot_operate_and_operate_can_export(exportable, monkeypatch):
    pytest.importorskip('mcp')
    from app.main import app
    from app.config import get_settings
    from app.services.agent_keys import agent_key_store
    sp, action, _ = exportable
    monkeypatch.setattr(get_settings(), 'auth_token', 'test-token')
    _, read = agent_key_store.create('read-' + uuid.uuid4().hex[:6], ['read'])
    _, operate = agent_key_store.create('operate-' + uuid.uuid4().hex[:6], ['read', 'operate'])
    args = {'sprite': sp['id'], 'actions': [action['id']], 'steps': ['export'], 'request_id': 'http-retry'}
    def rpc(client, key, tool, params):
        response = client.post('/api/mcp', headers={'Authorization': 'Bearer ' + key,
            'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                  'params': {'name': tool, 'arguments': params}})
        assert response.status_code == 200, response.text
        return response.json()['result']
    with TestClient(app) as client:
        denied = rpc(client, read, 'submit_processing_batch', args)
        assert denied['isError'] and 'operate' in denied['content'][0]['text']
        first = rpc(client, operate, 'submit_processing_batch', args)
        assert not first.get('isError'), first
        first_data = json.loads(first['content'][0]['text'])
        again = rpc(client, operate, 'submit_processing_batch', args)
        assert json.loads(again['content'][0]['text'])['batch_id'] == first_data['batch_id']
        wait_batch(operations.job_manager, first_data['batch_id'])


def test_operate_cannot_cancel_paid_job(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(operations.job_manager, 'get', lambda _: SimpleNamespace(type='generate'))
    with pytest.raises(queries.ToolInputError, match='付费生成'):
        operations.cancel_processing('paid')


def test_free_three_step_batch_uses_template_and_preset(tmp_path, monkeypatch):
    import cv2
    from app.services.template_store import template_store
    from app.core.background_remover import BackgroundRemover
    sp = sprite_store.create_sprite('three_steps_' + uuid.uuid4().hex[:6])
    action = sprite_store.create_action(sp['id'], 'walk')
    session = get_session(action['id'])
    video = session.storage.video_dir / 'upload.mp4'
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'mp4v'), 10, (32, 32))
    for i in range(10):
        writer.write(np.full((32, 32, 3), i * 20, np.uint8))
    writer.release()
    session.video_info = session.video_processor.load_video(str(video))
    template = template_store.add_video(uuid.uuid4().hex + '.mp4', video.read_bytes())
    template_store.update(template['id'], {'extract_rule': {'start': 0, 'end': .5, 'fps': 5}})
    sprite_store.update_action(sp['id'], action['id'], {'template_id': template['id']})
    monkeypatch.setattr(BackgroundRemover, 'get_model_path', lambda _: video)
    def remove(self, image, **kwargs):
        return np.dstack([image[:, :, :3], np.full(image.shape[:2], 255, np.uint8)])
    monkeypatch.setattr(BackgroundRemover, 'remove_background', remove)
    manager = JobManager(storage_path=tmp_path / 'jobs.db')
    monkeypatch.setattr(operations, 'job_manager', manager)
    steps = ['extract', 'matting', 'export']
    plan = operations.plan_processing(sp['id'], [action['id']], steps)
    assert all(row['status'] == 'run' for row in plan['actions'][0]['plan']['steps'].values())
    batch = operations.submit_processing(sp['id'], [action['id']], steps, 'three-steps')
    wait_batch(manager, batch['batch_id'])
    result = operations.batch_view(batch['batch_id'])
    assert result['status'] == 'done', result
    assert all(value['status'] == 'done' for value in result['result']['actions'][0]['steps'].values())
    assert all(f.has_processed for f in session.frame_manager.frames)
    assert not (session.storage.video_dir / 'takes.json').is_file(), '计划与免费加工不应迁移或提交付费素材版本'
    manager.close()


def test_failed_partial_export_is_not_skipped_on_retry(exportable, monkeypatch, tmp_path):
    from app.core.exporter import Exporter
    sp, action, session = exportable
    manager = JobManager(storage_path=tmp_path / 'jobs.db')
    monkeypatch.setattr(operations, 'job_manager', manager)
    def fail(self, frames, cfg):
        (cfg.output_path / 'partial.png').write_bytes(b'partial')
        raise RuntimeError('export interrupted')
    with monkeypatch.context() as patch:
        patch.setattr(Exporter, 'export', fail)
        batch = operations.submit_processing(sp['id'], [action['id']], ['export'], 'failed-export')
        wait_batch(manager, batch['batch_id'])
        assert operations.batch_view(batch['batch_id'])['status'] == 'error'
    assert session.storage.list_exports() == []
    plan = operations.plan_processing(sp['id'], [action['id']], ['export'])
    assert plan['actions'][0]['plan']['steps']['export']['status'] == 'run'
    retry = operations.submit_processing(sp['id'], [action['id']], ['export'], 'retry-export')
    wait_batch(manager, retry['batch_id'])
    assert operations.batch_view(retry['batch_id'])['status'] == 'done'
    assert not list(session.storage.exports_dir.rglob('partial.png'))
    manager.close()


def test_completed_steps_skip_missing_sources_and_continue_to_export(exportable, tmp_path, monkeypatch):
    sp, action, session = exportable
    assert session.video_info is None
    assert not action.get('template_id')
    monkeypatch.setattr(operations, 'estimate_extract_mb', lambda *a: pytest.fail('skipped extract needs no estimate'))
    from app.config import get_settings
    # Completed matting must not acquire an oversized model reservation either.
    monkeypatch.setattr(get_settings(), 'matting_memory_mb', 10**9)
    jm = JobManager(storage_path=tmp_path / 'jobs.db')
    monkeypatch.setattr(operations, 'job_manager', jm)
    try:
        steps = ['extract', 'matting', 'export']
        plan = operations.plan_processing(sp['id'], [action['id']], steps)
        assert plan['actions'][0]['plan']['steps']['extract']['status'] == 'skip'
        batch = operations.submit_processing(sp['id'], [action['id']], steps, 'skip-done')
        wait_batch(jm, batch['batch_id'])
        result = operations.batch_view(batch['batch_id'])
        assert result['status'] == 'done', result
        rows = result['result']['actions'][0]['steps']
        assert rows['extract']['status'] == rows['matting']['status'] == 'skipped'
        assert rows['export']['status'] == 'done'
        assert result['progress'] == 100
        # Explicit redo still requires its real inputs.
        with pytest.raises(queries.ToolInputError, match='批次未提交'):
            operations.submit_processing(sp['id'], [action['id']], ['extract'], 'force-redo', force=True)
    finally:
        jm.close()


def test_single_file_download_blocks_incomplete_and_allows_completed(exportable):
    from app.main import app
    _, _, session = exportable
    directory = session.storage.export_dir('partial')
    marker = directory / '.incomplete'
    marker.touch()
    (directory / 'frame.png').write_bytes(b'partial')
    with TestClient(app) as client:
        base = f'/api/sessions/{session.id}/export/partial'
        assert client.get(base + '/download').status_code == 409
        assert client.get(base + '/files/frame.png').status_code == 409
        assert client.get(base + '/files/.incomplete').status_code == 409
        marker.unlink()
        assert client.get(base + '/files/frame.png').status_code == 200
        assert client.get(base + '/files/missing.png').status_code == 404
