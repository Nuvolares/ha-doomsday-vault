"use strict";

// ---------- helpers ----------
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const BUSY = ["queued", "checking", "downloading", "verifying", "indexing"];
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function timeAgo(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (_) { /* storage unavailable: fine */ }
  return null;
}

async function api(path, options = {}) {
  const init = { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const resp = await fetch(path, init);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `${resp.status} ${resp.statusText}`);
  return data;
}

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

function statusPill(src) {
  if (!src.enabled) return `<span class="pill idle">Offline</span>`;
  if (src.status === "ready") return `<span class="pill ready">Online</span>`;
  if (src.status === "error") return `<span class="pill error">Fault</span>`;
  if (BUSY.includes(src.status)) return `<span class="pill busy">${esc(src.status)}</span>`;
  return `<span class="pill idle">${src.held ? "Cancelled" : "Not stocked"}</span>`;
}

function progressInfo(src, now) {
  const p = src.progress;
  if (!p || !p.total) return null;
  const pct = Math.min(100, (p.done / p.total) * 100);
  const elapsed = Math.max(1, now - p.started);
  const speed = (p.done - (p.bytes_at_start || 0)) / elapsed;
  const eta = speed > 0 ? (p.total - p.done) / speed : null;
  return { pct, speed, eta };
}

function fmtEta(sec) {
  if (sec == null || !isFinite(sec)) return "";
  if (sec < 90) return `${Math.round(sec)} s`;
  if (sec < 5400) return `${Math.round(sec / 60)} min`;
  return `${(sec / 3600).toFixed(1)} h`;
}

// ---------- state ----------
const ui = { tab: "library", overview: null, mapId: null, lastQuery: "" };

// ---------- theme ----------
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#theme-toggle").textContent = theme === "green" ? "Amber" : "Green";
}
applyTheme(store("dv-theme") === "amber" ? "amber" : "green");
$("#theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "green" ? "amber" : "green";
  applyTheme(next);
  store("dv-theme", next);
});

// ---------- boot sequence ----------
let bootDone = false;
function finishBoot() {
  if (bootDone) return;
  bootDone = true;
  $("#boot").classList.add("done");
  try { sessionStorage.setItem("dv-booted", "1"); } catch (_) {}
  setTimeout(() => $("#boot").remove(), 500);
}

async function runBoot(overviewPromise) {
  let seen = false;
  try { seen = sessionStorage.getItem("dv-booted") === "1"; } catch (_) {}
  if (seen) { finishBoot(); return; }
  ["click", "keydown"].forEach((ev) => document.addEventListener(ev, finishBoot, { once: true }));

  const out = $("#boot-text");
  const type = async (line) => {
    if (REDUCED_MOTION) { out.textContent += line + "\n"; return; }
    for (const ch of line) {
      if (bootDone) return;
      out.textContent += ch;
      await new Promise((r) => setTimeout(r, ch === "." ? 6 : 14));
    }
    out.textContent += "\n";
  };
  await type("DOOMSDAY VAULT TERMINAL  v0.5");
  await type("> INITIALIZING ARCHIVE CORE .......... OK");
  await type("> MOUNTING KNOWLEDGE VAULT ........... OK");
  const o = await overviewPromise.catch(() => null);
  if (o) {
    const ready = o.sources.filter((s) => s.enabled && s.status === "ready").length;
    await type(`> ${ready} VOLUMES ONLINE · ${fmtBytes(o.disk.library)} PRESERVED`);
    await type(o.initiated ? "> WELCOME BACK, SURVIVOR." : "> VAULT EMPTY. AWAITING STOCKING ORDER.");
  } else {
    await type("> ARCHIVE LINK ............... FAULT");
  }
  setTimeout(finishBoot, REDUCED_MOTION ? 400 : 900);
}

// ---------- tabs ----------
function showTab(name) {
  ui.tab = name;
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.setAttribute("aria-selected", String(b.dataset.tab === name));
    if (b.dataset.tab === name) b.scrollIntoView({ block: "nearest", inline: "nearest" });  // narrow screens scroll the tab bar
  });
  document.querySelectorAll(".tab").forEach((t) => { t.hidden = t.id !== `tab-${name}`; });
  render();
  if (name === "search") $("#search-q").focus();
  if (name === "inventory") loadInventory();
  if (name === "armory") loadArmory();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------- reader ----------
// Everything opens inside the panel; PDFs go through the built-in viewer so the
// Home Assistant mobile app never hands them to an external browser.
let readerDepth = 0;

