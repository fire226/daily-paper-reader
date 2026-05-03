#!/usr/bin/env python3
"""Step 2.2 run local embedding retrieval for a single run date."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from filter import encode_queries
from model_loader import is_remote_embedding_enabled, load_sentence_transformer

from pipeline_v2.step1_fetch import (  # noqa: E402
    PaperRecord,
    RunContext,
    build_run_context,
    log,
    resolve_run_date,
)
from pipeline_v2.step2_1_bm25 import (  # noqa: E402
    QueryResult,
    RetrievalQuery,
    TaggedPaperRecord,
    build_tagged_papers,
    estimate_dynamic_top_k,
    load_papers_from_json,
    load_retrieval_queries,
    query_result_to_dict,
    resolve_input_path,
)

DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"


@dataclass(slots=True)
class EmbeddingPaperView:
    paper: PaperRecord

    @property
    def text_for_embedding(self) -> str:
        title = str(self.paper.title or "").strip()
        abstract = str(self.paper.abstract or "").strip()
        if title and abstract:
            return f"passage: Title: {title}\n\nAbstract: {abstract}"
        if title:
            return f"passage: Title: {title}"
        if abstract:
            return f"passage: Abstract: {abstract}"
        return ""


@dataclass(slots=True)
class EmbeddingStats:
    papers_total: int = 0
    queries_total: int = 0
    tagged_papers: int = 0
    total_hits: int = 0
    embedding_backend: str = "local"


@dataclass(slots=True)
class EmbeddingArtifacts:
    output_path: Path | None = None


@dataclass(slots=True)
class EmbeddingStepInput:
    run_date: date
    papers: list[PaperRecord]
    queries: list[RetrievalQuery]
    top_k: int
    output_path_override: Path | None = None
    model_name: str | None = None
    device: str | None = None
    batch_size: int = 8
    max_length: int | None = None


@dataclass(slots=True)
class EmbeddingStepOutput:
    run_date: date | None = None
    tagged_papers: list[TaggedPaperRecord] = field(default_factory=list)
    query_results: list[QueryResult] = field(default_factory=list)
    artifacts: EmbeddingArtifacts = field(default_factory=EmbeddingArtifacts)
    stats: EmbeddingStats = field(default_factory=EmbeddingStats)
    warnings: list[str] = field(default_factory=list)


STEP2_2_NOTES = [
    "Decide whether to hydrate query embeddings from config cache in v2 or keep runtime-only encoding",
    "Decide whether device/model should stay CLI-visible or move entirely into caller/context defaults",
    "Add unit tests for query loading, embedding ranking, and JSON serialization",
]


def resolve_output_path(context: RunContext, step_input: EmbeddingStepInput) -> Path:
    if step_input.output_path_override is not None:
        return step_input.output_path_override
    run_date_token = step_input.run_date.strftime("%Y%m%d")
    return context.archive_root / run_date_token / "filtered" / f"arxiv_papers_{run_date_token}.embedding.json"
def resolve_embedding_backend(model: Any) -> str:
    if getattr(model, "is_remote", False) or is_remote_embedding_enabled():
        return "remote_or_local_fallback"
    return "local"


def load_embedding_model(model_name: str, device: str) -> Any:
    return load_sentence_transformer(model_name, device=device, allow_remote=True, log=log)


def set_max_seq_length(model: Any, max_length: int | None) -> None:
    if max_length is None or max_length <= 0:
        return
    if hasattr(model, "max_seq_length"):
        try:
            model.max_seq_length = max_length
            return
        except Exception:
            pass
    if hasattr(model, "_first_module"):
        try:
            first = model._first_module()
            if hasattr(first, "max_seq_length"):
                first.max_seq_length = max_length
        except Exception:
            pass


def compute_paper_embeddings(
    model: Any,
    papers: list[PaperRecord],
    *,
    batch_size: int,
    max_length: int | None,
) -> np.ndarray:
    texts = [EmbeddingPaperView(paper=paper).text_for_embedding for paper in papers]
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    set_max_seq_length(model, max_length)
    log(f"[INFO] 正在为 {len(texts)} 条记录计算向量表示...")
    return model.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
        batch_size=batch_size,
        show_progress_bar=False,
    )


def rank_queries(
    model: Any,
    papers: list[PaperRecord],
    queries: list[RetrievalQuery],
    *,
    top_k: int,
    batch_size: int,
    max_length: int | None,
) -> tuple[list[QueryResult], dict[str, set[str]]]:
    paper_ids = [paper.id for paper in papers]
    tags_by_paper_id: dict[str, set[str]] = {}
    empty_results: dict[int, QueryResult] = {}

    if not papers:
        return [QueryResult(query=query, sim_scores={}) for query in queries], tags_by_paper_id

    paper_embeddings = compute_paper_embeddings(
        model,
        papers,
        batch_size=batch_size,
        max_length=max_length,
    )
    if paper_embeddings.size == 0:
        return [QueryResult(query=query, sim_scores={}) for query in queries], tags_by_paper_id

    query_texts = [str(query.query_text or "").strip() for query in queries]
    valid_queries: list[tuple[int, RetrievalQuery]] = []
    valid_texts: list[str] = []
    for idx, (query, text) in enumerate(zip(queries, query_texts, strict=False)):
        if not text:
            empty_results[idx] = QueryResult(query=query, sim_scores={})
            continue
        valid_queries.append((idx, query))
        valid_texts.append(text)

    indexed_results: dict[int, QueryResult] = {}
    if valid_texts:
        query_embeddings = encode_queries(
            model,
            valid_texts,
            batch_size=batch_size,
            max_length=max_length,
        )
        for emb_idx, (original_idx, query) in enumerate(valid_queries):
            sims = np.dot(paper_embeddings, query_embeddings[emb_idx])
            ranked_indices = np.argsort(-sims)
            if top_k > 0:
                ranked_indices = ranked_indices[:top_k]
            sim_scores: dict[str, dict[str, float | int]] = {}
            for rank_idx, paper_idx in enumerate(ranked_indices.tolist(), start=1):
                paper_id = paper_ids[paper_idx]
                sim_scores[paper_id] = {
                    "score": float(sims[paper_idx]),
                    "rank": rank_idx,
                }
                if query.paper_tag:
                    tags_by_paper_id.setdefault(paper_id, set()).add(query.paper_tag)
            indexed_results[original_idx] = QueryResult(query=query, sim_scores=sim_scores)

    ordered_results: list[QueryResult] = []
    for idx, query in enumerate(queries):
        if idx in indexed_results:
            ordered_results.append(indexed_results[idx])
        elif idx in empty_results:
            ordered_results.append(empty_results[idx])
        else:
            ordered_results.append(QueryResult(query=query, sim_scores={}))

    return ordered_results, tags_by_paper_id


def write_embedding_output(path: Path, output: EmbeddingStepOutput) -> None:
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


def run_embedding_step(context: RunContext, step_input: EmbeddingStepInput) -> EmbeddingStepOutput:
    output_path = resolve_output_path(context, step_input)
    warnings: list[str] = []
    if not step_input.queries:
        warnings.append("No embedding queries were loaded from subscription config")

    model_name = str(step_input.model_name or DEFAULT_EMBED_MODEL).strip() or DEFAULT_EMBED_MODEL
    device = str(step_input.device or "cpu").strip() or "cpu"
    batch_size = max(int(step_input.batch_size or 8), 1)

    log(
        f"Embedding run_date={step_input.run_date.isoformat()} papers={len(step_input.papers)} "
        f"queries={len(step_input.queries)} top_k={step_input.top_k} model={model_name} device={device}"
    )

    model = load_embedding_model(model_name, device)
    query_results, tags_by_paper_id = rank_queries(
        model,
        step_input.papers,
        step_input.queries,
        top_k=max(int(step_input.top_k or 0), 0),
        batch_size=batch_size,
        max_length=step_input.max_length,
    )
    tagged_papers = build_tagged_papers(step_input.papers, tags_by_paper_id)
    total_hits = sum(len(item.sim_scores) for item in query_results)

    output = EmbeddingStepOutput(
        run_date=step_input.run_date,
        tagged_papers=tagged_papers,
        query_results=query_results,
        artifacts=EmbeddingArtifacts(output_path=output_path),
        stats=EmbeddingStats(
            papers_total=len(step_input.papers),
            queries_total=len(step_input.queries),
            tagged_papers=len(tagged_papers),
            total_hits=total_hits,
            embedding_backend=resolve_embedding_backend(model),
        ),
        warnings=warnings,
    )
    write_embedding_output(output_path, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step 2.2 run embedding retrieval for a single run date")
    parser.add_argument("--run-date", required=False, help="Run date in YYYY-MM-DD or YYYYMMDD")
    parser.add_argument(
        "--input-path-override",
        default=None,
        help="Optional Step 1 raw input path override",
    )
    parser.add_argument(
        "--output-path-override",
        default=None,
        help="Optional embedding output path override",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Per-query top k. Defaults to adaptive value based on paper count.",
    )
    parser.add_argument(
        "--model-name",
        default=DEFAULT_EMBED_MODEL,
        help="Embedding model name override",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Embedding device override, e.g. cpu or cuda",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Embedding encode batch size",
    )
    parser.add_argument(
        "--max-length",
        type=int,
        default=None,
        help="Optional embedding max sequence length",
    )
    parser.add_argument(
        "--print-todos",
        action="store_true",
        help="Print Step 2.2 follow-up notes and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.print_todos:
        for idx, item in enumerate(STEP2_2_NOTES, start=1):
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
    step_input = EmbeddingStepInput(
        run_date=run_date,
        papers=papers,
        queries=queries,
        top_k=max(top_k, 1),
        output_path_override=Path(args.output_path_override).resolve() if args.output_path_override else None,
        model_name=str(args.model_name or DEFAULT_EMBED_MODEL).strip() or DEFAULT_EMBED_MODEL,
        device=str(args.device or "cpu").strip() or "cpu",
        batch_size=max(int(args.batch_size or 8), 1),
        max_length=int(args.max_length) if args.max_length is not None else None,
    )

    log("Step 2.2 v2 starting")
    log(f"run_date={run_date.isoformat()}")
    log(f"input_path={input_path}")
    output = run_embedding_step(context, step_input)
    log(f"tagged_papers={output.stats.tagged_papers}")
    log(f"query_results={len(output.query_results)}")
    log(f"output_path={output.artifacts.output_path}")
    if output.warnings:
        log(f"warnings={len(output.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
