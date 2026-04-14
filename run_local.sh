#!/bin/bash
# daily-paper-reader 本地部署启动脚本
# 用法: ./run_local.sh [--fetch-days N] [--fetch-mode auto|standard|skims]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 加载环境变量
if [ -f .env ]; then
    set -a
    source .env
    set +a
    echo "[INFO] 已加载 .env 环境变量"
else
    echo "[ERROR] 缺少 .env 文件，请先配置 OpenRouter API Key"
    exit 1
fi

# 使用 conda 环境的 Python
PYTHON="/home/ghz/miniconda3/envs/daily-paper-reader/bin/python3"

if [ ! -x "$PYTHON" ]; then
    echo "[ERROR] 找不到 conda 环境 daily-paper-reader，请先创建: conda create -n daily-paper-reader python=3.11"
    exit 1
fi

echo "[INFO] Python: $PYTHON"
echo "[INFO] LLM Base: $LLM_PRIMARY_BASE_URL"
echo "[INFO] 模型: $BLT_FILTER_MODEL (filter) / $BLT_SUMMARY_MODEL (summary)"
echo "[INFO] 注意: OpenRouter 不支持 Rerank API，Step 3 将自动跳过，使用 RRF 分数兜底"
echo ""

# 运行流水线
exec "$PYTHON" src/main.py \
    --embedding-device cpu \
    --embedding-batch-size 8 \
    "$@"
