# Xplore — Phase 2 handoff

Written 14 September 2026. Read after `HANDOFF.md` and its two supplements. Every number
below was queried from the live stores when this was written.

This phase covers eight workstreams set by the owner (numbered as the owner numbered them):

1. An LLM-powered second level that finds related tweets and links **across Xplore and
   Mneme's full-text library**.
2. A **Compendium Story Tracker** generated automatically over the same period, run on an
   OPML list the app prompts for.
3. The logo and icon in `assets/` used in the UI design.
4. A **desktop icon** that launches the app.
5. An evaluation of turning Xplore into a Chrome/Firefox extension, with pros and cons.
6. A search mode that produces **related links**, LLM-supported with a user-supplied API
   key. It covers the whole X library first, and Mneme later (see 8).
7. **A second integration path across all three apps**, parallel to the OSINT path, which
   Xplore is also part of. It starts with a **new UI for Xplore**, built first around
   requirements a, b and c (below).
8. Xplore and Mneme **stay separate apps with access to each other's data, and Mneme is
   the template Xplore conforms to**. Unifying them is a much later question. More
   generally, look for ways the capabilities that already exist can be cross-matched into
   new outputs.

Requirements a, b and c, restated because the new UI starts from them:

- **a.** An index of bookmarked tweets, each with a local link to its archived text
  where it has a relevant link.
- **b.** A time frame: bookmarked in the last 24 hours, 3 days, 7 days, month, all, or a
  custom date range. Default: all.
- **c.** Full-text and tag search, listed by when bookmarks were added, with an
  alternative view by date created.

---

## 0. Rules that apply to every session

- **No bookmarks or collected content on GitHub without the owner's explicit permission**
  (stated 14 Sep). That covers `xbookmarks.db*`, `x-links*.csv/html`, `unparsed/`, article
  text and transcripts. The exports also hold live gift and access tokens (FT
  `accessToken`, Washington Post `pwapi_token`). `.gitignore` already covers these. Check
  what is staged before every push; a private repo is not permission. The repo is
  **github.com/talws/xplore** (private, empty). The first push is on hold until the
  bookmark-date scan finishes (§1).
- **Mneme and Compendium have their own sessions.** From an Xplore session, read their
  code and data but do not write to them. Integration goes through exports, their APIs,
  or code proposed to *their* sessions. The one sanctioned write path into Mneme is §F,
  and it needs the owner's go-ahead.
- **SQLite discipline:** commit per row and `PRAGMA busy_timeout = 30000` in any new
  pass. Never run two sessions writing `xbookmarks.db` at once.
- **Never kill processes by command line.** Mneme and Xplore both run `python server.py`.
  Find the port owner with `netstat -ano`.
- **Test on a copy.** Use `sqlite3 backup()` to a scratch file and run a second server
  with `--db <copy> --port 8599`.

---

## 1. State at handoff

| | |
|---|---|
| Tweets | 27,424 rows. **Only 23,049 are bookmarks**: 4,328 are tweets quoted by a bookmark, and 47 were probably removed from bookmarks on X (see Bookmark dates). 27,243 classified into a 25-topic taxonomy |
| Tweet languages | en 24,482 · el 1,437 · es 400 · fr 169 |
| Posted | mostly 2022–2026 (2026: 10,420; 2024: 6,520; 2025: 5,129) |
| Threads | 1,841 threads, 22,249 of the authors' own replies |
| Unique outbound links | 5,729 |
| Archived articles | 4,366 ok, **8.93 M words** (includes 112 video transcripts) |
| Not recovered | 1,012 failed · 393 thin · 11 bot walls. Worklist 1,291 pages after dedupe |
| Bookmark dates | **Complete.** 23,049 dated, 13 Nov 2019 – 14 Sep 2026, none before posting. Per year: 2022 1,447 · 2023 3,038 · 2024 5,700 · 2025 4,426 · 2026 8,408 |
| Database | `xbookmarks.db`, ~180 MB |

Taxonomy by size: Gaza Conflict 4,720 · Iran-US Relations 1,723 · US Foreign Policy 1,642 ·
Cultural Commentary 1,514 · War Crimes 1,449 · Israel-Palestine 1,387 · Economic Inequality
1,355 · Historical Analysis 1,145 · AI and Technology 1,125 · Russia-Ukraine War 1,065 ·
Europe Politics 1,049 · Media Bias 1,039 · Latin America Politics 1,023 · Protests and
Activism 1,003 · Human Rights Abuses 982 · Middle East Geopolitics 970 · China Relations 816
· Climate Change 717 · Censorship 592 · Energy Politics 545 · Immigration Policy 454 ·
Surveillance 326 · International Law 322 · Drug Trade 168 · Academic Freedom 112.

