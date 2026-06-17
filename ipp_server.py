#!/usr/bin/env python3
"""
Auto-Print IPP server — makes this computer's printer addable from any device.

This implements just enough of IPP/1.1 + 2.0 (RFC 8011, plus the AirPrint
driverless conventions) for macOS, iOS, Windows and Linux to add it through
their native "Add Printer" dialog and print to it. Incoming documents are
handed to CUPS `lp`, so whatever the agent can print, this can print.

It is meant to sit behind an HTTPS tunnel (e.g. Cloudflare Tunnel on your own
domain) so people can add it as a printer over the internet:

    printer URL on the device:  ipps://print.yourdomain.com/ipp/print

Run standalone:
    python3 ipp_server.py --port 8631 --printer HP_OfficeJet_Pro_9010_series \
        --name "Auto-Print"

…or let agent.py start it for you with `--ipp`.

Standard library only (and CUPS `lp`/`lpstat`).
"""

import argparse
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# IPP wire-format constants
# --------------------------------------------------------------------------- #
# Delimiter (group) tags
TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04
TAG_UNSUPPORTED = 0x05

# Value tags
V_INTEGER = 0x21
V_BOOLEAN = 0x22
V_ENUM = 0x23
V_OCTET = 0x30
V_DATETIME = 0x31
V_RESOLUTION = 0x32
V_RANGE = 0x33
V_TEXT = 0x41
V_NAME = 0x42
V_KEYWORD = 0x44
V_URI = 0x45
V_CHARSET = 0x47
V_NATLANG = 0x48
V_MIMETYPE = 0x49

# Operation ids
OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_CREATE_JOB = 0x0005
OP_SEND_DOCUMENT = 0x0006
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRS = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRS = 0x000B

# Status codes
S_OK = 0x0000
S_OK_IGNORED = 0x0001
S_CLIENT_BAD_REQUEST = 0x0400
S_NOT_FOUND = 0x0406
S_NOT_SUPPORTED = 0x0501

ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ps", ".urf",
}

_START = time.time()


# --------------------------------------------------------------------------- #
# Encoding helpers
# --------------------------------------------------------------------------- #
def _attr(buf, tag, name, values):
    """Append an attribute (with one or more 1setOf values) to *buf*."""
    name_b = name.encode("utf-8")
    for i, val in enumerate(values):
        buf.append(tag)
        if i == 0:
            buf += struct.pack(">H", len(name_b)) + name_b
        else:
            buf += struct.pack(">H", 0)
        buf += struct.pack(">H", len(val)) + val


def _s(text):
    return text.encode("utf-8")


def _int(n):
    return struct.pack(">i", int(n))


def _bool(b):
    return struct.pack(">b", 1 if b else 0)


def _resolution(x, y, units=3):  # 3 == dots-per-inch
    return struct.pack(">iiB", x, y, units)


def _range(lo, hi):
    return struct.pack(">ii", lo, hi)


# --------------------------------------------------------------------------- #
# Request parsing
# --------------------------------------------------------------------------- #
def parse_ipp(body):
    """Return (version_bytes, op_id, request_id, attrs_dict, document_bytes)."""
    if len(body) < 8:
        return b"\x02\x00", None, 0, {}, b""
    version = bytes(body[0:2])
    op_id = struct.unpack(">H", body[2:4])[0]
    req_id = struct.unpack(">I", body[4:8])[0]
    i = 8
    attrs = {}
    last_name = None
    while i < len(body):
        tag = body[i]
        if tag == TAG_END:
            i += 1
            break
        if tag in (TAG_OPERATION, TAG_JOB, TAG_PRINTER, TAG_UNSUPPORTED):
            i += 1
            continue
        i += 1
        name_len = struct.unpack(">H", body[i:i + 2])[0]
        i += 2
        name = body[i:i + name_len].decode("utf-8", "replace")
        i += name_len
        val_len = struct.unpack(">H", body[i:i + 2])[0]
        i += 2
        val = body[i:i + val_len]
        i += val_len
        key = name if name else last_name
        if name:
            attrs[name] = val
            last_name = name
        elif key:  # additional 1setOf value — keep the first, ignore extras
            pass
    return version, op_id, req_id, attrs, body[i:]


