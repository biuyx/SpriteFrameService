"""数据目录体检：迁移（整个 data/ 从别的机器搬过来）之后跑一遍，确认没缺东西。

    .venv/bin/python scripts/check_data.py                 # 读 backend/.env 里的 SPRITE_DATA_DIR
    .venv/bin/python scripts/check_data.py /data/sprite-data

查这些：
  · 角色 / 动作 / 帧：帧文件是否都在约定位置（frames/raw|proc/<帧id>.png）
  · 元数据说抠过图、但处理图不在的帧——搬漏了，界面上会显示成未抠图
  · 参考视频库的视频、首帧参考集的图片是否都在
  · 文件名乱码：用资源管理器「发送到压缩文件夹」打的包，中文名常按 GBK 存，
    到 Linux 上解出来就是乱码字节，界面里找不到这些文件

有问题时退出码为 1。只读，不改任何东西。
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


def _bad_name(name: str) -> bool:
    """非 UTF-8 文件名在 Python 里会变成 \\udc80-\\udcff 的代理字符。"""
    return any("\udc80" <= ch <= "\udcff" for ch in name)


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check(data: Path) -> int:
    problems: list = []
    stat = Counter()

    def problem(msg):
        problems.append(msg)

    if not data.is_dir():
        print(f"数据目录不存在: {data}")
        return 1

    # ---- 文件名乱码
    garbled = [p for p in data.rglob("*") if _bad_name(p.name)]
    if garbled:
        problem(f"{len(garbled)} 个文件名是乱码（多半是 GBK 编码的 zip 解出来的），例: "
                f"{garbled[0].relative_to(data)!r}")

    # ---- 角色 / 动作 / 帧
    for sprite_json in sorted(data.glob("sprites/*/sprite.json")):
        sp = _load(sprite_json) or {}
        stat["角色"] += 1
        for ref in sp.get("actions", []):
            adir = sprite_json.parent / "actions" / ref["id"]
            if not (adir / "action.json").is_file():
                problem(f"角色「{sp.get('name')}」的动作「{ref.get('name')}」档案缺失")
                continue
            stat["动作"] += 1
            if (adir / "first_frame.png").is_file():
                stat["首帧"] += 1
            frames = _load(adir / "frames.json") or []
            for f in frames:
                fid = f.get("id")
                if not fid:
                    continue
                stat["帧"] += 1
                if not (adir / "frames" / "raw" / f"{fid}.png").is_file():
                    stat["缺原始帧"] += 1
                if f.get("processed_path"):
                    stat["已抠图"] += 1
                    if not (adir / "frames" / "proc" / f"{fid}.png").is_file():
                        stat["缺处理图"] += 1
            if (adir / "video" / "takes.json").is_file():
                for t in (_load(adir / "video" / "takes.json") or {}).get("takes", []):
                    if t.get("status") == "succeeded":
                        stat["视频版本"] += 1
    if stat["缺原始帧"]:
        problem(f"{stat['缺原始帧']} 帧的原始图不在")
    if stat["缺处理图"]:
        problem(f"{stat['缺处理图']} 帧标记为已抠图、但处理图不在（界面会显示成未抠图）")

    # ---- 参考视频库
    tdir = data / "templates"
    tjson = _load(tdir / "templates.json") or []
    tlist = tjson if isinstance(tjson, list) else tjson.get("templates", [])
    for t in tlist:
        stat["模板"] += 1
        if t.get("file") and not (tdir / t["file"]).is_file():
            stat["缺模板视频"] += 1
    if stat["缺模板视频"]:
        problem(f"{stat['缺模板视频']} 个参考视频模板的视频文件不在")

    # ---- 首帧参考集
    for sj in data.glob("first_frame_sets/*/set.json"):
        s = _load(sj) or {}
        stat["参考集"] += 1
        for f in s.get("frames", []):
            if f.get("file") and not (sj.parent / f["file"]).is_file():
                stat["缺参考集图"] += 1
    if stat["缺参考集图"]:
        problem(f"{stat['缺参考集图']} 张参考集首帧图不在")

    # ---- 其他
    stat["Spine 模板"] = len(_load(data / "spine_templates.json") or [])
    stat["旧会话"] = sum(1 for p in (data / "sessions").glob("*") if p.is_dir()) \
        if (data / "sessions").is_dir() else 0
    size = sum(p.stat().st_size for p in data.rglob("*") if p.is_file())

    print(f"数据目录: {data}（{size / 1024 ** 3:.2f} GB）")
    order = ["角色", "动作", "首帧", "帧", "已抠图", "视频版本", "模板", "参考集",
             "Spine 模板", "旧会话"]
    print("  " + " · ".join(f"{k} {stat[k]}" for k in order))
    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for p in problems:
            print("  ✗ " + p)
        return 1
    print("\n✓ 没发现问题")
    return 0


def main():
    if len(sys.argv) > 1:
        data = Path(sys.argv[1])
    else:
        from app.config import get_settings
        data = get_settings().resolved_data_dir
    sys.exit(check(data))


if __name__ == "__main__":
    os.environ.setdefault("SPRITE_WARMUP_MODEL", "false")
    main()
