import support  # noqa: F401  (must stay first: it disables bytecode and isolates the environment)

import contextlib
import io
import json
import os
import pty
import random
import re
import shutil
import stat
import struct
import subprocess
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from datetime import time as time_of_day
from pathlib import Path
from unittest import mock

from omawrapped import VERSION, aggregate, cli, share, system
from support import (GLYPH, LAUNCHER, ME, OTHER, PLUGIN_ID, STUBBED, IsolatedCase, StubbedCase, Stubs, bytecode_dirs,
                     cli_env, clock, commit, init_repo, isolated_env, make_sample, notification, real_tool)

SVG = "{http://www.w3.org/2000/svg}"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NO_CARD = "There is no card yet. Draw one with `omawrapped card`, or click the widget."
COPY_FAILED = "wl-copy could not copy the card. Is a Wayland session running?"
NO_WL_COPY = "wl-copy was not found (package wl-clipboard), so nothing was copied."
NO_XDG_OPEN = "xdg-open was not found (package xdg-utils), so the card was not opened."
NO_MENU = ("omarchy-menu-select was not found, so there is no menu to show. "
           "`omawrapped copy` and `omawrapped show` do the same from a terminal.")
CLICK_HINT = "Click here to show it in its folder."
# What the menu is given: the prompt, then five fixed options, then pause or resume, whichever makes sense.
MENU = ["OmaWrapped", "\U000f0150\tToday so far", "\U000f0a33\tCard of the last 7 days",
        "\U000f0e17\tCard of the last 30 days", "\U000f018f\tCopy card", "\U000f0770\tShow in folder"]
PAUSE = "\U000f03e4\tPause counting"
RESUME = "\U000f040a\tResume counting"
STATUS = PLUGIN_ID + " status"
FLUSH = PLUGIN_ID + " flush"


def row(name: str, time: str) -> str:
    """An app of a list under `omawrapped today`: two spaces, the name in 28 columns, the time in 9, right-aligned."""
    return "  " + name.ljust(28) + " " + time.rjust(9)


# Today as busy_day() writes it, as `omawrapped today` lists it: the top five apps, name and time in columns.
TODAY = "Today  3h 07m"
TODAY_ROWS = [
    "  Ghostty                         1h 20m",
    "  Chromium                           52m",
    "  Zed                                31m",
    "  Slack                              14m",
    "  Obsidian                            6m",
]
TODAY_BODY = "Ghostty 1h 20m \u00b7 Chromium 52m \u00b7 Zed 31m"
NOT_FOLLOWED = "The setting was saved, but the sampler has not followed. See `omawrapped status`.\n"
# The pause between two looks at the sampler, so that the tests do not wait for the real 0.2 seconds.
FAST = {"OMAWRAPPED_POLL_SECONDS": "0.01"}
# The CLI runs with the stand-ins and links to the harmless real tools on its PATH: the renderer is one of them.
needs_renderer = unittest.skipUnless(real_tool("rsvg-convert"), "rsvg-convert is not installed")


def svg_texts(path: Path) -> list:
    return ["".join(element.itertext()) for element in ET.parse(path).getroot().iter(SVG + "text")]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# A time the way the card and the CLI write it: 3h 07m, 52m.
DURATION = re.compile(r"\b\d+h \d\dm\b|\b\d+m\b")


def leaks(started: dict, private: list, root: Path, cards=()) -> list:
    """(program, argument, why) for every argument that gives away what the user did, or where they keep it.

    started is {program: the arguments of each of its calls}, as Stubs.everything() has it. An argument is a leak
    when it holds a time, or one of the private words in any case, or is a path below root (the fake machine) that
    is not the path of a card in `cards` handed to xdg-open or nautilus. Nautilus is started through uwsm-app, which
    is looked through.
    """
    found = []
    for started_program, calls in started.items():
        for arguments in calls:
            program = started_program
            if program == "uwsm-app" and arguments[:2] == ["--", "nautilus"]:
                program, arguments = "nautilus", arguments[2:]
            for argument in arguments:
                if argument.startswith(str(root)):
                    if argument not in cards or program not in ("xdg-open", "nautilus"):
                        found.append((program, argument, "a path"))
                    continue
                if DURATION.search(argument):
                    found.append((program, argument, "a time"))
                found.extend((program, argument, word) for word in private if word.lower() in argument.lower())
    return found


class CliCase(IsolatedCase):
    """Runs bin/omawrapped as a subprocess in a fake machine, with stand-ins for the programs it starts."""

    def setUp(self):
        super().setUp()
        self.stubs = Stubs(self.tmp)
        self.today = date.today()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.addCleanup(self.assert_no_bytecode)
        self.addCleanup(self.assert_no_notification_program_started)

    def sampler_reply(self, **fields) -> str:
        """What a running sampler answers to `status`. By default it records to this test's data folder."""
        status = {"counting": True, "locked": False, "idleSeconds": 120, "countKeptAwake": True, "ignoreApps": [],
                  "todayMs": 0, "dataDir": str(self.data_home / "omawrapped")}
        status.update(fields)
        return json.dumps(status)

    def sampler(self, **fields) -> None:
        """A running sampler, as the shell stand-in reports it."""
        self.stubs.reply_to_status(self.sampler_reply(**fields))

    def samplers(self, *replies) -> None:
        """The sampler as it changes: one reply to each `status`, in turn, the last one for good. A reply is the
        fields of sampler(), or None when nothing answers."""
        self.stubs.reply_to_status_in_turn(*["" if reply is None else self.sampler_reply(**reply) for reply in replies])

    def assert_no_notification_program_started(self):
        """After every test: a notification goes over the session bus, so neither program for it was started."""
        for name in ("omarchy-notification-send", "notify-send"):
            self.assertEqual(self.stubs.argv(name), [], "%s was started" % name)

    def assert_no_bytecode(self):
        self.assertEqual(bytecode_dirs(), [], "the launcher must not leave bytecode in the plugin folder")

    def environment(self, **extra) -> dict:
        env = cli_env(self.tmp, self.stubs)
        env.update(extra)
        return env

    def run_cli(self, *args, stdin=subprocess.DEVNULL, cwd=None, launcher=LAUNCHER, umask=None, **extra_env):
        return subprocess.run(
            [str(launcher), *map(str, args)], capture_output=True, text=True, stdin=stdin,
            env=self.environment(**extra_env), cwd=str(cwd or self.work), timeout=60,
            umask=-1 if umask is None else umask,
        )

    def ok(self, *args, **kwargs):
        result = self.run_cli(*args, **kwargs)
        self.assertEqual(result.returncode, 0, "omawrapped %s failed:\n%s" % (" ".join(map(str, args)), result.stderr))
        return result

    # ---- Fixtures ----

    def record(self, count=40, end=None):
        """Day files for the `count` days up to `end` (default: today), from make_sample."""
        make_sample.write_days(self.tmp, count, end or self.today, random.Random(7))

    def recorded(self) -> dict:
        """{date: the file's content} of every day file, read back with nothing but json."""
        return {date.fromisoformat(path.stem): json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(self.days.glob("*.json"))}

    def busy_day(self) -> None:
        """Today as 3h 07m in six apps, written in no order of time: the top five and the top three differ."""
        minutes = {"slack": 14, "signal": 4, "com.mitchellh.ghostty": 80, "zed": 31, "obsidian": 6, "chromium": 52}
        self.write_day(self.today, active_ms=sum(minutes.values()) * 60000,
                       apps_ms={app: ms * 60000 for app, ms in minutes.items()})

    def window(self, length: int) -> list:
        """The fixture files of the last `length` days that were recorded, oldest first."""
        first = self.today - timedelta(days=length - 1)
        return [raw for day, raw in sorted(self.recorded().items()) if first <= day <= self.today]

    def stats(self, *args) -> dict:
        return json.loads(self.ok("stats", "--json", *args).stdout)

    def local(self, day: date, at: time_of_day = time_of_day(12)) -> datetime:
        return datetime.combine(day, at).astimezone()

    def make_card(self, name="omawrapped-2026-10-02.png", mtime=1700000000, content=None) -> Path:
        """A card in the Pictures folder, last modified at mtime (seconds), so that the order is not up to the clock."""
        path = self.pictures / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG_SIGNATURE + name.encode() + bytes(range(256)) if content is None else content)
        os.utime(path, (mtime, mtime))
        return path

    def sent(self) -> list:
        """What the test bus was sent so far. Only for a case with needs_bus."""
        return self.bus.notifications()

    def assert_nothing_started(self, *names):
        """None of the desktop programs (except the named ones) was started. The two that used to send a
        notification are never started: a notification is sent over the session bus."""
        for name in STUBBED:
            if name not in names:
                self.assertEqual(self.stubs.argv(name), [], "%s was started" % name)

    def without_python_gobject(self) -> dict:
        """The variable that makes `import gi` fail in a run, as on a machine without python-gobject."""
        folder = self.tmp / "no-gobject"
        self.write(folder / "gi" / "__init__.py", "raise ImportError('No module named gi')\n")
        return {"PYTHONPATH": str(folder)}

    def repo_with_commits(self, folder: Path) -> Path:
        """A repository whose commits fall inside, on the edge of and outside the last week. Oldest first."""
        repo = init_repo(folder / "project")
        commit(repo, self.local(self.today - timedelta(days=60)))
        commit(repo, self.local(self.today - timedelta(days=7), time_of_day(23, 59, 59)))
        commit(repo, self.local(self.today - timedelta(days=6), time_of_day(0, 0, 0)))
        commit(repo, self.local(self.today - timedelta(days=1)))
        commit(repo, self.local(self.today - timedelta(days=1), time_of_day(13)), email=OTHER)
        return repo


class OnTheBus:
    """For a test case with needs_bus whose runs all have the test bus, unless a test names another address."""

    needs_bus = True

    def run_cli(self, *args, **kwargs):
        return super().run_cli(*args, **{**self.on_bus, **kwargs})


class LauncherTests(CliCase):
    def test_no_arguments_prints_help_and_exits_0(self):
        result = self.ok()
        self.assertIn("usage: omawrapped", result.stdout)
        for command in ("card", "today", "copy", "show", "pause", "resume", "menu", "stats", "status", "reset"):
            self.assertIn(command, result.stdout)
        self.assertEqual(result.stderr, "")

    def test_version(self):
        result = self.ok("--version")
        self.assertEqual(result.stdout.strip(), "omawrapped " + VERSION)

    def test_help_of_every_command(self):
        for command in ("card", "today", "copy", "show", "pause", "resume", "menu", "stats", "status", "reset"):
            with self.subTest(command=command):
                self.assertIn("usage: omawrapped " + command, self.ok(command, "--help").stdout)

    def test_the_new_commands_say_what_they_are_for(self):
        # argparse wraps the help to the width of the terminal, so only the words are compared.
        words = " ".join(self.ok("--help").stdout.split())
        for text in ("copy the last card's image to the clipboard", "show the last card in the file manager",
                     "pick one of the above from Omarchy's menu (what a middle click on the widget does)",
                     "today's screen time and top apps", "stop counting until you resume",
                     "count again after a pause"):
            self.assertIn(text, words)

    def test_the_menu_says_everything_it_offers(self):
        words = " ".join(self.ok("menu", "--help").stdout.split())
        for text in ("today so far", "last 7 or 30 days", "copy the last card", "pause or resume counting"):
            self.assertIn(text, words)
        self.assertNotIn("with the card", words)

    def test_card_has_a_notify_option_and_copy_one_too_but_show_has_none(self):
        self.assertIn("--notify", self.ok("card", "--help").stdout)
        self.assertIn("--notify", self.ok("copy", "--help").stdout)
        for command in ("today", "pause", "resume"):
            self.assertIn("--notify", self.ok(command, "--help").stdout)
        self.assertNotIn("--notify", self.ok("show", "--help").stdout)
        self.assertNotIn("--notify", self.ok("menu", "--help").stdout)

    def test_the_notify_options_say_that_a_failure_is_said_too(self):
        said = {"card": "say on the desktop that the card is ready, or why it is not",
                "copy": "say on the desktop that the card is on the clipboard, or why it is not",
                "pause": "say on the desktop that counting is paused, or why it is not",
                "resume": "say on the desktop that counting is back, or why it is not",
                "today": "say it on the desktop too"}
        for command, words in said.items():
            with self.subTest(command=command):
                self.assertIn(words, " ".join(self.ok(command, "--help").stdout.split()))

    def test_an_unknown_command_is_an_argument_error(self):
        result = self.run_cli("frobnicate")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_it_runs_from_any_folder_and_through_a_symlink(self):
        link = self.tmp / "linked-omawrapped"
        link.symlink_to(LAUNCHER)
        result = self.ok("--version", launcher=link, cwd="/")
        self.assertEqual(result.stdout.strip(), "omawrapped " + VERSION)

    def test_running_leaves_no_bytecode_in_the_plugin_folder(self):
        self.record(10)
        self.ok("stats")
        self.ok("status")
        self.assertEqual(bytecode_dirs(), [])

    def test_the_stand_ins_come_first_on_the_path(self):
        for name in STUBBED:
            self.assertEqual(shutil.which(name, path=self.stubs.path), str(self.stubs.dir / name))


