/**
 * background.js — does all the talking to localhost.
 *
 * bridge.js used to fetch http://localhost:8502 directly from the content
 * script. Its GET /api/ping worked, but every POST failed with "Failed to
 * fetch" and no OPTIONS request ever reached the server.
 *
 * The reason is that in MV3 a content script's fetch is subject to the *page's*
 * CORS rules -- host permissions do not exempt it. A simple GET slips through on
 * the Access-Control-Allow-Origin header alone, but a POST carrying
 * Content-Type: application/json needs a preflight, and a preflight from an
 * https://x.com page to a loopback address also trips Chrome's Private Network
 * Access rules. Chrome refuses it before the server ever sees it, which is
 * exactly the silence in the log.
 *
 * An extension service worker is not a page context: it makes requests under
 * the extension's own host permissions and neither restriction applies. So the
 * content script now asks this worker to do it and gets the answer back over
 * the message channel.
 */

const SERVER = "http://localhost:8502";

// MV3 service workers are terminated when idle, and a long capture run is a
// long sequence of short bursts. That is fine -- each message wakes it -- but
// it means nothing may be kept in a module-level variable between calls.

async function call(path, body, isJson) {
  const opts = body === undefined
    ? { method: "GET" }
    : {
        method: "POST",
        headers: { "Content-Type": isJson ? "application/json" : "text/plain" },
        body,
      };
  const res = await fetch(SERVER + path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) {}
  return { ok: res.ok, status: res.status, data };
}

// ── scheduled sync ───────────────────────────────────────────────────────────
//
// The first version scheduled this with a setTimeout inside the x.com page.
// That was wrong, and the symptom was worse than an outright failure: it ran on
// 5, 6 and 8 September and silently skipped the 7th. A page timer only fires
// while that exact tab is open, and Chrome freezes and discards background tabs
// at its own discretion, so a 24-hour timeout there is a coin toss.
//
// chrome.alarms belongs to the extension, not to a page. It wakes the service
// worker even after Chrome has terminated it, and it does not care whether any
// x.com tab exists -- if none does, this opens one in the background, lets it
// capture, and closes it again.

const SYNC_ALARM = "xbc-sync";
const CHECK_EVERY_MIN = 60;              // wake hourly, act only when due
const SYNC_DUE_MS = 20 * 60 * 60 * 1000; // ~daily, with slack so a late wake still counts
const SYNC_URL = "https://x.com/i/history";

chrome.runtime.onInstalled.addListener(() => ensureAlarm());
chrome.runtime.onStartup.addListener(() => ensureAlarm());

function ensureAlarm() {
  chrome.alarms.create(SYNC_ALARM, { periodInMinutes: CHECK_EVERY_MIN, delayInMinutes: 1 });
}
ensureAlarm();   // also on every worker wake, in case the alarm was lost

// A run that never reports back -- server down, a login wall, a tab that failed
// to load -- would otherwise leave lastSync unset forever: the alarm would
// retry every hour and each attempt would leak another background tab. Treat
// anything still in flight after this long as failed.
const SYNC_STALE_MS = 30 * 60 * 1000;

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== SYNC_ALARM) return;
  const { lastSync = 0, syncStartedAt = 0 } =
    await chrome.storage.local.get(["lastSync", "syncStartedAt"]);

  if (syncStartedAt) {
    if (Date.now() - syncStartedAt < SYNC_STALE_MS) return;   // still working
    console.warn("[xbc] sync did not report back; abandoning it");
    await syncFinished(0);                                    // closes any tab we opened
    // Distinct from the server being down: the server answered, but the page
    // never delivered. A login wall on x.com is the likeliest cause, and that
    // needs a human.
    await notifyOnce(
      "stalled",
      "Xplore — sync did not complete",
      "The server is running but x.com returned nothing. You may need to sign " +
      "in to x.com again.");
    return;                                                   // try again next hour
  }
  if (Date.now() - lastSync < SYNC_DUE_MS) return;
  runSync();
});

// A dead server and "no new bookmarks" look identical from the outside, and
// that has now cost two silent outages: once when the server did not start at
// all, once when it started and crashed on a Unicode error before binding the
// port. Both were only noticed by chance. So a scheduled sync that cannot
// reach the server says so out loud.
//
// Throttled, because the alarm wakes hourly and a server that is off will stay
// off for hours; one notification a day is a reminder, twelve is noise.
const NOTIFY_EVERY_MS = 12 * 60 * 60 * 1000;

