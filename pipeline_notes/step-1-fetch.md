# Step 1 Fetch

## 代码入口

- 文件：`src/step1_fetch.py`
- 主函数：`run_fetch_step(context, step_input)`
- 输入类型：`FetchStepInput`
- 输出类型：`FetchStepOutput`

## 输入

`FetchStepInput` 关键字段：

- `run_date`：当天日期
- `ignore_seen`：是否忽略历史抓取状态
- `output_path_override`：可选输出路径覆盖
- `categories_override`：可选 arXiv 分类覆盖

`RunContext` 提供：

- `config.yaml` 路径
- `archive/` 根目录
- `crawl_state.json`
- `arxiv_seen.json`

## 输出

主要输出文件：

- `archive/YYYYMMDD/raw/arxiv_papers_YYYYMMDD.json`

`FetchStepOutput` 包含：

- `papers`：`PaperRecord` 列表
- `backend`：当前抓取后端，默认是 `arxiv`
- `artifacts.raw_output_path`
- `stats`
- `warnings`

## 做了什么

Step 1 负责生成“当天候选论文池”。

它会：

1. 根据 `run_date` 计算 UTC 日期窗口
2. 生成按大类分组的 arXiv 查询
3. 拉取当天各分类论文
4. 做去重与标准化
5. 根据历史 seen state 过滤已见论文（除非 `ignore_seen=True`）
6. 把结果写入 `raw/` 目录
7. 更新 `crawl_state.json` 与 `arxiv_seen.json`

## 怎么实现的

核心实现点：

- `build_day_bounds()`：构造当天时间窗口
- `build_arxiv_query()`：生成分类查询表达式
- `load_fetch_state()`：加载历史抓取与 seen 状态
- `fetch_arxiv_papers()`：执行实际抓取
- `deduplicate_raw_papers()`：按论文 ID 去重
- `normalize_raw_paper()`：转换成统一 `PaperRecord`
- `write_fetch_output()`：输出原始 JSON
- `persist_fetch_state()`：更新状态文件

## 备注

这是后续所有检索步骤的上游输入，后续 Step 2.x 都直接消费这里产出的 `papers` 或 raw JSON。
