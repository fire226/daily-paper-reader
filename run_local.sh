#!/bin/bash
# daily-paper-reader 本地部署启动脚本
# 用法: ./run_local.sh [--start-date YYYYMMDD] [--end-date YYYYMMDD]
# 默认行为：抓取最近 9 天（今天 - 8天 ~ 今天）

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
echo "[INFO] 模型: $FILTER_MODEL (filter) / $SUMMARY_MODEL (summary)"
echo "[INFO] 注意: OpenRouter 不支持 Rerank API，Step 3 将自动跳过，使用 RRF 分数兜底"
echo ""

# 默认日期：最近 9 天
TODAY=$(date +%Y%m%d)
DEFAULT_START=$(date -d "9 days ago" +%Y%m%d)
START_DATE="$DEFAULT_START"
END_DATE="$TODAY"

# 解析可选参数 --start-date / --end-date
while [[ $# -gt 0 ]]; do
    case "$1" in
        --start-date)
            START_DATE="$2"
            shift 2
            ;;
        --end-date)
            END_DATE="$2"
            shift 2
            ;;
        *)
            echo "[WARN] 未知参数: $1"
            shift
            ;;
    esac
done

echo "[INFO] 区间抓取: $START_DATE ~ $END_DATE"

# 运行流水线
exec "$PYTHON" pipeline_range.py \
    --start-date "$START_DATE" \
    --end-date "$END_DATE" \
    --embedding-device cpu \
    --embedding-batch-size 8
