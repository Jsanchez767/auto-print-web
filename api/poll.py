"""GET /api/poll?agent=<id> — the local agent pulls its next queued job."""

import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(__file__))
from _store import agent_ok, next_queued_job, update_job, send_json, StoreError  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if not agent_ok(self):
            send_json(self, {"error": "Invalid or missing agent token"}, 401)
            return
        params = parse_qs(urlparse(self.path).query)
        agent_id = (params.get("agent", [""])[0]).strip()[:64]
        if not agent_id:
            send_json(self, {"error": "Missing agent query parameter"}, 400)
            return
        try:
            job = next_queued_job(agent_id)
            if job:
                update_job(job["id"], status="printing")
        except StoreError as exc:
            send_json(self, {"error": str(exc)}, 503)
            return
        send_json(self, {"job": job})
