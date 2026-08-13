"""工序记录（recipe）：让结果可复现、中间品可重建、问题可追因。

每个产生副作用的操作在成功后追加一条 step 到动作目录的 recipe.json：

    {
      "source": {"kind": "upload", "filename": "walk.mp4", "at": ...},
      "steps": [
        {"op": "extract", "at": ..., "params": {"start_time": 0, "fps": 12}, "result": {...}},
        ...
      ]
    }

写入时机在任务函数内部（持有会话锁），无并发问题；请求线程内的
同步操作（魔棒应用等）与后台任务在同一会话上本就互斥。
记录失败绝不影响业务操作——追溯是锦上添花，不能反过来弄坏主流程。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_STEPS = 500   # 防失控上限；正常一个动作几十步


def _path(session):
    return session.storage.root / "recipe.json"


def _load(session) -> dict:
    p = _path(session)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("steps", [])
                return data
        except (json.JSONDecodeError, OSError):
            logger.warning("recipe.json 损坏，重新开始记录: %s", p)
    return {"source": None, "steps": []}


def _save(session, data: dict) -> None:
    p = _path(session)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def set_source(session, source: dict) -> None:
    """记录素材来源（上传/生成）。换视频会重置工序链——旧链对应旧素材。"""
    try:
        data = _load(session)
        data["source"] = {**source, "at": time.time()}
        data["steps"] = []
        _save(session, data)
    except Exception:
        logger.exception("记录素材来源失败（不影响业务）")


def record_step(session, op: str, params: Optional[dict] = None,
                result: Optional[Any] = None) -> None:
    """追加一条工序（在操作成功后调用）。"""
    try:
        data = _load(session)
        step = {"op": op, "at": time.time(), "params": params or {}}
        if result is not None:
            step["result"] = result
        data["steps"].append(step)
        if len(data["steps"]) > MAX_STEPS:
            data["steps"] = data["steps"][-MAX_STEPS:]
        _save(session, data)
    except Exception:
        logger.exception("记录工序失败（不影响业务）: %s", op)


def read(session) -> dict:
    return _load(session)
