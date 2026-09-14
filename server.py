"""
server.py — local ingest + search for the X bookmark collector.

Runs on port 8502, which is the port the previous (lost) tool used. Standalone
by design: this has its own SQLite database and shares nothing with Mneme.
Searching the two together is a later, separate job -- a federation seam, not a
shared store.

The browser side sends the *raw* GraphQL response and parsing happens here, in
Python. That split is deliberate: when X changes a field, the fix is a server
edit and a re-parse of stored payloads, not a new extension release that has to
be reloaded in the browser.

    python server.py                 # ./xbookmarks.db, port 8502
    python server.py --db D:/xbookmarks.db --port 8600
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from flask import Flask, jsonify, request, Response, send_from_directory

# Windows consoles default to a legacy codepage -- cp1253 on this machine --
# and the startup banner below contains box-drawing characters and an em-dash.
# Launched from the login script, stdout is a redirected file using that
# codepage, so those characters raise UnicodeEncodeError and kill the server
# before it ever binds the port. That is precisely how it died silently after
# the reboot on 10 September: the extension reported "server unreachable" and
# nothing had been captured since.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from store import Store, as_mneme
from parse import parse_bookmarks_response, is_end_of_timeline, parse_thread
import capture   # capture-on-view: /api/capture*, see capture.py

app = Flask(__name__)
app.register_blueprint(capture.bp)
store: Store | None = None

# Bound properly in __main__; defaults exist so importing this module for tests
# does not leave the request handlers referencing undefined globals.
DB_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xbookmarks.db")
PORT: int = 8502

# The collector posts from a page served by x.com, so the browser will preflight
# and then check the origin. Nothing else may talk to this server: it is bound
# to localhost, but an over-broad "*" would let any site the owner visits read
# their bookmark database.
ALLOWED_ORIGINS = {"https://x.com", "https://twitter.com"}


@app.after_request
def _cors(resp: Response) -> Response:
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        # Chrome's Private Network Access: a preflight from a public origin to
        # a loopback address is refused unless this is echoed back. Belt and
        # braces -- the extension now routes through its service worker, where
        # the rule does not apply -- but it costs one header and makes a direct
        # page fetch work too.
        if request.headers.get("Access-Control-Request-Private-Network"):
            resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


@app.route("/api/<path:_p>", methods=["OPTIONS"])
def _preflight(_p):
    return ("", 204)


@app.route("/api/ping")
def ping():
    """The collector calls this before it starts, so a stopped server is an
    immediate, visible error rather than 24,000 silently dropped tweets."""
    return jsonify({"ok": True, "service": "xcollector", "port": PORT})


@app.route("/api/ingest", methods=["POST"])
def ingest():
    """Take one raw `Bookmarks` GraphQL response body.

    Returns `added` so the browser side knows whether to keep scrolling: a page
    that adds nothing means we have reached tweets we already hold, which is
    the natural stop condition for an incremental run.
    """
    raw = request.get_data(as_text=True)
    if not raw:
        return jsonify({"error": "empty body"}), 400
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"invalid JSON: {e}"}), 400

    try:
        records, cursor = parse_bookmarks_response(payload)
    except Exception as e:                       # never lose a page to a parse bug
        _quarantine(raw, e)
        return jsonify({"error": f"parse failed, payload saved: {e}"}), 500

    if not records:
        # Two very different things look alike here, and telling them apart is
        # the difference between "finished" and "being throttled".
        if is_end_of_timeline(payload):
            return jsonify({"ok": True, "added": 0, "updated": 0, "parsed": 0,
                            "cursor": cursor, "end": True,
                            "note": "end of timeline"})
        # Otherwise it is an empty timeline, which is what X returns once it
        # starts rate limiting. The caller must back off, not keep going.
        _quarantine(raw, "no records parsed")
        return jsonify({"ok": True, "added": 0, "updated": 0, "parsed": 0,
                        "cursor": cursor, "note": "no tweets found in payload"})

    result = store.upsert_tweets(records)
    result.update({"ok": True, "parsed": len(records), "cursor": cursor,
                   # Lets a Deep scan keep walking while any bookmark still
                   # lacks a date, even across pages it has already dated.
                   "undated": store.db.execute(
                       "SELECT COUNT(*) FROM tweets WHERE deleted=0 AND bookmarked_at IS NULL"
                   ).fetchone()[0]})
    return jsonify(result)


@app.route("/api/scanlog", methods=["POST"])
def scanlog():
    """The extension panel's status lines, written to the server log.

    Without this a stalled bookmark scan could only be diagnosed by someone
    reading the panel on the x.com tab; three Deep scans stalled on 14 Sep with
    nothing in the log to say why.
    """
    body = request.get_json(silent=True) or {}
    stamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")
    print(f"[panel {stamp}] {str(body.get('msg', ''))[:300]}", flush=True)
    return jsonify({"ok": True})


@app.route("/api/ingest/dom", methods=["POST"])
def ingest_dom():
    """Fallback path: records scraped from the DOM rather than intercepted.

    Lossier -- the timeline truncates at 280 characters and the remainder is
    genuinely absent from the page -- so anything arriving here is marked
    `truncated` unless the sender proved otherwise, which flags it for the
    expansion pass instead of quietly storing a half tweet as complete.
    """
    body = request.get_json(silent=True) or {}
    items = body.get("items") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return jsonify({"error": 'expected {"items": [...]}'}), 400

    records = []
    for it in items:
        tid = str(it.get("id") or "").strip()
        if not tid:
            continue
        handle = (it.get("author_handle") or "").lstrip("@")
        records.append({
            "id": tid,
            "url": it.get("url") or f"https://x.com/{handle or 'i'}/status/{tid}",
            "author_handle": handle or None,
            "author_name": it.get("author_name"),
            "created_at": it.get("created_at"),
            "text": it.get("text") or "",
            "truncated": bool(it.get("truncated", True)),
            "is_thread": bool(it.get("is_thread")),
            "quoted_text": it.get("quoted_text"),
            "source": "dom",
            "media": it.get("media") or [],
            "links": it.get("links") or [],
            "hashtags": it.get("hashtags") or [],
        })

    if not records:
        return jsonify({"ok": True, "added": 0, "updated": 0, "parsed": 0})
    result = store.upsert_tweets(records)
    result.update({"ok": True, "parsed": len(records)})
    return jsonify(result)


@app.route("/api/stats")
def stats():
    return jsonify(store.stats())


@app.route("/api/search")
def search():
    return jsonify({"results": store.search(
        q=(request.args.get("q") or "").strip(),
        author=(request.args.get("author") or "").strip(),
        topic=(request.args.get("topic") or "").strip(),
        tag=(request.args.get("tag") or "").strip(),
        date_from=(request.args.get("from") or "").strip(),
        date_to=(request.args.get("to") or "").strip(),
        added_from=(request.args.get("addedFrom") or "").strip(),
        added_to=(request.args.get("addedTo") or "").strip(),
        limit=min(int(request.args.get("limit", 50)), 500),
        offset=int(request.args.get("offset", 0)),
    )})


@app.route("/api/threads/next")
def threads_next():
    """The next bookmark whose conversation should be fetched.

    Server-side queue rather than a list held in the page: the browser has to
    navigate to each permalink, and navigation destroys any state the content
    script was holding. Asking again after every hop is the only arrangement
    that survives.
    """
    q = store.thread_queue(limit=1)
    return jsonify({"remaining": store.thread_queue_size(),
                    "next": q[0] if q else None})


@app.route("/api/ingest/thread", methods=["POST"])
def ingest_thread():
    """One raw TweetDetail response. ?root=<tweet id> says which one it is for."""
    root_id = (request.args.get("root") or "").strip()
    raw = request.get_data(as_text=True)
    if not root_id or not raw:
        return jsonify({"error": "root and body required"}), 400
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"invalid JSON: {e}"}), 400
    try:
        root, parts = parse_thread(payload, root_id)
    except Exception as e:
        _quarantine(raw, f"thread parse: {e}")
        return jsonify({"error": str(e)}), 500

    if root is None:
        # The focal tweet was not in the payload -- deleted, protected, or a
        # login wall. Mark it done anyway or the queue offers it forever.
        store.db.execute("UPDATE tweets SET thread_fetched_at=? WHERE id=?",
                         (__import__("datetime").datetime.now(
                             __import__("datetime").timezone.utc)
                          .isoformat(timespec="seconds").replace("+00:00", "Z"), root_id))
        store.db.commit()
        return jsonify({"ok": True, "parts": 0, "note": "root tweet not in payload"})

    n = store.save_thread(root_id, parts)
    return jsonify({"ok": True, "parts": n, "remaining": store.thread_queue_size()})


def _int(v, default: int, lo: int = 0, hi: int | None = None) -> int:
    """A query-string integer that cannot 500 the request."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    n = max(lo, n)
    return min(n, hi) if hi is not None else n


