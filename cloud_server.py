#!/usr/bin/env python3
"""
Auto-Print Cloud server — one always-on process for Render (or any host).

It does three jobs in a single Python-stdlib server:

  1. Serves the web UI (index.html / app.js / style.css).
  2. Relay API for browser uploads + the local agent
     (/api/print, /api/printers, /api/jobs, /api/poll, /api/heartbeat,
      /api/status, /api/default).
  3. A public **IPP endpoint** (/ipp/print) so any device can add this as a
     native printer (AirPrint-style) and print to whichever printer is set as
     the default on the site. IPP jobs are enqueued and the local agent prints
     them with CUPS `lp`.

Unlike the Vercel functions, this is a long-running process, so:
  * there is no 4.5 MB request cap (large PDFs/photos print), and
  * the queue lives in memory — no external Redis needed.

Environment:
  PORT            port to bind (Render sets this automatically; default 8000)
  ACCESS_KEY      key the browser must send (X-Access-Key). Empty = no key.
  AGENT_TOKEN     token the local agent must send (X-Agent-Token). Empty = none.
  IPP_DEFAULT     optional "agent_id||printer" to force the AirPrint target.
  PUBLIC_HOST     optional public hostname for the advertised ipps:// URI.

Standard library only.
"""

import argparse
import base64
import hmac
import json
import os
import socket
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ipp_server as ipp  # reuse the IPP wire codec

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_FILES = {"/": "index.html", "/index.html": "index.html",
                "/app.js": "app.js", "/style.css": "style.css",
                "/print-here": "print-here.html",
                "/print-here.html": "print-here.html"}

ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
IPP_DEFAULT = os.environ.get("IPP_DEFAULT", "")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")

JOB_TTL = 3600              # seconds a finished job stays in the feed
AGENT_ONLINE_WINDOW = 90    # seconds since last heartbeat to count as online
MAX_FEED = 100

ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".doc", ".docx", ".rtf", ".odt", ".ps",
}

# --------------------------------------------------------------------------- #
# In-memory store (single Render instance → in-process state is enough)
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_agents = {}        # agent_id -> {host, printers, default, last_seen}
_queues = {}        # agent_id -> [job, ...] pending for that agent
_feed = []          # recent jobs (most recent last)
_default_route = {"agent_id": "", "printer": ""}  # site-chosen AirPrint target

if IPP_DEFAULT and "||" in IPP_DEFAULT:
    a, p = IPP_DEFAULT.split("||", 1)
    _default_route = {"agent_id": a, "printer": p}


def _now():
    return time.time()


def _prune_feed():
    cutoff = _now() - JOB_TTL
    while _feed and _feed[0]["ts"] < cutoff and _feed[0]["status"] in ("printed", "error"):
        _feed.pop(0)
    while len(_feed) > MAX_FEED:
        _feed.pop(0)


def register_agent(agent_id, host, printers, default):
    with _lock:
        _agents[agent_id] = {
            "host": host or "computer",
            "printers": printers or [],
            "default": default or "",
            "last_seen": _now(),
        }


def list_agents():
    out = []
    now = _now()
    with _lock:
        for aid, a in _agents.items():
            out.append({
                "agent_id": aid,
                "host": a["host"],
                "online": (now - a["last_seen"]) <= AGENT_ONLINE_WINDOW,
                "printers": a["printers"],
                "default": a.get("default", ""),
            })
    return out


def online_agents():
    return [a for a in list_agents() if a["online"]]


def enqueue_job(job):
    with _lock:
        _queues.setdefault(job["agent_id"], []).append(job)
        _feed.append(job)
        _prune_feed()


def next_job(agent_id):
    with _lock:
        q = _queues.get(agent_id)
        if q:
            return q.pop(0)
    return None


def update_status(job_id, status, detail):
    with _lock:
        for j in _feed:
            if j["id"] == job_id:
                j["status"] = status
                j["detail"] = detail
                break


def resolve_default_route():
    """Return (agent_id, printer) for IPP jobs, or (None, None)."""
    with _lock:
        route = dict(_default_route)
    if route["agent_id"]:
        return route["agent_id"], route["printer"]
    # Fall back to the first online agent and its default/first printer.
    for a in online_agents():
        printer = a.get("default") or (a["printers"][0]["name"] if a["printers"] else "")
        if printer:
            return a["agent_id"], printer
    return None, None


