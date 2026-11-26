@echo off
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel% neq 0 (
  echo 没有找到 Python，请先安装 Python 3.10 或以上版本。
  pause
  exit /b 1
)

if not exist ".venv" (
  py -3 -m venv .venv
)

.venv\Scripts\python.exe -m pip install -r requirements.txt

echo.
echo 排产系统启动中...
echo 本机访问：http://127.0.0.1:8000/
echo 同一网络内请使用本机局域网IP加端口8000访问。
echo 关闭这个窗口会停止系统。
echo.

start http://127.0.0.1:8000/
.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
pause
