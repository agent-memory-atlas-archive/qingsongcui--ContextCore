"""Exercise AppKit menu actions, WebKit readbacks and history on an isolated port."""
import json
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("xcrun"), reason="requires macOS AppKit and the Swift toolchain")
def test_native_menu_readbacks_and_navigation(tmp_path):
    macos = Path(__file__).resolve().parents[2] / "macos"
    binary = tmp_path / "verify-shortcuts"
    subprocess.run(["xcrun", "swiftc", "-framework", "Cocoa", "-framework", "WebKit",
                    str(macos / "WorkbenchShortcuts.swift"), str(macos / "tests/VerifyWorkbenchShortcuts.swift"),
                    "-o", str(binary)], check=True, timeout=180)
    requests = []
    fixtures = {
        "/api/manage/agent-preferences": {
            "favorite_hosts": ["codex"], "recent_hosts": [], "host_names": {"codex": "Codex CLI"},
        },
        "/api/manage/hosts/codex/state": {
            "host_id": "codex", "selection": {"provider_id": "fixture", "provider_name": "Example",
                                               "model_id": "test-model", "status": "externally_changed", "connection_mode": "direct"},
        },
        "/api/account-quotas": {"items": [{"name": "Codex", "status": "offline",
            "last_success_at": "2026-05-20T12:20:00Z", "snapshot": {
                "windows": [{"label": "Five hours", "used_percent": 73}], "balances": []}},
            {"name": "OpenRouter", "source": "openrouter", "status": "available",
             "last_success_at": "2026-05-20T12:20:00Z", "snapshot": {"balances": [
                 {"unit": "USD", "scope": "key", "amount": "1"}, {"unit": "USD", "scope": "account", "amount": "10"},
             ]}}]},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            requests.append(path)
            if path == "/":
                content_type = "text/html; charset=utf-8"
                body = b"""<div id="root"></div><script>window.draft = 'retained'; window.visits = 0;
                window.addEventListener('popstate', () => window.visits++);</script>"""
            elif path in fixtures:
                content_type = "application/json"
                body = json.dumps(fixtures[path]).encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run([str(binary), f"http://127.0.0.1:{server.server_port}"],
                           cwd=tmp_path, check=True, timeout=45, capture_output=True, text=True)
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert sorted(path for path in requests if path.startswith("/api/")) == sorted(fixtures)
