// related.js — the Related drawer, on a result card or for a search.
// C1 answers first (signals, no key); with AI on, C2 then widens the search and
// judges each item, and the drawer re-orders with a reason for each.
// In the expanded view each item can be marked relevant or not: those marks are
// the test set eval_related.py uses to check whether AI actually helps.
import { esc, api, num, compact, fmtDay, fmtDateTime } from "./util.js";
import { openDetail } from "./detail.js";
import { aiReady } from "./settings.js";

const LABEL = {
  link: "same link", quote: "thread or quote", words: "shared words", author: "same author",
  tags: "hashtag or tag", topic: "same topic", time: "saved near in time",
};
const ORDER = ["link", "quote", "words", "author", "tags", "topic", "time"];
const STRONG = new Set(["link", "quote", "words"]);
const WINDOWS = [3, 7, 30, 90];
const COLLAPSED = 3;
const REL_LABEL = ["only shares words", "loosely related", "closely related", "same story"];

function aiPreference() {
  try { return localStorage.getItem("xp_ai"); } catch (e) { return null; }
}
function setAiPreference(on) {
  try { localStorage.setItem("xp_ai", on ? "1" : "0"); } catch (e) {}
}

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

function aiBits(item) {
  if (!item.ai) return { mark: "", why: "", cls: "" };
  const n = item.ai.relevance;
  const dots = [1, 2, 3].map(i => `<i class="${i <= n ? "on" : ""}"></i>`).join("");
  return {
    mark: `<span class="rrel" title="AI: ${REL_LABEL[n]}" aria-label="AI: ${REL_LABEL[n]}">${dots}</span>`,
    why: item.ai.why ? `<div class="rwhy">${esc(item.ai.why)}</div>` : "",
    cls: n === 0 ? " r0" : "",
  };
}

// Relevant / not buttons, shown in the expanded view only.
function judge(s, key, kind, value) {
  if (!s.expanded || !key) return "";
  const v = s.gold.relevant.has(key) ? "relevant" : s.gold.not.has(key) ? "not" : "";
  return `<span class="rjudge" data-key="${esc(key)}" data-kind="${kind}" data-value="${esc(value)}">
    <button class="rj${v === "relevant" ? " on" : ""}" data-verdict="relevant" aria-pressed="${v === "relevant"}" title="Relevant to this bookmark">✓</button>
    <button class="rj no${v === "not" ? " on" : ""}" data-verdict="not" aria-pressed="${v === "not"}" title="Not relevant">✗</button>
  </span>`;
}

function linkItem(s, l) {
  const readable = l.archive_status === "ok" || l.archive_status === "thin";
  const href = readable ? `/article?url=${encodeURIComponent(l.url)}` : l.url;
  const bits = [`<span>${esc(l.host)}</span>`];
  if (l.archive_status === "ok") bits.push(`<span class="st ok">${l.kind === "video" ? "transcript" : "archived"} · ${compact(l.words)} words</span>`);
  else if (l.archive_status === "thin") bits.push(`<span class="st warn">teaser only</span>`);
  if (["failed", "thin", "botscreen"].includes(l.archive_status)) {
    bits.push(`<button class="st recover" data-recover="${esc(l.url)}" data-domain="${esc(l.host)}">recover →</button>`);
  }
  if (l.saved_by > 1) bits.push(`<span>in ${num(l.saved_by)} bookmarks</span>`);
  // Publication date right after the site, where enrich_links.py found one
  // (a calendar date, no time; about 60% of linked pages have it).
  if (l.article_date) bits.splice(1, 0, `<span title="Publication date">published ${esc(fmtDay(l.article_date))}</span>`);
  const a = aiBits(l);
  return `<div class="ritem${a.cls}">
    ${judge(s, l.key, "url", l.url)}${a.mark}<a class="rtitle" href="${esc(href)}" target="_blank" rel="noopener"
       ${readable ? `data-reader data-title="${esc(l.title || l.url)}"` : ""}>${esc(l.title || l.url)}</a>
    <div class="link-meta">${bits.join("")}</div>
    ${a.why}
    <div class="rchips">${chips(l)}</div>${bar(l.score)}
  </div>`;
}

