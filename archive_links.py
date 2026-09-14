"""
archive_links.py — archive the articles the bookmarked tweets point at.

This is the part no X exporter does. A tweet survives as long as X keeps it; the
article it linked to can be paywalled, rewritten or simply gone. 62 of these
links already return 404 and 191 have unreachable hosts, so for those the
archive is the only copy that will exist.

Two tiers, chosen per link rather than tried blindly:

  direct   — for links the metadata pass fetched successfully. Cheap and fast,
             and it yields the publisher's own text.
  Wayback  — for links that returned 401/403 (paywall, bot-blocker) or failed
             outright. Trying those directly again would spend a request to
             learn something already known.

That routing comes free from `meta_status`, recorded by enrich_links.py, and it
matters: at Wayback's required pacing, sending all 5,775 there would take most
of a day, while sending only the ~1,300 that need it takes about three hours.

Wayback pacing is not tunable downward. The owner's archive.org access has been
blocked by rate limiting before, and a repeat would cost far more than the time
saved: 8s between calls, a longer pause every five, and exponential backoff on
any 403. Both the delay and the backoff are ported from Mneme's fetcher, which
earned them.

Resumable: a URL already stored is skipped, so this can be stopped and resumed
freely, and a later run only picks up links that arrived since.

    python archive_links.py --limit 50        # a taste
    python archive_links.py                   # everything outstanding
    python archive_links.py --phase wayback   # just the hard ones
"""

from __future__ import annotations

import argparse
import datetime
import json
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urlsplit

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Phrases that mean "you reached a bot challenge, not the article". Storing one
# of these as the archived text would be worse than storing nothing, because it
# looks like a success.
BOT_SCREENS = [
    "just a moment", "enable javascript and cookies to continue",
    "performance & security by cloudflare", "checking your browser",
    "verify you are human", "ray id", "attention required! | cloudflare",
    "access denied", "are you a robot",
    # Anubis. Missing from the first list, so 13 of its challenge pages were
    # stored as successfully archived articles.
    "making sure you're not a bot", "making sure you&#39;re not a bot",
    "protected by anubis",
    # archive.today's CAPTCHA page, as served on 2026-09-13. About 75 words,
    # enough to pass MIN_WORDS, and without these it would be stored as an
    # archived article. Deliberately specific: "one more step" alone also
    # appears in ordinary prose.
    "please complete the security check to access",
    "why do i have to complete a captcha",
]

# Video hosts. There is no article text to extract, so fetching them wastes a
# request to learn nothing -- 150 of the 1,318 archiving failures were simply
# videos being asked for prose. Marked "video" rather than "failed", so the
# worklist of genuinely lost articles stays honest and a later transcript pass
# (yt-dlp/Whisper) has a ready-made queue.
#
# Matched on *video* paths, not bare hosts. The first version matched
# `odysee.com/`, `tiktok.com/`, `rumble.com/` and so on, which also caught
# channel pages, profiles and homepages that yt-dlp cannot read -- and missed
# `youtube.com/live/` recordings entirely, nine of which had been "archived" as
# articles whose whole text was YouTube's language picker. Checked against all
# 5,781 stored links before switching.
VIDEO_HOSTS = re.compile(
    r"(youtube\.com/watch\?|youtube\.com/shorts/|youtube\.com/live/|youtu\.be/[\w-]{6,}"
    r"|vimeo\.com/(?:video/)?\d+|player\.vimeo\.com/video/\d+"
    r"|dailymotion\.com/video/|dai\.ly/"
    r"|rumble\.com/v[a-z0-9]+-|rumble\.com/embed/"
    r"|odysee\.com/@[^/?#]+/[^/?#]+"
    r"|twitch\.tv/videos/\d+|clips\.twitch\.tv/|twitch\.tv/[^/?#]+/clip/"
    r"|tiktok\.com/@[^/?#]+/video/\d+|tiktok\.com/t/[A-Za-z0-9]+|vm\.tiktok\.com/"
    r"|facebook\.com/watch|facebook\.com/[^/?#]+/videos/|fb\.watch/"
    r"|bitchute\.com/video/|streamable\.com/[a-z0-9]+)", re.I)


