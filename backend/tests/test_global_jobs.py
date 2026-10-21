import threading
import time
import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.concurrency import ConcurrencyGate
from app.services.job_manager import JobManager, JobStatus, JobSubmissionError


def wait_for(fn):
    end = time.monotonic() + 5
    while not fn() and time.monotonic() < end:
        time.sleep(.01)
    assert fn()


def test_stop_all_covers_hidden_queue_preserves_outputs_and_does_not_wait_for_callbacks(tmp_path):
    jm = JobManager(storage_path=tmp_path/'jobs.db', local_gate=ConcurrencyGate(lambda: 1))
    jm.MAX_JOBS = 1
    artifact = tmp_path/'finished.png'
    artifact.write_bytes(b'completed material')
    completed = jm.submit('export', lambda ctx: {'file': str(artifact)}, target='finished', memory_mb=1)
    wait_for(lambda: completed.finished_at)
    callback_started, callback_release, worker_release = (threading.Event() for _ in range(3))
    calls = []

    def callback():
        calls.append('cancel')
        callback_started.set()
        callback_release.wait(4)
        worker_release.set()

    def work(ctx):
        ctx.register_cancel(callback)
        worker_release.wait(5)

    running = jm.submit('background', work, target='running', memory_mb=1)
    wait_for(lambda: running.status == JobStatus.RUNNING and running._cancel_callbacks)
    queued = [jm.submit('generate', lambda ctx: pytest.fail('queued work ran'), target=str(i), memory_mb=1) for i in range(4)]
    try:
        assert len(jm.list(limit=1)) == 1  # A visible page must not bound stop-all.
        started = time.monotonic()
        result = jm.stop_all()
        assert time.monotonic()-started < 1
        assert result['requested_count'] == 5 and result['queued_cancelled'] == 4
        assert callback_started.wait(2)
        assert all(j.status == JobStatus.CANCELLED for j in queued)
        assert jm.get(completed.id).result == {'file': str(artifact)}
        assert artifact.read_bytes() == b'completed material'
        assert jm.control_state()['admission_paused']
        jm.stop_all()  # Idempotent callbacks; reads remain live while callback waits.
        assert calls == ['cancel']
        with pytest.raises(JobSubmissionError):
            jm.submit('generate', lambda ctx: pytest.fail('must not resubmit'))
    finally:
        callback_release.set(); worker_release.set()
        wait_for(lambda: running.finished_at)
        jm.close()


def test_admin_pause_survives_restart_and_deployment_maintenance_does_not_resume_old_jobs(tmp_path):
    path = tmp_path/'jobs.db'
    jm = JobManager(storage_path=path)
    jm.stop_all(); jm.close()
    restored = JobManager(storage_path=path)
    try:
        assert not restored.control_state()['accepting_jobs']
        restored.maintenance(True); restored.maintenance(False)
        assert not restored.control_state()['accepting_jobs']
        with pytest.raises(JobSubmissionError):
            restored.submit('generate', lambda ctx: pytest.fail('paid replay'))
        assert restored.list() == []
        restored.set_admission_paused(False)
        job = restored.submit('export', lambda ctx: 'new work')
        wait_for(lambda: job.finished_at)
        assert job.result == 'new work'
    finally:
        restored.close()


def test_submissions_racing_stop_all_cannot_pass_the_pause(monkeypatch, tmp_path):
    jm = JobManager(storage_path=tmp_path/'jobs.db'); jm.initialize()
    writing, release, finished = (threading.Event() for _ in range(3))
    original = jm._store.admission_paused
    outcomes = []
    def persist(enabled=None):
        if enabled:
            writing.set(); assert release.wait(3)
        return original(enabled)
    monkeypatch.setattr(jm._store, 'admission_paused', persist)
    stopper = threading.Thread(target=jm.stop_all)
    def submit():
        try: jm.submit('generate', lambda ctx: outcomes.append('ran'))
        except JobSubmissionError: outcomes.append('rejected')
        finally: finished.set()
    producer = threading.Thread(target=submit)
    try:
        stopper.start(); assert writing.wait(2)
        producer.start(); assert not finished.wait(.05)
        release.set(); stopper.join(3); producer.join(3)
        assert outcomes == ['rejected'] and jm.list() == []
    finally:
        release.set(); stopper.join(3)
        if producer.ident: producer.join(3)
        jm.close()


