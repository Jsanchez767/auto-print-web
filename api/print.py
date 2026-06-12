"""POST /api/print — a browser enqueues a print job for the agent to pick up."""

import base64
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _store import access_ok, enqueue_job, read_json, send_json, StoreError  # noqa: E402

# Vercel serverless functions cap request bodies at ~4.5 MB, so the base64
# payload must stay under that. ~3 MB raw -> ~4 MB encoded.
MAX_RAW_BYTES = 3 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".doc", ".docx", ".rtf", ".odt", ".ps",
}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        if not access_ok(self):
            send_json(self, {"error": "Invalid or missing access key"}, 401)
            return
        try:
            data = read_json(self)
        except Exception as exc:  # noqa: BLE001
            send_json(self, {"error": f"Bad request: {exc}"}, 400)
            return

        device = (data.get("device") or "Unknown device").strip()[:60]
        printer = (data.get("printer") or "").strip()
        agent_id = (data.get("agent_id") or "").strip()[:64]
        copies = max(1, min(int(data.get("copies") or 1), 50))
        kind = data.get("kind", "text")

        if not agent_id:
            send_json(self, {"error": "Choose a printer (no target computer selected)"}, 400)
            return

        job = {
            "id": uuid.uuid4().hex[:8],
            "device": device,
            "agent_id": agent_id,
            "host": (data.get("host") or "").strip()[:80],
            "printer": printer,
            "copies": copies,
            "kind": kind,
            "name": "",
            "status": "queued",
            "detail": "",
            "ts": time.time(),
        }

        try:
            if kind == "file":
                filename = os.path.basename(data.get("filename") or "document")
                ext = os.path.splitext(filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    raise ValueError("Unsupported or missing file type")
                content_b64 = data.get("content") or ""
                raw = base64.b64decode(content_b64, validate=True)
                if not raw:
                    raise ValueError("File is empty")
                if len(raw) > MAX_RAW_BYTES:
                    raise ValueError("File exceeds 3 MB (Vercel upload limit)")
                job["name"] = filename
                job["ext"] = ext
                job["content"] = content_b64
            else:
                text = data.get("text") or ""
                if not text.strip():
                    raise ValueError("Nothing to print")
                first = text.strip().splitlines()[0]
                job["name"] = (first[:40] + "…") if len(first) > 40 else first
                job["text"] = text
        except Exception as exc:  # noqa: BLE001
            send_json(self, {"error": str(exc)}, 400)
            return

        try:
            enqueue_job(job)
        except StoreError as exc:
            send_json(self, {"error": str(exc)}, 503)
            return

        public = {k: v for k, v in job.items() if k not in ("content", "text")}
        send_json(self, {"job": public})
