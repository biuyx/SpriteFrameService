"""历史记录管理模块（移植自 SpriteFrameStudio，服务化后快照落盘）。

快照以 PNG 文件存于会话的 history/ 目录，避免整帧未压缩数组常驻内存：
一次 200 帧 1024x1024 RGBA 的批量操作，原实现单步就占用约 800MB，
10 步叠加可达数 GB。落盘后内存仅保留路径引用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional

import shutil
import time

from app.core.frame_manager import FrameManager
from app.models.frame_data import FrameData
from app.utils.image_utils import read_image, save_image


@dataclass
class HistoryEntry:
    """单条历史记录（快照存于磁盘，此处仅持有路径）。"""
    step_id: int                              # 步骤编号（全局自增）
    operation_name: str                       # 操作名称
    description: str                          # 详细描述
    timestamp: float                          # 操作时间戳
    affected_indices: List[int]               # 受影响的帧索引列表
    snapshot_paths: Dict[int, Path] = field(default_factory=dict)   # {帧索引: 快照文件}
    snapshot_states: Dict[int, bool] = field(default_factory=dict)  # {帧索引: 快照时是否为 processed}
    bytes_used: int = 0                       # 本条快照占用磁盘（字节）


class HistoryManager:
    """历史记录管理器（磁盘快照）。"""
    MAX_STEPS = 10                            # 最大历史步数

    def __init__(self, snapshot_dir: Optional[Path] = None):
        self._entries: List[HistoryEntry] = []
        self._step_counter: int = 1
        self._total_bytes: int = 0
        self._snapshot_dir = Path(snapshot_dir) if snapshot_dir else None

    # --- 快照落盘 ---
    def _step_dir(self, step_id: int) -> Optional[Path]:
        if self._snapshot_dir is None:
            return None
        d = self._snapshot_dir / f"step_{step_id:04d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _disk_source(frame: FrameData) -> Optional[Path]:
        """取帧当前显示图像对应的磁盘文件（内存中有数组时返回 None，需重新编码）。"""
        if frame.processed_image is not None or frame.image is not None:
            return None
        if frame.has_processed and frame.processed_path and frame.processed_path.exists():
            return frame.processed_path
        if frame.image_path and frame.image_path.exists():
            return frame.image_path
        return None

    def _write_snapshot(self, step_dir: Path, idx: int, frame: FrameData) -> Optional[Path]:
        """写入单帧快照，返回文件路径。优先复制已有 PNG，避免重复编码。"""
        dest = step_dir / f"{idx}.png"
        src = self._disk_source(frame)
        if src is not None:
            try:
                shutil.copyfile(src, dest)
                return dest
            except OSError:
                return None
        img = frame.display_image
        if img is None:
            return None
        try:
            save_image(dest, img)
        except Exception:
            return None
        return dest

    def push_snapshot(self, name: str, description: str, frame_indices: List[int],
                      frame_manager: FrameManager) -> int:
        """创建快照并添加到历史记录。"""
        step_id = self._step_counter
        step_dir = self._step_dir(step_id)

        snapshot_paths: Dict[int, Path] = {}
        snapshot_states: Dict[int, bool] = {}
        bytes_used = 0

        if step_dir is not None:
            for idx in frame_indices:
                frame = frame_manager.get_frame(idx)
                if frame is None or frame.display_image is None:
                    continue
                path = self._write_snapshot(step_dir, idx, frame)
                if path is None:
                    continue
                snapshot_paths[idx] = path
                snapshot_states[idx] = frame.has_processed
                try:
                    bytes_used += path.stat().st_size
                except OSError:
                    pass

        entry = HistoryEntry(
            step_id=step_id,
            operation_name=name,
            description=description,
            timestamp=time.time(),
            affected_indices=list(frame_indices),
            snapshot_paths=snapshot_paths,
            snapshot_states=snapshot_states,
            bytes_used=bytes_used,
        )

        self._entries.append(entry)
        self._total_bytes += bytes_used
        self._step_counter += 1

        self._cleanup_history()

        return entry.step_id

    def revert_to(self, step_id: int, frame_manager: FrameManager) -> List[int]:
        """回退到指定步骤（恢复到该步骤执行后的状态）。

        step_id=0 表示回退到初始状态。
        """
        if not self._entries:
            return []

        if step_id == 0:
            target_idx = -1
        else:
            target_idx = None
            for i, entry in enumerate(self._entries):
                if entry.step_id == step_id:
                    target_idx = i
                    break

            if target_idx is None:
                return []

            if target_idx == len(self._entries) - 1:
                return []

        affected_indices = set()

        for i in range(len(self._entries) - 1, target_idx, -1):
            entry = self._entries[i]
            for idx, path in entry.snapshot_paths.items():
                frame = frame_manager.get_frame(idx)
                if frame is None:
                    continue
                snapshot = read_image(path) if path.exists() else None
                if snapshot is None:
                    continue
                was_processed = entry.snapshot_states.get(idx, False)
                if was_processed:
                    frame.processed_image = snapshot
                else:
                    frame.image = snapshot
                    frame.processed_image = None
                    # 同时清掉处理结果的路径引用：否则 has_processed 仍为真，
                    # display_image 会返回已被置空的 processed_image（None）。
                    # 磁盘上的旧文件由随后的 flush→save_original 删除。
                    frame.processed_path = None
                affected_indices.add(idx)

            self._discard(entry)

        self._entries = self._entries[:target_idx + 1]

        return list(affected_indices)

    def clear(self):
        """清空历史记录并删除全部快照文件。"""
        for entry in self._entries:
            self._discard(entry)
        self._entries.clear()
        self._total_bytes = 0
        self._step_counter = 1
        # 兜底：清掉可能残留的快照目录
        if self._snapshot_dir and self._snapshot_dir.exists():
            shutil.rmtree(self._snapshot_dir, ignore_errors=True)

    def get_entries(self) -> List[HistoryEntry]:
        """获取历史记录列表（最新的在最前面）"""
        return list(reversed(self._entries))

    def get_memory_usage(self) -> str:
        """快照占用（现为磁盘占用）。"""
        current_mb = self._total_bytes / (1024 * 1024)
        return f"{current_mb:.1f} MB"

    # --- 内部 ---
    def _discard(self, entry: HistoryEntry) -> None:
        """删除一条历史的快照文件与引用。"""
        self._total_bytes -= entry.bytes_used
        entry.bytes_used = 0
        step_dir = self._snapshot_dir / f"step_{entry.step_id:04d}" if self._snapshot_dir else None
        entry.snapshot_paths.clear()
        entry.snapshot_states.clear()
        if step_dir and step_dir.exists():
            shutil.rmtree(step_dir, ignore_errors=True)

    def _cleanup_history(self):
        while len(self._entries) > self.MAX_STEPS:
            oldest_entry = self._entries.pop(0)
            self._discard(oldest_entry)
