"""LocalAI 生图通道（gpt-image-2.5 家族等，走公司网关）。

与方舟的区别：
  · 要登录换 JWT（/api/auth/login），不是一次性的 API Key
  · 生图是两段式：提交拿任务号 → 轮询 /api/ai/gateway-image/{id}
  · 参考图只收公网 URL（最多 9 张），所以参考图先传 OSS 换地址
  · 失败不计费（网关侧 usage.quota = 0）

本模块只管"把提示词和图变成一张图"，provider 选择在 image_gen 里做。
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Callable, List, Optional
from urllib.parse import urlparse

import httpx

from app.config import get_settings

# 生图参考图在 OSS 上的落点（按内容哈希去重，批量生成时同一张立绘只传一次）
_REF_PREFIX = "sprite-service/first-frame-refs"
_url_cache: dict[str, str] = {}
_url_lock = threading.Lock()
_token_lock = threading.Lock()
_token: dict = {"value": "", "at": 0.0}


class LocalAIError(RuntimeError):
    """LocalAI 通道不可用或生成失败（信息可直接展示给使用者）。"""


def configured() -> bool:
    return get_settings().localai_configured


def _base() -> str:
    return get_settings().localai_base_url.rstrip("/")


def _session_token(force: bool = False) -> str:
    """登录换 token（进程内缓存；401 时强制重登一次）。"""
    s = get_settings()
    with _token_lock:
        if not force and _token["value"] and time.time() - _token["at"] < 1800:
            return _token["value"]
        try:
            r = httpx.post(f"{_base()}/api/auth/login",
                           json={"usernameOrEmail": s.localai_username,
                                 "password": s.localai_password},
                           timeout=httpx.Timeout(20.0))
        except httpx.HTTPError as e:
            raise LocalAIError(
                f"连不上 LocalAI（{urlparse(_base()).hostname or _base()}）：{type(e).__name__}。"
                "请检查服务器能否访问网关地址") from e
        if r.status_code in (401, 403):
            raise LocalAIError(
                f"LocalAI 账号登录失败（HTTP {r.status_code}）：请核对 "
                "SPRITE_LOCALAI_USERNAME / SPRITE_LOCALAI_PASSWORD，"
                "或该账号是否被禁用")
        if r.status_code >= 400:
            raise LocalAIError(f"LocalAI 登录失败：HTTP {r.status_code} {r.text[:120]}")
        try:
            data = (r.json() or {}).get("data") or {}
        except ValueError as e:
            raise LocalAIError("LocalAI 登录返回的不是 JSON，网关地址可能填错了") from e
        token = (data.get("token") or "").strip()
        if not token:
            raise LocalAIError("LocalAI 登录没拿到 token：请确认账号密码，以及网关版本")
        _token.update({"value": token, "at": time.time()})
        return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_session_token()}"}


def _call(method: str, url: str, **kw) -> httpx.Response:
    """发请求；401 时重登一次再试。"""
    r = httpx.request(method, url, headers=_headers(), **kw)
    if r.status_code == 401:
        _session_token(force=True)
        r = httpx.request(method, url, headers=_headers(), **kw)
    return r


def _explain(r: httpx.Response, stage: str) -> str:
    """把网关的报错翻成能照做的中文。"""
    try:
        body = r.json()
    except ValueError:
        body = None
    msg = ""
    if isinstance(body, dict):
        err = body.get("error")
        msg = err if isinstance(err, str) else (body.get("message") or "")
    if r.status_code == 403:
        return (f"LocalAI 拒绝（403）：这个账号没有「ai:image」权限，"
                f"请管理员在 LocalAI 里给该账号勾上「生图」")
    if r.status_code == 401:
        return "LocalAI 鉴权失败（401）：账号密码或权限有变化，请重新核对配置"
    if r.status_code == 400:
        return f"LocalAI 参数有误（400）：{msg or r.text[:160]}"
    return f"LocalAI {stage}失败：HTTP {r.status_code} {msg or r.text[:160]}"


def _public_url(data: bytes, content_type: str = "image/png") -> str:
    """参考图传 OSS 换公网地址（按内容哈希去重，进程内缓存）。"""
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
        raise LocalAIError(
            "参考图要先传到 OSS 才能交给 LocalAI（它只收公网 URL），"
            f"但上传失败：{e}") from e
    with _url_lock:
        _url_cache[digest] = url
    return url


def generate_image(prompt: str, images: Optional[List[bytes]] = None, *,
                   model: Optional[str] = None, ratio: Optional[str] = None,
                   size: Optional[str] = None,
                   progress: Optional[Callable[[float, str], None]] = None,
                   cancelled: Optional[Callable[[], bool]] = None) -> bytes:
    """生成一张图并返回图片字节。images 为参考图（顺序即提示词里的图1、图2…）。"""
    s = get_settings()
    if not configured():
        raise LocalAIError(
            "未配置 LocalAI：请在 backend/.env 设置 SPRITE_LOCALAI_BASE_URL、"
            "SPRITE_LOCALAI_USERNAME、SPRITE_LOCALAI_PASSWORD")
    model = (model or s.localai_image_model).strip()

    def note(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    payload = {"model": model, "prompt": prompt, "n": 1,
               "outputFormat": "png", "background": "opaque"}
    if ratio:
        payload["ratio"] = ratio
    if size:
        payload["size"] = size
    if images:
        note(12, "参考图上传中...")
        urls = [_public_url(b, "image/png") for b in images]
        payload["imageUrls"] = urls[:9]

    if cancelled and cancelled():
        raise LocalAIError("已取消")
    note(20, f"提交生图任务（{model}）...")
    r = _call("POST", f"{_base()}/api/ai/gateway-image", json=payload,
              timeout=httpx.Timeout(60.0))
    if r.status_code >= 400:
        raise LocalAIError(_explain(r, "提交"))
    try:
        task = r.json() or {}
    except ValueError as e:
        raise LocalAIError("LocalAI 提交后返回的不是 JSON") from e
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise LocalAIError(f"LocalAI 没返回任务号：{str(task)[:160]}")

    deadline = time.time() + max(30, s.localai_timeout_seconds)
    last_note = 0.0
    while True:
        if cancelled and cancelled():
            raise LocalAIError("已取消")
        if time.time() > deadline:
            raise LocalAIError(
                f"LocalAI 生图超时（{s.localai_timeout_seconds} 秒未出图）：任务 {task_id} "
                "可能仍在上游执行；请稍后到 LocalAI 后台确认，避免重复提交")
        r = _call("GET", f"{_base()}/api/ai/gateway-image/{task_id}",
                  timeout=httpx.Timeout(30.0))
        if r.status_code >= 400:
            raise LocalAIError(_explain(r, "查询"))
        try:
            st = r.json() or {}
        except ValueError as e:
            raise LocalAIError("LocalAI 查询结果不是 JSON") from e
        status = str(st.get("status") or "").upper()
        if status == "SUCCESS":
            urls = list(st.get("savedUrls") or [])
            if not urls:
                out = st.get("output") or {}
                urls = [img.get("url") for img in (out.get("images") or [])
                        if isinstance(img, dict) and img.get("url")]
            if not urls:
                raise LocalAIError("LocalAI 返回成功但没有图片地址")
            note(80, "下载生成结果...")
            return _download(urls[0])
        if status == "FAILURE":
            err = st.get("error") or {}
            detail = err.get("message") if isinstance(err, dict) else str(err or "")
            raise LocalAIError(_friendly_failure(model, detail))
        now = time.time()
        if now - last_note >= 3:
            last_note = now
            pct = 20 + min(55, 5 * len(str(st.get("progress") or "")))
            note(pct, f"生图中（{model}）...")
        time.sleep(max(0.5, s.localai_poll_seconds))


def _friendly_failure(model: str, detail: str) -> str:
    """上游失败时说清怎么办（gpt-image-2.5 家族的不稳定是已知的）。"""
    low = (detail or "").lower()
    if "不可用" in (detail or "") or "unavailable" in low or "not available" in low:
        alt = "gpt-image-2" if model != "gpt-image-2" else "gpt-image-2.5-sunburst"
        return (f"{model} 上游暂时不可用（这次不计费）。换 {alt} 再试一次通常能出图")
    if "content" in low and ("policy" in low or "reject" in low or "safety" in low):
        return f"生图被上游内容策略拒绝（{detail[:80]}）：改一下提示词再试"
    return f"LocalAI 生图失败：{detail[:200] or '上游未给出原因'}（失败不计费）"


def _download(url: str, attempts: int = 3) -> bytes:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            r = httpx.get(url, timeout=httpx.Timeout(60.0, read=180.0))
            if r.status_code < 400 and r.content:
                return r.content
            last = LocalAIError(f"HTTP {r.status_code}")
        except httpx.HTTPError as e:
            last = e
        time.sleep(1 + i)
    host = urlparse(url).hostname or "结果地址"
    raise LocalAIError(f"图片已生成，但下载失败（{host}）：{last}")
