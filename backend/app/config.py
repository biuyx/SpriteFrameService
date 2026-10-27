"""应用配置 - 使用 pydantic-settings 管理，全部路径跨平台。"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


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

# 生图模型的可读名（配置里没写标签时用；认不出的就显示原始 id）
_IMAGE_MODEL_LABELS = {
    "doubao-seedream-5-0-pro-260628": "Seedream 5.0 Pro（方舟）",
    "doubao-seedream-4-5-250828": "Seedream 4.5（方舟）",
    "gpt-image-2.5-sunburst": "GPT-Image-2.5 Sunburst",
    "gpt-image-2.5": "GPT-Image-2.5",
    "gpt-image-2": "GPT-Image-2",
}


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
    # 本地处理的并发上限：cpu 池线程数，同时是 local_gate 的上限（两者取同一个值，
    # 流水线里的抽帧/抠图/导出与直接提交的本地任务共用它）。调大前先压测显存——
    # 抠图在 GPU 上跑，一台 P4（8G）同时几路会溢出。
    max_workers: int = 4
    export_workers: int = Field(default=1, ge=1, le=4)
    # 0 = 按物理内存的 85% 自动设置；CPU BRIA 按实测预留 16 GiB。
    local_memory_budget_mb: int = Field(default=0, ge=0)
    matting_memory_mb: int = Field(default=16384, ge=1)
    matting_threads: int = Field(default=4, ge=1, le=32)
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
    # 生图模型（首帧生成用；需在方舟控制台开通）。SPRITE_ARK_IMAGE_MODEL 可覆盖
    ark_image_model: str = "doubao-seedream-5-0-pro-260628"
    # 方舟可选生图模型（id:标签，分号分隔）；留空则只列 ark_image_model 一个
    ark_image_models: str = ""
    # 生图结果的取回方式：默认拿 URL 再下载；机器连不上结果服务器（对象存储/CDN）
    # 时置 1，改成让接口直接回 base64，省掉那一跳
    ark_image_b64: bool = False
    ark_timeout_seconds: int = 900   # 单次生成总超时

    # --- weimeta 生图网关（gpt-image-2.5 家族；直连上游，不经 LocalAI）---
    # 通道选择：ark = 只走方舟；weimeta = 只走网关；auto = 按模型 id 判断
    image_provider: str = "auto"
    weimeta_base_url: str = "https://api.weimeta.ai"
    weimeta_api_key: str = ""        # Bearer 密钥；留空则网关通道不可用
    # 可选生图模型（id:标签，分号分隔）。默认 gpt-image-2.5-sunburst
    weimeta_image_models: str = (
        "gpt-image-2.5-sunburst:GPT-Image-2.5 Sunburst（默认）;"
        "gpt-image-2.5:GPT-Image-2.5;"
        "gpt-image-2:GPT-Image-2（最稳）")
    weimeta_image_model: str = "gpt-image-2.5-sunburst"
    weimeta_timeout_seconds: int = 300   # 单张生图（含提交+轮询）总超时
    weimeta_poll_seconds: float = 2.0    # 轮询间隔

    generate_max_concurrent: int = 5  # 同时进行中的生成任务数上限（1~10）
    first_frame_max_concurrent: int = 3  # 同时进行中的首帧生图数上限（1~10）

    # --- 处理 ---
    force_cpu: bool = False
    warmup_model: bool = True        # 启动时后台预加载默认抠图模型（测试里关掉）
    mcp_enabled: bool = True         # /api/mcp：给 Claude Code 等 AI 客户端用（需装 mcp 包）
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
    def weimeta_configured(self) -> bool:
        """生图网关是否可用：地址和密钥都要有。"""
        return bool(self.weimeta_base_url.strip() and self.weimeta_api_key.strip())

    @property
    def weimeta_image_models_list(self) -> list[dict]:
        """网关可选生图模型：[{"id", "label", "provider"}]。"""
        return self._parse_models(self.weimeta_image_models, "weimeta")

    @property
    def ark_image_models_list(self) -> list[dict]:
        """方舟可选生图模型：默认只有 ark_image_model 一个，可用同格式追加。"""
        return self._parse_models(self.ark_image_models or self.ark_image_model, "ark")

    def _parse_models(self, spec: str, provider: str) -> list[dict]:
        result = []
        for item in (spec or "").split(";"):
            item = item.strip()
            if not item:
                continue
            mid, _, label = item.partition(":")
            mid = mid.strip()
            result.append({"id": mid,
                           "label": label.strip() or _IMAGE_MODEL_LABELS.get(mid, mid),
                           "provider": provider})
        return result

    @property
    def image_models_list(self) -> list[dict]:
        """界面可选的生图模型（带 provider 标记）。

        网关的模型只在配好密钥后才列出来——否则界面上能选却一定失败。
        """
        models = list(self.ark_image_models_list)
        if self.weimeta_configured:
            models += self.weimeta_image_models_list
        return models

    @property
    def default_image_model(self) -> str:
        """默认生图模型：网关可用时用它的默认（gpt-image-2.5-sunburst），否则回退方舟。"""
        if self.image_provider in ("weimeta", "auto") and self.weimeta_configured:
            return self.weimeta_image_model.strip() or self.ark_image_model
        return self.ark_image_model

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