class StatsTests(CliCase):
    def test_nothing_recorded(self):
        result = self.ok("stats")
        self.assertIn("Nothing was recorded", result.stdout)
        self.assertIn(str(self.data_home / "omawrapped"), result.stdout)

    def test_nothing_recorded_as_json_has_zeros(self):
        stats = self.stats()
        self.assertEqual(stats["screen_time_ms"], 0)
        self.assertEqual(stats["active_days"], 0)
        self.assertEqual(stats["average_ms_per_active_day"], 0)
        self.assertIsNone(stats["busiest_day"])
        self.assertIsNone(stats["busiest_hour"])
        self.assertEqual(stats["apps"], [])
        self.assertEqual(len(stats["days"]), 7)

    def test_the_sampler_is_asked_to_flush_first(self):
        self.sampler()
        self.ok("stats")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [PLUGIN_ID + " status", PLUGIN_ID + " flush"])

    def test_without_a_sampler_nothing_is_flushed(self):
        self.ok("stats")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [PLUGIN_ID + " status"])

    def test_a_sampler_that_records_elsewhere_is_left_alone(self):
        # The command tried out on a copy of the data must not reach into the real sampler.
        self.record()
        self.sampler(dataDir="/somewhere/else/omawrapped")
        self.ok("stats")
        self.ok("card", "-o", self.tmp / "c.svg", "--copy", "none", "--days", "1")
        self.ok("reset", "--yes")
        self.assertEqual(set(self.stubs.calls("omarchy-shell")), {PLUGIN_ID + " status"})

    def test_ignored_apps_stay_off_the_list_for_earlier_days_too(self):
        self.record()
        everything = self.stats()
        self.write(self.config / "omarchy" / "shell.json", json.dumps({"version": 1, "bar": {"layout": {"right": [
            {"id": PLUGIN_ID, "ignoreApps": "SLACK, chrome-web.whatsapp.com__-Default"}]}}}))
        self.write_day(self.today, active_ms=7200000, apps_ms={"slack": 3600000, "web:web.whatsapp.com": 3600000})
        names = [app["name"] for app in self.stats()["apps"]]
        self.assertNotIn("Slack", names)
        self.assertNotIn("web.whatsapp.com", names)
        self.assertIn("Ghostty", names)
        self.assertGreater(len(everything["apps"]), len(names) - 1)

    def test_json_totals_and_days_for_a_week(self):
        self.record()
        stats = self.stats()
        week = self.window(7)
        self.assertEqual(len(week), 7)
        self.assertEqual(stats["screen_time_ms"], sum(raw["active_ms"] for raw in week))
        self.assertEqual(len(stats["days"]), 7)
        self.assertEqual(stats["period"], {"start": (self.today - timedelta(days=6)).isoformat(),
                                           "end": self.today.isoformat(), "days": 7})
        self.assertEqual([entry["date"] for entry in stats["days"]],
                         [(self.today - timedelta(days=6 - i)).isoformat() for i in range(7)])
        self.assertEqual([entry["ms"] for entry in stats["days"]], [raw["active_ms"] for raw in week])
        self.assertEqual(stats["app_switches"], sum(raw["switches"] for raw in week))
        self.assertEqual(stats["active_days"], 7)
        self.assertEqual(stats["average_ms_per_active_day"], stats["screen_time_ms"] // 7)

    def test_json_hours_and_apps(self):
        self.record()
        stats = self.stats()
        week = self.window(7)
        self.assertEqual(stats["hours_ms"], [sum(raw["hours_ms"][h] for raw in week) for h in range(24)])
        self.assertEqual(sum(app["ms"] for app in stats["apps"]), sum(sum(raw["apps_ms"].values()) for raw in week))
        times = [app["ms"] for app in stats["apps"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertIn("Ghostty", [app["name"] for app in stats["apps"]])
        self.assertEqual(stats["busiest_hour"], stats["hours_ms"].index(max(stats["hours_ms"])))

    def test_json_busiest_day(self):
        self.record()
        stats = self.stats()
        first = self.today - timedelta(days=6)
        files = {day: raw for day, raw in self.recorded().items() if first <= day <= self.today}
        busiest = max(sorted(files), key=lambda day: files[day]["active_ms"])
        self.assertEqual(stats["busiest_day"], {"date": busiest.isoformat(), "ms": files[busiest]["active_ms"]})

    def test_the_month_covers_30_days(self):
        self.record()
        stats = self.stats("--month")
        self.assertEqual(len(stats["days"]), 30)
        self.assertEqual(stats["period"]["days"], 30)
        self.assertEqual(stats["screen_time_ms"], sum(raw["active_ms"] for raw in self.window(30)))

    def test_days_option(self):
        self.record(400)
        for length in (1, 10, 366):
            with self.subTest(days=length):
                stats = self.stats("--days", length)
                self.assertEqual(len(stats["days"]), length)
                self.assertEqual(stats["screen_time_ms"], sum(raw["active_ms"] for raw in self.window(length)))

    def test_week_is_the_default_and_can_be_asked_for(self):
        self.record()
        self.assertEqual(self.stats("--week"), self.stats())

    def test_days_without_a_file_count_as_zero(self):
        self.record()
        missing = self.today - timedelta(days=2)
        (self.days / (missing.isoformat() + ".json")).unlink()
        stats = self.stats()
        entry = next(entry for entry in stats["days"] if entry["date"] == missing.isoformat())
        self.assertEqual(entry["ms"], 0)
        self.assertEqual(stats["active_days"], 6)
        self.assertEqual(stats["screen_time_ms"], sum(raw["active_ms"] for raw in self.window(7)))

    def test_unusable_day_files_are_skipped(self):
        self.record()
        broken = self.today - timedelta(days=1)
        (self.days / (broken.isoformat() + ".json")).write_text("{ not json", encoding="utf-8")
        (self.days / "notes.json").write_text("{}", encoding="utf-8")
        stats = self.stats()
        self.assertEqual(stats["active_days"], 6)
        self.assertEqual(len(stats["days"]), 7)

    def test_text_report(self):
        self.record()
        out = self.ok("stats").stdout
        total = sum(raw["active_ms"] for raw in self.window(7))
        self.assertIn("OmaWrapped \u00b7 Last 7 days \u00b7 " + aggregate.last_days(7, self.today).span, out)
        self.assertRegex(out, r"(?m)^Screen time\s+" + re.escape(clock(total)))
        for heading in ("Busiest day", "Busiest hour", "App switches", "Commits", "Top apps", "By day"):
            self.assertIn(heading, out)
        self.assertIn("Ghostty", out)
        self.assertIn("no repositories found in ~/projects, ~/code", out)

    def test_time_without_an_hour_breakdown_does_not_crash_the_text_report(self):
        # The reader accepts a file whose hours are missing; the JSON then says the busiest hour is null.
        self.write_day(self.today, active_ms=3600000, hours_ms=[], apps_ms={"slack": 3600000}, switches=3)
        self.assertIsNone(self.stats()["busiest_hour"])
        result = self.run_cli("stats")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Screen time", result.stdout)

    def test_under_a_minute_is_too_little_for_a_card(self):
        # Times are shown in minutes, so a card of 59 seconds would say "0m" everywhere.
        self.write_day(self.today, active_ms=59999, apps_ms={"slack": 59999})
        result = self.run_cli("card", "-o", self.tmp / "c.svg", "--copy", "none")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Less than a minute was recorded for the last 7 days", result.stderr)
        self.assertFalse((self.tmp / "c.svg").exists())
        said = self.run_cli("stats")
        self.assertEqual(said.returncode, 0, said.stderr)
        self.assertIn("Less than a minute was recorded for the last 7 days", said.stdout)
        self.assertEqual(self.stats()["screen_time_ms"], 59999)

    def test_one_minute_is_enough_for_a_card(self):
        self.write_day(self.today, active_ms=60000, apps_ms={"slack": 60000})
        self.ok("card", "--days", "1", "-o", self.tmp / "c.svg", "--copy", "none")
        self.assertIn("1m", svg_texts(self.tmp / "c.svg"))

    def test_the_message_names_the_period(self):
        self.assertIn("Nothing was recorded for today", self.run_cli("stats", "--days", "1").stdout)
        self.assertIn("Nothing was recorded for the last 30 days", self.run_cli("stats", "--month").stdout)

    def test_time_without_an_hour_breakdown_still_makes_a_card(self):
        self.write_day(self.today, active_ms=3600000, hours_ms=[], apps_ms={"slack": 3600000}, switches=3)
        self.ok("card", "-o", self.tmp / "c.svg", "--copy", "none")
        self.assertIn("1h 00m", svg_texts(self.tmp / "c.svg"))

    def test_exclude_by_id_leaves_the_total_alone(self):
        self.record()
        everything = self.stats()
        for value in ("com.mitchellh.ghostty", "COM.MITCHELLH.GHOSTTY"):
            with self.subTest(exclude=value):
                stats = self.stats("--exclude", value)
                self.assertNotIn("Ghostty", [app["name"] for app in stats["apps"]])
                self.assertEqual(len(stats["apps"]), len(everything["apps"]) - 1)
                self.assertEqual(stats["screen_time_ms"], everything["screen_time_ms"])
                self.assertEqual(stats["days"], everything["days"])

    def test_exclude_by_name_in_any_case_and_more_than_once(self):
        self.record()
        everything = self.stats()
        stats = self.stats("--exclude", "ghostty", "--exclude", "SLACK")
        names = [app["name"] for app in stats["apps"]]
        self.assertNotIn("Ghostty", names)
        self.assertNotIn("Slack", names)
        self.assertEqual(len(names), len(everything["apps"]) - 2)
        self.assertEqual(stats["screen_time_ms"], everything["screen_time_ms"])

    def test_commits_in_a_repository_folder(self):
        self.record()
        repos = self.tmp / "repos"
        self.repo_with_commits(repos)
        stats = self.stats("--repos", repos)
        self.assertEqual(stats["commits"], 2)
        self.assertEqual(stats["repos_with_commits"], 1)
        self.assertEqual(stats["repos_scanned"], 1)

    def test_commits_follow_the_period(self):
        repos = self.tmp / "repos"
        self.repo_with_commits(repos)
        self.assertEqual(self.stats("--repos", repos, "--month")["commits"], 3)
        self.assertEqual(self.stats("--repos", repos, "--days", "2")["commits"], 1)
        self.assertEqual(self.stats("--repos", repos, "--days", "1")["commits"], 0)

    def test_a_folder_without_repositories_means_commits_is_null(self):
        self.record()
        empty = self.tmp / "no-repos-here"
        empty.mkdir()
        stats = self.stats("--repos", empty)
        self.assertIsNone(stats["commits"])
        self.assertEqual(stats["repos_scanned"], 0)
        self.assertIsNone(self.stats("--repos", self.tmp / "missing")["commits"])

    def test_repository_folders_may_be_repeated_or_comma_separated(self):
        first, second = self.tmp / "first", self.tmp / "second"
        self.repo_with_commits(first)
        commit(init_repo(second / "other"), self.local(self.today - timedelta(days=2)))
        for args in (("--repos", first, "--repos", second), ("--repos", "%s, %s" % (first, second))):
            with self.subTest(args=[str(a) for a in args]):
                stats = self.stats(*args)
                self.assertEqual((stats["commits"], stats["repos_scanned"], stats["repos_with_commits"]), (3, 2, 2))

    def test_without_git_commits_is_null(self):
        repos = self.tmp / "repos"
        self.repo_with_commits(repos)
        stats = json.loads(self.ok("stats", "--json", "--repos", repos, PATH=str(self.stubs.dir)).stdout)
        self.assertIsNone(stats["commits"])
        self.assertEqual(stats["repos_scanned"], 0)

    def test_default_folders_are_projects_and_code_in_home(self):
        self.repo_with_commits(self.home / "code")
        stats = self.stats()
        self.assertEqual((stats["commits"], stats["repos_scanned"]), (2, 1))

    def test_folders_from_the_widget_settings(self):
        repos = self.tmp / "configured"
        self.repo_with_commits(repos)
        self.write(self.home / ".config" / "omarchy" / "shell.json", json.dumps(
            {"bar": {"layout": {"right": [{"id": PLUGIN_ID, "repoDirs": str(repos)}]}}}))
        self.assertEqual(self.stats()["commits"], 2)
        # An explicit --repos beats the setting.
        self.assertIsNone(self.stats("--repos", self.tmp / "missing")["commits"])

    def test_nothing_is_written_to_the_data_folder_by_reading(self):
        self.ok("stats")
        self.assertFalse((self.data_home / "omawrapped").exists())


class ArgumentErrorTests(CliCase):
    def assert_argument_error(self, *args, message=""):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(message, result.stderr)
        # Nothing ran: the shell was not asked for anything, nothing was written.
        self.assertEqual(self.stubs.calls("omarchy-shell"), [])
        self.assertEqual(self.stubs.calls("wl-copy"), [])
        self.assertFalse(self.pictures.exists())

    def test_days_out_of_range(self):
        for command in ("stats", "card"):
            for value in ("0", "400", "367", "-3", "abc", "1.5", ""):
                with self.subTest(command=command, days=value):
                    self.assert_argument_error(command, "--days", value)

    def test_the_range_error_says_what_is_allowed(self):
        self.assert_argument_error("stats", "--days", "0", message="a whole number from 1 to 366")
        self.assert_argument_error("stats", "--days", "abc", message="a whole number from 1 to 366")

    def test_week_and_month_together(self):
        for command in ("stats", "card"):
            with self.subTest(command=command):
                self.assert_argument_error(command, "--week", "--month", message="not allowed with argument")

    def test_other_period_options_together(self):
        self.assert_argument_error("stats", "--days", "5", "--month", message="not allowed with argument")
        self.assert_argument_error("stats", "--week", "--days", "5", message="not allowed with argument")

    def test_today_excludes_every_other_period_option_in_either_order(self):
        for command in ("stats", "card"):
            for other in (("--week",), ("--month",), ("--days", "3"), ("--days", "1")):
                for args in (("--today", *other), (*other, "--today")):
                    with self.subTest(command=command, args=args):
                        self.assert_argument_error(command, *args, message="not allowed with argument")

    def test_today_takes_no_period_and_no_file_but_a_notify_option(self):
        self.assert_argument_error("today", "--days", "3", message="unrecognized arguments")
        self.assert_argument_error("today", "--today", message="unrecognized arguments")
        self.assert_argument_error("today", "x", message="unrecognized arguments")

    def test_pause_and_resume_take_nothing_but_a_notify_option(self):
        for command in ("pause", "resume"):
            with self.subTest(command=command):
                self.assert_argument_error(command, "--yes", message="unrecognized arguments")
                self.assert_argument_error(command, "now", message="unrecognized arguments")

    def test_a_bad_copy_choice(self):
        self.assert_argument_error("card", "--copy", "everything", message="invalid choice")

    def test_show_and_menu_take_no_notify_option(self):
        self.assert_argument_error("show", "--notify", message="unrecognized arguments")
        self.assert_argument_error("menu", "--notify", message="unrecognized arguments")

    def test_copy_and_show_take_one_file_at_most(self):
        self.assert_argument_error("copy", "a.png", "b.png", message="unrecognized arguments")
        self.assert_argument_error("show", "a.png", "b.png", message="unrecognized arguments")


class EmptyStateTests(CliCase):
    """What `card` and `stats` say when less than a minute was counted: it depends on whether the sampler is there."""

    def counting(self, when="the last 7 days"):
        return ("OmaWrapped has counted less than a minute for %s so far. "
                "It is counting now: try again in a minute." % when)

    def paused(self, when="the last 7 days"):
        return ("OmaWrapped has counted less than a minute for %s, and counting is paused. "
                "Resume it from the widget's menu or with `omawrapped resume`." % when)

    def lines(self, what="the last 7 days", length=7, first="Nothing"):
        """The three lines for a machine without a sampler of ours, as they were."""
        return ("%s was recorded for %s (%s).\n"
                "OmaWrapped counts while its widget is enabled in the bar: omarchy plugin enable %s\n"
                "Data folder: %s" % (first, what, aggregate.last_days(length, self.today).span, PLUGIN_ID,
                                     self.data_home / "omawrapped"))

    def said_by_card(self, *args) -> str:
        result = self.run_cli("card", "-o", self.tmp / "c.svg", "--copy", "none", *args)
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertFalse((self.tmp / "c.svg").exists())
        self.assertTrue(result.stderr.endswith("\n"))
        return result.stderr[:-1]

    def said_by_stats(self, *args, length=7) -> str:
        result = self.ok("stats", *args)
        period = aggregate.last_days(length, self.today)
        header = "OmaWrapped · %s · %s\n\n" % (period.label, period.span)
        self.assertTrue(result.stdout.startswith(header), result.stdout)
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(result.stderr, "")
        return result.stdout[len(header):-1]

    def test_a_counting_sampler_is_asked_to_be_patient_in_one_line(self):
        self.sampler()
        self.assertEqual(self.said_by_card(), self.counting())
        self.assertEqual(self.said_by_stats(), self.counting())

    def test_a_paused_sampler_says_how_to_resume_in_one_line(self):
        self.sampler(paused=True)
        self.assertEqual(self.said_by_card(), self.paused())
        self.assertEqual(self.said_by_stats(), self.paused())

    def test_a_status_without_the_paused_key_is_a_sampler_that_counts(self):
        self.sampler(paused=False)
        self.assertEqual(self.said_by_card(), self.counting())
        self.sampler()
        self.assertEqual(self.said_by_card(), self.counting())

    def test_without_a_sampler_the_three_lines_stay_as_they_were(self):
        self.assertEqual(self.said_by_card(), self.lines())
        self.assertEqual(self.said_by_stats(), self.lines())

    def test_a_sampler_that_records_elsewhere_is_no_sampler_of_ours(self):
        # Its pause is not ours to report either.
        for fields in ({}, {"paused": True}):
            with self.subTest(fields=fields):
                self.sampler(dataDir="/somewhere/else/omawrapped", **fields)
                self.assertEqual(self.said_by_card(), self.lines())
                self.assertEqual(self.said_by_stats(), self.lines())

    def test_garbage_from_the_shell_is_no_sampler(self):
        self.stubs.reply_to_status("this is not json")
        self.assertEqual(self.said_by_card(), self.lines())

    def test_a_few_seconds_are_still_less_than_a_minute_and_say_the_same(self):
        self.write_day(self.today, active_ms=59999, apps_ms={"slack": 59999})
        self.assertEqual(self.said_by_card(), self.lines(first="Less than a minute"))
        self.sampler()
        self.assertEqual(self.said_by_card(), self.counting())
        self.assertEqual(self.said_by_stats(), self.counting())
        self.sampler(paused=True)
        self.assertEqual(self.said_by_stats(), self.paused())

    def test_the_period_is_named_the_way_the_option_asks(self):
        situations = (
            ("no sampler", None, lambda when, length: self.lines(when, length)),
            ("a counting one", {}, lambda when, length: self.counting(when)),
            ("a paused one", {"paused": True}, lambda when, length: self.paused(when)),
        )
        options = ((("--today",), "today", 1), (("--month",), "the last 30 days", 30),
                   (("--days", "3"), "the last 3 days", 3))
        for name, fields, expected in situations:
            if fields is not None:
                self.sampler(**fields)
            for args, when, length in options:
                with self.subTest(sampler=name, args=args):
                    self.assertEqual(self.said_by_card(*args), expected(when, length))
                    self.assertEqual(self.said_by_stats(*args, length=length), expected(when, length))

    def test_the_sampler_is_still_asked_to_flush_first(self):
        self.sampler()
        self.said_by_card()
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS, FLUSH])

    def test_json_has_no_message_at_all(self):
        self.sampler()
        self.assertEqual(self.stats()["screen_time_ms"], 0)


class TodayOptionTests(CliCase):
    """--today is one day: the same as --days 1."""

    def test_stats_for_today_is_stats_for_one_day(self):
        self.record()
        stats = self.stats("--today")
        self.assertEqual(stats, self.stats("--days", "1"))
        self.assertEqual(stats["period"], {"start": self.today.isoformat(), "end": self.today.isoformat(), "days": 1})
        self.assertEqual(stats["screen_time_ms"], self.window(1)[0]["active_ms"])
        self.assertEqual(len(stats["days"]), 1)

    def test_the_text_report_is_headed_with_today(self):
        self.record()
        self.assertIn("OmaWrapped · Today · " + aggregate.last_days(1, self.today).span,
                      self.ok("stats", "--today").stdout)
        self.assertEqual(self.ok("stats", "--today").stdout, self.ok("stats", "--days", "1").stdout)

    def test_a_card_for_today_is_the_card_of_one_day(self):
        self.record()
        self.ok("card", "--today", "-o", self.tmp / "a.svg", "--copy", "none")
        self.ok("card", "--days", "1", "-o", self.tmp / "b.svg", "--copy", "none")
        self.assertEqual(svg_texts(self.tmp / "a.svg"), svg_texts(self.tmp / "b.svg"))
        self.assertIn("Today · " + aggregate.last_days(1, self.today).span, svg_texts(self.tmp / "a.svg"))

    @needs_renderer
    def test_the_default_name_of_a_card_for_today_is_the_one_of_one_day(self):
        self.record()
        self.assertEqual(self.ok("card", "--today", "--copy", "none").stdout,
                         str(self.pictures / ("omawrapped-%s-1d.png" % self.today.isoformat())) + "\n")

    def test_the_options_say_what_it_is(self):
        for command in ("stats", "card"):
            with self.subTest(command=command):
                self.assertIn("--today", self.ok(command, "--help").stdout)
        words = " ".join(self.ok("stats", "--help").stdout.split())
        self.assertIn("--today today only", words)


class CardTests(CliCase):
    def test_no_data_exits_1_writes_nothing_and_says_so(self):
        output = self.tmp / "c.svg"
        result = self.run_cli("card", "-o", output)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Nothing was recorded", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(self.pictures.exists())
        self.assertEqual(self.stubs.calls("wl-copy"), [])
        self.assertEqual(self.stubs.calls("xdg-open"), [])

    def test_no_data_with_the_default_output_writes_nothing(self):
        self.assertEqual(self.run_cli("card").returncode, 1)
        self.assertFalse(self.pictures.exists())

    def test_the_sampler_is_asked_to_flush_first(self):
        self.record()
        self.sampler()
        self.ok("card", "-o", self.tmp / "c.svg", "--copy", "none")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [PLUGIN_ID + " status", PLUGIN_ID + " flush"])

    def test_svg_output_prints_exactly_its_absolute_path(self):
        self.record()
        output = self.tmp / "out" / "c.svg"
        result = self.ok("card", "-o", output, "--copy", "none")
        self.assertEqual(result.stdout, str(output) + "\n")
        root = ET.parse(output).getroot()
        self.assertEqual((root.tag, root.get("width"), root.get("height")), (SVG + "svg", "1600", "900"))
        self.assertIn("Saved " + str(output), result.stderr)

    def test_a_relative_output_path_is_printed_absolute(self):
        self.record()
        result = self.ok("card", "-o", "c.svg", "--copy", "none", cwd=self.work)
        self.assertEqual(result.stdout, str(self.work / "c.svg") + "\n")
        self.assertTrue((self.work / "c.svg").is_file())

    def test_a_tilde_in_the_output_path_means_home(self):
        self.record()
        result = self.ok("card", "-o", "~/cards/c.svg", "--copy", "none")
        self.assertEqual(result.stdout, str(self.home / "cards" / "c.svg") + "\n")
        self.assertTrue((self.home / "cards" / "c.svg").is_file())

    def test_an_existing_file_is_replaced(self):
        self.record()
        output = self.write(self.tmp / "c.svg", "old")
        self.ok("card", "-o", output, "--copy", "none")
        self.assertIn("<svg", output.read_text(encoding="utf-8"))

    def test_the_card_shows_the_recorded_time(self):
        self.record()
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "none")
        total = sum(raw["active_ms"] for raw in self.window(7))
        found = svg_texts(output)
        self.assertIn(clock(total), found)
        self.assertIn("Last 7 days \u00b7 " + aggregate.last_days(7, self.today).span, found)
        self.assertIn("Ghostty", found)

    def test_month_and_days_options_change_the_period(self):
        self.record()
        cases = ((("--month",), "Last 30 days"), (("--days", "10"), "Last 10 days"), (("--days", "1"), "Today"))
        for args, label in cases:
            with self.subTest(args=args):
                output = self.tmp / "c.svg"
                self.ok("card", "-o", output, "--copy", "none", *args)
                self.assertTrue(any(text.startswith(label) for text in svg_texts(output)), svg_texts(output))

    def test_exclude_removes_the_app_from_the_card(self):
        self.record()
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "none", "--exclude", "chromium")
        found = svg_texts(output)
        self.assertNotIn("Chromium", found)
        self.assertIn("Ghostty", found)

    def test_a_theme_folder_without_colors_is_refused(self):
        # A mistyped folder must not quietly give a card in the fallback colours.
        self.record()
        empty = self.tmp / "themes" / "no-such-theme"
        empty.mkdir(parents=True)
        for folder in (empty, self.tmp / "nowhere"):
            result = self.run_cli("card", "--theme", folder, "-o", self.tmp / "c.svg", "--copy", "none")
            self.assertEqual(result.returncode, 1)
            self.assertIn("is not a theme folder", result.stderr)
            self.assertFalse((self.tmp / "c.svg").exists())

    def test_the_footer_and_colors_come_from_the_system_and_the_theme_folder(self):
        self.record()
        theme = self.write(self.tmp / "themes" / "my-theme" / "colors.toml",
                           'background = "#0a1b2c"\nforeground = "#e0e0e0"\naccent = "#ff8800"\n').parent
        for name in ("one", "two"):
            self.write(self.home / ".config" / "omarchy" / "plugins" / name / "manifest.json", "{}")
        self.write(self.home / ".config" / "omarchy" / "plugins" / "not-a-plugin" / "readme.txt", "x")
        self.write(self.omarchy / "version", "9.9.9\n")
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "none", "--theme", theme)
        self.assertIn("My Theme theme \u00b7 2 plugins \u00b7 Omarchy 9.9.9", svg_texts(output))
        self.assertIn('fill="#0a1b2c"', output.read_text(encoding="utf-8"))

    def test_the_commit_tile_appears_when_repositories_are_found(self):
        self.record()
        repos = self.tmp / "repos"
        self.repo_with_commits(repos)
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "none", "--repos", repos)
        found = svg_texts(output)
        self.assertIn("commits in 1 repo", found)
        self.assertIn("2", found)

    def test_there_is_no_commit_tile_when_nothing_was_committed(self):
        self.record()
        repos = self.tmp / "repos"
        repo = init_repo(repos / "quiet")
        commit(repo, self.local(self.today - timedelta(days=90)))
        self.assertEqual(self.stats("--repos", repos)["commits"], 0)
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "none", "--repos", repos)
        self.assertFalse([text for text in svg_texts(output) if "commit" in text])

    def test_there_is_no_commit_tile_without_repositories(self):
        self.record()
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "none", "--repos", self.tmp / "missing")
        self.assertFalse([text for text in svg_texts(output) if "commit" in text])

    def test_an_output_that_cannot_be_written_is_an_error_not_a_crash(self):
        self.record()
        blocker = self.write(self.tmp / "blocker", "a file, not a folder")
        result = self.run_cli("card", "-o", blocker / "c.svg", "--copy", "none")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.stubs.calls("wl-copy"), [])

    # ---- the clipboard and the viewer ----

    def test_the_path_is_copied_by_default(self):
        self.record()
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output)
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "text/plain"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), str(output).encode())

    def test_copy_path_gives_it_the_path_on_its_standard_input(self):
        self.record()
        output = self.tmp / "c.svg"
        result = self.ok("card", "-o", output, "--copy", "path")
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "text/plain"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), str(output).encode())
        self.assertIn("(path copied)", result.stderr)

    def test_copy_none_runs_no_wl_copy(self):
        self.record()
        self.ok("card", "-o", self.tmp / "c.svg", "--copy", "none")
        self.assertEqual(self.stubs.calls("wl-copy"), [])

    @needs_renderer
    def test_copy_image_sends_a_png(self):
        self.record()
        result = self.ok("card", "-o", self.tmp / "c.png", "--copy", "image")
        self.assertEqual(self.stubs.calls("wl-copy"), ["--type image/png"])
        self.assertIn("(image copied)", result.stderr)

    def test_copy_image_of_an_svg_is_labelled_as_an_svg(self):
        self.record()
        self.ok("card", "-o", self.tmp / "c.svg", "--copy", "image")
        self.assertEqual(self.stubs.calls("wl-copy"), ["--type image/svg+xml"])

    def test_open_starts_xdg_open_with_the_path(self):
        self.record()
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "none", "--open")
        self.assertEqual(self.stubs.wait_for_call("xdg-open"), [str(output)])

    def test_the_viewer_is_not_started_unless_asked(self):
        self.record()
        self.ok("card", "-o", self.tmp / "c.svg")
        self.assertEqual(self.stubs.calls("xdg-open"), [])

    def test_a_missing_wl_copy_is_only_a_warning(self):
        self.record()
        self.stubs.remove("wl-copy")
        result = self.run_cli("card", "-o", self.tmp / "c.svg")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, NO_WL_COPY + "\nSaved %s\n" % (self.tmp / "c.svg"))
        self.assertEqual(result.stdout, str(self.tmp / "c.svg") + "\n")

    def test_a_failing_wl_copy_is_only_a_warning_too(self):
        self.record()
        self.stubs.fail("wl-copy")
        for what in ("path", "image"):
            with self.subTest(copy=what):
                result = self.run_cli("card", "-o", self.tmp / "c.svg", "--copy", what)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, COPY_FAILED + "\nSaved %s\n" % (self.tmp / "c.svg"))
                self.assertEqual(result.stdout, str(self.tmp / "c.svg") + "\n")

    def test_a_missing_xdg_open_is_only_a_warning(self):
        self.record()
        self.stubs.remove("xdg-open")
        result = self.run_cli("card", "-o", self.tmp / "c.svg", "--copy", "none", "--open")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "Saved %s\n%s\n" % (self.tmp / "c.svg", NO_XDG_OPEN))
        self.assertEqual(result.stdout, str(self.tmp / "c.svg") + "\n")

    # ---- the default output: a PNG in the Pictures folder ----

    @needs_renderer
    def test_the_default_output_is_a_png_in_the_pictures_folder(self):
        self.record()
        expected = self.pictures / ("omawrapped-%s.png" % self.today.isoformat())
        result = self.ok("card")
        self.assertEqual(result.stdout, str(expected) + "\n")
        data = expected.read_bytes()
        self.assertEqual(data[:8], PNG_SIGNATURE)
        self.assertEqual(struct.unpack(">II", data[16:24]), (1600, 900))
        self.assertEqual(os.listdir(self.pictures), [expected.name])
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "text/plain"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), str(expected).encode())

    @needs_renderer
    def test_the_month_card_is_named_with_a_month_suffix(self):
        self.record()
        expected = self.pictures / ("omawrapped-%s-month.png" % self.today.isoformat())
        self.assertEqual(self.ok("card", "--month", "--copy", "none").stdout, str(expected) + "\n")
        self.assertTrue(expected.is_file())

    @needs_renderer
    def test_a_card_of_n_days_is_named_with_the_number_of_days(self):
        self.record()
        expected = self.pictures / ("omawrapped-%s-10d.png" % self.today.isoformat())
        self.assertEqual(self.ok("card", "--days", "10", "--copy", "none").stdout, str(expected) + "\n")
        self.assertTrue(expected.is_file())

    @needs_renderer
    def test_seven_days_and_thirty_days_get_the_week_and_month_names(self):
        self.record()
        day = self.today.isoformat()
        self.ok("card", "--days", "7", "--copy", "none")
        self.ok("card", "--days", "30", "--copy", "none")
        self.assertEqual(sorted(os.listdir(self.pictures)),
                         ["omawrapped-%s-month.png" % day, "omawrapped-%s.png" % day])

    @needs_renderer
    def test_the_pictures_folder_follows_xdg_pictures_dir(self):
        self.record()
        elsewhere = self.tmp / "My Photos"
        result = self.ok("card", "--copy", "none", XDG_PICTURES_DIR=str(elsewhere))
        self.assertEqual(result.stdout, str(elsewhere / ("omawrapped-%s.png" % self.today.isoformat())) + "\n")
        self.assertFalse(self.pictures.exists())

    @needs_renderer
    def test_a_png_output_may_be_named_anything(self):
        self.record()
        output = self.tmp / "mine.PNG"
        self.ok("card", "-o", output, "--copy", "none")
        self.assertEqual(output.read_bytes()[:8], PNG_SIGNATURE)


