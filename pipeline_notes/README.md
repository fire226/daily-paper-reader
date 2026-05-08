# Pipeline Notes

## 总体定位

当前 pipeline 的主入口是仓库根目录下的 `pipeline_range.py`。

它负责：

- 加载 `.env`
- 构建 `RunContext`
- 解析日期区间与运行参数
- 按天顺序执行新版日粒度 pipeline
- 对已有中间产物做复用与跳过
- 在全部日期跑完后整理 `docs/README.md` 和 `_sidebar.md`

当前实现不是“重构中的草稿”，而是已经投入使用的新版 pipeline。文档只解释现状，不再讨论旧版脚本迁移策略。

## 执行顺序

单日执行顺序如下：

1. `step1_fetch.py`
2. `step2_1_bm25.py`
3. `step2_2_embedding.py`
4. `step2_3_rrf.py`
5. `step3_rerank.py`
6. `step4_llm_refine.py`
7. `step5_select.py`
8. `step6_enrichment.py`
9. `step7_generate_docs.py`

其中 Step 2 被拆成三段：BM25、Embedding、RRF 融合。

## 主调度结构

`pipeline_range.py` 中最重要的函数：

- `main()`：解析 CLI，确定日期范围与全局参数。
- `run_day_pipeline()`：执行单天任务。
- `try_load_output()`：已有产物存在时，尝试直接复用。
- `ensure_docs_runtime_files()`：确保 `docs/_sidebar.md` 与 `docs/_404.md` 存在。
- `sort_sidebar_by_date()`：所有日期完成后，对侧边栏按日期倒序重排。
- `write_home_readme()`：把最新一日报告同步成 `docs/README.md` 首页。

## 共享上下文

几乎所有 step 都接收 `step1_fetch.RunContext`，里面包含：

- `root_dir`
- `archive_root`
- `config_path`
- `crawl_state_path`
- `seen_state_path`
- `config`

也就是说，配置文件与归档目录是在 Step 1 层统一建模的，后续 step 共享同一个上下文。

## 订阅配置如何进入 pipeline

配置源头是 `config.yaml`，但并不是每个 step 都自己直接解析全部订阅结构。

统一入口是 `src/subscription_plan.py`：

- 负责把 `subscriptions.intent_profiles` 归一化
- 负责处理 runtime filter（如环境变量指定的 profile/tag）
- 产出不同 step 可直接消费的查询输入

其中：

- Step 2.1 / Step 2.2 使用 `bm25_queries`
- Step 4 使用 `context_queries`

因此，`subscription_plan.py` 是“订阅配置到检索/评分输入”的中间层。

## 产物目录

以某一天 `YYYYMMDD` 为例：

- `archive/YYYYMMDD/raw/arxiv_papers_YYYYMMDD.json`
  - Step 1 原始抓取结果
- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.bm25.json`
  - Step 2.1 BM25 检索结果
- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.embedding.json`
  - Step 2.2 向量检索结果
- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.json`
  - Step 2.3 RRF 融合结果
- `archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.rerank.json`
  - Step 3 重排结果
- `archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.llm.json`
  - Step 4 LLM 打分结果
- `archive/YYYYMMDD/recommend/arxiv_papers_YYYYMMDD.<mode>.json`
  - Step 5 推荐结果
- `archive/YYYYMMDD/enriched/arxiv_papers_YYYYMMDD.enriched.json`
  - Step 6 增强结果
- `archive/YYYYMMDD/txt/*.txt`
  - Step 6 为精读论文缓存的全文文本

文档输出：

- `docs/YYYY/MM/DD/*.md`
  - 单篇论文页
- `docs/YYYY/MM/DD/README.md`
  - 当日总报告
- `docs/_sidebar.md`
  - Docsify 侧边栏
- `docs/README.md`
  - 首页，显示最新一期日报摘要

## 跳过与复用策略

`pipeline_range.py` 会优先检查目标产物是否已存在：

- 存在且 `--force-existing` 未开启：尝试加载旧产物并跳过该 step
- 旧产物损坏或加载失败：重新计算该 step
- 若当日日报 `docs/YYYY/MM/DD/README.md` 已存在，整个单日任务可直接跳过

这意味着 pipeline 具备比较细粒度的断点续跑能力。

## 运行参数覆盖关系

覆盖优先级大体如下：

1. CLI 参数
2. `config.yaml`
3. 各 step 内置默认值

典型例子：

- `mode`：优先 CLI，其次 `config.yaml` 的 `arxiv_paper_setting.mode`
- `embedding-model-name`：优先 CLI，否则使用 `step2_2_embedding.DEFAULT_EMBED_MODEL`
- `docs_dir`：优先 `--docs-dir-override`，其次 `config.yaml`，最后默认 `docs/`

## 外部依赖边界

当前新版 pipeline 的外部依赖分为四类：

- 抓取：`arxiv`
- 向量模型：`sentence-transformers` 或远程 embedding fallback
- 重排：SiliconFlow Reranker（可关闭）
- LLM：OpenRouter / OpenAI-compatible 接口

其中最重要的降级逻辑：

- Step 3 可通过 `--disable-rerank` 强制走本地 fallback
- Step 6 可通过 `--skip-enrichment` 完全跳过

因此即使部分外部能力不可用，pipeline 仍可在降级模式下完成主流程。

## 各 step 文档

- `step-1-fetch.md`
- `step-2-1-bm25.md`
- `step-2-2-embedding.md`
- `step-2-3-rrf.md`
- `step-3-rerank.md`
- `step-4-llm-refine.md`
- `step-5-select.md`
- `step-6-enrichment.md`
- `step-7-generate-docs.md`
