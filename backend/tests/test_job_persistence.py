"""跨重启查询、事务幂等、并发重复提交。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.job_manager import Job, JobManager, JobStatus, JobSubmissionError
from app.services.job_store import JobStore


def wait_done(job):
    deadline = time.monotonic() + 6
    while job.finished_at is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert job.finished_at is not None


def test_terminal_result_and_key_survive_restart_and_eviction(tmp_path):
    path = tmp_path / 'jobs.db'
    first = JobManager(storage_path=path)
    first.MAX_JOBS = 1
    job = first.submit('export', lambda ctx: {'file': 'out.png'}, target='a',
                       params={'format': 'png'}, request_key='stable')
    wait_done(job)
    another = first.submit('export', lambda ctx: 2, target='b', params={})
    wait_done(another)
    assert first.get(job.id).result == {'file': 'out.png'}
    first.close()
    restored = JobManager(storage_path=path)
    called = []
    same = restored.submit('export', lambda ctx: called.append(1), target='a',
                           params={'format': 'png'}, request_key='stable')
    assert same.id == job.id and same.result == {'file': 'out.png'}
    assert not called
    with pytest.raises(JobSubmissionError):
        restored.submit('export', lambda ctx: None, target='a', params={'format': 'gif'}, request_key='stable')
    restored.close()


def test_interrupted_queue_and_running_are_terminal_not_replayed(tmp_path):
    path = tmp_path / 'jobs.db'
    store = JobStore(path)
    for status in [JobStatus.QUEUED, JobStatus.RUNNING]:
        job = Job(id=status.value, type='processing_batch', status=status,
                  result={'actions': [{'name': 'walk', 'steps': {'extract': 'done'}}]})
        store.save(job.to_dict())
    restored = JobManager(storage_path=path)
    for name in ['queued', 'running']:
        job = restored.get(name)
        assert job.status == JobStatus.ERROR and job.interrupted
        assert '新的请求编号' in job.error
        assert job.result['actions'][0]['steps']['extract'] == 'done'
    assert not restored._pending
    restored.close()


def test_parallel_retries_submit_exactly_once(tmp_path):
    manager = JobManager(storage_path=tmp_path / 'jobs.db')
    release = threading.Event()
    calls = []
    def work(ctx):
        calls.append(1)
        release.wait(5)
        return 'ok'
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            jobs = list(pool.map(lambda _: manager.submit('export', work, target='same',
                                params={'format': 'png'}, request_key='retry'), range(30)))
        assert len({j.id for j in jobs}) == 1
    finally:
        release.set()
    wait_done(jobs[0])
    assert calls == [1]
    manager.close()


def test_automatic_dedup_only_active_and_parameters_must_match():
    manager = JobManager()
    release = threading.Event()
    a = manager.submit('export', lambda ctx: release.wait(5), target='a', params={'scale': 1})
    b = manager.submit('export', lambda ctx: None, target='a', params={'scale': 1})
    c = manager.submit('export', lambda ctx: None, target='a', params={'scale': 2})
    assert a.id == b.id and a.id != c.id
    release.set()
    wait_done(a)
    wait_done(c)
    d = manager.submit('export', lambda ctx: None, target='a', params={'scale': 1})
    assert d.id != a.id
    wait_done(d)
    manager.close()


def test_second_scheduler_cannot_mark_live_jobs_interrupted(tmp_path):
    path = tmp_path / 'jobs.db'
    first = JobManager(storage_path=path)
    first.initialize()
    second = JobManager(storage_path=path)
    with pytest.raises(RuntimeError, match='调度进程'):
        second.initialize()
    second.close()
    first.close()


def test_maintenance_rejects_new_work_without_cancelling_existing():
    manager = JobManager()
    release = threading.Event()
    job = manager.submit('t', lambda ctx: release.wait(5), pool='io')
    assert manager.maintenance(True)['active'] == 1
    with pytest.raises(JobSubmissionError) as exc:
        manager.submit('t', lambda ctx: None)
    assert exc.value.status_code == 503
    assert not job.cancel_requested
    release.set()
    wait_done(job)
    manager.maintenance(False)
    next_job = manager.submit('t', lambda ctx: 1)
    wait_done(next_job)
    manager.close()


def test_old_active_job_remains_visible_when_recent_history_exceeds_limit(tmp_path):
    manager = JobManager(storage_path=tmp_path / 'jobs.db')
    manager.MAX_JOBS = 2
    release = threading.Event()
    old = manager.submit('t', lambda ctx: release.wait(5), pool='io')
    try:
        for _ in range(4):
            wait_done(manager.submit('export', lambda ctx: 1))
        assert old.id in {j['id'] for j in manager.list(limit=2)}
    finally:
        release.set()
    wait_done(old)
    manager.close()
