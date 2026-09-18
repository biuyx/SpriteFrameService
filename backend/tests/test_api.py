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


def test_可以后期把模板绑定到动作(client, sprite, two_templates):
    """界面上此前没有绑定入口，只能手调接口；现在生成页有按钮，接口这条路要稳。"""
    own, _ = two_templates
    a = client.post(f"/api/sprites/{sprite['id']}/actions",
                    json={"name": "待绑定"}).json()
    assert not a.get("template_id"), "新建动作默认不绑模板"

    r = client.patch(f"/api/sprites/{sprite['id']}/actions/{a['id']}",
                     json={"template_id": own["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["template_id"] == own["id"]

    again = next(x for x in client.get(f"/api/sprites/{sprite['id']}/actions").json()["actions"]
                 if x["id"] == a["id"])
    assert again["template_id"] == own["id"], "绑定未落库"


def test_绑定后抽帧规则校验放行该模板(client, sprite, two_templates):
    own, other = two_templates
    a = client.post(f"/api/sprites/{sprite['id']}/actions",
                    json={"name": "绑定后"}).json()
    client.post(f"/api/sprites/{sprite['id']}/actions/{a['id']}/open")

    # 绑之前：判定不出归属，不拦
    before = client.post(f"/api/sessions/{a['id']}/frames/extract-rule",
                         json={"template_id": other["id"]})
    assert "不属于动作" not in before.json().get("detail", "")

    client.patch(f"/api/sprites/{sprite['id']}/actions/{a['id']}",
                 json={"template_id": own["id"]})
    # 绑之后：别人的模板被拦下，自己的放行到下一步校验
    blocked = client.post(f"/api/sessions/{a['id']}/frames/extract-rule",
                          json={"template_id": other["id"]})
    assert "不属于动作" in blocked.json()["detail"]
    allowed = client.post(f"/api/sessions/{a['id']}/frames/extract-rule",
                          json={"template_id": own["id"]})
    assert "尚未抽帧" in allowed.json()["detail"]


def test_改绑到另一个模板(client, sprite, two_templates):
    own, other = two_templates
    a = client.post(f"/api/sprites/{sprite['id']}/actions",
                    json={"name": "改绑"}).json()
    client.patch(f"/api/sprites/{sprite['id']}/actions/{a['id']}",
                 json={"template_id": own["id"]})
    r = client.patch(f"/api/sprites/{sprite['id']}/actions/{a['id']}",
                     json={"template_id": other["id"]})
    assert r.json()["template_id"] == other["id"], "应能改绑而不是只能绑一次"


# ------------------------------------------------------- 素材速览
def test_视频速览列出全部动作(client, sprite):
    """一次拿齐，前端不必为每个动作单独发请求。"""
    names = ["走路", "待机", "睡觉"]
    for n in names:
        client.post(f"/api/sprites/{sprite['id']}/actions", json={"name": n})
    r = client.get(f"/api/sprites/{sprite['id']}/videos")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["sprite"] == sprite["name"]
    assert sorted(i["name"] for i in d["items"]) == sorted(names)


def test_没有素材时字段仍然齐全(client, sprite):
    """前端按固定字段渲染，缺素材不能少字段。"""
    client.post(f"/api/sprites/{sprite['id']}/actions", json={"name": "空动作"})
    item = client.get(f"/api/sprites/{sprite['id']}/videos").json()["items"][0]
    for k in ("action_id", "name", "status", "has_first_frame",
              "take_id", "takes", "generating", "video"):
        assert k in item, f"缺字段 {k}"
    assert item["take_id"] is None and item["video"] is None
    assert item["takes"] == 0 and item["generating"] == 0


def test_只统计成功的版本并取当前使用的那个(client, sprite):
    import json as _json
    from app.services.sprite_store import sprite_store

    a = client.post(f"/api/sprites/{sprite['id']}/actions",
                    json={"name": "多版本"}).json()
    vd = sprite_store.action_dir(sprite["id"], a["id"]) / "video"
    vd.mkdir(parents=True, exist_ok=True)
    (vd / "takes.json").write_text(_json.dumps({
        "current": "t_ok2",
        "takes": [
            {"id": "t_ok1", "status": "succeeded", "bytes": 1},
            {"id": "t_bad", "status": "error"},
            {"id": "t_run", "status": "running"},
            {"id": "t_ok2", "status": "succeeded", "fps": 24, "resolution": "480p",
             "bytes": 2048, "actual_duration": 5, "source": "generate"},
        ]}), encoding="utf-8")

    item = next(i for i in client.get(f"/api/sprites/{sprite['id']}/videos").json()["items"]
                if i["action_id"] == a["id"])
    assert item["takes"] == 2, "只数成功的版本"
    assert item["generating"] == 1, "进行中的单独计数"
    assert item["take_id"] == "t_ok2", "应取当前使用的那个"
    assert item["video"]["duration"] == 5 and item["video"]["fps"] == 24


def test_索引损坏不影响整体列表(client, sprite):
    from app.services.sprite_store import sprite_store

    good = client.post(f"/api/sprites/{sprite['id']}/actions", json={"name": "好的"}).json()
    bad = client.post(f"/api/sprites/{sprite['id']}/actions", json={"name": "坏的"}).json()
    vd = sprite_store.action_dir(sprite["id"], bad["id"]) / "video"
    vd.mkdir(parents=True, exist_ok=True)
    (vd / "takes.json").write_text("{ 这不是 json", encoding="utf-8")

    items = client.get(f"/api/sprites/{sprite['id']}/videos").json()["items"]
    assert len(items) == 2, "一个动作读不出来不该让整个列表挂掉"
    assert next(i for i in items if i["action_id"] == bad["id"])["error"]
    assert "error" not in next(i for i in items if i["action_id"] == good["id"])


def test_速览不会为动作创建目录(client, sprite):
    """只读接口不该有副作用——构造 SessionStorage 会顺手建目录。"""
    from app.services.sprite_store import sprite_store

    a = client.post(f"/api/sprites/{sprite['id']}/actions", json={"name": "别建目录"}).json()
    vd = sprite_store.action_dir(sprite["id"], a["id"]) / "video"
    assert not vd.exists()
    client.get(f"/api/sprites/{sprite['id']}/videos")
    assert not vd.exists(), "速览接口不该创建 video 目录"


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
