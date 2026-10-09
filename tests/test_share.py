import support  # noqa: F401  (must stay first: it disables bytecode and isolates the environment)

import json
import os
import shutil
import subprocess
import unittest
import warnings
from pathlib import Path
from unittest import mock

from omawrapped import share
from support import GLYPH, STUBBED, Bus, IsolatedCase, StubbedCase, Stubs, notification, real_tool

COPY_FAILED = "wl-copy could not copy the card. Is a Wayland session running?"
NO_WL_COPY = "wl-copy was not found (package wl-clipboard), so nothing was copied."
NO_MENU = ("omarchy-menu-select was not found, so there is no menu to show. "
           "`omawrapped copy` and `omawrapped show` do the same from a terminal.")
# Every byte value, a NUL and a newline included: nothing may get lost on the way to the clipboard.
IMAGE = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


class ShareCase(StubbedCase):
    def card(self, name: str, mtime: int = None, content: bytes = IMAGE) -> Path:
        """A file in the Pictures folder. mtime (seconds) is set explicitly: the order must not depend on the clock."""
        path = self.pictures / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def spied(self, function):
        """Patches subprocess.<function> to record its calls and still run it (the program is a stand-in)."""
        patcher = mock.patch.object(share.subprocess, function, wraps=getattr(subprocess, function))
        self.addCleanup(patcher.stop)
        return patcher.start()


class DetachedCase(ShareCase):
    """For the helpers that start a program and leave it running, which nobody waits for: Popen's warning is moot."""

    def setUp(self):
        super().setUp()
        catcher = warnings.catch_warnings()
        catcher.__enter__()
        self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)


class LatestCardTests(ShareCase):
    def test_none_without_a_pictures_folder(self):
        self.assertFalse(self.pictures.exists())
        self.assertIsNone(share.latest_card())

    def test_none_in_an_empty_folder(self):
        self.pictures.mkdir()
        self.assertIsNone(share.latest_card())

    def test_one_card(self):
        path = self.card("omawrapped-2026-10-09.png", 1000)
        self.assertEqual(share.latest_card(), path)

    def test_the_newest_of_several_by_modification_time_not_by_name(self):
        # The names run the other way round, so neither sorting by name nor by the date in it gives the answer.
        self.card("omawrapped-2026-10-09.png", 1000)
        newest = self.card("omawrapped-2026-09-01-month.png", 3000)
        self.card("omawrapped-2026-10-02.png", 2000)
        self.assertEqual(share.latest_card(), newest)

    def test_a_card_touched_later_is_the_newest(self):
        first = self.card("omawrapped-2026-10-02.png", 1000)
        second = self.card("omawrapped-2026-10-09.png", 2000)
        self.assertEqual(share.latest_card(), second)
        os.utime(first, (3000, 3000))
        self.assertEqual(share.latest_card(), first)

    def test_files_that_are_not_cards_are_ignored_however_new(self):
        old = self.card("omawrapped-2026-10-02.png", 1000)
        for name in ("omawrapped-2026-10-09.jpg", "omawrapped-2026-10-09.svg", "omawrapped.png", "omawrapped-.txt",
                     "screenshot-2026-10-09.png", "Omawrapped-2026-10-09.png", ".omawrapped-2026-10-09.png.1.part",
                     "notes.txt"):
            self.card(name, 9000)
        self.assertEqual(share.latest_card(), old)

    def test_without_a_card_other_files_give_none(self):
        self.card("screenshot.png", 1000)
        self.assertIsNone(share.latest_card())

    def test_a_folder_that_looks_like_a_card_is_ignored(self):
        old = self.card("omawrapped-2026-10-02.png", 1000)
        folder = self.pictures / "omawrapped-2026-10-09.png"
        folder.mkdir()
        os.utime(folder, (9000, 9000))
        self.assertEqual(share.latest_card(), old)

    def test_only_a_folder_gives_none(self):
        (self.pictures / "omawrapped-2026-10-09.png").mkdir(parents=True)
        self.assertIsNone(share.latest_card())

    def test_cards_in_subfolders_are_not_looked_for(self):
        self.card("old/omawrapped-2026-10-09.png", 9000)
        self.assertIsNone(share.latest_card())

    def test_the_pictures_folder_follows_xdg_pictures_dir(self):
        elsewhere = self.tmp / "My Photos"
        with mock.patch.dict(os.environ, {"XDG_PICTURES_DIR": str(elsewhere)}):
            self.assertIsNone(share.latest_card())
            elsewhere.mkdir()
            path = elsewhere / "omawrapped-2026-10-09.png"
            path.write_bytes(IMAGE)
            self.assertEqual(share.latest_card(), path)

    def test_equal_times_give_the_same_answer_every_time(self):
        self.card("omawrapped-2026-10-02.png", 1000)
        later = self.card("omawrapped-2026-10-09.png", 1000)
        self.assertEqual(share.latest_card(), later)

    def test_the_result_is_a_path_in_the_pictures_folder(self):
        path = self.card("omawrapped-2026-10-09.png", 1000)
        found = share.latest_card()
        self.assertIsInstance(found, Path)
        self.assertEqual(found.parent, self.pictures)
        self.assertEqual(found, path)