// opts.q: search query to highlight; opts.page: PDF page to open at.
function readerUrl(url, opts = {}) {
  const abs = new URL(url, location.href);
  if (/\.pdf$/i.test(abs.pathname)) {
    const params = new URLSearchParams({ file: abs.pathname + abs.search });
    if (opts.page) params.set("page", opts.page);
    if (opts.q) params.set("q", opts.q);
    return `static/pdf.html?${params}`;
  }
  if (opts.q) {
    // reader.js inside the article picks this up, highlights the terms and scrolls to the best passage.
    try { sessionStorage.setItem("dv-highlight", JSON.stringify({ q: opts.q, t: Date.now() })); } catch (_) {}
  }
  return url;
}

function openReader(url, title, opts = {}) {
  readerDepth = 0;
  $("#reader-back").disabled = true;
  $("#reader-title").textContent = title || "";
  $("#reader-frame").src = readerUrl(url, opts);
  $("#reader").hidden = false;
  $("#reader-close").focus();
}
function closeReader() {
  $("#reader").hidden = true;
  $("#reader-frame").src = "about:blank";
}
$("#reader-frame").addEventListener("load", () => {
  if ($("#reader").hidden) return;
  readerDepth++;
  $("#reader-back").disabled = readerDepth <= 1;
  try {
    const title = $("#reader-frame").contentDocument.title;
    if (title) $("#reader-title").textContent = title;
  } catch (_) { /* cross-origin page: keep the existing title */ }
});
$("#reader-back").addEventListener("click", () => {
  if (readerDepth <= 1) return;
  readerDepth -= 2; // the load event after going back adds one again
  $("#reader-frame").contentWindow.history.back();
});
$("#reader-close").addEventListener("click", closeReader);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#reader").hidden) closeReader(); });

// ---------- rendering ----------
function groupByCategory(list) {
  const groups = new Map();
  for (const s of list) {
    if (!groups.has(s.category)) groups.set(s.category, []);
    groups.get(s.category).push(s);
  }
  return groups;
}

function renderHeader(o) {
  const ready = o.sources.filter((s) => s.enabled && s.status === "ready").length;
  const enabled = o.sources.filter((s) => s.enabled).length;
  let line = `${ready}/${enabled} volumes online · ${fmtBytes(o.disk.library)} archived · ${fmtBytes(o.disk.free)} free`;
  if (o.portable) line = `Offline copy · ${ready} volumes · ${fmtBytes(o.disk.library)}${o.export ? ` · exported ${o.export.exported_at.slice(0, 10)}` : ""}`;
  if (!o.portable && EXPORT_BUSY.includes(o.export?.state)) {
    line += ` · exporting ${o.export.total ? `${((o.export.done / o.export.total) * 100).toFixed(0)}%` : "…"}`;
  }
  if (o.current) {
    const cur = o.sources.find((s) => s.id === o.current);
    const p = cur && progressInfo(cur, o.now);
    line += ` · receiving ${cur ? cur.name : ""}${p ? ` ${p.pct.toFixed(0)}%` : ""}`;
  }
  $("#status-line").textContent = line;
}

function renderLibrary(o) {
  const el = $("#tab-library");
  const enabled = o.sources.filter((s) => s.enabled);
  const total = enabled.reduce((sum, s) => sum + (parseSize(s.size_hint) || 0), 0);
  let html = "";
  if (!o.initiated && !o.portable) {
    html += `<div class="stock-banner">
      <div><h2>Vault not stocked</h2>
      <p>${enabled.length} sources are selected (about ${fmtBytes(total)}). Review them in the <a href="#" data-goto="archive">Archive</a> tab, then start the download. Small archives arrive first; updates then run on schedule.</p></div>
      <button class="btn" type="button" data-action="initiate">Initiate download</button></div>`;
  }
  for (const [cat, items] of groupByCategory(enabled)) {
    const online = items.filter((s) => s.status === "ready").length;
    html += `<section class="category"><div class="category-head"><h2>${esc(cat)}</h2><span>${online}/${items.length} online</span></div><div class="cards">`;
    for (const s of items) {
      const p = progressInfo(s, o.now);
      const openable = !!s.open_url;
      const tag = openable ? "button" : "div";
      const attrs = openable ? `type="button" data-open="${esc(s.open_url)}" data-title="${esc(s.name)}"` : "";
      html += `<${tag} class="card${openable ? "" : " offline"}" ${attrs}>
        <h3>${esc(s.name)}</h3>
        <p>${esc(s.description || "")}</p>
        ${p ? `<div class="bar" title="${p.pct.toFixed(1)}%"><i style="width:${p.pct}%"></i></div>` : ""}
        <div class="meta">${statusPill(s)}
          <span>${s.installed_bytes ? fmtBytes(s.installed_bytes) : esc(s.size_hint || "")}</span>
          ${s.articles ? `<span>${s.articles.toLocaleString()} articles</span>` : ""}
        </div></${tag}>`;
    }
    html += `</div></section>`;
  }
  if (!enabled.length) html += `<p class="empty">No sources enabled. Visit the Archive tab.</p>`;
  el.innerHTML = html;
}

