#!/usr/bin/env python3
"""
Daily Paper Reader 本地服务器
- 静态文件服务（从项目根目录）
- /api/config  GET/PUT 读写 config.yaml
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

# ── Quick-fetch task state ───────────────────────────────────────
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
    """Background thread: run src/main.py with conda python."""
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

    cmd = [CONDA_PYTHON, "src/main.py"] + args
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
    """Static file server + /api/config endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/quick-fetch":
            return self._handle_quick_fetch()
        self.send_error(404, "Not Found")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            return self._handle_get_config()
        if parsed.path == "/api/quick-fetch/status":
            return self._handle_quick_fetch_status()
        # fallback: static file
        return super().do_GET()

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            return self._handle_put_config()
        self.send_error(404, "Not Found")

    # ── Quick-fetch handlers ─────────────────────────────────────

    def _handle_quick_fetch(self):
        """POST /api/quick-fetch — start a local pipeline run in background."""
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
            fetch_days = str(body.get("fetch_days", "10")).strip() or "10"
            fetch_mode = str(body.get("fetch_mode", "")).strip()
            profile_tag = str(body.get("profile_tag", "")).strip()
            allow_rerun = bool(body.get("allow_rerun", False))

            args = ["--fetch-days", fetch_days]
            if not allow_rerun:
                args.append("--skip-existing")
            if fetch_mode:
                args += ["--fetch-mode", fetch_mode]
            if profile_tag:
                args += ["--profile-tag", profile_tag]

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
            "message": f"已启动本地抓取任务 (fetch_days={fetch_days})",
            "args": args,
        })

    def _handle_quick_fetch_status(self):
        """GET /api/quick-fetch/status — poll task progress."""
        with _fetch_lock:
            task = dict(_fetch_task)
        # Only return last 4KB of log to keep response small
        if len(task["log"]) > 4096:
            task["log_tail"] = "..." + task["log"][-4096:]
        else:
            task["log_tail"] = task["log"]
        self._json_response(200, task)

    # ── handlers ──────────────────────────────────────────────

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

    # ── helpers ────────────────────────────────────────────────

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
