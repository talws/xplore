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
import sqlite3
import datetime
from typing import Iterable

SCHEMA_VERSION = 1


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

    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        # timeout, and the matching PRAGMA, because background passes hold
        # brief write locks. Without it the default 5s applies and concurrent
        # writes fail outright: 32 "database is locked" 500s were logged while
        # the transcript pass ran, every one of them a bookmark not captured.
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA busy_timeout = 30000")
        self.db.row_factory = sqlite3.Row
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
        link_cols = {r[1] for r in self.db.execute("PRAGMA table_info(links)")}
        for name in ("resolved_url", "resolve_status", "resolved_at"):
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
            # No bookmark time recovered yet, so "bookmarked" falls back to
            # the day we first saw the tweet.
            "undated":         q("SELECT COUNT(*) FROM tweets WHERE deleted=0 AND bookmarked_at IS NULL"),
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

    def find(self, q: str = "", tag: str = "", topic: str = "", author: str = "",
             since_days: int | None = None, date_from: str = "", date_to: str = "",
             sort: str = "bookmarked", has_archive: str = "",
             limit: int = 40, offset: int = 0) -> dict:
        """The one search the app needs: tweets, matched on their own text *or*
        on the text of the articles they link to.

        A query that only searched tweets would miss the 8 million archived
        words, which is most of what this collection actually knows. So a hit
        on an article's body surfaces the tweet that pointed at it, and the
        result carries a link to the local copy.

        `sort`:
          bookmarked — when it entered the collection. NOTE: every tweet from
                       the original backfill shares one date (the import day),
                       because x.com does not tell us when a bookmark was made.
                       Accurate only for tweets captured since.
          posted     — the tweet's own date, accurate for everything.
        """
        where, params = ["t.deleted = 0"], []

        if q:
            # Two indexes, one result set. UNION rather than OR across a join,
            # so neither FTS table is scanned for rows the other already found.
            where.append("""t.id IN (
                SELECT t2.id FROM tweets_fts f JOIN tweets t2 ON t2.rowid = f.rowid
                 WHERE tweets_fts MATCH ?
                UNION
                SELECT l.tweet_id FROM articles_fts af
                  JOIN articles a ON a.rowid = af.rowid
                  JOIN links l ON COALESCE(l.resolved_url, l.expanded_url) = a.url
                 WHERE articles_fts MATCH ?)""")
            params += [q, q]
        if topic:
            where.append("COALESCE(tp.topic_user, tp.topic) = ?")
            params.append(topic)
        if tag:
            where.append("EXISTS (SELECT 1 FROM user_tags ut "
                         "WHERE ut.tweet_id = t.id AND ut.tag = ?)")
            params.append(tag)
        if author:
            where.append("t.author_handle = ?")
            params.append(author.lstrip("@"))
        if has_archive == "yes":
            where.append("""EXISTS (SELECT 1 FROM links l3
                JOIN articles a3 ON a3.url = COALESCE(l3.resolved_url, l3.expanded_url)
                WHERE l3.tweet_id = t.id AND a3.status = 'ok')""")
        elif has_archive == "links":
            where.append("EXISTS (SELECT 1 FROM links l4 WHERE l4.tweet_id = t.id)")

        added = "COALESCE(t.bookmarked_at, t.first_seen)"
        date_col = added if sort == "bookmarked" else "t.created_at"

        if since_days:
            where.append(f"{date_col} >= datetime('now', ?)")
            params.append(f"-{int(since_days)} days")
        if date_from:
            where.append(f"{date_col} >= ?")
            params.append(date_from)
        if date_to:
            where.append(f"{date_col} <= ?")
            params.append(date_to + "T23:59:59Z" if len(date_to) == 10 else date_to)

        sql = f"""
            SELECT t.id, t.url, t.author_handle, t.author_name, t.created_at,
                   t.text, t.quoted_text, t.truncated, t.is_thread,
                   {added} AS added_at,
                   COALESCE(tp.topic_user, tp.topic) AS topic
            FROM tweets t
            LEFT JOIN topics tp ON tp.tweet_id = t.id
            WHERE {' AND '.join(where)}
            ORDER BY {date_col} DESC, t.id DESC
            LIMIT ? OFFSET ?"""
        rows = [dict(r) for r in self.db.execute(sql, params + [limit, offset])]

        count = self.db.execute(
            f"SELECT COUNT(*) FROM tweets t LEFT JOIN topics tp ON tp.tweet_id = t.id "
            f"WHERE {' AND '.join(where)}", params).fetchone()[0]

        if rows:
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            links: dict[str, list] = {}
            for row in self.db.execute(f"""
                SELECT l.tweet_id,
                       COALESCE(l.resolved_url, l.expanded_url) AS url,
                       MAX(NULLIF(l.title,'')) AS title,
                       MAX(l.article_date) AS article_date,
                       MAX(a.status) AS archive_status,
                       MAX(a.word_count) AS words
                FROM links l
                LEFT JOIN articles a ON a.url = COALESCE(l.resolved_url, l.expanded_url)
                WHERE l.tweet_id IN ({marks})
                -- Group by the expression, not the alias: `articles` also has
                -- a `url` column, so the bare alias is ambiguous.
                GROUP BY l.tweet_id, COALESCE(l.resolved_url, l.expanded_url)""", ids):
                d = dict(row)
                links.setdefault(d.pop("tweet_id"), []).append(d)
            tags: dict[str, list[str]] = {}
            for tid, tg in self.db.execute(
                    f"SELECT tweet_id, tag FROM user_tags WHERE tweet_id IN ({marks})", ids):
                tags.setdefault(tid, []).append(tg)
            for r in rows:
                r["links"] = links.get(r["id"], [])
                r["tags"] = tags.get(r["id"], [])

        return {"total": count, "results": rows}

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
