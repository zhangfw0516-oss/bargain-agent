"""Entry point of the packaged Windows app (BargainAgent.exe).

Double-click: start the local web UI and open it in the default browser. If it is
already running, just open the browser. ``BargainAgent.exe --check-due`` checks every
due monitor once and exits (for Windows Task Scheduler).
"""

from __future__ import annotations

import socket
import sys
import threading
import webbrowser

import config
import webapp

URL = f"http://{webapp.HOST}:{webapp.DEFAULT_PORT}/"


def _running() -> bool:
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((webapp.HOST, webapp.DEFAULT_PORT)) == 0


def check_due() -> int:
    import cli

    logs = config.APP_DIR / "logs"
    logs.mkdir(exist_ok=True)
    return cli.main(["check-due", "--log", str(logs / "check-due.log")])


def main() -> int:
    if "--check-due" in sys.argv[1:]:
        return check_due()
    if not config.env_file().exists():
        print(f"Settings file not found: {config.env_file()}")
        input("Press Enter to close.")
        return 1
    if _running():
        print("Bargain Agent is already running; opening the browser.")
        webbrowser.open(URL)
        return 0
    httpd = webapp.serve()
    print("Bargain Agent is running at", URL)
    print("Keep this window open while you use it. Close it to stop.")
    threading.Timer(1.0, webbrowser.open, args=(URL,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
