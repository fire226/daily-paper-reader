#!/usr/bin/env python3
"""Step 2.1 run local BM25 retrieval for a single run date."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from subscription_plan import build_pipeline_inputs

from pipeline_v2.step1_fetch import (  # noqa: E402
    PaperRecord,
    RunContext,
    build_run_context,
    log,
    read_json_file,
    resolve_run_date,
)

TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")


@dataclass(slots=True)
class RetrievalQuery:
    type: str
    tag: str
    paper_tag: str
    query_text: str
    logic_cn: str = ""


@dataclass(slots=True)
class TaggedPaperRecord:
    id: str
    source: str
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    primary_category: str | None = None
    categories: list[str] = field(default_factory=list)
    published: str | None = None
    link: str | None = None
    updated_at: str | None = None
    version: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QueryResult:
    query: RetrievalQuery
    sim_scores: dict[str, dict[str, float | int]] = field(default_factory=dict)


@dataclass(slots=True)
class BM25Stats:
    papers_total: int = 0
    queries_total: int = 0
    tagged_papers: int = 0
    total_hits: int = 0


@dataclass(slots=True)
class BM25Artifacts:
    output_path: Path | None = None


@dataclass(slots=True)
class BM25StepInput:
    run_date: date
    papers: list[PaperRecord]
    queries: list[RetrievalQuery]
    top_k: int
    output_path_override: Path | None = None


@dataclass(slots=True)
class BM25StepOutput:
    run_date: date | None = None
    tagged_papers: list[TaggedPaperRecord] = field(default_factory=list)
    query_results: list[QueryResult] = field(default_factory=list)
    artifacts: BM25Artifacts = field(default_factory=BM25Artifacts)
    stats: BM25Stats = field(default_factory=BM25Stats)
    warnings: list[str] = field(default_factory=list)


STEP2_1_NOTES = [
    "Decide whether top_k should stay explicit or revert to adaptive defaults long-term",
    "Add unit tests for query loading, BM25 ranking, and JSON serialization",
]


class BM25Index:
    """Lightweight BM25 index for local retrieval."""

    def __init__(self, tokenized_docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_len = [len(tokens) for tokens in tokenized_docs]
        self.avgdl = sum(self.doc_len) / max(len(self.doc_len), 1)
        self.inverted: dict[str, list[tuple[int, int]]] = {}
        self.idf: dict[str, float] = {}

        doc_freq: dict[str, int] = {}
        for idx, tokens in enumerate(tokenized_docs):
            freqs: dict[str, int] = {}
            for token in tokens:
                freqs[token] = freqs.get(token, 0) + 1
            for token, tf in freqs.items():
                doc_freq[token] = doc_freq.get(token, 0) + 1
                self.inverted.setdefault(token, []).append((idx, tf))

        total_docs = len(tokenized_docs)
        for token, df in doc_freq.items():
            self.idf[token] = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: Iterable[str]) -> list[float]:
        scores = [0.0] * len(self.doc_len)
        if not self.doc_len:
            return scores

        query_tf: dict[str, int] = {}
        for token in query_tokens:
            query_tf[token] = query_tf.get(token, 0) + 1

        for token, query_count in query_tf.items():
            idf = self.idf.get(token)
            if idf is None:
                continue
            for doc_idx, tf in self.inverted.get(token, []):
                dl = self.doc_len[doc_idx]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / max(self.avgdl, 1e-9))
                scores[doc_idx] += idf * (tf * (self.k1 + 1) / max(denom, 1e-9)) * query_count
        return scores


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    return TOKEN_RE.findall(text.lower())


def paper_text_for_bm25(paper: PaperRecord) -> str:
    title = str(paper.title or "").strip()
    abstract = str(paper.abstract or "").strip()
    if title and abstract:
        return f"Title: {title}\n\nAbstract: {abstract}"
    if title:
        return f"Title: {title}"
    if abstract:
        return f"Abstract: {abstract}"
    return ""


def estimate_dynamic_top_k(total_papers: int) -> int:
    safe_total = max(int(total_papers or 0), 0)
    if safe_total <= 0:
        return 50
    blocks = (safe_total - 1) // 1000
    return 50 * (blocks + 1)


def resolve_input_path(context: RunContext, run_date: date, input_path_override: Path | None) -> Path:
    if input_path_override is not None:
        return input_path_override
    run_date_token = run_date.strftime("%Y%m%d")
    return context.archive_root / run_date_token / "raw" / f"arxiv_papers_{run_date_token}.json"


def resolve_output_path(context: RunContext, step_input: BM25StepInput) -> Path:
    if step_input.output_path_override is not None:
        return step_input.output_path_override
    run_date_token = step_input.run_date.strftime("%Y%m%d")
    return context.archive_root / run_date_token / "filtered" / f"arxiv_papers_{run_date_token}.bm25.json"


def load_papers_from_json(path: Path) -> list[PaperRecord]:
    raw = read_json_file(path)
    if isinstance(raw, dict):
        items = raw.get("papers") or raw.get("tagged_papers") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    papers: list[PaperRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        paper_id = str(item.get("id") or "").strip()
        if not paper_id:
            continue
        authors = item.get("authors") if isinstance(item.get("authors"), list) else []
        categories = item.get("categories") if isinstance(item.get("categories"), list) else []
        papers.append(
            PaperRecord(
                id=paper_id,
                source=str(item.get("source") or "arxiv").strip() or "arxiv",
                title=str(item.get("title") or "").strip(),
                abstract=str(item.get("abstract") or "").strip(),
                authors=[str(author or "").strip() for author in authors if str(author or "").strip()],
                primary_category=str(item.get("primary_category") or "").strip() or None,
                categories=[str(category or "").strip() for category in categories if str(category or "").strip()],
                published=str(item.get("published") or "").strip() or None,
                link=str(item.get("link") or "").strip() or None,
                updated_at=str(item.get("updated_at") or "").strip() or None,
                version=str(item.get("version") or "").strip() or None,
            )
        )
    return papers


def load_retrieval_queries(config: dict[str, Any]) -> list[RetrievalQuery]:
    pipeline_inputs = build_pipeline_inputs(config or {})
    raw_queries = pipeline_inputs.get("bm25_queries") or []
    queries: list[RetrievalQuery] = []
    for item in raw_queries:
        if not isinstance(item, dict):
            continue
        query_text = str(item.get("query_text") or "").strip()
        if not query_text:
            continue
        queries.append(
            RetrievalQuery(
                type=str(item.get("type") or "").strip(),
                tag=str(item.get("tag") or "").strip(),
                paper_tag=str(item.get("paper_tag") or "").strip(),
                query_text=query_text,
                logic_cn=str(item.get("logic_cn") or "").strip(),
            )
        )
    return queries


def build_bm25_index(papers: list[PaperRecord]) -> BM25Index:
    tokenized_docs = [tokenize(paper_text_for_bm25(paper)) for paper in papers]
    return BM25Index(tokenized_docs=tokenized_docs)


def build_tagged_papers(papers: list[PaperRecord], tags_by_paper_id: dict[str, set[str]]) -> list[TaggedPaperRecord]:
    tagged_papers: list[TaggedPaperRecord] = []
    for paper in papers:
        tags = sorted(tags_by_paper_id.get(paper.id) or [])
        if not tags:
            continue
        tagged_papers.append(
            TaggedPaperRecord(
                id=paper.id,
                source=paper.source,
                title=paper.title,
                abstract=paper.abstract,
                authors=list(paper.authors),
                primary_category=paper.primary_category,
                categories=list(paper.categories),
                published=paper.published,
                link=paper.link,
                updated_at=paper.updated_at,
                version=paper.version,
                tags=tags,
            )
        )
    return tagged_papers


def rank_queries(
    bm25: BM25Index,
    papers: list[PaperRecord],
    queries: list[RetrievalQuery],
    top_k: int,
) -> tuple[list[QueryResult], dict[str, set[str]]]:
    paper_ids = [paper.id for paper in papers]
    tags_by_paper_id: dict[str, set[str]] = {}
    query_results: list[QueryResult] = []

    for query in queries:
        query_text = str(query.query_text or "").strip()
        if not query_text:
            query_results.append(QueryResult(query=query, sim_scores={}))
            continue

        scores = bm25.score(tokenize(query_text))
        ranked_indices = sorted(range(len(scores)), key=lambda idx: scores[idx], reverse=True)
        if top_k > 0:
            ranked_indices = ranked_indices[:top_k]

        sim_scores: dict[str, dict[str, float | int]] = {}
        for rank_idx, idx in enumerate(ranked_indices, start=1):
            paper_id = paper_ids[idx]
            sim_scores[paper_id] = {"score": float(scores[idx]), "rank": rank_idx}
            if query.paper_tag:
                tags_by_paper_id.setdefault(paper_id, set()).add(query.paper_tag)

        query_results.append(QueryResult(query=query, sim_scores=sim_scores))

    return query_results, tags_by_paper_id


def query_result_to_dict(query_result: QueryResult) -> dict[str, Any]:
    query = query_result.query
    return {
        "type": query.type,
        "tag": query.tag,
        "paper_tag": query.paper_tag,
        "query_text": query.query_text,
        "logic_cn": query.logic_cn,
        "sim_scores": query_result.sim_scores,
    }


def write_bm25_output(path: Path, output: BM25StepOutput) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_date": output.run_date.isoformat() if output.run_date else "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tagged_papers": [asdict(paper) for paper in output.tagged_papers],
        "query_results": [query_result_to_dict(item) for item in output.query_results],
        "stats": asdict(output.stats),
        "warnings": list(output.warnings),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_bm25_step(context: RunContext, step_input: BM25StepInput) -> BM25StepOutput:
    output_path = resolve_output_path(context, step_input)
    warnings: list[str] = []

    if not step_input.queries:
        warnings.append("No BM25 queries were loaded from subscription config")

    log(
        f"BM25 run_date={step_input.run_date.isoformat()} "
        f"papers={len(step_input.papers)} queries={len(step_input.queries)} top_k={step_input.top_k}"
    )

    bm25 = build_bm25_index(step_input.papers)
    query_results, tags_by_paper_id = rank_queries(
        bm25=bm25,
        papers=step_input.papers,
        queries=step_input.queries,
        top_k=max(int(step_input.top_k or 0), 0),
    )
    tagged_papers = build_tagged_papers(step_input.papers, tags_by_paper_id)
    total_hits = sum(len(item.sim_scores) for item in query_results)

    output = BM25StepOutput(
        run_date=step_input.run_date,
        tagged_papers=tagged_papers,
        query_results=query_results,
        artifacts=BM25Artifacts(output_path=output_path),
        stats=BM25Stats(
            papers_total=len(step_input.papers),
            queries_total=len(step_input.queries),
            tagged_papers=len(tagged_papers),
            total_hits=total_hits,
        ),
        warnings=warnings,
    )
    write_bm25_output(output_path, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step 2.1 run local BM25 for a single run date")
    parser.add_argument("--run-date", required=False, help="Run date in YYYY-MM-DD or YYYYMMDD")
    parser.add_argument(
        "--input-path-override",
        default=None,
        help="Optional Step 1 raw input path override",
    )
    parser.add_argument(
        "--output-path-override",
        default=None,
        help="Optional BM25 output path override",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Per-query top k. Defaults to adaptive value based on paper count.",
    )
    parser.add_argument(
        "--print-todos",
        action="store_true",
        help="Print Step 2.1 follow-up notes and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.print_todos:
        for idx, item in enumerate(STEP2_1_NOTES, start=1):
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

    input_path_override = Path(args.input_path_override).resolve() if args.input_path_override else None
    input_path = resolve_input_path(context, run_date, input_path_override)
    papers = load_papers_from_json(input_path)
    queries = load_retrieval_queries(context.config)
    top_k = int(args.top_k) if args.top_k is not None else estimate_dynamic_top_k(len(papers))
    step_input = BM25StepInput(
        run_date=run_date,
        papers=papers,
        queries=queries,
        top_k=max(top_k, 1),
        output_path_override=Path(args.output_path_override).resolve() if args.output_path_override else None,
    )

    log("Step 2.1 v2 starting")
    log(f"run_date={run_date.isoformat()}")
    log(f"input_path={input_path}")
    output = run_bm25_step(context, step_input)
    log(f"tagged_papers={output.stats.tagged_papers}")
    log(f"query_results={len(output.query_results)}")
    log(f"output_path={output.artifacts.output_path}")
    if output.warnings:
        log(f"warnings={len(output.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
