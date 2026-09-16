"""读取既有 Spine 工程，反解出一份可复用的导出模板。

支持两种参考来源：
    .json   Spine 官方导出的骨架数据（公开格式，优先）
    .skel   3.8 二进制骨架数据（自解析；只读我们需要的那几层）
配套的 .atlas 提供画布尺寸与图集参数。

反解出来的模板记录导出时要照抄的东西：
    骨架版本、帧命名格式与起始帧号、统一画布边长、图集参数，
    以及每个动画的 插槽名 / 骨骼偏移与缩放 / 帧率 / 帧数。

骨骼偏移是关键：美术会逐个动画微调位置让角色站位对齐，
这些值只存在于工程文件里，不看参考工程就复现不出来。
"""
from __future__ import annotations

import json
import re
import struct
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SUPPORTED_BINARY_VERSIONS = ("3.8",)


class SpineImportError(Exception):
    """参考工程无法解析（消息即原因）。"""


# ------------------------------------------------------------ 二进制读取
class _Reader:
    """Spine 二进制流：大端、varint、长度前缀字符串。"""

    def __init__(self, data: bytes):
        self.b, self.i = data, 0

    def _need(self, n: int) -> None:
        if self.i + n > len(self.b):
            raise SpineImportError("文件在偏移 %d 处提前结束" % self.i)

    def byte(self) -> int:
        self._need(1)
        v = self.b[self.i]
        self.i += 1
        return v

    def boolean(self) -> bool:
        return self.byte() != 0

    def int32(self) -> int:
        self._need(4)
        v = struct.unpack_from(">i", self.b, self.i)[0]
        self.i += 4
        return v

    def float(self) -> float:
        self._need(4)
        v = struct.unpack_from(">f", self.b, self.i)[0]
        self.i += 4
        return v

    def varint(self, optimize_positive: bool = True) -> int:
        b = self.byte()
        result = b & 0x7F
        for shift in (7, 14, 21, 28):
            if not (b & 0x80):
                break
            b = self.byte()
            result |= (b & 0x7F) << shift
        if optimize_positive:
            return result
        return (result >> 1) ^ -(result & 1)

    def string(self) -> Optional[str]:
        n = self.varint()
        if n == 0:
            return None
        if n == 1:
            return ""
        self._need(n - 1)
        s = self.b[self.i:self.i + n - 1].decode("utf-8", "replace")
        self.i += n - 1
        return s


# 附件类型（3.8）：只有 region 需要读字段，其余我们不用但要跳过
_A_REGION, _A_BOUNDING, _A_MESH, _A_LINKED, _A_PATH, _A_POINT, _A_CLIP = range(7)

# 时间轴类型（3.8）
_SLOT_ATTACHMENT, _SLOT_COLOR, _SLOT_TWO_COLOR = 0, 1, 2
_BONE_ROTATE, _BONE_TRANSLATE, _BONE_SCALE, _BONE_SHEAR = 0, 1, 2, 3


def _read_curve(r: _Reader) -> None:
    t = r.byte()
    if t == 2:                       # bezier
        for _ in range(4):
            r.float()


