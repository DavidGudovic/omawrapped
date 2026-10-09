import support  # noqa: F401  (must stay first: it disables bytecode and isolates the environment)

import json
import os
import pty
import random
import re
import shutil
import struct
import subprocess
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from datetime import time as time_of_day
from pathlib import Path

from omawrapped import VERSION, aggregate
from support import (LAUNCHER, OTHER, PLUGIN_ID, IsolatedCase, Stubs, bytecode_dirs, cli_env, clock, commit,
                     init_repo, make_sample)

SVG = "{http://www.w3.org/2000/svg}"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The CLI runs with PATH=<stubs>:/usr/bin, so that is the only place it can find the renderer.
needs_renderer = unittest.skipUnless(os.access("/usr/bin/rsvg-convert", os.X_OK),
                                     "/usr/bin/rsvg-convert is not installed")


def svg_texts(path: Path) -> list:
    return ["".join(element.itertext()) for element in ET.parse(path).getroot().iter(SVG + "text")]


class CliCase(IsolatedCase):
    """Runs bin/omawrapped as a subprocess in a fake machine, with stand-ins for the programs it starts."""

    def setUp(self):
        super().setUp()
        self.stubs = Stubs(self.tmp)
        self.today = date.today()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.addCleanup(self.assert_no_bytecode)

    def assert_no_bytecode(self):
        self.assertEqual(bytecode_dirs(), [], "the launcher must not leave bytecode in the plugin folder")

    def environment(self, **extra) -> dict:
        env = cli_env(self.tmp, self.stubs)
        env.update(extra)
        return env

    def run_cli(self, *args, stdin=subprocess.DEVNULL, cwd=None, launcher=LAUNCHER, **extra_env):
        return subprocess.run(
            [str(launcher), *map(str, args)], capture_output=True, text=True, stdin=stdin,
            env=self.environment(**extra_env), cwd=str(cwd or self.work), timeout=60,
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

    def window(self, length: int) -> list:
        """The fixture files of the last `length` days that were recorded, oldest first."""
        first = self.today - timedelta(days=length - 1)
        return [raw for day, raw in sorted(self.recorded().items()) if first <= day <= self.today]

    def stats(self, *args) -> dict:
        return json.loads(self.ok("stats", "--json", *args).stdout)

    def local(self, day: date, at: time_of_day = time_of_day(12)) -> datetime:
        return datetime.combine(day, at).astimezone()

    def repo_with_commits(self, folder: Path) -> Path:
        """A repository whose commits fall inside, on the edge of and outside the last week. Oldest first."""
        repo = init_repo(folder / "project")
        commit(repo, self.local(self.today - timedelta(days=60)))
        commit(repo, self.local(self.today - timedelta(days=7), time_of_day(23, 59, 59)))
        commit(repo, self.local(self.today - timedelta(days=6), time_of_day(0, 0, 0)))
        commit(repo, self.local(self.today - timedelta(days=1)))
        commit(repo, self.local(self.today - timedelta(days=1), time_of_day(13)), email=OTHER)
        return repo


class LauncherTests(CliCase):
    def test_no_arguments_prints_help_and_exits_0(self):
        result = self.ok()
        self.assertIn("usage: omawrapped", result.stdout)
        for command in ("card", "stats", "status", "reset"):
            self.assertIn(command, result.stdout)
        self.assertEqual(result.stderr, "")

    def test_version(self):
        result = self.ok("--version")
        self.assertEqual(result.stdout.strip(), "omawrapped " + VERSION)

    def test_help_of_every_command(self):
        for command in ("card", "stats", "status", "reset"):
            with self.subTest(command=command):
                self.assertIn("usage: omawrapped " + command, self.ok(command, "--help").stdout)

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
        for name in ("omarchy-shell", "wl-copy", "xdg-open"):
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

    def test_the_shell_is_asked_to_flush_first(self):
        self.ok("stats")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [PLUGIN_ID + " flush"])

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
        self.assert_argument_error("stats", "--days", "0", message="between 1 and 366")

    def test_week_and_month_together(self):
        for command in ("stats", "card"):
            with self.subTest(command=command):
                self.assert_argument_error(command, "--week", "--month", message="not allowed with argument")

    def test_other_period_options_together(self):
        self.assert_argument_error("stats", "--days", "5", "--month", message="not allowed with argument")
        self.assert_argument_error("stats", "--week", "--days", "5", message="not allowed with argument")

    def test_a_bad_copy_choice(self):
        self.assert_argument_error("card", "--copy", "everything", message="invalid choice")


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

    def test_the_shell_is_asked_to_flush_first(self):
        self.record()
        self.ok("card", "-o", self.tmp / "c.svg", "--copy", "none")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [PLUGIN_ID + " flush"])

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
        self.assertEqual(self.stubs.calls("wl-copy"), ["-- " + str(output)])

    def test_copy_path_asks_for_the_path(self):
        self.record()
        output = self.tmp / "c.svg"
        result = self.ok("card", "-o", output, "--copy", "path")
        self.assertEqual(self.stubs.calls("wl-copy"), ["-- " + str(output)])
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
        (self.stubs.dir / "wl-copy").unlink()
        # /usr/bin may hold a real wl-copy, so the path is the stand-ins' folder alone.
        result = self.run_cli("card", "-o", self.tmp / "c.svg", PATH=str(self.stubs.dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wl-copy was not found", result.stderr)
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
        self.assertEqual(self.stubs.calls("wl-copy"), ["-- " + str(expected)])

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


class StatusTests(CliCase):
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
        ]
        for reply, expected in cases:
            with self.subTest(reply=reply):
                self.stubs.reply_to_status(json.dumps(reply))
                self.assertIn("Sampler   " + expected, self.ok("status").stdout)

    def test_garbage_from_the_shell_means_not_running(self):
        self.stubs.reply_to_status("this is not json")
        self.assertIn("Sampler   not running", self.ok("status").stdout)

    def test_lists_the_helpers(self):
        out = self.ok("status").stdout
        self.assertRegex(out, r"(?m)^Renderer\s+rsvg-convert (found|MISSING)")
        self.assertRegex(out, r"(?m)^Clipboard\s+wl-copy found")

    def test_counts_repositories(self):
        repos = self.tmp / "repos"
        self.repo_with_commits(repos)
        self.assertRegex(self.ok("status", "--repos", repos).stdout,
                         r"(?m)^Git\s.*\b1 repository under " + re.escape(str(repos)))
        init_repo(repos / "second")
        self.assertRegex(self.ok("status", "--repos", repos).stdout, r"(?m)^Git\s.*\b2 repositories under ")

    def test_says_whether_git_is_there(self):
        self.assertRegex(self.ok("status").stdout, r"(?m)^Git\s+found")
        # A path with the stand-ins alone has no git, no rsvg-convert and no fc-match.
        out = self.ok("status", PATH=str(self.stubs.dir)).stdout
        self.assertRegex(out, r"(?m)^Git\s+MISSING")
        self.assertRegex(out, r"(?m)^Renderer\s+rsvg-convert MISSING")

    def test_default_repository_folders(self):
        self.assertIn("0 repositories under ~/projects, ~/code", self.ok("status").stdout)

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

    def test_yes_asks_the_shell_to_discard_what_it_holds(self):
        self.ok("reset", "--yes")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [PLUGIN_ID + " discard"])

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


