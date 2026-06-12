"""GET /api/jobs — recent activity feed for the web UI."""

import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _store import access_ok, feed, send_json, StoreError  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if not access_ok(self):
            send_json(self, {"error": "Invalid or missing access key"}, 401)
            return
        try:
            send_json(self, {"jobs": feed()})
        except StoreError as exc:
            send_json(self, {"error": str(exc)}, 503)
