// library.js — the bookmark index: time frame, facets, search and results.
import { $, $$, esc, api, num, relDate, fmtDateTime, highlight, terms, langName,
         TYPE_LABEL, STATUS_LABEL } from "./util.js";
import { linkRow, quoteBlock, WORKLIST } from "./render.js";
import { openDetail, setTaxonomy } from "./detail.js";
import { toggleDrawer } from "./related.js";

const DEFAULTS = {
  q: "", since: "", from: "", to: "", sort: "bookmarked", order: "desc",
  topic: "", tag: "", status: "", contentType: "", language: "", author: "", thread: "",
  scope: "bookmarks",
};
export const state = { ...DEFAULTS };

const RANGES = [["", "All time"], ["1", "Last 24 hours"], ["3", "Last 3 days"],
                ["7", "Last 7 days"], ["30", "Last month"], ["custom", "Custom range…"]];
const PAGE = 40;

let facets = null;
let offset = 0, total = 0, seq = 0, loading = false, exhausted = false, failed = false;
let selectedId = null;
let T = [];                     // folded query terms, for highlighting
const expanded = new Set();     // facet groups showing every item

/* ── state ────────────────────────────────────────────────────────────── */

export function paramsForHash() {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(state)) if (v !== DEFAULTS[k]) p.set(k, v);
  return p.toString();
}

export function applyParams(p) {
  for (const k of Object.keys(DEFAULTS)) state[k] = p.get(k) ?? DEFAULTS[k];
  $("#q").value = state.q;
}

function refresh() {
  document.dispatchEvent(new CustomEvent("xp:state"));
  renderSidebar();
  renderChips();
  load(true);
}

export function setQuery(q) {
  if (q === state.q) return;
  state.q = q;
  refresh();
}

/** Toggle a facet; `force` sets it even when it is already on (from the detail panel). */
export function setFilter(key, value, force = false) {
  if (!(key in DEFAULTS)) return;
  if (key === "scope") state.scope = state.scope === value && !force ? "bookmarks" : value;
  else state[key] = state[key] === value && !force ? "" : value;
  refresh();
}

/* ── sidebar ──────────────────────────────────────────────────────────── */

function item(key, value, label, count, title = "") {
  const on = state[key] === value;
  return `<button class="filter-item${on ? " active" : ""}" data-filter="${key}" data-value="${esc(value)}"
    aria-pressed="${on}"${title ? ` title="${esc(title)}"` : ""}>
    <span class="fi-name">${esc(label)}</span>${count != null ? `<span class="fi-count">${num(count)}</span>` : ""}</button>`;
}

function capped(id, items, n, render) {
  const all = expanded.has(id);
  let html = (all ? items : items.slice(0, n)).map(render).join("");
  if (items.length > n) {
    html += `<button class="more-btn" data-expand="${id}">${all ? "Show fewer" : `Show all ${items.length}`}</button>`;
  }
  return html;
}

function renderSidebar() {
  $("#basis").textContent = state.sort === "bookmarked" ? "by bookmark date" : "by post date";
  const custom = !!(state.from || state.to);
  $("#f-time").innerHTML = RANGES.map(([v, label]) => {
    const on = v === "custom" ? custom : !custom && state.since === v;
    return `<button class="filter-item${on ? " active" : ""}" data-range="${v}" aria-pressed="${on}">
      <span class="fi-name">${label}</span></button>`;
  }).join("");
  const box = $("#custom-range");
  box.hidden = !(custom || box.dataset.open === "1");
  $("#cf").value = state.from;
  $("#ct").value = state.to;

  if (!facets) return;
  $("#f-status").innerHTML = facets.status
    .map(s => item("status", s.value, STATUS_LABEL[s.value] || s.value, s.count)).join("");
  $("#f-type").innerHTML = facets.contentType
    .map(s => item("contentType", s.value, TYPE_LABEL[s.value] || s.value, s.count)).join("")
    + item("thread", "yes", "Threads", facets.threads, "Bookmarks with the author's own follow-up posts");
  $("#f-topic").innerHTML = capped("topic", facets.topics, 10, t => item("topic", t.value, t.value, t.count));
  $("#f-tags-wrap").hidden = !facets.tags.length;
  $("#f-tag").innerHTML = capped("tag", facets.tags, 10, t => item("tag", t.value, "#" + t.value, t.count));
  $("#f-lang").innerHTML = capped("lang", facets.language, 6,
    t => item("language", t.value, langName(t.value), t.count));
  $("#f-author").innerHTML = capped("author", facets.authors, 10,
    a => item("author", a.value, a.name || "@" + a.value, a.count, "@" + a.value));

  const note = $("#side-note");
  note.hidden = !facets.undated;
  if (facets.undated) {
    note.innerHTML = `<b>${num(facets.undated)}</b> stored tweets have no bookmark date. They were not seen
      on the last full walk of your bookmarks, so they were probably removed on X, and the library leaves
      them out. <button data-filter="scope" data-value="undated">${state.scope === "undated" ? "Back to the library" : "Show them"}</button>`;
  }
}

