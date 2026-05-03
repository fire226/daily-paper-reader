# Step 6 Generate Docs

原文件：`src/6.generate_docs.py`

## 现在是怎么做的

这一步把推荐结果最终转换成 Docsify 可展示的 Markdown 文档，并补充多种增强内容。

当前功能非常多，已经明显超出“单纯生成 docs”：

1. 读取推荐结果
2. 生成 Markdown 页面
3. 更新 sidebar
4. 拉取 arXiv 元数据
5. 下载 PDF / 抽文本 / 抽图
6. 生成中文翻译
7. 生成速览和详细总结
8. 兼容历史 Markdown 格式
9. 单篇补生成与 sidebar-only 模式

## 当前输入输出

输入：

- Step 5 推荐结果
- LLM 配置
- docs 目录配置
- 本地已有 Markdown 和 PDF 辅助信息

输出：

- `docs/YYYY/MM/DD/*.md`
- `docs/_sidebar.md`
- 相关图片和附属文本

## 当前问题

1. 文件过大，职责最分散
2. 文档渲染、元数据补全、LLM 总结、sidebar 维护、格式兼容都混在一起
3. 这一步既像生成器，又像迁移脚本，又像内容修复脚本
4. 对流水线主链来说，真正核心的是“把推荐结果变成最终发布物”，但现在周边功能过多

## 应该怎么改

建议按职责拆成几个明确组件：

1. `DocsPageRenderer`
2. `SidebarUpdater`
3. `PaperMetaResolver`
4. `PaperAssetBuilder`
5. `SummaryGenerator`
6. `MarkdownNormalizer`

## 建议的新接口

```python
def run_generate_docs_step(
    context: RunContext,
    select_output: SelectStepOutput,
) -> GenerateDocsStepOutput:
    ...
```

输出建议包含：

- `generated_pages`
- `sidebar_updated`
- `docs_dir`
- `artifact_paths`
- `stats`
- `warnings`

## 重构重点

这一步建议最后动，因为它已经不是简单的 pipeline step，而是“发布层”。

重构目标不是删功能，而是把“主发布路径”和“增强功能”拆开：

1. 核心路径：推荐结果 -> Markdown 页面 -> sidebar
2. 增强路径：翻译、总结、PDF 文本、图表抽取

只有这样，后续入口层才可能稳定对接它。
