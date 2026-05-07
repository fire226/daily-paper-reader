#!/bin/bash
# daily-paper-reader v2 本地运行脚本
# 用法: ./run_local.sh [pipeline_range.py args...]
# 默认行为：抓取最近 9 天（今天 - 8 天 ~ 今天）

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
    echo "[ERROR] 缺少 .env 文件，请先配置本地运行所需 API Key"
    exit 1
fi

# 使用 conda 环境的 Python
PYTHON="/home/ghz/miniconda3/envs/daily-paper-reader/bin/python3"

if [ ! -x "$PYTHON" ]; then
    echo "[ERROR] 找不到 conda 环境 daily-paper-reader，请先创建: conda create -n daily-paper-reader python=3.11"
    exit 1
fi

TODAY=$(date +%Y%m%d)
DEFAULT_START=$(date -d "8 days ago" +%Y%m%d)

HAS_START=0
HAS_END=0
PIPELINE_ARGS=("$@")
for ((i=0; i<${#PIPELINE_ARGS[@]}; i++)); do
    case "${PIPELINE_ARGS[$i]}" in
        --start-date)
            HAS_START=1
            ((i+=1))
            ;;
        --end-date)
            HAS_END=1
            ((i+=1))
            ;;
    esac
done

if [ "$HAS_START" -eq 0 ]; then
    PIPELINE_ARGS+=(--start-date "$DEFAULT_START")
fi
if [ "$HAS_END" -eq 0 ]; then
    PIPELINE_ARGS+=(--end-date "$TODAY")
fi

if [ -n "${SILICONFLOW_API_KEY:-}" ]; then
    RERANK_STATUS="enabled"
else
    RERANK_STATUS="fallback-only"
fi

if [ -n "${OPENROUTER_API_KEY:-${LLM_API_KEY:-}}" ]; then
    LLM_STATUS="enabled"
else
    LLM_STATUS="skip-llm-steps"
fi

echo "[INFO] Python: $PYTHON"
echo "[INFO] Filter model: ${FILTER_MODEL:-${LLM_MODEL:-deepseek/deepseek-v3.2}}"
echo "[INFO] Rerank model: ${RERANK_MODEL:-Qwen3-Reranker-8B} ($RERANK_STATUS)"
echo "[INFO] LLM refine/enrichment: $LLM_STATUS"
echo "[INFO] 日期区间参数: ${PIPELINE_ARGS[*]}"
echo ""

exec "$PYTHON" pipeline_range.py "${PIPELINE_ARGS[@]}"
