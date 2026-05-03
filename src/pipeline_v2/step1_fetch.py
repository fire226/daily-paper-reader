#!/usr/bin/env python3
"""Step 1 fetch papers published on a single run date.

This module is the day-scoped Step 1 implementation for the v2 pipeline.
It fetches arXiv metadata, writes the raw paper pool, and updates local fetch state.
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import arxiv

try:
    import yaml
except Exception:  # pragma: no cover - optional import guard
    yaml = None


FetchBackend = Literal["arxiv", "unknown"]
CATEGORIES_TO_FETCH = [
    "cs",
    "math",
    "stat",
    "q-bio",
    "q-fin",
    "eess",
    "econ",
    "physics",
    "cond-mat",
    "hep-ph",
    "hep-th",
    "gr-qc",
    "astro-ph",
]


@dataclass(slots=True)
class RunContext:
    root_dir: Path
    archive_root: Path
    config_path: Path
    crawl_state_path: Path
    seen_state_path: Path
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FetchStepInput:
    run_date: date
    ignore_seen: bool = False
    output_path_override: Path | None = None
    categories_override: list[str] | None = None


@dataclass(slots=True)
class PaperRecord:
    # Core fields: required by the filtering pipeline.
    id: str
    source: str
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    primary_category: str | None = None
    categories: list[str] = field(default_factory=list)
    published: str | None = None
    link: str | None = None

    # Optional extension fields: arXiv-side metadata.
    updated_at: str | None = None
    version: str | None = None


@dataclass(slots=True)
class FetchStats:
    total_papers: int = 0
    deduplicated_papers: int = 0
    categories_used: list[str] = field(default_factory=list)
    queries_attempted: int = 0
    query_failures: int = 0


@dataclass(slots=True)
class FetchStateUpdates:
    crawl_state: dict[str, Any] | None = None
    seen_state: dict[str, Any] | None = None


@dataclass(slots=True)
class FetchLoadedState:
    crawl_state: dict[str, Any] = field(default_factory=dict)
    seen_state: dict[str, Any] = field(default_factory=dict)
    last_crawl_at: datetime | None = None
    latest_published_at: datetime | None = None
    seen_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class FetchArtifacts:
    raw_output_path: Path | None = None


@dataclass(slots=True)
class FetchStepOutput:
    run_date: date | None = None
    papers: list[PaperRecord] = field(default_factory=list)
    backend: FetchBackend = "unknown"
    artifacts: FetchArtifacts = field(default_factory=FetchArtifacts)
    stats: FetchStats = field(default_factory=FetchStats)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ArxivFetchResult:
    raw_papers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    queries_attempted: int = 0
    query_failures: int = 0


STEP1_NOTES = [
    "Add unit tests for run-date parsing, state loading, and state updates",
    "Decide whether Step 1 should expose config-derived defaults or stay fully caller-driven",
    "Decide whether state files should move under archive/<run_date>/ or remain global",
]


def log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


def load_config(config_path: Path) -> dict[str, Any]:
    """Load config.yaml when available.

    Step 1 v2 is still caller-driven; config is loaded only for optional future use.
    """
    if not config_path.exists():
        return {}
    if yaml is None:
        return {}
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def build_run_context(root_dir: Path) -> RunContext:
    config_path = root_dir / "config.yaml"
    return RunContext(
        root_dir=root_dir,
        archive_root=root_dir / "archive",
        config_path=config_path,
        crawl_state_path=root_dir / "archive" / "crawl_state.json",
        seen_state_path=root_dir / "archive" / "arxiv_seen.json",
        config=load_config(config_path),
    )


def resolve_run_date(value: str) -> date:
    text = str(value or "").strip()
    if not text:
        raise ValueError("run date is required")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("run date must use YYYY-MM-DD or YYYYMMDD") from exc


def build_day_bounds(run_date: date) -> tuple[datetime, datetime]:
    start_dt = datetime(run_date.year, run_date.month, run_date.day, tzinfo=timezone.utc)
    return start_dt, start_dt + timedelta(days=1)


def resolve_output_path(context: RunContext, step_input: FetchStepInput) -> Path:
    if step_input.output_path_override is not None:
        return step_input.output_path_override
    run_date_token = step_input.run_date.strftime("%Y%m%d")
    return (
        context.archive_root
        / run_date_token
        / "raw"
        / f"arxiv_papers_{run_date_token}.json"
    )


def parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def load_fetch_state(context: RunContext, step_input: FetchStepInput) -> FetchLoadedState:
    """Load persisted fetch state.

    New Step 1 behavior:
    - when ignore_seen=true, use an empty state
    - otherwise read crawl_state.json and arxiv_seen.json if present
    """
    if step_input.ignore_seen:
        return FetchLoadedState()

    crawl_state = read_json_file(context.crawl_state_path)
    seen_state = read_json_file(context.seen_state_path)
    raw_ids = seen_state.get("ids") if isinstance(seen_state.get("ids"), list) else []
    seen_ids = {str(item).strip() for item in raw_ids if str(item).strip()}
    return FetchLoadedState(
        crawl_state=crawl_state if isinstance(crawl_state, dict) else {},
        seen_state=seen_state if isinstance(seen_state, dict) else {},
        last_crawl_at=parse_iso_datetime(crawl_state.get("last_crawl_at")) if isinstance(crawl_state, dict) else None,
        latest_published_at=parse_iso_datetime(seen_state.get("latest_published_at")) if isinstance(seen_state, dict) else None,
        seen_ids=seen_ids,
    )


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_arxiv_query(window_start: datetime, window_end: datetime, category: str) -> str:
    start_str = normalize_datetime(window_start).strftime("%Y%m%d%H%M")
    end_str = normalize_datetime(window_end).strftime("%Y%m%d%H%M")
    return f"cat:{category}* AND submittedDate:[{start_str} TO {end_str}]"


def raw_version_from_id(paper_id: str) -> str | None:
    value = str(paper_id or "").strip().lower()
    if "v" not in value:
        return None
    suffix = value.rsplit("v", 1)[-1]
    return f"v{suffix}" if suffix.isdigit() else None


def fetch_from_arxiv(
    step_input: FetchStepInput,
    loaded_state: FetchLoadedState,
) -> ArxivFetchResult:
    """Fetch papers from arXiv API.

    This implementation intentionally fetches metadata only.
    """
    window_start, window_end = build_day_bounds(step_input.run_date)
    client = arxiv.Client(page_size=200, delay_seconds=3.0, num_retries=5)
    unique_papers: dict[str, dict[str, Any]] = {}
    seen_ids = set(loaded_state.seen_ids)
    warnings: list[str] = []
    queries_attempted = 0
    query_failures = 0
    categories = step_input.categories_override or CATEGORIES_TO_FETCH

    log(f"arXiv fetch run_date={step_input.run_date.isoformat()} categories={len(categories)}")
    for category in categories:
        query = build_arxiv_query(window_start, window_end, category)
        search = arxiv.Search(
            query=query,
            max_results=None,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        log(
            "fetching "
            f"category={category} "
            f"{window_start.strftime('%Y%m%d%H%M')}..{window_end.strftime('%Y%m%d%H%M')}"
        )
        queries_attempted += 1
        try:
            for result in client.results(search):
                paper_id = str(result.get_short_id() or "").strip()
                if not paper_id or paper_id in unique_papers:
                    continue
                if paper_id in seen_ids:
                    continue
                pdf_link = getattr(result, "pdf_url", None) or result.entry_id
                unique_papers[paper_id] = {
                    "id": paper_id,
                    "source": "arxiv",
                    "title": str(result.title or "").replace("\n", " ").strip(),
                    "abstract": str(result.summary or "").replace("\n", " ").strip(),
                    "authors": [str(author.name or "").strip() for author in (result.authors or []) if str(author.name or "").strip()],
                    "primary_category": str(getattr(result, "primary_category", None) or "").strip() or None,
                    "categories": [str(item or "").strip() for item in (getattr(result, "categories", None) or []) if str(item or "").strip()],
                    "published": normalize_datetime(result.published).isoformat() if getattr(result, "published", None) else None,
                    "updated_at": normalize_datetime(result.updated).isoformat() if getattr(result, "updated", None) else None,
                    "link": str(pdf_link or "").strip() or None,
                    "version": raw_version_from_id(paper_id),
                }
                seen_ids.add(paper_id)
        except Exception as exc:
            query_failures += 1
            warning = (
                "arXiv query failed "
                f"category={category} run_date={step_input.run_date.isoformat()} "
                f"error={exc}"
            )
            warnings.append(warning)
            log(f"warn={warning}")

    return ArxivFetchResult(
        raw_papers=list(unique_papers.values()),
        warnings=warnings,
        queries_attempted=queries_attempted,
        query_failures=query_failures,
    )


def normalize_papers(_raw_papers: list[dict[str, Any]]) -> list[PaperRecord]:
    """Normalize source payloads to PaperRecord.

    This function deduplicates by id and normalizes nullable fields.
    """
    normalized: dict[str, PaperRecord] = {}
    for raw in _raw_papers:
        if not isinstance(raw, dict):
            continue
        paper_id = str(raw.get("id") or "").strip()
        if not paper_id:
            continue
        if paper_id in normalized:
            continue
        authors = raw.get("authors") if isinstance(raw.get("authors"), list) else []
        categories = raw.get("categories") if isinstance(raw.get("categories"), list) else []
        normalized[paper_id] = PaperRecord(
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
        )
    return list(normalized.values())


def build_state_updates(
    step_input: FetchStepInput,
    loaded_state: FetchLoadedState,
    papers: list[PaperRecord],
) -> FetchStateUpdates:
    """Build state updates without writing them yet.

    State is derived only from the current run plus loaded state.
    """
    _, window_end = build_day_bounds(step_input.run_date)
    finished_at = normalize_datetime(window_end).isoformat()
    base_seen_ids = set(loaded_state.seen_ids)

    latest_published_at: datetime | None = loaded_state.latest_published_at
    new_seen_ids = set(base_seen_ids)
    for paper in papers:
        new_seen_ids.add(paper.id)
        published_dt = parse_iso_datetime(paper.published)
        if published_dt and (latest_published_at is None or published_dt > latest_published_at):
            latest_published_at = published_dt

    seen_state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "latest_published_at": latest_published_at.isoformat() if latest_published_at else "",
        "ids": sorted(new_seen_ids),
    }
    crawl_state = {"last_crawl_at": finished_at}
    return FetchStateUpdates(crawl_state=crawl_state, seen_state=seen_state)


def write_state_updates(context: RunContext, state_updates: FetchStateUpdates) -> None:
    if state_updates.crawl_state is not None:
        context.crawl_state_path.parent.mkdir(parents=True, exist_ok=True)
        context.crawl_state_path.write_text(
            json.dumps(state_updates.crawl_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if state_updates.seen_state is not None:
        context.seen_state_path.parent.mkdir(parents=True, exist_ok=True)
        context.seen_state_path.write_text(
            json.dumps(state_updates.seen_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def write_raw_output(raw_output_path: Path, papers: list[PaperRecord]) -> None:
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(paper) for paper in papers]
    raw_output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_fetch_step(context: RunContext, step_input: FetchStepInput) -> FetchStepOutput:
    """Fetch the run-date paper pool and persist Step 1 artifacts."""
    raw_output_path = resolve_output_path(context, step_input)

    # Load state once at the top so downstream helpers stay pure.
    loaded_state = load_fetch_state(context, step_input)
    if step_input.ignore_seen:
        log("state=ignore_seen (effective state disabled)")
    else:
        log(
            "state="
            f"seen_ids={len(loaded_state.seen_ids)} "
            f"last_crawl_at={loaded_state.last_crawl_at.isoformat() if loaded_state.last_crawl_at else 'none'} "
            f"latest_published_at={loaded_state.latest_published_at.isoformat() if loaded_state.latest_published_at else 'none'}"
        )

    fetch_result = fetch_from_arxiv(step_input, loaded_state)
    backend: FetchBackend = "arxiv"
    categories_used = step_input.categories_override or list(CATEGORIES_TO_FETCH)

    papers = normalize_papers(fetch_result.raw_papers)
    state_updates = build_state_updates(step_input, loaded_state, papers)

    write_raw_output(raw_output_path, papers)
    write_state_updates(context, state_updates)

    stats = FetchStats(
        total_papers=len(papers),
        deduplicated_papers=len(papers),
        categories_used=categories_used,
        queries_attempted=fetch_result.queries_attempted,
        query_failures=fetch_result.query_failures,
    )
    return FetchStepOutput(
        run_date=step_input.run_date,
        papers=papers,
        backend=backend,
        artifacts=FetchArtifacts(
            raw_output_path=raw_output_path,
        ),
        stats=stats,
        warnings=fetch_result.warnings,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Step 1 fetch papers for a single run date")
    parser.add_argument("--run-date", required=False, help="Run date in YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--ignore-seen", action="store_true", help="Ignore persisted seen/crawl state")
    parser.add_argument("--output-path-override", default=None, help="Optional raw output path override")
    parser.add_argument(
        "--categories",
        default=None,
        help="Optional comma-separated arXiv categories override",
    )
    parser.add_argument(
        "--print-todos",
        action="store_true",
        help="Print Step 1 follow-up notes and exit",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.print_todos:
        for idx, item in enumerate(STEP1_NOTES, start=1):
            print(f"{idx}. {item}")
        return 0

    if not args.run_date:
        parser.error("the following arguments are required: --run-date")

    root_dir = Path(__file__).resolve().parents[2]
    context = build_run_context(root_dir)
    try:
        categories_override = None
        if args.categories:
            categories_override = [item.strip() for item in str(args.categories).split(",") if item.strip()]
        step_input = FetchStepInput(
            run_date=resolve_run_date(args.run_date),
            ignore_seen=bool(args.ignore_seen),
            output_path_override=Path(args.output_path_override).resolve() if args.output_path_override else None,
            categories_override=categories_override,
        )
    except ValueError as exc:
        parser.error(str(exc))

    log("Step 1 v2 starting")
    log(f"run_date={step_input.run_date.isoformat()}")
    log("backend=arxiv")

    output = run_fetch_step(context, step_input)
    log(f"backend={output.backend}")
    log(f"papers={output.stats.total_papers}")
    if output.warnings:
        log(f"warnings={len(output.warnings)}")
    log(f"raw_output_path={output.artifacts.raw_output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