class IsolationTests(CliCase):
    """The tests that matter most: nothing may reach the real desktop, clipboard or data."""

    def test_the_environment_of_a_run_holds_only_the_fake_machine(self):
        env = self.environment()
        for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_DATA_DIRS", "XDG_PICTURES_DIR", "OMARCHY_PATH",
                     "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL"):
            self.assertTrue(env[name].startswith(str(self.tmp)), "%s=%s" % (name, env[name]))
        self.assertEqual(env["PATH"], "%s:/usr/bin" % (self.tmp / "stubs"))
        for name in ("WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "SSH_AUTH_SOCK"):
            self.assertNotIn(name, env)

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

    def test_every_command_only_ever_reached_the_stand_ins(self):
        self.record()
        repos = self.tmp / "repos"
        self.repo_with_commits(repos)
        self.ok("stats")
        self.ok("status")
        self.ok("card", "-o", self.tmp / "c.svg", "--open", "--repos", repos)
        self.stubs.wait_for_call("xdg-open")
        self.ok("reset", "--yes")
        self.assertEqual(self.stubs.calls("omarchy-shell"), [
            PLUGIN_ID + " flush", PLUGIN_ID + " status", PLUGIN_ID + " flush", PLUGIN_ID + " discard"])
        self.assertEqual(self.stubs.calls("wl-copy"), ["-- " + str(self.tmp / "c.svg")])
        self.assertEqual(self.stubs.calls("xdg-open"), [str(self.tmp / "c.svg")])


if __name__ == "__main__":
    unittest.main()
