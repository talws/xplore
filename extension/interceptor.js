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
 * X sends the bookmarks timeline over XHR, not fetch, so fetch is left alone.
 * The match is on the GraphQL *operation name*, which survived the /i/bookmarks
 * to /i/history move in August 2026 -- the route was relabelled, the operation
 * was not.
 *
 * Nothing is parsed here. The raw body is handed to the isolated world, which
 * relays it to localhost for the Python side to interpret. Keeping the browser
 * dumb means a change in X's field names is a server edit, not a new extension
 * build that has to be reloaded.
 */

(function () {
  "use strict";

  const TAG = "[xbc]";
  const seenOps = new Set();

  const post = (kind, detail) =>
    window.postMessage({ __xbc: true, kind, ...detail }, window.location.origin);

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

    if (url.indexOf("/graphql/") !== -1) {
      const onLoad = function () {
        xhr.removeEventListener("load", onLoad);
        // Operation name is the last path segment before the query string.
        const op = url.split("?")[0].split("/").pop() || "";

        if (op.indexOf("Bookmarks") !== -1) {
          try {
            // The status travels too: a rate limit or error page is otherwise
            // indistinguishable, on the server, from X simply not asking.
            if (xhr.responseText) post("bookmarks", { body: xhr.responseText, status: xhr.status });
            else post("bookmarksEmpty", { status: xhr.status });
          } catch (e) {
            post("error", { message: "could not read response: " + e });
          }
        } else if (op.indexOf("TweetDetail") !== -1) {
          // The conversation behind a single tweet. Captured the same way and
          // for the same reason: the page asks for it anyway when you open a
          // permalink, so the thread pass is a navigation loop rather than any
          // new kind of request.
          try {
            if (xhr.responseText) post("threaddetail", { body: xhr.responseText });
          } catch (e) {}
        } else if (!seenOps.has(op)) {
          // The whole capture hangs on one substring match against a name x.com
          // owns and can rename silently -- and the failure mode is nothing
          // happening at all. Naming what was seen and skipped turns the next
          // rename from a mystery into one line in the console.
          seenOps.add(op);
          console.debug(`${TAG} GraphQL op seen and ignored: ${op}`);
          post("op", { op });
        }
      };
      xhr.addEventListener("load", onLoad);
    }
    return origSend.apply(this, arguments);
  };

  console.info(`${TAG} interceptor active in page world`);
  post("ready", {});
})();
