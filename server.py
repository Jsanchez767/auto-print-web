#!/usr/bin/env python3
"""
Auto-Print Web — a zero-dependency LAN print server.

Run this on the computer that is connected to the printer. Any computer on the
same network can open the web app in a browser, pick a printer, and send text or
a file. The job is printed *silently* (no dialog) using the macOS/Linux CUPS
`lp` command. A live activity feed (Server-Sent Events) shows jobs on every
connected device in real time.

Usage:
    python3 server.py            # listen on 0.0.0.0:8000
    python3 server.py --port 9000
    python3 server.py --host 127.0.0.1 --port 8000
"""

import argparse
import base64
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_FILES = {"/": "index.html", "/index.html": "index.html",
                "/app.js": "app.js", "/style.css": "style.css"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB cap on a single print job
ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".doc", ".docx", ".rtf", ".odt", ".ps",
}

# --------------------------------------------------------------------------- #
# In-memory state (shared across threads)
# --------------------------------------------------------------------------- #

_state_lock = threading.Lock()
_jobs = []                # recent print jobs (most recent last), capped
_sse_clients = []         # list of queue.Queue, one per connected browser
_MAX_JOBS = 100


def _broadcast(event_type, payload):
    """Push an event to every connected Server-Sent-Events client."""
    message = f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
    with _state_lock:
        clients = list(_sse_clients)
    for q in clients:
        try:
            q.put_nowait(message)
        except queue.Full:
            pass


def _record_job(job):
    with _state_lock:
        _jobs.append(job)
        if len(_jobs) > _MAX_JOBS:
            del _jobs[: len(_jobs) - _MAX_JOBS]


# --------------------------------------------------------------------------- #
# Printer helpers (CUPS / lp)
# --------------------------------------------------------------------------- #

def list_printers():
    """Return {"printers": [...], "default": name|None}."""
    printers = []
    default = None
    try:
        out = subprocess.run(
            ["lpstat", "-p"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in out.splitlines():
            m = re.match(r"printer (\S+)", line)
            if m:
                status = "idle" if "idle" in line else (
                    "printing" if "printing" in line else "unknown"
                )
                printers.append({"name": m.group(1), "status": status})
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["lpstat", "-d"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        m = re.search(r"system default destination:\s*(\S+)", out)
        if m:
            default = m.group(1)
    except Exception:
        pass
    return {"printers": printers, "default": default,
            "host": socket.gethostname(), "online": True}


def submit_print(printer, file_path, copies=1):
    """Submit a file to the printer via `lp`. Returns the CUPS request id."""
    cmd = ["lp"]
    if printer:
        cmd += ["-d", printer]
    if copies and copies > 1:
        cmd += ["-n", str(int(copies))]
    cmd.append(file_path)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "lp failed")
    m = re.search(r"request id is (\S+)", result.stdout)
    return m.group(1) if m else result.stdout.strip()


def _safe_extension(filename):
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in ALLOWED_EXTENSIONS else ""


def handle_print_request(data):
    """
    data keys:
      device   - friendly sender name
      printer  - target printer name
      copies   - int
      kind     - "text" | "file"
      text     - text body (kind == text)
      filename - original filename (kind == file)
      content  - base64 file content (kind == file)
    Returns the recorded job dict.
    """
    device = (data.get("device") or "Unknown device").strip()[:60]
    printer = (data.get("printer") or "").strip()
    copies = int(data.get("copies") or 1)
    copies = max(1, min(copies, 50))
    kind = data.get("kind", "text")

    tmp_dir = tempfile.mkdtemp(prefix="autoprint_")
    job = {
        "id": uuid.uuid4().hex[:8],
        "device": device,
        "printer": printer,
        "copies": copies,
        "kind": kind,
        "name": "",
        "status": "pending",
        "detail": "",
        "ts": time.time(),
    }
    try:
        if kind == "file":
            filename = os.path.basename(data.get("filename") or "document")
            ext = _safe_extension(filename)
            if not ext:
                raise ValueError("Unsupported or missing file type")
            content_b64 = data.get("content") or ""
            raw = base64.b64decode(content_b64, validate=True)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise ValueError("File exceeds 25 MB limit")
            if len(raw) == 0:
                raise ValueError("File is empty")
            file_path = os.path.join(tmp_dir, "job" + ext)
            with open(file_path, "wb") as f:
                f.write(raw)
            job["name"] = filename
        else:
            text = data.get("text") or ""
            if not text.strip():
                raise ValueError("Nothing to print")
            file_path = os.path.join(tmp_dir, "job.txt")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(text)
            preview = text.strip().splitlines()[0] if text.strip() else "text"
            job["name"] = (preview[:40] + "…") if len(preview) > 40 else preview

        request_id = submit_print(printer, file_path, copies)
        job["status"] = "printed"
        job["detail"] = request_id
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        job["status"] = "error"
        job["detail"] = str(exc)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    _record_job(job)
    _broadcast("job", job)
    return job


# --------------------------------------------------------------------------- #
# HTTP request handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "AutoPrintWeb/1.0"

    # --- helpers -------------------------------------------------------- #
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
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

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_UPLOAD_BYTES + 1024 * 1024:
            raise ValueError("Request too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # --- routing -------------------------------------------------------- #
    def do_GET(self):  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0]

        if path in STATIC_FILES:
            self._send_file(os.path.join(STATIC_DIR, STATIC_FILES[path]))
            return
        if path == "/api/printers":
            self._send_json(list_printers())
            return
        if path == "/api/jobs":
            with _state_lock:
                self._send_json({"jobs": list(_jobs)})
            return
        if path == "/api/info":
            self._send_json({"host": socket.gethostname(), "time": time.time()})
            return
        if path == "/api/events":
            self._handle_sse()
            return
        self.send_error(404, "Not found")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/print":
            try:
                data = self._read_json_body()
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"Bad request: {exc}"}, status=400)
                return
            job = handle_print_request(data)
            status = 200 if job["status"] != "error" else 500
            self._send_json({"job": job}, status=status)
            return
        self.send_error(404, "Not found")

    # --- Server-Sent Events --------------------------------------------- #
    def _handle_sse(self):
        q = queue.Queue(maxsize=100)
        with _state_lock:
            _sse_clients.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(msg.encode("utf-8"))
                except queue.Empty:
                    msg = ": keep-alive\n\n"  # heartbeat
                    self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _state_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    def log_message(self, fmt, *args):  # quieter logging
        return


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def local_ip():
    """Best-effort LAN IP for printing connection instructions."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    parser = argparse.ArgumentParser(description="Auto-Print Web server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    ip = local_ip()
    printers = list_printers()["printers"]
    print("=" * 60)
    print("  Auto-Print Web is running")
    print("=" * 60)
    print(f"  This computer : {socket.gethostname()}")
    print(f"  Open locally  : http://localhost:{args.port}")
    print(f"  Other devices : http://{ip}:{args.port}")
    print(f"  Printers found: {', '.join(p['name'] for p in printers) or 'none'}")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
