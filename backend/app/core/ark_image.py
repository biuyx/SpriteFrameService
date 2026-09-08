"""Ark 生图（Doubao-Seedream）：同步单请求，返回图片字节。

与视频生成共用 base_url 与 API Key；参考图以 base64 data URL 传入
（生图接口支持 base64，区别于视频参考只收公网 URL）。
"""
from __future__ import annotations

import base64
from typing import List, Optional

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
    payload = {
        "model": model or s.ark_image_model,
        "prompt": prompt,
        "size": size,
        "response_format": "url",
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
            r = c.post(f"{s.ark_base_url}/images/generations", json=payload,
                       headers={"Authorization": f"Bearer {key}"})
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
            url = data[0].get("url") if data else None
            if not url:
                raise ArkImageError("生图响应缺少图片 URL")
            img = c.get(url)
            img.raise_for_status()
            return img.content
    except httpx.HTTPError as e:
        raise ArkImageError(f"生图网络错误: {e}") from e
