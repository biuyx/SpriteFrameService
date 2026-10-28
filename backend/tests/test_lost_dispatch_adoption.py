"""提交响应丢失（读超时）后的找回与兜底。

场景：POST 出去了但响应没读回来——远端可能已经在生成（可能已计费），我们既拿不到
任务号，也不敢重发。方舟的任务列表带创建时间，可以按「同模型 + 提交时刻」认回来；
认不出来就等到超时后判定失败，把人从「永远生成中」里放出来。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.core import video_generator as video
from app.services.storage import SessionStorage
from app.services.take_store import TakeStore

MODEL = "doubao-seedance-2-0-fast-260128"


class Ctx:
    def report(self, percent, message=""):
        pass


def _stamp(offset_seconds: float) -> str:
    """按提交时刻生成 cgt- 任务号里的时间戳。"""
    return time.strftime("%Y%m%d%H%M%S", time.localtime(time.time() + offset_seconds))


def _session(tmp_path, started_ago: float = 60, model: str = MODEL):
    """造一个「提交响应丢失」的 take。"""
    storage = SessionStorage("lost-dispatch", tmp_path / "s")
    store = TakeStore(storage)
    take = store.add("generate", status="pending", model=model)
    store.update(take["id"], {
        "status": "running", "dispatch_state": "unknown",
        "dispatch_started_at": time.time() - started_ago,
        "error": "生成提交结果未确认，可能已计费。请先核对远端结果，禁止重复提交；read timed out",
    })
    return SimpleNamespace(storage=storage), store, take["id"]


def _install_client(monkeypatch, tasks, video_bytes=b"video-bytes", status="succeeded",
                    per_page=20):
    """tasks: [(id, model, status)]，按传入顺序切页（真实接口也是倒序分页）。"""
    calls = {"lists": 0, "gets": 0, "downloads": 0}

    class Client:
        @classmethod
        def for_model(cls, model=None):
            return cls()

        def list_tasks(self, page_num=1, page_size=20):
            calls["lists"] += 1
            chunk = tasks[(page_num - 1) * per_page: page_num * per_page]
            return {"items": [{"id": tid, "model": m, "status": s} for tid, m, s in chunk]}

        def get_task(self, rid):
            calls["gets"] += 1
            return {"status": status, "content": {"video_url": "https://cdn.test/v.mp4"},
                    "usage": {"completion_tokens": 100}}

        def download(self, url, dest):
            calls["downloads"] += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(video_bytes)
            return len(video_bytes)

        def close(self):
            pass

    monkeypatch.setattr(video, "ArkClient", Client)
    return calls


def test_读超时丢掉的任务号能从远端任务列表认回来(tmp_path, monkeypatch):
    session, store, tid = _session(tmp_path, started_ago=60)
    rid = f"cgt-{_stamp(-60)}-abcde"
    calls = _install_client(monkeypatch, [(rid, MODEL, "succeeded")])

    out = video.run_reconcile(session, Ctx())

    take = store.get(tid)
    assert take["remote_task_id"] == rid, "应按模型与提交时刻认回任务号"
    assert take["status"] == "succeeded", "认回来之后应正常取回并落盘"
    assert calls["gets"] == 1 and calls["downloads"] == 1
    assert any(r["status"] == "adopted" for r in out["results"])


def test_候选任务模型不同就不认(tmp_path, monkeypatch):
    """同一时刻别人可能也在生成，模型对不上就不能当成自己的。"""
    session, store, tid = _session(tmp_path, started_ago=60)
    _install_client(monkeypatch, [(f"cgt-{_stamp(-60)}-other", "doubao-seedance-2-0-260128", "succeeded")])

    video.run_reconcile(session, Ctx())

    take = store.get(tid)
    assert not take.get("remote_task_id"), "模型不匹配不应认领"
    assert take["status"] == "running", "还没到超时，应继续等"


def test_多个候选就不猜(tmp_path, monkeypatch):
    session, store, tid = _session(tmp_path, started_ago=60)
    stamp = _stamp(-60)
    _install_client(monkeypatch, [(f"cgt-{stamp}-a", MODEL, "succeeded"),
                                  (f"cgt-{stamp}-b", MODEL, "succeeded")])

    video.run_reconcile(session, Ctx())
    assert not store.get(tid).get("remote_task_id"), "分不清是哪次提交时不要乱认"


def test_超出等待窗口还找不到就判失败放行(tmp_path, monkeypatch):
    """一直卡在「生成中」比判失败更糟：人没法重做。"""
    session, store, tid = _session(tmp_path, started_ago=3600)
    _install_client(monkeypatch, [])

    video.run_reconcile(session, Ctx())

    take = store.get(tid)
    assert take["status"] == "failed"
    assert take["dispatch_state"] == "resolved"
    assert "查无此任务" in take["error"] and "可以重新生成" in take["error"]
    # 判定失败后应能重新生成
    store.reserve_generation(model=MODEL)


def test_时间还没到就继续等(tmp_path, monkeypatch):
    session, store, tid = _session(tmp_path, started_ago=120)
    _install_client(monkeypatch, [])

    out = video.run_reconcile(session, Ctx())

    take = store.get(tid)
    assert take["status"] == "running" and not take.get("remote_task_id")
    assert any(r["status"] == "waiting" for r in out["results"])


def test_同模型连着提交两次时按秒级对齐挑(tmp_path, monkeypatch):
    """线上真实情况：15:35:27 和 15:36:16 各提交过一次同模型任务。

    宽窗口里两个都命中，但真正的这次和提交时刻只差 0.1 秒，靠这个分出来。
    """
    session, store, tid = _session(tmp_path, started_ago=60)
    stamp = _stamp(-60)
    near = f"cgt-{stamp}-near"
    far = f"cgt-{_stamp(-109)}-far"          # 49 秒前的另一次提交
    _install_client(monkeypatch, [(near, MODEL, "succeeded"), (far, MODEL, "succeeded")])

    video.run_reconcile(session, Ctx())
    assert store.get(tid)["remote_task_id"] == near


def test_秒级也分不开时用参数指纹(tmp_path, monkeypatch):
    session, store, tid = _session(tmp_path, started_ago=60)
    store.update(tid, {"params": {"resolution": "480p", "ratio": "adaptive", "duration": 5}})
    stamp = _stamp(-60)
    a, b = f"cgt-{stamp}-a", f"cgt-{_stamp(-62)}-b"
    calls = {"lists": 0, "gets": 0, "downloads": 0}

    class Client:
        @classmethod
        def for_model(cls, model=None):
            return cls()

        def list_tasks(self, page_num=1, page_size=20):
            return {"items": [
                {"id": a, "model": MODEL, "status": "succeeded",
                 "resolution": "720p", "ratio": "16:9", "duration": 5},
                {"id": b, "model": MODEL, "status": "succeeded",
                 "resolution": "480p", "ratio": "adaptive", "duration": 5},
            ]}

        def get_task(self, rid):
            return {"status": "succeeded", "content": {"video_url": "https://cdn.test/v.mp4"}}

        def download(self, url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"v")
            return 1

        def close(self):
            pass

    monkeypatch.setattr(video, "ArkClient", Client)
    video.run_reconcile(session, Ctx())
    assert store.get(tid)["remote_task_id"] == b, "分辨率和参数一致的那个才是自己的"


def test_提交时刻被挤到后面几页也能翻到(tmp_path, monkeypatch):
    """任务多的时候，15 分钟前的提交可能已经在第 4 页往后——要按时间翻页，不能只翻 3 页。"""
    session, store, tid = _session(tmp_path, started_ago=800)
    decoys = [(f"cgt-{_stamp(-790 + i * 10)}-d{i:02d}", "doubao-seedance-2-0-260128", "succeeded")
              for i in range(60)]                      # 60 个更新的任务，占满 3 页
    target = (f"cgt-{_stamp(-800)}-target", MODEL, "succeeded")
    calls = _install_client(monkeypatch, decoys + [target])

    video.run_reconcile(session, Ctx())

    assert store.get(tid)["remote_task_id"] == target[0]
    assert calls["lists"] >= 4, "应该翻到第 4 页才找到"


def test_任务号里的时间能解出来():
    ts = video._task_stamp("cgt-20261028153616-c6m4m")
    assert ts and abs(ts - time.mktime(time.strptime("20261028153616", "%Y%m%d%H%M%S"))) < 1
    assert video._task_stamp("不是任务号") is None
