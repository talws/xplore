"""
export_unrecovered.py — the articles still missing, with every route to them.

Same worklist as the Recover tab in Xplore: articles whose archiving failed,
came back as a paywall teaser, or hit a bot wall -- counted only where a live
bookmark still points at them, and dropping out as soon as they are recovered.
Regenerate the file to refresh it; the Recover tab itself is always live.

Each article gets, in order of usefulness:

  archive.today  -- the one that matters. Pass its security check and the
                    Xplore extension saves the text automatically (capture.js).
  Wayback        -- the snapshot history. Readable, but opening it captures
                    nothing: capture.js only runs on archive.today pages.
  title search   -- for articles that moved or were syndicated elsewhere.
  better route   -- where archive.today is the wrong tool: Substack's
                    open.substack.com redirector, Google Docs, and shorteners
                    that were never resolved to the real article.

Grouped by domain, because the work is per-publisher.

    python export_unrecovered.py
"""

from __future__ import annotations

import csv
import datetime
import html
import os
import re
import sqlite3
from collections import defaultdict
from urllib.parse import quote, urlsplit

import urls

WORKLIST_STATUSES = ("failed", "thin", "botscreen")

REASONS = {
    "http401": "hard paywall", "http402": "payment required", "http403": "bot-blocked",
    "http404": "gone (404)", "http406": "refused our request", "http410": "removed",
    "http429": "rate-limited", "http503": "unavailable", "urlerror": "host gone",
    "timeouterror": "timed out",
}
STATUS_REASON = {"thin": "paywall teaser", "botscreen": "bot wall"}

SHORTENERS = ("trib.al", "bit.ly", "buff.ly", "rb.gy", "share.google", "ow.ly",
              "dlvr.it", "tinyurl.com", "t.co", "goo.gl", "lnkd.in")


def host_of(url: str) -> str:
    return re.sub(r"^www\.", "", (urlsplit(url).hostname or "").lower())


def better_route(url: str) -> tuple[str | None, str | None]:
    """(url, label) when archive.today is not the best way to this article."""
    h, path = host_of(url), urlsplit(url).path
    m = re.match(r"^/pub/([^/]+)/p/([^/?#]+)", path)
    if h == "open.substack.com" and m:
        return f"https://{m.group(1)}.substack.com/p/{m.group(2)}", "direct Substack page"
    m = re.match(r"^/document/d/([\w-]+)", path)
    if h == "docs.google.com" and m:
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt", "Google Doc as text"
    if any(h == s or h.endswith("." + s) for s in SHORTENERS):
        return url, "unresolved shortener — open it, then use archive.today on the real address"
    return None, None