class CardNotifyTests(OnTheBus, CliCase):
    """card --notify: the desktop is told, in one of three ways, what became of the card."""

    def click(self, path) -> list:
        return [str(LAUNCHER), "show", str(path)]

    @needs_renderer
    def test_an_image_on_the_clipboard_is_announced_as_copied(self):
        self.record()
        output = self.tmp / "c.png"
        result = self.ok("card", "-o", output, "--copy", "image", "--notify")
        self.assertEqual(result.stdout, str(output) + "\n")
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "image/png"]])
        self.assertEqual(self.sent(), [notification(
            "Card copied", "Paste it into a post. " + CLICK_HINT, image=output, click=self.click(output))])

    @needs_renderer
    def test_a_path_on_the_clipboard_is_announced_as_saved(self):
        self.record()
        output = self.tmp / "c.png"
        self.ok("card", "-o", output, "--notify")
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "text/plain"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), str(output).encode())
        self.assertEqual(self.sent(), [notification(
            "Card saved", "Its path is on the clipboard. " + CLICK_HINT, image=output, click=self.click(output))])

    @needs_renderer
    def test_nothing_on_the_clipboard_says_where_the_card_is(self):
        self.record()
        output = self.tmp / "c.png"
        self.ok("card", "-o", output, "--copy", "none", "--notify")
        self.assertEqual(self.stubs.argv("wl-copy"), [])
        self.assertEqual(self.sent(), [notification(
            "Card saved", "%s. %s" % (output, CLICK_HINT), image=output, click=self.click(output))])

    @needs_renderer
    def test_the_default_card_is_announced_with_the_path_that_was_printed(self):
        self.record()
        result = self.ok("card", "--copy", "image", "--notify")
        expected = self.pictures / ("omawrapped-%s.png" % self.today.isoformat())
        self.assertEqual(result.stdout, str(expected) + "\n")
        self.assertEqual(self.sent(), [notification(
            "Card copied", "Paste it into a post. " + CLICK_HINT, image=expected, click=self.click(expected))])

    @needs_renderer
    def test_a_png_is_a_png_whatever_the_case_of_its_name(self):
        self.record()
        output = self.tmp / "mine.PNG"
        self.ok("card", "-o", output, "--copy", "none", "--notify")
        self.assertEqual(self.sent()[0]["hints"]["image-path"], str(output))

    def test_the_image_of_an_svg_is_not_passed_on(self):
        self.record()
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "image", "--notify")
        self.assertEqual(self.sent(), [notification(
            "Card copied", "Paste it into a post. " + CLICK_HINT, click=self.click(output))])
        self.assertNotIn("image-path", self.sent()[0]["hints"])

    def test_a_click_runs_this_command_by_its_absolute_path(self):
        self.record()
        link = self.tmp / "linked-omawrapped"
        link.symlink_to(LAUNCHER)
        output = self.tmp / "c.svg"
        # Even when the command was started through a link that will be gone, the click must find it.
        self.ok("card", "-o", output, "--copy", "none", "--notify", launcher=link)
        command = json.loads(self.sent()[0]["hints"]["omarchy-exec-argv"])
        self.assertEqual(command, [str(LAUNCHER), "show", str(output)])
        self.assertTrue(os.path.isabs(command[0]) and os.access(command[0], os.X_OK))

    def test_a_relative_output_is_announced_by_its_absolute_path(self):
        self.record()
        self.ok("card", "-o", "c.svg", "--copy", "none", "--notify", cwd=self.work)
        output = self.work / "c.svg"
        self.assertEqual(self.sent(), [notification(
            "Card saved", "%s. %s" % (output, CLICK_HINT), click=self.click(output))])

    def test_without_notify_nothing_is_said(self):
        self.record()
        for copy in ("path", "image", "none"):
            self.ok("card", "-o", self.tmp / "c.svg", "--copy", copy)
        self.assertEqual(self.sent(), [])
        self.assert_nothing_started("wl-copy", "omarchy-shell")

    def test_a_copy_that_failed_is_reported_and_the_card_is_called_saved(self):
        self.record()
        self.stubs.fail("wl-copy")
        output = self.tmp / "c.svg"
        for what in ("path", "image"):
            with self.subTest(copy=what):
                self.bus.clear()
                result = self.ok("card", "-o", output, "--copy", what, "--notify")
                self.assertEqual(result.stderr, COPY_FAILED + "\nSaved %s\n" % output)
                self.assertEqual(result.stdout, str(output) + "\n")
                # A warning is not a failure: the desktop is told about the card, and that is all it is told.
                self.assertEqual(self.sent(), [notification(
                    "Card saved", "%s. %s" % (output, CLICK_HINT), click=self.click(output))])

    def test_a_missing_wl_copy_is_reported_and_the_card_is_called_saved(self):
        self.record()
        self.stubs.remove("wl-copy")
        output = self.tmp / "c.svg"
        result = self.ok("card", "-o", output, "--notify")
        self.assertEqual(result.stderr, NO_WL_COPY + "\nSaved %s\n" % output)
        self.assertEqual(self.sent(), [notification(
            "Card saved", "%s. %s" % (output, CLICK_HINT), click=self.click(output))])

    def test_no_program_is_started_to_say_it_and_none_is_needed(self):
        # Omarchy's notification tool and notify-send would be given the text as arguments, where everybody can
        # read it. They are not started, and a machine without them gets the notification all the same.
        self.record()
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "image", "--notify")
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.stubs.argv("omarchy-notification-send"), [])
        self.assertEqual(self.stubs.argv("notify-send"), [])
        self.stubs.remove("omarchy-notification-send", "notify-send")
        self.ok("card", "-o", output, "--copy", "image", "--notify")
        self.assertEqual(len(self.sent()), 2)

    def test_without_a_bus_the_card_is_still_made_and_nothing_is_said_about_it(self):
        self.record()
        output = self.tmp / "c.svg"
        for name, address in (("none named", ""), ("a dead one", isolated_env(self.tmp)["DBUS_SESSION_BUS_ADDRESS"])):
            with self.subTest(bus=name):
                result = self.ok("card", "-o", output, "--copy", "none", "--notify", DBUS_SESSION_BUS_ADDRESS=address)
                self.assertEqual(result.stdout, str(output) + "\n")
                self.assertEqual(result.stderr, "Saved %s\n" % output)
        self.assertEqual(self.sent(), [])

    def test_without_python_gobject_the_card_is_still_made_and_nothing_is_said_about_it(self):
        self.record()
        output = self.tmp / "c.svg"
        result = self.ok("card", "-o", output, "--copy", "none", "--notify", **self.without_python_gobject())
        self.assertEqual((result.stdout, result.stderr), (str(output) + "\n", "Saved %s\n" % output))
        self.assertEqual(self.sent(), [])

    def test_a_service_that_refuses_does_not_fail_the_card(self):
        self.record()
        self.bus.refuse(True)
        output = self.tmp / "c.svg"
        result = self.ok("card", "-o", output, "--copy", "none", "--notify")
        self.assertEqual(result.stdout, str(output) + "\n")
        self.assertEqual(result.stderr, "Saved %s\n" % output)
        self.assertEqual(self.sent(), [])

    def test_open_and_notify_together(self):
        self.record()
        output = self.tmp / "c.svg"
        self.ok("card", "-o", output, "--copy", "none", "--open", "--notify")
        self.assertEqual(self.stubs.wait_for_argv("xdg-open"), [[str(output)]])
        self.assertEqual(len(self.sent()), 1)


