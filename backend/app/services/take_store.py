"""视频版本（take）存储：一个动作可保存多次上传/生成的视频，选其一使用。

布局（动作的 video/ 目录）：
    takes.json      {"current": "t_ab12cd34", "takes": [ {...}, ... ]}
    t_ab12cd34.mp4  视频本体

take 字段：
    id / source(upload|generate) / status(pending|running|succeeded|failed)
    created_at / filename / model / prompt / params / remote_task_id
    seed / usage / fps / error

生成的 take 是花过钱的源料：状态与 remote_task_id 第一时间落盘，
进程重启后可通过 reconcile 重挂远端任务取回结果。

兼容：旧动作只有 upload.mp4 时，首次访问自动登记为一个 upload take。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import List, Optional


class TakeStore:
    def __init__(self, storage):
        self._dir: Path = storage.video_dir
        self._json: Path = self._dir / "takes.json"

    # ------------------------------------------------------------ 基础
    def path(self, take_id: str) -> Path:
        return self._dir / f"{take_id}.mp4"

    def _load(self) -> dict:
        if self._json.is_file():
            try:
                data = json.loads(self._json.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("current", None)
                    data.setdefault("takes", [])
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {"current": None, "takes": []}

    def _save(self, data: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._json.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._json)

    def _migrate_legacy(self, data: dict) -> dict:
        """旧动作的 upload.* 视频懒迁移为一个 take。

        不改文件名——旧视频可能正被 VideoProcessor 打开（Windows 下 rename
        会被占用拒绝），take.file 本就支持任意文件名，登记原名即可。
        """
        if data["takes"]:
            return data
        # 注意：不能拿「takes.json 是否存在」当门槛——异常路径可能留下空壳
        # json，那会永久卡死迁移。只要还没有任何 take 且旧文件在，就登记。
        exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".ts", ".wmv"}
        for f in sorted(self._dir.glob("upload.*")):
            if f.suffix.lower() not in exts or ".part" in f.name:
                continue
            tid = f"t_{uuid.uuid4().hex[:8]}"
            try:
                created = f.stat().st_mtime
            except OSError:
                created = time.time()
            data["takes"].append({
                "id": tid, "source": "upload", "status": "succeeded",
                "created_at": created, "filename": f.name, "file": f.name,
            })
            data["current"] = tid
            self._save(data)
            break
        return data

    # ------------------------------------------------------------ 查询
    def load(self) -> dict:
        return self._migrate_legacy(self._load())

    def list(self) -> dict:
        data = self.load()
        return data

    def get(self, take_id: str) -> Optional[dict]:
        for t in self.load()["takes"]:
            if t["id"] == take_id:
                return t
        return None

    def video_file(self, take_id: str) -> Optional[Path]:
        t = self.get(take_id)
        if t is None:
            return None
        p = self._dir / t.get("file", f"{take_id}.mp4")
        return p if p.is_file() else None

    def current_id(self) -> Optional[str]:
        return self.load()["current"]

    def pending_remote(self) -> List[dict]:
        """未完结且有远端任务 ID 的 take（reconcile 用）。"""
        return [t for t in self.load()["takes"]
                if t.get("status") in ("pending", "running") and t.get("remote_task_id")]

    # ------------------------------------------------------------ 修改
    def add(self, source: str, **fields) -> dict:
        tid = f"t_{uuid.uuid4().hex[:8]}"
        take = {"id": tid, "source": source, "status": fields.pop("status", "pending"),
                "created_at": time.time(), "file": f"{tid}.mp4", **fields}
        data = self.load()
        data["takes"].append(take)
        self._save(data)
        return take

    def update(self, take_id: str, patch: dict) -> Optional[dict]:
        data = self.load()
        for t in data["takes"]:
            if t["id"] == take_id:
                t.update(patch)
                self._save(data)
                return t
        return None

    def set_current(self, take_id: str) -> bool:
        data = self.load()
        if not any(t["id"] == take_id and t.get("status") == "succeeded"
                   for t in data["takes"]):
            return False
        data["current"] = take_id
        self._save(data)
        return True

    def delete(self, take_id: str) -> bool:
        data = self.load()
        take = next((t for t in data["takes"] if t["id"] == take_id), None)
        if take is None:
            return False
        p = self._dir / take.get("file", f"{take_id}.mp4")
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        data["takes"] = [t for t in data["takes"] if t["id"] != take_id]
        if data["current"] == take_id:
            # 退回最近一个成功的 take（可能为 None）
            succ = [t for t in data["takes"] if t.get("status") == "succeeded"]
            data["current"] = succ[-1]["id"] if succ else None
        self._save(data)
        return True
