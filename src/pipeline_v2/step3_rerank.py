#!/usr/bin/env python3
"""Step 3 rerank intent queries against a global candidate pool."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pipeline_v2.step1_fetch import RunContext, build_run_context, log, read_json_file, resolve_run_date
from pipeline_v2.step2_1_bm25 import QueryResult, TaggedPaperRecord
from pipeline_v2.step2_3_rrf import RRFArtifacts, RRFStats, RRFStepOutput, load_lane_query_results, load_lane_tagged_papers

MAX_CHARS_PER_DOC = 850
BATCH_SIZE = 100
TOKEN_SAFETY = 29000
GLOBAL_RRF_K = 60
LANE_TOP_K_BASE = 30
LANE_TOP_K_STEP = 10
LANE_TOP_K_MAX = 120
GLOBAL_POOL_GUARANTEED_MIN = 5
GLOBAL_POOL_GUARANTEED_MAX = 20
GLOBAL_POOL_RRF_MIN = 60
GLOBAL_POOL_RRF_MAX = 300
DEFAULT_RERANK_MODEL = "Qwen3-Reranker-8B"
DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"


@dataclass(slots=True)
class RerankQuery:
    type: str
    tag: str
    paper_tag: str
    query_text: str
    logic_cn: str = ""
    ranked: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RerankStats:
    used_rerank: bool = False
    fallback_used: bool = False
    intent_queries_total: int = 0
    global_candidate_count: int = 0
    ranked_queries: int = 0


@dataclass(slots=True)
class RerankArtifacts:
    output_path: Path | None = None


@dataclass(slots=True)
class RerankStepInput:
    run_date: date
    rrf_output: RRFStepOutput
    top_n: int | None = None
    rerank_model: str | None = None
    output_path_override: Path | None = None
    disable_rerank: bool = False


@dataclass(slots=True)
class RerankStepOutput:
    run_date: date | None = None
    papers: list[TaggedPaperRecord] = field(default_factory=list)
    global_candidate_ids: list[str] = field(default_factory=list)
    ranked_queries: list[RerankQuery] = field(default_factory=list)
    artifacts: RerankArtifacts = field(default_factory=RerankArtifacts)
    stats: RerankStats = field(default_factory=RerankStats)
    warnings: list[str] = field(default_factory=list)


STEP3_NOTES = [
    "Add unit tests for global pool construction, fallback ranking, and rerank response parsing",
    "Decide whether Step 3 should expose provider selection beyond the default SiliconFlow path",
]


class SiliconFlowRerankClient:
    def __init__(self, api_key: str, model: str, base_url: str = DEFAULT_SILICONFLOW_BASE_URL, timeout: int = 120):
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip() or DEFAULT_RERANK_MODEL
        self.timeout = max(int(timeout or 120), 1)
        raw_base = str(base_url or DEFAULT_SILICONFLOW_BASE_URL).strip().rstrip("/")
        self.endpoint = raw_base if raw_base.lower().endswith("/rerank") else f"{raw_base}/rerank"

    def rerank(self, query: str, documents: list[str], top_n: int | None = None, model: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": str(model or self.model or "").strip() or DEFAULT_RERANK_MODEL,
            "query": str(query or "").strip(),
            "documents": list(documents or []),
        }
        if top_n is not None:
            payload["top_n"] = max(int(top_n), 1)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(self.endpoint, headers=headers, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("SiliconFlow rerank returned a non-object response")
        return data


def build_token_encoder() -> Any:
    try:
        import tiktoken  # type: ignore

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def estimate_tokens(text: str, encoder: Any) -> int:
    if encoder is None:
        return max(1, len(text) // 3)
    return len(encoder.encode(text))


def score_to_stars(score: float) -> int:
    if score >= 0.9:
        return 5
    if score >= 0.5:
        return 4
    if score >= 0.1:
        return 3
    if score >= 0.01:
        return 2
    return 1


def resolve_input_path(context: RunContext, run_date: date, override: Path | None) -> Path:
    if override is not None:
        return override
    token = run_date.strftime("%Y%m%d")
    return context.archive_root / token / "filtered" / f"arxiv_papers_{token}.json"


def resolve_output_path(context: RunContext, step_input: RerankStepInput) -> Path:
    if step_input.output_path_override is not None:
        return step_input.output_path_override
    token = step_input.run_date.strftime("%Y%m%d")
    return context.archive_root / token / "rank" / f"arxiv_papers_{token}.rerank.json"


def load_rrf_output(path: Path) -> RRFStepOutput:
    payload = read_json_file(path)
    run_date_raw = str(payload.get("run_date") or "").strip()
    run_date = resolve_run_date(run_date_raw) if run_date_raw else None
    stats_payload = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    return RRFStepOutput(
        run_date=run_date,
        tagged_papers=load_lane_tagged_papers(payload),
        query_results=load_lane_query_results(payload),
        artifacts=RRFArtifacts(output_path=path),
        stats=RRFStats(**stats_payload) if isinstance(stats_payload, dict) else RRFStats(),
        warnings=[str(item) for item in (payload.get("warnings") or []) if str(item).strip()],
    )


def is_intent_query(query_result: QueryResult) -> bool:
    query_type = str(query_result.query.type or "").strip().lower()
    return query_type in {"intent_query", "llm_query"}


def select_intent_queries(query_results: list[QueryResult]) -> list[QueryResult]:
    return [item for item in query_results if is_intent_query(item)]


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        paper_id = str(item or "").strip()
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        out.append(paper_id)
    return out


def _clamp_int(value: float | int, min_value: int, max_value: int) -> int:
    return max(min_value, min(int(value), max_value))


def resolve_global_pool_budget(total_papers: int, intent_query_count: int) -> tuple[int, int, int]:
    total = max(int(total_papers or 0), 0)
    intent_count = max(int(intent_query_count or 0), 1)
    if total <= 0:
        lane_top_k = LANE_TOP_K_BASE
    else:
        blocks = (total - 1) // 1000
        lane_top_k = min(LANE_TOP_K_BASE + LANE_TOP_K_STEP * blocks, LANE_TOP_K_MAX)
    guaranteed_per_lane = _clamp_int(
        round(lane_top_k * 0.25),
        GLOBAL_POOL_GUARANTEED_MIN,
        GLOBAL_POOL_GUARANTEED_MAX,
    )
    global_rrf_top = _clamp_int(
        lane_top_k * intent_count,
        GLOBAL_POOL_RRF_MIN,
        GLOBAL_POOL_RRF_MAX,
    )
    return lane_top_k, guaranteed_per_lane, global_rrf_top


def get_ranked_paper_ids(query_result: QueryResult) -> list[str]:
    items: list[tuple[str, int | None, float]] = []
    for paper_id, meta in query_result.sim_scores.items():
        if not isinstance(meta, dict):
            continue
        rank_raw = meta.get("rank")
        score_raw = meta.get("score")
        try:
            rank = int(rank_raw) if rank_raw is not None else None
        except Exception:
            rank = None
        try:
            score = float(score_raw) if score_raw is not None else 0.0
        except Exception:
            score = 0.0
        items.append((str(paper_id), rank, score))
    if all(rank is not None for _, rank, _ in items):
        items.sort(key=lambda item: ((item[1] if item[1] is not None else 10**9), item[0]))
    else:
        items.sort(key=lambda item: (-item[2], item[0]))
    return [paper_id for paper_id, _rank, _score in items]


def build_global_candidate_ids(
    query_results: list[QueryResult],
    *,
    guaranteed_per_lane: int,
    global_limit: int,
) -> list[str]:
    score_map: dict[str, float] = {}
    hit_count: dict[str, int] = {}
    guaranteed_ids: list[str] = []

    for query_result in query_results:
        top_ids = get_ranked_paper_ids(query_result)
        if not top_ids:
            continue
        if guaranteed_per_lane > 0:
            guaranteed_ids.extend(top_ids[:guaranteed_per_lane])
        for rank_idx, paper_id in enumerate(top_ids, start=1):
            score_map[paper_id] = score_map.get(paper_id, 0.0) + 1.0 / (GLOBAL_RRF_K + rank_idx)
            hit_count[paper_id] = hit_count.get(paper_id, 0) + 1

    ranked = sorted(
        score_map.items(),
        key=lambda item: (-item[1], -hit_count.get(item[0], 0), item[0]),
    )
    global_ids = [paper_id for paper_id, _score in ranked]
    if global_limit > 0:
        global_ids = global_ids[:global_limit]
    return _unique_keep_order(list(guaranteed_ids) + list(global_ids))


def build_candidate_paper_pool(
    tagged_papers: list[TaggedPaperRecord],
    global_candidate_ids: list[str],
) -> tuple[list[TaggedPaperRecord], list[str]]:
    paper_index = {paper.id: paper for paper in tagged_papers}
    candidate_papers: list[TaggedPaperRecord] = []
    missing_ids: list[str] = []
    for paper_id in global_candidate_ids:
        paper = paper_index.get(paper_id)
        if paper is None:
            missing_ids.append(paper_id)
            continue
        candidate_papers.append(paper)
    return candidate_papers, missing_ids


def format_doc(title: str, abstract: str) -> str:
    content = f"Title: {title}\nAbstract: {abstract}".strip()
    if len(content) > MAX_CHARS_PER_DOC:
        content = content[:MAX_CHARS_PER_DOC]
    return content


def build_documents(papers_by_id: dict[str, TaggedPaperRecord], paper_ids: list[str]) -> list[str]:
    docs: list[str] = []
    for paper_id in paper_ids:
        paper = papers_by_id.get(paper_id)
        if paper is None:
            docs.append(f"[Missing paper {paper_id}]")
            continue
        title = str(paper.title or "").strip()
        abstract = str(paper.abstract or "").strip()
        if title or abstract:
            docs.append(format_doc(title, abstract))
        else:
            docs.append(f"[Empty paper {paper_id}]")
    return docs


def iter_batches(
    docs_with_idx: list[tuple[int, str]],
    query_tokens: int,
    encoder: Any,
) -> list[tuple[list[int], list[str]]]:
    batches: list[tuple[list[int], list[str]]] = []
    pos = 0
    while pos < len(docs_with_idx):
        total_tokens = query_tokens
        batch_docs: list[str] = []
        batch_indices: list[int] = []

        while pos < len(docs_with_idx) and len(batch_docs) < BATCH_SIZE:
            orig_idx, doc = docs_with_idx[pos]
            doc_tokens = estimate_tokens(doc, encoder)
            if total_tokens + doc_tokens > TOKEN_SAFETY and batch_docs:
                break
            batch_docs.append(doc)
            batch_indices.append(orig_idx)
            total_tokens += doc_tokens
            pos += 1

        if not batch_docs:
            pos += 1
            continue
        batches.append((batch_indices, batch_docs))
    return batches


def normalize_ranked_items(items: list[tuple[str, float | None]]) -> list[dict[str, Any]]:
    if not items:
        return []
    numeric_scores = [score for _paper_id, score in items if score is not None]
    min_score = min(numeric_scores) if numeric_scores else None
    max_score = max(numeric_scores) if numeric_scores else None
    total = len(items)
    ranked: list[dict[str, Any]] = []
    for idx, (paper_id, score) in enumerate(items, start=1):
        if (
            score is not None
            and min_score is not None
            and max_score is not None
            and max_score > min_score
        ):
            normalized = (score - min_score) / (max_score - min_score)
        elif total == 1:
            normalized = 1.0
        else:
            normalized = (total - idx) / (total - 1)
        normalized = float(max(0.0, min(1.0, normalized)))
        ranked.append(
            {
                "paper_id": paper_id,
                "score": normalized,
                "star_rating": score_to_stars(normalized),
            }
        )
    ranked.sort(key=lambda item: (-float(item["score"]), str(item["paper_id"])))
    return ranked


def run_query_fallback_rerank(
    query_result: QueryResult,
    global_candidate_ids: list[str],
    top_n: int | None,
) -> list[dict[str, Any]]:
    allowed_ids = set(global_candidate_ids)
    items: list[tuple[str, float | None]] = []
    for paper_id in get_ranked_paper_ids(query_result):
        if paper_id not in allowed_ids:
            continue
        meta = query_result.sim_scores.get(paper_id)
        score = None
        if isinstance(meta, dict):
            raw_score = meta.get("score")
            try:
                score = float(raw_score) if raw_score is not None else None
            except Exception:
                score = None
        items.append((paper_id, score))
    if top_n is not None and top_n > 0:
        items = items[:top_n]
    return normalize_ranked_items(items)


def extract_rerank_results(response: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(response.get("output"), dict):
        results = response.get("output", {}).get("results")
    else:
        results = response.get("results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def run_query_model_rerank(
    reranker: SiliconFlowRerankClient,
    query_result: QueryResult,
    papers_by_id: dict[str, TaggedPaperRecord],
    global_candidate_ids: list[str],
    *,
    rerank_model: str,
    top_n: int | None,
    encoder: Any,
) -> list[dict[str, Any]]:
    query_text = str(query_result.query.query_text or "").strip()
    if not query_text or not global_candidate_ids:
        return []

    documents = build_documents(papers_by_id, global_candidate_ids)
    docs_with_idx = list(enumerate(documents))
    random.shuffle(docs_with_idx)

    query_tokens = estimate_tokens(query_text, encoder)
    batches = iter_batches(docs_with_idx, query_tokens, encoder)
    if not batches:
        return []

    log(
        f"[INFO] Step 3 model rerank tag={query_result.query.tag or ''} "
        f"candidates={len(global_candidate_ids)} batches={len(batches)}"
    )

    rrf_scores: dict[int, float] = {}
    for batch_idx, (batch_indices, batch_docs) in enumerate(batches, start=1):
        log(f"[INFO] Step 3 rerank batch {batch_idx}/{len(batches)} docs={len(batch_docs)}")
        response = reranker.rerank(
            query=query_text,
            documents=batch_docs,
            top_n=len(batch_docs),
            model=rerank_model,
        )
        ranked = sorted(
            extract_rerank_results(response),
            key=lambda item: float(item.get("relevance_score", item.get("score", 0.0)) or 0.0),
            reverse=True,
        )
        for rank_idx, item in enumerate(ranked, start=1):
            try:
                idx = int(item.get("index", -1))
            except Exception:
                idx = -1
            if idx < 0 or idx >= len(batch_indices):
                continue
            orig_idx = batch_indices[idx]
            rrf_scores[orig_idx] = rrf_scores.get(orig_idx, 0.0) + 1.0 / (GLOBAL_RRF_K + rank_idx)

    if not rrf_scores:
        return []

    ranked_items = sorted(
        ((global_candidate_ids[idx], score) for idx, score in rrf_scores.items()),
        key=lambda item: (-item[1], item[0]),
    )
    if top_n is not None and top_n > 0:
        ranked_items = ranked_items[:top_n]
    return normalize_ranked_items(ranked_items)


def resolve_rerank_client(step_input: RerankStepInput) -> tuple[SiliconFlowRerankClient | None, str]:
    if step_input.disable_rerank:
        return None, "rerank_disabled"
    api_key = str(os.getenv("SILICONFLOW_API_KEY") or "").strip()
    if not api_key:
        return None, "no_api_key"
    model_name = str(step_input.rerank_model or "").strip() or DEFAULT_RERANK_MODEL
    base_url = str(os.getenv("SILICONFLOW_BASE_URL") or DEFAULT_SILICONFLOW_BASE_URL).strip() or DEFAULT_SILICONFLOW_BASE_URL
    return SiliconFlowRerankClient(api_key=api_key, model=model_name, base_url=base_url), ""


def write_rerank_output(path: Path, output: RerankStepOutput) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_date": output.run_date.isoformat() if output.run_date else "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "papers": [asdict(paper) for paper in output.papers],
        "global_candidate_ids": list(output.global_candidate_ids),
        "ranked_queries": [asdict(item) for item in output.ranked_queries],
        "stats": asdict(output.stats),
        "warnings": list(output.warnings),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_rerank_step(context: RunContext, step_input: RerankStepInput) -> RerankStepOutput:
    output_path = resolve_output_path(context, step_input)
    warnings = list(step_input.rrf_output.warnings)
    intent_queries = select_intent_queries(step_input.rrf_output.query_results)
    stats = RerankStats(intent_queries_total=len(intent_queries))

    lane_top_k, guaranteed_per_lane, global_rrf_top = resolve_global_pool_budget(
        len(step_input.rrf_output.tagged_papers),
        len(intent_queries),
    )
    log(
        f"Step 3 run_date={step_input.run_date.isoformat()} papers={len(step_input.rrf_output.tagged_papers)} "
        f"queries={len(step_input.rrf_output.query_results)} intent_queries={len(intent_queries)} "
        f"lane_top_k={lane_top_k} guaranteed_per_lane={guaranteed_per_lane} global_top={global_rrf_top}"
    )

    global_candidate_ids = build_global_candidate_ids(
        step_input.rrf_output.query_results,
        guaranteed_per_lane=guaranteed_per_lane,
        global_limit=global_rrf_top,
    )
    stats.global_candidate_count = len(global_candidate_ids)

    candidate_papers, missing_candidate_ids = build_candidate_paper_pool(
        step_input.rrf_output.tagged_papers,
        global_candidate_ids,
    )
    if missing_candidate_ids:
        warnings.append(f"{len(missing_candidate_ids)} global candidate ids were missing from the tagged paper pool")
    papers_by_id = {paper.id: paper for paper in candidate_papers}

    if not intent_queries:
        warnings.append("No intent queries were available for Step 3 rerank")
        log("[WARN] Step 3 found no intent queries; output will contain an empty ranked_queries list.")
    if not global_candidate_ids:
        warnings.append("Global candidate pool is empty; Step 3 produced no ranked results")
        log("[WARN] Step 3 global candidate pool is empty; no rerank will be attempted.")

    reranker, fallback_reason = resolve_rerank_client(step_input)
    if fallback_reason:
        warnings.append(f"Step 3 is using fallback ranking: reason={fallback_reason}")
        log(f"[WARN] Step 3 rerank unavailable, falling back to Step 2.3 fused ranking. reason={fallback_reason}")

    encoder = build_token_encoder() if reranker is not None else None
    rerank_model = str(step_input.rerank_model or "").strip() or DEFAULT_RERANK_MODEL
    ranked_queries: list[RerankQuery] = []

    for query_result in intent_queries:
        ranked: list[dict[str, Any]] = []
        query_fallback_reason = fallback_reason
        if reranker is not None and global_candidate_ids:
            try:
                ranked = run_query_model_rerank(
                    reranker,
                    query_result,
                    papers_by_id,
                    global_candidate_ids,
                    rerank_model=rerank_model,
                    top_n=step_input.top_n,
                    encoder=encoder,
                )
                if ranked:
                    stats.used_rerank = True
            except Exception as exc:
                query_fallback_reason = "rerank_request_failed"
                warning = (
                    f"Step 3 model rerank failed for tag={query_result.query.tag or ''}: {exc}"
                )
                warnings.append(warning)
                log(f"[WARN] {warning}")

        if not ranked:
            ranked = run_query_fallback_rerank(query_result, global_candidate_ids, step_input.top_n)
            if ranked:
                stats.fallback_used = True
                if query_fallback_reason == "rerank_request_failed":
                    log(
                        f"[WARN] Step 3 fallback ranking applied for tag={query_result.query.tag or ''} "
                        f"reason={query_fallback_reason}"
                    )

        ranked_queries.append(
            RerankQuery(
                type=query_result.query.type,
                tag=query_result.query.tag,
                paper_tag=query_result.query.paper_tag,
                query_text=query_result.query.query_text,
                logic_cn=query_result.query.logic_cn,
                ranked=ranked,
            )
        )

    stats.ranked_queries = sum(1 for item in ranked_queries if item.ranked)
    output = RerankStepOutput(
        run_date=step_input.run_date,
        papers=candidate_papers,
        global_candidate_ids=global_candidate_ids,
        ranked_queries=ranked_queries,
        artifacts=RerankArtifacts(output_path=output_path),
        stats=stats,
        warnings=warnings,
    )
    write_rerank_output(output_path, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step 3 rerank intent queries against a global candidate pool")
    parser.add_argument("--run-date", required=False, help="Run date in YYYY-MM-DD or YYYYMMDD")
    parser.add_argument(
        "--input-path-override",
        default=None,
        help="Optional Step 2.3 fused input path override",
    )
    parser.add_argument(
        "--output-path-override",
        default=None,
        help="Optional Step 3 output path override",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="Per-query top N to keep after rerank or fallback",
    )
    parser.add_argument(
        "--rerank-model",
        default=os.getenv("RERANK_MODEL") or DEFAULT_RERANK_MODEL,
        help="Rerank model name for SiliconFlow",
    )
    parser.add_argument(
        "--disable-rerank",
        action="store_true",
        help="Skip external rerank and force fallback ranking",
    )
    parser.add_argument(
        "--print-todos",
        action="store_true",
        help="Print Step 3 follow-up notes and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.print_todos:
        for idx, item in enumerate(STEP3_NOTES, start=1):
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

    input_path = resolve_input_path(
        context,
        run_date,
        Path(args.input_path_override).resolve() if args.input_path_override else None,
    )
    rrf_output = load_rrf_output(input_path)
    step_input = RerankStepInput(
        run_date=run_date,
        rrf_output=rrf_output,
        top_n=max(int(args.top_n), 1) if args.top_n is not None else None,
        rerank_model=str(args.rerank_model or "").strip() or DEFAULT_RERANK_MODEL,
        output_path_override=Path(args.output_path_override).resolve() if args.output_path_override else None,
        disable_rerank=bool(args.disable_rerank),
    )

    log("Step 3 v2 starting")
    log(f"run_date={run_date.isoformat()}")
    log(f"input_path={input_path}")
    output = run_rerank_step(context, step_input)
    log(f"global_candidate_count={output.stats.global_candidate_count}")
    log(f"ranked_queries={output.stats.ranked_queries}")
    log(f"used_rerank={output.stats.used_rerank}")
    log(f"fallback_used={output.stats.fallback_used}")
    log(f"output_path={output.artifacts.output_path}")
    if output.warnings:
        log(f"warnings={len(output.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
