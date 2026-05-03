# Step 2.1 BM25

原文件：`src/2.1.retrieval_papers_bm25.py`

## 现在是怎么做的

这一步负责根据订阅查询，对 Step 1 产出的单日论文池做 BM25 检索，并把命中结果写成带 query 信息和 tag 信息的中间文件。

旧实现的核心动作是：

1. 读取 Step 1 产出的 raw 论文 JSON
2. 从 `config.yaml` 的 `intent_profiles` 构造 BM25 查询列表
3. 用 `title + abstract` 作为 BM25 文本
4. 对每个 query 计算 BM25 分数并保留 top k 候选
5. 为命中论文打上 `tags`
6. 把带 tag 的论文和每个 query 的命中结果写回 `filtered` 文件

旧实现里，Step 2.1 实际还混入了不少额外职责：

1. 支持本地 BM25 和 Supabase RPC 两条召回路径
2. 在本地 raw 文件缺失时触发 fallback fetch
3. 解析布尔表达式和 term 规则
4. 根据日期窗口推导 Supabase 召回范围
5. 做多 source 分组与结果合并

## 当前输入输出

旧实现的输入：

- `archive/<token>/raw/arxiv_papers_<token>.json`
- `config.yaml`
- `subscription_plan.build_pipeline_inputs()` 生成的 `bm25_queries`
- 可选的 Supabase BM25 后端配置

旧实现的输出：

- `archive/<token>/filtered/arxiv_papers_<token>.bm25.json`

旧输出文件里主要包含：

1. `papers`
2. `queries`
3. 每个 query 的 `sim_scores`
4. 论文上的 `tags`

## 当前问题

1. 文件过大，查询构造、数据加载、检索执行、输出序列化都混在一起
2. Step 2.1 明明应该是 retrieval step，却承担了 fallback fetch 逻辑
3. 本地 BM25 和 Supabase RPC 是两套实现，但对外没有清楚边界
4. 日期范围、目录路径、运行模式严重依赖环境变量，接口不显式
5. 输出虽然有 `queries` 和 `sim_scores`，但内部数据结构不够清晰，不利于 Step 2.2 / Step 2.3 统一
6. 旧实现仍然带着多天窗口和多 source 的复杂性，不符合现在已经确定的“单天 pipeline”原则

## 这一步原本到底是做什么的

在新的 day-scoped pipeline 里，Step 2.1 应该被重新定义为：

给定某一天的论文池，和给定的一组 BM25 查询，返回 query 级的候选论文结果，并把命中论文打上对应 tag。

它不应该再负责：

1. 抓论文
2. 补抓论文
3. 推导日期区间
4. 从环境变量猜输入路径
5. 兼容多种完全不同的数据源路径

换句话说，Step 2.1 的职责应当非常单纯：

1. 接收论文池
2. 接收查询计划
3. 跑 BM25
4. 输出 query 级召回结果

## Step 2.1 蓝图

Step 2.1 的接口应当围绕三个概念组织：

1. `RunContext`：运行环境
2. `BM25StepInput`：这次 BM25 检索任务
3. `BM25StepOutput`：BM25 检索产物

其中，影响检索结果的参数应当显式放在 `BM25StepInput` 里；路径和默认配置则放在 `RunContext` 里。

### `RunContext`

Step 2.1 继续复用流水线统一的 `RunContext`，至少应包含：

```python
@dataclass(slots=True)
class RunContext:
    root_dir: Path
    archive_root: Path
    config_path: Path
    config: dict[str, Any]
```

- `root_dir`：项目根目录，用来定位整个工作区。
- `archive_root`：`archive/` 根目录，用来推导当天输入输出路径。
- `config_path`：`config.yaml` 路径，用来读取订阅配置。
- `config`：已加载配置内容，用来构造 query plan 和默认参数。

### 共享 `RetrievalQuery`

Step 2.1 和 Step 2.2 最好共享同一种 query 类型。前端虽然区分 `keywords` 和 `intent_queries`，但进入 retrieval lane 之后，它们都已经是同一个 query list，只保留 `type` 作为来源信息。

```python
@dataclass(slots=True)
class RetrievalQuery:
    type: str
    tag: str
    paper_tag: str
    query_text: str
    logic_cn: str = ""
```

