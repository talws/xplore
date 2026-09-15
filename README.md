# Xplore

**Your X (Twitter) bookmarks, kept on your own computer: captured, their links archived,
and all of it searchable.**

X keeps your bookmarks as an endless scroll with no search. The articles they point to
disappear behind paywalls or go offline. Xplore fixes both.

- It **collects your bookmarks automatically** while you use Chrome.
- It **saves the text of the articles and videos they link to**, so you can still read them
  when the original is gone.
- It gives you **one search box** over all of it, plus a **Related** view that shows what else
  you saved on the same subject.

Everything is stored in a single file on your computer. There is no account, no cloud and no
telemetry.

> **Status: tester build.** Setting it up means installing Python and running a few commands,
> which this guide walks through step by step. A one-click installer comes later.

---

## Contents

1. [What Xplore does](#1-what-xplore-does)
2. [What leaves your computer](#2-what-leaves-your-computer)
3. [What you need](#3-what-you-need)
4. [Setup, step by step](#4-setup-step-by-step)
5. [Your first sync](#5-your-first-sync)
6. [Filling the archive](#6-filling-the-archive)
7. [Using Xplore](#7-using-xplore)
8. [Topics (optional, uses AI)](#8-topics-optional-uses-ai)
9. [Troubleshooting](#9-troubleshooting)
10. [Updating and uninstalling](#10-updating-and-uninstalling)
11. [Known limits](#11-known-limits)
12. [What's in the folder](#12-whats-in-the-folder)

---

## 1. What Xplore does

Xplore has three parts that work together:

| Part | What it is | What it does |
|---|---|---|
| **The Xplore extension** | A small Chrome extension | While you are on x.com, it saves your bookmarks as X loads them and passes them to Xplore. It also saves archived article pages you unlock by hand (see [Recover](#recover-articles-behind-paywalls)). |
| **The Xplore server** | A program that runs quietly in the background | Keeps your library in `xbookmarks.db` inside the Xplore folder, and serves the app. |
| **The Xplore app** | A window on your desktop (or a browser tab at <http://localhost:8502>) | Where you search, browse, read and organise. |

### Capturing bookmarks

- Once a day, the extension opens your X bookmarks in a background tab, collects anything new,
  and closes the tab. Chrome needs to be open and you need to be signed in to x.com.
- It keeps each bookmark's full text, author, date posted, **date you bookmarked it**, the post
  it quotes, and its links, photos and hashtags.
- Photos and videos are **not downloaded**. Xplore stores their addresses and shows photos from
  X when you are online.
- For threads, it can also fetch the author's own follow-up posts, so the whole argument is
  searchable, not just the first post.

### Archiving what your bookmarks link to

Separate commands (section 6) follow each link, and each command skips what it has already done:

- **short links** (t.co, bit.ly and so on) are resolved to the real address;
- **article text** is saved from the page itself, or from the Internet Archive's Wayback Machine
  if the page is gone or blocked;
- **YouTube and other videos** get their transcripts;
- paywall teasers and bot-check pages are recognised and **not** counted as saved articles.

### Recover: articles behind paywalls

Some articles can't be fetched automatically. They often *do* exist on archive.today, which only
lets a person in. The **Recover** tab lists them, most-bookmarked first. You open one, pass the
check, and the extension saves the text into Xplore.

### Search and browse

- **One search box** covers bookmark text, threads, quoted posts **and the full text of every
  archived article.** Each result says where it matched, for example *"Matched only in the linked
  article"*, and search words are highlighted.
- **Filters:** time frame (last 24 hours, 3 days, 7 days, month, or your own dates), archive
  state, content type (article links, video, images, text only), topic, your own tags,
  language and author.
- **Sort** by when you bookmarked or when it was posted, newest or oldest first.
- **Detail panel:** the whole thread, the quoted post, links with an excerpt of the saved text,
  photos, and your own topic and tags.
- **Reader:** saved articles open inside the app as clean text.

### Related

Every bookmark has a **Related** button. It shows other bookmarks and articles on the same
subject, and why each one is there:

- same link;
- quotes it, or is in the same thread;
- shared distinctive words;
- same author;
- same hashtag or tag;
- same topic;
- saved around the same time.

A search has the same drawer, "Related to *your words*", at the top of the results.

---

## 2. What leaves your computer

| When | What goes where |
|---|---|
| You browse x.com | The extension reads the bookmark data X sends *your* browser, using your own signed-in session. It sends that data only to the Xplore server on your computer (`localhost:8502`). |
| You run the archiving commands | Xplore visits the linked websites and the Wayback Machine (archive.org), like a browser opening each link. |
| You open an article on archive.today | That page's text goes to your Xplore server. Nothing else on archive.today is read. |
| You open the detail panel | Photos load from X's servers. |
| You run `topics.py` (optional) | Up to 260 characters of each bookmark's text go to DeepSeek's AI service, on your own API key and billed to it. |

Nothing else is sent anywhere. The extension can only talk to x.com, archive.today and your own
computer.

> X's terms restrict automated collection. Xplore reads only your own bookmarks, through your
> own browser, for personal use.

---

## 3. What you need

- **Windows 10 or 11.** This build has only been tested on Windows 11.
- **Google Chrome.** Other Chromium browsers (Edge, Brave) may work but are untested.
- **An X account**, signed in, in that Chrome.
- **Python 3.11 or newer.** Step 2 below installs it. Tested with 3.14.
- **Disk space:** about 200 MB for 25,000 bookmarks with their archived articles.
- About **15 minutes** for setup, then time for the first sync (section 5).

---

## 4. Setup, step by step

### Step 1: Put the Xplore folder somewhere permanent

Unzip `Xplore-….zip` into a folder you will keep, for example `Documents\Xplore`.

- **Don't** use Program Files, which isn't writable.
- **Don't** leave it in Downloads: your library is saved inside this folder.

From here on, "the Xplore folder" means that folder: the one containing `server.py`,
`README.md` and the `extension` folder.

### Step 2: Install Python

1. Go to <https://www.python.org/downloads/> and download the latest **Python 3** for Windows.
2. Run the installer. On the first screen, **tick "Add python.exe to PATH"**, then click
   **Install Now**.
3. Check it worked. Open **PowerShell** (press Start, type `powershell`, press Enter) and type:

   ```
   python --version
   ```

   You should see `Python 3.x.x`. If Windows opens the Microsoft Store instead, the PATH box
   wasn't ticked: run the installer again and choose **Modify**.

### Step 3: Install Xplore's Python packages

1. Open PowerShell **in the Xplore folder**: open the folder in File Explorer, click the address
   bar, type `powershell` and press Enter.
2. Run:

   ```
   python -m pip install -r requirements.txt
   ```

   This downloads Flask, requests, Beautiful Soup, certifi, pywebview (for the desktop window)
   and yt-dlp (for video transcripts). It takes a minute or two. Ignore any
   "not on PATH" warnings about script folders.

### Step 4: Create the desktop icon and start Xplore

In the same PowerShell window, run:

```
powershell -ExecutionPolicy Bypass -File create-shortcut.ps1
```

An **Xplore** icon appears on your desktop. Double-click it.

- The first time, you'll see the Xplore logo for up to a minute while the server starts. Then
  the app opens, empty for now.
- **Closing the window does not stop the server.** It keeps running in the background so the
  extension can deliver bookmarks.
- Double-clicking the icon again brings the window back; it never starts a second copy.

*No window?* Double-click `start.bat` in the Xplore folder instead, wait a minute, and open
<http://localhost:8502> in Chrome. The app is the same.

### Step 5: Install the extension in Chrome

1. In Chrome, go to `chrome://extensions`.
2. Turn on **Developer mode** (the switch at the top right).
3. Click **Load unpacked**.
4. Select the **`extension`** folder *inside* the Xplore folder (not the Xplore folder itself).
5. **Xplore** appears in the list. If Chrome asks to allow it on archive.today sites, allow it.
6. Optionally, pin it: click the puzzle-piece icon in the toolbar, then the pin next to Xplore.

Chrome may later show a "Disable developer mode extensions" warning. Choose to **keep** it. That
warning appears for any extension not installed from the Chrome Web Store.

### Step 6: Start Xplore automatically when you sign in (recommended)

Capture only works while the Xplore server is running. To start it every time you sign in to
Windows:

1. Press **Win + R**, type `shell:startup` and press Enter. A folder opens.
2. In that folder, right-click → **New** → **Shortcut**.
3. For the location, enter the following, with **your** Xplore folder's path (keep the quotes):

   ```
   wscript.exe "C:\Users\YOU\Documents\Xplore\start-collector.vbs"
   ```

4. Click **Next**, name it `Xplore server`, and click **Finish**.

It starts the server silently, with no window. Its messages go to `server.log` in the Xplore
folder.

---

## 5. Your first sync

1. In Chrome, make sure you are **signed in to x.com**.
2. Go to <https://x.com/i/bookmarks>. X may redirect it to `x.com/i/history`; that's fine.
3. A small **Xplore** panel appears at the bottom right of the page, with three buttons:

   | Button | What it does |
   |---|---|
   | **Start** | Collects new bookmarks, and stops once it reaches ones Xplore already has. |
   | **Deep** | Walks your **entire** bookmark list. Use it the **first time**, and then about once a month as a check. |
   | **Threads** | For bookmarks that are threads, opens each one in turn (about one every 10 seconds) to collect the author's follow-up posts. Optional, and slow for large libraries. |

4. Press **Deep** the first time. The panel counts pages and bookmarks as it scrolls. **Keep the
   tab open** until it says *complete — end of bookmarks*.
   - Speed: roughly 20 bookmarks per page, a few seconds per page. 5,000 bookmarks take about
     15–20 minutes.
   - If X slows down or the panel stops making progress, X is rate-limiting. Close the tab,
     wait an hour, and press **Deep** again; nothing collected so far is lost.
5. Open the Xplore app. Your bookmarks are there.

**After that it's automatic.** The extension checks every hour and syncs about once a day in a
background tab, as long as Chrome and the Xplore server are running. If the server isn't running,
Chrome shows a notification: *"Xplore — not running."*

---

## 6. Filling the archive

Bookmarks appear straight away. The **articles they link to** are saved by four commands.

- Run them one at a time, in PowerShell in the Xplore folder. The app can stay open.
- Every command is resumable: stop it with **Ctrl+C** whenever you like, and the next run carries
  on from there, only doing new work.
- Run them again after syncs to process new bookmarks.

```
python resolve_links.py
python enrich_links.py
python archive_links.py
python transcripts.py
```

| Command | What it does | How long |
|---|---|---|
| `resolve_links.py` | Turns short links (t.co, bit.ly…) into real addresses | A few minutes per thousand links |
| `enrich_links.py` | Gets each page's title and publication date | A few minutes per thousand links |
| `archive_links.py` | Saves article text: first directly, then from the Wayback Machine for pages that failed | **Slow on purpose.** Direct saving is quick; Wayback allows about one page every 8 seconds, so thousands of links can take hours. Leave it running. |
| `transcripts.py` | Saves transcripts of linked YouTube (and similar) videos | About 3 seconds per video. Optional. |

Useful options:

- `--limit 200` does only 200 items. It's good for a first try.
- `--retry-failed` tries again the ones that failed last time.

When `archive_links.py` has finished, open the **Recover** tab to rescue what it couldn't reach
(see below).

---

## 7. Using Xplore

### The layout

- **Top bar:** the Library and Recover tabs, the search box, counts, and a light / dark / system
  theme switch (the half-moon button).
- **Left sidebar:** filters, with how many bookmarks each one holds.
- **Middle:** your bookmarks.
- **Right:** the detail panel, when you click a bookmark.

On a narrow window, the filters move behind the **☰** button.

### Bookmark cards

Each card shows:

- the author and when you bookmarked it (hover the date to see when it was posted);
- the text; a **thread · N** badge means the author's follow-ups are saved;
- the quoted post, if any;
- every link, with its state:
  - **archived · N words**: click the title to read the saved copy in the app. Ctrl+click opens
    it in a browser tab; **original ↗** opens the live page;
  - **teaser**: only the paywall teaser was saved;
  - **not recovered** or **bot wall**: click **recover →** to find it in the Recover tab;
  - **not archived yet**: run the commands in section 6;
- the topic, your tags, how many photos and videos, **Related**, and **Open on X ↗**.

A green stripe on the left means the card has an archived article. Amber means an article is
still missing.

### Searching

Type in the search box. Results update as you type, and Enter searches at once.

- The search covers the bookmark, its thread, the post it quotes, and the full saved text of
  their articles.
- **Upper or lower case doesn't matter.** For Greek, **type the accents**: `Τσίπρας` finds it,
  `τσιπρας` does not.
- Use quotes for an exact phrase: `"international law"`.
- Use `OR` for either word: `iran OR israel`.
- Under each result, *"Matched in…"* says where the words were found.

### Filters and sorting

Click a filter in the sidebar to apply it, and click again to remove it. Active filters show as
chips above the results, each with an **×**, plus **Clear all**.

- **Time frame** follows the sort: by bookmark date or by post date. **Custom range** takes two
  dates.
- **Archive state:** archived text, to recover, not archived yet, no links.
- **Content type:** article links, video, images, text only, threads.
- **Topics, Tags, Language, Authors:** the longer lists have a **Show all** link.

Clicking a topic, tag or author name on a card filters by it too. The address bar keeps your
current search and filters, so a bookmark or a reload brings you back to the same list.

### The detail panel

Click anywhere on a card, not on a link or button, to open it. It has:

- the full thread, numbered;
- the quoted post and its links;
- links with an excerpt of their saved text;
- photos (click one to open it full size);
- **Topic:** type your own, or pick one; your choice always overrides the automatic one;
- **Tags:** any words you like, separated by commas;
- **Remove…**: takes the bookmark out of Xplore. It stays bookmarked on X, and Xplore ignores
  it from then on. **Undo** is offered straight away.

### Related

Click **Related** on a card. A drawer opens underneath with the three best related links and three
best related bookmarks. Each has a bar for how closely it's related and labels for why.

Click **Show all** to see everything, plus:

- **signal switches**, to ignore one kind of evidence (for example "same author");
- a **"near in time"** setting: within 3, 7, 30 or 90 days.

Click a related bookmark to open it in the detail panel. When you've searched, **Related to
"…"** at the top of the results does the same for your search words.

### Recover articles behind paywalls

1. Open the **Recover** tab. It lists every article Xplore couldn't save, most-bookmarked first.
   The sidebar filters it by website.
2. Click **Open on archive.today ↗**. It opens in your browser.
   - If you use the desktop window, it opens in your **default** browser. The extension lives
     in **Chrome**, so if your default browser isn't Chrome, copy the link into Chrome.
3. Pass archive.today's security check.
4. The page reloads, and a message in its corner says **"Xplore: archived — N words"**.
5. Come back to Xplore. The item fades out as saved.

If archive.today has no copy, it says so: that article can't be recovered this way.

### Keyboard

| Key | Does |
|---|---|
| `/` | Jump to the search box |
| `j` / `k` | Next / previous bookmark (opens it in the detail panel) |
| `Enter` | Open the focused bookmark |
| `Esc` | Close the reader, the detail panel, or the filters |

---

## 8. Topics (optional, uses AI)

Xplore can sort your bookmarks into about 25 topics, such as "Climate Change" or "AI and
Technology". This uses DeepSeek's AI service with **your own API key**, and DeepSeek bills you
for it.

1. Create an account and an API key at <https://platform.deepseek.com>, and add a little credit.
2. Save the key for Xplore. In PowerShell:

   ```
   setx DEEPSEEK_API_KEY "sk-your-key-here"
   ```

   Then **close PowerShell and open a new one**: the key only reaches new windows.
3. In the Xplore folder:

   ```
   python topics.py --propose
   python topics.py --assign --limit 200
   python topics.py --assign
   ```

   - `--propose` reads a sample of 300 bookmarks and decides on the topic list.
   - `--assign --limit 200` is a trial.
   - `--assign` without a limit classifies everything else.

   Re-run `--assign` after syncs to classify new bookmarks. `python topics.py --show` prints how
   many bookmarks each topic has.

Your own topic edits are never overwritten.

### AI in Related (optional)

With an API key, **Related** gets smarter:

- the AI looks for more than shared words: the people and places involved, the same names in
  English and Greek, and different wordings of the same thing;
- it then judges each result, marking how closely it's related (● to ●●●) with a short reason.

**To set it up:**

1. Click the **sliders button** at the top right of Xplore to open **AI settings**.
2. Choose a provider (DeepSeek is the default and the only one tested) and paste your API key.
   If you already set `DEEPSEEK_API_KEY` above, Xplore uses that and you can skip the key.
3. Keep or change the spending limits: **per request** (default $0.02) and **per day** (default
   $0.50). Xplore refuses any call that would go over either one.
4. Click **Save and test**. You should see "Works", with the cost of the test, a tiny fraction of
   a cent.

**Using it:**

- **The AI switch:** in any Related drawer, the **AI** button turns it on or off.
  - The drawer first shows the ordinary results, then re-orders them a few seconds later with the
    AI's judgement.
  - Opening the same drawer again is instant and free, because results are remembered.
  - A typical drawer costs about a fifth of a cent.
- **Test set:** in **Show all**, each item has **✓** and **✗** buttons. Mark results as relevant
  or not relevant; this is how you can check whether AI actually helps. After marking a good
  number (say 30 bookmarks), run:

  ```
  python eval_related.py
  ```

  It compares plain Related with AI Related on your marks.

Your key is saved in `%USERPROFILE%\.xplore\config.json` on your computer, and never appears in
the app or its logs. Your marks are saved beside it in `gold.json`.

---

## 9. Troubleshooting

**The Xplore panel doesn't appear on x.com.**
Reload the x.com tab. After installing or updating the extension, tabs that were already open
don't have it yet. Check that it's switched on in `chrome://extensions`.

**The panel says "server not running", or Chrome shows "Xplore — not running".**
Start Xplore: the desktop icon, or `start.bat`. Set up the automatic start ([step 6](#step-6-start-xplore-automatically-when-you-sign-in-recommended))
so it doesn't happen again.

**The desktop icon shows "Xplore's server did not start."**
In PowerShell, in the Xplore folder, run `python server.py` to see the error on screen.
- *No module named …*: repeat [step 3](#step-3-install-xplores-python-packages).
- *Address already in use*: something else is using port 8502.

The log of automatic starts is `server.log` in the Xplore folder.

**A sync stops partway.**
X limits how fast bookmarks can be read. Wait an hour and press **Start**, or **Deep** for the
first sync. Nothing collected is lost.

**Search can't find a Greek word I can see in a post.**
Type it with its accents.

**Recover: I passed the check but nothing was saved.**
The page must be open in the Chrome that has the extension. Look for the corner message:
- *already archived*: it's already there;
- *not saved — …*: the reason follows (for example, the page was too short to be an article).

**A command says "database is locked".**
Two archiving commands were running at once. Stop one; run them one after another.

---

## 10. Updating and uninstalling

**To update to a new version:**

1. Quit the server: close the window, then end any **python** process in Task Manager whose
   command line mentions `server.py`.
2. Unzip the new version **over** the Xplore folder. Your library (`xbookmarks.db` and
   `unparsed\`) is not in the zip, so it's kept.
3. Run `python -m pip install -r requirements.txt` again.
4. In `chrome://extensions`, click the reload arrow on Xplore, then reload your x.com tab.
5. Start Xplore.

**To uninstall:**

1. Remove the extension in `chrome://extensions`.
2. Delete the `Xplore server` shortcut in `shell:startup`, and the desktop icon.
3. Quit the server (see above).
4. Delete the Xplore folder. **This deletes your library.** Keep `xbookmarks.db` if you want it
   later.
5. Optionally, delete `%USERPROFILE%\.xplore` (the desktop window's settings).

---

## 11. Known limits

- **X changes things.** If X changes how its site loads bookmarks, capture can stop until
  Xplore is updated. Anything that fails to be read is kept in `unparsed\`, so it can be
  recovered afterwards.
- **Videos aren't played in Xplore.** Open them on X. Photos need an internet connection.
- **Hard paywalls:** some articles can't be recovered at all, if archive.today has no copy.
- **Greek search is sensitive to accents** (see Searching).
- **Related is word-based.** It can connect two posts that merely share a word, like "data".
  AI-assisted ranking is planned.
- **The date you bookmarked** is exact for bookmarks collected by a sync, including the first
  Deep scan.
- **Tested on Windows 11 with Chrome only.**

---

## 12. What's in the folder

| File or folder | What it is |
|---|---|
| `xplore.pyw` | The desktop window (what the desktop icon runs) |
| `server.py` | The Xplore server |
| `start-collector.vbs`, `start.bat` | Start the server without a window |
| `create-shortcut.ps1` | Makes the desktop icon |
| `extension\` | The Chrome extension |
| `ui\`, `assets\` | The app's interface, logo and icons |
| `store.py`, `parse.py`, `related.py`, `capture.py`, `urls.py` | The library, bookmark reading, Related, Recover, link cleaning |
| `resolve_links.py`, `enrich_links.py`, `archive_links.py`, `transcripts.py` | The archiving commands (section 6) |
| `topics.py` | Topics (section 8) |
| `export_links.py`, `export_unrecovered.py` | Export your links, or the Recover list, as CSV and HTML files |
| `redate_bookmarks.py`, `retrim_articles.py` | Maintenance tools; not needed in normal use |
| `test_parse.py` | A self-test: `python test_parse.py` |
| `requirements.txt` | The Python packages |
| **Created when you use it:** `xbookmarks.db` | **Your library.** Back this file up. |
| `unparsed\`, `*.log` | Data X sent that couldn't be read, and logs |
