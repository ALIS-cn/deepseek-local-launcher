@echo off
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0app.py"
  exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0app.py"
  exit /b 0
)

echo 未检测到 Python，请先安装 Python 3.10 或更高版本。
pause