class CopyCommandTests(CliCase):
    needs_bus = True

    def test_without_a_file_the_newest_card_in_the_pictures_folder_is_copied(self):
        self.make_card("omawrapped-2026-10-09.png", mtime=1000)
        newest = self.make_card("omawrapped-2026-10-02.png", mtime=3000)
        self.make_card("omawrapped-2026-09-30-month.png", mtime=2000)
        result = self.ok("copy")
        self.assertEqual(result.stdout, str(newest) + "\n")
        self.assertEqual(result.stderr, "Copied %s (image)\n" % newest)
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "image/png"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), newest.read_bytes())
        self.assert_nothing_started("wl-copy")

    def test_other_files_in_the_pictures_folder_are_not_cards(self):
        card = self.make_card(mtime=1000)
        self.make_card("screenshot-2026-10-09.png", mtime=5000)
        self.make_card("omawrapped-2026-10-09.svg", mtime=5000)
        self.assertEqual(self.ok("copy").stdout, str(card) + "\n")

    def test_the_pictures_folder_is_the_one_the_system_names(self):
        elsewhere = self.tmp / "My Photos"
        card = self.make_card()
        moved = elsewhere / card.name
        elsewhere.mkdir()
        card.rename(moved)
        self.assertEqual(self.ok("copy", XDG_PICTURES_DIR=str(elsewhere)).stdout, str(moved) + "\n")

    def test_a_file_is_copied_instead_of_the_newest_card(self):
        self.make_card(mtime=5000)
        chosen = self.make_card("omawrapped-2026-01-01.png", mtime=1000, content=PNG_SIGNATURE + b"chosen")
        result = self.ok("copy", chosen)
        self.assertEqual(result.stdout, str(chosen) + "\n")
        self.assertEqual(self.stubs.stdin("wl-copy"), PNG_SIGNATURE + b"chosen")

    def test_a_file_need_not_be_in_the_pictures_folder_or_named_like_a_card(self):
        file = self.write(self.work / "mine.png", "not really a png")
        result = self.ok("copy", "mine.png")
        self.assertEqual(result.stdout, str(file) + "\n")
        self.assertEqual(result.stderr, "Copied %s (image)\n" % file)
        self.assertEqual(self.stubs.stdin("wl-copy"), b"not really a png")

    def test_a_tilde_means_home_and_the_path_is_printed_whole(self):
        file = self.write(self.home / "cards" / "c.png", "x")
        self.assertEqual(self.ok("copy", "~/cards/../cards/c.png").stdout, str(file) + "\n")

    def test_an_svg_is_copied_as_an_svg(self):
        file = self.write(self.work / "c.svg", "<svg/>")
        self.ok("copy", file)
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "image/svg+xml"]])

    def test_without_any_card_it_says_so(self):
        for prepare in ("no folder", "empty folder", "other files only"):
            with self.subTest(pictures=prepare):
                if prepare == "empty folder":
                    self.pictures.mkdir()
                elif prepare == "other files only":
                    self.make_card("screenshot.png")
                result = self.run_cli("copy")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, NO_CARD + "\n")
                self.assert_nothing_started()

    def test_a_file_that_does_not_exist(self):
        self.make_card()
        missing = self.tmp / "nowhere" / "c.png"
        result = self.run_cli("copy", missing)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "%s does not exist.\n" % missing)
        self.assert_nothing_started()

    def test_a_folder_is_not_a_card(self):
        result = self.run_cli("copy", self.work)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("is not a file", result.stderr)
        self.assert_nothing_started()

    def test_a_missing_wl_copy_is_an_error(self):
        self.make_card()
        self.stubs.remove("wl-copy")
        result = self.run_cli("copy")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, NO_WL_COPY + "\n")

    def test_a_failing_wl_copy_is_an_error(self):
        self.make_card()
        self.stubs.fail("wl-copy")
        result = self.run_cli("copy")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, COPY_FAILED + "\n")

    def test_notify_says_that_the_card_is_on_the_clipboard(self):
        card = self.make_card("omawrapped-2026-10-02.png")
        result = self.ok("copy", "--notify", **self.on_bus)
        self.assertEqual(result.stdout, str(card) + "\n")
        self.assertEqual(self.sent(), [notification(
            "Card copied", "omawrapped-2026-10-02.png is on the clipboard. Paste it anywhere.", image=card)])

    def test_notify_names_the_file_that_was_copied(self):
        file = self.write(self.work / "mine.png", "x")
        self.ok("copy", file, "--notify", **self.on_bus)
        self.assertEqual(self.sent(), [notification(
            "Card copied", "mine.png is on the clipboard. Paste it anywhere.", image=file)])

    def test_notify_starts_no_program_to_say_it(self):
        self.make_card()
        self.ok("copy", "--notify", **self.on_bus)
        self.assertEqual(len(self.sent()), 1)
        self.assert_nothing_started("wl-copy")

    def test_notify_without_a_bus_changes_nothing(self):
        card = self.make_card()
        for address in ("", isolated_env(self.tmp)["DBUS_SESSION_BUS_ADDRESS"]):
            with self.subTest(address=address):
                result = self.ok("copy", "--notify", DBUS_SESSION_BUS_ADDRESS=address)
                self.assertEqual(result.stdout, str(card) + "\n")
                self.assertEqual(result.stderr, "Copied %s (image)\n" % card)
        self.assertEqual(self.sent(), [])

    def test_a_service_that_refuses_changes_nothing(self):
        card = self.make_card()
        self.bus.refuse(True)
        result = self.ok("copy", "--notify", **self.on_bus)
        self.assertEqual((result.stdout, result.stderr), (str(card) + "\n", "Copied %s (image)\n" % card))

    def test_without_notify_nothing_is_said(self):
        self.make_card()
        self.ok("copy", **self.on_bus)
        self.assertEqual(self.sent(), [])
        self.assert_nothing_started("wl-copy")

    def test_a_copy_that_failed_is_said_on_the_desktop_and_not_announced_as_done(self):
        self.make_card()
        self.stubs.fail("wl-copy")
        self.assertEqual(self.run_cli("copy", "--notify").returncode, 1)
        result = self.run_cli("copy", "--notify", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", COPY_FAILED + "\n"))
        self.assertEqual(self.sent(), [notification("OmaWrapped", COPY_FAILED)])
        self.assert_nothing_started("wl-copy")

    def test_the_sampler_is_not_involved(self):
        self.make_card()
        self.ok("copy")
        self.assertEqual(self.stubs.argv("omarchy-shell"), [])


class ShowCommandTests(CliCase):
    def test_without_a_file_the_newest_card_is_shown(self):
        self.make_card("omawrapped-2026-10-09.png", mtime=1000)
        newest = self.make_card("omawrapped-2026-10-02.png", mtime=3000)
        result = self.ok("show")
        self.assertEqual(result.stdout, str(newest) + "\n")
        self.assertEqual(self.stubs.wait_for_argv("uwsm-app"), [["--", "nautilus", "--select", str(newest)]])
        self.assert_nothing_started("uwsm-app")

    def test_a_file_is_shown_instead(self):
        self.make_card(mtime=5000)
        file = self.write(self.work / "mine.png", "x")
        result = self.ok("show", "mine.png")
        self.assertEqual(result.stdout, str(file) + "\n")
        self.assertEqual(self.stubs.wait_for_argv("uwsm-app"), [["--", "nautilus", "--select", str(file)]])

    def test_without_uwsm_app_nautilus_is_started_directly(self):
        card = self.make_card()
        self.stubs.remove("uwsm-app")
        self.ok("show")
        self.assertEqual(self.stubs.wait_for_argv("nautilus"), [["--select", str(card)]])
        self.assert_nothing_started("nautilus")

    def test_without_nautilus_the_folder_is_opened(self):
        card = self.make_card()
        self.stubs.remove("nautilus")
        result = self.ok("show")
        self.assertEqual(result.stdout, str(card) + "\n")
        self.assertEqual(self.stubs.wait_for_argv("xdg-open"), [[str(self.pictures)]])
        self.assert_nothing_started("xdg-open")

    def test_without_a_file_manager_it_is_an_error_that_says_where_the_card_is(self):
        card = self.make_card()
        self.stubs.remove("nautilus", "xdg-open")
        result = self.run_cli("show")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "No file manager was found to show the card in. It is at %s.\n" % card)
        self.assert_nothing_started()

    def test_without_any_card_it_says_so(self):
        result = self.run_cli("show")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, NO_CARD + "\n")
        self.assert_nothing_started()

    def test_a_file_that_does_not_exist(self):
        self.make_card()
        missing = self.tmp / "nowhere" / "c.png"
        result = self.run_cli("show", missing)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "%s does not exist.\n" % missing)
        self.assert_nothing_started()

    def test_a_click_on_a_notification_shows_the_card(self):
        # What the notification of `card --notify` runs: the command it was given, as it was given.
        card = self.make_card()
        result = subprocess.run([str(LAUNCHER), "show", str(card)], capture_output=True, text=True,
                                env=self.environment(), cwd="/", stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stubs.wait_for_argv("uwsm-app"), [["--", "nautilus", "--select", str(card)]])


class TodayTests(CliCase):
    needs_bus = True

    def test_lists_the_time_and_the_top_five_apps_most_time_first(self):
        self.busy_day()
        result = self.ok("today")
        self.assertEqual(result.stdout.splitlines(), [TODAY] + TODAY_ROWS)
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(result.stderr, "")

    def test_the_columns_are_those_of_the_top_apps_of_stats_without_the_percentage(self):
        self.busy_day()
        rows = self.ok("stats", "--today").stdout.split("Top apps\n")[1].split("\n\n")[0].splitlines()
        self.assertEqual([line[:40] for line in rows][:5], TODAY_ROWS)
        self.assertEqual(rows[0], TODAY_ROWS[0] + "   43%")

    def test_fewer_apps_are_fewer_rows(self):
        self.write_day(self.today, active_ms=5400000, apps_ms={"zed": 1800000, "com.mitchellh.ghostty": 3600000})
        self.assertEqual(self.ok("today").stdout.splitlines(),
                         ["Today  1h 30m", row("Ghostty", "1h 00m"), row("Zed", "30m")])

    def test_a_long_name_is_cut_to_the_width_of_the_column(self):
        self.write_day(self.today, active_ms=3600000, apps_ms={"a" * 40: 3600000})
        self.assertEqual(self.ok("today").stdout.splitlines()[1], "  " + "A" + "a" * 27 + " " + "1h 00m".rjust(9))

    def test_only_today_counts(self):
        self.busy_day()
        self.write_day(self.today - timedelta(days=1), active_ms=36000000, apps_ms={"discord": 36000000})
        self.write_day(self.today + timedelta(days=1), active_ms=36000000, apps_ms={"discord": 36000000})
        self.assertEqual(self.ok("today").stdout.splitlines(), [TODAY] + TODAY_ROWS)

    def test_nothing_counted_yet(self):
        result = self.ok("today")
        self.assertEqual((result.stdout, result.stderr), ("Today  nothing counted yet\n", ""))

    def test_less_than_a_minute_is_nothing_counted_yet_and_a_minute_is_something(self):
        self.write_day(self.today, active_ms=59999, apps_ms={"slack": 59999})
        self.assertEqual(self.ok("today").stdout, "Today  nothing counted yet\n")
        self.write_day(self.today, active_ms=60000, apps_ms={"slack": 60000})
        self.assertEqual(self.ok("today").stdout.splitlines(), ["Today  1m", row("Slack", "1m")])

    def test_a_paused_sampler_is_said_in_the_first_line_in_both_cases(self):
        self.sampler(paused=True)
        self.assertEqual(self.ok("today").stdout, "Today  nothing counted yet (paused)\n")
        self.busy_day()
        self.assertEqual(self.ok("today").stdout.splitlines(), [TODAY + " (paused)"] + TODAY_ROWS)

    def test_a_sampler_that_counts_says_nothing_of_pausing(self):
        self.busy_day()
        for fields in ({}, {"paused": False}, {"counting": False}):
            with self.subTest(fields=fields):
                self.sampler(**fields)
                self.assertEqual(self.ok("today").stdout.splitlines(), [TODAY] + TODAY_ROWS)

    def test_the_pause_of_a_sampler_that_records_elsewhere_is_not_ours_to_report(self):
        self.busy_day()
        self.sampler(paused=True, dataDir="/somewhere/else/omawrapped")
        self.assertEqual(self.ok("today").stdout.splitlines(), [TODAY] + TODAY_ROWS)

    def write_ignored(self, apps: str) -> None:
        self.write(self.config / "omarchy" / "shell.json", json.dumps(
            {"version": 1, "bar": {"layout": {"right": [{"id": PLUGIN_ID, "ignoreApps": apps}]}}}))

    def test_ignored_apps_stay_out_and_the_next_one_takes_their_place(self):
        self.busy_day()
        self.write_ignored("SLACK, com.mitchellh.ghostty")
        # The time is still screen time: the total does not change.
        self.assertEqual(self.ok("today").stdout.splitlines(), [
            TODAY, TODAY_ROWS[1], TODAY_ROWS[2], TODAY_ROWS[4], row("Signal", "4m")])

    def test_ignoring_every_app_leaves_the_total_alone(self):
        self.busy_day()
        self.write_ignored("slack, signal, ghostty, zed, obsidian, chromium")
        self.assertEqual(self.ok("today").stdout, TODAY + "\n")

    def test_the_sampler_is_asked_to_flush_first(self):
        self.sampler()
        self.ok("today")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS, FLUSH])

    def test_a_sampler_that_is_not_ours_or_not_there_is_left_alone(self):
        self.ok("today")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS])
        self.sampler(dataDir="/somewhere/else/omawrapped")
        self.ok("today")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS, STATUS])

    def test_it_never_runs_git(self):
        repos = self.tmp / "repos"
        self.repo_with_commits(repos)
        self.record()
        self.stubs.replace_tool("git")
        # The trap works: a command that does look at repositories does start it.
        self.ok("stats", "--repos", repos)
        self.assertNotEqual(self.stubs.argv("git"), [])
        self.stubs.logs.joinpath("git.log").unlink()
        self.write(self.config / "omarchy" / "shell.json", json.dumps(
            {"version": 1, "bar": {"layout": {"right": [{"id": PLUGIN_ID, "repoDirs": str(repos)}]}}}))
        self.ok("today")
        self.ok("today", "--notify", **self.on_bus)
        self.assertEqual(self.stubs.argv("git"), [])

    def test_it_writes_nothing(self):
        self.ok("today", "--notify", **self.on_bus)
        self.assertFalse((self.data_home / "omawrapped").exists())
        self.assertFalse(self.pictures.exists())
        self.assert_nothing_started("omarchy-shell")


class TodayNotifyTests(OnTheBus, CliCase):
    def say(self, *args) -> list:
        """What the bus was sent by `today --notify`, so far."""
        self.ok("today", "--notify", *args)
        return self.sent()

    def test_the_headline_is_the_time_and_the_body_the_top_three_apps(self):
        self.busy_day()
        self.assertEqual(self.say(), [notification("Today: 3h 07m", TODAY_BODY)])

    def test_there_is_no_image_and_no_click_command(self):
        self.busy_day()
        self.assertEqual(self.say()[0]["hints"], {"urgency": 0, "omarchy-glyph": GLYPH})

    def test_fewer_than_three_apps(self):
        self.write_day(self.today, active_ms=5400000, apps_ms={"zed": 1800000, "com.mitchellh.ghostty": 3600000})
        self.assertEqual(self.say(), [notification("Today: 1h 30m", "Ghostty 1h 00m · Zed 30m")])
        self.write_day(self.today, active_ms=3600000, apps_ms={"zed": 3600000})
        self.assertEqual(self.say()[1:], [notification("Today: 1h 00m", "Zed 1h 00m")])

    def test_nothing_counted_yet_has_an_empty_body(self):
        self.assertEqual(self.say(), [notification("Today: nothing counted yet", "")])

    def test_less_than_a_minute_has_no_apps_either(self):
        self.write_day(self.today, active_ms=59999, apps_ms={"slack": 59999})
        self.assertEqual(self.say(), [notification("Today: nothing counted yet", "")])

    def test_a_paused_sampler_is_added_to_the_body(self):
        self.busy_day()
        self.sampler(paused=True)
        self.assertEqual(self.say(), [notification("Today: 3h 07m", TODAY_BODY + " · counting is paused")])

    def test_a_paused_sampler_without_apps_has_only_that_for_a_body(self):
        self.sampler(paused=True)
        self.assertEqual(self.say(), [notification("Today: nothing counted yet", "Counting is paused.")])

    def test_time_in_ignored_apps_alone_is_time_without_apps(self):
        self.write_day(self.today, active_ms=3600000, apps_ms={"slack": 3600000})
        self.write(self.config / "omarchy" / "shell.json", json.dumps(
            {"version": 1, "bar": {"layout": {"right": [{"id": PLUGIN_ID, "ignoreApps": "slack"}]}}}))
        self.assertEqual(self.say(), [notification("Today: 1h 00m", "")])
        self.sampler(paused=True)
        self.assertEqual(self.say()[1:], [notification("Today: 1h 00m", "Counting is paused.")])

    def test_the_output_is_printed_as_well(self):
        self.busy_day()
        self.assertEqual(self.ok("today", "--notify").stdout.splitlines(), [TODAY] + TODAY_ROWS)

    def test_without_notify_nothing_is_said(self):
        self.busy_day()
        self.ok("today")
        self.assertEqual(self.sent(), [])
        self.assert_nothing_started("omarchy-shell")

    def test_no_program_is_started_to_say_it_and_none_is_needed(self):
        self.busy_day()
        self.ok("today", "--notify")
        self.assertEqual(self.stubs.argv("omarchy-notification-send"), [])
        self.assertEqual(self.stubs.argv("notify-send"), [])
        self.stubs.remove("omarchy-notification-send", "notify-send")
        self.assertEqual(self.say()[1:], [notification("Today: 3h 07m", TODAY_BODY)])

    def test_the_success_is_the_exit_status_0(self):
        self.busy_day()
        self.assertEqual(self.run_cli("today", "--notify").returncode, 0)


class PausingTests:
    """What `pause` and `resume` have in common, tested for each of them. The two classes below say how they differ.

    The sampler is told to follow the setting by the stand-in: the pause between two looks is made short.
    """

    command = None       # the command under test
    word = None          # what it sets paused to, as omarchy-bar is given it
    paused = None        # the same as a status says it
    confirmed = None     # what it prints when the sampler has followed
    announced = None     # (headline, body) of the notification then
    not_running = None   # what it prints when there is no sampler to follow
    needs_bus = True

    BAR = ["set", PLUGIN_ID, "paused", None, "--json"]

    def run_it(self, *args, **extra):
        return self.run_cli(self.command, *args, **{**FAST, **extra})

    def following(self):
        """A sampler that has followed the setting."""
        self.sampler(paused=self.paused)

    def not_following(self):
        """A sampler that does what it was doing before."""
        self.sampler(paused=not self.paused)

    def bar_call(self) -> list:
        return [part if part is not None else self.word for part in self.BAR]

    def test_omarchy_is_asked_to_set_the_setting(self):
        self.following()
        self.assertEqual(self.run_it().returncode, 0)
        self.assertEqual(self.stubs.argv("omarchy-bar"), [self.bar_call()])

    def test_confirmed_on_the_first_look(self):
        self.following()
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, self.confirmed + "\n", ""))
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS])
        self.assert_nothing_started("omarchy-shell", "omarchy-bar")

    def test_confirmed_after_a_few_looks(self):
        self.samplers({"paused": not self.paused}, {"paused": not self.paused}, {"paused": self.paused})
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, self.confirmed + "\n", ""))
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS] * 3)

    def test_a_sampler_that_answers_late_is_a_sampler_that_followed(self):
        self.samplers(None, None, None, {"paused": self.paused})
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, self.confirmed + "\n", ""))
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS] * 4)

    def test_confirmed_on_the_last_look_and_not_after_it(self):
        self.samplers(*[{"paused": not self.paused}] * 9, {"paused": self.paused})
        self.assertEqual(self.run_it().returncode, 0)
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS] * 10)
        self.samplers(*[{"paused": not self.paused}] * 10, {"paused": self.paused})
        self.assertEqual(self.run_it().returncode, 1)

    def test_a_sampler_that_never_follows_is_reported_after_ten_looks(self):
        self.not_following()
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", NOT_FOLLOWED))
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS] * 10)
        self.assertEqual(len(self.stubs.argv("omarchy-bar")), 1)

    def test_the_pause_between_two_looks_follows_the_environment(self):
        self.not_following()
        started = time.monotonic()
        self.run_it(OMAWRAPPED_POLL_SECONDS="0")
        self.assertLess(time.monotonic() - started, 1.5)
        started = time.monotonic()
        self.run_it(OMAWRAPPED_POLL_SECONDS="0.1")
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    def test_without_a_sampler_the_setting_is_saved_and_that_is_said(self):
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, self.not_running + "\n", ""))
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS] * 10)
        self.assertEqual(self.stubs.argv("omarchy-bar"), [self.bar_call()])

    def test_garbage_from_the_shell_is_no_sampler(self):
        self.stubs.reply_to_status("this is not json")
        self.assertEqual(self.run_it().stdout, self.not_running + "\n")

    def test_a_sampler_that_records_elsewhere_is_not_ours_to_confirm(self):
        self.sampler(paused=self.paused, dataDir="/somewhere/else/omawrapped")
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", NOT_FOLLOWED))
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS] * 10)

    def test_the_sampler_is_only_asked_for_its_status(self):
        self.following()
        self.run_it()
        self.not_following()
        self.run_it()
        self.assertEqual(set(self.stubs.calls("omarchy-shell")), {STATUS})

    def test_notify_says_it_when_the_sampler_has_followed(self):
        self.following()
        result = self.run_it("--notify", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout), (0, self.confirmed + "\n"))
        self.assertEqual(self.sent(), [notification(*self.announced)])
        self.assert_nothing_started("omarchy-shell", "omarchy-bar")

    def test_notify_does_not_announce_when_the_sampler_has_not_followed_or_is_not_there(self):
        # Not followed: the command failed, and says so on the desktop instead (see below). Not there: it saved
        # the setting and has nothing to announce.
        self.not_following()
        self.assertEqual(self.run_it("--notify", **self.on_bus).returncode, 3)
        self.assertEqual(self.sent(), [notification("OmaWrapped", NOT_FOLLOWED.strip())])
        self.bus.clear()
        self.stubs.reply.unlink()
        self.assertEqual(self.run_it("--notify", **self.on_bus).returncode, 0)
        self.assertEqual(self.sent(), [])

    def test_a_sampler_that_is_not_the_one_for_this_folder_is_a_failure_said_on_the_desktop(self):
        self.sampler(paused=self.paused, dataDir="/somewhere/else/omawrapped")
        result = self.run_it("--notify", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", NOT_FOLLOWED))
        self.assertEqual(self.sent(), [notification("OmaWrapped", NOT_FOLLOWED.strip())])

    def test_omarchy_refusing_is_said_on_the_desktop_with_notify_and_only_then(self):
        self.following()
        self.stubs.fail("omarchy-bar", 3, reason="Unknown widget: %s\nTry `omarchy plugin list`." % PLUGIN_ID)
        said = "Omarchy did not accept the change: Unknown widget: %s. Is the widget enabled?" % PLUGIN_ID
        result = self.run_it(**self.on_bus)
        self.assertEqual((result.returncode, result.stderr), (1, said + "\n"))
        self.assertEqual(self.sent(), [])
        result = self.run_it("--notify", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", said + "\n"))
        self.assertEqual(self.sent(), [notification("OmaWrapped", said)])

    def test_a_missing_omarchy_bar_is_said_on_the_desktop_with_notify(self):
        self.following()
        self.stubs.remove("omarchy-bar")
        said = "omarchy-bar was not found, so the pause could not be %s. It is part of Omarchy 4." % (
            "saved" if self.word == "true" else "lifted")
        result = self.run_it("--notify", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", said + "\n"))
        self.assertEqual(self.sent(), [notification("OmaWrapped", said)])

    def test_a_service_that_refuses_a_failure_leaves_the_exit_status_alone(self):
        self.following()
        self.stubs.remove("omarchy-bar")
        self.bus.refuse(True)
        self.assertEqual(self.run_it("--notify", **self.on_bus).returncode, 1)
        self.assertEqual(self.sent(), [])

    def test_without_notify_nothing_is_said(self):
        self.following()
        self.run_it()
        self.assert_nothing_started("omarchy-shell", "omarchy-bar")

    def test_a_desktop_without_notifications_is_not_an_error(self):
        self.following()
        for name, address in (("none named", ""), ("a dead one", isolated_env(self.tmp)["DBUS_SESSION_BUS_ADDRESS"])):
            with self.subTest(bus=name):
                result = self.run_it("--notify", DBUS_SESSION_BUS_ADDRESS=address)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, self.confirmed + "\n", ""))
        self.bus.refuse(True)
        result = self.run_it("--notify", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout), (0, self.confirmed + "\n"))
        self.assertEqual(self.sent(), [])

    def test_without_omarchy_bar_it_says_so(self):
        self.following()
        self.stubs.remove("omarchy-bar")
        result = self.run_it("--notify")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "omarchy-bar was not found, so the pause could not be %s. "
                                        "It is part of Omarchy 4.\n" % ("saved" if self.word == "true" else "lifted"))
        # Nothing was changed, so nobody was asked or told anything.
        self.assert_nothing_started()

    def test_omarchy_refusing_gives_its_reason(self):
        self.following()
        self.stubs.fail("omarchy-bar", 3, reason="Unknown widget: %s\nTry `omarchy plugin list`." % PLUGIN_ID)
        result = self.run_it("--notify")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "Omarchy did not accept the change: Unknown widget: %s. "
                                        "Is the widget enabled?\n" % PLUGIN_ID)
        self.assertEqual(self.stubs.argv("omarchy-bar"), [self.bar_call()])
        # The change was refused: the sampler was not asked whether it followed, and nothing was announced.
        self.assert_nothing_started("omarchy-bar")

    def test_omarchys_own_name_and_full_stop_are_left_out_of_the_reason(self):
        self.stubs.fail("omarchy-bar", 1, reason="omarchy-bar: could not find widget %s." % PLUGIN_ID)
        self.assertEqual(self.run_it().stderr, "Omarchy did not accept the change: could not find widget %s. "
                                               "Is the widget enabled?\n" % PLUGIN_ID)

    def test_omarchy_refusing_without_a_reason(self):
        self.following()
        self.stubs.fail("omarchy-bar", 1, reason="")
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "Omarchy did not accept the change: no reason given. Is the widget enabled?\n")
        self.assert_nothing_started("omarchy-bar")

    def test_omarchy_refusing_with_a_blank_line_for_a_reason(self):
        self.stubs.fail("omarchy-bar", 1, reason="\n\n")
        self.assertIn("no reason given", self.run_it().stderr)

    def test_omarchy_that_cannot_be_started(self):
        self.following()
        broken = self.stubs.dir / "omarchy-bar"
        broken.write_text("#!/no/such/interpreter\n", encoding="utf-8")
        broken.chmod(0o755)
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "Omarchy did not accept the change: no reason given. Is the widget enabled?\n")
        self.assertNotIn("Traceback", result.stderr)

    def test_the_shell_is_not_asked_before_the_setting_is_saved(self):
        self.stubs.fail("omarchy-bar")
        self.run_it()
        self.assertEqual(self.stubs.calls("omarchy-shell"), [])

    def test_nothing_is_written_to_the_data_folder(self):
        self.following()
        self.run_it()
        self.assertFalse((self.data_home / "omawrapped").exists())


