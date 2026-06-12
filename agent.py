#!/usr/bin/env python3
"""
Auto-Print Agent — runs on the computer connected to the printer.

It connects *outbound* to your relay (so no firewall/port-forwarding is
needed), reports its printers, pulls queued print jobs, prints them silently,
and reports the result back.

Cross-platform:
  * macOS / Linux — uses the CUPS `lp` / `lpstat` commands (built in).
  * Windows        — uses PowerShell `Get-Printer` to list printers and prints
                     text with `Out-Printer` (built in). For PDFs/images it uses
                     SumatraPDF (a free, no-admin portable .exe — just drop it
                     next to agent.py) or Adobe Reader if installed.

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

Requires only the Python standard library.
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
IS_WINDOWS = os.name == "nt"
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
    if IS_WINDOWS:
        return _win_list_printers()
    return _cups_list_printers()


def _cups_list_printers():
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
    if IS_WINDOWS:
        return _win_default_printer()
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


# --------------------------------------------------------------------------- #
# Windows printing (PowerShell built-ins + optional SumatraPDF/Adobe)
# --------------------------------------------------------------------------- #
def _run_powershell(script, timeout=20):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )


def _ps_quote(value):
    """Quote a string for safe use inside a PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _win_list_printers():
    printers = []
    try:
        ps = "Get-Printer | ForEach-Object { \"$($_.Name)|$($_.PrinterStatus)\" }"
        out = _run_powershell(ps).stdout
        for line in out.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            name, _, status = line.partition("|")
            s = status.strip().lower()
            norm = "idle" if s in ("normal", "0", "3") else (
                "printing" if "print" in s else "unknown"
            )
            if name.strip():
                printers.append({"name": name.strip(), "status": norm})
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not list printers (Windows): {exc}")
    return printers


def _win_default_printer():
    try:
        ps = "(Get-CimInstance -Class Win32_Printer -Filter 'Default = True').Name"
        out = _run_powershell(ps).stdout.strip()
        return out.splitlines()[0].strip() if out else ""
    except Exception:  # noqa: BLE001
        return ""


def _find_pdf_printer_tool():
    """Locate a tool that can print PDFs/images silently. Returns (path, kind)."""
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.environ
    pf = env.get("ProgramFiles", r"C:\Program Files")
    pf86 = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = env.get("LOCALAPPDATA", "")

    sumatra_names = ["SumatraPDF.exe", "SumatraPDF-portable.exe"]
    sumatra_dirs = [here, os.path.join(pf, "SumatraPDF"),
                    os.path.join(pf86, "SumatraPDF")]
    if local:
        sumatra_dirs.append(os.path.join(local, "SumatraPDF"))
    for d in sumatra_dirs:
        for n in sumatra_names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p, "sumatra"
    on_path = shutil.which("SumatraPDF")
    if on_path:
        return on_path, "sumatra"

    adobe_subs = [
        r"Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
        r"Adobe\Acrobat DC\Acrobat\Acrobat.exe",
        r"Adobe\Reader 11.0\Reader\AcroRd32.exe",
    ]
    for base in (pf86, pf):
        for sub in adobe_subs:
            p = os.path.join(base, sub)
            if os.path.isfile(p):
                return p, "adobe"
    return None, None


def _win_print(path, printer, copies, ext):
    # Plain text prints with no extra software.
    if ext == ".txt":
        target = f" -Name {_ps_quote(printer)}" if printer else ""
        for _ in range(copies):
            ps = f"Get-Content -Raw -LiteralPath {_ps_quote(path)} | Out-Printer{target}"
            r = _run_powershell(ps, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip() or "Out-Printer failed")
        return "submitted (text)"

    tool, kind = _find_pdf_printer_tool()
    if kind == "sumatra":
        args = [tool, "-print-to", printer, "-silent", "-exit-when-done"]
        if copies and copies > 1:
            args += ["-print-settings", f"{copies}x"]
        args.append(path)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "SumatraPDF print failed")
        return "submitted (SumatraPDF)"
    if kind == "adobe":
        # AcroRd32 /t <file> <printer> prints, then leaves the app open; close it.
        for _ in range(copies):
            subprocess.run([tool, "/t", path, printer], timeout=120)
        _run_powershell(
            "Get-Process AcroRd32,Acrobat -ErrorAction SilentlyContinue | "
            "Stop-Process -Force -ErrorAction SilentlyContinue", timeout=20)
        return "submitted (Adobe)"

    raise RuntimeError(
        "No silent PDF printer found on this Windows PC. Drop the free, no-admin "
        "SumatraPDF (portable .exe) next to agent.py, or install Adobe Reader."
    )


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
    """Materialize the job to a temp file and submit it to the printer."""
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
            ext = ".txt"
            text = job.get("text") or ""
            path = os.path.join(tmp_dir, "job.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

        if IS_WINDOWS:
            return _win_print(path, printer, copies, ext)

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