def parse_skel(data: bytes) -> dict:
    """解析 3.8 .skel，返回 {version, bones, slots, skins, animations}。"""
    r = _Reader(data)
    hash_ = r.string()
    version = r.string() or ""
    if not version.startswith(SUPPORTED_BINARY_VERSIONS):
        raise SpineImportError(
            "只支持 Spine %s 的二进制骨架，该文件是 %s；"
            "请改用 Spine 里 Export → JSON 导出的 .json 作为参考"
            % ("/".join(SUPPORTED_BINARY_VERSIONS), version or "未知版本"))
    x, y, w, h = r.float(), r.float(), r.float(), r.float()
    nonessential = r.boolean()
    fps = images_path = None
    if nonessential:
        fps = r.float()
        images_path = r.string()
        r.string()                                    # audioPath

    strings = [r.string() for _ in range(r.varint())]

    def sref() -> Optional[str]:
        idx = r.varint()
        return strings[idx - 1] if idx else None

    # ---- 骨骼 ----
    bones = []
    for i in range(r.varint()):
        name = r.string()
        parent = r.varint() if i else None
        rot = r.float()
        bx, by = r.float(), r.float()
        sx, sy = r.float(), r.float()
        r.float(), r.float()                          # shearX/Y
        r.float()                                     # length
        r.varint()                                    # transformMode
        r.boolean()                                   # skinRequired
        if nonessential:
            r.int32()                                 # color
        bones.append({"name": name, "parent": parent, "rotation": rot,
                      "x": bx, "y": by, "scaleX": sx, "scaleY": sy})

    # ---- 插槽 ----
    slots = []
    for _ in range(r.varint()):
        name = r.string()
        bone = r.varint()
        r.int32()                                     # color
        r.int32()                                     # darkColor
        attachment = sref()
        r.varint()                                    # blendMode
        slots.append({"name": name, "bone": bone, "attachment": attachment})

    # ---- 约束：逐条跳过 ----
    for _ in range(r.varint()):                       # IK
        r.string()
        r.varint()
        r.boolean()
        for _ in range(r.varint()):
            r.varint()
        r.varint()
        r.float(); r.float()
        r.byte(); r.byte(); r.byte(); r.byte()
    for _ in range(r.varint()):                       # transform
        r.string()
        r.varint()
        r.boolean()
        for _ in range(r.varint()):
            r.varint()
        r.varint()
        r.boolean(); r.boolean()
        for _ in range(8):
            r.float()
    for _ in range(r.varint()):                       # path
        r.string()
        r.varint()
        r.boolean()
        for _ in range(r.varint()):
            r.varint()
        r.varint()
        r.varint(); r.varint(); r.varint()
        r.float(); r.float()
        for _ in range(4):
            r.float()

    # ---- 皮肤 ----
    def read_attachment(default_name: Optional[str]) -> Optional[dict]:
        name = sref() or default_name
        atype = r.byte()
        if atype != _A_REGION:
            raise SpineImportError(
                "参考工程含非 region 附件（类型 %d，如网格/裁剪）——"
                "当前导出只支持纯逐帧贴图动画" % atype)
        path = sref() or name
        rot = r.float()
        ax, ay = r.float(), r.float()
        asx, asy = r.float(), r.float()
        aw, ah = r.float(), r.float()
        r.int32()                                     # color
        return {"name": name, "path": path, "rotation": rot, "x": ax, "y": ay,
                "scaleX": asx, "scaleY": asy, "width": aw, "height": ah}

    def read_skin(default: bool) -> Optional[dict]:
        if default:
            slot_count = r.varint()
            if slot_count == 0:
                return None
            skin_name = "default"
        else:
            skin_name = sref()
            for _ in range(r.varint()):
                r.varint()                            # bones
            for _ in range(3):
                for _ in range(r.varint()):
                    r.varint()                        # 三类约束
            slot_count = r.varint()
        atts: Dict[int, Dict[str, dict]] = {}
        for _ in range(slot_count):
            slot_index = r.varint()
            bucket = atts.setdefault(slot_index, {})
            for _ in range(r.varint()):
                key = sref()
                a = read_attachment(key)
                if a:
                    bucket[key or a["name"]] = a
        return {"name": skin_name, "attachments": atts}

    skins = []
    default_skin = read_skin(True)
    if default_skin:
        skins.append(default_skin)
    for _ in range(r.varint()):
        s = read_skin(False)
        if s:
            skins.append(s)

    # 3.8 的 linked mesh 是解析期在内存里回填的，流里没有对应段落，不要读

    for _ in range(r.varint()):                       # events
        sref()
        r.varint(False); r.float(); r.string()
        if r.string():
            r.float(); r.float()

    # ---- 动画 ----
    animations = OrderedDict()
    for _ in range(r.varint()):
        aname = r.string()
        animations[aname] = _read_animation(r, sref, len(bones))

    return {"hash": hash_, "version": version, "fps": fps,
            "images_path": images_path, "bbox": (x, y, w, h),
            "bones": bones, "slots": slots, "skins": skins,
            "animations": animations, "strings": strings}


