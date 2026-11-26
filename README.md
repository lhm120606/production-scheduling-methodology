# 生产排产系统

本地运行的铸造生产排产系统，使用 Python、FastAPI、React 和 SQLite。系统根据订单交期、工序及产能生成排产计划，支持 Excel 订单导入、周计划、节点提醒、进度记录和 Excel 导出。

本仓库包含程序源码及测试，不包含实际订单、数据库、原始上传表或历史导出。排产规则和默认产能针对原使用场景设置，使用前应结合实际业务核对，不能直接视为适用于其他工厂的生产规则。

## 本地运行

需要 Python 3.10 或以上、Node.js 和 pnpm。先在项目根目录构建前端：

```bash
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
```

然后安装后端依赖并启动：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

在浏览器访问 `http://127.0.0.1:8000/`。首次启动将创建空数据库；需要自行导入订单或手动录入。

Windows 用户可使用 Python 虚拟环境并用对应的 `Scripts` 目录启动，或者在前端构建后运行 `启动排产系统.bat`。Mac 可在前端构建后运行 `启动排产系统.command`。这两个原有启动脚本监听局域网地址，系统没有登录鉴权，只应在可信网络使用，不要直接暴露到公网。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用独立临时数据库，不需要真实订单。

## 数据和打包

运行时数据库和上传文件位于 `work/`，导出文件位于 `outputs/`；它们不会提交到仓库。`build_windows_app.bat` 可在 Windows 上打包，需先构建前端。仓库中的打包脚本不会把本地数据库、上传表或导出文件复制进发行包；新安装的数据为空。分享任何打包产物前仍需检查内容。
