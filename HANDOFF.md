# Xplore — handoff

**Xplore** is a standalone local tool that captures your x.com bookmarks, resolves
and archives the articles they link to, classifies everything by topic, and makes
the lot searchable. It currently lives at `C:\Users\mihal\Desktop\Code\xcollector`
and runs on **port 8502** (the port the earlier, lost tool used).

It is **deliberately separate from Mneme**. That is an owner decision, recorded
2026-09-04 and not to be revisited casually: browser bookmarks and x.com
bookmarks are different collections and keep different stores. Searching the two
together is a *federation* job, not a merge. Do not add an `x` collection to
Mneme, and do not route tweets or their articles through Mneme's store.

---

## TODO — start here

### 1. Capture-on-view for the 1,126 unarchivable articles  ← highest value

**The finding that drives this:** an NYT article that the archiver could not get
*was* fully available on archive.is once the owner passed the bot check by hand.
So the content exists; only automated access is blocked.

**Two things are true and both matter:**

- **The archive.ph / archive.is tier was never implemented.** Earlier notes and
  conversation described a "three-tier" fetch (direct → Wayback → archive.ph).
  Only two tiers were built. `articles.source` shows 3,854 `direct`, 1,879
  `wayback`, and zero `archiveph`. The schema comment mentions it; no code does.
- **Adding it server-side will only partly work.** archive.ph sits behind
  Cloudflare, which is the same wall that produced 699 `http403` results in the
  first place. Mneme's `fetcher.py` has `_archph_cloudflare()` and a mirror list
  (`archive.ph`, `.is`, `.today`, `.li`, `.fo`) precisely because it kept
  getting challenged — port that, but expect partial success.

**The reliable route is Mneme's "auto-scrape on view".** Mneme's extension
captures a page's text when the owner *visits* a bookmarked page that has no
text yet (`background.js`, plus `/api/clip` and `/api/clip/status`). A human in
the loop passes the Cloudflare check for free.

Suggested shape:

1. A worklist view in Xplore listing the 1,126 (`articles.status IN
   ('failed','thin','botscreen')`), each with an archive.is link, ordered by how
   many bookmarks reference it.
2. Extend the Xplore extension with a content script on `archive.ph|is|today|li|fo`
   and on the publisher domains, which asks the server "do you already have text
   for this URL?" and if not, extracts and posts it.
3. Reuse `archive_links.py`'s `extract()` and `classify()` so a captured page is
   stored and classified identically to a fetched one.

Worth reading first: `bookmarktool/version2/fetcher.py` (`should_skip`,
`_is_bot_screen`, `_archph_cloudflare`, the mirror rotation) and
`bookmarktool/extension/background.js` for the on-view trigger. Read only —
Mneme has its own active session.

### 2. Make Xplore a real desktop app

Today it is a Flask process started by a login script plus a browser tab. It
should be a thing you launch from the desktop.

- **Name: Xplore.** Rename the folder, the window title, `app.html`'s `<title>`,
  the extension name in `extension/manifest.json`, and the server banner.
- **Branding assets:** the owner has a logo (wordmark) and an app icon (rounded
  square, blue gradient, X + bookmark-stack motif). **They are not yet in the
  repo** — they were shared in chat and need saving to
  `xcollector/assets/xplore-logo.png` and `assets/xplore-icon.png`, then wired
  into `app.html`'s header and the extension icons (replacing the placeholder
  `icon-16/48/128.png` I generated).
- **Launcher options**, cheapest first: a desktop shortcut to
  `start-collector.vbs` that also opens `http://localhost:8502`; or a real
  window via `pywebview`/Tauri wrapping the same local server; or Electron if a
  tray icon and native menus are wanted. The server does not need to change for
  any of them.
- Keep the login auto-start — but see the Unicode trap below.

### 3. Then the phases the owner has already scoped

- **(d)** LLM-powered second level: find related tweets and links *across Xplore
  and Mneme's full-text repository*. This is the federation seam, and it is why
  the stores are separate.
- **(e)** A Compendium auto-generated Story Tracker over the same period, from
  an OPML list the app prompts for.
- **(f)** Second integration path across all three apps, parallel to the OSINT
  path.
