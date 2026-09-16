"""Spine 工程反解：atlas 解析、帧名格式推断、JSON / skel 骨架。

参考工程（efRenter1101）不在仓库里，相关用例在缺文件时跳过；
不依赖它的部分用构造数据覆盖。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.spine_import import (SpineImportError, build_template,
                                   infer_frame_pattern, parse_atlas, parse_json,
                                   parse_skel)

REFERENCE = Path(r"D:\AI\efRenter1101源文件\efRenter1101源文件")
needs_ref = pytest.mark.skipif(not REFERENCE.is_dir(),
                               reason="参考工程不在本机")


# ---------------------------------------------------------------- atlas
ATLAS_TEXT = """
page.png
size: 1525,916
format: RGBA8888
filter: Linear,Linear
repeat: none
bath1_0001
  rotate: false
  xy: 1139, 139
  size: 40, 68
  orig: 128, 128
  offset: 44, 28
  index: -1
bath1_0002
  rotate: false
  xy: 1049, 490
  size: 40, 69
  orig: 128, 128
  offset: 45, 28
  index: -1
"""


def test_解析atlas页头与区域():
    at = parse_atlas(ATLAS_TEXT)
    assert at["page"]["file"] == "page.png"
    assert at["page"]["size"] == "1525,916"
    assert at["page"]["format"] == "RGBA8888"
    assert list(at["regions"]) == ["bath1_0001", "bath1_0002"]
    assert at["regions"]["bath1_0001"]["orig"] == "128, 128"


# ---------------------------------------------------------------- 帧名推断
@pytest.mark.parametrize("names,pattern,start", [
    (["walk1_0001", "walk1_0002"], "{anim}_{i:04d}", 1),
    (["walk1_0000", "walk1_0001"], "{anim}_{i:04d}", 0),
    (["walk1-001", "walk1-002"], "{anim}-{i:03d}", 1),
])
def test_推断帧命名格式(names, pattern, start):
    p, s = infer_frame_pattern(names)
    assert p == pattern
    assert s == start


def test_起始帧号取各动画最小值里最常见的():
    names = ["a_0000", "a_0001", "b_0000", "b_0001", "c_0001"]
    _, start = infer_frame_pattern(names)
    assert start == 0, "两个动画从 0 起、一个从 1 起，应取 0"


def test_无分隔符时帧号宽度存在歧义():
    """walk101 既可能是 walk1+01 也可能是 walk+101，无分隔符时无解。

    取贪心最短动作名（walk + 3 位帧号）。真实工程都带分隔符，属边角情况；
    真遇上了在导出弹窗里手改帧名格式即可。
    """
    p, _ = infer_frame_pattern(["walk101", "walk102"])
    assert p == "{anim}{i:03d}"


def test_无法识别时给出缺省():
    p, s = infer_frame_pattern(["没有数字", "也没有"])
    assert p == "{anim}_{i:04d}" and s is None


# ---------------------------------------------------------------- JSON 骨架
def _json_skeleton():
    return {
        "skeleton": {"hash": "h", "spine": "3.8.99", "images": "./images/"},
        "bones": [{"name": "root"},
                  {"name": "walk1", "parent": "root", "x": -3.7, "y": -22.6,
                   "scaleX": 0.95, "scaleY": 0.95}],
        "slots": [{"name": "walk1_0006", "bone": "walk1"}],
        "skins": [{"name": "default", "attachments": {
            "walk1_0006": {
                "walk1_0000": {"width": 320, "height": 320, "x": 27.7, "y": -58.4},
                "walk1_0001": {"width": 320, "height": 320, "x": 27.7, "y": -58.4},
            }}}],
        "animations": {"walk1": {"slots": {"walk1_0006": {"attachment": [
            {"time": 0, "name": "walk1_0000"},
            {"time": 0.1, "name": "walk1_0001"},
            {"time": 0.2, "name": "walk1_0000"},
        ]}}}},
    }


def test_解析JSON骨架():
    sk = parse_json(_json_skeleton())
    assert sk["version"] == "3.8.99"
    assert [b["name"] for b in sk["bones"]] == ["root", "walk1"]
    assert sk["slots"][0]["name"] == "walk1_0006"
    assert len(sk["skins"]) == 1
    assert list(sk["animations"]) == ["walk1"]


def test_模板收敛出导出约定():
    tpl = build_template(parse_json(_json_skeleton()), parse_atlas(ATLAS_TEXT), "t")
    assert tpl["spine_version"] == "3.8.99"
    assert tpl["frame_pattern"] == "{anim}_{i:04d}"
    assert tpl["canvas"] == 320, "画布应取附件的渲染尺寸"
    a = tpl["animations"][0]
    assert a["name"] == "walk1"
    assert a["slot"] == "walk1_0006", "插槽名未必是首帧，要照抄"
    assert a["offset"] == {"x": -3.7, "y": -22.6}
    assert a["scale"] == {"x": 0.95, "y": 0.95}
    assert a["render"] == [320, 320]
    assert a["att_offset"] == {"x": 27.7, "y": -58.4}
    assert a["fps"] == 10.0, "由关键帧间隔 0.1s 反推"
    assert a["loop"] is True, "末帧回到首帧即循环"


def test_图集压缩比由orig与渲染尺寸算出():
    """贴图按 0.4 压过、附件仍按 320 画——比例要照抄，否则贴图白白变大。"""
    tpl = build_template(parse_json(_json_skeleton()), parse_atlas(ATLAS_TEXT), "t")
    assert tpl["atlas"]["scale"] == pytest.approx(0.4), tpl["atlas"]
    assert tpl["atlas"]["format"] == "RGBA8888"


def test_记录皮肤数量供导入端提示():
    tpl = build_template(parse_json(_json_skeleton()), None, "t")
    assert tpl["skin_count"] == 1 and tpl["skin_names"] == ["default"]


# ---------------------------------------------------------------- skel 二进制
def test_拒绝不支持的二进制版本():
    # hash(长度前缀字符串) + version "4.1.00"
    data = bytes([2, ord("h"), 7]) + b"4.1.00"
    with pytest.raises(SpineImportError, match="只支持 Spine"):
        parse_skel(data)


def test_截断文件给出明确错误():
    with pytest.raises(SpineImportError, match="提前结束"):
        parse_skel(bytes([2, ord("h"), 7]) + b"3.8.9")


@needs_ref
def test_解析真实skel与已知真值一致():
    sk = parse_skel((REFERENCE / "shuchu" / "efRenter1101.skel").read_bytes())
    assert sk["version"] == "3.8.99"
    assert len(sk["bones"]) == 21
    assert len(sk["slots"]) == 20
    assert len(sk["animations"]) == 20
    atts = sum(len(b) for s in sk["skins"] for b in s["attachments"].values())
    assert atts == 387


@needs_ref
def test_真实工程反解出的模板():
    from app.core.spine_import import load_reference
    tpl = load_reference(REFERENCE, "efRenter1101")
    assert tpl["spine_version"] == "3.8.99"
    assert tpl["frame_pattern"] == "{anim}_{i:04d}"
    assert tpl["canvas"] == 320
    assert len(tpl["animations"]) == 20
    assert tpl["atlas"]["scale"] == pytest.approx(0.4)
    assert any(a["offset"] != {"x": 0.0, "y": 0.0} for a in tpl["animations"]), \
        "美术的逐动画对齐偏移应被反解出来"