async function notifyOnce(key, title, message) {
  const stamp = "notified_" + key;
  const got = await chrome.storage.local.get(stamp);
  if (Date.now() - (got[stamp] || 0) < NOTIFY_EVERY_MS) return;
  await chrome.storage.local.set({ [stamp]: Date.now() });
  try {
    chrome.notifications.create(key + ":" + Date.now(), {
      type: "basic",
      iconUrl: chrome.runtime.getURL("icon-128.png"),
      title,
      message,
      priority: 2,
    });
  } catch (e) {
    console.warn("[xbc] could not raise notification", e);
  }
}

async function serverAlive() {
  try {
    const r = await call("/api/ping");
    return !!(r.ok && r.data && r.data.service === "xcollector");
  } catch (e) {
    return false;
  }
}

async function runSync() {
  // Check before opening anything. If the server is down the tab would load,
  // capture, and have nowhere to deliver -- so this saves a pointless page
  // load as well as making the failure visible.
  if (!(await serverAlive())) {
    await chrome.storage.local.set({ syncStartedAt: 0 });
    await notifyOnce(
      "serverdown",
      "Xplore — not running",
      "Bookmarks are not being captured. Start the server (start.bat in the " +
      "xcollector folder), then it will catch up on its own.");
    return;
  }
  await chrome.storage.local.set({ syncStartedAt: Date.now() });
  // Reuse a bookmarks tab if one is already open -- reloading it is cheaper
  // and less surprising than opening a second one.
  const tabs = await chrome.tabs.query({ url: ["https://x.com/i/history*",
                                               "https://x.com/i/bookmarks*"] });
  if (tabs.length) {
    await chrome.storage.local.set({ syncTabId: tabs[0].id, syncTabOurs: false });
    chrome.tabs.reload(tabs[0].id);
    return;
  }
  // None open: make one, in the background so it never steals focus.
  const tab = await chrome.tabs.create({ url: SYNC_URL, active: false });
  await chrome.storage.local.set({ syncTabId: tab.id, syncTabOurs: true });
}

/** Called by the content script when a scheduled run finishes.
 *  Closes the tab only if we were the ones who opened it. */
async function syncFinished(added) {
  const { syncTabId, syncTabOurs } = await chrome.storage.local.get(
    ["syncTabId", "syncTabOurs"]);
  await chrome.storage.local.set({ lastSync: Date.now(), lastAdded: added || 0,
                                   syncStartedAt: 0,
                                   // Reset the throttles on success, so the
                                   // *next* outage is reported immediately
                                   // rather than being suppressed by a
                                   // notification sent half a day ago.
                                   notified_serverdown: 0, notified_stalled: 0 });
  if (syncTabOurs && syncTabId) {
    try { await chrome.tabs.remove(syncTabId); } catch (e) {}
  }
  await chrome.storage.local.set({ syncTabId: null, syncTabOurs: false });
}

// ── thread pass driver ───────────────────────────────────────────────────────
//
// The worker runs the thread pass; the page only captures and reports.
//
// The first design let each page navigate itself with location.href. That ran
// every tweet through one renderer process, which grew until Chrome killed it
// with "Out of Memory" -- measured at 1,224 -> 1,735 -> 2,139 MB within about
// three minutes, one crash roughly every 20 minutes. A crashed page runs no
// code, so nothing in it could recover, and a run left overnight stood idle
// from 04:00 to 11:37. Three changes, all here:
//
//   * One tab. The run records its tab id, and only that tab takes part, so a
//     tweet opened in another tab is no longer pulled into the pass.
//   * Recycling. Every RECYCLE_EVERY tweets the tab is replaced by a fresh one,
//     which gets a fresh renderer process and so starts from zero memory.
//   * A watchdog. The page sends a heartbeat on every tweet; if none arrives
//     for BEAT_STALE_MS the tab is recreated. Crashes it cannot prevent, it at
//     least recovers from.

const THREAD_ALARM = "xbc-thread-watch";
// Well under the smallest crash-free run observed (36 tweets).
const RECYCLE_EVERY = 25;
const BEAT_STALE_MS = 3 * 60 * 1000;

