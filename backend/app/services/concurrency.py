"""外部 API 调用的并发闸门。

超限的任务排队等待而不是拒绝——远端生成本来就要等一会儿，
多等一点比让用户重试友好。

等待期间必须做两件事，否则界面会骗人：
  1. 持续上报「排队中」，不然任务停在上一条消息上，看着像已经在干活
  2. 反复查取消，不然排队中的任务取消不掉，只能等它排到

用裸 Semaphore 的 `with gate:` 两条都做不到——这正是首帧生成一度
在批量时十几个都显示「生图中」却一张没画、且取消无效的原因。
"""
from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from typing import Callable


@contextmanager
def heartbeat(ctx, label: str, start: float = 15, end: float = 80,
              expect: float = 25.0, interval: float = 2.0):
    """阻塞调用期间按已等待秒数推进进度。

    同步接口（如 Seedream 生图）拿不到真实进度，只能给个心跳，
    否则进度条整段静止，看着像卡死。用指数逼近：越等越慢、永不到 end，
    不假装知道还剩多久。
    """
    stop = threading.Event()
    t0 = time.time()

    def tick():
        while not stop.wait(interval):
            elapsed = time.time() - t0
            pct = start + (end - start) * (1 - math.exp(-elapsed / max(1.0, expect)))
            try:
                ctx.report(min(end, pct), f"{label}，已等待 {elapsed:.0f}s...")
            except Exception:
                break

    th = threading.Thread(target=tick, name="progress-heartbeat", daemon=True)
    th.start()
    try:
        yield
    finally:
        stop.set()
        th.join(timeout=0.5)


class ConcurrencyGate:
    """上限每次现取（设置改了立即生效）。"""

    def __init__(self, limit: Callable[[], int], label: str = "生成"):
        self._limit = limit
        self._label = label
        self._active = 0
        self._cond = threading.Condition()

    @property
    def active(self) -> int:
        return self._active

    def acquire(self, ctx) -> None:
        with self._cond:
            while self._active >= max(1, self._limit()):
                ctx.report(1, f"排队中（{self._label}并发已满 {self._active} 个）...")
                if ctx.cancelled():
                    raise RuntimeError("已取消")
                self._cond.wait(timeout=2)
            # 闸门空出来时循环条件先不成立就直接放行了，这里再查一次：
            # 排队期间点了取消就不该继续——这些调用是按次计费的
            if ctx.cancelled():
                raise RuntimeError("已取消")
            self._active += 1

    def try_acquire(self) -> bool:
        """非阻塞取许可，供调度层做准入：拿不到就让任务留在队列里，
        不要占着工作线程空等（16 个 io 线程曾被 13 个空等的首帧任务占满）。"""
        with self._cond:
            if self._active >= max(1, self._limit()):
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    class _Held:
        def __init__(self, gate, ctx):
            self._gate, self._ctx = gate, ctx

        def __enter__(self):
            self._gate.acquire(self._ctx)
            return self._gate

        def __exit__(self, *exc):
            self._gate.release()
            return False

    def hold(self, ctx):
        """with gate.hold(ctx): ... —— 带排队上报与取消检查的 with 写法。"""
        return self._Held(self, ctx)
