"""网页上传暂存：把用户本机的一个文件夹（或一个 zip）传到服务器，返回服务器上的目录。

参考视频库批量导入、首帧参考集导入、批量建档原本都只能填**服务器上**的路径——
服务跑在别的机器上时，用户填的是自己电脑上的路径，服务器上根本没有。

这里只解决「文件怎么到服务器上」：先落到暂存区，再把暂存目录交给原来的导入
接口。导入逻辑（文件名解析、key 匹配、去重）一行不改，两段式的「先预览再导入」
也天然兼容——暂存目录在两步之间一直在。
"""
from __future__ import annotations

import shutil
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import get_settings
from app.utils.zipnames import member_name

router = APIRouter(prefix="/uploads", tags=["uploads"])

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
SPINE_EXTS = {".json", ".skel", ".atlas"}
# 只落地导入用得上的文件：Thumbs.db、.DS_Store、随手放进去的文档都不要
ALLOWED_EXTS = IMAGE_EXTS | VIDEO_EXTS | SPINE_EXTS

MAX_FILES = 5000
STAGING_TTL = 24 * 3600          # 暂存一天：够预览 + 导入，过期的下次上传时顺手清掉
_CHUNK = 1024 * 1024


def staging_root() -> Path:
    return get_settings().resolved_data_dir / "tmp" / "uploads"


def _limits() -> tuple:
    """单文件与单次总量上限（MB→字节）。总量给到单文件的 4 倍：一批模板视频经常上 G。"""
    per_file = max(1, get_settings().max_upload_mb) * 1024 * 1024
    return per_file, per_file * 4


def safe_rel(name: str) -> Optional[PurePosixPath]:
    """浏览器给的相对路径 → 安全的相对路径；越界的返回 None。

    Windows 上的浏览器可能给反斜杠，盘符、绝对路径、..、空段一律拒绝。
    """
    if not name:
        return None
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ":" in p.parts[0]:
        return None
    if any(part in ("", ".", "..") for part in p.parts):
        return None
    return p


def _gc() -> None:
    root = staging_root()
    if not root.is_dir():
        return
    cutoff = time.time() - STAGING_TTL
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _picked_dir(root: Path) -> Path:
    """用户选的那个文件夹在暂存区里对应哪一层。

    选文件夹上传时，相对路径都带着文件夹自己的名字（deliverable/角色A/x.png）；
    zip 常常也包着一层同名目录。只剥这一层包装：再往下剥就会把「只有一个角色」
    的首帧根目录剥成角色目录本身，批量建档就认不出结构了。
    """
    entries = [p for p in root.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root


def _extract_zip(src: Path, dest: Path, budget: int) -> tuple:
    """解 zip 到 dest，只取允许的文件类型，防路径穿越。返回 (文件数, 字节数, 跳过数)。"""
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="不是有效的 zip 文件")
    n = size = skipped = 0
    with z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = safe_rel(member_name(info))
            # macOS 打的包里带 __MACOSX 资源叉，不是用户的文件
            if rel is None or rel.parts[0] == "__MACOSX" \
                    or rel.suffix.lower() not in ALLOWED_EXTS:
                skipped += 1
                continue
            size += info.file_size
            if size > budget:
                raise HTTPException(status_code=400, detail="解压后体积超过上限")
            n += 1
            if n > MAX_FILES:
                raise HTTPException(status_code=400, detail="文件数超过上限")
            out = dest.joinpath(*rel.parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as fsrc, open(out, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst, _CHUNK)
    return n, size, skipped


async def _save(up: UploadFile, out: Path, per_file: int, budget: list) -> int:
    """流式落盘，边写边查上限。budget 是剩余总量（单元素列表，跨文件扣减）。"""
    written = 0
    with open(out, "wb") as f:
        while True:
            chunk = await up.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > per_file:
                raise HTTPException(status_code=400,
                                    detail=f"单个文件超过上限：{out.name}")
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise HTTPException(status_code=400, detail="本次上传总量超过上限")
            f.write(chunk)
    return written


@router.post("/staging")
async def upload_staging(files: List[UploadFile] = File(...),
                         paths: List[str] = Form(default=[])):
    """上传一个文件夹（多文件 + 各自的相对路径）或一个 zip，落到暂存区。

    返回 dir：服务器上的目录，直接填给原来那几个「按目录导入」的接口。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"文件数超过上限（{MAX_FILES}）")
    _gc()
    per_file, total_cap = _limits()
    root = staging_root() / uuid.uuid4().hex[:12]
    root.mkdir(parents=True, exist_ok=True)
    count = total = skipped = 0
    try:
        # 只传了一个 zip：当作打包好的文件夹
        only = files[0]
        budget = [total_cap]
        if len(files) == 1 and (only.filename or "").lower().endswith(".zip"):
            tmp = root / "_upload.zip"
            await _save(only, tmp, total_cap, budget)
            body = root / "content"
            body.mkdir()
            count, total, skipped = _extract_zip(tmp, body, total_cap)
            tmp.unlink(missing_ok=True)
            base = body
        else:
            base = root
            for i, up in enumerate(files):
                # 浏览器选目录时文件名只有末段，相对路径单独随表单字段过来
                rel = safe_rel(paths[i] if i < len(paths) and paths[i] else up.filename)
                if rel is None or rel.suffix.lower() not in ALLOWED_EXTS:
                    skipped += 1
                    continue
                out = base.joinpath(*rel.parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                total += await _save(up, out, per_file, budget)
                count += 1
        if not count:
            raise HTTPException(
                status_code=400,
                detail="没有可导入的文件（只收图片、视频与 Spine 的 .json/.skel/.atlas）")
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    picked = _picked_dir(base)
    return {"dir": str(picked), "name": picked.name if picked != base else "",
            "files": count, "bytes": total, "skipped": skipped}