class PauseTests(PausingTests, CliCase):
    command = "pause"
    word = "true"
    paused = True
    confirmed = "Counting is paused. `omawrapped resume`, or the widget's menu, starts it again."
    announced = ("Counting paused", "Resume it from the widget's menu.")
    not_running = "Saved. The sampler is not running; it will start paused."

    def test_a_status_without_the_paused_key_is_not_a_pause(self):
        self.sampler()
        self.assertEqual(self.run_it().returncode, 1)

    def test_nine_looks_a_fifth_of_a_second_apart_without_the_variable(self):
        self.sampler(paused=False)
        started = time.monotonic()
        self.assertEqual(self.run_cli("pause").returncode, 1)
        self.assertGreaterEqual(time.monotonic() - started, 1.8)
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS] * 10)


class ResumeTests(PausingTests, CliCase):
    command = "resume"
    word = "false"
    paused = False
    confirmed = "Counting again."
    announced = ("Counting again", "")
    not_running = "Saved. The sampler is not running; it will count when it starts."

    def test_a_status_without_the_paused_key_is_a_sampler_that_counts(self):
        self.sampler()
        result = self.run_it()
        self.assertEqual((result.returncode, result.stdout), (0, "Counting again.\n"))
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS])


class MenuTests(CliCase):
    needs_bus = True

    def assert_only_asked(self, *names):
        """Nothing was started but the menu, the sampler (asked its status once, to build the menu) and the named."""
        self.assert_nothing_started("omarchy-menu-select", "omarchy-shell", *names)
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS])

    def test_the_six_options_reach_the_menu_in_order_each_with_its_glyph_and_a_tab(self):
        self.ok("menu")
        self.assertEqual(self.stubs.argv("omarchy-menu-select"), [MENU + [PAUSE]])
        self.assertEqual(len(self.stubs.argv("omarchy-menu-select")[0]), 7)
        self.assertEqual(self.stubs.argv("omarchy-menu-select")[0][0], "OmaWrapped")
        for option in MENU[1:] + [PAUSE]:
            glyph, tab, label = option.partition("\t")
            self.assertEqual((len(glyph), tab), (1, "\t"), option)

    def test_the_last_option_is_pause_without_a_sampler_and_with_one_that_counts(self):
        cases = [("no sampler", None), ("one that counts", {}), ("one that says it is not paused", {"paused": False}),
                 ("one that is away", {"counting": False}), ("one that is locked", {"counting": False, "locked": True}),
                 ("one that records elsewhere", {"paused": True, "dataDir": "/somewhere/else/omawrapped"})]
        for name, fields in cases:
            with self.subTest(sampler=name):
                if fields is not None:
                    self.sampler(**fields)
                self.ok("menu")
                self.assertEqual(self.stubs.argv("omarchy-menu-select")[-1], MENU + [PAUSE])

    def test_the_last_option_is_resume_when_our_sampler_is_paused(self):
        self.sampler(paused=True)
        self.ok("menu")
        self.assertEqual(self.stubs.argv("omarchy-menu-select"), [MENU + [RESUME]])
        self.assert_only_asked()

    def test_a_dismissed_menu_does_nothing_and_succeeds(self):
        self.record()
        self.make_card()
        result = self.run_cli("menu")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assert_only_asked()

    def test_a_missing_menu_is_an_error(self):
        self.stubs.remove("omarchy-menu-select")
        result = self.run_cli("menu")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, NO_MENU + "\n")
        self.assert_nothing_started("omarchy-shell")

    def test_a_menu_that_breaks_is_an_error(self):
        self.stubs.fail("omarchy-menu-select", 2)
        result = self.run_cli("menu")
        self.assertEqual(result.returncode, 1)
        self.assertIn("omarchy-menu-select", result.stderr)
        self.assert_only_asked()

    def test_an_answer_that_is_not_an_option_does_nothing(self):
        self.make_card()
        self.stubs.choose_in_menu("Delete everything")
        result = self.run_cli("menu")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Delete everything", result.stderr)
        self.assert_only_asked()

    def test_the_option_that_is_not_on_offer_cannot_be_chosen(self):
        # Pause is offered to a sampler that counts, resume to one that does not: the other is not an option.
        self.sampler(paused=True)
        self.stubs.choose_in_menu("Pause counting")
        result = self.run_cli("menu")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Pause counting", result.stderr)
        self.assertEqual(self.stubs.argv("omarchy-bar"), [])

    def test_today_so_far_does_what_today_notify_does(self):
        self.busy_day()
        self.stubs.choose_in_menu("Today so far")
        result = self.ok("menu", **self.on_bus)
        self.assertEqual(result.stdout.splitlines(), [TODAY] + TODAY_ROWS)
        self.assertEqual(self.sent(), [notification("Today: 3h 07m", TODAY_BODY)])
        # The menu asked the sampler for its status, and `today` asked again.
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS, STATUS])
        self.assert_nothing_started("omarchy-menu-select", "omarchy-shell")

    def test_today_so_far_without_anything_counted(self):
        self.stubs.choose_in_menu("Today so far")
        result = self.ok("menu", **self.on_bus)
        self.assertEqual(result.stdout, "Today  nothing counted yet\n")
        self.assertEqual(self.sent(), [notification("Today: nothing counted yet", "")])

    def test_today_so_far_asks_the_sampler_to_flush_and_tells_when_it_is_paused(self):
        self.busy_day()
        self.sampler(paused=True)
        self.stubs.choose_in_menu("Today so far")
        # A paused sampler is offered resume, but today is shown all the same.
        result = self.ok("menu", **self.on_bus)
        self.assertEqual(result.stdout.splitlines(), [TODAY + " (paused)"] + TODAY_ROWS)
        self.assertEqual(self.sent(), [notification("Today: 3h 07m", TODAY_BODY + " \u00b7 counting is paused")])
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS, STATUS, FLUSH])

    def test_pause_counting_does_what_pause_notify_does(self):
        # The sampler counts when the menu opens and has paused by the time it is asked again.
        self.samplers({}, {"paused": True})
        self.stubs.choose_in_menu("Pause counting")
        result = self.ok("menu", **FAST, **self.on_bus)
        self.assertEqual(result.stdout,
                         "Counting is paused. `omawrapped resume`, or the widget's menu, starts it again.\n")
        self.assertEqual(self.stubs.argv("omarchy-bar"), [["set", PLUGIN_ID, "paused", "true", "--json"]])
        self.assertEqual(self.sent(), [notification("Counting paused", "Resume it from the widget's menu.")])
        self.assert_nothing_started("omarchy-menu-select", "omarchy-shell", "omarchy-bar")

    def test_pause_counting_without_a_sampler_saves_the_setting_and_says_nothing_on_the_desktop(self):
        self.stubs.choose_in_menu("Pause counting")
        result = self.ok("menu", **FAST, **self.on_bus)
        self.assertEqual(result.stdout, "Saved. The sampler is not running; it will start paused.\n")
        self.assertEqual(self.stubs.argv("omarchy-bar"), [["set", PLUGIN_ID, "paused", "true", "--json"]])
        self.assertEqual(self.sent(), [])

    def test_pause_counting_that_the_sampler_does_not_follow_fails_as_pause_does(self):
        self.sampler()
        self.stubs.choose_in_menu("Pause counting")
        result = self.run_cli("menu", **FAST)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", NOT_FOLLOWED))
        self.assertEqual(self.sent(), [])

    def test_pause_counting_without_omarchy_bar_fails_as_pause_does(self):
        self.stubs.remove("omarchy-bar")
        self.stubs.choose_in_menu("Pause counting")
        result = self.run_cli("menu")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "omarchy-bar was not found, so the pause could not be saved. "
                                        "It is part of Omarchy 4.\n")

    def test_resume_counting_does_what_resume_notify_does(self):
        self.samplers({"paused": True}, {"paused": False})
        self.stubs.choose_in_menu("Resume counting")
        result = self.ok("menu", **FAST, **self.on_bus)
        self.assertEqual(result.stdout, "Counting again.\n")
        self.assertEqual(self.stubs.argv("omarchy-bar"), [["set", PLUGIN_ID, "paused", "false", "--json"]])
        self.assertEqual(self.sent(), [notification("Counting again", "")])
        self.assert_nothing_started("omarchy-menu-select", "omarchy-shell", "omarchy-bar")

    def test_resume_counting_that_the_sampler_does_not_follow_fails_as_resume_does(self):
        self.sampler(paused=True)
        self.stubs.choose_in_menu("Resume counting")
        result = self.run_cli("menu", **FAST)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", NOT_FOLLOWED))
        self.assertEqual(self.sent(), [])

    def test_the_other_entries_leave_the_setting_alone(self):
        self.record()
        self.make_card()
        for label in ("Today so far", "Copy card", "Show in folder"):
            self.stubs.choose_in_menu(label)
            self.ok("menu", **self.on_bus)
        self.assertEqual(self.stubs.argv("omarchy-bar"), [])

    def test_copy_card_copies_the_newest_card_and_says_so(self):
        self.make_card("omawrapped-2026-10-09.png", mtime=1000)
        newest = self.make_card("omawrapped-2026-10-02.png", mtime=3000)
        self.stubs.choose_in_menu("Copy card")
        result = self.ok("menu", **self.on_bus)
        self.assertEqual(result.stdout, str(newest) + "\n")
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "image/png"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), newest.read_bytes())
        self.assertEqual(self.sent(), [notification(
            "Card copied", "omawrapped-2026-10-02.png is on the clipboard. Paste it anywhere.", image=newest)])
        self.assert_only_asked("wl-copy")

    def test_copy_card_without_a_card_fails_as_copy_does(self):
        self.stubs.choose_in_menu("Copy card")
        result = self.run_cli("menu")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", NO_CARD + "\n"))
        self.assert_only_asked()

    def test_copy_card_with_a_failing_wl_copy_fails_as_copy_does(self):
        self.make_card()
        self.stubs.fail("wl-copy")
        self.stubs.choose_in_menu("Copy card")
        result = self.run_cli("menu")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", COPY_FAILED + "\n"))
        self.assert_only_asked("wl-copy")

    def test_show_in_folder_shows_the_newest_card(self):
        self.make_card("omawrapped-2026-10-09.png", mtime=1000)
        newest = self.make_card("omawrapped-2026-10-02.png", mtime=3000)
        self.stubs.choose_in_menu("Show in folder")
        result = self.ok("menu")
        self.assertEqual(result.stdout, str(newest) + "\n")
        self.assertEqual(self.stubs.wait_for_argv("uwsm-app"), [["--", "nautilus", "--select", str(newest)]])
        self.assert_only_asked("uwsm-app")

    def test_show_in_folder_without_a_card_or_a_file_manager_fails_as_show_does(self):
        self.stubs.choose_in_menu("Show in folder")
        result = self.run_cli("menu")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", NO_CARD + "\n"))
        card = self.make_card()
        self.stubs.remove("nautilus", "xdg-open")
        result = self.run_cli("menu")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "No file manager was found to show the card in. It is at %s.\n" % card)

    def card_choice(self, label, suffix):
        """The menu draws a card: it is saved, opened, its image copied, and the desktop is told."""
        self.record()
        self.stubs.choose_in_menu(label)
        expected = self.pictures / ("omawrapped-%s%s.png" % (self.today.isoformat(), suffix))
        result = self.ok("menu", **self.on_bus)
        self.assertEqual(result.stdout, str(expected) + "\n")
        self.assertEqual(os.listdir(self.pictures), [expected.name])
        data = expected.read_bytes()
        self.assertEqual(data[:8], PNG_SIGNATURE)
        self.assertEqual(self.stubs.wait_for_argv("xdg-open"), [[str(expected)]])
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "image/png"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), data)
        self.assertEqual(self.sent(), [notification(
            "Card copied", "Paste it into a post. " + CLICK_HINT, image=expected,
            click=[str(LAUNCHER), "show", str(expected)])])
        self.assertIn("Saved %s (image copied)" % expected, result.stderr)
        self.assert_nothing_started("omarchy-menu-select", "omarchy-shell", "wl-copy", "xdg-open")
        return expected

    @needs_renderer
    def test_the_card_of_the_last_7_days(self):
        self.card_choice("Card of the last 7 days", "")

    @needs_renderer
    def test_the_card_of_the_last_30_days(self):
        self.card_choice("Card of the last 30 days", "-month")

    @needs_renderer
    def test_the_two_cards_cover_the_periods_they_name(self):
        self.record()
        self.stubs.choose_in_menu("Card of the last 7 days")
        self.ok("menu")
        self.stubs.choose_in_menu("Card of the last 30 days")
        self.ok("menu")
        self.assertEqual(sorted(os.listdir(self.pictures)), [
            "omawrapped-%s-month.png" % self.today.isoformat(), "omawrapped-%s.png" % self.today.isoformat()])

    def test_a_card_without_data_fails_as_card_does(self):
        for label in ("Card of the last 7 days", "Card of the last 30 days"):
            with self.subTest(label=label):
                self.stubs.choose_in_menu(label)
                result = self.run_cli("menu")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("Nothing was recorded", result.stderr)
                self.assertFalse(self.pictures.exists())
                self.assert_nothing_started("omarchy-menu-select", "omarchy-shell")

    @needs_renderer
    def test_the_menu_asks_the_sampler_to_flush_before_it_draws(self):
        self.record()
        self.sampler()
        self.stubs.choose_in_menu("Card of the last 7 days")
        self.ok("menu")
        # The menu asked once to build its last entry, the card once more to find out whether to flush.
        self.assertEqual(self.stubs.calls("omarchy-shell"), [STATUS, STATUS, FLUSH])

    @needs_renderer
    def test_a_viewer_that_is_not_there_is_a_warning_not_a_failure(self):
        self.record()
        self.stubs.remove("xdg-open")
        self.stubs.choose_in_menu("Card of the last 7 days")
        result = self.ok("menu", **self.on_bus)
        self.assertIn(NO_XDG_OPEN, result.stderr)
        self.assertEqual(len(self.sent()), 1)


