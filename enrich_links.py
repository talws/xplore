"""
enrich_links.py — fetch each linked article's title and publication date.

Deliberately *not* archiving. This reads the first chunk of each page, takes the
title and the published date out of the metadata, and throws the rest away. It
exists because half the links have no title and none have a date -- neither is
carried in a tweet payload -- which makes the export and the archive queue hard
to read.

Politeness matters less here than in resolve_links.py, because these are ~5,700
different hosts rather than repeated hits on t.co, but the same global pacer is
used so the total request rate stays modest either way.

Resumable: rows already carrying fetched_meta_at are skipped, so it can be
interrupted, and a later run only touches links that arrived since.

    python enrich_links.py                    # everything outstanding
    python enrich_links.py --limit 50         # a taste
    python enrich_links.py --retry-failed
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import html
import io
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

READ_BYTES = 200_000        # JSON-LD often sits well below <head>
_pace_lock = threading.Lock()
_last = [0.0]

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR = re.compile(r'(\w[\w:.-]*)\s*=\s*["\']([^"\']*)["\']', re.I)
_JSONLD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)

# Ordered by trustworthiness: a publisher's own article:published_time beats a
# generic "date" meta, which beats a <time> element that might be a comment.
DATE_KEYS = [
    "article:published_time", "og:article:published_time",
    "datepublished", "publishdate", "publication_date", "pubdate",
    "dc.date.issued", "dcterms.date", "sailthru.date",
    "parsely-pub-date", "date",
]


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc)\
                   .isoformat(timespec="seconds").replace("+00:00", "Z")


def _norm_date(v: str | None) -> str | None:
    """Reduce whatever the page said to YYYY-MM-DD, or give up.

    Publishers emit every format imaginable; anything that does not clearly
    yield a plausible date is dropped rather than guessed at, because a wrong
    date is worse than a missing one.
    """
    if not v:
        return None
    v = str(v).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", v)          # ISO, the common case
    if not m:
        m2 = re.search(r"(\d{4})/(\d{2})/(\d{2})", v)
        if m2:
            m = m2
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _from_jsonld(body: str) -> str | None:
    for m in _JSONLD.finditer(body):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                for k in ("datePublished", "dateCreated", "uploadDate"):
                    d = _norm_date(node.get(k))
                    if d:
                        return d
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return None


def extract(body: str) -> tuple[str | None, str | None]:
    """(title, YYYY-MM-DD) from a page's markup."""
    metas = []
    for tag in _META.finditer(body):
        attrs = {k.lower(): v for k, v in _ATTR.findall(tag.group(0))}
        key = (attrs.get("property") or attrs.get("name") or
               attrs.get("itemprop") or "").lower()
        if key:
            metas.append((key, attrs.get("content", "")))
    lookup = dict(metas)

    # og:title is usually the headline; <title> often carries site furniture.
    title = lookup.get("og:title") or lookup.get("twitter:title")
    if not title:
        m = _TITLE.search(body)
        title = m.group(1) if m else None
    if title:
        title = html.unescape(re.sub(r"\s+", " ", title)).strip()[:300] or None

    date = None
    for k in DATE_KEYS:
        if k in lookup:
            date = _norm_date(lookup[k])
            if date:
                break
    if not date:
        date = _from_jsonld(body)
    if not date:
        m = re.search(r'<time[^>]+datetime=["\']([^"\']+)["\']', body, re.I)
        if m:
            date = _norm_date(m.group(1))
    return title, date


def fetch(url: str, timeout: float = 15.0) -> tuple[str | None, str | None, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en,el;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "xml" not in ctype:
                return None, None, "nothtml"
            raw = r.read(READ_BYTES)
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                except Exception:
                    pass          # truncated gzip: fall through and decode best-effort
            body = raw.decode("utf-8", "replace")
            t, d = extract(body)
            return t, d, "ok"
    except urllib.error.HTTPError as e:
        return None, None, f"http{e.code}"
    except Exception as e:
        return None, None, type(e).__name__.lower()[:20]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="xbookmarks.db")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db, check_same_thread=False, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    cols = {r[1] for r in db.execute("PRAGMA table_info(links)")}
    for name in ("article_date", "fetched_meta_at", "meta_status"):
        if name not in cols:
            db.execute(f"ALTER TABLE links ADD COLUMN {name} TEXT")
    db.commit()

    cond = "fetched_meta_at IS NULL" if not args.retry_failed \
        else "(fetched_meta_at IS NULL OR meta_status != 'ok')"
    urls = [r[0] for r in db.execute(
        f"""SELECT DISTINCT COALESCE(resolved_url, expanded_url) FROM links
            WHERE {cond} AND COALESCE(resolved_url, expanded_url) LIKE 'http%'""").fetchall()]
    if args.limit:
        urls = urls[:args.limit]
    total = len(urls)
    if not total:
        print("nothing to enrich")
        return
    print(f"fetching metadata for {total} articles "
          f"({args.workers} workers, >={args.delay}s between requests)")

    lock = threading.Lock()
    done, got_title, got_date = [0], [0], [0]
    started = time.time()

    def pace():
        with _pace_lock:
            wait = _last[0] + args.delay - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()

    def work(url: str):
        pace()
        title, date, status = fetch(url)
        with lock:
            # Only fill a title we do not already have: X's card title is
            # generally the publisher's own headline, and overwriting it with a
            # <title> full of site furniture would be a downgrade.
            db.execute("""
                UPDATE links
                   SET title = CASE WHEN COALESCE(title,'') = '' THEN COALESCE(?, title)
                                    ELSE title END,
                       article_date = COALESCE(?, article_date),
                       fetched_meta_at = ?, meta_status = ?
                 WHERE COALESCE(resolved_url, expanded_url) = ?
            """, (title, date, _utcnow(), status, url))
            done[0] += 1
            if title:
                got_title[0] += 1
            if date:
                got_date[0] += 1
            n = done[0]
            if n % 50 == 0 or n == total:
                db.commit()
                rate = n / max(time.time() - started, 1)
                print(f"  {n}/{total}  titles={got_title[0]} dates={got_date[0]}  "
                      f"{rate:.1f}/s  eta {(total-n)/max(rate,0.01)/60:.0f} min", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, urls))
    db.commit()

    print(f"\ndone in {(time.time()-started)/60:.1f} min")
    for status, c in db.execute(
            "SELECT meta_status, COUNT(DISTINCT COALESCE(resolved_url,expanded_url)) "
            "FROM links WHERE meta_status IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8"):
        print(f"  {c:>5}  {status}")
    db.close()


if __name__ == "__main__":
    main()
