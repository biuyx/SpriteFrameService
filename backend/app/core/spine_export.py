"""Spine 资源导出：序列帧 → Spine JSON（骨架数据）+ atlas 图集。

结构对齐既有工程（efRenter1101，Spine 3.8.99）：
    骨骼  root + 每个动作一个骨骼（bone 名 = 动画名）
    插槽  每个动作一个 slot，slot 名 = 该动作首帧名
    附件  每帧一个 region attachment，名为 {动画}_{4位帧号}
    动画  各自控制自己的 slot，用 attachment timeline 逐帧切换

产物 .json + .atlas + .png 可直接喂给 spine-runtime（与 .skel 等价）；
美术需要工程文件时在 Spine 里 Import Data 选该 JSON，另存为 .spine 即可
（.spine 是私有二进制格式，官方无 API，CLI 也无法生成）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

SPINE_VERSION = "3.8.99"

# 模板变体 → Spine 动画名（与既有工程一致；模板库里可逐条改写）
DEFAULT_ANIM_MAP: Dict[str, str] = {
    "待机": "wait1", "背面待机": "wait2", "做饭": "cook1",
    "走路": "walk1", "背面走动": "walk2",
    "走路入住": "checkin_walk1", "背面入住": "checkin_walk2",
    "入住": "checkin1", "入住背面": "checkin2",
    "收拾行李": "tidy1", "睡觉": "sleep1", "洗澡": "bath1", "如厕": "sit_toilet1",
    "购物": "shop1", "工作": "sit_work1", "工作背面": "sit_work2",
    "手工": "sit_craft1", "吃饭": "sit_eat1", "休息": "sit_wait1", "泡澡": "soak1",
}


def frame_name(anim: str, index: int, pattern: str = "{anim}_{i:04d}") -> str:
    return pattern.format(anim=anim, i=index)


def build_skeleton(anims: List[dict], images_path: str = "./images/",
                   version: str = SPINE_VERSION) -> dict:
    """anims: [{name, frames:[帧名...], fps, loop, width, height}]

    每条 anim 还可带参考工程反解出来的字段（都可缺省）：
        slot            插槽名（缺省用首帧名）
        bone_offset     骨骼静态偏移 {x, y}——美术逐动画的对齐微调
        bone_scale      骨骼缩放 {x, y}
        att_offset      附件偏移 {x, y}
        render          附件渲染尺寸 (宽, 高)；与图集里的贴图尺寸无关，
                        贴图压缩过也照样按这个尺寸画
    """
    bones = [{"name": "root"}]
    slots, skin_atts, animations = [], {}, {}

    for a in anims:
        if not a["frames"]:
            continue
        name = a["name"]
        slot = a.get("slot") or a["frames"][0]   # 缺省沿用：slot 名 = 首帧名
        bone = {"name": name, "parent": "root"}
        off = a.get("bone_offset") or {}
        if off.get("x"):
            bone["x"] = round(float(off["x"]), 4)
        if off.get("y"):
            bone["y"] = round(float(off["y"]), 4)
        bsc = a.get("bone_scale") or {}
        if bsc.get("x") not in (None, 1):
            bone["scaleX"] = round(float(bsc["x"]), 4)
        if bsc.get("y") not in (None, 1):
            bone["scaleY"] = round(float(bsc["y"]), 4)
        bones.append(bone)
        slots.append({"name": slot, "bone": name})

        rw, rh = a.get("render") or (int(a["width"]), int(a["height"]))
        aoff = a.get("att_offset") or {}
        att = {"width": int(rw), "height": int(rh)}
        if aoff.get("x"):
            att["x"] = round(float(aoff["x"]), 4)
        if aoff.get("y"):
            att["y"] = round(float(aoff["y"]), 4)
        skin_atts[slot] = {f: dict(att) for f in a["frames"]}
        fps = max(0.1, float(a.get("fps") or 12))
        keys = [{"time": round(i / fps, 6), "name": f}
                for i, f in enumerate(a["frames"])]
        # 末尾补一个 key 让动画时长 = 帧数/fps（否则最后一帧时长为 0）：
        # 循环动作回到首帧可无缝衔接，单次动作保持结束姿势
        tail = a["frames"][0] if a.get("loop", True) else a["frames"][-1]
        keys.append({"time": round(len(a["frames"]) / fps, 6), "name": tail})
        animations[name] = {"slots": {slot: {"attachment": keys}}}

    # 包围盒按附件的渲染尺寸算（贴图可能压缩过，渲染尺寸才是画面上的大小）
    def _render_wh(a):
        return a.get("render") or (int(a["width"]), int(a["height"]))

    sized = [_render_wh(a) for a in anims if a["frames"]]
    box_w = max([s[0] for s in sized] or [0])
    box_h = max([s[1] for s in sized] or [0])
    payload = {
        "skeleton": {
            "hash": "", "spine": version or SPINE_VERSION,
            "x": -box_w / 2, "y": -box_h / 2, "width": box_w, "height": box_h,
            "images": images_path, "audio": "",
        },
        "bones": bones,
        "slots": slots,
        "skins": [{"name": "default", "attachments": skin_atts}],
        "animations": animations,
    }
    payload["skeleton"]["hash"] = hashlib.md5(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()[:11]
    return payload


# ------------------------------------------------------------ 图集打包
def _trim(rgba: np.ndarray, alpha_min: int = 2) -> Tuple[np.ndarray, int, int]:
    """裁掉透明边，返回 (裁剪图, 左偏移, 下偏移)——offset 以原图左下角为基准。

    alpha_min=2 与 Spine 官方打包器行为一致：缩放产生的极淡边缘像素
    （alpha 1~2）不计入包围盒。用既有工程 efRenter1101 的 387 帧比对，
    size/offset/orig 三项与其 .atlas 逐帧吻合（±1px）。
    """
    h, w = rgba.shape[:2]
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        return rgba, 0, 0
    ys, xs = np.nonzero(rgba[:, :, 3] > alpha_min)
    if not len(ys):
        return rgba[:1, :1], 0, h - 1
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    return rgba[y0:y1 + 1, x0:x1 + 1], x0, h - 1 - y1


def pack_atlas(items: Sequence[Tuple[str, np.ndarray]], page_name: str,
               padding: int = 2, max_size: int = 4096, alpha_min: int = 2,
               progress=None) -> Tuple[str, bytes]:
    """把帧打包成单页图集。返回 (atlas 文本, PNG 字节)。

    货架(shelf)排布 + 空白裁剪：小图尺寸接近时填充率已足够，
    且行为稳定可预测（同一批帧每次打包结果一致）。
    """
    trimmed = []
    for i, (name, img) in enumerate(items):
        t, ox, oy = _trim(img, alpha_min)
        trimmed.append({"name": name, "img": t, "ox": ox, "oy": oy,
                        "ow": img.shape[1], "oh": img.shape[0],
                        "w": t.shape[1], "h": t.shape[0]})
        if progress and i % 50 == 0:
            progress(i, len(items))

    order = sorted(trimmed, key=lambda r: (-r["h"], -r["w"]))
    # 估一个接近正方形的页宽
    area = sum((r["w"] + padding) * (r["h"] + padding) for r in order)
    page_w = min(max_size, max(int(np.sqrt(area) * 1.15),
                               max(r["w"] for r in order) + padding * 2))
    x = y = row_h = 0
    for r in order:
        if x + r["w"] + padding > page_w:
            x = 0
            y += row_h + padding
            row_h = 0
        r["x"], r["y"] = x, y
        x += r["w"] + padding
        row_h = max(row_h, r["h"])
    page_h = y + row_h + padding

    page = np.zeros((page_h, page_w, 4), np.uint8)
    for r in order:
        page[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]] = r["img"]

    lines = ["", f"{page_name}.png", f"size: {page_w},{page_h}",
             "format: RGBA8888", "filter: Linear,Linear", "repeat: none"]
    for r in sorted(trimmed, key=lambda r: r["name"]):
        lines += [r["name"], "  rotate: false",
                  f"  xy: {r['x']}, {r['y']}",
                  f"  size: {r['w']}, {r['h']}",
                  f"  orig: {r['ow']}, {r['oh']}",
                  f"  offset: {r['ox']}, {r['oy']}",
                  "  index: -1"]
    ok, buf = cv2.imencode(".png", cv2.cvtColor(page, cv2.COLOR_RGBA2BGRA))
    if not ok:
        raise RuntimeError("图集编码失败")
    return "\n".join(lines) + "\n", buf.tobytes()


def fit_canvas(rgba: np.ndarray, size: Optional[int], scale: float = 1.0) -> np.ndarray:
    """缩放并居中放入统一方形画布（保证所有帧同尺寸、锚点一致）。"""
    img = rgba
    if scale and scale != 1.0:
        h, w = img.shape[:2]
        img = cv2.resize(img, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4)
    if not size:
        return img
    h, w = img.shape[:2]
    if h > size or w > size:                       # 超出画布则等比缩到刚好放下
        k = min(size / w, size / h)
        img = cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))),
                         interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    out = np.zeros((size, size, 4), np.uint8)
    top, left = (size - h) // 2, (size - w) // 2
    out[top:top + h, left:left + w] = img
    return out
