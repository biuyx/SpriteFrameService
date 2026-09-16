"""Spine 导出：帧命名、画布、图集打包、骨架结构。

结构与既有工程 efRenter1101（Spine 3.8.99）对齐，模板能带来的那些字段
（插槽名 / 骨骼偏移 / 渲染尺寸）都要真的落到产物里。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.core.spine_export import (SPINE_VERSION, build_skeleton, fit_canvas,
                                   frame_name, pack_atlas)


# ---------------------------------------------------------------- 帧命名
@pytest.mark.parametrize("pattern,index,expect", [
    ("{anim}_{i:04d}", 1, "walk1_0001"),
    ("{anim}_{i:04d}", 0, "walk1_0000"),
    ("{anim}-{i:03d}", 12, "walk1-012"),
    ("{anim}{i:02d}", 7, "walk107"),
])
def test_帧命名格式(pattern, index, expect):
    assert frame_name("walk1", index, pattern) == expect


# ---------------------------------------------------------------- 画布
def test_画布让结果与源帧尺寸无关(rgba):
    """只给画布不给缩放系数时，不同源尺寸应得到同样的构图占比。

    曾经默认 0.4 缩放 + 128 画布，源图 200 时兜底不生效，角色只剩 38%。
    """
    ratios = []
    for size in (200, 320, 512, 640, 1024):
        img = rgba(size=size, box=int(size * 0.6))
        out = fit_canvas(img, 128, 1.0)
        assert out.shape[:2] == (128, 128)
        ys, xs = np.nonzero(out[:, :, 3] > 0)
        ratios.append(round((xs.max() - xs.min() + 1) / 128, 2))
    assert len(set(ratios)) == 1, f"不同源尺寸构图不一致：{ratios}"


def test_画布居中且不裁掉内容(rgba):
    img = rgba(size=64, box=64)           # 整幅不透明
    out = fit_canvas(img, 128, 1.0)
    assert out.shape[:2] == (128, 128)
    ys, xs = np.nonzero(out[:, :, 3] > 0)
    assert xs.min() == (128 - 64) // 2 and ys.min() == (128 - 64) // 2


def test_超出画布等比压到放得下(rgba):
    img = rgba(size=512, box=512)
    out = fit_canvas(img, 128, 1.0)
    assert out.shape[:2] == (128, 128)
    assert (out[:, :, 3] > 0).all(), "等比压缩后应铺满画布"


def test_缩放系数与画布同时给时先缩再压(rgba):
    img = rgba(size=320, box=320)
    assert fit_canvas(img, None, 0.4).shape[:2] == (128, 128)


# ---------------------------------------------------------------- 图集
def _parse_atlas(text):
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    i += 1
    page = {}
    while i < len(lines) and ":" in lines[i] and not lines[i].startswith(" "):
        k, v = lines[i].split(":", 1)
        page[k.strip()] = v.strip()
        i += 1
    regions, cur = {}, None
    for line in lines[i:]:
        if not line.strip():
            continue
        if not line.startswith(" "):
            cur = line.strip()
            regions[cur] = {}
        else:
            k, v = line.split(":", 1)
            regions[cur][k.strip()] = v.strip()
    return page, regions


def test_图集裁掉透明边并记录原尺寸与偏移(rgba):
    img = rgba(size=128, box=40)
    text, png = pack_atlas([("a_0001", img)], "page")
    page, regions = _parse_atlas(text)
    r = regions["a_0001"]
    assert r["orig"].replace(" ", "") == "128,128", "orig 应是裁剪前的原尺寸"
    w, h = [int(x) for x in r["size"].split(",")]
    assert (w, h) == (40, 40), f"裁剪后应只剩内容区，实际 {w}x{h}"
    ox, oy = [int(x) for x in r["offset"].split(",")]
    assert ox == (128 - 40) // 2, "左偏移应等于左侧空白宽度"
    assert oy == (128 - 40) // 2, "下偏移以左下角为基准"
    assert page["format"] == "RGBA8888" and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_图集裁剪阈值忽略极淡边缘():
    """缩放产生的 alpha 1~2 的边缘像素不计入包围盒（与官方打包器一致）。"""
    img = np.zeros((64, 64, 4), np.uint8)
    img[20:44, 20:44, 3] = 255
    img[10, 10, 3] = 2                    # 一枚极淡像素
    _, regions = _parse_atlas(pack_atlas([("a_0001", img)], "p")[0])
    w, h = [int(x) for x in regions["a_0001"]["size"].split(",")]
    assert (w, h) == (24, 24), f"极淡像素不该撑大包围盒，实际 {w}x{h}"


def test_图集区域数与帧数一致(rgba):
    items = [(f"a_{i:04d}", rgba(size=64, box=20 + i)) for i in range(12)]
    text, _ = pack_atlas(items, "page")
    _, regions = _parse_atlas(text)
    assert len(regions) == 12
    assert set(regions) == {n for n, _ in items}


def test_全透明帧不报错():
    blank = np.zeros((32, 32, 4), np.uint8)
    text, png = pack_atlas([("a_0001", blank)], "p")
    assert "a_0001" in text and png


# ---------------------------------------------------------------- 骨架
def _anim(name="walk1", n=3, **extra):
    return {"name": name, "frames": [f"{name}_{i:04d}" for i in range(n)],
            "fps": 12, "loop": True, "width": 128, "height": 128, **extra}


def test_骨架基本结构():
    skel = build_skeleton([_anim("walk1"), _anim("wait1")])
    assert skel["skeleton"]["spine"] == SPINE_VERSION
    assert [b["name"] for b in skel["bones"]] == ["root", "walk1", "wait1"]
    assert all(b.get("parent") == "root" for b in skel["bones"][1:])
    assert len(skel["slots"]) == 2
    assert set(skel["animations"]) == {"walk1", "wait1"}
    assert len(skel["skins"]) == 1 and skel["skins"][0]["name"] == "default"


def test_插槽名缺省取首帧且皮肤按插槽索引():
    skel = build_skeleton([_anim("walk1", 3)])
    slot = skel["slots"][0]["name"]
    assert slot == "walk1_0000"
    assert set(skel["skins"][0]["attachments"][slot]) == {
        "walk1_0000", "walk1_0001", "walk1_0002"}


def test_模板可覆盖插槽名():
    skel = build_skeleton([_anim("walk1", 3, slot="walk1_0006")])
    assert skel["slots"][0]["name"] == "walk1_0006"
    assert "walk1_0006" in skel["skins"][0]["attachments"]


def test_骨骼偏移与缩放写入骨架():
    """美术逐动画的对齐微调只存在于工程文件里，模板导入后要能照抄。"""
    skel = build_skeleton([_anim("bath1", 2,
                                 bone_offset={"x": -19.976, "y": 146.232},
                                 bone_scale={"x": 0.9, "y": 0.9})])
    bone = skel["bones"][1]
    assert bone["x"] == pytest.approx(-19.976)
    assert bone["y"] == pytest.approx(146.232)
    assert bone["scaleX"] == pytest.approx(0.9)


def test_缩放为1时不写scale字段():
    skel = build_skeleton([_anim("walk1", 2, bone_scale={"x": 1, "y": 1})])
    assert "scaleX" not in skel["bones"][1]


def test_渲染尺寸独立于导出尺寸():
    """贴图压缩过时附件仍按渲染尺寸画——搞错角色会缩到 2.5 分之一。"""
    skel = build_skeleton([_anim("walk1", 2, render=(320, 320),
                                 att_offset={"x": 27.7, "y": -58.4})])
    att = next(iter(skel["skins"][0]["attachments"].values()))
    first = next(iter(att.values()))
    assert (first["width"], first["height"]) == (320, 320)
    assert first["x"] == pytest.approx(27.7)
    assert skel["skeleton"]["width"] == 320, "包围盒应按渲染尺寸算"


def test_时间轴逐帧切附件且末尾补一帧():
    skel = build_skeleton([_anim("walk1", 4)])
    keys = skel["animations"]["walk1"]["slots"]["walk1_0000"]["attachment"]
    assert len(keys) == 5, "4 帧应有 5 个 key（末尾补一个定时长）"
    assert [k["name"] for k in keys[:4]] == [f"walk1_{i:04d}" for i in range(4)]
    assert keys[0]["time"] == 0
    assert keys[-1]["time"] == pytest.approx(4 / 12)


def test_循环动画末尾回到首帧_单次动画保持末帧():
    loop = build_skeleton([_anim("walk1", 3, loop=True)])
    once = build_skeleton([_anim("walk1", 3, loop=False)])
    lk = loop["animations"]["walk1"]["slots"]["walk1_0000"]["attachment"]
    ok = once["animations"]["walk1"]["slots"]["walk1_0000"]["attachment"]
    assert lk[-1]["name"] == "walk1_0000", "循环动画末尾应回到首帧"
    assert ok[-1]["name"] == "walk1_0002", "单次动画应保持结束姿势"


def test_版本可由模板指定():
    skel = build_skeleton([_anim()], version="3.8.75")
    assert skel["skeleton"]["spine"] == "3.8.75"


def test_空帧动画被跳过():
    skel = build_skeleton([_anim("walk1", 2), {"name": "empty", "frames": [],
                                               "fps": 12, "width": 1, "height": 1}])
    assert "empty" not in skel["animations"]
    assert [b["name"] for b in skel["bones"]] == ["root", "walk1"]
