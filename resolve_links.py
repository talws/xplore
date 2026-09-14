"""
resolve_links.py — follow shortened URLs to their real destinations.

36% of captured links are bare t.co, and several hundred more sit behind
trib.al / bit.ly / reut.rs / buff.ly. Until they are resolved, deduplication is
broken -- the same article bookmarked twice arrives under two different t.co
codes and counts as two links -- and archiving would fetch the same page
repeatedly.

Deliberately slow. The owner's overriding constraint is not being locked out,
and nothing here is urgent: it is a background pass over a fixed backlog, so it
runs at a few requests per second with a small pool rather than saturating
anything. t.co in particular is Twitter infrastructure and worth treating
gently even though redirect resolution is unauthenticated.

Resumable by construction: each result is written as it arrives and resolved
rows are skipped on the next run, so it can be interrupted at any point.

    python resolve_links.py                 # resolve everything outstanding
    python resolve_links.py --limit 200     # a taste
    python resolve_links.py --workers 2 --delay 0.5
"""

from __future__ import annotations

import argparse
import datetime
import re
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import urls

# Hosts whose whole purpose is to redirect. youtu.be is included because
# resolving it to youtube.com/watch?v=... makes the same video dedupe.
SHORTENERS = {
    "t.co", "bit.ly", "trib.al", "reut.rs", "buff.ly", "aje.io", "ow.ly",
    "dlvr.it", "ift.tt", "goo.gl", "tinyurl.com", "youtu.be", "wapo.st",
    "nyti.ms", "on.ft.com", "econ.st", "apne.ws", "huff.to", "cnn.it",
    "bbc.in", "gu.com", "politi.co", "hill.cm", "cbsn.ws", "nbcnews.to",
    "abcn.ws", "usat.ly", "lat.ms", "bloom.bg", "reuters.tv", "spon.de",
    "sz.de", "zpr.io", "shar.es", "flip.it", "j.mp", "wp.me", "fb.me",
    "nyer.cm", "theatln.tc", "s.nikkei.com", "mol.im", "dailym.ai",
    # Found missing when 22 on.wsj.com links surfaced in the unrecovered
    # worklist still wearing their shortener.
    "on.wsj.com", "a.msn.com", "apple.news", "n.pr", "npr.org/l",
    "cnb.cx", "for.tn", "bbc.co.uk/l", "gizmo.do", "engt.co", "tcrn.ch",
    "vrge.co", "arstechnica.com/l", "cs.pn", "axios.link", "tgam.ca",
    "smh.re", "thetim.es", "ind.pn", "metr.o", "es.pn", "nyp.st",
    # Found on the unrecovered worklist on 2026-09-14, never attempted because
    # they were not listed: 38 ft.trib.al, 47 aje.news, 26 pulse.ly and more.
    "aje.news", "pulse.ly", "ti.me", "ara.tv", "eurac.tv", "mdn.tv", "scim.ag",
    "u.afp.com", "xhtxs.cn", "interc.pt", "pewrsr.ch", "go.nature.com",
    "link.europa.eu", "abcnews.link", "go.nybooks.com", "l.humanite.fr",
    "go.shr.lc", "go.france24.com", "dozz.es", "jrnl.ie", "l.euronews.com",
    "l.leparisien.fr", "l.franceinfo.fr", "to.pbs.org", "fam.ag", "amn.st",
    "shorturl.at", "uni.cf", "cutt.ly", "v.aa.com.tr", "omgwh.at", "oceana.ly",
    "go.icj-cij.org", "visitlink.me", "propub.li", "vk.cc", "to.welt.de",
    "twp.ai", "ndnco.cc", "ab.co", "lnkd.in",
}
_SHORT_HOSTS = frozenset(s for s in SHORTENERS if "/" not in s)
_SHORT_PATHS = tuple(s + "/" for s in SHORTENERS if "/" in s)

# urllib verifies against the Windows certificate store, which on this machine
# cannot build bit.ly's chain: every one of 87 bit.ly links failed with
# CERTIFICATE_VERIFY_FAILED and was recorded as "urlerror". certifi's bundle
# is the one requests already uses here.
try:
    import certifi
    _TLS = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _TLS = ssl.create_default_context()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

_print_lock = threading.Lock()
_pace_lock = threading.Lock()
_last_request = [0.0]


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc)\
                   .isoformat(timespec="seconds").replace("+00:00", "Z")


def host_of(url: str) -> str:
    try:
        h = url.split("://", 1)[-1].split("/", 1)[0].split("?")[0]
        return h.lower().removeprefix("www.")
    except Exception:
        return ""


def is_shortener(url: str) -> bool:
    """Listed host, a subdomain of one, or a listed host/path prefix.

    Exact host matching missed 38 ft.trib.al links (trib.al is listed) and
    could never match the "npr.org/l" style entries at all.
    """
    host = host_of(url)
    parts = host.split(".")
    if any(".".join(parts[i:]) in _SHORT_HOSTS for i in range(len(parts) - 1)):
        return True
    rest = url.split("://", 1)[-1].lower().removeprefix("www.")
    return rest.startswith(_SHORT_PATHS)


def migrate(db: sqlite3.Connection) -> None:
    """Add the resolution columns if they are not there yet.

    A new column rather than rewriting expanded_url in place: that column is
    half of the links primary key, and the original short form is worth
    keeping -- it is what the tweet actually contained.
    """
    cols = {r[1] for r in db.execute("PRAGMA table_info(links)")}
    for name, decl in (("resolved_url", "TEXT"),
                       ("resolve_status", "TEXT"),
                       ("resolved_at", "TEXT")):
        if name not in cols:
            db.execute(f"ALTER TABLE links ADD COLUMN {name} {decl}")
    db.execute("CREATE INDEX IF NOT EXISTS idx_links_resolved ON links(resolved_url)")
    db.commit()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the first hop and read Location ourselves.

    Following automatically would work, but hop-by-hop lets us cap the chain,
    avoid downloading a body, and see exactly where a shortener points even
    when the destination is slow or hostile.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Redirect(newurl)


