"""视频上传与信息 API。"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import get_session
from app.config import get_settings

router = APIRouter(prefix="/sessions/{session_id}/video", tags=["video"])


@router.post("")
async def upload_video(session_id: str, file: UploadFile = File(...)):
    """上传视频文件并读取元数据（分块落盘，避免整个文件进内存）。"""
    session = get_session(session_id)

    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    # 上传前放掉旧视频的解码句柄，否则 Windows 下文件被占用
    session.release_video_handle()

    # take 化：每次上传是一个新版本，与旧版本并存
    from app.services.take_store import TakeStore
    take_store = TakeStore(session.storage)
    take = take_store.add("upload", filename=file.filename)

    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    try:
        dest, written = await session.storage.save_video_stream(
            file, file.filename, max_bytes=max_bytes,
            dest=take_store.path(take["id"]),
        )
    except ValueError as e:
        take_store.delete(take["id"])
        raise HTTPException(status_code=413, detail=str(e))
    except Exception:
        take_store.delete(take["id"])
        raise

    if written == 0:
        take_store.delete(take["id"])
        raise HTTPException(status_code=400, detail="文件为空")

    try:
        video_info = session.video_processor.load_video(str(dest))
    except Exception as e:
        take_store.delete(take["id"])
        raise HTTPException(status_code=400, detail=f"无法解析视频: {e}")

    # 上传成功：标记 take 成功并设为当前使用
    take_store.update(take["id"], {"status": "succeeded", "bytes": written})
    take_store.set_current(take["id"])
    session.video_info = video_info

    # 清理旧的帧数据
    session.frame_manager.clear()
    session.history.clear()

    # 工序记录：素材来源（换视频重置工序链）
    from app.services import recipe
    recipe.set_source(session, {
        "kind": "upload",
        "take_id": take["id"],
        "filename": file.filename,
        "bytes": written,
        "video": {"width": video_info.width, "height": video_info.height,
                  "fps": video_info.fps, "duration": video_info.duration},
    })

    return {"video_info": video_info.model_dump(), "path": str(dest),
            "take_id": take["id"]}


@router.get("")
def get_video_file(session_id: str):
    """返回视频文件（供浏览器 <video> 预览）。"""
    session = get_session(session_id)
    path = session.storage.video_path
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="未上传视频")

    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return FileResponse(str(path), media_type=media_type)


@router.get("/info")
def get_video_info(session_id: str):
    session = get_session(session_id)
    if session.video_info is None:
        path = session.storage.video_path
        if path is None:
            raise HTTPException(status_code=404, detail="未上传视频")
        session.video_info = session.video_processor.load_video(str(path))
    return session.video_info.model_dump()
