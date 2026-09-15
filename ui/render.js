// render.js — markup shared by result cards and the detail panel.
import { esc, highlight, snippetHtml, compact, num, fmtDay } from "./util.js";

export const WORKLIST = new Set(["failed", "thin", "botscreen"]);

const MISSING = { failed: "not recovered", botscreen: "bot wall" };
const VIDEO_STATE = { nosubs: "no subtitles", private: "private video", unavailable: "unavailable",
                      downloaderror: "download failed", blocked: "blocked" };

/* One outbound link. Readable copies open in the in-app reader (a plain click)
   or a new tab (modifier click); missing ones hand off to Recover. */
export function linkRow(l, T, opts = {}) {
  const readable = l.archive_status === "ok" || l.archive_status === "thin";
  const href = readable ? `/article?url=${encodeURIComponent(l.url)}` : l.url;
  const bits = [`<span class="host">${esc(l.host || "")}</span>`];
  if (l.article_date) bits.push(`<span title="Publication date">published ${esc(fmtDay(l.article_date))}</span>`);

  if (l.archive_status === "ok") {
    bits.push(`<span class="st ok">${l.kind === "video" ? "transcript" : "archived"} · ${compact(l.words)} words</span>`);
  } else if (l.archive_status === "thin") {
    bits.push(`<span class="st warn" title="Only the paywall teaser was captured">teaser · ${num(l.words)} words</span>`);
  } else if (WORKLIST.has(l.archive_status)) {
    bits.push(`<span class="st warn">${MISSING[l.archive_status]}</span>`);
  } else if (l.kind === "video") {
    bits.push(`<span class="st muted">${esc(VIDEO_STATE[l.archive_status] || "video")}</span>`);
  } else if (!l.archive_status) {
    bits.push(`<span class="st muted">not archived yet</span>`);
  }
  if (WORKLIST.has(l.archive_status)) {
    bits.push(`<button class="st recover" data-recover="${esc(l.url)}" data-domain="${esc(l.host || "")}"
      title="Find this article in the Recover view">recover →</button>`);
  }
  if (readable) bits.push(`<a href="${esc(l.url)}" target="_blank" rel="noopener" title="The original page">original ↗</a>`);

  return `<div class="link ${esc(l.kind || "")}">
    <a class="link-title" href="${esc(href)}" target="_blank" rel="noopener"
       ${readable ? `data-reader data-title="${esc(l.title || l.url)}"` : ""}>${highlight(l.title || l.url, T)}</a>
    <div class="link-meta">${bits.join("")}</div>
    ${l.snippet ? `<div class="snippet">${snippetHtml(l.snippet)}</div>` : ""}
    ${opts.excerpt && l.excerpt ? `<div class="excerpt">${esc(l.excerpt)}</div>` : ""}
  </div>`;
}

export function quoteBlock(q, T, opts = {}) {
  const links = (q.links || []).filter(l => l.kind !== "tweet");
  return `<div class="quote">
    <div class="q-who">${esc(q.author_name || "")} <span class="handle">@${esc(q.author_handle || "?")}</span>
      ${opts.excerpt ? ` · <a href="${esc(q.url)}" target="_blank" rel="noopener">open ↗</a>` : ""}</div>
    <div class="text">${highlight(q.text, T)}</div>
    ${links.length ? `<div class="links">${links.map(l => linkRow(l, T, opts)).join("")}</div>` : ""}
  </div>`;
}
