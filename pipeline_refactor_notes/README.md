# Pipeline Refactor Notes

这组文档只关注核心流水线步骤，不改 `pipeline_range.py`，先把每一步的现状和重构目标说清楚。

目标不是立刻重写，而是先建立统一认知：

1. 现在每一步到底做了什么
2. 上下游通过什么文件和字段衔接
3. 现在哪些职责混在一起了
4. 未来每一步应该改成什么接口

当前建议顺序：

1. Step 1 `fetch`
2. Step 2.1 `bm25`
3. Step 2.2 `embedding`
4. Step 2.3 `rrf`
5. Step 3 `rerank`
6. Step 4 `llm refine`
7. Step 5 `select`
8. Step 6 `generate docs`

统一重构原则：

1. 先保留旧文件，新增一套清晰接口版本
2. 每一步先拆成“入口层”和“业务层”
3. 输入输出尽量显式，不依赖隐式环境变量
4. 每一步都能单独运行、单独测试、单独替换
5. 等步骤层稳定后，再改总入口去适配新接口

核心设计：按天运行，不混日期

这次重构的最上层原则，是把“单天”定义为整条流水线的最小处理单位。

也就是说：

1. Step 1 只抓取某一个自然日发表的论文
2. Step 2 到 Step 6 也都只处理这一天的论文池和中间结果
3. `archive/YYYYMMDD/` 下的所有文件都只能对应这一天，不能混入别的日期
4. 如果用户要处理一个日期区间 `A -> B`，应该由外层调度器按天循环运行完整 pipeline，而不是先抓一个跨天的大论文池，再在后面拆分

这样设计的原因是：

1. 最符合项目目标：论文必须按发表日期严格隔离，不能混天
2. 每一步的输入输出边界最清楚，目录语义也最清楚
3. 出错、补跑、重跑时都可以精确定位到某一天
4. 可以避免“先抓多天，再拆分”的中间混合状态，减少时间归属和状态管理上的歧义

因此，未来的新 pipeline 应该采用这样的模型：

1. day-scoped pipeline：每次完整运行只对应一个日期
2. range orchestration：日期区间只是外层调度方式，不是流水线内部的数据处理单位

建议给未来的新步骤统一一个接口形态：

```python
def run_step(context: RunContext, step_input: StepInput) -> StepOutput:
    ...
```

其中：

- `RunContext`：运行日期、路径、配置、source backend、模式
- `StepInput`：显式声明依赖的上一步产物
- `StepOutput`：数据、产物路径、统计信息、告警信息

在这个模型里，`RunContext` 里的“运行日期”应该是一个明确的单日，而不是一个模糊区间。

文件列表：

- `step-1-fetch.md`
- `step-2-1-bm25.md`
- `step-2-2-embedding.md`
- `step-2-3-rrf.md`
- `step-3-rerank.md`
- `step-4-llm-refine.md`
- `step-5-select.md`
- `step-6-generate-docs.md`
