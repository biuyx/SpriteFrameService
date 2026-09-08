"""新角色首帧生成编排：立绘 + 参考首帧 → Seedream 生图 → 物化为动作首帧。

参考图约定（与提示词严格对应）：图1 = 参考首帧集中同 key 的图（动作姿势来源），
图2 = 新角色立绘（样貌来源；key 含 back 用背面，否则正面）。
"""
from __future__ import annotations

import threading
from typing import Optional, Tuple

from app.services.ffset_store import ffset_store
from app.services.sprite_store import sprite_store
from app.services.template_store import template_store

DEFAULT_PROMPT = (
    "角色样貌完全跟随第二张参考图。身体姿势完全照搬第一张参考图的动作，"
    "纯色灰色背景，完整全身出镜，禁止修改角色外貌服饰，只迁移动作姿态。"
    "身体姿势完全照搬第一张参考图的动作。完整复刻图一动作姿势。"
)

# 生图并发闸门（秒级请求，小并发即可跑满）
_gate = threading.Semaphore(3)


def action_key(action: dict) -> str:
    """动作对应的参考 key：绑定模板的 key 优先，其次动作名。"""
    tpl = template_store.get(action.get("template_id") or "")
    return (tpl or {}).get("key") or (action.get("name") or "").strip()


def resolve_refs(sprite_id: str, action: dict,
                 set_id: str) -> Tuple[bytes, bytes, str, str]:
    """解析生成所需的两张参考图。返回 (立绘, 参考首帧, key, 朝向)。

    失败抛 ValueError，消息即跳过原因。
    """
    key = action_key(action)
    if not key:
        raise ValueError("无法确定动作 key")
    frame_p = ffset_store.frame_path(set_id, key)
    if frame_p is None:
        raise ValueError(f"参考集缺少动作 {key}")

    role = "back" if "back" in key.lower() else "front"
    refs = sprite_store.list_refs(sprite_id)
    pick = next((r for r in refs if (r.get("role") or "") == role), None)
    if pick is None and role == "back":
        pick = next((r for r in refs if (r.get("role") or "") == "front"), None)
    if pick is None:
        raise ValueError("缺少立绘——在「首帧图库」上传并标记 正面/背面")
    art_p = sprite_store.ref_path(sprite_id, pick)
    if not art_p.is_file():
        raise ValueError("立绘文件缺失")
    return art_p.read_bytes(), frame_p.read_bytes(), key, role


def run_gen_first_frame(sprite_id: str, action_id: str, set_id: str,
                        prompt: Optional[str], ctx) -> dict:
    """生成单个动作的首帧（跑在 io 池；按张计费）。"""
    import cv2
    import numpy as np

    from app.core.ark_image import ArkImageError, generate_image

    action = sprite_store.get_action(sprite_id, action_id)
    ctx.report(5, "准备参考图...")
    art, ref, key, role = resolve_refs(sprite_id, action, set_id)

    ctx.report(15, "生图中（Seedream）...")
    with _gate:
        if ctx.cancelled():
            raise RuntimeError("已取消")
        try:
            # 图序与提示词对应：图1=姿势参考，图2=角色立绘
            data = generate_image(prompt or DEFAULT_PROMPT, [ref, art])
        except ArkImageError as e:
            raise RuntimeError(str(e))

    ctx.report(85, "落盘...")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError("生成结果不是可识别的图片")
    dest = sprite_store.action_dir(sprite_id, action_id) / "first_frame.png"
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("图片编码失败")
    dest.write_bytes(buf.tobytes())

    sprite_store.update_action(sprite_id, action_id, {
        "first_frame": {"kind": "ai_generated", "file": "first_frame.png",
                        "ref_set": set_id, "ref_key": key, "role": role},
    })
    ctx.report(100, f"首帧已生成（参考 {key}）")
    return {"action_id": action_id, "key": key, "bytes": len(data)}
