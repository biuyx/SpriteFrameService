<#
    构建 Windows 免安装绿色包（内嵌 Python，目标机器无需安装任何环境）。

    产物: dist-portable\SpriteFrameService-portable-<日期>.zip
    结构:
        SpriteFrameService\
            python\        内嵌 CPython（含 pip 装好的依赖）
            backend\       后端源码
            frontend\dist\ 前端构建产物
            rtmlib\        RTMPose 推理库
            models\        抠图与姿势模型
            data\          运行时数据（首次启动创建）
            启动服务.bat
            使用说明.txt

    用法（在项目根目录）:
        powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1
        powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1 -SkipModels
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.13.1",
    [switch]$SkipModels,          # 不打包模型（产出精简包）
    [switch]$KeepStaging          # 保留中间目录，便于排查
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$outDir  = Join-Path $root "dist-portable"
$staging = Join-Path $outDir "SpriteFrameService"
$cache   = Join-Path $outDir "_cache"

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Info($msg)     { Write-Host "    $msg" -ForegroundColor DarkGray }

# ---------------------------------------------------------------- 准备
Step 1 "准备目录"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $staging, $cache | Out-Null
Info "输出: $staging"

# ---------------------------------------------------------------- 内嵌 Python
Step 2 "获取内嵌 Python $PythonVersion"
$embedZip = Join-Path $cache "python-$PythonVersion-embed-amd64.zip"
if (-not (Test-Path $embedZip)) {
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
    Info "下载 $url"
    Invoke-WebRequest -Uri $url -OutFile $embedZip -UseBasicParsing
} else {
    Info "使用缓存"
}
$pyDir = Join-Path $staging "python"
Expand-Archive -Path $embedZip -DestinationPath $pyDir -Force

# 内嵌版默认禁用 site 机制，需放开才能加载 pip 装的第三方包
$pth = Get-ChildItem -Path $pyDir -Filter "python*._pth" | Select-Object -First 1
if ($null -eq $pth) { throw "未找到 ._pth 文件，内嵌包结构异常" }
$lines = Get-Content $pth.FullName
$lines = $lines -replace '^\s*#\s*import\s+site\s*$', 'import site'
if ($lines -notcontains 'import site') { $lines += 'import site' }
if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
Set-Content -Path $pth.FullName -Value $lines -Encoding ASCII
Info "已放开 site-packages 加载"

# ---------------------------------------------------------------- 依赖
Step 3 "安装运行时依赖"
$sitePkgs = Join-Path $pyDir "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $sitePkgs | Out-Null

# 只装真正被 import 的包：rembg 与 aiofiles 全项目从未被引用（抠图走代码自带的
# onnxruntime 会话），mediapipe 仅为可选回退且 3.13 常无 wheel，一并排除。
#
# 版本全部钉死：分发包不能让 pip 每次解析到最新。这组版本已通过冒烟(29/29)与
# 端到端测试。实测放开版本会装到 onnxruntime 1.28，在内嵌 Python 下 DLL 初始化
# 失败；opencv 也会跳到 5.x 大版本。
$reqFile = Join-Path $cache "requirements-portable.txt"
@"
fastapi==0.141.1
uvicorn[standard]==0.52.1
python-multipart==0.0.32
pydantic==2.10.6
pydantic-settings==2.15.0
numpy==2.4.6
opencv-python-headless==4.11.0.86
Pillow==11.3.0
onnxruntime==1.20.1
tqdm==4.67.1
"@ | Set-Content -Path $reqFile -Encoding UTF8
# tqdm 是 rtmlib/tools/file.py 的模块级依赖，缺了姿势检测会直接 ImportError。
# rtmlib 还会 import openvino，但只在 backend=='openvino' 分支内懒加载，
# 本项目固定用 onnxruntime，因此不打包那 300MB。

