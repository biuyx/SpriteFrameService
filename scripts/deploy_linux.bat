@echo off
setlocal
chcp 65001 > nul 2>&1
cd /d "%~dp0\.."

rem 一键部署到 Linux 服务器。实际干活的是 scripts\deploy_linux.sh，需要 Git for Windows。
rem 双击 = 部署当前提交；命令行可带参数：--status  --dry-run  --rollback  --help
set "GITBASH=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%GITBASH%" set "GITBASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not exist "%GITBASH%" set "GITBASH=%LocalAppData%\Programs\Git\bin\bash.exe"
if not exist "%GITBASH%" (
    echo 未找到 Git Bash，请先安装 Git for Windows
    pause & exit /b 1
)

"%GITBASH%" scripts/deploy_linux.sh %*
pause