- **(g)** Xplore eventually producing and updating a *separate* Mneme
  collection. Design the seam, not a merge.

### 4. Smaller, still open

- **Video transcripts — done.** `yt-dlp` installed; `transcripts.py` recovered
  102 of 150 (629,222 words). The rest: 19 no subtitles, 8 private, 8
  unavailable, 6 download errors, 4 bot-blocked, 3 OS errors. **The video
  detector is too broad** — `odysee.com/` also matches *channel* pages, which
  yt-dlp cannot read at all. Tighten `VIDEO_HOSTS` in `archive_links.py`.
- **Bookmark dates for the backfill.** See the known-limitations section — this
  is the one data gap that will never heal by itself.
- **4 tweets are unclassified** out of 27,262; DeepSeek declines them. Harmless.
- **Thread pass: check it finished**, then regenerate the exports so threads
  show. Two robustness fixes are still owed, both needing an extension reload
  so *not* mid-run: (a) **scope thread mode to its own tab** — the flag is
  extension-wide, so any `/status/` page opened in the same Chrome profile gets
  pulled into the pass; (b) **auto-recover from a renderer crash** — a crashed
  tab runs no JS, so the 45s timeout cannot fire; the worker should notice and
  reload it.

---

## Current state (night of 12–13 September)

| | |
|---|---|
| Tweets | 27,262 |
| Classified by topic | 27,258 (25-topic taxonomy) |
| Unique outbound links | 5,727 (after shortener resolution) |
| Articles archived | 4,457 — 3,854 direct, 603 Wayback |
| Words of archived text | ~8,000,000 |
| Unrecoverable articles | 1,126 (the TODO-1 worklist) |
| Video transcripts | **done** — 102 of 150 (68%), 629,222 words |
| Thread pass | **running overnight** — 182 fetched by 23:00 UTC, 175 real threads, 1,954 author replies; ~2,000 left at ~4/min |
| Database | `xbookmarks.db`, ~138 MB |

---

## Architecture

**Capture is interception, not scraping.** The extension hooks the page's
`XMLHttpRequest` and matches the GraphQL operation name — `Bookmarks` for the
timeline, `TweetDetail` for threads. Three things were established the hard way
and must not be undone:

- A normal content script runs in an **isolated world** and its `window` is not
  the page's, so patching XHR there catches nothing. The interceptor must be a
  `"world": "MAIN"` content script.
- **Inline `<script>` injection does not execute on x.com** (CSP). MAIN-world
  manifest registration is the only route; Firefox MV2 would use
  `wrappedJSObject`, userscripts use `unsafeWindow`.
- **X uses XHR, not `fetch`.** Patching `fetch` catches nothing.
- **Content-script `fetch` to `localhost` is refused** in MV3 — a simple GET
  slips through, but any preflighted POST from an https page to a loopback
  address is blocked before the server sees it. All network calls go through the
  **service worker**, which runs under the extension's own host permissions.

**Parsing happens server-side, in Python**, from the *raw* response. When X
renames a field the fix is a server edit and a re-parse, not an extension
release. Unparsable payloads are saved to `unparsed/` so they can be replayed
offline.

**Scheduling is `chrome.alarms` in the service worker**, not a page timer. A
page `setTimeout` only fires while that exact tab is open and unfrozen — that
mistake silently skipped 7 September. The alarm wakes hourly, acts when a sync
is ~20h overdue, opens a background tab if none exists, and closes it after.
A watchdog abandons any run that has not reported back in 30 minutes.

**`/i/bookmarks` now 302s to `/i/history`**, but the GraphQL operation was *not*
renamed with the route. Match the operation, not the path; both paths are handled.

---

## Files

