"""并发下的数据一致性：同一动作的版本索引与 OSS 上传缓存。

这两处都是「读 - 改 - 写」的小 JSON，写的时候虽然用 tmp+replace 保证不写坏，
但读改写之间没有互斥，并发时后写的会盖掉先写的：
  · takes.json —— 两个人同时给同一动作生成视频，花过钱的版本在界面上消失
    （实测 16 个并发只剩 1 个，还抛 FileNotFoundError）
  · .oss_cache.json —— 会把别人刚写进去的哈希抹掉，导致重复上传

本地处理闸门的用例在 test_local_gate.py。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.services.concurrency import dir_lock
from app.services.take_store import TakeStore


class FakeStorage:
    """TakeStore 只用到 video_dir。"""

    def __init__(self, root: Path):
        self.video_dir = root / "video"


def _race(n, fn):
    """n 个线程用屏障卡到同一刻起跑，返回 (异常列表, 峰值并发)。"""
    barrier = threading.Barrier(n)
    errs, peak, cur = [], 0, 0
    lock = threading.Lock()

    def worker(i):
        nonlocal peak, cur
        try:
            barrier.wait()
            with lock:
                cur += 1
                peak = max(peak, cur)
            fn(i)
        except Exception as e:                      # noqa: BLE001
            errs.append(e)
        finally:
            with lock:
                cur -= 1

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return errs, peak


# ---------------------------------------------------------------- 版本索引
def test_并发生成同一动作不丢版本(tmp_path):
    """抽卡本来就是一个动作多个版本，并发登记必须一个不少。"""
    store = TakeStore(FakeStorage(tmp_path))
    n = 16
    errs, _ = _race(n, lambda i: store.add("generate", status="pending", model=f"m{i}"))
    assert not errs, f"并发登记抛异常：{errs[:3]}"
    kept = store.load()["takes"]
    assert len(kept) == n, f"提交 {n} 个只剩 {len(kept)} 个"
    assert len({t["id"] for t in kept}) == n, "版本 id 有重复"
    assert len({t["model"] for t in kept}) == n, "有版本被覆盖"


def test_并发改不同版本不互相覆盖(tmp_path):
    store = TakeStore(FakeStorage(tmp_path))
    takes = [store.add("generate", status="pending") for _ in range(8)]

    def patch(i):
        store.update(takes[i]["id"], {"status": "succeeded", "seed": i})

    errs, _ = _race(8, patch)
    assert not errs, errs[:3]
    got = {t["id"]: t for t in store.load()["takes"]}
    assert all(got[t["id"]]["status"] == "succeeded" for t in takes), "有版本的状态丢了"
    assert {got[t["id"]]["seed"] for t in takes} == set(range(8)), "有版本的字段被覆盖"


def test_并发选当前版本与删除(tmp_path):
    store = TakeStore(FakeStorage(tmp_path))
    takes = [store.add("generate", status="succeeded") for _ in range(6)]
    errs, _ = _race(6, lambda i: store.set_current(takes[i]["id"]))
    assert not errs, errs[:3]
    assert store.current_id() in {t["id"] for t in takes}, "current 指向了不存在的版本"

    def drop(i):
        store.delete(takes[i]["id"])

    errs, _ = _race(6, drop)
    assert not errs, errs[:3]
    left = store.load()
    assert left["takes"] == [], f"删除后还剩 {len(left['takes'])} 个"
    assert left["current"] is None, "版本全删了 current 应清空"


def test_不同动作的版本互不干扰(tmp_path):
    """锁按目录分片，两个动作同时写各自的索引，两边都要完整。"""
    a, b = TakeStore(FakeStorage(tmp_path / "a")), TakeStore(FakeStorage(tmp_path / "b"))
    errs, _ = _race(8, lambda i: (a if i % 2 else b).add("generate", status="pending"))
    assert not errs, errs[:3]
    assert len(a.load()["takes"]) == 4 and len(b.load()["takes"]) == 4


# ---------------------------------------------------------------- OSS 上传缓存
def test_OSS缓存并发写入不丢条目(tmp_path, monkeypatch):
    """上传在锁外、回写时重读：不能拿早先的副本把别人写进去的哈希抹掉。"""
    import app.core.video_generator as vg

    calls = []
    monkeypatch.setattr("app.core.oss_uploader.upload_bytes",
                        lambda data, key, ctype: calls.append(key) or f"http://x/{key}")

    class Sess:
        class storage:                      # noqa: N801
            root = tmp_path

    n = 8
    errs, _ = _race(n, lambda i: vg._cached_upload(Sess, f"data{i}".encode(), "jpg",
                                                  "image/jpeg"))
    assert not errs, errs[:3]
    cache = json.loads((tmp_path / ".oss_cache.json").read_text(encoding="utf-8"))
    assert len(cache) == n, f"上传 {n} 个只缓存了 {len(cache)} 个"
    assert len(calls) == n, "每个内容都该只上传一次"

    # 第二次调用应全部命中缓存，不再上传
    calls.clear()
    for i in range(n):
        vg._cached_upload(Sess, f"data{i}".encode(), "jpg", "image/jpeg")
    assert calls == [], f"命中缓存却又上传了：{calls}"


def test_同一动作的目录锁是同一把(tmp_path):
    assert dir_lock(tmp_path) is dir_lock(tmp_path)
    assert dir_lock(tmp_path) is not dir_lock(tmp_path / "other")
