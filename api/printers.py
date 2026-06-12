"""GET /api/printers — every agent's printers, grouped by host."""

import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _store import access_ok, list_agents, send_json, StoreError  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if not access_ok(self):
            send_json(self, {"error": "Invalid or missing access key"}, 401)
            return
        try:
            agents = list_agents()
        except StoreError as exc:
            send_json(self, {"error": str(exc)}, 503)
            return
        online = [a for a in agents if a.get("online")]
        send_json(self, {
            "agents": [
                {
                    "agent_id": a.get("agent_id"),
                    "host": a.get("host"),
                    "online": a.get("online", False),
                    "printers": a.get("printers", []),
                }
                for a in agents
            ],
            "online": bool(online),
        })
