"""本地处理闸门：cpu 池与流水线共用同一个上限。

此前是「cpu 池 2 个线程」加「流水线自己的闸门 2」两套互不知情的限额，实际最多 4 个
并发，谁也管不住谁。现在两处取同一个 local_gate（上限即 SPRITE_MAX_WORKERS，
池子大小也取同一个值，所以拿到许可的任务总有线程可用）。
"""
from __future__ import annotations

import threading
import time

from app.services.concurrency import ConcurrencyGate, local_gate
from app.services.job_manager import JobManager, JobStatus


def _wait(pred, timeout=5.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def test_cpu池默认走统一闸门():
    """不给 gate 的 cpu 任务也要受 local_gate 限制。"""
    gate = ConcurrencyGate(lambda: 2, "本地处理")
    jm = JobManager(max_workers=4, io_workers=2, local_gate=gate)
    release = threading.Event()
    jobs = [jm.submit("t", lambda ctx: release.wait(timeout=5), pool="cpu")
            for _ in range(8)]
    assert _wait(lambda: gate.active == 2), f"闸门没放行到上限：{gate.active}"
    time.sleep(0.4)                        # 给可能的多余放行留出时间
    assert gate.active == 2, f"同时运行 {gate.active} 个，闸门没管住 cpu 池"
    assert sum(j.status == JobStatus.QUEUED for j in jobs) == 6
    release.set()
    assert _wait(lambda: gate.active == 0), "全部结束后许可没归零"
    assert _wait(lambda: all(j.status == JobStatus.DONE for j in jobs))


def test_io池不受本地闸门限制():
    """视频生成这类纯网络等待不能挤在本地闸门里，否则一等等几分钟把抠图堵死。"""
    gate = ConcurrencyGate(lambda: 1, "本地处理")
    jm = JobManager(max_workers=2, io_workers=4, local_gate=gate)
    release = threading.Event()
    jobs = [jm.submit("t", lambda ctx: release.wait(timeout=5), pool="io") for _ in range(3)]
    assert _wait(lambda: sum(j.status == JobStatus.RUNNING for j in jobs) == 3), \
        "io 任务被本地闸门挡住了"
    assert gate.active == 0
    release.set()


def test_显式给的闸门优先于默认():
    local = ConcurrencyGate(lambda: 1, "本地处理")
    own = ConcurrencyGate(lambda: 3, "生图")
    jm = JobManager(max_workers=4, io_workers=2, local_gate=local)
    release = threading.Event()
    jobs = [jm.submit("t", lambda ctx: release.wait(timeout=5), pool="cpu", gate=own)
            for _ in range(3)]
    assert _wait(lambda: own.active == 3), "显式闸门没生效"
    assert local.active == 0
    release.set()


def test_流水线用的是同一个闸门():
    """流水线里的抽帧/抠图/导出必须和直接提交的本地任务抢同一个许可。"""
    from app.core import pipeline
    assert pipeline._local_gate is local_gate


def test_本地闸门上限取自设置():
    from app.config import get_settings
    assert local_gate._limit() == max(1, get_settings().max_workers)
    assert get_settings().max_workers >= 2, "默认上限不该退回到 1"
