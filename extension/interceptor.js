/**
 * interceptor.js — runs in the PAGE's world, not the extension's.
 *
 * This has to be a `"world": "MAIN"` content script. Two things were verified
 * against the live site and both rule out the usual approaches:
 *
 *   1. An ordinary content script runs in an isolated world with its own
 *      `window`, so patching XMLHttpRequest there intercepts nothing the page
 *      does. Tested: the patch caught zero requests while the timeline loaded.
 *   2. Injecting a <script> tag with inline code does not execute on x.com --
 *      its CSP blocks it. Tested via a shared-DOM attribute the injected code
 *      never got to set.
 *
 * Manifest-declared MAIN-world injection is extension-privileged and bypasses
 * both. Firefox MV2 would use window.wrappedJSObject instead; userscripts get
 * the same thing as unsafeWindow.
 *
 * Nothing is parsed here. The raw body is handed to the isolated world, which
 * relays it to localhost for the Python side to interpret. Keeping the browser
 * dumb means a change in X's field names is a server edit, not a new extension
 * build that has to be reloaded.
 *
 * ── Three ways this captured nothing, all fixed here ────────────────────────
 *
 * On 25 November 2026 a full run reported "0 new, 0 seen, 0 dated, 0 pages"
 * while the bookmarks timeline plainly loaded in front of the owner. The panel
 * was talking to the server, so the break was here. Any of these produces that
 * exact symptom, and from outside they are indistinguishable:
 *
 *   - **The page may use fetch rather than XHR.** Only XHR was hooked. Both
 *     are hooked now.
 *   - **The response may not be text.** `xhr.responseText` *throws*
 *     (InvalidStateError) when `responseType` is "json" or "arraybuffer", and
 *     the old code swallowed that in a catch whose message nothing ever read.
 *     Every response type is handled, and a failure to read one is reported.
 *   - **X may have renamed the operation.** Matching is loose now, and every
 *     GraphQL operation name seen is reported once, so a rename costs one line
 *     in the server log instead of a silent week of empty runs.
 *
 * Diagnostics go to the isolated world as "diag" messages, which bridge.js
 * forwards to the server log. Nothing here writes to the console and hopes
 * somebody is looking at it.
 */

