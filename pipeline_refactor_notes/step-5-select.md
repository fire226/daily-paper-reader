# Step 5 Select

原文件：`src/5.select_papers.py`

## 现在是怎么做的

这一步根据 LLM 打分，把论文分配到 `deep_dive` 和 `quick_skim` 两个结果区，并支持多种 mode。

当前实现大致流程：

1. 读取 Step 4 结果
2. 把评分结果和论文元信息合并
3. 构造 candidates
4. 按 score 分层
5. 按 mode 做配额和分配策略
6. 输出推荐 JSON

## 当前输入输出

输入：

- Step 4 的 LLM 输出
- `config.yaml` 中的 `arxiv_paper_setting.mode`
- tag 数量统计

输出：

- `archive/<token>/recommend/arxiv_papers_<token>.<mode>.json`

## 当前问题

1. 这一步的业务规则很多，但术语还不够稳定
2. `deep_dive` / `quick_skim`、bucket、mode、priority score 这些概念写在一个文件里
3. 评分 merge 和推荐策略本应是两层逻辑
4. 字段命名已有历史痕迹，比如 `deep_divecandidates` 拼写就不统一

## 应该怎么改

建议拆成：

1. `build_scored_candidates()`
2. `SelectionPolicy`
3. `ModeConfigResolver`
4. `RecommendationAssembler`

## 建议的新接口

```python
def run_select_step(
    context: RunContext,
    llm_output: LLMRefineStepOutput,
) -> SelectStepOutput:
    ...
```

输出建议包含：

- `deep_dive`
- `quick_skim`
- `mode`
- `selection_policy`
- `output_path`
- `stats`

## 重构重点

这一步适合重点提炼“推荐规则”，而不是改算法。重构后应该能让人一眼看懂：

1. 分层标准是什么
2. 每个 mode 差异是什么
3. 为什么某篇论文进了 deep 或 quick
