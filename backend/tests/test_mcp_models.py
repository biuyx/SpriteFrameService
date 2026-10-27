"""MCP 里暴露「亚太·无预过滤」备选：体检/平台状态给出可选模型，计划可指定模型。"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.mcp_server import generation, queries

BP_IMAGE = "ep-20261027150254-4cxlt"
BP_VIDEO = "ep-20261027144709-pwhn6"


@pytest.fixture
def apac(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "byteplus_api_key", "ark-test-byteplus")
    return s


def _sprite():
    from app.services.sprite_store import sprite_store
    import uuid
    return sprite_store.create_sprite("mcp-model-" + uuid.uuid4().hex[:6])


def test_体检里给出可选模型与亚太备选(apac):
    sp = _sprite()
    out = queries.readiness(sp["id"])
    models = out["models"]
    assert models["首帧生图"]["默认"] == get_settings().default_image_model
    assert any(m["id"] == BP_IMAGE for m in models["首帧生图"]["可选"])
    assert any(m["id"] == BP_VIDEO for m in models["视频"]["可选"])
    apac_block = models["无预过滤备选"]
    assert BP_IMAGE in apac_block["图片"] and BP_VIDEO in apac_block["视频"]
    assert "审核" in apac_block["说明"] and "慢" in apac_block["说明"]


def test_没配亚太密钥时体检不提亚太(monkeypatch):
    monkeypatch.setattr(get_settings(), "byteplus_api_key", "")
    sp = _sprite()
    out = queries.readiness(sp["id"])
    assert "无预过滤备选" not in out["models"]
    assert all(m["通道"] != "byteplus" for m in out["models"]["首帧生图"]["可选"])


def test_平台状态里有模型清单(apac):
    st = queries.platform_status()
    models = st["生图/视频模型"]
    assert any(m["id"] == BP_IMAGE for m in models["首帧生图"]["可选"])
    assert any(m["id"] == BP_VIDEO for m in models["视频"]["可选"])


def test_计划可以指定亚太模型(apac, monkeypatch, tmp_path):
    """审核被拦时，计划里能明确换成亚太的无预过滤模型。"""
    from app.mcp_server import generation as gen
    from app.core import first_frame_generator
    from app.core import pipeline
    from app.services import prompt_store

    sp = _sprite()
    from app.services.sprite_store import sprite_store
    action = sprite_store.create_action(sp["id"], "walk")
    monkeypatch.setattr(pipeline, "plan_action", lambda *a, **k: {
        "steps": {"firstframe": {"status": "run"}}, "runnable": True})
    monkeypatch.setattr(pipeline, "check_firstframe", lambda *a, **k: {"set_id": "fs1"})
    monkeypatch.setattr(first_frame_generator, "resolve_refs",
                        lambda *a, **k: (b"art", b"ref", "walk", "front"))
    monkeypatch.setattr(gen, "get_session", lambda aid: None)
    monkeypatch.setattr(prompt_store, "resolve_for_action",
                        lambda scope, item: {"text": "p", "prompt_id": None,
                                             "version": None, "name": "内置"})
    plan = gen.plan_generation(sp["id"], [action["id"]], ["firstframe"], image_model=BP_IMAGE)
    assert plan["blockers"] == []
    assert plan["items"][0]["model"] == BP_IMAGE
    assert plan["items"][0]["recipe"]


def test_计划拒绝不存在的模型(apac):
    sp = _sprite()
    with pytest.raises(queries.ToolInputError) as e:
        generation.plan_generation(sp["id"], [], ["firstframe"], image_model="no-such-model")
    assert "不在可选列表" in str(e.value)


def test_计划拒绝不在列表的视频模型(apac):
    sp = _sprite()
    with pytest.raises(queries.ToolInputError) as e:
        generation.plan_generation(sp["id"], [], ["generate"], video_model="doubao-nope")
    assert "video_model" in str(e.value)
