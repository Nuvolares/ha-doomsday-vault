"use strict";
// Inventory (supplies with expiration dates and photos) and Armory (synced with WeaponLog).
// Uses the helpers from app.js: $, esc, api, toast, ui, openReader, timeAgo.

const SOON_DAYS = 30;
const DAY = 86400000;

function daysUntil(iso, today) {
  return Math.round((Date.parse(iso) - Date.parse(today)) / DAY);
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function fmtQty(n) {
  if (n == null) return "";
  return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
}

// ======================= Inventory =======================

const inv = { data: null, view: "all", editing: null, pending: [] };

async function loadInventory() {
  try {
    inv.data = await api("api/inventory");
  } catch (err) {
    $("#inv-body").innerHTML = `<p class="error">${esc(err.message)}</p>`;
    return;
  }
  renderInventory();
}

function expiryInfo(item, today) {
  if (!item.expires) return null;
  const d = daysUntil(item.expires, today);
  if (d < 0) return { cls: "error", text: `expired ${-d === 1 ? "yesterday" : `${-d} days ago`}`, d };
  if (d === 0) return { cls: "error", text: "expires today", d };
  if (d <= SOON_DAYS) return { cls: "busy", text: `expires in ${d} day${d === 1 ? "" : "s"}`, d };
  return { cls: "idle", text: `expires ${item.expires}`, d };
}

function invCard(item, today) {
  const exp = expiryInfo(item, today);
  const photo = item.photos[0];
  const qty = item.quantity != null ? `${fmtQty(item.quantity)}${item.unit ? ` ${esc(item.unit)}` : ""}` : esc(item.unit || "");
  return `<button class="card item-card" type="button" data-inv="${esc(item.id)}">
    ${photo ? `<img class="thumb" src="inventory/photos/${encodeURIComponent(photo)}" alt="" loading="lazy">` : `<span class="thumb none" aria-hidden="true">${esc(item.category.slice(0, 1))}</span>`}
    <span class="item-main">
      <h3>${esc(item.name)}</h3>
      <span class="item-meta">${qty ? `<b>${qty}</b>` : ""}${item.location ? `<span>${esc(item.location)}</span>` : ""}</span>
      ${exp ? `<span class="pill ${exp.cls}">${esc(exp.text)}</span>` : ""}
    </span></button>`;
}

function renderInventory() {
  const body = $("#inv-body");
  if (!inv.data) return;
  const { items, today } = inv.data;
  const filter = $("#inv-filter").value.trim().toLowerCase();
  const expired = items.filter((i) => i.expires && daysUntil(i.expires, today) <= 0);
  const soon = items.filter((i) => i.expires && daysUntil(i.expires, today) > 0 && daysUntil(i.expires, today) <= SOON_DAYS);

  let list = items.filter((i) => !filter || `${i.name} ${i.category} ${i.location} ${i.notes}`.toLowerCase().includes(filter));
  if (inv.view === "soon") list = list.filter((i) => soon.includes(i));
  if (inv.view === "expired") list = list.filter((i) => expired.includes(i));

  let html = `<div class="summary">
    <div><span class="big-num">${items.length}</span><small>items</small></div>
    <div class="${expired.length ? "bad" : ""}"><span class="big-num">${expired.length}</span><small>expired</small></div>
    <div class="${soon.length ? "warn" : ""}"><span class="big-num">${soon.length}</span><small>expire within ${SOON_DAYS} days</small></div>
    <div><span class="big-num">${new Set(items.map((i) => i.category)).size}</span><small>categories</small></div>
  </div>`;

  if (!items.length) {
    html += `<p class="empty">Nothing stocked yet. Press <b>+ Add item</b> to record food, water, medical supplies and gear.</p>`;
  } else if (!list.length) {
    html += `<p class="empty">No items match.</p>`;
  } else if (inv.view !== "all") {
    // Expiry views: one list, soonest first.
    list.sort((a, b) => a.expires.localeCompare(b.expires));
    html += `<div class="cards">${list.map((i) => invCard(i, today)).join("")}</div>`;
  } else {
    if (expired.length + soon.length && !filter) {
      const due = [...expired, ...soon].sort((a, b) => a.expires.localeCompare(b.expires));
      html += `<section class="category attention"><div class="category-head"><h2>Use or replace soon</h2><span>${plural(due.length, "item")}</span></div>
        <div class="cards">${due.map((i) => invCard(i, today)).join("")}</div></section>`;
    }
    for (const [cat, group] of groupByCategory(list)) {
      html += `<section class="category"><div class="category-head"><h2>${esc(cat)}</h2><span>${plural(group.length, "item")}</span></div>
        <div class="cards">${group.map((i) => invCard(i, today)).join("")}</div></section>`;
    }
  }
  body.innerHTML = html;
}

$("#inv-filter").addEventListener("input", renderInventory);
document.querySelectorAll("[data-inv-view]").forEach((b) => b.addEventListener("click", () => {
  inv.view = b.dataset.invView;
  document.querySelectorAll("[data-inv-view]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  renderInventory();
}));

// --- editor ---
const invDialog = $("#inv-editor");
const invForm = $("#inv-form");

function renderInvPhotos() {
  const item = inv.editing;
  const saved = (item?.photos || []).map((name) => `<span class="thumb-wrap">
      <button type="button" class="thumb-btn" data-photo-open="${esc(name)}" aria-label="Open photo"><img src="inventory/photos/${encodeURIComponent(name)}" alt=""></button>
      <button type="button" class="thumb-x portable-hide" data-photo-remove="${esc(name)}" aria-label="Remove photo">&times;</button></span>`);
  const pending = inv.pending.map((p, idx) => `<span class="thumb-wrap pending">
      <img src="${p.url}" alt=""><button type="button" class="thumb-x" data-photo-drop="${idx}" aria-label="Remove photo">&times;</button></span>`);
  $("#inv-photos").innerHTML = saved.concat(pending).join("") || `<span class="hint">No photos.</span>`;
}

function openInvEditor(item) {
  inv.editing = item;
  inv.pending.forEach((p) => URL.revokeObjectURL(p.url));
  inv.pending = [];
  const portable = !!ui.overview?.portable;
  $("#inv-title").textContent = portable ? "Item" : item ? "Edit item" : "Add item";
  $("#inv-error").textContent = "";
  invForm.querySelector("fieldset").disabled = portable;
  const items = inv.data?.items || [];
  $("#inv-cats").innerHTML = (inv.data?.categories || []).map((c) => `<option value="${esc(c)}">`).join("");
  $("#inv-locations").innerHTML = [...new Set(items.map((i) => i.location).filter(Boolean))].map((l) => `<option value="${esc(l)}">`).join("");
  const v = item || {};
  for (const f of ["name", "category", "location", "unit", "purchased", "expires", "notes"]) invForm.elements[f].value = v[f] ?? "";
  invForm.elements.quantity.value = v.quantity ?? "";
  $("#inv-delete").hidden = !item;
  renderInvPhotos();
  invDialog.showModal();
}

$("#inv-add").addEventListener("click", () => openInvEditor(null));
$("#inv-body").addEventListener("click", (e) => {
  const card = e.target.closest("[data-inv]");
  if (card) openInvEditor(inv.data.items.find((i) => i.id === card.dataset.inv));
});

// Photos are scaled down in the browser (phone photos are often 5-10 MB) and sent as JPEG.
async function shrinkPhoto(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  return new Promise((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Could not read the photo"))), "image/jpeg", 0.85));
}

async function uploadPhoto(id, blob) {
  const resp = await fetch(`api/inventory/items/${encodeURIComponent(id)}/photos`, { method: "POST", body: blob, headers: { "Content-Type": "image/jpeg" } });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `${resp.status} ${resp.statusText}`);
  return data;
}

$("#inv-photo-input").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  for (const file of files) {
    try {
      const blob = await shrinkPhoto(file);
      if (inv.editing) {
        inv.editing = await uploadPhoto(inv.editing.id, blob);  // existing item: save right away
      } else {
        inv.pending.push({ blob, url: URL.createObjectURL(blob) });  // new item: uploaded after the first save
      }
    } catch (err) {
      $("#inv-error").textContent = err.message;
    }
  }
  renderInvPhotos();
  if (inv.editing) loadInventory();
});

$("#inv-photos").addEventListener("click", async (e) => {
  const open = e.target.closest("[data-photo-open]");
  if (open) { invDialog.close(); openReader(`inventory/photos/${encodeURIComponent(open.dataset.photoOpen)}`, inv.editing?.name); return; }
  const remove = e.target.closest("[data-photo-remove]");
  if (remove && confirm("Remove this photo?")) {
    try {
      inv.editing = await api(`api/inventory/items/${encodeURIComponent(inv.editing.id)}/photos/${encodeURIComponent(remove.dataset.photoRemove)}`, { method: "DELETE" });
      renderInvPhotos();
      loadInventory();
    } catch (err) { $("#inv-error").textContent = err.message; }
  }
  const drop = e.target.closest("[data-photo-drop]");
  if (drop) {
    const [p] = inv.pending.splice(Number(drop.dataset.photoDrop), 1);
    URL.revokeObjectURL(p.url);
    renderInvPhotos();
  }
});

invForm.addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const body = Object.fromEntries(new FormData(invForm).entries());
  try {
    let saved = inv.editing
      ? await api(`api/inventory/items/${encodeURIComponent(inv.editing.id)}`, { method: "PUT", body })
      : await api("api/inventory/items", { method: "POST", body });
    for (const p of inv.pending) saved = await uploadPhoto(saved.id, p.blob);
    inv.pending = [];
    invDialog.close();
    toast(`${saved.name}: saved`);
    loadInventory();
  } catch (err) {
    $("#inv-error").textContent = err.message;
  }
});

