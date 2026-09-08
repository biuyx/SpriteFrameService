"""提示词库：多版本 + 绑定匹配动作 + 解析。

作用域（scope）：
    first_frame   首帧生图（立绘 + 参考首帧 → Seedream）
    video_ref     视频生成·带参考视频（动作由参考视频定义，短提示词）
    video_i2v     视频生成·纯图生视频（绿幕长模板）

记录：
    {id, scope, name, notes,
     versions: [{v, text, at, note}], current,      # 编辑不覆盖：追加版本，可回滚
     bindings: [{level: global|group|key|template, value}],
     created_at, updated_at}

解析优先级（动作级记忆在 API 层先查）：template > key > group > global > 内置兜底。
同一作用域内，同一 (level, value) 绑定是排他的：绑给一条就自动从其它条目摘掉。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import List, Optional, Tuple

from app.config import get_settings

SCOPES = ("first_frame", "video_ref", "video_i2v")
LEVELS = ("global", "group", "key", "template")
SCOPE_LABEL = {"first_frame": "首帧生图", "video_ref": "视频·参考视频",
               "video_i2v": "视频·图生视频"}

# 内置兜底文案（灌库为 v1；库被清空时也保证有值）
BUILTIN = {
    "first_frame": (
        "角色样貌完全跟随第二张参考图。身体姿势完全照搬第一张参考图的动作，"
        "纯色灰色背景，完整全身出镜，禁止修改角色外貌服饰，只迁移动作姿态。"
        "身体姿势完全照搬第一张参考图的动作。完整复刻图一动作姿势。"
    ),
    "video_ref": "图片参考视频进行动作，固定镜头，无运镜，背景不变，角色位置朝向需要和参考视频完全一致。",
}


class PromptStoreError(Exception):
    pass


class PromptStore:
    def __init__(self):
        self._lock = threading.RLock()

    @property
    def _json(self):
        return get_settings().resolved_data_dir / "prompts" / "prompts.json"

    # ------------------------------------------------------------ 读写
    def _load(self) -> list:
        p = self._json
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save(self, items: list) -> None:
        p = self._json
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)

    # ------------------------------------------------------------ 查询
    def list(self, scope: Optional[str] = None) -> List[dict]:
        with self._lock:
            items = self._load()
        if scope:
            items = [x for x in items if x.get("scope") == scope]
        return items

    def get(self, prompt_id: str) -> Optional[dict]:
        return next((x for x in self.list() if x["id"] == prompt_id), None)

    @staticmethod
    def current_text(rec: dict) -> str:
        cur = rec.get("current")
        for v in rec.get("versions", []):
            if v["v"] == cur:
                return v["text"]
        return rec["versions"][-1]["text"] if rec.get("versions") else ""

    # ------------------------------------------------------------ 变更
    def create(self, scope: str, name: str, text: str, notes: str = "",
               bindings: Optional[list] = None, note: str = "初始版本") -> dict:
        if scope not in SCOPES:
            raise PromptStoreError(f"未知作用域: {scope}")
        if not text.strip():
            raise PromptStoreError("提示词内容不能为空")
        with self._lock:
            items = self._load()
            rec = {
                "id": f"pm_{uuid.uuid4().hex[:8]}", "scope": scope,
                "name": (name or "").strip() or "未命名提示词", "notes": notes or "",
                "versions": [{"v": 1, "text": text.strip(), "at": time.time(),
                              "note": note}],
                "current": 1, "bindings": [],
                "created_at": time.time(), "updated_at": time.time(),
            }
            items.append(rec)
            self._apply_bindings(items, rec, bindings or [])
            self._save(items)
            return rec

    def add_version(self, prompt_id: str, text: str, note: str = "") -> dict:
        if not text.strip():
            raise PromptStoreError("提示词内容不能为空")
        with self._lock:
            items = self._load()
            rec = self._find(items, prompt_id)
            v = max((x["v"] for x in rec["versions"]), default=0) + 1
            rec["versions"].append({"v": v, "text": text.strip(),
                                    "at": time.time(), "note": note or ""})
            rec["current"] = v
            rec["updated_at"] = time.time()
            self._save(items)
            return rec

    def set_current(self, prompt_id: str, v: int) -> dict:
        with self._lock:
            items = self._load()
            rec = self._find(items, prompt_id)
            if not any(x["v"] == v for x in rec["versions"]):
                raise PromptStoreError(f"版本 v{v} 不存在")
            rec["current"] = v
            rec["updated_at"] = time.time()
            self._save(items)
            return rec

    def update_meta(self, prompt_id: str, name: Optional[str] = None,
                    notes: Optional[str] = None) -> dict:
        with self._lock:
            items = self._load()
            rec = self._find(items, prompt_id)
            if name is not None:
                rec["name"] = name.strip() or rec["name"]
            if notes is not None:
                rec["notes"] = notes
            rec["updated_at"] = time.time()
            self._save(items)
            return rec

    def set_bindings(self, prompt_id: str, bindings: list) -> dict:
        with self._lock:
            items = self._load()
            rec = self._find(items, prompt_id)
            rec["bindings"] = []
            self._apply_bindings(items, rec, bindings)
            rec["updated_at"] = time.time()
            self._save(items)
            return rec

    def delete(self, prompt_id: str) -> bool:
        with self._lock:
            items = self._load()
            n = len(items)
            items = [x for x in items if x["id"] != prompt_id]
            self._save(items)
            return len(items) < n

    def _find(self, items: list, prompt_id: str) -> dict:
        rec = next((x for x in items if x["id"] == prompt_id), None)
        if rec is None:
            raise PromptStoreError(f"提示词不存在: {prompt_id}")
        return rec

    @staticmethod
    def _apply_bindings(items: list, rec: dict, bindings: list) -> None:
        """写入绑定；同作用域内同一 (level, value) 排他。"""
        clean = []
        for b in bindings:
            level = (b or {}).get("level")
            value = str((b or {}).get("value") or "").strip()
            if level not in LEVELS:
                continue
            if level == "global":
                value = ""
            elif not value:
                continue
            if any(c["level"] == level and c["value"] == value for c in clean):
                continue
            clean.append({"level": level, "value": value})
            for other in items:
                if other["id"] == rec["id"] or other.get("scope") != rec["scope"]:
                    continue
                other["bindings"] = [x for x in other.get("bindings", [])
                                     if not (x["level"] == level and x["value"] == value)]
        rec["bindings"] = clean

    # ------------------------------------------------------------ 解析
    def resolve(self, scope: str, template_id: Optional[str] = None,
                key: Optional[str] = None, group: Optional[str] = None
                ) -> Tuple[Optional[dict], Optional[str]]:
        """按 template > key > group > global 找到匹配条目，返回 (记录, 命中层级)。"""
        items = self.list(scope)

        def by(level, value):
            if not value:
                return None
            return next((x for x in items if any(
                b["level"] == level and b["value"] == value
                for b in x.get("bindings", []))), None)

        for level, value in (("template", template_id), ("key", key),
                             ("group", group), ("global", "*")):
            rec = (by(level, value) if level != "global" else next(
                (x for x in items if any(b["level"] == "global"
                                         for b in x.get("bindings", []))), None))
            if rec is not None:
                return rec, level
        return None, None

    def import_record(self, rec: dict) -> bool:
        """项目包导入：同 id 已存在则跳过；绑定按排他规则并入。"""
        if not rec.get("id") or rec.get("scope") not in SCOPES or not rec.get("versions"):
            return False
        with self._lock:
            items = self._load()
            if any(x["id"] == rec["id"] for x in items):
                return False
            new = {k: rec.get(k) for k in ("id", "scope", "name", "notes", "versions",
                                            "current", "created_at", "updated_at")}
            new["bindings"] = []
            items.append(new)
            self._apply_bindings(items, new, rec.get("bindings") or [])
            self._save(items)
            return True

    # ------------------------------------------------------------ 内置灌库
    def ensure_seeds(self) -> int:
        """库为空的作用域灌入内置文案（幂等）。返回新增条数。"""
        with self._lock:
            items = self._load()
            have = {x.get("scope") for x in items}
            added = 0
            if "first_frame" not in have:
                items.append(self._seed("first_frame", "首帧默认", BUILTIN["first_frame"]))
                added += 1
            if "video_ref" not in have:
                items.append(self._seed("video_ref", "参考视频默认", BUILTIN["video_ref"]))
                added += 1
            if "video_i2v" not in have:
                from app.services.prompt_templates import ACTION_TEMPLATES, GENERIC_TEMPLATE
                items.append(self._seed("video_i2v", "绿幕通用", GENERIC_TEMPLATE,
                                        notes="{action} 会替换为动作名"))
                added += 1
                for k, text in ACTION_TEMPLATES.items():
                    items.append(self._seed("video_i2v", f"绿幕·{k}", text,
                                            bindings=[{"level": "key", "value": k}]))
                    added += 1
            if added:
                self._save(items)
            return added

    @staticmethod
    def _seed(scope, name, text, notes="", bindings=None) -> dict:
        return {
            "id": f"pm_{uuid.uuid4().hex[:8]}", "scope": scope, "name": name,
            "notes": notes,
            "versions": [{"v": 1, "text": text, "at": time.time(), "note": "内置"}],
            "current": 1,
            "bindings": bindings if bindings is not None else [{"level": "global", "value": ""}],
            "created_at": time.time(), "updated_at": time.time(),
        }


prompt_store = PromptStore()


def builtin_text(scope: str, action_name: str = "") -> str:
    """无库条目时的代码内置兜底。"""
    if scope == "video_i2v":
        from app.services.prompt_templates import (ACTION_TEMPLATES, ALIASES,
                                                    GENERIC_TEMPLATE)
        name = (action_name or "").strip()
        lower = name.lower()
        k = lower if lower in ACTION_TEMPLATES else ALIASES.get(name) or ALIASES.get(lower)
        if k and k in ACTION_TEMPLATES:
            return ACTION_TEMPLATES[k]
        for kk in ACTION_TEMPLATES:
            if kk in lower:
                return ACTION_TEMPLATES[kk]
        return GENERIC_TEMPLATE.replace("{action}", name)
    return BUILTIN.get(scope, "")


def resolve_for_action(scope: str, action: dict) -> dict:
    """动作级完整解析：记忆 > 库绑定 > 内置。返回 {text, prompt_id, version, name, source}。"""
    from app.services.template_store import template_store

    prefs = (action.get("gen_prefs") or {}).get(
        "first_frame" if scope == "first_frame" else "video") or {}
    # 记忆的作用域要一致（视频记忆区分 参考视频/图生视频）
    if prefs.get("prompt_text") and prefs.get("scope", scope) == scope:
        return {"text": prefs["prompt_text"], "prompt_id": prefs.get("prompt_id"),
                "version": prefs.get("prompt_version"),
                "name": prefs.get("prompt_name") or "动作记忆", "source": "action"}

    tpl = template_store.get(action.get("template_id") or "")
    key = (tpl or {}).get("key") or (action.get("name") or "").strip()
    group = ((tpl or {}).get("group") or "").strip() or None
    rec, level = prompt_store.resolve(scope, template_id=(tpl or {}).get("id"),
                                      key=key, group=group)
    if rec is not None:
        text = PromptStore.current_text(rec)
        if scope == "video_i2v":
            text = text.replace("{action}", action.get("name", ""))
        return {"text": text, "prompt_id": rec["id"], "version": rec["current"],
                "name": rec["name"], "source": level}
    return {"text": builtin_text(scope, action.get("name", "")), "prompt_id": None,
            "version": None, "name": "内置默认", "source": "builtin"}
