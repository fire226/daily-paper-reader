# Step 6 Enrichment

## 代码入口

- 文件：`src/step6_enrichment.py`
- 主函数：`run_enrichment_step(context, step_input)`
- 输入类型：`EnrichmentStepInput`
- 输出类型：`EnrichmentStepOutput`

## 输入

`EnrichmentStepInput` 关键字段：

- `run_date`
- `select_output`
- `rerank_output`
- `llm_model`
- `max_output_tokens`

## 输出

输出文件：

- `archive/YYYYMMDD/enriched/arxiv_papers_YYYYMMDD.enriched.json`

`EnrichmentStepOutput` 包含：

- `enriched_papers`
- `stats`
- `warnings`

每条 `EnrichedPaper` 可能包含：

- `title_zh`
- `abstract_zh`
- `glance_motivation`
- `glance_method`
- `glance_result`
- `glance_conclusion`
- `deep_summary`

## 做了什么

这一步把推荐论文进一步加工成适合展示的中文内容。

它会：

1. 批量翻译所有推荐论文的标题与摘要
2. 批量生成四段速览信息（动机/方法/结果/结论）
3. 对精读论文额外抓取全文文本
4. 生成精读论文的详细中文总结

## 怎么实现的

核心函数：

- `translate_batch()`：批量翻译 title / abstract
- `glance_batch()`：批量生成速览四元组
- `fetch_paper_text()`：通过 `https://r.jina.ai/<pdf_url>` 拉取全文，并缓存到 `archive/YYYYMMDD/txt/`
- `generate_deep_summaries()`：仅对精读论文生成长摘要
- `write_enrichment_output()`：输出增强结果

## 备注

- 若缺少 `OPENROUTER_API_KEY` / `LLM_API_KEY`，整步会跳过并写 warning。
- `pipeline_range.py` 也支持 `--skip-enrichment` 完全跳过这一步。