# --------------------------------------------------------------------------- #
# Response builders
# --------------------------------------------------------------------------- #
def _response_head(req_id, status, version=b"\x02\x00"):
    buf = bytearray()
    buf += version                     # echo client's IPP version
    buf += struct.pack(">H", status)
    buf += struct.pack(">I", req_id)
    buf.append(TAG_OPERATION)
    _attr(buf, V_CHARSET, "attributes-charset", [_s("utf-8")])
    _attr(buf, V_NATLANG, "attributes-natural-language", [_s("en")])
    return buf


def printer_attributes(buf, printer_uri, name, color):
    buf.append(TAG_PRINTER)
    uptime = max(1, int(time.time() - _START))
    _attr(buf, V_URI, "printer-uri-supported", [_s(printer_uri)])
    _attr(buf, V_KEYWORD, "uri-authentication-supported", [_s("none")])
    _attr(buf, V_KEYWORD, "uri-security-supported", [_s("tls")])
    _attr(buf, V_NAME, "printer-name", [_s(name)])
    _attr(buf, V_TEXT, "printer-info", [_s(name)])
    _attr(buf, V_TEXT, "printer-make-and-model", [_s("Auto-Print Virtual Printer")])
    _attr(buf, V_TEXT, "printer-location", [_s("Auto-Print")])
    _attr(buf, V_URI, "printer-more-info", [_s(printer_uri)])
    _attr(buf, V_ENUM, "printer-state", [_int(3)])  # 3 == idle
    _attr(buf, V_KEYWORD, "printer-state-reasons", [_s("none")])
    _attr(buf, V_KEYWORD, "ipp-versions-supported", [_s("1.1"), _s("2.0")])
    _attr(buf, V_KEYWORD, "ipp-features-supported", [_s("airprint-1.7")])
    _attr(buf, V_ENUM, "operations-supported", [
        _int(OP_PRINT_JOB), _int(OP_VALIDATE_JOB), _int(OP_CREATE_JOB),
        _int(OP_SEND_DOCUMENT), _int(OP_CANCEL_JOB), _int(OP_GET_JOB_ATTRS),
        _int(OP_GET_JOBS), _int(OP_GET_PRINTER_ATTRS),
    ])
    _attr(buf, V_CHARSET, "charset-configured", [_s("utf-8")])
    _attr(buf, V_CHARSET, "charset-supported", [_s("utf-8")])
    _attr(buf, V_NATLANG, "natural-language-configured", [_s("en")])
    _attr(buf, V_NATLANG, "generated-natural-language-supported", [_s("en")])
    _attr(buf, V_MIMETYPE, "document-format-default", [_s("application/pdf")])
    _attr(buf, V_MIMETYPE, "document-format-supported", [
        _s("application/pdf"), _s("image/jpeg"),
        _s("image/png"), _s("application/octet-stream"),
    ])
    _attr(buf, V_MIMETYPE, "document-format-preferred", [_s("application/pdf")])
    _attr(buf, V_BOOLEAN, "printer-is-accepting-jobs", [_bool(True)])
    _attr(buf, V_INTEGER, "queued-job-count", [_int(0)])
    _attr(buf, V_KEYWORD, "pdl-override-supported", [_s("attempted")])
    _attr(buf, V_INTEGER, "printer-up-time", [_int(uptime)])
    _attr(buf, V_KEYWORD, "compression-supported", [_s("none")])
    _attr(buf, V_BOOLEAN, "color-supported", [_bool(color)])
    _attr(buf, V_KEYWORD, "print-color-mode-supported",
          [_s("color"), _s("monochrome")] if color else [_s("monochrome")])
    _attr(buf, V_KEYWORD, "print-color-mode-default",
          [_s("color")] if color else [_s("monochrome")])
    _attr(buf, V_KEYWORD, "sides-supported",
          [_s("one-sided"), _s("two-sided-long-edge"), _s("two-sided-short-edge")])
    _attr(buf, V_KEYWORD, "sides-default", [_s("one-sided")])
    _attr(buf, V_RESOLUTION, "printer-resolution-default", [_resolution(300, 300)])
    _attr(buf, V_RESOLUTION, "printer-resolution-supported", [_resolution(300, 300)])
    _attr(buf, V_INTEGER, "copies-default", [_int(1)])
    _attr(buf, V_RANGE, "copies-supported", [_range(1, 99)])
    _attr(buf, V_KEYWORD, "media-default", [_s("na_letter_8.5x11in")])
    _attr(buf, V_KEYWORD, "media-supported",
          [_s("na_letter_8.5x11in"), _s("iso_a4_210x297mm")])
    _attr(buf, V_KEYWORD, "media-ready",
          [_s("na_letter_8.5x11in"), _s("iso_a4_210x297mm")])
    # Do not advertise URF raster here: browser kiosk and default agent path
    # only support PDF/TXT/images listed above.
    _attr(buf, V_KEYWORD, "job-creation-attributes-supported",
          [_s("copies"), _s("sides"), _s("media"), _s("print-color-mode")])
    _attr(buf, V_BOOLEAN, "printer-supply-info-uri-supported", [_bool(False)])


