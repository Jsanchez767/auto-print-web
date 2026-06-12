# Auto-Print Web 🖨️

Send a print job from **any computer on the internet** and have it print
**automatically** on the one computer that's connected to your printer — no print
dialog, no port-forwarding.

It works in two parts:

```
 Any computer (internet)        Vercel (cloud)              Your printer computer
 ┌──────────────────┐  HTTPS   ┌─────────────────┐  poll   ┌──────────────────────┐
 │   Web UI         │ ───────▶ │  Relay API      │ ◀────── │  agent.py            │
 │  (type/upload)   │          │  (Upstash queue │ ──jobs▶ │  receives job → lp → │
 └──────────────────┘          │   + activity)   │ status  │  Printer             │
                               └─────────────────┘ ◀────── └──────────────────────┘
```

- **Vercel** hosts the web UI and a small relay API (a job queue, backed by
  Upstash Redis).
- **`agent.py`** runs on the computer connected to the printer. It dials *out* to
  Vercel (so no firewall changes are needed), pulls queued jobs, prints them
  silently with the CUPS `lp` command, and reports status back.

> Browsers can't print silently on their own, and a Vercel function in the cloud
> can't reach your physical printer. The agent bridges that gap.

---

## Project layout

```
auto-print-web/
├── index.html, app.js, style.css   # web UI (served by Vercel)
├── api/                            # Vercel Python serverless functions
│   ├── _store.py                   # Upstash Redis client + helpers
│   ├── print.py      POST /api/print      (browser enqueues a job)
│   ├── jobs.py       GET  /api/jobs        (activity feed)
│   ├── printers.py   GET  /api/printers    (printers reported by the agent)
│   ├── poll.py       GET  /api/poll        (agent pulls next job)
│   ├── heartbeat.py  POST /api/heartbeat   (agent reports printers)
│   └── status.py     POST /api/status      (agent reports job result)
├── agent.py                        # runs on the printer computer
├── server.py                       # optional: local-only LAN mode (no cloud)
├── vercel.json
└── .env.example
```

---

## Deploy to Vercel

### 1. Push to GitHub
Create a repo on GitHub, then from this folder:

```bash
git remote add origin https://github.com/<you>/auto-print-web.git
git branch -M main
git push -u origin main
```

### 2. Import into Vercel
- Go to [vercel.com/new](https://vercel.com/new) and import the GitHub repo.
- Framework preset: **Other** (the defaults are fine — `api/*.py` is auto-detected).

### 3. Add Upstash Redis (the job queue)
- In your Vercel project: **Storage → Create Database → Upstash for Redis** (free
  tier) and connect it to the project.
- This automatically adds `KV_REST_API_URL` and `KV_REST_API_TOKEN` (or
  `UPSTASH_REDIS_REST_URL` / `..._TOKEN`) to your environment. No code changes
  needed — `api/_store.py` reads either pair.

### 4. Set the access secrets
In **Settings → Environment Variables**, add:

| Name          | Value                              | Used by                       |
|---------------|------------------------------------|-------------------------------|
| `ACCESS_KEY`  | a key you give to people who print | the web UI                    |
| `AGENT_TOKEN` | a long random string               | `agent.py` on the printer PC  |

Redeploy after adding them.

---

## Run the agent (on the printer's computer)

This is the macOS/Linux machine connected to the printer. It only needs Python 3
and the built-in `lp`/`lpstat` commands.

```bash
python3 agent.py --relay https://your-app.vercel.app --token YOUR_AGENT_TOKEN
```

Or with environment variables (see `.env.example`):

```bash
RELAY_URL=https://your-app.vercel.app AGENT_TOKEN=YOUR_AGENT_TOKEN python3 agent.py
```

You should see it report its printers and start polling. Leave it running.

> Tip: to keep it running after you close the terminal, use `nohup`, a `tmux`
> session, or a `launchd`/`systemd` service.

---

## Use it

1. Open `https://your-app.vercel.app` on any computer or phone.
2. Enter the **access key** (the `ACCESS_KEY` you set on Vercel).
3. Pick a printer (the list comes from the agent), type text or choose a file,
   and press **Print now**.
4. The job is queued; the agent prints it within a couple of seconds. The live
   feed shows status on every connected device.

**File limits:** max **3 MB** per job (Vercel caps serverless request bodies at
~4.5 MB). Supported: PDF, TXT, PNG, JPG, GIF, BMP, DOC, DOCX, RTF, ODT, PS.

---

## Optional: local-only mode (no cloud)

If both computers are on the same LAN and you don't need the internet, you can
skip Vercel entirely and run the original direct server on the printer computer:

```bash
python3 server.py            # serves the UI + prints directly via lp
```

Then open the printed `http://<lan-ip>:8000` URL from the other computer.

---

## Security notes

- The Vercel URL is public, so **always set `ACCESS_KEY` and `AGENT_TOKEN`** to
  long random strings. Without `ACCESS_KEY`, anyone with the URL can print.
- The agent connects outbound only — your printer is never exposed to the
  internet directly.
- Temp files are deleted immediately after each job is submitted to CUPS.
- Job content lives in Redis only briefly (1-hour TTL) and is dropped from the
  feed once printed.