### What changed on 13–14 September (supplement 3)

- **Bookmark dates solved.** A timeline entry's `sortIndex` is **Unix milliseconds shifted
  left 20 bits**. It is *not* a tweet-style snowflake; that reading was tried first and put
  13 September bookmarks in January 2025. Established on 740 live bookmarks: the epoch sits
  between two hard bounds (a bookmark cannot predate its tweet, or postdate its capture),
  and that 7-minute window contains Unix 0. Zero violations; the tightest case fits to the
  second.
  - `parse.bookmark_time()` decodes it. `store.upsert_tweets` saves `sort_index` and
    `bookmarked_at` and counts `dated`.
  - `redate_bookmarks.py` recomputes every date from the stored `sort_index`, with no
    re-scrape.
  - `/api/ingest` returns `undated`. `bridge.js` keeps a Deep scan walking while undated
    tweets remain, and stops at X's end-of-timeline marker. `/api/stats` has `undated`, and
    the UI date warning shows only while that count is above zero.
  - **Backfill complete, 14 Sep 14:18 local:** 1,154 pages. `redate_bookmarks.py` dry run
    changes 0 rows.
  - **Why earlier Deep runs stalled after a few pages:** pressing Deep while the hourly
    auto-sync was mid-run *stopped* that sync, and a sync on its own stops after 3 pages
    with nothing new. Fixed: Deep now upgrades a running sync. The panel's status lines,
    a once-a-minute scroll heartbeat (with tab visibility) and any non-200 bookmarks
    response now go to `POST /api/scanlog` and appear in `server-8502.log` as `[panel …]`.
    A stalled run can be read from the log without asking the owner.
  - **Quoted tweets are stored as tweet rows but are not bookmarks.**
    `parse_bookmarks_response` keeps tweets that appear only as a bookmark's quoted tweet,
    and 4,328 such rows exist. The full walk proves it: they were seen on the timeline but
    never carried a `sortIndex`. **Workstream A must not count them as bookmarks.** Treat
    "is a bookmark" as `sort_index IS NOT NULL`, and show them only as the quoted part of
    the bookmark that quotes them. The 47 rows not seen at all were probably removed from
    bookmarks on X; decide with the owner whether to tombstone them.
- **`urls.py`** — one URL cleaner used everywhere. `for_original()` drops tracking junk
  but keeps gift and access codes. `for_archive()` also drops access codes, because
  archive.today matches addresses exactly. `match_key()` is the dedupe key: host without
  `www` + path + cleaned query, with path case preserved. Only `&amp;` escapes are undone,
  never `html.unescape`, which turns `&section=` into `§ion=`. Host-scoped junk tables
  cover nytimes, substack, cnn, docs.google, facebook, springer, ft and wsj. Self-test:
  `python urls.py`.
