"""
store.py — SQLite store for the X bookmark collector.

Deliberately its own database, separate from Mneme. The two libraries are meant
to be searched together one day, but they are not the same collection and do
not share a store.

SQLite rather than a JSON file because the sibling project demonstrates exactly
what that costs: Mneme's fulltext file is 127 MB, rewritten in full on every
single save after being read back as a shrink-guard. At 24,000 tweets that is
terabytes of I/O. Here a write touches one row.

Schema notes:

- The tweet id is the primary key, so a re-scrape is idempotent by construction
  and no separate "already seen" bookkeeping is needed.
- `first_seen` / `last_seen` let an incremental run tell "new to us" from
  "seen again", which is what the collector's stop condition keys on.
- Media and links are separate tables rather than JSON blobs so that "every
  article a tweet pointed at" is a plain query. That is the whole point of the
  outbound-link work.
- FTS5 gives search on day one. It is kept in sync by triggers so callers can
  never forget to update it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import datetime
import threading
import time
import unicodedata
from typing import Iterable
from urllib.parse import quote, urlsplit

SCHEMA_VERSION = 1


# ── Shared vocabulary: find(), facets() and the Mneme-shaped read API ────────

# Articles still missing, i.e. the Recover worklist. capture.py imports this,
# so "unrecovered" means the same thing in the worklist and in the facets.
WORKLIST_STATUSES = ("failed", "thin", "botscreen")

# What a row is. Only a timeline entry carries a sortIndex, so that is the test
# for "is a bookmark". A row without one that some bookmark quotes is stored for
# the quote. A row without one that nothing quotes was not seen on the last full
# walk of the timeline, and was probably removed from bookmarks on X.
SCOPES = {
    "bookmarks": "t.sort_index IS NOT NULL",
    # Quoted by any row, not just by a bookmark: a quote of a quote is stored
    # too (11 on 14 Sep). One unquoted row was a thread part.
    "undated": ("t.sort_index IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM tweets b WHERE b.quoted_id = t.id) "
                "AND NOT EXISTS (SELECT 1 FROM thread_parts p WHERE p.id = t.id)"),
    "all": "1 = 1",
}

# The archived-article store. Created by the Store as well as by
# archive_links.py (which imports this), because find(), facets() and stats()
# read it: a brand-new database must not fail before the first archive pass.
ARTICLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url         TEXT PRIMARY KEY,      -- the resolved destination
    title       TEXT,
    text        TEXT,
    word_count  INTEGER,
    source      TEXT,                  -- direct | wayback | archiveph | view | transcript
    snap_url    TEXT,                  -- where the copy actually came from
    http_status TEXT,
    status      TEXT,                  -- ok | botscreen | thin | failed | skipped
    fetched_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, text, content='articles', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, text) VALUES (new.rowid, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, text)
    VALUES ('delete', old.rowid, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, text)
    VALUES ('delete', old.rowid, old.title, old.text);
    INSERT INTO articles_fts(rowid, title, text) VALUES (new.rowid, new.title, new.text);
END;
"""

_LINKED = ("FROM links l LEFT JOIN articles a "
           "ON a.url = COALESCE(l.resolved_url, l.expanded_url) WHERE l.tweet_id = t.id")
_X_HOST = "(" + " OR ".join(
    f"COALESCE(l.resolved_url, l.expanded_url) LIKE '%://{h}/%'"
    for h in ("x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com")) + ")"

# Mneme's `contentType`, for a bookmark. Overlapping: a tweet with a photo and
# an article link counts under both. primary_type() picks one for display.
CONTENT_TYPES = {
    "article": (f"EXISTS (SELECT 1 {_LINKED} AND COALESCE(a.source, '') <> 'transcript' "
                f"AND COALESCE(a.status, '') NOT IN ('skipped', 'video') AND NOT {_X_HOST})"),
    "video": ("(EXISTS (SELECT 1 FROM media m WHERE m.tweet_id = t.id "
              "AND m.kind IN ('video', 'animated_gif')) "
              f"OR EXISTS (SELECT 1 {_LINKED} AND (a.source = 'transcript' OR a.status = 'video')))"),
    "image": "EXISTS (SELECT 1 FROM media m WHERE m.tweet_id = t.id AND m.kind = 'photo')",
    "text": ("NOT EXISTS (SELECT 1 FROM links l WHERE l.tweet_id = t.id) "
             "AND NOT EXISTS (SELECT 1 FROM media m WHERE m.tweet_id = t.id)"),
}

_ARCHIVED = f"EXISTS (SELECT 1 {_LINKED} AND a.status = 'ok')"
_WL = ", ".join(f"'{s}'" for s in WORKLIST_STATUSES)

# Can the linked text be read here? Mneme's `status`, for a bookmark's links.
STATUSES = {
    "archived": _ARCHIVED,
    "unrecovered": f"NOT {_ARCHIVED} AND EXISTS (SELECT 1 {_LINKED} AND a.status IN ({_WL}))",
    "pending": f"NOT {_ARCHIVED} AND EXISTS (SELECT 1 {_LINKED} AND a.url IS NULL AND NOT {_X_HOST})",
    "nolinks": "NOT EXISTS (SELECT 1 FROM links l WHERE l.tweet_id = t.id)",
}

ARTICLE_HITS = ("SELECT a.url FROM articles_fts af JOIN articles a ON a.rowid = af.rowid "
                "WHERE articles_fts MATCH :q")

LIST_COLS = """t.id, t.url, t.author_handle, t.author_name, t.created_at, t.text,
       t.quoted_text, t.quoted_id, t.quoted_handle, t.truncated, t.is_thread,
       t.thread_done, t.lang, t.sort_index IS NOT NULL AS is_bookmark"""

# Mneme's date facet vocabulary (version2/facets.py date_bucket).
DATE_ORDER = ["Today", "Past week", "Past month", "Past 3 months", "Past year"]
UNCATEGORISED = "Uncategorised"


def bucket_sql(col: str) -> str:
    """Mneme's date_bucket() as a SQL expression over an ISO timestamp column."""
    days = f"CAST(julianday('now') - julianday({col}) AS INTEGER)"
    return (f"CASE WHEN {col} IS NULL THEN 'Unknown' "
            f"WHEN {days} <= 0 THEN 'Today' WHEN {days} <= 7 THEN 'Past week' "
            f"WHEN {days} <= 30 THEN 'Past month' WHEN {days} <= 90 THEN 'Past 3 months' "
            f"WHEN {days} <= 365 THEN 'Past year' ELSE substr({col}, 1, 4) END")


def host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")


def link_kind(url: str, source: str | None, status: str | None) -> str:
    if source == "transcript" or status == "video":
        return "video"
    if host_of(url) in ("x.com", "twitter.com", "mobile.twitter.com"):
        return "tweet"
    if status == "skipped":
        return "other"
    return "article"


