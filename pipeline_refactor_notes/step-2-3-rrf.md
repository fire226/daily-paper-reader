# Step 2.3 RRF

原文件：`src/2.3.retrieval_papers_rrf.py`

## 现在是怎么做的

这一步读取 BM25 和 Embedding 的结果，按 query 对齐后用 RRF 融合，得到统一候选集。

旧实现的核心动作是：

1. 读取 `*.bm25.json` 和 `*.embedding.json`
2. 为两边 query 生成稳定的对齐 key
3. 对齐 BM25 和 embedding 的同一条 query
4. 提取两边各自的 rank list
5. 按 RRF 规则融合 rank
6. 合并两边的论文池与 tags
7. 输出 fused JSON

这一步在旧实现里已经比 2.1 / 2.2 清晰很多，但仍然是脚本式组织，领域对象和契约还没有被显式定义出来。

## 当前输入输出

旧实现的输入：

- `archive/<token>/filtered/arxiv_papers_<token>.bm25.json`
- `archive/<token>/filtered/arxiv_papers_<token>.embedding.json`

旧实现的输出：

- `archive/<token>/filtered/arxiv_papers_<token>.json`

旧输出文件里主要包含：

1. `papers`
2. `queries`
3. 每个 query 的融合后 `sim_scores`

## 当前问题

1. query 对齐规则很关键，但仍然藏在脚本内工具函数里
2. `make_query_key()`、paper merge、query merge 的契约没有显式文档化
3. RRF 结果仍然沿用旧式 JSON 结构，没有抽象为明确领域对象
4. 输入结构依赖 2.1 / 2.2 的旧 JSON 约定，接口没有真正对象化

## 这一步原本到底是做什么的

在新的 day-scoped pipeline 里，Step 2.3 应该被重新定义为：

给定同一天的两条 retrieval lane 输出，按 query 对齐后做 RRF 融合，得到统一的 query 级候选结果和统一的带 tag 论文池。

它不应该再负责：

1. 猜测路径
2. 解析多套不同格式的输入 JSON
3. 重新定义 query 结构或论文结构

换句话说，Step 2.3 的职责应当非常单纯：

1. 对齐两边 query
2. 融合两边 rank list
3. 合并两边论文池
4. 输出 fused retrieval 结果

## Step 2.3 蓝图

Step 2.3 的接口应当围绕三个概念组织：

1. `RunContext`：运行环境
2. `BM25StepOutput` / `EmbeddingStepOutput`：两条 retrieval lane 的输入
3. `RRFStepOutput`：RRF 融合产物

### 共享 `RetrievalQuery`

Step 2.3 不重新定义 query 类型，而是直接复用 Step 2.1 / 2.2 共享的 query 类型。

```python
@dataclass(slots=True)
class RetrievalQuery:
    type: str
    tag: str
    paper_tag: str
    query_text: str
    logic_cn: str = ""
```

### 共享 `TaggedPaperRecord`

Step 2.3 也复用 Step 2.1 / 2.2 共享的论文视图。

```python
@dataclass(slots=True)
class TaggedPaperRecord:
    id: str
    source: str
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    primary_category: str | None = None
    categories: list[str] = field(default_factory=list)
    published: str | None = None
    link: str | None = None
    updated_at: str | None = None
    version: str | None = None
    tags: list[str] = field(default_factory=list)
```

### 共享 `QueryResult`

Step 2.3 的输入也是两边的 `QueryResult`，输出仍然是同样形状的 `QueryResult`，只是其中的 `score` / `rank` 已经变成了 RRF 融合后的结果。

```python
@dataclass(slots=True)
class QueryResult:
    query: RetrievalQuery
    sim_scores: dict[str, dict[str, float | int]] = field(default_factory=dict)
```

### `QueryAlignmentKey`

为了稳定对齐 BM25 和 embedding 的同一条 query，建议把对齐键显式定义出来。

```python
@dataclass(frozen=True, slots=True)
class QueryAlignmentKey:
    type: str
    paper_tag: str
    query_text: str
```

- `type`：区分 `keyword` / `intent_query`。
- `paper_tag`：确保同一主题下不同 query 不会误合并。
- `query_text`：确保同 tag 的多条 query 不会互相覆盖。

这和旧实现的 `make_query_key()` 是同一个思路，只是把它提升成明确的领域对象。

### `RRFStepInput`

