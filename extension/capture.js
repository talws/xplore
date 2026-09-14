/**
 * capture.js — runs on archive.today pages (archive.ph and its mirrors).
 *
 * Capture-on-view. The archiver could not reach ~1,100 articles because
 * archive.today puts a CAPTCHA in front of every snapshot. A person passes it
 * in seconds; then this script sends the unlocked page to Xplore.
 *
 * It does nothing on the CAPTCHA page itself -- that step is the person's, and
 * when it succeeds the page reloads into the snapshot and this runs again. It
 * also does nothing for archive pages unrelated to the collection: it asks the
 * server first, and only a page the server recognises gets sent.
 *
 * Nothing is parsed here. The server extracts and classifies, with the same
 * code that judged the automatic archive run, so a teaser or a bot wall is
 * refused there rather than trusted here.
 */

(function () {
  "use strict";

  // Text of the challenge page as observed on 2026-09-13, plus the Cloudflare
  // variants archive.today has used before.
  const CHALLENGE = /one more step|complete the security check|just a moment|checking your browser|verify you are human/i;

  function isChallenge() {
    const head = document.title + " " + (document.body ? document.body.innerText.slice(0, 1500) : "");
    return CHALLENGE.test(head);
  }

  function ask(kind, body) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ __xbc: true, kind, body }, (r) => {
          if (chrome.runtime.lastError)
            return resolve({ ok: false, error: chrome.runtime.lastError.message });
          resolve(r || { ok: false, error: "no response" });
        });
      } catch (e) {
        resolve({ ok: false, error: String(e) });   // extension reloaded under us
      }
    });
  }

  function toast(msg, good) {
    const el = document.createElement("div");
    el.textContent = msg;
    el.style.cssText =
      "position:fixed;left:16px;bottom:16px;z-index:2147483647;" +
      "font:13px/1.4 system-ui,sans-serif;padding:9px 13px;border-radius:9px;" +
      "box-shadow:0 4px 18px #0006;color:#fff;max-width:420px;" +
      (good ? "background:#14532d;" : "background:#44403c;");
    document.documentElement.appendChild(el);
    setTimeout(() => el.remove(), 7000);
  }

  let done = false;

  async function run() {
    if (done || isChallenge()) return;
    const page = location.href;

    const st = await ask("captureStatus", { page });
    const s = (st && st.data) || {};
    if (!st.ok) return;                 // server down: stay silent on a page the owner is reading
    if (!s.known) return;               // not one of ours
    done = true;
    if (!s.needs) {
      toast(`Xplore: already archived (${s.words} words)`, true);
      return;
    }

    // The archived body lives in #CONTENT; sending only that keeps the message
    // small. The server strips archive.today's chrome either way.
    const content = document.querySelector("#CONTENT");
    const html = content ? content.outerHTML : document.documentElement.outerHTML;
    const r = await ask("capture", { page, html });
    const x = (r && r.data) || {};
    if (x.saved) toast(`Xplore: archived — ${x.words.toLocaleString()} words`, true);
    else toast(`Xplore: not saved — ${x.reason || x.status || r.error || "unknown"}`, false);
  }

  // Snapshots are static HTML, so the load event is enough; the short delay
  // lets archive.today finish inserting its header before the check reads the page.
  if (document.readyState === "complete") setTimeout(run, 800);
  else window.addEventListener("load", () => setTimeout(run, 800));
})();