def _find_args(a, limit_default: int = 40, limit_max: int = 200) -> dict:
    s = lambda k, d="": (a.get(k) or d).strip()
    since = s("since")
    return dict(
        q=s("q"), tag=s("tag"), topic=s("topic"), author=s("author"),
        since_days=int(since) if since.isdigit() else None,
        date_from=s("from"), date_to=s("to"),
        sort=s("sort", "bookmarked"), order=s("order", "desc"),
        has_archive=s("archive"), language=s("language"),
        content_type=s("contentType"), status=s("status"),
        date_added=s("dateAdded"), scope=s("scope", "bookmarks"), thread=s("thread"),
        limit=_int(a.get("limit"), limit_default, 1, limit_max),
        offset=_int(a.get("offset"), 0),
    )


@app.route("/api/find")
def api_find():
    """The search the UI runs: bookmarks matched on their own text, their
    thread, the tweet they quote, or the text of the articles they link to."""
    return jsonify(store.find(**_find_args(request.args)))


@app.route("/api/tweets/<tid>")
def api_tweet(tid):
    """One bookmark for the detail panel: thread, quoted tweet, links with
    excerpts of their archived text, media and hashtags."""
    d = store.tweet(tid.strip())
    if d is None:
        return jsonify({"error": "no such tweet"}), 404
    return jsonify(d)


