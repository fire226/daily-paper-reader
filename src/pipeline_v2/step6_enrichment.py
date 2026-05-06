#!/usr/bin/env python3
"""Step 6 LLM enrichment for selected papers."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from llm import OpenRouterClient

from pipeline_v2.step1_fetch import RunContext, build_run_context, log, read_json_file, resolve_run_date
from pipeline_v2.step3_rerank import RerankStepOutput, load_rerank_output
from pipeline_v2.step4_llm_refine import LLMRefineStepOutput, load_llm_refine_output
from pipeline_v2.step5_select import SelectStepOutput, load_select_output

DEFAULT_LLM_MODEL = os.getenv("FILTER_MODEL") or os.getenv("LLM_MODEL") or "deepseek/deepseek-v3.2"


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
    llm_calls: int = 0


@dataclass(slots=True)
class EnrichmentArtifacts:
    output_path: Path | None = None


@dataclass(slots=True)
class EnrichmentStepInput:
    run_date: date
    select_output: SelectStepOutput
    rerank_output: RerankStepOutput
    llm_model: str = ""
    max_output_tokens: int = 4000
    output_path_override: Path | None = None


@dataclass(slots=True)
class EnrichmentStepOutput:
    run_date: date | None = None
    enriched_papers: list[EnrichedPaper] = field(default_factory=list)
    artifacts: EnrichmentArtifacts = field(default_factory=EnrichmentArtifacts)
    stats: EnrichmentStats = field(default_factory=EnrichmentStats)
    warnings: list[str] = field(default_factory=list)


ENRICHMENT_NOTES = [
    "Add unit tests for batch translate and batch glance",
    "Decide whether deep summary should be integrated into this step or stay separate",
]


def _norm_text(value: Any) -> str:
    return str(value or "").strip()


def resolve_output_path(context: RunContext, step_input: EnrichmentStepInput) -> Path:
    if step_input.output_path_override is not None:
        return step_input.output_path_override
    token = step_input.run_date.strftime("%Y%m%d")
    return context.archive_root / token / "enriched" / f"arxiv_papers_{token}.enriched.json"


def _make_llm_client(api_key: str, model: str, max_output_tokens: int) -> OpenRouterClient:
    client = OpenRouterClient(api_key=api_key, model=model)
    client.kwargs.update({"temperature": 0.2, "max_tokens": max_output_tokens})
    return client


def _call_llm_structured(
    client: OpenRouterClient,
    messages: list[dict[str, str]],
    schema_name: str,
    schema: dict[str, Any],
) -> dict[str, Any] | None:
    resp = client.chat_structured(
        messages=messages,
        schema_name=schema_name,
        schema=schema,
        strict=True,
        allow_json_object_fallback=True,
    )
    if resp.get("refusal"):
        return None
    if resp.get("finish_reason") not in (None, "stop"):
        return None
    if resp.get("parse_error") is not None:
        return None
    parsed = resp.get("parsed")
    return parsed if isinstance(parsed, dict) else None


def build_paper_map(papers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    paper_map: dict[str, dict[str, Any]] = {}
    for p in papers:
        pid = _norm_text(p.get("id"))
        if pid:
            paper_map[pid] = p
    return paper_map


def translate_batch(
    client: OpenRouterClient,
    papers: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    if not papers:
        return {}

    items = []
    for p in papers:
        pid = _norm_text(p.get("id") or p.get("paper_id"))
        title = _norm_text(p.get("title"))
        abstract = _norm_text(p.get("abstract"))
        if pid and (title or abstract):
            items.append({"id": pid, "title": title, "abstract": abstract})

    if not items:
        return {}

    schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title_zh": {"type": "string"},
                        "abstract_zh": {"type": "string"},
                    },
                    "required": ["id", "title_zh", "abstract_zh"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }

    system_prompt = (
        "你是一名熟悉机器学习与自然科学论文的专业翻译。"
        "请将每篇论文的 title 和 abstract 翻译为自然、准确的中文。"
        "保持学术风格，尽量保留专有名词。"
    )
    user_prompt = (
        f"请翻译以下 {len(items)} 篇论文的标题和摘要：\n\n"
        f"{json.dumps(items, ensure_ascii=False)}\n\n"
        "输出 JSON 格式：\n"
        '{"results": [{"id": "paper_id", "title_zh": "...", "abstract_zh": "..."}]}\n'
        "每篇论文必须有一条结果。输出严格 JSON，不要 markdown。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    parsed = _call_llm_structured(client, messages, "translate_batch", schema)
    if not parsed:
        return {}

    results = parsed.get("results") or []
    out: dict[str, dict[str, str]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        pid = _norm_text(item.get("id"))
        if pid:
            out[pid] = {
                "title_zh": _norm_text(item.get("title_zh")),
                "abstract_zh": _norm_text(item.get("abstract_zh")),
            }
    return out


def glance_batch(
    client: OpenRouterClient,
    papers: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    if not papers:
        return {}

    items = []
    for p in papers:
        pid = _norm_text(p.get("id") or p.get("paper_id"))
        title = _norm_text(p.get("title"))
        abstract = _norm_text(p.get("abstract"))
        if pid and (title or abstract):
            items.append({"id": pid, "title": title, "abstract": abstract})

    if not items:
        return {}

    schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "motivation": {"type": "string"},
                        "method": {"type": "string"},
                        "result": {"type": "string"},
                        "conclusion": {"type": "string"},
                    },
                    "required": ["id", "motivation", "method", "result", "conclusion"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }

    system_prompt = (
        "你是一名学术论文速览专家。请为每篇论文生成四行速览：\n"
        "- Motivation：为什么要做这个研究（一句话）\n"
        "- Method：用了什么方法（一句话）\n"
        "- Result：主要结果是什么（一句话）\n"
        "- Conclusion：结论或意义（一句话）\n"
        "每行控制在 100 字以内。"
    )
    user_prompt = (
        f"请为以下 {len(items)} 篇论文生成速览：\n\n"
        f"{json.dumps(items, ensure_ascii=False)}\n\n"
        "输出 JSON 格式：\n"
        '{"results": [{"id": "paper_id", "motivation": "...", "method": "...", "result": "...", "conclusion": "..."}]}\n'
        "每篇论文必须有一条结果。输出严格 JSON，不要 markdown。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    parsed = _call_llm_structured(client, messages, "glance_batch", schema)
    if not parsed:
        return {}

    results = parsed.get("results") or []
    out: dict[str, dict[str, str]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        pid = _norm_text(item.get("id"))
        if pid:
            out[pid] = {
                "motivation": _norm_text(item.get("motivation")),
                "method": _norm_text(item.get("method")),
                "result": _norm_text(item.get("result")),
                "conclusion": _norm_text(item.get("conclusion")),
            }
    return out


def fetch_paper_text(pdf_url: str, txt_path: Path | None = None) -> str:
    """Fetch paper full text via Jina reader, or from existing txt file."""
    if txt_path and txt_path.exists():
        return txt_path.read_text(encoding="utf-8")

    if not pdf_url:
        return ""

    try:
        jina_url = f"https://r.jina.ai/{pdf_url}"
        resp = requests.get(jina_url, timeout=120)
        if resp.status_code == 200:
            text = resp.text.strip()
            if text:
                if txt_path:
                    txt_path.parent.mkdir(parents=True, exist_ok=True)
                    txt_path.write_text(text, encoding="utf-8")
                return text
    except Exception as e:
        log(f"[WARN] Jina fetch failed for {pdf_url}: {e}")

    return ""


def _strip_thinking_tags(text: str) -> str:
    """Remove <thought>...</thought> and <reasoning>...</reasoning> blocks from LLM output."""
    text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL)
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL)
    return text.strip()


def _strip_llm_preamble(text: str) -> str:
    """Remove common LLM preamble like '好的，作为...' before the actual content."""
    # Strip initial preamble
    m = re.match(r"^好的[，,].*?(?=#|\n\n)", text, re.DOTALL)
    if m:
        text = text[m.end():].strip()
    # Strip continuation preamble like "好的，我们继续从..."
    text = re.sub(r"(?m)^好的[，,].*?(?=\n)", "", text)
    # Strip "---" separator lines that appear in continuations
    text = re.sub(r"\n---\n(?=\s+\d)", "\n", text)
    return text.strip()


def generate_deep_summaries(
    client: OpenRouterClient,
    deep_papers: list[dict[str, Any]],
    paper_text_map: dict[str, str],
    max_retries: int = 3,
) -> dict[str, str]:
    """Generate detailed Chinese summaries for deep-dive papers."""
    if not deep_papers:
        return {}

    system_prompt = (
        "你是一名资深学术论文分析助手，请使用中文、以 Markdown 形式，"
        "对给定论文做结构化、深入、客观的总结。"
    )
    user_prompt = (
        "请基于下面提供的论文内容，生成一段详细的中文总结，要求按照如下要点依次展开：\n"
        "1. 论文的核心问题与整体含义（研究动机和背景）。\n"
        "2. 论文提出的方法论：核心思想、关键技术细节、公式或算法流程（用文字说明即可）。\n"
        "3. 实验设计：使用了哪些数据集 / 场景，它的 benchmark 是什么，对比了哪些方法。\n"
        "4. 资源与算力：如果文中有提到，请总结使用了多少算力（GPU 型号、数量、训练时长等）。若未明确说明，也请指出这一点。\n"
        "5. 实验数量与充分性：大概做了多少组实验（如不同数据集、消融实验等），这些实验是否充分、是否客观、公平。\n"
        "6. 论文的主要结论与发现。\n"
        "7. 优点：方法或实验设计上有哪些亮点。\n"
        "8. 不足与局限：包括实验覆盖、偏差风险、应用限制等。\n\n"
        "请用分层标题和项目符号（Markdown 格式）组织上述内容，语言尽量简洁但信息要尽量完整。\n"
        "要求：最后单独输出一行\u201c（完）\u201d作为结束标记。"
    )

    results: dict[str, str] = {}

    for paper in deep_papers:
        pid = _norm_text(paper.get("id") or paper.get("paper_id"))
        title = _norm_text(paper.get("title"))
        abstract = _norm_text(paper.get("abstract"))
        paper_text = paper_text_map.get(pid, "")

        if not pid or not (title or abstract):
            continue

        md_metadata = f"### Title\n{title}\n\n### Abstract\n{abstract}"

        messages = [{"role": "system", "content": system_prompt}]
        if paper_text:
            messages.append({"role": "user", "content": f"### 论文 PDF 提取文本 ###\n{paper_text}"})
        messages.append({"role": "user", "content": f"### 论文 Markdown 元数据 ###\n{md_metadata}"})
        messages.append({"role": "user", "content": user_prompt})

        summary = ""
        for attempt in range(1, max_retries + 1):
            try:
                client.kwargs.update({"temperature": 0.3, "max_tokens": 4096})
                resp = client.chat(messages=messages)
                summary = _strip_thinking_tags((resp.get("content") or "").strip())
                if not summary:
                    continue
                if "（完）" in summary:
                    break
                cont_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "你上一次的总结可能被截断了，请从中断处继续补全，不要重复已输出内容。"},
                    {"role": "user", "content": f"上一次输出如下：\n\n{summary}\n\n请继续补全，最后以一行\u201c\uff08完\uff09\u201d结束。"},
                ]
                client.kwargs.update({"temperature": 0.3, "max_tokens": 2048})
                cont_resp = client.chat(messages=cont_messages)
                cont = _strip_thinking_tags((cont_resp.get("content") or "").strip())
                merged = f"{summary}\n\n{cont}".strip()
                if "（完）" in merged:
                    summary = merged
                    break
            except Exception as e:
                log(f"[WARN] Deep summary failed for {pid} (attempt {attempt}): {e}")
                time.sleep(2 * attempt)

        if summary:
            summary = _strip_llm_preamble(summary)
            summary = summary.replace("（完）", "").strip()
            # Remove duplicated section headers from continuation
            summary = re.sub(r"(?m)^(#{1,4}\s+.+?)\n\1", r"\1", summary)
            results[pid] = summary

    return results


def write_enrichment_output(path: Path, output: EnrichmentStepOutput) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_date": output.run_date.isoformat() if output.run_date else "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enriched_papers": [asdict(item) for item in output.enriched_papers],
        "stats": asdict(output.stats),
        "warnings": list(output.warnings),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_enriched_paper(raw: dict[str, Any]) -> EnrichedPaper:
    return EnrichedPaper(
        paper_id=_norm_text(raw.get("paper_id")),
        title_zh=_norm_text(raw.get("title_zh")),
        abstract_zh=_norm_text(raw.get("abstract_zh")),
        glance_motivation=_norm_text(raw.get("glance_motivation")),
        glance_method=_norm_text(raw.get("glance_method")),
        glance_result=_norm_text(raw.get("glance_result")),
        glance_conclusion=_norm_text(raw.get("glance_conclusion")),
        deep_summary=_norm_text(raw.get("deep_summary")),
    )


def load_enrichment_output(path: Path) -> EnrichmentStepOutput:
    payload = read_json_file(path)
    run_date_raw = str(payload.get("run_date") or "").strip()
    run_date = resolve_run_date(run_date_raw) if run_date_raw else None
    stats_payload = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    return EnrichmentStepOutput(
        run_date=run_date,
        enriched_papers=[_parse_enriched_paper(item) for item in (payload.get("enriched_papers") or []) if isinstance(item, dict)],
        artifacts=EnrichmentArtifacts(output_path=path),
        stats=EnrichmentStats(**stats_payload) if isinstance(stats_payload, dict) else EnrichmentStats(),
        warnings=[str(item) for item in (payload.get("warnings") or []) if str(item).strip()],
    )


def run_enrichment_step(context: RunContext, step_input: EnrichmentStepInput) -> EnrichmentStepOutput:
    output_path = resolve_output_path(context, step_input)
    warnings: list[str] = []
    llm_model = _norm_text(step_input.llm_model) or DEFAULT_LLM_MODEL

    all_papers = list(step_input.select_output.deep_dive) + list(step_input.select_output.quick_skim)
    if not all_papers:
        warnings.append("No papers to enrich")
        log("[WARN] No papers to enrich, skipping enrichment.")
        return EnrichmentStepOutput(
            run_date=step_input.run_date,
            artifacts=EnrichmentArtifacts(output_path=output_path),
            stats=EnrichmentStats(papers_total=0),
            warnings=warnings,
        )

    api_key = _norm_text(os.getenv("OPENROUTER_API_KEY")) or _norm_text(os.getenv("LLM_API_KEY"))
    if not api_key:
        warnings.append("Missing OPENROUTER_API_KEY / LLM_API_KEY, skipping LLM enrichment")
        log("[WARN] Missing OPENROUTER_API_KEY / LLM_API_KEY, skipping LLM enrichment.")
        return EnrichmentStepOutput(
            run_date=step_input.run_date,
            artifacts=EnrichmentArtifacts(output_path=output_path),
            stats=EnrichmentStats(papers_total=len(all_papers)),
            warnings=warnings,
        )

    stats = EnrichmentStats(papers_total=len(all_papers))
    client = _make_llm_client(api_key, llm_model, step_input.max_output_tokens)

    log(
        f"Enrichment run_date={step_input.run_date.isoformat()} "
        f"papers={len(all_papers)} deep_dive={len(step_input.select_output.deep_dive)} "
        f"model={llm_model}"
    )

    paper_map = build_paper_map(
        [asdict(paper) for paper in step_input.rerank_output.papers]
    )

    enriched_by_id: dict[str, EnrichedPaper] = {}

    log(f"[INFO] Starting batch translation for {len(all_papers)} papers")
    translations = translate_batch(client, all_papers)
    stats.llm_calls += 1
    for pid, tr in translations.items():
        ep = enriched_by_id.setdefault(pid, EnrichedPaper(paper_id=pid))
        ep.title_zh = tr.get("title_zh", "")
        ep.abstract_zh = tr.get("abstract_zh", "")
    stats.translated = len(translations)
    log(f"[INFO] Translation complete: {stats.translated}/{len(all_papers)} papers")

    log(f"[INFO] Starting batch glance for {len(all_papers)} papers")
    glances = glance_batch(client, all_papers)
    stats.llm_calls += 1
    for pid, gl in glances.items():
        ep = enriched_by_id.setdefault(pid, EnrichedPaper(paper_id=pid))
        ep.glance_motivation = gl.get("motivation", "")
        ep.glance_method = gl.get("method", "")
        ep.glance_result = gl.get("result", "")
        ep.glance_conclusion = gl.get("conclusion", "")
    stats.glanced = len(glances)
    log(f"[INFO] Glance complete: {stats.glanced}/{len(all_papers)} papers")

    deep_papers_raw = list(step_input.select_output.deep_dive)
    deep_summaries: dict[str, str] = {}
    if deep_papers_raw:
        log(f"[INFO] Fetching paper text for {len(deep_papers_raw)} deep-dive papers")
        date_token = step_input.run_date.strftime("%Y%m%d")
        paper_text_map: dict[str, str] = {}
        for p in deep_papers_raw:
            pid = _norm_text(p.get("id") or p.get("paper_id"))
            pdf_url = _norm_text(p.get("link") or p.get("pdf_url"))
            if pid and pdf_url:
                txt_path = context.archive_root / date_token / "txt" / f"{pid.replace('/', '-')}.txt"
                text = fetch_paper_text(pdf_url, txt_path)
                if text:
                    paper_text_map[pid] = text
        log(f"[INFO] Paper text fetched: {len(paper_text_map)}/{len(deep_papers_raw)} papers")

        log(f"[INFO] Generating deep summaries for {len(deep_papers_raw)} papers")
        deep_summaries = generate_deep_summaries(client, deep_papers_raw, paper_text_map)
        stats.llm_calls += 1
        stats.deep_summarized = len(deep_summaries)
        log(f"[INFO] Deep summary complete: {stats.deep_summarized}/{len(deep_papers_raw)} papers")

    enriched_papers: list[EnrichedPaper] = []
    for paper in all_papers:
        pid = _norm_text(paper.get("id") or paper.get("paper_id"))
        ep = enriched_by_id.get(pid)
        if ep is None:
            ep = EnrichedPaper(paper_id=pid)
        if pid in deep_summaries:
            ep.deep_summary = deep_summaries[pid]
        enriched_papers.append(ep)

    output = EnrichmentStepOutput(
        run_date=step_input.run_date,
        enriched_papers=enriched_papers,
        artifacts=EnrichmentArtifacts(output_path=output_path),
        stats=stats,
        warnings=warnings,
    )
    write_enrichment_output(output_path, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step 6 LLM enrichment for selected papers")
    parser.add_argument("--run-date", required=False, help="Run date in YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--mode", default="standard", help="Recommendation mode to enrich")
    parser.add_argument(
        "--select-input-path-override",
        default=None,
        help="Optional Step 5 select output path override",
    )
    parser.add_argument(
        "--rerank-input-path-override",
        default=None,
        help="Optional Step 3 rerank output path override",
    )
    parser.add_argument(
        "--output-path-override",
        default=None,
        help="Optional enrichment output path override",
    )
    parser.add_argument(
        "--llm-model",
        default=os.getenv("FILTER_MODEL") or DEFAULT_LLM_MODEL,
        help="LLM model for enrichment",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=4000,
        help="Max output tokens for LLM",
    )
    parser.add_argument(
        "--print-todos",
        action="store_true",
        help="Print Step 6 follow-up notes and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.print_todos:
        for idx, item in enumerate(ENRICHMENT_NOTES, start=1):
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
    rerank_input_path = (
        Path(args.rerank_input_path_override).resolve()
        if args.rerank_input_path_override
        else context.archive_root / token / "rank" / f"arxiv_papers_{token}.rerank.json"
    )

    select_output = load_select_output(select_input_path)
    rerank_output = load_rerank_output(rerank_input_path)

    step_input = EnrichmentStepInput(
        run_date=run_date,
        select_output=select_output,
        rerank_output=rerank_output,
        llm_model=_norm_text(args.llm_model) or DEFAULT_LLM_MODEL,
        max_output_tokens=max(int(args.max_output_tokens), 256),
        output_path_override=Path(args.output_path_override).resolve() if args.output_path_override else None,
    )

    log("Step 6 v2 starting")
    log(f"run_date={run_date.isoformat()}")
    output = run_enrichment_step(context, step_input)
    log(f"papers_total={output.stats.papers_total}")
    log(f"translated={output.stats.translated}")
    log(f"glanced={output.stats.glanced}")
    log(f"llm_calls={output.stats.llm_calls}")
    log(f"output_path={output.artifacts.output_path}")
    if output.warnings:
        log(f"warnings={len(output.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