| File | |
|---|---|
| `server.py` | Flask: ingest, find, article viewer, topics, tags, removal, thread queue |
| `store.py` | SQLite schema, migrations, `find()`, tombstones, threads |
| `parse.py` | GraphQL → records. Pure, no I/O. `parse_thread` keeps author-only replies |
| `app.html` | The UI. Plain file, editable without restarting |
| `resolve_links.py` | Follows shorteners to real destinations |
| `enrich_links.py` | Fetches title + publication date only |
| `archive_links.py` | direct + Wayback archiving, `extract()`, `classify()`, `is_video()` |
| `topics.py` | Two-pass LLM classification (DeepSeek) |
| `export_links.py` | CSV + HTML exports, and the `link_export` SQL view |
| `export_unrecovered.py` | The TODO-1 worklist, grouped by domain |
| `extension/` | MV3: `interceptor.js` (MAIN), `bridge.js` (isolated), `background.js` (worker) |
| `start-collector.vbs`, `start.bat` | Launch with no console window |

`DEEPSEEK_API_KEY` is a persistent user environment variable. Only DeepSeek is
configured — no Anthropic or OpenAI key.

---

## Traps that have already cost time

**Found on the night of 12–13 September:**

- **Lock contention drops bookmarks silently.** `transcripts.py` committed every
  5 videos at ~30s each, holding a write transaction for ~2½ minutes, while the
  server had SQLite's 5s default timeout. Result: 32 × `database is locked` and
  500s on `/api/ingest` — bookmarks not captured. Fixed with commit-per-row and
  `PRAGMA busy_timeout = 30000` in `store.py`. **Any new pass must commit per
  row and set a busy timeout. Never run two sessions writing this DB at once.**
- **`ask(kind, body)` silently dropped extra fields.** The thread post never
  sent `root`, so the server got `root=undefined`, never marked the tweet
  fetched, and the pass reloaded one permalink forever. Fixed; the queue now
  also **counts attempts at hand-out** (max 2), plus a 45s stall timeout and a
  same-URL guard, so no single tweet can stall the pass again.
- **Ordering by `reply_count` picks viral tweets, not threads.** The first offer
  was a 13,940-reply post whose page froze the renderer. Now: self-declared
  threads first (🧵, "1/", "a thread"), and `reply_count < 5000` excluded.
- **Mneme's server has the same command line.** Both run as `python server.py`
  with relative paths, so killing by command line would take down Mneme on
  7331. Find the owner of **8502** with `netstat -ano` and kill only that PID and
  its parent.
- **A failed call looked like an empty queue.** "no threads queued" fired on
  errors too. The panel now says `server error N` or `server unreachable`.
- **Buffered logs hide deaths.** The transcript pass died after 5 videos with an
  empty log. Run passes with `python -u` and write an exit marker.

**Earlier:**

- **cp1253 kills the server on startup.** The banner prints box-drawing
  characters; launched from the login script, stdout is a redirected file in the
  legacy Greek codepage, and the `UnicodeEncodeError` kills the process *before
  it binds the port*. It looked exactly like "no new bookmarks" for two days.
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` is at the top of
  `server.py` — keep it in anything new that prints.
- **`Get-NetTCPConnection` is unreliable on this machine.** It reports nothing
  while the port is plainly serving. Use `netstat -ano`. Trusting it once caused
  me to kill the running server.
- **PowerShell `*>` writes UTF-16 logs.** A `grep` for a completion line in such
  a log never matches, which left a background waiter spinning forever. Use
  `| Out-File -Encoding utf8`.
- **archive.org has rate-limited this machine before.** Wayback pacing (8s, a
  longer pause every fifth, exponential backoff on 403) is not tunable downward.
  The dead links exist *only* there.
- **Removal must be a tombstone.** The tweet is still bookmarked on x.com, so a
  hard delete is silently undone by the next sync. `tweets.deleted` + skip in
  `upsert_tweets`.
- **A shorter capture must never overwrite a longer one**, or re-running the
  collector destroys text an expansion pass recovered.
- **t.co returns HTTP 200, not a 3xx.** The destination is in a meta-refresh and
  a JS `location.replace`. A redirect-only resolver marks every t.co link
  "resolved to itself" — which the first pass did.

---

## Known limitations, stated plainly

- **Bookmark dates — SOLVED 14 Sep, backfill complete (23,049 dated, 2019–2026).** `sortIndex` is Unix
  milliseconds shifted left 20 bits (not a snowflake; the snowflake reading was
  tried and was wrong). The parser stores `sort_index` + `bookmarked_at`; a Deep
  scan fills the backfill; `redate_bookmarks.py` recomputes dates offline. The
  earlier "decoded to 2023-04-23" was the wrong decoding. See
  `HANDOFF-PHASE-2.md` §1.
- **Thread detection under-counts.** `is_thread` fires on tweets that are
  *replies*, but the commonest bookmark is a thread's *opener*, which is
  indistinguishable from a standalone tweet in the payload. The queue unions
  that flag with text markers (🧵, "1/", "a thread"). It is a heuristic.
- **The parser was written against a synthetic payload** — the real `Bookmarks`
  shape was never directly observed, because Chrome's devtools bridge does not
  surface these XHRs (x.com runs a service worker). It handled 27,000 real
  tweets with one quarantined payload, which turned out to be the
  end-of-timeline marker. `test_parse.py` is a regression net, not proof.
- **Hard paywalls are genuinely lost to automation.** Only 25% of `http401` were
  recovered; the Wayback crawler hits the same wall. TODO-1 is the answer.
- **"Caught up" is a heuristic** — 3 barren pages, ~75 tweets past the last new
  one. Verified once with the `Deep` button (walks ~1,000 deep): found 0 new, so
  the shortcut is sound. Worth re-running monthly as a drift check.

---

## Running it

```
python server.py                  # or start.bat, or the Startup shortcut
```

Then `http://localhost:8502`. The minimal earlier UI is at `/simple`.