(function () {
  "use strict";

  const TAG = "[xbc]";
  const seen = new Set();          // operation names already reported
  const MAX_REPORTED_OPS = 25;     // x.com fires a lot of them; enough to spot a rename

  const post = (kind, detail) =>
    window.postMessage({ __xbc: true, kind, ...detail }, window.location.origin);

  const diag = (msg) => post("diag", { msg: String(msg).slice(0, 300) });

  // Which operation carries the bookmarks timeline. Deliberately loose: X
  // relabelled the route /i/bookmarks -> /i/history in August 2026 without
  // renaming the operation, so the next change may well be the other way
  // round. Mutations are excluded -- adding or deleting a single bookmark
  // returns a tiny payload with no tweets in it, which the server would
  // quarantine as an empty timeline and the panel would read as rate limiting.
  const TIMELINE = /bookmark|history/i;
  const MUTATION = /create|delete|remove|add|update|toggle|folder|move/i;

  function classify(op) {
    if (op.indexOf("TweetDetail") !== -1) return "threaddetail";
    if (TIMELINE.test(op) && !MUTATION.test(op)) return "bookmarks";
    return null;
  }

  /** The response body as text, whatever the page asked for.
   *
   * Returns a string, or null when the body has to be read asynchronously.
   * Throws only what the XHR itself throws, so the caller can report it.
   */
  function bodyOf(xhr) {
    const type = xhr.responseType;
    if (type === "" || type === "text") return xhr.responseText;
    if (type === "json") return xhr.response == null ? "" : JSON.stringify(xhr.response);
    if (type === "arraybuffer") return new TextDecoder("utf-8").decode(new Uint8Array(xhr.response));
    return null;                   // blob, document: handled by the caller
  }

  // ── asking for page one ─────────────────────────────────────────────────────
  //
  // On a fresh load x.com renders the newest bookmarks without requesting them
  // (its own cache), and nothing of that page is in the document either --
  // checked, 25 November 2026. The first request we can see is triggered by
  // scrolling and returns page TWO, so the newest bookmarks were invisible:
  // a run reported "83 seen, 0 new" with two days of bookmarks on screen.
  //
  // The fix is x.com's own request with the cursor taken out, which is exactly
  // what the page itself sends on a cold load. One extra request per page
  // load, on the page's own session and headers, issued once -- far below the
  // traffic the scrolling itself produces.
  let askedForFirstPage = false;

  function requestFirstPage(url, headers) {
    if (askedForFirstPage) return;
    askedForFirstPage = true;
    let first;
    try {
      const u = new URL(url, location.origin);
      const vars = JSON.parse(u.searchParams.get("variables") || "{}");
      // The key has been plain "cursor" for years, but the whole response was
      // renamed in November 2026, so it is matched rather than assumed.
      const key = Object.keys(vars).find((k) => /cursor/i.test(k));
      if (!key) {
        return diag("request carried no cursor, so it was already page one: "
                    + Object.keys(vars).join(","));
      }
      delete vars[key];
      u.searchParams.set("variables", JSON.stringify(vars));
      first = u.toString();
    } catch (e) {
      return diag("could not build the page-one request: " + e);
    }

    const opts = { credentials: "include", method: "GET" };
    if (headers) opts.headers = headers;

    origFetch(first, opts)
      .then((res) => res.text().then((t) => {
        if (!t) return diag("page one came back empty (HTTP " + res.status + ")");
        diag("asked x.com for page one directly (HTTP " + res.status + ")");
        post("bookmarks", { body: t, status: res.status });
      }))
      .catch((e) => diag("page-one request failed: " + e));
  }

  function deliver(kind, op, body, status, via) {
    if (kind === "threaddetail") post("threaddetail", { body });
    else post("bookmarks", { body, status });
    const mark = "hit:" + op + ":" + via;
    if (!seen.has(mark)) {
      seen.add(mark);
      diag(`capturing ${op} over ${via}`);
    }
  }

  function noteOp(op, via) {
    if (!op || seen.has(op) || seen.size > MAX_REPORTED_OPS) return;
    seen.add(op);
    console.debug(`${TAG} GraphQL op seen and ignored: ${op}`);
    diag(`op seen and ignored (${via}): ${op}`);
  }

  // Operation name is the last path segment before the query string.
  const opOf = (url) => url.split("?")[0].split("/").pop() || "";

  // Worth looking inside. "/graphql/" is where the timeline has always lived,
  // but a move to a plain REST path would be invisible through that filter
  // alone and would look exactly like the November 2026 outage did, so
  // anything with "bookmark" in the address is examined too.
  const WATCH = (url) => url.indexOf("/graphql/") !== -1 || /bookmark/i.test(url);

  // ── XMLHttpRequest ──────────────────────────────────────────────────────────

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    try {
      this.__xbcUrl = typeof url === "string" ? url : String(url || "");
    } catch (e) {}
    return origOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function () {
    const xhr = this;
    const url = xhr.__xbcUrl || "";

    if (WATCH(url)) {
      const onLoad = function () {
        xhr.removeEventListener("load", onLoad);
        const op = opOf(url);
        const kind = classify(op);
        if (!kind) return noteOp(op, "xhr");

        let body = null;
        try {
          body = bodyOf(xhr);
        } catch (e) {
          return diag(`could not read ${op} (responseType "${xhr.responseType}"): ${e}`);
        }

        if (body) return deliver(kind, op, body, xhr.status, "xhr");

        // A Blob has to be read asynchronously; everything else that lands
        // here really was empty, which is what X returns when it throttles.
        const r = xhr.response;
        if (body === null && r && typeof r.text === "function") {
          r.text().then((t) => {
            if (t) deliver(kind, op, t, xhr.status, "xhr blob");
            else post("bookmarksEmpty", { status: xhr.status });
          }).catch((e) => diag(`could not read ${op} blob: ${e}`));
          return;
        }
        if (kind === "bookmarks") post("bookmarksEmpty", { status: xhr.status });
      };
      xhr.addEventListener("load", onLoad);
    }
    return origSend.apply(this, arguments);
  };

  // ── fetch ───────────────────────────────────────────────────────────────────
  //
  // X sent the bookmarks timeline over XHR when this was written, and fetch was
  // deliberately left alone. That is exactly the assumption that has to stop
  // being load-bearing: hooking both costs one clone of a response the page has
  // already received, and removes a whole class of silent failure.

  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function (input, init) {
      let url = "";
      try {
        url = typeof input === "string" ? input : (input && input.url) || "";
      } catch (e) {}
      // A Request object carries its own headers; a string URL leaves them in
      // init. Either way this is what page one has to be asked for with.
      const headers = (init && init.headers)
        || (input && typeof input !== "string" && input.headers) || null;

      const out = origFetch.apply(this, arguments);
      if (!url || !WATCH(url)) return out;

      // .then on the original promise: a rejection still propagates to the
      // page untouched, and the page gets the original Response object.
      return out.then((res) => {
        try {
          const op = opOf(url);
          const kind = classify(op);
          if (!kind) noteOp(op, "fetch");
          else {
            res.clone().text()
              .then((t) => {
                if (t) deliver(kind, op, t, res.status, "fetch");
                else if (kind === "bookmarks") post("bookmarksEmpty", { status: res.status });
              })
              .catch((e) => diag(`could not read ${op} over fetch: ${e}`));
            // The page's own request carries the session headers, so page one
            // is asked for with them rather than anything assembled here.
            if (kind === "bookmarks" && onBookmarksPage())
              requestFirstPage(url, headers);
          }
        } catch (e) {}
        return res;
      });
    };
  }

  // ── the first page never crosses the network ────────────────────────────────
  //
  // Measured 25 November 2026: on a fresh load of /i/history, x.com renders the
  // newest bookmarks WITHOUT requesting them. The first BookmarkTimeline fetch
  // happens only once scrolling starts, and it returns the *second* page. A run
  // reported "83 seen, 0 new, 0 dated" while two days of new bookmarks sat on
  // screen, because page one was already inside the document.
  //
  // So the document is read once per load and whatever it carries is handed
  // over exactly like a network response. The server finds tweets structurally,
  // so how the blob is nested does not matter.

  const BOOKMARK_PATHS = ["/i/bookmarks", "/i/history"];
  const onBookmarksPage = () =>
    BOOKMARK_PATHS.some((p) => location.pathname.indexOf(p) !== -1);

  const MARKERS = ["bookmark_timeline", "BookmarkTimeline", 'entry_id":"tweet-'];
  let sentLength = 0;               // the blob already handed over, by length

  function scanDocument(why) {
    if (!onBookmarksPage()) return;
    let candidates = 0, parsed = 0;
    const shapes = [];

    for (const s of document.querySelectorAll("script")) {
      const text = s.textContent || "";
      if (text.length < 200 || !MARKERS.some((m) => text.indexOf(m) !== -1)) continue;
      candidates += 1;
      if (text.length === sentLength) continue;
      try {
        JSON.parse(text);            // whole script is JSON: hand it straight over
        sentLength = text.length;
        parsed += 1;
        post("bookmarks", { body: text, status: 200 });
      } catch (e) {
        // Wrapped in an assignment or a call. Report the opening so the shape
        // can be read off the log rather than guessed at.
        shapes.push(`type=${s.type || "(none)"} len=${text.length} head=${text.slice(0, 90)}`);
      }
    }

    // Belt and braces: the state may be on a global rather than in a script.
    for (const name of ["__INITIAL_STATE__", "__NEXT_DATA__", "__META_DATA__", "__STATE__"]) {
      try {
        const v = window[name];
        if (!v) continue;
        const text = typeof v === "string" ? v : JSON.stringify(v);
        if (!MARKERS.some((m) => text.indexOf(m) !== -1) || text.length === sentLength) continue;
        sentLength = text.length;
        parsed += 1;
        post("bookmarks", { body: text, status: 200 });
      } catch (e) {}
    }

    if (parsed) diag(`preloaded bookmarks handed over (${why}, ${parsed} blob(s))`);
    else if (candidates) diag(`preloaded blob found but not JSON (${why}): ${shapes[0] || ""}`);
    else if (why === "late")
      diag("no preloaded bookmark payload in the document; page one may need a scroll to the top");
  }

  // Three looks: the markup arrives in pieces, and a single-page navigation
  // into /i/history renders without reloading this script at all.
  if (onBookmarksPage()) {
    document.addEventListener("DOMContentLoaded", () => scanDocument("domready"));
    setTimeout(() => scanDocument("1s"), 1000);
    setTimeout(() => scanDocument("late"), 4000);
  }

  console.info(`${TAG} interceptor active in page world`);
  post("ready", {});
  diag(`interceptor active (fetch ${typeof origFetch === "function" ? "hooked" : "absent"})`);
})();
