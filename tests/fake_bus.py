#!/usr/bin/python3
"""A session bus of its own with a stand-in for the desktop's notification service, for the tests.

    tests/fake_bus.py LOG

starts a private D-Bus daemon, takes the name org.freedesktop.Notifications
on it and prints the bus's address, alone on a line, once it is ready to be
used as DBUS_SESSION_BUS_ADDRESS. Every notification sent there is appended
to LOG as one line of JSON: app, replaces, icon, summary, body, actions,
hints, expire. While a file named LOG.refuse exists, the service answers
with an error instead, as a desktop without notifications would.

It ends on SIGTERM, and when the program that started it is gone. Nothing
here can reach the real session bus: the tests must never notify the person
whose machine they run on.
"""

import ctypes
import json
import os
import signal
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

NAME = "org.freedesktop.Notifications"
INTERFACE = """<node><interface name="org.freedesktop.Notifications">
  <method name="Notify">
    <arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/>
    <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/>
    <arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/>
  </method>
</interface></node>"""
KEYS = ("app", "replaces", "icon", "summary", "body", "actions", "hints", "expire")
PR_SET_PDEATHSIG = 1


def main(log: str) -> int:
    # Dies with whatever started it, so a test run that is killed leaves no bus behind.
    ctypes.CDLL(None).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    Gio.TestDBus.unset()
    bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    bus.up()
    address = bus.get_bus_address()
    connection = Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)
    loop = GLib.MainLoop()
    sent = 0

    def called(_connection, _sender, _path, _interface, _method, parameters, invocation):
        nonlocal sent
        if os.path.exists(log + ".refuse"):
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", "refused, as the test asked")
            return
        sent += 1
        with open(log, "a", encoding="utf-8") as out:
            out.write(json.dumps(dict(zip(KEYS, parameters.unpack())), ensure_ascii=False) + "\n")
        invocation.return_value(GLib.Variant("(u)", (sent,)))

    interface = Gio.DBusNodeInfo.new_for_xml(INTERFACE).interfaces[0]
    register = getattr(connection, "register_object_with_closures2", None) or connection.register_object
    register("/org/freedesktop/Notifications", interface, called, None, None)
    Gio.bus_own_name_on_connection(connection, NAME, Gio.BusNameOwnerFlags.NONE,
                                   lambda *_: print(address, flush=True), lambda *_: loop.quit())
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    # Python only runs its signal handlers between two of its own instructions; this gives it some.
    GLib.timeout_add(100, lambda: True)
    try:
        loop.run()
    finally:
        connection.close_sync(None)
        bus.down()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: fake_bus.py LOG")
    sys.exit(main(sys.argv[1]))
