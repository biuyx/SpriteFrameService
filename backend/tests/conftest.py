"""测试夹具：把数据目录指向临时目录，绝不碰真实 data/。

get_settings 带 lru_cache，改环境变量后必须清缓存，否则拿到的还是旧配置。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture(scope="session", autouse=True)
def _isolated_data_dir(tmp_path_factory):
    """整轮测试共用一个临时数据目录。"""
    d = tmp_path_factory.mktemp("spriteframe-data")
    old = os.environ.get("SPRITE_DATA_DIR")
    os.environ["SPRITE_DATA_DIR"] = str(d)
    os.environ.setdefault("SPRITE_AUTH_TOKEN", "")      # 测试不走认证

    from app.config import get_settings
    get_settings.cache_clear()
    assert get_settings().resolved_data_dir == d, "数据目录未隔离，测试拒绝继续"

    yield d

    if old is None:
        os.environ.pop("SPRITE_DATA_DIR", None)
    else:
        os.environ["SPRITE_DATA_DIR"] = old
    get_settings.cache_clear()


class FakeCtx:
    """替身 JobContext：记录上报、可模拟取消。"""

    def __init__(self):
        self.reports = []
        self._cancelled = False
        self.cancel_callbacks = []

    def report(self, percent, message=""):
        self.reports.append((float(percent), message))

    def cancelled(self):
        return self._cancelled

    def cancel(self):
        self._cancelled = True

    def register_cancel(self, fn):
        self.cancel_callbacks.append(fn)

    @property
    def messages(self):
        return [m for _, m in self.reports]

    @property
    def percents(self):
        return [p for p, _ in self.reports]


@pytest.fixture
def ctx():
    return FakeCtx()


@pytest.fixture
def rgba():
    """造一张 RGBA 测试图：size×size 画布，中间一块不透明方块。"""
    import numpy as np

    def _make(size=64, box=24, color=(200, 100, 50)):
        img = np.zeros((size, size, 4), np.uint8)
        a = (size - box) // 2
        img[a:a + box, a:a + box, :3] = color
        img[a:a + box, a:a + box, 3] = 255
        return img

    return _make
