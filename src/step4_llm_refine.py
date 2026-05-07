#!/usr/bin/env python3
"""Step 4 LLM refine papers for a single run date."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from llm import OpenRouterClient
from subscription_plan import build_pipeline_inputs

from step1_fetch import RunContext, build_run_context, log, read_json_file, resolve_run_date
from step3_rerank import RerankQuery, RerankStepOutput, load_rerank_output

DEFAULT_FILTER_MODEL = os.getenv("FILTER_MODEL") or os.getenv("LLM_MODEL") or "deepseek/deepseek-v3.2"
DEFAULT_FILTER_CONCURRENCY = 4
MAX_FILTER_RETRIES = 3
MAX_CHARS_PER_DOC = 2000


@dataclass(slots=True)
class LLMRefineRequirement:
    id: str
    query: str
    tag: str
    description_en: str = ""


@dataclass(slots=True)
class ScoredItem:
    paper_id: str
    score: float
    evidence_en: str = ""
    evidence_cn: str = ""
    tldr_en: str = ""
    tldr_cn: str = ""
    matched_requirement_index: int = 0
    matched_query_tag: str = ""
    matched_query_text: str = ""


@dataclass(slots=True)
class LLMRefineStats:
    requirements_count: int = 0
    candidate_papers: int = 0
    scored_papers: int = 0
    failed_papers: int = 0
    batches_dispatched: int = 0
    batches_succeeded: int = 0
    recovery_attempts: int = 0


@dataclass(slots=True)
class LLMRefineArtifacts:
    output_path: Path | None = None


@dataclass(slots=True)
class LLMRefineStepInput:
    run_date: date
    rerank_output: RerankStepOutput
    min_star: int = 4
    batch_size: int = 25
    max_chars: int = MAX_CHARS_PER_DOC
    filter_model: str = ""
    max_output_tokens: int = 4000
    filter_concurrency: int = DEFAULT_FILTER_CONCURRENCY
    output_path_override: Path | None = None


@dataclass(slots=True)
class LLMRefineStepOutput:
    run_date: date | None = None
    scored_items: list[ScoredItem] = field(default_factory=list)
    artifacts: LLMRefineArtifacts = field(default_factory=LLMRefineArtifacts)
    stats: LLMRefineStats = field(default_factory=LLMRefineStats)
    warnings: list[str] = field(default_factory=list)


STEP4_NOTES = [
    "Add unit tests for requirement building, candidate filtering, and result merging",
    "Decide whether composite profile requirements should be reintroduced",
    "Decide whether LLM provider should be configurable beyond OpenRouter",
]


def _norm_text(value: Any) -> str:
    return str(value or "").strip()


def _slug(text: str, fallback: str = "query") -> str:
    raw = str(text or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    raw = re.sub(r"-+", "-", raw).strip("-")
    return raw or fallback


def _coerce_score(value: Any) -> float:
    try:
        score = float(value)
    except Exception:
        score = 0.0
    return max(0.0, min(10.0, score))


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = _norm_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def resolve_input_path(context: RunContext, run_date: date, override: Path | None) -> Path:
    if override is not None:
        return override
    token = run_date.strftime("%Y%m%d")
    return context.archive_root / token / "rank" / f"arxiv_papers_{token}.rerank.json"


def resolve_output_path(context: RunContext, step_input: LLMRefineStepInput) -> Path:
    if step_input.output_path_override is not None:
        return step_input.output_path_override
    token = step_input.run_date.strftime("%Y%m%d")
    return context.archive_root / token / "rank" / f"arxiv_papers_{token}.llm.json"


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    lowered = _norm_text(value).lower()
    if lowered in {"0", "false", "no", "off"}:
        return False
    if lowered in {"1", "true", "yes", "on"}:
        return True
    return default


def _normalize_query_tag(raw_tag: str, query_text: str, idx: int) -> str:
    text = str(raw_tag or "").strip()
    if text.startswith("query:"):
        base = text.split(":", 1)[1].strip()
        return f"query:{_slug(base, fallback=f'q{idx}')}"
    if text:
        return f"query:{_slug(text, fallback=f'q{idx}')}"
    return f"query:{_slug(query_text, fallback=f'q{idx}')}"


def _collect_profile_composite_clauses(profile: dict[str, Any]) -> list[str]:
    clauses: list[str] = []
    for item in profile.get("keywords") or []:
        if isinstance(item, dict) and not _as_bool(item.get("enabled"), True):
            continue
        if isinstance(item, dict):
            text = _norm_text(
                item.get("query")
                or item.get("keyword")
                or item.get("text")
                or item.get("expr")
                or ""
            )
        else:
            text = _norm_text(item)
        if text:
            clauses.append(text)
    for item in profile.get("intent_queries") or []:
        if isinstance(item, dict) and not _as_bool(item.get("enabled"), True):
            continue
        if isinstance(item, dict):
            text = _norm_text(
                item.get("query")
                or item.get("text")
                or item.get("keyword")
                or item.get("expr")
                or ""
            )
        else:
            text = _norm_text(item)
        if text:
            clauses.append(text)
    return _unique_keep_order(clauses)


def _build_profile_composite_requirement(
    profile: dict[str, Any],
    index: int,
    seen_queries: set[str],
) -> LLMRefineRequirement | None:
    if not isinstance(profile, dict) or not _as_bool(profile.get("enabled"), True):
        return None
    clauses = _collect_profile_composite_clauses(profile)
    if len(clauses) < 2:
        return None

    tag = _norm_text(profile.get("tag") or f"profile-{index + 1}")
    description = _norm_text(profile.get("description") or tag)
    focus_label = description or tag
    composite_query = (
        f"Papers central to {focus_label}, especially work that connects or combines: "
        f"{'; '.join(clauses[:10])}."
    )
    lowered = composite_query.lower()
    if lowered in seen_queries:
        return None
    seen_queries.add(lowered)

    composite_tag = f"query:{_slug(tag, fallback=f'profile-{index + 1}')}:composite"
    return LLMRefineRequirement(
        id=f"req-composite-{_slug(tag, fallback=f'profile-{index + 1}')}",
        query=composite_query,
        tag=composite_tag,
        description_en=(
            f"Find papers central to the combined {focus_label} theme. "
            f"Consider these signals together: {'; '.join(clauses[:8])}"
        ),
    )


def build_user_requirements(config: dict[str, Any]) -> list[LLMRefineRequirement]:
    requirements: list[LLMRefineRequirement] = []
    seen: set[str] = set()

    pipeline_inputs = build_pipeline_inputs(config or {})
    for item in pipeline_inputs.get("context_queries") or []:
        text = _norm_text(item.get("query"))
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        tag = _normalize_query_tag(
            _norm_text(item.get("tag")),
            text,
            len(requirements) + 1,
        )
        requirements.append(
            LLMRefineRequirement(
                id=f"req-{len(requirements) + 1}",
                query=text,
                tag=tag,
                description_en=f"Find papers relevant to this user requirement: {text}",
            )
        )

    profiles = (((config or {}).get("subscriptions") or {}).get("intent_profiles") or [])
    if isinstance(profiles, list):
        for idx, profile in enumerate(profiles):
            composite_req = _build_profile_composite_requirement(profile, idx, seen)
            if composite_req:
                requirements.append(composite_req)

    return requirements


def build_candidate_papers(
    ranked_queries: list[RerankQuery],
    paper_map: dict[str, dict[str, Any]],
    min_star: int,
    max_chars: int,
) -> list[dict[str, str]]:
    candidate_ids: list[str] = []
    seen: set[str] = set()
    for rq in ranked_queries:
        for item in rq.ranked:
            star_rating = int(item.get("star_rating", 0) or 0)
            if star_rating < min_star:
                continue
            pid = _norm_text(item.get("paper_id"))
            if not pid or pid in seen:
                continue
            seen.add(pid)
            candidate_ids.append(pid)

    docs: list[dict[str, str]] = []
    for pid in candidate_ids:
        paper = paper_map.get(pid)
        if not paper:
            continue
        title = _norm_text(paper.get("title"))
        abstract = _norm_text(paper.get("abstract"))
        content = f"Title: {title}\nAbstract: {abstract}".strip()
        if len(content) > max_chars:
            content = content[:max_chars]
        if content:
            docs.append({"id": pid, "content": content})
    return docs


def build_paper_map(papers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    paper_map: dict[str, dict[str, Any]] = {}
    for p in papers:
        pid = _norm_text(p.get("id"))
        if pid:
            paper_map[pid] = p
    return paper_map


def build_filter_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "matched_requirement_index": {"type": "integer"},
                        "evidence_en": {"type": "string"},
                        "evidence_cn": {"type": "string"},
                        "tldr_en": {"type": "string"},
                        "tldr_cn": {"type": "string"},
                        "score": {"type": "number"},
                    },
                    "required": [
                        "id",
                        "matched_requirement_index",
                        "evidence_en",
                        "evidence_cn",
                        "tldr_en",
                        "tldr_cn",
                        "score",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def build_filter_messages(
    all_requirements: list[LLMRefineRequirement],
    docs: list[dict[str, str]],
    retry_note: str,
) -> list[dict[str, str]]:
    system_prompt = (
        "You are an intelligent Research Relevance Evaluator. "
        "Score papers (0-10) based purely on relevance to ANY item in user's requirement list. "
        "Prioritize conceptual/method relevance over exact term overlap. "
        "Use the rubric and return JSON only."
    )

    req_lines: list[str] = []
    for idx, req in enumerate(all_requirements, start=1):
        desc = _norm_text(req.description_en) or _norm_text(req.query)
        req_tag = _norm_text(req.tag)
        if desc:
            if req_tag:
                req_lines.append(f"{idx}. {desc} [tag={req_tag}]")
            else:
                req_lines.append(f"{idx}. {desc}")

    user_prompt = (
        "User requirements list:\n"
        f"{chr(10).join(req_lines)}\n\n"
        "SCORING RUBRIC:\n"
        "9-10: Direct Requirement Match (same problem target and same evaluation intent)\n"
        "8-9: Strong Method Match (different wording but equivalent objective/technical core)\n"
        "6-8: Methodological Bridge (transferable method/approach likely useful for requirement)\n"
        "3-4: Tangential (same broad discipline, weak link)\n"
        "0-2: Noise (irrelevant)\n\n"
        "GUARDRAILS:\n"
        "1) Beware of Polysemy: If a keyword is ambiguous, only match the sense that aligns with the user's intent.\n"
        "2) Reject Literal Matching: Do NOT score high just because the same word appears.\n"
        "3) Reward Conceptual Equivalence: If wording differs but goals/methods are equivalent, score as high relevance.\n"
        "4) Reward Enabling Methods: If a paper provides a generally applicable method/tool that directly supports requirement tasks, do not under-score it.\n"
        "5) Be strict only when mismatch is substantive (different task objective, incompatible setting, or no reusable method).\n\n"
        "Papers:\n"
        f"{json.dumps(docs, ensure_ascii=False)}\n\n"
        "Output JSON format example:\n"
        '{"results": [{"id": "paper_id", "matched_requirement_index": 1, "evidence_en": "short English phrase", "evidence_cn": "简短中文短语", "tldr_en": "one-sentence TLDR", "tldr_cn": "一句话 TLDR", "score": 7}]}\n\n'
        "Requirement: You MUST return exactly one result for every input paper. "
        "The results length must match the papers length, and every input id must appear once.\n\n"
        "Output must be a single-line JSON string. "
        "Do not include line breaks inside any string fields. "
        "Avoid double quotes inside evidence text fields.\n\n"
        "Task: Evaluate papers against the WHOLE requirement list. "
        "If a paper matches any one point, it can get a high score. "
        "Set matched_requirement_index to the best-matched requirement (1-based). "
        "Use semantic interpretation, not only lexical overlap, to decide relevance and score tier. "
        "Evidence must be provided in both languages: "
        "evidence_en (English) and evidence_cn (Chinese). "
        "They should be short phrases linking the paper to the matched requirement; "
        "they do NOT need to be direct quotes. "
        "Also generate TLDR in both languages: tldr_en and tldr_cn. "
        "TLDR should be one sentence summarizing what the paper does and why it matters. "
        "Keep TLDR concise: <= 120 characters in English and <= 60 Chinese characters. "
        "Then give a score (0-10). "
        'If unrelated, use evidence_en="not relevant", evidence_cn="不相关", '
        'tldr_en="not relevant", tldr_cn="不相关", score 0, matched_requirement_index=0.'
    )

    if retry_note:
        user_prompt += f"\n\nRetry correction note:\n{retry_note}"

    repeated = f"{user_prompt}\n\nLet me repeat that:\n{user_prompt}"

    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": repeated + "\n\nOutput must be strict JSON only, no markdown, no fences, no extra text.",
        },
    ]


def call_filter(
    client: OpenRouterClient,
    all_requirements: list[LLMRefineRequirement],
    docs: list[dict[str, str]],
    schema: dict[str, Any],
    debug_dir: str,
    debug_tag: str,
    retry_note: str = "",
) -> list[dict[str, Any]]:
    messages = build_filter_messages(all_requirements, docs, retry_note)
    resp = client.chat_structured(
        messages=messages,
        schema_name="rerank_batch",
        schema=schema,
        strict=True,
        allow_json_object_fallback=True,
    )
    content = str(resp.get("content") or "")
    try:
        if resp.get("refusal"):
            raise ValueError(f"structured output refusal: {resp.get('refusal')}")
        if resp.get("finish_reason") not in (None, "stop"):
            raise ValueError(f"unexpected finish_reason: {resp.get('finish_reason')}")
        if resp.get("parse_error") is not None:
            raise resp["parse_error"]
        payload = resp.get("parsed")
        if not isinstance(payload, dict):
            raise ValueError("parsed payload is not an object")
    except Exception as exc:
        preview = (content or "").strip().replace("\n", " ")
        if len(preview) > 800:
            preview = preview[:800] + "..."
        debug_path = ""
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            tag = debug_tag or f"batch_{int(time.time())}"
            debug_path = os.path.join(debug_dir, f"filter_raw_{tag}.txt")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(content or "")
        msg = f"JSON parse failed: {exc}. raw={preview}"
        if debug_path:
            msg = f"{msg} | saved={debug_path}"
        raise ValueError(msg)

    results = payload.get("results", [])
    if not isinstance(results, list):
        return []
    return results


def _normalize_filter_result_item(item: dict[str, Any]) -> dict[str, Any]:
    legacy = _norm_text(item.get("evidence"))
    evidence_en = _norm_text(item.get("evidence_en") or legacy)
    evidence_cn = _norm_text(item.get("evidence_cn") or legacy or evidence_en)
    score = _coerce_score(item.get("score"))
    tldr_en = _norm_text(item.get("tldr_en")) or ("not relevant" if score <= 0 else evidence_en)
    tldr_cn = _norm_text(item.get("tldr_cn")) or ("不相关" if score <= 0 else (evidence_cn or tldr_en))
    return {
        "id": _norm_text(item.get("id")),
        "matched_requirement_index": _coerce_int(item.get("matched_requirement_index"), 0),
        "evidence_en": evidence_en,
        "evidence_cn": evidence_cn,
        "tldr_en": tldr_en,
        "tldr_cn": tldr_cn,
        "score": score,
    }


def validate_filter_results(
    batch_docs: list[dict[str, str]],
    results: Any,
) -> list[dict[str, Any]]:
    expected_ids = [_norm_text(doc.get("id")) for doc in batch_docs if _norm_text(doc.get("id"))]
    if not expected_ids:
        return []
    if not isinstance(results, list):
        raise ValueError("results must be a list")

    expected_set = set(expected_ids)
    normalized_by_id: dict[str, dict[str, Any]] = {}
    problems: list[str] = []

    for idx, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            problems.append(f"item#{idx}: not an object")
            continue
        normalized = _normalize_filter_result_item(item)
        pid = normalized["id"]
        if not pid:
            problems.append(f"item#{idx}: missing id")
            continue
        if pid not in expected_set:
            problems.append(f"item#{idx}: unexpected id={pid}")
            continue
        if pid in normalized_by_id:
            problems.append(f"item#{idx}: duplicate id={pid}")
            continue
        normalized_by_id[pid] = normalized

    missing_ids = [pid for pid in expected_ids if pid not in normalized_by_id]
    if missing_ids:
        problems.append(f"missing ids={','.join(missing_ids)}")

    if problems:
        raise ValueError("; ".join(problems))

    return [normalized_by_id[pid] for pid in expected_ids]


def build_filter_retry_note(
    batch_docs: list[dict[str, str]],
    attempt: int,
    error: Exception | None,
) -> str:
    expected_ids = [_norm_text(doc.get("id")) for doc in batch_docs if _norm_text(doc.get("id"))]
    previous_error = _norm_text(error) or "unknown validation error"
    return (
        f"Retry attempt {attempt}. The previous output was invalid: {previous_error}. "
        f"You must return exactly {len(expected_ids)} results for these ids only: {', '.join(expected_ids)}. "
        "Every id must appear once. Do not omit ids. Do not repeat ids. "
        "Keep matched_requirement_index as an integer and score within 0-10."
    )


def recover_filter_results(
    batch_docs: list[dict[str, str]],
    runner: Callable[[list[dict[str, str]], int, str], list[dict[str, Any]]],
    max_attempts: int = MAX_FILTER_RETRIES,
    debug_tag: str = "batch",
) -> list[dict[str, Any]]:
    if not batch_docs:
        return []

    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        retry_note = build_filter_retry_note(batch_docs, attempt, last_error) if last_error else ""
        try:
            raw_results = runner(batch_docs, attempt, retry_note)
            return validate_filter_results(batch_docs, raw_results)
        except Exception as exc:
            last_error = exc
            log(f"[WARN] filter {debug_tag} attempt {attempt}/{max_attempts} invalid: {exc}")

    raise ValueError(f"filter {debug_tag} failed after {max_attempts} attempts: {last_error}")


def _make_filter_client(api_key: str, model: str, max_output_tokens: int) -> OpenRouterClient:
    client = OpenRouterClient(api_key=api_key, model=model)
    client.kwargs.update({"temperature": 0.1, "max_tokens": max_output_tokens})
    return client


def _make_filter_runner(
    client: OpenRouterClient,
    all_requirements: list[LLMRefineRequirement],
    schema: dict[str, Any],
    debug_dir: str,
    base_tag: str,
) -> Callable[[list[dict[str, str]], int, str], list[dict[str, Any]]]:
    def _runner(
        docs: list[dict[str, str]],
        attempt: int,
        retry_note: str,
    ) -> list[dict[str, Any]]:
        return call_filter(
            client,
            all_requirements=all_requirements,
            docs=docs,
            schema=schema,
            debug_dir=debug_dir,
            debug_tag=f"{base_tag}_attempt_{attempt:02d}",
            retry_note=retry_note,
        )

    return _runner


def merge_filter_result(
    merged: dict[str, dict[str, Any]],
    item: dict[str, Any],
    requirement_by_index: dict[int, LLMRefineRequirement],
) -> None:
    pid = _norm_text(item.get("id") or item.get("paper_id"))
    if not pid:
        return

    score = _coerce_score(item.get("score"))
    evidence_en = _norm_text(item.get("evidence_en"))
    evidence_cn = _norm_text(item.get("evidence_cn"))
    tldr_en = _norm_text(item.get("tldr_en"))
    tldr_cn = _norm_text(item.get("tldr_cn"))
    legacy = _norm_text(item.get("evidence"))
    if not evidence_en:
        evidence_en = legacy
    if not evidence_cn:
        evidence_cn = legacy or evidence_en
    if not tldr_en:
        tldr_en = "not relevant" if score <= 0 else evidence_en
    if not tldr_cn:
        tldr_cn = "不相关" if score <= 0 else (evidence_cn or tldr_en)

    matched_idx = _coerce_int(item.get("matched_requirement_index"), 0)
    matched_req = requirement_by_index.get(matched_idx) if matched_idx > 0 else None
    matched_tag = _norm_text((matched_req or {}).tag) if matched_req else ""
    matched_query = _norm_text((matched_req or {}).query) if matched_req else ""

    prev = merged.get(pid)
    if (prev is None) or (score > float(prev.get("score", 0))):
        merged[pid] = {
            "paper_id": pid,
            "score": score,
            "evidence_en": evidence_en,
            "evidence_cn": evidence_cn,
            "tldr_en": tldr_en,
            "tldr_cn": tldr_cn,
            "matched_requirement_index": matched_idx,
            "matched_query_tag": matched_tag,
            "matched_query_text": matched_query,
        }


def _filter_batch(
    batch_idx: int,
    batch: list[dict[str, str]],
    api_key: str,
    all_requirements: list[LLMRefineRequirement],
    filter_model: str,
    max_output_tokens: int,
    schema: dict[str, Any],
    debug_dir: str,
) -> tuple[int, list[dict[str, str]], list[dict[str, Any]]]:
    client = _make_filter_client(api_key, filter_model, max_output_tokens)
    runner = _make_filter_runner(
        client,
        all_requirements=all_requirements,
        schema=schema,
        debug_dir=debug_dir,
        base_tag=f"batch_{batch_idx:03d}",
    )
    return (
        batch_idx,
        batch,
        recover_filter_results(
            batch,
            runner,
            max_attempts=MAX_FILTER_RETRIES,
            debug_tag=f"batch_{batch_idx:03d}",
        ),
    )


def write_llm_refine_output(path: Path, output: LLMRefineStepOutput) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_date": output.run_date.isoformat() if output.run_date else "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scored_items": [asdict(item) for item in output.scored_items],
        "stats": asdict(output.stats),
        "warnings": list(output.warnings),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_scored_item(raw: dict[str, Any]) -> ScoredItem:
    return ScoredItem(
        paper_id=_norm_text(raw.get("paper_id")),
        score=_coerce_score(raw.get("score")),
        evidence_en=_norm_text(raw.get("evidence_en")),
        evidence_cn=_norm_text(raw.get("evidence_cn")),
        tldr_en=_norm_text(raw.get("tldr_en")),
        tldr_cn=_norm_text(raw.get("tldr_cn")),
        matched_requirement_index=_coerce_int(raw.get("matched_requirement_index"), 0),
        matched_query_tag=_norm_text(raw.get("matched_query_tag")),
        matched_query_text=_norm_text(raw.get("matched_query_text")),
    )


def load_llm_refine_output(path: Path) -> LLMRefineStepOutput:
    payload = read_json_file(path)
    run_date_raw = str(payload.get("run_date") or "").strip()
    run_date = resolve_run_date(run_date_raw) if run_date_raw else None
    stats_payload = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    return LLMRefineStepOutput(
        run_date=run_date,
        scored_items=[_parse_scored_item(item) for item in (payload.get("scored_items") or []) if isinstance(item, dict)],
        artifacts=LLMRefineArtifacts(output_path=path),
        stats=LLMRefineStats(**stats_payload) if isinstance(stats_payload, dict) else LLMRefineStats(),
        warnings=[str(item) for item in (payload.get("warnings") or []) if str(item).strip()],
    )


def run_llm_refine_step(context: RunContext, step_input: LLMRefineStepInput) -> LLMRefineStepOutput:
    output_path = resolve_output_path(context, step_input)
    warnings: list[str] = []
    filter_model = _norm_text(step_input.filter_model) or DEFAULT_FILTER_MODEL

    requirements = build_user_requirements(context.config)
    if not requirements:
        warnings.append("No user requirements built from config")
        log("[WARN] No user requirements built from config, skipping LLM refine.")
        return LLMRefineStepOutput(
            run_date=step_input.run_date,
            artifacts=LLMRefineArtifacts(output_path=output_path),
            stats=LLMRefineStats(requirements_count=0),
            warnings=warnings,
        )

    paper_map = build_paper_map(
        [asdict(paper) for paper in step_input.rerank_output.papers]
    )
    docs = build_candidate_papers(
        step_input.rerank_output.ranked_queries,
        paper_map,
        step_input.min_star,
        step_input.max_chars,
    )
    if not docs:
        warnings.append(f"No candidate papers with star_rating >= {step_input.min_star}")
        log(f"[WARN] No candidate papers with star_rating >= {step_input.min_star}, skipping LLM refine.")
        return LLMRefineStepOutput(
            run_date=step_input.run_date,
            artifacts=LLMRefineArtifacts(output_path=output_path),
            stats=LLMRefineStats(requirements_count=len(requirements)),
            warnings=warnings,
        )

    api_key = _norm_text(os.getenv("OPENROUTER_API_KEY")) or _norm_text(os.getenv("LLM_API_KEY"))
    if not api_key:
        warnings.append("Missing OPENROUTER_API_KEY / LLM_API_KEY")
        log("[WARN] Missing OPENROUTER_API_KEY / LLM_API_KEY, skipping LLM refine.")
        return LLMRefineStepOutput(
            run_date=step_input.run_date,
            artifacts=LLMRefineArtifacts(output_path=output_path),
            stats=LLMRefineStats(
                requirements_count=len(requirements),
                candidate_papers=len(docs),
            ),
            warnings=warnings,
        )

    stats = LLMRefineStats(
        requirements_count=len(requirements),
        candidate_papers=len(docs),
    )

    log(
        f"LLM refine run_date={step_input.run_date.isoformat()} "
        f"requirements={len(requirements)} candidates={len(docs)} "
        f"min_star={step_input.min_star} batch_size={step_input.batch_size} "
        f"model={filter_model} concurrency={step_input.filter_concurrency}"
    )

    random.shuffle(docs)
    batches = [docs[i : i + step_input.batch_size] for i in range(0, len(docs), step_input.batch_size)]
    total_batches = len(batches)
    stats.batches_dispatched = total_batches

    requirement_by_index: dict[int, LLMRefineRequirement] = {
        i + 1: r for i, r in enumerate(requirements)
    }
    schema = build_filter_schema()
    debug_dir = str(output_path.parent / "debug")
    merged: dict[str, dict[str, Any]] = {}
    failed_docs: list[dict[str, str]] = []

    max_workers = max(1, step_input.filter_concurrency)
    pending: dict[Any, tuple[int, list[dict[str, str]]]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, batch in enumerate(batches, start=1):
            log(f"[INFO] filter batch {idx}/{total_batches} dispatch docs={len(batch)}")
            pending[executor.submit(
                _filter_batch,
                idx,
                batch,
                api_key,
                requirements,
                filter_model,
                step_input.max_output_tokens,
                schema,
                debug_dir,
            )] = (idx, batch)

        for future in as_completed(pending):
            idx, batch = pending[future]
            try:
                _, batch_docs, results = future.result()
            except Exception as exc:
                log(f"[WARN] filter batch {idx}/{total_batches} failed: {exc}")
                failed_docs.extend(batch)
                continue
            stats.batches_succeeded += 1
            log(f"[INFO] filter batch {idx}/{total_batches} docs={len(batch_docs)} completed")
            for item in results:
                merge_filter_result(merged, item, requirement_by_index)

    if failed_docs:
        log(f"[WARN] {len(failed_docs)} papers lost due to failed batches")

    scored_items = sorted(merged.values(), key=lambda x: x.get("score", 0), reverse=True)
    stats.scored_papers = len(scored_items)
    stats.failed_papers = len(docs) - len(scored_items)

    output = LLMRefineStepOutput(
        run_date=step_input.run_date,
        scored_items=[
            ScoredItem(
                paper_id=_norm_text(item.get("paper_id")),
                score=_coerce_score(item.get("score")),
                evidence_en=_norm_text(item.get("evidence_en")),
                evidence_cn=_norm_text(item.get("evidence_cn")),
                tldr_en=_norm_text(item.get("tldr_en")),
                tldr_cn=_norm_text(item.get("tldr_cn")),
                matched_requirement_index=_coerce_int(item.get("matched_requirement_index"), 0),
                matched_query_tag=_norm_text(item.get("matched_query_tag")),
                matched_query_text=_norm_text(item.get("matched_query_text")),
            )
            for item in scored_items
        ],
        artifacts=LLMRefineArtifacts(output_path=output_path),
        stats=stats,
        warnings=warnings,
    )
    write_llm_refine_output(output_path, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step 4 LLM refine papers for a single run date")
    parser.add_argument("--run-date", required=False, help="Run date in YYYY-MM-DD or YYYYMMDD")
    parser.add_argument(
        "--input-path-override",
        default=None,
        help="Optional Step 3 rerank input path override",
    )
    parser.add_argument(
        "--output-path-override",
        default=None,
        help="Optional Step 4 output path override",
    )
    parser.add_argument(
        "--min-star",
        type=int,
        default=4,
        help="Minimum star_rating to include as candidate",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="Batch size for LLM calls",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=MAX_CHARS_PER_DOC,
        help="Max chars per document (title + abstract)",
    )
    parser.add_argument(
        "--filter-model",
        default=os.getenv("FILTER_MODEL") or DEFAULT_FILTER_MODEL,
        help="LLM model for filtering",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=4000,
        help="Max output tokens for LLM",
    )
    parser.add_argument(
        "--filter-concurrency",
        type=int,
        default=DEFAULT_FILTER_CONCURRENCY,
        help="Concurrent LLM batch workers",
    )
    parser.add_argument(
        "--print-todos",
        action="store_true",
        help="Print Step 4 follow-up notes and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.print_todos:
        for idx, item in enumerate(STEP4_NOTES, start=1):
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
    rerank_output = load_rerank_output(input_path)
    step_input = LLMRefineStepInput(
        run_date=run_date,
        rerank_output=rerank_output,
        min_star=max(int(args.min_star), 0),
        batch_size=max(int(args.batch_size), 1),
        max_chars=max(int(args.max_chars), 100),
        filter_model=_norm_text(args.filter_model) or DEFAULT_FILTER_MODEL,
        max_output_tokens=max(int(args.max_output_tokens), 256),
        filter_concurrency=max(int(args.filter_concurrency), 1),
        output_path_override=Path(args.output_path_override).resolve() if args.output_path_override else None,
    )

    log("Step 4 v2 starting")
    log(f"run_date={run_date.isoformat()}")
    log(f"input_path={input_path}")
    output = run_llm_refine_step(context, step_input)
    log(f"requirements={output.stats.requirements_count}")
    log(f"candidate_papers={output.stats.candidate_papers}")
    log(f"scored_papers={output.stats.scored_papers}")
    log(f"failed_papers={output.stats.failed_papers}")
    log(f"batches_dispatched={output.stats.batches_dispatched}")
    log(f"batches_succeeded={output.stats.batches_succeeded}")
    log(f"recovery_attempts={output.stats.recovery_attempts}")
    log(f"output_path={output.artifacts.output_path}")
    if output.warnings:
        log(f"warnings={len(output.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
