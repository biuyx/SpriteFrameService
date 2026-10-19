"""Paid boundaries use mocked providers; real SQLite, job scheduler and assets."""
import io
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from app.config import get_settings
from app.mcp_server import generation
from app.services.generation_ledger import GenerationLedger
from app.services.job_manager import JobManager, JobSubmissionError
from app.services.sprite_store import sprite_store


def ledger_plan(mode='trial', calls=1):
    return {'mode': mode, 'blockers': [],
            'items': [{'recipe': 'recipe-v1', 'seconds': 4} for _ in range(calls)]}


def test_quota_is_atomic_between_competing_batches_and_durable(tmp_path):
    ledger = GenerationLedger(tmp_path / 'paid.db')
    ledger.set_quota('agent', 1, 4)
    approvals = [ledger.approve('agent', ledger_plan()) for _ in range(20)]
    def attempt(index):
        try:
            return ledger.reserve(approvals[index], 'agent', str(index))
        except JobSubmissionError:
            return None
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(20)))
    assert len([r for r in results if r]) == 1
    again = GenerationLedger(ledger.path)
    assert again.report('agent')['reserved_or_charged'] == {'calls': 1, 'seconds': 4}


def test_retry_and_single_use_approval_and_caller_binding(tmp_path):
    ledger = GenerationLedger(tmp_path / 'paid.db')
    ledger.set_quota('a', 10, 40)
    approval = ledger.approve('a', ledger_plan())
    first = ledger.reserve(approval, 'a', 'retry')
    assert ledger.reserve(approval, 'a', 'retry') == first
    for owner, request in [('b', 'retry'), ('a', 'new')]:
        with pytest.raises(JobSubmissionError):
            ledger.reserve(approval, owner, request)
    with pytest.raises(JobSubmissionError):
        ledger.reserve(ledger.approve('a', ledger_plan()), 'a', 'retry')


def test_unknown_attempt_is_charged_unused_reservation_released_and_never_replayed(tmp_path):
    ledger = GenerationLedger(tmp_path / 'paid.db')
    ledger.set_quota('a', 2, 8)
    batch = ledger.reserve(ledger.approve('a', ledger_plan(calls=2)), 'a', 'r')['id']
    ledger.dispatch(batch, 0, 4)
    ledger.remote(batch, 0, 'provider-job-123')
    ledger = GenerationLedger(ledger.path)  # Process failure/restart, no refund for unknown remote work.
    with pytest.raises(JobSubmissionError):
        ledger.dispatch(batch, 0, 4)
    ledger.close(batch)
    ledger.close(batch)
    report = ledger.report('a')
    assert report['reserved_or_charged'] == {'calls': 1, 'seconds': 4}
    assert report['attempts'][0]['result']['remote_task_id'] == 'provider-job-123'
    assert report['currency_cost'] is None
    with pytest.raises(JobSubmissionError):
        ledger.dispatch(batch, 1, 4)


def test_production_requires_successful_trial_and_human_qualification(tmp_path):
    ledger = GenerationLedger(tmp_path / 'paid.db')
    ledger.set_quota('a', 10, 40)
    with pytest.raises(JobSubmissionError):
        ledger.approve('a', ledger_plan('production'))
    batch = ledger.reserve(ledger.approve('a', ledger_plan()), 'a', 'r')['id']
    ledger.dispatch(batch, 0, 4)
    ledger.close(batch)
    with pytest.raises(JobSubmissionError):
        ledger.qualify(batch, 'looks good')
    ledger.complete(batch, 0, {'usage': {'tokens': 42}})
    with pytest.raises(ValueError):
        ledger.qualify(batch, '')
    ledger.qualify(batch, '人工确认动作节奏和角色一致')
    assert ledger.approve('a', ledger_plan('production'))
    changed = ledger_plan('production')
    changed['items'][0]['recipe'] = 'edited-template'
    with pytest.raises(JobSubmissionError):
        ledger.approve('a', changed)


@pytest.mark.parametrize('expired', [False, True])
def test_revoke_or_expire_while_queued_blocks_paid_boundary(tmp_path, monkeypatch, expired):
    ledger = GenerationLedger(tmp_path / 'paid.db')
    ledger.set_quota('a', 1, 4)
    approval = ledger.approve('a', ledger_plan())
    batch = ledger.reserve(approval, 'a', 'r')['id']
    if expired:
        monkeypatch.setattr(time, 'time', lambda: 10**12)
    else:
        ledger.revoke(approval)
    with pytest.raises(JobSubmissionError):
        ledger.dispatch(batch, 0, 4)
    ledger.close(batch)
    assert ledger.report('a')['reserved_or_charged']['calls'] == 0


