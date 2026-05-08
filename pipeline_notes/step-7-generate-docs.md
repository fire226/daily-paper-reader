# Step 7 Generate Docs

## 代码入口

- 文件：`src/step7_generate_docs.py`
- 主函数：`run_generate_docs_step(context, step_input)`
- 输入类型：`GenerateDocsStepInput`
- 输出类型：`GenerateDocsStepOutput`

## 输入

`GenerateDocsStepInput` 关键字段：

- `run_date`
- `select_output`
- `enrichment_output`
- `mode`
- `output_dir_override`

## 输出

直接写入 `docs/` 目录：

- `docs/YYYY/MM/DD/*.md`：单篇论文页
- `docs/YYYY/MM/DD/README.md`：当日日报
- `docs/_sidebar.md`：Docsify 侧边栏

`GenerateDocsStepOutput` 记录：

- `artifacts.docs_dir`
- `artifacts.paper_paths`
- `artifacts.sidebar_path`
- `stats.papers_generated`
- `stats.daily_report_generated`
- `stats.sidebar_updated`

## 做了什么

这一步把推荐结果正式落成 Docsify 可展示内容。

它会：

1. 为每篇推荐论文生成 Markdown 页面
2. 生成当天总报告 `README.md`
3. 把当天内容插入 `_sidebar.md`

## 怎么实现的

核心函数：

- `resolve_docs_dir()`：解析文档输出目录
- `prepare_paper_paths()`：确定单篇文档路径
- `prepare_day_report_paths()`：确定当日日报路径
- `build_markdown_content()`：生成单篇论文 Markdown（含 front matter、摘要、TLDR、详细总结等）
- `build_day_report_markdown()`：生成当日日报
- `update_sidebar()`：把当天精读/速读论文写入 Docsify 侧边栏
- `run_generate_docs_step()`：执行全部落盘逻辑

## 与 `pipeline_range.py` 的分工

Step 7 自己只负责“当天文档页 + 当天侧边栏块”。

跨日期的两个后处理不在 Step 7 中，而在 `pipeline_range.py` 收尾阶段完成：

- `sort_sidebar_by_date()`：按日期倒序整理 `_sidebar.md`
- `write_home_readme()`：把最新一期日报同步到 `docs/README.md`

因此，Step 7 负责单日展示产物，`pipeline_range.py` 负责全局首页与侧边栏的最终整理。
