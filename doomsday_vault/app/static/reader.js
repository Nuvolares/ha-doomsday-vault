// Injected into every archived page.
// Keeps every navigation inside the Doomsday Vault panel:
//  - PDFs open in the built-in viewer instead of the browser's PDF handler
//  - links that would open a new window open in place
//  - links to the internet are blocked with a notice (they would leave the app)
// and, when the page was opened from a search result, highlights the search
// terms and scrolls to the passage that matches best.
(function () {
  "use strict";
  var base = document.currentScript.src.replace(/\/static\/reader\.js.*$/, "");

  try {
    if (localStorage.getItem("dv-theme") === "amber") {
      document.documentElement.setAttribute("data-dv-theme", "amber");
    }
  } catch (e) { /* storage blocked: stay green */ }

  // --- design safety net ----------------------------------------------------
  // reader.css restyles pages generically. Two kinds of pages need help afterwards:
  //  - fixed layouts (PDFs or scans converted to HTML, text placed at exact positions
  //    over page images): any other font makes text run into the pictures, so these
  //    keep their original design;
  //  - sites whose own rules still paint light boxes: light text on white is
  //    unreadable, so those backgrounds are cleared.
  var SKIP = /^(IMG|VIDEO|CANVAS|SVG|PICTURE|IFRAME|EMBED|OBJECT|MARK|INPUT|SELECT|TEXTAREA|BUTTON|MATH)$/;

  function skin() { return document.querySelector('link[href$="/static/reader.css"]'); }

  function isFixedLayout() {
    if (document.querySelector('meta[name="generator"][content*="pdf2htmlEX"], #page-container .pf, .pf > .pc')) return true;
    var all = document.body.getElementsByTagName("*"), positioned = 0;
    for (var i = 0; i < all.length && i < 5000; i++) {
      var el = all[i];
      if (el.childElementCount || !el.textContent.trim()) continue;
      var pos = getComputedStyle(el).position;
      if ((pos === "absolute" || pos === "fixed") && ++positioned > 40) return true;
    }
    return false;
  }

  function rgba(value) {
    var m = /rgba?\(([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)(?:[,/ ]+([\d.]+%?))?/.exec(value || "");
    if (!m) return null;
    var a = m[4] === undefined ? 1 : (m[4].slice(-1) === "%" ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
    return { r: +m[1], g: +m[2], b: +m[3], a: a };
  }

  function light(c) { return (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) / 255 > 0.45; }

  function clearLightBoxes() {
    var all = document.body.getElementsByTagName("*");
    for (var i = 0; i < all.length && i < 8000; i++) {
      var el = all[i];
      if (SKIP.test(el.nodeName.toUpperCase()) || el.closest("svg, #dv-hitbar")) continue;
      var cs = getComputedStyle(el), bg = rgba(cs.backgroundColor);
      if (bg && bg.a > 0.2 && light(bg)) el.style.setProperty("background-color", "transparent", "important");
      if (cs.backgroundImage !== "none") el.style.setProperty("background-image", "none", "important");
      // Dark text forced by the site would now sit on the dark background.
      var fg = rgba(cs.color);
      if (fg && !light(fg) && (fg.r + fg.g + fg.b) / 765 < 0.35) el.style.setProperty("color", "inherit", "important");
    }
  }

  function guardDesign() {
    var link = skin();
    if (!link || !document.body) return;
    try {
      if (isFixedLayout()) { link.remove(); return; }
      clearLightBoxes();
    } catch (e) { /* never break the page over cosmetics */ }
  }

  if (document.readyState === "complete") {
    guardDesign();
  } else {
    document.addEventListener("DOMContentLoaded", guardDesign);
    window.addEventListener("load", guardDesign);  // again once late styles and scripts have run
  }

  var noteTimer;
  function note(text) {
    var el = document.getElementById("dv-offline-note");
    if (!el) {
      el = document.createElement("div");
      el.id = "dv-offline-note";
      el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.hidden = false;
    clearTimeout(noteTimer);
    noteTimer = setTimeout(function () { el.hidden = true; }, 3500);
  }

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0) return;
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link) return;
    var url;
    try { url = new URL(link.getAttribute("href"), location.href); } catch (e) { return; }
    if (url.protocol !== "http:" && url.protocol !== "https:") return;

    if (url.origin !== location.origin) {
      event.preventDefault();
      note("External link: not stored in the library (" + url.hostname + ")");
      return;
    }
    if (/\.pdf$/i.test(url.pathname)) {
      event.preventDefault();
      location.href = base + "/static/pdf.html?file=" + encodeURIComponent(url.pathname + url.search);
      return;
    }
    if (link.target && link.target !== "_self") {
      event.preventDefault();
      location.href = url.href;
    }
  }, true);

  // --- search highlighting -------------------------------------------------

  var STYLE =
    "mark.dv-hit{background:rgba(255,214,92,.38)!important;color:inherit!important;border-radius:2px;padding:0 1px}" +
    "mark.dv-hit.dv-current{background:rgba(255,214,92,.85)!important;color:#000!important}" +
    "#dv-hitbar{position:fixed;right:12px;bottom:12px;z-index:2147483647;display:flex;gap:6px;align-items:center;" +
    "padding:6px 8px;background:#0c160d;border:1px solid #41ff74;color:#e1ffe8;font:14px ui-monospace,Consolas,monospace;border-radius:4px}" +
    "#dv-hitbar button{all:unset;cursor:pointer;padding:2px 8px;border:1px solid #41ff74;border-radius:3px;color:#41ff74}" +
    "#dv-hitbar button:focus-visible{outline:2px solid #41ff74;outline-offset:2px}";

  function searchTerms(query) {
    var words = String(query || "").toLowerCase().match(/[\p{L}\p{N}]+/gu) || [];
    return words.filter(function (w, i) { return (w.length > 2 || /\d/.test(w)) && words.indexOf(w) === i; });
  }

  function escapeRe(text) { return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  function blockOf(node) {
    return node.closest("p,li,td,th,dd,dt,blockquote,pre,h1,h2,h3,h4,h5,h6,figcaption,section,article,div") || document.body;
  }

  function highlight(query) {
    var terms = searchTerms(query);
    if (!terms.length || !document.body) return;
    var re = new RegExp(terms.map(escapeRe).join("|"), "giu");
    var skip = /^(SCRIPT|STYLE|NOSCRIPT|TEXTAREA|INPUT|SELECT|OPTION|MARK)$/;
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        return !n.parentNode || skip.test(n.parentNode.nodeName) || !n.nodeValue.trim()
          ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);

    var marks = [];
    for (var i = 0; i < nodes.length && marks.length < 800; i++) {
      var node = nodes[i], text = node.nodeValue;
      re.lastIndex = 0;
      if (!re.test(text)) continue;
      re.lastIndex = 0;
      var frag = document.createDocumentFragment(), last = 0, m;
      while ((m = re.exec(text))) {
        frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var mark = document.createElement("mark");
        mark.className = "dv-hit";
        mark.textContent = m[0];
        frag.appendChild(mark);
        marks.push(mark);
        last = m.index + m[0].length;
      }
      frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    }
    if (!marks.length) return;

    // The best passage is the block that contains the most different search terms.
    var best = 0, bestScore = -1, scored = new Map();
    marks.forEach(function (mark, idx) {
      var block = blockOf(mark);
      if (!scored.has(block)) {
        var content = block.textContent.toLowerCase();
        scored.set(block, terms.filter(function (t) { return content.indexOf(t) !== -1; }).length);
      }
      if (scored.get(block) > bestScore) { bestScore = scored.get(block); best = idx; }
    });

    var style = document.createElement("style");
    style.textContent = STYLE;
    document.head.appendChild(style);

    var current = best;
    var bar = document.createElement("div");
    bar.id = "dv-hitbar";
    bar.innerHTML = '<button type="button" aria-label="Previous match">&#9650;</button>' +
      '<span aria-live="polite"></span><button type="button" aria-label="Next match">&#9660;</button>' +
      '<button type="button" aria-label="Clear highlights">&#10005;</button>';
    document.body.appendChild(bar);
    var buttons = bar.querySelectorAll("button"), counter = bar.querySelector("span");

    function show(idx) {
      marks[current].classList.remove("dv-current");
      current = (idx + marks.length) % marks.length;
      marks[current].classList.add("dv-current");
      marks[current].scrollIntoView({ block: "center" });
      counter.textContent = (current + 1) + " / " + marks.length;
    }
    buttons[0].addEventListener("click", function () { show(current - 1); });
    buttons[1].addEventListener("click", function () { show(current + 1); });
    buttons[2].addEventListener("click", function () {
      marks.forEach(function (mark) { mark.replaceWith(document.createTextNode(mark.textContent)); });
      bar.remove();
    });
    show(best);
  }

  // The panel stores the query just before opening a search result.
  function pendingQuery() {
    try {
      var raw = sessionStorage.getItem("dv-highlight");
      if (!raw) return null;
      sessionStorage.removeItem("dv-highlight");
      var data = JSON.parse(raw);
      return Date.now() - data.t < 60000 ? data.q : null;
    } catch (e) { return null; }
  }

  var query = pendingQuery();
  if (query) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { highlight(query); });
    } else {
      highlight(query);
    }
  }
})();
