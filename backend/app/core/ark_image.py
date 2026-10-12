"""Ark 生图（Doubao-Seedream）：同步单请求，返回图片字节。

与视频生成共用 base_url 与 API Key；参考图以 base64 data URL 传入
（生图接口支持 base64，区别于视频参考只收公网 URL）。
"""
from __future__ import annotations

import base64
import time
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from app.config import get_settings


class ArkImageError(RuntimeError):
    """生图失败（含中文提示）。"""


_HINTS = {
    "AccessDenied": "生图模型未开通——请在火山方舟控制台「开通管理」开通 Doubao-Seedream 5.0 pro",
    "InvalidEndpointOrModel.NotFound": "生图模型 id 不存在，请核对 SPRITE_ARK_IMAGE_MODEL 配置",
    "OverdueBalance": "火山引擎账户欠费，请充值后重试",
    "RateLimitExceeded": "触发生图限流，请稍后重试",
    "SensitiveContentDetected": "输入或生成内容被安全审核拦截，请检查立绘/提示词",
    "QuotaExceeded": "生图额度不足，请在方舟控制台检查配额",
}


# 连接阶段就失败＝请求根本没发出去，重试不会重复计费；
# 读超时之类不能这么重试——那时服务端可能已经在生成了。
_CONNECT_ERRORS = (httpx.ConnectTimeout, httpx.ConnectError)
_BACKOFF = (2.0, 5.0, 10.0)


def _sleep(i: int) -> None:
    time.sleep(_BACKOFF[min(i, len(_BACKOFF) - 1)])


def _post_generation(client: httpx.Client, url: str, payload: dict,
                     headers: dict, attempts: int = 3) -> httpx.Response:
    """发起生图请求：只在「连都没连上」时重试。

    连接阶段失败意味着请求没送达，重试安全；一旦连上了就不重试——
    重发可能变成生成两次、计费两次。
    """
    last = None
    for i in range(attempts):
        try:
            return client.post(url, json=payload, headers=headers)
        except _CONNECT_ERRORS as e:
            last = e
            if i + 1 < attempts:
                _sleep(i)
    raise ArkImageError(
        f"连不上生图接口（{urlparse(url).hostname}）：{last}。"
        f"重试 {attempts} 次都没通，检查这台机器的网络/代理")


def _fetch_result(client: httpx.Client, url: str, attempts: int = 4) -> bytes:
    """取回生成结果。

    图已经出来了、钱也已经花了，就卡在从对象存储下载这一跳上——网络抖一下
    不该把它丢掉，所以重试几次；实在不通就把是哪台服务器连不上说清楚。
    """
    last = None
    for i in range(attempts):
        try:
            r = client.get(url)
            r.raise_for_status()
            return r.content
        except httpx.HTTPError as e:
            last = e
            if i + 1 < attempts:
                _sleep(i)
    host = urlparse(url).hostname or "结果服务器"
    raise ArkImageError(
        f"图已生成（这次调用已计费），但取回失败：连不上 {host}（{last}）。"
        f"重试 {attempts} 次都没通——检查这台机器到该域名的网络/代理；"
        "长期不通可在 backend/.env 里设 SPRITE_ARK_IMAGE_B64=1，改成让接口直接回图")


def generate_image(prompt: str, images: Optional[List[bytes]] = None,
                   size: str = "1.5K", model: Optional[str] = None,
                   timeout: float = 180.0) -> bytes:
    """生成一张图并返回字节。images 为参考图（顺序即提示词中的图1、图2…）。

    默认 Seedream 5.0 pro：size 档位 1K/1.5K/2K（1.5K 与 1K 同价、效果更优）；
    输出 png；该模型不支持组图，不传 sequential_image_generation。
    """
    s = get_settings()
    key = s.ark_key_value
    if not key:
        raise ArkImageError("未配置 Ark API Key")
    b64 = bool(getattr(s, "ark_image_b64", False))
    payload = {
        "model": model or s.ark_image_model,
        "prompt": prompt,
        "size": size,
        "response_format": "b64_json" if b64 else "url",
        "output_format": "png",
        "watermark": False,
    }
    if images:
        payload["image"] = [
            "data:image/png;base64," + base64.b64encode(b).decode()
            for b in images
        ]
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as c:
            r = _post_generation(c, f"{s.ark_base_url}/images/generations",
                                 payload, {"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                try:
                    err = (r.json() or {}).get("error") or {}
                except Exception:
                    err = {}
                code = err.get("code") or str(r.status_code)
                hint = _HINTS.get(code)
                detail = (err.get("message") or "")[:160]
                raise ArkImageError(f"{hint}（{code}）" if hint
                                    else f"生图失败 {code}: {detail}")
            data = (r.json() or {}).get("data") or []
            first = data[0] if data else {}
            if b64:
                raw = first.get("b64_json")
                if not raw:
                    raise ArkImageError("生图响应缺少图片数据（b64_json）")
                return base64.b64decode(raw)
            url = first.get("url")
            if not url:
                raise ArkImageError("生图响应缺少图片 URL")
            return _fetch_result(c, url)
    except httpx.HTTPError as e:
        raise ArkImageError(f"生图网络错误: {e}") from e
