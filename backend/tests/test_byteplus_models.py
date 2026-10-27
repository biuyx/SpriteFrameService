"""亚太接入点（无预过滤）：模型只在配好密钥后出现，并按模型自动切换接入点。"""
from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.core.ark_client import ArkClient
from app.core.image_gen import provider_for


BP_VIDEO = "ep-20261027144709-pwhn6"
BP_IMAGE = "ep-20261027150254-4cxlt"


@pytest.fixture
def no_bp(monkeypatch):
    monkeypatch.setattr(get_settings(), "byteplus_api_key", "")
    return get_settings()


@pytest.fixture
def with_bp(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "byteplus_api_key", "ark-test-byteplus-key")
    monkeypatch.setattr(s, "byteplus_base_url", "https://ark.ap-southeast.bytepluses.com/api/v3")
    return s


def test_没配密钥时亚太模型不出现在下拉里(no_bp):
    assert no_bp.byteplus_models_list == []
    assert no_bp.byteplus_image_models_list == []
    assert all(m["provider"] == "ark" for m in no_bp.video_models_list)
    assert all(m["provider"] != "byteplus" for m in no_bp.image_models_list)


def test_配了密钥后视频和图片都有亚太备选(with_bp):
    videos = with_bp.video_models_list
    bp_videos = [m for m in videos if m["provider"] == "byteplus"]
    assert len(bp_videos) == 4, "Seedance 2.0 / Fast / Mini / 2.5"
    assert {BP_VIDEO, "ep-20261027150350-c2l6q", "ep-20261027150430-87g7q",
            "ep-20261027144026-6dxbq"} <= {m["id"] for m in bp_videos}
    assert all("无预过滤" in m["label"] for m in bp_videos)

    images = with_bp.image_models_list
    bp_images = [m for m in images if m["provider"] == "byteplus"]
    assert [m["id"] for m in bp_images] == [BP_IMAGE]
    assert "无预过滤" in bp_images[0]["label"]

    # 默认模型不受影响
    assert with_bp.default_image_model != BP_IMAGE


def test_按模型选接入点(with_bp):
    assert with_bp.ark_provider_for(BP_VIDEO) == "byteplus"
    assert with_bp.ark_provider_for(BP_IMAGE) == "byteplus"
    assert with_bp.ark_provider_for("doubao-seedance-2-0-mini-260615") == "ark"
    assert with_bp.ark_provider_for(None) == "ark"

    base, key = with_bp.ark_endpoint("byteplus")
    assert base == "https://ark.ap-southeast.bytepluses.com/api/v3"
    assert key == "ark-test-byteplus-key"


def test_视频客户端按模型切换接入点(with_bp, monkeypatch):
    captured = {}

    class FakeHttpClient:
        def __init__(self, *a, **kw):
            captured["headers"] = kw.get("headers")

        def close(self):
            pass

    monkeypatch.setattr("app.core.ark_client.httpx.Client", FakeHttpClient)
    client = ArkClient.for_model(BP_VIDEO)
    assert client._base == "https://ark.ap-southeast.bytepluses.com/api/v3"
    assert captured["headers"]["Authorization"] == "Bearer ark-test-byteplus-key"
    client.close()

    client = ArkClient.for_model("doubao-seedance-2-0-mini-260615")
    assert client._base.startswith("https://ark.cn-beijing.volces.com")
    client.close()


def test_没配密钥却用了亚太模型时报清楚(with_bp, monkeypatch):
    monkeypatch.setattr(with_bp, "byteplus_api_key", "")
    with pytest.raises(Exception) as e:
        ArkClient.for_model(BP_VIDEO)
    assert "SPRITE_BYTEPLUS_API_KEY" in str(e.value)


def test_生图按模型走亚太接入点(with_bp, monkeypatch):
    called = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": [{"url": "https://cdn.test/x.png"}]}

    def fake_post(self, url, json=None, headers=None, **kw):
        called["url"] = url
        called["auth"] = headers["Authorization"]
        return FakeResp()

    monkeypatch.setattr("app.core.ark_image._post_generation",
                        lambda client, url, payload, headers, **kw: fake_post(
                            None, url, payload, headers))
    monkeypatch.setattr("app.core.ark_image._fetch_result", lambda *a, **kw: b"img")
    from app.core.ark_image import generate_image
    assert generate_image("x", None, model=BP_IMAGE) == b"img"
    assert called["url"].startswith("https://ark.ap-southeast.bytepluses.com")
    assert called["auth"] == "Bearer ark-test-byteplus-key"
    assert provider_for(BP_IMAGE) == "byteplus"


def test_能力接口在配好后列出亚太模型(with_bp):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        d = c.get("/api/generate/capabilities").json()
    ids = {m["id"]: m["provider"] for m in d["models"]}
    assert ids.get(BP_VIDEO) == "byteplus"
    assert d["byteplus_configured"] is True
