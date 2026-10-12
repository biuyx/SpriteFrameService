"""从别的机器（含 Windows）整体搬过来的 data/：帧元数据里的绝对路径不该再起作用。

frames.json 存的是落盘时的绝对路径（D:\\AI\\...\\frames\\proc\\<id>.png）。帧文件本来就
按 id 存在会话目录里，路径只是冗余；但读取方以前直接信它——搬到 /data/... 之后
所有帧都显示「未抠图」，流水线把抠图整轮重跑。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.frame_manager import FrameManager
from app.services.frame_store import FrameStore, resolve_frame_file
from app.services.storage import SessionStorage

WIN = "D:\\AI\\SpriteFrameService\\data\\sprites\\sp_old\\actions\\a1\\frames"


def _png(path: Path):
    # 只需要文件在：这里不走 cv2.imwrite——它在 Windows 上写不了中文路径，
    # 而 pytest 会拿中文用例名建临时目录
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")


@pytest.fixture
def migrated(tmp_path):
    """一个「从 Windows 搬来」的会话目录：文件在约定位置，元数据里是 D:\\ 路径。"""
    st = SessionStorage("a1", root=tmp_path / "a1")
    meta = []
    for i, fid in enumerate(["f0", "f1", "f2"]):
        _png(st.raw_path(fid))
        processed = i < 2                       # f0、f1 抠过图，f2 没有
        if processed:
            _png(st.proc_path(fid))
        meta.append({
            "id": fid, "index": i, "timestamp": i * 0.1, "status": "bg_removed" if processed else "raw",
            "is_selected": True,
            "image_path": f"{WIN}\\raw\\{fid}.png",
            "processed_path": f"{WIN}\\proc\\{fid}.png" if processed else None,
        })
    st.frames_json.write_text(json.dumps(meta), encoding="utf-8")
    return st


def _load(st):
    fm = FrameManager()
    FrameStore(st).load_metadata(fm)
    return fm


def test_搬过来的帧仍认得出抠过图(migrated):
    fm = _load(migrated)
    assert [f.has_processed for f in fm.frames] == [True, True, False]
    assert all(f.has_image for f in fm.frames)


def test_路径改指向本机的约定位置(migrated):
    fm = _load(migrated)
    f0 = fm.get_frame(0)
    assert f0.processed_path == migrated.proc_path("f0")
    assert f0.image_path == migrated.raw_path("f0")


def test_再存一次就把新路径写回去(migrated):
    """读一次、存一次，frames.json 自己就修好了，不需要单独跑迁移脚本。"""
    fm = _load(migrated)
    FrameStore(migrated).save_metadata(fm)
    meta = json.loads(migrated.frames_json.read_text(encoding="utf-8"))
    assert not any("D:\\" in (m["processed_path"] or "") for m in meta)
    assert meta[0]["processed_path"] == str(migrated.proc_path("f0"))


def test_元数据里没有处理结果就不从盘上捞(migrated):
    """历史回滚靠把 processed_path 置空标记「未处理」，磁盘旧文件稍后才删。"""
    _png(migrated.proc_path("f2"))           # 盘上有个残留的处理图
    fm = _load(migrated)
    assert fm.get_frame(2).has_processed is False


def test_约定位置没有时仍用原路径兜底(tmp_path):
    other = tmp_path / "别处" / "x.png"
    _png(other)
    assert resolve_frame_file(str(other), tmp_path / "不存在.png") == other


def test_两处都没有就是没有(tmp_path):
    p = resolve_frame_file("D:\\nope\\x.png", tmp_path / "x.png")
    assert p is not None and not p.exists()


def test_从别的动作取首帧也认新位置(tmp_path, monkeypatch):
    """「用某动作的第 N 帧当首帧」走的是 sprite_store.frame_image_path，同样要能找到。"""
    from app.services.sprite_store import SpriteStore
    store = SpriteStore(root=tmp_path)
    sp = store.create_sprite("搬家角色")
    a = store.create_action(sp["id"], "走路")
    d = store.action_dir(sp["id"], a["id"])
    _png(d / "frames" / "proc" / "fx.png")
    (d / "frames.json").write_text(json.dumps([{
        "id": "fx", "index": 0, "timestamp": 0,
        "image_path": f"{WIN}\\raw\\fx.png", "processed_path": f"{WIN}\\proc\\fx.png",
    }]), encoding="utf-8")
    p = store.frame_image_path(sp["id"], a["id"], 0)
    assert p == d / "frames" / "proc" / "fx.png"