def is_video(url: str) -> bool:
    return bool(VIDEO_HOSTS.search(url or ""))


# Links that can never become an archived article, so they must not sit on the
# recovery worklist either. Deliberately narrow. A broader "bare domain" rule was
# dry-run first and rejected: it would have hidden campaign and tool sites the
# owner bookmarked on purpose, and it matched 79 homepages that archive fine
# (GOV.UK, CounterPunch, a 186,000-word single-page compendium).
_TELEGRAM_POST = re.compile(r"^/(?:s/)?[^/]+/\d+/?$")


def not_article(url: str) -> str | None:
    """A reason this link is not an article, or None if it may be one.

    * x.com / twitter.com -- X's own pages (the bare homepage was the most
      bookmarked "unrecoverable article", 33 times; also /i/broadcasts). They sit
      behind X's login wall, archive.today cannot help, and X content is what
      the collector itself captures.
    * Telegram channel pages with no post number -- a live feed, not a
      document. Posts (t.me/<channel>/<id>) are kept.
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = re.sub(r"^(?:www|m|mobile)\.", "", (parts.hostname or "").lower())
    if host in ("x.com", "twitter.com"):
        return "x.com page"
    if host in ("t.me", "telegram.me") and not _TELEGRAM_POST.match(parts.path or ""):
        return "telegram channel"
    return None


MIN_WORDS = 60          # below this it is a stub, a paywall teaser or a nav page
# Subscription walls. On the automatic run Haaretz, the FT, Jacobin, WSJ and
# Bloomberg pages came back as a few hundred words of site chrome plus a lede
# and were counted as archived -- 394 of them. A marker alone is not enough:
# "already a subscriber" and "subscribe now" sit in the footer of complete
# 2,400-word articles. So a marker only counts on a short page. Measured: the
# teasers cluster under 500 words, the real articles above 1,000.
PAYWALL_MARKERS = (
    "subscribe to unlock this article", "explore more offers",
    "this article is for subscribers",
    "article printing is available to subscribers only",
    "this copy is for your personal, non-commercial use",
    "gift this article",
)
PAYWALL_MAX_WORDS = 600
# One captured page turned out to be 186,000 words -- a legitimate single-page
# compendium, not an extraction fault. Most are ~1,100. The cap is insurance
# against a pathological page, not against that one.
MAX_CHARS = 600_000
# Where the article stops and the page's tail begins: reader comments, the
# publisher's copyright line, "more on this topic" boxes. An FT page archived
# with its comment thread counted 18,395 words for a 1,331-word article; seven
# FT copies were inflated like that. Matched as whole lines, so a sentence that
# quotes one of these phrases does not cut an article short, and only after
# TAIL_MIN_WORDS, because some sites print a line like this near the top.
# "Jump to comments section" was dry-run and rejected: the FT puts it in the
# byline and it cut six articles to under 250 words.
TAIL_LINES = frozenset({
    "reuse this content",              # FT, Guardian, Economist: end of the body
    "comment guidelines",              # FT and Foreign Policy comment threads
    "comments are closed on this story.",
    "discussion about this post",      # Substack
    "explore more on these topics",    # Guardian and its imitators
})
TAIL_PREFIXES = ("copyright the financial times limited",)
TAIL_MIN_WORDS = 150


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc)\
                   .isoformat(timespec="seconds").replace("+00:00", "Z")


SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url         TEXT PRIMARY KEY,      -- the resolved destination
    title       TEXT,
    text        TEXT,
    word_count  INTEGER,
    source      TEXT,                  -- direct | wayback | archiveph
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

NOISE = ["script", "style", "noscript", "iframe", "svg", "canvas", "form",
         "nav", "header", "footer", "aside"]
MAIN_HINTS = ["article", "main", '[role="main"]', ".article-body", ".article-content",
              ".post-content", ".entry-content", ".story-body", "#content"]


def extract(html: str) -> tuple[str | None, str]:
    """(title, readable text). Deliberately modest -- no extra dependencies.

    Takes the largest plausible article container rather than the whole body,
    which keeps navigation and comment threads out of the stored text.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = None
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()

    for tag in soup(NOISE):
        tag.decompose()

    best, best_len = None, 0
    for sel in MAIN_HINTS:
        try:
            for node in soup.select(sel):
                t = node.get_text(" ", strip=True)
                if len(t) > best_len:
                    best, best_len = node, len(t)
        except Exception:
            continue
    node = best if best_len > 400 else soup.body or soup
    text = re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", node.get_text("\n", strip=True)))
    return (title[:300] if title else None), trim_tail(text)