```python
@dataclass(slots=True)
class RRFStepInput:
    run_date: date
    bm25_output: BM25StepOutput
    embedding_output: EmbeddingStepOutput
    top_k: int
    rrf_k: int = 60
    output_path_override: Path | None = None
```

- `run_date`：本次融合对应的自然日。
- `bm25_output`：BM25 lane 的结构化输出。
- `embedding_output`：embedding lane 的结构化输出。
- `top_k`：每条融合后 query 最终保留多少个候选论文。
- `rrf_k`：RRF 的常数项，用来控制 rank 衰减。
- `output_path_override`：可选输出路径覆盖，主要给测试或特殊运行使用。

### `RRFStats`

```python
@dataclass(slots=True)
class RRFStats:
    bm25_queries: int = 0
    embedding_queries: int = 0
    fused_queries: int = 0
    fused_papers: int = 0
```

- `bm25_queries`：BM25 输入 query 数量。
- `embedding_queries`：embedding 输入 query 数量。
- `fused_queries`：成功融合后的 query 数量。
- `fused_papers`：融合后带 tag 论文池大小。

### `RRFArtifacts`

```python
@dataclass(slots=True)
class RRFArtifacts:
    output_path: Path | None = None
```

- `output_path`：融合结果文件的实际落盘位置。

### `RRFStepOutput`

```python
@dataclass(slots=True)
class RRFStepOutput:
    run_date: date | None = None
    tagged_papers: list[TaggedPaperRecord] = field(default_factory=list)
    query_results: list[QueryResult] = field(default_factory=list)
    artifacts: RRFArtifacts = field(default_factory=RRFArtifacts)
    stats: RRFStats = field(default_factory=RRFStats)
    warnings: list[str] = field(default_factory=list)
```

- `run_date`：本次结果对应的自然日。
- `tagged_papers`：融合后带 tag 的论文列表。
- `query_results`：按 query 融合后的 RRF 结果。
- `artifacts`：文件产物信息。
- `stats`：统计信息。
- `warnings`：非致命问题列表。

### 统一入口

```python
def run_rrf_step(context: RunContext, step_input: RRFStepInput) -> RRFStepOutput:
    ...
```

## RRF 到底在做什么

Step 2.3 融合的不是 BM25 原始分数和 embedding 原始分数，而是两边的排名。

它的核心思想是：

1. 同一条 query 在 BM25 下有一个 rank list
2. 同一条 query 在 embedding 下也有一个 rank list
3. 对同一篇论文，把两边的 rank 贡献相加：

```text
score += 1 / (rrf_k + rank)
```

这意味着：

1. 两边都排得靠前的论文，会拿到更高融合分
2. 只在单边出现的论文，也可以保留，但另一边贡献为 0
3. 未出现在某一侧 top k 中，效果上等价于该侧“没有证据”，因此会受到隐式缺席惩罚

这里必须保持旧算法语义：

1. 不补全未上榜论文的缺失 rank
2. 不把另一侧缺席补成 `top_k + 1`
3. 只对实际进入 rank list 的结果做 RRF 加和

## 应该怎么改

新的 Step 2.3 应该明确收敛成下面这条链路：

1. 接收同一天的 `BM25StepOutput` 和 `EmbeddingStepOutput`
2. 用 `QueryAlignmentKey` 对齐两边 query
3. 对每条 query 提取 BM25 和 embedding 的 rank list
4. 按 RRF 规则融合成新的 `QueryResult`
5. 合并两边的 `TaggedPaperRecord`，tags 取并集
6. 把结果写到 `archive/YYYYMMDD/filtered/`

## 建议的文件输入输出

在 day-scoped pipeline 里，Step 2.3 的默认文件边界应该是：

输入：

- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.bm25.json`
- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.embedding.json`

输出：

- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.json`

也就是说，Step 2.3 也必须严格绑定到单日目录，不能跨天混用。

## 重构重点

这一步最关键的不是修改 RRF 算法，而是把结构说清楚：

1. query 对齐规则必须显式化
2. query 级输入输出结构必须对象化
3. 论文池合并规则必须显式化
4. 维持旧算法语义，不引入新的融合策略

如果 Step 2.1 和 Step 2.2 已经共享了 `RetrievalQuery`、`TaggedPaperRecord`、`QueryResult`，那么 Step 2.3 就会非常简单：它只需要对齐、融合、合并，不需要再做数据结构翻译。
