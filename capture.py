"""
capture.py — capture-on-view: recover articles a human has already unlocked.

About 1,100 linked articles could not be archived automatically -- paywalls,
bot walls, dead hosts. Many of them do exist on archive.today (archive.ph and
its mirrors), but that site sits behind a CAPTCHA, so no server-side fetcher
gets through. Verified 2026-09-13 with a real browser:

    archive.ph/newest/https://www.nytimes.com/live/2026/06/26/world/us-iran-strikes-hormuz
      -> 302 to archive.ph/20260627014806/https://www.nytimes.com/live/2026/06/26/...
      -> "One more step. Please complete the security check to access archive.ph"

Two consequences shape this module:

  * A snapshot exists and a human can pass the check, so the reliable route is
    to capture the page *after* the owner has opened it -- the same idea as
    Mneme's capture-on-visit, but gated on Xplore's own worklist rather than on
    browser bookmarks.
  * The snapshot URL carries the original URL in its path. That is what ties a
    captured page back to the bookmark it belongs to, with no dependence on
    archive.today's page markup.

The browser sends raw HTML; extraction and classification happen here, using
the same extract() and classify() as archive_links.py, so a captured page is
judged exactly like a fetched one -- a CAPTCHA page or a paywall teaser is
refused rather than stored as a success.

Short-id snapshot URLs (archive.ph/AbCdE) do not carry the original. For those
the header of the page is searched for it. That markup has NOT been observed
directly (every attempt met the CAPTCHA), so that fallback is best-effort and
the first real capture of a short-id page is its test.
"""

from __future__ import annotations

import datetime
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from flask import Blueprint, jsonify, request

from archive_links import MAX_CHARS, classify, extract
import urls

bp = Blueprint("capture", __name__)
_store = None

ARCHIVE_HOSTS = {"archive.ph", "archive.is", "archive.today", "archive.li",
                 "archive.fo", "archive.md", "archive.vn"}

# archive.ph/<14-digit timestamp>/<original>, or /newest/ and /oldest/ lookups.
_SNAPSHOT = re.compile(r"^https?://[^/]+/(?:\d{14}|newest|oldest)/(https?://.+)$", re.I)

# archive.today's own page furniture; the archived body is inside #CONTENT.
# Taken from Mneme's fetcher.py, where it ran against real snapshot pages.
_ARCHIVE_NOISE_IDS = ("HEADER", "FOOTER", "THUMBS-BLOCK", "SPONSORS", "ADBLOCK")

WORKLIST_STATUSES = ("failed", "thin", "botscreen")


def init(store) -> None:
    global _store
    _store = store


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc)\
                   .isoformat(timespec="seconds").replace("+00:00", "Z")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def normalise(url: str) -> str:
    """Comparison key -- see urls.match_key.

    Must strip at least what urls.for_archive() strips: the archive link is
    built from the cleaned address, archive.today files the snapshot under it,
    and matching that back to a stored URL still carrying "&amp;" and utm_*
    junk only works if both reduce to the same key.
    """
    return urls.match_key(url)


def original_from_page(page_url: str, html: str | None = None) -> str | None:
    """Which article does this archive page hold?"""
    if _host(page_url) not in ARCHIVE_HOSTS:
        return None
    m = _SNAPSHOT.match(page_url.split("#", 1)[0])
    if m:
        return m.group(1)
    if not html:
        return None
    # Short-id page: best-effort, see the module docstring.
    try:
        soup = BeautifulSoup(html, "html.parser")
        for sel in ("input#SHARE_LONGLINK", "input[name=q]"):
            el = soup.select_one(sel)
            if el and (el.get("value") or "").startswith("http"):
                v = el["value"]
                inner = _SNAPSHOT.match(v)
                return inner.group(1) if inner else (None if _host(v) in ARCHIVE_HOSTS else v)
        header = soup.find(id="HEADER")
        if header:
            for a in header.find_all("a", href=True):
                if a["href"].startswith("http") and _host(a["href"]) not in ARCHIVE_HOSTS:
                    return a["href"]
    except Exception:
        pass
    return None


