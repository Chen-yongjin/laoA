@echo off
rem =====================================================================
rem  Build "Lao Niu Xuan Gu" (Windows, Nuitka) -- ASCII only on purpose.
rem  WHY ASCII: cmd.exe parses .bat files with the OEM codepage (936 on
rem  Chinese Windows). A UTF-8 file with Chinese text gets mangled there,
rem  which breaks even "rem" and "if errorlevel" lines. Chinese docs live
rem  in docs/ (Chinese build guide) instead.
rem =====================================================================
setlocal

echo === Lao Niu Xuan Gu - Windows build (Nuitka) ===
echo.

rem ---- 1) find a usable Python (3.11 or newer) -----------------------
set PYCMD=
for %%V in (3.11 3.12 3.13 3) do (
  if "%PYCMD%"=="" (
    py -%%V --version >nul 2>&1 && set PYCMD=py -%%V
  )
)
if "%PYCMD%"=="" (
  python --version >nul 2>&1 && set PYCMD=python
)
if "%PYCMD%"=="" (
  echo [ERROR] Python not found.
  echo.
  echo   Install Python 3.11 or newer, and tick "Add python.exe to PATH":
  echo     1^) https://www.python.org/downloads/release/python-3119/
  echo     2^) or in cmd:  winget install Python.Python.3.11
  echo.
  pause
  exit /b 1
)
for /f "tokens=*" %%v in ('%PYCMD% --version 2^>^&1') do echo Using Python: %%v  ^(%PYCMD%^)
echo.

rem ---- 2) virtualenv + dependencies ----------------------------------
if not exist .venv\Scripts\python.exe (
  echo [1/3] Creating virtualenv .venv ...
  %PYCMD% -m venv .venv || goto :err
)
echo [2/3] Installing dependencies ...
.venv\Scripts\pip install -U pip >nul
.venv\Scripts\pip install -e ".[dev]" || goto :err

rem ---- 3) Nuitka build -----------------------------------------------
echo [3/3] Building with Nuitka (first run 20-60 min: downloads a C compiler) ...
echo.
.venv\Scripts\python build\nuitka_build.py || goto :err

echo.
echo Done. Output folder: dist\LaoniuTrader
dir /b dist\LaoniuTrader\*.exe
echo   Fallback (PyInstaller): .venv\Scripts\pyinstaller --noconfirm --clean build\laoa_trader.spec
echo.
echo Quick self-test: run that exe with --version and --doctor (see docs, code 6).
pause
exit /b 0

:err
echo.
echo [ERROR] Build failed - send the last 30 lines above to the author.
pause
exit /b 1
