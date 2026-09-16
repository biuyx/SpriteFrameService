"""接口层：抽帧规则归属校验、Spine 产物列表与删除。

全程只碰临时数据目录（见 conftest），不触真实 data/。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sprite(client):
    r = client.post("/api/sprites", json={"name": "测试角色"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def two_templates(tmp_path_factory):
    """两条模板，分别绑给两个动作，用来构造「写到别人模板上」的场景。"""
    from app.services.template_store import template_store
    made = []
    for key, variant in (("01_walk", "走路"), ("02_work", "工作")):
        try:
            made.append(template_store.add_video(f"{key} ({variant}).mp4", b"fake"))
        except ValueError:
            made.append(next(t for t in template_store.list()
                             if t["key"] == key and t["variant"] == variant))
    return made


# ------------------------------------------------------- 抽帧规则归属校验
def test_拒绝写入不属于该动作的模板(client, sprite, two_templates):
    own, other = two_templates
    a = client.post(f"/api/sprites/{sprite['id']}/actions",
                    json={"name": "走路"}).json()
    client.patch(f"/api/sprites/{sprite['id']}/actions/{a['id']}",
                 json={"template_id": own["id"]})
    client.post(f"/api/sprites/{sprite['id']}/actions/{a['id']}/open")

    r = client.post(f"/api/sessions/{a['id']}/frames/extract-rule",
                    json={"template_id": other["id"]})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "不属于动作" in detail
    assert "走路" in detail, "错误信息应指出该动作可写入哪个模板"


def test_拒绝不存在的模板(client, sprite, two_templates):
    own, _ = two_templates
    a = client.post(f"/api/sprites/{sprite['id']}/actions",
                    json={"name": "走路2"}).json()
    client.patch(f"/api/sprites/{sprite['id']}/actions/{a['id']}",
                 json={"template_id": own["id"]})
    client.post(f"/api/sprites/{sprite['id']}/actions/{a['id']}/open")

    r = client.post(f"/api/sessions/{a['id']}/frames/extract-rule",
                    json={"template_id": "tp_nope"})
    assert r.status_code == 400


def test_本动作的模板放行到下一步校验(client, sprite, two_templates):
    """归属校验通过后，才轮到「尚未抽帧」这类业务校验。"""
    own, _ = two_templates
    a = client.post(f"/api/sprites/{sprite['id']}/actions",
                    json={"name": "走路3"}).json()
    client.patch(f"/api/sprites/{sprite['id']}/actions/{a['id']}",
                 json={"template_id": own["id"]})
    client.post(f"/api/sprites/{sprite['id']}/actions/{a['id']}/open")

    r = client.post(f"/api/sessions/{a['id']}/frames/extract-rule",
                    json={"template_id": own["id"]})
    assert r.status_code == 400
    assert "尚未抽帧" in r.json()["detail"], \
        f"应越过归属校验、落到抽帧校验上，实际：{r.json()['detail']}"


def test_动作未绑模板时不拦(client, sprite, two_templates):
    """判定不出归属就放行，不挡住历史流程。"""
    own, _ = two_templates
    a = client.post(f"/api/sprites/{sprite['id']}/actions",
                    json={"name": "没绑模板"}).json()
    client.post(f"/api/sprites/{sprite['id']}/actions/{a['id']}/open")
    r = client.post(f"/api/sessions/{a['id']}/frames/extract-rule",
                    json={"template_id": own["id"]})
    assert "不属于动作" not in r.json().get("detail", "")


# ------------------------------------------------------- Spine 产物
def test_产物列表初始为空(client, sprite):
    r = client.get(f"/api/sprites/{sprite['id']}/spine/exports")
    assert r.status_code == 200
    assert r.json()["exports"] == []


def test_下载不存在的产物报404(client, sprite):
    r = client.get(f"/api/sprites/{sprite['id']}/spine/download", params={"name": "无"})
    assert r.status_code == 404


@pytest.mark.parametrize("bad", ["..", "%2e%2e", "....//"])
def test_删除产物挡住路径穿越(client, sprite, bad):
    r = client.delete(f"/api/sprites/{sprite['id']}/spine/exports/{bad}")
    assert r.status_code in (404, 405), f"{bad} 未被挡住：{r.status_code}"


def test_导出预检列出动作与动画名(client, sprite, two_templates):
    own, _ = two_templates
    a = client.post(f"/api/sprites/{sprite['id']}/actions",
                    json={"name": "走路"}).json()
    client.patch(f"/api/sprites/{sprite['id']}/actions/{a['id']}",
                 json={"template_id": own["id"]})
    r = client.get(f"/api/sprites/{sprite['id']}/spine/preview")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["action_id"] == a["id"] for i in items)
    assert all("anim" in i and "frames" in i for i in items)


def test_没有动作时导出报错(client, sprite):
    r = client.post(f"/api/sprites/{sprite['id']}/spine/export", json={})
    assert r.status_code == 400
    assert "没有可导出的动作" in r.json()["detail"]


# ------------------------------------------------------- Spine 模板库
def test_导入不存在的路径报错(client):
    r = client.post("/api/spine-templates/import", json={"path": "D:/不存在的目录"})
    assert r.status_code == 400
    assert "路径不存在" in r.json()["detail"]


def test_模板库初始为空(client):
    r = client.get("/api/spine-templates")
    assert r.status_code == 200
    assert isinstance(r.json()["templates"], list)
