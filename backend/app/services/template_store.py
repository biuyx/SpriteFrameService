"""动作模板库：全局共享的参考动作视频。

布局：
    data/templates/
        templates.json     索引
        tp_xxxxxxxx.mp4    视频本体

模板记录：
    {id, key, variant, filename, file, bytes, created_at,
     oss_url, oss_hash}   # OSS URL 按内容哈希缓存——同一模板给任意多个
                          # 动作/角色复用，只上传一次

文件名解析（适配实际命名习惯，空格/中英括号混用/多括号组均可）：
    "14_sit_front  (工作).mp4"        → key=14_sit_front, variant=工作
    "01_idle_front (待机)（4秒）.mp4" → key=01_idle_front, variant=待机, duration_hint=4
    "01_idle_front.mp4"               → key=01_idle_front, variant=""
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from app.config import get_settings

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

_GROUP_RE = re.compile(r"[（(]\s*([^)）]*?)\s*[)）]")
_DUR_RE = re.compile(r"^(\d+)\s*秒$")


def parse_template_name(stem: str) -> tuple[str, str, int | None]:
    """从文件名主干解析 (key, variant, duration_hint)。

    括号组可有多个：不含「N秒」的第一组为变体名，「N秒」组为建议时长。
    """
    stem = stem.replace("　", " ").strip()
    groups = _GROUP_RE.findall(stem)
    key = _GROUP_RE.sub("", stem).strip().strip("_ ")
    variant, duration = "", None
    for g in groups:
        g = g.strip()
        m = _DUR_RE.match(g)
        if m:
            duration = int(m.group(1))
        elif not variant and g:
            variant = g
    return key, variant, duration


class TemplateStore:
    def __init__(self, root: Optional[Path] = None):
        self._root = root
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root or (get_settings().resolved_data_dir / "templates")

    @property
    def _json(self) -> Path:
        return self.root / "templates.json"

    def path(self, template: dict) -> Path:
        return self.root / template["file"]

    # ------------------------------------------------------------ 读写
    def _load(self) -> list:
        if self._json.is_file():
            try:
                data = json.loads(self._json.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save(self, items: list) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._json.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self._json)

    def list(self) -> List[dict]:
        with self._lock:
            return self._load()

    def get(self, template_id: str) -> Optional[dict]:
        for t in self.list():
            if t["id"] == template_id:
                return t
        return None

    def by_key(self, key: str) -> List[dict]:
        return [t for t in self.list() if t["key"] == key]

    def delete(self, template_id: str) -> bool:
        with self._lock:
            items = self._load()
            t = next((x for x in items if x["id"] == template_id), None)
            if t is None:
                return False
            try:
                (self.root / t["file"]).unlink(missing_ok=True)
            except OSError:
                pass
            self._save([x for x in items if x["id"] != template_id])
            return True

    def groups(self) -> List[str]:
        """全部分组名（不含未分组），按名称排序。"""
        return sorted({(t.get("group") or "").strip()
                       for t in self.list()} - {""})

    def by_group(self, group: str) -> List[dict]:
        g = (group or "").strip()
        return [t for t in self.list() if (t.get("group") or "").strip() == g]

    def update(self, template_id: str, patch: dict) -> dict:
        """编辑 key/variant/duration_hint/group/extract_rule；(key, variant) 保持唯一。"""
        allowed = {"key", "variant", "duration_hint", "group", "extract_rule"}
        with self._lock:
            items = self._load()
            t = next((x for x in items if x["id"] == template_id), None)
            if t is None:
                raise FileNotFoundError(f"模板不存在: {template_id}")
            new = {**t, **{k: v for k, v in patch.items() if k in allowed}}
            new["key"] = str(new.get("key") or "").strip()
            new["variant"] = str(new.get("variant") or "").strip()
            new["group"] = str(new.get("group") or "").strip()
            if not new["key"]:
                raise ValueError("动作 key 不能为空")
            dup = next((x for x in items if x["id"] != template_id
                        and x["key"] == new["key"] and x["variant"] == new["variant"]), None)
            if dup is not None:
                raise ValueError(f"已存在同 key+变体 的模板: {dup['filename']}")
            t.update(new)
            self._save(items)
            return t

    def add_video(self, filename: str, data: bytes, group: str = "") -> dict:
        """单文件入库（上传用）：从文件名解析 key/变体/时长，按 (key, variant) 去重。"""
        suffix = Path(filename).suffix.lower()
        if suffix not in VIDEO_EXTS:
            raise ValueError(f"不支持的视频格式: {suffix or '(无扩展名)'}")
        key, variant, duration = parse_template_name(Path(filename).stem)
        if not key:
            raise ValueError("无法从文件名解析动作 key")
        with self._lock:
            items = self._load()
            dup = next((x for x in items
                        if x["key"] == key and x["variant"] == variant), None)
            if dup is not None:
                raise ValueError(
                    f"已存在同 key+变体 的模板（{dup['filename']}），如需替换请先删除")
            tid = f"tp_{uuid.uuid4().hex[:8]}"
            dest = self.root / f"{tid}.mp4"
            self.root.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            rec = {
                "id": tid, "key": key, "variant": variant,
                "duration_hint": duration, "group": (group or "").strip(),
                "filename": filename, "file": dest.name,
                "bytes": len(data), "created_at": time.time(),
                "oss_url": None, "oss_hash": None,
            }
            items.append(rec)
            self._save(items)
            return rec

    def resolve_or_add(self, rec: dict, data: bytes) -> tuple[str, str, bool]:
        """项目包导入用：同 (key, variant) 已存在则复用其 id，否则加入新记录
        （尽量保留原 id，撞车时换新）。返回 (原id, 落库后id, 是否新增)。"""
        with self._lock:
            items = self._load()
            old_id = rec["id"]
            dup = next((x for x in items if x["key"] == rec.get("key")
                        and x["variant"] == rec.get("variant")), None)
            if dup is not None:
                return old_id, dup["id"], False
            tid = old_id
            if any(x["id"] == tid for x in items):
                tid = f"tp_{uuid.uuid4().hex[:8]}"
            dest = self.root / f"{tid}.mp4"
            self.root.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            new = {**rec, "id": tid, "file": dest.name, "bytes": len(data),
                   "created_at": time.time()}
            items.append(new)
            self._save(items)
            return old_id, tid, True

    # ------------------------------------------------------------ 导入
    def scan_import(self, dir_path: Path, group: str = "") -> dict:
        """扫描目录导入模板视频；按 (key, variant) 去重，幂等。"""
        if not dir_path.is_dir():
            raise FileNotFoundError(f"目录不存在: {dir_path}")
        with self._lock:
            items = self._load()
            existing = {(t["key"], t["variant"]) for t in items}
            imported, skipped = [], []
            for f in sorted(dir_path.iterdir()):
                if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                    continue
                key, variant, duration = parse_template_name(f.stem)
                if (key, variant) in existing:
                    skipped.append(f.name)
                    continue
                tid = f"tp_{uuid.uuid4().hex[:8]}"
                dest = self.root / f"{tid}.mp4"
                self.root.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
                rec = {
                    "id": tid, "key": key, "variant": variant,
                    "duration_hint": duration, "group": (group or "").strip(),
                    "filename": f.name, "file": dest.name,
                    "bytes": dest.stat().st_size, "created_at": time.time(),
                    "oss_url": None, "oss_hash": None,
                }
                items.append(rec)
                existing.add((key, variant))
                imported.append(rec)
            self._save(items)
            return {"imported": [t["id"] for t in imported],
                    "imported_count": len(imported),
                    "skipped": skipped}

    # ------------------------------------------------------------ OSS 缓存
    def ensure_oss_url(self, template_id: str) -> str:
        """返回模板的公网 URL；未上传或文件已变时上传一次并缓存。"""
        from app.core.oss_uploader import upload_bytes
        with self._lock:
            items = self._load()
            t = next((x for x in items if x["id"] == template_id), None)
            if t is None:
                raise FileNotFoundError(f"模板不存在: {template_id}")
            p = self.root / t["file"]
            if not p.is_file():
                raise FileNotFoundError(f"模板文件缺失: {t['file']}")
            data = p.read_bytes()
            digest = hashlib.md5(data).hexdigest()
            if t.get("oss_url") and t.get("oss_hash") == digest:
                return t["oss_url"]
            # 对象名用内容哈希：同一文件天然去重，重复导入也指向同一对象
            url = upload_bytes(data, f"seedance/sprite-service/templates/{digest}.mp4",
                               "video/mp4")
            t["oss_url"] = url
            t["oss_hash"] = digest
            self._save(items)
            return url


template_store = TemplateStore()
