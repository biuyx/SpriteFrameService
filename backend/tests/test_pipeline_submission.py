"""停止/重启后可续跑；真实排队、退出中和并发提交不会重复启动流水线。"""
import copy
import importlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.api import sprites as api
from app.core import pipeline
jobs = importlib.import_module('app.services.job_manager')


def wait_for(fn):
    deadline = time.monotonic() + 4
    while not fn() and time.monotonic() < deadline:
        time.sleep(.01)
    assert fn()


@pytest.fixture
def env(monkeypatch):
    manager = jobs.JobManager(io_workers=1)
    release = threading.Event()
    action = {'id': 'act', 'name': 'sleep', 'pipeline': {
        'status': 'running', 'job_id': 'old', 'steps': ['extract', 'matting', 'export'],
        'force': [], 'done': ['extract'], 'current': 'matting'}}
    calls = []
    monkeypatch.setattr(jobs, 'job_manager', manager)
    monkeypatch.setattr(api.sprite_store, 'get_sprite', lambda sid: {'id': sid})
    monkeypatch.setattr(api.sprite_store, 'get_action', lambda sid, aid: copy.deepcopy(action))
    monkeypatch.setattr(api.sprite_store, 'update_action',
                        lambda sid, aid, patch: action.update(copy.deepcopy(patch)))
    monkeypatch.setattr(api.sprite_store, 'list_actions', lambda sid: [copy.deepcopy(action)])
    monkeypatch.setattr(pipeline, 'plan_action', lambda *args: {'runnable': True, 'steps': {}})
    monkeypatch.setattr(pipeline, 'size_ops_of', lambda sid: [])

    def run(sid, aid, ctx):
        calls.append(copy.deepcopy(action['pipeline']))
        action['pipeline'].update(status='running', job_id=ctx.job_id)
        assert release.wait(5)
        action['pipeline'].update(status='cancelled' if ctx.cancelled() else 'done')

    monkeypatch.setattr(pipeline, 'run_pipeline', run)
    yield manager, action, release, calls
    release.set()
    wait_for(lambda: all(j.finished_at is not None for j in manager._jobs.values()))
    manager.close()


def submit(mode):
    if mode == 'start':
        return api.pipeline_start('sprite', api.PipelineStartRequest(
            action_ids=['act'], steps=['extract', 'matting', 'export']))
    return api.pipeline_resume('sprite', api.PipelineResumeRequest(action_ids=['act']))


@pytest.mark.parametrize('mode', ['start', 'resume'])
@pytest.mark.parametrize('old_status', ['cancelled', 'error', 'missing'])
def test_stale_running_after_stop_or_restart_can_submit(env, mode, old_status):
    manager, action, release, calls = env
    if old_status != 'missing':
        manager._jobs['old'] = jobs.Job(id='old', type='pipeline',
            status=jobs.JobStatus(old_status), finished_at=time.time(),
            metadata={'sprite_id': 'sprite', 'action_id': 'act'}, interrupted=old_status == 'error')
    result = submit(mode)
    assert len(result['submitted']) == 1 and not result['skipped']
    jid = result['submitted'][0]['job_id']
    assert action['pipeline']['job_id'] == jid != 'old'
    wait_for(lambda: calls)
    assert calls[0]['job_id'] == jid and calls[0]['force'] == []
    if mode == 'resume':
        assert calls[0]['done'] == ['extract']  # 续跑保持已完成工序及原参数。


def test_true_queued_job_without_action_job_id_blocks_start_and_resume(env):
    manager, action, release, calls = env
    blocker = manager.submit('test', lambda ctx: release.wait(5), pool='io')
    wait_for(lambda: blocker.status == jobs.JobStatus.RUNNING)
    first = submit('resume')['submitted'][0]
    queued = manager.get(first['job_id'])
    assert queued.status == jobs.JobStatus.QUEUED
    action['pipeline']['job_id'] = None  # 兼容旧版排队时没保存 job_id 的状态。
    before = copy.deepcopy(action)
    for mode in ('start', 'resume'):
        result = submit(mode)
        assert not result['submitted']
        assert result['skipped'][0]['reason'] == '流水线已排队'
        assert result['skipped'][0]['job_id'] == queued.id
    assert action == before and not calls


def test_running_and_still_exiting_cancellation_block_duplicates(env):
    manager, action, release, calls = env
    first = submit('resume')['submitted'][0]
    wait_for(lambda: calls)
    assert submit('start')['skipped'][0]['reason'] == '流水线进行中'
    assert manager.cancel(first['job_id'])
    # cancel_requested=true 不是退出完成，不能趁清理或远端取消尚未结束重提。
    assert submit('resume')['skipped'][0]['reason'] == '流水线正在停止，请等待退出'
    release.set()
    wait_for(lambda: manager.get(first['job_id']).finished_at)
    assert len(submit('resume')['submitted']) == 1


@pytest.mark.parametrize('mode', ['start', 'resume'])
def test_admission_rejection_preserves_previous_pipeline(env, mode):
    manager, action, release, calls = env
    before = copy.deepcopy(action)
    manager.set_admission_paused(True)
    with pytest.raises(jobs.JobSubmissionError):
        submit(mode)
    assert action == before and manager.list() == [] and not calls