class StatusTests(CliCase):
    needs_bus = True

    def test_names_the_data_folder_and_the_version(self):
        result = self.ok("status")
        self.assertIn("OmaWrapped " + VERSION, result.stdout)
        self.assertIn(str(self.data_home / "omawrapped"), result.stdout)
        self.assertIn("nothing recorded yet", result.stdout)

    def test_describes_what_is_stored(self):
        self.record(5)
        out = self.ok("status").stdout
        first = (self.today - timedelta(days=4)).isoformat()
        self.assertIn("%s: 5 days, %s to %s" % (self.data_home / "omawrapped", first, self.today.isoformat()), out)

    def test_one_day_is_not_plural(self):
        self.record(1)
        self.assertIn(": 1 day, ", self.ok("status").stdout)

    def test_the_sampler_is_not_running_when_the_shell_does_not_answer(self):
        out = self.ok("status").stdout
        self.assertIn("Sampler   not running", out)
        self.assertEqual(self.stubs.calls("omarchy-shell"), [PLUGIN_ID + " status"])

    def test_the_sampler_state_comes_from_the_shell(self):
        cases = [
            ({"counting": True, "locked": False, "idleSeconds": 90, "todayMs": 3600000},
             "running, counting; away after 90s without input; today 1h 00m"),
            ({"counting": False, "locked": True, "idleSeconds": 60, "todayMs": 0}, "running, paused (session locked)"),
            ({"counting": False, "locked": False, "idleSeconds": 60, "todayMs": 120000}, "running, paused (away)"),
            ({"countKeptAwake": False, "idleSeconds": 60},
             "running, counting; away after 60s without input (a video or call does not count); today 0m"),
        ]
        for reply, expected in cases:
            with self.subTest(reply=reply):
                self.sampler(**reply)
                self.assertIn("Sampler   " + expected, self.ok("status").stdout)

    def test_a_paused_sampler_is_paused_by_you_and_that_beats_the_other_two_reasons(self):
        paused = ("Sampler   running, paused by you (`omawrapped resume` starts it again); "
                  "away after 120s without input; today 0m")
        for fields in ({"counting": False}, {"counting": True}, {"counting": False, "locked": True},
                       {"counting": True, "locked": True}, {}):
            with self.subTest(fields=fields):
                self.sampler(paused=True, **fields)
                self.assertIn(paused, self.ok("status").stdout.splitlines())

    def test_a_sampler_that_is_not_paused_says_what_it_did_before(self):
        self.sampler(paused=False, counting=False, locked=True)
        self.assertIn("Sampler   running, paused (session locked); away after 120s without input; today 0m",
                      self.ok("status").stdout.splitlines())
        self.sampler(paused=False)
        self.assertIn("Sampler   running, counting; away after 120s without input; today 0m",
                      self.ok("status").stdout.splitlines())

    def test_the_pause_of_a_sampler_that_records_elsewhere_is_not_reported(self):
        self.sampler(paused=True, dataDir="/somewhere/else/omawrapped")
        out = self.ok("status").stdout
        self.assertIn("Sampler   running, but recording to /somewhere/else/omawrapped, not the folder above", out)
        self.assertNotIn("paused by you", out)

    def test_a_sampler_recording_elsewhere_is_named_as_such(self):
        self.sampler(dataDir="/somewhere/else/omawrapped")
        self.assertIn("Sampler   running, but recording to /somewhere/else/omawrapped, not the folder above",
                      self.ok("status").stdout)

    def test_says_what_is_ignored(self):
        self.assertIn("Ignored   no apps", self.ok("status").stdout)
        self.sampler(ignoreApps=["steam", "org.keepassxc.KeePassXC"])
        self.assertIn("Ignored   steam, org.keepassxc.KeePassXC", self.ok("status").stdout)

    def test_without_a_sampler_what_is_ignored_comes_from_the_settings(self):
        self.write(self.config / "omarchy" / "shell.json", json.dumps({"version": 1, "bar": {"layout": {"center": [
            {"id": PLUGIN_ID, "ignoreApps": "steam, signal"}]}}}))
        self.assertIn("Ignored   steam, signal", self.ok("status").stdout)

    def test_garbage_from_the_shell_means_not_running(self):
        self.stubs.reply_to_status("this is not json")
        self.assertIn("Sampler   not running", self.ok("status").stdout)

    def test_lists_the_helpers(self):
        out = self.ok("status").stdout
        self.assertRegex(out, r"(?m)^Renderer\s+rsvg-convert (found|MISSING)")
        self.assertRegex(out, r"(?m)^Clipboard\s+wl-copy found")

    def status_lines(self, **kwargs) -> list:
        return self.ok("status", **kwargs).stdout.splitlines()

    def test_the_new_lines_follow_the_clipboard_line_in_this_order(self):
        lines = self.status_lines()
        first = next(number for number, line in enumerate(lines) if line.startswith("Clipboard"))
        self.assertEqual(lines[first:first + 5], [
            "Clipboard wl-copy found", "Notify    over the session bus",
            "Menu      omarchy-menu-select found", "Pause     omarchy-bar found", "Files     nautilus found"])

    def test_notifications_go_over_the_session_bus(self):
        self.assertIn("Notify    over the session bus", self.status_lines(**self.on_bus))
        self.assertIn("Notify    over the session bus", self.status_lines())

    def test_without_a_session_bus_the_line_says_so(self):
        self.assertIn("Notify    MISSING (there is no session bus; results are still printed)",
                      self.status_lines(DBUS_SESSION_BUS_ADDRESS=""))

    def test_without_python_gobject_the_line_says_so(self):
        self.assertIn("Notify    MISSING (python-gobject is not installed; results are still printed)",
                      self.status_lines(**self.without_python_gobject()))
        # It is the first thing a notification needs, so it is the one said when the bus is missing as well.
        self.assertIn("Notify    MISSING (python-gobject is not installed; results are still printed)",
                      self.status_lines(DBUS_SESSION_BUS_ADDRESS="", **self.without_python_gobject()))

    def test_the_programs_that_used_to_send_notifications_have_no_say_in_it(self):
        self.stubs.remove("omarchy-notification-send", "notify-send")
        self.assertIn("Notify    over the session bus", self.status_lines())
        self.assertIn("Notify    MISSING (there is no session bus; results are still printed)",
                      self.status_lines(DBUS_SESSION_BUS_ADDRESS=""))

    def test_looking_at_the_notifications_sends_none(self):
        self.record()
        self.ok("status", **self.on_bus)
        self.assertEqual(self.sent(), [])
        self.assert_nothing_started("omarchy-shell")

    def test_the_menu_tool(self):
        self.assertIn("Menu      omarchy-menu-select found", self.status_lines())
        self.stubs.remove("omarchy-menu-select")
        self.assertIn("Menu      MISSING (the widget's middle-click menu needs Omarchy's menu)", self.status_lines())

    def test_the_pause_tool(self):
        self.assertIn("Pause     omarchy-bar found", self.status_lines())
        self.stubs.remove("omarchy-bar")
        self.assertIn("Pause     MISSING (pausing needs Omarchy's bar command)", self.status_lines())

    def test_the_file_manager(self):
        self.assertIn("Files     nautilus found", self.status_lines())
        # uwsm-app only decides how nautilus is started.
        self.stubs.remove("uwsm-app")
        self.assertIn("Files     nautilus found", self.status_lines())
        self.stubs.remove("nautilus")
        self.assertIn("Files     xdg-open only (opens the folder without selecting the card)", self.status_lines())
        self.stubs.remove("xdg-open")
        self.assertIn("Files     MISSING", self.status_lines())

    def test_nautilus_is_found_without_xdg_open(self):
        self.stubs.remove("xdg-open")
        self.assertIn("Files     nautilus found", self.status_lines())

    def test_a_machine_with_none_of_it_says_so_on_every_line(self):
        self.stubs.remove(*[name for name in STUBBED if name != "omarchy-shell"])
        lines = self.status_lines(DBUS_SESSION_BUS_ADDRESS="")
        for expected in ("Clipboard wl-copy MISSING (package wl-clipboard)",
                         "Notify    MISSING (there is no session bus; results are still printed)",
                         "Menu      MISSING (the widget's middle-click menu needs Omarchy's menu)",
                         "Pause     MISSING (pausing needs Omarchy's bar command)",
                         "Files     MISSING"):
            self.assertIn(expected, lines)

    def test_looking_starts_none_of_them(self):
        self.ok("status")
        self.assert_nothing_started("omarchy-shell")

    def test_counts_repositories(self):
        repos = self.tmp / "repos"
        self.repo_with_commits(repos)
        # The folders come from the widget's setting, the way a user sets them.
        self.write(self.config / "omarchy" / "shell.json", json.dumps({"version": 1, "bar": {"layout": {"right": [
            {"id": PLUGIN_ID, "repoDirs": str(repos)}]}}}))
        self.assertRegex(self.ok("status").stdout, r"(?m)^Git\s.*\b1 repository under " + re.escape(str(repos)))
        init_repo(repos / "second")
        self.assertRegex(self.ok("status").stdout, r"(?m)^Git\s.*\b2 repositories under ")

    def test_says_whether_git_is_there(self):
        self.assertRegex(self.ok("status").stdout, r"(?m)^Git\s+found")
        # A path with the stand-ins alone has no git, no rsvg-convert and no fc-match.
        out = self.ok("status", PATH=str(self.stubs.dir)).stdout
        self.assertRegex(out, r"(?m)^Git\s+MISSING")
        self.assertRegex(out, r"(?m)^Renderer\s+rsvg-convert MISSING")

    def test_default_repository_folders(self):
        self.assertIn("0 repositories under ~/projects, ~/code", self.ok("status").stdout)

    def test_without_a_repository_the_git_line_says_where_the_folders_are_set(self):
        self.assertIn("Git       found; 0 repositories under ~/projects, ~/code "
                      "(the widget's repoDirs setting says where to look)", self.ok("status").stdout.splitlines())
        # Without git it is still the folders that are wrong, or the git that is missing: both are said.
        self.assertIn("Git       MISSING (package git); 0 repositories under ~/projects, ~/code "
                      "(the widget's repoDirs setting says where to look)",
                      self.ok("status", PATH=str(self.stubs.dir)).stdout.splitlines())

    def test_with_repositories_the_git_line_has_no_hint(self):
        repos = self.tmp / "repos"
        init_repo(repos / "first")
        self.write(self.config / "omarchy" / "shell.json", json.dumps({"version": 1, "bar": {"layout": {"right": [
            {"id": PLUGIN_ID, "repoDirs": str(repos)}]}}}))
        self.assertIn("Git       found; 1 repository under %s" % repos, self.ok("status").stdout.splitlines())
        init_repo(repos / "second")
        self.assertIn("Git       found; 2 repositories under %s" % repos, self.ok("status").stdout.splitlines())
        self.assertNotIn("repoDirs", self.ok("status").stdout)

    def test_reading_the_status_writes_nothing(self):
        self.ok("status")
        self.assertFalse((self.data_home / "omawrapped").exists())


class ResetTests(CliCase):
    def setUp(self):
        super().setUp()
        self.record(12)
        self.card = self.write(self.pictures / "omawrapped-2026-01-01.png", "a card I made")

    def assert_nothing_deleted(self):
        self.assertEqual(len(list(self.days.glob("*.json"))), 12)
        self.assertEqual(self.card.read_text(encoding="utf-8"), "a card I made")

    def test_without_a_terminal_and_without_yes_nothing_is_deleted(self):
        result = self.run_cli("reset")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to delete", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assert_nothing_deleted()
        self.assertEqual(self.stubs.calls("omarchy-shell"), [])

    def test_yes_deletes_the_day_files_and_says_how_many(self):
        result = self.ok("reset", "--yes")
        self.assertIn("Deleted 12 day files.", result.stdout)
        self.assertFalse((self.data_home / "omawrapped").exists())
        self.assertEqual(self.card.read_text(encoding="utf-8"), "a card I made")
        self.assertEqual(os.listdir(self.pictures), [self.card.name])

    def test_yes_asks_the_sampler_to_discard_what_it_holds(self):
        self.sampler()
        result = self.ok("reset", "--yes")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [PLUGIN_ID + " status", PLUGIN_ID + " discard"])
        self.assertEqual(result.stderr, "")

    def test_without_a_sampler_there_is_nothing_to_discard(self):
        result = self.ok("reset", "--yes")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [PLUGIN_ID + " status"])
        self.assertEqual(result.stderr, "")

    def test_a_sampler_that_does_not_answer_is_reported(self):
        self.sampler()
        self.stubs.stop_answering()
        result = self.ok("reset", "--yes")
        self.assertIn("The sampler did not answer", result.stderr)
        self.assertIn("Deleted 12 day files.", result.stdout)

    def test_the_short_flag(self):
        self.assertIn("Deleted 12 day files.", self.ok("reset", "-y").stdout)

    def test_one_file_is_not_plural(self):
        for path in sorted(self.days.glob("*.json"))[1:]:
            path.unlink()
        self.assertIn("Deleted 1 day file.", self.ok("reset", "--yes").stdout)

    def test_nothing_to_delete(self):
        self.ok("reset", "--yes")
        self.assertIn("Deleted 0 day files.", self.ok("reset", "--yes").stdout)

    def test_leftovers_of_interrupted_writes_go_too(self):
        self.write(self.days / ".2026-03-03.json.Ab12Cd", "half")
        self.ok("reset", "--yes")
        self.assertFalse((self.data_home / "omawrapped").exists())

    def test_stats_after_a_reset_find_nothing(self):
        self.ok("reset", "--yes")
        self.assertEqual(self.stats()["screen_time_ms"], 0)

    def run_on_a_terminal(self, answer: bytes):
        """Runs `reset` with stdin on a pseudo-terminal, which types `answer`."""
        try:
            master, slave = pty.openpty()
        except OSError as error:
            self.skipTest("no pseudo-terminal is available: %s" % error)
        try:
            process = subprocess.Popen([str(LAUNCHER), "reset"], stdin=slave, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, env=self.environment(), cwd=str(self.work))
        finally:
            os.close(slave)
        try:
            os.write(master, answer)
            out, err = process.communicate(timeout=30)
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.communicate()
        return process.returncode, out, err

    def test_on_a_terminal_it_asks_and_no_deletes_nothing(self):
        for answer in (b"n\n", b"\n", b"maybe\n"):
            with self.subTest(answer=answer):
                code, out, err = self.run_on_a_terminal(answer)
                self.assertEqual(code, 1, err)
                self.assertIn("Delete 12 days of recorded activity in %s" % (self.data_home / "omawrapped"), out)
                self.assertIn("Nothing was deleted", err)
                self.assert_nothing_deleted()
                self.assertEqual(self.stubs.calls("omarchy-shell"), [])

    def test_on_a_terminal_yes_deletes(self):
        for answer in (b"y\n", b"YES\n"):
            with self.subTest(answer=answer):
                self.record(12)
                code, out, err = self.run_on_a_terminal(answer)
                self.assertEqual(code, 0, err)
                self.assertIn("Deleted 12 day files.", out)
                self.assertFalse((self.data_home / "omawrapped").exists())
        self.assertEqual(self.card.read_text(encoding="utf-8"), "a card I made")


# ---- The fixes for what a marketplace reviewer found ----
#
# Private activity data (the apps used and for how long, the e-mail the commits are made with, the folders the
# projects are in) was given to other programs as arguments, and the arguments of a program can be read by every
# user of the machine, in the process list. The groups below are one per fix.

class ReviewerFindingTests(CliCase):
    """Group 1: what is in the notification of `today` is not in any argument of any program."""

    needs_bus = True
    APPS = ["Ghostty", "Chromium", "Zed", "Slack", "Obsidian", "Signal"]
    IDS = ["com.mitchellh.ghostty", "chromium", "zed", "slack", "obsidian", "signal"]
    TIMES = ["3h 07m", "1h 20m", "52m", "31m", "14m", "6m", "4m"]

    def setUp(self):
        super().setUp()
        self.busy_day()
        self.sampler()
        for tool in ("git", "rsvg-convert", "fc-match"):
            self.stubs.spy(tool)

    def started(self) -> dict:
        return self.stubs.everything()

    def assert_nothing_private_was_an_argument(self):
        started = self.started()
        self.assertEqual(leaks(started, self.APPS + self.IDS + self.TIMES, self.tmp), [])
        # The programs that used to be given the notification as arguments were not started at all.
        self.assertNotIn("omarchy-notification-send", started)
        self.assertNotIn("notify-send", started)
        self.assertEqual(self.stubs.argv("omarchy-notification-send"), [])
        self.assertEqual(self.stubs.argv("notify-send"), [])

    def test_today_so_far_in_the_menu_says_it_over_the_bus_and_starts_no_program_with_it(self):
        self.stubs.choose_in_menu("Today so far")
        result = self.ok("menu", **self.on_bus)
        self.assertEqual(result.stdout.splitlines(), [TODAY] + TODAY_ROWS)
        self.assertEqual(self.sent(), [notification("Today: 3h 07m", TODAY_BODY)])
        self.assertEqual(self.sent()[0]["summary"], "Today: " + clock(11220000))
        for app in ("Ghostty", "Chromium", "Zed"):
            self.assertIn(app, self.sent()[0]["body"])
        # The programs that were started: the menu, and the sampler asked for its status and to flush.
        self.assertEqual(sorted(self.started()), ["omarchy-menu-select", "omarchy-shell"])
        self.assert_nothing_private_was_an_argument()

    def test_today_notify_says_it_over_the_bus_and_starts_no_program_with_it(self):
        result = self.ok("today", "--notify", **self.on_bus)
        self.assertEqual(result.stdout.splitlines(), [TODAY] + TODAY_ROWS)
        self.assertEqual(self.sent(), [notification("Today: 3h 07m", TODAY_BODY)])
        self.assertEqual(sorted(self.started()), ["omarchy-shell"])
        self.assert_nothing_private_was_an_argument()

    def test_the_check_would_have_caught_the_way_it_used_to_be_done(self):
        # Without this a check that cannot fail would pass: what the notification program used to be given.
        self.ok("today", "--notify", **self.on_bus)
        subprocess.run([str(self.stubs.dir / "omarchy-notification-send"), "--app-name", "OmaWrapped", "-g", GLYPH,
                        "Today: 3h 07m", TODAY_BODY], check=True, env={})
        # The guard of every test is not to see this one.
        self.addCleanup((self.stubs.logs / "omarchy-notification-send.log").unlink)
        found = leaks(self.started(), self.APPS + self.IDS + self.TIMES, self.tmp)
        self.assertEqual({program for program, _, _ in found}, {"omarchy-notification-send"})
        self.assertIn(("omarchy-notification-send", "Today: 3h 07m", "a time"), found)
        self.assertIn(("omarchy-notification-send", TODAY_BODY, "Ghostty"), found)
        with self.assertRaises(AssertionError):
            self.assert_nothing_private_was_an_argument()

    def test_a_machine_without_a_bus_gets_the_numbers_on_stdout_and_nothing_else_started(self):
        result = self.run_cli("today", "--notify")
        self.assertEqual(result.stdout.splitlines(), [TODAY] + TODAY_ROWS)
        self.assertEqual(sorted(self.started()), ["omarchy-shell"])
        self.assert_nothing_private_was_an_argument()


