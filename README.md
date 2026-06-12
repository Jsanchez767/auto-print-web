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

## Two ways to host

| | **Vercel** (serverless) | **Render** (always-on) — recommended |
|---|---|---|
| File size limit | 4.5 MB / request | none (large PDFs/photos OK) |
| Queue backend | Upstash Redis | in-memory (no external DB) |
| **Add as a native printer (AirPrint/IPP)** | ❌ | ✅ `cloud_server.py` |
| Custom domain | DNS on Vercel | CNAME → Render (no nameserver move) |
| Server file | `api/*.py` | `cloud_server.py` |

Both still need **`agent.py`** running on the printer computer — the cloud can't
physically reach a printer.

---

## Add it as a printer on your devices (AirPrint / IPP)

With the Render server (`cloud_server.py`), the app exposes an **IPP endpoint** so
any Mac, iPhone, iPad, Windows or Linux device can add it as a normal printer and
use the native print dialog. Jobs go to whichever printer is set as the **default
on the site** ("Add to your devices" card → *Use selected printer*).

```
 Any device          Render (cloud)                 Your printer computer
 ┌───────────┐ ipps  ┌────────────────────┐  poll   ┌────────────────────┐
 │ Print →   │ ────▶ │ /ipp/print  +  UI  │ ◀────── │ agent.py           │
 │ Auto-Print│       │ in-memory queue    │ ──jobs▶ │  → lp → Printer     │
 └───────────┘       └────────────────────┘ status  └────────────────────┘
```

**Add the printer:**
- **macOS:** System Settings → Printers & Scanners → Add Printer → **IP** tab →
  Address `print.yourdomain.com`, Protocol **Internet Printing Protocol – IPP**,
  Queue `ipp/print`. (Use the exact `ipps://…/ipp/print` URL from the site's
  "Add to your devices" card.)
- **iPhone/iPad:** native AirPrint discovery works on the same network; for
  internet use, devices print through the site or a configured IPP profile.
- The site shows the exact address to copy.

> Security note: the IPP endpoint accepts jobs and sends them to the **default
> printer chosen on the site** — pick that default deliberately.

---

## Deploy to Render (recommended)

1. Push this repo to GitHub (see below).
2. Render dashboard → **New → Blueprint** → pick this repo. It reads
   [`render.yaml`](render.yaml) and creates a `web` service running
   `python3 cloud_server.py`.
3. Set env vars in the dashboard:
   - `ACCESS_KEY` — what users type in the web UI
   - `AGENT_TOKEN` — what the local agent sends
   - `PUBLIC_HOST` *(optional)* — your custom domain, e.g. `print.maticsapp.com`
4. **Custom domain (no nameserver change):** Render service → Settings → Custom
   Domains → add `print.maticsapp.com`. Render shows a CNAME target; add that as a
   **CNAME record** in your existing Vercel DNS. Render issues TLS automatically.
5. Run the agent on the printer computer, pointed at the Render URL:
   ```bash
   python3 agent.py --relay https://print.maticsapp.com --token "$AGENT_TOKEN"
   ```
6. Open the site, enter the access key, pick a printer, and click **Use selected
   printer** in the "Add to your devices" card to make it the AirPrint target.

> **Cold starts:** Render's free tier sleeps after ~15 min idle; the first print
> may time out while it wakes. The **Starter** plan stays always-on.

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
├── ipp_server.py                   # IPP/AirPrint codec + local IPP server (agent --ipp)
├── cloud_server.py                 # always-on server for Render (UI + relay + IPP)
├── server.py                       # optional: local-only LAN mode (no cloud)
├── render.yaml                     # Render blueprint
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

The agent runs on the machine connected to the printer. It only needs Python 3.

**macOS / Linux** — uses the built-in `lp`/`lpstat` commands:

```bash
python3 agent.py --relay https://your-app.vercel.app --token YOUR_AGENT_TOKEN
```

**Windows** — uses built-in PowerShell. Install Python 3 from python.org, then:

```powershell
python agent.py --relay https://your-app.vercel.app --token YOUR_AGENT_TOKEN
```

- Listing printers and **plain-text** printing work with no extra software
  (PowerShell `Get-Printer` / `Out-Printer`).
- **PDFs and images** need a silent printer. The easiest is the free
  **SumatraPDF** — download the **portable** `.exe` (no admin/install required)
  and drop it in the same folder as `agent.py`. Adobe Reader also works if it's
  already installed. Without one of these, PDF jobs report a clear error.

Or with environment variables (see `.env.example`):

```bash
RELAY_URL=https://your-app.vercel.app AGENT_TOKEN=YOUR_AGENT_TOKEN python3 agent.py
```

You should see it report its printers and start polling. Leave it running.

> Work computers: the agent only makes **outbound** HTTPS calls, so it usually
> works without firewall changes — but installing Python (or SumatraPDF) may need
> IT approval on locked-down machines.

> Tip: to keep it running after you close the terminal, use `nohup`/`tmux` or a
> `launchd`/`systemd` service (macOS/Linux), or Task Scheduler (Windows).

---

## Sharing printers from more than one computer

Anyone can share their printers — the app supports **multiple printer-host
computers at once**, on any mix of macOS, Linux, and Windows. So you can connect
your **Windows work PC** and print to its office printers from your Mac at home.
Each person just runs their own agent:

1. Copy this project (or just `agent.py`) to their computer.
2. Run it with the **same relay URL and `AGENT_TOKEN`**, and an optional
   friendly name:

   ```bash
   python3 agent.py \
     --relay https://your-app.vercel.app \
     --token YOUR_AGENT_TOKEN \
     --name "Work PC"
   ```

Each agent automatically gets a **stable, unique id** and registers its own
printers. In the web app, the printer dropdown groups printers **by computer**,
e.g.:

```
▾ Jesus' MacBook
    HP OfficeJet Pro 9010 (idle)
    HP Color LaserJet (idle)
▾ Work PC
    Office HP LaserJet (idle)
    Front Desk Brother (idle)
```

Pick any printer and the job is routed to that specific computer's agent.
Offline computers are shown greyed-out. Everyone shares one `AGENT_TOKEN`; if you
want per-person revocation instead, that can be added later.

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