function parseSize(text) {
  const m = /^\s*~?\s*([\d.]+)\s*([KMGT]?)/i.exec(text || "");
  if (!m) return 0;
  return parseFloat(m[1]) * 1024 ** " KMGT".indexOf((m[2] || " ").toUpperCase());
}

function renderArchive(o) {
  const list = $("#archive-list");
  if (list.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
  const filter = $("#archive-filter").value.trim().toLowerCase();
  const items = o.sources.filter((s) => !filter || `${s.name} ${s.category} ${s.book || ""} ${s.url || ""}`.toLowerCase().includes(filter));
  let html = "";
  for (const [cat, group] of groupByCategory(items)) {
    html += `<div class="category-head"><h2>${esc(cat)}</h2><span>${group.length} sources</span></div>`;
    for (const s of group) {
      const p = progressInfo(s, o.now);
      const origin = s.type === "kiwix" ? `kiwix: ${s.folder}/${s.book}` : s.url;
      html += `<div class="src-row${s.enabled ? "" : " disabled"}" data-id="${esc(s.id)}">
        <label class="switch" title="${s.enabled ? "Disable" : "Enable"}">
          <input type="checkbox" data-action="toggle" ${s.enabled ? "checked" : ""} aria-label="Enable ${esc(s.name)}"><span></span>
        </label>
        <div class="src-name"><strong>${esc(s.name)}</strong>
          <small>${esc(origin)}${s.version ? ` · installed ${esc(s.version)}` : ""}${s.last_checked ? ` · checked ${timeAgo(s.last_checked)}` : ""}</small>
          ${s.error ? `<p class="error">${esc(s.error)}</p>` : ""}
          ${s.held && !BUSY.includes(s.status) ? `<small>Download cancelled. Scheduled updates skip this source; press Update to continue where it stopped.</small>` : ""}
        </div>
        <div class="src-size">${s.installed_bytes ? fmtBytes(s.installed_bytes) : "—"}<br>${esc(s.size_hint || "")}</div>
        <div class="src-status">${statusPill(s)}
          ${p ? `<div class="bar"><i style="width:${p.pct}%"></i></div><span>${p.pct.toFixed(1)}% · ${fmtBytes(p.speed)}/s · ${fmtEta(p.eta)}</span>` : ""}
          <select data-action="schedule" aria-label="Update schedule for ${esc(s.name)}">
            ${["default", "daily", "weekly", "monthly", "manual"].map((v) => `<option value="${v}" ${s.schedule === v ? "selected" : ""}>${v === "default" ? `default (${o.options.update_schedule})` : v}</option>`).join("")}
          </select>
        </div>
        <div class="src-actions">
          ${BUSY.includes(s.status)
            ? `<button class="btn small danger" data-action="cancel">Cancel</button>`
            : `<button class="btn small ghost" data-action="check" ${!s.enabled ? "disabled" : ""}>Update</button>`}
          <button class="btn small ghost" data-action="edit">Edit</button>
          ${s.installed_bytes ? `<button class="btn small danger" data-action="purge">Purge</button>` : ""}
          <button class="btn small danger" data-action="delete">Delete</button>
        </div></div>`;
    }
  }
  list.innerHTML = html || `<p class="empty">No sources match.</p>`;
}

function renderMap(o) {
  const el = $("#tab-map");
  const maps = o.sources.filter((s) => s.enabled && s.category === "Maps" && s.open_url);
  if (!maps.length) {
    el.innerHTML = `<p class="empty">No offline map stocked yet.${o.sources.some((s) => s.category === "Maps" && s.enabled) ? " It will appear here once downloaded." : " Enable a map in the Archive tab."}</p>`;
    return;
  }
  if (!maps.some((m) => m.id === ui.mapId)) ui.mapId = maps[0].id;
  const current = maps.find((m) => m.id === ui.mapId);
  const frame = el.querySelector("iframe");
  if (frame && frame.dataset.id === current.id) return; // don't reload a map in use
  el.innerHTML = `${maps.length > 1 ? `<div class="map-picker">${maps.map((m) => `<button class="btn small ${m.id === current.id ? "" : "ghost"}" data-map="${esc(m.id)}">${esc(m.name)}</button>`).join("")}</div>` : ""}
    <iframe data-id="${esc(current.id)}" src="${esc(current.open_url)}" title="${esc(current.name)}"></iframe>`;
}

async function renderStatus(o) {
  const el = $("#tab-status");
  const d = o.disk;
  const other = Math.max(0, d.used - d.library);
  const pct = (n) => `${((n / d.total) * 100).toFixed(2)}%`;
  const cur = o.sources.find((s) => s.id === o.current);
  const p = cur && progressInfo(cur, o.now);
  const queue = o.pending.map((id) => o.sources.find((s) => s.id === id)?.name || id);
  let logLines = [];
  try { logLines = await api("api/log"); } catch (_) {}
  el.innerHTML = `<div class="status-grid">
    <div class="panel"><h2>Storage</h2>
      <div class="big-num">${fmtBytes(d.library)}</div><div>archived in ${esc(o.options.storage_path)}</div>
      <div class="disk-bar" role="img" aria-label="Disk usage"><i class="lib" style="width:${pct(d.library)}"></i><i class="other" style="width:${pct(other)}"></i><i class="reserve" style="width:${pct(Math.min(d.reserve, d.free))}"></i></div>
      <div class="legend"><span><b style="background:var(--accent)"></b>Library</span><span><b style="background:var(--dim)"></b>Other data</span><span><b style="background:var(--danger)"></b>Reserve ${fmtBytes(d.reserve)}</span><span>${fmtBytes(d.free)} free of ${fmtBytes(d.total)}</span></div>
    </div>
    <div class="panel"><h2>Transfer</h2>
      ${cur ? `<div data-id="${esc(cur.id)}"><strong>${esc(cur.name)}</strong> — ${esc(cur.status)}
          <button class="btn small danger" data-action="cancel" style="margin-left:8px">Cancel</button></div>
        ${p ? `<div class="bar" style="margin:8px 0"><i style="width:${p.pct}%"></i></div><div>${fmtBytes(cur.progress.done)} / ${fmtBytes(cur.progress.total)} · ${fmtBytes(p.speed)}/s · ${fmtEta(p.eta)} left</div>` : ""}`
        : `<div>Idle.</div>`}
      <h3 style="margin-top:14px;font-size:18px">Queue (${queue.length})</h3>
      <div style="font-size:13px">${queue.length ? queue.map(esc).join("<br>") : "Empty."}</div>
      ${cur || queue.length ? `<button class="btn small danger" data-action="cancel-all" style="margin-top:10px">Cancel all downloads</button>` : ""}
    </div>
    ${o.portable ? `<div class="panel"><h2>Offline copy</h2><dl class="kv">
      <dt>Exported</dt><dd>${o.export ? esc(o.export.exported_at.replace("T", " ")) : "unknown"}</dd>
      <dt>Location</dt><dd>${esc(o.options.storage_path)}</dd>
      <dt>Library server</dt><dd>${o.kiwix_running ? "running" : "stopped"}</dd>
      </dl><p style="font-size:12px;color:var(--dim)">Read-only copy. Add or update content in Home Assistant, then export to this drive again.</p></div>` : ""}
    ${!o.portable && o.export && o.export.state !== "idle" ? `<div class="panel"><h2>Export to drive</h2>
      <div><strong>${esc(o.export.drive_name || "")}</strong>: ${esc(o.export.state)}</div>
      ${EXPORT_BUSY.includes(o.export.state) && o.export.total ? `<div class="bar" style="margin:8px 0"><i style="width:${(o.export.done / o.export.total) * 100}%"></i></div>
        <div>${fmtBytes(o.export.done)} / ${fmtBytes(o.export.total)} · ${esc(o.export.current || "")}</div>` : ""}
      ${o.export.error ? `<p class="error">${esc(o.export.error)}</p>` : ""}</div>` : ""}
    <div class="panel portable-hide"><h2>Schedule</h2><dl class="kv">
      <dt>Updates</dt><dd>${esc(o.options.update_schedule)} at ${String(o.options.update_hour).padStart(2, "0")}:00</dd>
      <dt>Bandwidth</dt><dd>${o.options.max_download_mbit ? `${o.options.max_download_mbit} Mbit/s` : "unlimited"}</dd>
      <dt>Reserve</dt><dd>${o.options.min_free_gb} GB kept free</dd>
      <dt>Library server</dt><dd>${o.kiwix_running ? "running" : "stopped (no archives yet)"}</dd>
      <dt>Stocked</dt><dd>${o.initiated ? "yes" : "not yet"}</dd>
    </dl><p style="font-size:12px;color:var(--dim)">Change these in the app's Configuration tab in Home Assistant.</p></div>
  </div>
  <div class="panel" style="margin-top:14px"><h2>Log</h2><pre class="log">${logLines.map((l) => `<span class="${esc(l.level)}">${new Date(l.t * 1000).toLocaleTimeString()} ${esc(l.msg)}</span>`).join("\n")}</pre></div>`;
}

function render() {
  const o = ui.overview;
  if (!o) return;
  document.body.classList.toggle("portable", !!o.portable);
  renderHeader(o);
  if (exportDialog.open && !exportDialog.querySelector('[data-step="progress"]').hidden) renderExportProgress(o.export);
  if (ui.tab === "library") renderLibrary(o);
  if (ui.tab === "archive") renderArchive(o);
  if (ui.tab === "map") renderMap(o);
  if (ui.tab === "status") renderStatus(o);
  if (ui.tab === "search") fillScope(o);
}

// ---------- polling ----------
let pollTimer;
async function refresh() {
  clearTimeout(pollTimer);
  try {
    ui.overview = await api("api/overview");
    render();
  } catch (err) {
    $("#status-line").textContent = `Archive link fault: ${err.message}`;
  }
  if (ui.tab === "armory") await loadArmory();
  const busy = ui.overview && (ui.overview.current || ui.overview.pending.length
    || EXPORT_BUSY.includes(ui.overview.export?.state) || (ui.tab === "armory" && ui.armory?.sync.running));
  pollTimer = setTimeout(refresh, busy || ["archive", "status"].includes(ui.tab) ? 3000 : 20000);
  return ui.overview;
}

// ---------- search ----------
function fillScope(o) {
  const sel = $("#search-scope");
  const value = sel.value;
  const ready = o.sources.filter((s) => s.enabled && s.status === "ready");
  sel.innerHTML = `<option value="">All sources</option>` + ready.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  sel.value = ready.some((s) => s.id === value) ? value : "";
}

async function runSearch(q) {
  q = q.trim();
  const out = $("#search-results");
  if (q.length < 2) { out.innerHTML = `<p class="empty">Type at least two characters.</p>`; return; }
  ui.lastQuery = q;
  out.innerHTML = `<p class="empty">Scanning archive…</p>`;
  const scope = $("#search-scope").value;
  try {
    const data = await api(`api/search?q=${encodeURIComponent(q)}${scope ? `&source=${encodeURIComponent(scope)}` : ""}`);
    if (ui.lastQuery !== q) return;
    if (!data.groups.length) { out.innerHTML = `<p class="empty">No records found for “${esc(q)}”.</p>`; return; }
    out.innerHTML = data.groups.map((g) => `<section class="search-group">
      <h3>${esc(g.source)}<small>${esc(g.category)}</small></h3>
      ${g.results.map((r) => `<button class="hit" type="button" data-open="${esc(r.link)}" data-title="${esc(r.title)}" data-q="${esc(q)}"${r.page ? ` data-page="${esc(r.page)}"` : ""}>
        <strong>${esc(r.title)}</strong>${r.snippet ? `<span>${esc(r.snippet).replace(/«/g, "<mark>").replace(/»/g, "</mark>")}</span>` : ""}
      </button>`).join("")}</section>`).join("");
  } catch (err) {
    out.innerHTML = `<p class="error">Search failed: ${esc(err.message)}</p>`;
  }
}

$("#search-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch($("#search-q").value); });
$("#quick-search").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#quick-q").value;
  $("#search-q").value = q;
  showTab("search");
  runSearch(q);
});

