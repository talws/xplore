// reader.js — archived articles open over the app rather than in a browser tab,
// which matters inside the desktop window, where a new tab is the system browser.
import { $ } from "./util.js";

export function openReader(href, title) {
  $("#reader-title").textContent = title || "";
  $("#reader-open").href = href;
  $("#reader-frame").src = href;
  $("#reader").hidden = false;
  $("#reader-close").focus();
}

export function closeReader() {
  const r = $("#reader");
  if (r.hidden) return false;
  r.hidden = true;
  $("#reader-frame").src = "about:blank";
  return true;
}

export function initReader() {
  $("#reader-close").addEventListener("click", closeReader);
}