def _match_article(original: str):
    """The articles row for this URL.

    Exact address first. Then the cleaned key with path case preserved -- short
    links and archive ids are case-sensitive, bit.ly/AbC is not bit.ly/abc. Only
    if that finds nothing, a fully lower-cased key, and only when it points at a
    single article, so a guess can never attach text to the wrong bookmark.

    The same page is often stored several times over (http and https, www, host
    case, tracking junk: 336 such groups). A strict-key match may therefore find
    several rows; that is not ambiguity, and capture() fills all of them.
    """
    db = _store.db
    row = db.execute("SELECT url, status, word_count FROM articles WHERE url = ?",
                     (original,)).fetchone()
    if row:
        return row
    rows = db.execute("SELECT url, status, word_count FROM articles").fetchall()
    for loose in (False, True):
        want = urls.match_key(original, loose=loose)
        hits = [r for r in rows if urls.match_key(r["url"], loose=loose) == want]
        if hits and (not loose or len(hits) == 1):
            return next((h for h in hits if h["status"] != "ok"), hits[0])
    return None


def _same_page(url: str) -> list[str]:
    """Every stored address that is this page, by strict key."""
    key = urls.match_key(url)
    return [r["url"] for r in _store.db.execute("SELECT url FROM articles")
            if urls.match_key(r["url"]) == key] or [url]


def _resolve(page: str, explicit_url: str | None, html: str | None = None):
    """(articles row or None, original url or None) for a page being viewed.

    The page address itself is tried first. Some bookmarks *are* archive links
    -- people share archive.ph/AbCdE on x.com -- and those rows are keyed by
    the archive URL, not by the article behind it. Two such rows had been
    "archived" as CAPTCHA pages; without this they could never be recovered,
    because the original derived from the page would never match them.
    """
    page = (page or "").split("#", 1)[0]
    if page:
        row = _match_article(page)
        if row:
            return row, page
    original = (explicit_url or "").strip() or original_from_page(page, html)
    if not original:
        return None, None
    return _match_article(original), original