function tweetItem(s, t) {
  const a = aiBits(t);
  return `<div class="ritem rtweet${a.cls}" data-open="${esc(t.id)}" tabindex="0" role="button" title="Open this bookmark">
    ${judge(s, "t:" + t.id, "tweet", t.id)}
    <div class="row1">${a.mark}<span class="rwho">${esc(t.author_name || "@" + t.author_handle)}</span>
      <span class="handle">@${esc(t.author_handle || "")}</span></div>
    <div class="rdates">posted <b>${esc(fmtDateTime(t.created_at))}</b>${t.added_at
      ? ` · bookmarked <b>${esc(fmtDateTime(t.added_at))}</b>` : ""}</div>
    <div class="rtext">${esc(t.opener ?? t.text)}</div>
    ${a.why}
    <div class="rchips">${chips(t.related)}</div>${bar(t.related.score)}
  </div>`;
}

function aiNote(s) {
  if (!s.ai) return "";
  if (s.aiBusy) return `<div class="rai-note">AI is widening the search and judging each item…</div>`;
  const ai = s.data?.ai;
  if (!ai) return "";
  if (ai.error) {
    return `<div class="rai-note bad">AI unavailable: ${esc(ai.error)} <button data-settings>Settings</button></div>`;
  }
  const also = (ai.added_terms || []).slice(0, 8);
  const cost = ai.cost_usd > 0 ? `$${ai.cost_usd.toFixed(4)}` : "no cost (from cache)";
  return `<div class="rai-note">Ordered by AI (${esc(ai.model)}) · ${cost}${also.length ? ` · also searched: ${esc(also.join(", "))}` : ""}</div>`;
}

function render(d) {
  const s = d._rel;
  const r = s.data;
  const nL = s.expanded ? r.links.length : COLLAPSED;
  const nT = s.expanded ? r.tweets.length : COLLAPSED;
  const marked = s.gold.relevant.size + s.gold.not.size;
  const controls = s.expanded ? `<div class="rel-controls">
      ${ORDER.map(k => `<button class="rtoggle${s.off.has(k) ? "" : " on"}" data-sig="${k}" aria-pressed="${!s.off.has(k)}"
        title="${s.off.has(k) ? "Count" : "Ignore"} this signal">${LABEL[k]} <b>${num(r.signals[k] || 0)}</b></button>`).join("")}
      <label class="rwin">near in time
        <select data-window>${WINDOWS.map(x => `<option value="${x}"${x === s.window ? " selected" : ""}>within ${x} days</option>`).join("")}</select>
      </label></div>
      <div class="rgold-note">Mark items ✓ or ✗ to build your test set for AI Related${marked ? ` · ${marked} marked here` : ""}.</div>` : "";
  const hidden = Math.max(0, r.links.length - COLLAPSED) + Math.max(0, r.tweets.length - COLLAPSED);
  const empty = !r.links.length && !r.tweets.length;
  d.innerHTML = `
    <div class="rel-head"><span>Related
        <button class="rai" data-ai aria-pressed="${s.ai}" title="${s.ai ? "Turn off" : "Use"} AI to widen the search and judge each item">AI</button></span>
      <span class="rel-meta">${num(r.total_tweets)} bookmarks · ${num(r.total_links)} links</span></div>
    ${aiNote(s)}
    ${controls}
    ${r.links.length ? `<div class="rel-sec">Links</div>${r.links.slice(0, nL).map(l => linkItem(s, l)).join("")}` : ""}
    ${r.tweets.length ? `<div class="rel-sec">Bookmarks</div>${r.tweets.slice(0, nT).map(t => tweetItem(s, t)).join("")}` : ""}
    ${empty ? `<div class="rel-empty">Nothing related found${s.off.size ? " with these signals" : ""}.</div>` : ""}
    ${s.expanded ? `<button class="rel-more" data-expand>Show fewer</button>`
      // Always offered: with three results or fewer nothing is hidden, but the
      // signal switches and the ✓/✗ marks live in the expanded view too.
      : `<button class="rel-more" data-expand>${hidden ? `Show all · ${r.links.length} links, ${r.tweets.length} bookmarks` : "Signals and marking"}</button>`}`;
  d.querySelector("[data-window]")?.addEventListener("change", e => {
    s.window = Number(e.target.value);
    load(d);
  });
}