def main():
    db = sqlite3.connect("file:xbookmarks.db?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    marks = ",".join("?" * len(WORKLIST_STATUSES))

    rows = db.execute(f"""
        SELECT a.url, a.status,
               COALESCE(NULLIF(a.title, ''), MAX(NULLIF(l.title, ''))) AS title,
               MAX(l.article_date) AS adate,
               MAX(l.meta_status) AS ms,
               COUNT(DISTINCT l.tweet_id) AS refs
          FROM articles a
          JOIN links l ON COALESCE(l.resolved_url, l.expanded_url) = a.url
          JOIN tweets t ON t.id = l.tweet_id AND t.deleted = 0
         WHERE a.status IN ({marks})
         GROUP BY a.url
         ORDER BY refs DESC, a.url""", WORKLIST_STATUSES).fetchall()

    tweets: dict[str, list] = defaultdict(list)
    for r in db.execute(f"""
        SELECT DISTINCT COALESCE(l.resolved_url, l.expanded_url) AS u,
               t.url, t.author_handle, substr(COALESCE(t.created_at, ''), 1, 10) AS d
          FROM links l
          JOIN tweets t ON t.id = l.tweet_id AND t.deleted = 0
          JOIN articles a ON a.url = COALESCE(l.resolved_url, l.expanded_url)
         WHERE a.status IN ({marks})
         ORDER BY d DESC""", WORKLIST_STATUSES):
        if len(tweets[r["u"]]) < 3:
            tweets[r["u"]].append(r)

    recovered = db.execute("""SELECT COUNT(*) FROM articles
        WHERE status='ok' AND source IN ('archiveph','view')""").fetchone()[0]
    db.close()

    if not rows:
        print("nothing left to recover")
        return

    items = []
    for r in rows:
        url = r["url"]
        title = r["title"] if r["title"] and r["title"] != url else None
        route, route_label = better_route(url)
        items.append({
            "url": url, "original": urls.for_original(url), "title": title, "domain": host_of(url), "refs": r["refs"],
            "adate": r["adate"] or "",
            "reason": STATUS_REASON.get(r["status"]) or REASONS.get(r["ms"] or "", "not archived"),
            # Cleaned: archive.today matches addresses exactly, and tracking
            # parameters turned real snapshots into 404s.
            "archive_today": urls.archive_today(url),
            "wayback": urls.wayback(url),
            "search": ("https://duckduckgo.com/?q=" + quote(f'"{title}"')) if title else "",
            "route": route or "", "route_label": route_label or "",
            "tweets": tweets.get(url, []),
        })

    # One entry per page: the same article is often stored under several
    # addresses (http and https, www, host case, tracking junk). Recovering any
    # copy fills them all, so listing each copy would only inflate the work.
    merged: dict[str, dict] = {}
    for it in items:
        key = urls.match_key(it["url"])
        m = merged.get(key)
        if m is None:
            merged[key] = it
            continue
        m["refs"] += it["refs"]
        m["title"] = m["title"] or it["title"]
        m["adate"] = m["adate"] or it["adate"]
        seen = {t["url"] for t in m["tweets"]}
        m["tweets"] = (m["tweets"] + [t for t in it["tweets"] if t["url"] not in seen])[:3]
    items = sorted(merged.values(), key=lambda it: (-it["refs"], it["url"]))
    for it in items:
        if not it["search"] and it["title"]:
            it["search"] = "https://duckduckgo.com/?q=" + quote(f'"{it["title"]}"')

    by_domain: dict[str, list] = defaultdict(list)
    for it in items:
        by_domain[it["domain"]].append(it)
    ordered = sorted(by_domain.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    # ── CSV ──────────────────────────────────────────────────────────────────
    with open("x-links-unrecovered.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["title", "domain", "reason", "bookmarks", "article_date", "url",
                    "archive_today", "wayback", "better_route", "better_route_note", "first_tweet"])
        for it in items:
            w.writerow([it["title"] or "", it["domain"], it["reason"], it["refs"], it["adate"],
                        it["original"], it["archive_today"], it["wayback"], it["route"],
                        it["route_label"], it["tweets"][0]["url"] if it["tweets"] else ""])

    # ── HTML ─────────────────────────────────────────────────────────────────
    e = html.escape
    out = []
    for domain, its in ordered:
        out.append(f'<section data-d="{e(domain)}"><h2>{e(domain)} <span class="n">{len(its)}</span></h2>')
        for it in its:
            vias = " ".join(
                f'<a href="{e(t["url"])}" target="_blank" rel="noopener">@{e(t["author_handle"] or "?")}</a>'
                f'<span class="dd">{e(t["d"])}</span>' for t in it["tweets"])
            route = (f'<a class="alt" href="{e(it["route"])}" target="_blank" rel="noopener">'
                     f'{e(it["route_label"])}</a>') if it["route"] else ""
            out.append(f'''<div class="it" data-u="{e(it["url"])}" data-s="{e((it["title"] or "") + " " + it["url"]).lower()}">
  <div class="t"><a href="{e(it["original"])}" target="_blank" rel="noopener">{e(it["title"] or it["original"])}</a></div>
  <div class="m"><span class="r">{e(it["reason"])}</span>
    <span>{it["refs"]} bookmark{"s" if it["refs"] != 1 else ""}</span>
    {f'<span>{e(it["adate"])}</span>' if it["adate"] else ""}
    <span class="via">{vias}</span></div>
  <div class="go">
    <a class="primary" href="{e(it["archive_today"])}" target="_blank" rel="noopener">archive.today →</a>
    <a href="{e(it["wayback"])}" target="_blank" rel="noopener">Wayback</a>
    {f'<a href="{e(it["search"])}" target="_blank" rel="noopener">search title</a>' if it["search"] else ""}
    {route}
  </div>
</div>''')
        out.append("</section>")

    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    doc = f"""<!doctype html><meta charset="utf-8">
<title>Unrecovered articles — {len(items)}</title>
<style>
 :root{{color-scheme:light dark;--line:#8883;--dim:#8888;--accent:#3b82f6}}
 [hidden]{{display:none!important}}
 body{{font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0 auto;
   padding:26px 22px 60px;max-width:940px}}
 h1{{font-size:20px;margin:0 0 3px}}
 .sub{{color:var(--dim);font-size:13px;margin-bottom:12px}}
 .how{{font-size:13.5px;border:1px solid var(--line);border-radius:9px;padding:10px 13px;
   margin-bottom:14px}}
 .bar{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px}}
 input[type=search]{{font:inherit;padding:7px 10px;border:1px solid var(--line);
   border-radius:8px;background:transparent;color:inherit;flex:1;min-width:240px}}
 label{{font-size:13px;color:var(--dim)}}
 section{{border-top:1px solid var(--line);padding:12px 0}}
 h2{{font-size:14px;margin:0 0 6px}}
 .n{{color:var(--dim);font-weight:400}}
 .it{{padding:8px 0 10px;border-top:1px dashed #8882}}
 .it:first-of-type{{border-top:0}}
 .it.opened{{opacity:.5}}
 .t a{{color:inherit;text-decoration:none;font-weight:540}}
 .t a:hover{{text-decoration:underline}}
 .m{{font-size:12px;color:var(--dim);display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin:2px 0 5px}}
 .r{{border:1px solid #d97706;color:#d97706;border-radius:99px;padding:0 8px}}
 .via a{{color:var(--accent);text-decoration:none}}
 .dd{{margin:0 6px 0 3px;opacity:.7}}
 .go{{display:flex;gap:12px;flex-wrap:wrap;font-size:13px}}
 .go a{{color:var(--accent);text-decoration:none}}
 .go a:hover{{text-decoration:underline}}
 .go a.primary{{border:1px solid var(--accent);border-radius:7px;padding:1px 9px}}
 .go a.alt{{color:#16a34a}}
 footer{{color:var(--dim);font-size:12px;border-top:1px solid var(--line);margin-top:20px;padding-top:12px}}
</style>
<h1>Articles still to recover</h1>
<div class="sub">{len(items):,} articles across {len(ordered)} domains · {recovered} recovered so far ·
 generated {generated}</div>
<div class="how"><b>archive.today →</b> is the one that saves automatically: pass the security check
 and the Xplore extension stores the text. <b>Wayback</b> and <b>search title</b> are for reading
 only — nothing is captured from them. A green link is a better route than archive.today for
 that article.</div>
<div class="bar">
  <input type="search" id="q" placeholder="Filter by domain or title…">
  <label><input type="checkbox" id="hide"> hide opened</label>
</div>
<div id="list">{''.join(out)}</div>
<footer>Regenerate with <code>python export_unrecovered.py</code> to drop recovered articles; the
 Recover tab in Xplore (http://localhost:8502) is always current. "Opened" is remembered in this
 browser only.</footer>
<script>
const KEY = "xp_unrecovered_opened";
const load = () => {{ try {{ return new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); }} catch (e) {{ return new Set(); }} }};
const save = s => {{ try {{ localStorage.setItem(KEY, JSON.stringify([...s])); }} catch (e) {{}} }};
const opened = load();
const its = [...document.querySelectorAll(".it")], secs = [...document.querySelectorAll("section")];
its.forEach(el => {{ if (opened.has(el.dataset.u)) el.classList.add("opened"); }});
function apply() {{
  const v = document.getElementById("q").value.toLowerCase().trim();
  const hide = document.getElementById("hide").checked;
  secs.forEach(s => {{
    const dm = !v || s.dataset.d.includes(v);
    let any = false;
    s.querySelectorAll(".it").forEach(el => {{
      const show = (dm || el.dataset.s.includes(v)) && !(hide && el.classList.contains("opened"));
      el.hidden = !show; any = any || show;
    }});
    s.hidden = !any;
  }});
}}
document.getElementById("q").addEventListener("input", apply);
document.getElementById("hide").addEventListener("change", apply);
document.getElementById("list").addEventListener("click", e => {{
  const a = e.target.closest(".go a"); if (!a) return;
  const el = a.closest(".it"); opened.add(el.dataset.u); save(opened); el.classList.add("opened");
}});
</script>"""
    with open("x-links-unrecovered.html", "w", encoding="utf-8") as fh:
        fh.write(doc)

    routes = sum(1 for it in items if it["route"])
    print(f"{len(items):,} unrecovered articles across {len(ordered)} domains ({recovered} recovered so far)")
    print(f"  {routes} have a better route than archive.today")
    for f in ("x-links-unrecovered.html", "x-links-unrecovered.csv"):
        print(f"  {f}  ({os.path.getsize(f)/1024:.0f} KB)")
    for domain, its in ordered[:10]:
        print(f"    {len(its):>4}  {domain}")


if __name__ == "__main__":
    main()
