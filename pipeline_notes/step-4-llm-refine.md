# Step 4 LLM Refine

## 代码入口

- 文件：`src/step4_llm_refine.py`
- 主函数：`run_llm_refine_step(context, step_input)`
- 输入类型：`LLMRefineStepInput`
- 输出类型：`LLMRefineStepOutput`

## 输入

`LLMRefineStepInput` 关键字段：

- `run_date`
- `rerank_output`
- `min_star`
- `batch_size`
- `max_chars`
- `filter_model`
- `max_output_tokens`
- `filter_concurrency`

## 输出

输出文件：

- `archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.llm.json`

`LLMRefineStepOutput` 包含：

- `scored_items`
- `stats`
- `warnings`

每个 `ScoredItem` 至少包含：

- `paper_id`
- `score`
- `evidence_en` / `evidence_cn`
- `tldr_en` / `tldr_cn`
- `matched_query_tag`
- `matched_query_text`

## 做了什么

这一步是“面向用户订阅语义”的精筛评分层。

它会：

1. 从 `subscription_plan.py` 生成用户需求列表
2. 从 Step 3 的全局候选池中筛出需要打分的论文
3. 按批次调用 LLM
4. 对每篇论文给出 0-10 分评分、证据、TLDR 与匹配 query
5. 合并并写出最终打分结果

## 怎么实现的

核心函数：

- `build_user_requirements()`：把 `config.yaml` 订阅意图转成 LLM 评分需求
- `build_candidate_papers()`：从 rerank 结果中构造候选论文集合
- `chunk_candidates()`：按 `batch_size` 分批
- `score_batch_once()` / `score_batch_with_recovery()`：单批评分与失败恢复
- `merge_scored_items()`：汇总批次输出
- `run_llm_refine_step()`：调度并发评分流程

## 备注

Step 4 的输出是 Step 5 选稿的唯一评分来源。
