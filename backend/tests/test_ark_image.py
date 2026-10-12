"""生图结果的取回：网络抖一下不该把已经计费的那张图丢掉。"""
from __future__ import annotations

import httpx
import pytest

from app.core.ark_image import (ArkImageError, _fetch_result,
                                _post_generation)

URL = "https://ark-content.tos-cn-beijing.volces.com/abc.png?sig=1"


class FakeResp:
    def __init__(self, content=b"PNGDATA", status=200):
        self.content = content
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)


class FakeClient:
    """按脚本依次抛异常/返回，记录调用次数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def _next(self):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url):
        return self._next()

    def post(self, url, json=None, headers=None):
        return self._next()


def test_一次就成功不重试():
    c = FakeClient([FakeResp()])
    assert _fetch_result(c, URL) == b"PNGDATA"
    assert c.calls == 1


def test_连接超时会重试(monkeypatch):
    monkeypatch.setattr("app.core.ark_image.time.sleep", lambda *_: None)
    c = FakeClient([httpx.ConnectTimeout("timed out"), FakeResp(b"OK")])
    assert _fetch_result(c, URL) == b"OK"
    assert c.calls == 2, "第一次超时后应该再试"


def test_一直不通就说清楚是哪台服务器(monkeypatch):
    monkeypatch.setattr("app.core.ark_image.time.sleep", lambda *_: None)
    c = FakeClient([httpx.ConnectTimeout("timed out")] * 4)
    with pytest.raises(ArkImageError) as e:
        _fetch_result(c, URL)
    msg = str(e.value)
    assert "ark-content.tos-cn-beijing.volces.com" in msg, "要指出连不上谁"
    assert "已计费" in msg, "图已经生成了，别让人以为白跑一趟"
    assert "SPRITE_ARK_IMAGE_B64" in msg, "给出长期不通时的出路"
    assert c.calls == 4


def test_重试次数可调(monkeypatch):
    monkeypatch.setattr("app.core.ark_image.time.sleep", lambda *_: None)
    c = FakeClient([httpx.ConnectTimeout("x")] * 5)
    with pytest.raises(ArkImageError):
        _fetch_result(c, URL, attempts=5)
    assert c.calls == 5


# ---------------------------------------------------------------- 发起请求
API = "https://ark.cn-beijing.volces.com/api/v3/images/generations"


def _post(c, attempts=3):
    return _post_generation(c, API, {"model": "m"}, {"Authorization": "Bearer x"},
                            attempts=attempts)


def test_连不上接口时重试(monkeypatch):
    monkeypatch.setattr("app.core.ark_image.time.sleep", lambda *_: None)
    c = FakeClient([httpx.ConnectError("refused"), FakeResp(b"ok")])
    assert _post(c) is not None
    assert c.calls == 2, "连接阶段失败＝请求没发出去，重试不会重复计费"


def test_已经连上之后的错误不重试(monkeypatch):
    """读超时可能意味着服务端已经在生成了，重发会变成生成两次、计费两次。"""
    monkeypatch.setattr("app.core.ark_image.time.sleep", lambda *_: None)
    c = FakeClient([httpx.ReadTimeout("slow"), FakeResp(b"ok")])
    with pytest.raises(httpx.ReadTimeout):
        _post(c)
    assert c.calls == 1


def test_一直连不上就报清楚(monkeypatch):
    monkeypatch.setattr("app.core.ark_image.time.sleep", lambda *_: None)
    c = FakeClient([httpx.ConnectTimeout("t")] * 3)
    with pytest.raises(ArkImageError) as e:
        _post(c)
    assert "ark.cn-beijing.volces.com" in str(e.value)
    assert c.calls == 3