// ---------- actions ----------
document.addEventListener("click", async (e) => {
  const open = e.target.closest("[data-open]");
  if (open) { openReader(open.dataset.open, open.dataset.title, { q: open.dataset.q, page: open.dataset.page }); return; }

  const goto = e.target.closest("[data-goto]");
  if (goto) { e.preventDefault(); showTab(goto.dataset.goto); return; }

  const mapBtn = e.target.closest("[data-map]");
  if (mapBtn) { ui.mapId = mapBtn.dataset.map; $("#tab-map").innerHTML = ""; render(); return; }

  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  const row = btn.closest("[data-id]");
  const src = row && ui.overview.sources.find((s) => s.id === row.dataset.id);
  try {
    if (action === "initiate") {
      const r = await api("api/initiate", { method: "POST" });
      toast(`Stocking order accepted: ${r.queued} sources queued`);
    } else if (action === "check") {
      await api(`api/sources/${encodeURIComponent(src.id)}/check`, { method: "POST" });
      toast(`${src.name}: update queued`);
    } else if (action === "cancel") {
      await api(`api/sources/${encodeURIComponent(src.id)}/cancel`, { method: "POST" });
      toast(`${src.name}: download cancelled`);
    } else if (action === "cancel-all") {
      if (!confirm("Cancel the running download and empty the queue? Partial downloads are kept and continue when you press Update.")) return;
      const r = await api("api/cancel-all", { method: "POST" });
      toast(`${r.cancelled} downloads cancelled`);
    } else if (action === "edit") {
      openEditor(src);
    } else if (action === "purge") {
      if (!confirm(`Delete the downloaded files of "${src.name}" (${fmtBytes(src.installed_bytes)})? The source stays in your list.`)) return;
      await api(`api/sources/${encodeURIComponent(src.id)}/purge`, { method: "POST" });
      toast(`${src.name}: files purged`);
    } else if (action === "delete") {
      if (!confirm(`Remove "${src.name}" from your sources${src.installed_bytes ? ` and delete its ${fmtBytes(src.installed_bytes)} of files` : ""}?`)) return;
      await api(`api/sources/${encodeURIComponent(src.id)}`, { method: "DELETE" });
      toast(`${src.name}: removed`);
    }
  } catch (err) {
    toast(err.message);
  }
  refresh();
});

