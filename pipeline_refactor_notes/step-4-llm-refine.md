# Step 4 LLM Refine

原文件：`src/4.llm_refine_papers.py`

## 现在是怎么做的

这一步是业务价值很高的一步：根据用户订阅需求，用 LLM 给候选论文打分，并生成证据、TLDR、匹配 query 等信息。

当前文件做了很多事：

1. 读取排序后的候选论文
2. 从配置构造 user requirements
3. 生成 prompt
4. 并发调用 LLM
5. 重试与恢复
6. 结果去重与合并
7. 生成输出 JSON

## 当前输入输出

输入：

- Step 3 输出的排序结果
- `config.yaml` 中的订阅配置
- LLM 环境变量

输出：

- `archive/<token>/rank/arxiv_papers_<token>.llm.json`

## 当前问题

1. 用户需求构造、评分执行、结果整理混在一起
2. 这一步已经不只是“refine”，实际上承担了业务判断核心
3. prompt 规则、字段契约、恢复逻辑都很多，但没有被清晰分层
4. 对新人来说最难看懂，因为这里既有业务语义又有工程容错

## 应该怎么改

建议把它拆成三个层次：

1. `RequirementBuilder`
2. `LLMRefineExecutor`
3. `LLMRefineResultAssembler`

## 建议的新接口

```python
def run_llm_refine_step(
    context: RunContext,
    rerank_output: RerankStepOutput,
) -> LLMRefineStepOutput:
    ...
```

输出建议包含：

- `scored_items`
- `requirements`
- `matched_queries`
- `output_path`
- `stats`
- `failures`
- `warnings`

## 重构重点

未来这里应该做到两点：

1. “用户意图建模”是单独可读的
2. “LLM 评分执行”是单独可测的

只有这样，这一步才不会继续成为整个流水线里最大的黑盒。
