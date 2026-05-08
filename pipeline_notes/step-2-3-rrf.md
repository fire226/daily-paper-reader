# Step 2.3 RRF

## 代码入口

- 文件：`src/step2_3_rrf.py`
- 主函数：`run_rrf_step(context, step_input)`
- 输入类型：`RRFStepInput`
- 输出类型：`RRFStepOutput`

## 输入

`RRFStepInput` 关键字段：

- `run_date`
- `bm25_output`
- `embedding_output`
- `top_k`
- `rrf_k`

其中前两个输入分别来自 Step 2.1 与 Step 2.2。

## 输出

输出文件：

- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.json`

`RRFStepOutput` 包含：

- `tagged_papers`
- `query_results`
- `stats`
- `warnings`

## 做了什么

这一层负责把关键词召回和语义召回融合成统一候选池。

它会：

1. 对齐 BM25 与 Embedding 两路 query
2. 读取两路各自的 rank 列表
3. 用 Reciprocal Rank Fusion 融合分数
4. 截断为每个 query 的融合 Top-K
5. 合并论文标签信息，得到统一 `tagged_papers`

## 怎么实现的

核心函数：

- `build_query_alignment_key()`：用 `type + paper_tag + query_text` 对齐两路 query
- `normalize_rank_list()`：把不同通道的结果统一成 rank 序列
- `fuse_query_scores()`：执行 RRF 分数融合
- `merge_tagged_papers()`：合并两路标签后的论文对象
- `write_rrf_output()`：写出融合结果

## 备注

Step 2.3 的输出既是 Step 3 的输入，也是“统一召回结果”的归档版本。
