"""精灵/动作实体层。

领域模型（docs/sprite-domain-design.md）：
    精灵 Sprite（角色）→ 动作 Action[]（walk/idle/…）→ 各自的工作数据。

磁盘布局：
    data/sprites/{sprite_id}/
        sprite.json          档案 + 工艺预设 + 动作索引
        reference/           立绘、参考图、九宫格原图
        actions/{action_id}/ 动作工作目录（即原「会话」目录布局）

action_id 全局唯一（uuid hex12），SessionManager 用它直接定位工作目录，
现有 /api/sessions/{id}/... 端点树无需改动。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from app.config import get_settings

# 常用动作名预设（跨精灵统一命名用，前端展示）
COMMON_ACTION_NAMES = [
    "idle", "walk", "run", "attack", "hit", "die",
    "jump", "skill", "victory", "sleep",
]

_NAME_RE = re.compile(r"[^\w一-鿿\- ]")


def _clean_name(name: str, fallback: str) -> str:
    """清洗名称：去掉路径分隔等危险字符，保留中英文/数字/连字符。"""
    name = _NAME_RE.sub("", (name or "").strip())[:40]
    return name or fallback


def _now() -> float:
    return time.time()


class SpriteStoreError(Exception):
    """实体层错误（API 层转 4xx）。"""


class SpriteStore:
    """精灵注册表：sprite.json / action.json 的读写与 action → sprite 索引。

    并发：整表一把锁。实体操作都是小 JSON 读写，锁粒度足够；
    动作内的重活（抽帧/抠图）走各自的会话锁，不经过这里。
    """

    def __init__(self, root: Optional[Path] = None):
        self._root = root
        self._lock = threading.RLock()
        # action_id -> sprite_id 索引（启动时扫描，之后随写操作维护）
        self._action_index: Dict[str, str] = {}
        self._scanned = False

    # ------------------------------------------------------------ 路径
    @property
    def root(self) -> Path:
        return self._root or (get_settings().resolved_data_dir / "sprites")

    def sprite_dir(self, sprite_id: str) -> Path:
        return self.root / sprite_id

    def sprite_json(self, sprite_id: str) -> Path:
        return self.sprite_dir(sprite_id) / "sprite.json"

    def reference_dir(self, sprite_id: str) -> Path:
        return self.sprite_dir(sprite_id) / "reference"

    def action_dir(self, sprite_id: str, action_id: str) -> Path:
        return self.sprite_dir(sprite_id) / "actions" / action_id

    def action_json(self, sprite_id: str, action_id: str) -> Path:
        return self.action_dir(sprite_id, action_id) / "action.json"

    # ------------------------------------------------------------ 索引
    def _ensure_index(self) -> None:
        """扫描磁盘建立 action → sprite 索引（进程内只做一次）。"""
        if self._scanned:
            return
        self._action_index.clear()
        if self.root.is_dir():
            for sp_dir in self.root.iterdir():
                actions = sp_dir / "actions"
                if not (sp_dir / "sprite.json").is_file() or not actions.is_dir():
                    continue
                for act_dir in actions.iterdir():
                    if (act_dir / "action.json").is_file():
                        self._action_index[act_dir.name] = sp_dir.name
        self._scanned = True

    def locate_action(self, action_id: str) -> Optional[Path]:
        """按 action_id 定位工作目录（SessionManager 寻址用）。"""
        with self._lock:
            self._ensure_index()
            sid = self._action_index.get(action_id)
        if sid is None:
            return None
        d = self.action_dir(sid, action_id)
        return d if d.is_dir() else None

    def sprite_of_action(self, action_id: str) -> Optional[str]:
        with self._lock:
            self._ensure_index()
            return self._action_index.get(action_id)

    # ------------------------------------------------------------ 读写
    def _read_json(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SpriteStoreError(f"不存在: {path.parent.name}")
        except json.JSONDecodeError as e:
            raise SpriteStoreError(f"数据损坏: {path.name}: {e}")

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------ 精灵
    def list_sprites(self) -> List[dict]:
        with self._lock:
            self._ensure_index()
            result = []
            if self.root.is_dir():
                for sp_dir in sorted(self.root.iterdir()):
                    p = sp_dir / "sprite.json"
                    if p.is_file():
                        try:
                            result.append(self._read_json(p))
                        except SpriteStoreError:
                            continue
            return result

    def get_sprite(self, sprite_id: str) -> dict:
        with self._lock:
            return self._read_json(self.sprite_json(sprite_id))

    def create_sprite(self, name: str, tags: Optional[List[str]] = None) -> dict:
        sprite_id = f"sp_{uuid.uuid4().hex[:10]}"
        payload = {
            "id": sprite_id,
            "name": _clean_name(name, "未命名精灵"),
            "tags": [_clean_name(t, "") for t in (tags or []) if t.strip()],
            "created_at": _now(),
            # 工艺预设：全精灵统一的默认参数（动作可覆盖）
            "preset": {
                "matting": {"model": "isnet-anime", "alpha_threshold": 128,
                            "erode": 1, "feather": 0},
                "output": {},
            },
            "actions": [],
        }
        with self._lock:
            self._ensure_index()
            self.reference_dir(sprite_id).mkdir(parents=True, exist_ok=True)
            (self.sprite_dir(sprite_id) / "actions").mkdir(parents=True, exist_ok=True)
            self._write_json(self.sprite_json(sprite_id), payload)
        return payload

    def update_sprite(self, sprite_id: str, patch: dict) -> dict:
        """浅合并更新（仅允许 name/tags/preset）。"""
        with self._lock:
            data = self._read_json(self.sprite_json(sprite_id))
            if "name" in patch:
                data["name"] = _clean_name(str(patch["name"]), data["name"])
            if "tags" in patch and isinstance(patch["tags"], list):
                data["tags"] = [_clean_name(str(t), "") for t in patch["tags"] if str(t).strip()]
            if "preset" in patch and isinstance(patch["preset"], dict):
                data.setdefault("preset", {}).update(patch["preset"])
            self._write_json(self.sprite_json(sprite_id), data)
            return data

    def delete_sprite(self, sprite_id: str) -> bool:
        """删除精灵及其全部动作数据（不可恢复；调用方负责确认与释放句柄）。"""
        import shutil
        with self._lock:
            d = self.sprite_dir(sprite_id)
            if not d.is_dir():
                return False
            self._ensure_index()
            for aid, sid in list(self._action_index.items()):
                if sid == sprite_id:
                    del self._action_index[aid]
            shutil.rmtree(d, ignore_errors=True)
            return True

    # ------------------------------------------------------------ 动作
    def list_actions(self, sprite_id: str) -> List[dict]:
        with self._lock:
            data = self._read_json(self.sprite_json(sprite_id))
        result = []
        for ref in data.get("actions", []):
            try:
                a = self._read_json(self.action_json(sprite_id, ref["id"]))
            except SpriteStoreError:
                continue
            a["summary"] = self._action_summary(sprite_id, ref["id"])
            result.append(a)
        return result

    def get_action(self, sprite_id: str, action_id: str) -> dict:
        with self._lock:
            return self._read_json(self.action_json(sprite_id, action_id))

    def create_action(self, sprite_id: str, name: str,
                      first_frame: Optional[dict] = None) -> dict:
        action_id = uuid.uuid4().hex[:12]   # 与旧会话 ID 同形，兼容现有端点
        payload = {
            "id": action_id,
            "sprite_id": sprite_id,
            "name": _clean_name(name, "未命名动作"),
            "status": "new",                 # new → active → final
            "created_at": _now(),
            "first_frame": first_frame,      # {"kind": "upload"|"action_frame"|"grid_cell", ...}
            "preset_override": {},           # 偏离精灵预设的部分（显式标出）
        }
        with self._lock:
            data = self._read_json(self.sprite_json(sprite_id))
            self.action_dir(sprite_id, action_id).mkdir(parents=True, exist_ok=True)
            self._write_json(self.action_json(sprite_id, action_id), payload)
            data["actions"].append({"id": action_id, "name": payload["name"]})
            self._write_json(self.sprite_json(sprite_id), data)
            self._ensure_index()
            self._action_index[action_id] = sprite_id
        return payload

    def register_claimed_action(self, sprite_id: str, action_id: str,
                                name: str) -> dict:
        """登记一个已就位的目录为动作（旧会话认领：目录已被移动到位）。"""
        payload = {
            "id": action_id,
            "sprite_id": sprite_id,
            "name": _clean_name(name, "认领动作"),
            "status": "active",
            "created_at": _now(),
            "first_frame": {"kind": "legacy_session"},
            "preset_override": {},
        }
        with self._lock:
            data = self._read_json(self.sprite_json(sprite_id))
            self._write_json(self.action_json(sprite_id, action_id), payload)
            data["actions"].append({"id": action_id, "name": payload["name"]})
            self._write_json(self.sprite_json(sprite_id), data)
            self._ensure_index()
            self._action_index[action_id] = sprite_id
        return payload

    def update_action(self, sprite_id: str, action_id: str, patch: dict) -> dict:
        allowed = {"name", "status", "first_frame", "preset_override"}
        with self._lock:
            data = self._read_json(self.action_json(sprite_id, action_id))
            for k in allowed & set(patch.keys()):
                if k == "name":
                    data["name"] = _clean_name(str(patch["name"]), data["name"])
                elif k == "status" and patch[k] in ("new", "active", "final"):
                    data["status"] = patch[k]
                else:
                    data[k] = patch[k]
            self._write_json(self.action_json(sprite_id, action_id), data)
            # 名称变更同步到精灵索引
            if "name" in patch:
                sp = self._read_json(self.sprite_json(sprite_id))
                for ref in sp.get("actions", []):
                    if ref["id"] == action_id:
                        ref["name"] = data["name"]
                self._write_json(self.sprite_json(sprite_id), sp)
            return data

    def delete_action(self, sprite_id: str, action_id: str) -> bool:
        import shutil
        with self._lock:
            d = self.action_dir(sprite_id, action_id)
            if not d.is_dir():
                return False
            shutil.rmtree(d, ignore_errors=True)
            try:
                sp = self._read_json(self.sprite_json(sprite_id))
                sp["actions"] = [r for r in sp.get("actions", []) if r["id"] != action_id]
                self._write_json(self.sprite_json(sprite_id), sp)
            except SpriteStoreError:
                pass
            self._action_index.pop(action_id, None)
            return True

    # ------------------------------------------------------------ 看板摘要
    def _action_summary(self, sprite_id: str, action_id: str) -> dict:
        """轻量统计（不加载工作态）：看板卡片用。"""
        d = self.action_dir(sprite_id, action_id)

        def count(sub: str, pattern: str = "*") -> int:
            p = d / sub
            return sum(1 for _ in p.glob(pattern)) if p.is_dir() else 0

        video_dir = d / "video"
        has_video = video_dir.is_dir() and any(
            f.is_file() and ".part" not in f.name for f in video_dir.iterdir()
        )
        return {
            "has_video": has_video,
            "frame_count": count("frames/raw", "*.png"),
            "processed_count": count("frames/proc", "*.png"),
            "export_count": count("exports"),
        }

    def frame_image_path(self, sprite_id: str, action_id: str,
                         frame_index: int) -> Optional[Path]:
        """取某动作第 N 帧的图像文件（处理图优先），不加载工作态。"""
        d = self.action_dir(sprite_id, action_id)
        fj = d / "frames.json"
        if not fj.is_file():
            return None
        try:
            frames = json.loads(fj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        for f in frames:
            if f.get("index") == frame_index:
                for key in ("processed_path", "image_path"):
                    p = f.get(key)
                    if p and Path(p).is_file():
                        return Path(p)
        return None

    def materialize_first_frame(self, sprite_id: str, action_id: str,
                                first_frame: Optional[dict]) -> Optional[dict]:
        """把首帧来源物化为动作目录下的 first_frame.png。

        血统边只是引用；物理拷贝保证源动作被删后新动作首帧仍在
        （它是视频生成的输入，属于源料）。返回补充了 file 字段的 first_frame。
        """
        import shutil
        if not first_frame or first_frame.get("kind") != "action_frame":
            return first_frame
        src_action = str(first_frame.get("action", ""))
        try:
            idx = int(first_frame.get("frame_index"))
        except (TypeError, ValueError):
            return first_frame
        src_sprite = self.sprite_of_action(src_action)
        if src_sprite is None:
            return first_frame
        src = self.frame_image_path(src_sprite, src_action, idx)
        if src is None:
            return first_frame
        dest = self.action_dir(sprite_id, action_id) / "first_frame.png"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        except OSError:
            return first_frame
        return {**first_frame, "file": "first_frame.png"}

    def cover_path(self, sprite_id: str, action_id: str) -> Optional[Path]:
        """看板封面：第一帧的处理图，其次原图（按 frames.json 的顺序）。"""
        d = self.action_dir(sprite_id, action_id)
        fj = d / "frames.json"
        if fj.is_file():
            try:
                frames = json.loads(fj.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                frames = []
            for f in frames[:1]:
                for key in ("processed_path", "image_path"):
                    p = f.get(key)
                    if p and Path(p).is_file():
                        return Path(p)
        # 无帧时退回 take 封面（视频生成落地后会有）
        return None


sprite_store = SpriteStore()
