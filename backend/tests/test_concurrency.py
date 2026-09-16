"""并发闸门与心跳。

这些行为曾经出过问题：批量首帧时十几个任务显示「生图中」其实在排队、
排队期间取消无效、进度整段静止，所以逐条钉住。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.services.concurrency import ConcurrencyGate, heartbeat
from tests.conftest import FakeCtx


def test_并发不超过上限():
    gate = ConcurrencyGate(lambda: 2, "生图")
    peak, cur, lock = 0, 0, threading.Lock()

    def work():
        nonlocal peak, cur
        with gate.hold(FakeCtx()):
            with lock:
                cur += 1
                peak = max(peak, cur)
            time.sleep(0.15)
            with lock:
                cur -= 1

    ts = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak <= 2, f"并发峰值 {peak} 超过上限 2"
    assert gate.active == 0, "闸门计数未归零"


def test_排队时上报排队中而不是沿用上一条消息():
    gate = ConcurrencyGate(lambda: 1, "生图")
    holder = FakeCtx()
    gate.acquire(holder)

    waiter = FakeCtx()
    done = threading.Event()

    def wait():
        gate.acquire(waiter)
        done.set()

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.3)
    assert any("排队中" in m for m in waiter.messages), \
        f"排队期间没有上报排队中，实际消息：{waiter.messages}"
    assert "生图" in waiter.messages[0], "排队提示里应说明是哪种并发满了"

    gate.release()
    assert done.wait(3), "闸门释放后排队任务没有继续"
    t.join()
    gate.release()


def test_排队期间取消立即生效():
    """这些调用按次计费，排队时点了取消就不能再往下走。"""
    gate = ConcurrencyGate(lambda: 1, "生图")
    gate.acquire(FakeCtx())

    waiter = FakeCtx()
    err = []

    def wait():
        try:
            gate.acquire(waiter)
        except RuntimeError as e:
            err.append(str(e))

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.2)
    waiter.cancel()
    t.join(timeout=5)
    assert err == ["已取消"], f"排队中取消未生效：{err}"
    gate.release()
    assert gate.active == 0, "取消后闸门计数不应被占用"


def test_闸门空出的瞬间取消也不放行():
    """循环条件先不成立就直接放行过——补的那次检查要挡住。"""
    gate = ConcurrencyGate(lambda: 1, "生图")
    gate.acquire(FakeCtx())

    waiter = FakeCtx()
    waiter.cancel()                       # 进来就是已取消
    with pytest.raises(RuntimeError, match="已取消"):
        gate.acquire(waiter)
    gate.release()


def test_有空位但已取消时也不放行():
    """闸门不满就不进等待循环，只有循环后那次检查能挡住——专打这个分支。"""
    gate = ConcurrencyGate(lambda: 2, "生图")      # 有空位
    ctx = FakeCtx()
    ctx.cancel()
    with pytest.raises(RuntimeError, match="已取消"):
        gate.acquire(ctx)
    assert gate.active == 0, "已取消的任务不该占用许可"
    assert not any("排队中" in m for m in ctx.messages), "没满就不该报排队"


def test_try_acquire_不阻塞且按上限放行():
    gate = ConcurrencyGate(lambda: 2, "生图")
    assert gate.try_acquire() is True
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False, "超过上限还能拿到许可"
    gate.release()
    assert gate.try_acquire() is True
    for _ in range(3):
        gate.release()
    assert gate.active == 0


def test_上限改动立即生效():
    limit = {"n": 1}
    gate = ConcurrencyGate(lambda: limit["n"], "生图")
    assert gate.try_acquire() and not gate.try_acquire()
    limit["n"] = 3                        # 设置里调大
    assert gate.try_acquire(), "上限调大后应立刻能再拿"
    gate.release(); gate.release()


def test_hold_异常时也释放许可():
    gate = ConcurrencyGate(lambda: 1, "生图")
    with pytest.raises(ValueError):
        with gate.hold(FakeCtx()):
            raise ValueError("boom")
    assert gate.active == 0, "任务抛异常后许可泄漏了"


def test_心跳推进进度且不超过上限():
    ctx = FakeCtx()
    with heartbeat(ctx, "生图中", start=15, end=80, expect=1, interval=0.1):
        time.sleep(0.55)
    assert len(ctx.reports) >= 3, f"心跳次数太少：{ctx.reports}"
    pcts = ctx.percents
    assert pcts == sorted(pcts), "进度应单调不降"
    assert pcts[0] >= 15 and max(pcts) <= 80, f"进度越界：{pcts}"
    assert all("已等待" in m for m in ctx.messages), "心跳消息应带已等待秒数"


def test_心跳退出后不再上报():
    ctx = FakeCtx()
    with heartbeat(ctx, "生图中", start=10, end=90, expect=1, interval=0.05):
        time.sleep(0.2)
    n = len(ctx.reports)
    time.sleep(0.3)
    assert len(ctx.reports) == n, "心跳线程退出后仍在上报"