async function getRun() {
  const { threadRun } = await chrome.storage.local.get("threadRun");
  return threadRun || { on: false };
}

async function setRun(patch) {
  const run = Object.assign({}, await getRun(), patch);
  await chrome.storage.local.set({ threadRun: run });
  return run;
}

/** Point the run's tab at `url`, or replace the tab with a fresh one.
 *  The new tab is created before the old one is closed, so closing the old tab
 *  can never close its window. */
async function openThreadUrl(oldTabId, url, fresh) {
  let old = null;
  try { old = oldTabId ? await chrome.tabs.get(oldTabId) : null; } catch (e) { old = null; }
  if (old && !fresh) {
    await chrome.tabs.update(old.id, { url });
    return old.id;
  }
  // Created blank and recorded as the run's tab *before* it is sent anywhere.
  // Otherwise the new page can boot and ask "am I the run's tab?" before the
  // run knows its id, be told no, and sit idle until the watchdog fires -- a
  // three-minute stall on every single recycle.
  const opts = { url: "about:blank", active: old ? !!old.active : false };
  if (old) { opts.windowId = old.windowId; opts.index = old.index + 1; }
  const created = await chrome.tabs.create(opts);
  await setRun({ tabId: created.id });
  if (old) { try { await chrome.tabs.remove(old.id); } catch (e) {} }
  await chrome.tabs.update(created.id, { url });
  return created.id;
}

async function threadStart(tabId) {
  if (!(await serverAlive())) return { ok: false, error: "server unreachable" };
  await setRun({ on: true, tabId, sinceRecycle: 0, lastBeat: Date.now(),
                 current: null, retriedId: null, done: 0, recoveries: 0,
                 recycles: 0, startedAt: Date.now() });
  chrome.alarms.create(THREAD_ALARM, { periodInMinutes: 1 });
  return threadAdvance();
}

async function threadEnd(reason) {
  await setRun({ on: false, endedAt: Date.now(), endReason: reason });
  chrome.alarms.clear(THREAD_ALARM);
  if (reason === "complete") {
    try {
      chrome.notifications.create("threadsdone:" + Date.now(), {
        type: "basic", iconUrl: chrome.runtime.getURL("icon-128.png"),
        title: "Xplore — thread pass complete",
        message: "Every queued thread has been fetched.", priority: 1,
      });
    } catch (e) {}
  }
}

/** Claim the next tweet from the server and send the run's tab to it. */
async function threadAdvance() {
  const run = await getRun();
  if (!run.on) return { ok: true, stopped: true };

  let r;
  try { r = await call("/api/threads/next"); } catch (e) { r = { ok: false, status: 0 }; }
  if (!r.ok) {
    // Don't end the run over a server hiccup. Leave it for the watchdog, which
    // retries once the heartbeat goes stale, and say so out loud.
    await setRun({ lastBeat: Date.now() });
    await notifyOnce("serverdown", "Xplore — not running",
      "The thread pass cannot reach the server. Start it (start.bat) and the " +
      "pass will resume on its own.");
    return { ok: false, error: r.status ? "server error " + r.status : "server unreachable" };
  }

  const nxt = r.data && r.data.next;
  if (!nxt) { await threadEnd("complete"); return { ok: true, complete: true }; }

  const fresh = (run.sinceRecycle || 0) >= RECYCLE_EVERY;
  const tabId = await openThreadUrl(run.tabId, nxt.url, fresh);
  await setRun({ tabId, current: nxt, lastBeat: Date.now(),
                 remaining: r.data.remaining,
                 sinceRecycle: fresh ? 1 : (run.sinceRecycle || 0) + 1,
                 recycles: (run.recycles || 0) + (fresh ? 1 : 0) });
  return { ok: true, next: nxt, remaining: r.data.remaining };
}

