"""持久化任务、幂等提交和按资源准入。任务函数不跨重启重放。"""
from __future__ import annotations
import hashlib
import json
import logging
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.config import get_settings
from app.services.job_store import JobStore
from app.services.resources import ResourceCoordinator, resources

logger = logging.getLogger(__name__)


class JobSubmissionError(ValueError):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


class JobStatus(str, Enum):
    QUEUED = 'queued'
    RUNNING = 'running'
    DONE = 'done'
    ERROR = 'error'
    CANCELLED = 'cancelled'


@dataclass
class Job:
    id: str
    type: str
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.
    message: str = ''
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    cancel_requested: bool = False
    request_key: Optional[str] = None
    fingerprint: str = ''
    metadata: dict = field(default_factory=dict)
    resource: str = 'heavy'
    targets: list = field(default_factory=list)
    memory_mb: int = 0
    interrupted: bool = False
    _cancel_callbacks: list = field(default_factory=list)
    _lock: Any = field(default_factory=threading.RLock)
    _persist: Any = None
    _saved_at: float = 0.

    def to_dict(self):
        with self._lock:
            data = {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith('_')}
            data['status'] = self.status.value
            data['progress'] = round(self.progress, 1)
            return data

    @classmethod
    def from_dict(cls, data):
        data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and not k.startswith('_')}
        data['status'] = JobStatus(data['status'])
        return cls(**data)


class JobContext:
    def __init__(self, job):
        self._job = job

    @property
    def job_id(self):
        return self._job.id

    def _save(self, force=False):
        if self._job._persist:
            self._job._persist(self._job, force)

    def report(self, percent, message=''):
        with self._job._lock:
            self._job.progress = float(percent)
            if message:
                self._job.message = message
        self._save()

    def set_result(self, data):
        with self._job._lock:
            self._job.result = data
        self._save(True)

    def cancelled(self):
        return self._job.cancel_requested

    def register_cancel(self, fn):
        with self._job._lock:
            cancelled = self._job.cancel_requested
            if not cancelled:
                self._job._cancel_callbacks.append(fn)
        # 远端任务创建与登记回调之间可能已收到停止请求，不能漏掉取消。
        if cancelled:
            try:
                fn()
            except Exception:
                logger.exception('取消任务回调失败')


