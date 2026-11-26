Windows 版打包说明

Windows 程序需在 Windows 电脑上打包。

准备：
1. 安装 Python 3.10 或以上版本、Node.js 和 pnpm。
2. 在项目根目录运行 pnpm --dir frontend install --frozen-lockfile，随后运行 pnpm --dir frontend build。
3. 双击 build_windows_app.bat。

打包结果位于 release_windows\国刚铸模滚动排产_Windows，包含启动程序和前端页面。打包脚本不会复制本地数据库、上传表或导出文件；首次启动将创建空数据库。

数据保存在“文档\国刚铸模滚动排产小工具数据”，每台电脑的数据独立，不会自动同步。分享发行包前请检查内容。
