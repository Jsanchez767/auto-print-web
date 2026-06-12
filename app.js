"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  key: $("key"),
  device: $("device"),
  printer: $("printer"),
  file: $("file"),
  drop: $("drop"),
  dropText: $("dropText"),
  fileList: $("fileList"),
  copies: $("copies"),
  send: $("send"),
  msg: $("msg"),
  jobs: $("jobs"),
  dot: $("dot"),
  connText: $("connText"),
  hostName: $("hostName"),
  ippUrl: $("ippUrl"),
  ippHost: $("ippHost"),
  ippQueue: $("ippQueue"),
  osSteps: $("osSteps"),
  copyIpp: $("copyIpp"),
  defaultRoute: $("defaultRoute"),
  setDefault: $("setDefault"),
  ippMsg: $("ippMsg"),
};

// Files staged for printing (DataTransfer lets us add across multiple drops).
let staged = new DataTransfer();

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
// File drag & drop (multiple)
// --------------------------------------------------------------------- //
const MAX_BYTES = 25 * 1024 * 1024;

function syncFileInput() {
  els.file.files = staged.files;
}

function addFiles(fileList) {
  for (const f of fileList) {
    // Skip exact duplicates (same name + size).
    let dup = false;
    for (const existing of staged.files) {
      if (existing.name === f.name && existing.size === f.size) { dup = true; break; }
    }
    if (!dup) staged.items.add(f);
  }
  syncFileInput();
  renderFileList();
}

function removeFile(index) {
  const next = new DataTransfer();
  Array.from(staged.files).forEach((f, i) => { if (i !== index) next.items.add(f); });
  staged = next;
  syncFileInput();
  renderFileList();
}

function clearFiles() {
  staged = new DataTransfer();
  syncFileInput();
  renderFileList();
}

function renderFileList() {
  const files = Array.from(staged.files);
  els.dropText.textContent = files.length
    ? `${files.length} file${files.length > 1 ? "s" : ""} selected — add more`
    : "Choose files or drop them here";
  els.fileList.innerHTML = "";
  files.forEach((f, i) => {
    const li = document.createElement("li");
    const over = f.size > MAX_BYTES;
    li.className = "file-item" + (over ? " over" : "");
    const meta = document.createElement("span");
    meta.className = "file-meta";
    meta.textContent = `${f.name} · ${formatSize(f.size)}${over ? " · too large" : ""}`;
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "file-rm";
    rm.textContent = "\u00d7";
    rm.title = "Remove";
    rm.addEventListener("click", (ev) => { ev.preventDefault(); removeFile(i); });
    li.appendChild(meta);
    li.appendChild(rm);
    els.fileList.appendChild(li);
  });
}

els.file.addEventListener("change", () => {
  // Browser replaces selection on each pick; merge into staged set.
  addFiles(els.file.files);
});
["dragenter", "dragover"].forEach((e) =>
  els.drop.addEventListener(e, (ev) => { ev.preventDefault(); els.drop.classList.add("hover"); })
);
["dragleave", "drop"].forEach((e) =>
  els.drop.addEventListener(e, (ev) => { ev.preventDefault(); els.drop.classList.remove("hover"); })
);
els.drop.addEventListener("drop", (ev) => {
  if (ev.dataTransfer.files.length) addFiles(ev.dataTransfer.files);
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
    const agents = data.agents || [];
    const onlineAgents = agents.filter((a) => a.online);
    setConn(data.online, hostSummary(onlineAgents));

    const prev = els.printer.value;
    els.printer.innerHTML = "";
    let optionCount = 0;
    agents.forEach((a) => {
      if (!a.printers || !a.printers.length) return;
      const group = document.createElement("optgroup");
      group.label = `${a.host || "computer"}${a.online ? "" : " (offline)"}`;
      a.printers.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = `${a.agent_id}||${p.name}`;
        opt.textContent = `${p.name} (${p.status})`;
        opt.disabled = !a.online;
        group.appendChild(opt);
        optionCount++;
      });
      els.printer.appendChild(group);
    });
    if (!optionCount) {
      els.printer.innerHTML = '<option value="">No printers shared yet</option>';
    } else if (prev) {
      els.printer.value = prev; // keep selection across refreshes
    }
    renderDefaultRoute(data.default_route, agents);
  } catch (e) {
    setConn(false);
  }
}