def trim_tail(text: str) -> str:
    """Drop everything from the first end-of-article line onwards."""
    lines = text.split("\n")
    words = 0
    for i, line in enumerate(lines):
        low = line.strip().lower()
        if words >= TAIL_MIN_WORDS and (low in TAIL_LINES or low.startswith(TAIL_PREFIXES)):
            return "\n".join(lines[:i]).rstrip()
        words += len(line.split())
    return text


def looks_like_bot_screen(text: str) -> bool:
    low = text[:2000].lower()
    return any(p in low for p in BOT_SCREENS)


def classify(title, text) -> tuple[str, int]:
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    words = len(text.split())
    if looks_like_bot_screen(text):
        return "botscreen", words
    if words < MIN_WORDS:
        return "thin", words
    if words < PAYWALL_MAX_WORDS:
        low = text.lower()
        if any(m in low for m in PAYWALL_MARKERS):
            return "thin", words
    return "ok", words


# ── Wayback ───────────────────────────────────────────────────────────────────

class Wayback:
    """Serial, slow, and unapologetic about it.

    8 seconds between calls with a longer pause every fifth, plus exponential
    backoff on 403. archive.org has rate-limited this machine before; the cost
    of being impatient is losing access to the only copy of the dead links.
    """
    def __init__(self, delay=8.0, burst=5, burst_pause=20.0):
        self.delay, self.burst, self.burst_pause = delay, burst, burst_pause
        self._n, self._last, self._403, self._until = 0, 0.0, 0, 0.0

    def wait(self):
        if time.time() < self._until:
            time.sleep(self._until - time.time())
        self._n += 1
        gap = time.time() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap + random.uniform(0, 0.5))
        if self._n % self.burst == 0:
            time.sleep(self.burst_pause)
        self._last = time.time()

    def penalise(self):
        self._403 += 1
        self._until = time.time() + min(600, 60 * self._403)
        print(f"    [!] archive.org 403 — backing off "
              f"{int(self._until - time.time())}s", flush=True)

    def reset(self):
        self._403 = 0

    def snapshot(self, sess: requests.Session, url: str) -> str | None:
        self.wait()
        try:
            r = sess.get(f"https://archive.org/wayback/available?url={quote_plus(url)}",
                         timeout=15)
            if r.status_code == 200:
                self.reset()
                snap = (r.json().get("archived_snapshots") or {}).get("closest") or {}
                if snap.get("available") and str(snap.get("status")) == "200":
                    return snap["url"]
                return None                      # genuinely not archived
            if r.status_code == 403:
                self.penalise()
                return None
        except Exception:
            pass
        # Availability API missed or errored; CDX is slower but more thorough.
        self.wait()
        try:
            r = sess.get("https://web.archive.org/cdx/search/cdx"
                         f"?url={quote_plus(url)}&output=json&limit=5"
                         "&fl=timestamp,original&filter=statuscode:200", timeout=25)
            if r.status_code == 200:
                self.reset()
                rows = [x for x in json.loads(r.text.strip() or "[]")
                        if len(x) == 2 and x[0] != "timestamp"]
                if rows:
                    return f"https://web.archive.org/web/{rows[-1][0]}/{rows[-1][1]}"
            elif r.status_code == 403:
                self.penalise()
        except Exception:
            pass
        return None

    def text(self, sess: requests.Session, snap_url: str):
        # The id_ suffix asks Wayback for the original bytes without its own
        # toolbar and rewriting, which otherwise ends up in the extracted text.
        raw = re.sub(r"(/web/\d+)(/)", r"\1id_\2", snap_url)
        for attempt in (raw, snap_url):
            self.wait()
            try:
                r = sess.get(attempt, timeout=30)
                if r.status_code == 200 and r.text:
                    return r.text, attempt
                if r.status_code == 403:
                    self.penalise()
            except Exception:
                continue
        return None, None


