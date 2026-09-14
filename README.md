# X Bookmark Collector

Captures your x.com bookmarks into a local SQLite database. **Standalone** — it
has its own store and shares nothing with Mneme. Searching the two together is a
later job (a federation seam), not a shared database.

Runs on port **8502**, the port the previous tool used.

## Run

```
python server.py
```

Then open <http://localhost:8502> to search. `--db` and `--port` override the
defaults.

## Install the extension (one manual step)

Chrome blocks automation on `chrome://extensions`, so this part is yours:

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. **Load unpacked** → select the `extension/` folder here
4. Go to <https://x.com/i/history>, Bookmarks tab
5. A small panel appears bottom-right. Press **Start**.

It scrolls, captures, and stops on its own when it reaches tweets already
stored. Progress shows in the panel.

## How it captures

It intercepts the `Bookmarks` GraphQL response over XHR. Three things were
verified against the live site, each of which rules out a simpler approach:

- **A normal content script can't do it.** Content scripts run in an isolated
  world with their own `window`, so patching `XMLHttpRequest` there caught zero
  requests while the timeline loaded.
- **Injecting a `<script>` tag doesn't work either** — x.com's CSP blocks inline
  script execution. Hence `"world": "MAIN"` in the manifest, which is
  extension-privileged and bypasses both problems.
- **X sends this over XHR, not `fetch`.** Patching `fetch` catches nothing.

The browser sends the **raw** response; all parsing happens in Python. When X
renames a field the fix is a server edit and a re-parse, not a new extension
build. Anything that fails to parse is saved under `unparsed/` so it can be
replayed offline instead of re-scraped.

`/i/bookmarks` now redirects to `/i/history`, but the GraphQL operation was
*not* renamed with the route — the match is on the operation name, so both work.

## What it stores

`tweets` (id, author, ISO timestamp, full text, counts, quoted tweet),
`media`, `links`, `hashtags`, `topics`, `runs`, plus an FTS5 index for search.

- **No binaries.** Media URLs and author-supplied alt text only.
- **Outbound links** are recorded per tweet and deduplicated across them, ready
  for archiving. `GET /api/links/pending` lists them most-referenced first.
  This is the part no X exporter does.
- **Re-scraping is idempotent** — the tweet id is the primary key — and
  **lossless**: a shorter capture never overwrites a longer one, so re-running
  the collector cannot undo text recovered by an expansion pass.

## Known limits

- **~29% of tweets are truncated at 280 characters** in the timeline, and the
  remainder is genuinely absent from the DOM (verified: `innerText` equals
  `textContent`, no line clamp). Interception recovers long posts via
  `note_tweet`; anything still short is flagged `truncated=1` for a later
  expansion pass.
- **Threads** are flagged (`is_thread`), not expanded. Expanding needs one extra
  request per tweet, which at 24,000 bookmarks would invite rate limiting.
- The **DOM fallback** (`/api/ingest/dom`) is lossier and needs a *visible* tab;
  X will not render a virtualised timeline into a hidden or zero-width window.
  Interception has no such constraint.
- The parser was built against a synthetic payload — the real response shape has
  never been directly observed, because Chrome's devtools bridge does not
  surface these XHRs (x.com runs a service worker). `test_parse.py` is a
  regression net, not proof. The first live run is the real test, which is
  exactly why unparsed payloads are kept.

## Files

| File | |
|---|---|
| `store.py` | SQLite schema, upsert, search, link queue |
| `parse.py` | GraphQL response → tweet records (pure, no I/O) |
| `server.py` | Flask: ingest, search, stats, UI |
| `test_parse.py` | `python test_parse.py` |
| `extension/` | MV3: `interceptor.js` (MAIN world), `bridge.js` (isolated) |
