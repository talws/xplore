// app.js — Xplore's shell: views, theme, header stats, keyboard, URL state.
import { $, $$, api, num, compact } from "./util.js";
import * as library from "./library.js";
import * as recover from "./recover.js";
import { closeDetail, initDetail, isOpen as detailOpen } from "./detail.js";
import { openReader, closeReader, initReader } from "./reader.js";
import "./settings.js";   // wires the AI settings dialog on import

const VIEWS = ["library", "recover"];
let view = "library";

/* ── theme: system → light → dark ─────────────────────────────────────── */

const THEMES = ["system", "light", "dark"];
const THEME_LABEL = { system: "Theme: follows the system", light: "Theme: light", dark: "Theme: dark" };
const currentTheme = () => document.documentElement.dataset.theme || "system";

function setTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  try { t === "system" ? localStorage.removeItem("xp_theme") : localStorage.setItem("xp_theme", t); }
  catch (e) {}
  const b = $("#theme");
  b.dataset.mode = t;
  b.title = THEME_LABEL[t];
  b.setAttribute("aria-label", THEME_LABEL[t]);
}

/* ── views and the URL ────────────────────────────────────────────────── */

// State lives in the hash, so a reload or a copied address comes back to the same list.
function writeHash() {
  const qs = view === "library" ? library.paramsForHash() : recover.paramsForHash();
  history.replaceState(null, "", "#" + view + (qs ? "?" + qs : ""));
}

function readHash() {
  const [v, qs] = location.hash.slice(1).split("?");
  return { view: VIEWS.includes(v) ? v : "library", params: new URLSearchParams(qs || "") };
}

function show(v, opts) {
  view = v;
  $$("#views button[data-view]").forEach(b => {
    b.classList.toggle("on", b.dataset.view === v);
    b.setAttribute("aria-selected", String(b.dataset.view === v));
  });
  $("#v-library").hidden = v !== "library";
  $("#side-library").hidden = v !== "library";
  $("#v-recover").hidden = v !== "recover";
  $("#side-recover").hidden = v !== "recover";
  if (v !== "library") closeDetail();
  if (v === "recover") recover.enter(opts);
  writeHash();
}

const closeNav = () => document.body.classList.remove("nav-open");

/* ── header ───────────────────────────────────────────────────────────── */

async function stats() {
  try {
    const s = await api("/api/stats");
    $("#header-stats").innerHTML = `
      <span class="stat" title="Bookmarks held by Xplore"><b>${num(s.bookmarks)}</b> bookmarks</span>
      <span class="stat optional" title="${num(s.articleWords)} words of archived article text and video transcripts">
        <span class="dot" style="background:var(--ok)"></span><b>${num(s.articles)}</b> articles · ${compact(s.articleWords)} words</span>`;
  } catch (e) {
    $("#header-stats").innerHTML = `<span class="stat bad" title="Start it with start.bat">server unreachable</span>`;
  }
}

/* ── wiring ───────────────────────────────────────────────────────────── */

setTheme(currentTheme());
$("#theme").addEventListener("click", () => setTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]));
$$("#views button[data-view]").forEach(b => b.addEventListener("click", () => show(b.dataset.view)));
$("#logo").addEventListener("click", e => { e.preventDefault(); show("library"); });
$("#nav-toggle").addEventListener("click", () => document.body.classList.toggle("nav-open"));
$("#scrim").addEventListener("click", closeNav);

let timer;
const runSearch = () => {
  clearTimeout(timer);
  if (view !== "library") show("library");
  library.setQuery($("#q").value.trim());
};
$("#q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(runSearch, 250); });
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });

// Archived copies open in the reader; missing ones hand off to Recover.
document.addEventListener("click", e => {
  const a = e.target.closest("a[data-reader]");
  if (a && e.button === 0 && !(e.ctrlKey || e.metaKey || e.shiftKey || e.altKey)) {
    e.preventDefault();
    openReader(a.getAttribute("href"), a.dataset.title);
    return;
  }
  const r = e.target.closest("[data-recover]");
  if (r) {
    e.preventDefault();
    show("recover", { domain: r.dataset.domain, url: r.dataset.recover });
  }
});

document.addEventListener("keydown", e => {
  const typing = e.target.closest?.("input, textarea, select, [contenteditable]");
  if (e.key === "Escape") {
    if (closeReader()) return;
    if (document.body.classList.contains("nav-open")) { closeNav(); return; }
    if (detailOpen()) { closeDetail(); return; }
    if (typing) e.target.blur();
    return;
  }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "/") { e.preventDefault(); $("#q").focus(); $("#q").select(); }
  else if (view === "library" && $("#reader").hidden && (e.key === "j" || e.key === "k")) {
    library.move(e.key === "j" ? 1 : -1);
  }
});

document.addEventListener("xp:state", () => { closeNav(); writeHash(); });
document.addEventListener("xp:recover-state", writeHash);

initDetail();
initReader();
library.init();
recover.init();

const start = readHash();
if (start.view === "library") library.applyParams(start.params);
else recover.applyParams(start.params);
library.start();
show(start.view);
recover.refreshCount();
stats();
setInterval(stats, 5 * 60 * 1000);
