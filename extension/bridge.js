/**
 * bridge.js — isolated world. Relays captured payloads to localhost and drives
 * the scroll, with a small on-page panel so a long run is visible.
 *
 * Split from interceptor.js because the two need different privileges: the
 * interceptor must be in the page's world to see the page's XHR, while this
 * needs the extension's host permission to reach http://localhost:8502 from an
 * https://x.com page. They talk over window.postMessage, which crosses the
 * world boundary; the DOM is shared, everything else is not.
 */

(function () {
  "use strict";

  const PATHS = ["/i/bookmarks", "/i/history"];
  // 5s between scroll steps: enough for X to fetch and render a page without
  // producing a request rate worth noticing.
  const SCROLL_MS = 5000;
  // Exponential backoff on failure: 5s, 10s, 20s, 40s, 80s. Answering a rate
  // limit at the same cadence that provoked it is how an account gets locked.
  const BACKOFF_BASE_MS = 5000;
  const BACKOFF_MAX_MS = 5 * 60 * 1000;
  // Consecutive pages that add nothing new. The timeline is newest-first, so
  // three in a row means we have reached what a previous run already stored.
  const CAUGHT_UP_AFTER = 3;
  // A deep run ignores that heuristic. Three barren pages is only ~75 tweets
  // past the last new one, which is a guess about how far back a gap could
  // reach, not a proof that nothing lies below. A deep run walks until x.com
  // says the timeline has ended, or until it is unmistakably past anything new.
  const CAUGHT_UP_AFTER_DEEP = 40;
  // Consecutive server failures before giving up. Without this an unreachable
  // server means the stop signal never arrives and the page scrolls forever,
  // which is exactly the traffic pattern worth not producing.
  const MAX_FAILURES = 5;
  // Seconds between conversation fetches during the thread pass. One tweet
  // every 10s is ~2,000 in five and a half hours, unattended, and is a request
  // rate x.com will not notice.
  const THREAD_GAP_MS = 10000;

  const onSyncPage = () => PATHS.some((p) => location.pathname.indexOf(p) !== -1);
  // A permalink: /<handle>/status/<id>. The thread pass walks these.
  const statusId = () => (location.pathname.match(/\/status\/(\d+)/) || [])[1];

  const S = {
    running: false, timer: null, auto: false, deep: false,
    pages: 0, added: 0, updated: 0, dated: 0, barren: 0, failures: 0,
  };

  // Scheduling lives in the service worker now (chrome.alarms). A setTimeout
  // here only survives while this exact tab is open and unfrozen, which is why
  // the 7th of September was silently skipped.

  // ── panel ──────────────────────────────────────────────────────────────────

  let panel, statusEl, btn;

  function buildPanel() {
    if (panel || !document.body) return;
    panel = document.createElement("div");
    panel.style.cssText =
      "position:fixed;bottom:16px;right:16px;z-index:2147483647;" +
      "font:12px/1.4 system-ui,sans-serif;background:#111;color:#eee;" +
      "border:1px solid #444;border-radius:10px;padding:10px 12px;" +
      "box-shadow:0 4px 18px #0007;min-width:190px";
    panel.innerHTML =
      '<div style="font-weight:600;margin-bottom:6px">X Bookmark Collector</div>' +
      '<div id="xbc-status" style="opacity:.8;margin-bottom:8px">idle</div>' +
      '<div style="display:flex;gap:6px">' +
      '<button id="xbc-btn" style="font:inherit;flex:1;padding:5px 8px;' +
      'border-radius:6px;border:1px solid #666;background:#222;color:#eee;' +
      'cursor:pointer">Start</button>' +
      '<button id="xbc-deep" title="Walk the whole timeline, ignoring the ' +
      'caught-up shortcut" style="font:inherit;padding:5px 10px;' +
      'border-radius:6px;border:1px solid #666;background:#1a1a1a;color:#bbb;' +
      'cursor:pointer">Deep</button>' +
      '<button id="xbc-threads" title="Fetch the full text of threaded ' +
      'bookmarks, one every 10s" style="font:inherit;padding:5px 10px;' +
      'border-radius:6px;border:1px solid #666;background:#1a1a1a;color:#bbb;' +
      'cursor:pointer">Threads</button></div>';
    document.body.appendChild(panel);
    statusEl = panel.querySelector("#xbc-status");
    btn = panel.querySelector("#xbc-btn");
    btn.addEventListener("click", () => (S.running ? stop("stopped") : start(false)));
    // Deep while an ordinary sync is running upgrades that run instead of
    // stopping it. The hourly auto-sync can be mid-run when the page opens, and
    // a Deep press that silently stopped it looked like Deep doing nothing.
    panel.querySelector("#xbc-deep").addEventListener("click", () => {
      if (S.running && !S.deep) {
        S.deep = true; S.barren = 0;
        return say("switched to deep scan — walking the whole timeline…");
      }
      return S.running ? stop("stopped") : start(true);
    });
    panel.querySelector("#xbc-threads").addEventListener("click", async () => {
      const st = await ask("threadRole");
      if (st.on) {
        await ask("threadStop");
        threadMine = false;
        return say("thread pass stopped");
      }
      say("starting thread pass…");
      // This tab becomes the run's tab: the worker navigates it from here.
      const r = await ask("threadStart");
      if (!r.ok) say(r.error || "could not start the thread pass");
      else if (r.complete) say("no threads queued");
    });
  }

  function say(msg) {
    if (statusEl) statusEl.textContent = msg;
    console.info("[xbc]", msg);
    // Mirrored to the server log, so a stalled run can be diagnosed without
    // someone having to read the panel.
    ask("scanlog", msg);
  }

  // ── scrolling ──────────────────────────────────────────────────────────────

  // Jump to the bottom rather than nudging by a viewport: X's timeline is
  // virtualised and only ~4-8 tweets exist in the DOM at once, so incremental
  // scrolling was measured not to drive it reliably.
  function nextDelay() {
    if (!S.failures) return SCROLL_MS;
    return Math.min(BACKOFF_BASE_MS * Math.pow(2, S.failures - 1), BACKOFF_MAX_MS);
  }

  function step() {
    if (!S.running) return;
    window.scrollTo(0, document.body.scrollHeight);
    // About once a minute: proof the loop is alive, and whether the page is
    // still growing. Scrolling with no new pages means X stopped loading.
    S.steps = (S.steps || 0) + 1;
    if (S.steps % 12 === 0)
      ask("scanlog", `scrolling · ${S.pages} pages · height ${document.body.scrollHeight}` +
                     ` · tab ${document.visibilityState}`);
    S.timer = setTimeout(step, nextDelay());
  }

  function start(deep) {
    if (S.running) return;
    if (!onSyncPage()) return say("open x.com/i/history first");
    S.running = true;
    S.deep = !!deep;
    S.pages = S.added = S.updated = S.dated = S.barren = S.failures = 0;
    if (btn) btn.textContent = "Stop";
    say(deep ? "deep scan — walking the whole timeline…" : "running…");
    step();
  }

  function stop(why) {
    S.running = false;
    clearTimeout(S.timer);
    if (btn) btn.textContent = "Start";
    say(`${why} — ${S.added} new, ${S.updated} seen, ${S.dated} dated, ${S.pages} pages`);
    if (S.auto) { S.auto = false; ask("syncDone", S.added); }
  }

  // ── relay ──────────────────────────────────────────────────────────────────

  /** Ask the service worker to talk to localhost.
   *
   * Not a direct fetch: a content script's fetch obeys the page's CORS rules,
   * so a POST from https://x.com to a loopback address is refused before it
   * leaves the browser. See background.js.
   */
  function ask(kind, body, extra) {  // also used for control messages, where body is a count
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ __xbc: true, kind, body, ...(extra || {}) }, (r) => {
          if (chrome.runtime.lastError)
            return resolve({ ok: false, status: 0,
                             error: chrome.runtime.lastError.message });
          resolve(r || { ok: false, status: 0, error: "no response" });
        });
      } catch (e) {
        // Thrown when the extension was reloaded and this content script is
        // now orphaned; the page needs a reload.
        resolve({ ok: false, status: 0, error: String(e) });
      }
    });
  }

  async function send(body, http) {
    if (http && http !== 200) ask("scanlog", `bookmarks request returned HTTP ${http}`);
    const res = await ask("ingest", body);
    const data = res.data || {};

    if (!res.ok) {
      const what = res.status ? `server error ${res.status}` : "server unreachable";
      if (++S.failures >= MAX_FAILURES) return stop(what);
      return say(`${what} (${S.failures}/${MAX_FAILURES})`);
    }

    S.failures = 0;
    S.pages += 1;
    S.added += data.added || 0;
    S.updated += data.updated || 0;
    S.dated += data.dated || 0;

    if (data.end) {
      // x.com's own end-of-timeline marker. Distinct from an empty response,
      // which means rate limiting -- treating the two alike made a completed
      // run look like a throttled one.
      return stop("complete — end of bookmarks");
    }

    if (!data.parsed) {
      // An empty timeline is what x.com returns when it begins rate limiting.
      // Reading that as "nothing new, carry on" would answer a rate limit with
      // more requests, so it spends the failure budget instead.
      if (++S.failures >= MAX_FAILURES) return stop("no data — likely rate limited");
      return say(`empty response (${S.failures}/${MAX_FAILURES}) — backing off`);
    }

    // A deep run keeps walking while the server still holds undated bookmarks.
    // Re-walking the timeline to recover bookmark dates adds nothing, so
    // counting only new tweets stopped the first attempt after 37 pages.
    // Tweets removed from x.com never get a date, so this ends at x.com's own
    // end-of-timeline marker rather than at zero.
    const productive = data.added > 0 ||
      (S.deep && (data.dated > 0 || data.undated > 0));
    if (!productive) {
      if (S.auto && !S.running) {            // scheduled, page one held nothing new
        S.auto = false;
        ask("syncDone", 0);
        return say("auto-sync — nothing new");
      }
      const limit = S.deep ? CAUGHT_UP_AFTER_DEEP : CAUGHT_UP_AFTER;
      if (++S.barren >= limit) return stop(S.deep ? "deep scan complete" : "caught up");
    } else {
      S.barren = 0;
      // Woken by the schedule and page one held something new, so there may be
      // more below it: backfill down to what we already have, then stop. Only
      // in auto mode -- a passive capture during ordinary browsing must never
      // start scrolling the page by itself.
      if (!S.running && S.auto) start();
    }
    say(`${S.added} new · ${S.updated} seen · ${S.dated} dated · ${S.pages} pages`);
  }

  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;                 // ignore other frames
    const d = ev.data;
    if (!d || d.__xbc !== true) return;
    if (d.kind === "bookmarks" && d.body) send(d.body, d.status);
    else if (d.kind === "bookmarksEmpty") say(`X sent an empty bookmarks response (HTTP ${d.status})`);
    else if (d.kind === "threaddetail" && d.body) onThreadDetail(d.body);
    else if (d.kind === "ready") console.debug("[xbc] interceptor ready");
  });

  // ── thread pass ────────────────────────────────────────────────────────────
  //
  // The service worker runs the pass: it decides which tab takes part, picks
  // the next tweet, navigates the tab, swaps in a fresh tab every 25 tweets and
  // reopens one that has crashed. This page only captures its own conversation,
  // posts it, and reports when it is done. It used to navigate itself, which
  // kept every tweet in one renderer until Chrome killed it for memory.

  let threadMine = false;       // is this the run's tab?
  let threadRoleKnown = false;
  let threadPending = null;     // a TweetDetail that arrived before the role did
  let threadSent = false;       // one report per page load
  let threadTimeout = null;

  function threadReport(note) {
    if (threadSent) return;
    threadSent = true;
    clearTimeout(threadTimeout);
    if (note) say(note);
    // Wait before moving on: one tweet every ~10s is a rate x.com won't notice.
    setTimeout(() => ask("threadDone"), THREAD_GAP_MS);
  }

  async function postThread(body) {
    const root = statusId();
    if (!root) return threadReport("not a tweet page — moving on");
    // `root` travels with the payload; the server keys the thread on it.
    const r = await ask("ingestThread", body, { root });
    if (!r.ok) {
      return threadReport(r.status ? `server error ${r.status} — moving on`
                                   : "server unreachable — moving on");
    }
    const parts = (r.data && r.data.parts) || 0;
    const left = (r.data && r.data.remaining);
    threadReport(`thread: +${parts} parts${left != null ? ` · ${left} left` : ""}`);
  }

  function onThreadDetail(body) {
    if (threadSent) return;
    if (!threadRoleKnown) { threadPending = body; return; }  // role not back yet
    if (!threadMine) return;                                 // ordinary browsing
    postThread(body);
  }

  async function threadBoot() {
    if (!statusId()) { threadRoleKnown = true; return; }
    const role = await ask("threadRole");
    threadMine = !!(role && role.mine);
    threadRoleKnown = true;
    if (!threadMine) { threadPending = null; return; }

    if (panel) panel.style.display = "block";
    say(`thread pass — ${role.done || 0} done` +
        (role.remaining != null ? ` · ${role.remaining} left` : ""));
    ask("threadBeat");
    // A page that never delivers its conversation still has to hand over.
    threadTimeout = setTimeout(
      () => threadReport("no conversation returned — moving on"), 45000);
    if (threadPending) { const b = threadPending; threadPending = null; postThread(b); }
  }

  // ── boot ───────────────────────────────────────────────────────────────────

  function boot() {
    buildPanel();
    if (!panel) return;
    // Visible on the bookmarks page, and on the run's own tab during a thread
    // pass -- otherwise a run would be invisible for hours.
    panel.style.display = onSyncPage() ? "block" : "none";
    threadBoot();
    ask("isSyncTab").then((r) => {
      S.auto = !!(r && r.isSync);
      if (S.auto) say("auto-sync — checking for new bookmarks");
    });

    ask("ping").then(async (r) => {
      if (!r.ok) return say("server not running (port 8502)");
      // Don't clobber a thread-pass status line with "ready".
      if (threadMine) return;
      say(S.auto ? "auto-sync running" : "ready — server up");
    });
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", boot);
  else boot();

  // x.com is a single-page app: it navigates between /i/history and everything
  // else without a reload, so the panel has to follow route changes itself.
  let lastPath = location.pathname;
  setInterval(() => {
    if (location.pathname === lastPath) return;
    lastPath = location.pathname;
    if (panel) panel.style.display = (onSyncPage() || threadMine) ? "block" : "none";
    if (S.running && !onSyncPage()) stop("left the bookmarks page");
  }, 1000);
})();
