"""描边与导出时的尺寸处理。

两条关键约定：
  - 顺序固定「缩放 → 描边」，所以描边宽度填几 px 成品就是几 px
  - 一律作用在副本上，帧数据不动
"""
from __future__ import annotations

import numpy as np
import pytest

from app.api.export_api import apply_size_ops
from app.core.outline import add_outline, pad_rgba, required_pad
from app.models.export_config import ExportOutlineConfig, ExportScaleConfig


def _solid(size=64, box=24, alpha=255):
    img = np.zeros((size, size, 4), np.uint8)
    a = (size - box) // 2
    img[a:a + box, a:a + box, :3] = (30, 30, 30)
    img[a:a + box, a:a + box, 3] = alpha
    return img


def _bbox(img):
    ys, xs = np.nonzero(img[:, :, 3] > 128)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


# ---------------------------------------------------------------- 描边
def test_外描边向外扩且宽度正确():
    img = _solid(64, 24)
    x0, y0, x1, y1 = _bbox(img)
    out = add_outline(img, 3, (255, 255, 255), 1.0, "outer", "round", False, 128)
    nx0, ny0, nx1, ny1 = _bbox(out)
    assert x0 - nx0 == 3, f"左侧应外扩 3px，实际 {x0 - nx0}"
    assert nx1 - x1 == 3 and ny1 - y1 == 3 and y0 - ny0 == 3


def test_外描边写入alpha而不只是RGB():
    """旧实现只写 RGB 不写 alpha，外侧半宽永远透明，等于没描。"""
    img = _solid(64, 24)
    out = add_outline(img, 3, (255, 255, 255), 1.0, "outer", "round", False, 128)
    x0, y0, _, _ = _bbox(img)
    assert out[y0, x0 - 2, 3] > 128, "描边区域的 alpha 没有被写上"
    assert tuple(out[y0, x0 - 2, :3]) == (255, 255, 255)


def test_内描边不改变外轮廓():
    img = _solid(64, 24)
    before = _bbox(img)
    out = add_outline(img, 3, (255, 0, 0), 1.0, "inner", "round", False, 128)
    assert _bbox(out) == before, "内描边不应把轮廓撑大"
    assert tuple(out[before[1], before[0], :3]) == (255, 0, 0)


def test_描边宽度按像素生效():
    img = _solid(80, 24)
    for w in (1, 2, 5):
        out = add_outline(img, w, (255, 255, 255), 1.0, "outer", "round", False, 128)
        x0 = _bbox(img)[0] - _bbox(out)[0]
        assert x0 == w, f"宽度 {w} 时实际外扩 {x0}"


def test_不透明度影响描边而不影响角色():
    img = _solid(64, 24)
    out = add_outline(img, 3, (255, 255, 255), 0.5, "outer", "round", False, 128)
    x0, y0, _, _ = _bbox(img)
    assert 0 < out[y0, x0 - 2, 3] < 255, "半透明描边的 alpha 应介于两者之间"
    assert out[y0 + 5, x0 + 5, 3] == 255, "角色本体应保持不透明"


def test_贴边时需要扩边():
    full = _solid(32, 32)                 # 内容顶满画布
    assert required_pad([full[:, :, 3]], 3, 128) >= 3, "贴边内容应要求扩边"
    inner = _solid(64, 20)
    assert required_pad([inner[:, :, 3]], 3, 128) == 0, "有余量时不必扩边"


def test_扩边保持居中且尺寸一致():
    img = _solid(32, 32)
    out = pad_rgba(img, 4)
    assert out.shape[:2] == (40, 40)
    assert (out[:4, :, 3] == 0).all(), "扩出来的应是透明边"


# ---------------------------------------------------------------- 导出尺寸处理
def test_不启用时原样返回():
    imgs = [_solid(64, 24)]
    out = apply_size_ops(imgs, ExportScaleConfig(), ExportOutlineConfig())
    assert out[0] is imgs[0], "都没启用时不该复制或改动"


def test_按比例缩放每帧按自身尺寸算():
    """曾用首帧算出目标尺寸套给所有帧，帧尺寸不齐时会被强行拉成一样大。"""
    imgs = [_solid(64, 24), _solid(128, 48)]
    cfg = ExportScaleConfig(enabled=True, mode="percent", percent=50)
    out = apply_size_ops(imgs, cfg, ExportOutlineConfig())
    assert out[0].shape[:2] == (32, 32)
    assert out[1].shape[:2] == (64, 64), "第二帧应按自身尺寸缩，而不是跟首帧一致"


def test_固定尺寸模式统一到同一目标():
    imgs = [_solid(64, 24), _solid(128, 48)]
    cfg = ExportScaleConfig(enabled=True, mode="size", width=100, height=80)
    out = apply_size_ops(imgs, cfg, ExportOutlineConfig())
    assert all(o.shape[:2] == (80, 100) for o in out), "精灵图每格必须一致"


def test_缩放不改变原数组():
    imgs = [_solid(64, 24)]
    before = imgs[0].copy()
    apply_size_ops(imgs, ExportScaleConfig(enabled=True, percent=50),
                   ExportOutlineConfig())
    assert np.array_equal(imgs[0], before), "导出处理必须是非破坏性的"


def test_先缩放后描边使宽度等于成品像素():
    """先描后缩会让 3px 变成 1.5px；顺序必须是缩放在前。"""
    imgs = [_solid(128, 48)]
    out = apply_size_ops(
        imgs,
        ExportScaleConfig(enabled=True, mode="percent", percent=50),
        ExportOutlineConfig(enabled=True, width=3, color=(255, 255, 255),
                            position="outer", antialias=False, auto_pad=False))
    assert out[0].shape[:2] == (64, 64)
    scaled = apply_size_ops([_solid(128, 48)],
                            ExportScaleConfig(enabled=True, percent=50),
                            ExportOutlineConfig())[0]
    assert _bbox(scaled)[0] - _bbox(out[0])[0] == 3, "成品里的描边应正好 3px"


def test_描边要求RGBA否则整步跳过():
    rgb = np.zeros((32, 32, 3), np.uint8)
    notes = []
    out = apply_size_ops([rgb], None,
                         ExportOutlineConfig(enabled=True, width=2), notes.append)
    assert out[0] is rgb
    assert any("跳过描边" in n for n in notes)


def test_宽度为0时不描边():
    imgs = [_solid(64, 24)]
    out = apply_size_ops(imgs, None, ExportOutlineConfig(enabled=True, width=0))
    assert out[0] is imgs[0]


def test_一批帧统一扩同一个边量():
    """帧间尺寸与基线必须一致，否则动画会抖。"""
    imgs = [_solid(32, 32), _solid(32, 12)]   # 一帧贴边、一帧有余量
    out = apply_size_ops(imgs, None,
                         ExportOutlineConfig(enabled=True, width=3,
                                             position="outer", auto_pad=True))
    assert out[0].shape == out[1].shape, f"扩边后尺寸不一致：{out[0].shape} vs {out[1].shape}"
    assert out[0].shape[0] > 32, "贴边帧应触发扩边"


def test_None帧被跳过不报错():
    out = apply_size_ops([None, _solid(64, 24)],
                         ExportScaleConfig(enabled=True, percent=50),
                         ExportOutlineConfig())
    assert out[0] is None and out[1].shape[:2] == (32, 32)