- `type`：查询类型，例如 `keyword` 或 `intent_query`。
- `tag`：订阅主题标签，用来标记这条 query 属于哪个主题。
- `paper_tag`：写回论文的标签名，例如 `keyword:xxx` 或 `query:xxx`。
- `query_text`：实际用于 BM25 的查询文本。
- `logic_cn`：给人看的中文逻辑说明，主要用于调试和下游展示。

这里保留 `type` 的目的，是为了让后续步骤知道 query 的来源；但 Step 2.1 本身不应因为 `type=keyword` 或 `type=intent_query` 而切换成两套不同 BM25 算法。

### `TaggedPaperRecord`

从 Step 2 开始，论文对象已经不再只是 Step 1 的纯元数据对象，而是“元数据 + 命中 tag”的组合。为了让 Step 2.1、Step 2.2、Step 2.3 共用同一种论文视图，建议显式引入 `TaggedPaperRecord`。

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

- `id`：论文唯一标识，通常是 arXiv id。
- `source`：数据来源标识。
- `title` / `abstract`：BM25 和 embedding 都要用到的主要文本字段。
- `authors` / `primary_category` / `categories` / `published` / `link` / `updated_at` / `version`：延续 Step 1 的标准元数据。
- `tags`：这篇论文被哪些 query lane 命中过的标签集合。

这个对象表达的是 Step 2 的“论文视图”：

1. 这篇论文本身是什么
2. 它目前被哪些 retrieval query 命中过

### `BM25StepInput`

```python
@dataclass(slots=True)
class BM25StepInput:
    run_date: date
    papers: list[PaperRecord]
    queries: list[RetrievalQuery]
    top_k: int
    output_path_override: Path | None = None
```

- `run_date`：本次检索对应的自然日，和 Step 1 输出保持同一天。
- `papers`：来自 Step 1 的标准化论文池。
- `queries`：已经构造好的 BM25 查询计划。
- `top_k`：每个 query 保留多少个候选论文。
- `output_path_override`：可选输出路径覆盖，主要给测试或特殊运行使用。

这里有一个关键设计点：

Step 2.1 的结果不应该先把所有 query 对同一篇论文的得分聚合成一个单独总分。BM25 是按 query 分别召回的，因此输出应当保留 query 粒度。也就是说，Step 2.1 更像在生成一个“稀疏 query-paper 命中矩阵”，而不是一个“每篇论文唯一总分榜”。

### 共享 `QueryResult`

Step 2.1 和 Step 2.2 也最好共享同一种 query 结果结构。

```python
@dataclass(slots=True)
class QueryResult:
    query: RetrievalQuery
    sim_scores: dict[str, dict[str, float | int]] = field(default_factory=dict)
```

- `query`：对应的查询对象。
- `sim_scores`：命中论文的分数字典，key 是 `paper_id`，value 至少包含 `score` 和 `rank`。

它表达的是 Step 2 的“query 视图”：

1. 这一条 query 命中了哪些论文
2. 这些论文在这一条 query 下各自排第几
3. 这一条 query 的候选论文 top k 是什么

例如：

```python
QueryResult(
    query=RetrievalQuery(...),
    sim_scores={
        "2601.00123v1": {"score": 12.3, "rank": 1},
        "2601.00456v1": {"score": 11.8, "rank": 2},
    },
)
```

这不是一个完整的 `m * n` 分数矩阵，而是一个稀疏表示：

1. 有多少 query，就会有多少个 `QueryResult`
2. 每个 `QueryResult` 只保留本条 query 的 top k 命中
3. 如果一篇论文没有进入任何 query 的 top k，它通常就不会进入 Step 2 的后续候选池

### `BM25Stats`

```python
@dataclass(slots=True)
class BM25Stats:
    papers_total: int = 0
    queries_total: int = 0
    tagged_papers: int = 0
    total_hits: int = 0
```

- `papers_total`：输入论文池大小。
- `queries_total`：本次执行的 query 数量。
- `tagged_papers`：最终至少命中一个 query 的论文数。
- `total_hits`：所有 query 命中数总和。

### `BM25Artifacts`