def _read_animation(r: _Reader, sref, bone_count: int) -> dict:
    """只保留我们用得上的：插槽附件切换 + 骨骼位移/缩放。"""
    slot_keys: Dict[int, List[Tuple[float, Optional[str]]]] = {}
    for _ in range(r.varint()):
        slot_index = r.varint()
        for _ in range(r.varint()):
            ttype = r.byte()
            frames = r.varint()
            if ttype == _SLOT_ATTACHMENT:
                slot_keys[slot_index] = [(r.float(), sref()) for _ in range(frames)]
            elif ttype == _SLOT_COLOR:
                for f in range(frames):
                    r.float(); r.int32()
                    if f < frames - 1:
                        _read_curve(r)
            elif ttype == _SLOT_TWO_COLOR:
                for f in range(frames):
                    r.float(); r.int32(); r.int32()
                    if f < frames - 1:
                        _read_curve(r)
            else:
                raise SpineImportError("未知的插槽时间轴类型 %d" % ttype)

    bone_keys: Dict[int, dict] = {}
    for _ in range(r.varint()):
        bone_index = r.varint()
        info = bone_keys.setdefault(bone_index, {})
        for _ in range(r.varint()):
            ttype = r.byte()
            frames = r.varint()
            if ttype == _BONE_ROTATE:
                for f in range(frames):
                    r.float(); r.float()
                    if f < frames - 1:
                        _read_curve(r)
            elif ttype in (_BONE_TRANSLATE, _BONE_SCALE, _BONE_SHEAR):
                vals = []
                for f in range(frames):
                    t = r.float()
                    vals.append((t, r.float(), r.float()))
                    if f < frames - 1:
                        _read_curve(r)
                info["translate" if ttype == _BONE_TRANSLATE else
                     "scale" if ttype == _BONE_SCALE else "shear"] = vals
            else:
                raise SpineImportError("未知的骨骼时间轴类型 %d" % ttype)

    # IK / transform / path / deform / drawOrder / event：一律跳过
    for _ in range(r.varint()):                       # IK
        r.varint()
        for f in range(r.varint()):
            r.float(); r.float(); r.byte(); r.boolean(); r.boolean()
            _read_curve(r)
    for _ in range(r.varint()):                       # transform
        r.varint()
        for f in range(r.varint()):
            r.float(); r.float(); r.float(); r.float(); r.float()
            _read_curve(r)
    for _ in range(r.varint()):                       # path
        r.varint()
        for _ in range(r.varint()):
            r.byte()
            for f in range(r.varint()):
                r.float(); r.float()
                _read_curve(r)
    for _ in range(r.varint()):                       # deform
        for _ in range(r.varint()):
            r.varint()
            for _ in range(r.varint()):
                raise SpineImportError("参考工程含形变动画，当前导出不支持")
    for _ in range(r.varint()):                       # draw order
        r.float()
        for _ in range(r.varint()):
            r.varint(); r.varint()
    for _ in range(r.varint()):                       # events
        r.float(); r.varint()
        r.varint(False); r.float()
        if r.boolean():
            r.string()

    return {"slots": slot_keys, "bones": bone_keys}


# ------------------------------------------------------------ JSON 参考
def parse_json(payload: dict) -> dict:
    """Spine 官方 JSON 骨架数据 → 与 parse_skel 同构的结果。"""
    sk = payload.get("skeleton") or {}
    bones = [{"name": b.get("name"), "parent": b.get("parent"),
              "rotation": b.get("rotation", 0), "x": b.get("x", 0),
              "y": b.get("y", 0), "scaleX": b.get("scaleX", 1),
              "scaleY": b.get("scaleY", 1)} for b in payload.get("bones", [])]
    name_to_idx = {b["name"]: i for i, b in enumerate(bones)}
    slots = [{"name": s.get("name"), "bone": name_to_idx.get(s.get("bone"), 0),
              "attachment": s.get("attachment")} for s in payload.get("slots", [])]
    slot_idx = {s["name"]: i for i, s in enumerate(slots)}

    skins = []
    raw_skins = payload.get("skins")
    if isinstance(raw_skins, dict):                   # 3.7 及更早：{皮肤名: {...}}
        raw_skins = [{"name": k, "attachments": v} for k, v in raw_skins.items()]
    for sk_item in (raw_skins or []):
        atts: Dict[int, Dict[str, dict]] = {}
        for slot_name, bucket in (sk_item.get("attachments") or {}).items():
            idx = slot_idx.get(slot_name)
            if idx is None:
                continue
            atts[idx] = {k: {"name": k, "path": v.get("path", k),
                             "rotation": v.get("rotation", 0),
                             "x": v.get("x", 0), "y": v.get("y", 0),
                             "scaleX": v.get("scaleX", 1), "scaleY": v.get("scaleY", 1),
                             "width": v.get("width", 0), "height": v.get("height", 0)}
                         for k, v in bucket.items()}
        skins.append({"name": sk_item.get("name", "default"), "attachments": atts})

    animations = OrderedDict()
    for aname, a in (payload.get("animations") or {}).items():
        slot_keys, bone_keys = {}, {}
        for slot_name, tl in (a.get("slots") or {}).items():
            idx = slot_idx.get(slot_name)
            if idx is None or "attachment" not in tl:
                continue
            slot_keys[idx] = [(k.get("time", 0), k.get("name"))
                              for k in tl["attachment"]]
        for bone_name, tl in (a.get("bones") or {}).items():
            idx = name_to_idx.get(bone_name)
            if idx is None:
                continue
            info = {}
            for key, field in (("translate", "translate"), ("scale", "scale")):
                if key in tl:
                    info[field] = [(k.get("time", 0), k.get("x", 0), k.get("y", 0))
                                   for k in tl[key]]
            if info:
                bone_keys[idx] = info
        animations[aname] = {"slots": slot_keys, "bones": bone_keys}

    return {"hash": sk.get("hash", ""), "version": sk.get("spine", ""),
            "fps": sk.get("fps"), "images_path": sk.get("images"),
            "bbox": (sk.get("x", 0), sk.get("y", 0),
                     sk.get("width", 0), sk.get("height", 0)),
            "bones": bones, "slots": slots, "skins": skins,
            "animations": animations, "strings": []}