class CopyTests(ShareCase):
    def test_copy_image_runs_wl_copy_with_the_png_type_and_the_file_on_stdin(self):
        path = self.card("omawrapped-2026-10-09.png")
        share.copy_image(path)
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "image/png"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), IMAGE)

    def test_copy_image_of_an_svg_says_so(self):
        for name in ("card.svg", "card.SVG"):
            with self.subTest(name=name):
                path = self.card(name, content=b"<svg/>\n")
                share.copy_image(path)
                self.assertEqual(self.stubs.argv("wl-copy")[-1], ["--type", "image/svg+xml"])
                self.assertEqual(self.stubs.stdin("wl-copy"), b"<svg/>\n")

    def test_copy_image_of_any_other_name_is_sent_as_a_png(self):
        path = self.card("mine.PNG")
        share.copy_image(path)
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "image/png"]])

    def test_copy_path_runs_wl_copy_with_the_text_type_and_the_path_on_stdin(self):
        share.copy_path(Path("/some folder/omawrapped-2026-10-09.png"))
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "text/plain"]])
        # The path, byte for byte: not a newline after it, as echo would add.
        self.assertEqual(self.stubs.stdin("wl-copy"), b"/some folder/omawrapped-2026-10-09.png")

    def test_the_path_is_not_among_the_arguments_of_wl_copy(self):
        # What a program is started with can be read by every user of the machine: the path goes in on stdin.
        share.copy_path(Path("/home/me/secret-client/omawrapped-2026-10-09.png"))
        self.assertEqual([argument for call in self.stubs.argv("wl-copy") for argument in call],
                         ["--type", "text/plain"])

    def test_a_path_that_looks_like_an_option_is_still_a_path(self):
        share.copy_path(Path("-n"))
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "text/plain"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), b"-n")

    def test_a_path_of_any_characters_arrives_as_it_is(self):
        # Spaces at the ends, a newline, a tab, characters of every size, and a name that is not text at all.
        names = ["/a/ spaced name /c.png", "/a/line\nbreak\tand tab.png", "/a/\u00e9\u2192\U0001f600.png",
                 os.fsdecode(b"/a/not-utf8-\xff.png")]
        for name in names:
            with self.subTest(name=name):
                share.copy_path(Path(name))
                self.assertEqual(self.stubs.stdin("wl-copy"), os.fsencode(name))

    def test_a_path_that_cannot_be_encoded_is_an_error_and_wl_copy_is_not_started(self):
        with self.assertRaises(share.ShareError) as caught:
            share.copy_path(Path("/a/\ud83d.png"))
        self.assertEqual(str(caught.exception), COPY_FAILED)
        self.assertEqual(self.stubs.argv("wl-copy"), [])

    def test_a_missing_wl_copy_is_an_error_with_the_package_to_install(self):
        self.stubs.remove("wl-copy")
        path = self.card("omawrapped-2026-10-09.png")
        for function in (share.copy_image, share.copy_path):
            with self.subTest(function=function.__name__):
                with self.assertRaises(share.ShareError) as caught:
                    function(path)
                self.assertEqual(str(caught.exception), NO_WL_COPY)

    def test_a_failing_wl_copy_is_an_error_that_names_the_likely_cause(self):
        self.stubs.fail("wl-copy", 1)
        path = self.card("omawrapped-2026-10-09.png")
        for function in (share.copy_image, share.copy_path):
            with self.subTest(function=function.__name__):
                with self.assertRaises(share.ShareError) as caught:
                    function(path)
                self.assertEqual(str(caught.exception), COPY_FAILED)

    def test_a_wl_copy_that_hangs_is_an_error_too(self):
        path = self.card("omawrapped-2026-10-09.png")
        for function in (share.copy_image, share.copy_path):
            with self.subTest(function=function.__name__):
                with mock.patch.object(share.subprocess, "run", side_effect=subprocess.TimeoutExpired("wl-copy", 10)):
                    with self.assertRaises(share.ShareError) as caught:
                        function(path)
                self.assertEqual(str(caught.exception), COPY_FAILED)

    def test_a_wl_copy_that_cannot_be_started_is_an_error_too(self):
        with mock.patch.object(share.subprocess, "run", side_effect=PermissionError("denied")):
            with self.assertRaises(share.ShareError) as caught:
                share.copy_path(Path("/x.png"))
        self.assertEqual(str(caught.exception), COPY_FAILED)

    def test_wl_copy_gets_10_seconds_and_nothing_to_read_from_us_but_its_input(self):
        # wl-copy stays behind to serve the clipboard: a pipe of ours that it inherited for its output would never
        # be closed. What it reads is written to it and the pipe is closed again before it is waited for.
        run = self.spied("run")
        path = self.card("omawrapped-2026-10-09.png")
        share.copy_image(path)
        share.copy_path(path)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 10)
            self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("capture_output", call.kwargs)
            self.assertNotEqual(call.kwargs.get("stdin"), subprocess.PIPE)
        self.assertNotIn("input", run.call_args_list[0].kwargs)
        self.assertEqual(run.call_args_list[1].kwargs["input"], str(path).encode("utf-8"))
        self.assertNotIn("stdin", run.call_args_list[1].kwargs)

    def test_a_file_that_cannot_be_read_is_an_error_and_wl_copy_is_not_started(self):
        with self.assertRaises(share.ShareError) as caught:
            share.copy_image(self.pictures / "gone.png")
        self.assertEqual(str(caught.exception),
                         "%s could not be read, so nothing was copied." % (self.pictures / "gone.png"))
        self.assertEqual(self.stubs.argv("wl-copy"), [])

    def test_the_file_is_closed_again(self):
        path = self.card("omawrapped-2026-10-09.png")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            share.copy_image(path)
        self.stubs.fail("wl-copy")
        for _ in range(5):
            with self.assertRaises(share.ShareError):
                share.copy_image(path)
            with self.assertRaises(share.ShareError):
                share.copy_path(path)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)


