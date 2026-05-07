#!/usr/bin/env python3
"""Step 2.3 fuse BM25 and embedding retrieval lanes with RRF."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from step1_fetch import RunContext, build_run_context, log, read_json_file, resolve_run_date
from step2_1_bm25 import (
    BM25Artifacts,
    BM25Stats,
    BM25StepOutput,
    QueryResult,
    RetrievalQuery,
    TaggedPaperRecord,
    query_result_to_dict,
)
from step2_2_embedding import EmbeddingArtifacts, EmbeddingStats, EmbeddingStepOutput


@dataclass(frozen=True, slots=True)
class QueryAlignmentKey:
    type: str
    paper_tag: str
    query_text: str


@dataclass(slots=True)
class RRFStats:
    bm25_queries: int = 0
    embedding_queries: int = 0
    fused_queries: int = 0
    fused_papers: int = 0


@dataclass(slots=True)
class RRFArtifacts:
    output_path: Path | None = None


@dataclass(slots=True)
class RRFStepInput:
    run_date: date
    bm25_output: BM25StepOutput
    embedding_output: EmbeddingStepOutput
    top_k: int
    rrf_k: int = 60
    output_path_override: Path | None = None


@dataclass(slots=True)
class RRFStepOutput:
    run_date: date | None = None
    tagged_papers: list[TaggedPaperRecord] = field(default_factory=list)
    query_results: list[QueryResult] = field(default_factory=list)
    artifacts: RRFArtifacts = field(default_factory=RRFArtifacts)
    stats: RRFStats = field(default_factory=RRFStats)
    warnings: list[str] = field(default_factory=list)


STEP2_3_NOTES = [
    "Decide whether Step 2.3 should warn or fail when one lane is completely empty",
    "Add unit tests for query alignment, rank normalization, and paper merge semantics",
]


def resolve_bm25_input_path(context: RunContext, run_date: date, override: Path | None) -> Path:
    if override is not None:
        return override
    token = run_date.strftime("%Y%m%d")
    return context.archive_root / token / "filtered" / f"arxiv_papers_{token}.bm25.json"


def resolve_embedding_input_path(context: RunContext, run_date: date, override: Path | None) -> Path:
    if override is not None:
        return override
    token = run_date.strftime("%Y%m%d")
    return context.archive_root / token / "filtered" / f"arxiv_papers_{token}.embedding.json"


def resolve_output_path(context: RunContext, step_input: RRFStepInput) -> Path:
    if step_input.output_path_override is not None:
        return step_input.output_path_override
    token = step_input.run_date.strftime("%Y%m%d")
    return context.archive_root / token / "filtered" / f"arxiv_papers_{token}.json"


def parse_retrieval_query(raw: dict[str, Any]) -> RetrievalQuery:
    query_payload = raw.get("query") if isinstance(raw.get("query"), dict) else raw
    return RetrievalQuery(
        type=str(query_payload.get("type") or "").strip(),
        tag=str(query_payload.get("tag") or "").strip(),
        paper_tag=str(query_payload.get("paper_tag") or query_payload.get("tag") or "").strip(),
        query_text=str(query_payload.get("query_text") or "").strip(),
        logic_cn=str(query_payload.get("logic_cn") or "").strip(),
    )


def parse_query_result(raw: dict[str, Any]) -> QueryResult:
    query = parse_retrieval_query(raw)
    sim_scores = raw.get("sim_scores") if isinstance(raw.get("sim_scores"), dict) else {}
    normalized_scores: dict[str, dict[str, float | int]] = {}
    for paper_id, meta in sim_scores.items():
        if isinstance(meta, dict):
            score = meta.get("score", 0.0)
            rank = meta.get("rank", 0)
        else:
            score = 0.0
            rank = 0
        try:
            score_value = float(score)
        except Exception:
            score_value = 0.0
        try:
            rank_value = int(rank)
        except Exception:
            rank_value = 0
        normalized_scores[str(paper_id)] = {"score": score_value, "rank": rank_value}
    return QueryResult(query=query, sim_scores=normalized_scores)


def parse_tagged_paper(raw: dict[str, Any]) -> TaggedPaperRecord | None:
    paper_id = str(raw.get("id") or "").strip()
    if not paper_id:
        return None
    authors = raw.get("authors") if isinstance(raw.get("authors"), list) else []
    categories = raw.get("categories") if isinstance(raw.get("categories"), list) else []
    tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
    return TaggedPaperRecord(
        id=paper_id,
        source=str(raw.get("source") or "arxiv").strip() or "arxiv",
        title=str(raw.get("title") or "").strip(),
        abstract=str(raw.get("abstract") or "").strip(),
        authors=[str(item or "").strip() for item in authors if str(item or "").strip()],
        primary_category=str(raw.get("primary_category") or "").strip() or None,
        categories=[str(item or "").strip() for item in categories if str(item or "").strip()],
        published=str(raw.get("published") or "").strip() or None,
        link=str(raw.get("link") or "").strip() or None,
        updated_at=str(raw.get("updated_at") or "").strip() or None,
        version=str(raw.get("version") or "").strip() or None,
        tags=[str(item or "").strip() for item in tags if str(item or "").strip()],
    )


def load_lane_query_results(payload: dict[str, Any]) -> list[QueryResult]:
    raw_items = payload.get("query_results") or payload.get("queries") or []
    results: list[QueryResult] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        results.append(parse_query_result(item))
    return results


def load_lane_tagged_papers(payload: dict[str, Any]) -> list[TaggedPaperRecord]:
    raw_items = payload.get("tagged_papers") or payload.get("papers") or []
    tagged_papers: list[TaggedPaperRecord] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        parsed = parse_tagged_paper(item)
        if parsed is not None:
            tagged_papers.append(parsed)
    return tagged_papers


def load_bm25_output(path: Path) -> BM25StepOutput:
    payload = read_json_file(path)
    run_date_raw = str(payload.get("run_date") or "").strip()
    run_date = resolve_run_date(run_date_raw) if run_date_raw else None
    return BM25StepOutput(
        run_date=run_date,
        tagged_papers=load_lane_tagged_papers(payload),
        query_results=load_lane_query_results(payload),
        artifacts=BM25Artifacts(output_path=path),
        stats=BM25Stats(**(payload.get("stats") or {})) if isinstance(payload.get("stats"), dict) else BM25Stats(),
        warnings=[str(item) for item in (payload.get("warnings") or []) if str(item).strip()],
    )


def load_embedding_output(path: Path) -> EmbeddingStepOutput:
    payload = read_json_file(path)
    run_date_raw = str(payload.get("run_date") or "").strip()
    run_date = resolve_run_date(run_date_raw) if run_date_raw else None
    stats_payload = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    return EmbeddingStepOutput(
        run_date=run_date,
        tagged_papers=load_lane_tagged_papers(payload),
        query_results=load_lane_query_results(payload),
        artifacts=EmbeddingArtifacts(output_path=path),
        stats=EmbeddingStats(**stats_payload) if isinstance(stats_payload, dict) else EmbeddingStats(),
        warnings=[str(item) for item in (payload.get("warnings") or []) if str(item).strip()],
    )


def build_query_alignment_key(query: RetrievalQuery) -> QueryAlignmentKey:
    return QueryAlignmentKey(
        type=str(query.type or "").strip(),
        paper_tag=str(query.paper_tag or "").strip(),
        query_text=str(query.query_text or "").strip(),
    )


def normalize_rank_list(sim_scores: dict[str, dict[str, float | int]]) -> list[tuple[str, int]]:
    items: list[tuple[str, float, int | None]] = []
    for paper_id, meta in sim_scores.items():
        if not isinstance(meta, dict):
            continue
        try:
            score = float(meta.get("score", 0.0))
        except Exception:
            score = 0.0
        try:
            rank = int(meta.get("rank")) if meta.get("rank") is not None else None
        except Exception:
            rank = None
        items.append((str(paper_id), score, rank))

    if all(rank is not None for _, _, rank in items):
        items.sort(key=lambda item: (item[2] if item[2] is not None else 10**9, item[0]))
    else:
        items.sort(key=lambda item: (-item[1], item[0]))

    rank_list: list[tuple[str, int]] = []
    for idx, (paper_id, _score, _rank) in enumerate(items, start=1):
        rank_list.append((paper_id, idx))
    return rank_list


def rrf_fuse_rank_lists(
    bm25_ranks: list[tuple[str, int]],
    embedding_ranks: list[tuple[str, int]],
    rrf_k: int,
) -> dict[str, float]:
    score_map: dict[str, float] = {}
    for paper_id, rank in bm25_ranks:
        score_map[paper_id] = score_map.get(paper_id, 0.0) + 1.0 / (rrf_k + rank)
    for paper_id, rank in embedding_ranks:
        score_map[paper_id] = score_map.get(paper_id, 0.0) + 1.0 / (rrf_k + rank)
    return score_map


def merge_tagged_paper_pools(
    base_papers: list[TaggedPaperRecord],
    incoming_papers: list[TaggedPaperRecord],
) -> list[TaggedPaperRecord]:
    merged: dict[str, TaggedPaperRecord] = {}

    def merge_one(paper: TaggedPaperRecord) -> None:
        existing = merged.get(paper.id)
        if existing is None:
            merged[paper.id] = TaggedPaperRecord(**asdict(paper))
            return
        existing.tags = sorted({*existing.tags, *paper.tags})
        if not existing.source and paper.source:
            existing.source = paper.source
        if not existing.title and paper.title:
            existing.title = paper.title
        if not existing.abstract and paper.abstract:
            existing.abstract = paper.abstract
        if not existing.authors and paper.authors:
            existing.authors = list(paper.authors)
        if not existing.primary_category and paper.primary_category:
            existing.primary_category = paper.primary_category
        if not existing.categories and paper.categories:
            existing.categories = list(paper.categories)
        if not existing.published and paper.published:
            existing.published = paper.published
        if not existing.link and paper.link:
            existing.link = paper.link
        if not existing.updated_at and paper.updated_at:
            existing.updated_at = paper.updated_at
        if not existing.version and paper.version:
            existing.version = paper.version

    for paper in base_papers:
        merge_one(paper)
    for paper in incoming_papers:
        merge_one(paper)

    return list(merged.values())


def fuse_query_results(
    bm25_results: list[QueryResult],
    embedding_results: list[QueryResult],
    *,
    top_k: int,
    rrf_k: int,
) -> list[QueryResult]:
    bm25_map = {build_query_alignment_key(item.query): item for item in bm25_results}
    embedding_map = {build_query_alignment_key(item.query): item for item in embedding_results}
    all_keys = sorted(set(bm25_map) | set(embedding_map), key=lambda key: (key.type, key.paper_tag, key.query_text))

    fused_results: list[QueryResult] = []
    for key in all_keys:
        bm25_result = bm25_map.get(key)
        embedding_result = embedding_map.get(key)
        query = (bm25_result or embedding_result).query
        bm25_ranks = normalize_rank_list(bm25_result.sim_scores if bm25_result else {})
        embedding_ranks = normalize_rank_list(embedding_result.sim_scores if embedding_result else {})
        score_map = rrf_fuse_rank_lists(bm25_ranks, embedding_ranks, rrf_k)
        ranked_items = sorted(score_map.items(), key=lambda item: (-item[1], item[0]))
        if top_k > 0:
            ranked_items = ranked_items[:top_k]
        sim_scores = {
            paper_id: {"score": float(score), "rank": rank_idx}
            for rank_idx, (paper_id, score) in enumerate(ranked_items, start=1)
        }
        fused_results.append(QueryResult(query=query, sim_scores=sim_scores))
    return fused_results


def write_rrf_output(path: Path, output: RRFStepOutput) -> None:
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


def run_rrf_step(context: RunContext, step_input: RRFStepInput) -> RRFStepOutput:
    output_path = resolve_output_path(context, step_input)
    warnings = list(step_input.bm25_output.warnings) + list(step_input.embedding_output.warnings)
    if not step_input.bm25_output.query_results:
        warnings.append("BM25 lane produced no query results")
    if not step_input.embedding_output.query_results:
        warnings.append("Embedding lane produced no query results")

    log(
        f"RRF run_date={step_input.run_date.isoformat()} "
        f"bm25_queries={len(step_input.bm25_output.query_results)} "
        f"embedding_queries={len(step_input.embedding_output.query_results)} "
        f"top_k={step_input.top_k} rrf_k={step_input.rrf_k}"
    )

    fused_query_results = fuse_query_results(
        step_input.bm25_output.query_results,
        step_input.embedding_output.query_results,
        top_k=max(int(step_input.top_k or 0), 0),
        rrf_k=max(int(step_input.rrf_k or 60), 1),
    )
    fused_tagged_papers = merge_tagged_paper_pools(
        step_input.bm25_output.tagged_papers,
        step_input.embedding_output.tagged_papers,
    )

    output = RRFStepOutput(
        run_date=step_input.run_date,
        tagged_papers=fused_tagged_papers,
        query_results=fused_query_results,
        artifacts=RRFArtifacts(output_path=output_path),
        stats=RRFStats(
            bm25_queries=len(step_input.bm25_output.query_results),
            embedding_queries=len(step_input.embedding_output.query_results),
            fused_queries=len(fused_query_results),
            fused_papers=len(fused_tagged_papers),
        ),
        warnings=warnings,
    )
    write_rrf_output(output_path, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step 2.3 fuse BM25 and embedding lanes with RRF")
    parser.add_argument("--run-date", required=False, help="Run date in YYYY-MM-DD or YYYYMMDD")
    parser.add_argument(
        "--bm25-input-path-override",
        default=None,
        help="Optional BM25 lane input path override",
    )
    parser.add_argument(
        "--embedding-input-path-override",
        default=None,
        help="Optional embedding lane input path override",
    )
    parser.add_argument(
        "--output-path-override",
        default=None,
        help="Optional fused output path override",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=200,
        help="Per-query fused top k",
    )
    parser.add_argument(
        "--rrf-k",
        type=int,
        default=60,
        help="RRF denominator offset",
    )
    parser.add_argument(
        "--print-todos",
        action="store_true",
        help="Print Step 2.3 follow-up notes and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.print_todos:
        for idx, item in enumerate(STEP2_3_NOTES, start=1):
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

    bm25_input_path = resolve_bm25_input_path(
        context,
        run_date,
        Path(args.bm25_input_path_override).resolve() if args.bm25_input_path_override else None,
    )
    embedding_input_path = resolve_embedding_input_path(
        context,
        run_date,
        Path(args.embedding_input_path_override).resolve() if args.embedding_input_path_override else None,
    )
    bm25_output = load_bm25_output(bm25_input_path)
    embedding_output = load_embedding_output(embedding_input_path)
    step_input = RRFStepInput(
        run_date=run_date,
        bm25_output=bm25_output,
        embedding_output=embedding_output,
        top_k=max(int(args.top_k or 0), 1),
        rrf_k=max(int(args.rrf_k or 60), 1),
        output_path_override=Path(args.output_path_override).resolve() if args.output_path_override else None,
    )

    log("Step 2.3 v2 starting")
    log(f"run_date={run_date.isoformat()}")
    log(f"bm25_input_path={bm25_input_path}")
    log(f"embedding_input_path={embedding_input_path}")
    output = run_rrf_step(context, step_input)
    log(f"fused_papers={output.stats.fused_papers}")
    log(f"fused_queries={output.stats.fused_queries}")
    log(f"output_path={output.artifacts.output_path}")
    if output.warnings:
        log(f"warnings={len(output.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
