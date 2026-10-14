"""MCP 调用审计：谁、什么时候、调了什么、参数、成没成、花了多久。

一行一条 JSON，落在 data/logs/agent_audit.jsonl，超过 10MB 轮转一份。
到了能花钱的阶段，这就是对账的依据。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from app.config import get_settings

MAX_BYTES = 10 * 1024 * 1024
_ARG_CHARS = 500
_lock = threading.Lock()


def _path():
    return get_settings().resolved_data_dir / "logs" / "agent_audit.jsonl"


def _clip(args: Any) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= _ARG_CHARS else s[:_ARG_CHARS] + "…"


def record(agent: Optional[dict], tool: str, args: Any, ok: bool,
           ms: float, error: str = "") -> None:
    """写一条审计。写失败只吞掉——审计不能反过来让工具调用失败。"""
    line = {"ts": round(time.time(), 3),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "agent": (agent or {}).get("name", "?"),
            "agent_id": (agent or {}).get("id", "?"),
            "tool": tool, "args": _clip(args), "ok": ok, "ms": round(ms)}
    if error:
        line["error"] = error[:300]
    try:
        p = _path()
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.is_file() and p.stat().st_size > MAX_BYTES:
                p.replace(p.with_suffix(".jsonl.1"))
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def tail(n: int = 50) -> list:
    """最近 n 条（测试与排查用）。"""
    p = _path()
    if not p.is_file():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()[-n:]
    return [json.loads(x) for x in lines if x.strip()]