class OpenFileTests(DetachedCase):
    def test_xdg_open_gets_the_path(self):
        share.open_file(Path("/some folder/omawrapped-2026-10-09.png"))
        self.assertEqual(self.stubs.wait_for_argv("xdg-open"), [["/some folder/omawrapped-2026-10-09.png"]])

    def test_it_is_started_detached(self):
        popen = self.spied("Popen")
        share.open_file(Path("/x.png"))
        self.stubs.wait_for_argv("xdg-open")
        self.assertEqual(popen.call_count, 1)
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["start_new_session"], True)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)

    def test_a_missing_xdg_open_is_an_error(self):
        self.stubs.remove("xdg-open")
        with self.assertRaises(share.ShareError) as caught:
            share.open_file(Path("/x.png"))
        self.assertEqual(str(caught.exception),
                         "xdg-open was not found (package xdg-utils), so the card was not opened.")

    def test_an_xdg_open_that_cannot_be_started_is_an_error(self):
        with mock.patch.object(share.subprocess, "Popen", side_effect=PermissionError("denied")):
            with self.assertRaises(share.ShareError) as caught:
                share.open_file(Path("/x.png"))
        self.assertIn("xdg-open", str(caught.exception))
        self.assertEqual(self.stubs.argv("xdg-open"), [])


class ShowInFolderTests(DetachedCase):
    PATH = Path("/home/me/Pictures/omawrapped 2026-10-09.png")

    def test_with_uwsm_app_the_file_manager_is_started_through_it(self):
        share.show_in_folder(self.PATH)
        self.assertEqual(self.stubs.wait_for_argv("uwsm-app"), [["--", "nautilus", "--select", str(self.PATH)]])
        self.assertEqual(self.stubs.argv("nautilus"), [])
        self.assertEqual(self.stubs.argv("xdg-open"), [])

    def test_without_uwsm_app_nautilus_is_started_directly(self):
        self.stubs.remove("uwsm-app")
        share.show_in_folder(self.PATH)
        self.assertEqual(self.stubs.wait_for_argv("nautilus"), [["--select", str(self.PATH)]])
        self.assertEqual(self.stubs.argv("xdg-open"), [])

    def test_without_nautilus_the_folder_is_opened_with_xdg_open(self):
        self.stubs.remove("nautilus")
        share.show_in_folder(self.PATH)
        self.assertEqual(self.stubs.wait_for_argv("xdg-open"), [[str(self.PATH.parent)]])
        # uwsm-app is for starting Nautilus; with no Nautilus there is nothing for it to start.
        self.assertEqual(self.stubs.argv("uwsm-app"), [])

    def test_with_neither_it_is_an_error_that_says_where_the_card_is(self):
        self.stubs.remove("nautilus", "xdg-open")
        with self.assertRaises(share.ShareError) as caught:
            share.show_in_folder(self.PATH)
        self.assertEqual(str(caught.exception),
                         "No file manager was found to show the card in. It is at %s." % self.PATH)

    def test_uwsm_app_alone_is_not_a_file_manager(self):
        self.stubs.remove("nautilus", "xdg-open")
        with self.assertRaises(share.ShareError):
            share.show_in_folder(self.PATH)
        self.assertEqual(self.stubs.argv("uwsm-app"), [])

    def test_it_is_started_detached(self):
        popen = self.spied("Popen")
        share.show_in_folder(self.PATH)
        self.stubs.wait_for_argv("uwsm-app")
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["start_new_session"], True)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)

    def test_a_program_that_cannot_be_started_is_an_error(self):
        with mock.patch.object(share.subprocess, "Popen", side_effect=PermissionError("denied")):
            with self.assertRaises(share.ShareError) as caught:
                share.show_in_folder(self.PATH)
        self.assertIn("uwsm-app", str(caught.exception))


