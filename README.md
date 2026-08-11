# SpriteFrameService · 精灵帧服务

基于 [SpriteFrameStudio（小猫学游戏）](https://github.com/game-cat/SpriteFrameStudio) 重构的服务化版本：以 **FastAPI 后端服务 + REST API + Vue3 前端页面** 形态提供，去掉原项目中的视频生成（i2v / SmoothMix）模块（将另行实现）。

- **部署目标**：Linux（venv + systemd）；开发可在 Windows 完成。
- **核心能力**：视频抽帧、AI/颜色抠图、姿势/轮廓/特征/区域SSIM 分析、去相似帧、找循环帧、循环过渡、首尾补帧、描边、缩放/裁剪/边缘优化、RealESRGAN 增强、魔棒编辑、精灵图/GIF/WebP/Godot 导出、历史撤销。

> ⚠️ 本项目基于 SpriteFrameStudio 开发，原作者：小猫学游戏，原项目协议 **CC BY 4.0**（可商用、可修改、需署名）。

---

## 架构

```
backend/                 FastAPI 后端（无 Qt 依赖）
  app/
    api/                  REST 路由：sessions/videos/frames/analysis/background/image/export/history/jobs/capabilities
    core/                 移植的核心模块（抽帧/抠图/姿势/导出/魔棒/历史）
    models/               pydantic 模型（帧/姿势/导出配置）
    services/             会话、帧落盘、后台任务管理
    utils/                image_utils（web）、pngquant（跨平台）
  run.py                  uvicorn 入口
  .env.example            环境配置模板
frontend/                Vue3 + Vite 前端（离线可用，产物 dist/ 由后端托管）
data/                    运行时数据（视频/帧/导出，可配 SPRITE_DATA_DIR）
models/                  模型目录（AI抠图/rtmpose/realesrgan，可配 SPRITE_MODELS_DIR）
tools/                   外部二进制（pngquant，可配 SPRITE_TOOLS_DIR）
scripts/                 初始化与启动脚本（Windows .bat / Linux .sh）
deploy/                  systemd 服务单元模板
rtmlib/                  RTMPose 推理库（与原项目同款，已随仓库分发，无需另行放置）
```

**服务模型**：每个会话（session）对应一个视频项目。视频与帧以文件形式存于 `data/sessions/{id}/`，处理任务（抽帧/抠图/分析/导出）在进程内线程池异步执行，前端轮询 `GET /api/jobs/{id}` 获取进度。

**持久化**：会话数据以文件为准，进程重启后首次访问该会话会自动从磁盘恢复（帧元数据、帧图像、视频）。关停服务不会删除任何数据；只有显式 `DELETE /api/sessions/{id}` 才会清除磁盘内容。

**并发**：同一会话的后台任务串行执行（会话级锁），不同会话之间并行，避免并发改写同一批帧。

---

## 快速开始

### Windows（开发）

```bat
:: 前置：Python 3.11~3.13、Node.js 18+
scripts\setup_windows.bat      :: 创建 .venv、装依赖、构建前端
scripts\run_windows.bat        :: 启动后端 http://127.0.0.1:8000
```

前端开发热更新（可选）：

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173，/api 代理到 8000
```

### Linux（部署）

```bash
# 前置：python3（3.11~3.13）、node（可选，用于构建前端）、模型文件
chmod +x scripts/*.sh
scripts/setup_linux.sh          # 建 .venv、装依赖、构建前端
scripts/run_linux.sh            # 启动（默认 0.0.0.0:8000）
```

**systemd 托管**（生产）：

```bash
# 复制 deploy/spriteframe.service，修改其中的项目路径后：
sudo cp deploy/spriteframe.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spriteframe
```

**Nginx 反代示例**（可选）：

```nginx
server {
    listen 80;
    server_name sprite.example.com;
    client_max_body_size 4g;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

> ⚠️ **对外暴露前须知**：认证**默认关闭**。未设置 `SPRITE_AUTH_TOKEN` 时任何能访问到端口的人都可以创建/删除会话、上传视频、提交计算任务。对外提供时请：
>
> - 设置 `SPRITE_AUTH_TOKEN` 启用认证（见下节），或在反代层加认证；
> - 把 `SPRITE_CORS_ORIGINS` 收敛为具体来源，不要保留 `*`；
> - 保持 `SPRITE_DEBUG_ERRORS=false`（否则任务错误会返回服务端路径与堆栈）；
> - 按机器实际容量下调 `SPRITE_MAX_UPLOAD_MB` 与 `SPRITE_MAX_EXTRACT_FRAMES`，并让 Nginx 的 `client_max_body_size` 与前者一致；
> - 尽量走 HTTPS——令牌与 Cookie 在明文 HTTP 上会被中间人截获。

---

## 内网访问

默认只监听 `127.0.0.1`（仅本机）。要让局域网内其他机器访问，需要三步——**缺一不可**：

**1. 改监听地址**（`backend/.env`）：

```ini
SPRITE_HOST=0.0.0.0
SPRITE_PORT=8088
```

`0.0.0.0` 表示监听所有网卡，本机 `127.0.0.1` 与内网地址都能访问。

**2. 开启认证**——内网暴露必须做，否则同网段任何人都能读写你的数据：

```ini
SPRITE_AUTH_TOKEN=<随机令牌>
# 内网走明文 HTTP，Cookie 不能带 Secure，否则浏览器拒绝保存
SPRITE_AUTH_COOKIE_SECURE=false
```

**3. 放行防火墙**（Windows，需管理员权限）。按「专用」网络和具体网段收敛，
避免把服务暴露到 VPN / 代理虚拟网卡上：

```powershell
New-NetFirewallRule -DisplayName "SpriteFrameService (LAN)" -Direction Inbound -Protocol TCP -LocalPort 8088 -Action Allow -Profile Private -RemoteAddress 192.168.2.0/24
```

Linux（ufw）对应：

```bash
sudo ufw allow from 192.168.2.0/24 to any port 8088 proto tcp
```

完成后同事用 `http://<你的内网IP>:8088` 访问，输入令牌即可。前端是同源托管的，
不需要额外配置 CORS。

> ⚠️ 内网是明文 HTTP，令牌与 Cookie 在网段内可被抓包。若网络环境不可信，
> 在前面挂一层 HTTPS 反代，并把 `SPRITE_AUTH_COOKIE_SECURE` 改回 `auto`。

---

## 访问认证

**默认关闭**：不设置 `SPRITE_AUTH_TOKEN` 时行为与不带认证完全一致，本机 `127.0.0.1` 自用不受影响。

启用只需配置一个令牌：

```bash
# 生成一个随机令牌
python -c "import secrets; print(secrets.token_urlsafe(32))"
# 写入 backend/.env
SPRITE_AUTH_TOKEN=<上一步生成的令牌>
```

启用后：

| 使用方式 | 认证方式 |
| ---- | ---- |
| 浏览器 | 打开页面出现登录页，输入令牌后下发 HttpOnly Cookie，后续自动携带；右上角可「退出登录」 |
| 脚本 / CLI | 请求头 `Authorization: Bearer <token>` |
| 自带测试脚本 | 读取环境变量 `SPRITE_AUTH_TOKEN`，无需改动 |

```bash
curl -H "Authorization: Bearer $SPRITE_AUTH_TOKEN" http://127.0.0.1:8000/api/capabilities
```

**为什么同时用 Cookie 和 Header**：前端有大量 `<img src>` / `<video src>` / 下载链接直接指向 API（帧图像、姿势叠加、魔棒 mask、导出下载），这类请求无法携带自定义请求头，因此登录时下发 Cookie 让它们自动通过；Bearer Header 则保留给脚本与 CLI。Cookie 为 `HttpOnly` + `SameSite=Lax`，存的是令牌派生值而非令牌本身，前端 JS 读不到。

免认证的端点只有 `/api/health` 与 `/api/auth/*`（登录流程自身）。连续登录失败 10 次会触发 5 分钟节流。

---

## 模型与外部依赖（跨平台注意）

| 依赖 | 位置 | 说明 |
| ---- | ---- | ---- |
| AI 抠图模型 `.onnx` | `models/` | u2net / u2net_human_seg / silueta / isnet-anime / bria-rmbg-2.0，未安装时 API 会提示 |
| RTMPose 模型 | `models/rtmpose/` | `yolox_*.onnx` + `rtmw_*.onnx`（姿势检测） |
| rtmlib | 项目根 `rtmlib/` | RTMPose 推理库，**非 PyPI 同名包**；已随仓库分发，无需手动放置 |
| RealESRGAN | `models/realesrgan/` | 可执行文件 `realesrgan-ncnn-vulkan(.exe)` + `models/` 下的 `.param/.bin` |
| pngquant | `tools/pngquant/pngquant(.exe)` | PNG 压缩；Linux 需放置 Linux 版本二进制 |

### 下载与安装模型

**AI 抠图模型**（放到 `models/` 根下，文件名即模型名）：

| 模型 | 大小 | 下载 |
| ---- | ---- | ---- |
| `isnet-anime.onnx` | 168 MB | 动漫/插画专用，做游戏精灵图首选 |
| `u2net.onnx` | 168 MB | 通用 |
| `silueta.onnx` | 42 MB | 轻量，适合快速试参数 |
| `u2net_human_seg.onnx` | 168 MB | 真人像专用，卡通角色用不上 |

前四个来自 rembg 官方 release，公开可下载：

```bash
BASE=https://github.com/danielgatis/rembg/releases/download/v0.0.0
for m in isnet-anime u2net silueta u2net_human_seg; do
  curl -L -o "models/$m.onnx" "$BASE/$m.onnx"
done
```

`bria-rmbg-2.0.onnx` 是 **gated 模型**，需先在 [briaai/RMBG-2.0](https://huggingface.co/briaai/RMBG-2.0)
登录并接受 BRIA 许可（免费授权仅限非商业用途，商用需单独购买），再下载
`onnx/model.onnx`（1.02 GB）重命名放入 `models/`。**只能用全精度版**——
`model_fp16.onnx` 的输入是 fp16，而代码固定喂 float32，会类型不匹配。

> 边缘质量 bria-rmbg-2.0 > isnet-anime > silueta > u2net，但 bria 的 ONNX 导出含
> 可变形卷积，约 1100 个算子无法走 CUDA，即便在 RTX 4070 Ti 上也要 ~9s/帧
> （isnet-anime 仅 0.4s）。建议日常迭代用 isnet-anime，定稿再用 bria。

**RTMPose 姿势模型**（放到 `models/rtmpose/`）。压缩包解压出来都叫
`end2end.onnx`，**必须按下表重命名**，否则代码找不到：

```bash
mkdir -p models/rtmpose && cd models/rtmpose
BASE=https://download.openmmlab.com/mmpose/v1/projects
curl -LO "$BASE/rtmposev1/onnx_sdk/yolox_m_8xb8-300e_humanart-c2c7a14a.zip"
curl -LO "$BASE/rtmw/onnx_sdk/rtmw-dw-x-l_simcc-cocktail14_270e-256x192_20231122.zip"
unzip -o '*.zip'
# 96.7MB 的是检测模型，218.1MB 的是姿态模型，分别重命名：
mv <96.7MB的end2end.onnx>  yolox_m_8xb8-300e_humanart-c2c7a14a.onnx
mv <218.1MB的end2end.onnx> rtmw-dw-x-l_simcc-cocktail14_270e-256x192_20231122.onnx
rm -rf *.zip 20230928 deploy.json detail.json pipeline.json output_*.jpg
```

不放这两个文件时「动作分析」的**姿势**模式会直接报错（默认不联网下载，
原因见下方常见问题）。轮廓 / 图像特征 / 分区域SSIM 三种模式不依赖它们。

> MediaPipe 未安装时，「姿势」模式会自动回退到 RTMPose。Python 3.13 通常没有
> mediapipe wheel，且新版已移除 solutions API——RTMPose 效果足够，不必折腾。
> 做卡通精灵图去重时「轮廓匹配」往往比姿势更合适（姿势模型按真人骨架训练），
> 且快约 3 倍。

所有外部路径均可通过环境变量配置（见 `backend/.env.example`）：

```ini
SPRITE_DATA_DIR=data
SPRITE_MODELS_DIR=...          # 开发期可指向原项目 models 目录
SPRITE_TOOLS_DIR=tools
SPRITE_HOST=127.0.0.1
SPRITE_PORT=8000

# 安全与资源上限（详见 backend/.env.example）
SPRITE_CORS_ORIGINS=*          # 对外暴露时收敛为具体来源
SPRITE_DEBUG_ERRORS=false      # true 时任务错误会带完整 traceback（仅调试）
SPRITE_MAX_UPLOAD_MB=2048      # 单个视频上传上限
SPRITE_MAX_EXTRACT_FRAMES=2000 # 单次抽帧帧数上限
SPRITE_ALLOW_MODEL_DOWNLOAD=false  # 允许 RTMPose 缺模型时联网下载
```

> Windows 下外部二进制带 `.exe` 后缀，Linux 下为裸名——代码按 `sys.platform` 自动解析，无需修改源码。

---

## API 概览

基础路径 `/api`，交互式文档见 `http://<host>:8000/docs`。

| 分组 | 端点 | 说明 |
| ---- | ---- | ---- |
| 认证 | `GET /api/auth/status`<br>`POST /api/auth/login`<br>`POST /api/auth/logout` | 是否需要认证 / 登录（下发 Cookie）/ 登出。未配置令牌时全部放行 |
| 能力 | `GET /api/capabilities` | 平台、可用模型、GPU、导出格式 |
| 会话 | `POST/GET/DELETE /api/sessions[/{id}]` | 创建/查询/删除项目会话 |
| 视频 | `POST /api/sessions/{id}/video`<br>`GET /api/sessions/{id}/video`<br>`GET /api/sessions/{id}/video/info` | 上传 / 预览流 / 元数据 |
| 帧 | `POST .../frames/extract`<br>`GET .../frames`<br>`GET .../frames/{i}/image?type=raw\|processed\|preview`<br>`POST .../frames/selection`<br>`DELETE .../frames/{i}`<br>`POST .../frames/reorder`<br>`POST .../frames/loop-transition`<br>`POST .../frames/supplement` | 抽帧(任务) / 列表 / 图像 / 选择 / 删除 / 重排 / 循环过渡预览(GIF) / 首尾补帧 |
| 分析 | `POST .../analysis/detect`<br>`GET .../analysis/{i}`<br>`GET .../analysis/{i}/overlay?mode=pose`<br>`POST .../analysis/remove-similar`<br>`POST .../analysis/find-loop` | 姿势/轮廓/特征/SSIM 检测与比对 |
| 背景 | `POST .../background/test`<br>`POST .../background/remove`<br>`POST .../background/outline` | 单帧调参 / 批量抠图 / 描边 |
| 图像 | `POST .../image/scale`<br>`POST .../image/crop-whitespace`<br>`POST .../image/optimize-edges`<br>`POST .../image/enhance`<br>`POST .../image/wand/select`<br>`POST .../image/wand/apply` | 缩放/裁剪/边缘/增强/魔棒 |
| 导出 | `POST .../export`<br>`GET .../export/list`<br>`GET .../export/{name}/download` | 精灵图/GIF/WebP/Godot，结果打包 zip |
| 历史 | `GET .../history`<br>`POST .../history/revert` | 撤销/回退 |
| 任务 | `GET /api/jobs`<br>`GET /api/jobs/{id}`<br>`POST /api/jobs/{id}/cancel` | 后台任务状态与取消 |

> 长耗时操作（抽帧、抠图、分析、导出等）均返回 `{job_id}`，前端轮询 `/api/jobs/{id}`。

---

## GPU 加速（可选）

后端默认使用 onnxruntime CPU。Linux 服务器如有 NVIDIA GPU：

```bash
.venv/bin/python -m pip uninstall -y onnxruntime onnxruntime-gpu
.venv/bin/python -m pip install onnxruntime-gpu
# 设置 SPRITE_FORCE_CPU=false（默认）后，抠图模型将优先使用 CUDA provider
```

---

## 冒烟测试

```bash
# 进程内（无需启动服务，需先安装 requirements-dev.txt）
.venv/bin/python scripts/smoke_test.py
# 对运行中的服务
.venv/bin/python scripts/smoke_test.py --live http://127.0.0.1:8000
```

覆盖：会话→上传→抽帧→特征检测→去相似→抠图→缩放→找循环→历史回退→精灵图/GIF导出→下载→清理。

---

## 循环过渡与首尾补帧

（移植自原 SpriteFrameStudio 对应功能）

- **循环过渡**：让帧动画首尾无缝衔接。在右侧「循环处理」面板开启后，对选中帧生成过渡预览（GIF），导出精灵图/GIF 时也会自动应用。支持两种模式：
  - `blend` 像素混合：末尾 T 帧与开头 T 帧一一交叉淡入淡出
  - `align` 轮廓对齐：按 alpha 质心对齐后仅混合 RGB，轮廓更清晰
- **首尾补帧**：在选中帧的「尾帧→首帧」之间生成 N 帧（1~7），追加到帧管理并标记「补」，使循环播放更连贯。当前为轻量线性插值实现（无需额外依赖）；原项目的 AI（RIFE）插帧可在此基础上替换。

---

## 常见问题

**Q：RealESRGAN 提示不可用？**
A：需在 `models/realesrgan/` 放置 `realesrgan-ncnn-vulkan`（Linux）或 `.exe`（Windows）及 `models/` 下的 `.param/.bin` 文件。

**Q：姿势(RTM)检测找不到 rtmlib？**
A：rtmlib 已随仓库分发（项目根 `rtmlib/rtmlib/...`），无需手动放置；确认 `models/rtmpose/` 下有对应模型即可。

**Q：「动作分析」的姿势模式报「未找到本地 RTMPose 模型」？**
A：默认不联网下载（rtmlib 的下载不校验哈希，且离线部署不应有意外外连）。按
「模型与外部依赖 → 下载与安装模型」一节把两个 onnx 放进 `models/rtmpose/` 即可；
注意压缩包解压出来都叫 `end2end.onnx`，必须重命名。急用也可设
`SPRITE_ALLOW_MODEL_DOWNLOAD=true` 让 rtmlib 自动下载。

**Q：抠图后边缘有一圈原背景色的白边？**
A：代码只做 alpha 合成、不做颜色净化（`rgba[:,:,:3]` 直接沿用原始 RGB），
半透明边缘像素保留着「主体色 × 背景色」的混合值。缓解办法：换边缘更锐的模型
（isnet-anime / bria-rmbg-2.0），并设 `Alpha阈值=128`、`腐蚀=1`、`羽化=0`。
**注意 Alpha 阈值不要填 1~50 的小值**——那会把淡白边缘一律提升为完全不透明，
比填 0 更糟；要么 0（保留柔和边缘），要么 ≥128（硬边）。羽化会加宽过渡带，
做精灵图基本不要开。

**Q：重启服务后之前的项目还在吗？**
A：在。会话数据以文件形式存于 `data/sessions/{id}/`，重启后用同一个会话 ID 访问即可自动恢复。只有显式删除会话才会清除磁盘数据。

**Q：mediapipe / onnxruntime 在 Python 3.13 无 wheel？**
A：用 conda 创建 Python 3.11 环境再执行 setup 脚本。

**Q：浏览器预览视频黑屏/不支持编码？**
A：浏览器 `<video>` 仅支持部分编码（如 H.264）。不支持时请使用「帧预览」功能（抽帧后查看缩略图）。

---

## 开源协议

本项目基于 [SpriteFrameStudio](https://github.com/game-cat/SpriteFrameStudio)（原作者：小猫学游戏）重构，遵循 **CC BY 4.0**（署名 4.0 国际）。使用或分发本项目时请保留本声明。
