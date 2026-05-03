# Step 2.2 Embedding

原文件：`src/2.2.retrieval_papers_embedding.py`

## 现在是怎么做的

这一步负责根据订阅查询，对 Step 1 产出的单日论文池做语义向量召回，并把命中结果写成带 query 信息和 tag 信息的中间文件。

旧实现的核心动作是：

1. 读取 Step 1 产出的 raw 论文 JSON
2. 从 `config.yaml` 的 `intent_profiles` 构造 embedding 查询列表
3. 用 `title + abstract` 作为论文侧语义文本
4. 对每个 query 计算向量相似度并保留 top k 候选
5. 为命中论文打上 `tags`
6. 把带 tag 的论文和每个 query 的命中结果写回 `filtered` 文件

旧实现里，Step 2.2 实际还混入了不少额外职责：

1. 支持本地 embedding 检索和 Supabase 向量 RPC 两条召回路径
2. 维护 embedding cache
3. 在本地 raw 文件缺失时触发 fallback fetch
4. 做多 source 路由和结果合并
5. 混合模型加载、设备选择、cache 状态回写

## 当前输入输出

旧实现的输入：

- `archive/<token>/raw/arxiv_papers_<token>.json`
- `config.yaml`
- `subscription_plan.build_pipeline_inputs()` 生成的 `embedding_queries`
- embedding 模型、设备、cache 相关参数
- 可选的 Supabase 向量后端配置

旧实现的输出：

- `archive/<token>/filtered/arxiv_papers_<token>.embedding.json`

旧输出文件里主要包含：

1. `papers`
2. `queries`
3. 每个 query 的 `sim_scores`
4. 论文上的 `tags`

## 当前问题

1. 与 Step 2.1 存在大量重复结构，但没有统一抽象
2. retrieval、模型加载、cache、数据源路由混在一起
3. embedding cache 带状态回写，配置和运行状态边界不干净
4. 本地向量检索和 RPC 检索是两套实现，但对外没有清楚边界
5. 输出虽然有 `queries` 和 `sim_scores`，但没有和 BM25 明确共享同一套数据结构
6. 旧实现仍然带着 fallback fetch、多 source、多天窗口等复杂性，不符合现在已经确定的“单天 pipeline”原则

## 这一步原本到底是做什么的

在新的 day-scoped pipeline 里，Step 2.2 应该被重新定义为：

给定某一天的论文池，和给定的一组语义查询，返回 query 级的语义候选论文结果，并把命中论文打上对应 tag。

它不应该再负责：

1. 抓论文
2. 补抓论文
3. 推导日期区间
4. 从环境变量猜输入路径
5. 在主路径中兼容多种完全不同的数据源路径

换句话说，Step 2.2 的职责应当非常单纯：

1. 接收论文池
2. 接收查询计划
3. 跑语义检索
4. 输出 query 级召回结果

## Step 2.2 蓝图

Step 2.2 的接口应当围绕三个概念组织：

1. `RunContext`：运行环境
2. `EmbeddingStepInput`：这次语义检索任务
3. `EmbeddingStepOutput`：语义检索产物

其中，影响检索结果的参数应当显式放在 `EmbeddingStepInput` 里；路径和默认配置则放在 `RunContext` 里。

### `RunContext`

Step 2.2 继续复用流水线统一的 `RunContext`，至少应包含：

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

Step 2.2 应当与 Step 2.1 共享同一种 query 类型。前端虽然区分 `keywords` 和 `intent_queries`，但进入 retrieval lane 之后，它们都已经是同一个 query list，只保留 `type` 作为来源信息。

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
- `tag`：订阅主题标签。
- `paper_tag`：写回论文的标签名，例如 `keyword:xxx` 或 `query:xxx`。
- `query_text`：实际用于 embedding 的查询文本。
- `logic_cn`：给人看的中文逻辑说明。

这里保留 `type` 的目的，是为了让后续步骤知道 query 的来源；但 Step 2.2 本身不应因为 `type=keyword` 或 `type=intent_query` 而切换成两套不同语义检索算法。

### 共享 `TaggedPaperRecord`

Step 2.2 应当与 Step 2.1、Step 2.3 共用同一种论文视图。

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
- `title` / `abstract`：embedding 主要使用的文本字段。
- 其余字段：延续 Step 1 的标准元数据。
- `tags`：这篇论文被哪些 query lane 命中过的标签集合。

### `EmbeddingStepInput`

```python
@dataclass(slots=True)
class EmbeddingStepInput:
    run_date: date
    papers: list[PaperRecord]
    queries: list[RetrievalQuery]
    top_k: int
    output_path_override: Path | None = None
    model_name: str | None = None
    device: str | None = None
```