$("#archive-list").addEventListener("change", async (e) => {
  const row = e.target.closest("[data-id]");
  const src = row && ui.overview.sources.find((s) => s.id === row.dataset.id);
  if (!src) return;
  const action = e.target.dataset.action;
  try {
    if (action === "toggle") {
      const enabled = e.target.checked;
      await saveSource({ ...src, enabled }, src.id);
      if (!enabled && src.installed_bytes &&
          confirm(`"${src.name}" is now offline. Also delete its ${fmtBytes(src.installed_bytes)} of downloaded files?`)) {
        await api(`api/sources/${encodeURIComponent(src.id)}/purge`, { method: "POST" });
      }
    } else if (action === "schedule") {
      await saveSource({ ...src, schedule: e.target.value }, src.id);
      e.target.blur();
      toast(`${src.name}: schedule set to ${e.target.value}`);
    }
  } catch (err) {
    toast(err.message);
  }
  refresh();
});

$("#archive-filter").addEventListener("input", () => ui.overview && renderArchive(ui.overview));
$("#update-all").addEventListener("click", async () => {
  try {
    const r = await api("api/update-all", { method: "POST" });
    toast(`${r.queued} sources queued for update check`);
  } catch (err) { toast(err.message); }
  refresh();
});

function saveSource(src, id) {
  const fields = ["name", "category", "type", "folder", "book", "url", "enabled", "restyle", "schedule", "size_hint", "description"];
  const body = Object.fromEntries(fields.map((f) => [f, src[f]]));
  return id
    ? api(`api/sources/${encodeURIComponent(id)}`, { method: "PUT", body })
    : api("api/sources", { method: "POST", body });
}

