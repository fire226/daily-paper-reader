# TODO — daily-paper-reader

## 已知问题

1. **网络不稳定导致 LLM/JINA API 调用间歇性失败**
   - OpenRouter SSL 连接断开（Step 4 LLM refine）
   - JINA API 超时（Step 6 PDF 下载）
   - 这是环境/网络问题，非代码 bug。重试逻辑已存在

2. **Step 5 没有 `--deep-dive-target` / `--quick-skim-target` 参数**
   - 当前 standard 模式默认 deep=5, quick=10，已经满足需求
   - 如果需要自定义数量，需在 Step 5 中添加新参数

3. **Supabase 查询窗口基于当前时间而非目标日期**
   - `resolve_supabase_recall_window()` 使用当前 UTC 时间计算窗口
   - DPR_RUN_DATE 为单日格式（如 20260101）时，anchor 仍是 "now"
   - 效果：查询的是最近 9 天的论文，而非 2026-01-01 附近
   - 对功能无影响（Supabase 里就是近期论文），但语义上不够精确

### 待完成

- [ ] 完整测试 Day 20260406~20260407（需等网络稳定后重跑）
- [ ] 日志清理：`logs/` 下旧的 `run_*.log`，只保留最近 N 条

### 搁置（暂不开发）

- **每日简报**：区间模式下为每天生成独立简报（精读区摘要 + 速读区列表）。等区间功能稳定后再做。
- **日历选择器**：当前用两个 `<input type=\"date\">`，后续可升级为日历组件。
- **多天结果聚合展示**：首页只展示最近一次运行信息，不展示多天结果列表。
