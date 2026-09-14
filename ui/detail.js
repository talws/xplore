// detail.js — the right-hand panel: one bookmark in full.
import { $, esc, api, post, num, fmtDateTime, highlight, langName } from "./util.js";
import { linkRow, quoteBlock } from "./render.js";

let current = null;
let taxonomy = [];

export const setTaxonomy = t => { taxonomy = t || []; };
export const isOpen = () => $("#detail").classList.contains("open");

const emit = (name, detail) => document.dispatchEvent(new CustomEvent(name, { detail }));

export async function openDetail(id, T = []) {
  current = id;
  const panel = $("#detail");
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
  $("#detail-body").innerHTML = `<div class="loading-row">Loading…</div>`;
  try {
    const d = await api(`/api/tweets/${encodeURIComponent(id)}`);
    if (current !== id) return;
    $("#detail-body").innerHTML = render(d, T);
    panel.scrollTop = 0;
    wire(d);
  } catch (e) {
    if (current === id) {
      $("#detail-body").innerHTML = `<div class="empty"><p>Could not load this bookmark.</p><small>${esc(e.message)}</small></div>`;
    }
  }
}

export function closeDetail() {
  if (!isOpen()) return;
  current = null;
  const panel = $("#detail");
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
  emit("xp:detail-closed");
}

// pbs.twimg.com serves a small rendition on request; the full image is a click away.
const thumb = url => /pbs\.twimg\.com\/media\//.test(url) && !url.includes("?") ? url + "?name=small" : url;

function render(d, T) {
  const sec = (title, body) => `<section class="d-sec"><h4>${title}</h4>${body}</section>`;
  const links = (d.links || []).filter(l => l.kind !== "tweet");
  const photos = (d.mediaItems || []).filter(m => m.kind === "photo");
  const videos = (d.mediaItems || []).filter(m => m.kind !== "photo");
  const handle = esc(d.author_handle || "");

  let html = `
    <div class="d-who">
      <button class="who" data-xfilter="author" data-value="${handle}" title="Show only @${handle}">${esc(d.author_name || "@" + d.author_handle)}</button>
      <span class="handle">@${handle}</span>
    </div>
    <div class="d-dates">${d.is_bookmark
      ? `Bookmarked <b>${esc(fmtDateTime(d.bookmarked_at))}</b> · `
      : `<span class="pill warn">not a current bookmark</span> `}Posted ${esc(fmtDateTime(d.created_at))}</div>
    <div class="d-actions">
      <a class="btn primary" href="${esc(d.url)}" target="_blank" rel="noopener">Open on X ↗</a>
      <button class="btn" id="d-copy">Copy link</button>
      <button class="btn danger" id="d-remove">Remove…</button>
    </div>
    <div id="d-remove-box" class="confirm" hidden>
      Remove this bookmark from Xplore? It stays bookmarked on X, and Xplore will skip it from now on.
      Its stored text is cleared; undo brings the entry back and the next sync refills the text.
      <div class="row"><button class="btn danger" id="d-remove-yes">Remove</button><button class="btn" id="d-remove-no">Cancel</button></div>
    </div>
    <div class="text d-text">${highlight(d.opener ?? d.text, T)}</div>`;
  if (d.truncated) html += `<div class="hint">Only the first 280 characters were captured.</div>`;

  if (d.thread?.length) {
    html += sec(`Thread · ${d.thread.length} more post${d.thread.length > 1 ? "s" : ""} by @${handle}`,
      `<ol class="thread" start="2">${d.thread.map(p => `<li>${highlight(p.text, T)}</li>`).join("")}</ol>`);
  }
  if (d.quoted) {
    html += sec("Quoted post", quoteBlock(d.quoted, T, { excerpt: true }));
  } else if (d.quoted_text) {
    html += sec("Quoted post", `<div class="quote"><div class="q-who">@${esc(d.quoted_handle || "?")}</div>
      <div class="text">${highlight(d.quoted_text, T)}</div></div>`);
  }
  if (links.length) {
    html += sec(`Links · ${links.length}`, `<div class="links">${links.map(l => linkRow(l, T, { excerpt: true })).join("")}</div>`);
  }
  if (photos.length || videos.length) {
    html += sec("Media",
      (photos.length ? `<div class="media-grid">${photos.map(m => `<a href="${esc(m.url)}" target="_blank" rel="noopener">
        <img src="${esc(thumb(m.url))}" alt="${esc(m.alt || "Photo")}" loading="lazy" referrerpolicy="no-referrer"></a>`).join("")}</div>` : "")
      + (videos.length ? `<div class="hint">${videos.length} video${videos.length > 1 ? "s" : ""}: open on X to play.</div>` : ""));
  }

  const eng = [["replies", d.reply_count], ["reposts", d.retweet_count], ["likes", d.like_count]]
    .filter(([, n]) => n != null).map(([k, n]) => `${num(n)} ${k}`).join(" · ");
  html += sec("Details", `<dl class="meta-grid">
    <dt><label for="d-topic">Topic</label></dt>
    <dd><input id="d-topic" list="d-taxonomy" value="${esc(d.topic || "")}" placeholder="—"
      title="Your label overrides the model's${d.topic_model ? ` (model says: ${esc(d.topic_model)})` : ""}">
      <datalist id="d-taxonomy">${taxonomy.map(t => `<option value="${esc(t)}">`).join("")}</datalist></dd>
    <dt><label for="d-tags">Tags</label></dt>
    <dd><input id="d-tags" value="${esc((d.tags || []).join(", "))}" placeholder="comma separated"></dd>
    <dt>Language</dt>
    <dd>${d.lang ? `<button class="linkish" data-xfilter="language" data-value="${esc(d.lang)}">${esc(langName(d.lang))}</button>` : "—"}</dd>
    ${d.hashtags?.length ? `<dt>Hashtags</dt><dd>${d.hashtags.map(h => "#" + esc(h)).join(" ")}</dd>` : ""}
    ${eng ? `<dt>Engagement</dt><dd>${eng}</dd>` : ""}
    ${d.quotedBy?.length ? `<dt>Quoted by</dt><dd>${d.quotedBy.map(q => `<a href="${esc(q.url)}" target="_blank" rel="noopener">@${esc(q.author_handle)}</a>`).join(", ")}</dd>` : ""}
    <dt>Captured</dt><dd>${esc(fmtDateTime(d.first_seen))}</dd>
  </dl><div id="d-saved" class="saved" aria-live="polite"></div>`);
  return html;
}

