@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel% neq 0 (
  echo 没有找到 Python。请先安装 Python 3.10 或以上版本。
  pause
  exit /b 1
)

if not exist ".venv-win" (
  py -3 -m venv .venv-win
)

.venv-win\Scripts\python.exe -m pip install --upgrade pip
.venv-win\Scripts\python.exe -m pip install -r requirements.txt pyinstaller==6.11.1

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release_windows rmdir /s /q release_windows

.venv-win\Scripts\pyinstaller.exe --clean --noconfirm --onedir --name guogang-server --paths . desktop\guogang_server.py --hidden-import=backend --hidden-import=backend.app --hidden-import=uvicorn --hidden-import=uvicorn.logging --hidden-import=uvicorn.loops --hidden-import=uvicorn.loops.auto --hidden-import=uvicorn.protocols --hidden-import=uvicorn.protocols.http --hidden-import=uvicorn.protocols.http.auto --hidden-import=uvicorn.protocols.websockets --hidden-import=uvicorn.protocols.websockets.auto --hidden-import=uvicorn.lifespan --hidden-import=uvicorn.lifespan.on --hidden-import=multipart --hidden-import=openpyxl.cell._writer
if %errorlevel% neq 0 (
  echo 后端打包失败。
  pause
  exit /b 1
)

.venv-win\Scripts\pyinstaller.exe --clean --noconfirm --onefile --windowed --name 启动国刚排产系统 desktop\windows_launcher.py
if %errorlevel% neq 0 (
  echo 启动器打包失败。
  pause
  exit /b 1
)

mkdir "release_windows\国刚铸模滚动排产_Windows"
xcopy /E /I /Y "dist\guogang-server" "release_windows\国刚铸模滚动排产_Windows" >nul
copy /Y "dist\启动国刚排产系统.exe" "release_windows\国刚铸模滚动排产_Windows\" >nul
mkdir "release_windows\国刚铸模滚动排产_Windows\frontend"
xcopy /E /I /Y "frontend\dist" "release_windows\国刚铸模滚动排产_Windows\frontend\dist" >nul
mkdir "release_windows\国刚铸模滚动排产_Windows\seed\work\data"
mkdir "release_windows\国刚铸模滚动排产_Windows\seed\work\uploads"
mkdir "release_windows\国刚铸模滚动排产_Windows\seed\outputs"
copy /Y "desktop\README_WINDOWS打包.txt" "release_windows\国刚铸模滚动排产_Windows\使用说明.txt" >nul

powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'release_windows\国刚铸模滚动排产_Windows' -DestinationPath 'release_windows\国刚铸模滚动排产_Windows.zip' -Force"

echo.
echo Windows 版已生成：
echo release_windows\国刚铸模滚动排产_Windows.zip
echo.
pause