def primary_type(r: dict) -> str:
    kinds = {l["kind"] for l in r.get("links") or []}
    media = r.get("media") or {}
    if "article" in kinds:
        return "article"
    if "video" in kinds or media.get("video") or media.get("animated_gif"):
        return "video"
    if media.get("photo"):
        return "image"
    return "text"


def fold(s: str) -> str:
    """Case- and accent-free, for explaining which part of a result matched.

    More lenient than the index: FTS5's unicode61 tokenizer folds case, but
    strips diacritics from Latin script only, so to the index 'Τσίπρας' and
    'τσιπρας' are different words (measured 14 Sep: 21 hits against 0)."""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c)).casefold().replace("ς", "σ")


def query_terms(q: str) -> list[str]:
    q = re.sub(r"\b(AND|OR|NOT|NEAR)\b", " ", q or "")
    return [w for w in re.findall(r"\w+", q) if len(w) > 1]


def as_mneme(r: dict, base_url: str = "") -> dict:
    """A find() result as a Mneme bookmark record (bookmarks.py `_merged`)."""
    first = (r.get("opener") or r.get("text") or "").strip().split("\n", 1)[0]
    handle = r.get("author_handle") or ""
    name = r.get("author_name") or handle
    archived = [l for l in r.get("links") or [] if l.get("archive_status") == "ok"]
    state = "archived" if archived else "live"
    return {
        "id": r["id"],
        "url": r["url"],
        "title": (f"@{handle}: " if handle else "") + first[:140],
        "description": r.get("text") or "",
        "folder": "",
        "tags": r.get("tags") or [],
        "status": state,
        "linkStatus": state,
        "addedAt": r.get("added_at") or "",
        "datePublished": r.get("created_at") or "",
        "author": [name] if name else [],
        "authorHandle": handle,
        "publication": "X",
        "language": r.get("lang") or "",
        "contentType": r.get("contentType") or "",
        "topic": r.get("topic") or "",
        "collection": "x",
        "indexed": bool(archived),
        "archiveLink": (f"{base_url}/article?url={quote(archived[0]['url'], safe='')}"
                        if archived else ""),
        "summary": "",
        "links": r.get("links") or [],
        "quoted": r.get("quoted"),
    }


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc)\
                   .isoformat(timespec="seconds").replace("+00:00", "Z")


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tweets (
    id             TEXT PRIMARY KEY,   -- X's own tweet id; the natural key
    url            TEXT NOT NULL,      -- canonical permalink
    author_handle  TEXT,
    author_name    TEXT,
    author_id      TEXT,
    created_at     TEXT,               -- ISO8601 UTC, when the tweet was posted
    text           TEXT,               -- full text; note_tweet preferred over legacy
    lang           TEXT,

    -- Capture quality. `truncated` means we stored the 280-char timeline form
    -- and the real text is longer; a later expansion pass clears it.
    truncated      INTEGER NOT NULL DEFAULT 0,
    is_thread      INTEGER NOT NULL DEFAULT 0,
    thread_done    INTEGER NOT NULL DEFAULT 0,

    quoted_id      TEXT,
    quoted_handle  TEXT,
    quoted_text    TEXT,

    reply_count    INTEGER,
    retweet_count  INTEGER,
    like_count     INTEGER,

    source         TEXT,               -- 'graphql' | 'dom'
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    raw            TEXT                -- original tweet object, for reparsing later
);

CREATE INDEX IF NOT EXISTS idx_tweets_author  ON tweets(author_handle);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at);
CREATE INDEX IF NOT EXISTS idx_tweets_trunc   ON tweets(truncated) WHERE truncated = 1;
CREATE INDEX IF NOT EXISTS idx_tweets_thread  ON tweets(is_thread) WHERE is_thread = 1;

CREATE TABLE IF NOT EXISTS media (
    tweet_id TEXT NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
    kind     TEXT,                     -- photo | video | animated_gif
    url      TEXT,                     -- reference only; binaries are never stored
    alt      TEXT,                     -- author-supplied alt text: real searchable content
    PRIMARY KEY (tweet_id, url)
);

CREATE TABLE IF NOT EXISTS links (
    tweet_id     TEXT NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
    url          TEXT,                 -- the t.co form as it appeared
    expanded_url TEXT,                 -- the real destination
    display_url  TEXT,
    title        TEXT,                 -- card title when X supplied one
    -- Archive state for the three-tier fetch. NULL = not attempted yet.
    archived_at  TEXT,
    archive_url  TEXT,
    archive_status TEXT,
    PRIMARY KEY (tweet_id, expanded_url)
);

CREATE INDEX IF NOT EXISTS idx_links_pending ON links(archived_at) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_links_target  ON links(expanded_url);

CREATE TABLE IF NOT EXISTS hashtags (
    tweet_id TEXT NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    PRIMARY KEY (tweet_id, tag)
);

