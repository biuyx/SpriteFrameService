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
import httpx
import numpy as np

from app.config import get_settings
from app.core.ark_client import ArkClient, ArkError
from app.services.take_store import TakeStore


class PollInterrupted(RuntimeError):
    """轮询中断但远端任务未到终态（网络中断/本地等待超时）。

    远端任务已创建、正在计费执行——take 必须保持 running，
    之后打开动作时自动重挂（reconcile）取回结果，而不是判死丢件。
    """

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


# ------------------------------------------------------------ OSS 哈希缓存
def _cached_upload(session, data: bytes, ext: str, content_type: str) -> str:
    """按内容哈希上传 OSS 并缓存 URL（动作目录 .oss_cache.json）。

    同一首帧图/参考视频在多次生成间只上传一次；对象名即哈希，
    跨动作的相同文件也指向同一对象。
    """
    import hashlib
    import json as _json
    from app.core.oss_uploader import upload_bytes

    digest = hashlib.md5(data).hexdigest()
    cache_path = session.storage.root / ".oss_cache.json"
    cache = {}
    if cache_path.is_file():
        try:
            cache = _json.loads(cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cache = {}
    if cache.get(digest):
        return cache[digest]
    url = upload_bytes(data, f"seedance/sprite-service/refs/{digest}.{ext}",
                       content_type)
    cache[digest] = url
    try:
        cache_path.write_text(_json.dumps(cache, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except OSError:
        pass
    return url


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
    # 下载幂等，网络波动重试；仍失败则由外层保持 running，重挂时再下载
    last_err = None
    for attempt in range(4):
        try:
            size = client.download(url, dest)
            break
        except httpx.HTTPError as e:
            last_err = e
            time.sleep(5 * (attempt + 1))
    else:
        raise last_err
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
    net_fails = 0
    while True:
        if ctx is not None and ctx.cancelled():
            raise ArkError("已请求取消")
        try:
            info = client.get_task(task_id)
            net_fails = 0
        except httpx.HTTPError as e:
            # 网络波动：查询是幂等的，重试而不是判死——远端任务已在计费执行
            net_fails += 1
            if net_fails >= 8:
                raise PollInterrupted(
                    f"轮询连续失败 {net_fails} 次（{type(e).__name__}）——"
                    f"远端任务仍在执行，重新打开该动作可自动重挂取回")
            if ctx is not None:
                elapsed = int(time.time() - started)
                ctx.report(min(90, 10 + elapsed / 3),
                           f"网络波动，重试 {net_fails}/8 ...")
            time.sleep(min(POLL_MAX, delay + 5))
            continue
        status = info.get("status")
        if status == TERMINAL_OK:
            return info
        if status in TERMINAL_BAD:
            err = (info.get("error") or {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ArkError(f"远端任务{status}: {msg or '无详情'}")
        if time.time() > deadline:
            raise PollInterrupted(
                f"本地等待超时（任务仍在远端执行，ID={task_id}），"
                f"重新打开该动作可自动重挂取回")
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
        # 记录所用模板：结果版本可与参考视频做对比回看
        template_id=req.get("template_id") or None,
        # 提示词来源（库条目 + 版本）：素材版本可追溯用的是哪版文案
        prompt_id=req.get("prompt_id") or None,
        prompt_version=req.get("prompt_version"),
        prompt_name=req.get("prompt_name") or None,
    )
    take_id = take["id"]

    try:
        ctx.report(2, "准备请求...")
        image_url = resolve_first_frame(session, req.get("first_frame"))
        if not image_url:
            raise ArkError("角色首帧参考图不可用（文件缺失或帧不存在）")

        template_id = req.get("template_id")
        if template_id or req.get("use_reference_video"):
            # Ark 硬规则一：first_frame 角色与参考媒体不能同请求混用——
            # 带参考视频时首帧图改以 reference_image 传入，分工写进提示词首句。
            # Ark 硬规则二（实测）：参考媒体只接受公网 URL——上传 OSS 换 URL，
            # 且全部按内容哈希缓存：同一模板/同一图给任意多次生成复用，只传一次。
            from app.core.oss_uploader import OssError
            ctx.report(3, "准备参考媒体（OSS）...")
            try:
                if template_id:
                    from app.services.template_store import template_store
                    video_url = template_store.ensure_oss_url(template_id)
                else:
                    ref_path = session.storage.root / "reference_video.mp4"
                    if not ref_path.is_file():
                        raise ArkError("参考视频文件缺失")
                    video_url = _cached_upload(session, ref_path.read_bytes(),
                                               "mp4", "video/mp4")
                # image_url 当前是 data URL，取回原始 JPEG 字节上传
                img_bytes = base64.b64decode(image_url.split(",", 1)[1])
                image_pub = _cached_upload(session, img_bytes, "jpg", "image/jpeg")
            except (OssError, FileNotFoundError) as e:
                raise ArkError(str(e)) from e

            role_intro = (
                "参考图1为角色的形象、脸部、发型与服装，全程严格保持一致，"
                "不得改变角色设计。参考视频1仅作为动作与镜头节奏的参考，"
                "不参考其中的角色形象与画面风格。"
            )
            content = [
                {"type": "text", "text": role_intro + req.get("prompt", "")},
                {"type": "image_url", "image_url": {"url": image_pub},
                 "role": "reference_image"},
                {"type": "video_url", "video_url": {"url": video_url},
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
        rec = take_store.get(take_id) or {}
        # 远端任务已创建且不是远端终态失败（网络中断/下载失败/本地超时）：
        # 保持 running，打开动作时自动重挂取回——付费结果不能丢
        recoverable = (bool(rec.get("remote_task_id"))
                       and not isinstance(e, ArkError))
        if recoverable:
            take_store.update(take_id, {
                "status": "running",
                "error": f"轮询中断（重新打开该动作自动重挂取回）: {str(e)[:180]}",
            })
        else:
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
            except httpx.HTTPError as e:
                # 网络错误：take 保持 running，下次重挂再试；不影响其余条目
                results.append({"id": tid, "status": "network_error",
                                "detail": f"{type(e).__name__}: {str(e)[:100]}"})
    finally:
        client.close()
    return {"reconciled": len(results), "results": results}
