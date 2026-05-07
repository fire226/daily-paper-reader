#!/usr/bin/env python3
"""Step 5 select papers for deep dive and quick skim."""

from __future__ import annotations

import argparse
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

from subscription_plan import count_subscription_tags

from step1_fetch import RunContext, build_run_context, log, read_json_file, resolve_run_date
from step3_rerank import RerankStepOutput, load_rerank_output
from step4_llm_refine import LLMRefineStepOutput, ScoredItem, load_llm_refine_output

PRIORITY_DEEP_SCORE = 9.0

MODES: dict[str, dict[str, Any]] = {
    "standard": {
        "quick_base": 10,
        "quick_strategy": "uniform",
        "deep_unlimited": False,
        "deep_base": 5,
        "deep_strategy": "round_robin",
    },
    "extend": {
        "quick_base": 15,
        "quick_strategy": "uniform",
        "deep_unlimited": False,
        "deep_base": 10,
        "deep_strategy": "round_robin",
    },
    "spark": {
        "quick_base": 10,
        "quick_strategy": "low_bias",
        "deep_unlimited": False,
        "deep_base": 5,
        "deep_strategy": "round_robin",
    },
    "skims": {
        "all_quick_min_score": 8.0,
    },
}

STEP5_NOTES = [
    "Add unit tests for score layering, round-robin, and allocation strategies",
    "Decide whether mode configs should be exposable via config.yaml in the future",
]


@dataclass(slots=True)
class SelectStats:
    mode: str = ""
    tag_count: int = 0
    deep_dive_candidates: int = 0
    deep_cap: int | None = None
    deep_selected: int = 0
    quick_candidates: int = 0
    quick_skim_target: int | None = None
    quick_selected: int = 0


@dataclass(slots=True)
class SelectArtifacts:
    output_path: Path | None = None


@dataclass(slots=True)
class SelectStepInput:
    run_date: date
    llm_output: LLMRefineStepOutput
    rerank_output: RerankStepOutput
    mode: str = "standard"
    output_path_override: Path | None = None


@dataclass(slots=True)
class SelectStepOutput:
    run_date: date | None = None
    deep_dive: list[dict[str, Any]] = field(default_factory=list)
    quick_skim: list[dict[str, Any]] = field(default_factory=list)
    artifacts: SelectArtifacts = field(default_factory=SelectArtifacts)
    stats: SelectStats = field(default_factory=SelectStats)
    warnings: list[str] = field(default_factory=list)


