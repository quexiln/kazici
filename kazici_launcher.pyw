"""kazici launcher (opens no console window).

On double-click: if the web server is not running, starts it in the background, then opens the UI in the browser.
If the server is already running, only opens the browser.

    kazici_launcher.pyw            -> start + open in browser
    kazici_launcher.pyw --quiet    -> start only (used at Windows startup)
    kazici_launcher.pyw --port 9000

Server output goes to kazici-web.log. To stop it, use the "Stop server" button in the UI.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "kazici-web.log"


def _arg(name: str, default: str | None = None) -> str | None:
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else default
    return default


PORT = int(_arg("--port", os.environ.get("KAZICI_PORT", "8765")))
URL = f"http://127.0.0.1:{PORT}/"


def alive() -> bool:
    """Is a kazici server answering on this port?"""
    try:
        with urllib.request.urlopen(URL + "api/jobs", timeout=1.5) as r:
            return r.status == 200 and b'"jobs"' in r.read(200)
    except Exception:
        return False


def console_python() -> str:
    """The server must run with python.exe and a hidden console (not pythonw): pythonw has no console, so
    every console program it starts (the Claude agent, the Playwright driver) opens its own visible cmd
    window. Child processes share the hidden console, so no window appears."""
    exe = Path(sys.executable)
    py = exe.with_name("python.exe")
    return str(py if py.exists() else exe)


def message(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "kazici", 0x10)
    except Exception:
        print(text)


def start_server() -> bool:
    log = open(LOG, "a", encoding="utf-8")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting (port {PORT}) ---\n")
    log.flush()
    flags = 0
    if sys.platform == "win32":
        # Do not use DETACHED_PROCESS: it overrides CREATE_NO_WINDOW and child processes open windows.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    subprocess.Popen([console_python(), "-m", "kazici", "web", "--no-open", "--port", str(PORT)],
                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True, env=env)
    deadline = time.time() + 40
    while time.time() < deadline:
        if alive():
            return True
        time.sleep(0.4)
    return False


def main() -> None:
    quiet = "--quiet" in sys.argv
    if not alive():
        if not start_server():
            tail = ""
            try:
                tail = "\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
            except Exception:
                pass
            message(f"Could not start the server (port {PORT}).\n\nLog: {LOG}\n\n{tail}")
            return
    if not quiet:
        webbrowser.open(URL)


if __name__ == "__main__":
    main()
