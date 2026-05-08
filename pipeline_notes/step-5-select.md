# Step 5 Select

## 代码入口

- 文件：`src/step5_select.py`
- 主函数：`run_select_step(context, step_input)`
- 输入类型：`SelectStepInput`
- 输出类型：`SelectStepOutput`

## 输入

`SelectStepInput` 关键字段：

- `run_date`
- `llm_output`
- `rerank_output`
- `mode`

`mode` 支持：

- `standard`
- `extend`
- `spark`
- `skims`

## 输出

输出文件：

- `archive/YYYYMMDD/recommend/arxiv_papers_YYYYMMDD.<mode>.json`

`SelectStepOutput` 包含：

- `deep_dive`
- `quick_skim`
- `stats`
- `warnings`

## 做了什么

这一步把 Step 4 的打分结果真正转成“日报推荐清单”。

它会：

1. 把打分结果与原论文信息合并
2. 生成可选候选集
3. 按分数分层
4. 根据 `mode` 配置决定精读/速读策略
5. 产出 `deep_dive` 与 `quick_skim`

## 怎么实现的

核心实现：

- `MODES`：定义不同推荐模式
- `build_scored_papers()`：把 `ScoredItem` 合并回论文对象
- `split_score_layers()`：按分数层切片
- `round_robin_select()`：按 tag 轮转分配精读名额
- `allocate_uniform()` / `allocate_low_bias()`：速读区分配策略
- `run_select_step()`：生成最终推荐结果

## 备注

Step 5 是从“评分层”进入“产品输出层”的分界点。后续 enrichment 和 docs 都只处理这里选中的论文。
