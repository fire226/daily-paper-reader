#!/usr/bin/env python3
"""
日期区间批量抓取入口脚本。
按天独立运行完整 pipeline（Step 2.1 → 2.2 → 2.3 → 4 → 5 → 6），
每天的结果落入 archive/{day}/（中间文件）和 docs/YYYY/MM/DD/（文档），与 main.py 共用同目录。

用法：
  python pipeline_range.py --start-date 20260401 --end-date 20260410
  python pipeline_range.py --start-date 20260401 --end-date 20260410 --force-existing
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SRC_DIR = os.path.join(os.path.dirname(__file__), "src")
ROOT_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(ROOT_DIR, "config.yaml")

# Make src/ importable
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def run_step(label, args_list, env=None):
    print(f"[INFO] {label}: {' '.join(args_list)}", flush=True)
    subprocess.run(args_list, check=True, env=env)


def parse_date(s):
    return datetime.strptime(s, "%Y%m%d").date()


def load_config():
    try:
        import yaml

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def resolve_summary_step_env():
    env = os.environ.copy()
    summary_api_key = (os.getenv("SUMMARY_API_KEY") or "").strip()
    summary_base_url = (os.getenv("SUMMARY_BASE_URL") or "").strip()
    summary_model = (os.getenv("SUMMARY_MODEL") or "deepseek/deepseek-v3.2").strip()
    if summary_api_key:
        env["LLM_API_KEY"] = summary_api_key
    if summary_base_url:
        env["LLM_BASE_URL"] = summary_base_url
    env["LLM_MODEL"] = summary_model
    return env


def backfill_missing_sidebar_entries(
    docs_dir: str, sidebar_path: str, python: str, env: dict
):
    """
    扫描 archive/ 下所有已有 README.md 的日期目录，
    将侧边栏中缺失的日期条目补全（通过 Step 6 --sidebar-only，不触发 LLM）。
    """
    DATE_MARKER_RE = re.compile(r"<!--dpr-date:(\d{8})-->")

    # 1. 解析侧边栏已有日期
    existing_dates: set = set()
    if os.path.exists(sidebar_path):
        with open(sidebar_path, encoding="utf-8") as f:
            content = f.read()
        existing_dates = set(DATE_MARKER_RE.findall(content))

    # 2. 扫描 archive/ 下有 docs 的日期
    archive_root = os.path.join(ROOT_DIR, "archive")
    all_dates_with_docs: list = []
    for date_dir in os.listdir(archive_root):
        if not re.fullmatch(r"\d{8}", date_dir):
            continue
        ym = date_dir[:6]
        day = date_dir[6:]
        day_readme = os.path.join(docs_dir, ym, day, "README.md")
        if os.path.exists(day_readme):
            all_dates_with_docs.append(date_dir)

    # 3. 取差集，逆序（从新到旧，避免旧日期覆盖新日期）
    missing_dates = sorted(set(all_dates_with_docs) - existing_dates, reverse=True)

    if not missing_dates:
        print("[INFO] 侧边栏补全：无缺失日期，跳过")
        return

    print(f"[INFO] 侧边栏补全：发现 {len(missing_dates)} 个缺失日期：{missing_dates}")

    for date_str in missing_dates:
        print(f"[INFO] 补全侧边栏：{date_str}")
        day_env = env.copy()
        day_env["DPR_RUN_DATE"] = date_str
        day_env["DPR_ARCHIVE_DIR"] = os.path.join(archive_root, date_str)
        day_env["DPR_SINGLE_DAY"] = "1"
        summary_env = resolve_summary_step_env()
        summary_env.update(day_env)
        run_step(
            f"Backfill Sidebar [{date_str}]",
            [python, os.path.join(SRC_DIR, "6.generate_docs.py"), "--sidebar-only"],
            env=summary_env,
        )


def main():
    parser = argparse.ArgumentParser(description="日期区间批量抓取 pipeline")
    parser.add_argument("--start-date", required=True, help="起始日期 YYYYMMDD")
    parser.add_argument("--end-date", required=True, help="结束日期 YYYYMMDD")
    parser.add_argument(
        "--force-existing",
        action="store_true",
        help="强制重拉已有结果的日期（忽略 skip 逻辑）",
    )
    parser.add_argument(
        "--embedding-device", default="cpu", help="Embedding 设备 (default: cpu)"
    )
    parser.add_argument(
        "--embedding-batch-size", type=int, default=8, help="Embedding 批大小"
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="每步保留的 Top K 论文数（传给 Step 2.1/2.2）",
    )
    parser.add_argument(
        "--min-star",
        type=int,
        default=None,
        help="Step 4 最低星级过滤（默认 4，设为 5 可大幅减少 LLM 调用）",
    )
    args = parser.parse_args()

    python = sys.executable
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if end_date < start_date:
        print(
            f"[ERROR] end-date ({args.end_date}) 不能早于 start-date ({args.start_date})",
            flush=True,
        )
        sys.exit(1)

    config = load_config()
    paper_setting = config.get("arxiv_paper_setting") or {}

    total_days = (end_date - start_date).days + 1
    print(
        f"[INFO] 日期区间抓取: {start_date} ~ {end_date} ({total_days} 天)", flush=True
    )
    print(f"[INFO] 中间文件: archive/", flush=True)
    print(f"[INFO] 文档输出: docs/", flush=True)

    # Load .env
    env = os.environ.copy()
    env_path = os.path.join(ROOT_DIR, ".env")
    if os.path.isfile(env_path):
        for line in open(env_path, encoding="utf-8").read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
            # Also set in current process for in-process imports (e.g. should_skip_rerank)
            os.environ.setdefault(k.strip(), v.strip())

    current = start_date
    day_index = 0
    while current <= end_date:
        day_index += 1
        day_str = current.strftime("%Y%m%d")
        day_archive_dir = os.path.join(ROOT_DIR, "archive", day_str)

        print(f"\n{'=' * 60}", flush=True)
        print(f"[INFO] [{day_index}/{total_days}] 处理日期: {day_str}", flush=True)
        print(f"[INFO] 中间文件: {day_archive_dir}", flush=True)
        print(f"{'=' * 60}\n", flush=True)

        # 检查是否已有完整输出（默认跳过，--force-existing 时重拉）
        if not args.force_existing:
            day_docs_dir = os.path.join(
                ROOT_DIR,
                "docs",
                current.strftime("%Y"),
                current.strftime("%m"),
                current.strftime("%d"),
            )
            if os.path.isdir(day_docs_dir) and any(
                f.endswith(".md") for f in os.listdir(day_docs_dir)
            ):
                print(
                    f"[INFO] 跳过 {day_str}：输出目录已存在且包含 .md 文件", flush=True
                )
                current += timedelta(days=1)
                continue

        # 设置环境变量
        day_env = env.copy()
        day_env["DPR_RUN_DATE"] = day_str
        day_env["DPR_ARCHIVE_DIR"] = day_archive_dir
        # DPR_SINGLE_DAY=1 告知各 Step 脚本：当前是 pipeline_range 单日模式，
        # 应严格按 DPR_RUN_DATE 指定的单日计算时间窗口，忽略 config.yaml 的 days_window。
        # 与 main.py 模式区分：main.py 使用单日 token 仅作目录标识，实际窗口由 days_window 决定。
        day_env["DPR_SINGLE_DAY"] = "1"

        # 每天 top_k 自适应：由 Step 2.1/2.2 自动检测，不需要手动设置

        # Build step-specific args
        top_k_args = ["--top-k", str(args.top_k)] if args.top_k else []

        # Step 1 - Fetch arXiv
        fetch_src = os.path.join(SRC_DIR, "maintain", "fetchers", "fetch_arxiv.py")
        run_step(
            f"Step 1 - Fetch [{day_str}]",
            [python, fetch_src],
            env=day_env,
        )

        # Step 2.1 - BM25
        run_step(
            f"Step 2.1 - BM25 [{day_str}]",
            [python, os.path.join(SRC_DIR, "2.1.retrieval_papers_bm25.py")]
            + top_k_args,
            env=day_env,
        )

        # Step 2.2 - Embedding
        run_step(
            f"Step 2.2 - Embedding [{day_str}]",
            [
                python,
                os.path.join(SRC_DIR, "2.2.retrieval_papers_embedding.py"),
                "--device",
                str(args.embedding_device),
                "--batch-size",
                str(args.embedding_batch_size),
            ]
            + top_k_args,
            env=day_env,
        )

        # Step 2.3 - RRF
        run_step(
            f"Step 2.3 - RRF [{day_str}]",
            [python, os.path.join(SRC_DIR, "2.3.retrieval_papers_rrf.py")],
            env=day_env,
        )

        # Step 3 - Rerank (跳过：区间模式下默认不走 rerank)
        # 与 main.py 一致：仅当未设置 LLM_BASE_URL 时才执行 rerank
        from utils import should_skip_rerank, prepare_rerank_fallback

        skip_rerank, rerank_base = should_skip_rerank()
        rrf_path = os.path.join(
            day_archive_dir, "filtered", f"arxiv_papers_{day_str}.json"
        )
        rerank_path = os.path.join(
            day_archive_dir, "rank", f"arxiv_papers_{day_str}.json"
        )
        if skip_rerank:
            print(
                f"[INFO] Step 3 - Rerank 已跳过 [{day_str}]: base={rerank_base}",
                flush=True,
            )
            prepare_rerank_fallback(rrf_path, rerank_path)
        else:
            run_step(
                f"Step 3 - Rerank [{day_str}]",
                [python, os.path.join(SRC_DIR, "3.rank_papers.py")],
                env=day_env,
            )

        # Step 4 - LLM refine（默认跳过已有结果，--force-existing 时重拉）
        llm_path = os.path.join(
            day_archive_dir, "rank", f"arxiv_papers_{day_str}.llm.json"
        )
        if not args.force_existing and os.path.exists(llm_path):
            print(
                f"[INFO] Step 4 - LLM refine 已跳过 [{day_str}]: 输出已存在", flush=True
            )
        else:
            step4_args = []
            if args.min_star is not None:
                step4_args = ["--min-star", str(args.min_star)]
            run_step(
                f"Step 4 - LLM refine [{day_str}]",
                [python, os.path.join(SRC_DIR, "4.llm_refine_papers.py")] + step4_args,
                env=day_env,
            )

        # Step 5 - Select (默认跳过已有结果，--force-existing 时重拉)
        recommend_path = os.path.join(
            day_archive_dir, "recommend", f"arxiv_papers_{day_str}.standard.json"
        )
        if not args.force_existing and os.path.exists(recommend_path):
            print(f"[INFO] Step 5 - Select 已跳过 [{day_str}]: 输出已存在", flush=True)
        else:
            run_step(
                f"Step 5 - Select [{day_str}]",
                [python, os.path.join(SRC_DIR, "5.select_papers.py")],
                env=day_env,
            )

        # Step 6 - Generate Docs（默认跳过已有结果，--force-existing 时重拉）
        day_docs_subdir = os.path.join(
            ROOT_DIR,
            "docs",
            current.strftime("%Y"),
            current.strftime("%m"),
            current.strftime("%d"),
        )
        if (
            not args.force_existing
            and os.path.isdir(day_docs_subdir)
            and any(f.endswith(".md") for f in os.listdir(day_docs_subdir))
        ):
            print(
                f"[INFO] Step 6 - Generate Docs 已跳过 [{day_str}]: 输出已存在",
                flush=True,
            )
        else:
            summary_env = resolve_summary_step_env()
            summary_env.update(day_env)
            run_step(
                f"Step 6 - Generate Docs [{day_str}]",
                [python, os.path.join(SRC_DIR, "6.generate_docs.py")],
                env=summary_env,
            )

        print(f"\n[INFO] ✅ {day_str} 完成 ({day_index}/{total_days})", flush=True)
        current += timedelta(days=1)

    # 补全侧边栏：将 archive/ 下已有文档但未写入侧边栏的日期补充进来
    sidebar_path = os.path.join(ROOT_DIR, "docs", "_sidebar.md")
    backfill_missing_sidebar_entries(
        os.path.join(ROOT_DIR, "docs"), sidebar_path, python, env
    )

    # 保存运行记录
    run_record = {
        "type": "range",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "status": "success",
        "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "total_days": total_days,
        "docs_dir": os.path.join(ROOT_DIR, "docs"),
    }
    record_path = os.path.join(ROOT_DIR, "data", "last_run.json")
    os.makedirs(os.path.dirname(record_path), exist_ok=True)
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(run_record, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 60}", flush=True)
    print(f"[INFO] 🎉 区间抓取全部完成！", flush=True)
    print(f"[INFO] 文档输出: docs/", flush=True)
    print(f"{'=' * 60}", flush=True)


if __name__ == "__main__":
    main()
