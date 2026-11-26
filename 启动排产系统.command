#!/bin/zsh
set -e

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有找到 python3，请先安装 Python 3.10 或以上版本。"
  read "?按回车退出"
  exit 1
fi

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -r requirements.txt

echo ""
echo "排产系统启动中..."
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || true)
echo "本机访问：http://127.0.0.1:8000/"
if [ -n "$LAN_IP" ]; then
  echo "同一网络内访问：http://${LAN_IP}:8000/"
fi
echo "关闭这个窗口会停止系统。"
echo ""

.venv/bin/python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

READY=0
for _ in {1..60}; do
  if curl -fsS "http://127.0.0.1:8000/api/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "系统启动超时，请查看上方错误信息。"
  wait "$SERVER_PID" || true
  exit 1
fi

echo "系统已就绪，正在打开网页..."
open "http://127.0.0.1:8000/" >/dev/null 2>&1 || true
wait "$SERVER_PID"