& python -m pip install --disable-pip-version-check -q `
    --target $sitePkgs --only-binary=:all: -r $reqFile
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
$depMB = [math]::Round((Get-ChildItem $sitePkgs -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Info "依赖体积 $depMB MB"

# 瘦身：测试用例、缓存、类型存根对运行无用
foreach ($pat in @("__pycache__", "tests", "test")) {
    Get-ChildItem -Path $sitePkgs -Filter $pat -Recurse -Directory -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
Get-ChildItem -Path $sitePkgs -Filter "*.pyi" -Recurse -File -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
$depMB2 = [math]::Round((Get-ChildItem $sitePkgs -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Info "瘦身后 $depMB2 MB"

# ---------------------------------------------------------------- 前端
Step 4 "准备前端产物"
$distSrc = Join-Path $root "frontend\dist"
if (-not (Test-Path (Join-Path $distSrc "index.html"))) {
    Info "frontend\dist 缺失，尝试构建（需 Node.js）"
    Push-Location (Join-Path $root "frontend")
    & npm install; & npm run build
    Pop-Location
    if (-not (Test-Path (Join-Path $distSrc "index.html"))) { throw "前端构建失败" }
}
$dst = Join-Path $staging "frontend\dist"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$distSrc\*" $dst -Recurse -Force
Info "已复制前端产物"

# ---------------------------------------------------------------- 源码
Step 5 "复制后端与推理库"
robocopy (Join-Path $root "backend") (Join-Path $staging "backend") /E /NFL /NDL /NJH /NJS /NP `
    /XD "__pycache__" ".venv" /XF "*.pyc" ".env" | Out-Null
robocopy (Join-Path $root "rtmlib")  (Join-Path $staging "rtmlib")  /E /NFL /NDL /NJH /NJS /NP `
    /XD "__pycache__" /XF "*.pyc" | Out-Null
if ($LASTEXITCODE -ge 8) { throw "源码复制失败" }
Info "backend / rtmlib 已就位（排除 .env 与缓存）"

# ---------------------------------------------------------------- 模型
Step 6 "打包模型"
if ($SkipModels) {
    Info "已跳过（-SkipModels）"
    New-Item -ItemType Directory -Force -Path (Join-Path $staging "models") | Out-Null
} else {
    # bria-rmbg-2.0 是 gated 模型且许可仅授权非商业自用，不随包分发
    $modelsSrc = Join-Path $root "models"
    $modelsDst = Join-Path $staging "models"
    New-Item -ItemType Directory -Force -Path $modelsDst | Out-Null
    $names = @("u2net.onnx","u2net_human_seg.onnx","silueta.onnx","isnet-anime.onnx")
    foreach ($n in $names) {
        $p = Join-Path $modelsSrc $n
        if (Test-Path $p) { Copy-Item $p $modelsDst -Force; Info "抠图 $n" }
        else { Write-Warning "缺少模型 $n，跳过" }
    }
    $rtmSrc = Join-Path $modelsSrc "rtmpose"
    if (Test-Path $rtmSrc) {
        $rtmDst = Join-Path $modelsDst "rtmpose"
        New-Item -ItemType Directory -Force -Path $rtmDst | Out-Null
        Copy-Item "$rtmSrc\*.onnx" $rtmDst -Force
        Info "姿势模型 $((Get-ChildItem $rtmDst -Filter *.onnx).Count) 个"
    }
    if (Test-Path (Join-Path $modelsSrc "bria-rmbg-2.0.onnx")) {
        Info "bria-rmbg-2.0 因许可限制不打包（见使用说明）"
    }
}

# ---------------------------------------------------------------- 启动器与说明
Step 7 "生成启动器与说明"
New-Item -ItemType Directory -Force -Path (Join-Path $staging "data") | Out-Null

$launcher = @'
@echo off
chcp 65001 > nul 2>&1
title 精灵帧工作室 SpriteFrameService
cd /d "%~dp0"

set "PY=%~dp0python\python.exe"
if not exist "%PY%" (
    echo [错误] 缺少内嵌 Python，安装包可能未解压完整。
    echo 请把 ZIP 完整解压后再运行，不要直接在压缩包内双击。
    pause & exit /b 1
)

if not exist "backend\.env" (
    echo 首次启动，正在生成默认配置 ...
    > "backend\.env" echo # 首次启动自动生成。改完保存后重启本程序生效。
    >> "backend\.env" echo # 仅本机访问；要让同事通过内网访问，改成 0.0.0.0 并设置下面的令牌。
    >> "backend\.env" echo SPRITE_HOST=127.0.0.1
    >> "backend\.env" echo SPRITE_PORT=8000
    >> "backend\.env" echo # 内网开放时务必设置访问令牌，否则同网段任何人都能读写你的数据
    >> "backend\.env" echo # SPRITE_AUTH_TOKEN=
    >> "backend\.env" echo # SPRITE_AUTH_COOKIE_SECURE=false
)

for /f "usebackq tokens=1,2 delims==" %%a in ("backend\.env") do (
    if /i "%%a"=="SPRITE_PORT" set "PORT=%%b"
)
if not defined PORT set "PORT=8000"

echo.
echo   精灵帧工作室 正在启动 ...
echo   启动后浏览器会自动打开 http://127.0.0.1:%PORT%
echo   关闭本窗口即可停止服务。
echo.

start "" /b cmd /c "timeout /t 4 > nul & start http://127.0.0.1:%PORT%"

set "PYTHONPATH=%~dp0backend"
set "PYTHONIOENCODING=utf-8"
"%PY%" backend\run.py

echo.
echo 服务已停止。
pause
'@
Set-Content -Path (Join-Path $staging "启动服务.bat") -Value $launcher -Encoding Default

$readme = @'
精灵帧工作室 SpriteFrameService — Windows 免安装版
==================================================

【怎么用】
  1. 把整个文件夹解压到任意位置（例如 D:\SpriteFrameService）
     ※ 必须先解压，不要在压缩包里直接双击
  2. 双击「启动服务.bat」
  3. 稍等几秒，浏览器会自动打开操作界面
  4. 关闭那个黑色命令行窗口即可停止服务

  不需要安装 Python、Node.js 或任何其他软件。
  不写注册表、不需要管理员权限。删掉整个文件夹就等于卸载。

【数据存在哪】
  你的视频、抽出的帧、导出结果都在 data\ 目录下。
  换机器时连同整个文件夹一起拷走即可，重启后项目仍在。

【自带哪些模型】
  抠图： isnet-anime（动漫/插画，做游戏精灵图首选）
         u2net（通用）、silueta（轻量快速）、u2net_human_seg（真人像）
  姿势： RTMPose（yolox 检测 + rtmw 姿态），动作分析用

  另有 BRIA RMBG-2.0 边缘质量更好，但它是受限模型、许可仅授权非商业
  使用，不能随包分发。需要的话自行到 huggingface.co/briaai/RMBG-2.0
  接受许可后下载 onnx/model.onnx，重命名为 bria-rmbg-2.0.onnx 放进
  models\ 目录即可被识别。

【抠图有白边怎么办】
  在「背景处理」页把参数调成：Alpha阈值=128、腐蚀=1、羽化=0。
  注意 Alpha 阈值不要填 1~50 的小数值，那比填 0 更糟。
  模型优先用 isnet-anime。

【想让同事也能用（内网访问）】
  1. 用记事本打开 backend\.env
  2. 把 SPRITE_HOST 改成 0.0.0.0
  3. 去掉 SPRITE_AUTH_TOKEN 与 SPRITE_AUTH_COOKIE_SECURE 前面的 # 号，
     并填一个自己定的口令，例如：
         SPRITE_AUTH_TOKEN=my-secret-2026
         SPRITE_AUTH_COOKIE_SECURE=false
  4. 保存，重启本程序
  5. 以管理员身份运行 PowerShell，放行防火墙（端口按实际填写）：
         New-NetFirewallRule -DisplayName "SpriteFrameService" -Direction Inbound `
             -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
  6. 同事浏览器打开 http://你的内网IP:8000 ，输入上面设的口令

  ※ 开放内网前请务必设置令牌，否则同网段任何人都能读写你的数据。
  ※ 内网是明文传输，令牌在网段内可被抓包，不要用重要密码。

