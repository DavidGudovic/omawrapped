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
from support import STUBBED, StubbedCase, Stubs

GLYPH = "\U000f154d"
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

    def test_copy_path_runs_wl_copy_with_the_path_after_two_dashes(self):
        share.copy_path(Path("/some folder/omawrapped-2026-10-09.png"))
        self.assertEqual(self.stubs.argv("wl-copy"), [["--", "/some folder/omawrapped-2026-10-09.png"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), b"")

    def test_a_path_that_looks_like_an_option_is_still_a_path(self):
        share.copy_path(Path("-n"))
        self.assertEqual(self.stubs.argv("wl-copy"), [["--", "-n"]])

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

    def test_wl_copy_gets_10_seconds_and_no_pipe_of_ours(self):
        # wl-copy stays behind to serve the clipboard: a pipe it inherited would never be closed.
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
        self.assertEqual(run.call_args_list[1].kwargs["stdin"], subprocess.DEVNULL)

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
    CLICK = ["/plugin/bin/omawrapped", "show", "/pictures/omawrapped-2026-10-09.png"]
    IMAGE = Path("/pictures/omawrapped-2026-10-09.png")

    def sent(self) -> list:
        return self.stubs.argv("omarchy-notification-send")

    def test_the_headline_and_the_body_are_all_it_needs(self):
        self.assertTrue(share.notify("Card saved", "It is in Pictures."))
        self.assertEqual(self.sent(), [["--app-name", "OmaWrapped", "-g", GLYPH, "Card saved", "It is in Pictures."]])

    def test_the_body_may_be_left_out(self):
        self.assertTrue(share.notify("Card saved"))
        self.assertEqual(self.sent(), [["--app-name", "OmaWrapped", "-g", GLYPH, "Card saved", ""]])

    def test_an_image_comes_before_the_text(self):
        share.notify("Card copied", "Paste it.", image=self.IMAGE)
        self.assertEqual(self.sent(), [["--app-name", "OmaWrapped", "-g", GLYPH, "--image", str(self.IMAGE),
                                        "Card copied", "Paste it."]])

    def test_a_click_command_comes_last_after_exec(self):
        share.notify("Card saved", "Click it.", click=self.CLICK)
        self.assertEqual(self.sent(), [["--app-name", "OmaWrapped", "-g", GLYPH, "Card saved", "Click it.",
                                        "--exec", *self.CLICK]])

    def test_image_and_click_together(self):
        share.notify("Card copied", "Paste it. Click it.", image=self.IMAGE, click=self.CLICK)
        argv = self.sent()[0]
        self.assertEqual(argv, ["--app-name", "OmaWrapped", "-g", GLYPH, "--image", str(self.IMAGE),
                                "Card copied", "Paste it. Click it.", "--exec", *self.CLICK])
        self.assertEqual(argv[argv.index("--exec"):], ["--exec", *self.CLICK])

    def test_the_click_command_is_passed_word_by_word(self):
        share.notify("Card saved", "x", click=["/a folder/omawrapped", "show", Path("/b folder/c.png")])
        self.assertEqual(self.sent()[0][-4:], ["--exec", "/a folder/omawrapped", "show", "/b folder/c.png"])

    def test_a_headline_with_a_tab_and_a_newline_arrives_unchanged(self):
        share.notify("a\tb", "c\nd")
        self.assertEqual(self.sent()[0][-2:], ["a\tb", "c\nd"])

    def test_without_omarchys_tool_notify_send_is_used(self):
        self.stubs.remove("omarchy-notification-send")
        self.assertTrue(share.notify("Card saved", "Click it.", image=self.IMAGE, click=self.CLICK))
        self.assertEqual(self.stubs.argv("notify-send"),
                         [["-a", "OmaWrapped", "-i", str(self.IMAGE), "Card saved", "Click it."]])

    def test_notify_send_without_an_image(self):
        self.stubs.remove("omarchy-notification-send")
        share.notify("Card saved", "Click it.", click=self.CLICK)
        self.assertEqual(self.stubs.argv("notify-send"), [["-a", "OmaWrapped", "Card saved", "Click it."]])

    def test_omarchys_tool_is_preferred_when_both_exist(self):
        share.notify("Card saved", "x")
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.stubs.argv("notify-send"), [])

    def test_with_neither_it_is_false_and_nothing_is_said(self):
        self.stubs.remove("omarchy-notification-send", "notify-send")
        self.assertIs(share.notify("Card saved", "x", image=self.IMAGE, click=self.CLICK), False)

    def test_a_tool_that_fails_gives_false(self):
        self.stubs.fail("omarchy-notification-send", 1)
        self.assertIs(share.notify("Card saved", "x"), False)
        self.assertEqual(len(self.sent()), 1)
        # Omarchy's tool was the one asked; its failure is not a reason to say it twice.
        self.assertEqual(self.stubs.argv("notify-send"), [])

    def test_a_failing_notify_send_gives_false(self):
        self.stubs.remove("omarchy-notification-send")
        self.stubs.fail("notify-send", 1)
        self.assertIs(share.notify("Card saved", "x"), False)

    def test_a_tool_that_hangs_or_cannot_be_started_gives_false(self):
        for error in (subprocess.TimeoutExpired("omarchy-notification-send", 10), PermissionError("denied")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(share.subprocess, "run", side_effect=error):
                    self.assertIs(share.notify("Card saved", "x"), False)

    def test_the_tool_gets_10_seconds_and_nothing_to_write_to(self):
        run = self.spied("run")
        share.notify("Card saved", "x")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 10)
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)

    def test_the_glyph_is_the_chart_box_of_the_nerd_font(self):
        self.assertEqual(share.GLYPH, "\U000f154d")


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

    def test_the_log_is_json_one_call_a_line(self):
        subprocess.run([str(self.stubs.dir / "notify-send"), "a\nb"], check=True, env={})
        lines = (self.stubs.logs / "notify-send.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line) for line in lines], [["a\nb"]])


if __name__ == "__main__":
    unittest.main()
