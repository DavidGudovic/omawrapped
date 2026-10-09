"""Hands a finished card to the desktop: the clipboard, the file manager, a notification, Omarchy's menu.

The helpers for the clipboard, the file manager and the menu start a program that ships with Omarchy. Each looks
for its program first and raises ShareError, with a sentence that can be shown to the user as it is, when it cannot
do its job.

What a program is started with can be read by every user of the machine, in the process list. So nothing about
what the user did goes into a program's arguments: a notification is sent from here, over the session bus, and
text for the clipboard goes in through the program's standard input. The only thing of the user's that is ever
an argument is the path of the card, for the two programs that open it.
"""

import json
import os
import shutil
import subprocess
import threading
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

def _wl_copy(arguments: list, source: Path = None, text: str = None) -> None:
    """Runs wl-copy with the arguments. What it copies comes in on its standard input: the file `source` or `text`."""
    if not shutil.which("wl-copy"):
        raise ShareError("wl-copy was not found (package wl-clipboard), so nothing was copied.")
    try:
        stdin = open(source, "rb") if source else None
    except OSError as error:
        raise ShareError("%s could not be read, so nothing was copied." % source) from error
    # wl-copy stays behind to serve the clipboard. It must not inherit a
    # pipe of ours, or reading that pipe would wait for it forever.
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if source:
            done = subprocess.run(["wl-copy", *arguments], stdin=stdin, timeout=TIMEOUT, **quiet)
        else:
            done = subprocess.run(["wl-copy", *arguments], input=os.fsencode(text), timeout=TIMEOUT, **quiet)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
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
    _wl_copy(["--type", "text/plain"], text=str(path))


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

NOTIFICATIONS = "org.freedesktop.Notifications"


def _bus_address():
    """Where the session bus listens, or None when there is none. One is never started from here."""
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    # "autolaunch:" asks for a bus to be started; that is not this command's to do.
    if address and not address.startswith("autolaunch:"):
        return address
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime, "bus").exists():
        return "unix:path=%s/bus" % runtime
    return None


def _gio():
    """(Gio, GLib) from python-gobject, which Omarchy ships. Raises ImportError or ValueError without it."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    return Gio, GLib


def cannot_notify():
    """Why no notification can be sent, in a few words. None when one can."""
    try:
        _gio()
    except (ImportError, ValueError):
        return "python-gobject is not installed"
    return None if _bus_address() else "there is no session bus"


def notify(headline: str, body: str = "", image: Path = None, click: list = None) -> bool:
    """Says something on the desktop. True when the notification service took it.

    The notification is handed to the service by this process, over the
    session bus. It is never given to a program to send, not Omarchy's
    omarchy-notification-send either: that would put the text into the
    program's arguments, where every user of the machine can read it, and a
    notification may say which apps were used and for how long.

    What is sent is what omarchy-notification-send sends: Omarchy's shell
    takes the icon from the omarchy-glyph hint and runs click, a command as a
    list of words, when the notification is clicked. The outcome of whatever
    this is about was already printed, so a desktop without notifications is
    not an error here, only a False.
    """
    address = _bus_address()
    if not address:
        return False
    try:
        Gio, GLib = _gio()
    except (ImportError, ValueError):
        return False
    try:
        hints = {"urgency": GLib.Variant("y", 0), "omarchy-glyph": GLib.Variant("s", GLYPH)}
        if image:
            hints["image-path"] = GLib.Variant("s", str(image))
        if click:
            words = json.dumps([str(word) for word in click], ensure_ascii=False, separators=(",", ":"))
            hints["omarchy-exec-argv"] = GLib.Variant("s", words)
        # app name, id to replace, app icon, summary, body, actions, hints, the server's own expiry time
        message = GLib.Variant("(susssasa{sv}i)", (APP_NAME, 0, "", headline, body, [], hints, -1))
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        # One limit for the whole exchange. Something that takes the connection and then says nothing would
        # otherwise keep this command, and the widget that waits for it, for good.
        patience = Gio.Cancellable()
        limit = threading.Timer(TIMEOUT, patience.cancel)
        limit.daemon = True
        limit.start()
        try:
            connection = Gio.DBusConnection.new_for_address_sync(address, flags, None, patience)
            try:
                connection.call_sync(NOTIFICATIONS, "/org/freedesktop/Notifications", NOTIFICATIONS, "Notify",
                                     message, GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, int(TIMEOUT * 1000),
                                     patience)
            finally:
                _hang_up(connection, patience, GLib)
        finally:
            limit.cancel()
    # GLib.Error: no bus, no service there, or no answer in time. The others: text that cannot be sent, such as
    # half a character.
    except (GLib.Error, TypeError, ValueError):
        return False
    return True


def _hang_up(connection, patience, GLib) -> None:
    """Closes the connection. A notification that was taken stays taken if the goodbye fails."""
    try:
        connection.close_sync(patience)
    except GLib.Error:
        pass


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