@pytest.fixture
def paid_trial(tmp_path, monkeypatch):
    from app.services.ffset_store import ffset_store
    from app.core import ark_image
    image = io.BytesIO()
    Image.new('RGB', (16, 16), 'red').save(image, format='PNG')
    raw = image.getvalue()
    source = tmp_path / 'refs'
    source.mkdir()
    (source / 'walk.png').write_bytes(raw)
    group = 'm3-' + uuid.uuid4().hex
    fs = ffset_store.import_dir(source, group, group=group)
    sp = sprite_store.create_sprite('m3-' + uuid.uuid4().hex)
    sprite_store.update_sprite(sp['id'], {'template_binding': {'group': group, 'first_frame_set_id': fs['id']}})
    action = sprite_store.create_action(sp['id'], 'walk')
    ref = sprite_store.add_ref(sp['id'], 'art.png', raw)
    sprite_store.update_ref(sp['id'], ref['id'], {'role': 'front'})
    monkeypatch.setattr(get_settings(), 'ark_api_key', 'fake-no-network')
    ledger = GenerationLedger(tmp_path / 'generation.db')
    manager = JobManager(storage_path=tmp_path / 'jobs.db')
    monkeypatch.setattr(generation, 'generation_ledger', ledger)
    monkeypatch.setattr(generation, 'job_manager', manager)
    calls = []
    def fake_image(*args, **kwargs):
        calls.append(args)
        return raw
    monkeypatch.setattr(ark_image, 'generate_image', fake_image)
    yield sp, action, ledger, manager, calls
    manager.close()


def wait(manager, job):
    deadline = time.monotonic() + 5
    while manager.get(job).finished_at is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert manager.get(job).finished_at is not None


def test_real_trial_image_write_and_parallel_retries_generate_once(paid_trial):
    sp, action, ledger, manager, calls = paid_trial
    plan = generation.plan_generation(sp['id'], [action['id']], ['firstframe'])
    assert not plan['blockers']
    ledger.set_quota('owner', 1, 0)
    approval = ledger.approve('owner', plan)
    with ThreadPoolExecutor(max_workers=10) as pool:
        rows = list(pool.map(lambda _: generation.submit_generation(approval, 'same', 'owner'), range(20)))
    assert len({r['job_id'] for r in rows}) == 1
    job = rows[0]['job_id']
    wait(manager, job)
    assert manager.get(job).status.value == 'done', manager.get(job).error
    assert len(calls) == 1
    assert (sprite_store.action_dir(sp['id'], action['id']) / 'first_frame.png').is_file()
    assert ledger.report('owner')['attempts'][0]['status'] == 'succeeded'
    assert generation.submit_generation(approval, 'same', 'owner')['job_id'] == job
    with pytest.raises(Exception, match='当前调用方'):
        generation.generation_batch(job, 'someone-else')


def test_changed_reference_invalidates_approved_plan_before_any_charge(paid_trial):
    sp, action, ledger, manager, calls = paid_trial
    plan = generation.plan_generation(sp['id'], [action['id']], ['firstframe'])
    ledger.set_quota('owner', 1, 0)
    approval = ledger.approve('owner', plan)
    ref = sprite_store.list_refs(sp['id'])[0]
    sprite_store.ref_path(sp['id'], ref).write_bytes(b'changed')
    with pytest.raises(JobSubmissionError, match='变化'):
        generation.submit_generation(approval, 'retry', 'owner')
    assert calls == []
    assert ledger.report('owner')['reserved_or_charged']['calls'] == 0


def test_provider_failure_stays_unknown_and_retry_cannot_charge_again(paid_trial, monkeypatch):
    from app.core import ark_image
    sp, action, ledger, manager, calls = paid_trial
    def timeout(*args, **kwargs):
        calls.append(1)
        raise ark_image.ArkImageError('response timeout')
    monkeypatch.setattr(ark_image, 'generate_image', timeout)
    plan = generation.plan_generation(sp['id'], [action['id']], ['firstframe'])
    ledger.set_quota('owner', 1, 0)
    approval = ledger.approve('owner', plan)
    result = generation.submit_generation(approval, 'retry', 'owner')
    wait(manager, result['job_id'])
    assert manager.get(result['job_id']).status.value == 'error'
    generation.submit_generation(approval, 'retry', 'owner')
    assert len(calls) == 1
    assert ledger.report('owner')['attempts'][0]['status'] == 'unknown'


