"""MCP 服务（一期，只读）：工具清单、角色总览、就绪检查、看图、任务等待、权限与审计。

工具逻辑用 SDK 的进程内客户端直连；权限与审计只在 HTTP 这一层生效，走 TestClient。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import uuid

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("mcp")

from mcp.client import Client  # noqa: E402

from app.mcp_server import audit  # noqa: E402
from app.mcp_server.server import server  # noqa: E402
from app.services.sprite_store import sprite_store  # noqa: E402


def call(name, args=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args or {})
    return asyncio.run(go())


def texts(r):
    return [c.text for c in r.content if c.type == "text"]


def images(r):
    return [Image.open(io.BytesIO(base64.b64decode(c.data))) for c in r.content if c.type == "image"]


def data(r):
    assert not r.is_error, texts(r)
    return json.loads(texts(r)[0])


def _png(path, size=(64, 64), rgba=True, box=(16, 16, 48, 48)):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA" if rgba else "RGB", size, (0, 0, 0, 0) if rgba else (90, 90, 90))
    im.paste((220, 60, 60, 255) if rgba else (220, 60, 60), box)
    im.save(path)


@pytest.fixture
def sprite():
    """一个角色：idle 有首帧，walk 没有。名字带随机后缀，避免和别的用例撞名。"""
    sp = sprite_store.create_sprite(f"测试角色_{uuid.uuid4().hex[:6]}")
    idle = sprite_store.create_action(sp["id"], "idle")
    walk = sprite_store.create_action(sp["id"], "walk")
    _png(sprite_store.action_dir(sp["id"], idle["id"]) / "first_frame.png")
    return {"sprite": sp, "idle": idle, "walk": walk}


# ---------------------------------------------------------------- 工具清单
def test_一期全是只读工具():
    async def go():
        async with Client(server) as c:
            return (await c.list_tools()).tools
    tools = {t.name: t for t in asyncio.run(go())}
    assert {"list_sprites", "get_sprite_overview", "get_template_library", "check_readiness",
            "view_first_frames", "view_frames", "view_video", "list_jobs", "wait_job",
            "get_platform_status"} <= set(tools)
    for t in tools.values():
        assert t.annotations and t.annotations.read_only_hint, f"{t.name} 应标为只读"


def test_服务说明里讲清先定型再量产():
    from app.mcp_server.server import INSTRUCTIONS
    for word in ("先定型，再量产", "check_readiness", "模板的问题", "不要重复提交"):
        assert word in INSTRUCTIONS


# ---------------------------------------------------------------- 了解现状
def test_角色可以按名字找(sprite):
    sp = sprite["sprite"]
    d = data(call("get_sprite_overview", {"sprite": sp["name"]}))
    assert d["sprite"]["id"] == sp["id"]
    stages = {a["name"]: a["stage"] for a in d["actions"]}
    assert stages == {"idle": "缺视频", "walk": "缺首帧"}


def test_找不到时告诉AI怎么办(sprite):
    r = call("get_sprite_overview", {"sprite": "没有这个人"})
    assert r.is_error and "list_sprites" in texts(r)[0]
    r = call("view_frames", {"sprite": sprite["sprite"]["id"], "action": "fly"})
    assert r.is_error and "现有动作" in texts(r)[0] and "idle" in texts(r)[0]


def test_角色列表带阶段统计(sprite):
    d = data(call("list_sprites", {"keyword": sprite["sprite"]["name"]}))
    assert d["count"] == 1
    assert d["sprites"][0]["stages"] == {"缺视频": 1, "缺首帧": 1}


# ---------------------------------------------------------------- 就绪检查
def test_就绪检查列出阻塞与没定型的提示词(sprite):
    d = data(call("check_readiness", {"sprite": sprite["sprite"]["id"]}))
    assert "阻塞" in d["verdict"]
    walk = next(a for a in d["actions"] if a["name"] == "walk")
    assert walk["steps"]["首帧"].startswith("阻塞"), "没有参考首帧集"
    assert walk["settings"]["参考视频模板"] == "未绑定"
    assert any("walk" in b["动作"] for b in d["blockers"])
    # 测试数据目录里没有提示词库：命中内置兜底，要提醒「没定型」
    assert any("内置兜底" in w["问题"] for w in d["warnings"])
    assert d["paid_estimate"]["首帧生图"] == 0 and d["paid_estimate"]["视频生成"] == 0


def test_连带阻塞不算根因(sprite, monkeypatch):
    """前一步过不去，后一步「没有输入」只是连带——只报根因，AI 才不会被三条原因带偏。"""
    from app.core import pipeline

    def no_set(sid, action, opts):
        raise pipeline.StepBlocked(f"参考首帧集缺少动作 {action['name']}")

    def need_ff(sid, action, opts):
        raise pipeline.StepBlocked("缺少首帧（将由上一步生成）")

    monkeypatch.setattr(pipeline, "check_firstframe", no_set)
    monkeypatch.setattr(pipeline, "check_generate", need_ff)
    d = data(call("check_readiness", {"sprite": sprite["sprite"]["id"],
                                      "steps": ["firstframe", "generate"]}))
    walk = next(a for a in d["actions"] if a["name"] == "walk")
    assert walk["steps"]["视频"] == "阻塞：等上一步（首帧）先通过"
    assert [b["原因"] for b in d["blockers"]] == ["首帧：参考首帧集缺少动作 walk"]
    assert d["blockers"][0]["涉及模板"] == ["未绑定模板"]
    assert "1 处" in d["连带阻塞"]


def test_就绪检查算花费与设定来源(sprite, monkeypatch):
    """检查都通过时：缺首帧的要生图；视频时长按 动作单独设定 > 模板推荐 > 默认。"""
    from app.core import pipeline
    monkeypatch.setattr(pipeline, "check_firstframe", lambda *a, **k: {"set_name": "样板集"})
    monkeypatch.setattr(pipeline, "check_generate", lambda *a, **k: {"template": "t"})
    sp, idle = sprite["sprite"], sprite["idle"]
    sprite_store.update_action(sp["id"], idle["id"], {"gen_prefs": {"video": {"duration": 7}}})
    d = data(call("check_readiness", {"sprite": sp["id"], "steps": ["firstframe", "generate"]}))
    est = d["paid_estimate"]
    assert (est["首帧生图"], est["视频生成"], est["视频总时长"]) == (1, 2, "11s")
    rows = {a["name"]: a for a in d["actions"]}
    assert rows["idle"]["steps"]["首帧"] == "跳过：已完成"
    assert rows["walk"]["steps"]["首帧"] == "执行（样板集）"
    assert rows["idle"]["settings"]["视频时长"] == "7s（动作单独设定）"
    assert rows["walk"]["settings"]["视频时长"] == "4s（默认）"
    assert d["verdict"].startswith("就绪")


# ---------------------------------------------------------------- 看图
def test_看首帧_立绘在前_缺的标出来(sprite):
    sp = sprite["sprite"]
    ref = sprite_store.add_ref(sp["id"], "立绘", _png_bytes())
    sprite_store.update_ref(sp["id"], ref["id"], {"role": "front"})
    r = call("view_first_frames", {"sprite": sp["id"]})
    assert not r.is_error
    assert len(images(r)) == 1
    legend = texts(r)[0]
    assert "1 = 正面立绘" in legend, "立绘排第一格，好和首帧比对"
    assert "2 = idle" in legend and "3 = walk（没有首帧）" in legend


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 200, 10)).save(buf, "PNG")
    return buf.getvalue()


def _frames(sp, act, sizes):
    d = sprite_store.action_dir(sp["id"], act["id"])
    rows = []
    for i, size in enumerate(sizes):
        fid = f"f{i:03d}"
        _png(d / "frames" / "raw" / f"{fid}.png", size, rgba=False)
        s = size[0] // 4
        _png(d / "frames" / "proc" / f"{fid}.png", size, box=(s, s, 3 * s, 3 * s))
        rows.append({"id": fid, "index": i, "processed_path": f"D:\\旧机器\\{fid}.png",
                     "image_path": f"D:\\旧机器\\raw\\{fid}.png"})
    (d / "frames.json").write_text(json.dumps(rows), encoding="utf-8")


def test_看帧序列_均匀抽取_报出尺寸不一致(sprite):
    sp, idle = sprite["sprite"], sprite["idle"]
    _frames(sp, idle, [(64, 64)] * 6 + [(32, 32)] * 4)
    r = call("view_frames", {"sprite": sp["id"], "action": "idle", "max_frames": 5})
    assert not r.is_error, texts(r)
    t = texts(r)[0]
    assert "共 10 帧" in t and "均匀取了 5 帧" in t
    assert "1=第0帧" in t and "5=第9帧" in t, "首尾都要取到"
    assert "帧尺寸不一致" in t and "64×64 ×6" in t
    assert len(images(r)) == 1


def test_统一裁空白按相对坐标_不同尺寸的帧对得齐():
    from app.mcp_server.views import crop_union
    big = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    big.paste((255, 0, 0, 255), (40, 40, 60, 60))
    small = big.resize((50, 50))
    a, b = crop_union([big, small])
    assert a.size[0] == pytest.approx(b.size[0] * 2, abs=2), "裁的是同一块相对区域"


def test_看视频_抽帧并给出版本信息(sprite):
    import cv2
    sp, idle = sprite["sprite"], sprite["idle"]
    vdir = sprite_store.action_dir(sp["id"], idle["id"]) / "video"
    vdir.mkdir(parents=True, exist_ok=True)
    w = cv2.VideoWriter(str(vdir / "t_1.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
    for i in range(20):
        w.write(np.full((64, 64, 3), i * 10, np.uint8))
    w.release()
    (vdir / "takes.json").write_text(json.dumps({"current": "t_1", "takes": [
        {"id": "t_1", "file": "t_1.mp4", "status": "succeeded", "model": "m",
         "prompt_name": "走路", "prompt_version": 3}]}), encoding="utf-8")
    r = call("view_video", {"sprite": sp["id"], "action": "idle", "samples": 4})
    assert not r.is_error, texts(r)
    assert len(images(r)) == 1
    t = texts(r)[0]
    assert "走路 v3" in t and "t_1（当前）" in t
    r = call("view_video", {"sprite": sp["id"], "action": "walk"})
    assert r.is_error and "还没有生成成功的视频" in texts(r)[0]


# ---------------------------------------------------------------- 任务
def test_等任务_结束就返回结果():
    from app.services.job_manager import job_manager
    job = job_manager.submit("export", lambda ctx: {"files": 3})
    d = data(call("wait_job", {"job_id": job.id, "max_wait_seconds": 10}))
    assert d["status"] == "done" and d["result"] == {"files": 3}


def test_任务记录丢了_提醒别重复提交():
    d = data(call("wait_job", {"job_id": "nope", "max_wait_seconds": 0}))
    assert d["status"] == "unknown" and "不要直接重新提交" in d["message"]


# ---------------------------------------------------------------- 权限与审计（HTTP 层）
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
     "MCP-Protocol-Version": "2025-11-25"}


def _rpc(client, key, tool, args=None):
    r = client.post("/api/mcp", headers={**H, "Authorization": f"Bearer {key}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": tool, "arguments": args or {}}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture
def secured(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "auth_token", "T")
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_密钥没有该权限就拒绝(secured):
    from app.services.agent_keys import agent_key_store
    _, key = agent_key_store.create(f"只能加工_{uuid.uuid4().hex[:4]}", ["operate"])
    res = _rpc(secured, key, "list_sprites")
    assert res["isError"] and "没有「read」权限" in res["content"][0]["text"]


def test_每次调用都记审计_分得清是谁(secured):
    from app.services.agent_keys import agent_key_store
    name = f"审计_{uuid.uuid4().hex[:4]}"
    _, key = agent_key_store.create(name, ["read"])
    assert not _rpc(secured, key, "list_sprites", {"keyword": "x"})["isError"]
    assert _rpc(secured, key, "get_sprite_overview", {"sprite": "不存在"})["isError"]
    mine = [x for x in audit.tail(50) if x["agent"] == name]
    assert [(x["tool"], x["ok"]) for x in mine] == [("list_sprites", True),
                                                   ("get_sprite_overview", False)]
    assert '"keyword": "x"' in mine[0]["args"]
    assert "找不到角色" in mine[1]["error"]


def test_平台口令也能用_记为管理员(secured):
    _rpc(secured, "T", "get_platform_status")
    assert audit.tail(1)[0]["agent"] == "平台口令"
