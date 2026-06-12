"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  key: $("key"),
  device: $("device"),
  printer: $("printer"),
  text: $("text"),
  file: $("file"),
  drop: $("drop"),
  dropText: $("dropText"),
  copies: $("copies"),
  send: $("send"),
  msg: $("msg"),
  jobs: $("jobs"),
  dot: $("dot"),
  connText: $("connText"),
  hostName: $("hostName"),
};

let currentTab = "text";

// --------------------------------------------------------------------- //
// Persisted fields
// --------------------------------------------------------------------- //
els.device.value = localStorage.getItem("ap_device") || "";
els.key.value = localStorage.getItem("ap_key") || "";
els.device.addEventListener("input", () => localStorage.setItem("ap_device", els.device.value));
els.key.addEventListener("input", () => {
  localStorage.setItem("ap_key", els.key.value);
  loadPrinters();
  loadJobs();
});

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (els.key.value) h["X-Access-Key"] = els.key.value;
  return h;
}

// --------------------------------------------------------------------- //
// Tabs
// --------------------------------------------------------------------- //
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    currentTab = btn.dataset.tab;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.remove("active"));
    $("tab-" + currentTab).classList.add("active");
  });
});

// --------------------------------------------------------------------- //
// File drag & drop
// --------------------------------------------------------------------- //
function updateDropLabel() {
  const f = els.file.files[0];
  els.dropText.textContent = f
    ? `📄 ${f.name} (${formatSize(f.size)})`
    : "Click to choose a file or drop it here (max 3 MB)";
}
els.file.addEventListener("change", updateDropLabel);
["dragenter", "dragover"].forEach((e) =>
  els.drop.addEventListener(e, (ev) => { ev.preventDefault(); els.drop.classList.add("hover"); })
);
["dragleave", "drop"].forEach((e) =>
  els.drop.addEventListener(e, (ev) => { ev.preventDefault(); els.drop.classList.remove("hover"); })
);
els.drop.addEventListener("drop", (ev) => {
  if (ev.dataTransfer.files.length) {
    els.file.files = ev.dataTransfer.files;
    updateDropLabel();
  }
});

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

// --------------------------------------------------------------------- //
// Connection / printers
// --------------------------------------------------------------------- //
function setConn(online, host) {
  els.dot.className = "dot " + (online ? "on" : "off");
  els.connText.textContent = online ? "printer online" : "printer offline";
  if (host) els.hostName.textContent = host;
}

async function loadPrinters() {
  try {
    const res = await fetch("/api/printers", { headers: authHeaders() });
    if (res.status === 401) { setConn(false); els.connText.textContent = "enter access key"; return; }
    const data = await res.json();
    setConn(data.online, data.host);
    els.printer.innerHTML = "";
    if (!data.printers || !data.printers.length) {
      els.printer.innerHTML = '<option value="">No printers reported yet</option>';
      return;
    }
    data.printers.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = `${p.name} (${p.status})`;
      if (p.name === data.default) opt.selected = true;
      els.printer.appendChild(opt);
    });
  } catch (e) {
    setConn(false);
  }
}

// --------------------------------------------------------------------- //
// Activity feed (polling)
// --------------------------------------------------------------------- //
function renderJobs(jobs) {
  els.jobs.innerHTML = "";
  if (!jobs.length) {
    els.jobs.innerHTML = '<li class="empty">No jobs yet. Send your first print!</li>';
    return;
  }
  jobs.forEach((job) => {
    const li = document.createElement("li");
    li.className = "job " + job.status;
    const time = new Date(job.ts * 1000).toLocaleTimeString();
    const detail = job.status === "error"
      ? job.detail
      : `${job.printer || "default printer"} · ${job.copies} cop${job.copies > 1 ? "ies" : "y"}${job.detail ? " · " + job.detail : ""}`;
    li.innerHTML = `
      <div class="line1">
        <span class="name"></span>
        <span class="status">${job.status}</span>
      </div>
      <div class="line2"></div>`;
    li.querySelector(".name").textContent = `${job.name || "(untitled)"} — ${job.device}`;
    li.querySelector(".line2").textContent = `${time} · ${detail}`;
    els.jobs.appendChild(li);
  });
}

async function loadJobs() {
  try {
    const res = await fetch("/api/jobs", { headers: authHeaders() });
    if (res.status === 401) return;
    const data = await res.json();
    if (data.jobs) renderJobs(data.jobs);
  } catch (e) {}
}

// --------------------------------------------------------------------- //
// Send
// --------------------------------------------------------------------- //
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function showMsg(text, ok) {
  els.msg.textContent = text;
  els.msg.className = "msg " + (ok ? "ok" : "err");
}

els.send.addEventListener("click", async () => {
  const device = els.device.value.trim() || "Unknown device";
  const printer = els.printer.value;
  const copies = parseInt(els.copies.value, 10) || 1;
  const payload = { device, printer, copies, kind: currentTab };

  if (currentTab === "file") {
    const f = els.file.files[0];
    if (!f) return showMsg("Choose a file first.", false);
    if (f.size > 3 * 1024 * 1024) return showMsg("File exceeds 3 MB (Vercel limit).", false);
    payload.filename = f.name;
    payload.content = await readFileAsBase64(f);
  } else {
    if (!els.text.value.trim()) return showMsg("Type something to print.", false);
    payload.text = els.text.value;
  }

  els.send.disabled = true;
  showMsg("Sending…", true);
  try {
    const res = await fetch("/api/print", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok && data.job) {
      showMsg("Queued ✓ — the printer computer will print it shortly.", true);
      els.text.value = "";
      els.file.value = "";
      updateDropLabel();
      loadJobs();
    } else {
      showMsg("Failed: " + (data.error || "unknown error"), false);
    }
  } catch (e) {
    showMsg("Network error: " + e.message, false);
  } finally {
    els.send.disabled = false;
  }
});

// --------------------------------------------------------------------- //
// Init + polling loops
// --------------------------------------------------------------------- //
loadPrinters();
loadJobs();
setInterval(loadPrinters, 10000);
setInterval(loadJobs, 3000);
