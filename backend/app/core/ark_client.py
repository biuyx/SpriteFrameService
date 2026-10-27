"""火山方舟 Ark 视频生成客户端（Seedance 内容生成任务）。

接口形态以本机历史真实请求/响应为准（video-src/*.request.json）：

    POST {base}/contents/generations/tasks
        {"model": "...", "content": [{type:text},{type:image_url,role:first_frame}],
         "ratio": "1:1", "duration": 5, "watermark": false, "generate_audio": false}
        → {"id": "cgt-..."}
    GET  {base}/contents/generations/tasks/{id}
        → {"status": queued|running|succeeded|failed,
           "content": {"video_url": ...}, "usage": {...}, "seed": ..., ...}

密钥只进请求头，绝不进日志与异常信息。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class ArkError(Exception):
    """Ark 调用失败（异常信息不含密钥）。"""


# 常见错误的中文翻译（按消息关键词匹配）
_ERROR_HINTS = [
    ("overdue balance", "火山账户欠费，请到火山引擎控制台充值后重试"),
    ("insufficient balance", "火山账户余额不足，请充值后重试"),
    ("quota", "账户配额不足或已达上限，请到火山控制台确认"),
    ("rate limit", "请求过于频繁（触发平台限流），请稍后重试"),
    ("model not found", "模型不存在或未开通，请到方舟控制台确认该模型已开通"),
    ("sensitive", "内容未通过平台审核，请调整提示词或参考图"),
]


def friendly_ark_error(message: str) -> str:
    """翻译新错误与历史素材错误；不改写落盘的原始记录。"""
    lower = message.lower()
    pixel_limit = re.search(
        r"video pixel count[^:]*?must be (greater than or equal to|less than or equal to)\s+(\d+)",
        lower,
    )
    if pixel_limit:
        relation, count = pixel_limit.groups()
        if relation == "greater than or equal to":
            return (f"参考视频分辨率过低：宽×高至少需要 {int(count):,} 像素。"
                    "请放大或更换参考视频后重试；修改生成结果的分辨率无效。")
        return (f"参考视频分辨率过高：宽×高不能超过 {int(count):,} 像素。"
                "请缩小或更换参考视频后重试。")
    for key, hint in _ERROR_HINTS:
        if key in lower:
            return hint
    return message


def _summarize_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") or data
        msg = err.get("message") or str(err)
    except Exception:
        msg = resp.text[:200]
    # 用户看原因和下一步；完整上游错误（含 request id）留在服务日志里排查。
    logger.warning("Ark 接口错误（HTTP %s）：%s", resp.status_code, msg)
    hint = friendly_ark_error(msg)
    if hint != msg:
        return f"{hint}（HTTP {resp.status_code}）"
    return f"HTTP {resp.status_code}: {msg[:300]}"


class ArkClient:
    """方舟视频接口客户端。默认走国内接入点，也可显式指定 base_url/api_key。"""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        settings = get_settings()
        base, key = settings.ark_endpoint("ark")
        if base_url:
            base = base_url.rstrip("/")
        if api_key:
            key = api_key
        if not key:
            raise ArkError("未配置 Ark API Key（SPRITE_ARK_API_KEY 或环境变量 ARK_API_KEY）")
        self._base = base
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, read=60.0),
            headers={"Authorization": f"Bearer {key}"},
        )

    @classmethod
    def for_model(cls, model: Optional[str]) -> "ArkClient":
        """按模型选接入点：亚太（无预过滤）的 ep- 模型走 byteplus。"""
        settings = get_settings()
        provider = settings.ark_provider_for(model)
        base, key = settings.ark_endpoint(provider)
        if provider == "byteplus" and not key:
            raise ArkError(
                "这个模型走亚太接入点，但没配 SPRITE_BYTEPLUS_API_KEY；"
                "请到「设置」里填，或换用国内模型")
        return cls(base_url=base, api_key=key)

    def close(self):
        self._client.close()

    # ------------------------------------------------------------ 任务
    def create_task(self, model: str, content: list, **top_params) -> dict:
        """创建生成任务，返回 {"id": "cgt-..."}。"""
        body = {"model": model, "content": content}
        for k, v in top_params.items():
            if v is not None:
                body[k] = v
        r = self._client.post(f"{self._base}/contents/generations/tasks", json=body)
        if r.status_code >= 400:
            exc = ArkError(f"创建任务失败: {_summarize_error(r)}")
            exc.request_rejected = r.status_code in (400, 401, 403, 404, 405, 413, 415, 422, 429)
            raise exc
        data = r.json()
        if not data.get("id"):
            raise ArkError(f"创建任务响应缺少 id: {str(data)[:200]}")
        return data

    def get_task(self, task_id: str) -> dict:
        r = self._client.get(f"{self._base}/contents/generations/tasks/{task_id}")
        if r.status_code >= 400:
            raise ArkError(f"查询任务失败: {_summarize_error(r)}")
        return r.json()

    def cancel_task(self, task_id: str) -> bool:
        """尝试取消远端任务。端点未在文档确认，失败静默返回 False。"""
        try:
            r = self._client.delete(f"{self._base}/contents/generations/tasks/{task_id}")
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------ 结果
    def download(self, url: str, dest: Path, max_bytes: int = 500 * 1024 * 1024) -> int:
        """流式下载视频到 dest（video_url 是带签名的临时链接，成功即转存）。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        written = 0
        try:
            # 结果在对象存储上，不带鉴权头（签名在 URL 里）
            with httpx.Client(timeout=httpx.Timeout(30.0, read=120.0)) as dl:
                with dl.stream("GET", url) as r:
                    if r.status_code >= 400:
                        raise ArkError(f"下载视频失败: HTTP {r.status_code}")
                    with tmp.open("wb") as fh:
                        for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                            written += len(chunk)
                            if written > max_bytes:
                                raise ArkError("视频超出大小上限")
                            fh.write(chunk)
            tmp.replace(dest)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return written