class NotifyTests(ShareCase):
    """notify() sends the notification itself, over the session bus. The bus here is the test's own."""

    needs_bus = True
    CLICK = ["/plugin/bin/omawrapped", "show", "/pictures/omawrapped-2026-10-09.png"]
    IMAGE = Path("/pictures/omawrapped-2026-10-09.png")

    def setUp(self):
        super().setUp()
        self.use_bus()

    def sent(self) -> list:
        return self.bus.notifications()

    def assert_no_program_started(self):
        for name, calls in self.stubs.everything().items():
            self.assertEqual(calls, [], "%s was started" % name)

    def test_the_headline_and_the_body_are_all_it_needs(self):
        self.assertIs(share.notify("Card saved", "It is in Pictures."), True)
        self.assertEqual(self.sent(), [notification("Card saved", "It is in Pictures.")])

    def test_what_arrives_is_what_omarchys_own_tool_sends(self):
        share.notify("Card saved", "It is in Pictures.")
        self.assertEqual(self.sent(), [{
            "app": "OmaWrapped", "replaces": 0, "icon": "", "summary": "Card saved", "body": "It is in Pictures.",
            "actions": [], "hints": {"urgency": 0, "omarchy-glyph": "\U000f154d"}, "expire": -1}])

    def test_the_body_may_be_left_out(self):
        self.assertIs(share.notify("Card saved"), True)
        self.assertEqual(self.sent(), [notification("Card saved", "")])

    def test_an_image_is_a_hint(self):
        share.notify("Card copied", "Paste it.", image=self.IMAGE)
        self.assertEqual(self.sent()[0]["hints"], {"urgency": 0, "omarchy-glyph": GLYPH,
                                                    "image-path": "/pictures/omawrapped-2026-10-09.png"})
        # The icon of the notification itself stays empty: the shell takes it from the glyph.
        self.assertEqual(self.sent()[0]["icon"], "")

    def test_a_click_command_is_a_json_array_of_its_words(self):
        share.notify("Card saved", "Click it.", click=self.CLICK)
        self.assertEqual(self.sent()[0]["hints"]["omarchy-exec-argv"],
                         '["/plugin/bin/omawrapped","show","/pictures/omawrapped-2026-10-09.png"]')
        self.assertEqual(self.sent(), [notification("Card saved", "Click it.", click=self.CLICK)])

    def test_the_click_command_is_compact_json(self):
        share.notify("x", click=["a", "b c"])
        self.assertEqual(self.sent()[0]["hints"]["omarchy-exec-argv"], '["a","b c"]')

    def test_a_click_command_with_spaces_and_quotes_keeps_every_word_whole(self):
        words = ['say "hi"', "back\\slash", Path("/b folder/c.png"), "é→", "tab\there", ""]
        share.notify("x", click=words)
        hint = self.sent()[0]["hints"]["omarchy-exec-argv"]
        self.assertEqual(hint, r'["say \"hi\"","back\\slash","/b folder/c.png","é→","tab\there",""]')
        self.assertEqual(json.loads(hint), [str(word) for word in words])

    def test_image_and_click_together(self):
        share.notify("Card copied", "Paste it. Click it.", image=self.IMAGE, click=self.CLICK)
        self.assertEqual(self.sent(), [notification("Card copied", "Paste it. Click it.", image=self.IMAGE,
                                                    click=self.CLICK)])
        self.assertEqual(set(self.sent()[0]["hints"]), {"urgency", "omarchy-glyph", "image-path", "omarchy-exec-argv"})

    def test_a_click_command_that_is_empty_is_no_hint(self):
        share.notify("x", click=[])
        self.assertEqual(set(self.sent()[0]["hints"]), {"urgency", "omarchy-glyph"})

    def test_urgency_is_low_and_the_glyph_is_there_whatever_else_is_given(self):
        share.notify("a")
        share.notify("b", "c", image=self.IMAGE)
        share.notify("d", "e", click=self.CLICK)
        share.notify("f", "g", image=self.IMAGE, click=self.CLICK)
        self.assertEqual(len(self.sent()), 4)
        for sent in self.sent():
            self.assertEqual((sent["hints"]["urgency"], sent["hints"]["omarchy-glyph"]), (0, GLYPH))
            self.assertEqual((sent["app"], sent["expire"], sent["replaces"], sent["actions"]),
                             ("OmaWrapped", -1, 0, []))

    def test_each_notification_is_one_of_its_own(self):
        share.notify("one")
        share.notify("two")
        self.assertEqual([sent["summary"] for sent in self.sent()], ["one", "two"])

    def test_text_arrives_unchanged_a_tab_a_newline_and_characters_of_every_size(self):
        share.notify("a\tb", "c\nd")
        share.notify("T\u00e2che \u2713 \U0001f600 \U000f154d", "\u00b7 \u2014 \u00e9")
        self.assertEqual([(sent["summary"], sent["body"]) for sent in self.sent()],
                         [("a\tb", "c\nd"), ("T\u00e2che \u2713 \U0001f600 \U000f154d", "\u00b7 \u2014 \u00e9")])

    def test_it_starts_no_program_at_all(self):
        # Not omarchy-notification-send, not notify-send: what they would be given is readable by every user.
        popen, run = mock.patch.object(share.subprocess, "Popen"), mock.patch.object(share.subprocess, "run")
        with popen as started, run as ran:
            self.assertIs(share.notify("Today: 3h 07m", "Ghostty 1h 20m", image=self.IMAGE, click=self.CLICK), True)
        self.assertEqual((started.call_count, ran.call_count), (0, 0))
        self.assert_no_program_started()
        self.assertEqual(len(self.sent()), 1)

    def test_it_needs_neither_of_the_programs_that_used_to_send_it(self):
        self.stubs.remove("omarchy-notification-send", "notify-send")
        self.assertIs(share.notify("Card saved", "x"), True)
        self.assertEqual(len(self.sent()), 1)

    def test_the_connection_is_closed_again(self):
        share.notify("warm up")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(10):
            share.notify("Card saved", "x")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    # ---- no notification: False, and nothing started ----

    def no_address(self, **env) -> None:
        """For the rest of the test, no session bus is named, and none is found under XDG_RUNTIME_DIR."""
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)

    def assert_false_and_nothing_started(self, headline="Card saved", **kwargs):
        with mock.patch.object(share.subprocess, "Popen") as started, mock.patch.object(share.subprocess, "run") as ran:
            self.assertIs(share.notify(headline, "x", **kwargs), False)
        self.assertEqual((started.call_count, ran.call_count), (0, 0))
        self.assert_no_program_started()
        self.assertEqual(self.sent(), [])

    def test_without_a_bus_address_it_is_false_and_nothing_is_started(self):
        self.no_address()
        self.assert_false_and_nothing_started(image=self.IMAGE, click=self.CLICK)

    def test_an_empty_bus_address_is_none(self):
        self.no_address(DBUS_SESSION_BUS_ADDRESS="")
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = ""
        self.assert_false_and_nothing_started()

    def test_a_dead_address_is_false_and_nothing_is_started(self):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s" % (self.tmp / "run" / "no-bus")
        self.assert_false_and_nothing_started()

    def test_an_address_that_is_no_address_is_false(self):
        for address in ("nonsense", "unix:", "tcp:host=127.0.0.1,port=1"):
            with self.subTest(address=address):
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
                self.assert_false_and_nothing_started()

    def test_without_python_gobject_it_is_false_and_nothing_is_started(self):
        for error in (ImportError("No module named 'gi'"), ValueError("Namespace Gio not available")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(share, "_gio", side_effect=error):
                    self.assert_false_and_nothing_started()

    def test_a_service_that_refuses_gives_false_and_the_next_one_goes_through(self):
        self.bus.refuse(True)
        self.assert_false_and_nothing_started(click=self.CLICK)
        self.bus.refuse(False)
        self.assertIs(share.notify("Card saved", "x"), True)
        self.assertEqual(len(self.sent()), 1)

    def test_a_headline_that_cannot_be_encoded_is_false_with_nothing_started(self):
        # Half a character, such as a lone surrogate, cannot be sent as text.
        self.assert_false_and_nothing_started("\ud83d")

    def test_other_text_that_cannot_be_encoded_gives_false_and_the_next_one_goes_through(self):
        for fields in ({"body": "half \ud83d"}, {"image": Path("/a/\ud83d.png")}, {"click": ["/a", "\ud83d"]}):
            with self.subTest(fields=sorted(fields)):
                self.assertIs(share.notify("ok", **fields), False)
        self.assertEqual(self.sent(), [])
        self.assertIs(share.notify("Card saved", "x"), True)
        self.assertEqual(len(self.sent()), 1)

    def test_the_glyph_is_the_chart_box_of_the_nerd_font(self):
        self.assertEqual(share.GLYPH, "\U000f154d")

    def test_the_app_is_called_omawrapped(self):
        self.assertEqual(share.APP_NAME, "OmaWrapped")


class CannotNotifyTests(ShareCase):
    """cannot_notify() says why no notification can be sent, without sending one."""

    needs_bus = True

    def setUp(self):
        super().setUp()
        self.use_bus()

    def no_address(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)

    def test_none_when_a_notification_can_be_sent(self):
        self.assertIsNone(share.cannot_notify())

    def test_none_for_an_address_that_has_not_been_tried(self):
        # It is a look at what is there, not a connection.
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s" % (self.tmp / "run" / "no-bus")
        self.assertIsNone(share.cannot_notify())

    def test_without_python_gobject(self):
        for error in (ImportError("No module named 'gi'"), ValueError("Namespace Gio not available")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(share, "_gio", side_effect=error):
                    self.assertEqual(share.cannot_notify(), "python-gobject is not installed")

    def test_without_a_session_bus(self):
        self.no_address()
        self.assertEqual(share.cannot_notify(), "there is no session bus")

    def test_python_gobject_is_named_before_the_bus_when_both_are_missing(self):
        self.no_address()
        with mock.patch.object(share, "_gio", side_effect=ImportError):
            self.assertEqual(share.cannot_notify(), "python-gobject is not installed")

    def test_it_sends_nothing_and_starts_nothing(self):
        with mock.patch.object(share.subprocess, "Popen") as started, mock.patch.object(share.subprocess, "run") as ran:
            share.cannot_notify()
        self.assertEqual((started.call_count, ran.call_count), (0, 0))
        self.assertEqual(self.bus.notifications(), [])


class BusAddressTests(ShareCase):
    """Where the session bus is: the environment says, else the socket in the runtime folder, if it is there."""

    needs_bus = True

    def setUp(self):
        super().setUp()
        self.run_dir = self.tmp / "run"
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)

    def test_the_variable_comes_first(self):
        (self.run_dir / "bus").write_text("", encoding="utf-8")
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/elsewhere"
        self.assertEqual(share._bus_address(), "unix:path=/elsewhere")

    def test_the_socket_in_the_runtime_folder_is_used_when_it_is_there(self):
        (self.run_dir / "bus").write_text("", encoding="utf-8")
        self.assertEqual(share._bus_address(), "unix:path=%s/bus" % self.run_dir)
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = ""
        self.assertEqual(share._bus_address(), "unix:path=%s/bus" % self.run_dir)

    def test_a_socket_that_is_not_there_is_not_guessed(self):
        self.assertFalse((self.run_dir / "bus").exists())
        self.assertIsNone(share._bus_address())
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = ""
        self.assertIsNone(share._bus_address())

    def test_an_address_that_asks_for_a_bus_to_be_started_is_not_one(self):
        # "autolaunch:" would have a bus started for the command; it only ever uses one that is there.
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "autolaunch:"
        self.assertIsNone(share._bus_address())
        self.assertIs(share.notify("Card saved", "x"), False)
        (self.run_dir / "bus").write_text("", encoding="utf-8")
        self.assertEqual(share._bus_address(), "unix:path=%s/bus" % self.run_dir)

    def test_without_a_runtime_folder_there_is_none(self):
        del os.environ["XDG_RUNTIME_DIR"]
        self.assertIsNone(share._bus_address())
        os.environ["XDG_RUNTIME_DIR"] = ""
        self.assertIsNone(share._bus_address())

    def test_a_notification_finds_the_bus_through_the_runtime_folder(self):
        # The socket of the test bus, linked in where a session puts its own.
        socket = self.bus.address.split("=", 1)[1].split(",")[0]
        (self.run_dir / "bus").symlink_to(socket)
        self.assertIs(share.notify("Card saved", "x"), True)
        self.assertEqual(self.bus.notifications(), [notification("Card saved", "x")])

    def test_no_session_is_started_for_a_machine_without_one(self):
        # A bus is never started from here: with no address and no socket, that is the end of it.
        with mock.patch.object(share.subprocess, "Popen") as started, mock.patch.object(share.subprocess, "run") as ran:
            self.assertIs(share.notify("Card saved", "x"), False)
        self.assertEqual((started.call_count, ran.call_count), (0, 0))


class ChooseTests(ShareCase):
    OPTIONS = ["\U000f018f\tCopy card", "\U000f0770\tShow in folder", "Plain"]

    def test_the_label_is_returned_without_its_glyph(self):
        self.stubs.choose_in_menu("Show in folder")
        self.assertEqual(share.choose("OmaWrapped", self.OPTIONS), "Show in folder")

    def test_a_label_with_no_newline_after_it_is_returned_as_it_is(self):
        # The real menu prints the label as it is, with no newline.
        self.stubs.choice.write_text("Copy card", encoding="utf-8")
        self.assertEqual(share.choose("OmaWrapped", self.OPTIONS), "Copy card")

    def test_a_plain_option_is_returned_as_it_is(self):
        self.stubs.choose_in_menu("Plain")
        self.assertEqual(share.choose("OmaWrapped", self.OPTIONS), "Plain")

    def test_a_dismissed_menu_gives_none(self):
        self.assertIsNone(share.choose("OmaWrapped", self.OPTIONS))
        self.assertEqual(len(self.stubs.argv("omarchy-menu-select")), 1)

    def test_an_answer_of_nothing_gives_none(self):
        self.stubs.choice.write_text("\n", encoding="utf-8")
        self.assertIsNone(share.choose("OmaWrapped", self.OPTIONS))

    def test_the_prompt_and_the_options_reach_the_menu_unchanged(self):
        share.choose("Pick one", self.OPTIONS + ["with\nnewline", ""])
        self.assertEqual(self.stubs.argv("omarchy-menu-select"), [
            ["Pick one", "\U000f018f\tCopy card", "\U000f0770\tShow in folder", "Plain", "with\nnewline", ""]])

    def test_a_missing_menu_is_an_error_that_points_at_the_commands(self):
        self.stubs.remove("omarchy-menu-select")
        with self.assertRaises(share.ShareError) as caught:
            share.choose("OmaWrapped", self.OPTIONS)
        self.assertEqual(str(caught.exception), NO_MENU)

    def test_a_menu_that_breaks_is_an_error_not_a_dismissal(self):
        self.stubs.fail("omarchy-menu-select", 2)
        with self.assertRaises(share.ShareError):
            share.choose("OmaWrapped", self.OPTIONS)

    def test_a_menu_that_cannot_be_started_is_an_error(self):
        with mock.patch.object(share.subprocess, "run", side_effect=PermissionError("denied")):
            with self.assertRaises(share.ShareError):
                share.choose("OmaWrapped", self.OPTIONS)

    def test_the_menu_may_stay_open_for_ten_minutes(self):
        run = self.spied("run")
        share.choose("OmaWrapped", self.OPTIONS)
        self.assertEqual(run.call_args.kwargs.get("timeout"), 600)

    def test_a_menu_nobody_answers_counts_as_dismissed(self):
        # Its shell is gone; without an end the command would wait for it for good.
        timed_out = subprocess.TimeoutExpired(["omarchy-menu-select"], 600)
        with mock.patch.object(share.subprocess, "run", side_effect=timed_out):
            self.assertIsNone(share.choose("OmaWrapped", self.OPTIONS))


class StandInTests(ShareCase):
    """The stand-ins are what keeps every other test away from the desktop, so they are tested too."""

    def test_every_desktop_program_is_a_stand_in_and_nothing_else_is_on_the_path(self):
        self.assertEqual(len(STUBBED), 9)
        self.assertIn("omarchy-bar", STUBBED)
        for name in STUBBED:
            self.assertEqual(shutil.which(name), str(self.stubs.dir / name))
        for folder in os.environ["PATH"].split(":"):
            self.assertIn(Path(folder), (self.stubs.dir, self.stubs.tools))
        self.assertFalse(set(STUBBED) & {path.name for path in self.stubs.tools.iterdir()})

    def test_a_removed_stand_in_is_a_missing_program_whatever_is_installed(self):
        for number, name in enumerate(STUBBED):
            with self.subTest(name=name):
                root = self.tmp / ("again-%d" % number)
                root.mkdir()
                stubs = Stubs(root)
                stubs.remove(name)
                self.assertIsNone(shutil.which(name, path=stubs.path))

    def test_a_stand_in_logs_every_argument_whole(self):
        arguments = ["a\tb", "c\nd", "", "\U000f154d", "-x", "two words"]
        subprocess.run([str(self.stubs.dir / "xdg-open"), *arguments], check=True, env={})
        subprocess.run([str(self.stubs.dir / "xdg-open")], check=True, env={})
        self.assertEqual(self.stubs.argv("xdg-open"), [arguments, []])
        self.assertEqual(self.stubs.calls("xdg-open"), [" ".join(arguments), ""])

    def test_a_stand_in_keeps_what_wl_copy_read(self):
        subprocess.run([str(self.stubs.dir / "wl-copy"), "--type", "image/png"], input=IMAGE, check=True, env={})
        subprocess.run([str(self.stubs.dir / "wl-copy"), "--", "x"], input=b"second", check=True, env={})
        self.assertEqual(self.stubs.stdin("wl-copy", 0), IMAGE)
        self.assertEqual(self.stubs.stdin("wl-copy"), b"second")

    def test_a_stand_in_fails_when_told_to_and_still_logs(self):
        self.stubs.fail("nautilus", 3)
        done = subprocess.run([str(self.stubs.dir / "nautilus"), "--select", "x"], env={})
        self.assertEqual(done.returncode, 3)
        self.assertEqual(self.stubs.argv("nautilus"), [["--select", "x"]])

    def test_a_stand_in_says_why_it_failed_when_given_a_reason(self):
        failing = self.stubs.dir / "nautilus"
        self.stubs.fail("nautilus", 3, reason="no folder")
        done = subprocess.run([str(failing), "x"], capture_output=True, text=True, env={})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "", "no folder\n"))
        self.stubs.fail("nautilus", 4, reason="two\nlines\n")
        done = subprocess.run([str(failing)], capture_output=True, text=True, env={})
        self.assertEqual((done.returncode, done.stderr), (4, "two\nlines\n"))
        self.assertEqual(self.stubs.argv("nautilus"), [["x"], []])

    def test_omarchy_bar_reports_a_change_as_omarchy_does(self):
        bar = str(self.stubs.dir / "omarchy-bar")
        done = subprocess.run([bar, "set", "some.widget", "paused", "true", "--json"], capture_output=True, text=True,
                              env={})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "Set paused on some.widget\n", ""))
        done = subprocess.run([bar, "set", "other.widget", "idleSeconds", "30", "--json"], capture_output=True,
                              text=True, env={})
        self.assertEqual(done.stdout, "Set idleSeconds on other.widget\n")
        done = subprocess.run([bar, "list"], capture_output=True, text=True, env={})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual(self.stubs.argv("omarchy-bar"), [["set", "some.widget", "paused", "true", "--json"],
                                                          ["set", "other.widget", "idleSeconds", "30", "--json"],
                                                          ["list"]])

    def test_omarchy_bar_that_fails_always_says_why_unless_told_to_say_nothing(self):
        bar = str(self.stubs.dir / "omarchy-bar")
        self.stubs.fail("omarchy-bar", 2)
        done = subprocess.run([bar, "set", "w", "paused", "true", "--json"], capture_output=True, text=True, env={})
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertTrue(done.stderr.strip())
        self.assertEqual(len(done.stderr.splitlines()), 1)
        self.stubs.fail("omarchy-bar", 2, reason="Unknown widget: w")
        done = subprocess.run([bar, "set", "w", "paused", "true", "--json"], capture_output=True, text=True, env={})
        self.assertEqual(done.stderr, "Unknown widget: w\n")
        self.stubs.fail("omarchy-bar", 2, reason="")
        done = subprocess.run([bar, "set", "w", "paused", "true", "--json"], capture_output=True, text=True, env={})
        self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", ""))
        self.assertEqual(len(self.stubs.argv("omarchy-bar")), 3)

    def test_the_shell_answers_status_differently_on_successive_calls_when_told_to(self):
        shell = str(self.stubs.dir / "omarchy-shell")

        def ask(method="status"):
            return subprocess.run([shell, "plugin", method], capture_output=True, text=True, env={}, check=True).stdout

        self.stubs.reply_to_status_in_turn('{"n": 1}', "", '{"n": 3}')
        self.assertEqual([ask() for _ in range(5)], ['{"n": 1}', "", '{"n": 3}', '{"n": 3}', '{"n": 3}'])
        # Other methods are not counted, and giving the replies again starts again.
        self.stubs.reply_to_status_in_turn('{"n": 1}', '{"n": 2}')
        self.assertEqual(ask("flush"), "ok\n")
        self.assertEqual([ask(), ask(), ask()], ['{"n": 1}', '{"n": 2}', '{"n": 2}'])
        # One reply for every call is the way it was, and wins over the ones in turn until they are given again.
        self.stubs.reply_to_status('{"n": 0}')
        self.assertEqual([ask(), ask()], ['{"n": 0}', '{"n": 0}'])
        self.stubs.reply_to_status_in_turn("a")
        self.assertEqual(ask(), "a")
        with self.assertRaises(ValueError):
            self.stubs.reply_to_status_in_turn("two\nlines")

    def test_a_harmless_tool_can_be_replaced_by_a_stand_in_that_logs_its_calls(self):
        self.stubs.replace_tool("git")
        self.assertEqual(shutil.which("git"), str(self.stubs.tools / "git"))
        subprocess.run(["git", "status", "-s"], check=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(self.stubs.argv("git"), [["status", "-s"]])
        self.assertNotIn("git", STUBBED)

    def test_the_shell_answers_as_the_sampler_does(self):
        shell = str(self.stubs.dir / "omarchy-shell")

        def ask(method):
            return subprocess.run([shell, "plugin", method], capture_output=True, text=True, env={}, check=True).stdout

        self.assertEqual(ask("status"), "")
        self.assertEqual(ask("flush"), "ok\n")
        self.stubs.reply_to_status('{"counting": true}')
        self.assertEqual(ask("status"), '{"counting": true}')
        self.stubs.stop_answering()
        self.assertEqual(ask("discard"), "")
        self.assertEqual(self.stubs.calls("omarchy-shell"), ["plugin status", "plugin flush", "plugin status",
                                                             "plugin discard"])

    def test_the_menu_answers_with_the_label_chosen_and_exits_1_without_one(self):
        menu = str(self.stubs.dir / "omarchy-menu-select")
        done = subprocess.run([menu, "p", "a"], capture_output=True, text=True, env={})
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.stubs.choose_in_menu("Copy card")
        done = subprocess.run([menu, "p", "a"], capture_output=True, text=True, env={})
        self.assertEqual((done.returncode, done.stdout), (0, "Copy card\n"))

    def test_a_spy_logs_the_call_and_the_folder_and_runs_the_real_program(self):
        self.stubs.spy("git")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        done = subprocess.run(["git", "--version"], capture_output=True, text=True, cwd=elsewhere, env=os.environ)
        self.assertEqual(done.returncode, 0)
        self.assertTrue(done.stdout.startswith("git version"), done.stdout)
        self.assertEqual(self.stubs.argv("git"), [["--version"]])
        self.assertEqual(self.stubs.cwds("git"), [str(elsewhere)])
        self.assertEqual(shutil.which("git"), str(self.stubs.tools / "git"))

    def test_a_spy_hands_on_every_argument_and_the_standard_input_unchanged(self):
        if not real_tool("rsvg-convert") or not real_tool("fc-match"):
            self.skipTest("rsvg-convert and fc-match are needed")
        self.stubs.spy("rsvg-convert")
        self.stubs.spy("fc-match")
        svg = b"<svg xmlns='http://www.w3.org/2000/svg' width='3' height='2'><rect width='3' height='2'/></svg>"
        done = subprocess.run(["rsvg-convert", "--format=png"], input=svg, capture_output=True, env=os.environ)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout[:8], b"\x89PNG\r\n\x1a\n")
        done = subprocess.run(["fc-match", "-f", "%{family[0]} \u00e9", "monospace"], capture_output=True,
                              env=os.environ)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.stubs.argv("rsvg-convert"), [["--format=png"]])
        self.assertEqual(self.stubs.argv("fc-match"), [["-f", "%{family[0]} \u00e9", "monospace"]])

    def test_only_the_harmless_tools_can_be_spied_on(self):
        for name in STUBBED:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.stubs.spy(name)

    def test_a_spy_replaces_the_link_to_the_real_tool_and_a_stand_in_can_take_its_place_again(self):
        self.stubs.spy("git")
        self.assertFalse((self.stubs.tools / "git").is_symlink())
        self.stubs.replace_tool("git")
        subprocess.run(["git", "status"], check=True, env=os.environ)
        self.assertEqual(self.stubs.argv("git"), [["status"]])
        self.assertEqual(self.stubs.cwds("git"), [])

    def test_everything_started_is_listed_by_program(self):
        self.stubs.spy("git")
        self.assertEqual(self.stubs.everything(), {})
        subprocess.run([str(self.stubs.dir / "xdg-open"), "a"], check=True, env={})
        subprocess.run(["git", "--version"], check=True, capture_output=True, env=os.environ)
        subprocess.run([str(self.stubs.dir / "xdg-open"), "b"], check=True, env={})
        self.assertEqual(self.stubs.everything(), {"git": [["--version"]], "xdg-open": [["a"], ["b"]]})

    def test_the_log_is_json_one_call_a_line(self):
        subprocess.run([str(self.stubs.dir / "notify-send"), "a\nb"], check=True, env={})
        lines = (self.stubs.logs / "notify-send.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line) for line in lines], [["a\nb"]])


class BusTests(IsolatedCase):
    """The test bus is what keeps a notification of the tests off the real desktop, so it is tested too."""

    needs_bus = True

    def test_it_is_a_bus_of_its_own(self):
        self.assertTrue(self.bus.address.startswith("unix:path=/"), self.bus.address)
        self.assertTrue(Path(self.bus.address.split("=", 1)[1].split(",")[0]).exists())
        self.assertIsNone(self.bus.process.poll())
        self.assertNotEqual(self.bus.address, os.environ.get("DBUS_SESSION_BUS_ADDRESS"))

    def test_it_knows_nothing_of_the_environment_of_the_tests(self):
        environment = Path("/proc/%d/environ" % self.bus.process.pid).read_bytes().split(b"\0")
        names = sorted(item.split(b"=")[0].decode() for item in environment if item)
        self.assertEqual(names, ["HOME", "PATH", "TMPDIR", "XDG_RUNTIME_DIR"])
        self.assertIn(b"PATH=/usr/bin", environment)

    def test_what_it_is_sent_is_listed_oldest_first_and_cleared_on_request(self):
        self.use_bus()
        self.assertEqual(self.bus.notifications(), [])
        share.notify("one", "1")
        share.notify("two", "2")
        self.assertEqual([sent["summary"] for sent in self.bus.notifications()], ["one", "two"])
        self.bus.clear()
        self.assertEqual(self.bus.notifications(), [])
        share.notify("three", "3")
        self.assertEqual([sent["summary"] for sent in self.bus.notifications()], ["three"])

    def test_it_can_be_made_to_refuse_and_to_take_notifications_again(self):
        self.use_bus()
        self.bus.refuse(True)
        self.assertIs(share.notify("one"), False)
        self.bus.refuse(False)
        self.assertIs(share.notify("two"), True)
        self.assertEqual([sent["summary"] for sent in self.bus.notifications()], ["two"])

    def test_resetting_clears_what_was_said_and_stops_refusing(self):
        self.use_bus()
        self.bus.refuse(True)
        self.bus.reset()
        share.notify("one")
        self.bus.reset()
        self.assertEqual(self.bus.notifications(), [])
        self.assertIs(share.notify("two"), True)

    def test_each_test_starts_with_nothing_said_and_nothing_refused(self):
        # The test case clears the bus before every test: whatever an earlier test left is gone.
        self.assertEqual(self.bus.notifications(), [])
        self.assertFalse(Path(str(self.bus.log) + ".refuse").exists())
        self.use_bus()
        share.notify("left behind, and the next test must not see it")
        self.bus.refuse(True)

    def test_stopping_ends_that_process_and_removes_its_folder(self):
        bus = Bus()
        pid, folder = bus.process.pid, bus.log.parent
        self.assertTrue(folder.is_dir())
        bus.stop()
        self.assertIsNotNone(bus.process.poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(folder.exists())
        bus.stop()
        self.assertIsNone(self.bus.process.poll())

    def test_a_bus_that_does_not_start_is_an_error_and_leaves_nothing_running(self):
        started = []
        real = subprocess.Popen

        def not_the_bus(command, **options):
            process = real(["/usr/bin/false"], **options)
            started.append(process)
            return process

        with mock.patch.object(subprocess, "Popen", not_the_bus):
            with self.assertRaises(RuntimeError):
                Bus()
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())


if __name__ == "__main__":
    unittest.main()