// ---------- editor ----------
const editor = $("#editor");
const form = $("#editor-form");
let editingId = null;

function syncTypeFields() {
  const type = form.elements.type.value;
  form.querySelectorAll("[data-for]").forEach((el) => { el.hidden = el.dataset.for !== type; });
}
form.elements.type.addEventListener("change", syncTypeFields);

function openEditor(src) {
  editingId = src ? src.id : null;
  $("#editor-title").textContent = src ? "Edit source" : "Add source";
  $("#editor-error").textContent = "";
  const cats = [...new Set(ui.overview.sources.map((s) => s.category))];
  $("#category-list").innerHTML = cats.map((c) => `<option value="${esc(c)}">`).join("");
  const v = src || { type: "url", schedule: "default", enabled: true, category: "" };
  for (const name of ["name", "category", "type", "folder", "book", "url", "schedule", "size_hint", "description"]) {
    form.elements[name].value = v[name] ?? "";
  }
  form.elements.enabled.checked = v.enabled !== false;
  form.elements.restyle.checked = v.restyle !== false;
  syncTypeFields();
  editor.showModal();
}
$("#add-source").addEventListener("click", () => openEditor(null));

form.addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const data = Object.fromEntries(new FormData(form).entries());
  data.enabled = form.elements.enabled.checked;
  data.restyle = form.elements.restyle.checked;
  try {
    const saved = await saveSource(data, editingId);
    editor.close();
    toast(`${saved.name}: saved`);
    refresh();
  } catch (err) {
    $("#editor-error").textContent = err.message;
  }
});

// ---------- export to drive ----------
const EXPORT_BUSY = ["preparing", "copying"];
const DEFAULT_PRIORITY = { "Medical": "high", "Survival & Preparedness": "high", "Food & Farming": "high", "Repair & DIY": "high" };
const PRIORITY_RANK = { low: 0, normal: 1, high: 2 };
const exp = { plan: null, selected: new Set(), priority: {} };
try { exp.priority = JSON.parse(store("dv-export-priority") || "{}"); } catch (_) { exp.priority = {}; }

const exportDialog = $("#exporter");

function exportStep(step) {
  exportDialog.querySelectorAll("[data-step]").forEach((s) => { s.hidden = s.dataset.step !== step; });
}

function openExport() {
  const busy = ui.overview && EXPORT_BUSY.includes(ui.overview.export?.state);
  exportStep(busy ? "progress" : "drives");
  if (busy) renderExportProgress(ui.overview.export);
  else loadDrives();
  exportDialog.showModal();
}

