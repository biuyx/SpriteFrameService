"""视频生成编排：组装请求 → 创建远端任务 → 轮询 → 下载落盘 → 登记 take。

关键约束：
- remote_task_id 拿到后第一时间落盘（takes.json）——远端任务是花钱的，
  进程崩溃后必须能凭它重挂取回结果（reconcile）
- video_url 是带签名的临时链接，succeeded 后立即下载转存
- 本编排跑在 io 线程池、不持会话锁：纯网络等待不该堵住本地处理
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from app.config import get_settings
from app.core.ark_client import ArkClient, ArkError
from app.services.take_store import TakeStore

logger = logging.getLogger(__name__)

POLL_INITIAL = 5.0
POLL_MAX = 15.0

TERMINAL_OK = "succeeded"
TERMINAL_BAD = ("failed", "cancelled", "expired")


# ------------------------------------------------------------ 首帧编码
def _encode_first_frame(path: Path, max_side: int = 1024) -> Optional[str]:
    """读图 → 等比缩至最长边 ≤max_side → JPEG(q90) → data URL。

    原图直接 base64 很容易超请求体上限，必须压。透明背景合成到白底
    （Seedance 输入是照片语义，透明通道无意义）。
    """
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        rgb = img[:, :, :3].astype(np.float32)
        img = (rgb * alpha + 255.0 * (1 - alpha)).astype(np.uint8)
    h, w = img.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def resolve_first_frame(session, first_frame: Optional[dict]) -> Optional[str]:
    """把首帧描述解析成 data URL。

    kind=action  动作目录的 first_frame.png（建动作时物化的血统首帧）
    kind=frame   当前动作的第 N 帧（处理图优先）
    其余/None    纯文生视频
    """
    if not first_frame:
        return None
    kind = first_frame.get("kind")
    if kind == "action":
        p = session.storage.root / "first_frame.png"
        return _encode_first_frame(p) if p.is_file() else None
    if kind == "frame":
        try:
            idx = int(first_frame.get("frame_index"))
        except (TypeError, ValueError):
            return None
        frame = session.frame_manager.get_frame(idx)
        if frame is None:
            return None
        for attr in ("processed_path", "image_path"):
            p = getattr(frame, attr, None)
            if p and Path(p).is_file():
                return _encode_first_frame(Path(p))
    return None


# ------------------------------------------------------------ 轮询共用
def _refresh_session_video(session, take_store: TakeStore, take_id: str) -> None:
    """生成的 take 成为当前版本时，同步刷新会话的视频元数据。

    否则前端 refreshSession 拉到 video_info=None，视频信息与预览区都
    不渲染——看起来就像“生成完成后什么都没发生”。
    """
    if take_store.current_id() != take_id:
        return
    try:
        p = session.storage.video_path
        if p and p.exists():
            session.video_info = session.video_processor.load_video(str(p))
    except Exception:
        logger.exception("刷新视频元数据失败（不影响生成结果）")


def _finalize_success(take_store: TakeStore, take_id: str, info: dict,
                      client: ArkClient) -> dict:
    """下载视频并把 take 置为成功；若当前无选中版本则自动选中。"""
    url = (info.get("content") or {}).get("video_url")
    if not url:
        raise ArkError("任务成功但响应缺少 video_url")
    dest = take_store.path(take_id)
    size = client.download(url, dest)
    take = take_store.update(take_id, {
        "status": "succeeded",
        "bytes": size,
        "seed": info.get("seed"),
        "usage": info.get("usage"),
        "fps": info.get("framespersecond"),
        "resolution": info.get("resolution"),
        "actual_duration": info.get("duration"),
    })
    if take_store.current_id() is None:
        take_store.set_current(take_id)
    return take


def poll_until_done(client: ArkClient, task_id: str, ctx=None,
                    timeout: Optional[float] = None) -> dict:
    """轮询远端任务直至终态；返回最终 info。ctx 可选（进度与取消）。"""
    deadline = time.time() + (timeout or get_settings().ark_timeout_seconds)
    delay = POLL_INITIAL
    started = time.time()
    while True:
        if ctx is not None and ctx.cancelled():
            raise ArkError("已请求取消")
        info = client.get_task(task_id)
        status = info.get("status")
        if status == TERMINAL_OK:
            return info
        if status in TERMINAL_BAD:
            err = (info.get("error") or {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ArkError(f"远端任务{status}: {msg or '无详情'}")
        if time.time() > deadline:
            raise ArkError(f"等待超时（任务仍在远端执行，ID={task_id}，可稍后重挂）")
        if ctx is not None:
            elapsed = int(time.time() - started)
            ctx.report(min(90, 10 + elapsed / 3), f"生成中（{status}，已等待 {elapsed}s）...")
        time.sleep(delay)
        delay = min(POLL_MAX, delay + 2.5)


# ------------------------------------------------------------ 主编排
def run_generate(session, req: dict, ctx) -> dict:
    """生成一个 take。req: {prompt, params{resolution,ratio,duration,seed}, first_frame}"""
    settings = get_settings()
    take_store = TakeStore(session.storage)
    params = req.get("params") or {}
    model = req.get("model") or settings.ark_model

    take = take_store.add(
        "generate", status="pending",
        model=model, prompt=req.get("prompt", ""),
        params=params, first_frame=req.get("first_frame"),
        used_reference_video=bool(req.get("use_reference_video")),
    )
    take_id = take["id"]

    try:
        ctx.report(2, "准备请求...")
        image_url = resolve_first_frame(session, req.get("first_frame"))
        if not image_url:
            raise ArkError("角色首帧参考图不可用（文件缺失或帧不存在）")

        if req.get("use_reference_video"):
            # Ark 硬规则：first_frame 角色与参考媒体不能同请求混用。
            # 带参考视频时，首帧图改以 reference_image 传入，
            # 各参考的分工在提示词首句显式声明（官方指南建议）。
            ref_path = session.storage.root / "reference_video.mp4"
            if not ref_path.is_file():
                raise ArkError("参考视频文件缺失")
            video_b64 = base64.b64encode(ref_path.read_bytes()).decode("ascii")
            role_intro = (
                "参考图1为角色的形象、脸部、发型与服装，全程严格保持一致，"
                "不得改变角色设计。参考视频1仅作为动作与镜头节奏的参考，"
                "不参考其中的角色形象与画面风格。"
            )
            content = [
                {"type": "text", "text": role_intro + req.get("prompt", "")},
                {"type": "image_url", "image_url": {"url": image_url},
                 "role": "reference_image"},
                {"type": "video_url",
                 "video_url": {"url": "data:video/mp4;base64," + video_b64},
                 "role": "reference_video"},
            ]
        else:
            content = [
                {"type": "text", "text": req.get("prompt", "")},
                {"type": "image_url", "image_url": {"url": image_url},
                 "role": "first_frame"},
            ]

        client = ArkClient()
        try:
            ctx.report(5, "提交生成任务...")
            created = client.create_task(
                model, content,
                resolution=params.get("resolution"),
                ratio=params.get("ratio"),
                duration=params.get("duration"),
                seed=params.get("seed"),
                watermark=False,
                generate_audio=False,
            )
            remote_id = created["id"]
            # 远端任务已产生（开始计费），第一时间落盘
            take_store.update(take_id, {"remote_task_id": remote_id, "status": "running"})
            ctx.register_cancel(lambda: client.cancel_task(remote_id))

            info = poll_until_done(client, remote_id, ctx)
            ctx.report(92, "生成完成，下载视频...")
            take = _finalize_success(take_store, take_id, info, client)
            _refresh_session_video(session, take_store, take_id)
        finally:
            client.close()

        # 工序记录（成功才记；素材来源在用户「使用该版本」时切换）
        from app.services import recipe
        recipe.record_step(session, "generate", {
            "model": model, "prompt": req.get("prompt", "")[:300],
            "params": params, "take_id": take_id,
            "seed": take.get("seed"),
        }, {"usage": take.get("usage")})

        ctx.report(100, "生成完成")
        return {"take": take}
    except Exception as e:
        take_store.update(take_id, {"status": "failed", "error": str(e)[:300]})
        raise


def run_reconcile(session, ctx) -> dict:
    """重挂未完结的远端任务：重启/超时后取回结果或确认失败。"""
    take_store = TakeStore(session.storage)
    pending = take_store.pending_remote()
    if not pending:
        return {"reconciled": 0, "results": []}

    client = ArkClient()
    results = []
    try:
        for i, take in enumerate(pending):
            tid, rid = take["id"], take["remote_task_id"]
            ctx.report(i / len(pending) * 100, f"检查 {tid} ...")
            try:
                info = client.get_task(rid)
                status = info.get("status")
                if status == TERMINAL_OK:
                    _finalize_success(take_store, tid, info, client)
                    _refresh_session_video(session, take_store, tid)
                    results.append({"id": tid, "status": "succeeded"})
                elif status in TERMINAL_BAD:
                    err = (info.get("error") or {})
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    take_store.update(tid, {"status": "failed",
                                            "error": f"远端{status}: {msg or ''}"[:300]})
                    results.append({"id": tid, "status": "failed"})
                else:
                    results.append({"id": tid, "status": status or "running"})
            except ArkError as e:
                results.append({"id": tid, "status": "error", "detail": str(e)[:120]})
    finally:
        client.close()
    return {"reconciled": len(results), "results": results}
