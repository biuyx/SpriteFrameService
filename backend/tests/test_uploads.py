"""网页上传暂存：选文件夹 / 传 zip → 服务器暂存目录 → 交给原来的按目录导入。"""
from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _png(color=(200, 100, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), color + (255,)).save(buf, "PNG")
    return buf.getvalue()


def _folder(files: dict):
    """模拟浏览器「选择文件夹」：文件名只有末段，相对路径另走 paths 字段。"""
    multipart = [("files", (Path(rel).name, data, "application/octet-stream"))
                 for rel, data in files.items()]
    return multipart, {"paths": list(files)}


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()



# ---------------------------------------------------------------- 选文件夹
def test_选文件夹上传保留目录结构(client):
    files, data = _folder({
        "deliverable/角色A/01_walk.png": _png(),
        "deliverable/角色A/02_idle.png": _png(),
        "deliverable/角色B/01_walk.png": _png(),
    })
    r = client.post("/api/uploads/staging", files=files, data=data)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["files"] == 3 and d["skipped"] == 0
    picked = Path(d["dir"])
    assert picked.name == "deliverable", "返回的应是用户选的那个文件夹本身"
    assert (picked / "角色A" / "02_idle.png").is_file()
    assert sorted(p.name for p in picked.iterdir()) == ["角色A", "角色B"]


def test_只剥一层包装_单角色目录不被剥掉(client):
    """首帧根目录里只有一个角色时，返回的仍应是根目录——否则批量建档认不出结构。"""
    files, data = _folder({"deliverable/角色A/01_walk.png": _png()})
    d = client.post("/api/uploads/staging", files=files, data=data).json()
    assert Path(d["dir"]).name == "deliverable"
    assert (Path(d["dir"]) / "角色A" / "01_walk.png").is_file()


def test_无关文件跳过不落地(client):
    files, data = _folder({
        "素材/a.png": _png(),
        "素材/Thumbs.db": b"junk",
        "素材/说明.txt": b"hello",
        "素材/.DS_Store": b"junk",
    })
    d = client.post("/api/uploads/staging", files=files, data=data).json()
    assert d["files"] == 1 and d["skipped"] == 3
    assert [p.name for p in Path(d["dir"]).iterdir()] == ["a.png"]


@pytest.mark.parametrize("bad", [
    "../逃出去.png", "a/../../逃出去.png", "/etc/逃出去.png",
    "C:/Windows/逃出去.png", "C:\\Windows\\逃出去.png", "..\\..\\逃出去.png",
])
def test_路径穿越一律拒绝(client, bad):
    from app.api.uploads import staging_root
    files, data = _folder({bad: _png(), "ok/a.png": _png()})
    d = client.post("/api/uploads/staging", files=files, data=data).json()
    assert d["skipped"] == 1, f"{bad} 应被跳过"
    root = staging_root()
    assert not list(root.parent.parent.rglob("逃出去.png")), "不能写到暂存区外面"


def test_没有可导入的文件报清楚(client):
    files, data = _folder({"x/readme.txt": b"hi"})
    r = client.post("/api/uploads/staging", files=files, data=data)
    assert r.status_code == 400
    assert "没有可导入的文件" in r.json()["detail"]


def test_失败时不留残渣(client):
    from app.api.uploads import staging_root
    before = set(staging_root().iterdir()) if staging_root().is_dir() else set()
    files, data = _folder({"x/readme.txt": b"hi"})
    client.post("/api/uploads/staging", files=files, data=data)
    after = set(staging_root().iterdir()) if staging_root().is_dir() else set()
    assert after == before


# ---------------------------------------------------------------- zip
def test_zip上传_包着一层同名目录(client):
    body = _zip({"deliverable/角色A/01_walk.png": _png(),
                 "deliverable/角色B/01_walk.png": _png()})
    d = client.post("/api/uploads/staging",
                    files=[("files", ("deliverable.zip", body, "application/zip"))]).json()
    assert d["files"] == 2
    assert Path(d["dir"]).name == "deliverable"
    assert sorted(p.name for p in Path(d["dir"]).iterdir()) == ["角色A", "角色B"]


def test_zip上传_没有包装层(client):
    body = _zip({"角色A/01_walk.png": _png(), "角色B/01_walk.png": _png()})
    d = client.post("/api/uploads/staging",
                    files=[("files", ("x.zip", body, "application/zip"))]).json()
    assert sorted(p.name for p in Path(d["dir"]).iterdir()) == ["角色A", "角色B"]


def test_zip里的穿越成员与mac资源叉都跳过(client):
    from app.api.uploads import staging_root
    body = _zip({"../逃出去.png": _png(), "__MACOSX/._a.png": b"x", "ok/a.png": _png()})
    d = client.post("/api/uploads/staging",
                    files=[("files", ("x.zip", body, "application/zip"))]).json()
    assert d["files"] == 1 and d["skipped"] == 2
    assert not list(staging_root().parent.parent.rglob("逃出去.png"))


def test_假zip报清楚(client):
    r = client.post("/api/uploads/staging",
                    files=[("files", ("x.zip", b"not a zip", "application/zip"))])
    assert r.status_code == 400 and "zip" in r.json()["detail"]


# ---------------------------------------------------------------- 接上原来的导入
def test_暂存目录直接喂给批量建档预览(client):
    """原来的导入逻辑一行不改，只是目录从「服务器路径」换成「暂存目录」。"""
    files, data = _folder({
        "交付/阿花/01_walk.png": _png(),
        "交付/阿花/02_idle.png": _png(),
        "交付/小明/01_walk.png": _png(),
    })
    staged = client.post("/api/uploads/staging", files=files, data=data).json()["dir"]
    r = client.post("/api/sprites/batch-scan", json={"frames_dir": staged})
    assert r.status_code == 200, r.text
    sprites = {s["dir_name"]: s for s in r.json()["sprites"]}
    assert set(sprites) == {"阿花", "小明"}
    assert {a["key"] for a in sprites["阿花"]["actions"]} == {"01_walk", "02_idle"}


def test_暂存目录直接喂给首帧参考集导入(client):
    files, data = _folder({"参考集/01_walk.png": _png(), "参考集/02_idle.png": _png()})
    staged = client.post("/api/uploads/staging", files=files, data=data).json()["dir"]
    r = client.post("/api/ffsets/import-dir", json={"dir": staged, "name": "上传来的参考集"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 清理
def test_过期暂存目录会被清掉(client):
    from app.api.uploads import STAGING_TTL, staging_root
    old = staging_root() / "过期的"
    old.mkdir(parents=True, exist_ok=True)
    (old / "a.png").write_bytes(b"x")
    stale = time.time() - STAGING_TTL - 60
    os.utime(old, (stale, stale))
    files, data = _folder({"新/a.png": _png()})
    client.post("/api/uploads/staging", files=files, data=data)
    assert not old.exists(), "超过一天的暂存应在下次上传时清掉"


@pytest.mark.parametrize("name,ok", [
    ("a/b.png", True), ("a\\b.png", True), ("../a.png", False),
    ("/a.png", False), ("C:/a.png", False), ("a//b.png", True), ("", False),
])
def test_safe_rel(name, ok):
    from app.api.uploads import safe_rel
    assert (safe_rel(name) is not None) == ok