class ArgumentAuditTests(CliCase):
    """Group 2: over every command, what any program is started with holds nothing the user did."""

    needs_bus = True
    MENU_CHOICES = ("Today so far", "Card of the last 7 days", "Card of the last 30 days", "Copy card",
                    "Show in folder", "Pause counting")

    def setUp(self):
        super().setUp()
        self.record()
        self.sampler()
        self.repos = self.tmp / "repos"
        self.secret = init_repo(self.repos / "secret-client")
        for days in (1, 2, 3):
            commit(self.secret, self.local(self.today - timedelta(days=days)))
        # The widget's setting says where the projects are, the way a user sets it.
        self.write(self.config / "omarchy" / "shell.json", json.dumps(
            {"version": 1, "bar": {"layout": {"right": [{"id": PLUGIN_ID, "repoDirs": str(self.repos)}]}}}))
        for tool in ("git", "rsvg-convert", "fc-match"):
            self.stubs.spy(tool)

    def private_words(self) -> list:
        """The apps of the sample data by name and by id, the e-mail, the project's folder, the data folder."""
        names = system.AppNames([])
        apps = {app for raw in self.recorded().values() for app in raw["apps_ms"]}
        return sorted({names.name(app) for app in apps} | apps | {
            ME, OTHER, "secret-client", str(self.data_home), "example.invalid"})

    def run_everything(self) -> None:
        """Each command that starts a program, and each choice of the menu, once, with the bus there."""
        env = {**FAST, **self.on_bus}
        self.ok("card", "--week", "--open", "--copy", "image", "--notify", **env)
        self.ok("card", "--month", "--copy", "path", **env)
        self.ok("card", "-o", self.tmp / "x.svg", **env)
        self.ok("copy", "--notify", **env)
        self.ok("show", **env)
        self.ok("today", "--notify", **env)
        self.sampler(paused=True)
        self.ok("pause", "--notify", **env)
        self.sampler()
        self.ok("resume", "--notify", **env)
        self.ok("stats", **env)
        self.ok("stats", "--json", **env)
        self.ok("status", **env)
        for label in self.MENU_CHOICES:
            self.stubs.choose_in_menu(label)
            if label == "Pause counting":
                # The menu opens on a sampler that counts, and finds it paused when it looks again.
                self.samplers({}, {"paused": True})
            else:
                self.sampler()
            self.ok("menu", **env)
        # The sixth entry is resume when the sampler is paused.
        self.stubs.choose_in_menu("Resume counting")
        self.samplers({"paused": True}, {"paused": False})
        self.ok("menu", **env)
        # xdg-open and nautilus are started detached: they may log after the command is done.
        self.wait_for_calls("xdg-open", 3)
        self.wait_for_calls("uwsm-app", 2)

    def wait_for_calls(self, name: str, count: int) -> None:
        deadline = time.monotonic() + 5
        while len(self.stubs.argv(name)) < count and time.monotonic() < deadline:
            time.sleep(0.05)

    def test_no_argument_of_any_program_holds_anything_private(self):
        self.run_everything()
        started = self.stubs.everything()
        # The audit looks at something: every program that can be started with an argument was.
        for name in ("git", "rsvg-convert", "fc-match", "wl-copy", "xdg-open", "uwsm-app", "omarchy-shell",
                     "omarchy-bar", "omarchy-menu-select"):
            self.assertTrue(started.get(name), "%s was not started, so there is nothing to look at" % name)
        self.assertEqual(sorted(started), sorted(["git", "rsvg-convert", "fc-match", "wl-copy", "xdg-open",
                                                  "uwsm-app", "omarchy-shell", "omarchy-bar", "omarchy-menu-select"]))
        # Each command that says something did say it, over the bus: eleven of them.
        self.assertEqual(len(self.sent()), 11)
        cards = {str(path) for path in self.pictures.glob("omawrapped-*.png")}
        self.assertEqual(len(cards), 2)
        self.assertEqual(leaks(started, self.private_words(), self.tmp, cards), [])

    def test_the_only_paths_are_the_cards_and_only_for_the_programs_that_open_or_show_them(self):
        self.run_everything()
        cards = {str(path) for path in self.pictures.glob("omawrapped-*.png")}
        week = str(self.pictures / ("omawrapped-%s.png" % self.today.isoformat()))
        month = str(self.pictures / ("omawrapped-%s-month.png" % self.today.isoformat()))
        self.assertEqual(cards, {week, month})
        everything = self.stubs.everything()
        paths = {name: sorted({argument for call in calls for argument in call if argument.startswith(str(self.tmp))})
                 for name, calls in everything.items()}
        self.assertEqual({name: found for name, found in paths.items() if found},
                         {"xdg-open": sorted([week, month]), "uwsm-app": [month]})
        # The week's card was opened by `card` and by the menu; the month's by the menu alone.
        self.assertEqual(everything["xdg-open"], [[week], [week], [month]])
        # The card that `show` and the menu's entry showed is the newest in Pictures.
        self.assertEqual(everything["uwsm-app"], [["--", "nautilus", "--select", month]] * 2)

    def test_the_fixed_words_are_all_that_is_left(self):
        self.run_everything()
        everything = self.stubs.everything()
        self.assertEqual({tuple(call) for call in everything["omarchy-shell"]},
                         {(PLUGIN_ID, "status"), (PLUGIN_ID, "flush")})
        self.assertEqual({tuple(call) for call in everything["omarchy-bar"]},
                         {("set", PLUGIN_ID, "paused", word, "--json") for word in ("true", "false")})
        self.assertEqual({tuple(call) for call in everything["wl-copy"]},
                         {("--type", "text/plain"), ("--type", "image/png")})
        self.assertEqual({tuple(call) for call in everything["rsvg-convert"]}, {("--format=png",)})
        self.assertEqual({tuple(call) for call in everything["fc-match"]}, {("-f", "%{family[0]}", "monospace")})
        self.assertEqual({call[0] for call in everything["omarchy-menu-select"]}, {"OmaWrapped"})
        labels = {label for call in everything["omarchy-menu-select"] for label in call[1:]}
        self.assertEqual(labels, set(MENU[1:] + [PAUSE, RESUME]))
        for call in everything["git"]:
            self.assertEqual(call[0], "--no-pager")
            self.assertIn(call[1], ("config", "log"))

    def test_git_was_started_in_the_project_and_not_told_where_it_is(self):
        self.run_everything()
        calls = self.stubs.argv("git")
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.stubs.cwds("git"), [str(self.secret)] * len(calls))
        self.assertEqual([argument for call in calls for argument in call if "secret-client" in argument], [])
        self.assertNotIn("-C", [argument for call in calls for argument in call])

    def test_the_audit_sees_every_kind_of_leak(self):
        # A check that cannot fail would pass. Each of these is the way it used to be done, or its like; the last
        # five are what is allowed.
        words = self.private_words()
        card = str(self.pictures / "omawrapped-2026-10-09.png")
        leaking = [
            {"omarchy-notification-send": [["--app-name", "OmaWrapped", "Today: 3h 07m", "Ghostty 1h 20m"]]},
            {"notify-send": [["Today: 52m"]]},
            {"git": [["-C", str(self.secret), "log"]]},
            {"git": [["log", "secret-client"]]},
            {"git": [["log", "--author=" + ME]]},
            {"wl-copy": [["--", card]]},
            {"wl-copy": [["--select", card]]},
            {"rsvg-convert": [["--format=png", "--output", str(self.tmp / "x.png")]]},
            {"xdg-open": [[str(self.data_home / "omawrapped")]]},
            {"xdg-open": [["/home/someone/slack.png"]]},
            {"uwsm-app": [["--", "xdg-open", card]]},
        ]
        allowed = [
            {"nautilus": [["--select", card]]},
            {"xdg-open": [[card]]},
            {"uwsm-app": [["--", "nautilus", "--select", card]]},
            {"omarchy-menu-select": [["OmaWrapped", "Card of the last 7 days", "Today so far"]]},
            {"omarchy-bar": [["set", PLUGIN_ID, "paused", "true", "--json"]]},
        ]
        for started in leaking:
            with self.subTest(leaking=started):
                self.assertTrue(leaks(started, words, self.tmp, {card}))
        for started in allowed:
            with self.subTest(allowed=started):
                self.assertEqual(leaks(started, words, self.tmp, {card}), [])


class ClipboardTests(CliCase):
    """Group 3: the path of the card goes to the clipboard on the standard input of wl-copy, not in its arguments."""

    def test_the_path_is_on_stdin_byte_for_byte_and_not_in_the_arguments(self):
        self.record()
        output = self.tmp / "my cards" / "café card.svg"
        result = self.ok("card", "--copy", "path", "-o", output)
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "text/plain"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), str(output).encode("utf-8"))
        self.assertFalse(self.stubs.stdin("wl-copy").endswith(b"\n"))
        self.assertEqual(result.stdout, str(output) + "\n")

    @needs_renderer
    def test_the_default_card_is_copied_the_same_way(self):
        self.record()
        result = self.ok("card", "--copy", "path")
        card = result.stdout.strip()
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "text/plain"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), card.encode("utf-8"))

    @needs_renderer
    def test_the_image_goes_on_stdin_too_and_the_path_is_not_an_argument(self):
        self.record()
        output = self.tmp / "secret-client" / "c.png"
        self.ok("card", "--copy", "image", "-o", output)
        self.assertEqual(self.stubs.argv("wl-copy"), [["--type", "image/png"]])
        self.assertEqual(self.stubs.stdin("wl-copy"), output.read_bytes())

    def test_nothing_is_copied_when_nothing_was_asked(self):
        self.record()
        self.ok("card", "--copy", "none", "-o", self.tmp / "c.svg")
        self.assertEqual(self.stubs.argv("wl-copy"), [])


class CardFileTests(CliCase):
    """Group 4: a card is the user's alone, and a render that fails leaves nothing behind and the old card as it was."""

    def test_a_png_and_an_svg_are_private_under_any_umask(self):
        self.record()
        for mask in (0o022, 0o000):
            for name in ("c.png", "c.svg"):
                with self.subTest(umask=oct(mask), name=name):
                    output = self.tmp / ("%03o" % mask) / name
                    self.ok("card", "-o", output, "--copy", "none", umask=mask)
                    self.assertEqual(mode(output), 0o600)

    def test_the_default_card_in_the_pictures_folder_is_private(self):
        self.record()
        result = self.ok("card", "--copy", "none", umask=0o022)
        self.assertEqual(mode(Path(result.stdout.strip())), 0o600)

    def test_a_card_that_replaces_a_world_readable_one_is_private_afterwards(self):
        self.record()
        for name in ("c.png", "c.svg"):
            with self.subTest(name=name):
                output = self.write(self.tmp / "out" / name, "old")
                output.chmod(0o644)
                self.ok("card", "-o", output, "--copy", "none", umask=0o022)
                self.assertNotEqual(output.read_bytes(), b"old")
                self.assertEqual(mode(output), 0o600)

    def test_no_temporary_file_is_left_after_a_card(self):
        self.record()
        for name in ("c.png", "c.svg"):
            self.ok("card", "-o", self.tmp / "out" / name, "--copy", "none")
        self.ok("card", "--copy", "none")
        self.assertEqual(sorted(os.listdir(self.tmp / "out")), ["c.png", "c.svg"])
        self.assertEqual(list(self.tmp.rglob("*.part")), [])

    def failing(self, how, said):
        """A card to draw over an old one, while the renderer fails as `how` does: the old one stays as it was."""
        self.record()
        self.stubs.replace_tool("rsvg-convert")
        how()
        output = self.write(self.tmp / "out" / "c.png", "precious")
        output.chmod(0o644)
        for _ in range(2):
            result = self.run_cli("card", "-o", output, "--copy", "none", "--notify")
            self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", said + "\n"))
            self.assertEqual(output.read_text(encoding="utf-8"), "precious")
            self.assertEqual(mode(output), 0o644)
            self.assertEqual(os.listdir(output.parent), ["c.png"])
            self.assertEqual(list(self.tmp.rglob("*.part")), [])
        self.assertEqual(self.stubs.argv("rsvg-convert"), [["--format=png"]] * 2)

    def test_a_renderer_that_fails_leaves_no_file_and_the_old_card_untouched(self):
        self.failing(lambda: self.stubs.fail("rsvg-convert", 1, reason="not an svg\nline 1: boom"),
                     "rsvg-convert could not render the card: line 1: boom")

    def test_a_renderer_that_prints_nothing_leaves_no_file_and_the_old_card_untouched(self):
        self.failing(lambda: None, "rsvg-convert could not render the card.")

    @needs_renderer
    def test_rsvg_convert_is_started_with_exactly_format_png(self):
        self.record()
        self.stubs.spy("rsvg-convert")
        output = self.tmp / "secret-client" / "c.png"
        self.ok("card", "-o", output, "--copy", "none")
        self.assertEqual(self.stubs.argv("rsvg-convert"), [["--format=png"]])
        self.assertEqual(struct.unpack(">II", output.read_bytes()[16:24]), (1600, 900))

    @needs_renderer
    def test_an_svg_is_not_rendered_at_all(self):
        self.record()
        self.stubs.spy("rsvg-convert")
        self.ok("card", "-o", self.tmp / "c.svg", "--copy", "none")
        self.assertEqual(self.stubs.argv("rsvg-convert"), [])


class GitInTheRepositoryTests(CliCase):
    """Group 5: git is started in the repository, which no argument names."""

    def test_stats_and_cards_start_git_in_the_repository_and_name_it_to_nobody(self):
        repos = self.tmp / "repos"
        secret = init_repo(repos / "secret-client")
        commit(secret, self.local(self.today - timedelta(days=1)))
        commit(secret, self.local(self.today - timedelta(days=2)))
        commit(secret, self.local(self.today - timedelta(days=1)), email=OTHER)
        other = init_repo(repos / "deep" / "other-client")
        commit(other, self.local(self.today - timedelta(days=3)))
        self.record()
        self.stubs.spy("git")
        self.assertEqual(self.stats("--repos", repos)["commits"], 3)
        self.ok("stats", "--repos", repos)
        self.ok("card", "-o", self.tmp / "c.svg", "--copy", "none", "--repos", repos)
        calls = self.stubs.argv("git")
        self.assertGreaterEqual(len(calls), 6)
        self.assertEqual(set(self.stubs.cwds("git")), {str(secret), str(other)})
        self.assertEqual(len(self.stubs.cwds("git")), len(calls))
        for arguments in calls:
            self.assertEqual(arguments[0], "--no-pager")
            self.assertNotIn("-C", arguments)
            for argument in arguments:
                self.assertNotIn("client", argument)
                self.assertNotIn(str(self.tmp), argument)
                self.assertNotIn(ME, argument)

    def test_status_counts_repositories_without_starting_git(self):
        repos = self.tmp / "repos"
        init_repo(repos / "secret-client")
        self.write(self.config / "omarchy" / "shell.json", json.dumps(
            {"version": 1, "bar": {"layout": {"right": [{"id": PLUGIN_ID, "repoDirs": str(repos)}]}}}))
        self.stubs.spy("git")
        self.assertIn("1 repository under %s" % repos, self.ok("status").stdout)
        self.assertEqual(self.stubs.argv("git"), [])


