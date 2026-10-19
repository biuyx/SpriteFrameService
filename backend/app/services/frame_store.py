"""帧图像落盘/读取：原始帧与处理后帧分别存 PNG（以帧 id 命名），元数据写 frames.json。"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4
from typing import List, Optional

import numpy as np

from app.core.frame_manager import FrameManager
from app.models.frame_data import FrameData, FrameStatus
from app.services.storage import SessionStorage
from app.utils.image_utils import read_image, save_image


def resolve_frame_file(stored: Optional[str], canonical: Path) -> Optional[Path]:
    """帧文件实际在哪。

    帧一向按 id 存在会话目录里（frames/raw|proc/<id>.png），frames.json 里那个绝对路径
    只是顺手记下的冗余。整个 data/ 换了机器或换了系统（D:\\... → /data/...）之后它就失效了，
    以前会让所有帧都显示成「未抠图」、流水线把抠图重跑一遍。所以以按 id 推出来的位置为准，
    旧路径只在那里没有时兜底。

    元数据里本来就没有路径时保持没有：历史回滚正是靠把 processed_path 置空来标记
    「未处理」的（磁盘上的旧文件稍后才删），不能从盘上把它捞回来。
    """
    if not stored:
        return None
    if canonical.exists():
        return canonical
    p = Path(stored)
    return p if p.exists() else canonical


def _frame_to_dict(frame: FrameData) -> dict:
    return {
        "id": frame.id,
        "index": frame.index,
        "timestamp": frame.timestamp,
        "status": frame.status.value,
        "is_selected": frame.is_selected,
        "image_path": str(frame.image_path) if frame.image_path else None,
        "processed_path": str(frame.processed_path) if frame.processed_path else None,
        "pose_id": frame.pose_id,
        "contour_id": frame.contour_id,
        "image_feature_id": frame.image_feature_id,
        "regional_feature_id": frame.regional_feature_id,
        "tag": frame.tag,
    }


class FrameStore:
    """帧存储：内存 FrameManager 的磁盘后端（文件以帧 id 命名）。"""

    def __init__(self, storage: SessionStorage):
        self._storage = storage

    # --- 保存 ---
    def save_raw_frames(self, frames: List[FrameData]) -> None:
        """将内存中的原始帧数组落盘，并清空数组只留路径。"""
        for frame in frames:
            if frame.image is not None:
                path = self._storage.raw_path(frame.id)
                save_image(path, frame.image)
                frame.image_path = path
                frame.image = None  # 释放内存

    def save_processed(self, frame_id: str, image: np.ndarray) -> Path:
        """保存处理后帧并返回路径。"""
        path = self._storage.proc_path(frame_id)
        save_image(path, image)
        return path

    # --- 读取 ---
    def load_raw(self, frame_id: str) -> Optional[np.ndarray]:
        path = self._storage.raw_path(frame_id)
        return read_image(path) if path.exists() else None

    def load_processed(self, frame_id: str) -> Optional[np.ndarray]:
        path = self._storage.proc_path(frame_id)
        return read_image(path) if path.exists() else None

    def raw_path(self, frame_id: str) -> Path:
        return self._storage.raw_path(frame_id)

    def proc_path(self, frame_id: str) -> Path:
        return self._storage.proc_path(frame_id)

    def remove_frame_files(self, frame_id: str) -> None:
        """删除帧的原始/处理文件。"""
        self._storage.remove_frame_files(frame_id)

    # --- 元数据 ---
    def save_metadata(self, frame_manager: FrameManager) -> None:
        """将当前帧元数据写入 frames.json。"""
        payload = [_frame_to_dict(f) for f in frame_manager.frames]
        tmp = self._storage.frames_json.with_name(f'.frames-{uuid4().hex}.tmp')
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8"
        )
        tmp.replace(self._storage.frames_json)

    def load_metadata(self, frame_manager: FrameManager) -> None:
        """从 frames.json 恢复帧元数据（仅当内存为空时）。"""
        if frame_manager.frame_count > 0 or not self._storage.frames_json.exists():
            return
        try:
            payload = json.loads(self._storage.frames_json.read_text(encoding="utf-8"))
        except Exception:
            return

        for d in payload:
            fid = d.get("id")
            if fid:
                image_path = resolve_frame_file(d.get("image_path"),
                                                self._storage.raw_path(fid))
                processed_path = resolve_frame_file(d.get("processed_path"),
                                                    self._storage.proc_path(fid))
            else:                     # 极老的数据没有 id，只能信存下来的路径
                image_path = Path(d["image_path"]) if d.get("image_path") else None
                processed_path = (Path(d["processed_path"])
                                  if d.get("processed_path") else None)
            kwargs = {
                "index": d["index"],
                "timestamp": d["timestamp"],
                "status": FrameStatus(d.get("status", "raw")),
                "is_selected": d.get("is_selected", False),
                "image_path": image_path,
                "processed_path": processed_path,
                "pose_id": d.get("pose_id"),
                "contour_id": d.get("contour_id"),
                "image_feature_id": d.get("image_feature_id"),
                "regional_feature_id": d.get("regional_feature_id"),
            }
            if d.get("id"):
                kwargs["id"] = d["id"]
            if d.get("tag"):
                kwargs["tag"] = d["tag"]
            frame_manager.add_frame(FrameData(**kwargs))
