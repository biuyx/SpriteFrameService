"""LocalAI 生图通道：通道选择、登录/重登、提交轮询、参考图转公网 URL、错误提示。

全部用假的 HTTP 层，不打真实网关。
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core import localai_image
from app.core.image_gen import ImageGenError, generate, provider_for, resolve_model


@pytest.fixture
def localai(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "localai_base_url", "http://localai.test:8081")
    monkeypatch.setattr(s, "localai_username", "sprite-service")
    monkeypatch.setattr(s, "localai_password", "secret")
    monkeypatch.setattr(s, "localai_poll_seconds", 0.01)
    monkeypatch.setattr(s, "localai_timeout_seconds", 5)
    localai_image._url_cache.clear()
    localai_image._token.update({"value": "", "at": 0.0})
    return s


class FakeResp:
    def __init__(self, status=200, body=None, content=b"", text=""):
        self.status_code = status
        self._body = body
        self.content = content
        self.text = text or json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def test_模型决定走哪条通道(localai):
    s = localai
    monkeypatch_ids = [m["id"] for m in s.localai_image_models_list]
    assert provider_for(monkeypatch_ids[0]) == "localai"
    assert provider_for("gpt-image-2.5-sunburst") == "localai"
    assert provider_for("doubao-seedream-5-0-pro-260628") == "ark"
    # 没给模型：LocalAI 配好时默认用它
    assert provider_for(None) == "localai"


def test_没配LocalAI时回退方舟(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "localai_base_url", "")
    monkeypatch.setattr(s, "localai_username", "")
    monkeypatch.setattr(s, "localai_password", "")
    assert provider_for(None) == "ark"
    assert resolve_model(None) == ("ark", s.ark_image_model)


def test_生成一张图_提交轮询下载(localai, monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None, **kw):
        calls.append(("POST", url, json))
        assert url.endswith("/api/auth/login")
        return FakeResp(body={"code": 200, "data": {"token": "jwt-1"}})

    def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
        calls.append((method, url, json, headers.get("Authorization")))
        if method == "POST" and url.endswith("/api/ai/gateway-image"):
            return FakeResp(body={"id": "task-1", "status": "QUEUED"})
        if method == "GET" and "task-1" in url:
            return FakeResp(body={"id": "task-1", "status": "SUCCESS",
                                  "savedUrls": ["http://oss.test/a.png"]})
        raise AssertionError(f"意外的请求 {method} {url}")

    def fake_get(url, timeout=None, **kw):
        assert url == "http://oss.test/a.png"
        return FakeResp(content=b"\x89PNG-fake")

    monkeypatch.setattr(localai_image.httpx, "post", fake_post)
    monkeypatch.setattr(localai_image.httpx, "request", fake_request)
    monkeypatch.setattr(localai_image.httpx, "get", fake_get)
    monkeypatch.setattr("app.core.oss_uploader.upload_bytes",
                        lambda data, key, ct: f"http://oss.test/{key.rsplit('/', 1)[-1]}")

    data = generate("画一个角色", [b"art", b"ref"], model="gpt-image-2.5-sunburst")
    assert data == b"\x89PNG-fake"
    submit = next(c for c in calls if c[0] == "POST" and c[1].endswith("/gateway-image"))
    body = submit[2]
    assert body["model"] == "gpt-image-2.5-sunburst"
    urls = body["imageUrls"]
    assert len(urls) == 2 and all(u.startswith("http://oss.test/") for u in urls), \
        "参考图要转成公网 URL"
    assert len(set(urls)) == 2, "两张不同的图应各传各的"
    assert "gateway" not in json.dumps(body), "网关接口不接受 provider 字段"
    assert all(c[3] in (None, "Bearer jwt-1") for c in calls if c[0] != "POST")


def test_令牌过期自动重登(localai, monkeypatch):
    logins = []

    def fake_post(url, json=None, timeout=None, **kw):
        logins.append(1)
        return FakeResp(body={"data": {"token": f"jwt-{len(logins)}"}})

    state = {"n": 0}

    def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResp(status=401, body={"error": "expired"})
        if method == "POST":
            assert headers["Authorization"] == "Bearer jwt-2"
            return FakeResp(body={"id": "t2", "status": "QUEUED"})
        return FakeResp(body={"status": "SUCCESS", "savedUrls": ["http://oss.test/b.png"]})

    monkeypatch.setattr(localai_image.httpx, "post", fake_post)
    monkeypatch.setattr(localai_image.httpx, "request", fake_request)
    monkeypatch.setattr(localai_image.httpx, "get", lambda url, timeout=None: FakeResp(content=b"ok"))
    monkeypatch.setattr("app.core.oss_uploader.upload_bytes", lambda *a: "http://oss.test/r.png")

    assert generate("x", None) == b"ok"
    assert len(logins) == 2, "401 后应重登一次"


def test_上游失败时说清换哪个模型(localai, monkeypatch):
    monkeypatch.setattr(localai_image.httpx, "post",
                        lambda url, json=None, timeout=None, **kw: FakeResp(body={"data": {"token": "t"}}))

    def fake_request(method, url, headers=None, json=None, timeout=None, **kw):
        if method == "POST":
            return FakeResp(body={"id": "t3", "status": "QUEUED"})
        return FakeResp(body={"status": "FAILURE",
                              "error": {"code": "X", "message": "模型暂时不可用"}})

    monkeypatch.setattr(localai_image.httpx, "request", fake_request)
    with pytest.raises(ImageGenError) as e:
        generate("x", None, model="gpt-image-2.5-sunburst")
    msg = str(e.value)
    assert "暂时不可用" in msg and "gpt-image-2" in msg and "不计费" in msg


def test_没权限时给出明确提示(localai, monkeypatch):
    monkeypatch.setattr(localai_image.httpx, "post",
                        lambda url, json=None, timeout=None, **kw: FakeResp(body={"data": {"token": "t"}}))
    monkeypatch.setattr(localai_image.httpx, "request",
                        lambda method, url, **kw: FakeResp(status=403, body={"error": "forbidden"}))
    with pytest.raises(ImageGenError) as e:
        generate("x", None)
    assert "ai:image" in str(e.value)


def test_登录失败提示核对账号(localai, monkeypatch):
    monkeypatch.setattr(localai_image.httpx, "post",
                        lambda url, json=None, timeout=None, **kw: FakeResp(status=401, body={"code": 401}))
    with pytest.raises(ImageGenError) as e:
        generate("x", None)
    assert "SPRITE_LOCALAI_USERNAME" in str(e.value)


def test_没配LocalAI时界面不列它的模型(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    s = get_settings()
    monkeypatch.setattr(s, "localai_base_url", "")
    monkeypatch.setattr(s, "localai_username", "")
    monkeypatch.setattr(s, "localai_password", "")
    with TestClient(app) as c:
        d = c.get("/api/generate/capabilities").json()
    assert {m["provider"] for m in d["image_models"]} == {"ark"}, "没配好就不该让它出现在下拉里"
    assert d["default_image_model"] == s.ark_image_model


def test_设置接口能配LocalAI(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.api import settings_api
    from app.main import app
    monkeypatch.setattr(settings_api, "_ENV_PATH", tmp_path / ".env")
    with TestClient(app) as c:
        r = c.put("/api/settings", json={"localai_base_url": "http://localai.test:8081",
                                         "localai_username": "sprite",
                                         "localai_password": "pw"})
        assert r.status_code == 200, r.text
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SPRITE_LOCALAI_BASE_URL=http://localai.test:8081" in text
    assert "SPRITE_LOCALAI_PASSWORD=pw" in text


def test_能力接口暴露图片模型(localai):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        d = c.get("/api/generate/capabilities").json()
    ids = {m["id"]: m["provider"] for m in d["image_models"]}
    assert ids.get("gpt-image-2.5-sunburst") == "localai"
    assert ids.get("doubao-seedream-5-0-pro-260628") == "ark"
    assert d["default_image_model"] == "gpt-image-2.5-sunburst"
    assert d["localai_configured"] is True
