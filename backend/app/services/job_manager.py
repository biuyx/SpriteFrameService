"""进程内后台任务管理器（线程池执行长任务，前端轮询状态）。"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Any

from app.config import get_settings

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    type: str
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    message: str = ""
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    cancel_requested: bool = False
    _cancel_callbacks: List[Callable[[], None]] = field(default_factory=list)
    _lock: Any = field(default_factory=threading.Lock)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status.value,
            "progress": round(self.progress, 1),
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobContext:
    """传给任务函数的上下文：进度上报 / 结果 / 取消。"""

    def __init__(self, job: Job):
        self._job = job

    def report(self, percent: float, message: str = "") -> None:
        with self._job._lock:
            self._job.progress = float(percent)
            if message:
                self._job.message = message

    def set_result(self, data: Any) -> None:
        with self._job._lock:
            self._job.result = data

    def cancelled(self) -> bool:
        return self._job.cancel_requested

    def register_cancel(self, fn: Callable[[], None]) -> None:
        with self._job._lock:
            self._job._cancel_callbacks.append(fn)


class JobManager:
    """线程池任务管理器。"""

    MAX_JOBS = 200   # 任务表上限，超出后淘汰最早的已结束任务

    def __init__(self, max_workers: Optional[int] = None, io_workers: int = 16):
        self._jobs: Dict[str, Job] = {}
        self._jobs_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers or get_settings().max_workers,
            thread_name_prefix="spriteframe-job",
        )
        # io 池：外部 API 的纯网络等待（视频生成等）。这类任务一等就是几分钟，
        # 放进 CPU 池会占满 worker 把本地处理全部堵死。
        self._io_executor = ThreadPoolExecutor(
            max_workers=io_workers,
            thread_name_prefix="spriteframe-io",
        )

    def _evict_locked(self) -> None:
        """在持有 _jobs_lock 时调用：淘汰最早的已结束任务。"""
        if len(self._jobs) <= self.MAX_JOBS:
            return
        finished = sorted(
            (j for j in self._jobs.values() if j.finished_at is not None),
            key=lambda j: j.finished_at,
        )
        for job in finished:
            if len(self._jobs) <= self.MAX_JOBS:
                break
            self._jobs.pop(job.id, None)

    def submit(self, job_type: str, fn: Callable[[JobContext], Any],
               lock: Optional[Any] = None, pool: str = "cpu") -> Job:
        """提交任务。

        lock: 可选的互斥锁（如会话锁）。在工作线程内获取，使同一会话的任务
        串行执行，避免并发改写帧数据；提交调用本身不会因此阻塞。
        pool: "cpu"（默认，本地处理）或 "io"（外部 API 等待，如视频生成）。
        """
        job = Job(id=uuid.uuid4().hex[:12], type=job_type)
        with self._jobs_lock:
            self._jobs[job.id] = job
            self._evict_locked()

        def _runner():
            ctx = JobContext(job)
            try:
                if lock is not None:
                    with job._lock:
                        job.message = "等待同会话的其他任务完成..."
                    lock.acquire()
                try:
                    with job._lock:
                        if job.cancel_requested:
                            job.status = JobStatus.CANCELLED
                            return
                        job.status = JobStatus.RUNNING
                        # 清掉排队提示，避免任务完成后仍显示「等待中」
                        job.message = ""
                    result = fn(ctx)
                    if not ctx.cancelled():
                        with job._lock:
                            job.status = JobStatus.DONE
                            job.result = result
                            job.progress = 100.0
                finally:
                    if lock is not None:
                        lock.release()
            except Exception as e:
                # 完整堆栈只进服务端日志；响应默认仅给异常类型与消息，
                # 避免向未鉴权的调用方泄露绝对路径等内部信息。
                logger.exception("任务失败 [%s/%s]", job.type, job.id)
                detail = f"{type(e).__name__}: {e}"
                if get_settings().debug_errors:
                    detail = f"{detail}\n{traceback.format_exc()}"
                with job._lock:
                    job.status = JobStatus.ERROR
                    job.error = detail
            finally:
                job.finished_at = time.time()
                # 任务结束后再淘汰一次，使任务表在提交停止后也能收敛到上限
                with self._jobs_lock:
                    self._evict_locked()

        executor = self._io_executor if pool == "io" else self._executor
        executor.submit(_runner)
        return job

    def cancel(self, job_id: str) -> bool:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        with job._lock:
            job.cancel_requested = True
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
            cbs = list(job._cancel_callbacks)
        for cb in cbs:
            try:
                cb()
            except Exception:
                pass
        return True

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[dict]:
        with self._jobs_lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]
        return [j.to_dict() for j in jobs]


job_manager = JobManager()
