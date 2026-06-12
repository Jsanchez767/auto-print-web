"""POST /api/status — the agent reports the result of a print job."""

import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _store import agent_ok, read_json, update_job, send_json, StoreError  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        if not agent_ok(self):
            send_json(self, {"error": "Invalid or missing agent token"}, 401)
            return
        try:
            data = read_json(self)
        except Exception as exc:  # noqa: BLE001
            send_json(self, {"error": f"Bad request: {exc}"}, 400)
            return
        jid = data.get("id")
        status = data.get("status", "error")
        detail = str(data.get("detail", ""))[:300]
        if not jid:
            send_json(self, {"error": "Missing job id"}, 400)
            return
        try:
            job = update_job(jid, status=status, detail=detail)
        except StoreError as exc:
            send_json(self, {"error": str(exc)}, 503)
            return
        send_json(self, {"ok": True, "job": job})