-- Topics are assigned by an LLM pass but must stay hand-editable, so the
-- machine's answer and the owner's override are kept in different columns and
-- the override always wins.
CREATE TABLE IF NOT EXISTS topics (
    tweet_id   TEXT PRIMARY KEY REFERENCES tweets(id) ON DELETE CASCADE,
    topic      TEXT,
    topic_user TEXT,
    assigned_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    ended_at   TEXT,
    seen       INTEGER DEFAULT 0,
    added      INTEGER DEFAULT 0,
    note       TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS tweets_fts USING fts5(
    text, author_handle, author_name, quoted_text,
    content='tweets', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS tweets_ai AFTER INSERT ON tweets BEGIN
    INSERT INTO tweets_fts(rowid, text, author_handle, author_name, quoted_text)
    VALUES (new.rowid, new.text, new.author_handle, new.author_name, new.quoted_text);
END;
CREATE TRIGGER IF NOT EXISTS tweets_ad AFTER DELETE ON tweets BEGIN
    INSERT INTO tweets_fts(tweets_fts, rowid, text, author_handle, author_name, quoted_text)
    VALUES ('delete', old.rowid, old.text, old.author_handle, old.author_name, old.quoted_text);
END;
CREATE TRIGGER IF NOT EXISTS tweets_au AFTER UPDATE ON tweets BEGIN
    INSERT INTO tweets_fts(tweets_fts, rowid, text, author_handle, author_name, quoted_text)
    VALUES ('delete', old.rowid, old.text, old.author_handle, old.author_name, old.quoted_text);
    INSERT INTO tweets_fts(rowid, text, author_handle, author_name, quoted_text)
    VALUES (new.rowid, new.text, new.author_handle, new.author_name, new.quoted_text);
END;
"""


class Store:
    # What counts as a thread worth fetching, in one place so the queue and its
    # size can never disagree.
    #
    # MAX_THREAD_ATTEMPTS: two goes, then give up. Forward progress matters more
    # than completeness -- a tweet that will not load must not be able to stall
    # the whole pass.
    #
    # reply_count < 5000 excludes viral tweets. Their pages carry tens of
    # thousands of replies, take for ever to render, and froze the browser on
    # the first run; a viral tweet is also rarely a thread.
    THREAD_CANDIDATE = """
        deleted = 0 AND thread_fetched_at IS NULL AND thread_attempts < 2
        AND COALESCE(reply_count, 0) < 5000
        AND (is_thread = 1
             OR text LIKE '%' || char(129525) || '%'
             OR lower(text) LIKE '%a thread%'
             OR lower(text) LIKE '%thread:%'
             OR text LIKE '1/%' OR text LIKE '%(1/%')"""

    # (time built, facets) -- see facets().
    _facet_cache: tuple[float, dict] | None = None

    @property
    def db(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use.

        One per thread, not one shared. Flask serves requests on separate
        threads, and a single connection used by two of them at once fails
        with "sqlite3.InterfaceError: bad parameter or other API misuse". The
        new UI's four parallel requests at page load hit it on 14 Sep, and an
        extension ingest arriving at the same moment would have been lost the
        same way. WAL lets these connections read side by side; writers wait
        on the busy timeout.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # timeout, and the matching PRAGMA, because background passes hold
            # brief write locks. Without it the default 5s applies and
            # concurrent writes fail outright: 32 "database is locked" 500s
            # were logged while the transcript pass ran, every one of them a
            # bookmark not captured.
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA foreign_keys = ON")      # per connection, not per file
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self._local = threading.local()
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        self.db.commit()

    def _migrate(self):
        """Idempotent column additions, so an existing database keeps its data.

        The first 26,946 tweets were captured before any of these existed.
        """
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(tweets)")}
        # "Is this row only here because a bookmark quotes it?" is asked per
        # row by the undated scope and the stats; without an index each ask
        # scans the whole table.
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tweets_quoted ON tweets(quoted_id)")
        # Removal has to be a tombstone, not a DELETE. The tweet is still
        # bookmarked on x.com, so the next sync would faithfully re-add it and
        # the removal would silently undo itself.
        if "deleted" not in cols:
            self.db.execute("ALTER TABLE tweets ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
            self.db.execute("ALTER TABLE tweets ADD COLUMN deleted_at TEXT")
        # Set once the conversation has been fetched, so the thread pass is
        # resumable and never re-fetches a conversation it already has.
        if "thread_fetched_at" not in cols:
            self.db.execute("ALTER TABLE tweets ADD COLUMN thread_fetched_at TEXT")
        # Counted when the queue hands a tweet out, not when it succeeds.
        # Without this the pass has no forward-progress guarantee: a tweet whose
        # TweetDetail never arrives is never marked fetched, so the queue keeps
        # offering it and the browser keeps reloading the same permalink for
        # ever. That is exactly what happened on the first run.
        if "thread_attempts" not in cols:
            self.db.execute(
                "ALTER TABLE tweets ADD COLUMN thread_attempts INTEGER NOT NULL DEFAULT 0")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tweets_live ON tweets(deleted) WHERE deleted = 0")

        # Bookmark order, recoverable later. x.com's timeline entries carry a
        # snowflake-shaped `sortIndex` that appears to encode when the bookmark
        # was made -- which nothing else in the payload does. The first capture
        # did not keep it, so this is null for the backfill; a re-scrape can
        # fill it in. Kept separate from first_seen, which only records when
        # *we* saw a tweet.
        if "sort_index" not in cols:
            self.db.execute("ALTER TABLE tweets ADD COLUMN sort_index TEXT")
            self.db.execute("ALTER TABLE tweets ADD COLUMN bookmarked_at TEXT")

        # Shortener resolution. resolve_links.py adds these on its first run,
        # but stats() and search read them, so a fresh database without them
        # crashed /api/stats until the resolver had been run once.
        # The same for the article store and enrich_links.py's columns, which
        # find(), facets() and stats() read: a fresh database failed
        # /api/stats with "no such table: articles" until the first archive pass.
        self.db.executescript(ARTICLES_SCHEMA)
        # How many tweets contain each word, read by related.py to pick a
        # seed's distinctive words. A view over the index; it stores nothing.
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS tweets_fts_vocab USING fts5vocab(tweets_fts, row)")
        link_cols = {r[1] for r in self.db.execute("PRAGMA table_info(links)")}
        for name in ("resolved_url", "resolve_status", "resolved_at",
                     "article_date", "fetched_meta_at", "meta_status"):
            if name not in link_cols:
                self.db.execute(f"ALTER TABLE links ADD COLUMN {name} TEXT")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_links_resolved ON links(resolved_url)")

        # Free-form labels the owner adds by hand, many per tweet. Distinct
        # from `topics`, which holds exactly one machine-assigned category:
        # this is how a tweet gets sub-categorised or cross-filed without
        # giving up its place in the taxonomy.
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS user_tags (
            tweet_id TEXT NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
            tag      TEXT NOT NULL,
            added_at TEXT,
            PRIMARY KEY (tweet_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_user_tags_tag ON user_tags(tag);
        """)

        # Replies by the original author, i.e. the thread itself. Replies by
        # anyone else are not stored: the owner only wants the author's own
        # continuation, and keeping the rest would multiply the database for
        # no gain.
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS thread_parts (
            root_id    TEXT NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
            id         TEXT NOT NULL,
            position   INTEGER,
            created_at TEXT,
            text       TEXT,
            PRIMARY KEY (root_id, id)
        );
        CREATE INDEX IF NOT EXISTS idx_thread_root ON thread_parts(root_id);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    # ── Ingest ────────────────────────────────────────────────────────────────

    def upsert_tweets(self, records: Iterable[dict]) -> dict:
        """Insert or refresh tweets. Returns {added, updated, links, media}.

        Re-scraping is expected and must be cheap and lossless, so this is
        careful about two things:

        * `first_seen` is preserved on update -- it is what "new bookmark"
          means, and clobbering it would destroy the only ordering we have for
          when a bookmark entered the library.
        * A shorter text never overwrites a longer one. The timeline gives 280
          truncated characters; an expansion pass gives the rest. Re-running the
          collector afterwards must not undo that.
        """
        added = updated = skipped = n_links = n_media = dated = 0
        now = _utcnow()
        cur = self.db.cursor()

        for r in records:
            tid = str(r.get("id") or "").strip()
            if not tid:
                continue

            row = cur.execute(
                "SELECT rowid, text, truncated, deleted, bookmarked_at FROM tweets WHERE id = ?",
                (tid,)
            ).fetchone()

            # A tweet the owner removed stays removed. It is still bookmarked
            # on x.com, so without this the next sync re-adds it and the
            # removal quietly undoes itself.
            if row is not None and row["deleted"]:
                skipped += 1
                continue

            text = r.get("text") or ""
            truncated = 1 if r.get("truncated") else 0
            # Counted so a re-walk of the whole timeline, which adds nothing,
            # can still tell the browser it is doing useful work.
            if r.get("bookmarked_at") and (row is None or not row["bookmarked_at"]):
                dated += 1

            if row is None:
                cur.execute("""
                    INSERT INTO tweets (id, url, author_handle, author_name, author_id,
                                        created_at, text, lang, truncated, is_thread,
                                        quoted_id, quoted_handle, quoted_text,
                                        reply_count, retweet_count, like_count,
                                        source, first_seen, last_seen, raw,
                                        sort_index, bookmarked_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    tid, r.get("url", ""), r.get("author_handle"), r.get("author_name"),
                    r.get("author_id"), r.get("created_at"), text, r.get("lang"),
                    truncated, 1 if r.get("is_thread") else 0,
                    r.get("quoted_id"), r.get("quoted_handle"), r.get("quoted_text"),
                    r.get("reply_count"), r.get("retweet_count"), r.get("like_count"),
                    r.get("source", "graphql"), now, now,
                    json.dumps(r["raw"], ensure_ascii=False) if r.get("raw") else None,
                    r.get("sort_index"), r.get("bookmarked_at"),
                ))
                added += 1
            else:
                keep_text = row["text"] or ""
                # Never trade a longer capture for a shorter one.
                if len(text) <= len(keep_text):
                    text, truncated = keep_text, row["truncated"]
                cur.execute("""
                    UPDATE tweets SET url=COALESCE(NULLIF(?,''), url),
                        author_handle=COALESCE(?, author_handle),
                        author_name=COALESCE(?, author_name),
                        created_at=COALESCE(?, created_at),
                        text=?, truncated=?,
                        quoted_text=COALESCE(?, quoted_text),
                        reply_count=COALESCE(?, reply_count),
                        retweet_count=COALESCE(?, retweet_count),
                        like_count=COALESCE(?, like_count),
                        sort_index=COALESCE(?, sort_index),
                        bookmarked_at=COALESCE(?, bookmarked_at),
                        last_seen=?
                    WHERE id=?
                """, (
                    r.get("url", ""), r.get("author_handle"), r.get("author_name"),
                    r.get("created_at"), text, truncated, r.get("quoted_text"),
                    r.get("reply_count"), r.get("retweet_count"), r.get("like_count"),
                    r.get("sort_index"), r.get("bookmarked_at"),
                    now, tid,
                ))
                updated += 1

            for m in r.get("media") or []:
                if not m.get("url"):
                    continue
                cur.execute(
                    "INSERT OR REPLACE INTO media(tweet_id, kind, url, alt) VALUES (?,?,?,?)",
                    (tid, m.get("kind"), m.get("url"), m.get("alt")))
                n_media += 1

            for l in r.get("links") or []:
                target = l.get("expanded_url") or l.get("url")
                if not target:
                    continue
                # Don't disturb archive columns on a re-scrape: an already
                # archived link must not be reset to pending.
                cur.execute("""
                    INSERT INTO links(tweet_id, url, expanded_url, display_url, title)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(tweet_id, expanded_url) DO UPDATE SET
                        title = COALESCE(excluded.title, links.title)
                """, (tid, l.get("url"), target, l.get("display_url"), l.get("title")))
                n_links += 1

            for tag in r.get("hashtags") or []:
                cur.execute(
                    "INSERT OR IGNORE INTO hashtags(tweet_id, tag) VALUES (?,?)",
                    (tid, tag.lower()))

        self.db.commit()
        return {"added": added, "updated": updated, "skipped": skipped,
                "links": n_links, "media": n_media, "dated": dated}

    # ── Read ──────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        q = lambda s, *a: self.db.execute(s, a).fetchone()[0]
        return {
            "tweets":          q("SELECT COUNT(*) FROM tweets WHERE deleted=0"),
            "authors":         q("SELECT COUNT(DISTINCT author_handle) FROM tweets WHERE deleted=0"),
            "truncated":       q("SELECT COUNT(*) FROM tweets WHERE truncated = 1 AND deleted=0"),
            "threads":         q("SELECT COUNT(*) FROM tweets WHERE is_thread = 1 AND deleted=0"),
            "withMedia":       q("SELECT COUNT(DISTINCT tweet_id) FROM media"),
            "links":           q("SELECT COUNT(*) FROM links"),
            # COALESCE, not expanded_url: two t.co codes pointing at the same
            # article are one link, and counting them twice overstates the
            # archive queue by about a third.
            "uniqueLinks":     q("SELECT COUNT(DISTINCT COALESCE(resolved_url, expanded_url)) FROM links"),
            "linksPending":    q("SELECT COUNT(DISTINCT COALESCE(resolved_url, expanded_url)) FROM links WHERE archived_at IS NULL"),
            "linksUnresolved": q("SELECT COUNT(DISTINCT expanded_url) FROM links WHERE resolved_url IS NULL"),
            "topics":          q("SELECT COUNT(*) FROM topics"),
            "removed":         q("SELECT COUNT(*) FROM tweets WHERE deleted=1"),
            # `tweets` counts rows; only rows with a sortIndex are bookmarks.
            # The rest are tweets a bookmark quotes, plus a few not seen on the
            # last full walk (probably removed on X) -- `undated`, which the UI
            # shows so a time frame never silently drops anything. It used to
            # count every row without a date, quoted tweets included.
            "bookmarks":       q(f"SELECT COUNT(*) FROM tweets t WHERE t.deleted=0 AND {SCOPES['bookmarks']}"),
            "quotedOnly":      q("SELECT COUNT(*) FROM tweets t WHERE t.deleted=0 AND t.sort_index IS NULL "
                                 "AND EXISTS (SELECT 1 FROM tweets b WHERE b.quoted_id = t.id)"),
            "undated":         q(f"SELECT COUNT(*) FROM tweets t WHERE t.deleted=0 AND {SCOPES['undated']}"),
            "articles":        q("SELECT COUNT(*) FROM articles WHERE status='ok'"),
            "articleWords":    q("SELECT COALESCE(SUM(word_count), 0) FROM articles WHERE status='ok'"),
            "oldest":          q("SELECT COALESCE(MIN(created_at), '') FROM tweets WHERE deleted=0"),
            "newest":          q("SELECT COALESCE(MAX(created_at), '') FROM tweets WHERE deleted=0"),
            "dbBytes":         os.path.getsize(self.path) if os.path.exists(self.path) else 0,
        }

    def set_tags(self, tweet_id: str, tags: list[str]) -> list[str]:
        """Replace a tweet's hand-written tags. Returns what was stored."""
        clean, seen = [], set()
        for t in tags:
            t = " ".join(str(t).split()).strip().lstrip("#")
            k = t.lower()
            if t and k not in seen:
                seen.add(k)
                clean.append(t)
        self.db.execute("DELETE FROM user_tags WHERE tweet_id = ?", (tweet_id,))
        self.db.executemany(
            "INSERT OR IGNORE INTO user_tags(tweet_id, tag, added_at) VALUES (?,?,?)",
            [(tweet_id, t, _utcnow()) for t in clean])
        self.db.commit()
        return clean

    def tag_counts(self) -> list[dict]:
        rows = self.db.execute("""
            SELECT ut.tag, COUNT(*) AS n FROM user_tags ut
            JOIN tweets t ON t.id = ut.tweet_id WHERE t.deleted = 0
            GROUP BY 1 ORDER BY n DESC, ut.tag
        """).fetchall()
        return [dict(r) for r in rows]

    def search(self, q: str = "", author: str = "", topic: str = "", tag: str = "",
               date_from: str = "", date_to: str = "",
               added_from: str = "", added_to: str = "",
               limit: int = 50, offset: int = 0) -> list[dict]:
        """Full-text search, or a plain listing when no query is given.

        The topic column returned is always COALESCE(topic_user, topic): the
        owner's own label wins over the model's, everywhere it is read.
        """
        params: list = []
        cols = "t.*, COALESCE(tp.topic_user, tp.topic) AS topic"
        if q:
            sql = (f"SELECT {cols} FROM tweets_fts f "
                   "JOIN tweets t ON t.rowid = f.rowid "
                   "LEFT JOIN topics tp ON tp.tweet_id = t.id "
                   "WHERE tweets_fts MATCH ? AND t.deleted = 0")
            params.append(q)
        else:
            sql = (f"SELECT {cols} FROM tweets t "
                   "LEFT JOIN topics tp ON tp.tweet_id = t.id "
                   "WHERE t.deleted = 0")
        if author:
            sql += " AND t.author_handle = ?"
            params.append(author.lstrip("@"))
        if topic:
            sql += " AND COALESCE(tp.topic_user, tp.topic) = ?"
            params.append(topic)
        if tag:
            sql += " AND EXISTS (SELECT 1 FROM user_tags ut " \
                   "WHERE ut.tweet_id = t.id AND ut.tag = ?)"
            params.append(tag)
        # When the tweet was posted -- known for everything.
        if date_from:
            sql += " AND t.created_at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND t.created_at <= ?"
            params.append(date_to + "T23:59:59Z" if len(date_to) == 10 else date_to)
        # When it was bookmarked. bookmarked_at is only populated where a
        # capture recorded sortIndex; first_seen stands in otherwise, which for
        # the original backfill is just the day it was imported.
        added = "COALESCE(t.bookmarked_at, t.first_seen)"
        if added_from:
            sql += f" AND {added} >= ?"
            params.append(added_from)
        if added_to:
            sql += f" AND {added} <= ?"
            params.append(added_to + "T23:59:59Z" if len(added_to) == 10 else added_to)

        sql += " ORDER BY t.created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = [dict(r) for r in self.db.execute(sql, params).fetchall()]
        if rows:
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            tags: dict[str, list[str]] = {}
            for tid, tg in self.db.execute(
                    f"SELECT tweet_id, tag FROM user_tags WHERE tweet_id IN ({marks})", ids):
                tags.setdefault(tid, []).append(tg)
            for r in rows:
                r["tags"] = tags.get(r["id"], [])
        return rows

    def fts_query(self, q: str) -> str:
        """The query as typed when FTS5 accepts it, otherwise its words quoted.

        People type `U.S.`, `don't` and `#Gaza`, which FTS5 reads as syntax and
        rejects; that surfaced as a 500 and an empty list. Checked against both
        tables because a column filter valid for one is an error in the other.
        """
        q = (q or "").strip()
        if not q:
            return ""
        try:
            for table in ("tweets_fts", "articles_fts"):
                self.db.execute(f"SELECT 1 FROM {table} WHERE {table} MATCH ? LIMIT 1",
                                (q,)).fetchall()
            return q
        except sqlite3.OperationalError:
            return " ".join(f'"{w}"' for w in re.findall(r"\w+", q))

    def find(self, q: str = "", tag: str = "", topic: str = "", author: str = "",
             since_days: int | None = None, date_from: str = "", date_to: str = "",
             sort: str = "bookmarked", has_archive: str = "",
             language: str = "", content_type: str = "", status: str = "",
             date_added: str = "", scope: str = "bookmarks", thread: str = "",
             order: str = "desc", limit: int = 40, offset: int = 0) -> dict:
        """The one search the app needs: bookmarks, matched on their own text,
        their thread, the tweet they quote, or the articles any of those link to.

        A query that only searched tweets would miss the 9 million archived
        words, which is most of what this collection actually knows. So a hit
        on an article's body surfaces the tweet that pointed at it, and each
        result says where it matched (`matchedIn`).

        `scope`: bookmarks (default) | undated | all. Tweets that are only
        quoted by a bookmark are stored as rows but are not bookmarks, and must
        not be listed or counted as such.

        `sort`: bookmarked (exact since the sortIndex backfill) | posted.
        """
        p: dict = {}

        def add(v) -> str:
            k = f"p{len(p)}"
            p[k] = v
            return ":" + k

        where = ["t.deleted = 0", SCOPES.get(scope, SCOPES["bookmarks"])]
        prefix = ""
        fq = self.fts_query(q)
        if fq:
            p["q"] = fq
            # Links meet articles through two indexed probes, not one join on
            # COALESCE(resolved_url, expanded_url): no index can serve that
            # expression, and it took 7 s for "gaza" and 10 s for
            # "iran OR israel" on the live data. This form takes under 0.1 s.
            # The CTE is materialised because a bookmark also matches through
            # the tweet it quotes.
            prefix = f"""WITH hit(id) AS MATERIALIZED (
                SELECT t2.id FROM tweets_fts f JOIN tweets t2 ON t2.rowid = f.rowid
                 WHERE tweets_fts MATCH :q
                UNION
                SELECT l.tweet_id FROM links l WHERE l.resolved_url IN ({ARTICLE_HITS})
                UNION
                SELECT l.tweet_id FROM links l
                 WHERE l.resolved_url IS NULL AND l.expanded_url IN ({ARTICLE_HITS}))
            """
            where.append("(t.id IN (SELECT id FROM hit) OR t.quoted_id IN (SELECT id FROM hit))")
        if topic == UNCATEGORISED:
            # The facet lists these under a name no row actually carries.
            where.append("COALESCE(tp.topic_user, tp.topic) IS NULL")
        elif topic:
            where.append(f"COALESCE(tp.topic_user, tp.topic) = {add(topic)}")
        if tag:
            where.append("EXISTS (SELECT 1 FROM user_tags ut "
                         f"WHERE ut.tweet_id = t.id AND ut.tag = {add(tag)})")
        if author:
            where.append(f"t.author_handle = {add(author.lstrip('@'))} COLLATE NOCASE")
        if language:
            where.append(f"t.lang = {add(language)}")
        if content_type in CONTENT_TYPES:
            where.append(CONTENT_TYPES[content_type])
        if status in STATUSES:
            where.append(STATUSES[status])
        if thread == "yes":
            where.append("(t.is_thread = 1 OR t.thread_done = 1)")
        if has_archive == "yes":
            where.append(STATUSES["archived"])
        elif has_archive == "links":
            where.append("EXISTS (SELECT 1 FROM links l WHERE l.tweet_id = t.id)")

        added = "COALESCE(t.bookmarked_at, t.first_seen)"
        date_col = added if sort == "bookmarked" else "t.created_at"

        # Stored dates read 2026-09-13T10:00:00Z. datetime('now') gives
        # "2026-09-13 12:00:00", and "T" sorts after " ", so comparing against it
        # let a "24 hours" filter admit most of the previous day.
        if since_days:
            where.append(f"{date_col} >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', "
                         f"{add(f'-{int(since_days)} days')})")
        if date_from:
            where.append(f"{date_col} >= {add(date_from)}")
        if date_to:
            where.append(f"{date_col} <= "
                         f"{add(date_to + 'T23:59:59Z' if len(date_to) == 10 else date_to)}")
        if date_added:
            where.append(f"{bucket_sql(added)} = {add(date_added)}")

        cond = " AND ".join(where)
        direction = "ASC" if order == "asc" else "DESC"
        rows = [dict(r) for r in self.db.execute(f"""{prefix}
            SELECT {LIST_COLS}, {added} AS added_at,
                   COALESCE(tp.topic_user, tp.topic) AS topic
              FROM tweets t
              LEFT JOIN topics tp ON tp.tweet_id = t.id
             WHERE {cond}
             ORDER BY {date_col} {direction}, t.id {direction}
             LIMIT {add(int(limit))} OFFSET {add(int(offset))}""", p)]
        count = self.db.execute(
            f"{prefix} SELECT COUNT(*) FROM tweets t "
            f"LEFT JOIN topics tp ON tp.tweet_id = t.id WHERE {cond}", p).fetchone()[0]
        return {"total": count, "results": self._decorate(rows, fq), "query": fq}

    def _in(self, sql: str, ids: list, before: tuple = ()) -> list:
        """Run `sql` with its `{}` replaced by one placeholder per id."""
        ids = list(dict.fromkeys(i for i in ids if i))
        if not ids:
            return []
        return self.db.execute(sql.format(",".join("?" * len(ids))),
                               [*before, *ids]).fetchall()

    def _links_for(self, ids: list, fq: str = "") -> dict[str, list]:
        """Each tweet's outbound links with their archive state, and, for a
        query, a highlighted snippet from any archived text that matched."""
        out: dict[str, list] = {}
        rows = [dict(r) for r in self._in("""
            SELECT l.tweet_id,
                   COALESCE(l.resolved_url, l.expanded_url) AS url,
                   COALESCE(MAX(NULLIF(l.title, '')), MAX(NULLIF(a.title, ''))) AS title,
                   MAX(l.article_date) AS article_date,
                   MAX(a.status) AS archive_status,
                   MAX(a.word_count) AS words,
                   MAX(a.source) AS source
              FROM links l
              LEFT JOIN articles a ON a.url = COALESCE(l.resolved_url, l.expanded_url)
             WHERE l.tweet_id IN ({})
             -- Group by the expression, not the alias: `articles` also has a
             -- `url` column, so the bare alias is ambiguous.
             GROUP BY l.tweet_id, COALESCE(l.resolved_url, l.expanded_url)""", ids)]
        snips: dict[str, str] = {}
        if fq and rows:
            # char(2)/char(3) rather than markup: the page escapes the text
            # first and only then turns these into <mark>.
            snips = dict(self._in("""
                SELECT a.url, snippet(articles_fts, 1, char(2), char(3), '…', 24)
                  FROM articles_fts af JOIN articles a ON a.rowid = af.rowid
                 WHERE articles_fts MATCH ? AND a.url IN ({})""",
                [r["url"] for r in rows], before=(fq,)))
        for r in rows:
            r["kind"] = link_kind(r["url"], r["source"], r["archive_status"])
            r["host"] = host_of(r["url"])
            if r["url"] in snips:
                r["snippet"] = snips[r["url"]]
            out.setdefault(r.pop("tweet_id"), []).append(r)
        return out

    def _decorate(self, rows: list[dict], fq: str = "") -> list[dict]:
        """Everything a result card shows beyond the tweet row itself."""
        if not rows:
            return rows
        ids = [r["id"] for r in rows]
        quoted = {r["id"]: dict(r) for r in self._in(
            "SELECT id, url, author_handle, author_name, created_at, text "
            "FROM tweets WHERE deleted = 0 AND id IN ({})",
            [r.get("quoted_id") for r in rows])}
        links = self._links_for(ids + list(quoted), fq)
        tags: dict[str, list[str]] = {}
        for tid, tg in self._in("SELECT tweet_id, tag FROM user_tags WHERE tweet_id IN ({})", ids):
            tags.setdefault(tid, []).append(tg)
        media: dict[str, dict] = {}
        for tid, kind, n in self._in(
                "SELECT tweet_id, kind, COUNT(*) FROM media WHERE tweet_id IN ({}) GROUP BY 1, 2", ids):
            media.setdefault(tid, {})[kind] = n
        parts: dict[str, list[str]] = {}
        for rid, text in self._in(
                "SELECT root_id, text FROM thread_parts WHERE root_id IN ({}) "
                "ORDER BY root_id, position", ids):
            parts.setdefault(rid, []).append(text or "")

        hits: set[str] = set()
        terms: list[str] = []
        if fq:
            hits = {r[0] for r in self._in(
                "SELECT t2.id FROM tweets_fts f JOIN tweets t2 ON t2.rowid = f.rowid "
                "WHERE tweets_fts MATCH ? AND t2.id IN ({})", ids + list(quoted), before=(fq,))}
            terms = [fold(w) for w in query_terms(fq)]

        def has(s) -> bool:
            s = fold(s or "")
            return bool(s) and any(re.search(r"(?<!\w)" + re.escape(w), s) for w in terms)

        for r in rows:
            full = r.get("text") or ""
            texts = parts.get(r["id"], [])
            joined = "\n\n".join(texts)
            # save_thread appends the author's replies to the opener's text so
            # that search sees them; the card shows the opener on its own.
            opener = full
            if joined and full.endswith(joined):
                opener = full[:len(full) - len(joined)].rstrip()
            r["opener"] = opener
            r["thread_count"] = len(texts)
            r["links"] = links.get(r["id"], [])
            r["tags"] = tags.get(r["id"], [])
            r["media"] = media.get(r["id"], {})
            qt = quoted.get(r.get("quoted_id") or "")
            if qt:
                qt = dict(qt, links=links.get(qt["id"], []))
            r["quoted"] = qt
            r["contentType"] = primary_type(r)
            if fq:
                m = []
                if r["id"] in hits:
                    if has(opener) or has(r.get("author_handle")) or has(r.get("author_name")):
                        m.append("tweet")
                    if has(r.get("quoted_text")):
                        m.append("quoted")
                    if joined and has(joined):
                        m.append("thread")
                    if not m:
                        m.append("tweet")
                if any(l.get("snippet") for l in r["links"]):
                    m.append("article")
                if qt:
                    if qt["id"] in hits and "quoted" not in m:
                        m.append("quoted")
                    if any(l.get("snippet") for l in qt["links"]):
                        m.append("quotedArticle")
                r["matchedIn"] = m
        return rows

    def tweet(self, tid: str) -> dict | None:
        """One bookmark with everything the detail panel shows."""
        row = self.db.execute(f"""
            SELECT {LIST_COLS}, t.reply_count, t.retweet_count, t.like_count,
                   t.bookmarked_at, t.first_seen, t.deleted,
                   COALESCE(t.bookmarked_at, t.first_seen) AS added_at,
                   COALESCE(tp.topic_user, tp.topic) AS topic,
                   tp.topic AS topic_model, tp.topic_user
              FROM tweets t LEFT JOIN topics tp ON tp.tweet_id = t.id
             WHERE t.id = ?""", (tid,)).fetchone()
        if not row:
            return None
        d = self._decorate([dict(row)])[0]
        d["thread"] = [dict(r) for r in self.db.execute(
            "SELECT id, position, created_at, text FROM thread_parts "
            "WHERE root_id = ? ORDER BY position", (tid,))]
        d["mediaItems"] = [dict(r) for r in self.db.execute(
            "SELECT kind, url, alt FROM media WHERE tweet_id = ?", (tid,))]
        d["hashtags"] = [r[0] for r in self.db.execute(
            "SELECT tag FROM hashtags WHERE tweet_id = ? ORDER BY tag", (tid,))]
        d["quotedBy"] = [dict(r) for r in self.db.execute(
            "SELECT id, url, author_handle FROM tweets "
            "WHERE quoted_id = ? AND deleted = 0 AND sort_index IS NOT NULL", (tid,))]
        groups = [d["links"]] + ([d["quoted"]["links"]] if d.get("quoted") else [])
        for group in groups:
            for l in group:
                if l["archive_status"] in ("ok", "thin"):
                    ex = self.db.execute("SELECT substr(text, 1, 1200) FROM articles WHERE url = ?",
                                         (l["url"],)).fetchone()
                    l["excerpt"] = ex[0] if ex else ""
        return d

    def facets(self, max_age: float = 30.0) -> dict:
        """Sidebar facets with counts, over bookmarks only.

        Field names follow Mneme's /api/facets (`topics`, `taxonomy`,
        `dateAdded`, `total`, value/count pairs) so Mneme's client can read it.
        Cached briefly: it is a dozen scans, and the sidebar asks on every load.
        """
        now = time.time()
        if self._facet_cache and now - self._facet_cache[0] < max_age:
            return self._facet_cache[1]
        base = ("FROM tweets t LEFT JOIN topics tp ON tp.tweet_id = t.id "
                f"WHERE t.deleted = 0 AND {SCOPES['bookmarks']}")
        pairs = lambda sql: [{"value": v if v is not None else "", "count": n}
                             for v, n in self.db.execute(sql)]

        topics = pairs(f"SELECT COALESCE(tp.topic_user, tp.topic), COUNT(*) {base} "
                       "GROUP BY 1 ORDER BY 2 DESC")
        for t in topics:
            t["value"] = t["value"] or UNCATEGORISED
        topics.sort(key=lambda t: t["value"] == UNCATEGORISED)     # stable: keeps count order

        added = "COALESCE(t.bookmarked_at, t.first_seen)"
        buckets = {v: n for v, n in self.db.execute(
            f"SELECT {bucket_sql(added)}, COUNT(*) {base} GROUP BY 1")}
        years = sorted((k for k in buckets if k not in DATE_ORDER and k != "Unknown"), reverse=True)

        # One pass for all the overlapping yes/no facets.
        named = {**{f"ct_{k}": c for k, c in CONTENT_TYPES.items()},
                 **{f"st_{k}": c for k, c in STATUSES.items()},
                 "threads": "(t.is_thread = 1 OR t.thread_done = 1)"}
        sums = self.db.execute(
            "SELECT " + ", ".join(f"SUM(CASE WHEN {c} THEN 1 ELSE 0 END)" for c in named.values())
            + f" {base}").fetchone()
        sums = dict(zip(named, (n or 0 for n in sums)))

        out = {
            "total": self.db.execute(f"SELECT COUNT(*) {base}").fetchone()[0],
            "topics": topics,
            "taxonomy": [t["value"] for t in topics if t["value"] != UNCATEGORISED],
            "assigned": sum(t["count"] for t in topics if t["value"] != UNCATEGORISED),
            "builtAt": "",
            "dateAdded": [{"value": k, "count": buckets[k]}
                          for k in [*DATE_ORDER, *years, "Unknown"] if k in buckets],
            "language": pairs(f"SELECT t.lang, COUNT(*) {base} GROUP BY 1 ORDER BY 2 DESC"),
            "contentType": [{"value": k, "count": sums[f"ct_{k}"]} for k in CONTENT_TYPES],
            "status": [{"value": k, "count": sums[f"st_{k}"]} for k in STATUSES],
            "threads": sums["threads"],
            "tags": [{"value": d["tag"], "count": d["n"]} for d in self.tag_counts()],
            "authors": [{"value": h, "name": nm, "count": n} for h, nm, n in self.db.execute(
                f"SELECT t.author_handle, MAX(t.author_name), COUNT(*) {base} "
                "GROUP BY 1 ORDER BY 3 DESC LIMIT 50")],
            "undated": self.db.execute(
                f"SELECT COUNT(*) FROM tweets t WHERE t.deleted = 0 AND {SCOPES['undated']}"
            ).fetchone()[0],
        }
        self._facet_cache = (now, out)
        return out

    def thread_queue(self, limit: int = 1) -> list[dict]:
        """Bookmarks that look like threads and have not been fetched yet.

        Two signals, unioned. `is_thread` catches bookmarks that are replies;
        the text markers catch thread *openers*, which the bookmarks payload
        cannot distinguish from a standalone tweet -- no in_reply_to, and
        conversation_id equal to their own id. Openers are the commoner
        bookmark, so without the text test the queue would be close to the
        wrong set entirely.

        Most-engaged first: a 5,000-reply thread is likelier to be substantial
        than one with three.
        """
        rows = self.db.execute(f"""
            SELECT id, url, author_handle, reply_count
            FROM tweets
            WHERE {self.THREAD_CANDIDATE}
            -- Self-declared threads first: a tweet saying "a thread" or
            -- numbering itself "1/" really is one. reply_count was a bad proxy
            -- and ordering by it put a 13,940-reply viral tweet first, whose
            -- page froze the renderer.
            ORDER BY CASE WHEN lower(text) LIKE '%a thread%'
                            OR lower(text) LIKE '%thread:%'
                            OR text LIKE '%' || char(129525) || '%'
                            OR text LIKE '1/%' OR text LIKE '%(1/%'
                          THEN 0 ELSE 1 END,
                     created_at DESC
            LIMIT ?""", (limit,)).fetchall()
        out = [dict(r) for r in rows]
        # Claim them: increment before handing them over.
        if out:
            self.db.executemany(
                "UPDATE tweets SET thread_attempts = thread_attempts + 1 WHERE id = ?",
                [(r["id"],) for r in out])
            self.db.commit()
        return out

    def thread_queue_size(self) -> int:
        return self.db.execute(
            f"SELECT COUNT(*) FROM tweets WHERE {self.THREAD_CANDIDATE}").fetchone()[0]

    def save_thread(self, root_id: str, parts: list[dict]) -> int:
        """Store the author's own replies and mark the root as fetched.

        Marked fetched even when there are no parts: "we looked and it was not
        a thread" is a result, and without recording it the queue would offer
        the same tweet forever.
        """
        self.db.executemany("""
            INSERT OR REPLACE INTO thread_parts
                (root_id, id, position, created_at, text)
            VALUES (?,?,?,?,?)""",
            [(root_id, p["id"], i + 1, p.get("created_at"), p.get("text") or "")
             for i, p in enumerate(parts)])
        # The thread text belongs in the searchable body of the bookmark: a
        # thread's argument is usually in the later tweets, and a search that
        # only saw the opener would miss it.
        if parts:
            sep = chr(10) + chr(10)
            joined = sep.join(p.get("text") or "" for p in parts)
            self.db.execute("""
                UPDATE tweets
                   SET text = text || char(10) || char(10) || ?,
                       is_thread = 1, thread_done = 1
                 WHERE id = ? AND thread_done = 0""", (joined, root_id))
        self.db.execute(
            "UPDATE tweets SET thread_fetched_at = ? WHERE id = ?",
            (_utcnow(), root_id))
        self.db.commit()
        return len(parts)

    def article(self, url: str) -> dict | None:
        r = self.db.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()
        return dict(r) if r else None

    def topic_counts(self) -> list[dict]:
        """Topics with counts, the owner's overrides taking precedence."""
        rows = self.db.execute("""
            SELECT COALESCE(tp.topic_user, tp.topic) AS topic, COUNT(*) AS n
            FROM topics tp JOIN tweets t ON t.id = tp.tweet_id
            WHERE t.deleted = 0 AND COALESCE(tp.topic_user, tp.topic) IS NOT NULL
            GROUP BY 1 ORDER BY n DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def known_ids(self, ids: Iterable[str]) -> set[str]:
        """Which of these tweet ids do we already hold?

        The collector's stop condition: a page that is entirely known means we
        have caught up with the previous run and can stop scrolling.
        """
        ids = list(ids)
        if not ids:
            return set()
        out: set[str] = set()
        for i in range(0, len(ids), 500):        # keep under SQLite's variable limit
            chunk = ids[i:i + 500]
            rows = self.db.execute(
                f"SELECT id FROM tweets WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            out |= {r[0] for r in rows}
        return out

    def pending_links(self, limit: int = 100) -> list[dict]:
        """Outbound links not yet archived, most-linked first.

        Deduplicated across tweets: if forty bookmarks point at the same
        article it is fetched once.
        """
        rows = self.db.execute("""
            SELECT expanded_url, COUNT(*) AS refs, MIN(title) AS title
            FROM links WHERE archived_at IS NULL AND expanded_url IS NOT NULL
            GROUP BY expanded_url ORDER BY refs DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_link_archived(self, expanded_url: str, archive_url: str | None, status: str):
        self.db.execute(
            "UPDATE links SET archived_at=?, archive_url=?, archive_status=? WHERE expanded_url=?",
            (_utcnow(), archive_url, status, expanded_url))
        self.db.commit()

    def remove(self, ids: Iterable[str]) -> int:
        """Tombstone tweets so the collector will not bring them back.

        Not a DELETE: the tweet is still bookmarked on x.com, so a hard delete
        would be undone by the next sync. The row stays, hidden from search and
        stats, and upsert_tweets skips it forever after.

        The text is blanked -- the point of removing something is usually that
        you did not want to keep it -- but the id remains, because the id is
        what makes the removal stick.
        """
        ids = [str(i) for i in ids if i]
        if not ids:
            return 0
        n = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            cur = self.db.execute(
                f"UPDATE tweets SET deleted=1, deleted_at=?, text='', quoted_text=NULL "
                f"WHERE id IN ({marks}) AND deleted=0", [_utcnow(), *chunk])
            n += cur.rowcount
            # Media and link rows are of no use once the tweet is gone, and
            # leaving them would keep dead URLs in the archive queue.
            self.db.execute(f"DELETE FROM media WHERE tweet_id IN ({marks})", chunk)
            self.db.execute(f"DELETE FROM links WHERE tweet_id IN ({marks})", chunk)
            self.db.execute(f"DELETE FROM hashtags WHERE tweet_id IN ({marks})", chunk)
            self.db.execute(f"DELETE FROM thread_parts WHERE root_id IN ({marks})", chunk)
        self.db.commit()
        return n

    def restore(self, ids: Iterable[str]) -> int:
        """Undo a removal. The text is gone, but the next sync will refill it."""
        ids = [str(i) for i in ids if i]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cur = self.db.execute(
            f"UPDATE tweets SET deleted=0, deleted_at=NULL WHERE id IN ({marks})", ids)
        self.db.commit()
        return cur.rowcount

    def set_topic(self, tweet_id: str, topic: str | None = None, topic_user: str | None = None):
        """Record a topic. The LLM writes `topic`; the owner writes `topic_user`."""
        self.db.execute("""
            INSERT INTO topics(tweet_id, topic, topic_user, assigned_at) VALUES (?,?,?,?)
            ON CONFLICT(tweet_id) DO UPDATE SET
                topic      = COALESCE(excluded.topic, topics.topic),
                topic_user = COALESCE(excluded.topic_user, topics.topic_user),
                assigned_at = excluded.assigned_at
        """, (tweet_id, topic, topic_user, _utcnow()))
        self.db.commit()
