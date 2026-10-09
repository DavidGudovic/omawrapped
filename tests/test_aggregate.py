import support  # noqa: F401  (must stay first: it disables bytecode and isolates the environment)

import unittest
from datetime import date, timedelta

from omawrapped import aggregate
from omawrapped.aggregate import Period, Summary, duration, hour_label, last_days, summarize
from omawrapped.store import Day

HOUR = 3600000
MINUTE = 60000


def hours_with(**per_hour) -> list:
    """24 hour slots; hours_with(h09=5, h10=7) puts 5 ms in hour 9 and 7 ms in hour 10."""
    slots = [0] * 24
    for key, ms in per_hour.items():
        slots[int(key[1:])] = ms
    return slots


class LastDaysTests(unittest.TestCase):
    def test_covers_exactly_n_dates_ending_on_the_given_day(self):
        today = date(2026, 10, 9)
        period = last_days(7, today)
        self.assertEqual(period.end, today)
        self.assertEqual(period.start, date(2026, 10, 3))
        self.assertEqual(period.length, 7)
        self.assertEqual(period.dates, [date(2026, 10, d) for d in range(3, 10)])

    def test_crosses_month_and_year_boundaries(self):
        self.assertEqual(last_days(7, date(2026, 3, 3)).dates[0], date(2026, 2, 25))
        self.assertEqual(last_days(5, date(2026, 1, 2)).dates,
                         [date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 1),
                          date(2026, 1, 2)])

    def test_leap_day_counts(self):
        self.assertEqual(last_days(3, date(2028, 3, 1)).dates, [date(2028, 2, 28), date(2028, 2, 29), date(2028, 3, 1)])

    def test_one_day_is_just_that_day(self):
        period = last_days(1, date(2026, 10, 9))
        self.assertEqual(period.dates, [date(2026, 10, 9)])
        self.assertEqual(period.start, period.end)

    def test_named_periods(self):
        self.assertEqual(aggregate.PERIODS, {"week": 7, "month": 30})
        self.assertEqual(last_days(aggregate.PERIODS["month"], date(2026, 10, 9)).length, 30)