Extension: `chrome://extensions` → Developer mode → **Load unpacked** →
`xcollector/extension`. Then `x.com/i/history` shows a panel bottom-right with
**Start** (sync), **Deep** (ignore the caught-up shortcut), **Threads** (the
conversation pass).

Passes, in the order they were meant to run:

```
python resolve_links.py      # shorteners → real URLs
python enrich_links.py       # title + publication date
python archive_links.py      # direct + Wayback
python topics.py --assign    # classify new arrivals
python export_links.py       # CSV + HTML
python export_unrecovered.py # the worklist
```

All are resumable and skip what they have already done, so re-running after a
sync only touches new material. None of them is scheduled yet — **worth wiring
into the same `chrome.alarms` cadence, or a Windows task**, because right now
they drift until run by hand.


---

## Supplement 2 — fork session, 13 September

Read after supplement 1. Numbers below were queried from `xbookmarks.db` when this
was written, not carried over.

### Done

- **Transcripts finished.** A retry added 10 more (114,937 words): 112 videos now have
  transcripts, 751,699 words. The 7 remaining `downloaderror`s are persistent (mostly
  TikTok short links); `nosubs` and `private` were deliberately not retried.
- **`VIDEO_HOSTS` fixed** (`archive_links.py`). Now matches video *paths*, not bare hosts. The old
  pattern caught channel pages, profiles and a homepage, and missed `youtube.com/live/` entirely —
  nine live recordings had been "archived" as articles whose whole text was YouTube's language
  picker. Tested against all stored links before switching. 16 rows moved to `video` and were
  transcribed; 3 non-video rows moved to `skipped`.
- **"Archived" was inflated; now corrected.** `classify()` gained two rules, and every existing
  `ok` row from `direct`/`wayback` was re-judged:
  - **394 subscription teasers → `thin`** — Haaretz 170, FT 105 + 28 via `ft.trib.al`, Jacobin 54,
    WSJ 20, Bloomberg 14. Rule: a subscription marker **and** under 600 words. Markers alone are
    unusable — "already a subscriber" and "subscribe now" sit in the footer of complete
    2,400-word articles. Teaser text is kept (the lede is real and stays searchable).
  - **16 bot walls → `botscreen`** — 13 Anubis ("Making sure you're not a bot"), and **2 archive.ph
    CAPTCHA pages that had been stored as archived articles**. Text blanked.
  - Result: **4,147 ok** (3,476 direct, 559 Wayback, 112 transcripts).
