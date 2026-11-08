"""Installs / removes Windows shortcuts for the kazici launcher.

    python install_shortcuts.py               -> "kazici" shortcut on the desktop and in the Start menu
    python install_shortcuts.py --startup     -> also start the server quietly at Windows startup
    python install_shortcuts.py --uninstall   -> remove all kazici shortcuts (including the old "kazıcı" ones)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAUNCHER = ROOT / "kazici_launcher.pyw"
ICON = ROOT / "kazici" / "static" / "kazici.ico"
NAME = "kazici.lnk"
OLD_NAMES = ("kazıcı.lnk",)  # shortcut name used by earlier versions

PS_CREATE = r"""
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut($env:K_LNK)
$lnk.TargetPath = $env:K_TARGET
$lnk.Arguments = $env:K_ARGS
$lnk.WorkingDirectory = $env:K_WD
$lnk.IconLocation = $env:K_ICON
$lnk.Description = $env:K_DESC
$lnk.Save()
"""


def folder(name: str) -> Path:
    """Desktop / Programs / Startup folder (including OneDrive redirection)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; [Environment]::GetFolderPath('{name}')"],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    return Path(out.stdout.strip())


def pythonw() -> str:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else exe)


def make_icon() -> None:
    if ICON.exists():
        return
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=72, fill=(107, 114, 207, 255))
    w = 24
    d.line((80, 72, 80, 184), fill="white", width=w)
    d.line((80, 128, 150, 72), fill="white", width=w)
    d.line((106, 108, 160, 184), fill="white", width=w)
    for x, y in ((80, 72), (80, 184), (150, 72), (160, 184)):
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill="white")
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def shortcut(path: Path, args: str, desc: str) -> None:
    env = {**os.environ, "K_LNK": str(path), "K_TARGET": pythonw(), "K_ARGS": args, "K_WD": str(ROOT),
           "K_ICON": f"{ICON},0" if ICON.exists() else "", "K_DESC": desc}
    subprocess.run(["powershell", "-NoProfile", "-Command", PS_CREATE], env=env, check=True)
    print(f"shortcut: {path}")


def main() -> None:
    places = {"Desktop": folder("Desktop"), "Programs": folder("Programs"), "Startup": folder("Startup")}
    if "--uninstall" in sys.argv:
        for p in places.values():
            for name in (NAME, *OLD_NAMES):
                f = p / name
                if f.exists():
                    f.unlink()
                    print(f"removed: {f}")
        return
    make_icon()
    launch = f'"{LAUNCHER}"'
    shortcut(places["Desktop"] / NAME, launch, "Start the kazici web UI")
    shortcut(places["Programs"] / NAME, launch, "Start the kazici web UI")
    if "--startup" in sys.argv:
        shortcut(places["Startup"] / NAME, launch + " --quiet", "Start the kazici server at sign-in")


if __name__ == "__main__":
    main()
