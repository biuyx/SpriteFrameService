"""AI 专用密钥：给 Claude Code 等 MCP 客户端用，和平台访问口令分开。

为什么不直接把访问口令发给 AI：
  · 吊销一个 AI 不该连累所有人重新登录
  · 审计日志要分得清是哪个 AI 干的
  · AI 的权限要能收窄（只读 / 可加工 / 可花钱），口令是全权的

密钥只在创建时出现一次，落盘的是 sha256 摘要；只能用于 /api/mcp，
拿它调平台的其他接口一律 401（AI 只能走 MCP 这条受控的路）。

权限范围：
    read      查询与看图（MCP 一期的全部工具）
    operate   免费加工：抽帧、抠图、导出……（预留）
    generate  花钱的生成：首帧、视频、流水线（预留）
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from app.config import get_settings

SCOPES = ("read", "operate", "generate")
KEY_PREFIX = "sfk_"
_TOUCH_INTERVAL = 60          # 「最近使用」最多一分钟落一次盘


class AgentKeyError(Exception):
    pass


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class AgentKeyStore:
    def __init__(self, path: Optional[Path] = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or get_settings().resolved_data_dir / "agent_keys.json"

    def _load(self) -> List[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("keys", [])
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise AgentKeyError(f"密钥文件读不出来: {e}")

    def _save(self, keys: List[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"keys": keys}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def public(rec: dict) -> dict:
        """对外展示：不含摘要。"""
        return {k: v for k, v in rec.items() if k != "hash"}

    def create(self, name: str, scopes: List[str], note: str = "") -> Tuple[dict, str]:
        """返回 (记录, 明文密钥)。明文只此一次，丢了只能吊销重发。"""
        name = (name or "").strip()
        if not name:
            raise AgentKeyError("密钥要有名字（审计日志里靠它认人）")
        bad = [s for s in scopes if s not in SCOPES]
        if bad or not scopes:
            raise AgentKeyError(f"权限范围只能是 {', '.join(SCOPES)}，收到: {scopes}")
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        rec = {"id": uuid.uuid4().hex[:8], "name": name,
               "scopes": [s for s in SCOPES if s in scopes], "note": note.strip(),
               "hash": _digest(key), "created_at": time.time(),
               "last_used_at": None, "revoked_at": None}
        with self._lock:
            keys = self._load()
            if any(k["name"] == name and not k.get("revoked_at") for k in keys):
                raise AgentKeyError(f"已有同名的有效密钥：{name}（先吊销，或换个名字）")
            keys.append(rec)
            self._save(keys)
        return self.public(rec), key

    def list(self) -> List[dict]:
        with self._lock:
            return [self.public(k) for k in self._load()]

    def revoke(self, key_id: str) -> dict:
        with self._lock:
            keys = self._load()
            rec = next((k for k in keys if k["id"] == key_id), None)
            if rec is None:
                raise AgentKeyError(f"没有这把密钥: {key_id}")
            if not rec.get("revoked_at"):
                rec["revoked_at"] = time.time()
                self._save(keys)
            return self.public(rec)

    def verify(self, key: str) -> Optional[dict]:
        """有效则返回记录（不含摘要），否则 None。逐条常量时间比较。"""
        if not key or not key.startswith(KEY_PREFIX):
            return None
        want = _digest(key)
        try:
            keys = self._load()
        except AgentKeyError:
            return None
        hit = None
        for k in keys:
            if hmac.compare_digest(k.get("hash", ""), want) and not k.get("revoked_at"):
                hit = k
        if hit is None:
            return None
        now = time.time()
        if not hit.get("last_used_at") or now - hit["last_used_at"] > _TOUCH_INTERVAL:
            self._touch(hit["id"], now)
        return self.public(hit)

    def _touch(self, key_id: str, now: float) -> None:
        try:
            with self._lock:
                keys = self._load()
                for k in keys:
                    if k["id"] == key_id:
                        k["last_used_at"] = now
                self._save(keys)
        except (OSError, AgentKeyError):
            pass                      # 记「最近使用」失败不影响鉴权


agent_key_store = AgentKeyStore()
