"""AI 专用密钥：只存摘要、可吊销、权限受限，且只能走 /api/mcp。

不依赖 mcp 包——没装 SDK 的环境也要能验证「AI 密钥进不了平台其他接口」。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services.agent_keys import AgentKeyError, AgentKeyStore


@pytest.fixture
def store(tmp_path):
    return AgentKeyStore(path=tmp_path / "keys.json")


def test_明文只给一次_落盘的是摘要(store):
    rec, key = store.create("claude-code", ["read"])
    assert key.startswith("sfk_") and len(key) > 40
    raw = store.path.read_text(encoding="utf-8")
    assert key not in raw, "明文密钥绝不能落盘"
    assert "hash" in raw
    assert "hash" not in rec, "对外返回的记录不带摘要"


def test_校验与吊销(store):
    rec, key = store.create("a", ["read"])
    assert store.verify(key)["name"] == "a"
    assert store.verify(key + "x") is None
    assert store.verify("随便什么") is None
    store.revoke(rec["id"])
    assert store.verify(key) is None, "吊销立即生效"


def test_同名有效密钥不能重复_吊销后可以重发(store):
    rec, _ = store.create("a", ["read"])
    with pytest.raises(AgentKeyError, match="同名"):
        store.create("a", ["read"])
    store.revoke(rec["id"])
    store.create("a", ["read"])


def test_权限范围校验(store):
    with pytest.raises(AgentKeyError):
        store.create("a", ["admin"])
    with pytest.raises(AgentKeyError):
        store.create("a", [])
    rec, _ = store.create("b", ["generate", "read"])
    assert rec["scopes"] == ["read", "generate"], "按固定顺序存"


def test_记最近使用时间(store):
    rec, key = store.create("a", ["read"])
    assert rec["last_used_at"] is None
    store.verify(key)
    assert store.list()[0]["last_used_at"] is not None


# ---------------------------------------------------------------- 与认证中间件的配合
@pytest.fixture
def secured(monkeypatch):
    """开启平台认证（口令 T）。"""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "auth_token", "T")
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_AI密钥进不了平台的其他接口(secured):
    from app.services.agent_keys import agent_key_store
    _, key = agent_key_store.create("越权测试", ["read", "operate", "generate"])
    r = secured.get("/api/sprites", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401, "AI 只能走 /api/mcp 这条受控的路"
    assert secured.get("/api/sprites", headers={"Authorization": "Bearer T"}).status_code == 200


def test_MCP只认Bearer(secured):
    from app.security import COOKIE_NAME, cookie_value
    assert secured.post("/api/mcp", json={}).status_code == 401
    secured.cookies.set(COOKIE_NAME, cookie_value("T"))
    try:
        r = secured.post("/api/mcp", json={})
        assert r.status_code == 401, "浏览器自动带的 Cookie 不能驱动 AI 接口"
    finally:
        secured.cookies.clear()


def test_MCP认AI密钥与平台口令(secured):
    from app.services.agent_keys import agent_key_store
    _, key = agent_key_store.create("放行测试", ["read"])
    for token in (key, "T"):
        r = secured.post("/api/mcp", json={}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code != 401, token[:4]
