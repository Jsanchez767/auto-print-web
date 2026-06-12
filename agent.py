#!/usr/bin/env python3
"""
Auto-Print Agent — runs on the computer connected to the printer.

It connects *outbound* to your Vercel relay (so no firewall/port-forwarding is
needed), reports its printers, pulls queued print jobs, prints them silently
with the CUPS `lp` command, and reports the result back.

Usage:
    python3 agent.py --relay https://your-app.vercel.app --token YOUR_AGENT_TOKEN

Options:
    --name "Front Desk Mac"   Friendly name shown in the web app (defaults to
                              this computer's hostname).

You can also use environment variables instead of flags:
    RELAY_URL=https://your-app.vercel.app AGENT_TOKEN=... python3 agent.py

Multiple people can each run their own agent; every agent registers its own
printers under a stable, automatically-generated agent id, so the web app shows
everyone's printers grouped by computer.

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
import threading
import time
import urllib.error
import urllib.request
import uuid

POLL_INTERVAL = 2.0        # seconds between job polls
HEARTBEAT_INTERVAL = 20.0  # seconds between printer/heartbeat reports
ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".doc", ".docx", ".rtf", ".odt", ".ps",
}


def stable_agent_id():
    """Return a stable id for this machine, persisted to a local file."""
    cfg_dir = os.path.expanduser("~/.config/auto-print")
    path = os.path.join(cfg_dir, "agent_id")
    try:
        if os.path.exists(path):
            with open(path) as f:
                existing = f.read().strip()
            if existing:
                return existing
        os.makedirs(cfg_dir, exist_ok=True)
        new_id = uuid.uuid4().hex
        with open(path, "w") as f:
            f.write(new_id)
        return new_id
    except OSError:
        # Fall back to a hostname-derived id if the file can't be written.
        return "host-" + uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname()).hex[:12]


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


def default_printer():
    """Return the system default printer name, or '' if none."""
    try:
        out = subprocess.run(
            ["lpstat", "-d"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        m = re.search(r"system default destination:\s*(\S+)", out)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return ""


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


def start_ipp_server(args, host):
    """Start the bundled IPP server in a background thread, if available."""
    try:
        import ipp_server
    except Exception as exc:  # noqa: BLE001
        print(f"  ! --ipp requested but ipp_server.py not importable: {exc}")
        return

    target = args.ipp_printer.strip() or None
    if not target:
        printers = list_printers()
        target = printers[0]["name"] if printers else None
    public_host = args.ipp_public_host.strip() or None
    name = f"{host} (Auto-Print)"

    def run():
        try:
            ipp_server.serve(args.ipp_port, target, name, public_host=public_host)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! IPP server stopped: {exc}")

    threading.Thread(target=run, daemon=True).start()
    print("-" * 60)
    print("  IPP server (add this as a native printer):")
    if public_host:
        print(f"    Over the internet : ipps://{public_host}/ipp/print")
    print(f"    Same network      : ipp://{host}:{args.ipp_port}/ipp/print")
    print(f"    Routes jobs to    : {target or 'system default'}")
    print("-" * 60)


def main():
    parser = argparse.ArgumentParser(description="Auto-Print local agent")
    parser.add_argument("--relay", default=os.environ.get("RELAY_URL", ""),
                        help="Base URL of your Vercel deployment")
    parser.add_argument("--token", default=os.environ.get("AGENT_TOKEN", ""),
                        help="Agent token (must match AGENT_TOKEN on Vercel)")
    parser.add_argument("--name", default=os.environ.get("AGENT_NAME", ""),
                        help="Friendly name shown in the web app (default: hostname)")
    parser.add_argument("--ipp", action="store_true",
                        default=os.environ.get("IPP_ENABLE", "").lower() in ("1", "true", "yes"),
                        help="Also run a local IPP server so devices can add this as a native printer.")
    parser.add_argument("--ipp-port", type=int,
                        default=int(os.environ.get("IPP_PORT", "8631")),
                        help="Port for the IPP server (default 8631).")
    parser.add_argument("--ipp-printer", default=os.environ.get("IPP_PRINTER", ""),
                        help="CUPS printer the IPP server routes to (default: system default).")
    parser.add_argument("--ipp-public-host", default=os.environ.get("IPP_PUBLIC_HOST", ""),
                        help="Public hostname of your HTTPS tunnel, e.g. print.example.com.")
    args = parser.parse_args()

    relay = args.relay.rstrip("/")
    token = args.token
    if not relay:
        sys.exit("Error: provide --relay https://your-app.vercel.app (or RELAY_URL)")

    agent_id = stable_agent_id()
    host = args.name.strip() or socket.gethostname()
    print("=" * 60)
    print("  Auto-Print Agent")
    print("=" * 60)
    print(f"  This computer : {host}")
    print(f"  Agent id      : {agent_id}")
    print(f"  Relay         : {relay}")
    print(f"  Printers      : {', '.join(p['name'] for p in list_printers()) or 'none'}")
    print("  Connecting… press Ctrl+C to stop.")
    print("=" * 60)

    if args.ipp:
        start_ipp_server(args, host)

    last_heartbeat = 0.0
    while True:
        now = time.time()
        # Heartbeat: report host + printers.
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            try:
                http("POST", f"{relay}/api/heartbeat", token,
                     {"agent_id": agent_id, "host": host,
                      "printers": list_printers(), "default": default_printer()})
                last_heartbeat = now
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    sys.exit("Error: agent token rejected (401). Check --token / AGENT_TOKEN.")
                print(f"  ! heartbeat failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! heartbeat failed: {exc}")

        # Poll for a job.
        try:
            resp = http("GET", f"{relay}/api/poll?agent={agent_id}", token)
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