function wire(d) {
  const saved = msg => {
    const s = $("#d-saved");
    s.textContent = msg;
    setTimeout(() => { if (s.textContent === msg) s.textContent = ""; }, 2500);
  };
  $("#d-topic").addEventListener("change", async e => {
    try {
      await post("/api/tweets/topic", { id: d.id, topic: e.target.value.trim() });
      saved("Topic saved");
      emit("xp:changed");
    } catch (err) { saved("Not saved: " + err.message); }
  });
  $("#d-tags").addEventListener("change", async e => {
    try {
      const r = await post("/api/tweets/tags", { id: d.id, tags: e.target.value });
      e.target.value = r.tags.join(", ");
      saved("Tags saved");
      emit("xp:changed");
    } catch (err) { saved("Not saved: " + err.message); }
  });
  $("#d-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(d.url); saved("Link copied"); }
    catch (e) { saved(d.url); }
  });
  const box = $("#d-remove-box");
  $("#d-remove").addEventListener("click", () => { box.hidden = false; });
  $("#d-remove-no").addEventListener("click", () => { box.hidden = true; });
  $("#d-remove-yes").addEventListener("click", async () => {
    try {
      await post("/api/tweets/remove", { ids: [d.id] });
    } catch (e) { saved("Not removed: " + e.message); return; }
    box.innerHTML = `Removed from Xplore. <button class="btn" id="d-undo">Undo</button>`;
    emit("xp:removed", { id: d.id });
    $("#d-undo").addEventListener("click", async () => {
      await post("/api/tweets/restore", { ids: [d.id] });
      box.hidden = true;
      emit("xp:restored", { id: d.id });
    });
  });
}

export function initDetail() {
  $("#detail-close").addEventListener("click", closeDetail);
  $("#detail").addEventListener("click", e => {
    const f = e.target.closest("[data-xfilter]");
    if (f) emit("xp:filter", { key: f.dataset.xfilter, value: f.dataset.value });
  });
}
