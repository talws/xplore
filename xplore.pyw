"""
xplore.pyw — Xplore as a desktop app: a branded window over the local server.

Double-click the desktop icon (create-shortcut.ps1 makes it):

  * if the server already answers on 8502, the window opens on the app at once;
  * otherwise the server is started hidden, exactly as the login script starts
    it (start-collector.vbs), and the window shows the logo while it binds.
    That takes 40-60 seconds, because the server counts the database first;
  * a second double-click brings the open window forward instead of opening
    another, and never starts a second server.

Closing the window leaves the server running. Capture depends on it: the
extension delivers bookmarks every hour whether or not the window is open.

Runs under pythonw, so there is no console and sys.stdout is None. Output goes
to a UTF-8 log instead; a legacy codepage has killed this project's server at
startup before.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8502
URL = f"http://localhost:{PORT}/"
TITLE = "Xplore"
ICON = os.path.join(HERE, "assets", "xplore.ico")
BIND_TIMEOUT = 180                 # seconds; binding normally takes 40-60
STORAGE = os.path.join(os.path.expanduser("~"), ".xplore", "webview")

if sys.stdout is None or sys.stderr is None:
    _log = open(os.path.join(HERE, "xplore-window.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def server_alive() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/ping", timeout=2) as r:
            return json.load(r).get("service") == "xcollector"
    except Exception:
        return False


def start_server() -> None:
    """The login script's own launch: hidden, logging to server.log."""
    subprocess.Popen(["wscript.exe", os.path.join(HERE, "start-collector.vbs")],
                     cwd=HERE, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)


def wait_for_server() -> bool:
    if server_alive():
        return True
    log("server not answering; starting it")
    start_server()
    t0 = time.time()
    while time.time() - t0 < BIND_TIMEOUT:
        time.sleep(1.5)
        if server_alive():
            log(f"server up after {time.time() - t0:.0f}s")
            return True
    log("server did not come up")
    return False


_mutex = None


def already_open() -> bool:
    """One window. A named mutex says whether another launcher holds it; if so,
    bring that window forward and stop."""
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, r"Local\Xplore.Window")
    if ctypes.get_last_error() != 183:          # ERROR_ALREADY_EXISTS
        return False
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)              # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return True


def _data_uri(name: str) -> str:
    with open(os.path.join(HERE, "assets", name), "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode()


def splash(message: str, detail: str, busy: bool = True) -> str:
    bar = '<div class="bar"><i></i></div>' if busy else '<div class="gap"></div>'
    return f"""<!doctype html><meta charset="utf-8"><title>{TITLE}</title>
<style>
 :root{{color-scheme:light dark;--bg:#f3f6fb;--ink:#53607c;--bar:#006cf0;--track:#dfe6f2}}
 @media (prefers-color-scheme:dark){{:root{{--bg:#090e1b;--ink:#9ca8c4;--bar:#4fa3ff;--track:#1d2843}}
   .l{{display:none!important}} .d{{display:block!important}}}}
 html,body{{height:100%;margin:0;background:var(--bg);color:var(--ink);
   font:14px/1.5 system-ui,"Segoe UI",sans-serif}}
 body{{display:grid;place-items:center}}
 .w{{text-align:center;padding:24px 16px}}
 img{{height:64px;width:auto;max-width:80vw;margin:0 auto;display:block}} .d{{display:none}}
 .bar{{width:220px;height:3px;background:var(--track);border-radius:3px;margin:30px auto 16px;overflow:hidden}}
 .bar i{{display:block;width:40%;height:100%;background:var(--bar);border-radius:3px;
   animation:s 1.2s ease-in-out infinite}}
 .gap{{height:30px}}
 @keyframes s{{from{{transform:translateX(-100%)}}to{{transform:translateX(250%)}}}}
 small{{display:block;margin-top:4px;opacity:.8}}
</style>
<div class="w">
 <img class="l" src="{_data_uri('xplore-logo-light-h64.png')}" alt="Xplore">
 <img class="d" src="{_data_uri('xplore-logo-dark-h64.png')}" alt="">
 {bar}<div>{message}</div><small>{detail}</small>
</div>"""


def run_window() -> None:
    import webview

    # Its own taskbar identity, so Windows shows the Xplore icon rather than
    # grouping the window under pythonw.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Xplore.Desktop")
    except Exception:
        pass

    up = server_alive()
    window = webview.create_window(
        TITLE,
        url=URL if up else None,
        html=None if up else splash("Starting Xplore…",
                                    "The server counts the library first; this takes up to a minute."),
        width=1440, height=920, min_size=(420, 560),
        background_color="#F3F6FB", text_select=True)

    def load_when_ready():
        if up:
            return
        if wait_for_server():
            window.load_url(URL)
        else:
            window.load_html(splash("Xplore's server did not start.",
                                    f"Its log is server.log in {HERE}. Close this window and try again.",
                                    busy=False))

    os.makedirs(STORAGE, exist_ok=True)
    # Not private: the theme choice and the Recover list's "opened" marks
    # live in the page's storage and should survive a restart.
    webview.start(load_when_ready, icon=ICON, private_mode=False, storage_path=STORAGE)


def run_edge_fallback() -> None:
    """If the embedded window cannot start, an Edge app window is the next best."""
    edge = shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if wait_for_server() and os.path.exists(edge):
        subprocess.Popen([edge, f"--app={URL}"], close_fds=True)


def main() -> None:
    if already_open():
        return
    try:
        run_window()
    except Exception:
        log("window failed:\n" + traceback.format_exc())
        run_edge_fallback()


if __name__ == "__main__":
    main()
