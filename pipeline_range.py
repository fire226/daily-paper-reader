#!/usr/bin/env python3
"""
日期区间批量抓取入口脚本。
按天独立运行完整 pipeline（Step 2.1 → 2.2 → 2.3 → 4 → 5 → 6），
每天完全独立，中间文件隔离在 data/range/{start}-{end}/{day}/ 下。

用法：
  python pipeline_range.py --start-date 20260401 --end-date 20260410
  python pipeline_range.py --start-date 20260401 --end-date 20260410 --skip-existing
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SRC_DIR = os.path.join(os.path.dirname(__file__), "src")
ROOT_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(ROOT_DIR, "config.yaml")


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
    summary_api_key = (os.getenv("SUMMARY_API_KEY") or os.getenv("BLT_SUMMARY_API_KEY") or "").strip()
    summary_base_url = (os.getenv("SUMMARY_BASE_URL") or os.getenv("BLT_SUMMARY_BASE_URL") or "").strip()
    summary_model = (os.getenv("SUMMARY_MODEL") or os.getenv("BLT_SUMMARY_MODEL") or "").strip()
    if summary_api_key:
        env["BLT_API_KEY"] = summary_api_key
    if summary_base_url:
        env["LLM_PRIMARY_BASE_URL"] = summary_base_url
        env["BLT_PRIMARY_BASE_URL"] = summary_base_url
        env["BLT_API_BASE"] = summary_base_url
    if summary_model:
        env["BLT_SUMMARY_MODEL"] = summary_model
    return env


def main():
    parser = argparse.ArgumentParser(description="日期区间批量抓取 pipeline")
    parser.add_argument("--start-date", required=True, help="起始日期 YYYYMMDD")
    parser.add_argument("--end-date", required=True, help="结束日期 YYYYMMDD")
    parser.add_argument("--skip-existing", action="store_true", help="跳过已有完整输出的天")
    parser.add_argument("--embedding-device", default="cpu", help="Embedding 设备 (default: cpu)")
    parser.add_argument("--embedding-batch-size", type=int, default=8, help="Embedding 批大小")
    args = parser.parse_args()

    python = sys.executable
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if end_date < start_date:
        print(f"[ERROR] end-date ({args.end_date}) 不能早于 start-date ({args.start_date})", flush=True)
        sys.exit(1)

    range_token = f"{start_date:%Y%m%d}-{end_date:%Y%m%d}"
    range_dir = os.path.join(ROOT_DIR, "data", "range", range_token)
    docs_dir = os.path.join(range_dir, "docs")

    config = load_config()
    paper_setting = config.get("arxiv_paper_setting") or {}

    total_days = (end_date - start_date).days + 1
    print(f"[INFO] 日期区间抓取: {start_date} ~ {end_date} ({total_days} 天)", flush=True)
    print(f"[INFO] 区间目录: {range_dir}", flush=True)
    print(f"[INFO] 文档输出: {docs_dir}", flush=True)

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

    current = start_date
    day_index = 0
    while current <= end_date:
        day_index += 1
        day_str = current.strftime("%Y%m%d")
        day_archive_dir = os.path.join(range_dir, day_str)

        print(f"\n{'='*60}", flush=True)
        print(f"[INFO] [{day_index}/{total_days}] 处理日期: {day_str}", flush=True)
        print(f"[INFO] 中间文件目录: {day_archive_dir}", flush=True)
        print(f"{'='*60}\n", flush=True)

        # 检查是否已有完整输出（skip-existing）
        if args.skip_existing:
            day_docs_dir = os.path.join(docs_dir, current.strftime("%Y"), current.strftime("%m"), current.strftime("%d"))
            if os.path.isdir(day_docs_dir) and any(f.endswith(".md") for f in os.listdir(day_docs_dir)):
                print(f"[INFO] 跳过 {day_str}：输出目录已存在且包含 .md 文件", flush=True)
                current += timedelta(days=1)
                continue

        # 设置环境变量
        day_env = env.copy()
        day_env["DPR_RUN_DATE"] = day_str
        day_env["DPR_ARCHIVE_DIR"] = day_archive_dir
        day_env["DOCS_DIR"] = docs_dir

        # 每天 top_k 自适应：由 Step 2.1/2.2 自动检测，不需要手动设置

        # Step 2.1 - BM25
        run_step(
            f"Step 2.1 - BM25 [{day_str}]",
            [python, os.path.join(SRC_DIR, "2.1.retrieval_papers_bm25.py")],
            env=day_env,
        )

        # Step 2.2 - Embedding
        run_step(
            f"Step 2.2 - Embedding [{day_str}]",
            [
                python, os.path.join(SRC_DIR, "2.2.retrieval_papers_embedding.py"),
                "--device", str(args.embedding_device),
                "--batch-size", str(args.embedding_batch_size),
            ],
            env=day_env,
        )

        # Step 2.3 - RRF
        run_step(
            f"Step 2.3 - RRF [{day_str}]",
            [python, os.path.join(SRC_DIR, "2.3.retrieval_papers_rrf.py")],
            env=day_env,
        )

        # Step 3 - Rerank (跳过：区间模式下默认不走 rerank)
        # 与 main.py 一致：仅 BLT 系 provider 时才执行 rerank
        from main import should_skip_rerank, prepare_rerank_fallback
        skip_rerank, rerank_base = should_skip_rerank()
        rrf_path = os.path.join(day_archive_dir, "filtered", f"arxiv_papers_{day_str}.json")
        rerank_path = os.path.join(day_archive_dir, "rank", f"arxiv_papers_{day_str}.json")
        if skip_rerank:
            print(f"[INFO] Step 3 - Rerank 已跳过 [{day_str}]: base={rerank_base}", flush=True)
            prepare_rerank_fallback(rrf_path, rerank_path)
        else:
            run_step(
                f"Step 3 - Rerank [{day_str}]",
                [python, os.path.join(SRC_DIR, "3.rank_papers.py")],
                env=day_env,
            )

        # Step 4 - LLM refine
        llm_path = os.path.join(day_archive_dir, "rank", f"arxiv_papers_{day_str}.llm.json")
        if args.skip_existing and os.path.exists(llm_path):
            print(f"[INFO] Step 4 - LLM refine 已跳过 [{day_str}]: 输出已存在", flush=True)
        else:
            run_step(
                f"Step 4 - LLM refine [{day_str}]",
                [python, os.path.join(SRC_DIR, "4.llm_refine_papers.py")],
                env=day_env,
            )

        # Step 5 - Select (区间模式使用 standard 模式)
        recommend_path = os.path.join(day_archive_dir, "recommend", f"arxiv_papers_{day_str}.standard.json")
        if args.skip_existing and os.path.exists(recommend_path):
            print(f"[INFO] Step 5 - Select 已跳过 [{day_str}]: 输出已存在", flush=True)
        else:
            run_step(
                f"Step 5 - Select [{day_str}]",
                [python, os.path.join(SRC_DIR, "5.select_papers.py")],
                env=day_env,
            )

        # Step 6 - Generate Docs
        day_docs_subdir = os.path.join(docs_dir, current.strftime("%Y"), current.strftime("%m"), current.strftime("%d"))
        if args.skip_existing and os.path.isdir(day_docs_subdir) and any(f.endswith(".md") for f in os.listdir(day_docs_subdir)):
            print(f"[INFO] Step 6 - Generate Docs 已跳过 [{day_str}]: 输出已存在", flush=True)
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

    # 保存运行记录
    run_record = {
        "type": "range",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "status": "success",
        "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "total_days": total_days,
        "docs_dir": docs_dir,
    }
    record_path = os.path.join(ROOT_DIR, "data", "last_run.json")
    os.makedirs(os.path.dirname(record_path), exist_ok=True)
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(run_record, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}", flush=True)
    print(f"[INFO] 🎉 区间抓取全部完成！", flush=True)
    print(f"[INFO] 文档输出: {docs_dir}", flush=True)
    print(f"{'='*60}", flush=True)


if __name__ == "__main__":
    main()
