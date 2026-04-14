# TODO — daily-paper-reader

## 日期区间抓取（当前优先）

### 目标

- 支持选择日期区间，按天独立运行完整 pipeline（BM25 → Embedding → RRF → LLM 评分 → 精选 → 生成文档）
- 10 天区间 = 10 次单天抓取的批处理，每天完全独立，单天运行和区间运行输出一致
- 用日期区间功能替代并移除原来的 quick-fetch（quick-fetch 等价于拉取最近 N 天，已是子功能）

### 已完成

- [x] `pipeline_range.py` — 区间入口脚本
  - 参数：`--start-date YYYYMMDD`、`--end-date YYYYMMDD`、`--skip-existing`、`--top-k`、`--min-star`
  - 循环遍历区间内每天，调用 Step 2.1 → 2.2 → 2.3 → 3 → 4 → 5 → 6
  - 环境变量隔离：`DPR_RUN_DATE`、`DPR_ARCHIVE_DIR`、`DOCS_DIR`
  - Step 3 自动判断是否跳过 rerank（与 main.py 一致）
  - 运行完成后写入 `data/last_run.json`

- [x] `serve.py`
  - 移除 `/api/quick-fetch`、`/api/quick-fetch/status`
  - 新增 `POST /api/range-fetch`、`GET /api/range-fetch/status`、`GET /api/last-run`

- [x] `workflows.runner.js`
  - `runRangeFetch(startDate, endDate, opts)` 替换 `runQuickFetchByDays`
  - 兼容旧 days API：传 `runRangeFetch(days)` 自动转换为日期区间
  - 状态轮询改用 `/api/range-fetch/status`
  - `chat.discussion.js`、`subscriptions.manager.js`、`test_subscriptions_manager.js` 同步更新

- [x] Step 脚本适配 `DPR_ARCHIVE_DIR`
  - `src/2.1.retrieval_papers_bm25.py` ✅
  - `src/2.2.retrieval_papers_embedding.py` ✅
  - `src/2.3.retrieval_papers_rrf.py` ✅
  - `src/3.rank_papers.py` ✅
  - `src/4.llm_refine_papers.py` ✅
  - `src/5.select_papers.py` ✅
  - `src/6.generate_docs.py` ✅（修复了两处硬编码 `archive/` 路径：L1962 和 L2477）

- [x] 测试验证（2026-01-01 ~ 2026-01-05，`--top-k 50`）
  - Day 20260101 完整跑通：Step 2.1 ✅ → 2.2 ✅ → 2.3 ✅ → 3(skip) ✅ → 4 ✅ → 5 ✅ → 6 ✅
  - 中间文件正确隔离在 `data/range/20260101-20260105/20260101/`
  - 文档生成在 `data/range/20260101-20260105/docs/202601/01/`（6 精读 + 11 速读）
  - Day 20260102~20260105 部分完成（Step 2.1/2.2/2.3 跑完，Step 4 因网络不稳定中断）

### 已知问题

1. **`src/6.generate_docs.py` 硬编码 archive 路径**
   - 已修复 L1962（log_dir）和 L2477（recommend_path），但需确认文件中没有其他遗漏
   - 修复方式：`os.getenv("DPR_ARCHIVE_DIR") or os.path.join(ROOT_DIR, "archive", date_str)`

2. **`pipeline_range.py` 中 `should_skip_rerank()` 需要 .env 加载到 os.environ**
   - `from main import should_skip_rerank` 在当前 Python 进程中运行，读 `os.getenv()`
   - 但 .env 只加载到了子进程 env dict，当前进程的 os.environ 没有
   - 已修复：加载 .env 时同时 `os.environ.setdefault(k, v)`

3. **网络不稳定导致 LLM/JINA API 调用间歇性失败**
   - OpenRouter SSL 连接断开（Step 4 LLM refine）
   - JINA API 超时（Step 6 PDF 下载）
   - 这是环境/网络问题，非代码 bug。重试逻辑已存在

4. **docs 输出不在浏览器可访问目录**
   - 区间模式的 docs 生成在 `data/range/{range}/docs/`，Docsify 从 `docs/` 读取
   - 临时方案：symlink `docs/202601/01 -> ../../data/range/.../docs/202601/01`
   - 需要决定长期方案：symlink / 复制 / 或让 serve.py 直接服务 range docs

5. **Step 5 没有 `--deep-dive-target` / `--quick-skim-target` 参数**
   - TODO.md 原计划传入这些参数，但 Step 5 实际用 `MODES` dict 的 `deep_base`/`quick_base`
   - 当前 standard 模式默认 deep=5, quick=10，已经满足需求
   - 如果需要自定义数量，需在 Step 5 中添加新参数

6. **Supabase 查询窗口基于当前时间而非目标日期**
   - `resolve_supabase_recall_window()` 使用当前 UTC 时间计算窗口
   - DPR_RUN_DATE 为单日格式（如 20260101）时，anchor 仍是 "now"
   - 效果：查询的是最近 9 天的论文（2026-04-05 ~ 2026-04-14），而非 2026-01-01 附近
   - 对功能无影响（Supabase 里就是近期论文），但语义上不够精确

### 待完成

- [ ] `index.html` — 前端日期区间选择器 UI
  - 移除：原 quick-fetch 按钮及其进度 UI
  - 新增：两个 `<input type="date">` + "开始抓取"按钮 + 跳过已有结果勾选框
  - 新增：运行日志区域（复用现有轮询样式）
  - 新增：首页展示最近一次运行信息（从 /api/last-run 获取）

- [ ] 完整测试 Day 20260102~20260105（需等网络稳定后重跑）

- [ ] 确认 docs 输出到浏览器的长期方案

- [ ] 日志清理：`logs/` 下旧的 `run_*.log`，只保留最近 N 条

### 搁置（暂不开发）

- **每日简报**：区间模式下为每天生成独立简报（精读区摘要 + 速读区列表）。等区间功能稳定后再做。
- **日历选择器**：当前用两个 `<input type=\"date\">`，后续可升级为日历组件。
- **多天结果聚合展示**：首页只展示最近一次运行信息，不展示多天结果列表。
