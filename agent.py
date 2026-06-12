#!/usr/bin/env python3
"""
Auto-Print Agent — runs on the computer connected to the printer.

It connects *outbound* to your Vercel relay (so no firewall/port-forwarding is
needed), reports its printers, pulls queued print jobs, prints them silently
with the CUPS `lp` command, and reports the result back.

Usage:
    python3 agent.py --relay https://your-app.vercel.app --token YOUR_AGENT_TOKEN

You can also use environment variables instead of flags:
    RELAY_URL=https://your-app.vercel.app AGENT_TOKEN=... python3 agent.py

Requires only the Python standard library (and CUPS `lp`/`lpstat`, built in on
macOS and Linux).
"""

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

POLL_INTERVAL = 2.0        # seconds between job polls
HEARTBEAT_INTERVAL = 20.0  # seconds between printer/heartbeat reports
ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".doc", ".docx", ".rtf", ".odt", ".ps",
}


def list_printers():
    printers = []
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
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not list printers: {exc}")
    return printers


def http(method, url, token, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Agent-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def print_job(job):
    """Materialize the job to a temp file and submit it via `lp`."""
    tmp_dir = tempfile.mkdtemp(prefix="autoprint_agent_")
    try:
        printer = (job.get("printer") or "").strip()
        copies = max(1, min(int(job.get("copies") or 1), 50))

        if job.get("kind") == "file":
            ext = job.get("ext", "")
            if ext not in ALLOWED_EXTENSIONS:
                raise ValueError("Unsupported file type")
            raw = base64.b64decode(job.get("content") or "", validate=True)
            if not raw:
                raise ValueError("Empty file")
            path = os.path.join(tmp_dir, "job" + ext)
            with open(path, "wb") as f:
                f.write(raw)
        else:
            text = job.get("text") or ""
            path = os.path.join(tmp_dir, "job.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

        cmd = ["lp"]
        if printer:
            cmd += ["-d", printer]
        if copies > 1:
            cmd += ["-n", str(copies)]
        cmd.append(path)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "lp failed")
        m = re.search(r"request id is (\S+)", result.stdout)
        return m.group(1) if m else "submitted"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Auto-Print local agent")
    parser.add_argument("--relay", default=os.environ.get("RELAY_URL", ""),
                        help="Base URL of your Vercel deployment")
    parser.add_argument("--token", default=os.environ.get("AGENT_TOKEN", ""),
                        help="Agent token (must match AGENT_TOKEN on Vercel)")
    args = parser.parse_args()

    relay = args.relay.rstrip("/")
    token = args.token
    if not relay:
        sys.exit("Error: provide --relay https://your-app.vercel.app (or RELAY_URL)")

    host = socket.gethostname()
    print("=" * 60)
    print("  Auto-Print Agent")
    print("=" * 60)
    print(f"  This computer : {host}")
    print(f"  Relay         : {relay}")
    print(f"  Printers      : {', '.join(p['name'] for p in list_printers()) or 'none'}")
    print("  Connecting… press Ctrl+C to stop.")
    print("=" * 60)

    last_heartbeat = 0.0
    while True:
        now = time.time()
        # Heartbeat: report host + printers.
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            try:
                http("POST", f"{relay}/api/heartbeat", token,
                     {"host": host, "printers": list_printers()})
                last_heartbeat = now
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    sys.exit("Error: agent token rejected (401). Check --token / AGENT_TOKEN.")
                print(f"  ! heartbeat failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! heartbeat failed: {exc}")

        # Poll for a job.
        try:
            resp = http("GET", f"{relay}/api/poll", token)
            job = resp.get("job")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! poll failed: {exc}")
            time.sleep(POLL_INTERVAL)
            continue

        if not job:
            time.sleep(POLL_INTERVAL)
            continue

        label = f"{job.get('name', '(untitled)')} from {job.get('device', '?')}"
        print(f"  → printing: {label}")
        try:
            request_id = print_job(job)
            status, detail = "printed", request_id
            print(f"    ✓ {detail}")
        except Exception as exc:  # noqa: BLE001
            status, detail = "error", str(exc)
            print(f"    ✗ {detail}")

        try:
            http("POST", f"{relay}/api/status", token,
                 {"id": job["id"], "status": status, "detail": detail})
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not report status: {exc}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAgent stopped.")