【常见问题】
  启动闪退 / 提示缺少 Python
      没有解压就直接在压缩包里双击了。请先解压整个文件夹。

  浏览器打不开、提示端口被占用
      改 backend\.env 里的 SPRITE_PORT 换个端口（如 8123），保存后重启。

  「动作分析」的姿势模式报错
      检查 models\rtmpose\ 下是否有两个 .onnx 文件。

  杀毒软件报警
      内嵌 Python 解释器有时会被误报。本包不写注册表、不联网上传，
      可将整个文件夹加入白名单。
'@
Set-Content -Path (Join-Path $staging "使用说明.txt") -Value $readme -Encoding UTF8
Info "启动服务.bat / 使用说明.txt 已生成"

# ---------------------------------------------------------------- 打包
Step 8 "压缩"
$stamp = Get-Date -Format "yyyyMMdd"
$zipName = if ($SkipModels) { "SpriteFrameService-portable-nomodels-$stamp.zip" }
           else { "SpriteFrameService-portable-$stamp.zip" }
$zipPath = Join-Path $outDir $zipName
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path $staging -DestinationPath $zipPath -CompressionLevel Optimal

$zipMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
$rawMB = [math]::Round((Get-ChildItem $staging -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)

if (-not $KeepStaging) { Remove-Item $staging -Recurse -Force }

Write-Host "`n完成" -ForegroundColor Green
Write-Host "  解压后 $rawMB MB / 压缩包 $zipMB MB"
Write-Host "  $zipPath"
