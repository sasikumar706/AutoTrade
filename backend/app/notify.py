"""Phone notifications. Two ways, either or both:

1. Built-in (no extra app, no account): when the server runs on the phone in Termux, it shows a normal Android
   notification itself through Termux:API (`pkg install termux-api` + the Termux:API add-on from the same store
   as Termux). Tapping it opens Kite Algo. Works offline. Turn off with PHONE_NOTIFY=false in backend/.env.
2. ntfy.sh (optional): for a server that is NOT on the phone (cloud/PC). Set NTFY_TOPIC in backend/.env.

Sending never blocks or breaks trading: it runs in a background thread and errors are ignored.
"""
from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import threading
import urllib.request

APP_PACKAGE = "com.sasikumar.kitealgo"
_ids = itertools.count(1)


def _termux_available() -> bool:
    return (os.environ.get("PHONE_NOTIFY", "true").strip().lower() not in ("0", "false", "no", "off")
            and shutil.which("termux-notification") is not None)


def _ntfy_on() -> bool:
    return bool(os.environ.get("NTFY_TOPIC", "").strip())


def enabled() -> bool:
    return _termux_available() or _ntfy_on()


def channels() -> list[str]:
    return [n for n, on in (("phone", _termux_available()), ("ntfy", _ntfy_on())) if on]


def _termux(title: str, message: str, priority: str) -> bool:
    try:
        cmd = ["termux-notification", "--title", title, "--content", message, "--id", f"kitealgo{next(_ids) % 50}",
               "--group", "kitealgo", "--priority", "high" if priority in ("high", "urgent") else "default",
               "--action", f"am start -n {APP_PACKAGE}/.MainActivity"]
        if priority in ("high", "urgent"):
            cmd += ["--vibrate", "300,150,300", "--sound"]
        r = subprocess.run(cmd, timeout=15, capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def _ntfy(title: str, message: str, priority: str, tags: str) -> bool:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(f"{server}/{topic}", data=message.encode("utf-8"), method="POST",
                                 headers={"Title": title.encode("ascii", "ignore").decode() or "Kite Algo",
                                          "Priority": priority, "Tags": tags})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _post(title: str, message: str, priority: str, tags: str) -> bool:
    ok = False
    if _termux_available():
        ok = _termux(title, message, priority) or ok
    if _ntfy_on():
        ok = _ntfy(title, message, priority, tags) or ok
    return ok


def send(title: str, message: str, priority: str = "default", tags: str = "chart_with_upwards_trend", wait: bool = False) -> bool:
    if not enabled():
        return False
    if wait:
        return _post(title, message, priority, tags)
    threading.Thread(target=_post, args=(title, message, priority, tags), daemon=True).start()
    return True
