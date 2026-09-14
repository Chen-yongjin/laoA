@echo off
chcp 65001 >nul
echo === 老A选股助手 打包 ===
echo.

rem ── 先检查 Python 3.11 是否存在（打包必须有；只运行 exe 的用户不需要）──
set PYCMD=
py -3.11 --version >nul 2>&1 && set PYCMD=py -3.11
if "%PYCMD%"=="" (
  python --version >nul 2>&1 && set PYCMD=python
)
if "%PYCMD%"=="" (
  echo [错误] 没有找到 Python。
  echo.
  echo   打包 exe 需要 Python 3.11（只运行别人打包好的 exe 则不需要）。
  echo   任选一种方式安装：
  echo     1^) 打开 https://www.python.org/downloads/release/python-3119/ 下载安装包，
  echo        安装时务必勾选 "Add python.exe to PATH"
  echo     2^) 或在命令行执行：winget install Python.Python.3.11
  echo.
  echo   装好后重新双击本脚本。
  pause
  exit /b 1
)
for /f "tokens=*" %%v in ('%PYCMD% --version 2^>^&1') do echo 使用解释器：%%v  ^(%PYCMD%^)
echo.

if not exist .venv\Scripts\python.exe (
  echo [1/3] 创建虚拟环境...
  %PYCMD% -m venv .venv || goto :err
)
echo [2/3] 安装依赖...
.venv\Scripts\pip install -U pip >nul
.venv\Scripts\pip install -e ".[dev]" || goto :err
echo [3/3] PyInstaller 打包...
.venv\Scripts\pyinstaller --noconfirm --clean build\laoa_trader.spec || goto :err
echo.
echo 完成！产物在 dist\老A选股助手\老A选股助手.exe
pause
exit /b 0
:err
echo 出错了，请把上面的报错发给我。
pause
exit /b 1