def test_simultaneous_start_and_resume_only_create_one_job(env):
    manager, action, release, calls = env
    barrier = threading.Barrier(9)
    def request(i):
        barrier.wait(3)
        return submit('start' if i % 2 else 'resume')
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(request, i) for i in range(8)]
        barrier.wait(3)
        results = [f.result(4) for f in futures]
    assert sum(len(r['submitted']) for r in results) == 1
    assert sum(len(r['skipped']) for r in results) == 7
    wait_for(lambda: calls)
    assert len(calls) == 1 and len(manager.list()) == 1


def test_fast_runner_completion_never_overwritten_with_queued(env):
    manager, action, release, calls = env
    release.set()
    result = submit('resume')
    job = manager.get(result['submitted'][0]['job_id'])
    wait_for(lambda: job.finished_at)
    assert action['pipeline']['status'] == 'done'
    assert calls[0]['job_id'] == job.id


def test_action_board_and_plan_correct_stale_state_without_writing(env):
    manager, action, release, calls = env
    manager._jobs['old'] = jobs.Job(id='old', type='pipeline',
        status=jobs.JobStatus.CANCELLED, finished_at=time.time())
    before = copy.deepcopy(action)
    assert api.list_actions('sprite')['actions'][0]['pipeline']['status'] == 'cancelled'
    plan = api.pipeline_plan('sprite', api.PipelinePlanRequest(action_ids=['act']))
    assert plan['plans'][0]['pipeline']['status'] == 'cancelled'
    assert action == before and not calls


def test_busy_pipeline_does_not_trigger_spine_side_effect(env, monkeypatch):
    manager, action, release, calls = env
    submit('resume')
    monkeypatch.setattr(pipeline, 'submit_spine_export',
                        lambda *args: pytest.fail('已有流水线时不应另起导出'))
    result = api.pipeline_start('sprite', api.PipelineStartRequest(action_ids=['act'], spine=True))
    assert not result['submitted'] and result['skipped']


def test_active_lookup_covers_all_jobs_and_matches_both_role_and_action(env):
    manager, action, release, calls = env
    manager.MAX_JOBS = 1
    manager.submit('pipeline', lambda ctx: release.wait(5), pool='io',
                   metadata={'sprite_id': 'other', 'action_id': 'act'})
    first = submit('resume')['submitted'][0]
    for i in range(5):
        manager.submit('test', lambda ctx: None, pool='io')
    assert len(manager.list(limit=1)) == 1
    assert manager.active_job('pipeline', sprite_id='sprite', action_id='act').id == first['job_id']


def test_persistent_recovery_does_not_treat_interrupted_job_as_active(tmp_path):
    path = tmp_path/'jobs.sqlite3'
    first = jobs.JobManager(storage_path=path)
    first.initialize()
    first._store.save(jobs.Job(id='interrupted', type='pipeline',
        metadata={'sprite_id': 'sprite', 'action_id': 'act'}).to_dict())
    first.close()
    restored = jobs.JobManager(storage_path=path)
    try:
        assert restored.active_job('pipeline', sprite_id='sprite', action_id='act') is None
        assert restored.get('interrupted').interrupted
    finally:
        restored.close()


def test_duplicate_ids_share_one_batch_member_and_job(env):
    manager, action, release, calls = env
    result = api.pipeline_start('sprite', api.PipelineStartRequest(
        action_ids=['act', 'act'], steps=['export'], spine=True))
    assert len(result['submitted']) == 1
    assert action['pipeline']['batch']['members'] == ['act']


def test_all_completed_steps_still_allow_explicit_spine_export(env, monkeypatch):
    monkeypatch.setattr(pipeline, 'plan_action', lambda *args: {
        'runnable': False, 'steps': {'export': {'status': 'skip', 'reason': '已完成'}}})
    monkeypatch.setattr(pipeline, 'submit_spine_export', lambda *args: {'job_id': 'spine'})
    result = api.pipeline_start('sprite', api.PipelineStartRequest(action_ids=['act'], spine=True))
    assert result['spine']['job_id'] == 'spine' and not result['submitted']


def test_stale_queued_without_job_is_displayed_as_interrupted_without_mutation(env):
    manager, action, release, calls = env
    action['pipeline'].update(status='queued', job_id=None)
    before = copy.deepcopy(action)
    state = api.list_actions('sprite')['actions'][0]['pipeline']
    assert state['status'] == 'error' and '中断' in state['error']
    assert action == before


def test_board_shows_live_stopping_state_instead_of_stale_flag(env):
    manager, action, release, calls = env
    first = submit('resume')['submitted'][0]
    wait_for(lambda: calls)
    manager.cancel(first['job_id'])
    before = copy.deepcopy(action)
    state = api.list_actions('sprite')['actions'][0]['pipeline']
    assert state['status'] == 'stopping' and state['job_id'] == first['job_id']
    assert action == before