async function loadFacets() {
  try {
    facets = await api("/api/facets");
    setTaxonomy(facets.taxonomy);
    renderSidebar();
    renderNotice();
  } catch (e) {
    $("#f-topic").innerHTML = `<div class="side-err">Facets unavailable: ${esc(e.message)}</div>`;
  }
}

function sideClick(e) {
  const f = e.target.closest("[data-filter]");
  if (f) { setFilter(f.dataset.filter, f.dataset.value); return; }
  const r = e.target.closest("[data-range]");
  if (r) {
    const box = $("#custom-range");
    if (r.dataset.range === "custom") {
      box.dataset.open = "1";
      box.hidden = false;
      $("#cf").focus();
      return;
    }
    box.dataset.open = "";
    state.since = r.dataset.range;
    state.from = state.to = "";
    refresh();
    return;
  }
  const x = e.target.closest("[data-expand]");
  if (x) {
    expanded.has(x.dataset.expand) ? expanded.delete(x.dataset.expand) : expanded.add(x.dataset.expand);
    renderSidebar();
  }
}

/* ── filter bar ───────────────────────────────────────────────────────── */

function renderChips() {
  const chips = [];
  const add = (key, label) => chips.push(`<span class="chip">${esc(label)}<button data-clear="${key}"
    aria-label="Remove filter: ${esc(label)}" title="Remove">×</button></span>`);
  if (state.q) add("q", `“${state.q}”`);
  if (state.since) add("since", RANGES.find(r => r[0] === state.since)?.[1] || `Last ${state.since} days`);
  if (state.from || state.to) add("range", `${state.from || "…"} → ${state.to || "…"}`);
  if (state.scope !== "bookmarks") add("scope", state.scope === "undated" ? "Undated tweets" : "All stored tweets");
  if (state.status) add("status", STATUS_LABEL[state.status] || state.status);
  if (state.contentType) add("contentType", TYPE_LABEL[state.contentType] || state.contentType);
  if (state.thread) add("thread", "Threads");
  if (state.topic) add("topic", state.topic);
  if (state.tag) add("tag", "#" + state.tag);
  if (state.language) add("language", langName(state.language));
  if (state.author) add("author", "@" + state.author);
  if (chips.length > 1) chips.push(`<button class="chip-clear" data-clear="all">Clear all</button>`);
  $("#chips").innerHTML = chips.join("");
  $("#sort").value = `${state.sort}:${state.order}`;
}

function renderNotice() {
  const n = $("#notice");
  const timed = state.since || state.from || state.to;
  n.hidden = !(facets?.undated && timed && state.scope === "bookmarks");
  if (!n.hidden) {
    n.innerHTML = `Time frames go by bookmark date. ${num(facets.undated)} stored tweets have none and are not
      included. <button class="linkish" data-filter="scope" data-value="undated">Show them</button>`;
  }
}

function countText() {
  const noun = state.scope === "bookmarks" ? (total === 1 ? "bookmark" : "bookmarks") : (total === 1 ? "tweet" : "tweets");
  return `${num(total)} ${noun}${state.q ? " matching" : ""}`;
}

/* ── results ──────────────────────────────────────────────────────────── */

const WHERE = {
  tweet: "the post", quoted: "the quoted post", thread: "the thread",
  article: "the linked article", quotedArticle: "the quoted post's article",
};

function explain(t) {
  const m = t.matchedIn || [];
  if (!state.q || !m.length) return "";
  const only = !m.includes("tweet");
  return `<div class="why${only ? " only" : ""}">${only ? "Matched only in " : "Matched in "}${m.map(k => WHERE[k] || k).join(", ")}</div>`;
}