# ------------------------------------------------------------ atlas
def parse_atlas(text: str) -> dict:
    """libgdx atlas → {page, regions{名: 字段}}。"""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    page = {"file": lines[i].strip() if i < len(lines) else ""}
    i += 1
    while i < len(lines) and ":" in lines[i] and not lines[i].startswith((" ", "\t")):
        k, v = lines[i].split(":", 1)
        page[k.strip()] = v.strip()
        i += 1
    regions, cur = OrderedDict(), None
    for line in lines[i:]:
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            cur = line.strip()
            regions[cur] = {}
        elif cur is not None and ":" in line:
            k, v = line.split(":", 1)
            regions[cur][k.strip()] = v.strip()
    return {"page": page, "regions": regions}


# ------------------------------------------------------------ 反解成模板
_FRAME_RE = re.compile(r"^(?P<anim>.+?)(?P<sep>[_\-]?)(?P<num>\d+)$")


def infer_frame_pattern(names: List[str]) -> Tuple[str, Optional[int]]:
    """从帧名反推命名格式与起始帧号，例如 walk1_0001 → ({anim}_{i:04d}, 1)。"""
    seps, widths, firsts = Counter(), Counter(), {}
    for n in names:
        m = _FRAME_RE.match(n)
        if not m:
            continue
        seps[m.group("sep")] += 1
        widths[len(m.group("num"))] += 1
        anim = m.group("anim")
        v = int(m.group("num"))
        firsts[anim] = min(firsts.get(anim, v), v)
    if not widths:
        return "{anim}_{i:04d}", None
    sep = seps.most_common(1)[0][0]
    width = widths.most_common(1)[0][0]
    start = Counter(firsts.values()).most_common(1)[0][0] if firsts else None
    return "{anim}%s{i:0%dd}" % (sep, width), start


def _fps_of(times: List[float]) -> Optional[float]:
    """由关键帧间隔反推帧率（取最常见的间隔）。"""
    gaps = [round(b - a, 4) for a, b in zip(times, times[1:]) if b > a]
    if not gaps:
        return None
    common = Counter(gaps).most_common(1)[0][0]
    return round(1.0 / common, 3) if common > 0 else None