class PeriodTests(unittest.TestCase):
    def test_span_of_one_day(self):
        self.assertEqual(Period(date(2026, 10, 9), date(2026, 10, 9)).span, "Oct 9, 2026")

    def test_span_within_a_month(self):
        self.assertEqual(Period(date(2026, 10, 3), date(2026, 10, 9)).span, "Oct 3 – 9, 2026")

    def test_span_across_months(self):
        self.assertEqual(Period(date(2026, 9, 28), date(2026, 10, 4)).span, "Sep 28 – Oct 4, 2026")

    def test_span_across_years(self):
        self.assertEqual(Period(date(2025, 12, 29), date(2026, 1, 4)).span, "Dec 29, 2025 – Jan 4, 2026")

    def test_span_of_a_whole_month(self):
        self.assertEqual(Period(date(2026, 2, 1), date(2026, 2, 28)).span, "Feb 1 – 28, 2026")

    def test_label(self):
        self.assertEqual(last_days(7, date(2026, 10, 9)).label, "Last 7 days")
        self.assertEqual(last_days(30, date(2026, 10, 9)).label, "Last 30 days")
        self.assertEqual(last_days(2, date(2026, 10, 9)).label, "Last 2 days")
        self.assertEqual(last_days(1, date(2026, 10, 9)).label, "Today")

    def test_length_counts_both_ends(self):
        self.assertEqual(Period(date(2026, 10, 3), date(2026, 10, 9)).length, 7)
        self.assertEqual(Period(date(2026, 10, 9), date(2026, 10, 9)).length, 1)


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.period = last_days(7, date(2026, 10, 9))  # Oct 3 - Oct 9
        self.days = {
            date(2026, 10, 4): Day(date(2026, 10, 4), active_ms=HOUR, switches=10,
                                   hours_ms=hours_with(h09=HOUR // 2, h10=HOUR // 2),
                                   apps_ms={"zed": 40 * MINUTE, "slack": 20 * MINUTE}),
            date(2026, 10, 7): Day(date(2026, 10, 7), active_ms=3 * HOUR, switches=25,
                                   hours_ms=hours_with(h10=HOUR, h14=2 * HOUR),
                                   apps_ms={"zed": HOUR, "chromium": 2 * HOUR}),
            date(2026, 10, 9): Day(date(2026, 10, 9), active_ms=30 * MINUTE, switches=3,
                                   hours_ms=hours_with(h23=30 * MINUTE), apps_ms={"slack": 30 * MINUTE}),
            # Outside the period: before it, and after it.
            date(2026, 10, 2): Day(date(2026, 10, 2), active_ms=9 * HOUR, switches=99,
                                   hours_ms=hours_with(h03=9 * HOUR), apps_ms={"old": 9 * HOUR}),
            date(2026, 10, 10): Day(date(2026, 10, 10), active_ms=8 * HOUR, switches=88,
                                    hours_ms=hours_with(h04=8 * HOUR), apps_ms={"future": 8 * HOUR}),
        }

    def test_totals(self):
        summary = summarize(self.days, self.period)
        self.assertIs(summary.period, self.period)
        self.assertEqual(summary.total_ms, HOUR + 3 * HOUR + 30 * MINUTE)
        self.assertEqual(summary.switches, 38)

    def test_per_day_list_covers_every_date_with_zero_for_unrecorded_days(self):
        summary = summarize(self.days, self.period)
        self.assertEqual(summary.daily_ms, [
            (date(2026, 10, 3), 0), (date(2026, 10, 4), HOUR), (date(2026, 10, 5), 0), (date(2026, 10, 6), 0),
            (date(2026, 10, 7), 3 * HOUR), (date(2026, 10, 8), 0), (date(2026, 10, 9), 30 * MINUTE),
        ])

    def test_hours_are_summed_over_the_period(self):
        summary = summarize(self.days, self.period)
        self.assertEqual(summary.hours_ms, hours_with(h09=HOUR // 2, h10=HOUR // 2 + HOUR, h14=2 * HOUR,
                                                      h23=30 * MINUTE))

    def test_apps_are_added_up_and_sorted_by_time(self):
        summary = summarize(self.days, self.period)
        self.assertEqual(summary.apps, [("chromium", 120 * MINUTE), ("zed", 100 * MINUTE), ("slack", 50 * MINUTE)])

    def test_apps_with_the_same_time_are_sorted_by_name(self):
        days = {date(2026, 10, 9): Day(date(2026, 10, 9), active_ms=HOUR,
                                       apps_ms={"cherry": 10, "apple": 10, "banana": 10, "big": 99})}
        summary = summarize(days, last_days(1, date(2026, 10, 9)))
        self.assertEqual([name for name, _ in summary.apps], ["big", "apple", "banana", "cherry"])

    def test_name_ties_ignore_case(self):
        apps = {"Zebra": 10, "alpha": 10, "Mango": 10}
        days = {date(2026, 10, 9): Day(date(2026, 10, 9), active_ms=HOUR, apps_ms=apps)}
        summary = summarize(days, last_days(1, date(2026, 10, 9)))
        self.assertEqual([name for name, _ in summary.apps], ["alpha", "Mango", "Zebra"])

    def test_days_outside_the_period_are_ignored(self):
        summary = summarize(self.days, self.period)
        self.assertNotIn("old", dict(summary.apps))
        self.assertNotIn("future", dict(summary.apps))
        self.assertEqual(summary.hours_ms[3], 0)
        self.assertEqual(summary.hours_ms[4], 0)
        self.assertNotIn(date(2026, 10, 2), [day for day, _ in summary.daily_ms])
        self.assertNotIn(date(2026, 10, 10), [day for day, _ in summary.daily_ms])

    def test_name_of_merges_ids_that_share_a_name(self):
        names = {"com.mitchellh.ghostty": "Ghostty", "ghostty": "Ghostty"}
        days = {
            date(2026, 10, 8): Day(date(2026, 10, 8), active_ms=HOUR,
                                   apps_ms={"com.mitchellh.ghostty": 1000, "zed": 500}),
            date(2026, 10, 9): Day(date(2026, 10, 9), active_ms=HOUR, apps_ms={"ghostty": 2000}),
        }
        summary = summarize(days, self.period, name_of=lambda app: names.get(app, app))
        self.assertEqual(summary.apps, [("Ghostty", 3000), ("zed", 500)])

    def test_exclude_by_id_ignores_case_and_keeps_the_totals(self):
        everything = summarize(self.days, self.period)
        summary = summarize(self.days, self.period, exclude=["SLACK"])
        self.assertEqual([name for name, _ in summary.apps], ["chromium", "zed"])
        self.assertEqual(summary.total_ms, everything.total_ms)
        self.assertEqual(summary.daily_ms, everything.daily_ms)
        self.assertEqual(summary.hours_ms, everything.hours_ms)
        self.assertEqual(summary.switches, everything.switches)

    def test_exclude_by_display_name_ignores_case_and_catches_every_id_with_that_name(self):
        names = {"com.mitchellh.ghostty": "Ghostty", "ghostty": "Ghostty", "zed": "Zed"}
        days = {date(2026, 10, 9): Day(date(2026, 10, 9), active_ms=HOUR,
                                       apps_ms={"com.mitchellh.ghostty": 1000, "ghostty": 2000, "zed": 500})}
        everything = summarize(days, self.period, name_of=lambda app: names.get(app, app))
        summary = summarize(days, self.period, name_of=lambda app: names.get(app, app), exclude=("gHOSTTY",))
        self.assertEqual(summary.apps, [("Zed", 500)])
        self.assertEqual(summary.total_ms, everything.total_ms)

    def test_exclude_by_id_leaves_other_ids_of_the_same_name(self):
        days = {date(2026, 10, 9): Day(date(2026, 10, 9), active_ms=HOUR, apps_ms={"first.id": 1000, "second.id": 300})}
        summary = summarize(days, self.period, name_of=lambda app: "Same Name", exclude=["FIRST.ID"])
        self.assertEqual(summary.apps, [("Same Name", 300)])

    def test_exclude_may_name_several_apps(self):
        summary = summarize(self.days, self.period, exclude=["zed", "slack"])
        self.assertEqual(summary.apps, [("chromium", 120 * MINUTE)])

    def test_nothing_recorded(self):
        summary = summarize({}, self.period)
        self.assertEqual(summary.total_ms, 0)
        self.assertEqual(summary.apps, [])
        self.assertEqual(summary.switches, 0)
        self.assertEqual(summary.hours_ms, [0] * 24)
        self.assertEqual(summary.daily_ms, [(day, 0) for day in self.period.dates])

    def test_summaries_do_not_share_state(self):
        first = summarize(self.days, self.period)
        second = summarize({}, self.period)
        self.assertEqual(first.total_ms, 4 * HOUR + 30 * MINUTE)
        self.assertEqual(second.hours_ms, [0] * 24)
        self.assertIsNot(first.hours_ms, second.hours_ms)


class SummaryPropertyTests(unittest.TestCase):
    def summary(self, daily, hours=None, total=None):
        start = date(2026, 10, 1)
        period = Period(start, start + timedelta(days=len(daily) - 1)) if daily else last_days(7, start)
        return Summary(period=period, total_ms=sum(daily) if total is None else total,
                       daily_ms=[(start + timedelta(days=i), ms) for i, ms in enumerate(daily)],
                       hours_ms=hours or [0] * 24)

    def test_active_days_counts_days_with_time(self):
        self.assertEqual(self.summary([0, 5, 0, 7, 1]).active_days, 3)
        self.assertEqual(self.summary([0, 0]).active_days, 0)

    def test_average_is_per_active_day(self):
        self.assertEqual(self.summary([0, 100, 0, 101, 0, 0, 0]).average_ms, 100)
        self.assertEqual(self.summary([3 * HOUR, 0, 0, 0, 0, 0, 0]).average_ms, 3 * HOUR)

    def test_average_of_nothing_is_zero(self):
        self.assertEqual(self.summary([0] * 7).average_ms, 0)
        self.assertEqual(Summary(period=last_days(7, date(2026, 10, 9))).average_ms, 0)

    def test_busiest_day_is_the_one_with_the_most_time(self):
        summary = self.summary([5, 90, 7])
        self.assertEqual(summary.busiest_day, (date(2026, 10, 2), 90))

    def test_busiest_day_tie_goes_to_the_earliest(self):
        summary = self.summary([5, 90, 7, 90, 90])
        self.assertEqual(summary.busiest_day, (date(2026, 10, 2), 90))

    def test_busiest_day_of_nothing_is_none(self):
        self.assertIsNone(self.summary([0, 0, 0]).busiest_day)
        self.assertIsNone(Summary(period=last_days(7, date(2026, 10, 9))).busiest_day)

    def test_peak_hour_is_the_hour_with_the_most_time(self):
        self.assertEqual(self.summary([1], hours_with(h13=500, h04=20)).peak_hour, 13)
        self.assertEqual(self.summary([1], hours_with(h23=1)).peak_hour, 23)
        self.assertEqual(self.summary([1], hours_with(h00=1)).peak_hour, 0)

    def test_peak_hour_tie_goes_to_the_earliest(self):
        self.assertEqual(self.summary([1], hours_with(h20=500, h09=500, h15=500)).peak_hour, 9)

    def test_peak_hour_of_nothing_is_none(self):
        self.assertIsNone(self.summary([1]).peak_hour)
        self.assertIsNone(Summary(period=last_days(7, date(2026, 10, 9))).peak_hour)


class DurationTests(unittest.TestCase):
    def test_examples(self):
        cases = [
            (0, "0m"),
            (59999, "0m"),
            (60000, "1m"),
            (59 * MINUTE + 59999, "59m"),
            (HOUR, "1h 00m"),
            (HOUR + 5 * MINUTE, "1h 05m"),
            ((38 * 3600 + 42 * 60 + 59) * 1000, "38h 42m"),
            (100 * HOUR, "100h 00m"),
            (-1, "0m"),
            (-HOUR, "0m"),
        ]
        for ms, expected in cases:
            with self.subTest(ms=ms):
                self.assertEqual(duration(ms), expected)

    def test_minutes_are_cut_not_rounded(self):
        self.assertEqual(duration(89999), "1m")
        self.assertEqual(duration(HOUR - 1), "59m")


class HourLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(hour_label(0), "00:00")
        self.assertEqual(hour_label(9), "09:00")
        self.assertEqual(hour_label(13), "13:00")
        self.assertEqual(hour_label(23), "23:00")


if __name__ == "__main__":
    unittest.main()
