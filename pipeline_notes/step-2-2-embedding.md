# Step 2.2 Embedding

## 代码入口

- 文件：`src/step2_2_embedding.py`
- 主函数：`run_embedding_step(context, step_input)`
- 输入类型：`EmbeddingStepInput`
- 输出类型：`EmbeddingStepOutput`

## 输入

`EmbeddingStepInput` 关键字段：

- `run_date`
- `papers`
- `queries`
- `top_k`
- `model_name`
- `device`
- `batch_size`
- `max_length`

## 输出

输出文件：

- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.embedding.json`

`EmbeddingStepOutput` 结构与 BM25 类似，也包含：

- `tagged_papers`
- `query_results`
- `stats.embedding_backend`
- `warnings`

## 做了什么

这一层是语义向量召回通道。

它会：

1. 把论文转成 `passage:` 前缀文本
2. 把 query 转成向量
3. 计算 query 与论文向量的相似度
4. 为每个 query 保留 Top-K
5. 给命中论文补上对应 tag

## 怎么实现的

核心函数：

- `EmbeddingPaperView.text_for_embedding`：构造论文向量输入文本
- `load_embedding_model()`：加载 sentence-transformer
- `set_max_seq_length()`：限制编码长度
- `compute_paper_embeddings()`：批量编码论文
- `rank_queries()`：编码 query 并计算向量相似度排序
- `write_embedding_output()`：输出 JSON

依赖模块：

- `model_loader.py`：本地/远程 embedding 模型加载
- `filter.encode_queries()`：统一 query 编码入口

## 备注

Step 2.2 与 Step 2.1 使用相同的 `RetrievalQuery` 结构，因此后续 RRF 可以按 query 对齐融合。
