// related.js — C1: the Related drawer, on a result card or for a search.
import { esc, api, num, compact, relDate } from "./util.js";
import { openDetail } from "./detail.js";

const LABEL = {
  link: "same link", quote: "thread or quote", words: "shared words", author: "same author",
  tags: "hashtag or tag", topic: "same topic", time: "saved near in time",
};
const ORDER = ["link", "quote", "words", "author", "tags", "topic", "time"];
const STRONG = new Set(["link", "quote", "words"]);
const WINDOWS = [3, 7, 30, 90];
const COLLAPSED = 3;

function chips(r) {
  return (r.signals || []).slice(0, 3).map(s => {
    let label = LABEL[s];
    if (s === "words" && r.terms?.length) label = "shared words: " + r.terms.slice(0, 4).join(", ");
    if (s === "time" && r.days != null) {
      const d = Math.round(r.days);
      label = d < 1 ? "saved the same day" : `saved ${d} day${d === 1 ? "" : "s"} apart`;
    }
    return `<span class="rchip${STRONG.has(s) ? "" : " g"}">${esc(label)}</span>`;
  }).join("");
}

const bar = pct => `<div class="rbar" title="Relatedness ${pct} of 100"><i style="width:${pct}%"></i></div>`;

function linkItem(l) {
  const readable = l.archive_status === "ok" || l.archive_status === "thin";
  const href = readable ? `/article?url=${encodeURIComponent(l.url)}` : l.url;
  const bits = [`<span>${esc(l.host)}</span>`];
  if (l.archive_status === "ok") bits.push(`<span class="st ok">${l.kind === "video" ? "transcript" : "archived"} · ${compact(l.words)} words</span>`);
  else if (l.archive_status === "thin") bits.push(`<span class="st warn">teaser only</span>`);
  if (["failed", "thin", "botscreen"].includes(l.archive_status)) {
    bits.push(`<button class="st recover" data-recover="${esc(l.url)}" data-domain="${esc(l.host)}">recover →</button>`);
  }
  if (l.saved_by > 1) bits.push(`<span>in ${num(l.saved_by)} bookmarks</span>`);
  return `<div class="ritem">
    <a class="rtitle" href="${esc(href)}" target="_blank" rel="noopener"
       ${readable ? `data-reader data-title="${esc(l.title || l.url)}"` : ""}>${esc(l.title || l.url)}</a>
    <div class="link-meta">${bits.join("")}</div>
    <div class="rchips">${chips(l)}</div>${bar(l.score)}
  </div>`;
}

function tweetItem(t) {
  return `<div class="ritem rtweet" data-open="${esc(t.id)}" tabindex="0" role="button" title="Open this bookmark">
    <div class="row1"><span class="rwho">${esc(t.author_name || "@" + t.author_handle)}</span>
      <span class="handle">@${esc(t.author_handle || "")}</span>
      <span class="when">· bookmarked ${esc(relDate(t.added_at))}</span></div>
    <div class="rtext">${esc(t.opener ?? t.text)}</div>
    <div class="rchips">${chips(t.related)}</div>${bar(t.related.score)}
  </div>`;
}

function render(d) {
  const s = d._rel;
  const r = s.data;
  const nL = s.expanded ? r.links.length : COLLAPSED;
  const nT = s.expanded ? r.tweets.length : COLLAPSED;
  const controls = s.expanded ? `<div class="rel-controls">
      ${ORDER.map(k => `<button class="rtoggle${s.off.has(k) ? "" : " on"}" data-sig="${k}" aria-pressed="${!s.off.has(k)}"
        title="${s.off.has(k) ? "Count" : "Ignore"} this signal">${LABEL[k]} <b>${num(r.signals[k] || 0)}</b></button>`).join("")}
      <label class="rwin">near in time
        <select data-window>${WINDOWS.map(x => `<option value="${x}"${x === s.window ? " selected" : ""}>within ${x} days</option>`).join("")}</select>
      </label></div>` : "";
  const hidden = Math.max(0, r.links.length - COLLAPSED) + Math.max(0, r.tweets.length - COLLAPSED);
  const empty = !r.links.length && !r.tweets.length;
  d.innerHTML = `
    <div class="rel-head"><span>Related</span>
      <span class="rel-meta">${num(r.total_tweets)} bookmarks · ${num(r.total_links)} links</span></div>
    ${controls}
    ${r.links.length ? `<div class="rel-sec">Links</div>${r.links.slice(0, nL).map(linkItem).join("")}` : ""}
    ${r.tweets.length ? `<div class="rel-sec">Bookmarks</div>${r.tweets.slice(0, nT).map(tweetItem).join("")}` : ""}
    ${empty ? `<div class="rel-empty">Nothing related found${s.off.size ? " with these signals" : ""}.</div>` : ""}
    ${s.expanded ? `<button class="rel-more" data-expand>Show fewer</button>`
      : hidden || empty ? `<button class="rel-more" data-expand>${hidden ? `Show all · ${r.links.length} links, ${r.tweets.length} bookmarks` : "Signals"}</button>` : ""}`;
  d.querySelector("[data-window]")?.addEventListener("change", e => {
    s.window = Number(e.target.value);
    load(d);
  });
}

async function load(d) {
  const s = d._rel;
  const my = ++s.seq;
  if (!s.data) d.innerHTML = `<div class="rel-head"><span>Related</span><span class="rel-meta">finding…</span></div>`;
  d.classList.add("busy");
  const params = { ...s.seed, window: s.window };
  if (s.off.size) params.signals = s.off.size === ORDER.length ? "none" : ORDER.filter(k => !s.off.has(k)).join(",");
  try {
    const r = await api("/api/related", params);
    if (my !== s.seq) return;
    s.data = r;
    render(d);
  } catch (e) {
    if (my === s.seq) d.innerHTML = `<div class="rel-head"><span>Related</span><span class="rel-meta">${esc(e.message)}</span></div>`;
  } finally {
    if (my === s.seq) d.classList.remove("busy");
  }
}

function onClick(e) {
  const d = e.currentTarget;
  const s = d._rel;
  const sig = e.target.closest("[data-sig]");
  if (sig) {
    s.off.has(sig.dataset.sig) ? s.off.delete(sig.dataset.sig) : s.off.add(sig.dataset.sig);
    load(d);
    return;
  }
  if (e.target.closest("[data-expand]")) {
    s.expanded = !s.expanded;
    if (s.data) render(d);
    return;
  }
  if (e.target.closest("a, button, select, label")) return;
  const o = e.target.closest("[data-open]");
  if (o) openDetail(o.dataset.open);
}

/** Open or close the drawer inside `host`. `seed` is {tweet: id} or {q: text}.
 *  Returns whether it is now open. */
export function toggleDrawer(host, seed) {
  const open = host.querySelector(":scope > .rel");
  if (open) { open.remove(); return false; }
  const d = document.createElement("div");
  d.className = "rel";
  d._rel = { seed, expanded: false, off: new Set(), window: 7, data: null, seq: 0 };
  d.addEventListener("click", onClick);
  d.addEventListener("keydown", e => {
    if (e.key === "Enter" && e.target.matches("[data-open]")) openDetail(e.target.dataset.open);
  });
  host.appendChild(d);
  load(d);
  return true;
}
