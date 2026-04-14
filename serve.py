#!/usr/bin/env python3
"""
Daily Paper Reader 本地服务器
- 静态文件服务（从项目根目录）
- /api/config  GET/PUT 读写 config.yaml
"""

import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"


class DPRHandler(SimpleHTTPRequestHandler):
    """Static file server + /api/config endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            return self._handle_get_config()
        # fallback: static file
        return super().do_GET()

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            return self._handle_put_config()
        self.send_error(404, "Not Found")

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
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
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
