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
        while True:
            if ctx.cancelled():
                raise RuntimeError("已取消")
            with self._cond:
                acquired = self._active < max(1, self._limit())
                if acquired:
                    self._active += 1
                active = self._active
            # ctx.report 会取得任务管理器锁；调度线程持该锁调用 try_acquire。
            # 所有 ctx 回调都必须在闸门锁外，避免两把锁反向等待。
            if acquired:
                try:
                    if ctx.cancelled():
                        raise RuntimeError("已取消")
                except BaseException:
                    self.release()
                    raise
                return
            ctx.report(1, f"排队中（{self._label}并发已满 {active} 个）...")
            if ctx.cancelled():
                raise RuntimeError("已取消")
            with self._cond:
                # 上报期间可能已释放许可，重新检查可避免错过通知后空等。
                if self._active >= max(1, self._limit()):
                    self._cond.wait(timeout=2)

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


# ---------------------------------------------------------------- 目录锁
_STRIPES = 64
_stripe_locks = [threading.RLock() for _ in range(_STRIPES)]


def dir_lock(path) -> threading.RLock:
    """按目录取一把可重入锁，给「读 - 改 - 写」的小 JSON 用（takes.json、.oss_cache.json）。

    这类文件只有几 KB，写的时候虽然用 tmp+replace 保证不写坏，但「读出来、追加、
    写回去」中间没有互斥，并发时后写的会盖掉先写的——实测 16 个并发只剩 1 个。

    为什么用固定数量的锁分片而不是每个目录一把：目录数随动作数增长，长期运行的
    服务不该为此攒下无上限的锁对象。不同目录偶尔撞到同一片只是多一点串行，不影响正确性。
    """
    return _stripe_locks[hash(str(path)) % _STRIPES]


# ---------------------------------------------------------------- 本地处理闸门
def _local_limit() -> int:
    from app.config import get_settings
    return max(1, get_settings().max_workers)


# 重型本地处理共用该闸门；导出走独立名额。两类入口均由 resources 统一检查
# 动作互斥与总内存预算，流水线不能绕开同一套限制。
local_gate = ConcurrencyGate(_local_limit, "本地处理")
