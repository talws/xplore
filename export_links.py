"""
export_links.py — the outbound links, joined to the tweets that pointed at them.

On "should this be a database or a file": it is already a database. These links
live in xbookmarks.db with their provenance intact, and a second store would be
a copy that drifts. So this creates a SQL VIEW -- the joined shape, queryable in
place with any SQLite tool -- and writes CSV and HTML as *artifacts*, which can
be regenerated at any time and are never the source of truth.

CSV is the working format: one row per link-and-tweet pair, so nothing is lost
when several tweets point at the same article, and a spreadsheet can pivot it.
HTML is the reading format: grouped by article, every referring tweet nested
underneath, links clickable.

    python export_links.py                  # both, next to the database
    python export_links.py --format csv
    python export_links.py --out D:/links
"""

from __future__ import annotations

import argparse
import csv
import datetime
import html
import os
import sqlite3
from collections import defaultdict

# One row per (link, tweet). COALESCE(resolved_url, expanded_url) everywhere:
# the resolved destination is the real identity of a link, and two t.co codes
# for the same article must collapse to one.
VIEW_SQL = """
CREATE VIEW IF NOT EXISTS link_export AS
SELECT
    COALESCE(l.resolved_url, l.expanded_url)      AS article_url,
    MAX(NULLIF(l.title, ''))                      AS article_title,
    MAX(l.article_date)                           AS article_date,
    t.url                                         AS tweet_url,
    t.created_at                                  AS tweet_date,
    t.author_handle                               AS tweet_author,
    COALESCE(tp.topic_user, tp.topic)             AS topic,
    substr(replace(replace(t.text, char(10), ' '), char(13), ' '), 1, 200) AS tweet_text,
    MIN(l.expanded_url)                           AS link_as_posted,
    MAX(l.archive_url)                            AS archive_url,
    MAX(l.archive_status)                         AS archive_status
FROM links l
JOIN tweets t ON t.id = l.tweet_id
LEFT JOIN topics tp ON tp.tweet_id = t.id
WHERE t.deleted = 0
  AND COALESCE(l.resolved_url, l.expanded_url) IS NOT NULL
-- One row per tweet-and-article, not per stored link row. The same article can
-- reach us twice from one tweet -- once through the URL entities and once
-- through the link card -- and after resolution both name the same
-- destination. 2,093 pairs were affected. MAX() over the title keeps the card's
-- version, which is the one that actually has a title.
GROUP BY article_url, t.id;
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    """Add article_date if it is not there, then (re)build the view.

    article_date is empty for now: no publication date exists anywhere in a
    tweet payload, so it can only come from fetching the article itself. The
    column exists so the export's shape does not change once it is filled.
    """
    cols = {r[1] for r in db.execute("PRAGMA table_info(links)")}
    if "article_date" not in cols:
        db.execute("ALTER TABLE links ADD COLUMN article_date TEXT")
    if "fetched_meta_at" not in cols:
        db.execute("ALTER TABLE links ADD COLUMN fetched_meta_at TEXT")
    db.execute("DROP VIEW IF EXISTS link_export")
    db.executescript(VIEW_SQL)
    db.commit()


def rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    return db.execute(
        "SELECT * FROM link_export ORDER BY tweet_date DESC, article_url"
    ).fetchall()


COLUMNS = ["article_title", "article_date", "article_url",
           "tweet_url", "tweet_date", "topic",
           "tweet_author", "tweet_text", "link_as_posted",
           "archive_url", "archive_status"]


def write_csv(data, path: str) -> None:
    # utf-8-sig so Excel opens Greek and emoji correctly instead of mojibake.
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in data:
            w.writerow([r[c] if r[c] is not None else "" for c in COLUMNS])


def write_html(data, path: str) -> None:
    """Grouped by article, with every referring tweet underneath."""
    groups: dict[str, list] = defaultdict(list)
    for r in data:
        groups[r["article_url"]].append(r)

    # Most-referenced first: if forty bookmarks point at one piece, that is the
    # single most useful thing this export can tell you.
    ordered = sorted(groups.items(),
                     key=lambda kv: (-len(kv[1]), kv[0]))

    esc = html.escape
    out = []
    for url, refs in ordered:
        first = refs[0]
        title = first["article_title"] or url
        date = first["article_date"] or ""
        topics = sorted({r["topic"] for r in refs if r["topic"]})
        host = url.split("://")[-1].split("/")[0].removeprefix("www.")
        out.append(f"""<article data-t="{esc(' '.join(topics)).lower()}"
 data-s="{esc((title + ' ' + url + ' ' + ' '.join(r['tweet_text'] or '' for r in refs)).lower())}">