def test_management_api_includes_other_browser_jobs_context_counts_and_requires_admin(monkeypatch, tmp_path):
    from app.api import jobs as api
    from app.config import get_settings
    from app.security import AuthMiddleware
    from app.services.agent_keys import agent_key_store
    from app.services.sprite_store import sprite_store
    jm = JobManager(storage_path=tmp_path/'api-jobs.db')
    monkeypatch.setattr(api, 'job_manager', jm)
    sp = sprite_store.create_sprite('global-jobs-'+uuid.uuid4().hex[:6])
    action = sprite_store.create_action(sp['id'], 'walk')
    one = jm.submit('export', lambda ctx: 'existing output', target=action['id'])
    wait_for(lambda: one.finished_at)
    unknown = jm.submit('reconcile', lambda ctx: None, pool='io')
    wait_for(lambda: unknown.finished_at)
    app = FastAPI(); app.include_router(api.router,prefix='/api'); app.add_middleware(AuthMiddleware)
    monkeypatch.setenv('SPRITE_AUTH_TOKEN', 'global-management-test-token'); get_settings.cache_clear()
    _, readonly = agent_key_store.create('jobs-readonly-'+uuid.uuid4().hex[:6], ['read'])
    headers = {'Authorization':'Bearer global-management-test-token'}
    try:
        with TestClient(app) as client:
            assert client.get('/api/jobs/manage').status_code == 401
            assert client.post('/api/jobs/stop-all',json={'confirm':True},headers={'Authorization':'Bearer '+readonly}).status_code == 401
            response = client.get('/api/jobs/manage?limit=1',headers=headers).json()
            assert response['counts']['done'] == 2 and len(response['jobs']) == 1
            rows = client.get('/api/jobs/manage',headers=headers).json()['jobs']
            assert next(j for j in rows if j['id']==one.id)['context']['sprite_name']==sp['name']
            assert next(j for j in rows if j['id']==unknown.id)['context']=={}
            assert client.post('/api/jobs/stop-all',json={'confirm':False},headers=headers).status_code==422
            assert client.post('/api/jobs/stop-all',json={'confirm':True},headers=headers).json()['admission_paused']
            assert client.get('/api/jobs/control',headers=headers).json()['accepting_jobs'] is False
            assert client.post('/api/jobs/admission',json={'paused':False},headers=headers).json()['accepting_jobs']
            assert jm.get(one.id).result=='existing output'
    finally:
        monkeypatch.delenv('SPRITE_AUTH_TOKEN'); get_settings.cache_clear(); jm.close()


def test_pipeline_cancel_during_matting_preserves_partial_frame_and_does_not_export(monkeypatch, tmp_path):
    from app.core import pipeline as p
    from app.api import background, export_api
    from tests.conftest import FakeCtx
    ctx = FakeCtx(); saved = []
    action = {'id':'cancel-action','pipeline':{'steps':['matting','export'],'done':[]}}
    session = SimpleNamespace(id=action['id'],lock=threading.RLock(),frame_manager=SimpleNamespace(frames=[SimpleNamespace(index=0,has_processed=False)]))
    monkeypatch.setattr(p.sprite_store,'get_action',lambda *args:action)
    monkeypatch.setattr(p.sprite_store,'get_sprite',lambda *args:{'preset':{}})
    monkeypatch.setattr(p,'get_session',lambda *args:session)
    monkeypatch.setattr(p,'_is_done',lambda *args:False)
    monkeypatch.setattr(p,'check_matting',lambda *args:{'model':'fake'})
    monkeypatch.setattr(p,'_save_state',lambda sid,aid,state:saved.append(dict(state)))
    monkeypatch.setattr(p.resources,'hold',lambda *args:nullcontext())
    artifact=tmp_path/'partial-frame.png'
    def process(*args):
        artifact.write_bytes(b'finished frame'); ctx.cancel()
    monkeypatch.setattr(background,'run_bg_remove',process)
    monkeypatch.setattr(export_api,'run_export',lambda *args:pytest.fail('cancelled pipeline exported'))
    result=p.run_pipeline('sprite',action['id'],ctx)
    assert result['status']=='cancelled' and result['done']==[]
    assert saved[-1]['status']=='cancelled' and saved[-1]['error'] is None
    assert artifact.read_bytes()==b'finished frame'


def test_video_cancelled_during_preparation_never_submits_paid_call(monkeypatch, tmp_path):
    from app.core import video_generator as v
    from app.services.storage import SessionStorage
    from tests.conftest import FakeCtx
    session=SimpleNamespace(storage=SessionStorage('cancel-video',tmp_path/'session'))
    monkeypatch.setattr(v,'resolve_first_frame',lambda *args:'data:image/jpeg;base64,AA==')
    class Client:
        def create_task(self,*args,**kwargs): pytest.fail('cancelled request must not be billed')
        def close(self): pass
    monkeypatch.setattr(v,'ArkClient',Client)
    ctx=FakeCtx();ctx.before_paid_call=ctx.cancel
    with pytest.raises(v.ArkError,match='取消'):
        v.run_generate(session,{'prompt':'test'},ctx)


def test_remote_cancel_uses_fresh_client_not_closed_polling_client(monkeypatch):
    from app.core import video_generator as v
    events=[]
    class Client:
        def __init__(self): events.append('opened')
        def cancel_task(self,task): events.append(task);return True
        def close(self):events.append('closed')
    monkeypatch.setattr(v,'ArkClient',Client)
    assert v._cancel_remote_task('existing-paid-id')
    assert events==['opened','existing-paid-id','closed']


def test_stop_racing_remote_id_registration_cancels_and_keeps_recoverable_take(monkeypatch,tmp_path):
    from app.core import video_generator as v
    from app.services.job_manager import Job, JobContext
    from app.services.storage import SessionStorage
    from app.services.take_store import TakeStore
    job=Job(id='cancel-race',type='generate');ctx=JobContext(job)
    session=SimpleNamespace(storage=SessionStorage('race',tmp_path/'session'))
    monkeypatch.setattr(v,'resolve_first_frame',lambda *args:'data:image/jpeg;base64,AA==')
    cancelled=[]
    class Client:
        def create_task(self,*args,**kwargs):
            job.cancel_requested=True
            return {'id':'paid-existing-id'}
        def cancel_task(self,task):cancelled.append(task);return True
        def close(self):pass
    monkeypatch.setattr(v,'ArkClient',Client)
    with pytest.raises(v.ArkError,match='取消'):
        v.run_generate(session,{'prompt':'original request'},ctx)
    assert cancelled==['paid-existing-id']
    pending=TakeStore(session.storage).pending_remote()
    assert len(pending)==1 and pending[0]['remote_task_id']=='paid-existing-id'
