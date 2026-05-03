# Step 1 Fetch

原文件：`src/maintain/fetchers/fetch_arxiv.py`

## 现在是怎么做的

这一步负责把某一个自然日发表的论文元数据抓下来，写入 `archive/<run_date>/raw/`。

当前实现同时支持两条路径：

1. 从 Supabase 读取最近论文或日期区间论文
2. 从 arXiv API 拉取当天论文

它还同时承担了这些职责：

1. 读取 `config.yaml`
2. 解析 `DPR_RUN_DATE` 和 `DPR_SINGLE_DAY`
3. 推导抓取窗口
4. 管理 `crawl_state.json`
5. 管理 `arxiv_seen.json`
6. 控制分片时间窗口
7. 落盘原始结果

## 当前输入输出

旧实现的输入：

- 环境变量：`DPR_RUN_DATE`、`DPR_SINGLE_DAY`
- 配置：`config.yaml` 中的抓取设置和 Supabase 相关设置
- 持久状态：`archive/crawl_state.json`、`archive/arxiv_seen.json`

旧实现的输出：

- 原始论文 JSON：`archive/<token>/raw/arxiv_papers_<token>.json`
- 抓取状态文件更新

## 当前问题

1. 入口逻辑和业务逻辑混在一起
2. “抓取日期”与“抓取窗口”规则很隐式，新人很难推断真实行为
3. Supabase 抓取和 arXiv API 抓取耦在一个大脚本里
4. seen state / crawl state 属于运行状态，不该和抓取主逻辑缠在一起
5. 文件命名、日期 token、窗口推导都藏在脚本内部

## 应该怎么改

新版本建议先明确放弃 Supabase 依赖，只保留 arXiv API 作为唯一来源。

同时，Step 1 应该严格服从总设计中的“单天处理”原则：

1. Step 1 的最小处理单位是一个自然日
2. Step 1 的输出目录必须唯一对应这一天
3. 不应把多天论文先混在一个 raw 文件里，再交给后续步骤拆分
4. 如果用户要处理一个日期区间，应由外层调度器逐天调用 Step 1

建议拆成 3 层：

1. `resolve_run_date(...) -> date`
2. `load_fetch_state(...)`、`fetch_from_arxiv(...)`、`normalize_papers(...)` 负责读取状态、抓取数据和标准化论文对象
3. `resolve_output_path(...)`、`write_raw_output(raw_output_path, ...)`、`build_state_updates(...)`、`write_state_updates(...)` 负责路径解析、落盘和状态更新

再把状态管理单独拆出去：

1. `load_crawl_state()`
2. `save_crawl_state()`
3. `load_seen_state()`
4. `save_seen_state()`

## Step 1 蓝图

Step 1 的接口应当围绕两个概念组织：

1. `RunContext`：程序运行环境
2. `FetchStepInput`：这次抓取任务本身

其中，影响业务结果的参数应当显式放在 `FetchStepInput` 里；运行环境默认值则放在 `RunContext` 里。

### `RunContext`

```python
@dataclass(slots=True)
class RunContext:
    root_dir: Path
    archive_root: Path
    config_path: Path
    crawl_state_path: Path
    seen_state_path: Path
    config: dict[str, Any]
```

- `root_dir`：项目根目录，用来定位整个工作区。
- `archive_root`：`archive/` 根目录，用来推导当天产物路径。
- `config_path`：`config.yaml` 的路径，用来读取默认配置。
- `crawl_state_path`：抓取状态文件路径，用来记录最近一次抓取时间。
- `seen_state_path`：seen 状态文件路径，用来记录已经见过的论文 id。
- `config`：已加载的配置内容，用来提供默认值但不直接当作业务输入。

### `FetchStepInput`

```python
@dataclass(slots=True)
class FetchStepInput:
    run_date: date
    ignore_seen: bool = False
    output_path_override: Path | None = None
    categories_override: list[str] | None = None
```

- `run_date`：本次要抓取的自然日，是 Step 1 最核心的业务输入。
- `ignore_seen`：是否忽略历史 seen 状态，决定本次是否跳过已见论文。
- `output_path_override`：可选输出路径覆盖，主要给测试或特殊运行使用。
- `categories_override`：可选分类覆盖，允许本次只抓指定 arXiv 分类而不是默认全集。

### `FetchStats`