- `run_date`：本次检索对应的自然日，和 Step 1 输出保持同一天。
- `papers`：来自 Step 1 的标准化论文池。
- `queries`：已经构造好的 embedding 查询计划。
- `top_k`：每个 query 保留多少个候选论文。
- `output_path_override`：可选输出路径覆盖，主要给测试或特殊运行使用。
- `model_name`：可选模型覆盖，主要给实验和测试使用。
- `device`：可选设备覆盖，例如 `cpu` / `cuda`。

和 Step 2.1 一样，Step 2.2 的结果也不应该先把所有 query 对同一篇论文的得分聚合成一个单独总分。它输出的仍应当是 query 粒度的“稀疏 query-paper 命中矩阵”。

### 共享 `QueryResult`

Step 2.2 也应当与 Step 2.1 共享同一种 query 结果结构。

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

### `EmbeddingStats`

```python
@dataclass(slots=True)
class EmbeddingStats:
    papers_total: int = 0
    queries_total: int = 0
    tagged_papers: int = 0
    total_hits: int = 0
    embedding_backend: str = "local"
```

- `papers_total`：输入论文池大小。
- `queries_total`：本次执行的 query 数量。
- `tagged_papers`：最终至少命中一个 query 的论文数。
- `total_hits`：所有 query 命中数总和。
- `embedding_backend`：本次实际使用的检索后端，例如 `local`。

### `EmbeddingArtifacts`

```python
@dataclass(slots=True)
class EmbeddingArtifacts:
    output_path: Path | None = None
```

- `output_path`：本次 embedding 结果文件的实际落盘位置。

### `EmbeddingStepOutput`

```python
@dataclass(slots=True)
class EmbeddingStepOutput:
    run_date: date | None = None
    tagged_papers: list[TaggedPaperRecord] = field(default_factory=list)
    query_results: list[QueryResult] = field(default_factory=list)
    artifacts: EmbeddingArtifacts = field(default_factory=EmbeddingArtifacts)
    stats: EmbeddingStats = field(default_factory=EmbeddingStats)
    warnings: list[str] = field(default_factory=list)
```

- `run_date`：本次结果对应的自然日。
- `tagged_papers`：带 tag 的论文列表，表示“至少被一条 query 命中过的论文集合”。
- `query_results`：query 级语义召回结果。
- `artifacts`：文件产物信息。
- `stats`：统计信息。
- `warnings`：非致命问题列表。

### 统一入口

```python
def run_embedding_step(context: RunContext, step_input: EmbeddingStepInput) -> EmbeddingStepOutput:
    ...
```

## 和 Step 2.1 / Step 2.3 的对接

Step 2.2 不应该被设计成一个孤立的 embedding 脚本，而应该作为 Step 2 retrieval lane 的另一条通道。

为了让 Step 2.3 简单，Step 2.2 应该满足下面两个约束：

1. 和 Step 2.1 共享 `RetrievalQuery`
2. 和 Step 2.1 / Step 2.3 共享 `TaggedPaperRecord` 与 `QueryResult`

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

## 应该怎么改

新的 Step 2.2 应该明确收敛成下面这条链路：

1. 从 Step 1 接收单日论文池
2. 从 `subscription_plan.build_pipeline_inputs(config)` 接收已经构造好的 `embedding_queries`
3. 在本地对 `title + abstract` 编码为向量
4. 对每个 query 计算 top k 候选
5. 把命中结果写成统一结构，并为论文打 tag
6. 把结果写到 `archive/YYYYMMDD/filtered/`

默认情况下，v2 的 Step 2.2 不应该继续保留：

1. Supabase 向量路径
2. fallback fetch
3. 从环境变量反推输入输出路径
4. 多 source 结果合并
5. 复杂 cache 状态回写

这些东西如果未来仍有价值，也应该在新的设计里作为可选后端或外层 orchestration，而不是继续和本地 embedding 主路径缠在一起。

## 建议的文件输入输出

在 day-scoped pipeline 里，Step 2.2 的默认文件边界应该是：

输入：

- `archive/YYYYMMDD/raw/arxiv_papers_YYYYMMDD.json`
- `config.yaml`

输出：

- `archive/YYYYMMDD/filtered/arxiv_papers_YYYYMMDD.embedding.json`

也就是说，Step 2.2 和 Step 2.1 一样，必须严格绑定到单日目录，不能跨天混用。

## 重构重点

这一步最关键的不是“把 embedding 检索写得多复杂”，而是把边界收干净：

1. Query plan 构造和 embedding 执行必须拆开
2. Step 2.2 只处理单日论文池，不负责抓取
3. Step 2.2 先定义本地 embedding 主路径，不先引入 Supabase 复杂度
4. 输出结构要和 Step 2.1 / Step 2.3 对齐，方便后续融合

如果 Step 2.2 按这个方向收敛，Step 2.3 的 RRF 融合就会简单很多。