async function loadDrives() {
  const list = $("#drive-list");
  list.innerHTML = `<p class="hint">Looking for drives…</p>`;
  let drives = [];
  try { drives = (await api("api/export/drives")).drives; } catch (err) { list.innerHTML = `<p class="error">${esc(err.message)}</p>`; return; }
  if (!drives.length) {
    list.innerHTML = `<div class="drive-empty">No drive found. To add one:
      <ol><li>Plug the drive into the Home Assistant server. Format it as <b>exFAT</b> or <b>NTFS</b> so Windows can read it.</li>
      <li>In Home Assistant open <b>Settings → System → Storage → Add storage → Local disk</b>.</li>
      <li>Choose usage <b>Media</b>, give it a name, and leave <b>read-only</b> off.</li>
      <li>Come back here and press <b>Refresh</b>.</li></ol></div>`;
    return;
  }
  list.innerHTML = drives.map((d) => {
    const warns = [];
    if (!d.writable) warns.push("Read-only: edit the storage in Home Assistant and turn off read-only.");
    if (d.fat32) warns.push("FAT32: archives over 4 GB (like Wikipedia) cannot be copied. exFAT or NTFS is better.");
    if (!d.windows_readable && !d.fat32) warns.push(`File system ${d.fstype}: Windows cannot read this drive. Use exFAT or NTFS.`);
    const prior = d.export ? `Contains a vault copy from ${esc(d.export.exported_at.replace("T", " "))} (${fmtBytes(d.export.bytes)}) - only changes will be copied.` : "No vault copy yet.";
    return `<button class="drive" type="button" data-drive="${esc(d.path)}" ${d.writable ? "" : "disabled"}>
      <strong>${esc(d.name)}</strong><span class="size">${fmtBytes(d.free)} free<br><small>of ${fmtBytes(d.total)}</small></span>
      <span class="meta">${esc(d.path)} · ${esc(d.fstype)}${d.network ? " · network share" : ""} · ${prior}</span>
      ${warns.map((w) => `<p class="warn">${esc(w)}</p>`).join("")}</button>`;
  }).join("");
}

async function choosePlan(path) {
  $("#export-error").textContent = "";
  try { exp.plan = await api(`api/export/plan?path=${encodeURIComponent(path)}`); } catch (err) { toast(err.message); return; }
  const p = exp.plan;
  // Default selection: what is enabled and fits the file system; everything already on the drive stays.
  exp.selected = new Set(p.items.filter((i) => !i.too_big_for_fat32 && (i.enabled || i.on_drive)).map((i) => i.id));
  for (const i of p.items) exp.priority[i.id] = exp.priority[i.id] || DEFAULT_PRIORITY[i.category] || "normal";
  if (!planNumbers().fits) fitToDrive();
  renderPlan();
  exportStep("content");
}

function planNumbers() {
  const p = exp.plan;
  const chosen = p.items.filter((i) => exp.selected.has(i.id));
  // Space we get back: free space, files from older exports, and anything deselected that is on the drive now.
  const reclaim = p.items.filter((i) => !exp.selected.has(i.id)).reduce((s, i) => s + i.on_drive, 0);
  const available = p.drive.free + p.obsolete + reclaim - p.reserve;
  const needed = chosen.reduce((s, i) => s + i.bytes - i.on_drive, 0) + p.runtime_new;
  return { needed, available, fits: needed <= available, total: chosen.reduce((s, i) => s + i.bytes, 0), count: chosen.length };
}

function fitToDrive() {
  const p = exp.plan;
  exp.selected = new Set(p.items.filter((i) => !i.too_big_for_fat32).map((i) => i.id));
  const candidates = p.items.filter((i) => exp.selected.has(i.id))
    .sort((a, b) => PRIORITY_RANK[exp.priority[a.id]] - PRIORITY_RANK[exp.priority[b.id]] || b.bytes - a.bytes);
  for (const item of candidates) {
    if (planNumbers().fits) break;
    exp.selected.delete(item.id);
  }
}

function renderPlan() {
  const p = exp.plan;
  const n = planNumbers();
  $("#plan-drive").innerHTML = `Drive <b>${esc(p.drive.name)}</b> · ${esc(p.drive.fstype)} · ${fmtBytes(p.drive.free)} free of ${fmtBytes(p.drive.total)}`;
  const bar = exportDialog.querySelector(".fit-bar");
  bar.classList.toggle("over", !n.fits);
  bar.querySelector("i").style.width = `${Math.min(100, (n.needed / Math.max(1, n.available)) * 100)}%`;
  $("#fit-text").innerHTML = `${n.count} sources selected (${fmtBytes(n.total)}). To copy: <b>${fmtBytes(n.needed)}</b> · space available: <b>${fmtBytes(Math.max(0, n.available))}</b> · `
    + (n.fits ? `<b>fits</b>, ${fmtBytes(n.available - n.needed)} to spare.` : `<b>does not fit</b>: deselect ${fmtBytes(n.needed - n.available)} more or press Fit to drive.`);
  const order = { high: 0, normal: 1, low: 2 };
  const rows = [...p.items].sort((a, b) => order[exp.priority[a.id]] - order[exp.priority[b.id]] || a.category.localeCompare(b.category) || a.name.localeCompare(b.name));
  $("#plan-list").innerHTML = rows.map((i) => {
    const on = exp.selected.has(i.id);
    const note = i.too_big_for_fat32 ? "too large for FAT32" : i.on_drive >= i.bytes ? "already on drive" : i.on_drive ? "update on drive" : "";
    return `<label class="plan-row${on ? "" : " off"}" data-id="${esc(i.id)}">
      <input type="checkbox" data-plan="toggle" ${on ? "checked" : ""} ${i.too_big_for_fat32 ? "disabled" : ""}>
      <span>${esc(i.name)}<small>${esc(i.category)}${note ? ` · ${note}` : ""}</small></span>
      <span class="num">${fmtBytes(i.bytes)}</span>
      <select data-plan="priority" aria-label="Priority of ${esc(i.name)}">
        ${["high", "normal", "low"].map((v) => `<option value="${v}" ${exp.priority[i.id] === v ? "selected" : ""}>${v[0].toUpperCase() + v.slice(1)}</option>`).join("")}
      </select></label>`;
  }).join("");
  exportDialog.querySelector('[data-export="start"]').disabled = !n.fits || !n.count;
}

