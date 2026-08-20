"""阿里云 OSS 上传（标准库实现，签名逻辑取自 seedance-video-gen skill）。

用途：Ark 的参考媒体（reference_video/reference_image）只接受公网 URL，
不接受 base64 内嵌——生成前需把本地媒体上传 OSS 换取 URL。

凭证与配置读取顺序：backend/.env 显式配置（设置面板写入）优先，
回退系统环境变量（与 seedance skill 的既有流程兼容）：
    OSS_BUCKET           bucket 名
    OSS_PUBLIC_DOMAIN    绑定的公网域名（同时用作上传域名，除非设了 ENDPOINT）
    OSS_ENDPOINT         可选，公网域名不能收签名 PUT 时使用
    ALIBABA_CLOUD_ACCESS_KEY_ID / _SECRET（或 OSS_ACCESS_KEY_ID / _SECRET）

AK/SK 只在请求签名时读取，绝不进日志、异常或任何接口返回。
"""
from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import hmac
import os
import re
import urllib.parse

import httpx

from app.config import _BACKEND_DIR

_ENV_FILE = _BACKEND_DIR / ".env"


class OssError(Exception):
    """OSS 配置缺失或上传失败（信息不含密钥）。"""


def _env_file_values() -> dict:
    """读取 backend/.env（每次现读，设置面板保存后立即生效）。"""
    try:
        result = {}
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
        return result
    except OSError:
        return {}


def _env(*names: str) -> str:
    """backend/.env 显式配置优先，回退系统环境变量。"""
    file_vals = _env_file_values()
    for n in names:
        v = file_vals.get(n, "").strip()
        if v:
            return v
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


def oss_configured() -> bool:
    return bool(_env("OSS_BUCKET") and _env("OSS_PUBLIC_DOMAIN")
                and _env("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_ID", "OSS_ACCESS_KEY_ID")
                and _env("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALIYUN_ACCESS_KEY_SECRET", "OSS_ACCESS_KEY_SECRET"))


def _safe_key(key: str) -> str:
    parts = []
    for part in key.replace("\\", "/").strip("/").split("/"):
        part = re.sub(r"[^A-Za-z0-9._-]+", "-", part.strip()).strip(".-_")
        if part:
            parts.append(part)
    if not parts:
        raise OssError("非法的对象键")
    return "/".join(parts)


def _upload_base(bucket: str) -> str:
    endpoint = _env("OSS_ENDPOINT")
    if endpoint:
        if not endpoint.startswith(("http://", "https://")):
            endpoint = "https://" + endpoint
        p = urllib.parse.urlparse(endpoint)
        host = p.netloc
        if not host.startswith(bucket + "."):
            host = f"{bucket}.{host}"
        return f"{p.scheme}://{host}"
    return _env("OSS_PUBLIC_DOMAIN").rstrip("/")


def upload_bytes(data: bytes, key: str, content_type: str) -> str:
    """上传字节到 OSS，返回公网 URL。"""
    bucket = _env("OSS_BUCKET")
    public_domain = _env("OSS_PUBLIC_DOMAIN").rstrip("/")
    ak = _env("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_ID", "OSS_ACCESS_KEY_ID")
    sk = _env("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALIYUN_ACCESS_KEY_SECRET", "OSS_ACCESS_KEY_SECRET")
    if not (bucket and public_domain and ak and sk):
        raise OssError(
            "未配置 OSS（参考媒体需要公网 URL）。请设置环境变量 "
            "OSS_BUCKET、OSS_PUBLIC_DOMAIN、ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET")

    key = _safe_key(key)
    content_md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
    date_header = email.utils.format_datetime(
        dt.datetime.now(dt.timezone.utc), usegmt=True)

    string_to_sign = "\n".join(["PUT", content_md5, content_type, date_header,
                                f"/{bucket}/{key}"])
    signature = base64.b64encode(
        hmac.new(sk.encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode("ascii")

    url = _upload_base(bucket) + "/" + urllib.parse.quote(key, safe="/._-")
    try:
        r = httpx.put(url, content=data, timeout=httpx.Timeout(30.0, write=300.0),
                      headers={
                          "Authorization": f"OSS {ak}:{signature}",
                          "Content-Type": content_type,
                          "Content-MD5": content_md5,
                          "Date": date_header,
                      })
    except httpx.HTTPError as e:
        raise OssError(f"OSS 上传失败: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise OssError(f"OSS 上传失败: HTTP {r.status_code}: {r.text[:150]}")

    return public_domain + "/" + urllib.parse.quote(key, safe="/._-")
