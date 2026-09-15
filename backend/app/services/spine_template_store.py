"""Spine 导出模板库：从参考工程反解出来的一套导出约定。

一条模板记录导出时要照抄的东西：骨架版本、帧命名格式与起始帧号、
渲染尺寸与图集画布、每个动画的插槽名/骨骼偏移/缩放/帧率/循环。

存放：data/spine_templates.json（单文件，模板不大且数量少）
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from app.config import get_settings


class SpineTemplateStore:
    def __init__(self, root: Optional[Path] = None):
        self._root = root
        self._lock = threading.RLock()

    @property
    def _json(self) -> Path:
        return (self._root or get_settings().resolved_data_dir) / "spine_templates.json"

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
        self._json.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._json.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self._json)

    def list(self) -> List[dict]:
        with self._lock:
            return self._load()

    def get(self, template_id: str) -> Optional[dict]:
        return next((t for t in self.list() if t["id"] == template_id), None)

    def add(self, template: dict) -> dict:
        """加入一条模板；同名的替换掉（重新导入同一个工程即更新）。"""
        with self._lock:
            items = self._load()
            name = (template.get("name") or "").strip() or "未命名模板"
            old = next((t for t in items if t.get("name") == name), None)
            rec = {**template, "name": name,
                   "id": (old or {}).get("id") or f"sp_{uuid.uuid4().hex[:8]}",
                   "created_at": (old or {}).get("created_at") or time.time(),
                   "updated_at": time.time()}
            items = [t for t in items if t["id"] != rec["id"]]
            items.append(rec)
            self._save(items)
            return rec

    def rename(self, template_id: str, name: str) -> dict:
        with self._lock:
            items = self._load()
            t = next((x for x in items if x["id"] == template_id), None)
            if t is None:
                raise FileNotFoundError(f"模板不存在: {template_id}")
            new = (name or "").strip()
            if not new:
                raise ValueError("模板名不能为空")
            if any(x["id"] != template_id and x.get("name") == new for x in items):
                raise ValueError(f"已存在同名模板: {new}")
            t["name"] = new
            t["updated_at"] = time.time()
            self._save(items)
            return t

    def delete(self, template_id: str) -> bool:
        with self._lock:
            items = self._load()
            if not any(t["id"] == template_id for t in items):
                return False
            self._save([t for t in items if t["id"] != template_id])
            return True


spine_template_store = SpineTemplateStore()
