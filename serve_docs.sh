#!/bin/bash
# 本地文档服务器 - 浏览器访问 http://localhost:8080
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOCS_DIR="$SCRIPT_DIR/docs"

if [ ! -d "$DOCS_DIR" ]; then
    echo "[ERROR] docs/ 目录不存在，请先运行流水线: ./run_local.sh"
    exit 1
fi

echo "[INFO] 文档服务器启动: http://localhost:8080"
echo "[INFO] Ctrl+C 停止"
cd "$SCRIPT_DIR"
python3 serve.py 8080