- **Comment threads cut from article text.** `archive_links.trim_tail()` stops at
  whole-line end markers ("Reuse this content", "Discussion about this post", "Explore
  more on these topics", "Comment guidelines", FT copyright). It needs 150 words before a
  marker counts. An FT page had counted 18,395 words for a 1,331-word article.
  `retrim_articles.py` applied it to stored rows: 361 articles, 140,526 words removed.
  "Jump to comments section" was rejected because the FT prints it in the byline.
- **Shortener resolver fixed.** It matches subdomains and path-style entries
  (`ft.trib.al`, `npr.org/l`), about 40 hosts were added, and it verifies TLS with
  `certifi`: every bit.ly link had failed Windows certificate-chain verification. 332 of 341
  outstanding links resolved. `--retry-failed` now only revisits links that never got past
  the shortener.
- **Capture-on-view** (`capture.py`, `extension/capture.js`, Recover tab) verified live: an
  NYT article saved at 1,328 words. The worklist groups URL variants of the same page (74
  merged) and shows cleaned links.
- **Fresh database fix:** `Store._migrate` adds the resolver columns. A brand-new database
  used to crash `/api/stats`.
- **Archive pass for the newly resolved links finished (14 Sep, ~07:00).**
  - New rows: 151 ok, 35 thin, 96 failed. Its Wayback retry of old non-ok direct rows
    turned 79 thin rows and 12 bot walls into full articles.
  - **The same retry also overwrote 102 thin and 4 ok rows with "failed".** They were
    restored from a pre-run snapshot: 143 rows, 35,148 words.
  - `store()` in `archive_links.py` now never replaces a better row (ok > thin > botscreen >
    failed, then more words). Thin teaser text is now kept, as supplement 2 intended.
  - 75 thin rows from this run have a word count but no text (13,851 words), because the
    old code blanked it. They are recoverable only by a re-fetch.
  - Result: ok 4,512 (9.28 M words), thin 361, failed 1,069. The worklist is
    regenerated: 1,120 pages, 123 recovered.

### Step 0 — done in the previous session (14 Sep)

1. ~~Deep scan~~ — complete, dates verified (above).
2. The git repo was initialised locally on `main`: 35 files of code, docs and assets, with
   no data. The owner approved the file list, then it was pushed to `talws/xplore`.
   **Check with `git log origin/main` that the push happened;** if it did not, show the
   owner `git status` and ask before pushing.
3. Note: `project-preservation` snapshots every repo under `Desktop\Code` daily at 13:30 to
   a `backup/<dirname>` branch on its remote. Now that `xcollector` has a remote, that
   includes `backup/xcollector` on `talws/xplore`. `.gitignore` keeps data out of it too;
   never un-ignore data files.

---

## 2. Facts about the siblings that the design depends on

### Mneme (`Desktop\Code\bookmarktool`, port 7331)

- **Store:** `bookmarks_data.json` (4.6 MB, 12,157 records: `id, url, title, folder, tags,
  status, addedAt, archiveLink, collection`) and `bookmarks_fulltext.json` (**138 MB**,
  `{"version":2, "data": {<id>: {"text": ...}}}`, 4,458 entries). It loads whole into RAM
  (about 7×, so roughly 1 GB) and rewrites in full on every save. **The SQLite/FTS5
  migration is still not done**; it is phase 3 of the OSINT plan.
- **API:** `GET /api/bookmarks?q=&folder=&contentType=&language=&topic=&dateAdded=&collection=`,
  `/api/facets`, `/api/clip`, `/api/clip/status`, and `POST /api/clip/bulk` (≤1000 items,
  honours `collection`; added 4 Sep from an X-collector session and not called by anything).
- **LLM:** `version2/summarizer.py` selects `MNEME_SUMMARIZER` = deepseek | anthropic |
  ollama | none. `facets.py` builds topics in two passes (taxonomy, then assignment).
- **Classifier:** `version2/extractor.py` `classify_page()` / `is_text_centred()`, shared
  with Compendium by decision.
- Collections in data: `archive` 7,778, unset 4,376, `active` 3.

### Compendium II (`Desktop\Code\Compendium II\app`, a Tkinter desktop app)

- **Story Tracker exists and is rich:** `core/story_tracker.py`, `story_seeder.py`,
  `story_pipeline.py`, `story_convergence.py`, and `ui/story_tracker_tab.py`.
  - A `Story` has keywords with weights and auto-hit terms, `logic` ANY/ALL/CUSTOM,
    `lookback_hours`, `opml_files`, `relevance_threshold`, `seed_context` (an LLM brief),
    `seed_article_urls`, `linked_story_ids` (convergence), `social_accounts` and
    `languages`.
  - Stories persist to `~/.newsdigest/stories.json`, which **does not exist now**; the
    config wipe of 2 Sep took them. Output is per-story HTML under `archive_dir`.
  - `seed_story(sources, story, client, model)` writes the brief and keywords from article
    URLs or text, which is exactly what bookmarks can supply.
- **OPML corpus:** `Compendium II\corpus\opml\current` (32 files), `archive` (34) and
  `bookmarks` (4). The app reads the absolute paths in `~/.newsdigest/config.json`
  (`last_opml_files`, `opml_metadata_map`); the app folder holds no OPML.
- **Feed history is thin:** `feeds/article_cache.db` holds 4,373 articles from a single
  day, 10 June 2026, and 20 runs between late May and 10 June. RSS only exposes recent
  items. **So a story tracker cannot be run backwards over months of bookmarks** (see §D).
- **Already expects Xplore.** `ROADMAP.md` S2: "Twitter/X bookmarks — ingest X bookmark JSON
  export; BM25 index over tweet text + linked article text" (`twitter_bookmarks.json`).
  `core/context_builder.py` `BookmarkSource` already does BM25 over Mneme's fulltext JSON.
- **Social accounts:** `ui/social_accounts.py` turns X handles into RSSHub feed URLs. The
  public RSSHub Twitter routes are unreliable; verify before relying on them.
- **LLM config** is multi-provider (`ai_provider`, fast/quality models per provider) in
  `config.json`, currently DeepSeek. `data/ebook_index.db` indexes 1,033 e-books.

### The OSINT path (for §G)

`Compendium II\app\Full OSINT project.docx` and `Desktop\Code\OSINT 1.docx` set out:

- a shared corpus (SQLite + FTS5, immutable captures);
- an **entity layer** (`entities`, `mentions` with `source_type` values including
  `x_tweet`, `co_occurrences`, `watchlists`, `alerts`);
- batch now, near-live later;
- individual use first, **team use eventually**.

The owner's stated gap: "I can't correlate actors and orgs between digests, and between
digests and bookmarks or books."

### Measured overlap between the libraries

Xplore's 5,368 distinct link targets and Mneme's 12,073 distinct pages share **76 pages**,
matched on `urls.match_key`. **URL matching will not connect these libraries; relatedness
has to be semantic.** The 76 are still worth a cheap first output (§F-1).

---

## 3. Workstreams

Recommended order: **A + B → C1 → C2 → D → F**. E is a document and can be done at any time.

### A. New UI, branded (items 3, 7, a/b/c)

A new UI, not a patch to `app.html`. Keep the old one at `/classic` until the new one has
parity.

**Scope of the first cut (a, b, c, all already served by `/api/find`):**

- **Library (a):** the tweet index. Each card shows author, text, quoted tweet, thread
  badge, tags and topic, and every link with its archive state. Archived links open
  `/article?url=` locally; unarchived ones show "recover" and hand off to the Recover view.
- **Time frame (b):** 24 h / 3 d / 7 d / month / all / custom range. Default all. The
  basis follows the sort (bookmarked or posted). Show the undated count while
  `/api/stats.undated > 0`, so a "month" filter never silently omits undated tweets.
- **Search (c):** one box over tweet text, quoted text, thread replies and archived article
  text; tag and topic chips; sort by bookmarked (default) or posted. Explain matches: say
  when a hit came only from the linked article, and highlight the terms.
- **Views:** Library/Search, Recover, and a **Related** mode toggle, hidden until C1 lands
  (§C).

**Mneme is the template (owner decision, 14 Sep).** The two apps are not merged, but a
person moving between them should find the same structure and vocabulary.

- **Mneme's layout:** a header with logo, one search box and live stats; a left facet
  sidebar (250 px); the results list; and a right detail panel (360 px). Mneme's facets are
  folders, link status, indexed, content type, language, date added, topics and import.
- **Xplore's equivalents:** time frame, topic, tags, author, language, archive state and
  content type in the sidebar. The detail panel holds the tweet, its thread and the
  archived article.
- **Same names for the same concepts,** in the UI and the API: `addedAt`, `contentType`,
  `language`, `topic`, `tags`, `collection`, `status`, `summary`, `author`,
  `datePublished`, `publication`.
- **Mneme-shaped read endpoints.** Xplore offers `GET /api/bookmarks?q=&contentType=&language=&topic=&dateAdded=&collection=`
  returning `{count, bookmarks}`, and `GET /api/facets` returning
  `{topics, taxonomy, dateAdded, total}`, alongside its own `/api/find`. Mneme can then
  read Xplore with the client code it already has.
- **Colours are Xplore's** (below), not Mneme's purple.

**Brand.** The assets are `assets/xplore-logo.png` (976×291, RGBA) and
`assets/xplore-icon.png` (548×571, **RGB with a white background**, not a transparent
rounded square).

- Palette from the pixels: navy `#00186c` (wordmark), primary blue `#0078f0` / `#006cf0`,
  bright `#009cfc`, deep `#003cb4` / `#002478` (the icon's gradient).
- Make a transparent, square-cropped icon master before generating anything else. Then
  produce the favicon, `.ico` (16–256), and extension `icon-16/48/128.png` from it.
- Replace the placeholder extension icons. Rename the extension "Xplore" in the manifest
  and panel. Rename the server banner and `<title>`.
- The logo is for light backgrounds. Make a dark-mode variant (white "plore"), or use the
  icon mark in the header when the theme is dark.

**Tech.** Plain HTML/CSS/JS served by Flask, with no build step and no CDN: it is a local
app and must work offline. ES modules are fine. The old `app.html` is a single file; the new
UI can be `ui/index.html` + `ui/*.js` + `ui/*.css`.

**Gate:** a, b and c work at parity with `app.html` on the live data. Branded throughout,
dark and light modes, and it works at a narrow width.

### B. Desktop launcher (item 4)

Cheapest first; each step is useful on its own.

1. **App-mode shortcut (about 1 hour).** `xplore.pyw`:
   - if port 8502 is not answering `/api/ping`, start the server hidden, using the same
     approach as `start-collector.vbs`;
   - wait for bind, which takes **40–60 s** because the server counts the database first;
   - open `msedge --app=http://localhost:8502` (or Chrome `--app=`).
   
   A desktop shortcut runs `pythonw xplore.pyw` with `assets/xplore.ico`. It gets its own
   window with no browser chrome, and Edge is always present on Windows.
2. **Standalone branded window — the owner's choice (14 Sep).** Skip step 1 as the end state
   and build this.
   - `pywebview` on Edge WebView2 (built into Windows 11) wraps `http://localhost:8502` in
     an "Xplore" window with the Xplore icon in the title bar and taskbar.
   - While the server binds, show the Xplore logo on a splash instead of a blank page.
   - Native file dialogs come with it; D's OPML picker uses them.
   - The desktop shortcut uses `assets/xplore.ico`. The server is unchanged.
   - Installing `pywebview` is a PyPI download, so ask first.
3. Keep the login auto-start: capture depends on the server running. Keep
   `sys.stdout.reconfigure(encoding="utf-8")` in anything that prints; cp1253 killed the
   server before.
4. Create the shortcut with `WScript.Shell` `CreateShortcut` from PowerShell. Single
   instance: if the server is up, only open the window.

**Gate:** double-clicking the icon from a cold start shows the UI with no console window,
and a second double-click does not start a second server.

### C. Related-items engine (items 1 and 6)

Build it in two levels. The first level is useful without any key and is also what the
LLM level re-ranks.

**C1 — level 1, no LLM, no key**

For a tweet, an article, or a free query, return candidates scored from signals that exist
today:

- FTS5/BM25 over tweets and articles (already in `store.find`);
- the same link or the same `match_key`;
- the same author;
- the same topic, or a shared hashtag or user tag;
- bookmarked close together in time (now possible, with exact bookmark dates);
- thread membership;
- quoted-tweet links.

Return them as grouped **related links** (articles) and **related tweets**, each with the
signals that fired. `GET /api/related?tweet=|url=|q=&scope=x|x+mneme&limit=`.

**C2 — level 2, LLM-supported**

1. **Owner decision (14 Sep): a paid API with the owner's key, DeepSeek for now, and the
   provider must be changeable.** DeepSeek's official docs (checked 14 Sep) list chat models
   only (`deepseek-flash`, `deepseek-v4-pro`; legacy names such as `deepseek-chat` still
   route) and **no embeddings endpoint**. So with DeepSeek:
   - **Recall** comes from C1 plus **LLM query expansion**. DeepSeek turns a query or a tweet
     into keywords, entities and their Greek/English equivalents, and those run through
     FTS5. Keyword recall alone misses paraphrase and cross-language matches, and 1,437
     tweets plus much of Mneme are Greek.
   - **Precision** comes from the re-rank (step 2).
   - **An optional second slot for an embeddings provider** (OpenAI, Voyage, Gemini) plugs
     in later if the owner adds such a key. Design the provider interface with separate
     `chat` and `embed` capabilities from the start. Store vectors in SQLite (`sqlite-vec`,
     or a float32 BLOB plus a numpy scan).
2. **LLM re-rank and explain.** Send the top 30–50 candidates (title plus a snippet, never
   whole articles) and ask for relevance and a one-line "why related". Cache results by
   (item, candidate, model).
3. **Query expansion** for Search→Related: turn a query into keywords and entities, then
   run C1 + embeddings.
4. **API key handling (user-inserted):**
   - a Settings panel with provider, key and model, reusing Compendium's provider list and
     its fast/quality split;
   - keys stored **outside the repo and the database**: `%USERPROFILE%\.xplore\config.json`,
     or Windows Credential Manager via `keyring`. `DEEPSEEK_API_KEY` from the environment
     as a fallback;
   - a "test key" button, cost estimate before any batch job, and a hard per-run budget;
   - never write keys into logs, exports or the page.

**UI (item 6):** a toggle between **Search** and **Related**. In Related mode a query or a
selected tweet returns clusters: related links first, then related tweets, each with its
"why". A scope switch reads **X library** or **X + Mneme**. Mneme stays disabled until C3 is
in. Each result card gets a "Related" drawer.

**C3 — scope to Mneme (federation, read-only)**

- Build a **sidecar index** in Xplore (`mneme_index.db`) from Mneme's two JSON files. Read
  them only; never write Mneme's files. Rebuild when their mtime changes, keyed by Mneme
  id + text hash so only changed entries are re-embedded.
- Loading 138 MB of JSON costs about 1 GB of RAM, **once per rebuild, offline**, never per
  query. Use a streaming parser (`ijson`) if memory is tight.
- When Mneme moves to SQLite, swap the adapter; the sidecar format does not change.
- Do not depend on Mneme's server running. `/api/bookmarks?q=` is keyword-only and would
  couple uptime.

**Evaluate before tuning.** Make a gold set with the owner: about 30 seed tweets or
queries, each with the relevant items marked. Report precision@10 for C1 alone, C1 plus
embeddings, and with re-rank. Without this, "related" cannot be judged.

**Gates:**

- C1: `/api/related` answers in under 1 s on the live data, with signals shown.
- C2: the gold set shows embeddings or re-rank beating C1, the key flow works end to end,
  and the budget is enforced.
- C3: Mneme results appear with a source badge and open Mneme's own view.

### D. Story Tracker generated from bookmarks (item 2)

**What it means concretely.** Xplore proposes stories from what the owner bookmarked in a
period. Examples: the top clusters of the last 7 or 30 days from C1/C2, or a hand-picked set
of tweets. It then **prompts for an OPML list** and creates Compendium Stories to track them
across that list.

**Periods — two different things, both measured in the code (14 Sep):**

- **Seed period:** which bookmarks define the story. Any Xplore time frame: 24 h, 3 d, 7 d,
  month, a custom range, or all. That reaches back to the oldest bookmark once the date
  backfill is complete. There is no limit here.
- **Tracking window:** which news gets searched.
  - **Backward:** Compendium's `lookback_hours`, **1 to 720 hours (up to 30 days)** by its UI
    spinbox, default 72. `fetch_all_feeds` drops items older than that cutoff. It is also
    capped by what each feed still lists, often only the newest 10–50 items, which is days
    for busy outlets. Story runs do **not** read `article_cache.db`, so no older history is
    used.
  - **Forward:** unlimited. The schedule is manual, daily, twice daily or weekly, at a set
    hour. Every run is saved as `run_<id>.json` in the story's archive and compared with the
    previous run, which yields the "new development" note. Coverage builds up from the
    first run onwards.
  - **Consequence:** a story can be seeded from bookmarks of any age, but its news coverage
    starts at most 30 days before its first run. Say this in the UI when the seed period is
    older. Historical backfill beyond 30 days (GDELT, Wayback CDX by domain) is research,
    not this phase.

**Owner decision on search depth and tracking (14 Sep, second answer):**

- **The search for related material ignores the seed period.** The seed period only picks
  which bookmarks start the story. Finding related items then reaches:
  - **Xplore:** every bookmark we hold, back to the oldest;
  - **Mneme:** everything it has, whatever the date written;
  - **Compendium:** its archive, as far back as that goes. It has almost no history today
    (one day in June), but it grows once Compendium runs again.
- **Needed for Compendium's part:** today a story run only reads live feeds within
  `lookback_hours`. Searching Compendium's archive means querying `feeds/article_cache.db`
  (every digest run's articles) and the saved story runs (`run_*.json`) as well. That is a
  change to propose to the Compendium session, not something to write from here.
- **Tracking forward is an editorial decision, never automatic.** A search, a related-links
  view or a story proposal must not create a scheduled Story on its own. Xplore proposes;
  the owner explicitly chooses **Track as story** and sets the schedule. Until then nothing
  runs on a timer.

**Setup flow:**

1. **Pick the seed.** In Xplore: a period, a topic, a Related cluster, or hand-picked
   tweets. Choose **Track as story**.
2. **Xplore proposes** a name, the seed articles (their archived text, so paywalled pages
   are not re-fetched) and the seed period.
3. **Seed with the LLM.** Compendium's `seed_story`, on the owner's DeepSeek key, writes
   the brief, weighted keywords and auto-hit terms. The owner edits them.
4. **Choose sources — flexible (owner decision, 14 Sep):**
   - any number of OPML files, from the corpus folders or anywhere on disk;
   - Compendium's saved collections;
   - extra feed URLs;
   - X accounts, for example the authors behind the seed bookmarks.
   
   Sources are saved per story and can be changed later.
5. **Settings:** look-back (default = seed period, capped at 720 h), schedule and hour,
   relevance threshold 1–5, languages (English/Greek).
6. **Run.** The first run happens immediately; the HTML opens from Xplore. Later runs flag
   new developments and link back to new bookmarks that match the story.

**Recommended shape: Compendium owns its format, Xplore calls it.**

1. **Cluster → story proposal in Xplore:**
   - name;
   - seed articles: the bookmarked article URLs, or their archived text passed directly,
     which avoids re-fetching paywalled pages;
   - the period, which sets `lookback_hours`;
   - the languages seen.
2. **Prompt for sources, flexibly** (setup flow step 4 above): several OPMLs from any
   folder, Compendium collections, extra feed URLs and X accounts. Offer
   `Compendium II\corpus\opml\current` by name first, with a native file picker for the
   rest, and remember the last selection.
3. **Bridge CLI inside Compendium** (proposed to the Compendium session, not written from
   here): `compendium_story_cli.py create --json <proposal>` calls `story_seeder.seed_story`
   (brief plus weighted keywords) and saves the Story. `run --id` calls
   `story_pipeline.run_story` headless. It runs in Compendium's own `app\venv`. Xplore
   invokes it as a subprocess and links the resulting HTML.
   - **Verify headless import first:** `story_pipeline` imports `ui.social_accounts`, which
     imports `tkinter`.
   - **Do not write `~/.newsdigest/stories.json` from Xplore;** a running Compendium UI
     would overwrite it.
4. **Convergence back to Xplore.** Compendium's matched articles for a story are linked
   from the cluster that produced it, and new bookmarks matching an active story are
   flagged. That makes it a loop, not a one-off export.

**Gate:** from a 7-day cluster, one click proposes a story, the OPML prompt appears, the CLI
seeds and runs it, and the HTML opens from Xplore.

### E. Evaluation: Xplore as a Chrome/Firefox extension (item 5)

**Recommendation: keep Xplore a desktop app with a thin capture extension, as now.** Revisit
a "lite" extension only once Mneme's stage-1 extension exists, so the two can share an
IndexedDB and classifier core.

| | For | Against |
|---|---|---|
| Distribution | Store listing, one-click install, no Python, no launcher | Store review. Intercepting x.com's private GraphQL in the page's main world is exactly what reviewers question. X's terms prohibit automated collection, so a public listing risks takedown; the owner's own no-lockout rule applies doubly |
| Capture | Already an MV3 extension; sits where the X session is | X changes a field and the fix waits days for store review; today it is a server edit, live in seconds |
| Storage and search | IndexedDB holds 27 k tweets + 9 M words (~200 MB); SQLite-wasm with FTS5 on OPFS gives real full-text search | A migration of the whole store; quota eviction risk unless persistent storage is granted; no shared file for Mneme or Compendium to read |
| Archiving | Could fetch with host permissions | `<all_urls>` is heavily scrutinised; MV3 service workers stop after ~30 s, so Wayback pacing (8 s, bursts) becomes alarm-driven chunks; **transcripts (yt-dlp) are impossible in a browser**; archive.today capture already works via `capture.js` |
| LLM | User key in `storage.local`; direct API calls | Keys in extension storage are readable by anyone with the profile; local embedding models are heavy in wasm |
| Firefox | Most of the MV3 code is portable | Verify MAIN-world content-script support in current Firefox (older builds needed `wrappedJSObject`); `sidebar_action` vs Chrome `sidePanel` are different shells |
| Integration (item 7) | — | The three-app integration and the OSINT corpus assume local stores and processes; an extension-only Xplore sits outside both |

### F. Mutual data access with Mneme, and cross-matching (item 8)

**Owner decision (14 Sep):** "Xplore and Mneme might not be the same app, but should have
access to each other's data — Mneme being the template to which Xplore should conform.
We'll see much later if we unify them." That settles the 4 Sep "separate stores" rule
and the earlier "separate Mneme collection" idea:

- **No copying between stores.** Each app keeps its own data. Nothing is published into
  Mneme's JSON, which also sidesteps its capacity limit: ~9 M more words would have pushed
  its RAM use to about 1.4 GB.
- **Each reads the other.**
  - Mneme reads Xplore through the Mneme-shaped `GET /api/bookmarks` and `/api/facets`
    (§A), under a collection name such as `x` in its collection switcher. That client
    change is proposed to the Mneme session.
  - Xplore reads Mneme through the read-only sidecar index (§C3).
- **Conform to Mneme:** record field names, facet vocabulary, UI structure (§A), and
  Mneme's classifier as the shared content-type authority (idea 3 below).
- **Unification later.** Keep the IDs and field names aligned, so merging the stores, if it
  ever happens, is a data migration rather than a redesign.

**Cross-matching ideas, cheapest first:**

1. **The 76 shared pages.** Output a "double-signal" list (saved in both places), and fill
   gaps both ways: 10 are unrecovered in Xplore; Mneme's status for most is `unknown`.
2. **Recovery across libraries.** Before sending the owner to archive.today, look the URL up
   in Mneme's fulltext, and the reverse. Small today (76), but free.
3. **Shared classifier.** Run Mneme's `classify_page()` / `is_text_centred()` on Xplore
   articles to catch non-articles that pass the word floor. Give Mneme Xplore's paywall
   teaser rule, `trim_tail()` and `urls.py`. Keep one copy of each rule.
4. **One taxonomy.** Assign Xplore's 25 topics to Mneme entries, or merge the two
   taxonomies, so topic facets mean the same thing in both.
5. **Export for Compendium S2.** Write `twitter_bookmarks.json` in the shape `ROADMAP.md` S2
   expects (tweet text + linked article text). Compendium's BM25 context pipeline already
   consumes the Mneme equivalent, so this is small and immediately useful in digests.
6. **Source profile.** Join Xplore link domains with Compendium's source metadata (country,
   type, leaning from the OPML/XLSX weights). Output: which outlets and leanings the
   bookmarks draw on, and blind spots against the OPML corpus.
7. **Suggested follows.** Most-bookmarked X authors become suggested Compendium
   `social_accounts`. Check RSSHub reliability first.
8. **Reading timeline.** Exact bookmark dates plus article publication dates
   (`enrich_links.py`) give the lag between publication and bookmark, "what I was reading
   when", and topic surges per week. The surges seed D.
9. **Entities (joins the OSINT path).** Extract people and organisations from tweets and
   archived articles into the OSINT `mentions` schema with `source_type='x_tweet'` /
   `'x_article'`. The owner's stated gap is exactly actor and org correlation across
   digests, bookmarks and books.
10. **Change detection.** Where the same page is archived in two places or at two times,
    diff the texts.

### G. The second integration path (item 7)

Two paths run in parallel and must not diverge:

- **OSINT path:** a shared corpus plus entity layer (`intel.db`), long-term, team-capable.
- **This path:** federation now, meaning three apps that stay separate and talk through
  **stable contracts**.

Make Xplore's side of the contract explicit, so the OSINT path can later read the same
thing:

- **Identifiers:** tweet id; `urls.match_key` for pages; article text hash (sha256 of the
  stored text).
- **Provenance on every record:** source app, capture method (direct / wayback / archiveph /
  view / transcript), captured_at, bookmarked_at, and the tweet it came from.
- **Read APIs:** `/api/find`, `/api/related`, `/article`, `/api/stats`, plus a versioned
  export `xplore_export.json` (schema v1, documented in the repo).
- **Batch now, live later:** every pass idempotent and timestamped, as the OSINT notes
  require.
- **Team-ready choices** that cost nothing now: no user-specific assumptions baked into
  IDs; tags carry `added_by` (null for single-user).

---

## 4. Decisions — answered by the owner, 14 September

1. **Mneme and Xplore:** separate apps with access to each other's data; **Mneme is the
   template Xplore conforms to**; unification much later (§A, §F).
2. **LLM:** a paid API with the owner's key. **DeepSeek for now, and the provider must be
   changeable.** DeepSeek has no embeddings, so related links use query expansion plus
   re-rank (§C2).
3. **Story Tracker periods:** explained in §D. Seed from any period, tracking up to 30 days
   back and unlimited forward. Owner to confirm this is acceptable after reading §D.
4. **Sources are flexible:** several OPMLs from anywhere, Compendium collections, extra
   feeds and X accounts, set per story (§D).
5. **Launcher:** a standalone window branded with the Xplore logo and icon, opened from a
   desktop icon (§B step 2).
6. **Priority:** related links (C) before the Story Tracker (D).

## 5. Traps carried forward (details in `HANDOFF.md`)

- cp1253 stdout kills the server at startup, so keep the UTF-8 reconfigure.
- The server takes 40–60 s to bind after a restart.
- `Get-NetTCPConnection` lies on this machine; use `netstat -ano`.
- PowerShell `*>` writes UTF-16.
- **After reloading the extension, reload the x.com tab.** The old content script is
  orphaned and every message fails.
- archive.org has rate-limited this machine; never lower Wayback pacing.
- A shorter capture must never overwrite a longer one.
- Removal is a tombstone, not a delete.
- `html.unescape` corrupts URLs (`&sect` → `§`).
- archive.today is exact-match on the address and CAPTCHA-gated even for a person.
- Mneme: never `git clean -fdx` in `bookmarktool`, and never put a destructive command in a
  runnable code block.