function renderExportProgress(e) {
  const box = $("#export-progress");
  if (!e) return;
  const pct = e.total ? (e.done / e.total) * 100 : 0;
  const elapsed = Math.max(1, (ui.overview?.now || Date.now() / 1000) - e.started);
  const speed = e.done / elapsed;
  const busy = EXPORT_BUSY.includes(e.state);
  const titles = { preparing: "Preparing…", copying: "Copying to drive…", done: "Export complete", cancelled: "Export cancelled", error: "Export failed" };
  box.innerHTML = `<h3 style="font-size:22px;margin-bottom:8px">${titles[e.state] || esc(e.state)}</h3>
    <p class="hint">Drive: ${esc(e.drive_name || e.drive || "")}</p>
    ${busy ? `<div class="bar" style="margin:10px 0"><i style="width:${pct}%"></i></div>
      <p>${fmtBytes(e.done)} of ${fmtBytes(e.total)} · ${fmtBytes(speed)}/s${speed > 0 && e.total ? ` · ${fmtEta((e.total - e.done) / speed)} left` : ""}</p>
      <p class="hint">${esc(e.current || "")}</p>
      <p class="hint">You can close this window; copying continues in the background (see the Status tab).</p>` : ""}
    ${e.state === "done" ? `<p>The drive now holds a portable Doomsday Vault. On a Windows PC, open the <code>DoomsdayVault</code> folder on the drive and double-click <code>Start Doomsday Vault.bat</code>.</p>
      <p class="hint">Before unplugging, remove the drive in Home Assistant (Settings → System → Storage) so nothing is still being written.</p>` : ""}
    ${e.state === "error" ? `<p class="error">${esc(e.error)}</p>` : ""}`;
  exportDialog.querySelector('[data-export="cancel"]').hidden = !busy;
}

$("#export-open").addEventListener("click", openExport);
exportDialog.addEventListener("click", async (e) => {
  const drive = e.target.closest("[data-drive]");
  if (drive) { choosePlan(drive.dataset.drive); return; }
  const action = e.target.closest("[data-export]")?.dataset.export;
  if (action === "close") exportDialog.close();
  if (action === "refresh") loadDrives();
  if (action === "back") { exportStep("drives"); loadDrives(); }
  if (action === "fit") { fitToDrive(); renderPlan(); }
  if (action === "all") { exp.selected = new Set(exp.plan.items.filter((i) => !i.too_big_for_fat32).map((i) => i.id)); renderPlan(); }
  if (action === "cancel") { await api("api/export/cancel", { method: "POST" }).catch(() => {}); refresh(); }
  if (action === "start") {
    try {
      await api("api/export/start", { method: "POST", body: { path: exp.plan.drive.path, sources: [...exp.selected] } });
      store("dv-export-priority", JSON.stringify(exp.priority));
      exportStep("progress");
      renderExportProgress({ state: "preparing", drive_name: exp.plan.drive.name, done: 0, total: 0, started: Date.now() / 1000 });
      refresh();
    } catch (err) {
      $("#export-error").textContent = err.message;
    }
  }
});
exportDialog.addEventListener("change", (e) => {
  const row = e.target.closest("[data-id]");
  if (!row) return;
  if (e.target.dataset.plan === "toggle") {
    if (e.target.checked) exp.selected.add(row.dataset.id); else exp.selected.delete(row.dataset.id);
  } else if (e.target.dataset.plan === "priority") {
    exp.priority[row.dataset.id] = e.target.value;
  }
  renderPlan();
});

// ---------- start ----------
// After vault.js (inventory and armory) has loaded too.
document.addEventListener("DOMContentLoaded", () => {
  const initialTab = location.hash.slice(1);
  if (document.querySelector(`.tabs button[data-tab="${CSS.escape(initialTab)}"]`)) showTab(initialTab);
  runBoot(refresh());
});
