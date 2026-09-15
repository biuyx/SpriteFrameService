"""精灵帧描边：纯色描边，语义对齐 PS 图层样式 Stroke。

与旧实现（沿轮廓 drawContours）的区别：
- 外描边同时写 RGB 与 **alpha**——旧实现只改 RGB，画在角色外侧的部分 alpha 仍为 0，
  肉眼看不见，这是"描边像没生效"的根因；
- 基于 alpha 的距离变换而非形态学，宽度精确、边缘自带亚像素抗锯齿，
  且不丢手指/道具/飘带等小部件（旧实现按轮廓面积过滤掉了）；
- 角色镂空处（手臂与身体之间等）同样描边。

风格：
    corner=round  圆角（欧氏距离，PS 默认观感）
    corner=miter  尖角（棋盘距离，方形扩张）
    antialias=False 硬边——像素风必须关掉抗锯齿，否则边缘出现半透明杂边
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

import cv2
import numpy as np

POSITIONS = ("outer", "inner", "center")


def _band(mask: np.ndarray, w: float, dist_type: int, antialias: bool,
          shape) -> np.ndarray:
    """距离变换得到宽度 w 的条带强度（0~1）。"""
    if w <= 0:
        return np.zeros(shape, np.float32)
    dist = cv2.distanceTransform((mask * 255).astype(np.uint8), dist_type, 5)
    if antialias:
        return np.clip(w + 0.5 - dist, 0.0, 1.0).astype(np.float32)
    return (dist <= w).astype(np.float32)


def _composite_under(rgba: np.ndarray, ring: np.ndarray,
                     color: Sequence[int]) -> np.ndarray:
    """描边层在角色下方（外描边）：角色 over 描边，同时写 RGB 与 alpha。"""
    alpha = rgba[:, :, 3]
    fg_a = alpha.astype(np.float32) / 255.0
    bg_a = ring * (1.0 - fg_a)
    out_a = fg_a + bg_a
    num = (rgba[:, :, :3].astype(np.float32) * fg_a[..., None]
           + np.array(color[:3], np.float32) * bg_a[..., None])
    out_rgb = num / np.maximum(out_a, 1e-6)[..., None]
    return np.dstack([
        np.clip(out_rgb, 0, 255).astype(np.uint8),
        np.clip(out_a * 255.0, 0, 255).astype(np.uint8),
    ])


def _overlay_on(rgba: np.ndarray, ring: np.ndarray,
                color: Sequence[int]) -> np.ndarray:
    """描边层盖在角色之上（内描边）：只改 RGB，不改变轮廓。"""
    out = rgba.copy()
    a = ring[..., None]
    out[:, :, :3] = np.clip(rgba[:, :, :3].astype(np.float32) * (1 - a)
                            + np.array(color[:3], np.float32) * a,
                            0, 255).astype(np.uint8)
    return out


def add_outline(rgba: np.ndarray, width: float, color: Sequence[int],
                opacity: float = 1.0, position: str = "outer",
                corner: str = "round", antialias: bool = True,
                threshold: int = 128) -> np.ndarray:
    """给 RGBA 图像加纯色描边。非 RGBA 或宽度为 0 时原样返回。

    外半(角色轮廓之外)走 over 合成——描边在角色下方，必须同时写 alpha；
    内半(轮廓之内)走覆盖——盖在角色上且不改变轮廓。
    居中 = 两者各一半，分别处理后无缝相接。
    """
    if width <= 0 or rgba.ndim != 3 or rgba.shape[2] != 4:
        return rgba
    if position not in POSITIONS:
        position = "outer"

    alpha = rgba[:, :, 3]
    solid = (alpha >= threshold).astype(np.uint8)
    if not solid.any():
        return rgba
    dist_type = cv2.DIST_L2 if corner == "round" else cv2.DIST_C
    op = float(np.clip(opacity, 0.0, 1.0))
    outer_w = {"outer": width, "center": width / 2.0, "inner": 0.0}[position]
    inner_w = {"outer": 0.0, "center": width / 2.0, "inner": width}[position]

    result = rgba
    if outer_w > 0:
        ring = _band(1 - solid, outer_w, dist_type, antialias, alpha.shape)
        ring = np.clip(ring * (1 - solid) * op, 0.0, 1.0)
        if ring.any():
            result = _composite_under(result, ring, color)
    if inner_w > 0:
        ring = _band(solid, inner_w, dist_type, antialias, alpha.shape)
        ring = np.clip(ring * solid * op, 0.0, 1.0)
        if ring.any():
            result = _overlay_on(result, ring, color)
    return result


def required_pad(alphas: List[np.ndarray], width: float, threshold: int = 128) -> int:
    """外描边需要的统一扩边像素数。

    取所有帧中角色像素到画布边界的最小余量；不足 width 时补差额。
    必须所有帧扩同一个值——精灵帧一旦各帧尺寸/基线不一致，导出的精灵图就废了。
    """
    margin = int(np.ceil(width))
    for a in alphas:
        if a is None:
            continue
        ys, xs = np.nonzero(a >= threshold)
        if not len(ys):
            continue
        m = min(int(ys.min()), int(xs.min()),
                int(a.shape[0] - 1 - ys.max()), int(a.shape[1] - 1 - xs.max()))
        margin = min(margin, m)
    return max(0, int(np.ceil(width)) - margin)


def pad_rgba(rgba: np.ndarray, pad: int) -> np.ndarray:
    """四边等量扩透明边（保持帧间对齐）。"""
    if pad <= 0:
        return rgba
    return cv2.copyMakeBorder(rgba, pad, pad, pad, pad,
                              cv2.BORDER_CONSTANT, value=(0, 0, 0, 0))
