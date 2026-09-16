"""任务管理器：准入队列、取消、错误处理。

准入队列的要点是「排队的任务不占线程池 worker」——曾经 16 个 io 线程被
13 个空等的首帧任务占满，别的 io 任务一个都排不进来。
"""
from __future__ import annotations

import threading
import time

from app.services.concurrency import ConcurrencyGate
from app.services.job_manager import JobManager, JobStatus


def _wait(pred, timeout=8.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def test_无闸门时照常执行():
    jm = JobManager(max_workers=2, io_workers=2)
    job = jm.submit("t", lambda ctx: 42)
    assert _wait(lambda: job.status == JobStatus.DONE), job.status
    assert job.result == 42
    assert job.progress == 100.0


def test_排队的任务不占线程池():
    """池子给 4 个线程、闸门只放 2 个：同时 running 必须是 2，不是 4。"""
    jm = JobManager(max_workers=2, io_workers=4)
    gate = ConcurrencyGate(lambda: 2, "生图")
    release = threading.Event()

    def work(ctx):
        release.wait(timeout=5)
        return "ok"

    jobs = [jm.submit("t", work, pool="io", gate=gate) for _ in range(8)]
    assert _wait(lambda: sum(j.status == JobStatus.RUNNING for j in jobs) == 2)
    time.sleep(0.4)                       # 给可能的多余放行留出时间

    running = [j for j in jobs if j.status == JobStatus.RUNNING]
    queued = [j for j in jobs if j.status == JobStatus.QUEUED]
    assert len(running) == 2, f"同时运行 {len(running)} 个，闸门失效"
    assert len(queued) == 6, f"排队 {len(queued)} 个，应为 6"
    assert gate.active == 2

    release.set()
    assert _wait(lambda: all(j.finished_at for j in jobs), timeout=15)
    assert all(j.status == JobStatus.DONE for j in jobs)
    assert gate.active == 0, "全部结束后闸门未归零"


def test_排队中的任务显示排队提示():
    jm = JobManager(max_workers=2, io_workers=4)
    gate = ConcurrencyGate(lambda: 1, "生图")
    release = threading.Event()
    jobs = [jm.submit("t", lambda ctx: release.wait(timeout=5), pool="io", gate=gate)
            for _ in range(3)]
    assert _wait(lambda: any(j.status == JobStatus.QUEUED and "排队中" in j.message
                             for j in jobs))
    release.set()
    _wait(lambda: all(j.finished_at for j in jobs), timeout=15)


def test_排队中取消不必等到排上():
    jm = JobManager(max_workers=2, io_workers=4)
    gate = ConcurrencyGate(lambda: 1, "生图")
    release = threading.Event()
    ran = []

    def work(ctx):
        ran.append(1)
        release.wait(timeout=5)

    first = jm.submit("t", work, pool="io", gate=gate)
    later = jm.submit("t", work, pool="io", gate=gate)
    assert _wait(lambda: first.status == JobStatus.RUNNING)
    assert later.status == JobStatus.QUEUED

    assert jm.cancel(later.id)
    assert _wait(lambda: later.status == JobStatus.CANCELLED), later.status
    assert later.finished_at is not None
    assert len(ran) == 1, "已取消的排队任务不该被执行——这些调用按次计费"

    release.set()
    _wait(lambda: first.finished_at, timeout=15)


def test_任务失败也释放许可():
    jm = JobManager(max_workers=2, io_workers=2)
    gate = ConcurrencyGate(lambda: 1, "生图")

    def boom(ctx):
        raise ValueError("炸了")

    job = jm.submit("t", boom, pool="io", gate=gate)
    assert _wait(lambda: job.status == JobStatus.ERROR), job.status
    assert "ValueError" in (job.error or "")
    assert gate.active == 0, "任务异常后许可泄漏，后续任务会被永久挡住"

    ok = jm.submit("t", lambda ctx: "ok", pool="io", gate=gate)
    assert _wait(lambda: ok.status == JobStatus.DONE), "许可泄漏导致后续任务卡死"


def test_上限调大后排队任务立即放行():
    jm = JobManager(max_workers=2, io_workers=4)
    limit = {"n": 1}
    gate = ConcurrencyGate(lambda: limit["n"], "生图")
    release = threading.Event()
    jobs = [jm.submit("t", lambda ctx: release.wait(timeout=5), pool="io", gate=gate)
            for _ in range(3)]
    assert _wait(lambda: sum(j.status == JobStatus.RUNNING for j in jobs) == 1)
    limit["n"] = 3
    assert _wait(lambda: sum(j.status == JobStatus.RUNNING for j in jobs) == 3), \
        "上限调大后排队任务没有跟进"
    release.set()
    _wait(lambda: all(j.finished_at for j in jobs), timeout=15)


def test_进度与消息可上报():
    jm = JobManager(max_workers=2, io_workers=2)

    def work(ctx):
        ctx.report(40, "干到一半")
        time.sleep(0.15)
        return "done"

    job = jm.submit("t", work)
    assert _wait(lambda: job.message == "干到一半", timeout=3)
    assert job.progress == 40
    assert _wait(lambda: job.status == JobStatus.DONE)
    assert job.progress == 100.0