def test_http_read_and_operate_keys_cannot_spend_or_cancel(paid_trial, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.agent_keys import agent_key_store
    monkeypatch.setattr(get_settings(), 'auth_token', 'test-secret')
    for scopes in (['read'], ['read', 'operate']):
        _, key = agent_key_store.create(uuid.uuid4().hex, scopes)
        with TestClient(app) as client:
            for tool, args in [('submit_generation_batch', {'approval_id': 'fake', 'request_id': 'r'}),
                               ('cancel_generation_batch', {'job_id': 'fake'})]:
                response = client.post('/api/mcp', headers={'Authorization': 'Bearer ' + key,
                    'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': 1,
                    'method': 'tools/call', 'params': {'name': tool, 'arguments': args}})
                assert response.status_code == 200
                assert response.json()['result']['isError']
                assert 'generate' in str(response.json())
    assert paid_trial[-1] == []


def test_trial_image_then_video_uses_frozen_inputs_and_records_real_usage(paid_trial, monkeypatch):
    from app.core import video_generator, oss_uploader
    from app.services.template_store import template_store
    from app.services.take_store import TakeStore
    from app.api.deps import get_session
    sp, action, ledger, manager, calls = paid_trial
    tpl = template_store.add_video('walk(' + uuid.uuid4().hex + ').mp4', b'template-video',
                                   group=sprite_store.get_sprite(sp['id'])['template_binding']['group'])
    sprite_store.update_action(sp['id'], action['id'], {'template_id': tpl['id']})
    monkeypatch.setattr(oss_uploader, 'oss_configured', lambda: True)
    uploads = []
    def upload(session, raw, ext, content_type):
        uploads.append((ext, raw))
        return 'https://example.test/' + ext
    monkeypatch.setattr(video_generator, '_cached_upload', upload)
    created = []
    class Client:
        def create_task(self, model, content, **kwargs):
            created.append((model, content, kwargs))
            assert ledger.report('owner')['attempts'][-1]['status'] == 'unknown'
            return {'id': 'remote-paid-42'}
        def download(self, url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b'result-video')
            return 12
        def cancel_task(self, remote): pass
        def close(self): pass
    monkeypatch.setattr(video_generator, 'ArkClient', Client)
    monkeypatch.setattr(video_generator, 'poll_until_done', lambda *a: {
        'status': 'succeeded', 'content': {'video_url': 'https://example.test/result'},
        'usage': {'completion_tokens': 123}, 'duration': 4})
    monkeypatch.setattr(video_generator, '_refresh_session_video', lambda *a: None)
    plan = generation.plan_generation(sp['id'], [action['id']], ['firstframe', 'generate'])
    assert not plan['blockers'], plan
    assert plan['estimate'] == {'calls': 2, 'video_seconds': 4}
    ledger.set_quota('owner', 2, 4)
    approval = ledger.approve('owner', plan)
    result = generation.submit_generation(approval, 'both', 'owner')
    wait(manager, result['job_id'])
    assert manager.get(result['job_id']).error is None
    assert len(created) == len(calls) == 1
    assert ('mp4', b'template-video') in uploads
    take = TakeStore(get_session(action['id']).storage)._load()['takes'][0]
    assert take['remote_task_id'] == 'remote-paid-42'
    assert take['usage'] == {'completion_tokens': 123}
    report = ledger.report('owner')
    assert report['reserved_or_charged'] == {'calls': 2, 'seconds': 4}
    video = next(a for a in report['attempts'] if a['seconds'])
    assert video['result']['take']['usage']['completion_tokens'] == 123


def test_cancel_before_dispatch_releases_quota_and_no_provider_call(paid_trial, monkeypatch):
    from app.core.first_frame_generator import first_frame_gate
    from app.services.concurrency import ConcurrencyGate
    import app.core.first_frame_generator as firstframe
    sp, action, ledger, manager, calls = paid_trial
    gate = ConcurrencyGate(lambda: 1, 'test')
    assert gate.try_acquire()
    monkeypatch.setattr(firstframe, 'first_frame_gate', gate)
    plan = generation.plan_generation(sp['id'], [action['id']], ['firstframe'])
    ledger.set_quota('owner', 1, 0)
    approval = ledger.approve('owner', plan)
    job = generation.submit_generation(approval, 'cancel', 'owner')['job_id']
    generation.cancel_generation(job, 'owner')
    gate.release()
    wait(manager, job)
    assert calls == []
    assert ledger.report('owner')['reserved_or_charged']['calls'] == 0


def test_http_generate_key_only_executes_own_approval(paid_trial, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.agent_keys import agent_key_store
    sp, action, ledger, manager, calls = paid_trial
    monkeypatch.setattr(get_settings(), 'auth_token', 'test-secret')
    owner, key = agent_key_store.create(uuid.uuid4().hex, ['read', 'generate'])
    _, other = agent_key_store.create(uuid.uuid4().hex, ['read', 'generate'])
    plan = generation.plan_generation(sp['id'], [action['id']], ['firstframe'])
    ledger.set_quota(owner['id'], 1, 0)
    approval = ledger.approve(owner['id'], plan)
    with TestClient(app) as client:
        def submit(token):
            response = client.post('/api/mcp', headers={'Authorization': 'Bearer ' + token,
                'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': 1,
                'method': 'tools/call', 'params': {'name': 'submit_generation_batch',
                'arguments': {'approval_id': approval, 'request_id': 'http'}}})
            assert response.status_code == 200
            return response.json()['result']
        assert submit(other)['isError']
        result = submit(key)
        assert not result.get('isError'), result
        import json
        body = json.loads(result['content'][0]['text'])
        wait(manager, body['job_id'])
    assert len(calls) == 1


def test_successful_trial_can_promote_same_recipe_to_production_and_withdraw(paid_trial):
    sp, action, ledger, manager, calls = paid_trial
    ledger.set_quota('owner', 5, 0)
    trial = generation.plan_generation(sp['id'], [action['id']], ['firstframe'])
    assert trial['items'][0]['recipe_state'] == 'draft'
    approval = ledger.approve('owner', trial)
    job = generation.submit_generation(approval, 'trial', 'owner')['job_id']
    wait(manager, job)
    ledger.qualify(manager.get(job).metadata['ledger_batch'], 'Reviewed sample')
    action2 = sprite_store.create_action(sp['id'], 'walk')
    production = generation.plan_generation(sp['id'], [action2['id']], ['firstframe'], 'production')
    assert production['items'][0]['recipe_state'] == 'qualified'
    assert not production['blockers']
    approval2 = ledger.approve('owner', production)
    ledger.mark_draft(production['items'][0]['recipe'])
    with pytest.raises(JobSubmissionError, match='变化'):
        generation.submit_generation(approval2, 'production', 'owner')
    assert len(calls) == 1


def test_planning_does_not_create_ledger(paid_trial):
    sp, action, ledger, manager, calls = paid_trial
    assert not ledger.path.exists()
    plan = generation.plan_generation(sp['id'], [action['id']], ['firstframe'])
    assert not plan['blockers']
    assert not ledger.path.exists()


def test_prompt_change_during_provider_queue_blocks_generation(paid_trial, monkeypatch):
    import app.core.first_frame_generator as firstframe
    from app.services.concurrency import ConcurrencyGate
    sp, action, ledger, manager, calls = paid_trial
    gate = ConcurrencyGate(lambda: 1, 'test')
    assert gate.try_acquire()
    monkeypatch.setattr(firstframe, 'first_frame_gate', gate)
    plan = generation.plan_generation(sp['id'], [action['id']], ['firstframe'])
    ledger.set_quota('owner', 1, 0)
    approval = ledger.approve('owner', plan)
    job = generation.submit_generation(approval, 'queue-edit', 'owner')['job_id']
    deadline = time.monotonic() + 5
    while '排队' not in manager.get(job).message and time.monotonic() < deadline:
        time.sleep(.01)
    assert '排队' in manager.get(job).message
    sprite_store.update_action(sp['id'], action['id'], {
        'gen_prefs': {'first_frame': {'prompt_text': 'new unapproved prompt'}}})
    gate.release()
    wait(manager, job)
    assert manager.get(job).status.value == 'error'
    assert calls == []
    assert ledger.report('owner')['reserved_or_charged']['calls'] == 0


def test_binding_change_invalidates_approval_even_with_identical_pose_bytes(paid_trial, tmp_path):
    from app.services.ffset_store import ffset_store
    sp, action, ledger, manager, calls = paid_trial
    binding = sprite_store.get_sprite(sp['id'])['template_binding']
    plan = generation.plan_generation(sp['id'], [action['id']], ['firstframe'])
    ledger.set_quota('owner', 1, 0)
    approval = ledger.approve('owner', plan)
    directory = tmp_path / 'replacement'
    directory.mkdir()
    (directory / 'walk.png').write_bytes(ffset_store.frame_path(binding['first_frame_set_id'], 'walk').read_bytes())
    replacement = ffset_store.import_dir(directory, 'replacement', binding['group'])
    sprite_store.update_sprite(sp['id'], {'template_binding': {**binding, 'first_frame_set_id': replacement['id']}})
    with pytest.raises(JobSubmissionError, match='变化'):
        generation.submit_generation(approval, 'old-binding', 'owner')
    assert calls == [] and manager.list() == []
    assert ledger.report('owner')['reserved_or_charged']['calls'] == 0