async function loadGold(d) {
  const s = d._rel;
  try {
    const g = await api("/api/gold", s.seed);
    s.gold = { relevant: new Set(g.relevant), not: new Set(g.not) };
    if (s.data) render(d);
  } catch (e) { /* marking just starts empty */ }
}

async function load(d) {
  const s = d._rel;
  const my = ++s.seq;
  if (!s.data) d.innerHTML = `<div class="rel-head"><span>Related</span><span class="rel-meta">finding…</span></div>`;
  d.classList.add("busy");
  const params = { ...s.seed, window: s.window };
  if (s.off.size) params.signals = s.off.size === ORDER.length ? "none" : ORDER.filter(k => !s.off.has(k)).join(",");
  try {
    // The fast answer first, so the drawer is never empty while the model thinks.
    const r = await api("/api/related", params);
    if (my !== s.seq) return;
    s.data = r;
    s.aiBusy = s.ai;
    render(d);
    d.classList.remove("busy");
    if (s.ai) {
      const r2 = await api("/api/related", { ...params, ai: 1 });
      if (my !== s.seq) return;
      s.data = r2;
      s.aiBusy = false;
      render(d);
    }
  } catch (e) {
    if (my !== s.seq) return;
    s.aiBusy = false;
    if (s.data) render(d);
    else d.innerHTML = `<div class="rel-head"><span>Related</span><span class="rel-meta">${esc(e.message)}</span></div>`;
  } finally {
    if (my === s.seq) d.classList.remove("busy");
  }
}

async function onJudge(d, btn) {
  const s = d._rel;
  const box = btn.closest(".rjudge");
  const key = box.dataset.key;
  const current = s.gold.relevant.has(key) ? "relevant" : s.gold.not.has(key) ? "not" : "";
  const verdict = current === btn.dataset.verdict ? "clear" : btn.dataset.verdict;
  try {
    const r = await fetch("/api/gold", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seed: s.seed, item: { [box.dataset.kind]: box.dataset.value }, verdict }),
    });
    const g = await r.json();
    if (!r.ok) throw new Error(g.error || `server error ${r.status}`);
    s.gold = { relevant: new Set(g.relevant), not: new Set(g.not) };
    render(d);
  } catch (e) {
    btn.title = "Not saved: " + e.message;
  }
}

function onClick(e) {
  const d = e.currentTarget;
  const s = d._rel;
  const verdict = e.target.closest("[data-verdict]");
  if (verdict) { e.stopPropagation(); onJudge(d, verdict); return; }
  if (e.target.closest("[data-settings]")) {
    document.dispatchEvent(new CustomEvent("xp:open-settings"));
    return;
  }
  if (e.target.closest("[data-ai]")) {
    s.ai = !s.ai;
    setAiPreference(s.ai);
    load(d);
    return;
  }
  const sig = e.target.closest("[data-sig]");
  if (sig) {
    s.off.has(sig.dataset.sig) ? s.off.delete(sig.dataset.sig) : s.off.add(sig.dataset.sig);
    load(d);
    return;
  }
  if (e.target.closest("[data-expand]")) {
    s.expanded = !s.expanded;
    if (s.expanded && !s.goldLoaded) { s.goldLoaded = true; loadGold(d); }
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
  d._rel = { seed, expanded: false, off: new Set(), window: 7, data: null, seq: 0, ai: false,
             aiBusy: false, gold: { relevant: new Set(), not: new Set() }, goldLoaded: false };
  d.addEventListener("click", onClick);
  d.addEventListener("keydown", e => {
    if (e.key === "Enter" && e.target.matches("[data-open]")) openDetail(e.target.dataset.open);
  });
  host.appendChild(d);
  // AI is on when the reader last left it on, or, the first time, when a key is set.
  const pref = aiPreference();
  (pref === null ? aiReady() : Promise.resolve(pref === "1")).then(on => {
    d._rel.ai = !!on;
    load(d);
  });
  return true;
}
