# Step 2.1 BM25

## 代码入口

- 文件：`src/step2_1_bm25.py`
- 主函数：`run_bm25_step(context, step_input)`
- 输入类型：`BM25StepInput`
- 输出类型：`BM25StepOutput`

## 输入

`BM25StepInput` 关键字段：

- `run_date`
- `papers`：来自 Step 1 的 `PaperRecord` 列表
- `queries`：`RetrievalQuery` 列表
- `top_k`：每个 query 保留的候选数

查询通常通过 `load_retrieval_queries(context.config)` 读取，底层来自 `subscription_plan.build_pipeline_inputs()`。

## 输出

输出文件：

- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.bm25.json`

`BM25StepOutput` 包含：

- `tagged_papers`
- `query_results`
- `stats`
- `warnings`

其中 `query_results[*].sim_scores` 保存每个 query 命中的论文分数与 rank。

## 做了什么

这一层是本地关键词检索通道。

它会：

1. 从订阅配置构造 BM25 查询
2. 为每篇论文拼接 `Title + Abstract` 文本
3. 建立轻量 BM25 倒排索引
4. 对每个 query 排序并保留 Top-K
5. 为命中的论文挂上 query 对应的 `paper_tag`

## 怎么实现的

核心函数：

- `tokenize()`：中英文混合 token 切分
- `paper_text_for_bm25()`：构造检索文本
- `BM25Index`：本地 BM25 索引实现
- `load_retrieval_queries()`：从订阅配置读取 query
- `estimate_dynamic_top_k()`：当 CLI 未指定 `top_k` 时，按候选规模估算默认值
- `build_tagged_papers()`：把 query 命中标签回填到论文对象
- `write_bm25_output()`：输出 JSON

## 备注

Step 2.1 和 Step 2.2 是并列召回通道，后续由 Step 2.3 统一融合。
