"""应用配置 - 使用 pydantic-settings 管理，全部路径跨平台。"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _exe_name(basename: str) -> str:
    """Windows 下补 .exe，Linux/macOS 保持裸名。"""
    return f"{basename}.exe" if _is_windows() else basename


_BACKEND_DIR = Path(__file__).resolve().parent.parent  # backend/
_ENV_FILES = [
    _BACKEND_DIR / ".env",
    _BACKEND_DIR.parent / ".env",
]


class Settings(BaseSettings):
    """服务设置。

    环境变量（backend/.env 可覆盖）：
        SPRITE_DATA_DIR     运行时数据目录（sessions/ 存放于此）
        SPRITE_MODELS_DIR   模型根目录（默认 <project_root>/models）
        SPRITE_TOOLS_DIR    外部二进制目录（pngquant、realesrgan）
        SPRITE_HOST / SPRITE_PORT
        SPRITE_FORCE_CPU    强制 CPU（跳过 GPU provider）
        SPRITE_MAX_WORKERS  Job 线程池并发数
        SPRITE_FRONTEND_DIR 前端 dist 目录（默认 <project_root>/frontend/dist）
    """

    model_config = SettingsConfigDict(
        env_prefix="SPRITE_",
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 目录 ---
    data_dir: Path = Path("data")
    models_dir: Path | None = None
    tools_dir: Path | None = None
    frontend_dir: Path | None = None

    # --- 服务 ---
    host: str = "127.0.0.1"
    port: int = 8000
    max_workers: int = 2
    cors_origins: str = "*"          # 逗号分隔的允许来源，"*" 为不限制
    debug_errors: bool = False       # 任务失败时是否把完整 traceback 返回给前端

    # --- 认证（默认关闭；设置 token 后自动启用） ---
    auth_token: str = ""             # 访问令牌，留空表示不启用认证
    auth_session_hours: int = 720    # 登录态有效期（小时），默认 30 天
    auth_cookie_secure: str = "auto" # Cookie Secure 标志：auto/true/false

    # --- Ark 视频生成（Seedance）---
    ark_api_key: str = ""            # 留空则回退环境变量 ARK_API_KEY；都空 = 功能置灰
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    # 可选模型（id:标签，分号分隔）。ID 均经模型列表接口/历史请求核实：
    # 2.0 完整版没有 -pro- 后缀，就叫 doubao-seedance-2-0-260128
    ark_models: str = ("doubao-seedance-2-0-mini-260615:2.0 Mini（默认，成本最低）;"
                       "doubao-seedance-2-0-fast-260128:2.0 Fast;"
                       "doubao-seedance-2-0-260128:2.0 Pro（完整版）")
    ark_model: str = "doubao-seedance-2-0-mini-260615"   # 默认模型
    ark_timeout_seconds: int = 900   # 单次生成总超时
    generate_daily_limit: int = 20   # 每日生成次数上限，0 = 不限制

    # --- 处理 ---
    force_cpu: bool = False
    realesrgan_tile: int = 0
    max_upload_mb: int = 2048        # 单个视频上传大小上限（MB）
    max_extract_frames: int = 2000   # 单次抽帧的帧数上限
    allow_model_download: bool = False  # 允许 RTMPose 在本地模型缺失时联网下载

    @property
    def ark_key_value(self) -> str:
        """Ark 访问密钥：显式配置优先，回退通用环境变量 ARK_API_KEY。"""
        return self.ark_api_key.strip() or os.environ.get("ARK_API_KEY", "").strip()

    @property
    def ark_models_list(self) -> list[dict]:
        """解析可选模型：[{"id": ..., "label": ...}]。"""
        result = []
        for item in self.ark_models.split(";"):
            item = item.strip()
            if not item:
                continue
            mid, _, label = item.partition(":")
            result.append({"id": mid.strip(), "label": label.strip() or mid.strip()})
        return result

    @property
    def generate_enabled(self) -> bool:
        return bool(self.ark_key_value)

    @property
    def auth_enabled(self) -> bool:
        """是否启用认证（配置了非空 token 即启用）。"""
        return bool(self.auth_token.strip())

    @property
    def auth_token_value(self) -> str:
        return self.auth_token.strip()

    @property
    def cors_origins_list(self) -> list[str]:
        """解析 CORS 来源配置。"""
        raw = (self.cors_origins or "*").strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()] or ["*"]

    @property
    def project_root(self) -> Path:
        """后端项目根目录（backend/ 的上层）。"""
        return Path(__file__).resolve().parent.parent.parent

    @property
    def sessions_dir(self) -> Path:
        """会话目录（基于解析后的 data_dir，跨 CWD 稳定）。"""
        return self.resolved_data_dir / "sessions"

    def _resolve(self, p: Path | str | None, default: Path) -> Path:
        p = Path(p).expanduser() if p else default
        return p if p.is_absolute() else self.project_root / p

    @property
    def resolved_data_dir(self) -> Path:
        return self._resolve(self.data_dir, self.project_root / "data")

    @property
    def resolved_models_dir(self) -> Path:
        default = self.project_root / "models"
        return self._resolve(self.models_dir, default)

    @property
    def resolved_tools_dir(self) -> Path:
        default = self.project_root / "tools"
        return self._resolve(self.tools_dir, default)

    @property
    def resolved_frontend_dir(self) -> Path:
        default = self.project_root / "frontend" / "dist"
        return self._resolve(self.frontend_dir, default)

    # --- 外部二进制（跨平台） ---
    @property
    def pngquant_path(self) -> Path | None:
        p = self.resolved_tools_dir / "pngquant" / _exe_name("pngquant")
        return p if p.exists() else None

    @property
    def realesrgan_exe(self) -> Path | None:
        """realesrgan-ncnn-vulkan 可执行文件。"""
        p = self.resolved_models_dir / "realesrgan" / _exe_name("realesrgan-ncnn-vulkan")
        return p if p.exists() else None

    @property
    def realesrgan_models_dir(self) -> Path | None:
        p = self.resolved_models_dir / "realesrgan" / "models"
        return p if p.exists() else None

    # --- 全局单例目录初始化 ---
    def ensure_dirs(self) -> None:
        self.resolved_data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
