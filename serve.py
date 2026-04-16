#!/usr/bin/env python3
"""
Daily Paper Reader 本地服务器
- 静态文件服务（从项目根目录）
- /api/config  GET/PUT 读写 config.yaml
- /api/range-fetch  POST 启动区间抓取
- /api/range-fetch/status  GET 轮询任务状态
- /api/last-run  GET 最近一次运行信息
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
ENV_PATH = SCRIPT_DIR / ".env"
CONDA_PYTHON = "/home/ghz/miniconda3/envs/daily-paper-reader/bin/python3"
LAST_RUN_PATH = SCRIPT_DIR / "data" / "last_run.json"

# ── Reset-content task state ───────────────────────────────────
_reset_task = {
    "status": "idle",       # idle | running | success | failure
    "started_at": None,
    "finished_at": None,
    "log": "",
    "exit_code": None,
}
_reset_lock = threading.Lock()


def _run_reset():
    """Background thread: run reset-content via conda python."""
    global _reset_task
    env = os.environ.copy()
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    cmd = [CONDA_PYTHON, "-c",
           "import shutil, os, glob; "
           "from datetime import datetime; "
           f"ROOT = r'{SCRIPT_DIR}'; "
           "TS = datetime.now().strftime('%Y%m%d%H%M%S'); "
           "DST = os.path.join(ROOT, 'docs'); "
           "SRC = os.path.join(ROOT, 'docs_init'); "
           "if os.path.exists(os.path.join(ROOT, 'docs')): "
           "    shutil.move(DST, os.path.join(ROOT, f'docs_backup_{{TS}}')); "
           "shutil.copytree(SRC, DST); "
           "ARC = os.path.join(ROOT, 'archive'); "
           "if os.path.isdir(ARC): "
           "    for d in glob.glob(os.path.join(ARC, '*')): "
           "        if os.path.isdir(d): shutil.rmtree(d); "
           "        else: os.remove(d); "
           "print('Reset completed.')"]
    log_lines = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            log_lines.append(line)
            with _reset_lock:
                _reset_task["log"] = "".join(log_lines)
        proc.wait()
        exit_code = proc.returncode
    except Exception as e:
        log_lines.append(f"\n[EXCEPTION] {e}\n")
        exit_code = -1

    with _reset_lock:
        _reset_task["status"] = "success" if exit_code == 0 else "failure"
        _reset_task["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _reset_task["log"] = "".join(log_lines)
        _reset_task["exit_code"] = exit_code


# ── Range-fetch task state ───────────────────────────────────────
_fetch_task = {
    "status": "idle",       # idle | running | success | failure
    "started_at": None,
    "finished_at": None,
    "args": None,
    "log": "",              # accumulated stdout+stderr
    "exit_code": None,
}
_fetch_lock = threading.Lock()


def _run_pipeline(args):
    """Background thread: run pipeline_range.py with conda python."""
    global _fetch_task
    env = os.environ.copy()
    # Load .env file
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    cmd = [CONDA_PYTHON, "pipeline_range.py"] + args
    log_lines = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            log_lines.append(line)
            with _fetch_lock:
                _fetch_task["log"] = "".join(log_lines)
        proc.wait()
        exit_code = proc.returncode
    except Exception as e:
        log_lines.append(f"\n[EXCEPTION] {e}\n")
        exit_code = -1

    with _fetch_lock:
        _fetch_task["status"] = "success" if exit_code == 0 else "failure"
        _fetch_task["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _fetch_task["log"] = "".join(log_lines)
        _fetch_task["exit_code"] = exit_code


class DPRHandler(SimpleHTTPRequestHandler):
    """Static file server + API endpoints."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/range-fetch":
            return self._handle_range_fetch()
        self.send_error(404, "Not Found")

        if parsed.path == "/api/reset-content":
            return self._handle_reset_content()
        self.send_error(404, "Not Found")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            return self._handle_get_config()
        if parsed.path == "/api/range-fetch/status":
            return self._handle_range_fetch_status()
        if parsed.path == "/api/reset-content/status":
            return self._handle_reset_content_status()
        if parsed.path == "/api/last-run":
            return self._handle_last_run()
        # fallback: static file
        return super().do_GET()

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            return self._handle_put_config()
        self.send_error(404, "Not Found")

    # ── Reset-content handlers ───────────────────────────────────

    def _handle_reset_content(self):
        """POST /api/reset-content — reset docs and archive."""
        global _reset_task

        with _reset_lock:
            if _reset_task["status"] == "running":
                self._json_response(409, {
                    "error": "已有重置任务正在运行",
                    "started_at": _reset_task["started_at"],
                })
                return

            _reset_task = {
                "status": "running",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "finished_at": None,
                "log": "",
                "exit_code": None,
            }

        t = threading.Thread(target=_run_reset, daemon=True)
        t.start()

        self._json_response(200, {
            "ok": True,
            "message": "已启动重置任务",
        })

    def _handle_reset_content_status(self):
        """GET /api/reset-content/status — poll reset task progress."""
        with _reset_lock:
            task = dict(_reset_task)
        if len(task["log"]) > 4096:
            task["log_tail"] = "..." + task["log"][-4096:]
        else:
            task["log_tail"] = task["log"]
        self._json_response(200, task)

    # ── Range-fetch handlers ─────────────────────────────────────

    def _handle_range_fetch(self):
        """POST /api/range-fetch — start a date-range pipeline run."""
        global _fetch_task

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}

        with _fetch_lock:
            if _fetch_task["status"] == "running":
                self._json_response(409, {
                    "error": "已有抓取任务正在运行",
                    "started_at": _fetch_task["started_at"],
                })
                return

            # Build args from request body
            start_date = str(body.get("start_date", "")).strip()
            end_date = str(body.get("end_date", "")).strip()
            skip_existing = bool(body.get("skip_existing", False))

            if not start_date or not end_date:
                self._json_response(400, {"error": "需要提供 start_date 和 end_date (YYYYMMDD)"})
                return

            args = ["--start-date", start_date, "--end-date", end_date]
            if skip_existing:
                args.append("--skip-existing")

            _fetch_task = {
                "status": "running",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "finished_at": None,
                "args": args,
                "log": "",
                "exit_code": None,
            }

        # Launch background thread
        t = threading.Thread(target=_run_pipeline, args=(args,), daemon=True)
        t.start()

        self._json_response(200, {
            "ok": True,
            "message": f"已启动区间抓取任务 ({start_date} ~ {end_date})",
            "args": args,
        })

    def _handle_range_fetch_status(self):
        """GET /api/range-fetch/status — poll task progress."""
        with _fetch_lock:
            task = dict(_fetch_task)
        # Only return last 4KB of log to keep response small
        if len(task["log"]) > 4096:
            task["log_tail"] = "..." + task["log"][-4096:]
        else:
            task["log_tail"] = task["log"]
        self._json_response(200, task)

    def _handle_last_run(self):
        """GET /api/last-run — return last run info."""
        if not LAST_RUN_PATH.is_file():
            self._json_response(200, {"exists": False})
            return
        try:
            data = json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
            data["exists"] = True
            self._json_response(200, data)
        except Exception as e:
            self._json_response(200, {"exists": False, "error": str(e)})

    # ── Config handlers ──────────────────────────────────────────

    def _handle_get_config(self):
        try:
            text = CONFIG_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._json_response(404, {"error": "config.yaml not found"})
            return
        self._json_response(200, {"content": text})

    def _handle_put_config(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._json_response(400, {"error": "invalid JSON"})
            return

        content = body.get("content", "")
        if not isinstance(content, str):
            self._json_response(400, {"error": "content must be a string"})
            return

        try:
            CONFIG_PATH.write_text(content, encoding="utf-8")
        except Exception as e:
            self._json_response(500, {"error": str(e)})
            return

        self._json_response(200, {"ok": True, "path": str(CONFIG_PATH)})

    # ── helpers ───────────────────────────────────────────────────

    def _json_response(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        # quieter log
        if "/api/" not in str(args):
            super().log_message(format, *args)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = HTTPServer(("0.0.0.0", port), DPRHandler)
    print(f"[INFO] Daily Paper Reader server: http://localhost:{port}")
    print(f"[INFO] Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Stopped.")


if __name__ == "__main__":
    main()
