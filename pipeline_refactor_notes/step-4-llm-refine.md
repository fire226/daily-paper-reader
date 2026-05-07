# Step 4 LLM Refine

原文件（已归档）：`src/legacy_archive/4.llm_refine_papers.py`

## 现在是怎么做的

这一步用 LLM 给候选论文打分（0-10），并生成中英文证据和 TLDR。

### 输入

- Step 3 输出（取候选论文和 ranked）
- `config.yaml` 中的订阅配置（取 `user_requirements`）
- LLM 环境变量

### 输出

- `archive/<token>/rank/arxiv_papers_<token>.llm.json`

### 具体流程

1. 从 config 构造 `user_requirements`（`direct` + `composite`）
2. 从 Step 3 的 `ranked_queries[*].ranked` 里筛出 `star_rating >= min_star` 的论文作为候选
3. 随机打乱，按 `batch_size`（默认 10）分批，并发调用 LLM（默认 4 并发）
4. 验证 LLM 返回（每篇论文都必须有结果），失败则重试
5. 合并结果，同一篇论文取 score 最高的
6. 写到 `.llm.json` 文件

## `user_requirements` 是什么

`user_requirements` 是一组"用户需求描述"，每条是一个 dict，格式如下：

```python
{
    "id": "req-1",
    "query": "unified multimodal information extraction ...",
    "tag": "query:unified-multimodal-information-extraction",
    "kind": "direct",
    "description_en": "Find papers relevant to this user requirement: ..."
}
```

这些需求会被拼进 LLM prompt，告诉 LLM "用户想找什么方向的论文"，LLM 再据此给每篇候选论文打分。

### 来源：只从 config 构造

v2 的 `user_requirements` 只从 config 构造，不读 Step 3 文件。如果 config 构造出的 requirements 为空，报错终止。

有两种需求类型：

#### `direct`：从 `context_queries` 构造

调用 `build_pipeline_inputs(config).context_queries`，返回的是 config 里所有 profile 的 keywords 和 intent_queries 的合并列表。每项格式：

```python
{"tag": "query:<tag>", "query": "<query_text>", "logic_cn": "..."}
```

keyword 的语义 query 和 intent_query 混在一起。对每条构造一个 `kind="direct"` 的需求：

```python
{
    "id": "req-1",
    "query": "extracting structured information from unstructured documents",
    "tag": "query:nlp-and-vision",
    "kind": "direct",
    "description_en": "Find papers relevant to this user requirement: ..."
}
```

#### `composite`：从 profile 构造

对每个 profile，把它的 keywords 和 intent_queries 拼成一条综合需求。只在某个 profile 下有 2 条以上条目时才生成。

```python
{
    "id": "req-composite-nlp-and-vision",
    "query": "Papers central to NLP and multimodal vision research, especially work that connects or combines: A; B; C.",
    "tag": "query:nlp-and-vision:composite",
    "kind": "composite",
    "description_en": "Find papers central to the combined ... Consider these signals together: ..."
}
```

### prompt 里的效果

所有 requirements 拼成一个列表，放在 prompt 里：

```
User requirements list:
1. Find papers relevant to this user requirement: extracting structured information ... [tag=query:information-extraction; type=direct]
2. Find papers relevant to this user requirement: multimodal document understanding ... [tag=query:multimodal; type=direct]
3. Find papers central to the combined NLP and Vision theme. Consider these signals together: ... [tag=query:nlp-and-vision:composite; type=composite]
```

LLM 逐篇论文对比所有 requirements，找到最匹配的那条，给出分数和 `matched_requirement_index`。

每次 API 调用包含**所有 requirements + 一个 batch 的论文**（默认 10 篇），不是每篇论文单独调。

## 参数调整

### `max_chars`：2000

原版默认 850 字符，实测 95% 的论文被截断，平均每篇丢失 608 字符（约 43% 的 Abstract 内容）。

调整为 2000 字符，覆盖 99% 的论文。调整后每篇论文约 500 tokens，对总上下文影响很小。

| 限制 | 截断率 | 平均丢失字符 |
|---|---|---|
| 850（原版） | 95% | 608 |
| 2000（v2） | ~1% | - |

### `batch_size`：25

原版默认 10。调高到 25 可以减少 API 调用次数，同时 25 篇论文 + requirements + rubric 总计约 12K tokens，远低于 128K 限制。

默认 `min_star=4` 时，实测候选论文通常在 10 篇以内，一次 batch 就够了。

## 当前问题

1. 用户需求构造、评分执行、结果整理混在一个 934 行的文件里
2. 候选论文筛选用了 `unique_tagged` 做 paper id 去重，语义不清晰
3. 输出是就地修改 Step 3 JSON，不是独立产物
4. 重试机制过于复杂（递归劈半 split recovery）
5. LLM 客户端硬编码为 `OpenRouterClient`，但没有显式声明

## 解决方案

### 问题 1：职责混杂

拆成三层：

- `step4_requirement_builder.py`：从 config 构造 `user_requirements`（`direct` + `composite`）
- `step4_llm_refiner.py`：prompt 构造、LLM 调用、验证、重试、结果合并
- `step4_llm_refine.py`（CLI 入口）：串联上面两层

### 问题 2：候选论文筛选逻辑

直接用 set 去重，不用 `unique_tagged`：

```python
candidate_ids = []
seen = set()
for q in ranked_queries:
    for item in q.ranked:
        if item["star_rating"] >= min_star and item["paper_id"] not in seen:
            seen.add(item["paper_id"])
            candidate_ids.append(item["paper_id"])
```

### 问题 3：就地修改

产出独立 JSON 文件：

- 输入：`archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.rerank.json` + config
- 输出：`archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.llm.json`

不再在 Step 3 文件上追加字段。

### 问题 4：重试机制

带 retry note 重试，最多 3 次。3 次都失败后直接报错，不再逐篇单独重试（单篇送入 LLM 没有其他论文参照，评分不可靠）。

去掉原版的递归劈半 split recovery。

### 问题 5：硬编码要求

保持原方案：LLM 必须为每篇输入论文返回一条结果，不相关的返回 `score=0`。

### 问题 6：LLM 客户端

直接用 `OpenRouterClient`，不走 `ClientFactory`。项目预计只使用 OpenRouter 的 API。

## 建议的新接口

```python
def build_user_requirements(config: dict[str, Any]) -> list[LLMRefineRequirement]:
    ...

def run_llm_refine_step(
    context: RunContext,
    step_input: LLMRefineStepInput,
) -> LLMRefineStepOutput:
    ...
```

## 建议的文件输入输出

输入：

- `archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.rerank.json`（候选论文和 ranked）
- `config.yaml`（构造 user_requirements）

输出：

- `archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.llm.json`
