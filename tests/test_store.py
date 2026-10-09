import support  # noqa: F401  (must stay first: it disables bytecode and isolates the environment)

import json
import os
import unittest
from datetime import date
from unittest import mock

from omawrapped import store
from support import IsolatedCase, day_json

DAY = date(2026, 3, 4)


def text_of(**fields) -> str:
    return json.dumps(day_json(DAY, **fields))


class DataDirTests(IsolatedCase):
    def test_absolute_xdg_data_home_is_used(self):
        elsewhere = self.tmp / "elsewhere"
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(elsewhere)}):
            self.assertEqual(store.data_dir(), elsewhere / "omawrapped")
            self.assertEqual(store.days_dir(), elsewhere / "omawrapped" / "days")

    def test_relative_xdg_data_home_is_ignored(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "relative/share"}):
            self.assertEqual(store.data_dir(), self.home / ".local" / "share" / "omawrapped")

    def test_empty_or_unset_xdg_data_home_falls_back_to_home(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}):
            self.assertEqual(store.data_dir(), self.home / ".local" / "share" / "omawrapped")
        with mock.patch.dict(os.environ):
            del os.environ["XDG_DATA_HOME"]
            self.assertEqual(store.data_dir(), self.home / ".local" / "share" / "omawrapped")


class ParseDayTests(unittest.TestCase):
    def test_valid_file_is_read(self):
        hours = [h * 1000 for h in range(24)]
        day = store.parse_day(text_of(active_ms=12345, hours_ms=hours, apps_ms={"a": 5000, "b": 7000}, switches=4), DAY)
        self.assertEqual(day.date, DAY)
        self.assertEqual(day.active_ms, 12345)
        self.assertEqual(day.hours_ms, hours)
        self.assertEqual(day.apps_ms, {"a": 5000, "b": 7000})
        self.assertEqual(day.switches, 4)

    def test_fields_missing_from_the_file_read_as_zero_or_empty(self):
        day = store.parse_day(json.dumps({"version": 1, "date": DAY.isoformat()}), DAY)
        self.assertEqual((day.active_ms, day.switches, day.apps_ms), (0, 0, {}))
        self.assertEqual(day.hours_ms, [0] * 24)

    def test_wrong_version_is_not_a_day_file(self):
        for version in (0, 2, "1", None, 1.5, [1]):
            with self.subTest(version=version):
                raw = day_json(DAY)
                raw["version"] = version
                self.assertIsNone(store.parse_day(json.dumps(raw), DAY))
        raw = day_json(DAY)
        del raw["version"]
        self.assertIsNone(store.parse_day(json.dumps(raw), DAY))

    def test_true_is_not_version_1(self):
        # JSON true compares equal to 1 in Python; it is still not the number the format asks for.
        raw = day_json(DAY)
        raw["version"] = True
        self.assertIsNone(store.parse_day(json.dumps(raw), DAY))

    def test_wrong_date_is_not_a_day_file(self):
        for stored in ("2026-03-05", "2025-03-04", "2026-3-4", "20260304", 20260304, None):
            with self.subTest(date=stored):
                raw = day_json(DAY)
                raw["date"] = stored
                self.assertIsNone(store.parse_day(json.dumps(raw), DAY))
        raw = day_json(DAY)
        del raw["date"]
        self.assertIsNone(store.parse_day(json.dumps(raw), DAY))

    def test_text_that_is_not_json_is_not_a_day_file(self):
        for text in ("", "{", "not json", '{"version": 1,', "\x00\x01"):
            with self.subTest(text=text):
                self.assertIsNone(store.parse_day(text, DAY))

    def test_json_that_is_not_an_object_is_not_a_day_file(self):
        for text in ("[]", "[1, 2]", "[%s]" % text_of(), "null", "42", '"text"', "true"):
            with self.subTest(text=text):
                self.assertIsNone(store.parse_day(text, DAY))

    def test_unusable_counters_read_as_zero(self):
        unusable = [-1, -0.5, 0, "12", "", True, False, None, [5], {"a": 5},
                    float("nan"), float("inf"), float("-inf")]
        for bad in unusable:
            with self.subTest(value=repr(bad)):
                day = store.parse_day(
                    text_of(active_ms=bad, switches=bad, hours_ms=[bad] * 24, apps_ms={"app": bad}), DAY)
                self.assertEqual(day.active_ms, 0)
                self.assertEqual(day.switches, 0)
                self.assertEqual(day.hours_ms, [0] * 24)
                self.assertEqual(day.apps_ms, {})

    def test_a_number_too_big_for_a_float_reads_as_zero(self):
        raw = '{"version": 1, "date": "2026-03-04", "active_ms": 1e999, "switches": 1e999, "apps_ms": {"a": 1e999}}'
        day = store.parse_day(raw, DAY)
        self.assertEqual((day.active_ms, day.switches, day.apps_ms), (0, 0, {}))

    def test_one_bad_counter_does_not_spoil_the_others(self):
        hours = [100, "x", 300] + [0] * 21
        day = store.parse_day(text_of(active_ms=-5, hours_ms=hours, apps_ms={"good": 10, "bad": -10}, switches=7), DAY)
        self.assertEqual(day.active_ms, 0)
        self.assertEqual(day.hours_ms[:3], [100, 0, 300])
        self.assertEqual(day.apps_ms, {"good": 10})
        self.assertEqual(day.switches, 7)

    def test_counters_are_whole_numbers(self):
        day = store.parse_day(text_of(active_ms=1500.0, switches=3.0, apps_ms={"a": 2500.0}), DAY)
        self.assertEqual((day.active_ms, day.switches, day.apps_ms), (1500, 3, {"a": 2500}))
        self.assertIsInstance(day.active_ms, int)
        self.assertIsInstance(day.apps_ms["a"], int)

    def test_short_hours_list_is_padded_to_24(self):
        day = store.parse_day(text_of(hours_ms=[100, 200]), DAY)
        self.assertEqual(day.hours_ms, [100, 200] + [0] * 22)
        self.assertEqual(store.parse_day(text_of(hours_ms=[]), DAY).hours_ms, [0] * 24)

    def test_long_hours_list_is_cut_to_24(self):
        day = store.parse_day(text_of(hours_ms=list(range(1, 31))), DAY)
        self.assertEqual(day.hours_ms, list(range(1, 25)))

    def test_hours_and_apps_of_the_wrong_type_read_as_empty(self):
        for wrong in ("text", 5, {"0": 100}, None):
            with self.subTest(hours_ms=wrong):
                self.assertEqual(store.parse_day(text_of(hours_ms=wrong), DAY).hours_ms, [0] * 24)
        for wrong in ("text", 5, [["a", 1]], None):
            with self.subTest(apps_ms=wrong):
                self.assertEqual(store.parse_day(text_of(apps_ms=wrong), DAY).apps_ms, {})

    def test_apps_without_time_or_name_are_dropped(self):
        apps = {"kept": 1, "zero": 0, "negative": -3, "": 500, "text": "9"}
        self.assertEqual(store.parse_day(text_of(apps_ms=apps), DAY).apps_ms, {"kept": 1})