def _norm_text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_score(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def resolve_output_path(context: RunContext, step_input: SelectStepInput) -> Path:
    if step_input.output_path_override is not None:
        return step_input.output_path_override
    token = step_input.run_date.strftime("%Y%m%d")
    return context.archive_root / token / "recommend" / f"arxiv_papers_{token}.{step_input.mode}.json"


def load_mode_config(config: dict[str, Any]) -> tuple[str, int]:
    setting = ((config or {}).get("arxiv_paper_setting") or {})
    mode = _norm_text(setting.get("mode")) or "standard"
    tag_count, _ = count_subscription_tags(config or {})
    return mode, tag_count


def build_paper_map(papers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    paper_map: dict[str, dict[str, Any]] = {}
    for p in papers:
        pid = _norm_text(p.get("id"))
        if pid:
            paper_map[pid] = p
    return paper_map


def build_scored_papers(
    scored_items: list[ScoredItem],
    paper_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in scored_items:
        pid = _norm_text(item.paper_id)
        if not pid or pid not in paper_map:
            continue
        score = float(item.score)
        prev = merged.get(pid)
        if prev is not None and score <= float(prev.get("llm_score", 0)):
            continue
        paper = dict(paper_map[pid])
        paper["llm_score"] = score
        paper["llm_evidence_en"] = _norm_text(item.evidence_en)
        paper["llm_evidence_cn"] = _norm_text(item.evidence_cn)
        paper["llm_evidence"] = paper["llm_evidence_cn"] or paper["llm_evidence_en"]
        paper["llm_tldr_en"] = _norm_text(item.tldr_en)
        paper["llm_tldr_cn"] = _norm_text(item.tldr_cn) or paper["llm_tldr_en"]
        paper["llm_tldr"] = paper["llm_tldr_cn"]
        paper["matched_query_tag"] = _norm_text(item.matched_query_tag)
        paper["matched_query_text"] = _norm_text(item.matched_query_text)
        paper["selection_source"] = "fresh_fetch"
        merged[pid] = paper
    return sorted(merged.values(), key=lambda x: (-float(x.get("llm_score", 0)), str(x.get("id") or "")))


def sort_by_score(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: (-float(x.get("llm_score", 0)), str(x.get("id") or "")))


def split_score_layers(
    candidates: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    layers: list[tuple[str, list[dict[str, Any]]]] = []
    high = [p for p in candidates if float(p.get("llm_score", 0)) >= 8.0]
    if high:
        layers.append(("8plus", sort_by_score(high)))
    mid = [p for p in candidates if 7.0 <= float(p.get("llm_score", 0)) < 8.0]
    layers.append(("7", sort_by_score(mid)))
    low = [p for p in candidates if 6.0 <= float(p.get("llm_score", 0)) < 7.0]
    layers.append(("6", sort_by_score(low)))
    return layers


def build_tag_map(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    tag_map: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        tags = item.get("llm_tags") or [item.get("matched_query_tag") or ""]
        tags = [t for t in tags if t]
        if not tags:
            tags = ["untagged"]
        for tag in tags:
            tag_map.setdefault(tag, []).append(item)
    for tag in tag_map:
        tag_map[tag] = sort_by_score(tag_map[tag])
    return tag_map


def round_robin_select(candidates: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    if cap <= 0:
        return []
    tag_map = build_tag_map(candidates)
    if not tag_map:
        return []

    tag_order = sorted(
        tag_map.keys(),
        key=lambda t: (-float(tag_map[t][0].get("llm_score", 0)), t),
    )

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    indices = {tag: 0 for tag in tag_order}

    while len(selected) < cap:
        added = False
        for tag in tag_order:
            items = tag_map[tag]
            idx = indices[tag]
            while idx < len(items) and items[idx].get("id") in selected_ids:
                idx += 1
            if idx < len(items):
                selected.append(items[idx])
                selected_ids.add(items[idx].get("id"))
                indices[tag] = idx + 1
                added = True
                if len(selected) >= cap:
                    break
        if not added:
            break
    return selected


def allocate_uniform(
    layers: list[tuple[str, list[dict[str, Any]]]],
    target: int,
) -> dict[str, list[dict[str, Any]]]:
    if target <= 0:
        return {name: [] for name, _ in layers}
    num_layers = len(layers)
    base = target // num_layers if num_layers else 0
    remainder = target % num_layers if num_layers else 0

    quotas: dict[str, int] = {}
    for idx, (name, _items) in enumerate(layers):
        quotas[name] = base + (1 if idx < remainder else 0)

    selected: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in layers}
    remaining = target
    for name, items in layers:
        take = min(len(items), quotas[name])
        selected[name] = items[:take]
        remaining -= take

    if remaining > 0:
        for name, items in layers:
            if remaining <= 0:
                break
            extra = items[len(selected[name]):]
            if not extra:
                continue
            take = min(len(extra), remaining)
            selected[name].extend(extra[:take])
            remaining -= take

    return selected


def allocate_low_bias(
    layers: list[tuple[str, list[dict[str, Any]]]],
    target: int,
    low_ratio: float = 0.7,
) -> dict[str, list[dict[str, Any]]]:
    if target <= 0:
        return {name: [] for name, _ in layers}

    tier_names = [name for name, _ in layers]
    quotas: dict[str, int] = {name: 0 for name in tier_names}

    if "6" in tier_names:
        low_quota = int(round(target * low_ratio))
        quotas["6"] = low_quota
        remaining = max(target - low_quota, 0)
        others = [n for n in tier_names if n != "6"]
    else:
        remaining = target
        others = tier_names[:]

    if others:
        base = remaining // len(others)
        rem = remaining % len(others)
        for idx, name in enumerate(others):
            quotas[name] += base + (1 if idx < rem else 0)

    selected: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in layers}
    remaining = target
    for name, items in layers:
        take = min(len(items), quotas.get(name, 0))
        selected[name] = items[:take]
        remaining -= take

    if remaining > 0:
        for name, items in layers:
            if remaining <= 0:
                break
            extra = items[len(selected[name]):]
            if not extra:
                continue
            take = min(len(extra), remaining)
            selected[name].extend(extra[:take])
            remaining -= take

    return selected


def interleave_layers(
    selected_by_layer: dict[str, list[dict[str, Any]]],
    order: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    idx = {name: 0 for name in order}
    added = True
    while added:
        added = False
        for name in order:
            items = selected_by_layer.get(name) or []
            if idx[name] < len(items):
                result.append(items[idx[name]])
                idx[name] += 1
                added = True
    return result


def select_deep_dive(candidates: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    if cap <= 0:
        return []
    return round_robin_select(candidates, cap)


def select_quick_skim(
    candidates: list[dict[str, Any]],
    target: int,
    strategy: str,
) -> list[dict[str, Any]]:
    layers = split_score_layers(candidates)
    order = [name for name, _ in layers]

    if strategy == "low_bias":
        selected_by_layer = allocate_low_bias(layers, target)
    else:
        selected_by_layer = allocate_uniform(layers, target)

    marked: dict[str, list[dict[str, Any]]] = {}
    for name, items in selected_by_layer.items():
        marked[name] = [dict(item, quick_tier=name) for item in items]

    return interleave_layers(marked, order)[:target]


def sanitize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        copied.pop("_source", None)
        cleaned.append(copied)
    return cleaned


def write_select_output(path: Path, output: SelectStepOutput) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_date": output.run_date.isoformat() if output.run_date else "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": output.stats.mode,
        "deep_dive": output.deep_dive,
        "quick_skim": output.quick_skim,
        "stats": asdict(output.stats),
        "warnings": list(output.warnings),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_select_output(path: Path) -> SelectStepOutput:
    payload = read_json_file(path)
    run_date_raw = str(payload.get("run_date") or "").strip()
    run_date = resolve_run_date(run_date_raw) if run_date_raw else None
    stats_payload = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    return SelectStepOutput(
        run_date=run_date,
        deep_dive=list(payload.get("deep_dive") or []),
        quick_skim=list(payload.get("quick_skim") or []),
        artifacts=SelectArtifacts(output_path=path),
        stats=SelectStats(**stats_payload) if isinstance(stats_payload, dict) else SelectStats(),
        warnings=[str(item) for item in (payload.get("warnings") or []) if str(item).strip()],
    )


def run_select_step(context: RunContext, step_input: SelectStepInput) -> SelectStepOutput:
    output_path = resolve_output_path(context, step_input)
    warnings: list[str] = []
    mode = step_input.mode
    mode_cfg = MODES.get(mode)
    if mode_cfg is None:
        warnings.append(f"Unknown mode '{mode}', falling back to standard")
        log(f"[WARN] Unknown mode '{mode}', falling back to standard")
        mode = "standard"
        mode_cfg = MODES["standard"]

    _, tag_count = load_mode_config(context.config)

    paper_map = build_paper_map(
        [asdict(paper) for paper in step_input.rerank_output.papers]
    )
    candidates = build_scored_papers(step_input.llm_output.scored_items, paper_map)

    log(
        f"Select run_date={step_input.run_date.isoformat()} mode={mode} "
        f"candidates={len(candidates)} tag_count={tag_count}"
    )

    if not candidates:
        warnings.append("No scored candidates available")
        log("[WARN] No scored candidates available, writing empty result")
        return SelectStepOutput(
            run_date=step_input.run_date,
            artifacts=SelectArtifacts(output_path=output_path),
            stats=SelectStats(mode=mode, tag_count=tag_count),
            warnings=warnings,
        )

    if mode_cfg.get("all_quick_min_score") is not None:
        threshold = float(mode_cfg["all_quick_min_score"])
        picked = [p for p in candidates if float(p.get("llm_score", 0)) >= threshold]
        picked = sort_by_score(picked)
        stats = SelectStats(
            mode=mode,
            tag_count=tag_count,
            deep_dive_candidates=0,
            deep_cap=None,
            deep_selected=0,
            quick_candidates=len(picked),
            quick_skim_target=None,
            quick_selected=len(picked),
        )
        output = SelectStepOutput(
            run_date=step_input.run_date,
            deep_dive=[],
            quick_skim=sanitize_items(picked),
            artifacts=SelectArtifacts(output_path=output_path),
            stats=stats,
            warnings=warnings,
        )
        write_select_output(output_path, output)
        return output

    deep_candidates = [p for p in candidates if float(p.get("llm_score", 0)) >= 8.0]
    deep_candidates = sort_by_score(deep_candidates)

    priority_deep = [p for p in deep_candidates if float(p.get("llm_score", 0)) >= PRIORITY_DEEP_SCORE]
    regular_deep = [p for p in deep_candidates if float(p.get("llm_score", 0)) < PRIORITY_DEEP_SCORE]

    cap: int | None = None
    deep_selected: list[dict[str, Any]] = []
    if mode_cfg.get("deep_unlimited"):
        deep_selected = priority_deep + regular_deep
    else:
        deep_base = int(mode_cfg.get("deep_base") or 0)
        cap = deep_base + tag_count
        need = max(cap - len(priority_deep), 0)
        if need == 0 or not regular_deep:
            deep_selected = priority_deep
        elif len(priority_deep) >= cap:
            deep_selected = priority_deep
        else:
            extra_selected = select_deep_dive(regular_deep, need)
            deep_selected = priority_deep + extra_selected

    selected_ids = {p.get("id") for p in deep_selected}
    deep_overflow = [p for p in deep_candidates if p.get("id") not in selected_ids]

    quick_candidates = [
        p for p in candidates
        if p.get("id") not in selected_ids and 6.0 <= float(p.get("llm_score", 0)) < 8.0
    ]
    if deep_overflow:
        quick_map = {p.get("id"): p for p in quick_candidates}
        for item in deep_overflow:
            pid = item.get("id")
            if pid not in quick_map:
                quick_candidates.append(item)

    quick_base = int(mode_cfg.get("quick_base") or 0)
    quick_target = quick_base + tag_count
    quick_strategy = str(mode_cfg.get("quick_strategy") or "uniform")
    quick_selected = select_quick_skim(quick_candidates, quick_target, quick_strategy)

    stats = SelectStats(
        mode=mode,
        tag_count=tag_count,
        deep_dive_candidates=len(deep_candidates),
        deep_cap=cap,
        deep_selected=len(deep_selected),
        quick_candidates=len(quick_candidates),
        quick_skim_target=quick_target,
        quick_selected=len(quick_selected),
    )

    output = SelectStepOutput(
        run_date=step_input.run_date,
        deep_dive=sanitize_items(deep_selected),
        quick_skim=sanitize_items(quick_selected),
        artifacts=SelectArtifacts(output_path=output_path),
        stats=stats,
        warnings=warnings,
    )
    write_select_output(output_path, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step 5 select papers for deep dive and quick skim")
    parser.add_argument("--run-date", required=False, help="Run date in YYYY-MM-DD or YYYYMMDD")
    parser.add_argument(
        "--llm-input-path-override",
        default=None,
        help="Optional Step 4 LLM output path override",
    )
    parser.add_argument(
        "--rerank-input-path-override",
        default=None,
        help="Optional Step 3 rerank output path override",
    )
    parser.add_argument(
        "--output-path-override",
        default=None,
        help="Optional output path override",
    )
    parser.add_argument(
        "--modes",
        default=None,
        help="Comma-separated modes (standard,extend,spark,skims). Default: config or standard,extend,spark",
    )
    parser.add_argument(
        "--print-todos",
        action="store_true",
        help="Print Step 5 follow-up notes and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.print_todos:
        for idx, item in enumerate(STEP5_NOTES, start=1):
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

    llm_input_path = (
        Path(args.llm_input_path_override).resolve()
        if args.llm_input_path_override
        else context.archive_root / token / "rank" / f"arxiv_papers_{token}.llm.json"
    )
    rerank_input_path = (
        Path(args.rerank_input_path_override).resolve()
        if args.rerank_input_path_override
        else context.archive_root / token / "rank" / f"arxiv_papers_{token}.rerank.json"
    )

    llm_output = load_llm_refine_output(llm_input_path)
    rerank_output = load_rerank_output(rerank_input_path)

    mode_text = args.modes
    if not mode_text:
        setting = ((context.config or {}).get("arxiv_paper_setting") or {})
        mode_text = _norm_text(setting.get("mode")) or "standard,extend,spark"

    modes = [m.strip() for m in str(mode_text or "").split(",") if m.strip()]
    modes = [m for m in modes if m in MODES]
    if not modes:
        parser.error("modes must include at least one of: standard, extend, spark, skims")

    log("Step 5 v2 starting")
    log(f"run_date={run_date.isoformat()}")
    log(f"modes={modes}")

    for mode in modes:
        step_input = SelectStepInput(
            run_date=run_date,
            llm_output=llm_output,
            rerank_output=rerank_output,
            mode=mode,
            output_path_override=(
                Path(args.output_path_override).resolve()
                if args.output_path_override and len(modes) == 1
                else None
            ),
        )
        output = run_select_step(context, step_input)
        log(
            f"mode={mode} deep={output.stats.deep_selected} quick={output.stats.quick_selected} "
            f"cap={output.stats.deep_cap} target={output.stats.quick_skim_target}"
        )
        log(f"output_path={output.artifacts.output_path}")
        if output.warnings:
            log(f"warnings={len(output.warnings)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
