# -*- coding: utf-8 -*-
"""Launch Tokenarium in the default browser or print a private snapshot."""

import argparse
import json
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

import tokenarium_server as server


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="Tokenarium · local Codex habitat")
    parser.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--snapshot", action="store_true",
                        help="print privacy-safe local metadata and exit")
    parser.add_argument("--all", action="store_true", help="include older local threads")
    args = parser.parse_args()

    if args.snapshot:
        print(json.dumps(server.public_snapshot(include_old=args.all),
                         ensure_ascii=False, indent=2))
        return 0

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), server.Handler)
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.35), webbrowser.open(url)),
                         daemon=True).start()
    print(f"✺ Tokenarium · 令牌生态园\n  {url}\n  Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