class DayFilesTests(IsolatedCase):
    def test_no_folder_means_no_files(self):
        self.assertFalse(self.days.exists())
        self.assertEqual(store.day_files(), [])

    def test_files_are_listed_oldest_first(self):
        for name in ("2026-03-10", "2025-12-31", "2026-03-02", "2026-01-15"):
            self.write(self.days / (name + ".json"), "{}")
        listed = store.day_files()
        self.assertEqual([day for day, _ in listed],
                         [date(2025, 12, 31), date(2026, 1, 15), date(2026, 3, 2), date(2026, 3, 10)])
        self.assertEqual(listed[0][1], self.days / "2025-12-31.json")

    def test_names_that_are_not_day_files_are_ignored(self):
        self.write(self.days / "2026-03-04.json", "{}")
        for name in ("2026-13-40.json", "2026-02-30.json", "2026-00-10.json", "notes.json", "2026-03-05.json.tmp",
                     "2026-3-5.json", ".2026-03-05.json.4242.tmp", "2026-03-05.JSON", "x2026-03-05.json",
                     "2026-03-05.jsonl", "20260305.json", "2026-03-05"):
            self.write(self.days / name, "{}")
        self.assertEqual([day for day, _ in store.day_files()], [DAY])


class LoadDaysTests(IsolatedCase):
    def test_range_includes_both_ends(self):
        for day in range(1, 11):
            self.write_day(date(2026, 3, day), active_ms=day * 1000)
        loaded = store.load_days(date(2026, 3, 3), date(2026, 3, 6))
        self.assertEqual(sorted(loaded), [date(2026, 3, d) for d in (3, 4, 5, 6)])
        self.assertEqual(loaded[date(2026, 3, 3)].active_ms, 3000)
        self.assertEqual(loaded[date(2026, 3, 6)].active_ms, 6000)

    def test_a_single_day_range_loads_that_day(self):
        self.write_day(date(2026, 3, 4), active_ms=7)
        self.write_day(date(2026, 3, 5), active_ms=8)
        self.assertEqual(list(store.load_days(DAY, DAY)), [DAY])

    def test_reversed_range_is_empty(self):
        self.write_day(DAY, active_ms=1)
        self.assertEqual(store.load_days(date(2026, 3, 6), date(2026, 3, 1)), {})

    def test_no_folder_loads_nothing(self):
        self.assertEqual(store.load_days(date(2026, 1, 1), date(2026, 12, 31)), {})

    def test_files_that_cannot_be_used_are_skipped(self):
        self.write_day(date(2026, 3, 1), active_ms=1)
        self.write(self.days / "2026-03-02.json", "this is not json")
        self.write(self.days / "2026-03-03.json", json.dumps(day_json(date(2026, 3, 9), active_ms=3)))
        self.write(self.days / "2026-03-04.json", json.dumps({**day_json(DAY, active_ms=4), "version": 2}))
        self.write(self.days / "2026-03-05.json", "[1, 2, 3]")
        (self.days / "2026-03-06.json").write_bytes(b'{"version": 1, "date": "2026-03-06", "x": "\xff\xfe"}')
        (self.days / "2026-03-07.json").mkdir()
        self.write_day(date(2026, 3, 8), active_ms=8)
        loaded = store.load_days(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual(sorted(loaded), [date(2026, 3, 1), date(2026, 3, 8)])


class ResetTests(IsolatedCase):
    def test_removes_day_files_and_leftovers_and_the_empty_folders(self):
        for day in (1, 2, 3):
            self.write_day(date(2026, 3, day), active_ms=1000)
        self.write(self.days / ".2026-03-03.json.Ab12Cd", "half a file")
        self.write(self.days / "2026-03-03.json.tmp", "half a file")
        self.assertEqual(store.reset(), 3)
        self.assertFalse(self.days.exists())
        self.assertFalse(store.data_dir().exists())
        self.assertTrue(self.data_home.is_dir())

    def test_counts_day_files_only(self):
        self.write_day(DAY, active_ms=1)
        self.write(self.days / "leftover.tmp", "x")
        self.assertEqual(store.reset(), 1)
        self.assertEqual(store.reset(), 0)

    def test_leftovers_alone_count_for_nothing_but_are_removed(self):
        self.write(self.days / ".2026-03-03.json.Ab12Cd", "half a file")
        self.assertEqual(store.reset(), 0)
        self.assertFalse(self.days.exists())

    def test_folders_that_hold_something_else_stay(self):
        self.write_day(DAY, active_ms=1)
        keep = self.write(store.data_dir() / "notes.txt", "mine")
        self.assertEqual(store.reset(), 1)
        self.assertFalse(self.days.exists())
        self.assertEqual(keep.read_text(encoding="utf-8"), "mine")

    def test_nothing_to_remove_is_a_no_op(self):
        self.assertEqual(store.reset(), 0)
        self.assertFalse(store.data_dir().exists())

    def test_an_empty_days_folder_is_removed_too(self):
        self.days.mkdir(parents=True)
        self.assertEqual(store.reset(), 0)
        self.assertFalse(store.data_dir().exists())

    def test_files_outside_the_data_folder_are_not_touched(self):
        self.write_day(DAY, active_ms=1)
        bystanders = [self.write(self.pictures / "omawrapped-2026-03-04.png", "png"),
                      self.write(self.data_home / "other-app" / "days" / "2026-03-04.json", "{}"),
                      self.write(self.home / "Documents" / "2026-03-04.json", "{}")]
        self.assertEqual(store.reset(), 1)
        for path in bystanders:
            self.assertTrue(path.exists(), path)


if __name__ == "__main__":
    unittest.main()
