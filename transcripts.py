"""
transcripts.py — fetch subtitles for the video links, so they become searchable.

151 of the bookmarked links are videos. The archiver marks them `status='video'`
and does not fetch them, because asking a video page for prose yields nothing.
But most of these videos *do* have a subtitle track, and a transcript is real
searchable text -- often the most quotable part of the bookmark.

Subtitles only. No audio is downloaded and Whisper is not involved: existing
tracks (author-uploaded or auto-generated) cover the majority, and downloading
video to transcribe it would be a different project with a different cost.

Stored in `articles` exactly like a fetched article, with `source='transcript'`,
so the same FTS index and the same UI surface it with no special cases.

Deliberately slow: one video at a time with a pause between, because this hits
YouTube from a residential IP and the owner's overriding constraint throughout
has been not getting locked out of anything.

    python transcripts.py --limit 5      # a taste
    python transcripts.py                # everything outstanding
    python transcripts.py --retry-failed
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import re
import sqlite3
import time

try:
    import yt_dlp
except ImportError:
    raise SystemExit("yt-dlp is not installed:  python -m pip install yt-dlp")

# Preference order. Greek and English are what this collection is in; the
# bare-language fallbacks catch regional variants like en-GB or el-GR.
LANGS = ["en", "en-orig", "en-US", "en-GB", "el", "el-GR"]

MIN_WORDS = 25          # shorter than this is a title card, not a transcript


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc)\
                   .isoformat(timespec="seconds").replace("+00:00", "Z")


def _from_json3(raw: str) -> str:
    """YouTube's json3 caption format -> plain text."""
    data = json.loads(raw)
    parts = []
    for ev in data.get("events") or []:
        for seg in ev.get("segs") or []:
            t = seg.get("utf8")
            if t:
                parts.append(t)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _from_vtt(raw: str) -> str:
    """WebVTT -> plain text, with the duplicate-rollup lines collapsed.

    Auto-generated VTT repeats each line as the caption scrolls, so a naive
    join produces the same sentence three or four times over. Dropping
    consecutive duplicates is what makes the result readable.
    """
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if (not line or line.startswith("WEBVTT") or "-->" in line
                or line.isdigit() or line.startswith(("NOTE", "Kind:", "Language:"))):
            continue
        line = re.sub(r"<[^>]+>", "", line)          # inline timing tags
        if out and out[-1] == line:
            continue
        out.append(line)
    # The rollup also repeats across lines, not just adjacently.
    deduped = []
    for line in out:
        if deduped and (line in deduped[-1] or deduped[-1] in line):
            if len(line) > len(deduped[-1]):
                deduped[-1] = line
            continue
        deduped.append(line)
    return re.sub(r"\s+", " ", " ".join(deduped)).strip()


def fetch_transcript(url: str) -> tuple[str | None, str, str]:
    """(title, text, status). Never raises."""
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "writesubtitles": False, "socket_timeout": 25,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        msg = str(e).lower()
        if "private" in msg:
            return None, "", "private"
        if "unavailable" in msg or "removed" in msg or "not exist" in msg:
            return None, "", "unavailable"
        if "sign in" in msg or "bot" in msg or "cookies" in msg:
            return None, "", "blocked"
        return None, "", type(e).__name__.lower()[:20]

    if not isinstance(info, dict):
        return None, "", "noinfo"
    title = info.get("title")

    tracks = {}
    tracks.update(info.get("subtitles") or {})
    # Author-provided tracks win over automatic ones, so they are merged second
    # only where a language is otherwise missing.
    for k, v in (info.get("automatic_captions") or {}).items():
        tracks.setdefault(k, v)
    if not tracks:
        return title, "", "nosubs"

    order = [l for l in LANGS if l in tracks] or list(tracks)[:1]
    import urllib.request
    for lang in order:
        for fmt in ("json3", "vtt", "srv1"):
            cand = [t for t in tracks[lang] if t.get("ext") == fmt]
            if not cand:
                continue
            try:
                req = urllib.request.Request(
                    cand[0]["url"], headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=25).read().decode(
                    "utf-8", "replace")
                text = _from_json3(raw) if fmt == "json3" else _from_vtt(raw)
                if len(text.split()) >= MIN_WORDS:
                    return title, text, "ok"
            except Exception:
                continue
    return title, "", "nosubs"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="xbookmarks.db")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--retry-failed", action="store_true")
    # Network-level failures only. Worth one more try because they were
    # transient on the first run; nosubs and private are not, and retrying
    # "blocked" invites YouTube to escalate.
    ap.add_argument("--retry-transient", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db, timeout=60)
    db.execute("PRAGMA busy_timeout = 60000")
    if args.retry_transient:
        cond = "status IN ('video', 'downloaderror', 'oserror')"
    elif args.retry_failed:
        cond = "status IN ('video', 'nosubs', 'blocked')"
    else:
        cond = "status = 'video'"
    urls = [r[0] for r in db.execute(
        f"SELECT url FROM articles WHERE {cond} ORDER BY url").fetchall()]
    if args.limit:
        urls = urls[:args.limit]
    if not urls:
        print("no videos outstanding")
        return

    print(f"fetching transcripts for {len(urls)} videos "
          f"(serial, {args.delay}s apart)")
    started, counts = time.time(), {}
    words_total = 0

    for i, url in enumerate(urls, 1):
        title, text, status = fetch_transcript(url)
        words = len(text.split())
        counts[status] = counts.get(status, 0) + 1
        words_total += words
        db.execute("""
            UPDATE articles
               SET title = COALESCE(?, title),
                   text = ?, word_count = ?,
                   source = 'transcript',
                   status = CASE WHEN ? = 'ok' THEN 'ok' ELSE ? END,
                   fetched_at = ?
             WHERE url = ?""",
            (title, text, words, status, status, _utcnow(), url))
        db.execute("UPDATE links SET archive_status = ? "
                   "WHERE COALESCE(resolved_url, expanded_url) = ?",
                   ("ok" if status == "ok" else status, url))
        # Commit every single row. Batching looked thriftier but each video
        # takes ~30s, so a five-row batch held the write lock for two and a
        # half minutes and the server could not record incoming bookmarks at
        # all. One row is a millisecond of lock.
        db.commit()
        if i % 5 == 0 or i == len(urls):
            rate = i / max(time.time() - started, 1)
            print(f"  {i}/{len(urls)}  ok={counts.get('ok',0)} "
                  f"nosubs={counts.get('nosubs',0)} "
                  f"gone={counts.get('unavailable',0)+counts.get('private',0)}  "
                  f"eta {(len(urls)-i)/max(rate,0.01)/60:.0f} min", flush=True)
        time.sleep(args.delay)

    db.commit()
    print(f"\ndone in {(time.time()-started)/60:.1f} min")
    for s, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {c:>4}  {s}")
    print(f"\n  {words_total:,} words of transcript added")
    db.close()


if __name__ == "__main__":
    main()
