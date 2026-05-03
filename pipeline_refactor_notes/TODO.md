# TODO

## Query Schema Cleanup

当前 v2 的阶段性决策是：

1. Step 2.1 和 Step 2.2 共用同一套 query list
2. 对 `keywords` 只使用 `keyword` 文本
3. 暂时忽略 `keywords[*].query` 这种 hidden semantic rewrite

这样做的原因是：

1. 先把 pipeline v2 的 retrieval lane 结构收干净
2. 避免 `2.1` / `2.2` query list 不一致，导致 `2.3` RRF 对齐复杂化
3. 把 config schema / front-end 交互改造留到后续单独处理

后续需要单独完成的事项：

1. 决定 `config.yaml` 里 `keywords[*].query` 是否彻底移除
2. 如果保留 `keywords[*].query`，前端必须显式展示它，而不是继续隐藏
3. 如果移除 `keywords[*].query`，需要同步清理：
   - front-end candidate generation prompt
   - front-end keyword editing UI
   - `subscription_plan` 的 query 构造逻辑
   - embedding cache 相关字段和兼容逻辑
4. 如果未来仍想保留“BM25 用 keyword / embedding 用 rewrite”的能力，应改成显式 query id 设计，而不是继续依赖 `query_text` 对齐