@app.route("/api/facets")
def api_facets():
    """Sidebar facets. Mneme's /api/facets shape, plus Xplore's own facets."""
    return jsonify(store.facets())


@app.route("/api/bookmarks")
def api_bookmarks():
    """Mneme's read contract, so Mneme's existing client can list Xplore.

    Same parameters as Mneme (`q`, `contentType`, `language`, `topic`,
    `dateAdded`, `collection`) and the same `{count, bookmarks}` envelope.
    Xplore's collection is named `x`. Unlike Mneme this pages: `limit`
    (default 1000, max 5000) and `offset`, with `total` for the full count.
    """
    a = request.args
    coll = (a.get("collection") or "").strip()
    if coll and coll not in ("x", "all"):
        return jsonify({"count": 0, "total": 0, "offset": 0, "bookmarks": []})
    args = _find_args(a, limit_default=1000, limit_max=5000)
    r = store.find(**args)
    base = request.host_url.rstrip("/")
    recs = [as_mneme(t, base) for t in r["results"]]
    return jsonify({"count": len(recs), "total": r["total"], "offset": args["offset"],
                    "bookmarks": recs})


@app.route("/article")
def article_view():
    """Serve the locally archived copy of an article.

    Plain text in a readable shell rather than the original markup: what was
    stored is the extracted text, and presenting it as a page would imply we
    kept a faithful rendering, which we did not.
    """
    url = (request.args.get("url") or "").strip()
    a = store.article(url) if url else None
    if not a:
        return Response("<p style='font:15px system-ui;padding:30px'>"
                        "No archived copy of that URL.</p>",
                        mimetype="text/html", status=404)
    import html as _h
    body = _h.escape(a["text"] or "").replace(chr(10), "<br>")
    return Response(f"""<!doctype html><meta charset="utf-8">
<title>{_h.escape(a['title'] or url)}</title>
<style>:root{{color-scheme:light dark}}
 body{{font:16px/1.65 Georgia,serif;margin:0 auto;padding:32px 22px;max-width:720px}}
 h1{{font:600 21px/1.35 system-ui,sans-serif;margin:0 0 6px}}
 .m{{font:12px system-ui,sans-serif;opacity:.6;margin-bottom:22px;
   padding-bottom:14px;border-bottom:1px solid #8883}}
 a{{color:#3b82f6}}</style>
<h1>{_h.escape(a['title'] or '(untitled)')}</h1>
<div class="m">archived from <a href="{_h.escape(url)}" target="_blank"
 rel="noopener">{_h.escape(url)}</a><br>
 via {_h.escape(a['source'] or '?')} · {a['word_count'] or 0} words ·
 captured {_h.escape((a['fetched_at'] or '')[:10])}
 {f"· <a href='{_h.escape(a['snap_url'])}' target='_blank' rel='noopener'>snapshot</a>" if a['snap_url'] else ""}
</div>
<div>{body}</div>""", mimetype="text/html")


