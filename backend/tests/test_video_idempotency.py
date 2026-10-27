"""Real admission, take journal and pipeline; every provider call is a local fake."""
import importlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from app.api import generate as api, sprites
from app.config import get_settings
from app.core import pipeline, video_generator as video
from app.services.concurrency import ConcurrencyGate
from app.services.job_manager import JobManager, JobSubmissionError
from app.services.session import Session
from app.services.sprite_store import sprite_store
from app.services.storage import SessionStorage
from app.services.take_store import TakeStore

jobs_module = importlib.import_module('app.services.job_manager')


def wait_for(fn):
    end = time.monotonic() + 5
    while not fn() and time.monotonic() < end:
        time.sleep(.01)
    assert fn()


@pytest.fixture
def env(tmp_path, monkeypatch):
    sp = sprite_store.create_sprite('video-idempotency-' + uuid.uuid4().hex)
    action = sprite_store.create_action(sp['id'], 'walk')
    root = sprite_store.action_dir(sp['id'], action['id'])
    session = Session(action['id'], SessionStorage(root))
    manager = JobManager(storage_path=tmp_path/'jobs.db', io_workers=8)
    release, entered = threading.Event(), threading.Event()
    state = SimpleNamespace(manager=manager, sp=sp, action=action, session=session,
                            release=release, entered=entered, calls=[], mode='success',
                            remote_status='succeeded', path=tmp_path/'jobs.db')
    monkeypatch.setattr(get_settings(), 'ark_api_key', 'fake-no-network')
    monkeypatch.setattr(api, 'get_session', lambda aid: session)
    monkeypatch.setattr(pipeline, 'get_session', lambda aid: session)
    monkeypatch.setattr(api, 'job_manager', manager)
    monkeypatch.setattr(jobs_module, 'job_manager', manager)
    monkeypatch.setattr(api, 'generate_gate', ConcurrencyGate(lambda: 5))
    monkeypatch.setattr(video, 'resolve_first_frame', lambda *a: 'data:image/png;base64,ZmFrZQ==')
    monkeypatch.setattr(video, '_cancel_remote_task', lambda rid: True)
    monkeypatch.setattr(video, '_refresh_session_video', lambda *a: setattr(session, 'video_info', True))
    monkeypatch.setattr(pipeline, 'check_generate', lambda *a: {})
    monkeypatch.setattr(api, '_default_generate_payload', lambda *a, **kw: payload())

    class Client:
        @classmethod
        def for_model(cls, model=None):
            """生产代码按模型选接入点；测试里一律用这个假客户端。"""
            return cls()
        def create_task(self, *args, **kwargs):
            receipt = TakeStore(session.storage).list()['takes'][-1]
            assert receipt['dispatch_state'] == 'unknown'  # persisted before POST
            state.calls.append(receipt['id']); entered.set()
            assert release.wait(5)
            if state.mode == 'timeout':
                raise httpx.ReadTimeout('fake lost create response')
            if state.mode == 'reject':
                exc = video.ArkError('fake HTTP 400 rejection'); exc.request_rejected = True
                raise exc
            return {'id': 'remote-' + str(len(state.calls))}
        def get_task(self, rid):
            if state.mode == 'poll_error':
                raise video.ArkError('fake transient status API error')
            return {'status': state.remote_status, 'content': {'video_url': 'fake://video'}}
        def download(self, url, path):
            path.write_bytes(b'fake provider video'); return 19
        def close(self): pass

    monkeypatch.setattr(video, 'ArkClient', Client)
    yield state
    release.set()
    wait_for(lambda: all(j.finished_at is not None for j in state.manager._jobs.values()))
    state.manager.close()


def payload():
    return {'prompt': 'local fake', 'model': get_settings().ark_model, 'params': {'duration': 4},
            'first_frame': {'kind': 'action'}, 'template_id': None,
            'use_reference_video': False, 'prompt_id': None,
            'prompt_version': None, 'prompt_name': None}


def generate(env, request='one', **overrides):
    return api.generate_video(env.action['id'], api.GenerateRequest(
        **{**payload(), 'request_id': request, 'remember': False, **overrides}))


def start(env, request='pipeline', force=None):
    return sprites.pipeline_start(env.sp['id'], sprites.PipelineStartRequest(
        action_ids=[env.action['id']], steps=['generate'], force=force, request_id=request))