class _Redirect(Exception):
    def __init__(self, url):
        self.url = url


# t.co does NOT reply with a 3xx. It returns HTTP 200 and an HTML stub whose
# destination sits in a meta-refresh, a <title>, and a JS location.replace().
# Verified directly against a live t.co URL -- a redirect-only resolver marks
# every one of them "resolved to itself", which is what the first pass did.
_META_REFRESH = re.compile(
    r"""http-equiv=["']?refresh["']?[^>]*?content=["']?[^"'>]*?url\s*=\s*([^"'>\s]+)""",
    re.I)
_JS_REPLACE = re.compile(r"""location\.replace\(\s*["']([^"']+)["']\s*\)""", re.I)


def _sniff_destination(body: str) -> str | None:
    """Pull the target out of a shortener's HTML stub, if there is one."""
    for rx in (_META_REFRESH, _JS_REPLACE):
        m = rx.search(body)
        if m:
            # JS string literals escape the slashes: https:\/\/example.com
            dest = m.group(1).replace("\\/", "/").strip()
            # The destination is read out of an HTML attribute, where "&" is
            # written "&amp;". Not unescaping it stored 252 URLs with
            # parameter names like "amp;utm_campaign". Only the ampersand is
            # unescaped: html.unescape would also turn "&section=" into
            # "§ion=", because it reads "&sect" as an HTML entity.
            dest = urls.unescape_amp(dest)
            if dest.startswith("http"):
                return dest
    return None


def resolve_one(url: str, timeout: float = 12.0, max_hops: int = 6) -> tuple[str | None, str]:
    """Return (final_url, status). Never raises.

    GET rather than HEAD: a HEAD of t.co returns 200 with no body, which hides
    the very thing we came for. Only the first few KB are read, so the cost is
    a request, not a download.
    """
    opener = urllib.request.build_opener(_NoRedirect,
                                         urllib.request.HTTPSHandler(context=_TLS))
    current = url
    for _ in range(max_hops):
        req = urllib.request.Request(
            current, method="GET",
            headers={"User-Agent": UA, "Accept": "text/html,*/*"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "html" not in ctype:
                    return current, "ok"           # a real file; chain ends
                raw = resp.read(4096)
                body = raw.decode("utf-8", "replace")
                dest = _sniff_destination(body)
                if dest and dest != current:
                    current = dest
                    continue                       # one more hop
                return current, "ok"
        except _Redirect as r:
            nxt = r.url
            if not nxt.startswith("http"):
                from urllib.parse import urljoin
                nxt = urljoin(current, nxt)
            current = nxt
            continue
        except urllib.error.HTTPError as e:
            # A dead destination still tells us where the shortener pointed,
            # which is worth keeping: archive.org may well have it.
            return (current if current != url else None), f"http{e.code}"
        except Exception as e:
            return (current if current != url else None), \
                   type(e).__name__.lower()[:20]
    return current, "ok" if current != url else "maxhops"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="xbookmarks.db")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2,
                    help="kept small on purpose; this is a background pass")
    ap.add_argument("--delay", type=float, default=0.4,
                    help="minimum seconds between requests, globally")
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db, check_same_thread=False, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout = 30000")
    migrate(db)

    # A retry only revisits links that never got past the shortener. A link
    # whose destination answered 403 already knows where it points; asking
    # the publisher again would learn nothing.
    where = "resolved_url IS NULL" if not args.retry_failed else \
            "(resolved_url IS NULL OR (resolve_status != 'ok' AND resolved_url = expanded_url))"
    rows = db.execute(
        f"SELECT DISTINCT expanded_url FROM links WHERE {where} AND expanded_url IS NOT NULL"
    ).fetchall()
    todo = [r[0] for r in rows if is_shortener(r[0])]
    if args.limit:
        todo = todo[:args.limit]

    total = len(todo)
    if not total:
        print("nothing to resolve")
        return
    print(f"resolving {total} shortened URLs "
          f"({args.workers} workers, >={args.delay}s between requests)")

    write_lock = threading.Lock()
    done = [0]
    started = time.time()

    def pace():
        """One global gate, so worker count and rate stay independent."""
        with _pace_lock:
            wait = _last_request[0] + args.delay - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.time()

    def work(url: str):
        pace()
        final, status = resolve_one(url)
        with write_lock:
            db.execute(
                "UPDATE links SET resolved_url=?, resolve_status=?, resolved_at=? "
                "WHERE expanded_url=?",
                (final or url, status, _utcnow(), url))
            db.commit()
            done[0] += 1
            n = done[0]
            if n % 25 == 0 or n == total:
                rate = n / max(time.time() - started, 1)
                eta = (total - n) / max(rate, 0.01)
                with _print_lock:
                    print(f"  {n}/{total}  {rate:.1f}/s  eta {eta/60:.1f} min",
                          flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, todo))
    db.commit()

    ok = db.execute("SELECT COUNT(*) FROM links WHERE resolve_status='ok'").fetchone()[0]
    uniq = db.execute(
        "SELECT COUNT(DISTINCT COALESCE(resolved_url, expanded_url)) FROM links"
    ).fetchone()[0]
    before = db.execute("SELECT COUNT(DISTINCT expanded_url) FROM links").fetchone()[0]
    print(f"\ndone in {(time.time()-started)/60:.1f} min")
    print(f"  resolved ok      : {ok}")
    print(f"  unique links now : {uniq}  (was {before} before resolution)")
    db.close()


if __name__ == "__main__":
    main()
