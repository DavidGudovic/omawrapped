"""Hands a finished card to the desktop: the clipboard, the file manager, a notification, Omarchy's menu.

Every helper starts a program that ships with Omarchy. It looks for the program first and raises ShareError, with a
sentence that can be shown to the user as it is, when it cannot do its job.
"""

import shutil
import subprocess
from pathlib import Path

from . import system

# The plugin's bar icon: Material Design "chart box" in the Nerd Font.
GLYPH = "\U000f154d"
APP_NAME = "OmaWrapped"
# Long enough for a busy session bus; short enough that a stuck program does not hang the command.
TIMEOUT = 10
# How long the menu may stay open. Nobody reads a menu for ten minutes; one that is still there then lost its
# shell, and the command waiting on it would otherwise wait for good.
MENU_TIMEOUT = 600
DETACHED = {
    "start_new_session": True,
    "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
}


class ShareError(Exception):
    """The card could not be handed over; the message says why, in one sentence."""


def latest_card():
    """The newest omawrapped-*.png in the Pictures folder, by modification time. None when there is none."""
    newest, newest_key = None, None
    try:
        found = list(system.pictures_dir().glob("omawrapped-*.png"))
    except OSError:
        return None
    for path in found:
        try:
            if not path.is_file():
                continue
            # The name settles a tie, so that the answer does not depend on the order the folder is listed in.
            key = (path.stat().st_mtime_ns, path.name)
        except OSError:
            continue
        if newest_key is None or key > newest_key:
            newest, newest_key = path, key
    return newest


# ---- the clipboard ----

def _wl_copy(arguments: list, source: Path = None) -> None:
    """Runs wl-copy with the arguments and, when given, the file `source` on its standard input."""
    if not shutil.which("wl-copy"):
        raise ShareError("wl-copy was not found (package wl-clipboard), so nothing was copied.")
    try:
        stdin = open(source, "rb") if source else subprocess.DEVNULL
    except OSError as error:
        raise ShareError("%s could not be read, so nothing was copied." % source) from error
    # wl-copy stays behind to serve the clipboard. It must not inherit a
    # pipe of ours, or reading that pipe would wait for it forever.
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        done = subprocess.run(["wl-copy", *arguments], stdin=stdin, timeout=TIMEOUT, **quiet)
    except (OSError, subprocess.SubprocessError) as error:
        raise ShareError("wl-copy could not copy the card. Is a Wayland session running?") from error
    finally:
        if source:
            stdin.close()
    if done.returncode != 0:
        raise ShareError("wl-copy could not copy the card. Is a Wayland session running?")


def copy_image(path: Path) -> None:
    """Puts the picture itself on the clipboard, to paste into a post or a chat."""
    kind = "image/svg+xml" if path.suffix.lower() == ".svg" else "image/png"
    _wl_copy(["--type", kind], path)


def copy_path(path: Path) -> None:
    """Puts the file's path on the clipboard, as text."""
    _wl_copy(["--", str(path)])


# ---- the file manager ----

def _start(command: list, failure: str) -> None:
    """Starts a program that outlives this command, with nothing of ours attached to it."""
    try:
        subprocess.Popen(command, **DETACHED)
    except OSError as error:
        raise ShareError(failure) from error


def open_file(path: Path) -> None:
    """Opens the file with the program the desktop has for it."""
    if not shutil.which("xdg-open"):
        raise ShareError("xdg-open was not found (package xdg-utils), so the card was not opened.")
    _start(["xdg-open", str(path)], "xdg-open could not be started, so the card was not opened.")


def show_in_folder(path: Path) -> None:
    """Shows the file in the file manager, selected. Without Nautilus, the folder it is in."""
    if shutil.which("nautilus"):
        command = ["nautilus", "--select", str(path)]
        # Omarchy starts desktop apps through uwsm-app, so that the session manages them.
        if shutil.which("uwsm-app"):
            command = ["uwsm-app", "--", *command]
    elif shutil.which("xdg-open"):
        command = ["xdg-open", str(path.parent)]
    else:
        raise ShareError("No file manager was found to show the card in. It is at %s." % path)
    _start(command, "%s could not be started, so the card was not shown." % command[0])


# ---- notifications and the menu ----

def notify(headline: str, body: str = "", image: Path = None, click: list = None) -> bool:
    """Says something on the desktop. True when a notification was sent.

    Omarchy's own tool is preferred: it can make the notification clickable,
    and click is the command a click runs. The outcome of whatever this is
    about was already printed, so a desktop without notifications is not an
    error here, only a False.
    """
    if shutil.which("omarchy-notification-send"):
        command = ["omarchy-notification-send", "--app-name", APP_NAME, "-g", GLYPH]
        if image:
            command += ["--image", str(image)]
        command += [headline, body]
        # --exec takes the rest of the line as the command, so it has to come last.
        if click:
            command += ["--exec", *map(str, click)]
    elif shutil.which("notify-send"):
        command = ["notify-send", "-a", APP_NAME]
        if image:
            command += ["-i", str(image)]
        command += [headline, body]
    else:
        return False
    try:
        done = subprocess.run(command, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def choose(prompt: str, options: list):
    """Asks the user to pick one of the options in Omarchy's menu. The label picked, or None when it is dismissed.

    An option may be "<glyph><TAB><label>": the menu shows the glyph and
    returns the label alone. The menu stays open until the user answers; one
    left open for MENU_TIMEOUT seconds counts as dismissed.
    """
    if not shutil.which("omarchy-menu-select"):
        raise ShareError("omarchy-menu-select was not found, so there is no menu to show. "
                         "`omawrapped copy` and `omawrapped show` do the same from a terminal.")
    try:
        done = subprocess.run(["omarchy-menu-select", prompt, *options], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                              timeout=MENU_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    except (OSError, subprocess.SubprocessError) as error:
        raise ShareError("omarchy-menu-select could not be started, so there is no menu to show.") from error
    # The menu exits with 1 and prints nothing when it is dismissed.
    if done.returncode == 1:
        return None
    if done.returncode != 0:
        raise ShareError("omarchy-menu-select failed, so there is no menu to show.")
    return done.stdout.rstrip("\n") or None