async function threadDone(tabId) {
  const run = await getRun();
  if (!run.on || run.tabId !== tabId) return { ok: true, ignored: true };
  await setRun({ done: (run.done || 0) + 1, lastBeat: Date.now(), retriedId: null });
  return threadAdvance();
}

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== THREAD_ALARM) return;
  const run = await getRun();
  if (!run.on) { chrome.alarms.clear(THREAD_ALARM); return; }
  if (Date.now() - (run.lastBeat || 0) < BEAT_STALE_MS) return;

  // Nothing from the page for three minutes: crashed, frozen or discarded.
  if (run.current && run.retriedId !== run.current.id) {
    // Give the same tweet one fresh tab before blaming it.
    const tabId = await openThreadUrl(run.tabId, run.current.url, true);
    await setRun({ tabId, lastBeat: Date.now(), retriedId: run.current.id,
                   sinceRecycle: 1, recoveries: (run.recoveries || 0) + 1 });
  } else {
    // This tweet has now killed two tabs. Move on, in a fresh tab.
    await setRun({ retriedId: null, sinceRecycle: RECYCLE_EVERY,
                   recoveries: (run.recoveries || 0) + 1 });
    await threadAdvance();
  }
});

// Reloading the extension orphans the page that was running the pass, so any
// run in progress is closed and must be started again by hand. The old
// page-driven flag is dropped too.
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.remove("threadMode");
  setRun({ on: false, endReason: "extension reloaded" });
  chrome.alarms.clear(THREAD_ALARM);
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.__xbc !== true) return;

  const done = (r) => sendResponse(r);
  const fail = (e) => sendResponse({ ok: false, status: 0, error: String(e && e.message || e) });

  if (msg.kind === "ping") {
    call("/api/ping").then(done).catch(fail);
    return true;                       // keep the channel open for the async reply
  }
  if (msg.kind === "ingest") {
    call("/api/ingest", msg.body, true).then(done).catch(fail);
    return true;
  }
  if (msg.kind === "scanlog") {          // panel status mirrored to the server log
    call("/api/scanlog", JSON.stringify({ msg: String(msg.body || "") }), true)
      .then(done).catch(fail);
    return true;
  }
  if (msg.kind === "ingestThread") {
    call("/api/ingest/thread?root=" + encodeURIComponent(msg.root), msg.body, true)
      .then(done).catch(fail);
    return true;
  }
  if (msg.kind === "threadsNext") {
    call("/api/threads/next").then(done).catch(fail);
    return true;
  }
  // The thread pass. Every one of these is answered relative to the tab that
  // sent it: only the run's own tab takes part.
  const senderTab = _sender.tab && _sender.tab.id;
  if (msg.kind === "threadStart") {
    threadStart(senderTab).then(done).catch(fail);
    return true;
  }
  if (msg.kind === "threadStop") {
    threadEnd("stopped").then(() => sendResponse({ ok: true })).catch(fail);
    return true;
  }
  if (msg.kind === "threadDone") {
    threadDone(senderTab).then(done).catch(fail);
    return true;
  }
  if (msg.kind === "threadBeat") {
    getRun().then(async (run) => {
      if (run.on && run.tabId === senderTab) await setRun({ lastBeat: Date.now() });
      sendResponse({ ok: true });
    }).catch(fail);
    return true;
  }
  if (msg.kind === "threadRole") {
    getRun().then((run) => sendResponse({
      ok: true, on: !!run.on, mine: !!(run.on && run.tabId === senderTab),
      done: run.done || 0, remaining: run.remaining, recycles: run.recycles || 0,
      recoveries: run.recoveries || 0,
    })).catch(fail);
    return true;
  }
  // Capture-on-view, from capture.js on archive.today pages.
  if (msg.kind === "captureStatus") {
    call("/api/capture/status?page=" + encodeURIComponent(msg.body.page))
      .then(done).catch(fail);
    return true;
  }
  if (msg.kind === "capture") {
    call("/api/capture?page=" + encodeURIComponent(msg.body.page), msg.body.html, false)
      .then(done).catch(fail);
    return true;
  }
  if (msg.kind === "ingestDom") {
    call("/api/ingest/dom", msg.body, true).then(done).catch(fail);
    return true;
  }
  // The content script asks whether this page load was a scheduled sync, so it
  // knows whether it may backfill unattended. sessionStorage cannot answer
  // that for a tab the worker just created.
  if (msg.kind === "isSyncTab") {
    chrome.storage.local.get(["syncTabId"]).then(({ syncTabId }) =>
      sendResponse({ ok: true, isSync: syncTabId === (_sender.tab && _sender.tab.id) }));
    return true;
  }
  if (msg.kind === "syncDone") {
    syncFinished(msg.body).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.kind === "syncNow") {          // the panel's manual trigger
    runSync().then(() => sendResponse({ ok: true }));
    return true;
  }
});
