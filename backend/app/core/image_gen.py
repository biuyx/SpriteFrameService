"""首帧生图的通道分发：方舟（Seedream）或 weimeta 网关（gpt-image-2.5 家族）。

两条通道都是直连上游，不经过任何中间服务。

选择规则：
  · 请求里给了模型 → 按模型归属判断（在哪边的模型清单里就走哪边）
  · 没给 → 看 SPRITE_IMAGE_PROVIDER：ark / weimeta 强制；auto（默认）在
    网关密钥配好时优先用它，否则回退方舟

两条通道的差异收在这里：方舟收 base64 参考图，网关只收公网 URL。
"""
from __future__ import annotations

from typing import Callable, List, Optional

from app.config import get_settings


class ImageGenError(RuntimeError):
    """生图失败（消息可直接展示）。"""


def provider_for(model: Optional[str]) -> str:
    """该模型走哪条通道。"""
    s = get_settings()
    model = (model or "").strip()
    if model:
        if any(m["id"] == model for m in s.weimeta_image_models_list):
            return "weimeta"
        if any(m["id"] == model for m in s.ark_image_models_list):
            return "ark"
    mode = (s.image_provider or "auto").strip().lower()
    if mode in ("ark", "weimeta"):
        return mode
    return "weimeta" if s.weimeta_configured else "ark"


def resolve_model(model: Optional[str]) -> tuple[str, str]:
    """返回 (provider, 实际模型 id)。"""
    s = get_settings()
    provider = provider_for(model)
    model = (model or "").strip()
    if model:
        return provider, model
    if provider == "weimeta":
        return provider, s.weimeta_image_model.strip() or s.ark_image_model
    return provider, s.ark_image_model


def generate(prompt: str, images: Optional[List[bytes]] = None, *,
             model: Optional[str] = None,
             progress: Optional[Callable[[float, str], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None) -> bytes:
    """生成一张图，返回图片字节。images 顺序即提示词里的图1、图2…"""
    provider, mid = resolve_model(model)
    if provider == "weimeta":
        from app.core.weimeta_image import WeimetaError, generate_image
        try:
            return generate_image(prompt, images, model=mid,
                                  progress=progress, cancelled=cancelled)
        except WeimetaError as e:
            raise ImageGenError(str(e)) from e

    from app.core.ark_image import ArkImageError, generate_image as ark_generate
    try:
        return ark_generate(prompt, images, model=mid)
    except ArkImageError as e:
        raise ImageGenError(str(e)) from e
