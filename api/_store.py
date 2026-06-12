"""
Shared helpers for the Vercel relay API: Upstash Redis (REST) client, auth, and
job-store operations. Files starting with "_" are not treated as routes by
Vercel, but can be imported by the route handlers in this directory.

Storage backend: Upstash Redis via its REST API (stdlib only, no pip deps).
Set up via Vercel's Upstash/KV integration, which provides these env vars:
  KV_REST_API_URL / KV_REST_API_TOKEN     (Vercel KV integration)
  UPSTASH_REDIS_REST_URL / ..._TOKEN       (Upstash integration)
Either pair works.

Auth env vars:
  ACCESS_KEY   - required from browsers to view/print (X-Access-Key header)
  AGENT_TOKEN  - required from the local agent (X-Agent-Token header)
"""

import json
import os
import time
import urllib.request

# --------------------------------------------------------------------------- #
# Upstash Redis REST client
# --------------------------------------------------------------------------- #

_REDIS_URL = (
    os.environ.get("KV_REST_API_URL")
    or os.environ.get("UPSTASH_REDIS_REST_URL")
    or ""
).rstrip("/")
_REDIS_TOKEN = (
    os.environ.get("KV_REST_API_TOKEN")
    or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    or ""
)

FEED_KEY = "feed"        # list of job ids, newest first
QUEUE_KEY = "queue"      # list of pending job ids for the agent (FIFO)
PRINTERS_KEY = "printers"  # JSON: {host, printers, ts}
JOB_TTL = 3600           # seconds a job record lives
MAX_FEED = 100


class StoreError(RuntimeError):
    pass


def _request(path, body):
    if not _REDIS_URL or not _REDIS_TOKEN:
        raise StoreError(
            "Storage is not configured. Set KV_REST_API_URL/KV_REST_API_TOKEN "
            "(or UPSTASH_REDIS_REST_URL/TOKEN) in your Vercel environment."
        )
    req = urllib.request.Request(
        f"{_REDIS_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_REDIS_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def redis(*args):
    """Run a single Redis command, return its result."""
    out = _request("", list(args))
    if isinstance(out, dict) and "error" in out:
        raise StoreError(out["error"])
    return out.get("result") if isinstance(out, dict) else out


def pipeline(commands):
    """Run several Redis commands in one round-trip, return list of results."""
    out = _request("/pipeline", commands)
    results = []
    for item in out:
        if isinstance(item, dict) and "error" in item:
            raise StoreError(item["error"])
        results.append(item.get("result") if isinstance(item, dict) else item)
    return results


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def access_ok(handler):
    expected = os.environ.get("ACCESS_KEY", "")
    if not expected:
        return True  # no key configured -> open
    given = handler.headers.get("X-Access-Key", "")
    return _const_eq(given, expected)


def agent_ok(handler):
    expected = os.environ.get("AGENT_TOKEN", "")
    if not expected:
        return True
    given = handler.headers.get("X-Agent-Token", "")
    return _const_eq(given, expected)


def _const_eq(a, b):
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


# --------------------------------------------------------------------------- #
# HTTP helpers (used by route handlers)
# --------------------------------------------------------------------------- #

def send_json(handler, obj, status=200):
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Job-store operations
# --------------------------------------------------------------------------- #

def enqueue_job(job):
    """Store a full job (incl. content) and queue it for the agent + feed."""
    jid = job["id"]
    pipeline([
        ["SET", f"job:{jid}", json.dumps(job), "EX", str(JOB_TTL)],
        ["RPUSH", QUEUE_KEY, jid],
        ["LPUSH", FEED_KEY, jid],
        ["LTRIM", FEED_KEY, "0", str(MAX_FEED - 1)],
    ])


def next_queued_job():
    """Pop the oldest queued job id and return its full record (or None)."""
    jid = redis("LPOP", QUEUE_KEY)
    if not jid:
        return None
    raw = redis("GET", f"job:{jid}")
    if not raw:
        return None
    return json.loads(raw)


def get_job(jid):
    raw = redis("GET", f"job:{jid}")
    return json.loads(raw) if raw else None


def update_job(jid, **fields):
    job = get_job(jid)
    if not job:
        return None
    job.update(fields)
    # Drop heavy content once the job is done to save memory.
    if fields.get("status") in ("printed", "error"):
        job.pop("content", None)
    redis("SET", f"job:{jid}", json.dumps(job), "EX", str(JOB_TTL))
    return job


def feed(limit=MAX_FEED):
    ids = redis("LRANGE", FEED_KEY, "0", str(limit - 1)) or []
    if not ids:
        return []
    results = pipeline([["GET", f"job:{i}"] for i in ids])
    jobs = []
    for raw in results:
        if raw:
            j = json.loads(raw)
            j.pop("content", None)  # never expose file content in the feed
            jobs.append(j)
    return jobs


def set_printers(host, printers):
    payload = {"host": host, "printers": printers, "ts": time.time()}
    redis("SET", PRINTERS_KEY, json.dumps(payload), "EX", "120")


def get_printers():
    raw = redis("GET", PRINTERS_KEY)
    if not raw:
        return {"host": None, "printers": [], "online": False, "ts": 0}
    data = json.loads(raw)
    data["online"] = (time.time() - data.get("ts", 0)) < 90
    return data
