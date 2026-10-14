"""给 AI 看的图：多张图拼成一张带编号的总览（contact sheet）。

为什么拼成一张：一次工具调用一张图，AI 一眼比对（首帧和立绘像不像、帧序列
是否连贯），也比几十张小图省上下文。格子只标数字——服务器上不一定有中文字体，
编号对应的名字随结果以文字图例返回。
"""
from __future__ import annotations

import io
import math
from typing import List, Optional, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

MAX_WIDTH = 1536            # Claude 看图的长边上限附近，再大会被缩
TILE = 224
GAP = 6
BG = (46, 46, 52)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # 老 Pillow 没有可缩放的默认字体
        return ImageFont.load_default()


def _checker(w: int, h: int, cell: int = 12) -> Image.Image:
    """透明区域铺棋盘格：抠图边缘的残边、半透明一眼可见。"""
    yy, xx = np.mgrid[0:h, 0:w]
    on = ((yy // cell + xx // cell) % 2).astype(bool)
    arr = np.where(on[..., None], np.uint8(205), np.uint8(240)).repeat(3, axis=2)
    return Image.fromarray(arr.astype(np.uint8))          # H×W×3 的 uint8 自动是 RGB


def _fit(img: Image.Image, box: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((box, box), Image.LANCZOS)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        base = _checker(img.width, img.height)
        base.paste(img, (0, 0), img)
        return base
    return img.convert("RGB")


def placeholder(text: str = "—") -> Image.Image:
    im = Image.new("RGB", (TILE, TILE), (80, 80, 88))
    d = ImageDraw.Draw(im)
    f = _font(28)
    d.text((TILE / 2, TILE / 2), text, fill=(170, 170, 178), font=f, anchor="mm")
    return im


def load(path) -> Optional[Image.Image]:
    try:
        with Image.open(path) as im:
            return im.copy()          # close() 会销毁图像数据，得在 with 里复制出来
    except (OSError, ValueError):
        return None


def contact_sheet(images: Sequence[Optional[Image.Image]], *, tile: int = TILE,
                  cols: Optional[int] = None, start: int = 1) -> bytes:
    """拼图并在每格左上角标编号（从 start 起）。缺图的格子画占位。返回 JPEG 字节。"""
    n = max(1, len(images))
    if cols is None:
        cols = min(n, max(1, (MAX_WIDTH + GAP) // (tile + GAP)))
    rows = math.ceil(n / cols)
    w = cols * tile + (cols + 1) * GAP
    h = rows * tile + (rows + 1) * GAP
    sheet = Image.new("RGB", (w, h), BG)
    draw = ImageDraw.Draw(sheet)
    font = _font(max(14, tile // 9))
    for i, img in enumerate(images):
        r, c = divmod(i, cols)
        x0, y0 = GAP + c * (tile + GAP), GAP + r * (tile + GAP)
        cell = _fit(img, tile) if img is not None else placeholder()
        sheet.paste(cell, (x0 + (tile - cell.width) // 2, y0 + (tile - cell.height) // 2))
        label = str(start + i)
        bx = draw.textbbox((0, 0), label, font=font)
        draw.rectangle([x0, y0, x0 + bx[2] + 10, y0 + bx[3] + 8], fill=(20, 20, 24))
        draw.text((x0 + 5, y0 + 3), label, fill=(255, 214, 90), font=font)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def crop_union(images: Sequence[Optional[Image.Image]], margin: float = 0.06) -> list:
    """按所有帧不透明区域的并集统一裁掉空白：角色放大看得清边缘，
    各帧裁同一块，帧间的相对位移（走动、起伏）照样保留。"""
    # 用相对坐标：同一动作里可能混着不同尺寸的帧（有的被缩放过），按像素裁会错位
    box = None
    for im in images:
        if im is None or im.mode not in ("RGBA", "LA"):
            continue
        b = im.getchannel("A").getbbox()
        if b:
            r = (b[0] / im.width, b[1] / im.height, b[2] / im.width, b[3] / im.height)
            box = r if box is None else (min(box[0], r[0]), min(box[1], r[1]),
                                         max(box[2], r[2]), max(box[3], r[3]))
    if box is None:
        return list(images)
    pad = max(box[2] - box[0], box[3] - box[1]) * margin
    x0, y0 = max(0.0, box[0] - pad), max(0.0, box[1] - pad)
    x1, y1 = min(1.0, box[2] + pad), min(1.0, box[3] + pad)
    return [None if im is None else
            im.crop((round(x0 * im.width), round(y0 * im.height),
                     round(x1 * im.width), round(y1 * im.height)))
            for im in images]


def size_census(paths) -> dict:
    """各尺寸各有几张（只读文件头，不解码）。"""
    from collections import Counter
    sizes = Counter()
    for p in paths:
        try:
            with Image.open(p) as im:
                sizes[f"{im.width}×{im.height}"] += 1
        except (OSError, ValueError):
            continue
    return dict(sizes.most_common())


def pick_evenly(items: List, k: int) -> List:
    """从序列里均匀挑 k 个（含首尾），保持顺序。"""
    if k <= 0 or len(items) <= k:
        return list(items)
    if k == 1:
        return [items[0]]
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def video_samples(path, k: int) -> tuple:
    """从视频里均匀取 k 帧。返回 ([PIL 图], {fps, frames, duration, width, height})。"""
    import cv2
    cap = cv2.VideoCapture(str(path))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        meta = {"fps": round(fps, 2), "frames": total,
                "duration": round(total / fps, 2) if fps else None,
                "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)}
        out = []
        for idx in pick_evenly(list(range(total)), k):
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            out.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)) if ok else None)
        return out, meta
    finally:
        cap.release()
