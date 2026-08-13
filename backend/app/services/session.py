"""会话管理：一个会话 = 一个视频项目，包含帧管理器/历史/分析数据。"""
from __future__ import annotations

import shutil
import threading
import time
from typing import Dict, List, Optional

import numpy as np

from app.config import get_settings
from app.core.frame_manager import FrameManager
from app.core.history_manager import HistoryManager
from app.core.pose_detector import PoseDetector
from app.core.video_processor import VideoProcessor
from app.models.frame_data import FrameStatus, VideoInfo
from app.services.frame_store import FrameStore
from app.services.storage import SessionStorage, create_session_storage
from app.utils.image_utils import save_image


class Session:
    """单个处理会话（内存态，生命周期 = 服务进程）。

    帧图像以文件形式存于磁盘（FrameStore），处理时按需载入内存。
    """

    def __init__(self, session_id: str, storage: SessionStorage):
        self.id = session_id
        self.storage = storage
        self.created_at = time.time()
        self.last_access = time.time()

        self.frame_manager = FrameManager()
        self.frame_store = FrameStore(storage)
        self.history = HistoryManager(storage.history_dir)
        self.video_info: Optional[VideoInfo] = None

        # 魔棒选区 mask（{帧索引: mask}），显式声明避免运行时动态挂载
        self.wand_masks: Dict[int, np.ndarray] = {}

        # 会话级互斥：同一会话的后台任务串行执行，避免并发改写帧数据
        self.lock = threading.RLock()

        # 懒加载处理器
        self._video_processor: Optional[VideoProcessor] = None
        self._pose_detector: Optional[PoseDetector] = None

    # --- 访问跟踪 ---
    def touch(self):
        self.last_access = time.time()

    @property
    def video_processor(self) -> VideoProcessor:
        if self._video_processor is None:
            self._video_processor = VideoProcessor()
        return self._video_processor

    @property
    def pose_detector(self) -> PoseDetector:
        if self._pose_detector is None:
            self._pose_detector = PoseDetector()
        return self._pose_detector

    # --- 帧图像加载/保存 ---
    def load_display_array(self, index: int) -> Optional[np.ndarray]:
        """加载帧的显示图像（内存处理图 → 内存原图 → 磁盘处理图 → 磁盘原图）。"""
        frame = self.frame_manager.get_frame(index)
        if frame is None:
            return None
        if frame.processed_image is not None:
            return frame.processed_image
        if frame.image is not None:
            return frame.image
        if self.frame_store.proc_path(frame.id).exists():
            img = self.frame_store.load_processed(frame.id)
            frame.processed_image = img
            return img
        if self.frame_store.raw_path(frame.id).exists():
            img = self.frame_store.load_raw(frame.id)
            frame.image = img
            return img
        return None

    def load_display_arrays(self, indices: List[int]) -> List[Optional[np.ndarray]]:
        """批量加载显示图像（临时驻留内存供处理/历史快照使用）。"""
        return [self.load_display_array(i) for i in indices]

    def save_processed(self, index: int, image: np.ndarray) -> None:
        """保存处理后帧到磁盘并更新元数据，随后释放内存数组。"""
        frame = self.frame_manager.get_frame(index)
        if frame is None:
            return
        path = self.frame_store.save_processed(frame.id, image)
        frame.processed_path = path
        frame.processed_image = None
        frame.image = None
        frame.status = FrameStatus.BACKGROUND_REMOVED

    def save_original(self, index: int, image: np.ndarray) -> None:
        """保存（回写）原始帧到磁盘，并丢弃之前的处理结果。"""
        frame = self.frame_manager.get_frame(index)
        if frame is None:
            return
        path = self.frame_store.raw_path(frame.id)
        save_image(path, image)
        frame.image_path = path
        # 回退到原始状态时清理处理结果，避免预览仍读到旧的处理图
        candidates = [frame.processed_path, self.frame_store.proc_path(frame.id)]
        frame.processed_path = None
        frame.processed_image = None
        frame.image = None
        frame.status = FrameStatus.RAW
        for p in candidates:
            if p and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    def release_video_handle(self) -> None:
        """释放视频解码句柄。

        cv2.VideoCapture 会一直持有文件句柄，Windows 下会导致覆盖上传的视频
        文件被占用（写入失败或删除失败），因此重新上传前必须先释放。
        """
        if self._video_processor is not None:
            self._video_processor.release()
            self._video_processor = None

    # --- 魔棒选区 ---
    MAX_WAND_MASKS = 8

    def set_wand_mask(self, index: int, mask: np.ndarray) -> None:
        """记录选区 mask，超出上限时淘汰最早的（避免全分辨率 mask 无限堆积）。"""
        self.wand_masks[index] = mask
        while len(self.wand_masks) > self.MAX_WAND_MASKS:
            self.wand_masks.pop(next(iter(self.wand_masks)))

    def clear_frame_arrays(self):
        """释放全部帧的内存数组（仅保留磁盘路径）。"""
        for f in self.frame_manager.frames:
            f.image = None
            f.processed_image = None

    def flush_modified_frames(self, indices: List[int]):
        """将指定帧的内存数组写回磁盘并清空。"""
        for idx in indices:
            frame = self.frame_manager.get_frame(idx)
            if frame is None:
                continue
            if frame.processed_image is not None:
                self.save_processed(idx, frame.processed_image)
            elif frame.image is not None:
                self.save_original(idx, frame.image)

    def persist_metadata(self):
        self.frame_store.save_metadata(self.frame_manager)

    # --- 概要 ---
    def summary(self) -> dict:
        fm = self.frame_manager
        return {
            "id": self.id,
            "created_at": self.created_at,
            "video_info": self.video_info.model_dump() if self.video_info else None,
            "frame_count": fm.frame_count,
            "selected_count": fm.selected_count,
            "history_steps": len(self.history.get_entries()),
            "history_memory": self.history.get_memory_usage(),
        }


