# -*- coding: utf-8 -*-
"""Tokenarium desktop app: frameless pywebview + the local observer server."""

import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import webview

import tokenarium_server as server
import zoo_app as shell


PORT = 8789


def start_server():
    server.PORT = PORT
    server.UI["want"] = "max"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    except OSError:
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def worker(window):
    hwnd = shell.find_window(os.getpid(), time.time() + 15)
    if not hwnd:
        print("Could not find Tokenarium's native window")
        return

    for _ in range(30):
        try:
            cards = window.evaluate_js("document.querySelectorAll('.habitat').length")
            ready = window.evaluate_js("document.readyState === 'complete'")
        except Exception:
            cards = None
            ready = False
        if cards is not None and ready:
            print("Rendered %s habitats; local HTTP controls ready" % cards)
            break
        time.sleep(0.3)

    alive = {"value": True}
    window.events.closed += lambda: alive.__setitem__("value", False)

    def close():
        alive["value"] = False
        try:
            window.destroy()
        except Exception:
            pass

    # Supplies right-bottom placement, rounded native region, fade-on-work,
    # multi-monitor avoidance, topmost behavior, and compact/expanded sizing.
    shell.drive(hwnd, lambda: alive["value"], close, js=None)


def configure_shell():
    shell.zoo_server = server
    shell.W = 430
    shell.H = 720
    shell.TOP_GAP = 130
    shell.MIN_H = 54
    shell.POS = "br"
    shell.TOPMOST = True
    shell.ALPHA = 96
    shell.GHOST = 18
    shell.DYNAMIC = True
    shell.FOLLOW = True
    shell.GRACE = 1.15
    shell.TITLE_HINT = "Tokenarium"


def main() -> int:
    configure_shell()
    shell.parse_args(sys.argv[1:])
    shell.dpi_aware()

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokenarium.log")
    log = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = log
    print("\n=== %s start pid=%d ===" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))

    httpd = start_server()
    if httpd is None:
        print("Tokenarium desktop app is already running on port %d" % PORT)
        return 0

    x, y, width, height = shell.target_rect()
    scale = shell.scale()
    window = webview.create_window(
        "Tokenarium",
        url="http://127.0.0.1:%d/?desktop=1" % PORT,
        frameless=True,
        easy_drag=False,
        shadow=True,
        on_top=shell.TOPMOST,
        # The widget must be activatable so WebView2 can receive button clicks.
        # We still keep it out of the taskbar via shell.make_widget.
        focus=True,
        width=int(width / scale),
        height=int(height / scale),
        x=int(x / scale),
        y=int(y / scale),
        min_size=(360, 54),
        background_color="#07110f",
        text_select=False,
    )
    storage = os.path.join(os.environ.get("LOCALAPPDATA", os.getcwd()),
                           "Tokenarium", "webview")
    try:
        webview.start(worker, window, gui="edgechromium", private_mode=False,
                      storage_path=storage)
    finally:
        httpd.shutdown()
        httpd.server_close()
    print("=== %s closed ===" % time.strftime("%Y-%m-%d %H:%M:%S"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