$("#inv-delete").addEventListener("click", async () => {
  const item = inv.editing;
  if (!item || !confirm(`Delete "${item.name}"${item.photos.length ? " and its photos" : ""}?`)) return;
  try {
    await api(`api/inventory/items/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    invDialog.close();
    toast(`${item.name}: deleted`);
    loadInventory();
  } catch (err) { $("#inv-error").textContent = err.message; }
});

// ======================= Armory =======================

const UNITS = {
  length: [["inches", "Inches"], ["cm", "cm"]],
  bullet: [["inches", "Inches"], ["mm", "mm"]],
  distance: [["yards", "Yards"], ["meters", "Meters"]],
};
// Rentals are never synced into the Armory; marking a weapon sold removes it after the next sync.
const OWNERSHIP = [["owned", "Owned"], ["sold", "Sold"]];
// Field lists mirror armory.py KINDS (the fields WeaponLog lets us edit).
const ARMORY = {
  weapons: {
    title: "Weapons", one: "weapon",
    fields: [
      { f: "name", label: "Name", req: true }, { f: "caliber", label: "Caliber", req: true },
      { f: "weapon_type", label: "Status", type: "select", options: OWNERSHIP },
      { f: "serial_number", label: "Serial number" },
      { f: "purchase_date", label: "Purchase date", type: "date" },
      { f: "purchase_price", label: "Purchase price", type: "number" },
      { f: "rounds_fired_base", label: "Rounds fired before logging", type: "number", int: true },
      { f: "barrel_length", label: "Barrel length", type: "number", group: "Barrel" },
      { f: "barrel_length_unit", label: "Unit", type: "select", options: UNITS.length, group: "Barrel" },
      { f: "twist_rate", label: "Twist rate (e.g. 1:8)", group: "Barrel" },
      { f: "twist_direction", label: "Twist direction", type: "select", options: [["R", "Right"], ["L", "Left"]], group: "Barrel" },
    ],
  },
  ammo: {
    title: "Ammunition", one: "ammunition",
    fields: [
      { f: "name", label: "Name", req: true }, { f: "caliber", label: "Caliber", req: true },
      { f: "grain", label: "Grain", type: "number", int: true, req: true }, { f: "style", label: "Style (FMJ, HP…)", req: true },
      { f: "quantity_on_hand", label: "Rounds on hand", type: "number", int: true },
      { f: "ballistic_coefficient", label: "Ballistic coefficient", type: "number", group: "Ballistics" },
      { f: "bc_standard", label: "BC model", type: "select", options: [["G1", "G1"], ["G7", "G7"]], group: "Ballistics" },
      { f: "muzzle_velocity", label: "Muzzle velocity (fps)", type: "number", int: true, group: "Ballistics" },
      { f: "bullet_length", label: "Bullet length", type: "number", group: "Ballistics" },
      { f: "bullet_length_unit", label: "Unit", type: "select", options: UNITS.bullet, group: "Ballistics" },
      { f: "test_barrel_length", label: "Test barrel length (in)", type: "number", group: "Ballistics" },
    ],
  },
  accessories: {
    title: "Accessories & equipment", one: "accessory",
    fields: [
      { f: "name", label: "Name", req: true }, { f: "category", label: "Category", req: true, list: true },
      { f: "type", label: "Type / model" },
      { f: "accessory_type", label: "Status", type: "select", options: OWNERSHIP },
      { f: "purchase_date", label: "Purchase date", type: "date" }, { f: "price", label: "Price", type: "number" },
      { f: "sight_height", label: "Sight height", type: "number", group: "Optic" },
      { f: "sight_height_unit", label: "Unit", type: "select", options: UNITS.length, group: "Optic" },
      { f: "adjustment_type", label: "Adjustment", type: "select", options: [["MOA", "MOA"], ["MILS", "MILS"]], group: "Optic" },
      { f: "adjustment_per_click", label: "Per click (e.g. 0.25)", type: "number", group: "Optic" },
      { f: "zero_distance", label: "Zero distance", type: "number", group: "Optic" },
      { f: "zero_distance_unit", label: "Unit", type: "select", options: UNITS.distance, group: "Optic" },
    ],
  },
};

let armoryLoading = null;
async function loadArmory() {
  if (armoryLoading) return armoryLoading;
  armoryLoading = (async () => {
    try {
      ui.armory = await api("api/armory");
      if (ui.tab === "armory") renderArmory();
    } catch (err) {
      $("#tab-armory").innerHTML = `<p class="error">${esc(err.message)}</p>`;
    } finally {
      armoryLoading = null;
    }
  })();
  return armoryLoading;
}

function maskSerial(s) {
  s = String(s || "");
  return s.length > 4 ? `•••${s.slice(-4)}` : s;
}

function armCard(kind, it) {
  let sub = "", big = "";
  if (kind === "weapons") {
    sub = [it.caliber, it.weapon_type && it.weapon_type !== "owned" ? it.weapon_type : "", it.serial_number ? `SN ${maskSerial(it.serial_number)}` : ""].filter(Boolean).map(esc).join(" · ");
    big = it.rounds_fired != null ? `<span class="item-meta"><b>${Number(it.rounds_fired).toLocaleString()}</b><span>rounds fired</span></span>` : "";
  } else if (kind === "ammo") {
    sub = [it.caliber, it.grain ? `${it.grain} gr` : "", it.style].filter(Boolean).map(esc).join(" · ");
    big = `<span class="item-meta"><b>${Number(it.quantity_on_hand || 0).toLocaleString()}</b><span>rounds on hand</span></span>`;
  } else {
    sub = [it.category, it.type].filter(Boolean).map(esc).join(" · ");
  }
  const status = it.push_error ? `<span class="pill error" title="${esc(it.push_error)}">not accepted</span>`
    : it.pending ? `<span class="pill busy">waiting to sync</span>` : "";
  const initial = esc((it.name || "?").slice(0, 1));
  return `<button class="card item-card" type="button" data-arm-kind="${kind}" data-arm-key="${esc(it.key)}">
    ${it.local_photo ? `<img class="thumb" src="armory/photos/${encodeURIComponent(it.local_photo)}" alt="" loading="lazy">` : `<span class="thumb none" aria-hidden="true">${initial}</span>`}
    <span class="item-main"><h3>${esc(it.name)}</h3><span class="item-sub">${sub}</span>${big}${status}</span></button>`;
}

function renderArmory() {
  const el = $("#tab-armory");
  const a = ui.armory;
  if (!a) { el.innerHTML = `<p class="empty">Loading…</p>`; return; }
  if (!a.configured) {
    el.innerHTML = `<div class="stock-banner"><div><h2>Connect WeaponLog</h2>
      <p>The Armory mirrors your weapons, ammunition and accessories from WeaponLog and keeps a copy here, so it stays available when WeaponLog or the internet is down. Changes you make here are sent to WeaponLog automatically.</p></div>
      <button class="btn" type="button" data-arm="connect">Connect</button></div>`;
    return;
  }
  const s = a.sync;
  let state;
  if (s.running) state = `<span class="pill busy">syncing…</span>`;
  else if (s.error) state = `<span class="pill error">sync failing</span>`;
  else if (s.last_success) state = `<span class="pill ready">in sync</span>`;
  else state = `<span class="pill idle">not synced yet</span>`;
  let html = `<div class="sync-bar panel">
    <div class="sync-state">${state}
      <span>WeaponLog <b>${esc(a.url.replace(/^https?:\/\//, ""))}</b> · last synced ${timeAgo(s.last_success)} · every ${a.interval} min</span>
      ${a.pending ? `<span class="warn-text">${a.pending} change${a.pending === 1 ? "" : "s"} waiting to be sent</span>` : ""}
    </div>
    <div class="sync-actions">
      <button class="btn small" type="button" data-arm="sync" ${s.running ? "disabled" : ""}>Sync now</button>
      <button class="btn small ghost" type="button" data-arm="connect">Connection</button>
    </div>
    ${s.error ? `<p class="error sync-error">${esc(s.error)}<br>Showing the copy from ${s.last_success ? new Date(s.last_success * 1000).toLocaleString() : "before the first sync"}. You can keep editing; changes are sent when WeaponLog is reachable again.</p>` : ""}
  </div>`;

  const ammo = a.items.ammo;
  if (ammo.length) {
    const byCal = new Map();
    for (const it of ammo) byCal.set(it.caliber, (byCal.get(it.caliber) || 0) + Number(it.quantity_on_hand || 0));
    html += `<div class="summary">${[...byCal].sort((x, y) => y[1] - x[1]).map(([cal, n]) =>
      `<div><span class="big-num">${n.toLocaleString()}</span><small>rounds ${esc(cal)}</small></div>`).join("")}</div>`;
  }

  for (const [kind, spec] of Object.entries(ARMORY)) {
    const list = [...a.items[kind]].sort((x, y) => String(x.name).localeCompare(String(y.name)));
    html += `<section class="category"><div class="category-head"><h2>${spec.title}</h2><span>${list.length}</span>
      <button class="btn small ghost head-btn" type="button" data-arm="add" data-kind="${kind}">+ Add</button></div>
      ${list.length ? `<div class="cards">${list.map((it) => armCard(kind, it)).join("")}</div>` : `<p class="hint">None yet.</p>`}</section>`;
  }
  if (s.log.length) {
    html += `<details class="panel"><summary>Sync log</summary><pre class="log">${[...s.log].reverse().map((l) =>
      `<span class="${esc(l.level)}">${new Date(l.t * 1000).toLocaleString()} ${esc(l.msg)}</span>`).join("\n")}</pre></details>`;
  }
  el.innerHTML = html;
}

// --- editor ---
const armDialog = $("#arm-editor");
const armForm = $("#arm-form");
let armEditing = null;  // { kind, item }

function fieldHtml(fd, value) {
  const v = value ?? "";
  const label = `${esc(fd.label)}${fd.req ? " *" : ""}`;
  if (fd.type === "select") {
    return `<label>${label}<select name="${fd.f}"><option value=""></option>${fd.options.map(([val, text]) =>
      `<option value="${esc(val)}" ${String(v) === val ? "selected" : ""}>${esc(text)}</option>`).join("")}</select></label>`;
  }
  const type = fd.type || "text";
  const extra = type === "number" ? `step="${fd.int ? 1 : "any"}" min="0" inputmode="${fd.int ? "numeric" : "decimal"}"` : "";
  return `<label>${label}<input name="${fd.f}" type="${type}" ${extra} ${fd.req ? "required" : ""} ${fd.list ? `list="arm-cat-list"` : ""} value="${esc(v)}"></label>`;
}

function openArmEditor(kind, item) {
  armEditing = { kind, item };
  const spec = ARMORY[kind];
  $("#arm-title").textContent = `${item ? "Edit" : "Add"} ${spec.one}`;
  $("#arm-error").textContent = "";
  $("#arm-delete").hidden = !item;
  const v = item || (kind === "ammo" ? { quantity_on_hand: 0 } : {});
  const main = spec.fields.filter((fd) => !fd.group);
  const groups = [...new Set(spec.fields.filter((fd) => fd.group).map((fd) => fd.group))];
  $("#arm-fields").innerHTML = `<div class="grid2">${main.map((fd) => fieldHtml(fd, v[fd.f])).join("")}</div>`
    + groups.map((g) => `<details class="field-group"><summary>${esc(g)}</summary><div class="grid2">${spec.fields.filter((fd) => fd.group === g).map((fd) => fieldHtml(fd, v[fd.f])).join("")}</div></details>`).join("")
    + `<datalist id="arm-cat-list">${(ui.armory?.categories || []).map((c) => `<option value="${esc(c)}">`).join("")}</datalist>`
    + (item?.push_error ? `<p class="error">WeaponLog did not accept the last change: ${esc(item.push_error)}</p>` : "");
  armDialog.showModal();
}

armForm.addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const { kind, item } = armEditing;
  const body = {};
  for (const fd of ARMORY[kind].fields) {
    let value = armForm.elements[fd.f].value.trim();
    if (fd.f === "quantity_on_hand" && value === "") value = "0";
    if (fd.type === "number" && value !== "" && fd.int) value = String(Math.round(Number(value)));
    // Only what changed is sent, so untouched fields keep following WeaponLog.
    if (item ? String(item[fd.f] ?? "") !== value : value !== "") body[fd.f] = fd.type === "number" && value !== "" ? Number(value) : value;
  }
  try {
    const path = item ? `api/armory/${kind}/${encodeURIComponent(item.key)}` : `api/armory/${kind}`;
    if (item && !Object.keys(body).length) { armDialog.close(); return; }
    const saved = await api(path, { method: item ? "PUT" : "POST", body });
    armDialog.close();
    toast(`${saved.name}: saved${ui.armory?.sync.error ? " (will sync when WeaponLog is reachable)" : ""}`);
    await loadArmory();
    setTimeout(loadArmory, 2500);  // pick up the result of the immediate sync
  } catch (err) {
    $("#arm-error").textContent = err.message;
  }
});

$("#arm-delete").addEventListener("click", async () => {
  const { kind, item } = armEditing;
  if (!confirm(`Delete "${item.name}" here and in WeaponLog?`)) return;
  try {
    await api(`api/armory/${kind}/${encodeURIComponent(item.key)}`, { method: "DELETE" });
    armDialog.close();
    toast(`${item.name}: deleted`);
    loadArmory();
  } catch (err) { $("#arm-error").textContent = err.message; }
});

// --- connection ---
const connDialog = $("#arm-connect");
const connForm = $("#arm-connect-form");

function openConnect() {
  const a = ui.armory || {};
  connForm.elements.url.value = a.url || "";
  connForm.elements.token.value = "";
  connForm.elements.token.required = !a.token_set;
  connForm.elements.interval.value = a.interval || 60;
  $("#arm-token-hint").textContent = a.token_set ? "A token is saved. Leave the field empty to keep it." : "";
  $("#arm-connect-error").textContent = "";
  connDialog.showModal();
}

connForm.addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const body = Object.fromEntries(new FormData(connForm).entries());
  if (ui.armory?.url && body.url.replace(/\/+$/, "") !== ui.armory.url
      && !confirm("Switching to another WeaponLog replaces the Armory copy with that server's data. Continue?")) return;
  try {
    ui.armory = await api("api/armory/config", { method: "PUT", body });
    connDialog.close();
    toast("WeaponLog connected, syncing…");
    renderArmory();
    setTimeout(loadArmory, 1500);
  } catch (err) {
    $("#arm-connect-error").textContent = err.message;
  }
});

$("#tab-armory").addEventListener("click", async (e) => {
  const card = e.target.closest("[data-arm-key]");
  if (card) {
    const kind = card.dataset.armKind;
    openArmEditor(kind, ui.armory.items[kind].find((i) => i.key === card.dataset.armKey));
    return;
  }
  const btn = e.target.closest("[data-arm]");
  if (!btn) return;
  if (btn.dataset.arm === "connect") openConnect();
  if (btn.dataset.arm === "add") openArmEditor(btn.dataset.kind, null);
  if (btn.dataset.arm === "sync") {
    try {
      await api("api/armory/sync", { method: "POST" });
      ui.armory.sync.running = true;
      renderArmory();
      refresh();
    } catch (err) { toast(err.message); }
  }
});
