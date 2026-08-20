"""运行时设置 API：Ark 密钥与生成并发数，写入 backend/.env 并热生效。

安全约定：
- 密钥读取永远打码返回（前 4 + 后 4），完整值只写不读
- 端点在认证中间件之内（/api/*），未登录不可访问
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings, _BACKEND_DIR

router = APIRouter(prefix="/settings", tags=["settings"])

_ENV_PATH = _BACKEND_DIR / ".env"


def _update_env_file(updates: dict) -> None:
    """更新 backend/.env 的键值（保留其余行与注释；缺失的键追加到末尾）。"""
    lines = _ENV_PATH.read_text(encoding="utf-8").splitlines() if _ENV_PATH.is_file() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        replaced = False
        for k in list(remaining.keys()):
            if stripped.startswith(f"{k}="):
                out.append(f"{k}={remaining.pop(k)}")
                replaced = True
                break
        if not replaced:
            out.append(line)
    for k, v in remaining.items():
        out.append(f"{k}={v}")
    tmp = _ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(_ENV_PATH)


def _reload_settings() -> None:
    """让 .env 修改立即生效（Settings 是 lru_cache 单例）。"""
    get_settings.cache_clear()


def _mask(key: str) -> Optional[str]:
    if not key:
        return None
    if len(key) <= 8:
        return "■" * len(key)
    return f"{key[:4]}…{key[-4:]}"


class SettingsPatch(BaseModel):
    ark_api_key: Optional[str] = Field(
        default=None, description="Ark 密钥；空字符串 = 清除文件中的配置")
    generate_max_concurrent: Optional[int] = Field(default=None, ge=1, le=10)
    # OSS（参考媒体中转）。空字符串 = 清除文件配置，回退系统环境变量
    oss_bucket: Optional[str] = None
    oss_public_domain: Optional[str] = None
    oss_endpoint: Optional[str] = None
    oss_access_key_id: Optional[str] = None
    oss_access_key_secret: Optional[str] = None


# 设置面板写入 .env 时使用的 OSS 键（与环境变量同名，oss_uploader 文件优先读取）
_OSS_ENV_KEYS = ["OSS_BUCKET", "OSS_PUBLIC_DOMAIN", "OSS_ENDPOINT",
                 "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"]


def _oss_state() -> dict:
    from app.core.oss_uploader import _env, _env_file_values, oss_configured
    file_vals = _env_file_values()
    return {
        "configured": oss_configured(),
        "bucket": _env("OSS_BUCKET") or None,
        "public_domain": _env("OSS_PUBLIC_DOMAIN") or None,
        "endpoint": _env("OSS_ENDPOINT") or None,
        "access_key_id_masked": _mask(_env(
            "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_ID", "OSS_ACCESS_KEY_ID")),
        "secret_set": bool(_env(
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALIYUN_ACCESS_KEY_SECRET",
            "OSS_ACCESS_KEY_SECRET")),
        # backend/.env 中显式配置了哪些键（决定“配置文件”标记与清除按钮）
        "from_file": [k for k in _OSS_ENV_KEYS if file_vals.get(k, "").strip()],
    }


@router.get("")
def get_runtime_settings():
    s = get_settings()
    key = s.ark_key_value
    return {
        "ark_api_key_masked": _mask(key),
        # 来源：file = backend/.env 显式配置；env = 系统环境变量 ARK_API_KEY
        "ark_api_key_source": ("file" if s.ark_api_key.strip()
                               else ("env" if key else None)),
        "generate_max_concurrent": s.generate_max_concurrent,
        "oss": _oss_state(),
    }


@router.put("")
def put_runtime_settings(req: SettingsPatch):
    updates = {}
    if req.ark_api_key is not None:
        key = req.ark_api_key.strip()
        if key and (len(key) < 20 or " " in key):
            raise HTTPException(status_code=400, detail="密钥格式不正确")
        updates["SPRITE_ARK_API_KEY"] = key   # 空串即清除（回退环境变量）
    if req.generate_max_concurrent is not None:
        updates["SPRITE_GENERATE_MAX_CONCURRENT"] = str(req.generate_max_concurrent)

    # ---- OSS ----
    if req.oss_public_domain is not None:
        domain = req.oss_public_domain.strip().rstrip("/")
        if domain and not domain.startswith(("http://", "https://")):
            raise HTTPException(status_code=400,
                                detail="公网域名需以 http:// 或 https:// 开头")
        updates["OSS_PUBLIC_DOMAIN"] = domain
    if req.oss_bucket is not None:
        bucket = req.oss_bucket.strip()
        if bucket and not bucket.replace("-", "").isalnum():
            raise HTTPException(status_code=400, detail="Bucket 名称格式不正确")
        updates["OSS_BUCKET"] = bucket
    if req.oss_endpoint is not None:
        updates["OSS_ENDPOINT"] = req.oss_endpoint.strip()
    if req.oss_access_key_id is not None:
        ak = req.oss_access_key_id.strip()
        if ak and (len(ak) < 10 or " " in ak):
            raise HTTPException(status_code=400, detail="AccessKey ID 格式不正确")
        updates["OSS_ACCESS_KEY_ID"] = ak
    if req.oss_access_key_secret is not None:
        sk = req.oss_access_key_secret.strip()
        if sk and (len(sk) < 10 or " " in sk):
            raise HTTPException(status_code=400, detail="AccessKey Secret 格式不正确")
        updates["OSS_ACCESS_KEY_SECRET"] = sk

    if not updates:
        raise HTTPException(status_code=400, detail="没有要更新的设置")

    _update_env_file(updates)
    _reload_settings()
    return get_runtime_settings()
