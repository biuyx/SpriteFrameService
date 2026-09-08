"""参考首帧集：某个完成角色的全套动作首帧，供新角色首帧生图作姿势参考。

布局：
    data/first_frame_sets/{set_id}/
        set.json          {id, name, group, created_at, frames:[{key,file}]}
        01_idle_front.png …（文件名 = 动作 key）

来源：目录导入（deliverable/{role}/ 结构）或从库内精灵归档
（动作 key 取绑定模板的 key，同 key 多动作共用一张，首个为准）。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from app.config import get_settings

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


class FfSetError(Exception):
    pass


class FfSetStore:
    def __init__(self, root: Optional[Path] = None):
        self._root = root
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root or (get_settings().resolved_data_dir / "first_frame_sets")

    def _set_dir(self, set_id: str) -> Path:
        return self.root / set_id

    def _read(self, set_id: str) -> Optional[dict]:
        p = self._set_dir(set_id) / "set.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, rec: dict) -> None:
        d = self._set_dir(rec["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "set.json.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(d / "set.json")

    # ------------------------------------------------------------ 查询
    def list(self) -> List[dict]:
        with self._lock:
            result = []
            if self.root.is_dir():
                for d in self.root.iterdir():
                    rec = self._read(d.name)
                    if rec:
                        result.append(rec)
            result.sort(key=lambda r: r.get("created_at", 0), reverse=True)
            return result

    def get(self, set_id: str) -> Optional[dict]:
        with self._lock:
            return self._read(set_id)

    def frame_path(self, set_id: str, key: str) -> Optional[Path]:
        rec = self.get(set_id)
        if not rec:
            return None
        f = next((x for x in rec.get("frames", []) if x["key"] == key), None)
        if not f:
            return None
        p = self._set_dir(set_id) / f["file"]
        return p if p.is_file() else None

    def delete(self, set_id: str) -> bool:
        with self._lock:
            d = self._set_dir(set_id)
            if not d.is_dir():
                return False
            shutil.rmtree(d, ignore_errors=True)
            return True

    # ------------------------------------------------------------ 入库
    def import_dir(self, dir_path: Path, name: str, group: str = "") -> dict:
        """目录导入：图片文件名主干即动作 key。"""
        if not dir_path.is_dir():
            raise FfSetError(f"目录不存在: {dir_path}")
        files = [f for f in sorted(dir_path.iterdir())
                 if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
        if not files:
            raise FfSetError("目录下没有图片文件")
        with self._lock:
            sid = f"fs_{uuid.uuid4().hex[:8]}"
            d = self._set_dir(sid)
            d.mkdir(parents=True, exist_ok=True)
            frames, seen = [], set()
            for f in files:
                key = f.stem.strip()
                if not key or key in seen:
                    continue
                dest = d / f"{key}.png"
                shutil.copyfile(f, dest)
                frames.append({"key": key, "file": dest.name})
                seen.add(key)
            rec = {"id": sid, "name": (name or dir_path.name).strip() or dir_path.name,
                   "group": (group or "").strip(),
                   "created_at": time.time(), "frames": frames}
            self._write(rec)
            return rec

    def archive_sprite(self, sprite_id: str, name: Optional[str] = None,
                       group: Optional[str] = None) -> dict:
        """把库内精灵的全部动作首帧归档为参考集（key 取绑定模板的 key）。"""
        from app.services.sprite_store import sprite_store
        from app.services.template_store import template_store

        sp = sprite_store.get_sprite(sprite_id)
        tpl_groups = set()
        entries = []           # (key, src_path)
        seen = set()
        for ref in sp.get("actions", []):
            try:
                a = sprite_store.get_action(sprite_id, ref["id"])
            except Exception:
                continue
            src = sprite_store.action_dir(sprite_id, ref["id"]) / "first_frame.png"
            if not src.is_file():
                continue
            tpl = template_store.get(a.get("template_id") or "")
            key = (tpl or {}).get("key") or a.get("name", "").strip()
            if tpl and (tpl.get("group") or "").strip():
                tpl_groups.add(tpl["group"].strip())
            if not key or key in seen:      # 同 key 多变体共用一张首帧
                continue
            entries.append((key, src))
            seen.add(key)
        if not entries:
            raise FfSetError("该精灵没有任何已就位的动作首帧")

        with self._lock:
            sid = f"fs_{uuid.uuid4().hex[:8]}"
            d = self._set_dir(sid)
            d.mkdir(parents=True, exist_ok=True)
            frames = []
            for key, src in entries:
                dest = d / f"{key}.png"
                shutil.copyfile(src, dest)
                frames.append({"key": key, "file": dest.name})
            rec = {"id": sid,
                   "name": (name or sp.get("name", "")).strip() or sprite_id,
                   "group": (group if group is not None
                             else (tpl_groups.pop() if len(tpl_groups) == 1 else "")),
                   "created_at": time.time(),
                   "source_sprite": sprite_id,
                   "frames": frames}
            self._write(rec)
            return rec


ffset_store = FfSetStore()