# ── direct ────────────────────────────────────────────────────────────────────

def fetch_direct(sess: requests.Session, url: str):
    try:
        r = sess.get(url, timeout=(8, 20), allow_redirects=True)
        return (r.text if r.status_code == 200 else None), str(r.status_code)
    except Exception as e:
        return None, type(e).__name__.lower()[:20]


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="xbookmarks.db")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3, help="direct phase only")
    ap.add_argument("--delay", type=float, default=0.4, help="direct phase pacing")
    ap.add_argument("--wayback-delay", type=float, default=8.0)
    ap.add_argument("--phase", choices=["direct", "wayback", "both"], default="both")
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db, check_same_thread=False, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()

    keep = "" if args.retry_failed else \
        "AND a.url IS NULL"
    if args.retry_failed:
        keep = "AND (a.url IS NULL OR a.status NOT IN ('ok','skipped'))"

    todo = db.execute(f"""
        SELECT DISTINCT COALESCE(l.resolved_url, l.expanded_url) AS u,
               MAX(l.meta_status) AS ms
        FROM links l
        LEFT JOIN articles a ON a.url = COALESCE(l.resolved_url, l.expanded_url)
        JOIN tweets t ON t.id = l.tweet_id AND t.deleted = 0
        WHERE COALESCE(l.resolved_url, l.expanded_url) LIKE 'http%' {keep}
        GROUP BY u
    """).fetchall()

    direct_set, wayback_set, skipped, videos = [], [], [], []
    for u, ms in todo:
        if is_video(u):
            videos.append(u)
        elif ms == "nothtml" or not_article(u):
            skipped.append(u)
        elif ms == "ok":
            direct_set.append(u)
        else:
            wayback_set.append(u)          # blocked, gone, or never checked

    if args.limit:
        direct_set = direct_set[:args.limit]
        wayback_set = wayback_set[:max(0, args.limit - len(direct_set))]

    for u in skipped:
        db.execute("""INSERT OR REPLACE INTO articles
                      (url,status,source,fetched_at) VALUES (?,'skipped','',?)""",
                   (u, _utcnow()))
    for u in videos:
        db.execute("""INSERT OR REPLACE INTO articles
                      (url,status,source,fetched_at) VALUES (?,'video','',?)""",
                   (u, _utcnow()))
    db.commit()
    if videos:
        print(f"  {len(videos)} video URLs marked 'video' and not fetched")

    print(f"to archive: {len(direct_set)} direct, {len(wayback_set)} via Wayback"
          f"{f', {len(skipped)} skipped as non-article' if skipped else ''}")

    lock = threading.Lock()
    counts = {"ok": 0, "botscreen": 0, "thin": 0, "failed": 0}
    pace_lock, last = threading.Lock(), [0.0]
    started = time.time()

    # A retry must never make a row worse. The Wayback phase re-tries every
    # direct row that is not "ok", and on 14 Sep it overwrote 102 "thin" rows
    # and 4 "ok" rows with "failed", deleting 32,000 words of stored text
    # (restored from a snapshot). Same rule as capture.py: keep the better copy.
    RANK = {"ok": 3, "thin": 2, "botscreen": 1}

    def store(url, title, text, source, snap, http, status, words):
        with lock:
            old = db.execute("SELECT status, COALESCE(word_count, 0) FROM articles WHERE url = ?",
                             (url,)).fetchone()
            if old:
                was, now = RANK.get(old[0], 0), RANK.get(status, 0)
                if was > now or (was == now > 0 and old[1] >= words):
                    db.execute("UPDATE links SET archived_at=? WHERE COALESCE(resolved_url,expanded_url)=?",
                               (_utcnow(), url))
                    counts[status if status in counts else "failed"] += 1
                    db.commit()
                    return
            db.execute("""INSERT OR REPLACE INTO articles
                (url,title,text,word_count,source,snap_url,http_status,status,fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (url, title, text, words, source, snap, http, status, _utcnow()))
            db.execute("""UPDATE links SET archived_at=?, archive_url=?, archive_status=?
                          WHERE COALESCE(resolved_url,expanded_url)=?""",
                       (_utcnow(), snap or url, status, url))
            counts[status if status in counts else "failed"] += 1
            db.commit()

    # ── phase 1: direct, concurrent ───────────────────────────────────────────
    if args.phase in ("direct", "both") and direct_set:
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Accept-Language": "en,el;q=0.8"})

        def do_direct(url):
            with pace_lock:
                w = last[0] + args.delay - time.time()
                if w > 0:
                    time.sleep(w)
                last[0] = time.time()
            html, http = fetch_direct(sess, url)
            if not html:
                store(url, None, "", "direct", None, http, "failed", 0)
                return
            title, text = extract(html)
            text = text[:MAX_CHARS]
            status, words = classify(title, text)
            # Thin text is kept: a paywall teaser's lede is real and searchable.
            store(url, title, text if status in ("ok", "thin") else "", "direct",
                  None, http, status, words)

        print(f"\nphase 1 — direct ({args.workers} workers)")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for i, _ in enumerate(pool.map(do_direct, direct_set), 1):
                if i % 50 == 0 or i == len(direct_set):
                    rate = i / max(time.time() - started, 1)
                    print(f"  {i}/{len(direct_set)}  ok={counts['ok']} "
                          f"bot={counts['botscreen']} thin={counts['thin']} "
                          f"fail={counts['failed']}  eta "
                          f"{(len(direct_set)-i)/max(rate,.01)/60:.0f} min", flush=True)

        # Anything the direct tier could not turn into an article is worth one
        # Wayback attempt: a bot screen or a 404 is exactly what it is for.
        retry = [r[0] for r in db.execute(
            "SELECT url FROM articles WHERE source='direct' AND status!='ok'")]
        wayback_set.extend(u for u in retry if u not in wayback_set)
        print(f"  {len(retry)} direct failures added to the Wayback queue")

    # ── phase 2: Wayback, serial ──────────────────────────────────────────────
    if args.phase in ("wayback", "both") and wayback_set:
        refs = {r[0]: r[1] for r in db.execute('''
            SELECT COALESCE(resolved_url, expanded_url) u, COUNT(*) n
            FROM links GROUP BY u''')}
        # Most-referenced first. At ~16s per link this run takes hours, and it
        # should be interruptible at any point without having wasted the time
        # on the long tail.
        wayback_set.sort(key=lambda u: -refs.get(u, 0))
        wb = Wayback(delay=args.wayback_delay)
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA})
        n = len(wayback_set)
        eta = n * args.wayback_delay * 2 / 60
        print(f"\nphase 2 — Wayback, {n} links, serial at {args.wayback_delay}s "
              f"(~{eta:.0f} min minimum)")
        for i, url in enumerate(wayback_set, 1):
            snap = wb.snapshot(sess, url)
            if not snap:
                store(url, None, "", "wayback", None, "nosnapshot", "failed", 0)
            else:
                html, used = wb.text(sess, snap)
                if not html:
                    store(url, None, "", "wayback", snap, "unfetchable", "failed", 0)
                else:
                    title, text = extract(html)
                    text = text[:MAX_CHARS]
                    status, words = classify(title, text)
                    store(url, title, text if status in ("ok", "thin") else "",
                          "wayback", used, "200", status, words)
            if i % 10 == 0 or i == n:
                el = time.time() - started
                print(f"  {i}/{n}  ok={counts['ok']} fail={counts['failed']}  "
                      f"elapsed {el/60:.0f} min", flush=True)

    print(f"\ndone in {(time.time()-started)/60:.1f} min")
    for s, c in db.execute(
            "SELECT status, COUNT(*) FROM articles GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {c:>5}  {s}")
    words = db.execute("SELECT COALESCE(SUM(word_count),0) FROM articles").fetchone()[0]
    print(f"\n  {words:,} words archived")
    db.close()


if __name__ == "__main__":
    main()