<h2><a href="{esc(url)}" target="_blank" rel="noopener">{esc(title)}</a></h2>
<div class="meta"><span class="host">{esc(host)}</span>
{f'<span class="date">{esc(date[:10])}</span>' if date else '<span class="no">no date</span>'}
{'' if first['article_title'] else '<span class="no">no title</span>'}
<span class="refs">{len(refs)} tweet{'s' if len(refs) != 1 else ''}</span>
{''.join(f'<span class="tag">{esc(t)}</span>' for t in topics)}</div>
<ul>""")
        for r in refs:
            out.append(
                f'<li><a href="{esc(r["tweet_url"])}" target="_blank" rel="noopener">'
                f'@{esc(r["tweet_author"] or "?")}</a>'
                f'<span class="d">{esc((r["tweet_date"] or "")[:10])}</span>'
                f'<span class="tx">{esc(r["tweet_text"] or "")}</span></li>')
        out.append("</ul></article>")

    n_links, n_rows = len(ordered), len(data)
    titled = sum(1 for _, refs in ordered if refs[0]["article_title"])
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    doc = f"""<!doctype html><meta charset="utf-8">
<title>X bookmark links — {n_links}</title>
<style>
 :root{{color-scheme:light dark;--line:#8883;--dim:#8888}}
 body{{font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;margin:0 auto;
   padding:28px 22px;max-width:900px}}
 h1{{font-size:20px;margin:0 0 3px}}
 .sub{{color:var(--dim);font-size:13px;margin-bottom:18px}}
 #f{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}}
 input,select{{font:inherit;padding:7px 10px;border:1px solid var(--line);
   border-radius:8px;background:transparent;color:inherit}}
 input{{flex:1;min-width:220px}}
 article{{border-top:1px solid var(--line);padding:14px 0}}
 h2{{font-size:15px;margin:0 0 5px;font-weight:600;line-height:1.4}}
 a{{color:#3b82f6;text-decoration:none}} a:hover{{text-decoration:underline}}
 .meta{{font-size:12px;color:var(--dim);display:flex;gap:9px;flex-wrap:wrap;
   align-items:center;margin-bottom:7px}}
 .host{{font-weight:600;color:inherit;opacity:.75}}
 .no{{opacity:.5;font-style:italic}}
 .tag{{border:1px solid var(--line);border-radius:99px;padding:1px 8px}}
 .refs{{opacity:.8}}
 ul{{margin:0;padding-left:16px}}
 li{{font-size:13px;margin:3px 0;color:var(--dim)}}
 .d{{margin:0 8px;opacity:.75}}
 .tx{{opacity:.9}}
 footer{{color:var(--dim);font-size:12px;border-top:1px solid var(--line);
   margin-top:22px;padding-top:12px}}
</style>
<h1>X bookmark links</h1>
<div class="sub">{n_links} unique articles from {n_rows} bookmarked tweets ·
 {titled} have titles ({100*titled//max(n_links,1)}%) · generated {generated}</div>
<div id="f">
  <input id="q" placeholder="Filter by title, URL or tweet text…">
  <select id="t"><option value="">all topics</option></select>
</div>
<div id="list">{''.join(out)}</div>
<footer>Generated by export_links.py from xbookmarks.db. Article titles come
from X's link cards, so about half are missing; publication dates need the
article itself to be fetched.</footer>
<script>
const arts=[...document.querySelectorAll("article")];
const sel=document.getElementById("t");
[...new Set(arts.flatMap(a=>a.dataset.t.split(" ").filter(Boolean)))].sort()
  .forEach(t=>sel.add(new Option(t,t)));
function apply(){{
  const q=document.getElementById("q").value.toLowerCase().trim(), t=sel.value;
  arts.forEach(a=>{{
    a.hidden=(q&&!a.dataset.s.includes(q))||(t&&!a.dataset.t.includes(t));
  }});
}}
document.getElementById("q").addEventListener("input",apply);
sel.addEventListener("change",apply);
</script>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="xbookmarks.db")
    ap.add_argument("--out", default=".")
    ap.add_argument("--format", choices=["csv", "html", "both"], default="both")
    args = ap.parse_args()

    db = sqlite3.connect(args.db, timeout=30)
    ensure_schema(db)
    data = rows(db)
    os.makedirs(args.out, exist_ok=True)

    made = []
    if args.format in ("csv", "both"):
        p = os.path.join(args.out, "x-links.csv")
        write_csv(data, p)
        made.append(p)
    if args.format in ("html", "both"):
        p = os.path.join(args.out, "x-links.html")
        write_html(data, p)
        made.append(p)

    uniq = len({r["article_url"] for r in data})
    titled = len({r["article_url"] for r in data if r["article_title"]})
    print(f"{len(data)} rows, {uniq} unique articles, {titled} with titles")
    for p in made:
        print(f"  {p}  ({os.path.getsize(p)/1024:.0f} KB)")
    db.close()


if __name__ == "__main__":
    main()