function renderDefaultRoute(route, agents) {
  if (!els.defaultRoute) return;
  if (route && route.printer) {
    const agent = (agents || []).find((a) => a.agent_id === route.agent_id);
    const host = agent ? agent.host : "";
    els.defaultRoute.textContent = host ? `${route.printer} @ ${host}` : route.printer;
  } else {
    els.defaultRoute.textContent = "none set";
  }
}

async function loadIppUrl() {
  if (!els.ippUrl) return;
  try {
    const res = await fetch("/api/info");
    const data = await res.json();
    if (data.ipp_uri) els.ippUrl.value = data.ipp_uri;
    const host = data.ipp_host || (data.ipp_uri || "").replace(/^[a-z]+:\/\//, "").split("/")[0] || "";
    const queue = data.ipp_queue || "ipp/print";
    if (els.ippHost) els.ippHost.value = host || "—";
    if (els.ippQueue) els.ippQueue.value = queue;
    renderOsSteps(currentOs, { host, queue, url: data.ipp_uri || "", secure: !!data.secure });
  } catch (e) {}
}

// --------------------------------------------------------------------- //
// "Add to your devices" — per-OS instructions
// --------------------------------------------------------------------- //
let currentOs = "mac";
let ippInfo = { host: "", queue: "ipp/print", url: "", secure: true };

function guessOs() {
  const ua = navigator.userAgent;
  if (/iPhone|iPad|iPod/.test(ua)) return "ios";
  if (/Android/.test(ua)) return "ios"; // closest guided path
  if (/Windows/.test(ua)) return "win";
  if (/Mac OS X|Macintosh/.test(ua)) return "mac";
  if (/Linux/.test(ua)) return "linux";
  return "mac";
}

function osStepsHtml(os, info) {
  const host = info.host || "—";
  const queue = info.queue || "ipp/print";
  const ippsUrl = info.url || "—";
  const httpsUrl = (info.secure ? "https://" : "http://") + host + "/" + queue;
  if (os === "mac") {
    return [
      "The <em>Add Printer</em> window won't work here (it uses port 631 without encryption). Use Terminal instead — it's two steps:",
      "Open <strong>Terminal</strong> (Applications → Utilities → Terminal).",
      "Paste this and press Return:",
      `<code>lpadmin -p AutoPrint -E -v "${ippsUrl}" -m everywhere</code>`,
      "If it replies <em>forbidden</em>, run it again with <code>sudo</code> in front and enter your Mac password.",
      "<strong>AutoPrint</strong> now appears in your printer list — print to it from any app.",
    ];
  }
  if (os === "win") {
    return [
      "Open <strong>Settings → Bluetooth &amp; devices → Printers &amp; scanners</strong>.",
      "Click <strong>Add device</strong>, wait, then <strong>Add manually</strong>.",
      "Choose <strong>Select a shared printer by name</strong> and paste:",
      `<code>${httpsUrl}</code>`,
      "Click <strong>Next</strong>, accept the <em>Generic / MS Publisher</em> driver, then <strong>Finish</strong>.",
    ];
  }
  if (os === "linux") {
    return [
      "Open a terminal and run (CUPS):",
      `<code>lpadmin -p AutoPrint -E -v "${ippsUrl}" -m everywhere</code>`,
      "Or in your printer settings, add a printer with connection URI:",
      `<code>${ippsUrl}</code>`,
      "Print to <strong>AutoPrint</strong> from any app.",
    ];
  }
  // iOS / mobile
  return [
    "On the <strong>same Wi-Fi</strong> as the printer computer, AirPrint shows it automatically — tap <strong>Share → Print</strong> and pick it.",
    "iPhone/iPad can't add a printer by address over the internet (iOS has no manual-add).",
    "From another network, use this website to upload and print instead.",
  ];
}

function renderOsSteps(os, info) {
  if (info) ippInfo = info;
  currentOs = os;
  if (!els.osSteps) return;
  document.querySelectorAll(".os-tab").forEach((t) =>
    t.classList.toggle("is-active", t.dataset.os === os));
  const steps = osStepsHtml(os, ippInfo);
  els.osSteps.innerHTML = "";
  steps.forEach((html) => {
    const li = document.createElement("li");
    // Lines that are pure notes (start with lowercase advice) stay as steps;
    // mark explicit notes for iOS.
    li.innerHTML = html;
    els.osSteps.appendChild(li);
  });
}

document.querySelectorAll(".os-tab").forEach((tab) => {
  tab.addEventListener("click", () => renderOsSteps(tab.dataset.os));
});

// Generic copy buttons (data-copy="elementId").
document.querySelectorAll("[data-copy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const target = document.getElementById(btn.dataset.copy);
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.value);
      ippShow("Copied ✓", true);
    } catch (e) {
      target.select();
      ippShow("Press ⌘C / Ctrl-C to copy.", true);
    }
  });
});