@app.route("/api/topics")
def api_topics():
    """Topics with counts, for the sidebar filter."""
    return jsonify({"topics": store.topic_counts()})


@app.route("/api/tweets/topic", methods=["POST"])
def api_set_topic():
    """Record the owner's own label for a tweet.

    Written to topic_user, never to topic, so a re-run of the classifier can
    never overwrite a hand-made decision. Everything that reads a topic reads
    COALESCE(topic_user, topic).
    """
    body = request.get_json(silent=True) or {}
    tid = str(body.get("id") or "").strip()
    topic = (body.get("topic") or "").strip()
    if not tid:
        return jsonify({"error": "id required"}), 400
    store.set_topic(tid, topic_user=topic or None)
    return jsonify({"ok": True, "id": tid, "topic": topic})


@app.route("/api/tags")
def api_tags():
    return jsonify({"tags": store.tag_counts()})


@app.route("/api/tweets/tags", methods=["POST"])
def api_set_tags():
    """Replace a tweet's hand-written tags.

    Separate from its topic: the topic is the single machine-assigned category,
    these are however many labels the owner wants on top of it. That is what
    sub-categorising without leaving the taxonomy looks like.
    """
    body = request.get_json(silent=True) or {}
    tid = str(body.get("id") or "").strip()
    if not tid:
        return jsonify({"error": "id required"}), 400
    raw = body.get("tags")
    if isinstance(raw, str):
        raw = [p for p in raw.replace(";", ",").split(",")]
    return jsonify({"ok": True, "id": tid, "tags": store.set_tags(tid, raw or [])})


@app.route("/api/tweets/remove", methods=["POST"])
def tweets_remove():
    """Remove tweets from this collection.

    A tombstone, not a DELETE -- see Store.remove. The tweet stays bookmarked
    on x.com, so anything less would be undone by the next sync.
    """
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
    if not ids:
        return jsonify({"error": "no ids given"}), 400
    return jsonify({"ok": True, "removed": store.remove(ids)})


@app.route("/api/tweets/restore", methods=["POST"])
def tweets_restore():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
    if not ids:
        return jsonify({"error": "no ids given"}), 400
    return jsonify({"ok": True, "restored": store.restore(ids)})


@app.route("/api/links/pending")
def links_pending():
    """Outbound links awaiting archiving, most-referenced first."""
    return jsonify({"links": store.pending_links(
        limit=min(int(request.args.get("limit", 100)), 1000))})