def job_attributes(buf, printer_uri, job_id, state=3):
    buf.append(TAG_JOB)
    job_uri = printer_uri.rstrip("/") + "/" + str(job_id)
    _attr(buf, V_URI, "job-uri", [_s(job_uri)])
    _attr(buf, V_INTEGER, "job-id", [_int(job_id)])
    _attr(buf, V_ENUM, "job-state", [_int(state)])  # 3 pending,5 processing,9 completed
    _attr(buf, V_KEYWORD, "job-state-reasons", [_s("none")])


# --------------------------------------------------------------------------- #
# Printing
# --------------------------------------------------------------------------- #
def _detect_ext(data, declared_format):
    if data[:5] == b"%PDF-":
        return ".pdf"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] in (b"UNIR", b"RINU"):  # URF raster magic ("UNIRAST")
        return ".urf"
    if declared_format == "image/jpeg":
        return ".jpg"
    if declared_format == "image/png":
        return ".png"
    if declared_format == "image/urf":
        return ".urf"
    if declared_format == "application/pdf":
        return ".pdf"
    return ""


def print_document(data, target_printer, copies, declared_format, log):
    if not data:
        raise ValueError("empty document")
    ext = _detect_ext(data, declared_format)
    if ext not in ALLOWED_EXTENSIONS:
        ext = ".pdf"
    tmp_dir = tempfile.mkdtemp(prefix="autoprint_ipp_")
    try:
        path = os.path.join(tmp_dir, "job" + ext)
        with open(path, "wb") as f:
            f.write(data)
        cmd = ["lp"]
        if target_printer:
            cmd += ["-d", target_printer]
        if copies and copies > 1:
            cmd += ["-n", str(copies)]
        cmd.append(path)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "lp failed")
        m = re.search(r"request id is (\S+)", res.stdout)
        rid = m.group(1) if m else "submitted"
        log(f"  ✓ printed {len(data)} bytes ({ext}) → {target_printer or 'default'} [{rid}]")
        return rid
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# HTTP / IPP handler
# --------------------------------------------------------------------------- #
class IPPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # injected by factory
    target_printer = None
    printer_name = "Auto-Print"
    color = True
    public_host = None
    logfn = staticmethod(print)
    _job_counter = [0]
    _lock = threading.Lock()

    def log_message(self, *_args):  # silence default access logging
        pass

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
                    self.rfile.readline()  # trailing CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()      # CRLF after chunk
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _printer_uri(self):
        host = self.public_host or self.headers.get("Host") or "localhost"
        scheme = "ipps" if self.public_host else "ipp"
        return f"{scheme}://{host}/ipp/print"

    def _send_ipp(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # A browser hitting the endpoint gets a friendly note.
        msg = (f"Auto-Print IPP server is running.\n"
               f"Add this as a printer (IPP):\n  {self._printer_uri()}\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def do_POST(self):
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/ipp":
            self.send_error(400, "expected application/ipp")
            return
        body = self._read_body()
        version, op_id, req_id, attrs, doc = parse_ipp(body)
        printer_uri = self._printer_uri()

        if op_id == OP_GET_PRINTER_ATTRS:
            buf = _response_head(req_id, S_OK, version)
            printer_attributes(buf, printer_uri, self.printer_name, self.color)
            buf.append(TAG_END)
            self._send_ipp(bytes(buf))
            return

        if op_id == OP_VALIDATE_JOB:
            buf = _response_head(req_id, S_OK, version)
            buf.append(TAG_END)
            self._send_ipp(bytes(buf))
            return

        if op_id in (OP_PRINT_JOB, OP_CREATE_JOB, OP_SEND_DOCUMENT):
            with self._lock:
                self._job_counter[0] += 1
                job_id = self._job_counter[0]
            copies = 1
            fmt = ""
            declared = attrs.get("document-format")
            if declared:
                fmt = declared.decode("utf-8", "replace")
            cval = attrs.get("copies")
            if cval and len(cval) == 4:
                copies = struct.unpack(">i", cval)[0]

            state = 9  # completed
            if doc:
                try:
                    print_document(doc, self.target_printer, copies, fmt, self.logfn)
                except Exception as exc:  # noqa: BLE001
                    self.logfn(f"  ✗ print failed: {exc}")
                    state = 8  # aborted
            else:
                # Create-Job with no doc yet (Send-Document will follow).
                state = 3  # pending

            buf = _response_head(req_id, S_OK, version)
            job_attributes(buf, printer_uri, job_id, state)
            buf.append(TAG_END)
            self._send_ipp(bytes(buf))
            return

        if op_id in (OP_GET_JOBS, OP_GET_JOB_ATTRS):
            buf = _response_head(req_id, S_OK, version)
            if op_id == OP_GET_JOB_ATTRS:
                job_attributes(buf, printer_uri, 1, 9)
            buf.append(TAG_END)
            self._send_ipp(bytes(buf))
            return

        if op_id == OP_CANCEL_JOB:
            buf = _response_head(req_id, S_OK, version)
            buf.append(TAG_END)
            self._send_ipp(bytes(buf))
            return

        # Unknown operation.
        buf = _response_head(req_id, S_NOT_SUPPORTED, version)
        buf.append(TAG_END)
        self._send_ipp(bytes(buf))


def make_handler(target_printer, printer_name, color, public_host, logfn):
    return type("BoundIPPHandler", (IPPHandler,), {
        "target_printer": target_printer,
        "printer_name": printer_name,
        "color": color,
        "public_host": public_host,
        "logfn": staticmethod(logfn),
    })


def serve(port, target_printer, printer_name, color=True, public_host=None,
          logfn=print, ready=None):
    handler = make_handler(target_printer, printer_name, color, public_host, logfn)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    logfn(f"  IPP server listening on 0.0.0.0:{port}")
    if public_host:
        logfn(f"  Add this printer (over the internet): ipps://{public_host}/ipp/print")
    logfn(f"  Add this printer (same network):     ipp://{socket.gethostname()}:{port}/ipp/print")
    logfn(f"  Routing jobs to printer: {target_printer or 'system default'}")
    if ready:
        ready.set()
    httpd.serve_forever()
    return httpd


def main():
    ap = argparse.ArgumentParser(description="Auto-Print IPP server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("IPP_PORT", "8631")))
    ap.add_argument("--printer", default=os.environ.get("IPP_PRINTER", ""),
                    help="CUPS printer to route jobs to (default: system default)")
    ap.add_argument("--name", default=os.environ.get("IPP_NAME", "Auto-Print"),
                    help="Printer name shown to clients")
    ap.add_argument("--public-host", default=os.environ.get("IPP_PUBLIC_HOST", ""),
                    help="Public hostname behind your HTTPS tunnel, e.g. print.example.com")
    ap.add_argument("--mono", action="store_true", help="Advertise as monochrome only")
    args = ap.parse_args()

    print("=" * 60)
    print("  Auto-Print IPP server")
    print("=" * 60)
    serve(args.port, args.printer.strip() or None, args.name,
          color=not args.mono, public_host=args.public_host.strip() or None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nIPP server stopped.")
