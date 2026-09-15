// util.js — small helpers shared by every Xplore view.

export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];

export function esc(t) {
  return String(t ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export async function api(path, params) {
  const qs = params
    ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null))
    : "";
  let r;
  try { r = await fetch(path + qs); }
  catch (e) { throw new Error("server unreachable"); }
  if (!r.ok) throw new Error(`server error ${r.status}`);
  return r.json();
}

export async function post(path, body) {
  let r;
  try {
    r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(body) });
  } catch (e) { throw new Error("server unreachable"); }
  if (!r.ok) throw new Error(`server error ${r.status}`);
  return r.json();
}

export const num = n => Number(n ?? 0).toLocaleString();

export function compact(n) {
  n = Number(n ?? 0);
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e4) return Math.round(n / 1e3) + "k";
  return n.toLocaleString();
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function fmtDate(iso, withYear = true) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(0, 10);
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${withYear ? " " + d.getFullYear() : ""}`;
}

// A calendar date with no time, such as an article's "2026-09-02". Read as a
// local date: new Date("2026-09-02") means UTC midnight, which shows the day
// before anywhere west of UTC.
export function fmtDay(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || "").trim());
  if (!m) return String(s || "");
  return fmtDate(`${m[1]}-${m[2]}-${m[3]}T00:00:00`);
}

export function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return `${fmtDate(iso)}, ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function relDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(0, 10);
  const s = (Date.now() - d) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)} d ago`;
  return fmtDate(iso, d.getFullYear() !== new Date().getFullYear());
}

// X's language codes, including its own q* pseudo-codes.
const LANGS = {
  en: "English", el: "Greek", es: "Spanish", fr: "French", de: "German", it: "Italian",
  pt: "Portuguese", nl: "Dutch", ar: "Arabic", fa: "Persian", he: "Hebrew", iw: "Hebrew",
  tr: "Turkish", ru: "Russian", uk: "Ukrainian", ro: "Romanian", pl: "Polish", ja: "Japanese",
  zh: "Chinese", ko: "Korean", hi: "Hindi", ur: "Urdu", in: "Indonesian", ca: "Catalan",
  zxx: "No text", qme: "Media only", qht: "Hashtags only", qam: "Mentions only",
  qct: "Cashtags only", und: "Undetermined", art: "Emoji only",
};
export const langName = c => LANGS[c] || (c ? c.toUpperCase() : "Unknown");

export const TYPE_LABEL = { article: "Article links", video: "Video", image: "Images", text: "Text only" };
export const STATUS_LABEL = {
  archived: "Archived text", unrecovered: "To recover", pending: "Not archived yet", nolinks: "No links",
};

/* Highlighting that agrees with the index. FTS5's unicode61 tokenizer ignores
   case and accents, so "τσιπρας" finds "Τσίπρας"; the highlight must match the
   same way or a correct hit shows nothing marked. */
function foldChar(c) {
  const f = c.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
  return f === "ς" ? "σ" : f;
}
const fold = s => [...s].map(foldChar).join("");

export function terms(q) {
  const words = (q || "").replace(/\b(AND|OR|NOT|NEAR)\b/g, " ").match(/[\p{L}\p{N}_]+/gu) || [];
  return [...new Set(words.filter(w => w.length > 1).map(fold))];
}

export function highlight(text, termList) {
  text = String(text ?? "");
  if (!termList?.length || !text) return esc(text);
  let folded = "";
  const map = [];
  let i = 0;
  for (const ch of text) {
    const f = foldChar(ch);
    for (let k = 0; k < f.length; k++) { folded += f[k]; map.push(i); }
    i += ch.length;
  }
  map.push(text.length);
  const ranges = [];
  for (const t of termList) {
    const re = new RegExp("(?<![\\p{L}\\p{N}_])" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gu");
    for (const m of folded.matchAll(re)) ranges.push([map[m.index], map[m.index + m[0].length]]);
  }
  if (!ranges.length) return esc(text);
  ranges.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const r of ranges) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([...r]);
  }
  let out = "", pos = 0;
  for (const [s, e] of merged) {
    out += esc(text.slice(pos, s)) + "<mark>" + esc(text.slice(s, e)) + "</mark>";
    pos = e;
  }
  return out + esc(text.slice(pos));
}

// The server marks article snippets with  … ; escape first, then mark.
export const snippetHtml = s => esc(s).replace(//g, "<mark>").replace(//g, "</mark>");
