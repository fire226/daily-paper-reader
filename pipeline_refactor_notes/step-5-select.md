# Step 5 Select

原文件：`src/5.select_papers.py`

## 现在是怎么做的

这一步根据 LLM 评分，把论文分配到"精读区"（deep_dive）和"速览区"（quick_skim）两个推荐列表。

### 输入

- Step 4 的 LLM 输出
- `config.yaml` 中的 `arxiv_paper_setting.mode`
- tag 数量统计

### 输出

- `archive/<token>/recommend/arxiv_papers_<token>.<mode>.json`

### 四种 mode

| mode | deep_dive | quick_skim | 策略 |
|---|---|---|---|
| standard | 5 + tag_count 篇 | 10 + tag_count 篇 | uniform |
| extend | 10 + tag_count 篇 | 15 + tag_count 篇 | uniform |
| spark | 5 + tag_count 篇 | 10 + tag_count 篇 | low_bias（偏爱低分层） |
| skims | 0 | 所有 ≥8 分的论文 | 无上限 |

### 三种分配策略

- **round_robin**（精读区）：按 tag 轮流选，保证每个 tag 都有代表性
- **uniform**（速览区，standard/extend）：名额均匀分配给三个分数层（≥8、7、6）
- **low_bias**（速览区，spark）：70% 名额给最低分层（6 分），剩余给高分层

### 分数层

- ≥8 分：可进入精读区
- 7-8 分：速览区
- 6-7 分：速览区
- <6 分：丢弃

## 代码问题

### 1. 输入读的是旧格式

原版读 `data.get("llm_ranked")`，这是旧 Step 4 在 Step 3 JSON 上追加的字段。v2 的 Step 4 产出独立 `.llm.json`，字段名是 `scored_items`。需要适配。

### 2. 评分 merge 和推荐策略混在一起

`build_scored_papers`（合并评分和论文元信息）和 `process_mode`（按 mode 分配论文）在同一个文件里，没有清晰分层。

### 3. 字段命名不一致

`deep_divecandidates` 少了一个下划线，应为 `deep_dive_candidates`。

### 4. 硬编码读 config

`load_arxiv_paper_setting` 和 `load_config_tag_count` 直接读 `CONFIG_FILE` 路径，没有通过 `RunContext` 传入，和其他步骤风格不一致。

### 5. `build_candidates` 函数几乎没有逻辑

只是给每篇论文加了 `_source` 和 `selection_source` 两个字段，没有筛选或去重。可以合并到 `build_scored_papers` 里。

### 6. substep 日志过于冗余

每个小步骤都有 `log_substep` + `group_start/group_end`，日志输出很碎。v2 只保留关键 `log` 输出。

## 业务问题

### 1. 四种 mode 硬编码

mode 的配额和策略全部硬编码在 `MODES` 字典里。v2 保持硬编码，不从 config 读取。

### 2. deep_dive 阈值硬编码

只有 ≥8 分的论文才能进入精读区。v2 保持硬编码。

### 3. quick_skim 分层硬编码

三个分数层（≥8、7、6）的边界硬编码，<6 分的论文直接丢弃。v2 保持硬编码。

### 4. tag_count 来源

从 config 的 `intent_profiles` 数量计算 tag_count，加到配额上。v2 保持原逻辑，通过 `context.config` 传入。

### 5. `selection_source`

v2 不引入 carryover 机制（carryover 是旧 pipeline 的跨日保留机制），`selection_source` 保持 `fresh_fetch`。去掉无意义的 `_source` 字段。

### 6. `force_all_into_quick`

保留 `--all-quick` 功能，但简化实现，不单独封装函数。

## v2 解决方案

### 函数设计

```
load_mode_config(config) -> (mode, tag_count)
build_scored_papers(scored_items, paper_map) -> list[dict]
split_score_layers(candidates) -> list[tuple[str, list[dict]]]
select_deep_dive(candidates, cap) -> list[dict]
select_quick_skim(candidates, target, strategy) -> list[dict]
run_select_step(context, step_input) -> SelectStepOutput
```

### 数据结构

```python
@dataclass(slots=True)
class SelectStepInput:
    run_date: date
    llm_output: LLMRefineStepOutput
    mode: str = "standard"
    output_path_override: Path | None = None

@dataclass(slots=True)
class SelectStepOutput:
    run_date: date | None = None
    deep_dive: list[dict[str, Any]] = field(default_factory=list)
    quick_skim: list[dict[str, Any]] = field(default_factory=list)
    artifacts: SelectArtifacts = field(default_factory=SelectArtifacts)
    stats: SelectStats = field(default_factory=SelectStats)
    warnings: list[str] = field(default_factory=list)
```

### 文件边界

- 输入：`archive/YYYYMMDD/rank/arxiv_papers_YYYYMMDD.llm.json` + config
- 输出：`archive/YYYYMMDD/recommend/arxiv_papers_YYYYMMDD.<mode>.json`

一次调用只处理一个 mode。如果需要多个 mode，CLI 层面循环调用。
