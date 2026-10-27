"""上传/提交的重试：传输层抖动要重试，签名或权限错误不要重试。"""
from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.core import oss_uploader, weimeta_image
from app.core.image_gen import ImageGenError, generate


class Resp:
    def __init__(self, status=200, body=None, text="", content=b""):
        self.status_code = status
        self._body = body
        self.text = text
        self.content = content

    def json(self):
        return self._body if self._body is not None else {}


DNS_ERROR = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")


@pytest.fixture
def oss(monkeypatch):
    env = {"OSS_BUCKET": "b", "OSS_PUBLIC_DOMAIN": "http://oss.test",
           "ALIBABA_CLOUD_ACCESS_KEY_ID": "ak", "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "sk"}
    monkeypatch.setattr(oss_uploader, "_env", lambda *names: env.get(names[0], ""))
    monkeypatch.setattr(oss_uploader.time, "sleep", lambda s: None)
    return oss_uploader


def test_OSS上传遇到网络抖动会重试(oss, monkeypatch):
    calls = {"n": 0}

    def fake_put(url, content=None, headers=None, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DNS_ERROR
        return Resp(status=200)

    monkeypatch.setattr(oss_uploader.httpx, "put", fake_put)
    assert oss.upload_bytes(b"x", "tmp/a.png", "image/png") == "http://oss.test/tmp/a.png"
    assert calls["n"] == 2, "第一次解析失败后应重试一次"


def test_OSS上传签名错误不重试(oss, monkeypatch):
    calls = {"n": 0}

    def fake_put(*a, **kw):
        calls["n"] += 1
        return Resp(status=403, text="SignatureDoesNotMatch")

    monkeypatch.setattr(oss_uploader.httpx, "put", fake_put)
    with pytest.raises(oss_uploader.OssError) as e:
        oss.upload_bytes(b"x", "tmp/a.png", "image/png")
    assert "403" in str(e.value) and calls["n"] == 1, "4xx 重试没用"


def test_OSS连续失败时报清楚是DNS(oss, monkeypatch):
    def always(*a, **kw):
        raise DNS_ERROR

    monkeypatch.setattr(oss_uploader.httpx, "put", always)
    with pytest.raises(oss_uploader.OssError) as e:
        oss.upload_bytes(b"x", "tmp/a.png", "image/png")
    assert "重试 3 次" in str(e.value) and "域名解析" in str(e.value)


@pytest.fixture
def gateway(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "weimeta_base_url", "https://gw.test")
    monkeypatch.setattr(s, "weimeta_api_key", "sk-test-key-1234567890")
    monkeypatch.setattr(s, "weimeta_poll_seconds", 0.01)
    monkeypatch.setattr(s, "weimeta_timeout_seconds", 3)
    monkeypatch.setattr(weimeta_image.time, "sleep", lambda s: None)
    weimeta_image._url_cache.clear()
    return s


def _fake_get(url, headers=None, timeout=None, **kw):
    if "gw.test" in url:
        return Resp(body={"status": "SUCCESS",
                          "output": {"images": [{"url": "https://cdn/x.png"}]}})
    return Resp(content=b"img")


def test_提交时DNS失败会重试(gateway, monkeypatch):
    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DNS_ERROR
        return Resp(body={"id": "t1", "status": "QUEUED"})

    monkeypatch.setattr(weimeta_image.httpx, "post", fake_post)
    monkeypatch.setattr(weimeta_image.httpx, "get", _fake_get)
    assert generate("x", None, model="gpt-image-2.5-sunburst") == b"img"
    assert calls["n"] == 2


def test_提交时连接被掐不重试(gateway, monkeypatch):
    """不是域名问题的连接失败：请求可能已到达网关，重试会重复扣费。"""
    calls = {"n": 0}

    def fake_post(*a, **kw):
        calls["n"] += 1
        raise httpx.ConnectError("Connection reset by peer")

    monkeypatch.setattr(weimeta_image.httpx, "post", fake_post)
    with pytest.raises(ImageGenError) as e:
        generate("x", None, model="gpt-image-2.5-sunburst")
    assert calls["n"] == 1
    assert "网关后台确认" in str(e.value)