```python
@dataclass(slots=True)
class BM25Artifacts:
    output_path: Path | None = None
```

- `output_path`：本次 BM25 结果文件的实际落盘位置。

### `BM25StepOutput`

```python
@dataclass(slots=True)
class BM25StepOutput:
    run_date: date | None = None
    tagged_papers: list[TaggedPaperRecord] = field(default_factory=list)
    query_results: list[QueryResult] = field(default_factory=list)
    artifacts: BM25Artifacts = field(default_factory=BM25Artifacts)
    stats: BM25Stats = field(default_factory=BM25Stats)
    warnings: list[str] = field(default_factory=list)
```

- `run_date`：本次结果对应的自然日。
- `tagged_papers`：带 tag 的论文列表，表示“至少被一条 query 命中过的论文集合”。
- `query_results`：query 级 BM25 召回结果。
- `artifacts`：文件产物信息。
- `stats`：统计信息。
- `warnings`：非致命问题列表。

这说明 Step 2.1 的输出天然有两层：

1. `tagged_papers`
2. `query_results`

前者是论文视图，后者是 query 视图。两者都要保留，不能只留其中一层。

### 统一入口

```python
def run_bm25_step(context: RunContext, step_input: BM25StepInput) -> BM25StepOutput:
    ...
```

## 和 Step 2.2 / Step 2.3 的对接

Step 2.1 不应该被设计成一个孤立的 BM25 脚本，而应该作为 Step 2 retrieval lane 的一部分。

为了让 Step 2.2 和 Step 2.3 简单，Step 2.1 应该满足下面两个约束：

1. 和 Step 2.2 共享 `RetrievalQuery`
2. 和 Step 2.2 / Step 2.3 共享 `TaggedPaperRecord` 与 `QueryResult`

也就是说，Step 2.1 和 Step 2.2 的区别应该只在“打分算法”上：

1. Step 2.1：BM25 分数
2. Step 2.2：embedding 相似度分数

除此以外：

1. 论文类型相同
2. query 类型相同
3. query 级结果结构相同

这样 Step 2.3 就只需要做一件事：

1. 用稳定的 query key 对齐两边的 `QueryResult`
2. 提取各自的 rank list
3. 按 RRF 规则融合

它就不需要再额外兼容两套不同的数据结构。

换句话说，Step 2.1 的一个重要任务不是“算出 BM25 分”，而是“给 Step 2.2 和 Step 2.3 提供一个结构稳定的 retrieval lane 输出”。

## 应该怎么改

新的 Step 2.1 应该明确收敛成下面这条链路：

1. 从 Step 1 接收单日论文池
2. 从 `subscription_plan.build_pipeline_inputs(config)` 接收已经构造好的 `bm25_queries`
3. 在本地对 `title + abstract` 建 BM25 索引
4. 对每个 query 计算 top k 候选
5. 把命中结果写成统一结构，并为论文打 tag
6. 把结果写到 `archive/YYYYMMDD/filtered/`

默认情况下，v2 的 Step 2.1 不应该继续保留：

1. Supabase BM25 路径
2. fallback fetch
3. 从环境变量反推输入输出路径
4. 多 source 结果合并

这些东西如果未来仍有价值，也应该在新的设计里作为可选后端或外层 orchestration，而不是继续和本地 BM25 主路径缠在一起。

## 建议的文件输入输出

在 day-scoped pipeline 里，Step 2.1 的默认文件边界应该是：

输入：

- `archive/YYYYMMDD/raw/arxiv_papers_YYYYMMDD.json`
- `config.yaml`

输出：

- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.bm25.json`

也就是说，Step 2.1 和 Step 1 一样，必须严格绑定到单日目录，不能跨天混用。

## 重构重点

这一步最关键的不是“把 BM25 算法写得多复杂”，而是把边界收干净：

1. Query plan 构造和 BM25 执行必须拆开
2. Step 2.1 只处理单日论文池，不负责抓取
3. Step 2.1 只定义本地 BM25 主路径，不先引入 Supabase 复杂度
4. 输出结构要和 Step 2.2 / Step 2.3 对齐，方便后续融合

如果 Step 2.1 按这个方向收敛，后面的 Step 2.2 embedding 和 Step 2.3 rrf 就会简单很多。