class JobManager:
    MAX_JOBS = 200

    def __init__(self, max_workers=None, io_workers=16, local_gate=None,
                 persistent=False, storage_path: Optional[Path] = None, coordinator=None):
        self._jobs: Dict[str, Job] = {}
        self._jobs_lock = threading.RLock()
        self._coordinator = coordinator or (ResourceCoordinator(heavy_gate=local_gate)
                                            if local_gate is not None else resources)
        self._local_gate = self._coordinator.heavy_gate
        self._executor = ThreadPoolExecutor(max_workers=max_workers or get_settings().max_workers,
                                           thread_name_prefix='spriteframe-job')
        self._export_executor = ThreadPoolExecutor(max_workers=get_settings().export_workers,
                                                  thread_name_prefix='spriteframe-export')
        self._io_executor = ThreadPoolExecutor(max_workers=io_workers, thread_name_prefix='spriteframe-io')
        self._cancel_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='spriteframe-cancel')
        self._persistent = persistent or storage_path is not None
        self._storage_path = storage_path
        self._store = None
        self._maintenance = False
        self._admission_paused = False
        self._pending = OrderedDict()
        self._pending_lock = self._jobs_lock
        self._admit_wake = threading.Event()
        self._closed = threading.Event()
        self._admitter = threading.Thread(target=self._admit_loop, daemon=True, name='spriteframe-admit')
        self._admitter.start()

    def initialize(self):
        with self._jobs_lock:
            if self._persistent and self._store is None:
                path = self._storage_path or get_settings().resolved_data_dir / 'jobs.sqlite3'
                store = JobStore(path)
                store.claim_owner()
                store.recover()
                self._store = store
                self._admission_paused = store.admission_paused()
                for data in self._store.latest(self.MAX_JOBS):
                    job = Job.from_dict(data)
                    job._persist = self._save
                    self._jobs[job.id] = job

    def _save(self, job, force=False):
        with self._jobs_lock:
            if self._store is not None and (force or time.monotonic() - job._saved_at >= 1):
                self._store.save(job.to_dict())
                job._saved_at = time.monotonic()

    def _evict_locked(self):
        finished = sorted((j for j in self._jobs.values() if j.finished_at is not None),
                          key=lambda j: j.finished_at)
        for job in finished:
            if len(self._jobs) <= self.MAX_JOBS:
                break
            self._jobs.pop(job.id, None)

    def _admit_loop(self):
        while not self._closed.is_set():
            self._admit_wake.wait(.25)
            self._admit_wake.clear()
            with self._jobs_lock:
                for jid, (job, runner, executor, gate, local) in list(self._pending.items()):
                    if job.cancel_requested:
                        self._pending.pop(jid)
                        continue
                    if local:
                        reason = self._coordinator.try_acquire(job.resource, job.targets, job.memory_mb)
                    else:
                        reason = None if gate.try_acquire() else f'排队中（并发已满 {gate.active} 个）...'
                    if reason is not None:
                        if job.message != reason:
                            job.message = reason
                            self._save(job)
                        continue
                    self._pending.pop(jid)
                    executor.submit(runner)

    def close(self):
        """测试/进程退出使用；必须先排空任务，不能静默丢弃排队任务。"""
        with self._jobs_lock:
            if any(j.finished_at is None for j in self._jobs.values()):
                raise RuntimeError('仍有任务运行或排队，不能关闭调度器')
        self._closed.set()
        self._admit_wake.set()
        self._admitter.join(timeout=2)
        for executor in (self._executor, self._export_executor, self._io_executor, self._cancel_executor):
            executor.shutdown(wait=True)
        if self._store:
            self._store.close()

    def submit(self, job_type: str, fn: Callable, lock=None, pool='cpu', gate=None,
               *, target=None, targets=None, params=None, request_key=None, metadata=None,
               resource=None, memory_mb=None):
        self.initialize()
        if self._closed.is_set():
            raise RuntimeError('任务管理器已关闭')
        local = pool == 'cpu' and gate is None
        resource = resource or ('export' if job_type in ('export', 'spine_export')
                                else 'matting' if job_type == 'background' else 'heavy')
        target_ids = sorted(set(targets or ([target] if target else
                                           [f'lock:{id(lock)}'] if lock is not None else [])))
        amount = memory_mb or (get_settings().matting_memory_mb if job_type == 'background' else 256)
        if local and amount > self._coordinator.budget():
            raise JobSubmissionError('任务预计内存超过预算，请减少帧数或尺寸，或调整处理内存预算', 400)
        signature = json.dumps([job_type, target_ids, params], sort_keys=True, default=str)
        fingerprint = hashlib.sha256(signature.encode()).hexdigest()
        with self._jobs_lock:
            if self._maintenance or self._admission_paused:
                raise JobSubmissionError('新任务提交已暂停，请到全局任务管理中查看状态', 503)
            if request_key:
                old = next((j for j in self._jobs.values() if j.request_key == request_key), None)
                if old is None and self._store:
                    data = self._store.get(request_key, by_key=True)
                    old = Job.from_dict(data) if data else None
                if old:
                    if old.fingerprint != fingerprint:
                        raise JobSubmissionError('请求编号已用于其他参数，请使用新的请求编号')
                    return old
            # 有稳定动作目标或显式参数时，合并正在进行的同一操作。
            if params is not None:
                old = next((j for j in self._jobs.values() if j.fingerprint == fingerprint
                            and j.status in (JobStatus.QUEUED, JobStatus.RUNNING)), None)
                if old and (not request_key or old.request_key == request_key):
                    return old
            job = Job(id=uuid.uuid4().hex[:12], type=job_type, request_key=request_key,
                      fingerprint=fingerprint, metadata=metadata or {}, resource=resource,
                      targets=target_ids, memory_mb=amount)
            job._persist = self._save
            self._save(job, True)  # 先落盘再准入；写不进去不执行副作用。
            self._jobs[job.id] = job
            self._evict_locked()

            def runner():
                ctx = JobContext(job)
                try:
                    if lock is not None:
                        job.message = '等待同动作的其他任务完成'
                        while not lock.acquire(timeout=.25):
                            if ctx.cancelled():
                                return
                    try:
                        if ctx.cancelled():
                            return
                        job.status = JobStatus.RUNNING
                        job.message = ''
                        self._save(job, True)
                        result = fn(ctx)
                        if not ctx.cancelled():
                            job.result, job.progress, job.status = result, 100., JobStatus.DONE
                    finally:
                        if lock is not None:
                            lock.release()
                except Exception as exc:
                    if not ctx.cancelled():
                        logger.exception('任务失败 [%s/%s]', job.type, job.id)
                        job.error = f'{type(exc).__name__}: {exc}'
                        if get_settings().debug_errors:
                            job.error += '\n' + traceback.format_exc()
                        job.status = JobStatus.ERROR
                finally:
                    if ctx.cancelled():
                        job.status = JobStatus.CANCELLED
                        job.message = '已取消'
                    job._cancel_callbacks.clear()  # 不让已结束任务的 bound method 留住模型实例。
                    if local:
                        self._coordinator.release(job.resource, job.targets, job.memory_mb)
                    elif gate is not None:
                        gate.release()
                    job.finished_at = time.time()
                    self._save(job, True)
                    with self._jobs_lock:
                        self._evict_locked()
                    self._admit_wake.set()

            executor = (self._io_executor if pool == 'io' else self._export_executor
                        if resource == 'export' else self._executor)
            if not local and gate is None:
                executor.submit(runner)
            else:
                job.message = '排队中...'
                self._pending[job.id] = (job, runner, executor, gate, local)
                self._admit_wake.set()
            return job

    def get(self, job_id):
        self.initialize()
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None and self._store:
                data = self._store.get(job_id)
                job = Job.from_dict(data) if data else None
            return job

    def active_job(self, job_type, **metadata):
        """查询真实未退出任务；取消请求发出后仍占位，且不受历史分页限制。"""
        self.initialize()
        with self._jobs_lock:
            return next((j for j in self._jobs.values()
                         if j.type == job_type and j.finished_at is None
                         and all(j.metadata.get(k) == v for k, v in metadata.items())), None)

    def maintenance(self, enabled):
        self.initialize()
        with self._jobs_lock:
            self._maintenance = bool(enabled)
            return {'enabled': self._maintenance,
                    'active': sum(j.finished_at is None for j in self._jobs.values())}

    def set_admission_paused(self, paused):
        self.initialize()
        with self._jobs_lock:
            if self._store:
                self._store.admission_paused(bool(paused))
            self._admission_paused = bool(paused)
            return self.control_state()

    def control_state(self):
        self.initialize()
        with self._jobs_lock:
            counts = self._store.status_counts() if self._store else {}
            if not self._store:
                for job in self._jobs.values():
                    counts[job.status.value] = counts.get(job.status.value, 0) + 1
            return {'admission_paused': self._admission_paused, 'maintenance': self._maintenance,
                    'accepting_jobs': not (self._admission_paused or self._maintenance), 'counts': counts,
                    'active': sum(j.finished_at is None for j in self._jobs.values())}

    def _request_cancel_locked(self, job):
        if job.finished_at is not None or job.cancel_requested:
            return []
        with job._lock:
            job.cancel_requested = True
            if job.id in self._pending:
                self._pending.pop(job.id)
                job.status, job.message = JobStatus.CANCELLED, '已取消（排队中）'
                job.finished_at = time.time()
            callbacks = list(job._cancel_callbacks)
        self._save(job, True)
        return callbacks

    @staticmethod
    def _call_cancel(callback):
        try:
            callback()
        except Exception:
            logger.exception('取消任务回调失败')

    def stop_all(self):
        """先关闭准入，再原子地标记全部未完成任务；不删除任何素材或历史。"""
        self.initialize()
        with self._jobs_lock:
            self.set_admission_paused(True)
            active = [j for j in self._jobs.values() if j.finished_at is None]
            queued_count = sum(j.id in self._pending for j in active)
            callbacks = [cb for job in active for cb in self._request_cancel_locked(job)]
            result = {'requested_count': len(active), 'job_ids': [j.id for j in active],
                      'queued_cancelled': queued_count,
                      'stopping_count': sum(j.finished_at is None for j in active),
                      'admission_paused': True}
        # 远端取消可能阻塞网络；不占任务锁、闸门锁或正在退出的任务线程池。
        for callback in callbacks:
            self._cancel_executor.submit(self._call_cancel, callback)
        self._admit_wake.set()
        return result

    def by_request_key(self, key):
        self.initialize()
        with self._jobs_lock:
            job = next((j for j in self._jobs.values() if j.request_key == key), None)
            if job is None and self._store:
                data = self._store.get(key, by_key=True)
                job = Job.from_dict(data) if data else None
            return job

    def list(self, limit=50):
        self.initialize()
        limit = max(1, min(limit, 2000))
        with self._jobs_lock:
            if self._store:
                saved = {d['id']: d for d in self._store.latest(max(1, min(limit, 2000)))}
                saved.update({j.id: j.to_dict() for j in self._jobs.values()})
                return sorted(saved.values(), key=lambda j: (j['status'] in ('queued', 'running'),
                                                             j['created_at']), reverse=True)[:limit]
            return [j.to_dict() for j in sorted(self._jobs.values(), key=lambda j: j.created_at,
                                               reverse=True)[:limit]]

    def cancel(self, job_id):
        job = self.get(job_id)
        if job is None or job.finished_at is not None:
            return False
        with self._jobs_lock:
            callbacks = self._request_cancel_locked(job)
        for callback in callbacks:
            self._call_cancel(callback)
        self._admit_wake.set()
        return True


job_manager = JobManager(persistent=True)
