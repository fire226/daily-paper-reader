#!/usr/bin/env python3
"""Step 7 generate Docsify Markdown pages for selected papers."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from step1_fetch import RunContext, build_run_context, log, resolve_run_date
from step3_rerank import RerankStepOutput, load_rerank_output
from step5_select import SelectStepOutput, load_select_output
from step6_enrichment import EnrichedPaper, EnrichmentStepOutput, load_enrichment_output


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
    mode: str = "standard"
    output_dir_override: Path | None = None


@dataclass(slots=True)
class GenerateDocsStepOutput:
    run_date: date | None = None
    artifacts: GenerateDocsArtifacts = field(default_factory=GenerateDocsArtifacts)
    stats: GenerateDocsStats = field(default_factory=GenerateDocsStats)
    warnings: list[str] = field(default_factory=list)


STEP7_NOTES = [
    "Add unit tests for markdown generation and sidebar update",
    "Decide whether home page update should be integrated or stay separate",
]


def _norm_text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_score(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def slugify(title: str) -> str:
    s = (title or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    return s or "paper"


def yaml_escape_value(s: str) -> str:
    s = str(s or "").strip()
    if not s:
        return '""'
    needs_quote = any(ch in s for ch in [":", "#", "@", "&", "*", "!", "|", ">", "'", '"', "{", "}", "[", "]", "%", "`", ",", "\n"])
    if needs_quote or s.startswith(" ") or s.endswith(" "):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def format_date_str(date_str: str) -> str:
    s = str(date_str or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def build_docsify_id_href(paper_id: str) -> str:
    return f"#/{paper_id}"


def resolve_docs_dir(context: RunContext, override: Path | None) -> Path:
    if override is not None:
        return override
    config = context.config or {}
    paper_setting = (config.get("arxiv_paper_setting") or {})
    crawler_setting = (config.get("crawler") or {})
    cfg_docs = paper_setting.get("docs_dir") or crawler_setting.get("docs_dir")
    if cfg_docs:
        p = Path(cfg_docs)
        if not p.is_absolute():
            p = context.root_dir / p
        return p
    return context.root_dir / "docs"


def prepare_paper_paths(docs_dir: Path, date_str: str, title: str, arxiv_id: str) -> tuple[Path, Path, str]:
    date_dir = docs_dir / date_str[:4] / date_str[4:6] / date_str[6:]
    slug = slugify(title)
    paper_id = _norm_text(arxiv_id)
    md_path = date_dir / f"{slug}.md"
    txt_path = date_dir / f"{slug}.txt"
    return md_path, txt_path, paper_id


def build_paper_docsify_href(date_str: str, title: str) -> str:
    slug = slugify(title)
    return f"#/{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}/{slug}"


def prepare_day_report_paths(docs_dir: Path, date_str: str) -> tuple[Path, Path]:
    date_dir = docs_dir / date_str[:4] / date_str[4:6] / date_str[6:]
    return date_dir, date_dir / "README.md"


def split_sidebar_tag(tag: str) -> tuple[str, str]:
    raw = str(tag or "").strip()
    if ":" in raw:
        kind, label = raw.split(":", 1)
        return kind.strip(), label.strip()
    return "other", raw


def build_tags_list(llm_tags: list[str]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in llm_tags:
        raw = str(tag).strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        tags.append(raw)
    return tags


def build_tags_html(llm_tags: list[str]) -> str:
    parts: list[str] = []
    for tag in llm_tags:
        kind, label = split_sidebar_tag(tag)
        if not label:
            continue
        css_class = f"dpr-tag-{kind}" if kind else "dpr-tag-other"
        parts.append(f'<span class="{css_class}">{html.escape(label)}</span>')
    return " ".join(parts)


def build_sidebar_item_payload(
    paper_id: str,
    title: str,
    tags: list[str],
    score: float,
    evidence: str,
) -> str:
    arxiv_id = paper_id.split("/")[-1]
    paper_link = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else f"#/{paper_id}"
    clean_tags: list[dict[str, str]] = []
    for tag in tags:
        kind, label = split_sidebar_tag(tag)
        if label:
            clean_tags.append({"kind": kind, "label": label})
    payload = {
        "title": title or paper_id,
        "link": paper_link,
        "score": f"{score:.1f}" if score else "-",
        "tags": clean_tags,
    }
    if evidence:
        payload["evidence"] = evidence
    return html.escape(json.dumps(payload, ensure_ascii=False), quote=True)


def build_markdown_content(
    paper: dict[str, Any],
    enriched: EnrichedPaper | None,
    section: str,
    tags_list: list[str],
) -> str:
    title = _norm_text(paper.get("title"))
    authors = paper.get("authors") or []
    published = _norm_text(paper.get("published"))[:10]
    pdf_url = _norm_text(paper.get("link") or paper.get("pdf_url"))
    score = paper.get("llm_score")
    evidence = _norm_text(paper.get("llm_evidence_cn") or paper.get("llm_evidence_en"))
    tldr = _norm_text(
        paper.get("llm_tldr_cn")
        or paper.get("llm_tldr")
        or paper.get("llm_tldr_en")
    )
    abstract_en = _norm_text(paper.get("abstract")) or "arXiv did not provide an abstract for this paper."

    zh_title = enriched.title_zh if enriched else ""
    zh_abstract = enriched.abstract_zh if enriched else ""
    glance_motivation = enriched.glance_motivation if enriched else ""
    glance_method = enriched.glance_method if enriched else ""
    glance_result = enriched.glance_result if enriched else ""
    glance_conclusion = enriched.glance_conclusion if enriched else ""

    lines: list[str] = ["---"]
    lines.append(f"title: {yaml_escape_value(title)}")
    if zh_title:
        lines.append(f"title_zh: {yaml_escape_value(zh_title)}")
    lines.append(f"authors: {yaml_escape_value(', '.join(authors) if authors else 'Unknown')}")
    lines.append(f"date: {yaml_escape_value(published or 'Unknown')}")
    if pdf_url:
        lines.append(f"pdf: {yaml_escape_value(pdf_url)}")
    if tags_list:
        lines.append(f"tags: [{', '.join(yaml_escape_value(t) for t in tags_list)}]")
    if score is not None:
        lines.append(f"score: {score}")
    if evidence:
        lines.append(f"evidence: {yaml_escape_value(evidence)}")
    if tldr:
        lines.append(f"tldr: {yaml_escape_value(tldr)}")
    if glance_motivation:
        lines.append(f"motivation: {yaml_escape_value(glance_motivation)}")
    if glance_method:
        lines.append(f"method: {yaml_escape_value(glance_method)}")
    if glance_result:
        lines.append(f"result: {yaml_escape_value(glance_result)}")
    if glance_conclusion:
        lines.append(f"conclusion: {yaml_escape_value(glance_conclusion)}")
    lines.append("---")
    lines.append("")

    if zh_abstract:
        lines.append("## 摘要")
        lines.append(zh_abstract)
        lines.append("")

    lines.append("## Abstract")
    lines.append(abstract_en)

    deep_summary = _norm_text(enriched.deep_summary) if enriched else ""
    if deep_summary:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 论文详细总结（自动生成）")
        lines.append("")
        lines.append(deep_summary)

    return "\n".join(lines)


def build_daily_brief_summary(
    date_label: str,
    deep_papers: list[dict[str, Any]],
    quick_papers: list[dict[str, Any]],
) -> str:
    total = len(deep_papers) + len(quick_papers)
    if total == 0:
        return ""
    lines: list[str] = []
    lines.append(f"本期共推荐 {total} 篇论文（精读 {len(deep_papers)} 篇，速读 {len(quick_papers)} 篇）。")
    if deep_papers:
        lines.append("")
        lines.append("**精读区亮点：**")
        for p in deep_papers[:3]:
            title = _norm_text(p.get("title"))
            score = p.get("llm_score", 0)
            evidence = _norm_text(p.get("llm_evidence_cn") or p.get("llm_evidence_en"))
            if title:
                lines.append(f"- {title}（{score:.0f} 分）{'：' + evidence if evidence else ''}")
    return "\n".join(lines)


def build_day_report_markdown(
    date_str: str,
    deep_papers: list[dict[str, Any]],
    quick_papers: list[dict[str, Any]],
) -> str:
    date_label = format_date_str(date_str)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total = len(deep_papers) + len(quick_papers)
    summary = build_daily_brief_summary(date_label, deep_papers, quick_papers)

    lines: list[str] = []
    lines.append(f"# 日报 · {date_label}")
    lines.append("")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append(f"- 当次推荐总数：{total}")
    lines.append(f"- 精读区：{len(deep_papers)}")
    lines.append(f"- 速读区：{len(quick_papers)}")
    if summary:
        lines.append("")
        lines.append("## 今日简报（AI）")
        lines.append(summary)
    lines.append("")

    lines.append("## 精读区")
    if deep_papers:
        for idx, p in enumerate(deep_papers, start=1):
            title = _norm_text(p.get("title"))
            score = p.get("llm_score", 0)
            suffix = f"（{score:.0f}）" if score else ""
            href = build_paper_docsify_href(date_str, title)
            lines.append(f"{idx}. [{title}]({href}) {suffix}")
    else:
        lines.append("- 本次无精读推荐。")
    lines.append("")

    lines.append("## 速读区")
    if quick_papers:
        for idx, p in enumerate(quick_papers, start=1):
            title = _norm_text(p.get("title"))
            score = p.get("llm_score", 0)
            suffix = f"（{score:.0f}）" if score else ""
            href = build_paper_docsify_href(date_str, title)
            lines.append(f"{idx}. [{title}]({href}) {suffix}")
    else:
        lines.append("- 本次无速读推荐。")
    lines.append("")

    lines.append("---")
    lines.append("使用键盘方向键可在日报/论文之间快速切换。")
    lines.append("")
    return "\n".join(lines)


def update_sidebar(
    sidebar_path: Path,
    date_str: str,
    deep_papers: list[dict[str, Any]],
    quick_papers: list[dict[str, Any]],
) -> None:
    marker = f"<!--dpr-date:{date_str}-->"
    date_label = format_date_str(date_str)
    day_heading = f"  * {date_label} {marker}\n"

    lines: list[str] = []
    if sidebar_path.exists():
        lines = sidebar_path.read_text(encoding="utf-8").splitlines(keepends=True)

    daily_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("* Daily Papers"):
            daily_idx = i
            break
    if daily_idx == -1:
        if not any("[首页]" in line for line in lines):
            lines.append("* [首页](/)\n")
        lines.append("* Daily Papers\n")
        daily_idx = len(lines) - 1

    day_idx = -1
    for i in range(daily_idx + 1, len(lines)):
        line = lines[i]
        if line.startswith("* "):
            break
        if marker in line:
            day_idx = i
            break

    if day_idx != -1:
        end = day_idx + 1
        while end < len(lines):
            if lines[end].startswith("  * ") and not lines[end].startswith("    * "):
                break
            end += 1
        del lines[day_idx:end]

    block: list[str] = [day_heading]
    if deep_papers:
        block.append("    * 精读区\n")
        for p in deep_papers:
            pid = _norm_text(p.get("id"))
            title = _norm_text(p.get("title")) or pid
            tags = build_tags_list(p.get("llm_tags") or [p.get("matched_query_tag") or ""])
            score = _coerce_score(p.get("llm_score"))
            evidence = _norm_text(p.get("llm_evidence_cn") or p.get("llm_evidence_en"))
            safe_title = html.escape(title)
            href = build_paper_docsify_href(date_str, title)
            payload_json = build_sidebar_item_payload(pid, title, tags, score, evidence)
            block.append(
                '      * '
                f'<a class="dpr-sidebar-item-link dpr-sidebar-item-structured" href="{href}" data-sidebar-item="{payload_json}">{safe_title}</a>\n'
            )
    if quick_papers:
        block.append("    * 速读区\n")
        for p in quick_papers:
            pid = _norm_text(p.get("id"))
            title = _norm_text(p.get("title")) or pid
            tags = build_tags_list(p.get("llm_tags") or [p.get("matched_query_tag") or ""])
            score = _coerce_score(p.get("llm_score"))
            evidence = _norm_text(p.get("llm_evidence_cn") or p.get("llm_evidence_en"))
            safe_title = html.escape(title)
            href = build_paper_docsify_href(date_str, title)
            payload_json = build_sidebar_item_payload(pid, title, tags, score, evidence)
            block.append(
                '      * '
                f'<a class="dpr-sidebar-item-link dpr-sidebar-item-structured" href="{href}" data-sidebar-item="{payload_json}">{safe_title}</a>\n'
            )

    insert_idx = daily_idx + 1
    lines[insert_idx:insert_idx] = block

    sidebar_path.parent.mkdir(parents=True, exist_ok=True)
    sidebar_path.write_text("".join(lines), encoding="utf-8")


def build_enrichment_map(
    enrichment_output: EnrichmentStepOutput | None,
) -> dict[str, EnrichedPaper]:
    if enrichment_output is None:
        return {}
    return {ep.paper_id: ep for ep in enrichment_output.enriched_papers}


def run_generate_docs_step(context: RunContext, step_input: GenerateDocsStepInput) -> GenerateDocsStepOutput:
    docs_dir = resolve_docs_dir(context, step_input.output_dir_override)
    warnings: list[str] = []
    date_str = step_input.run_date.strftime("%Y%m%d")

    enrichment_map = build_enrichment_map(step_input.enrichment_output)

    deep_papers = list(step_input.select_output.deep_dive)
    quick_papers = list(step_input.select_output.quick_skim)
    all_papers = deep_papers + quick_papers

    log(
        f"Generate docs run_date={step_input.run_date.isoformat()} "
        f"mode={step_input.mode} deep={len(deep_papers)} quick={len(quick_papers)} "
        f"docs_dir={docs_dir}"
    )

    paper_paths: list[Path] = []
    for paper in all_papers:
        pid = _norm_text(paper.get("id"))
        title = _norm_text(paper.get("title"))
        if not pid:
            continue

        enriched = enrichment_map.get(pid)
        tags = build_tags_list(paper.get("llm_tags") or [paper.get("matched_query_tag") or ""])
        section = "deep_dive" if paper in deep_papers else "quick_skim"

        md_path, txt_path, paper_id = prepare_paper_paths(docs_dir, date_str, title, pid)
        md_path.parent.mkdir(parents=True, exist_ok=True)

        content = build_markdown_content(paper, enriched, section, tags)
        md_path.write_text(content, encoding="utf-8")
        paper_paths.append(md_path)

    stats = GenerateDocsStats(papers_generated=len(paper_paths))

    _, day_readme = prepare_day_report_paths(docs_dir, date_str)
    day_readme.parent.mkdir(parents=True, exist_ok=True)
    day_content = build_day_report_markdown(date_str, deep_papers, quick_papers)
    day_readme.write_text(day_content, encoding="utf-8")
    stats.daily_report_generated = True

    sidebar_path = docs_dir / "_sidebar.md"
    update_sidebar(sidebar_path, date_str, deep_papers, quick_papers)
    stats.sidebar_updated = True

    log(f"[INFO] Generated {stats.papers_generated} paper pages")
    log(f"[INFO] Daily report: {day_readme}")
    log(f"[INFO] Sidebar: {sidebar_path}")

    return GenerateDocsStepOutput(
        run_date=step_input.run_date,
        artifacts=GenerateDocsArtifacts(
            docs_dir=docs_dir,
            paper_paths=paper_paths,
            sidebar_path=sidebar_path,
        ),
        stats=stats,
        warnings=warnings,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step 7 generate Docsify Markdown pages")
    parser.add_argument("--run-date", required=False, help="Run date in YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--mode", default="standard", help="Recommendation mode")
    parser.add_argument(
        "--select-input-path-override",
        default=None,
        help="Optional Step 5 select output path override",
    )
    parser.add_argument(
        "--enrichment-input-path-override",
        default=None,
        help="Optional Step 6 enrichment output path override",
    )
    parser.add_argument(
        "--output-dir-override",
        default=None,
        help="Optional docs output directory override",
    )
    parser.add_argument(
        "--skip-enrichment",
        action="store_true",
        help="Skip loading enrichment output",
    )
    parser.add_argument(
        "--print-todos",
        action="store_true",
        help="Print Step 7 follow-up notes and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.print_todos:
        for idx, item in enumerate(STEP7_NOTES, start=1):
            print(f"{idx}. {item}")
        return 0

    if not args.run_date:
        parser.error("the following arguments are required: --run-date")

    root_dir = Path(__file__).resolve().parents[2]
    context = build_run_context(root_dir)
    try:
        run_date = resolve_run_date(args.run_date)
    except ValueError as exc:
        parser.error(str(exc))

    token = run_date.strftime("%Y%m%d")

    select_input_path = (
        Path(args.select_input_path_override).resolve()
        if args.select_input_path_override
        else context.archive_root / token / "recommend" / f"arxiv_papers_{token}.{args.mode}.json"
    )
    select_output = load_select_output(select_input_path)

    enrichment_output: EnrichmentStepOutput | None = None
    if not args.skip_enrichment:
        enrichment_input_path = (
            Path(args.enrichment_input_path_override).resolve()
            if args.enrichment_input_path_override
            else context.archive_root / token / "enriched" / f"arxiv_papers_{token}.enriched.json"
        )
        if enrichment_input_path.exists():
            enrichment_output = load_enrichment_output(enrichment_input_path)
        else:
            log(f"[WARN] Enrichment file not found: {enrichment_input_path}, proceeding without enrichment")

    step_input = GenerateDocsStepInput(
        run_date=run_date,
        select_output=select_output,
        enrichment_output=enrichment_output,
        mode=args.mode,
        output_dir_override=Path(args.output_dir_override).resolve() if args.output_dir_override else None,
    )

    log("Step 7 v2 starting")
    log(f"run_date={run_date.isoformat()}")
    output = run_generate_docs_step(context, step_input)
    log(f"papers_generated={output.stats.papers_generated}")
    log(f"daily_report_generated={output.stats.daily_report_generated}")
    log(f"sidebar_updated={output.stats.sidebar_updated}")
    if output.warnings:
        log(f"warnings={len(output.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