function card(t) {
  const bm = state.sort === "bookmarked";
  const when = bm ? t.added_at : t.created_at;
  const links = (t.links || []).filter(l => l.kind !== "tweet");
  const bar = links.some(l => l.archive_status === "ok") ? "ok"
            : links.some(l => WORKLIST.has(l.archive_status)) ? "warn" : "";
  const badges = [];
  if (t.thread_count) badges.push(`<span class="pill" title="The author's own follow-up posts are stored">thread · ${t.thread_count + 1}</span>`);
  else if (t.is_thread) badges.push(`<span class="pill">reply</span>`);
  if (t.truncated) badges.push(`<span class="pill warn" title="Only the first 280 characters were captured">truncated</span>`);
  if (!t.is_bookmark) badges.push(`<span class="pill warn" title="Not seen on the last full walk of your bookmarks">undated</span>`);

  const foot = [];
  if (t.topic) foot.push(`<button class="pill topic" data-filter="topic" data-value="${esc(t.topic)}" title="Filter by topic">${esc(t.topic)}</button>`);
  for (const g of t.tags || []) foot.push(`<button class="pill" data-filter="tag" data-value="${esc(g)}">#${esc(g)}</button>`);
  const m = t.media || {};
  if (m.photo) foot.push(`<span class="pill muted">${m.photo} photo${m.photo > 1 ? "s" : ""}</span>`);
  const vids = (m.video || 0) + (m.animated_gif || 0);
  if (vids) foot.push(`<span class="pill muted">${vids} video${vids > 1 ? "s" : ""}</span>`);
  if (t.is_bookmark) foot.push(`<button class="rel-btn" data-related aria-expanded="false" title="Links and bookmarks related to this one">Related</button>`);

  let quote = "";
  if (t.quoted) quote = quoteBlock(t.quoted, T);
  else if (t.quoted_text) quote = `<div class="quote"><div class="q-who">@${esc(t.quoted_handle || "?")}</div>
    <div class="text">${highlight(t.quoted_text, T)}</div></div>`;

  return `<article class="card${t.id === selectedId ? " selected" : ""}" data-id="${esc(t.id)}" tabindex="0">
    <div class="card-bar ${bar}"></div>
    <div class="card-body">
      <div class="row1">
        <button class="who" data-filter="author" data-value="${esc(t.author_handle || "")}" title="Show only @${esc(t.author_handle || "")}">${esc(t.author_name || "@" + (t.author_handle || "?"))}</button>
        ${t.author_name ? `<span class="handle">@${esc(t.author_handle || "")}</span>` : ""}
        <span class="when" title="Bookmarked ${esc(fmtDateTime(t.added_at))} · Posted ${esc(fmtDateTime(t.created_at))}">· ${bm ? "bookmarked" : "posted"} ${esc(relDate(when))}</span>
        ${badges.join("")}
      </div>
      <div class="text">${highlight(t.opener ?? t.text, T)}</div>
      ${quote}
      ${links.length ? `<div class="links">${links.map(l => linkRow(l, T)).join("")}</div>` : ""}
      ${explain(t)}
      <div class="foot">${foot.join("")}<a class="on-x" href="${esc(t.url)}" target="_blank" rel="noopener">Open on X ↗</a></div>
    </div>
  </article>`;
}

// A typed search can seed Related too: the drawer sits above the results.
function queryDrawer() {
  return `<div class="rel-q"><button class="rel-q-btn" data-related-q aria-expanded="false"
    title="Links and bookmarks related to this search, found by more than its words">Related to “${esc(state.q)}” ▸</button></div>`;
}

const nearEnd = () => { const l = $("#list"); return l.scrollHeight - l.scrollTop - l.clientHeight < 900; };

export async function load(reset) {
  const results = $("#results");
  if (reset) {
    offset = 0;
    exhausted = false;
    failed = false;
    results.innerHTML = `<div class="skeleton"></div>`.repeat(4);
    $("#more").hidden = true;
    $("#list").scrollTop = 0;
  } else if (loading || exhausted || failed) {
    return;
  }
  const my = ++seq;
  loading = true;
  const params = { limit: PAGE, offset };
  for (const k of Object.keys(DEFAULTS)) params[k] = state[k];
  try {
    const r = await api("/api/find", params);
    if (my !== seq) return;
    T = terms(r.query || state.q);
    total = r.total;
    if (reset) results.innerHTML = state.q && state.scope === "bookmarks" ? queryDrawer() : "";
    if (reset && !r.results.length) {
      const filtered = paramsForHash().replace(/(^|&)(sort|order)=[^&]*/g, "");
      results.insertAdjacentHTML("beforeend", `<div class="empty"><p>Nothing matches.</p>
        <small>${filtered ? "Try removing a filter or widening the time frame." : "No bookmarks yet: run a sync from x.com/i/bookmarks."}</small></div>`);
    }
    results.insertAdjacentHTML("beforeend", r.results.map(card).join(""));
    offset += r.results.length;
    exhausted = offset >= total || !r.results.length;
    $("#count").textContent = countText();
    $("#more").hidden = exhausted;
    renderNotice();
  } catch (e) {
    if (my !== seq) return;
    failed = true;
    if (reset) {
      results.innerHTML = `<div class="empty"><p>Could not load bookmarks.</p><small>${esc(e.message)}</small></div>`;
      $("#count").textContent = "—";
    } else {
      $("#more").hidden = false;
      $("#more").textContent = `Load more (last try failed: ${e.message})`;
    }
  } finally {
    if (my === seq) {
      loading = false;
      if (!exhausted && !failed && nearEnd()) setTimeout(() => load(false), 0);
    }
  }
}