```python
@dataclass(slots=True)
class FetchStats:
    total_papers: int = 0
    deduplicated_papers: int = 0
    categories_used: list[str] = field(default_factory=list)
    queries_attempted: int = 0
    query_failures: int = 0
```

- `total_papers`：本次最终输出了多少篇论文。
- `deduplicated_papers`：去重后的论文数，用来反映最终有效结果规模。
- `categories_used`：本次实际使用了哪些 arXiv 分类，便于观察真实抓取范围。
- `queries_attempted`：实际发起了多少个 arXiv 查询请求。
- `query_failures`：有多少个查询请求失败了，用来衡量抓取稳定性。

### `FetchArtifacts`

```python
@dataclass(slots=True)
class FetchArtifacts:
    raw_output_path: Path | None = None
```

- `raw_output_path`：本次 raw JSON 文件的实际落盘位置。

### `PaperRecord`

```python
@dataclass(slots=True)
class PaperRecord:
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
```

- `id`：论文唯一标识，通常是 arXiv id。
- `source`：数据来源标识，当前主要是 `arxiv`。
- `title`：论文标题，供后续检索和筛选使用。
- `abstract`：论文摘要，供后续检索和筛选使用。
- `authors`：作者列表，供后续展示和辅助筛选使用。
- `primary_category`：arXiv 主分类，表示论文最主要的学科归属。
- `categories`：arXiv 分类列表，表示论文涉及的所有学科标签。
- `published`：论文发表时间，用来严格归属到某一天。
- `link`：论文链接，通常是 PDF 或 arXiv 页面链接。
- `updated_at`：arXiv 元数据更新时间，属于扩展字段。
- `version`：arXiv 版本号，属于扩展字段。

### `FetchStepOutput`

```python
@dataclass(slots=True)
class FetchStepOutput:
    run_date: date | None = None
    papers: list[PaperRecord] = field(default_factory=list)
    backend: Literal["arxiv", "unknown"] = "unknown"
    artifacts: FetchArtifacts = field(default_factory=FetchArtifacts)
    stats: FetchStats = field(default_factory=FetchStats)
    warnings: list[str] = field(default_factory=list)
```

- `run_date`：本次输出对应的自然日，保证结果和日期强绑定。
- `papers`：Step 1 产出的标准化论文列表。
- `backend`：本次实际使用的数据源后端，当前应为 `arxiv`。
- `artifacts`：本次运行产生的文件产物信息。
- `stats`：本次运行的统计信息，用来观察抓取效果。
- `warnings`：本次运行的非致命问题列表，用来保留错误和异常信息。

### 统一入口

```python
def run_fetch_step(context: RunContext, step_input: FetchStepInput) -> FetchStepOutput:
    ...
```

设计原则：

1. `run_date` 必须显式输入，因为整条 pipeline 已经确定按天严格隔离。
2. 路径放在 `RunContext` 中，因为它们属于运行环境，不是业务任务本身。
3. `output_path_override` 保留，是为了测试和特殊运行时可以覆盖默认路径。
4. `categories_override` 保留，是因为它会影响抓取结果；如果允许覆盖，就应当显式。
5. 输出只描述“本次产出了什么”，不重复塞大量环境细节。

## 建议的数据字段分层

Step 1 的单篇论文对象建议只保留“不包含正文”的信息，并分成两组字段：

核心字段：后续 Step 2 到 Step 5 默认可依赖。

- `id`
- `source`
- `title`
- `abstract`
- `authors`
- `primary_category`
- `categories`
- `published`
- `link`

扩展字段：arXiv 可能提供；下游默认不强依赖。

- `updated_at`
- `version`

这里故意不包含 PDF 正文、图表、全文抽取文本等内容。这些内容应留在后续“包含正文”的阶段处理，而不是进入 Step 1 的论文池对象。

同样，这个 v2 版本也不再为了兼容 Supabase 预留额外数据库字段。Step 1 只围绕 arXiv 原始元数据建模。

## 重构目标

重构后，这一步应该回答清楚三件事：

1. 抓的是哪一天
2. 数据到底来自 arXiv API
3. 最终产物写到了哪里

对新的 v2 实现，可以直接收敛成更简单的目标：

1. 输入是显式单日
2. 数据来源固定为 arXiv API
3. 输出是一批不包含正文的标准化论文对象
4. 状态只作为 Step 1 自己的内部机制，不向下游泄漏
