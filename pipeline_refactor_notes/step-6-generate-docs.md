# Step 6 Generate Docs

原文件：`src/6.generate_docs.py`（2675 行）

## 现在是怎么做的

把 Step 5 的推荐结果转换成 Docsify Markdown 网页，同时调 LLM 做翻译和总结。

## 拆分方案

原 Step 6 拆成两步：

### Step 6：LLM Enrichment

职责：对推荐论文调 LLM 生成增强内容。

输入：
- Step 5 推荐结果（`SelectStepOutput`）
- Step 3 论文元信息（`RerankStepOutput.papers`）

输出：
- `archive/YYYYMMDD/enriched/arxiv_papers_YYYYMMDD.enriched.json`

包含每篇论文的：
- 中文标题和摘要（翻译）
- 速览摘要（Motivation / Method / Result / Conclusion）
- 详细总结（基于 PDF 全文，可选）

### Step 7：Docsify Generate

职责：把推荐结果 + 增强内容生成 Docsify Markdown 页面。

输入：
- Step 5 推荐结果
- Step 6 增强内容（可选，没有则跳过增强部分）

输出：
- `docs/YYYY/MM/DD/<slug>.md` — 每篇论文页面
- `docs/YYYY/MM/DD/README.md` — 日报汇总页
- `docs/_sidebar.md` — 侧边栏
- `docs/README.md` — 首页

---

## LLM 调用优化

原版每篇论文调 3 次 LLM（翻译、速览、详细总结），20 篇论文就是 60 次。

优化方案：

### 翻译：批量调用

把多篇论文的标题和摘要打包成一个 JSON 数组，一次 LLM 调用完成所有翻译。

```
输入: [{"id": "paper-1", "title": "...", "abstract": "..."}, ...]
输出: [{"id": "paper-1", "title_zh": "...", "abstract_zh": "..."}, ...]
```

### 速览摘要：批量调用

同样打包成数组，一次调用完成所有速览。

```
输入: [{"id": "paper-1", "title": "...", "abstract": "..."}, ...]
输出: [{"id": "paper-1", "motivation": "...", "method": "...", "result": "...", "conclusion": "..."}, ...]
```

### 详细总结：保持单篇调用

详细总结需要 PDF 全文，token 量大，无法批量。保持每篇单独调用，但只对 deep_dive 论文执行（quick_skim 不需要详细总结）。

### 调用次数对比

| | 原版（20 篇论文） | v2 |
|---|---|---|
| 翻译 | 20 次 | 1 次 |
| 速览 | 20 次 | 1 次 |
| 详细总结 | 20 次 | 5 次（仅 deep_dive） |
| 总计 | 60 次 | 7 次 |

---

## 侧边栏策略

原版每次重写整个 `docs/_sidebar.md`。v2 改为按日期段替换：

1. 读取现有侧边栏
2. 找到当天日期的段落（`* YYYY-MM-DD <!--dpr-date:YYYYMMDD-->`）
3. 替换该段落内容（如果日期段不存在则插入）
4. 其他日期段落保持不变

这样：
- 当天的论文更新后，侧边栏跟着更新
- 如果手动删除了某天的论文目录，下次跑其他天时不会误恢复（因为只替换当天段落）
- 不会丢失手动对其他日期段落的修改

---

## 删除的功能

- 历史日报补生成（`backfill_history_day_reports`）
- 单篇补生成模式（`--paper-id`）
- 历史格式兼容代码（`normalize_meta_tldr_line`、`normalize_glance_block_format`、`strip_auto_sections` 等）
- sidebar-only 模式

---

## 代码风格

与其他 Step 保持一致：
- 通过 `RunContext` 传入 config
- 通过 `context.config` 访问配置
- LLM 客户端通过函数参数传入，不全局初始化
- 路径通过 `resolve_output_path` 解析
- dataclass 定义输入输出
- `run_*_step()` + `build_parser()` + `main()`

---

## Step 6 数据结构

```python
@dataclass(slots=True)
class EnrichedPaper:
    paper_id: str
    title_zh: str = ""
    abstract_zh: str = ""
    glance_motivation: str = ""
    glance_method: str = ""
    glance_result: str = ""
    glance_conclusion: str = ""
    deep_summary: str = ""

@dataclass(slots=True)
class EnrichmentStats:
    papers_total: int = 0
    translated: int = 0
    glanced: int = 0
    deep_summarized: int = 0

@dataclass(slots=True)
class EnrichmentArtifacts:
    output_path: Path | None = None

@dataclass(slots=True)
class EnrichmentStepInput:
    run_date: date
    select_output: SelectStepOutput
    rerank_output: RerankStepOutput
    output_path_override: Path | None = None

@dataclass(slots=True)
class EnrichmentStepOutput:
    run_date: date | None = None
    enriched_papers: list[EnrichedPaper] = field(default_factory=list)
    artifacts: EnrichmentArtifacts = field(default_factory=EnrichmentArtifacts)
    stats: EnrichmentStats = field(default_factory=EnrichmentStats)
    warnings: list[str] = field(default_factory=list)
```

## Step 7 数据结构

```python
@dataclass(slots=True)
class GenerateDocsStats:
    papers_generated: int = 0
    daily_report_generated: bool = False
    sidebar_updated: bool = False
    home_updated: bool = False

@dataclass(slots=True)
class GenerateDocsArtifacts:
    docs_dir: Path | None = None
    paper_paths: list[Path] = field(default_factory=list)
    sidebar_path: Path | None = None

@dataclass(slots=True)
class GenerateDocsStepInput:
    run_date: date
    select_output: SelectStepOutput
    enrichment_output: EnrichmentStepOutput | None = None
    output_dir_override: Path | None = None

@dataclass(slots=True)
class GenerateDocsStepOutput:
    run_date: date | None = None
    artifacts: GenerateDocsArtifacts = field(default_factory=GenerateDocsArtifacts)
    stats: GenerateDocsStats = field(default_factory=GenerateDocsStats)
    warnings: list[str] = field(default_factory=list)
```

## 文件边界

Step 6 输入：`archive/YYYYMMDD/recommend/arxiv_papers_YYYYMMDD.<mode>.json` + config
Step 6 输出：`archive/YYYYMMDD/enriched/arxiv_papers_YYYYMMDD.enriched.json`

Step 7 输入：Step 5 推荐结果 + Step 6 增强内容（可选）
Step 7 输出：`docs/YYYY/MM/DD/*.md` + `docs/_sidebar.md` + `docs/README.md`
