// recover.js — the worklist of articles the archiver could not reach.
// Opening one on archive.today and passing its check is enough: the extension's
// capture.js saves the text back to Xplore.
import { $, $$, esc, api, num } from "./util.js";

const REASON = { thin: "paywall teaser", botscreen: "bot wall" };
const META_REASON = {
  http401: "hard paywall", http402: "paywall", http403: "bot-blocked", http404: "gone",
  http410: "removed", urlerror: "host gone", timeouterror: "timed out", http429: "rate-limited",
  http503: "unavailable",
};
const reasonOf = it => REASON[it.status] || META_REASON[it.meta_status] || "not archived";

const rec = { domain: "", offset: 0, total: 0, allTotal: 0, busy: false, seq: 0, target: "" };

export function paramsForHash() {
  return rec.domain ? new URLSearchParams({ domain: rec.domain }).toString() : "";
}
export function applyParams(p) { rec.domain = p.get("domain") || ""; }

// Which archive links were opened this session, so a long list can be worked
// down without losing your place. Per-viewer convenience only.
function openedSet() {
  try { return new Set(JSON.parse(sessionStorage.getItem("xp_opened") || "[]")); }
  catch (e) { return new Set(); }
}
function markOpened(url) {
  try {
    const s = openedSet();
    s.add(url);
    sessionStorage.setItem("xp_opened", JSON.stringify([...s]));
  } catch (e) {}
}

function item(it, opened) {
  return `<article class="card rec${opened.has(it.url) ? " opened" : ""}" data-url="${esc(it.url)}">
    <div class="card-bar warn"></div>
    <div class="card-body">
      <div class="row1">
        <span class="who">${esc(it.domain)}</span>
        <span class="pill warn">${esc(reasonOf(it))}</span>
        <span class="when">${num(it.refs)} bookmark${it.refs === 1 ? "" : "s"}${it.article_date ? " · " + esc(it.article_date) : ""}</span>
      </div>
      <a class="rec-title" href="${esc(it.originalLink || it.url)}" target="_blank" rel="noopener">${esc(it.title || it.originalLink || it.url)}</a>
      <div class="link-meta">${it.tweet_url ? `bookmarked via <a href="${esc(it.tweet_url)}" target="_blank" rel="noopener">@${esc(it.author || "?")}</a>` : ""}
        ${it.variants > 1 ? ` · ${it.variants} address variants` : ""}</div>
      <a class="btn primary go" href="${esc(it.archiveLink)}" target="_blank" rel="noopener" data-url="${esc(it.url)}">Open on archive.today ↗</a>
    </div>
  </article>`;
}

function renderDomains(domains) {
  const row = (d, n, on) => `<button class="filter-item${on ? " active" : ""}" data-domain="${esc(d)}" aria-pressed="${on}">
    <span class="fi-name">${esc(d || "All sites")}</span><span class="fi-count">${num(n)}</span></button>`;
  $("#f-domain").innerHTML = row("", rec.allTotal, !rec.domain)
    + domains.map(([d, n]) => row(d, n, rec.domain === d)).join("");
}

function renderDone(r) {
  const box = $("#rec-done");
  if (!r.recoveredTotal) { box.hidden = true; return; }
  const last = r.recovered[0];
  box.hidden = false;
  box.innerHTML = `<b>${num(r.recoveredTotal)} recovered so far.</b>`
    + (last ? ` Latest: <a href="/article?url=${encodeURIComponent(last.url)}" data-reader data-title="${esc(last.title || last.url)}"
      target="_blank" rel="noopener">${esc(last.title || last.url)}</a> (${num(last.word_count)} words)` : "");
  // Anything saved since the list was drawn is dimmed in place, rather than
  // redrawing and losing the reader's position.
  const saved = new Set(r.recovered.map(x => x.url));
  $$("#rec-list .card").forEach(el => {
    if (saved.has(el.dataset.url) && !el.classList.contains("saved")) {
      el.classList.add("saved");
      const b = el.querySelector(".pill");
      b.textContent = "saved";
      b.className = "pill";
    }
  });
}

export async function refreshCount() {
  try {
    const r = await api("/api/capture/worklist", { limit: 1 });
    $("#rec-n").textContent = num(r.total);
    if (!rec.domain) rec.allTotal = r.total;
    renderDone(r);
  } catch (e) {
    $("#rec-n").textContent = "";
  }
}

export async function load(reset) {
  if (reset) {
    rec.offset = 0;
    $("#rec-list").innerHTML = `<div class="skeleton"></div>`.repeat(3);
    $("#rec-more").hidden = true;
  } else if (rec.busy || rec.offset >= rec.total) {
    return;
  }
  const my = ++rec.seq;
  rec.busy = true;
  try {
    const r = await api("/api/capture/worklist", { limit: 60, offset: rec.offset, domain: rec.domain });
    if (my !== rec.seq) return;
    if (reset) $("#rec-list").innerHTML = "";
    rec.total = r.total;
    if (!rec.domain) {
      rec.allTotal = r.total;
      $("#rec-n").textContent = num(r.total);
    }
    let domains = r.domains;
    if (rec.domain && !domains.some(([d]) => d === rec.domain)) domains = [[rec.domain, r.total], ...domains];
    renderDomains(domains);
    $("#rec-count").textContent = `${num(r.total)} article${r.total === 1 ? "" : "s"} to recover`
      + (rec.domain ? ` from ${rec.domain}` : "") + " · most-bookmarked first";
    const opened = openedSet();
    $("#rec-list").insertAdjacentHTML("beforeend", r.items.map(it => item(it, opened)).join("")
      || (rec.offset ? "" : `<div class="empty"><p>Nothing left to recover${rec.domain ? " from this site" : ""}.</p></div>`));
    rec.offset += r.items.length;
    $("#rec-more").hidden = rec.offset >= r.total;
    renderDone(r);
    if (rec.target) {
      const el = $$("#rec-list .card").find(c => c.dataset.url === rec.target);
      if (el) {
        el.scrollIntoView({ block: "center" });
        el.classList.add("flash");
      }
      rec.target = "";
    }
  } catch (e) {
    if (my === rec.seq && reset) {
      $("#rec-list").innerHTML = `<div class="empty"><p>Could not load the worklist.</p><small>${esc(e.message)}</small></div>`;
    }
  } finally {
    if (my === rec.seq) rec.busy = false;
  }
}

/** Show the view; `opts` = {domain, url} when handed an article from the library. */
export function enter(opts) {
  if (opts && "domain" in opts) {
    rec.domain = opts.domain || "";
    rec.target = opts.url || "";
    load(true);
  } else if (!$("#rec-list").children.length || $("#rec-list .skeleton")) {
    load(true);
  } else {
    refreshCount();
  }
}

export function init() {
  $("#f-domain").addEventListener("click", e => {
    const b = e.target.closest("[data-domain]");
    if (!b) return;
    rec.domain = rec.domain === b.dataset.domain ? "" : b.dataset.domain;
    document.dispatchEvent(new CustomEvent("xp:recover-state"));
    document.body.classList.remove("nav-open");
    load(true);
  });
  $("#rec-more").addEventListener("click", () => load(false));
  $("#rec-list").addEventListener("click", e => {
    const a = e.target.closest(".go");
    if (!a) return;
    markOpened(a.dataset.url);
    a.closest(".card").classList.add("opened");
  });
  // Back from an archive.today tab: pick up what was saved meanwhile.
  window.addEventListener("focus", () => { if (!$("#v-recover").hidden) refreshCount(); });
}
