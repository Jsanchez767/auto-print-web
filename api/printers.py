"""GET /api/printers — the printers reported by the local agent (heartbeat)."""

import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _store import access_ok, get_printers, send_json, StoreError  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if not access_ok(self):
            send_json(self, {"error": "Invalid or missing access key"}, 401)
            return
        try:
            data = get_printers()
        except StoreError as exc:
            send_json(self, {"error": str(exc)}, 503)
            return
        send_json(self, {
            "printers": data.get("printers", []),
            "host": data.get("host"),
            "online": data.get("online", False),
            "default": None,
        })
