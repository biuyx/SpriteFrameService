"""抠图模型会话常驻：跨任务复用、限个数、同一模型不并发重复加载、启动预热。

用替身会话代替真实 ONNX 模型——这里测的是缓存策略，不是推理。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.core import background_remover as br
from app.core.background_remover import BackgroundRemover


class FakeSession:
    built = []                       # 每次真的「加载」都记一笔

    def __init__(self, model_path, model_name="u2net", force_cpu=False,
                 progress_callback=None):
        time.sleep(0.05)             # 模拟加载耗时，好让并发测试撞上
        self.model_name = model_name
        self.force_cpu = force_cpu
        FakeSession.built.append((model_name, force_cpu))


@pytest.fixture(autouse=True)
def fake_models(monkeypatch):
    monkeypatch.setattr(br, "U2NetLocalSession", FakeSession)
    monkeypatch.setattr(BackgroundRemover, "get_model_path",
                        staticmethod(lambda name: Path(f"/fake/{name}.onnx")))
    BackgroundRemover._model_cache.clear()
    BackgroundRemover._load_locks.clear()
    FakeSession.built = []
    yield
    BackgroundRemover._model_cache.clear()


def test_跨任务复用_只加载一次():
    for _ in range(3):                        # 三个任务各自新建 remover
        BackgroundRemover()._init_rembg("bria-rmbg-2.0")
    assert FakeSession.built == [("bria-rmbg-2.0", False)]


def test_缓存有上限_淘汰最久没用的():
    for name in ("u2net", "isnet-anime", "bria-rmbg-2.0"):
        BackgroundRemover()._init_rembg(name)
    keys = list(BackgroundRemover._model_cache)
    assert len(keys) == BackgroundRemover.MAX_CACHED
    assert "u2net_gpu" not in keys, "最早用的应被淘汰"


def test_刚用过的不被淘汰():
    BackgroundRemover()._init_rembg("u2net")
    BackgroundRemover()._init_rembg("isnet-anime")
    BackgroundRemover()._init_rembg("u2net")          # 又用了一次，变成最新
    BackgroundRemover()._init_rembg("bria-rmbg-2.0")
    assert "u2net_gpu" in BackgroundRemover._model_cache
    assert "isnet-anime_gpu" not in BackgroundRemover._model_cache


def test_被淘汰的会话正在用的任务手里仍可用():
    r = BackgroundRemover()
    r._init_rembg("u2net")
    held = r._rembg_session
    BackgroundRemover()._init_rembg("isnet-anime")
    BackgroundRemover()._init_rembg("bria-rmbg-2.0")   # u2net 被挤出缓存
    assert r._rembg_session is held, "跑到一半的任务不能被抽走会话"


def test_两个任务同时要同一个模型_只加载一份():
    """bria 一份就是 1G 显存 + 十几秒，不能因为两个任务同时开跑就加载两遍。"""
    barrier = threading.Barrier(4)

    def run():
        barrier.wait()
        BackgroundRemover()._init_rembg("bria-rmbg-2.0")

    ts = [threading.Thread(target=run) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert FakeSession.built == [("bria-rmbg-2.0", False)]


def test_CPU与GPU会话分开缓存():
    BackgroundRemover()._init_rembg("u2net", force_cpu=False)
    BackgroundRemover()._init_rembg("u2net", force_cpu=True)
    assert sorted(BackgroundRemover._model_cache) == ["u2net_cpu", "u2net_gpu"]


def test_请求里没指定force_cpu时听配置的(monkeypatch):
    """以前 params.get('force_cpu', 默认) 遇到值为 None 不回落，SPRITE_FORCE_CPU 从没生效。"""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "force_cpu", True)
    seen = {}
    monkeypatch.setattr(BackgroundRemover, "_init_rembg",
                        lambda self, m, force_cpu=False: seen.setdefault("cpu", force_cpu)
                        or (_ for _ in ()).throw(RuntimeError("stop")))
    import numpy as np
    with pytest.raises(Exception):
        BackgroundRemover()._remove_ai(np.zeros((4, 4, 3), np.uint8),
                                       {"model": "u2net", "force_cpu": None})
    assert seen["cpu"] is True


def test_预热加载默认模型():
    BackgroundRemover.warmup("bria-rmbg-2.0")
    assert "bria-rmbg-2.0_gpu" in BackgroundRemover._model_cache


def test_预热失败不抛异常(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("显卡炸了")
    monkeypatch.setattr(br, "U2NetLocalSession", boom)
    BackgroundRemover.warmup("bria-rmbg-2.0")       # 不应抛出
    assert not BackgroundRemover._model_cache


def test_模型文件不在时预热直接跳过(monkeypatch):
    monkeypatch.setattr(BackgroundRemover, "get_model_path", staticmethod(lambda n: None))
    BackgroundRemover.warmup("bria-rmbg-2.0")
    assert FakeSession.built == []
