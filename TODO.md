# TODO — daily-paper-reader

## 日期区间抓取（当前优先）

### 目标

- 支持选择日期区间，按天独立运行完整 pipeline（BM25 → Embedding → RRF → LLM 评分 → 精选 → 生成文档）
- 10 天区间 = 10 次单天抓取的批处理，每天完全独立，单天运行和区间运行输出一致
- 用日期区间功能替代并移除原来的 quick-fetch（quick-fetch 等价于拉取最近 N 天，已是子功能）

### 新文件

- [x] `pipeline_range.py` — 区间入口脚本
  - 参数：`--start-date YYYYMMDD`、`--end-date YYYYMMDD`、`--skip-existing`
  - 循环遍历区间内每天：
    - 设置 `DPR_RUN_DATE={day}`、`DOCS_DIR=data/range/{start}-{end}/docs`
    - 设置 `DPR_ARCHIVE_DIR=data/range/{start}-{end}/{day}`（中间文件隔离）
    - 调用 Step 2.1 → 2.2 → 2.3 → 4 → 5 → 6（复用 main.py 的 run_step）
  - 不调 Step 1（Supabase 已有数据）、不调 Step 3（rerank 仅特定模型需要）
  - 每天 top_k 自适应：当天论文 ≤1000 → 50，>1000 → 100
  - Step 5 传入 `--deep-dive-target 5 --quick-skim-target 10`

### 文件变更

- [x] `serve.py`
  - 移除：`POST /api/quick-fetch`、`GET /api/quick-fetch/status`
  - 新增：`POST /api/range-fetch` — 接收 `{start_date, end_date, skip_existing}`，启动 pipeline_range.py
  - 新增：`GET /api/range-fetch/status` — 轮询任务状态和日志
  - 新增：`GET /api/last-run` — 返回最近一次运行的 {type, start_date, end_date, status, finished_at}
  - 任务锁：range-fetch 和原有 pipeline 共用 `_fetch_lock`，同一时刻只能一个任务运行

- [ ] `index.html`
  - 移除：原 quick-fetch 按钮及其进度 UI
  - 新增：日期区间选择器 — 两个 `<input type="date">` + "开始抓取"按钮 + 跳过已有结果勾选框
  - 新增：运行日志区域（复用现有轮询样式）
  - 新增：首页展示最近一次运行信息（从 /api/last-run 获取）

- [x] `workflows.runner.js`
  - 移除：`QUICK_FETCH_PRESETS`、`_localQuickFetch`、`runQuickFetchByDays`
  - 新增：`_localRangeFetch`、`runRangeFetch`（兼容旧 days API 和新 startDate/endDate API）
  - 所有状态轮询改用 `/api/range-fetch/status`
  - `chat.discussion.js`、`subscriptions.manager.js` 同步更新引用

- [x] 各 Step 脚本适配 `DPR_ARCHIVE_DIR`
  - Step 2.1/2.2/2.3/4/5：检查 `DPR_ARCHIVE_DIR` 环境变量，有则用它替代 `archive/{TODAY_STR}`
  - Step 6：已支持 `DOCS_DIR` 环境变量，无需改动
  - 无环境变量时行为不变，不影响原有单天 workflow

### 中间文件隔离

```
data/range/
  20260401-20260410/
    20260401/          ← DPR_ARCHIVE_DIR，当天所有中间文件
      raw/
      filtered/
      rank/
      recommend/
    20260402/
      ...
    docs/              ← DOCS_DIR
      2026/
        04/
          01/README.md, 精读文档, 速读文档
          02/...
```

与原有 `archive/YYYYMMDD/` 完全隔离，互不干扰。

### 验收

- [ ] 单天区间（start=end）输出与直接运行 main.py 完全一致
- [ ] 多天区间逐天独立处理，天与天之间无耦合
- [ ] 中间文件不与 archive/ 混在一起
- [ ] 前端可触发区间抓取、查看进度、查看最近一次运行信息
- [ ] --skip-existing 正确跳过已有完整输出的天
- [ ] quick-fetch 端点已移除，旧前端请求返回 404

## 日志/历史清理

- [ ] 清理 `logs/` 下旧的 `run_*.log`，只保留最近 N 条

## 搁置（暂不开发）

- **每日简报**：区间模式下为每天生成独立简报（精读区摘要 + 速读区列表）。等区间功能稳定后再做。
- **日历选择器**：当前用两个 `<input type="date">`，后续可升级为日历组件。
- **多天结果聚合展示**：首页只展示最近一次运行信息，不展示多天结果列表。
