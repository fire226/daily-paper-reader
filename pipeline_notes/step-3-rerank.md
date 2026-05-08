# Step 3 Rerank

## 代码入口

- 文件：`src/step3_rerank.py`
- 主函数：`run_rerank_step(context, step_input)`
- 输入类型：`RerankStepInput`
- 输出类型：`RerankStepOutput`

## 输入

`RerankStepInput` 关键字段：

- `run_date`
- `rrf_output`
- `top_n`
- `rerank_model`
- `disable_rerank`

## 输出

输出文件：

- `archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.rerank.json`

`RerankStepOutput` 包含：

- `papers`：全局候选论文对象
- `global_candidate_ids`
- `ranked_queries`
- `stats.used_rerank`
- `stats.fallback_used`

## 做了什么

这一层的目标不是再次“召回”，而是把 RRF 候选池变成更适合后续 LLM 精筛的全局排序结果。

它会：

1. 从 Step 2.3 中筛出 intent query
2. 构造全局候选池
3. 如果允许外部 rerank，则调用 SiliconFlow Reranker
4. 如果关闭或失败，则用本地 fallback 排序
5. 为每个 intent query 生成 `ranked` 列表

## 怎么实现的

核心函数：

- `is_intent_query()` / `select_intent_queries()`：筛出意图查询
- `resolve_global_pool_budget()`：估算全局候选池预算
- `build_global_candidate_pool()`：构造跨 query 的候选集合
- `SiliconFlowRerankClient.rerank()`：调用远程 reranker
- `fallback_rank_query()`：本地退化排序
- `run_rerank_step()`：统一调度 rerank 与 fallback

## 备注

- `--disable-rerank` 可强制走 fallback。
- 这一步的输出决定 Step 4 需要逐批打分的候选论文范围。
