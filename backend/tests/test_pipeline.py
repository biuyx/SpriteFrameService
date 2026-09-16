"""流水线：工序构成、导出处理摘要、完成判定。"""
from __future__ import annotations

import pytest

from app.core import pipeline as P


def test_工序构成不含缩放与描边():
    """两者会改写帧、且与导出环节的尺寸处理叠乘，已改由导出步骤非破坏性应用。"""
    assert P.STEPS == ["firstframe", "generate", "extract", "matting", "export"]
    assert "scale" not in P.STEPS and "outline" not in P.STEPS
    assert set(P.STEP_LABEL) == set(P.STEPS)


def test_每步都有上一步依赖声明():
    for step in P.STEPS[1:]:
        assert step in P.PREV_STEP, f"{step} 缺少上一步声明"
        assert P.PREV_STEP[step] in P.STEPS


def test_导出处理摘要按执行顺序给出(monkeypatch):
    monkeypatch.setattr(P.sprite_store, "get_sprite", lambda _sid: {"preset": {
        "scale": {"enabled": True, "mode": "percent", "percent": 40},
        "outline": {"enabled": True, "width": 3, "color": [255, 255, 255]},
    }})
    ops = P.size_ops_of("sp_x")
    assert len(ops) == 2
    assert ops[0].startswith("缩放") and "40%" in ops[0]
    assert ops[1].startswith("描边") and "3px" in ops[1]


def test_未启用时摘要为空(monkeypatch):
    monkeypatch.setattr(P.sprite_store, "get_sprite", lambda _sid: {"preset": {
        "scale": {"enabled": False}, "outline": {"enabled": False, "width": 3},
    }})
    assert P.size_ops_of("sp_x") == []


def test_描边宽度为0视为未启用(monkeypatch):
    monkeypatch.setattr(P.sprite_store, "get_sprite", lambda _sid: {"preset": {
        "outline": {"enabled": True, "width": 0},
    }})
    assert P.size_ops_of("sp_x") == []


def test_固定尺寸模式的摘要(monkeypatch):
    monkeypatch.setattr(P.sprite_store, "get_sprite", lambda _sid: {"preset": {
        "scale": {"enabled": True, "mode": "size", "width": 128, "height": 96},
    }})
    assert P.size_ops_of("sp_x") == ["缩放 128x96"]


def test_预设缺省合并不报错(monkeypatch):
    monkeypatch.setattr(P.sprite_store, "get_sprite", lambda _sid: {})
    assert P.size_ops_of("sp_x") == []
    cfg = P._preset("sp_x", "outline", P.DEFAULT_OUTLINE_PRESET)
    assert cfg["enabled"] is False and "width" in cfg


def test_本地闸门与首帧闸门都带上报():
    """裸信号量阻塞时不能上报也不能查取消，批量跑时会「看着像卡住」。"""
    from app.core.first_frame_generator import first_frame_gate
    from app.services.concurrency import ConcurrencyGate
    assert isinstance(P._local_gate, ConcurrencyGate)
    assert isinstance(first_frame_gate, ConcurrencyGate)


def test_生成闸门与首帧闸门是各自独立的():
    from app.api.generate import generate_gate
    from app.core.first_frame_generator import first_frame_gate
    assert generate_gate is not first_frame_gate
    generate_gate.try_acquire()
    assert first_frame_gate.active == 0, "两个闸门不应互相影响"
    generate_gate.release()