class FailuresOnTheDesktopTests(CliCase):
    """Group 6: a command that the desktop started says why it failed itself, over the bus, and exits 3.

    The widget used to take the first line of stderr and give it to the notification program as an argument.
    """

    needs_bus = True

    def first_line(self) -> str:
        return "Nothing was recorded for the last 7 days (%s)." % aggregate.last_days(7, self.today).span

    def said_on_the_desktop(self, message: str) -> list:
        return [notification("OmaWrapped", message)]

    # ---- the menu ----

    def test_copy_card_without_a_card_exits_3_says_why_on_stderr_and_on_the_bus(self):
        self.stubs.choose_in_menu("Copy card")
        result = self.run_cli("menu", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", NO_CARD + "\n"))
        self.assertEqual(self.sent(), self.said_on_the_desktop(NO_CARD))
        self.assertEqual(self.sent()[0]["summary"], "OmaWrapped")
        self.assertEqual(self.sent()[0]["body"], "There is no card yet. Draw one with `omawrapped card`, "
                                                 "or click the widget.")
        self.assert_nothing_started("omarchy-menu-select", "omarchy-shell")

    def test_without_a_bus_the_same_run_exits_1_and_sends_nothing(self):
        self.stubs.choose_in_menu("Copy card")
        for name, address in (("none named", ""), ("a dead one", isolated_env(self.tmp)["DBUS_SESSION_BUS_ADDRESS"])):
            with self.subTest(bus=name):
                result = self.run_cli("menu", DBUS_SESSION_BUS_ADDRESS=address)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", NO_CARD + "\n"))
        result = self.run_cli("menu", **self.without_python_gobject(), **self.on_bus)
        self.assertEqual((result.returncode, result.stderr), (1, NO_CARD + "\n"))
        self.assertEqual(self.sent(), [])
        self.assert_nothing_started("omarchy-menu-select", "omarchy-shell")

    def test_a_service_that_refuses_leaves_the_exit_status_at_1(self):
        self.stubs.choose_in_menu("Copy card")
        self.bus.refuse(True)
        result = self.run_cli("menu", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", NO_CARD + "\n"))
        self.assertEqual(self.sent(), [])

    def test_a_missing_menu_is_said(self):
        self.stubs.remove("omarchy-menu-select")
        result = self.run_cli("menu", **self.on_bus)
        self.assertEqual((result.returncode, result.stderr), (3, NO_MENU + "\n"))
        self.assertEqual(self.sent(), self.said_on_the_desktop(NO_MENU))

    def test_a_menu_that_breaks_is_said(self):
        self.stubs.fail("omarchy-menu-select", 2)
        said = "omarchy-menu-select failed, so there is no menu to show."
        result = self.run_cli("menu", **self.on_bus)
        self.assertEqual((result.returncode, result.stderr), (3, said + "\n"))
        self.assertEqual(self.sent(), self.said_on_the_desktop(said))

    def test_an_answer_that_is_not_an_option_is_said(self):
        self.stubs.choose_in_menu("Delete everything")
        self.make_card()
        self.assertEqual(self.run_cli("menu", **self.on_bus).returncode, 3)
        self.assertEqual(self.sent(), self.said_on_the_desktop(
            "The menu answered 'Delete everything', which is not one of its options."))

    def test_a_card_from_the_menu_without_data_says_the_first_line_only(self):
        self.stubs.choose_in_menu("Card of the last 7 days")
        result = self.run_cli("menu", **self.on_bus)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(len(result.stderr.splitlines()), 3)
        self.assertEqual(self.sent(), self.said_on_the_desktop(self.first_line()))

    def test_a_menu_that_is_dismissed_or_succeeds_says_nothing_and_keeps_its_status(self):
        self.record()
        self.make_card()
        self.assertEqual(self.run_cli("menu", **self.on_bus).returncode, 0)
        self.stubs.choose_in_menu("Copy card")
        self.assertEqual(self.run_cli("menu", **self.on_bus).returncode, 0)
        self.assertEqual(self.sent(), [notification(
            "Card copied", "omawrapped-2026-10-02.png is on the clipboard. Paste it anywhere.",
            image=self.pictures / "omawrapped-2026-10-02.png")])

    # ---- commands with --notify ----

    def test_card_notify_with_nothing_recorded_exits_3_and_says_the_first_line_of_the_reason_only(self):
        result = self.run_cli("card", "--week", "--notify", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout), (3, ""))
        # stderr is what it was: all three lines.
        span = aggregate.last_days(7, self.today).span
        self.assertEqual(result.stderr, "Nothing was recorded for the last 7 days (%s).\n"
                                        "OmaWrapped counts while its widget is enabled in the bar: "
                                        "omarchy plugin enable %s\nData folder: %s\n" % (
                                            span, PLUGIN_ID, self.data_home / "omawrapped"))
        self.assertEqual(self.sent(), self.said_on_the_desktop(self.first_line()))

    def test_card_notify_without_a_bus_exits_1_and_sends_nothing(self):
        result = self.run_cli("card", "--week", "--notify")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stderr.splitlines()), 3)
        self.assertEqual(self.sent(), [])
        self.bus.refuse(True)
        self.assertEqual(self.run_cli("card", "--week", "--notify", **self.on_bus).returncode, 1)
        self.assertEqual(self.sent(), [])

    def test_a_paused_or_counting_sampler_is_said_in_its_one_line(self):
        paused = ("OmaWrapped has counted less than a minute for the last 7 days, and counting is paused. "
                  "Resume it from the widget's menu or with `omawrapped resume`.")
        counting = ("OmaWrapped has counted less than a minute for the last 7 days so far. "
                    "It is counting now: try again in a minute.")
        self.sampler(paused=True)
        self.assertEqual(self.run_cli("card", "--notify", **self.on_bus).returncode, 3)
        self.assertEqual(self.sent(), self.said_on_the_desktop(paused))
        self.sampler()
        self.assertEqual(self.run_cli("card", "--notify", **self.on_bus).returncode, 3)
        self.assertEqual(self.sent()[1:], self.said_on_the_desktop(counting))

    def test_a_theme_that_is_not_one_is_said_as_it_is(self):
        self.record()
        empty = self.tmp / "no-such-theme"
        empty.mkdir()
        result = self.run_cli("card", "--theme", empty, "--notify", **self.on_bus)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.sent(), self.said_on_the_desktop(
            "%s is not a theme folder: it has no colors.toml." % empty))

    def test_a_renderer_that_fails_is_said_with_its_reason(self):
        self.record()
        self.stubs.replace_tool("rsvg-convert")
        self.stubs.fail("rsvg-convert", 1, reason="line 1: boom")
        result = self.run_cli("card", "-o", self.tmp / "c.png", "--notify", **self.on_bus)
        said = "rsvg-convert could not render the card: line 1: boom"
        self.assertEqual((result.returncode, result.stderr), (3, said + "\n"))
        self.assertEqual(self.sent(), self.said_on_the_desktop(said))

    def test_copy_notify_says_why_it_could_not_copy(self):
        result = self.run_cli("copy", "--notify", **self.on_bus)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", NO_CARD + "\n"))
        self.assertEqual(self.sent(), self.said_on_the_desktop(NO_CARD))
        self.bus.clear()
        missing = self.tmp / "nowhere.png"
        self.assertEqual(self.run_cli("copy", missing, "--notify", **self.on_bus).returncode, 3)
        self.assertEqual(self.sent(), self.said_on_the_desktop("%s does not exist." % missing))

    def test_pause_and_resume_notify_say_why_they_could_not_do_it(self):
        self.stubs.remove("omarchy-bar")
        for command, word in (("pause", "saved"), ("resume", "lifted")):
            with self.subTest(command=command):
                self.bus.clear()
                result = self.run_cli(command, "--notify", **self.on_bus)
                said = "omarchy-bar was not found, so the pause could not be %s. It is part of Omarchy 4." % word
                self.assertEqual((result.returncode, result.stderr), (3, said + "\n"))
                self.assertEqual(self.sent(), self.said_on_the_desktop(said))

    def test_today_notify_without_a_bus_prints_the_numbers_and_says_why_it_could_not_say_them(self):
        self.busy_day()
        sentence = ("Today could not be shown on the desktop: the notification service did not answer. "
                    "`omawrapped today` in a terminal prints it.")
        result = self.run_cli("today", "--notify")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines(), [TODAY] + TODAY_ROWS)
        self.assertEqual(result.stderr, sentence + "\n")
        self.assertEqual(self.sent(), [])

    def test_the_sentence_names_the_reason_the_desktop_could_not_be_told(self):
        self.busy_day()
        sentence = ("Today could not be shown on the desktop: %s. `omawrapped today` in a terminal prints it.\n")
        cases = [
            ("no session bus", {"DBUS_SESSION_BUS_ADDRESS": ""}, "there is no session bus"),
            ("python-gobject missing", {**self.without_python_gobject(), **self.on_bus},
             "python-gobject is not installed"),
            ("a dead address", {}, "the notification service did not answer"),
            ("a service that refuses", self.on_bus, "the notification service did not answer"),
        ]
        for name, env, reason in cases:
            with self.subTest(case=name):
                self.bus.reset()
                if name == "a service that refuses":
                    self.bus.refuse(True)
                result = self.run_cli("today", "--notify", **env)
                self.assertEqual((result.returncode, result.stderr), (1, sentence % reason))
                self.assertEqual(result.stdout.splitlines(), [TODAY] + TODAY_ROWS)
                self.assertEqual(self.sent(), [])

    def test_today_notify_with_the_bus_is_a_success(self):
        self.busy_day()
        self.assertEqual(self.run_cli("today", "--notify", **self.on_bus).returncode, 0)

    # ---- what does not change ----

    def test_a_failure_without_notify_exits_1_and_sends_nothing_even_with_the_bus_up(self):
        self.stubs.remove("omarchy-bar")
        for args in (("card", "--week"), ("copy",), ("show",), ("pause",), ("resume",), ("reset",)):
            with self.subTest(command=args):
                result = self.run_cli(*args, **self.on_bus)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotEqual(result.stderr, "")
        self.assertEqual(self.sent(), [])

    def test_a_warning_on_a_card_that_was_made_is_not_a_failure(self):
        self.record()
        self.stubs.fail("wl-copy")
        output = self.tmp / "c.svg"
        result = self.run_cli("card", "-o", output, "--notify", **self.on_bus)
        self.assertEqual(result.returncode, 0)
        self.assertIn(COPY_FAILED, result.stderr)
        self.assertEqual([sent["summary"] for sent in self.sent()], ["Card saved"])

    def test_an_error_in_the_arguments_exits_2_and_says_nothing_on_the_desktop(self):
        result = self.run_cli("card", "--days", "0", "--notify", **self.on_bus)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.run_cli("today", "--notify", "--days", "3", **self.on_bus).returncode, 2)
        self.assertEqual(self.run_cli("menu", "--notify", **self.on_bus).returncode, 2)
        self.assertEqual(self.sent(), [])

    def test_the_status_is_3_because_the_widget_asks_for_it(self):
        self.assertEqual(cli.SAID_ON_DESKTOP, 3)
        self.assertNotIn(cli.SAID_ON_DESKTOP, (0, 1, 2, 130))

    def test_no_program_is_started_to_say_it(self):
        self.stubs.choose_in_menu("Copy card")
        self.run_cli("menu", **self.on_bus)
        self.run_cli("card", "--week", "--notify", **self.on_bus)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(self.stubs.argv("omarchy-notification-send"), [])
        self.assertEqual(self.stubs.argv("notify-send"), [])


class MainInProcessTests(StubbedCase):
    """main() called in this process, as the tests of the other groups cannot: it forgets what a run said."""

    needs_bus = True

    def setUp(self):
        super().setUp()
        self.use_bus()
        # main() puts the signal for a closed pipe back to its default; the test runner is not to be changed.
        patcher = mock.patch.object(cli.signal, "signal")
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def say(self, *messages, status=1):
        """A stand-in for `_today` that says these things and ends with this status."""
        def today(notify):
            for message in messages:
                cli._say(message)
            return status
        return mock.patch.object(cli, "_today", today)

    def test_a_failure_from_the_desktop_is_said_there_and_exits_3(self):
        self.assertEqual(self.main("copy", "--notify"), (3, "", NO_CARD + "\n"))
        self.assertEqual(self.bus.notifications(), [notification("OmaWrapped", NO_CARD)])

    def test_the_menu_counts_as_the_desktop_without_the_option(self):
        self.stubs.choose_in_menu("Show in folder")
        self.assertEqual(self.main("menu"), (3, "", NO_CARD + "\n"))
        self.assertEqual(self.bus.notifications(), [notification("OmaWrapped", NO_CARD)])

    def test_another_command_does_not_count_as_the_desktop(self):
        self.assertEqual(self.main("copy"), (1, "", NO_CARD + "\n"))
        self.assertEqual(self.main("show"), (1, "", NO_CARD + "\n"))
        self.assertEqual(self.bus.notifications(), [])

    def test_it_is_the_last_thing_said_that_is_said_and_only_its_first_line(self):
        with self.say("The first thing", "The second thing\nand the rest of it", "The last thing\nwith more"):
            self.assertEqual(self.main("today", "--notify")[0], 3)
        self.assertEqual(self.bus.notifications(), [notification("OmaWrapped", "The last thing")])

    def test_what_a_run_said_is_forgotten_by_the_next_run(self):
        self.assertEqual(self.main("copy")[0], 1)
        self.assertEqual(cli._said, [NO_CARD])
        # This run fails without a word: the old message is not its to say.
        with self.say():
            self.assertEqual(self.main("today", "--notify"), (1, "", ""))
        self.assertEqual(cli._said, [])
        self.assertEqual(self.bus.notifications(), [])
        self.assertEqual(self.main("copy")[0], 1)
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(cli._said, [])

    def test_a_status_other_than_1_is_left_alone(self):
        for status in (0, 2, 3, 4):
            with self.subTest(status=status):
                with self.say("Something was said", status=status):
                    self.assertEqual(self.main("today", "--notify")[0], status)
        self.assertEqual(self.bus.notifications(), [])

    def test_an_interrupt_is_130_and_says_nothing(self):
        def interrupted(notify):
            cli._say("Something was said first")
            raise KeyboardInterrupt
        with mock.patch.object(cli, "_today", interrupted):
            self.assertEqual(self.main("today", "--notify")[0], 130)
        self.assertEqual(self.bus.notifications(), [])

    def test_a_message_with_nothing_to_say_is_not_sent(self):
        for message in ("", "   ", "\n\n"):
            with self.subTest(message=message):
                with self.say(message):
                    self.assertEqual(self.main("today", "--notify")[0], 1)
        self.assertEqual(self.bus.notifications(), [])

    def test_the_headline_is_the_name_of_the_app(self):
        with mock.patch.object(share, "APP_NAME", "Renamed"):
            self.assertEqual(self.main("copy", "--notify")[0], 3)
        self.assertEqual(self.bus.notifications()[0]["summary"], "Renamed")

    def test_a_service_that_refuses_leaves_the_status_at_1(self):
        self.bus.refuse(True)
        self.assertEqual(self.main("copy", "--notify")[0], 1)

    def test_stderr_is_the_same_with_and_without_the_bus(self):
        with_bus = self.main("copy", "--notify")
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": ""}):
            without = self.main("copy", "--notify")
        self.assertEqual((with_bus[1], with_bus[2]), (without[1], without[2]))
        self.assertEqual((with_bus[0], without[0]), (3, 1))


class IsolationTests(OnTheBus, CliCase):
    """The tests that matter most: nothing may reach the real desktop, clipboard or data."""

    def test_the_environment_of_a_run_holds_only_the_fake_machine(self):
        env = self.environment()
        for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_DATA_DIRS", "XDG_PICTURES_DIR", "OMARCHY_PATH",
                     "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL"):
            self.assertTrue(env[name].startswith(str(self.tmp)), "%s=%s" % (name, env[name]))
        # The stand-ins, and links to the harmless tools: no folder of the machine, so no real desktop program.
        self.assertEqual(env["PATH"], "%s:%s" % (self.tmp / "stubs", self.tmp / "tools"))
        self.assertEqual(sorted(path.name for path in (self.tmp / "tools").iterdir() if path.name in STUBBED), [])
        for folder in env["PATH"].split(":"):
            self.assertTrue(folder.startswith(str(self.tmp)), folder)
        for name in ("WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "SSH_AUTH_SOCK"):
            self.assertNotIn(name, env)

    def test_a_run_has_a_session_bus_that_is_not_there(self):
        # So that no test can notify the person who runs the suite, however it is written. A test that wants to
        # see a notification starts a bus of its own and says so.
        address = self.environment()["DBUS_SESSION_BUS_ADDRESS"]
        self.assertEqual(address, "unix:path=%s" % (self.tmp / "run" / "no-bus"))
        self.assertTrue(address.startswith("unix:path=" + str(self.tmp) + "/"))
        self.assertFalse(Path(address[len("unix:path="):]).exists())
        self.assertFalse((self.tmp / "run" / "bus").exists())

    def test_the_isolated_environment_names_a_bus_inside_its_root_that_does_not_exist(self):
        root = self.tmp / "another-root"
        address = isolated_env(root)["DBUS_SESSION_BUS_ADDRESS"]
        self.assertEqual(address, "unix:path=%s/run/no-bus" % root)
        self.assertFalse(Path(address[len("unix:path="):]).exists())
        self.assertFalse(root.exists())

    def test_the_variable_of_the_real_session_is_replaced_not_left_in_place(self):
        # The environment is patched without clearing what was there: the explicit value is what keeps a real
        # address out. Pretend the suite is run from a desktop session.
        real = "unix:path=/run/user/1000/bus"
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": real}):
            inner = IsolatedCase("write")
            inner.setUp()
            try:
                self.assertEqual(os.environ["DBUS_SESSION_BUS_ADDRESS"], "unix:path=%s/run/no-bus" % inner.tmp)
                self.assertNotEqual(share._bus_address(), real)
            finally:
                inner.doCleanups()
            self.assertEqual(os.environ["DBUS_SESSION_BUS_ADDRESS"], real)

    def test_in_this_process_too_the_bus_is_inside_the_fake_machine(self):
        self.assertTrue(share._bus_address().startswith("unix:path=" + str(self.tmp) + "/"), share._bus_address())
        self.assertFalse(Path(share._bus_address()[len("unix:path="):]).exists())
        self.assertIs(share.notify("Nobody is to see this"), False)
        self.assertEqual(self.sent(), [])

    def test_a_run_that_is_not_given_the_test_bus_cannot_reach_it(self):
        self.assertNotEqual(self.environment()["DBUS_SESSION_BUS_ADDRESS"], self.bus.address)
        self.busy_day()
        dead = self.environment()["DBUS_SESSION_BUS_ADDRESS"]
        result = self.run_cli("today", "--notify", DBUS_SESSION_BUS_ADDRESS=dead)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.sent(), [])

    def test_the_environment_and_the_temporary_folder_are_restored_after_a_test(self):
        before = dict(os.environ)
        inner = IsolatedCase("write")
        inner.setUp()
        try:
            self.assertEqual(os.environ["HOME"], str(inner.home))
            self.assertEqual(Path.home(), inner.home)
            self.assertTrue(inner.tmp.is_dir())
        finally:
            inner.doCleanups()
        self.assertEqual(dict(os.environ), before)
        self.assertFalse(inner.tmp.exists())

    def test_every_stand_in_hides_the_real_program_when_it_is_removed(self):
        for name in STUBBED:
            with self.subTest(name=name):
                folder = self.tmp / ("again-" + name)
                folder.mkdir()
                stubs = Stubs(folder)
                stubs.remove(name)
                self.assertIsNone(shutil.which(name, path=stubs.path))

    def test_every_command_only_ever_reached_the_stand_ins(self):
        self.record()
        self.busy_day()
        self.sampler()
        repos = self.tmp / "repos"
        self.repo_with_commits(repos)
        card = self.make_card()
        self.ok("stats")
        self.ok("status")
        self.ok("card", "-o", self.tmp / "c.svg", "--open", "--notify", "--repos", repos)
        self.ok("copy", "--notify")
        self.ok("copy", self.tmp / "c.svg")
        self.ok("show")
        self.ok("show", self.tmp / "c.svg")
        self.ok("menu")
        for label in ("Copy card", "Show in folder", "Today so far"):
            self.stubs.choose_in_menu(label)
            self.ok("menu")
        self.ok("today")
        self.ok("today", "--notify")
        # The sampler follows each of these as it is told: it is paused when `pause` looks, counting for `resume`.
        self.sampler(paused=True)
        self.ok("pause", "--notify", **FAST)
        self.sampler()
        self.ok("resume", "--notify", **FAST)
        # The menu offers what suits the sampler as it is when it opens; the sampler has followed at the next look.
        self.samplers({}, {"paused": True})
        self.stubs.choose_in_menu("Pause counting")
        self.ok("menu", **FAST)
        self.samplers({"paused": True}, {"paused": False})
        self.stubs.choose_in_menu("Resume counting")
        self.ok("menu", **FAST)
        self.sampler()
        self.ok("reset", "--yes")
        self.stubs.wait_for_argv("xdg-open")
        deadline = time.monotonic() + 5
        while len(self.stubs.argv("uwsm-app")) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        status, flush, discard = STATUS, FLUSH, PLUGIN_ID + " discard"
        self.assertEqual(self.stubs.calls("omarchy-shell"), [
            status, flush,                   # stats
            status,                          # status
            status, flush,                   # card
            status, status, status,          # the menu: dismissed, then copy and show
            status, status, flush,           # the menu: today so far
            status, flush, status, flush,    # today, today --notify
            status, status,                  # pause, resume
            status, status, status, status,  # the menu: pause counting, resume counting
            status, discard])                # reset
        self.assertEqual(self.stubs.argv("omarchy-bar"), [
            ["set", PLUGIN_ID, "paused", "true", "--json"], ["set", PLUGIN_ID, "paused", "false", "--json"],
            ["set", PLUGIN_ID, "paused", "true", "--json"], ["set", PLUGIN_ID, "paused", "false", "--json"]])
        self.assertEqual(self.stubs.argv("wl-copy"), [
            ["--type", "text/plain"], ["--type", "image/png"], ["--type", "image/svg+xml"], ["--type", "image/png"]])
        self.assertEqual(self.stubs.stdin("wl-copy", 0), str(self.tmp / "c.svg").encode())
        self.assertEqual(self.stubs.calls("xdg-open"), [str(self.tmp / "c.svg")])
        self.assertEqual(self.stubs.argv("uwsm-app"), [
            ["--", "nautilus", "--select", str(card)], ["--", "nautilus", "--select", str(self.tmp / "c.svg")],
            ["--", "nautilus", "--select", str(card)]])
        self.assertEqual(self.sent(), [
            notification("Card saved", "Its path is on the clipboard. " + CLICK_HINT,
                         click=[str(LAUNCHER), "show", str(self.tmp / "c.svg")]),
            notification("Card copied", "%s is on the clipboard. Paste it anywhere." % card.name, image=card),
            notification("Card copied", "%s is on the clipboard. Paste it anywhere." % card.name, image=card),
            notification("Today: 3h 07m", TODAY_BODY),
            notification("Today: 3h 07m", TODAY_BODY),
            notification("Counting paused", "Resume it from the widget's menu."),
            notification("Counting again", ""),
            notification("Counting paused", "Resume it from the widget's menu."),
            notification("Counting again", "")])
        self.assertEqual(len(self.stubs.argv("omarchy-menu-select")), 6)
        # Nothing but the stand-ins can have been reached, so nothing else was. The notifications went over the
        # bus: the programs that used to send them were not started.
        self.assertEqual(self.stubs.argv("nautilus"), [])
        self.assertEqual(self.stubs.argv("notify-send"), [])
        self.assertEqual(self.stubs.argv("omarchy-notification-send"), [])


if __name__ == "__main__":
    unittest.main()