function ippShow(text, ok) {
  if (!els.ippMsg) return;
  els.ippMsg.textContent = text;
  els.ippMsg.className = "msg " + (ok ? "ok" : "err");
}

if (els.copyIpp) {
  els.copyIpp.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(els.ippUrl.value);
      ippShow("Copied ✓", true);
    } catch (e) {
      els.ippUrl.select();
      ippShow("Press ⌘C to copy.", true);
    }
  });
}

if (els.setDefault) {
  els.setDefault.addEventListener("click", async () => {
    const selected = els.printer.value;
    if (!selected || selected.indexOf("||") === -1) {
      return ippShow("Pick a printer above first.", false);
    }
    const [agentId, printer] = selected.split("||");
    try {
      const res = await fetch("/api/default", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ agent_id: agentId, printer }),
      });
      if (res.ok) {
        ippShow(`Added devices will now print to ${printer} ✓`, true);
        loadPrinters();
      } else {
        ippShow("Could not set default (check access key).", false);
      }
    } catch (e) {
      ippShow("Network error: " + e.message, false);
    }
  });
}

function hostSummary(onlineAgents) {
  if (!onlineAgents.length) return null;
  if (onlineAgents.length === 1) return onlineAgents[0].host;
  return `${onlineAgents.length} computers`;
}

// --------------------------------------------------------------------- //
// Activity feed (polling)
// --------------------------------------------------------------------- //
function renderJobs(jobs) {
  els.jobs.innerHTML = "";
  if (!jobs.length) {
    els.jobs.innerHTML = '<li class="empty">No jobs yet.</li>';
    return;
  }
  jobs.forEach((job) => {
    const li = document.createElement("li");
    li.className = "job " + job.status;
    const time = new Date(job.ts * 1000).toLocaleTimeString();
    const target = job.host ? `${job.printer || "printer"} @ ${job.host}` : (job.printer || "default printer");
    const detail = job.status === "error"
      ? job.detail
      : `${target} · ${job.copies} cop${job.copies > 1 ? "ies" : "y"}${job.detail ? " · " + job.detail : ""}`;
    li.innerHTML = `
      <div class="line1">
        <span class="name"></span>
        <span class="badge">${job.status}</span>
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
  const selected = els.printer.value;
  const copies = parseInt(els.copies.value, 10) || 1;

  if (!selected || selected.indexOf("||") === -1) {
    return showMsg("Choose a printer first.", false);
  }
  const [agentId, printer] = selected.split("||");
  const hostLabel = els.printer.selectedOptions[0]
    ? els.printer.selectedOptions[0].parentNode.label.replace(/ \(offline\)$/, "")
    : "";

  const files = Array.from(staged.files);
  if (!files.length) return showMsg("Choose at least one file.", false);
  const tooBig = files.filter((f) => f.size > MAX_BYTES);
  if (tooBig.length) {
    return showMsg(`Too large (25 MB max): ${tooBig.map((f) => f.name).join(", ")}`, false);
  }

  els.send.disabled = true;
  let ok = 0;
  const failed = [];
  for (const f of files) {
    showMsg(`Sending ${f.name}…`, true);
    const payload = {
      device, agent_id: agentId, host: hostLabel, printer, copies, kind: "file",
      filename: f.name,
      content: await readFileAsBase64(f),
    };
    try {
      const res = await fetch("/api/print", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (res.ok && data.job) ok++;
      else failed.push(`${f.name}: ${data.error || "error"}`);
    } catch (e) {
      failed.push(`${f.name}: ${e.message}`);
    }
  }

  if (failed.length) {
    showMsg(`Queued ${ok}/${files.length}. Failed — ${failed.join("; ")}`, false);
  } else {
    showMsg(`Queued ${ok} file${ok > 1 ? "s" : ""} ✓ — printing shortly.`, true);
    clearFiles();
  }
  loadJobs();
  els.send.disabled = false;
});

// --------------------------------------------------------------------- //
// Init + polling loops
// --------------------------------------------------------------------- //
currentOs = guessOs();
loadIppUrl();
loadPrinters();
loadJobs();
setInterval(loadPrinters, 10000);
setInterval(loadJobs, 3000);
