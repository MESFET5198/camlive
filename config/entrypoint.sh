#!/bin/bash
# camlive 容器入口:
#   若 /app 为空(首次挂载空配置目录), 从 /app.default 自动拷贝默认配置
#   之后执行 server.py (端口取 PORT 环境变量, 默认 8081)
if [ ! -f /app/cameras.json ]; then
  echo "[init] /app 为空, 从默认模板初始化..."
  cp -a /app.default/. /app/
fi
exec python3 /app/server.py "${PORT:-8081}"
