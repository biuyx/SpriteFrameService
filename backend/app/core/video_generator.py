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
from app.services.concurrency import dir_lock


class PollInterrupted(RuntimeError):
    """轮询中断但远端任务未到终态（网络中断/本地等待超时）。

    远端任务已创建、正在计费执行——take 必须保持 running，
    之后打开动作时自动重挂（reconcile）取回结果，而不是判死丢件。
    """


class RemoteGenerationFailed(ArkError):
    """The provider explicitly reported a terminal failure, not a polling/transport error."""

logger = logging.getLogger(__name__)

POLL_INITIAL = 5.0
POLL_MAX = 15.0

TERMINAL_OK = "succeeded"
TERMINAL_BAD = ("failed", "cancelled", "expired")


# 提交响应丢失后的找回：任务 id 里带创建时间（cgt-YYYYMMDDHHMMSS-xxx），
# 按「同模型 + 提交时刻 ±窗口」去任务列表里认。认不到就别猜。
_ADOPT_WINDOW_SECONDS = 180     # 提交时刻前后多久算「同一次提交」
_ADOPT_GIVE_UP_SECONDS = 900    # 超过这么久还认不出来，就判定为失败，允许重生成
_ADOPT_MAX_PAGES = 40           # 最多往前翻 800 条任务（一页 20 条）


def _task_stamp(task_id: str) -> Optional[float]:
    """从 cgt-20261028153616-xxxxx 里取出创建时刻（本地时区秒）。"""
    try:
        return time.mktime(time.strptime(task_id.split("-")[1], "%Y%m%d%H%M%S"))
    except (IndexError, ValueError, OverflowError):
        return None


def _adopt_lost_task(client, take: dict, used_ids: set) -> Optional[str]:
    """提交响应丢失时，按「模型 + 提交时刻」把远端任务认回来。

    列表按创建时间倒序、一页 20 条；任务多的时候提交时刻可能落在好几页之后，
    所以按时间往前翻，翻过提交时刻就停（有上限，别无限翻）。

    只认唯一候选：候选多于一个说明分不清是哪次提交，宁可不动（也不重复花钱）。
    """
    started = take.get("dispatch_started_at") or 0
    model = take.get("model") or ""
    if not started or not model:
        return None
    for page in range(1, _ADOPT_MAX_PAGES + 1):
        try:
            data = client.list_tasks(page_num=page, page_size=20)
        except ArkError as e:
            logger.warning("找回丢失任务时查询任务列表失败：%s", e)
            return None
        items = data.get("items") or []
        candidates, stamps = [], []
        for t in items:
            tid = t.get("id") or ""
            stamp = _task_stamp(tid)
            if stamp:
                stamps.append(stamp)
            if not stamp or tid in used_ids or t.get("model") != model:
                continue
            if abs(stamp - started) <= _ADOPT_WINDOW_SECONDS:
                candidates.append(tid)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logger.warning("找回丢失任务时出现多个候选 %s，放弃自动认领", candidates[:5])
            return None
        if len(items) < 20:
            break
        # 这一页最老的任务都已经早于「提交时刻 - 窗口」→ 再往前全是更老的，没必要翻了
        if stamps and min(stamps) < started - _ADOPT_WINDOW_SECONDS:
            break
    return None


def _cancel_remote_task(task_id, model=None):
    # 停止请求在独立线程中执行，原轮询客户端可能已经退出并关闭。
    # 要按模型选接入点：亚太（无预过滤）的任务得去 byteplus 取消
    client = ArkClient.for_model(model)
    try:
        return client.cancel_task(task_id)
    finally:
        client.close()


