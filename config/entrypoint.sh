#!/bin/bash
# camlive 容器入口:
#   /app 由宿主机挂载。首次启动(空目录)时从 /app.default 拷贝程序和通用配置模板;
#   之后每次启动只补齐缺失的文件(cp -n 不覆盖), 不会改动你已有的 cameras.json 或代码。
#   然后执行 server.py (端口取 PORT 环境变量, 默认 8081)
if [ ! -f /app/cameras.json ]; then
  echo "[init] /app 为空, 从默认模板初始化..."
fi
cp -an /app.default/. /app/ 2>/dev/null || true
exec python3 /app/server.py "${PORT:-8081}"