def build_template(skeleton: dict, atlas: Optional[dict] = None,
                   name: str = "") -> dict:
    """把解析结果收敛成导出模板。"""
    bones = skeleton["bones"]
    slots = skeleton["slots"]
    all_atts = []
    for sk in skeleton["skins"]:
        for bucket in sk["attachments"].values():
            all_atts.extend(bucket.keys())
    pattern, start = infer_frame_pattern(all_atts)

    # 画布：附件自带宽高最可信，其次用 atlas 的 orig
    canvas = None
    sizes = Counter()
    for sk in skeleton["skins"]:
        for bucket in sk["attachments"].values():
            for a in bucket.values():
                if a.get("width") and a.get("height"):
                    sizes[(int(a["width"]), int(a["height"]))] += 1
    if sizes:
        canvas = max(sizes.most_common(1)[0][0])
    elif atlas:
        origs = Counter(r.get("orig", "") for r in atlas["regions"].values())
        if origs:
            try:
                canvas = max(int(v) for v in origs.most_common(1)[0][0].split(","))
            except ValueError:
                canvas = None

    anims = []
    for aname, a in skeleton["animations"].items():
        keys = next(iter(a["slots"].values()), [])
        times = [t for t, _ in keys]
        names = [n for _, n in keys if n]
        slot_index = next(iter(a["slots"].keys()), None)
        bone_name, offset, scale = None, None, None
        # 该动画控制的插槽 → 绑定的骨骼 → 静态偏移（美术的对齐微调）
        if slot_index is not None and slot_index < len(slots):
            bi = slots[slot_index]["bone"]
            if 0 <= bi < len(bones):
                b = bones[bi]
                bone_name = b["name"]
                offset = {"x": round(b["x"], 3), "y": round(b["y"], 3)}
                scale = {"x": round(b["scaleX"], 4), "y": round(b["scaleY"], 4)}

        # 附件自己也带偏移与渲染尺寸：贴图可能被压缩过，渲染尺寸才是画面上的大小
        render, att_offset = None, None
        for sk in skeleton["skins"]:
            bucket = sk["attachments"].get(slot_index)
            if not bucket:
                continue
            first = next(iter(bucket.values()), None)
            if first:
                if first.get("width") and first.get("height"):
                    render = [int(first["width"]), int(first["height"])]
                if first.get("x") or first.get("y"):
                    att_offset = {"x": round(first["x"], 3), "y": round(first["y"], 3)}
            break

        anims.append({
            "name": aname,
            "slot": slots[slot_index]["name"] if slot_index is not None
                    and slot_index < len(slots) else None,
            "bone": bone_name,
            "offset": offset,
            "scale": scale,
            "render": render,
            "att_offset": att_offset,
            "frames": len(names),
            "fps": _fps_of(times),
            # 末帧回到首帧即为循环衔接
            "loop": bool(names) and len(names) > 1 and names[-1] == names[0],
        })

    tpl = {
        "name": name or "导入的模板",
        "spine_version": skeleton.get("version") or "",
        # 导出只产出一套 default 皮肤；参考工程有多套时导入端会给出警告
        "skin_count": len(skeleton.get("skins") or []),
        "skin_names": [sk.get("name") for sk in (skeleton.get("skins") or [])],
        "frame_pattern": pattern,
        "start_index": start if start is not None else 1,
        "canvas": canvas,
        "images_path": skeleton.get("images_path") or "./images/",
        "animations": anims,
        "bone_offsets": {a["name"]: a["offset"] for a in anims if a["offset"]},
        "bone_scales": {a["name"]: a["scale"] for a in anims
                        if a["scale"] and (a["scale"]["x"] != 1 or a["scale"]["y"] != 1)},
        "slot_names": {a["name"]: a["slot"] for a in anims if a["slot"]},
    }
    if atlas:
        p = atlas["page"]
        # 图集贴图可能按比例压缩过（打包时缩放），而附件仍按原尺寸渲染。
        # orig / 渲染尺寸 就是这个压缩比，导出时要照抄，否则贴图白白变大。
        atlas_scale = None
        origs = Counter(r.get("orig", "") for r in atlas["regions"].values())
        if origs and canvas:
            try:
                o = max(int(v) for v in origs.most_common(1)[0][0].split(","))
                if o and canvas:
                    atlas_scale = round(o / canvas, 4)
            except ValueError:
                pass
        tpl["atlas"] = {"format": p.get("format", "RGBA8888"),
                        "filter": p.get("filter", "Linear,Linear"),
                        "repeat": p.get("repeat", "none"),
                        "page_size": p.get("size", ""),
                        "regions": len(atlas["regions"]),
                        "scale": atlas_scale}
    return tpl


def load_reference(path: Path, name: str = "") -> dict:
    """读一个参考工程（目录或 .json/.skel 文件），返回导出模板。"""
    path = Path(path)
    skel_file = json_file = atlas_file = None
    if path.is_dir():
        cands = list(path.rglob("*.json")) + list(path.rglob("*.skel")) + \
            list(path.rglob("*.atlas"))
        for f in cands:
            if f.suffix == ".skel" and skel_file is None:
                skel_file = f
            elif f.suffix == ".atlas" and atlas_file is None:
                atlas_file = f
            elif f.suffix == ".json" and json_file is None:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                    continue
                if isinstance(d, dict) and "skeleton" in d and "bones" in d:
                    json_file = f
    elif path.suffix == ".json":
        json_file = path
    elif path.suffix == ".skel":
        skel_file = path
    else:
        raise SpineImportError("请指向 Spine 工程目录，或 .json / .skel 文件")

    if json_file is None and skel_file is None:
        raise SpineImportError("目录里没找到 .json 或 .skel 骨架数据")
    if atlas_file is None and path.is_dir():
        pass                                          # 图集可选

    if json_file is not None:
        skeleton = parse_json(json.loads(json_file.read_text(encoding="utf-8")))
        source = json_file
    else:
        skeleton = parse_skel(skel_file.read_bytes())
        source = skel_file

    atlas = None
    if atlas_file is not None:
        atlas = parse_atlas(atlas_file.read_text(encoding="utf-8"))

    tpl = build_template(skeleton, atlas, name or source.stem)
    tpl["source"] = str(source)
    tpl["source_kind"] = source.suffix.lstrip(".")
    return tpl
