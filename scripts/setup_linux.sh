#!/usr/bin/env bash
# SpriteFrameService - Linux 环境初始化
set -e
cd "$(dirname "$0")/.."

echo "=========================================="
echo "  SpriteFrameService - Linux 初始化"
echo "=========================================="

PYTHON="${PYTHON:-python3}"

# 1. Python 虚拟环境
if [ ! -d ".venv" ]; then
  echo "[1/4] 创建 Python 虚拟环境 (.venv) ..."
  "$PYTHON" -m venv .venv
fi

# 2. 后端依赖
echo "[2/4] 安装后端依赖 ..."
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r backend/requirements.txt

# GPU 服务器可选：onnxruntime-gpu（需先装 NVIDIA 驱动，见 README「Linux GPU」）
#   SPRITE_GPU=1         新卡（Turing 及以上）：装最新版
#   SPRITE_GPU=pascal    Tesla P4/P40、GTX 10 系等 Pascal 卡：必须用钉死的组合，
#                        装最新版会在第一个卷积上直接失败（详见 requirements-gpu-pascal.txt）
if [ -n "$SPRITE_GPU" ]; then
  echo "      安装 onnxruntime-gpu（$SPRITE_GPU）..."
  .venv/bin/python -m pip uninstall -y onnxruntime onnxruntime-gpu
  if [ "$SPRITE_GPU" = "pascal" ]; then
    .venv/bin/python -m pip install -r backend/requirements-gpu-pascal.txt
  else
    .venv/bin/python -m pip install onnxruntime-gpu "nvidia-cudnn-cu12==9.*"
  fi
  .venv/bin/python -m pip install nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 \
    nvidia-cufft-cu12 nvidia-curand-cu12
  # CUDA 运行库是 pip 装进 venv 的，动态链接器默认找不到，登记一下
  SP=$(.venv/bin/python -c "import site; print(site.getsitepackages()[0])")
  ls -d "$SP"/nvidia/*/lib | sudo tee /etc/ld.so.conf.d/sprite-venv-cuda.conf >/dev/null
  sudo ldconfig
  .venv/bin/python -c "import onnxruntime as o; print('      providers:', o.get_available_providers())"
fi

# 3. 前端构建（需要 Node.js；如无则跳过，直接拷贝 dist）
if command -v npm >/dev/null 2>&1; then
  echo "[3/4] 构建前端 ..."
  ( cd frontend && npm install && npm run build )
else
  echo "[3/4] 未找到 npm，跳过前端构建。请将前端 dist 目录拷贝到 frontend/dist。"
fi

# 4. 模型目录检查
echo "[4/4] 检查模型目录 ..."
if [ ! -f "models/u2net.onnx" ] && [ -z "$SPRITE_MODELS_DIR" ]; then
  echo "      警告：未找到 models/ 目录。请放置抠图/姿势/RealESRGAN 模型，"
  echo "      或在 backend/.env 中设置 SPRITE_MODELS_DIR。"
fi

# 时区：下载包文件名里的时间戳按服务器本地时间生成，云镜像默认 UTC 会差 8 小时。
# 只在当前是 UTC 时改；SPRITE_TZ=keep 跳过，或用 SPRITE_TZ 指定别的时区
TZ_WANT="${SPRITE_TZ:-Asia/Shanghai}"
if [ "$TZ_WANT" != "keep" ] && command -v timedatectl >/dev/null; then
  TZ_NOW=$(timedatectl show -p Timezone --value 2>/dev/null)
  if [ "$TZ_NOW" = "Etc/UTC" ] || [ "$TZ_NOW" = "UTC" ]; then
    echo "      时区 $TZ_NOW → $TZ_WANT（改完需重启服务才生效）"
    sudo timedatectl set-timezone "$TZ_WANT"
  fi
fi

echo ""
echo "初始化完成。运行 scripts/run_linux.sh 启动服务。"