# ------------------------------------------------------------ 首帧编码
def _encode_first_frame(path: Path, max_side: int = 1024, *, image_bytes=None) -> Optional[str]:
    """读图 → 等比缩至最长边 ≤max_side → JPEG(q90) → data URL。

    原图直接 base64 很容易超请求体上限，必须压。透明背景合成到白底
    （Seedance 输入是照片语义，透明通道无意义）。
    """
    raw = np.frombuffer(image_bytes, np.uint8) if image_bytes is not None else np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
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
    lock = dir_lock(session.storage.root)

    def _read() -> dict:
        if not cache_path.is_file():
            return {}
        try:
            return _json.loads(cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    with lock:
        hit = _read().get(digest)
    if hit:
        return hit

    url = upload_bytes(data, f"seedance/sprite-service/refs/{digest}.{ext}",
                       content_type)
    # 上传在锁外（可能要几秒），存缓存时重新读一次：这期间别的生成可能写进了别的哈希，
    # 用早先那份内存里的副本回写会把它们抹掉
    with lock:
        cache = _read()
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
            session.load_video_info(str(p))
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
        "dispatch_state": "resolved",
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
            raise RemoteGenerationFailed(f"远端任务{status}: {msg or '无详情'}")
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
    if req.get('template_id'):
        from app.services.template_bindings import validate_video_reference
        validate_video_reference(session.id, req['template_id'])
    take_store = TakeStore(session.storage)
    params = req.get("params") or {}
    model = req.get("model") or settings.ark_model

    take = take_store.reserve_generation(
        owner_job_id=getattr(ctx, 'job_id', None),
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
    attempted = False

    try:
        ctx.report(2, "准备请求...")
        image_url = (_encode_first_frame(session.storage.root / 'first_frame.png',
                                        image_bytes=req['_first_frame_bytes'])
                     if '_first_frame_bytes' in req else resolve_first_frame(session, req.get("first_frame")))
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
                if '_reference_video_bytes' in req:
                    video_url = _cached_upload(session, req['_reference_video_bytes'], 'mp4', 'video/mp4')
                elif template_id:
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

        client = ArkClient.for_model(model)
        try:
            ctx.report(5, "提交生成任务...")
            if req.get('template_id'):
                from app.services.template_bindings import validate_video_reference
                validate_video_reference(session.id, req['template_id'])
            if hasattr(ctx, 'before_paid_call'):
                ctx.before_paid_call()
            if ctx.cancelled():
                raise ArkError('已请求取消')
            # Commit the uncertain boundary before POST. A lost create response
            # or process crash must block every entry point from charging again.
            take_store.update(take_id, {'dispatch_state': 'unknown',
                                        'dispatch_started_at': time.time()})
            attempted = True
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
            take_store.update(take_id, {"remote_task_id": remote_id, "status": "running",
                                       'dispatch_state': 'created'})
            if hasattr(ctx, 'record_remote'):
                ctx.record_remote(remote_id)
            ctx.register_cancel(lambda: _cancel_remote_task(remote_id, model))

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
        recoverable = (bool(rec.get('remote_task_id')) and not isinstance(e, RemoteGenerationFailed))
        if recoverable:
            take_store.update(take_id, {
                "status": "running",
                "error": ('本地已停止，远端取消已尝试；请核对原远端任务并取回已有结果，不要重复生成'
                          if ctx.cancelled() else f"轮询中断（重新打开该动作自动重挂取回）: {str(e)[:180]}"),
            })
        elif attempted and not rec.get('remote_task_id') and not getattr(e, 'request_rejected', False):
            take_store.update(take_id, {'status': 'running', 'dispatch_state': 'unknown',
                'error': '生成提交结果未确认，可能已计费。请先核对远端结果，禁止重复提交；' + str(e)[:120]})
        else:
            take_store.update(take_id, {"status": "failed", "error": str(e)[:300],
                                       'dispatch_state': 'rejected' if attempted else 'not_sent'})
        raise


def _adopt_lost_dispatches(take_store, ctx, results: list) -> list:
    """把「提交响应丢失」的 take 找回成有任务号的，或者在确认远端没有时判定失败。

    这类 take 没有 remote_task_id，普通重挂永远处理不到，会一直卡在「生成中」。
    方舟的任务列表带创建时间，按「同模型 + 提交时刻 ±3 分钟」能认回来。
    认不出来又不肯放弃，等于让人一直等着——所以超过 15 分钟就按失败处理并说明白。
    """
    takes = take_store.load()["takes"]
    lost = [t for t in takes
            if t.get("source") == "generate"
            and t.get("dispatch_state") == "unknown"
            and not t.get("remote_task_id")
            and t.get("status") in ("pending", "running")]
    if not lost:
        return []
    used = {t.get("remote_task_id") for t in takes if t.get("remote_task_id")}
    adopted = []
    for take in lost:
        tid = take["id"]
        client = None
        try:
            client = ArkClient.for_model(take.get("model"))
            rid = _adopt_lost_task(client, take, used)
            if rid:
                take_store.update(tid, {"remote_task_id": rid, "dispatch_state": "created",
                                        "status": "running", "error": None})
                used.add(rid)
                adopted.append(take_store.get(tid))
                results.append({"id": tid, "status": "adopted", "remote_task_id": rid})
                logger.info("找回丢失的远端任务：%s → %s", tid, rid)
            elif (time.time() - (take.get("dispatch_started_at") or 0)) > _ADOPT_GIVE_UP_SECONDS:
                take_store.update(tid, {
                    "status": "failed", "dispatch_state": "resolved",
                    "error": "提交响应丢失，远端任务列表里按「模型 + 提交时刻」查无此任务，"
                             "按失败处理；可以重新生成。若远端确实出了片，说明这次没取回、"
                             "已计费但不影响重做。"})
                results.append({"id": tid, "status": "failed",
                                "detail": "远端没有对应的任务，已解除锁定"})
            else:
                results.append({"id": tid, "status": "waiting",
                                "detail": "提交结果未确认，等远端任务出现后再自动认领"})
        except ArkError as e:
            results.append({"id": tid, "status": "error", "detail": str(e)[:120]})
        finally:
            if client is not None:
                client.close()
    return adopted


def run_reconcile(session, ctx) -> dict:
    """重挂未完结的远端任务：重启/超时后取回结果或确认失败。"""
    take_store = TakeStore(session.storage)
    results = []
    pending = take_store.pending_remote()
    # 先处理「连任务号都没有」的：能从远端认回来就变成可重挂的
    pending += _adopt_lost_dispatches(take_store, ctx, results)
    if not pending and not results:
        return {"reconciled": 0, "results": []}

    for i, take in enumerate(pending):
        tid, rid = take["id"], take["remote_task_id"]
        ctx.report(i / len(pending) * 100, f"检查 {tid} ...")
        # 每条 take 可能属于不同接入点（国内 / 亚太），各自建客户端
        client = None
        try:
            client = ArkClient.for_model(take.get("model"))
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
                    take_store.update(tid, {"status": "failed", 'dispatch_state': 'resolved',
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
            if client is not None:
                client.close()
    return {"reconciled": len(results), "results": results}
