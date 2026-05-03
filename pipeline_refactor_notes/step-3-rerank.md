# Step 3 Rerank

原文件：`src/3.rank_papers.py`

## 现在是怎么做的

这一步使用 SiliconFlow rerank API 对候选论文做进一步重排序。

它的当前业务链路是：

1. 读取 Step 2.3 的融合结果
2. 从所有 query 的结果里构造一个统一候选池
3. 只对 `intent_query` 这类语义查询执行 rerank
4. 把 rerank 结果写回到 query 对象上的 `ranked` 字段
5. 输出一个新的 rank 文件

旧实现里，Step 3 更像是“在 Step 2.3 输出 JSON 上继续补字段”，而不是定义一个新的、边界清晰的步骤产物。

## 当前输入输出

旧实现的输入：

- Step 2.3 输出的融合 JSON
- rerank 模型配置

旧实现的输出：

- `archive/<token>/rank/arxiv_papers_<token>.json`

旧输出文件的特点是：

1. 基本延续了 Step 2.3 的 `papers + queries` 结构
2. 只是在部分 query 上追加了 `ranked`
3. 仍然保留了很多后续并不会继续使用的字段

## 当前问题

1. 它在逻辑上是 Step 3，但实现上更像“就地增强 Step 2.3 结果”
2. “执行 rerank”和“构造全局候选池”耦在一起
3. rerank provider 选择不是步骤内部的显式策略，而是环境驱动
4. 输出仍然保留全部 query，但真正有用的其实只是 reranked intent queries
5. 对接手者来说，很容易搞不清这一步是不是必须存在，以及它到底产出了什么新信息

## 这一步原本到底是做什么的

在新的 day-scoped pipeline 里，Step 3 应该被重新定义为：

给定 Step 2.3 的融合结果，先构造统一候选池，再只针对语义 intent queries 做更贵但更准的 rerank，产出一份面向 Step 4 的精排结果。

它不应该再负责：

1. 继续保留全部 Step 2 query 结构当作主要输出
2. 让调用方误以为它只是“修改了 Step 2.3 文件”

换句话说，Step 3 的职责应当非常单纯：

1. 从 Step 2 的多 query 召回结果构造统一候选池
2. 只对 intent queries 做 rerank
3. 输出后续真正需要的最小信息集

## Step 3 蓝图

Step 3 的接口应当围绕三个概念组织：

1. `RunContext`：运行环境
2. `RerankStepInput`：这次 rerank 任务
3. `RerankStepOutput`：rerank 产物

### `RunContext`

Step 3 继续复用流水线统一的 `RunContext`，至少应包含：

```python
@dataclass(slots=True)
class RunContext:
    root_dir: Path
    archive_root: Path
    config_path: Path
    config: dict[str, Any]
```

### `RerankQuery`

Step 3 的输入虽然来自 Step 2.3，但输出不需要继续保留所有 query。
建议只保留真正参与 rerank 的 intent queries，并显式表示它们的 rerank 结果。

```python
@dataclass(slots=True)
class RerankQuery:
    type: str
    tag: str
    paper_tag: str
    query_text: str
    logic_cn: str = ""
    ranked: list[dict[str, Any]] = field(default_factory=list)
```

- `type`：应主要是 `intent_query`。
- `tag`：主题标签。
- `paper_tag`：对应论文标签。
- `query_text`：rerank 使用的语义查询文本。
- `logic_cn`：辅助说明。
- `ranked`：这条 query 在 rerank 后的结果列表，每项至少包含 `paper_id`、`score`、`star_rating`。

### `RerankStepInput`

```python
@dataclass(slots=True)
class RerankStepInput:
    run_date: date
    rrf_output: RRFStepOutput
    top_n: int | None = None
    rerank_model: str | None = None
    output_path_override: Path | None = None
```

- `run_date`：本次 rerank 对应的自然日。
- `rrf_output`：Step 2.3 的结构化输出。
- `top_n`：每条 reranked intent query 最终保留多少篇论文。
- `rerank_model`：可选 rerank 模型覆盖。
- `output_path_override`：可选输出路径覆盖，主要给测试或特殊运行使用。

### `RerankStats`

```python
@dataclass(slots=True)
class RerankStats:
    used_rerank: bool = False
    fallback_used: bool = False
    intent_queries_total: int = 0
    global_candidate_count: int = 0
    ranked_queries: int = 0
```

