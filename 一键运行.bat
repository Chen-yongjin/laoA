@echo off
rem ============================================================================
rem  老A选股助手 · 一键运行（Windows）
rem
rem  为什么有这个脚本：正常路径是"从 GitHub Actions 下打包好的 exe"，但那要等
rem  云端构建（而且 Actions 有额度/排队的时候会卡住）。这个脚本让你在本机直接跑起来：
rem  建一个只属于本程序的虚拟环境 → 装依赖 → 启动界面，不碰系统 Python 环境。
rem
rem  前提：本机装了 Python 3.11 或更新版本（没装的话脚本会告诉你去哪下）。
rem  第一次运行会装依赖（约 1~3 分钟，看网速），之后每次都是秒开。
rem ============================================================================
chcp 65001 >nul 2>nul
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo === 老A选股助手 · 一键运行 ===
echo.

rem ── 1) 找 Python（优先 py -3.11，其次 py -3，最后 PATH 里的 python）──
set "PY_CMD="
py -3.11 -c "import sys" >nul 2>nul && set "PY_CMD=py -3.11"
if not defined PY_CMD (
  py -3 -c "import sys" >nul 2>nul && set "PY_CMD=py -3"
)
if not defined PY_CMD (
  python -c "import sys" >nul 2>nul && set "PY_CMD=python"
)
if not defined PY_CMD (
  echo [X] 没找到 Python。
  echo     请先装 Python 3.11 或更新版本：https://www.python.org/downloads/windows/
  echo     安装时记得勾上 "Add python.exe to PATH"。
  echo     装完关掉这个窗口，再双击一次本脚本。
  start "" "https://www.python.org/downloads/windows/"
  echo.
  pause
  exit /b 1
)
echo [1/4] 用的 Python：%PY_CMD%

rem ── 2) 建虚拟环境（只在没有时建；已存在就直接用）──
if not exist ".venv\Scripts\python.exe" (
  echo [2/4] 第一次运行：正在建虚拟环境 .venv ...
  %PY_CMD% -m venv .venv
  if errorlevel 1 (
    echo [X] 建虚拟环境失败。请确认 Python 安装完整（或用管理员身份重试）。
    pause
    exit /b 1
  )
) else (
  echo [2/4] 虚拟环境已存在，直接用。
)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

rem ── 3) 装依赖（装过一次就不再装；用 -e . 装本仓库自身）──
if not exist ".venv\.deps_ok" (
  echo [3/4] 正在装依赖（第一次约 1~3 分钟，请稍等）...
  "%VENV_PY%" -m pip install --upgrade pip --quiet
  "%VENV_PY%" -m pip install -e . --quiet
  if errorlevel 1 (
    echo [X] 装依赖失败。常见原因：网络不通 / 公司代理。
    echo     可以试试国内镜像：
    echo       "%VENV_PY%" -m pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
  )
  echo ok> ".venv\.deps_ok"
) else (
  echo [3/4] 依赖已装过，跳过。
)

rem ── 4) 启动界面 ──
echo [4/4] 启动界面...
echo       （关窗 = 收进右下角托盘；要退出用托盘右键的【退出】）
echo.
"%VENV_PY%" -m laoa_trader
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo [X] 程序退出，返回码 %CODE%。上面几行通常就是原因。
  echo     排查用：  "%VENV_PY%" -m laoa_trader --doctor
  pause
)
exit /b %CODE%
