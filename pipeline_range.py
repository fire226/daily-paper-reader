#!/usr/bin/env python3
"""Run the day-scoped v2 pipeline over a date range."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pipeline_v2 import step1_fetch as step1
from pipeline_v2 import step2_1_bm25 as step2_bm25
from pipeline_v2 import step2_2_embedding as step2_embedding
from pipeline_v2 import step2_3_rrf as step2_rrf
from pipeline_v2 import step3_rerank as step3
from pipeline_v2 import step4_llm_refine as step4
from pipeline_v2 import step5_select as step5
from pipeline_v2 import step6_enrichment as step6
from pipeline_v2 import step7_generate_docs as step7


def _norm_text(value: object) -> str:
    return str(value or "").strip()


def parse_date_arg(value: str) -> date:
    return step1.resolve_run_date(value)


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip())


def resolve_mode(config: dict[str, object], override: str | None) -> str:
    raw_mode = _norm_text(override)
    if not raw_mode:
        setting = (config.get("arxiv_paper_setting") or {}) if isinstance(config, dict) else {}
        raw_mode = _norm_text(setting.get("mode")) or "standard"
    modes = [item.strip() for item in raw_mode.split(",") if item.strip()]
    modes = [item for item in modes if item in step5.MODES]
    if not modes:
        raise ValueError("mode must be one of: standard, extend, spark, skims")
    if len(modes) > 1:
        step1.log(f"[WARN] Multiple modes configured ({modes}); pipeline_range v2 will use only '{modes[0]}'.")
    return modes[0]


def ensure_docs_runtime_files(docs_dir: Path) -> None:
    docs_dir.mkdir(parents=True, exist_ok=True)
    sidebar_path = docs_dir / "_sidebar.md"
    if not sidebar_path.exists():
        sidebar_path.write_text("* [首页](/)\n* Daily Papers\n", encoding="utf-8")
    not_found_path = docs_dir / "_404.md"
    if not not_found_path.exists():
        not_found_path.write_text("# Not Found\n\nThe requested page was not found.\n", encoding="utf-8")


def sort_sidebar_by_date(sidebar_path: Path) -> None:
    if not sidebar_path.exists():
        return
    lines = sidebar_path.read_text(encoding="utf-8").splitlines(keepends=True)
    daily_idx = -1
    for idx, line in enumerate(lines):
        if line.strip().startswith("* Daily Papers"):
            daily_idx = idx
            break
    if daily_idx == -1:
        return

    date_blocks: list[tuple[str, list[str]]] = []
    current_block: list[str] = []
    current_date = ""
    for line in lines[daily_idx + 1 :]:
        if line.startswith("  * ") and "<!--dpr-date:" in line:
            if current_block and current_date:
                date_blocks.append((current_date, current_block))
            current_date = line.split("<!--dpr-date:", 1)[1].split("-->", 1)[0].strip()
            current_block = [line]
        elif current_block:
            current_block.append(line)

    if current_block and current_date:
        date_blocks.append((current_date, current_block))

    date_blocks.sort(key=lambda item: item[0], reverse=True)
    rebuilt = lines[: daily_idx + 1]
    for _date_key, block in date_blocks:
        rebuilt.extend(block)
    sidebar_path.write_text("".join(rebuilt), encoding="utf-8")


def list_day_report_paths(docs_dir: Path) -> list[Path]:
    reports: list[tuple[str, Path]] = []
    for report_path in docs_dir.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/README.md"):
        parts = report_path.parts
        if len(parts) < 4:
            continue
        date_token = f"{parts[-4]}{parts[-3]}{parts[-2]}"
        if len(date_token) == 8 and date_token.isdigit():
            reports.append((date_token, report_path))
    reports.sort(key=lambda item: item[0], reverse=True)
    return [path for _token, path in reports]


def extract_section(markdown: str, heading: str) -> str:
    lines = markdown.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() != heading:
            continue
        section: list[str] = []
        for next_line in lines[idx + 1 :]:
            if next_line.startswith("## "):
                break
            section.append(next_line)
        return "\n".join(section).strip()
    return ""


def extract_stat(markdown: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}：(.+)$", markdown, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def build_home_readme_from_report(report_path: Path) -> str:
    markdown = report_path.read_text(encoding="utf-8")
    date_token = f"{report_path.parts[-4]}{report_path.parts[-3]}{report_path.parts[-2]}"
    date_label = step7.format_date_str(date_token)
    run_time = datetime.fromtimestamp(report_path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total = extract_stat(markdown, "当次推荐总数") or "0"
    deep = extract_stat(markdown, "精读区") or "0"
    quick = extract_stat(markdown, "速读区") or "0"
    summary = extract_section(markdown, "## 今日简报（AI）")
    report_link = f"/{date_token[:4]}/{date_token[4:6]}/{date_token[6:]}/README"

    lines = [
        "# Daily Paper Reader",
        "",
        "## 每次日报",
        f"- 最新运行日期：{date_label}",
        f"- 运行时间：{run_time}",
        "- 运行状态：成功",
        f"- 本次总论文数：{total}",
        f"- 精读区：{deep}",
        f"- 速读区：{quick}",
        "",
        "### 今日简报（AI）",
    ]
    if summary:
        lines.append(summary)
    else:
        lines.append("> 暂无自动简报。")
    lines.extend([
        f"- 详情：[{report_link}]({report_link})",
        "",
        "使用键盘方向键可在日报/论文之间快速切换。",
        "",
    ])
    return "\n".join(lines)


def write_home_readme(docs_dir: Path) -> None:
    reports = list_day_report_paths(docs_dir)
    home_path = docs_dir / "README.md"
    if not reports:
        home_path.write_text(
            "# Daily Paper Reader\n\n## 每次日报\n- 暂无已生成日报。\n",
            encoding="utf-8",
        )
        return
    home_path.write_text(build_home_readme_from_report(reports[0]), encoding="utf-8")


def day_report_path_for(docs_dir: Path, run_date: date) -> Path:
    _day_dir, readme_path = step7.prepare_day_report_paths(docs_dir, run_date.strftime("%Y%m%d"))
    return readme_path


def try_load_output(path: Path, load_fn, step_label: str):
    try:
        return load_fn(path)
    except Exception as exc:
        step1.log(f"[WARN] {step_label} existing output unreadable, recomputing: {path} ({exc})")
        return None


def run_day_pipeline(
    context: step1.RunContext,
    run_date: date,
    docs_dir: Path,
    *,
    force_existing: bool,
    ignore_seen: bool,
    categories_override: list[str] | None,
    top_k_override: int | None,
    embedding_device: str,
    embedding_batch_size: int,
    embedding_model_name: str,
    embedding_max_length: int | None,
    rrf_top_k: int,
    rrf_k: int,
    rerank_top_n: int | None,
    rerank_model: str,
    disable_rerank: bool,
    min_star: int,
    filter_batch_size: int,
    filter_max_chars: int,
    filter_model: str,
    filter_max_output_tokens: int,
    filter_concurrency: int,
    mode: str,
    enrichment_model: str,
    enrichment_max_output_tokens: int,
    skip_enrichment: bool,
    docs_dir_override: Path | None,
) -> None:
    date_token = run_date.strftime("%Y%m%d")
    day_report_path = day_report_path_for(docs_dir, run_date)
    if not force_existing and day_report_path.exists():
        step1.log(f"[INFO] Skip {date_token}: daily report already exists at {day_report_path}")
        return

    archive_day_dir = context.archive_root / date_token
    step1.log("=" * 60)
    step1.log(f"[INFO] V2 pipeline date={date_token}")
    step1.log(f"[INFO] archive_dir={archive_day_dir}")
    step1.log(f"[INFO] docs_dir={docs_dir}")
    step1.log("=" * 60)

    raw_path = archive_day_dir / "raw" / f"arxiv_papers_{date_token}.json"
    if raw_path.exists() and not force_existing:
        step1.log(f"[INFO] Step 1 skipped [{date_token}]: output exists")
        papers = step2_bm25.load_papers_from_json(raw_path)
    else:
        fetch_output = step1.run_fetch_step(
            context,
            step1.FetchStepInput(
                run_date=run_date,
                ignore_seen=ignore_seen,
                categories_override=categories_override,
            ),
        )
        papers = list(fetch_output.papers)

    top_k = max(int(top_k_override), 1) if top_k_override is not None else step2_bm25.estimate_dynamic_top_k(len(papers))
    queries = step2_bm25.load_retrieval_queries(context.config)

    bm25_path = archive_day_dir / "filtered" / f"arxiv_papers_{date_token}.bm25.json"
    if bm25_path.exists() and not force_existing:
        bm25_output = try_load_output(bm25_path, step2_rrf.load_bm25_output, "Step 2.1")
        if bm25_output is not None:
            step1.log(f"[INFO] Step 2.1 skipped [{date_token}]: output exists")
    else:
        bm25_output = None
    if bm25_output is None:
        bm25_output = step2_bm25.run_bm25_step(
            context,
            step2_bm25.BM25StepInput(
                run_date=run_date,
                papers=papers,
                queries=queries,
                top_k=top_k,
            ),
        )

    embedding_path = archive_day_dir / "filtered" / f"arxiv_papers_{date_token}.embedding.json"
    if embedding_path.exists() and not force_existing:
        embedding_output = try_load_output(embedding_path, step2_rrf.load_embedding_output, "Step 2.2")
        if embedding_output is not None:
            step1.log(f"[INFO] Step 2.2 skipped [{date_token}]: output exists")
    else:
        embedding_output = None
    if embedding_output is None:
        embedding_output = step2_embedding.run_embedding_step(
            context,
            step2_embedding.EmbeddingStepInput(
                run_date=run_date,
                papers=papers,
                queries=queries,
                top_k=top_k,
                model_name=embedding_model_name,
                device=embedding_device,
                batch_size=embedding_batch_size,
                max_length=embedding_max_length,
            ),
        )

    rrf_path = archive_day_dir / "filtered" / f"arxiv_papers_{date_token}.json"
    if rrf_path.exists() and not force_existing:
        rrf_output = try_load_output(rrf_path, step3.load_rrf_output, "Step 2.3")
        if rrf_output is not None:
            step1.log(f"[INFO] Step 2.3 skipped [{date_token}]: output exists")
    else:
        rrf_output = None
    if rrf_output is None:
        rrf_output = step2_rrf.run_rrf_step(
            context,
            step2_rrf.RRFStepInput(
                run_date=run_date,
                bm25_output=bm25_output,
                embedding_output=embedding_output,
                top_k=max(rrf_top_k, 1),
                rrf_k=max(rrf_k, 1),
            ),
        )

    rerank_path = archive_day_dir / "rank" / f"arxiv_papers_{date_token}.rerank.json"
    if rerank_path.exists() and not force_existing:
        rerank_output = try_load_output(rerank_path, step3.load_rerank_output, "Step 3")
        if rerank_output is not None:
            step1.log(f"[INFO] Step 3 skipped [{date_token}]: output exists")
    else:
        rerank_output = None
    if rerank_output is None:
        rerank_output = step3.run_rerank_step(
            context,
            step3.RerankStepInput(
                run_date=run_date,
                rrf_output=rrf_output,
                top_n=rerank_top_n,
                rerank_model=rerank_model,
                disable_rerank=disable_rerank,
            ),
        )

    llm_path = archive_day_dir / "rank" / f"arxiv_papers_{date_token}.llm.json"
    if llm_path.exists() and not force_existing:
        llm_output = try_load_output(llm_path, step4.load_llm_refine_output, "Step 4")
        if llm_output is not None:
            step1.log(f"[INFO] Step 4 skipped [{date_token}]: output exists")
    else:
        llm_output = None
    if llm_output is None:
        llm_output = step4.run_llm_refine_step(
            context,
            step4.LLMRefineStepInput(
                run_date=run_date,
                rerank_output=rerank_output,
                min_star=max(min_star, 0),
                batch_size=max(filter_batch_size, 1),
                max_chars=max(filter_max_chars, 100),
                filter_model=filter_model,
                max_output_tokens=max(filter_max_output_tokens, 256),
                filter_concurrency=max(filter_concurrency, 1),
            ),
        )

    select_path = archive_day_dir / "recommend" / f"arxiv_papers_{date_token}.{mode}.json"
    if select_path.exists() and not force_existing:
        select_output = try_load_output(select_path, step5.load_select_output, "Step 5")
        if select_output is not None:
            step1.log(f"[INFO] Step 5 skipped [{date_token}]: output exists")
    else:
        select_output = None
    if select_output is None:
        select_output = step5.run_select_step(
            context,
            step5.SelectStepInput(
                run_date=run_date,
                llm_output=llm_output,
                rerank_output=rerank_output,
                mode=mode,
            ),
        )

    enrichment_output: step6.EnrichmentStepOutput | None = None
    if skip_enrichment:
        step1.log(f"[INFO] Step 6 skipped [{date_token}]: --skip-enrichment enabled")
    else:
        enrichment_path = archive_day_dir / "enriched" / f"arxiv_papers_{date_token}.enriched.json"
        if enrichment_path.exists() and not force_existing:
            enrichment_output = try_load_output(enrichment_path, step6.load_enrichment_output, "Step 6")
            if enrichment_output is not None:
                step1.log(f"[INFO] Step 6 skipped [{date_token}]: output exists")
        if enrichment_output is None:
            enrichment_output = step6.run_enrichment_step(
                context,
                step6.EnrichmentStepInput(
                    run_date=run_date,
                    select_output=select_output,
                    rerank_output=rerank_output,
                    llm_model=enrichment_model,
                    max_output_tokens=max(enrichment_max_output_tokens, 256),
                ),
            )

    docs_output = step7.run_generate_docs_step(
        context,
        step7.GenerateDocsStepInput(
            run_date=run_date,
            select_output=select_output,
            enrichment_output=enrichment_output,
            mode=mode,
            output_dir_override=docs_dir_override,
        ),
    )
    step1.log(
        f"[INFO] Day complete [{date_token}] deep={select_output.stats.deep_selected} "
        f"quick={select_output.stats.quick_selected} docs={docs_output.stats.papers_generated}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the day-scoped pipeline_v2 over a date range")
    parser.add_argument("--start-date", required=True, help="Start date in YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="End date in YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--mode", default=None, help="Recommendation mode: standard, extend, spark, skims")
    parser.add_argument("--force-existing", action="store_true", help="Re-run days even if day report already exists")
    parser.add_argument("--ignore-seen", action="store_true", help="Ignore persisted fetch state for Step 1")
    parser.add_argument("--categories", default=None, help="Optional comma-separated arXiv categories override")
    parser.add_argument("--top-k", type=int, default=None, help="Per-query top k for BM25 and embedding")
    parser.add_argument("--embedding-device", default="cpu", help="Embedding device, e.g. cpu or cuda")
    parser.add_argument("--embedding-batch-size", type=int, default=8, help="Embedding encode batch size")
    parser.add_argument("--embedding-model-name", default=step2_embedding.DEFAULT_EMBED_MODEL, help="Embedding model name override")
    parser.add_argument("--embedding-max-length", type=int, default=None, help="Optional embedding max sequence length")
    parser.add_argument("--rrf-top-k", type=int, default=200, help="Per-query top k after Step 2.3 RRF fusion")
    parser.add_argument("--rrf-k", type=int, default=60, help="RRF denominator offset")
    parser.add_argument("--rerank-top-n", type=int, default=None, help="Per-query top N to keep after Step 3 rerank")
    parser.add_argument("--rerank-model", default=os.getenv("RERANK_MODEL") or step3.DEFAULT_RERANK_MODEL, help="Rerank model name for SiliconFlow")
    parser.add_argument("--disable-rerank", action="store_true", help="Disable external rerank and force Step 3 fallback")
    parser.add_argument("--min-star", type=int, default=4, help="Minimum Step 3 star rating to keep for Step 4")
    parser.add_argument("--filter-batch-size", type=int, default=25, help="Step 4 LLM batch size")
    parser.add_argument("--filter-max-chars", type=int, default=step4.MAX_CHARS_PER_DOC, help="Max chars per candidate doc in Step 4")
    parser.add_argument("--filter-model", default=os.getenv("FILTER_MODEL") or step4.DEFAULT_FILTER_MODEL, help="LLM model for Step 4 filtering")
    parser.add_argument("--filter-max-output-tokens", type=int, default=4000, help="Max output tokens for Step 4")
    parser.add_argument("--filter-concurrency", type=int, default=step4.DEFAULT_FILTER_CONCURRENCY, help="Concurrent LLM workers for Step 4")
    parser.add_argument("--skip-enrichment", action="store_true", help="Skip Step 6 enrichment and generate docs without translations/summaries")
    parser.add_argument("--enrichment-model", default=os.getenv("FILTER_MODEL") or step6.DEFAULT_LLM_MODEL, help="LLM model for Step 6 enrichment")
    parser.add_argument("--enrichment-max-output-tokens", type=int, default=4000, help="Max output tokens for Step 6")
    parser.add_argument("--docs-dir-override", default=None, help="Optional docs output directory override")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    load_env_file(ROOT_DIR / ".env")
    context = step1.build_run_context(ROOT_DIR)

    try:
        start_date = parse_date_arg(args.start_date)
        end_date = parse_date_arg(args.end_date)
        mode = resolve_mode(context.config, args.mode)
    except ValueError as exc:
        parser.error(str(exc))

    if end_date < start_date:
        parser.error("end-date cannot be earlier than start-date")

    docs_dir_override = Path(args.docs_dir_override).resolve() if args.docs_dir_override else None
    docs_dir = step7.resolve_docs_dir(context, docs_dir_override)
    ensure_docs_runtime_files(docs_dir)

    categories_override = None
    if args.categories:
        categories_override = [item.strip() for item in str(args.categories).split(",") if item.strip()]

    total_days = (end_date - start_date).days + 1
    step1.log(
        f"[INFO] pipeline_v2 range start={start_date.isoformat()} end={end_date.isoformat()} "
        f"days={total_days} mode={mode} docs_dir={docs_dir}"
    )

    current = start_date
    while current <= end_date:
        run_day_pipeline(
            context,
            current,
            docs_dir,
            force_existing=bool(args.force_existing),
            ignore_seen=bool(args.ignore_seen),
            categories_override=categories_override,
            top_k_override=args.top_k,
            embedding_device=_norm_text(args.embedding_device) or "cpu",
            embedding_batch_size=max(int(args.embedding_batch_size), 1),
            embedding_model_name=_norm_text(args.embedding_model_name) or step2_embedding.DEFAULT_EMBED_MODEL,
            embedding_max_length=args.embedding_max_length,
            rrf_top_k=max(int(args.rrf_top_k), 1),
            rrf_k=max(int(args.rrf_k), 1),
            rerank_top_n=(max(int(args.rerank_top_n), 1) if args.rerank_top_n is not None else None),
            rerank_model=_norm_text(args.rerank_model) or step3.DEFAULT_RERANK_MODEL,
            disable_rerank=bool(args.disable_rerank),
            min_star=max(int(args.min_star), 0),
            filter_batch_size=max(int(args.filter_batch_size), 1),
            filter_max_chars=max(int(args.filter_max_chars), 100),
            filter_model=_norm_text(args.filter_model) or step4.DEFAULT_FILTER_MODEL,
            filter_max_output_tokens=max(int(args.filter_max_output_tokens), 256),
            filter_concurrency=max(int(args.filter_concurrency), 1),
            mode=mode,
            enrichment_model=_norm_text(args.enrichment_model) or step6.DEFAULT_LLM_MODEL,
            enrichment_max_output_tokens=max(int(args.enrichment_max_output_tokens), 256),
            skip_enrichment=bool(args.skip_enrichment),
            docs_dir_override=docs_dir_override,
        )
        current += timedelta(days=1)

    sort_sidebar_by_date(docs_dir / "_sidebar.md")
    write_home_readme(docs_dir)
    step1.log(f"[INFO] home_readme={docs_dir / 'README.md'}")
    step1.log(f"[INFO] sidebar={docs_dir / '_sidebar.md'}")
    step1.log("[INFO] pipeline_v2 range completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
