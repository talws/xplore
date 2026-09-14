# Xplore handoff — supplement 1 (night of 12–13 September)

Read this *after* `HANDOFF.md`. It only covers what changed after the fork
started. Nothing here replaces the main note.

---

## 1. NEW — the thread pass crashes with "Out of Memory"  ← fix before resuming it

**Symptom.** The Chrome tab running the thread pass dies on Chrome's crash page
with error code **Out of Memory**. Reloading the tab resumes the pass (the queue
lives on the server), but nothing reloads it automatically.

**Evidence, measured at 23:50 UTC.** The pass runs in bursts of about 20 minutes,
then crashes:

| Burst | Threads | Duration |
|---|---|---|
| 1 | 57 | 22 min |
| 2 | 36 | 19 min |
| 3 | 88 | 19 min |
| 4 | 103 | 24 min, still running |

At that point one Chrome process was using **1,224 MB**, far more than any other
(the next was 390 MB). The machine still had **3.1 GB free of 15.3 GB**. So this
is one page's process running out of memory, not the PC running out.

**Likely cause — now strongly supported.** The pass moves between tweets with
`location.href`, which is a same-site navigation. Chrome usually keeps the same
renderer process for those, so whatever X's app and its video players leave
behind builds up across hundreds of hops. **Checked two minutes later:** the same process (same PID) had
grown from 1,224 MB to **1,735 MB** while the pass moved through about seven
more tweets. One process growing steadily across navigations is exactly the
pattern this explanation predicts.

**Why the existing safeguards don't catch it.** A crashed tab runs no code, so
the 45-second stall timeout in `bridge.js` never fires. The pass just stops, and
stays stopped until someone reloads the tab.

**Fix: three parts, one extension reload.**

1. **The service worker drives the pass, not the page.** Store the thread tab's
   id in `chrome.storage`. The page reports "done" to the worker, and the worker
   navigates that tab to the next tweet. This also fixes the **same-profile
   hijack** (HANDOFF TODO 4a): other tabs no longer pick up thread mode, because
   only the stored tab id is driven.
2. **Recycle the tab regularly.** Every **~25 threads** (well under the smallest
   burst of 36), the worker closes the tab and opens a fresh one with
   `chrome.tabs.create({active: false})`, which gets a new renderer process.
   Keep only one x.com tab open during a run, or Chrome may reuse an existing
   process for the new tab.
3. **Heartbeat watchdog.** The page pings the worker on every tweet. An alarm
   every minute (MV3's minimum is 30 seconds) checks the last ping. If thread
   mode is on and there's been no ping for 3 minutes, it recreates the tab.
   That catches crashes the recycling doesn't prevent.

**Cheap mitigation, no code.** In X's settings, turn video autoplay off
(Accessibility, display and languages → Data usage → Autoplay → Never). Videos
inside threads are the most likely thing piling up memory. It's the owner's
account setting, so the owner should make the change.

**Until it's fixed:** reload the crashed tab. At the current rate each reload
gets about 100 threads before the next crash.

**STATUS, 13 September: implemented in the original session. Do not rebuild.**

- `background.js` now runs the pass (`threadStart`, `threadAdvance`,
  `threadDone`, `threadEnd`, `openThreadUrl`, and the alarm `xbc-thread-watch`).
  `bridge.js` only captures a tweet's conversation, posts it, and reports back.
  The old `threadMode` flag is deleted when the extension installs.
- The tab is recycled every 25 tweets. If the page goes silent for 3 minutes,
  the watchdog retries the same tweet once in a fresh tab, then skips it.
- **One more bug was found in review and fixed before release.** A fresh tab
  has to be opened blank and recorded as the run's tab *before* it navigates.
  Otherwise its page asks "am I the run's tab?" too early, gets told no, and
  sits idle until the watchdog fires: a 3-minute stall on every recycle.
- **Verified:** both files parse (0 errors), and a simulated 60-tweet run with
  fake tabs, storage and server passed **14/14**: recycle timing, the
  blank-then-record-then-navigate order, other tabs ignored, completion, the
  watchdog's retry-then-skip, and stop. **Not yet confirmed on the live site at
  the time of writing.** The live check is that the largest Chrome process
  changes PID roughly every 25 tweets instead of growing.
- **After any extension reload, the run switches off.** Open `x.com/i/history`
  and press **Threads** again.

---

## 2. Also changed in `HANDOFF.md` after the fork started

If the fork read `HANDOFF.md` before about 23:00 UTC on 12 September, these edits
are new. Full text is in its **Traps** section and **Current state** table.

- **Lock contention was dropping bookmarks.** A background pass held a write
  lock for about 2½ minutes, and the server had only SQLite's 5-second default
  timeout: 32 × `database is locked`, and 500 errors on `/api/ingest`. Fixed with
  a commit per row and `PRAGMA busy_timeout = 30000`. New passes must do both.
  Don't run two sessions writing `xbookmarks.db` at once.
- **The thread pass looped forever on one tweet** because `ask()` dropped the
  `root` field, so the server received `root=undefined`. Fixed. The queue now
  counts attempts as it hands tweets out (max 2); there's also a 45-second stall
  timeout and a guard against reloading the same URL.
- **Ordering by `reply_count` picked viral tweets.** The first one, with 13,940
  replies, froze the page. The queue now takes self-declared threads first and
  excludes tweets with 5,000 or more replies.
- **Mneme's server has the same command line** (`python server.py`, relative
  path). Find what's listening on 8502 with `netstat -ano`. Never kill processes
  by command line.
- **The panel no longer reports errors as an empty queue.** It now says
  `server error N` or `server unreachable`.
- **Run passes unbuffered** (`python -u`) and write an exit marker. The first
  transcript run died with an empty log.
- **Transcripts are done:** 102 of 150, 629,222 words. `VIDEO_HOSTS` is too
  broad: `odysee.com/` also matches channel pages, which yt-dlp can't read.

---

## 3. Thread pass status at 23:50 UTC

284 fetched · **275 real threads** · **3,314 of the authors' own replies** ·
about 1,900 left at 4 a minute, *when it isn't crashing*.
