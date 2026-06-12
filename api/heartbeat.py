"""POST /api/heartbeat — the agent reports its host name + available printers."""

import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _store import agent_ok, read_json, set_agent, send_json, StoreError  # noqa: E402


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
        agent_id = (data.get("agent_id") or "").strip()[:64]
        if not agent_id:
            send_json(self, {"error": "Missing agent_id"}, 400)
            return
        host = (data.get("host") or "printer computer").strip()[:80]
        printers = data.get("printers") or []
        if not isinstance(printers, list):
            printers = []
        try:
            set_agent(agent_id, host, printers[:50])
        except StoreError as exc:
            send_json(self, {"error": str(exc)}, 503)
            return
        send_json(self, {"ok": True})
