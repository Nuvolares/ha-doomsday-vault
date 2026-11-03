// In-app PDF viewer (PDF.js, bundled at image build time, works offline).
// URL parameters: file (same-origin path), page (open at), q (search terms to highlight).
import * as pdfjsLib from "./vendor/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL("./vendor/pdf.worker.min.mjs", import.meta.url).href;

const $ = (sel) => document.querySelector(sel);
const pagesEl = $("#pages");
const params = new URLSearchParams(location.search);

function pref(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (_) { /* storage unavailable */ }
  return null;
}

document.documentElement.dataset.theme = pref("dv-theme") === "amber" ? "amber" : "green";
if (window.self !== window.top) document.body.classList.add("embedded");

// Documents are always shown exactly as published; only the viewer around them uses the library design.
const terms = (params.get("q") || "").toLowerCase().match(/[\p{L}\p{N}]+/gu)?.filter(
  (w, i, all) => (w.length > 2 || /\d/.test(w)) && all.indexOf(w) === i) || [];

let pdf = null;
let baseSize = null;   // size of page 1 at scale 1
let scale = 1;
let fitWidth = true;
let generation = 0;    // bumps on every re-layout so stale renders are dropped
let observer = null;

function message(text) {
  pagesEl.innerHTML = "";
  const p = document.createElement("p");
  p.className = "pdf-message";
  p.textContent = text;
  pagesEl.append(p);
}

// Draw boxes over every occurrence of the search terms. Returns the top offset of the first one.
async function highlightTerms(el, page, viewport) {
  if (!terms.length) return null;
  const content = await page.getTextContent();
  let first = null;
  for (const item of content.items) {
    const text = (item.str || "").toLowerCase();
    if (!text) continue;
    const tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
    const height = Math.hypot(tx[2], tx[3]);
    const charWidth = (item.width * viewport.scale) / text.length;
    for (const term of terms) {
      for (let at = text.indexOf(term); at !== -1; at = text.indexOf(term, at + term.length)) {
        const box = document.createElement("div");
        box.className = "hitbox";
        box.style.left = `${tx[4] + at * charWidth}px`;
        box.style.top = `${tx[5] - height}px`;
        box.style.width = `${term.length * charWidth}px`;
        box.style.height = `${height * 1.2}px`;
        el.append(box);
        if (first === null || tx[5] - height < first) first = tx[5] - height;
      }
    }
  }
  return first;
}

async function renderPage(el, gen) {
  if (el.dataset.rendered) return null;
  el.dataset.rendered = "1";
  const page = await pdf.getPage(Number(el.dataset.n));
  if (gen !== generation) return null;
  const viewport = page.getViewport({ scale });
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(viewport.width * ratio);
  canvas.height = Math.floor(viewport.height * ratio);
  el.style.width = `${viewport.width}px`;
  el.style.height = `${viewport.height}px`;
  el.append(canvas);
  await page.render({
    canvasContext: canvas.getContext("2d"),
    viewport,
    transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null,
  }).promise;
  return highlightTerms(el, page, viewport);
}

function layout(keepPage) {
  const gen = ++generation;
  const available = Math.max(200, pagesEl.clientWidth - 26);
  if (fitWidth) scale = Math.min(4, available / baseSize.width);
  if (observer) observer.disconnect();
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) renderPage(entry.target, gen);
  }, { root: pagesEl, rootMargin: "1200px 0px" });

  pagesEl.innerHTML = "";
  for (let n = 1; n <= pdf.numPages; n++) {
    const el = document.createElement("div");
    el.className = "page";
    el.dataset.n = n;
    el.style.width = `${baseSize.width * scale}px`;
    el.style.height = `${baseSize.height * scale}px`;
    pagesEl.append(el);
    observer.observe(el);
  }
  $("#fit").disabled = fitWidth;
  if (keepPage) goTo(keepPage);
}

function currentPage() {
  const top = pagesEl.scrollTop + 40;
  let current = 1;
  for (const el of pagesEl.children) {
    if (el.offsetTop - pagesEl.offsetTop <= top) current = Number(el.dataset.n);
    else break;
  }
  return current;
}

function pageEl(n) {
  return pagesEl.querySelector(`.page[data-n="${n}"]`);
}

function goTo(n, offset = 0) {
  const el = pageEl(n);
  if (el) pagesEl.scrollTop = el.offsetTop - pagesEl.offsetTop - 6 + offset;
}

function zoom(factor) {
  const page = currentPage();
  fitWidth = false;
  scale = Math.min(5, Math.max(0.25, scale * factor));
  layout(page);
}

$("#zoom-in").addEventListener("click", () => zoom(1.25));
$("#zoom-out").addEventListener("click", () => zoom(0.8));
$("#fit").addEventListener("click", () => { fitWidth = true; layout(currentPage()); });
$("#page").addEventListener("change", (e) => {
  const n = Math.min(pdf ? pdf.numPages : 1, Math.max(1, Number(e.target.value) || 1));
  e.target.value = n;
  goTo(n);
});
pagesEl.addEventListener("scroll", () => {
  if (document.activeElement !== $("#page")) $("#page").value = currentPage();
}, { passive: true });

let resizeTimer;
window.addEventListener("resize", () => {
  if (!pdf || !fitWidth) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => layout(currentPage()), 200);
});

async function main() {
  const file = params.get("file") || "";
  // Only documents served by this app (same origin, absolute path) are opened.
  if (!file.startsWith("/") || file.startsWith("//")) {
    $("#title").textContent = "No document";
    message("No document was given.");
    return;
  }
  const name = decodeURIComponent(file.split("?")[0].split("/").pop() || "Document");
  $("#title").textContent = name;
  document.title = name;
  try {
    pdf = await pdfjsLib.getDocument({ url: file, isEvalSupported: false }).promise;
  } catch (err) {
    message(`Could not open this document: ${err.message}`);
    return;
  }
  const meta = await pdf.getMetadata().catch(() => null);
  const title = meta && meta.info && meta.info.Title;
  // Many PDFs carry junk titles like "Microsoft Word - draft.doc"; only use real ones.
  if (title && title.trim() && !/\.\w{2,4}$/.test(title.trim()) && !/^untitled$/i.test(title.trim())) {
    $("#title").textContent = title.trim();
    document.title = title.trim();
  }
  baseSize = (await pdf.getPage(1)).getViewport({ scale: 1 });
  $("#count").textContent = pdf.numPages;
  $("#page").max = pdf.numPages;
  layout();

  // Opened from a search result: render the target page first, then land on the first match.
  const target = Math.min(pdf.numPages, Math.max(1, Number(params.get("page")) || 1));
  if (target > 1 || terms.length) {
    const firstHit = await renderPage(pageEl(target), generation);
    goTo(target, firstHit !== null ? Math.max(0, firstHit - 80) : 0);
    $("#page").value = target;
  }
}

main();
