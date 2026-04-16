# Daily Paper Reader

每日自动抓取 arXiv 新论文，基于关键词和向量检索筛选，生成精读区和速览区推荐结果。

## 快速启动

```bash
./run_local.sh
```

首次运行前需在 `config.yaml` 中配置订阅关键词和大模型 API Key。

## 处理流程

```
Step 1  Fetch    → 从 arXiv 抓取指定日期范围的论文元数据
Step 2  Retrieval → BM25 关键词检索 + Embedding 向量检索，双路并行
Step 2  RRF      → Reciprocal Rank Fusion 融合两路结果，取 Top N
Step 4  LLM Refine→ 调用大模型对候选论文打分（0-10）
Step 5  Select   → 按评分分配精读区（高分）和速览区（中分）
Step 6  Docs     → 生成 Markdown 文档输出到 docs/ 目录
```

每日运行落入 `archive/{YYYYMMDD}/`，文档输出到 `docs/YYYY/MM/DD/`。

## 区间批量抓取

```bash
python pipeline_range.py --start-date 20260401 --end-date 20260410
```

## 配置文件

- `config.yaml` — 订阅关键词、检索参数、推荐模式
- `.env` — 大模型 API Key 和 Base URL
- `subscriptions.json` — 各 tag 的订阅配置

## FAQ

### 需要服务器吗？

不需要。项目完全本地运行。

### 每天处理多少篇论文？

由 `config.yaml` 中的 `days_window` 控制默认窗口天数。各步骤逐级筛选：从原始 ~2000篇/天 → RRF TopN（默认200）→ LLM评分后按评分分配精读区和速览区（各约5-10篇）。

### 支持其他论文源吗？

目前主要支持 arXiv。其他论文源（bioRxiv、medRxiv、ChemRxiv、会议论文等）的抓取链路部分存在但需要额外配置。