def set_default_route(agent_id, printer):
    with _lock:
        _default_route["agent_id"] = agent_id or ""
        _default_route["printer"] = printer or ""


# --------------------------------------------------------------------------- #
# Auth helpers (constant-time)
# --------------------------------------------------------------------------- #
def _const_eq(a, b):
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def access_ok(headers):
    if not ACCESS_KEY:
        return True
    return _const_eq(headers.get("X-Access-Key", ""), ACCESS_KEY)


def agent_ok(headers):
    if not AGENT_TOKEN:
        return True
    return _const_eq(headers.get("X-Agent-Token", ""), AGENT_TOKEN)


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AutoPrintCloud/1.0"

    def log_message(self, *_a):
        pass

    # ---- low-level IO ---- #
    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    continue
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _public_host(self):
        return PUBLIC_HOST or self.headers.get("Host") or "localhost"

    def _scheme(self):
        # Render terminates TLS and sets X-Forwarded-Proto.
        return (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()

    def _printer_uri(self):
        scheme = "ipps" if self._scheme() == "https" else "ipp"
        return f"{scheme}://{self._public_host()}/ipp/print"

    # ---- GET ---- #
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path in STATIC_FILES:
            self._file(os.path.join(STATIC_DIR, STATIC_FILES[path]))
            return
        if path == "/ipp/print":
            msg = (f"Auto-Print IPP endpoint is running.\n"
                   f"Add as a printer (IPP):\n  {self._printer_uri()}\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        if path == "/api/info":
            self._json({"ok": True, "time": _now(), "ipp_uri": self._printer_uri()})
            return
        if path == "/api/printers":
            if not access_ok(self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            agents = list_agents()
            aid, printer = resolve_default_route()
            self._json({"agents": agents, "online": any(a["online"] for a in agents),
                        "default_route": {"agent_id": aid or "", "printer": printer or ""}})
            return
        if path == "/api/jobs":
            if not access_ok(self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            with _lock:
                self._json({"jobs": list(_feed)})
            return
        if path == "/api/poll":
            if not agent_ok(self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            agent_id = ""
            for part in query.split("&"):
                if part.startswith("agent="):
                    agent_id = part[6:]
            if not agent_id:
                self._json({"error": "missing agent"}, 400)
                return
            job = next_job(agent_id)
            if job:
                update_status(job["id"], "printing", "")
            self._json({"job": job})
            return
        self.send_error(404, "Not found")

    # ---- POST ---- #
    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/ipp/print":
            self._handle_ipp()
            return
        if path == "/api/print":
            if not access_ok(self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            self._handle_browser_print()
            return
        if path == "/api/heartbeat":
            if not agent_ok(self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            data = self._json_body()
            if not data.get("agent_id"):
                self._json({"error": "missing agent_id"}, 400)
                return
            register_agent(data["agent_id"], data.get("host"),
                           data.get("printers"), data.get("default"))
            self._json({"ok": True})
            return
        if path == "/api/status":
            if not agent_ok(self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            data = self._json_body()
            update_status(data.get("id"), data.get("status"), data.get("detail"))
            self._json({"ok": True})
            return
        if path == "/api/default":
            if not access_ok(self.headers):
                self._json({"error": "unauthorized"}, 401)
                return
            data = self._json_body()
            set_default_route(data.get("agent_id"), data.get("printer"))
            self._json({"ok": True})
            return
        self.send_error(404, "Not found")

    def _json_body(self):
        try:
            raw = self._read_body()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # ---- browser upload ---- #
    def _handle_browser_print(self):
        data = self._json_body()
        agent_id = (data.get("agent_id") or "").strip()
        printer = (data.get("printer") or "").strip()
        if not agent_id:
            self._json({"error": "missing agent_id"}, 400)
            return
        filename = os.path.basename(data.get("filename") or "document")
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            self._json({"error": "unsupported file type"}, 400)
            return
        content = data.get("content") or ""
        try:
            raw = base64.b64decode(content, validate=True)
        except Exception:
            self._json({"error": "bad file encoding"}, 400)
            return
        if not raw:
            self._json({"error": "empty file"}, 400)
            return
        job = {
            "id": uuid.uuid4().hex[:8],
            "agent_id": agent_id,
            "device": (data.get("device") or "Unknown device")[:60],
            "host": data.get("host") or "",
            "printer": printer,
            "copies": max(1, min(int(data.get("copies") or 1), 50)),
            "kind": "file",
            "ext": ext,
            "content": content,
            "name": filename,
            "status": "queued",
            "detail": "",
            "ts": _now(),
        }
        enqueue_job(job)
        # Don't echo the file content back.
        self._json({"job": {k: v for k, v in job.items() if k != "content"}})

    # ---- IPP endpoint ---- #
    def _handle_ipp(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/ipp":
            self.send_error(400, "expected application/ipp")
            return
        body = self._read_body()
        version, op_id, req_id, attrs, doc = ipp.parse_ipp(body)
        printer_uri = self._printer_uri()

        if op_id == ipp.OP_GET_PRINTER_ATTRS:
            buf = ipp._response_head(req_id, ipp.S_OK, version)
            ipp.printer_attributes(buf, printer_uri, "Auto-Print", True)
            buf.append(ipp.TAG_END)
            self._send_ipp(bytes(buf))
            return

        if op_id == ipp.OP_VALIDATE_JOB:
            buf = ipp._response_head(req_id, ipp.S_OK, version)
            buf.append(ipp.TAG_END)
            self._send_ipp(bytes(buf))
            return

        if op_id in (ipp.OP_PRINT_JOB, ipp.OP_CREATE_JOB, ipp.OP_SEND_DOCUMENT):
            job_id = uuid.uuid4().int >> 96
            copies = 1
            cval = attrs.get("copies")
            if cval and len(cval) == 4:
                copies = struct.unpack(">i", cval)[0]
            fmt = ""
            declared = attrs.get("document-format")
            if declared:
                fmt = declared.decode("utf-8", "replace")
            uname = b""
            if attrs.get("requesting-user-name"):
                uname = attrs["requesting-user-name"]

            state = 9  # completed (we accept + queue)
            if doc:
                agent_id, printer = resolve_default_route()
                if not agent_id:
                    state = 8  # aborted: nowhere to send
                else:
                    ext = ipp._detect_ext(doc, fmt)
                    if ext not in ALLOWED_EXTENSIONS:
                        ext = ".pdf"
                    job = {
                        "id": uuid.uuid4().hex[:8],
                        "agent_id": agent_id,
                        "device": (uname.decode("utf-8", "replace") or "AirPrint")[:60],
                        "host": "",
                        "printer": printer,
                        "copies": max(1, min(copies, 50)),
                        "kind": "file",
                        "ext": ext,
                        "content": base64.b64encode(doc).decode("ascii"),
                        "name": f"AirPrint job{ext}",
                        "status": "queued",
                        "detail": "",
                        "ts": _now(),
                    }
                    enqueue_job(job)

            buf = ipp._response_head(req_id, ipp.S_OK, version)
            ipp.job_attributes(buf, printer_uri, job_id & 0x7FFFFFFF, state)
            buf.append(ipp.TAG_END)
            self._send_ipp(bytes(buf))
            return

        if op_id in (ipp.OP_GET_JOBS, ipp.OP_GET_JOB_ATTRS):
            buf = ipp._response_head(req_id, ipp.S_OK, version)
            if op_id == ipp.OP_GET_JOB_ATTRS:
                ipp.job_attributes(buf, printer_uri, 1, 9)
            buf.append(ipp.TAG_END)
            self._send_ipp(bytes(buf))
            return

        if op_id == ipp.OP_CANCEL_JOB:
            buf = ipp._response_head(req_id, ipp.S_OK, version)
            buf.append(ipp.TAG_END)
            self._send_ipp(bytes(buf))
            return

        buf = ipp._response_head(req_id, ipp.S_NOT_SUPPORTED, version)
        buf.append(ipp.TAG_END)
        self._send_ipp(bytes(buf))

    def _send_ipp(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    ap = argparse.ArgumentParser(description="Auto-Print cloud server")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 60)
    print("  Auto-Print Cloud server")
    print("=" * 60)
    print(f"  Listening : {args.host}:{args.port}")
    print(f"  Access key: {'set' if ACCESS_KEY else 'OPEN (no key)'}")
    print(f"  Agent tok : {'set' if AGENT_TOKEN else 'OPEN (no token)'}")
    print(f"  IPP add   : /ipp/print")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
