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


def _summarize_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") or data
        msg = err.get("message") or str(err)
    except Exception:
        msg = resp.text[:200]
    lower = msg.lower()
    for key, hint in _ERROR_HINTS:
        if key in lower:
            return f"{hint}（HTTP {resp.status_code}: {msg[:160]}）"
    return f"HTTP {resp.status_code}: {msg[:300]}"


class ArkClient:
    def __init__(self):
        settings = get_settings()
        key = settings.ark_key_value
        if not key:
            raise ArkError("未配置 Ark API Key（SPRITE_ARK_API_KEY 或环境变量 ARK_API_KEY）")
        self._base = settings.ark_base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, read=60.0),
            headers={"Authorization": f"Bearer {key}"},
        )

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
            raise ArkError(f"创建任务失败: {_summarize_error(r)}")
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