/* ── selection ────────────────────────────────────────────────────────── */

function select(el, scroll = false) {
  $$("#results .card.selected").forEach(c => c.classList.remove("selected"));
  el.classList.add("selected");
  selectedId = el.dataset.id;
  if (scroll) {
    el.focus({ preventScroll: true });
    el.scrollIntoView({ block: "nearest" });
  }
  openDetail(selectedId, T);
}

export function move(delta) {
  const cards = $$("#results .card");
  if (!cards.length) return;
  let i = cards.findIndex(c => c.dataset.id === selectedId);
  i = i < 0 ? 0 : Math.max(0, Math.min(cards.length - 1, i + delta));
  select(cards[i], true);
  if (i >= cards.length - 3) load(false);
}

/* ── wiring ───────────────────────────────────────────────────────────── */

export function init() {
  $("#results").addEventListener("click", e => {
    // The drawer handles its own clicks; none of them select the card.
    if (e.target.closest(".rel")) return;
    const rb = e.target.closest("[data-related]");
    if (rb) {
      const open = toggleDrawer(rb.closest(".card-body"), { tweet: rb.closest(".card").dataset.id });
      rb.setAttribute("aria-expanded", String(open));
      return;
    }
    const rq = e.target.closest("[data-related-q]");
    if (rq) {
      const open = toggleDrawer(rq.parentElement, { q: state.q });
      rq.setAttribute("aria-expanded", String(open));
      rq.textContent = `Related to “${state.q}” ${open ? "▾" : "▸"}`;
      return;
    }
    if (e.target.closest("a, input, [data-recover]")) return;
    const f = e.target.closest("[data-filter]");
    if (f) { setFilter(f.dataset.filter, f.dataset.value); return; }
    if (e.target.closest("button")) return;
    const c = e.target.closest(".card[data-id]");
    // A drag to select text is not a click on the card.
    if (c && !String(window.getSelection())) select(c);
  });
  $("#results").addEventListener("keydown", e => {
    if (e.key === "Enter" && e.target.matches(".card")) select(e.target);
  });
  $("#side-library").addEventListener("click", sideClick);
  $("#notice").addEventListener("click", sideClick);

  $("#chips").addEventListener("click", e => {
    const b = e.target.closest("[data-clear]");
    if (!b) return;
    const k = b.dataset.clear;
    if (k === "all") {
      Object.assign(state, DEFAULTS, { sort: state.sort, order: state.order });
      $("#q").value = "";
      $("#custom-range").dataset.open = "";
    } else if (k === "range") {
      state.from = state.to = "";
      $("#custom-range").dataset.open = "";
    } else {
      state[k] = DEFAULTS[k];
      if (k === "q") $("#q").value = "";
    }
    refresh();
  });

  $("#sort").addEventListener("change", e => {
    [state.sort, state.order] = e.target.value.split(":");
    refresh();
  });
  $("#c-apply").addEventListener("click", () => {
    state.from = $("#cf").value;
    state.to = $("#ct").value;
    state.since = "";
    refresh();
  });

  $("#more").addEventListener("click", () => {
    failed = false;
    $("#more").textContent = "Load more";
    load(false);
  });
  let ticking = false;
  $("#list").addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; if (nearEnd()) load(false); });
  }, { passive: true });

  document.addEventListener("xp:filter", e => setFilter(e.detail.key, e.detail.value, true));
  document.addEventListener("xp:changed", loadFacets);
  document.addEventListener("xp:detail-closed", () => {
    selectedId = null;
    $$("#results .card.selected").forEach(c => c.classList.remove("selected"));
  });
  document.addEventListener("xp:removed", e => {
    const el = $$("#results .card").find(c => c.dataset.id === e.detail.id);
    if (el) el.remove();
    total = Math.max(0, total - 1);
    offset = Math.max(0, offset - 1);
    $("#count").textContent = countText();
    loadFacets();
  });
  document.addEventListener("xp:restored", () => { loadFacets(); load(true); });
}

export function start() {
  renderSidebar();
  renderChips();
  loadFacets();
  load(true);
}