def test_parallel_same_request_only_dispatches_once(env):
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: generate(env), range(20)))
    assert len({r['job_id'] for r in results}) == 1
    assert env.entered.wait(2) and len(env.calls) == 1


def test_normal_generation_blocks_pipeline_and_resume_without_state_writes(env):
    generate(env); assert env.entered.wait(2)
    action = sprite_store.get_action(env.sp['id'], env.action['id'])
    before = dict(action.get('pipeline') or {})
    result = start(env)
    assert not result['submitted'] and '原视频' in result['skipped'][0]['reason']
    assert (sprite_store.get_action(env.sp['id'], env.action['id']).get('pipeline') or {}) == before
    assert len(env.calls) == 1


def test_pipeline_blocks_normal_generation_with_new_id(env):
    result = start(env)
    assert result['submitted'] and env.entered.wait(2)
    with pytest.raises(JobSubmissionError, match='已有任务'):
        generate(env, 'different-entry')
    assert len(env.calls) == 1


def test_cross_entry_race_creates_one_provider_request(env):
    barrier = threading.Barrier(3)
    def normal():
        barrier.wait(3)
        try:return generate(env)
        except JobSubmissionError:return None
    def piped():
        barrier.wait(3); return start(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b = pool.submit(normal),pool.submit(piped)
        barrier.wait(3); a.result(4); b.result(4)
    assert env.entered.wait(2) and len(env.calls) == 1
    assert len(env.manager.list()) == 1


def test_same_id_reuses_terminal_job_and_new_id_allows_explicit_new_take(env):
    env.release.set(); first = generate(env)['job_id']
    wait_for(lambda: env.manager.get(first).finished_at)
    assert generate(env)['job_id'] == first
    second = generate(env, 'explicit-new-take')['job_id']
    wait_for(lambda: env.manager.get(second).finished_at)
    assert second != first and len(env.calls) == 2


def test_legacy_no_id_reuses_same_terminal_request(env):
    env.release.set(); first = generate(env, None)['job_id']
    wait_for(lambda: env.manager.get(first).finished_at)
    assert generate(env, None)['job_id'] == first and len(env.calls) == 1


def test_changed_parameters_cannot_reuse_request_id(env):
    generate(env)
    with pytest.raises(JobSubmissionError, match='其他生成参数'):
        generate(env, prompt='changed')
    assert env.entered.wait(2) and len(env.calls) == 1


def test_durable_request_key_survives_eviction_and_restart(env, monkeypatch):
    env.release.set(); first = generate(env)['job_id']
    wait_for(lambda: env.manager.get(first).finished_at)
    env.manager._jobs.clear()
    assert generate(env)['job_id'] == first
    env.manager.close()
    env.manager = JobManager(storage_path=env.path)
    monkeypatch.setattr(api, 'job_manager', env.manager)
    monkeypatch.setattr(jobs_module, 'job_manager', env.manager)
    assert generate(env)['job_id'] == first and len(env.calls) == 1


@pytest.mark.parametrize('kind', ['pending', 'running', 'unknown'])
def test_unresolved_old_receipt_blocks_every_new_entry(env, kind):
    ts = TakeStore(env.session.storage)
    take = ts.add('generate', status='pending' if kind == 'pending' else 'running',
                  dispatch_state='unknown' if kind == 'unknown' else 'created',
                  remote_task_id=None if kind == 'unknown' else 'old-remote')
    with pytest.raises(JobSubmissionError, match='尚未确认'):
        generate(env)
    assert not start(env)['submitted'] and env.calls == []
    assert ts.get(take['id']) == take


def test_lost_create_response_is_quarantined_and_never_retried(env):
    env.mode = 'timeout'; env.release.set()
    jid = generate(env)['job_id']; wait_for(lambda: env.manager.get(jid).finished_at)
    receipt = TakeStore(env.session.storage).list()['takes'][-1]
    assert receipt['dispatch_state'] == 'unknown' and receipt['status'] == 'running'
    assert generate(env)['job_id'] == jid
    with pytest.raises(JobSubmissionError):generate(env, 'new-id-is-not-an-override')
    assert not start(env)['submitted'] and len(env.calls) == 1


def test_confirmed_provider_rejection_can_retry_only_as_new_intent(env):
    env.mode = 'reject'; env.release.set()
    jid = generate(env)['job_id']; wait_for(lambda: env.manager.get(jid).finished_at)
    assert TakeStore(env.session.storage).list()['takes'][-1]['dispatch_state'] == 'rejected'
    assert generate(env)['job_id'] == jid and len(env.calls) == 1
    env.mode = 'success'; next_id = generate(env, 'confirmed-new')['job_id']
    wait_for(lambda: env.manager.get(next_id).finished_at)
    assert env.manager.get(next_id).status.value == 'done' and len(env.calls) == 2


def test_transient_poll_error_keeps_remote_receipt_and_reconcile_resolves_it(env):
    env.mode = 'poll_error'; env.release.set()
    jid = generate(env)['job_id']; wait_for(lambda: env.manager.get(jid).finished_at)
    ts = TakeStore(env.session.storage); receipt = ts.list()['takes'][-1]
    assert receipt['status'] == 'running' and receipt['remote_task_id']
    with pytest.raises(JobSubmissionError):generate(env, 'must-not-charge-again')
    env.mode = 'success'
    ctx = SimpleNamespace(report=lambda *a: None)
    video.run_reconcile(env.session, ctx)
    assert ts.get(receipt['id'])['status'] == 'succeeded' and len(env.calls) == 1


def test_unresolved_receipt_cannot_be_deleted_to_bypass_paid_guard(env):
    ts = TakeStore(env.session.storage); receipt = ts.add('generate', status='running', remote_task_id='paid')
    with pytest.raises(JobSubmissionError, match='不能删除'):
        ts.delete(receipt['id'])
    assert ts.get(receipt['id']) is not None


def test_corrupt_take_index_fails_closed_without_reset_or_charge(env):
    file = env.session.storage.video_dir/'takes.json'
    file.write_text('broken index', encoding='utf-8')
    with pytest.raises(JobSubmissionError, match='索引无法读取'):generate(env)
    assert not start(env)['submitted'] and env.calls == []
    assert file.read_text() == 'broken index'


def test_runtime_reservation_protects_callers_that_bypass_submission_api(env, ctx):
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(video.run_generate, env.session, payload(), ctx)
        assert env.entered.wait(2)
        with pytest.raises(JobSubmissionError):video.run_generate(env.session, payload(), ctx)
        env.release.set(); first.result(3)
    assert len(env.calls) == 1 and len(TakeStore(env.session.storage).list()['takes']) == 1


def test_pipeline_terminal_retry_with_force_does_not_replay_paid_step(env):
    env.release.set(); first = start(env, force=['generate'])['submitted'][0]['job_id']
    wait_for(lambda: env.manager.get(first).finished_at)
    retry = start(env, force=['generate'])
    assert retry['submitted'][0]['job_id'] == first and len(env.calls) == 1
    with pytest.raises(JobSubmissionError, match='其他流水线参数'):
        start(env, force=[])


def test_new_default_pipeline_reuses_completed_video_without_new_charge(env):
    env.release.set(); jid = generate(env)['job_id']
    wait_for(lambda: env.manager.get(jid).finished_at)
    assert not start(env)['submitted'] and len(env.calls) == 1


def test_cancel_before_provider_create_leaves_no_uncertain_charge(env):
    gate = api.generate_gate
    for _ in range(5):assert gate.try_acquire()
    try:
        jid = generate(env)['job_id']; env.manager.cancel(jid)
        wait_for(lambda: env.manager.get(jid).finished_at)
        assert env.calls == [] and TakeStore(env.session.storage).list()['takes'] == []
    finally:
        for _ in range(5):gate.release()


def test_admission_pause_does_not_create_receipt_or_task(env):
    env.manager.set_admission_paused(True)
    with pytest.raises(JobSubmissionError):generate(env)
    assert env.calls == [] and not (env.session.storage.video_dir/'takes.json').exists()


def test_batch_single_and_pipeline_share_the_same_action_guard(env, monkeypatch):
    from app.core import oss_uploader
    from app.services.template_store import template_store
    monkeypatch.setattr(oss_uploader, 'oss_configured', lambda: True)
    monkeypatch.setattr(template_store, 'get', lambda tid: {'id': 'fake-template', 'variant': 'walk'})
    (env.session.storage.root/'first_frame.png').write_bytes(b'fake')
    req = api.BatchGenerateRequest(action_ids=[env.action['id'], env.action['id']], request_id='batch')
    batch = api.batch_generate(env.sp['id'], req)
    assert len(batch['submitted']) == 1 and env.entered.wait(2)
    replay = api.batch_generate(env.sp['id'], req)
    assert replay['submitted'][0]['job_id'] == batch['submitted'][0]['job_id']
    with pytest.raises(JobSubmissionError):generate(env, 'single-entry')
    assert not start(env)['submitted'] and len(env.calls) == 1


def test_video_in_progress_blocks_resume_and_preserves_original_done_steps(env):
    original = {'status': 'error', 'steps': ['generate', 'extract'], 'done': ['firstframe']}
    sprite_store.update_action(env.sp['id'], env.action['id'], {'pipeline': original})
    generate(env); assert env.entered.wait(2)
    result = sprites.pipeline_resume(env.sp['id'], sprites.PipelineResumeRequest(
        action_ids=[env.action['id']], request_id='resume'))
    assert not result['submitted'] and '原视频' in result['skipped'][0]['reason']
    assert sprite_store.get_action(env.sp['id'], env.action['id'])['pipeline'] == original


def test_cancel_at_paid_boundary_retains_remote_receipt_until_confirmed_terminal(env):
    jid = generate(env)['job_id']; assert env.entered.wait(2)
    env.manager.cancel(jid); env.release.set()
    wait_for(lambda: env.manager.get(jid).finished_at)
    ts = TakeStore(env.session.storage); receipt = ts.list()['takes'][-1]
    assert receipt['status'] == 'running' and receipt['remote_task_id']
    assert env.manager.get(jid).status.value == 'cancelled'
    with pytest.raises(JobSubmissionError):generate(env, 'new-id')
    assert not start(env)['submitted'] and len(env.calls) == 1
    env.remote_status = 'cancelled'
    video.run_reconcile(env.session, SimpleNamespace(report=lambda *a: None))
    assert ts.get(receipt['id'])['status'] == 'failed'
    env.remote_status = 'succeeded'; next_id = generate(env, 'confirmed-new')['job_id']
    wait_for(lambda: env.manager.get(next_id).finished_at)
    assert len(env.calls) == 2


def test_known_remote_failure_is_retryable_as_new_intent(env):
    env.remote_status = 'failed'; env.release.set()
    jid = generate(env)['job_id']; wait_for(lambda: env.manager.get(jid).finished_at)
    receipt = TakeStore(env.session.storage).list()['takes'][-1]
    assert receipt['status'] == 'failed'
    assert generate(env)['job_id'] == jid and len(env.calls) == 1
    env.remote_status = 'succeeded'; next_id = generate(env, 'after-terminal-failure')['job_id']
    wait_for(lambda: env.manager.get(next_id).finished_at)
    assert len(env.calls) == 2


@pytest.mark.parametrize('status,rejected', [(400, True), (429, True), (408, False), (500, False)])
def test_provider_rejection_and_server_uncertainty_are_distinguished(env, status, rejected):
    from app.core.ark_client import ArkClient, ArkError
    client = ArkClient()
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={'error': {'message': 'fake'}})))
    try:
        with pytest.raises(ArkError) as error:client.create_task('fake', [])
        assert error.value.request_rejected == rejected
    finally:client.close()


def test_paid_boundary_is_not_entered_when_journal_write_fails(env, monkeypatch):
    original = TakeStore._save
    writes = []
    def fail_boundary(self, data):
        writes.append(1)
        if len(writes) == 2:raise OSError('fake disk failure before provider POST')
        return original(self, data)
    monkeypatch.setattr(TakeStore, '_save', fail_boundary)
    env.release.set(); jid = generate(env)['job_id']
    wait_for(lambda: env.manager.get(jid).finished_at)
    assert env.calls == []
    assert TakeStore(env.session.storage).list()['takes'][-1]['dispatch_state'] == 'not_sent'


def test_legacy_history_without_a_request_key_requires_fresh_confirmation(env):
    TakeStore(env.session.storage).add('generate', status='succeeded', remote_task_id='historical-paid')
    with pytest.raises(JobSubmissionError, match='刷新页面'):
        generate(env, None)
    assert env.calls == []
    env.release.set(); jid = generate(env, 'confirmed-after-upgrade')['job_id']
    wait_for(lambda: env.manager.get(jid).finished_at)
    assert len(env.calls) == 1
