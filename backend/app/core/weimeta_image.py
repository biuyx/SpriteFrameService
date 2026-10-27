"""weimeta 生图网关（gpt-image-2.5 家族）直连。

直连上游：自己拿网关密钥直接调，不经任何中间服务，也不依赖别人的账号与额度。

契约（实测；官方文档里没有查询接口那一段）：
    提交  POST {base}/v2/images/generations   Authorization: Bearer <key>
          {model, prompt, n, size?, background?, output_format?, image_urls?}
          → {id, model, status:"QUEUED", created_at}
    查询  GET  {base}/v2/tasks/{id}           Authorization: Bearer <key>
          → {id, model, status, progress,
             output:{images:[{url}]}, usage:{quota}, error:{code,message}}
    状态  QUEUED → IN_PROGRESS → SUCCESS / FAILURE（是 FAILURE，不是 FAILED）
    失败的任务 usage.quota = 0，即失败不计费。

参考图只收公网 URL（image_urls，最多 9 张），我们本来就传 OSS，直接用。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Callable, List, Optional
from urllib.parse import urlparse

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_REF_PREFIX = "sprite-service/first-frame-refs"
_url_cache: dict[str, str] = {}
_url_lock = threading.Lock()


class WeimetaError(RuntimeError):
    """网关不可用或生成失败（信息可直接展示给使用者）。"""


def configured() -> bool:
    s = get_settings()
    return bool(s.weimeta_base_url.strip() and s.weimeta_api_key.strip())


def _base() -> str:
    return get_settings().weimeta_base_url.rstrip("/")


def _headers() -> dict:
    key = get_settings().weimeta_api_key.strip()
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _is_dns_error(e: Exception) -> bool:
    """是否是「域名没解析出来」——这种失败请求一定没发出去，重试安全。"""
    msg = str(e).lower()
    return ("name resolution" in msg or "nodename nor servname" in msg
            or "getaddrinfo" in msg or "name or service not known" in msg)


def _explain(r: httpx.Response, stage: str) -> str:
    """把网关报错翻成能照做的中文。"""
    try:
        body = r.json() or {}
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(err.get("code") or "")
    msg = str(err.get("message") or body.get("message") or "")[:200]
    if r.status_code == 401:
        return ("网关鉴权失败（401）：WEIMETA_API_KEY 无效或已过期，"
                "请到设置里换一把新的")
    if code == "model_not_found" or "模型不存在" in msg:
        return f"网关没有这个模型：{msg}（请核对模型名；此次未计费）"
    if r.status_code >= 500 and not msg:
        return f"网关{stage}失败：HTTP {r.status_code}（上游异常，可稍后重试）"
    return f"网关{stage}失败：HTTP {r.status_code} {msg or r.text[:160]}"


def _public_url(data: bytes, content_type: str = "image/png") -> str:
    """参考图传 OSS 换公网地址（按内容哈希去重，批量生成时立绘只传一次）。"""
    from app.core.oss_uploader import OssError, upload_bytes

    digest = hashlib.sha1(data).hexdigest()
    with _url_lock:
        hit = _url_cache.get(digest)
    if hit:
        return hit
    ext = "jpg" if content_type.endswith("jpeg") else "png"
    try:
        url = upload_bytes(data, f"{_REF_PREFIX}/{digest}.{ext}", content_type)
    except OssError as e:
        raise WeimetaError(
            "参考图要先传到 OSS 才能交给网关（它只收公网 URL），"
            f"但上传失败：{e}") from e
    with _url_lock:
        _url_cache[digest] = url
    return url


class WeimetaTransientError(WeimetaError):
    """上游偶发失败（EOF、openai request failed 之类）。失败不计费，值得重试。"""


_ATTEMPTS = 3          # 含首次；失败不计费，所以重试是安全的


def generate_image(prompt: str, images: Optional[List[bytes]] = None, *,
                   model: Optional[str] = None, size: Optional[str] = None,
                   progress: Optional[Callable[[float, str], None]] = None,
                   cancelled: Optional[Callable[[], bool]] = None) -> bytes:
    """生成一张图并返回图片字节。images 为参考图（顺序即提示词里的图1、图2…）。

    上游偶发失败（网关回 upstream_error / EOF）会自动重试：这类失败不计费，
    重试不会多花钱；内容策略拒绝、模型不存在、鉴权失败则不重试。
    """
    last: Optional[Exception] = None
    for attempt in range(_ATTEMPTS):
        try:
            return _generate_once(prompt, images, model=model, size=size,
                                  progress=progress, cancelled=cancelled,
                                  attempt=attempt)
        except WeimetaTransientError as e:
            last = e
            if attempt == _ATTEMPTS - 1:
                raise
            if progress:
                progress(22, f"上游抖动（{e}），{3 + attempt * 3} 秒后重试...")
            if cancelled and cancelled():
                raise WeimetaError("已取消") from e
            time.sleep(3 + attempt * 3)
    raise WeimetaError(str(last) if last else "生图失败")


def _generate_once(prompt: str, images: Optional[List[bytes]], *,
                   model: Optional[str], size: Optional[str],
                   progress: Optional[Callable[[float, str], None]],
                   cancelled: Optional[Callable[[], bool]],
                   attempt: int = 0) -> bytes:
    s = get_settings()
    if not configured():
        raise WeimetaError(
            "未配置生图网关：请在 backend/.env 设置 SPRITE_WEIMETA_BASE_URL 与 "
            "SPRITE_WEIMETA_API_KEY（或到「设置」里填）")
    model = (model or s.weimeta_image_model).strip()

    def note(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    payload: dict = {"model": model, "prompt": prompt, "n": 1,
                     "output_format": "png", "background": "opaque"}
    if size:
        payload["size"] = size
    if images:
        note(12, "参考图上传中...")
        payload["image_urls"] = [_public_url(b, "image/png") for b in images][:9]

    if cancelled and cancelled():
        raise WeimetaError("已取消")
    note(20, f"提交生图任务（{model}）...")
    r = None
    for attempt in range(3):
        try:
            r = httpx.post(f"{_base()}/v2/images/generations", json=payload,
                           headers=_headers(), timeout=httpx.Timeout(60.0))
            break
        except httpx.HTTPError as e:
            # 只有「域名都没解析出来」才是确定没发出去，可以放心重试；
            # 其它连接层失败可能已经到达网关，重试会重复扣费，直接报。
            if _is_dns_error(e) and attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise WeimetaError(
                f"连不上生图网关（{urlparse(_base()).hostname or _base()}）："
                f"{type(e).__name__}。这次提交不确定是否已到达网关，"
                "请先到网关后台确认没有生成记录，再重试") from e
    assert r is not None
    if r.status_code >= 400:
        raise WeimetaError(_explain(r, "提交"))
    task_id = str((r.json() or {}).get("id") or "").strip()
    if not task_id:
        raise WeimetaError(f"网关没返回任务号：{r.text[:160]}")

    deadline = time.time() + max(30, s.weimeta_timeout_seconds)
    last_note = 0.0
    net_failures = 0          # 连续的网络失败次数
    while True:
        if cancelled and cancelled():
            raise WeimetaError("已取消")
        if time.time() > deadline:
            raise WeimetaError(
                f"生图超时（{s.weimeta_timeout_seconds} 秒未出图）：任务 {task_id} "
                "可能仍在上游执行；先到网关后台确认，别急着重复提交（会再扣一次费）")
        try:
            r = httpx.get(f"{_base()}/v2/tasks/{task_id}", headers=_headers(),
                          timeout=httpx.Timeout(30.0))
        except httpx.HTTPError as e:
            # 任务已经提交、钱已经花，一次网络抖动不该把结果丢掉：重试到超时为止
            net_failures += 1
            note(35, f"查询中断（{type(e).__name__}），重试中...")
            if net_failures >= 8:
                raise WeimetaError(
                    f"查询生图任务连续失败 {net_failures} 次（{type(e).__name__}）："
                    f"任务 {task_id} 可能仍在网关执行，请稍后到网关后台确认，"
                    "别重复提交（会再扣一次费）") from e
            time.sleep(max(1.0, s.weimeta_poll_seconds))
            continue
        net_failures = 0
        if r.status_code >= 400:
            raise WeimetaError(_explain(r, "查询"))
        st = r.json() or {}
        status = str(st.get("status") or "").upper()
        if status == "SUCCESS":
            out = st.get("output") or {}
            urls = [img.get("url") for img in (out.get("images") or [])
                    if isinstance(img, dict) and img.get("url")]
            if not urls:
                raise WeimetaError("网关返回成功但没有图片地址")
            note(80, "下载生成结果...")
            return _download(urls[0])
        if status == "FAILURE":
            err = st.get("error") or {}
            detail = err.get("message") if isinstance(err, dict) else str(err or "")
            quota = _quota_of(st)
            msg = _friendly_failure(model, detail, quota)
            logger.warning("生图失败 task=%s model=%s quota=%s detail=%s",
                           task_id, model, quota, (detail or "")[:300])
            if _is_transient_detail(detail):
                raise WeimetaTransientError(msg)
            raise WeimetaError(msg)
        now = time.time()
        if now - last_note >= 3:
            last_note = now
            note(30 + min(45, 3 * len(str(st.get("progress") or ""))) if st.get("progress") else 35,
                 f"生图中（{model}）...")
        time.sleep(max(0.5, s.weimeta_poll_seconds))


def _quota_of(st: dict) -> Optional[int]:
    """网关终态的 usage.quota（失败时实测为 0）；取不到返回 None，不替它下结论。"""
    usage = st.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        return int(usage.get("quota"))
    except (TypeError, ValueError):
        return None


def _billing_note(quota: Optional[int]) -> str:
    if quota is None:
        return ""
    if quota <= 0:
        return "（网关返回 quota=0，本次未计费）"
    return f"（本次已计费 quota={quota}）"


def _is_transient_detail(detail: str) -> bool:
    """上游连接层的偶发失败（EOF、openai request failed…）——这类失败不计费，值得重试。"""
    low = (detail or "").lower()
    return any(k in low for k in ("upstream_error", "上游调用失败", "eof",
                                  "timeout", "timed out", "bad gateway",
                                  "connection reset", "502", "503"))


def _friendly_failure(model: str, detail: str, quota: Optional[int] = None) -> str:
    """上游失败时说清怎么办；计费与否只看网关返回的 usage.quota。"""
    low = (detail or "").lower()
    billing = _billing_note(quota)
    if "不可用" in (detail or "") or "unavailable" in low or "not available" in low:
        alt = "gpt-image-2" if model != "gpt-image-2" else "gpt-image-2.5-sunburst"
        return f"{model} 上游暂时不可用{billing}。换 {alt} 再试通常能出图"
    if "content" in low and ("policy" in low or "reject" in low or "safety" in low):
        return f"生图被上游内容策略拒绝（{detail[:80]}）{billing}：改一下提示词再试"
    return f"生图失败：{detail[:200] or '上游未给出原因'}{billing}"


def _download(url: str, attempts: int = 3) -> bytes:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            r = httpx.get(url, timeout=httpx.Timeout(60.0, read=180.0))
            if r.status_code < 400 and r.content:
                return r.content
            last = WeimetaError(f"HTTP {r.status_code}")
        except httpx.HTTPError as e:
            last = e
        time.sleep(1 + i)
    host = urlparse(url).hostname or "结果地址"
    raise WeimetaError(f"图片已生成，但下载失败（{host}）：{last}")