- `used_rerank`：本次是否真的调用了 rerank 模型。
- `fallback_used`：本次是否走了 fallback 路径。
- `intent_queries_total`：输入里可用于 rerank 的 intent query 数量。
- `global_candidate_count`：统一候选池大小。
- `ranked_queries`：真正产生 rerank 结果的 query 数量。

### `RerankArtifacts`

```python
@dataclass(slots=True)
class RerankArtifacts:
    output_path: Path | None = None
```

- `output_path`：Step 3 结果文件的实际落盘位置。

### `RerankStepOutput`

```python
@dataclass(slots=True)
class RerankStepOutput:
    run_date: date | None = None
    papers: list[TaggedPaperRecord] = field(default_factory=list)
    global_candidate_ids: list[str] = field(default_factory=list)
    ranked_queries: list[RerankQuery] = field(default_factory=list)
    artifacts: RerankArtifacts = field(default_factory=RerankArtifacts)
    stats: RerankStats = field(default_factory=RerankStats)
    warnings: list[str] = field(default_factory=list)
```

- `run_date`：本次结果对应的自然日。
- `papers`：候选论文池本身，供 Step 4 继续使用。
- `global_candidate_ids`：统一候选池论文 id 列表。
- `ranked_queries`：只包含 intent queries 的 rerank 结果。
- `artifacts`：文件产物信息。
- `stats`：统计信息。
- `warnings`：非致命问题列表。

## 为什么不再保留全部 query

在旧实现里，Step 3 输出仍然保留全部 query，但从业务上看，这并不是必须的。

原因是：

1. Step 3 只会对 `intent_query` 做 rerank
2. Step 4 继续使用的也是这些 reranked intent query 结果
3. keyword queries 在 Step 3 之后通常不再提供新的业务价值

因此，v2 应当收紧成“只保存后续真正需要的信息”，而不是继续复制整份 Step 2.3 结构。

也就是说，Step 3 不再是“对 Step 2.3 JSON 原地补字段”，而是产出一份新的、面向 Step 4 的 rerank 结果文件。

## `build_global_candidate_ids(...)` 到底在做什么

这是 Step 3 的核心预处理逻辑。

它做的不是 Step 2.3 那种“同一条 query 内部，两条 lane 的 RRF 融合”；而是：

1. 读取 Step 2.3 已经融合好的所有 query 结果
2. 把每条 query 的 top ids 拿出来
3. 先为每条 query 保底保留一小段候选论文
4. 再对不同 query 之间做一次全局 rank-based RRF 聚合
5. 得到一个统一候选池 `global_candidate_ids`

所以：

1. Step 2.3 的 RRF：是同一条 query 内部，BM25 + embedding 的 lane fusion
2. Step 3 的全局 RRF：是不同 query 之间的 cross-query aggregation

它的业务目标是：

1. 不让任意一个 query 完全失去代表性
2. 同时优先保留那些被多个 query 共同支持的论文
3. 控制后续 rerank API 的成本

## 应该怎么改

新的 Step 3 应该明确收敛成下面这条链路：

1. 接收 Step 2.3 的融合输出
2. 从全部 query 结果中构造统一候选池 `global_candidate_ids`
3. 只筛出 intent queries 作为 rerank query 集合
4. 对统一候选池中的论文执行 rerank
5. 输出只包含后续真正需要的信息，而不是复制整份 Step 2.3 结构

## 建议的文件输入输出

在 day-scoped pipeline 里，Step 3 的默认文件边界应该是：

输入：

- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.json`

输出：

- `archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.rerank.json`

这里建议显式使用 `.rerank.json` 后缀，而不是继续复用一个模糊的 `rank/arxiv_papers_YYYYMMDD.json`，这样更容易看出：

1. 它不是 Step 2.3 的原地修改版本
2. 它是一个新的、独立的 Step 3 产物

## 重构重点

这一步最关键的不是“换 rerank 模型”，而是把输出边界收干净：

1. 不再保留全部 query，只保留 reranked intent queries
2. 不再伪装成对 Step 2.3 的就地增强，而是定义独立产物
3. 把全局候选池构造与 rerank 输出结构显式化
4. 始终产出统一格式，即使内部走 fallback 也不改变外部接口