- **TODO 1 built: capture-on-view.** See below.
- **Non-articles taken off the worklist** — `not_article()` in `archive_links.py`, also routed in
  the archiver so future runs skip them: X's own pages (the bare `twitter.com` homepage was the
  most-bookmarked "unrecoverable article", 33 times; plus `x.com/i/broadcasts`) and Telegram channel
  pages without a post number. 16 rows. A broader "bare domain" rule was dry-run and
  rejected: it hid campaign and tool sites bookmarked on purpose and matched 79 homepages that
  archive fine. Substack (`open.substack.com`) and Google Docs/Drive links stay listed — they are
  real content, but archive.today is the wrong route for them (see Still open).

### Capture-on-view — how it works

Verified with a real browser: `archive.ph/newest/<url>` 302s to
`archive.ph/<14-digit timestamp>/<original url>`, then shows **a CAPTCHA even to a normal
browser** ("One more step — Please complete the security check to access archive.ph"). So a
snapshot exists, only a person gets in, and the snapshot URL carries the original address.

- **`capture.py`** (Flask blueprint, registered in `server.py`):
  `GET /api/capture/status?page=`, `POST /api/capture?page=` (raw HTML body),
  `GET /api/capture/worklist`. Reuses `extract()` and `classify()`, so a captured page is judged
  exactly like a fetched one. Matching order: the page address itself (some bookmarks *are*
  `archive.ph/AbCdE` links), then the original from the snapshot path, then normalised (tracking
  params, `www`, trailing slash). Never replaces a longer copy with a shorter one.
- **`extension/capture.js`** on the seven archive.today hosts only — no new host permissions. Does
  nothing on the CAPTCHA page; after the person passes it the page reloads and the script runs
  again. Asks the server first, so unrelated archive pages send nothing. Shows a small confirmation
  in the corner of the page.
- **Recover tab in `app.html`** — the worklist, most-bookmarked first, domain chips, "open on
  archive.today" per article, and a live count of what has been recovered. Returning to the tab
  dims items saved meanwhile without redrawing the list.
- **Server also refuses the CAPTCHA page** by phrase. It is ~100 words, which clears the
  60-word floor; `capture.js` skips it too, but the server does not trust that.

**Tested:** 19/19 end-to-end on a throwaway copy of the database (second server on port 8599):
URL parsing, CAPTCHA refusal, unknown URL, a real save, worklist shrink, no shorter-copy
overwrite, archive-chrome stripping, tracking-param matching, the article viewer, and the stored
row. Then 5/5 for bookmarks that are archive links. Live server verified after redeploy.
Extension files parsed with a real JS parser; manifest validated.

### Needs the owner

- **Reload the extension** (`chrome://extensions` → ↻). The manifest changed, so Chrome may ask to
  allow the extension on archive.today sites. The thread pass is finished, so nothing is interrupted.
- Then **Recover** tab → *open on archive.today* → pass the check. The corner of the page should say
  "Xplore: archived — N words". The first real capture is the true test of `#CONTENT` extraction on
  a live snapshot; that markup is from Mneme's `fetcher.py` and has not been observed directly here.

### Still open

- **Worklist: 1,488 reachable** (525 thin, 17 bot walls, 974 failed). The raw count is
  1,516; the other 28 rows are orphans keyed by shortener or FT sharing URLs that no link points
  to after resolution. Harmless.
- **37 worklist rows are keyed by unresolved `trib.al` shorteners** (ft.trib.al 37). Their archive.today link is
  built from the shortener, which archive.today is unlikely to hold. Resolve them first or capture
  those by hand from the FT URL.
- **Short-id snapshot pages** (`archive.ph/AbCdE`) not already bookmarked as such: the original is
  looked for in the page header. Unverified markup — best-effort.
- **Paywall rule covers six markers.** Other publishers' teasers can still pass as `ok`. The clusters
  query used to find these (same first 60 characters across many rows) is a cheap periodic check.
- **`open.substack.com` (12) and Google Docs/Drive (8) on the worklist** have better automatic routes
  than archive.today: rewrite `open.substack.com/pub/<author>/p/<slug>` to
  `<author>.substack.com/p/<slug>` and fetch directly; public Docs export as text via
  `/export?format=txt`. `capture.js` only runs on archive.today, so opening these by hand captures nothing.
- Threads: 2,186 fetched, 22,249 author replies stored. Exports not regenerated since.