class SessionManager:
    """工作态注册表（内存索引 + 磁盘持久化）。

    「会话」现在是动作（action）的工作态：ID 即 action_id，数据存于
    data/sprites/{sid}/actions/{aid}/。旧版匿名会话（data/sessions/{id}/）
    仍可按原 ID 打开，用于浏览与认领，属兼容路径。
    """

    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.RLock()

    def create(self) -> Session:
        """旧版匿名会话创建（仅兼容保留；正常路径走 open_action）。"""
        sid, storage = create_session_storage()
        session = Session(sid, storage)
        with self._lock:
            self._sessions[sid] = session
        return session

    @staticmethod
    def _locate_root(session_id: str):
        """定位工作目录：精灵动作优先，旧 sessions 目录兜底。"""
        from app.services.sprite_store import sprite_store
        root = sprite_store.locate_action(session_id)
        if root is not None:
            return root
        legacy = get_settings().sessions_dir / session_id
        return legacy if legacy.is_dir() else None

    def _restore(self, session_id: str) -> Optional[Session]:
        """从磁盘恢复工作态（动作目录或旧会话目录）。"""
        root = self._locate_root(session_id)
        if root is None:
            return None

        session = Session(session_id, SessionStorage(session_id, root=root))
        session.frame_store.load_metadata(session.frame_manager)

        video_path = session.storage.video_path
        if video_path and video_path.exists():
            try:
                session.video_info = session.video_processor.load_video(str(video_path))
            except Exception:
                session.video_info = None

        return session

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None:
            # 进程重启后首次访问：从磁盘恢复。恢复会读取视频元数据，
            # 放在锁外执行，避免阻塞其他会话的请求。
            restored = self._restore(session_id)
            if restored is None:
                return None
            with self._lock:
                # 双重检查：并发请求可能已经恢复过同一会话
                session = self._sessions.get(session_id)
                if session is None:
                    self._sessions[session_id] = restored
                    session = restored
                else:
                    self._release_handles(restored)

        session.touch()
        return session

    def get_required(self, session_id: str) -> Session:
        session = self.get(session_id)
        if session is None:
            raise KeyError(f"会话不存在: {session_id}")
        return session

    @staticmethod
    def _release_handles(session: Session) -> None:
        """释放视频/模型句柄（Windows 下文件被占用时无法删除）。"""
        if session._video_processor is not None:
            session._video_processor.release()
            session._video_processor = None
        if session._pose_detector is not None:
            session._pose_detector.release()
            session._pose_detector = None

    def delete(self, session_id: str) -> bool:
        """删除工作数据，包括磁盘（不可恢复）。

        若 ID 是精灵动作，则交由实体层删除（同步 sprite.json 与索引）；
        否则按旧版匿名会话处理。
        """
        with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is not None:
            self._release_handles(session)
            session.frame_manager.clear()

        from app.services.sprite_store import sprite_store
        sprite_id = sprite_store.sprite_of_action(session_id)
        if sprite_id is not None:
            return sprite_store.delete_action(sprite_id, session_id)

        if session is not None:
            session.storage.clear()
            return True

        # 仅存在于磁盘（进程重启后未被访问过）的旧会话也允许删除
        root = get_settings().sessions_dir / session_id
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)
            return True
        return False

    def release(self, session_id: str) -> bool:
        """从内存卸载会话但保留磁盘数据（下次访问自动恢复）。"""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        self._release_handles(session)
        session.frame_manager.clear()
        return True

    def release_all(self) -> int:
        """卸载全部会话（进程关闭时调用），不触碰磁盘数据。"""
        with self._lock:
            sids = list(self._sessions.keys())
        for sid in sids:
            self.release(sid)
        return len(sids)

    def list(self) -> list[dict]:
        """列出旧版匿名会话（data/sessions/ 下的目录），供「未归档认领」。

        精灵动作不在此列——它们经 /api/sprites 的看板呈现。
        """
        sessions_dir = get_settings().sessions_dir
        result = []
        if sessions_dir.is_dir():
            for d in sorted(sessions_dir.iterdir()):
                if not d.is_dir():
                    continue
                raw = d / "frames" / "raw"
                video = d / "video"
                result.append({
                    "id": d.name,
                    "created_at": d.stat().st_ctime,
                    "frame_count": sum(1 for _ in raw.glob("*.png")) if raw.is_dir() else 0,
                    "has_video": video.is_dir() and any(
                        f.is_file() and ".part" not in f.name for f in video.iterdir()),
                    "legacy": True,
                })
        return result

    def cleanup_idle(self, max_idle_seconds: float = 86400) -> int:
        """卸载空闲超时的会话（默认 24h）：仅释放内存，磁盘数据保留。"""
        now = time.time()
        with self._lock:
            dead = [sid for sid, s in self._sessions.items()
                    if now - s.last_access > max_idle_seconds]
        for sid in dead:
            self.release(sid)
        return len(dead)


session_manager = SessionManager()
