# Bug Fix: pipeline_range.py 单日窗口被错误扩大为 days_window 滚动窗口

## 问题

`DPR_RUN_DATE=20260406`（单日）时，`resolve_supabase_recall_window` 因 `days_window=9` 返回 9 天滚动窗口，
导致 04-06 的抓取实际拉取了 ~04-05~04-14 的论文，04-12 才 published 的论文出现在 04-06 结果中。

## 修改清单

### 1. pipeline_range.py
- 位置：循环体内 `day_env` 设置处（约 L124-125）
- 改动：添加 `day_env["DPR_SINGLE_DAY"] = "1"` 并补充注释说明语义

### 2. src/2.1.retrieval_papers_bm25.py
- 位置：`resolve_supabase_recall_window` 函数，单日分支（L132-136 区域）
- 改动：在 `DATE_RE_DAY.fullmatch(token)` 分支内，`safe_days > 1` 判断前，
  添加 `DPR_SINGLE_DAY == "1"` 的 early return

### 3. src/2.2.retrieval_papers_embedding.py
- 位置：`resolve_supabase_recall_window` 函数，单日分支（L132-136 区域）
- 改动：同上

### 4. src/maintain/fetchers/fetch_arxiv.py
- 位置：`resolve_supabase_time_window` 函数，单日分支（L81-90 区域）
- 改动：在 `re.match(r"^\d{8}$", token)` 分支内，`safe_days > 1` 判断前，
  添加 `DPR_SINGLE_DAY == "1"` 的 early return

## 环境变量语义

- `DPR_SINGLE_DAY=1`：pipeline_range.py 模式，严格按 DPR_RUN_DATE 指定的单日计算窗口
- 未设置 / 其他值：main.py 模式，保持原有滚动窗口行为（days_window 配置生效）