def _strip_archive_chrome(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for nid in _ARCHIVE_NOISE_IDS:
            el = soup.find(id=nid)
            if el:
                el.decompose()
        content = soup.find(id="CONTENT") or soup.find(id="content")
        return str(content) if content else str(soup)
    except Exception:
        return html


# ── endpoints ─────────────────────────────────────────────────────────────────

@bp.route("/api/capture/status")
def capture_status():
    """Is the page being viewed one of ours, and do we still need its text?

    Asked before any HTML is sent, so an ordinary archive.today visit that has
    nothing to do with the collection costs one tiny request and nothing else.
    """
    page = (request.args.get("page") or "").strip()
    row, original = _resolve(page, request.args.get("url"))
    if not row:
        return jsonify({"ok": True, "known": False, "original": original,
                        "reason": None if original else "no original url in page address"})
    return jsonify({"ok": True, "known": True, "articleUrl": row["url"],
                    "status": row["status"], "words": row["word_count"] or 0,
                    "needs": row["status"] != "ok"})


@bp.route("/api/capture", methods=["POST"])
def capture():
    """Store the text of a page the owner has opened.

    Refuses anything classify() would not call an article, and never replaces a
    longer copy with a shorter one -- the same rule the collector follows.
    """
    page = (request.args.get("page") or "").strip()
    html = request.get_data(as_text=True) or ""
    if not page or not html:
        return jsonify({"error": "page and body required"}), 400

    row, original = _resolve(page, request.args.get("url"), html)
    if not row:
        return jsonify({"ok": True, "saved": False, "original": original,
                        "reason": "not a link in the collection" if original
                                  else "could not tell which article this is"})

    archived = _host(page) in ARCHIVE_HOSTS
    body = _strip_archive_chrome(html) if archived else html
    title, text = extract(body)
    text = text[:MAX_CHARS]
    status, words = classify(title, text)
    if status != "ok":
        # Most often: the CAPTCHA page itself, or a paywall teaser.
        return jsonify({"ok": True, "saved": False, "status": status, "words": words,
                        "reason": {"botscreen": "still a bot check",
                                   "thin": "too little text — probably a teaser"}.get(status, status)})

    if row["status"] == "ok" and (row["word_count"] or 0) >= words:
        return jsonify({"ok": True, "saved": False, "status": "ok", "words": row["word_count"],
                        "reason": "already have a copy at least as long"})

    now = _utcnow()
    db = _store.db
    # Fill every stored copy of this page, so recovering it once clears its
    # duplicates from the worklist too.
    for url in _same_page(row["url"]):
        db.execute("""UPDATE articles
                         SET title = COALESCE(NULLIF(?, ''), title), text = ?, word_count = ?,
                             source = ?, snap_url = ?, http_status = 'view',
                             status = 'ok', fetched_at = ?
                       WHERE url = ? AND (status != 'ok' OR COALESCE(word_count, 0) < ?)""",
                   (title or "", text, words, "archiveph" if archived else "view",
                    page, now, url, words))
        db.execute("""UPDATE links SET archive_status = 'ok', archived_at = ?, archive_url = ?
                       WHERE COALESCE(resolved_url, expanded_url) = ?""",
                   (now, page, url))
        db.commit()
    return jsonify({"ok": True, "saved": True, "status": "ok", "words": words,
                    "articleUrl": row["url"], "title": title})


@bp.route("/api/capture/worklist")
def capture_worklist():
    """Articles still missing, most-referenced first, each with its archive link."""
    db = _store.db
    limit = min(int(request.args.get("limit", 60)), 500)
    offset = int(request.args.get("offset", 0))
    domain = (request.args.get("domain") or "").strip().lower()
    marks = ",".join("?" * len(WORKLIST_STATUSES))

    rows = db.execute(f"""
        SELECT a.url, a.status, a.word_count,
               COALESCE(NULLIF(a.title, ''), MAX(NULLIF(l.title, ''))) AS title,
               MAX(l.article_date) AS article_date,
               MAX(l.meta_status) AS meta_status,
               COUNT(DISTINCT l.tweet_id) AS refs,
               MIN(t.url) AS tweet_url, MIN(t.author_handle) AS author
          FROM articles a
          JOIN links l ON COALESCE(l.resolved_url, l.expanded_url) = a.url
          JOIN tweets t ON t.id = l.tweet_id AND t.deleted = 0
         WHERE a.status IN ({marks})
         GROUP BY a.url
         ORDER BY refs DESC, a.url""", WORKLIST_STATUSES).fetchall()

    # One entry per page. Rows arrive most-referenced first, so the first copy
    # of a page is the one shown; its duplicates add their bookmark counts.
    groups: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        key = urls.match_key(d["url"])
        g = groups.get(key)
        if g is not None:
            g["refs"] += d["refs"]
            g["variants"] += 1
            g["title"] = g["title"] or d["title"]
            g["article_date"] = g["article_date"] or d["article_date"]
            continue
        d["domain"] = _host(d["url"])
        # Cleaned before it is used: archive.today matches addresses exactly, so
        # a link carrying tracking junk misses the snapshot. Verified 2026-09-13:
        # the clean NYT address redirected to its snapshot, the same address
        # plus NYT's share parameters returned 404.
        d["archiveLink"] = urls.archive_today(d["url"])
        d["originalLink"] = urls.for_original(d["url"])   # keeps gift-link tokens
        d["variants"] = 1
        groups[key] = d
    items = sorted(groups.values(), key=lambda d: (-d["refs"], d["url"]))
    domains: dict[str, int] = {}
    for it in items:
        domains[it["domain"]] = domains.get(it["domain"], 0) + 1
    if domain:
        items = [it for it in items if it["domain"] == domain]

    recovered = [dict(r) for r in db.execute("""
        SELECT url, title, word_count, source, fetched_at FROM articles
         WHERE status = 'ok' AND source IN ('archiveph', 'view')
         ORDER BY fetched_at DESC LIMIT 15""")]
    return jsonify({
        "total": len(items),
        "items": items[offset:offset + limit],
        "domains": sorted(domains.items(), key=lambda kv: -kv[1])[:24],
        "recovered": recovered,
        "recoveredTotal": db.execute("""SELECT COUNT(*) FROM articles
            WHERE status='ok' AND source IN ('archiveph','view')""").fetchone()[0],
    })
