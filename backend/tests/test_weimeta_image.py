"""生图网关直连：通道选择、提交/轮询/下载、参考图转公网 URL、错误提示、设置接口。

全部用假的 HTTP 层，不打真实网关。
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core import weimeta_image
from app.core.image_gen import ImageGenError, generate, provider_for, resolve_model


@pytest.fixture
def gateway(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "weimeta_base_url", "https://gw.test")
    monkeypatch.setattr(s, "weimeta_api_key", "sk-test-key-1234567890")
    monkeypatch.setattr(s, "weimeta_poll_seconds", 0.01)
    monkeypatch.setattr(s, "weimeta_timeout_seconds", 5)
    weimeta_image._url_cache.clear()
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


def test_模型决定走哪条通道(gateway):
    assert provider_for("gpt-image-2.5-sunburst") == "weimeta"
    assert provider_for("gpt-image-2") == "weimeta"
    assert provider_for("doubao-seedream-5-0-pro-260628") == "ark"
    assert provider_for(None) == "weimeta", "网关密钥配好时默认走网关"


def test_没配网关时回退方舟(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "weimeta_api_key", "")
    assert provider_for(None) == "ark"
    assert resolve_model(None) == ("ark", s.ark_image_model)


def test_提交轮询下载一条龙(gateway, monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls.append(("POST", url, json, headers["Authorization"]))
        assert url == "https://gw.test/v2/images/generations"
        return FakeResp(body={"id": "task-1", "status": "QUEUED"})

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(("GET", url))
        if url.startswith("https://gw.test/v2/tasks/"):
            return FakeResp(body={"id": "task-1", "status": "SUCCESS",
                                  "output": {"images": [{"url": "https://cdn.test/a.png"}]},
                                  "usage": {"quota": 68500}})
        assert url == "https://cdn.test/a.png"
        return FakeResp(content=b"\x89PNG-fake")

    monkeypatch.setattr(weimeta_image.httpx, "post", fake_post)
    monkeypatch.setattr(weimeta_image.httpx, "get", fake_get)
    monkeypatch.setattr("app.core.oss_uploader.upload_bytes",
                        lambda data, key, ct: f"https://oss.test/{key.rsplit('/', 1)[-1]}")

    data = generate("画一个角色", [b"art", b"ref"], model="gpt-image-2.5-sunburst")
    assert data == b"\x89PNG-fake"
    submit = calls[0]
    assert submit[3] == "Bearer sk-test-key-1234567890"
    body = submit[2]
    assert body["model"] == "gpt-image-2.5-sunburst"
    urls = body["image_urls"]
    assert len(urls) == 2 and all(u.startswith("https://oss.test/") for u in urls), \
        "参考图要换成公网 URL 再交给网关"
    assert len(set(urls)) == 2


def test_模型不存在时说明未计费(gateway, monkeypatch):
    monkeypatch.setattr(weimeta_image.httpx, "post",
                        lambda *a, **k: FakeResp(status=503, body={
                            "error": {"code": "model_not_found",
                                      "message": "模型不存在或暂未开放，请核对模型名"}}))
    with pytest.raises(ImageGenError) as e:
        generate("x", None, model="nope-model")
    msg = str(e.value)
    assert "没有这个模型" in msg and "未计费" in msg


def test_密钥无效时提示换密钥(gateway, monkeypatch):
    monkeypatch.setattr(weimeta_image.httpx, "post",
                        lambda *a, **k: FakeResp(status=401, body={"error": "unauthorized"}))
    with pytest.raises(ImageGenError) as e:
        generate("x", None)
    assert "WEIMETA_API_KEY" in str(e.value)


def test_上游失败时说清换哪个模型(gateway, monkeypatch):
    monkeypatch.setattr(weimeta_image.httpx, "post",
                        lambda *a, **k: FakeResp(body={"id": "t3", "status": "QUEUED"}))
    monkeypatch.setattr(weimeta_image.httpx, "get",
                        lambda *a, **k: FakeResp(body={"status": "FAILURE",
                                                       "error": {"message": "模型暂时不可用"}}))
    with pytest.raises(ImageGenError) as e:
        generate("x", None, model="gpt-image-2.5-sunburst")
    msg = str(e.value)
    assert "暂时不可用" in msg and "gpt-image-2" in msg and "不计费" in msg


def test_未配置网关时给出明确提示(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "weimeta_api_key", "")
    from app.core.weimeta_image import WeimetaError
    with pytest.raises(WeimetaError) as e:
        weimeta_image.generate_image("x", None)
    assert "SPRITE_WEIMETA_API_KEY" in str(e.value)


def test_没配网关时界面不列它的模型(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    s = get_settings()
    monkeypatch.setattr(s, "weimeta_api_key", "")
    with TestClient(app) as c:
        d = c.get("/api/generate/capabilities").json()
    assert {m["provider"] for m in d["image_models"]} == {"ark"}
    assert d["default_image_model"] == s.ark_image_model


def test_配好网关后默认是sunburst(gateway):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        d = c.get("/api/generate/capabilities").json()
    ids = {m["id"]: m["provider"] for m in d["image_models"]}
    assert ids.get("gpt-image-2.5-sunburst") == "weimeta"
    assert ids.get("doubao-seedream-5-0-pro-260628") == "ark"
    assert d["default_image_model"] == "gpt-image-2.5-sunburst"
    assert d["weimeta_configured"] is True


def test_设置接口能配网关密钥(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.api import settings_api
    from app.main import app
    monkeypatch.setattr(settings_api, "_ENV_PATH", tmp_path / ".env")
    with TestClient(app) as c:
        r = c.put("/api/settings", json={"weimeta_base_url": "https://gw.test",
                                         "weimeta_api_key": "sk-abcdefghijklmnop"})
        assert r.status_code == 200, r.text
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SPRITE_WEIMETA_API_KEY=sk-abcdefghijklmnop" in text
    assert "SPRITE_WEIMETA_BASE_URL=https://gw.test" in text


def test_设置接口只回打码后的密钥(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    s = get_settings()
    monkeypatch.setattr(s, "weimeta_api_key", "sk-abcdefghijklmnop")
    with TestClient(app) as c:
        st = c.get("/api/settings").json()
    assert st["weimeta"]["configured"] is True
    assert st["weimeta"]["api_key_masked"] == "sk-a…mnop"
    assert "sk-abcdefghijklmnop" not in json.dumps(st, ensure_ascii=False)