def _quarantine(raw: str, reason) -> None:
    """Keep any payload we could not turn into tweets.

    The response shape here was never directly observed, so the first real run
    is also the first test of the parser. Throwing away a body we failed to
    parse would mean re-scraping to debug it; keeping it means the fix can be
    verified offline against the exact bytes that broke.
    """
    try:
        d = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "unparsed")
        os.makedirs(d, exist_ok=True)
        import time
        p = os.path.join(d, f"{int(time.time() * 1000)}.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(raw)
        print(f"  [!] unparsed payload saved -> {p}  ({reason})", file=sys.stderr)
    except Exception:
        pass


HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
ASSETS_DIR = os.path.join(HERE, "assets")

# Set explicitly. On Windows, Python's mimetypes reads the registry, where .js
# is often registered as text/plain, and a browser refuses to run an ES module
# served with that type: the page would load and do nothing.
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html",
                ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml"}


def _static(folder: str, name: str):
    ext = os.path.splitext(name)[1].lower()
    if ext not in STATIC_TYPES or not re.fullmatch(r"[\w.\-/]+", name) or ".." in name:
        return Response("not found", status=404)
    # No caching: the UI is plain files meant to be edited without a restart.
    return send_from_directory(folder, name, mimetype=STATIC_TYPES[ext], max_age=0)


@app.route("/")
def index():
    """The Xplore UI (ui/). Falls back to the classic page if it is absent."""
    if os.path.exists(os.path.join(UI_DIR, "index.html")):
        return _static(UI_DIR, "index.html")
    return index_classic()


@app.route("/ui/<path:name>")
def ui_file(name):
    return _static(UI_DIR, name)


@app.route("/assets/<path:name>")
def asset_file(name):
    return _static(ASSETS_DIR, name)


@app.route("/favicon.ico")
def favicon():
    return _static(ASSETS_DIR, "xplore.ico")


@app.route("/classic")
def index_classic():
    """The previous UI, kept until the new one has been used for a while.
    Kept in app.html so it can be edited without touching Python."""
    p = os.path.join(HERE, "app.html")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return Response(fh.read(), mimetype="text/html")
    return Response(UI, mimetype="text/html")


@app.route("/simple")
def index_simple():
    return Response(UI, mimetype="text/html")


UI = """<!doctype html><meta charset="utf-8"><title>X Bookmarks</title>
<style>
 :root{color-scheme:light dark}
 body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px;max-width:820px}
 h1{font-size:19px;margin:0 0 4px}
 .sub{opacity:.65;font-size:13px;margin-bottom:18px}
 input{font:inherit;padding:9px 12px;width:100%;box-sizing:border-box;
   border:1px solid #8884;border-radius:8px;background:transparent;color:inherit}
 .t{border-top:1px solid #8883;padding:14px 0}
 .h{font-weight:600}.m{opacity:.6;font-size:12px;margin-left:6px}
 .x{white-space:pre-wrap;margin:6px 0}
 a{color:#3b82f6}.tag{font-size:11px;border:1px solid #8884;border-radius:99px;
   padding:1px 7px;margin-right:4px;opacity:.75}
 .rm{font:inherit;font-size:11px;border:1px solid #8884;border-radius:99px;
   padding:1px 8px;background:transparent;color:inherit;opacity:.45;cursor:pointer}
 .rm:hover{opacity:1;border-color:#e11d48;color:#e11d48}
 #s{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;opacity:.7;margin-bottom:16px}
 #tops{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
 .tp{font-size:12px;border:1px solid #8884;border-radius:99px;padding:3px 10px;
   cursor:pointer;background:transparent;color:inherit;font-family:inherit}
 .tp:hover{border-color:#3b82f6}
 .tp.on{background:#3b82f6;border-color:#3b82f6;color:#fff}
 .topic{font-size:11px;opacity:.8;border-bottom:1px dotted #8886;cursor:text}
 #dates{display:flex;gap:16px;flex-wrap:wrap;align-items:center;font-size:12px;
   opacity:.8;margin-bottom:12px}
 #dates input[type=date]{font:inherit;padding:2px 5px;border:1px solid #8884;
   border-radius:5px;background:transparent;color:inherit;width:auto}
 #clr{font:inherit;font-size:12px;border:1px solid #8884;border-radius:99px;
   padding:2px 10px;background:transparent;color:inherit;cursor:pointer}
 .tags{font-size:11px;opacity:.85;border-bottom:1px dotted #8886;cursor:text;
   margin-left:6px}
 .tags b{font-weight:600;opacity:.6}
</style>
<h1>X Bookmarks</h1>
<div class="sub">Local collection, port 8502. Separate from Mneme.</div>
<div id="s"></div>
<div id="tops"></div>
<div id="dates">
  <label>posted <input type="date" id="df"> to <input type="date" id="dt"></label>
  <label>bookmarked <input type="date" id="af"> to <input type="date" id="at"></label>
  <button id="clr">clear</button>
</div>
<input id="q" placeholder="Search your bookmarked tweets…" autofocus>
<div id="r"></div>
<script>
const $=s=>document.querySelector(s);
const esc=t=>(t||"").replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
async function stats(){
  const s=await (await fetch("/api/stats")).json();
  $("#s").innerHTML=[["tweets",s.tweets],["authors",s.authors],
    ["links",s.uniqueLinks],["to archive",s.linksPending],
    ["truncated",s.truncated],["threads",s.threads],["removed",s.removed],
    ["db",(s.dbBytes/1048576).toFixed(1)+" MB"]]
    .map(([k,v])=>`<span><b>${v}</b> ${k}</span>`).join("");
}
let timer, activeTopic="", activeTag="";
async function topics(){
  const {topics:ts}=await (await fetch("/api/topics")).json();
  $("#tops").innerHTML=ts.map(t=>
    `<button class="tp${t.topic===activeTopic?" on":""}" data-t="${esc(t.topic)}">${esc(t.topic)} <b>${t.n}</b></button>`).join("");
  document.querySelectorAll(".tp").forEach(b=>b.onclick=()=>{
    activeTopic = activeTopic===b.dataset.t ? "" : b.dataset.t;   // click again to clear
    topics(); run();
  });
}
async function run(){
  const q=$("#q").value.trim();
  const p=new URLSearchParams({limit:50,q,topic:activeTopic});
  if($("#df").value) p.set("from",$("#df").value);
  if($("#dt").value) p.set("to",$("#dt").value);
  if($("#af").value) p.set("addedFrom",$("#af").value);
  if($("#at").value) p.set("addedTo",$("#at").value);
  if(activeTag) p.set("tag",activeTag);
  const r=await (await fetch("/api/search?"+p)).json();
  $("#r").innerHTML=r.results.map(t=>`<div class="t" data-id="${t.id}">
    <div><span class="h">@${esc(t.author_handle)}</span>
      <span class="m">${(t.created_at||"").slice(0,10)}</span>
      ${t.truncated?'<span class="tag">truncated</span>':""}
      ${t.is_thread?'<span class="tag">thread</span>':""}
      <span class="topic" contenteditable data-id="${t.id}"
        title="Click to edit — your label overrides the model's">${esc(t.topic||"—")}</span>
      <span class="tags" contenteditable data-id="${t.id}"
        title="Your own tags, comma separated — sub-categories on top of the topic"
        >${esc((t.tags||[]).join(", ")) || "+ tags"}</span>
      <button class="rm" data-id="${t.id}" title="Remove from collection">remove</button></div>
    <div class="x">${esc(t.text)}</div>
    <a href="${t.url}" target="_blank" rel="noopener">open on X →</a></div>`).join("")
    || "<div class='t'>nothing yet</div>";
}
// Removal is a tombstone server-side, so it survives the next sync instead of
// being quietly re-added. Undo is offered inline because it is irreversible
// from the UI otherwise.
document.addEventListener("click", async (e)=>{
  const b=e.target.closest(".rm"); if(!b) return;
  const id=b.dataset.id, row=b.closest(".t");
  await fetch("/api/tweets/remove",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({ids:[id]})});
  row.innerHTML=`<span class="m">removed.</span> <button class="rm undo" data-id="${id}">undo</button>`;
  row.querySelector(".undo").onclick=async ev=>{
    ev.stopPropagation();
    await fetch("/api/tweets/restore",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({ids:[id]})});
    run(); stats();
  };
  stats();
});
$("#q").addEventListener("input",()=>{clearTimeout(timer);timer=setTimeout(run,180)});
// An edited topic is written to topic_user, so re-running the classifier
// will not undo it.
document.addEventListener("blur",async e=>{
  const el=e.target; if(!el.classList||!el.classList.contains("topic")) return;
  const v=el.textContent.trim();
  await fetch("/api/tweets/topic",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:el.dataset.id,topic:v==="—"?"":v})});
  topics();
},true);
document.addEventListener("blur",async e=>{
  const el=e.target; if(!el.classList||!el.classList.contains("tags")) return;
  let v=el.textContent.trim(); if(v==="+ tags") v="";
  await fetch("/api/tweets/tags",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:el.dataset.id,tags:v})});
},true);
["df","dt","af","at"].forEach(id=>$("#"+id).addEventListener("change",run));
$("#clr").onclick=()=>{["df","dt","af","at"].forEach(id=>$("#"+id).value="");
  activeTopic=""; activeTag=""; topics(); run();};
stats();topics();run();
</script>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="X bookmark collector")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "xbookmarks.db"))
    ap.add_argument("--port", type=int, default=8502)
    args = ap.parse_args()

    DB_PATH, PORT = args.db, args.port      # noqa: F811 -- rebinding the defaults above

    # Started at login *and* possibly by hand, so a second copy is likely. Bail
    # out quietly if one is already serving, rather than dying on "address in
    # use" and leaving a confusing traceback in the log.
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://localhost:{PORT}/api/ping", timeout=2) as r:
            if json.load(r).get("service") == "xcollector":
                print(f"  already running on port {PORT} — nothing to do")
                sys.exit(0)
    except Exception:
        pass                                 # nothing there; carry on and start

    store = Store(DB_PATH)
    capture.init(store)
    s = store.stats()
    print()
    print("  Xplore — X bookmark collector")
    print("  ─────────────────────────────────────────────")
    print(f"  DB:      {DB_PATH}")
    print(f"  Held:    {s['tweets']} tweets  |  {s['uniqueLinks']} links "
          f"({s['linksPending']} to archive)")
    print(f"  Server:  http://localhost:{PORT}")
    print()
    app.run(host="localhost", port=PORT, debug=False, use_reloader=False)
