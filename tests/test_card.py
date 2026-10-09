import support  # noqa: F401  (must stay first: it disables bytecode and isolates the environment)

import os
import shutil
import stat
import struct
import unittest
import xml.etree.ElementTree as ET
from collections import Counter, namedtuple
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from omawrapped import aggregate, card, render, system
from omawrapped.store import Day
from support import SAMPLE_END, IsolatedCase, clock, sample_days, sample_summary

SVG = "{http://www.w3.org/2000/svg}"
THEMES = Path("/usr/share/omarchy/themes")
HOUR = 3600000
EPS = 1e-6
# (palette field, least contrast against the background)
FLOORS = (("ink", 4.5), ("secondary", 4.5), ("muted", 4.0), ("mark", 3.0), ("quiet", 1.7))
LONG_NAME = ("Very-Long-Application-Name-" * 3)[:60]

Box = namedtuple("Box", "text x y left right anchor size")


def boxes(svg: str, metrics: render.Metrics) -> list:
    """Every <text> of the card with the horizontal space it takes up."""
    found = []
    for element in ET.fromstring(svg).iter(SVG + "text"):
        text = "".join(element.itertext())
        x, y = float(element.get("x")), float(element.get("y"))
        size = float(element.get("font-size"))
        bold = element.get("font-weight") in ("700", "bold")
        spacing = float(element.get("letter-spacing", "0"))
        anchor = element.get("text-anchor", "start")
        width = metrics.width(text, size, bold, spacing)
        left = {"start": x, "end": x - width, "middle": x - width / 2}[anchor]
        found.append(Box(text, x, y, left, left + width, anchor, size))
    return found


def texts(svg: str) -> list:
    return ["".join(element.itertext()) for element in ET.fromstring(svg).iter(SVG + "text")]


class ColorTests(unittest.TestCase):
    def test_mix_endpoints_and_middle(self):
        self.assertEqual(card.mix("#102030", "#ffffff", 0), "#102030")
        self.assertEqual(card.mix("#102030", "#ffffff", 1), "#ffffff")
        self.assertEqual(card.mix("#000000", "#ffffff", 0.25), "#404040")
        self.assertEqual(card.mix("#ff0000", "#0000ff", 0.5), "#800080")

    def test_mix_works_per_channel_in_either_direction(self):
        self.assertEqual(card.mix("#ffffff", "#000000", 0.25), "#bfbfbf")
        self.assertEqual(card.mix("#00ff00", "#00ff00", 0.7), "#00ff00")

    def test_contrast_of_black_on_white_is_21(self):
        self.assertAlmostEqual(card.contrast("#000000", "#ffffff"), 21.0, places=6)
        self.assertAlmostEqual(card.contrast("#ffffff", "#000000"), 21.0, places=6)

    def test_contrast_of_a_color_on_itself_is_1(self):
        for color in ("#000000", "#ffffff", "#e68e0d", "#123456", "#808080"):
            with self.subTest(color=color):
                self.assertAlmostEqual(card.contrast(color, color), 1.0, places=9)

    def test_contrast_matches_a_known_wcag_value(self):
        # #767676 on white is the classic darkest grey that passes AA for text.
        self.assertAlmostEqual(card.contrast("#767676", "#ffffff"), 4.54, places=2)

    def test_contrast_is_symmetric(self):
        self.assertEqual(card.contrast("#336699", "#ffcc00"), card.contrast("#ffcc00", "#336699"))


class PaletteTests(unittest.TestCase):
    def assert_floors(self, palette):
        for field, floor in FLOORS:
            color = getattr(palette, field)
            self.assertRegex(color, r"^#[0-9a-f]{6}$", field)
            self.assertGreaterEqual(card.contrast(color, palette.background), floor,
                                    "%s %s on %s" % (field, color, palette.background))

    def test_a_comfortable_theme_keeps_its_own_colors(self):
        theme = system.Theme(background="#101315", foreground="#cacccc", accent="#e68e0d")
        palette = card.palette(theme)
        self.assertEqual(palette.background, "#101315")
        self.assertEqual(palette.ink, "#cacccc")
        self.assertEqual(palette.mark, "#e68e0d")
        self.assert_floors(palette)

    def test_a_deliberately_hostile_theme_still_meets_the_floors(self):
        # Foreground almost the background; the accent is the background.
        for background, foreground in (("#808080", "#818181"), ("#101010", "#111111"), ("#f0f0f0", "#efefef"),
                                       ("#7a7a7a", "#7b7b7b"), ("#ffffff", "#fefefe"), ("#000000", "#010101")):
            with self.subTest(background=background):
                self.assert_floors(card.palette(system.Theme(background=background, foreground=foreground,
                                                             accent=background)))

    def test_every_grey_background_meets_the_floors_with_a_matching_foreground(self):
        for level in range(0, 256, 5):
            grey = "#%02x%02x%02x" % (level, level, level)
            with self.subTest(grey=grey):
                self.assert_floors(card.palette(system.Theme(background=grey, foreground=grey, accent=grey)))

    def test_saturated_backgrounds_meet_the_floors(self):
        for background in ("#ff0000", "#00ff00", "#0000ff", "#ffff00", "#00ffff", "#ff00ff", "#2e7d32", "#ffb300"):
            with self.subTest(background=background):
                self.assert_floors(card.palette(system.Theme(background=background, foreground=background,
                                                             accent=background)))

    def test_a_dull_accent_is_replaced_by_the_ink(self):
        palette = card.palette(system.Theme(background="#101315", foreground="#cacccc", accent="#14171a"))
        self.assertEqual(palette.mark, palette.ink)


@unittest.skipUnless(THEMES.is_dir(), "%s does not exist on this machine: no installed themes to check" % THEMES)
class InstalledThemeTests(IsolatedCase):
    def test_every_installed_theme_meets_the_contrast_floors(self):
        folders = sorted(path for path in THEMES.iterdir() if (path / "colors.toml").is_file())
        if not folders:
            self.skipTest("%s holds no theme with a colors.toml" % THEMES)
        for folder in folders:
            with self.subTest(theme=folder.name):
                palette = card.palette(system.theme(folder))
                for field, floor in FLOORS:
                    ratio = card.contrast(getattr(palette, field), palette.background)
                    self.assertGreaterEqual(ratio, floor, "%s %s on %s" % (field, getattr(palette, field),
                                                                           palette.background))


def week_facts(**changes) -> card.Facts:
    values = {"theme_name": "Matte Black", "plugins": 3, "omarchy": "3.5.2", "commits": 1234, "repos": 3}
    values.update(changes)
    return card.Facts(**values)


def one_app_summary() -> aggregate.Summary:
    day = Day(SAMPLE_END, active_ms=5 * HOUR, hours_ms=[0] * 9 + [HOUR] * 5 + [0] * 10, apps_ms={LONG_NAME: 5 * HOUR},
              switches=12)
    return aggregate.summarize({SAMPLE_END: day}, aggregate.last_days(7, SAMPLE_END), lambda app: app)


def one_recorded_day_summary() -> aggregate.Summary:
    day = sample_days(SAMPLE_END, 7)[SAMPLE_END - timedelta(days=2)]
    return sample_summary(7, days={day.date: day})


def heavy_year_summary() -> aggregate.Summary:
    days = {}
    for offset in range(366):
        day = SAMPLE_END - timedelta(days=offset)
        days[day] = Day(day, active_ms=23 * HOUR, hours_ms=[HOUR] * 23 + [0], apps_ms={"code": 23 * HOUR}, switches=500)
    return sample_summary(366, days=days)


def scenarios() -> list:
    """(label, summary, facts) of every card the layout is checked on."""
    return [
        ("week", sample_summary(7), week_facts()),
        ("month", sample_summary(30), week_facts()),
        ("12 days", sample_summary(12), week_facts()),
        ("90 days", sample_summary(90), week_facts()),
        ("nothing recorded", sample_summary(7, days={}), week_facts(commits=None, repos=0)),
        ("nothing recorded, with commits", sample_summary(7, days={}), week_facts(commits=0, repos=0)),
        ("one 60-character app", one_app_summary(), week_facts()),
        ("one recorded day", one_recorded_day_summary(), week_facts()),
        ("today", sample_summary(1), week_facts()),
        ("a year", sample_summary(366, days=sample_days(SAMPLE_END, 366)), week_facts()),
        ("a year of 23-hour days", heavy_year_summary(), week_facts(commits=1234567, repos=120)),
        ("no commits found", sample_summary(7), week_facts(commits=None, repos=0)),
        ("long footer", sample_summary(7),
         week_facts(theme_name="Tokyo Night Storm Extra Long Edition Name", plugins=12, omarchy="3.5.2-rc1")),
    ]


class CardCase(IsolatedCase):
    """Cards are drawn with the metrics the real command uses, and with the estimate used without Pango."""

    def setUp(self):
        super().setUp()
        self.theme = system.Theme(name="Matte Black", background="#121212", foreground="#bebebe", accent="#e68e0d")
        self.variants = []
        exact = render.Metrics(system.monospace_family())
        if exact.exact:
            self.variants.append(("pango", exact))
        estimate = render.Metrics("monospace")
        estimate.exact = False
        self.variants.append(("estimate", estimate))

    def build(self, summary, facts, metrics=None, theme=None) -> str:
        return card.build(summary, facts, theme or self.theme, metrics or self.variants[0][1])

    def for_each_card(self, check):
        """Calls check(label, svg, metrics) for every scenario under every kind of metrics."""
        for label, summary, facts in scenarios():
            for kind, metrics in self.variants:
                with self.subTest(card=label, metrics=kind):
                    check(label, self.build(summary, facts, metrics), metrics)


class CardStructureTests(CardCase):
    def test_every_scenario_is_well_formed_xml_of_1600_by_900(self):
        def check(label, svg, metrics):
            root = ET.fromstring(svg)
            self.assertEqual(root.tag, SVG + "svg")
            self.assertEqual(root.get("width"), "1600")
            self.assertEqual(root.get("height"), "900")
            self.assertEqual(root.get("viewBox"), "0 0 1600 900")

        self.for_each_card(check)

    def test_the_background_fills_the_canvas_in_the_theme_color(self):
        root = ET.fromstring(self.build(sample_summary(7), week_facts()))
        first = next(root.iter(SVG + "rect"))
        self.assertEqual([first.get(k) for k in ("x", "y", "width", "height")], ["0.0", "0.0", "1600.0", "900.0"])
        self.assertEqual(first.get("fill"), self.theme.background)

    def test_every_fill_and_stroke_is_a_color_or_none(self):
        def check(label, svg, metrics):
            for element in ET.fromstring(svg).iter():
                for attribute in ("fill", "stroke"):
                    value = element.get(attribute)
                    if value is not None:
                        self.assertRegex(value, r"^(#[0-9a-f]{6}|none)$")

        self.for_each_card(check)

    def test_the_same_input_gives_the_same_card(self):
        summary, facts = sample_summary(30), week_facts()
        self.assertEqual(self.build(summary, facts), self.build(summary, facts))

    def test_the_font_family_ends_in_the_monospace_alias(self):
        svg = self.build(sample_summary(7), week_facts(), render.Metrics("JetBrainsMono Nerd Font"))
        self.assertEqual(ET.fromstring(svg).get("font-family"), "'JetBrainsMono Nerd Font', monospace")
        self.assertEqual(ET.fromstring(self.build(sample_summary(7), week_facts(), render.Metrics("monospace")))
                         .get("font-family"), "monospace")


class CardLayoutTests(CardCase):
    def test_no_text_runs_off_the_canvas(self):
        def check(label, svg, metrics):
            outside = ["%r spans %.1f..%.1f" % (b.text, b.left, b.right) for b in boxes(svg, metrics)
                       if b.left < -EPS or b.right > card.WIDTH + EPS]
            self.assertEqual(outside, [])

        self.for_each_card(check)

    def test_the_left_column_ends_where_the_right_column_begins(self):
        limit = card.LEFT[1] + 1

        def check(label, svg, metrics):
            if label == "long footer":
                return  # the footer is a row of its own, checked in the next test but one
            too_wide = ["%r ends at %.1f" % (b.text, b.right) for b in boxes(svg, metrics)
                        if b.anchor == "start" and b.x < 760 and b.right > limit + EPS]
            self.assertEqual(too_wide, [])

        self.for_each_card(check)

    def test_app_names_stop_short_of_their_durations(self):
        def check(label, svg, metrics):
            found = boxes(svg, metrics)
            names = [b for b in found if b.anchor == "start" and b.x == 800]
            values = [b for b in found if b.anchor == "end" and b.x == 1528]
            collisions = ["%r ends at %.1f, %r starts at %.1f" % (n.text, n.right, v.text, v.left)
                          for n in names for v in values if n.y == v.y and n.right >= v.left]
            self.assertEqual(collisions, [])

        self.for_each_card(check)

    def test_the_footer_text_stops_short_of_the_credit(self):
        def check(label, svg, metrics):
            footer = [b for b in boxes(svg, metrics) if b.y == 850]
            credit = [b for b in footer if b.anchor == "end"]
            self.assertEqual([b.text for b in credit], [card.REPO])
            collisions = ["%r ends at %.1f, the credit starts at %.1f" % (b.text, b.right, credit[0].left)
                          for b in footer if b.anchor == "start" and b.right >= credit[0].left]
            self.assertEqual(collisions, [])

        self.for_each_card(check)

    def test_texts_on_one_line_do_not_overlap(self):
        def check(label, svg, metrics):
            found = boxes(svg, metrics)
            overlaps = []
            for index, a in enumerate(found):
                for b in found[index + 1:]:
                    if a.y == b.y and a.left < b.right - EPS and b.left < a.right - EPS:
                        overlaps.append("y=%g: %r [%.1f..%.1f] overlaps %r [%.1f..%.1f]" % (
                            a.y, a.text, a.left, a.right, b.text, b.left, b.right))
            self.assertEqual(overlaps[:3], [], "%d pairs of texts overlap (first three shown)" % len(overlaps))

        self.for_each_card(check)


class CardContentTests(CardCase):
    PERIODS = (
        (7, "Last 7 days · Oct 3 – 9, 2026"),
        (30, "Last 30 days · Sep 10 – Oct 9, 2026"),
        (12, "Last 12 days · Sep 28 – Oct 9, 2026"),
        (90, "Last 90 days · Jul 12 – Oct 9, 2026"),
        (1, "Today · Oct 9, 2026"),
    )

    def top_apps(self, days: dict, length: int) -> list:
        """The five biggest apps of the last `length` days, worked out from the day files alone."""
        names = system.AppNames([])
        totals = Counter()
        for day in aggregate.last_days(length, SAMPLE_END).dates:
            for app, ms in (days[day].apps_ms.items() if day in days else ()):
                totals[names.name(app)] += ms
        return sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:5]

    def test_the_total_and_the_period_are_on_the_card(self):
        for length, header in self.PERIODS:
            days = sample_days(SAMPLE_END, length)
            with self.subTest(days=length):
                found = texts(self.build(sample_summary(length), week_facts()))
                self.assertIn(clock(sum(day.active_ms for day in days.values())), found)
                self.assertIn(header, found)
                self.assertIn("screen time", found)

    def test_a_period_across_years_shows_both_years(self):
        found = texts(self.build(sample_summary(7, end=date(2026, 1, 4)), week_facts()))
        self.assertIn("Last 7 days · Dec 29, 2025 – Jan 4, 2026", found)

    def test_the_average_per_active_day(self):
        days = sample_days(SAMPLE_END, 30)
        average = sum(day.active_ms for day in days.values()) // len(days)
        line = next(t for t in texts(self.build(sample_summary(30), week_facts())) if "per active day" in t)
        self.assertIn(clock(average), line)
        self.assertIn("30 of 30 days", line)

    def test_a_single_day_has_no_average_line(self):
        self.assertFalse([t for t in texts(self.build(sample_summary(1), week_facts())) if "per active day" in t])

    def test_the_top_five_apps_and_their_times_are_listed(self):
        for length in (7, 30, 90):
            days = sample_days(SAMPLE_END, length)
            found = texts(self.build(sample_summary(length), week_facts()))
            expected = self.top_apps(days, length)
            self.assertEqual(len(expected), 5)
            with self.subTest(days=length):
                for name, ms in expected:
                    self.assertIn(name, found)
                    self.assertIn(clock(ms), found)
                # In the order of their time: names appear top to bottom.
                shown = [t for t in found if t in {name for name, _ in expected}]
                self.assertEqual(shown, [name for name, _ in expected])

    def test_only_five_apps_are_listed(self):
        names = system.AppNames([])
        everything = Counter()
        for day in sample_days(SAMPLE_END, 7).values():
            for app, ms in day.apps_ms.items():
                everything[names.name(app)] += ms
        ranked = [name for name, _ in sorted(everything.items(), key=lambda item: (-item[1], item[0]))]
        self.assertGreater(len(ranked), 5)
        found = texts(self.build(sample_summary(7), week_facts()))
        for name in ranked[5:]:
            self.assertNotIn(name, found)

    def test_a_60_character_app_name_is_shortened_with_an_ellipsis(self):
        self.assertEqual(len(LONG_NAME), 60)
        for kind, metrics in self.variants:
            with self.subTest(metrics=kind):
                found = texts(self.build(one_app_summary(), week_facts(), metrics))
                self.assertNotIn(LONG_NAME, found)
                shortened = [t for t in found if t.endswith("…") and LONG_NAME.startswith(t[:-1])]
                self.assertEqual(len(shortened), 1, found)
                self.assertGreater(len(shortened[0]), 10)
                self.assertIn("5h 00m", found)

    def test_a_short_app_name_is_left_alone(self):
        found = texts(self.build(sample_summary(7, name_of=lambda app: "Short"), week_facts()))
        self.assertIn("Short", found)
        self.assertFalse([t for t in found if t.endswith("…")])

    def test_special_characters_in_an_app_name_are_escaped_and_round_trip(self):
        awkward = 'Fish <&"> \'n\' Chips'
        svg = self.build(sample_summary(7, name_of=lambda app: awkward if app == "chromium" else app), week_facts())
        self.assertIn(awkward, texts(svg))
        self.assertIn("&lt;", svg)
        self.assertIn("&amp;", svg)
        self.assertNotIn("<&", svg)

    def test_special_characters_in_the_footer_are_escaped_and_round_trip(self):
        facts = week_facts(theme_name='R&D <"dark">', omarchy="3<5&6")
        found = texts(self.build(sample_summary(7), facts))
        self.assertIn('R&D <"dark"> theme · 3 plugins · Omarchy 3<5&6', found)

    def test_characters_xml_cannot_hold_do_not_break_the_svg(self):
        # An app id is whatever the program called itself; JSON can store any of these.
        for awkward in ("bad\x01name", "bad\x00name", "bad\x1fname", "bad\ufffename"):
            with self.subTest(name=awkward):
                svg = self.build(sample_summary(7, name_of=lambda app: awkward if app == "chromium" else app),
                                 week_facts())
                try:
                    ET.fromstring(svg)
                except ET.ParseError as error:
                    self.fail("the card is not well-formed XML: %s" % error)

    def test_a_name_that_is_nothing_but_special_characters(self):
        svg = self.build(sample_summary(7, name_of=lambda app: '<&">'), week_facts())
        self.assertIn('<&">', texts(svg))

    def test_the_commit_tile(self):
        found = texts(self.build(sample_summary(7), week_facts(commits=1234, repos=3)))
        self.assertIn("1,234", found)
        self.assertIn("commits in 3 repos", found)

    def test_the_commit_tile_with_one_commit_in_one_repo(self):
        found = texts(self.build(sample_summary(7), week_facts(commits=1, repos=1)))
        self.assertIn("1", found)
        self.assertIn("commit in 1 repo", found)

    def test_the_commit_tile_with_no_commits(self):
        found = texts(self.build(sample_summary(7), week_facts(commits=0, repos=0)))
        self.assertIn("0", found)
        self.assertIn("commits in 0 repos", found)

    def test_no_commit_tile_when_commits_is_none(self):
        found = texts(self.build(sample_summary(7), week_facts(commits=None, repos=0)))
        self.assertFalse([t for t in found if "commit" in t.lower()], found)
        self.assertFalse([t for t in found if "repo" in t.lower() and "github" not in t.lower()], found)

    def test_four_headline_numbers_with_commits(self):
        days = sample_days(SAMPLE_END, 7)
        found = texts(self.build(sample_summary(7), week_facts(commits=42, repos=2)))
        busiest = max(sorted(days), key=lambda day: days[day].active_ms)
        hours = [sum(day.hours_ms[h] for day in days.values()) for h in range(24)]
        self.assertIn(busiest.strftime("%A"), found)
        self.assertIn("busiest day · " + clock(days[busiest].active_ms), found)
        self.assertIn("%02d:00" % hours.index(max(hours)), found)
        self.assertIn("busiest hour", found)
        self.assertIn(format(sum(day.switches for day in days.values()), ","), found)
        self.assertIn("app switches", found)
        self.assertNotIn("days active", found)

    def test_days_active_takes_the_place_of_the_commit_tile(self):
        found = texts(self.build(sample_summary(7), week_facts(commits=None, repos=0)))
        self.assertIn("7 / 7", found)
        self.assertIn("days active", found)

    def test_the_busiest_day_is_dated_in_periods_longer_than_a_week(self):
        days = sample_days(SAMPLE_END, 30)
        busiest = max(sorted(days), key=lambda day: days[day].active_ms)
        found = texts(self.build(sample_summary(30), week_facts()))
        self.assertIn("%s %s %d" % (busiest.strftime("%a"), busiest.strftime("%b"), busiest.day), found)

    def test_a_card_with_nothing_recorded(self):
        found = texts(self.build(sample_summary(7, days={}), week_facts(commits=None, repos=0)))
        self.assertIn("0m", found)
        self.assertIn("No app time recorded in this period.", found)
        self.assertIn("0 / 7", found)
        self.assertFalse([t for t in found if "per active day" in t])

    def test_one_recorded_day_among_seven(self):
        found = texts(self.build(one_recorded_day_summary(), week_facts(commits=None, repos=0)))
        self.assertIn("1 / 7", found)

    def test_the_captions_and_the_hour_scale(self):
        found = texts(self.build(sample_summary(7), week_facts()))
        for caption in ("OMAWRAPPED", "TOP APPS", "BY DAY", "BY HOUR", "00", "06", "12", "18"):
            self.assertIn(caption, found)

    def test_the_day_scale_names_the_days_of_a_week(self):
        found = texts(self.build(sample_summary(7), week_facts()))
        for offset in range(7):
            self.assertIn((SAMPLE_END - timedelta(days=offset)).strftime("%a"), found)

    def test_the_day_scale_numbers_the_days_of_twelve(self):
        found = texts(self.build(sample_summary(12), week_facts()))
        for offset in range(12):
            self.assertIn(str((SAMPLE_END - timedelta(days=offset)).day), found)

    def test_a_long_period_labels_its_ends(self):
        found = texts(self.build(sample_summary(90), week_facts()))
        self.assertIn("Jul 12", found)
        self.assertIn("Oct 9", found)

    def test_the_footer(self):
        found = texts(self.build(sample_summary(7), week_facts(theme_name="Matte Black", plugins=3, omarchy="3.5.2")))
        self.assertIn("Matte Black theme · 3 plugins · Omarchy 3.5.2", found)
        self.assertIn(card.REPO, found)

    def test_the_footer_leaves_out_what_it_does_not_know(self):
        cases = [
            (dict(plugins=1), "Matte Black theme · 1 plugin · Omarchy 3.5.2"),
            (dict(plugins=0), "Matte Black theme · Omarchy 3.5.2"),
            (dict(theme_name=""), "3 plugins · Omarchy 3.5.2"),
            (dict(omarchy=""), "Matte Black theme · 3 plugins"),
            (dict(theme_name="", plugins=0, omarchy="3.5.2"), "Omarchy 3.5.2"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertIn(expected, texts(self.build(sample_summary(7), week_facts(**changes))))

    def test_a_footer_with_nothing_to_say_is_just_the_credit(self):
        found = texts(self.build(sample_summary(7), week_facts(theme_name="", plugins=0, omarchy="")))
        self.assertFalse([t for t in found if "theme" in t or "plugin" in t or "Omarchy" in t], found)
        self.assertIn(card.REPO, found)

    def test_the_theme_colors_are_what_the_card_is_drawn_in(self):
        light = system.Theme(name="White", background="#ffffff", foreground="#000000", accent="#6e6e6e")
        svg = self.build(sample_summary(7), week_facts(), theme=light)
        palette = card.palette(light)
        self.assertIn('fill="%s"' % palette.background, svg)
        self.assertIn('fill="%s"' % palette.ink, svg)
        self.assertNotIn("#121212", svg)


# ---- render: text widths and the SVG-to-PNG step ----

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class MetricsCase(IsolatedCase):
    """The same checks run on Pango's measurements (when available) and on the estimate."""

    def setUp(self):
        super().setUp()
        self.variants = []
        exact = render.Metrics(system.monospace_family())
        if exact.exact:
            self.variants.append(("pango", exact))
        estimate = render.Metrics("monospace")
        estimate.exact = False
        self.variants.append(("estimate", estimate))

    def each(self, check):
        for kind, metrics in self.variants:
            with self.subTest(metrics=kind):
                check(metrics)


class WidthTests(MetricsCase):
    def test_width_grows_with_the_number_of_characters(self):
        def check(metrics):
            widths = [metrics.width("a" * count, 20) for count in range(1, 12)]
            self.assertEqual(widths, sorted(set(widths)))

        self.each(check)

    def test_width_grows_with_the_size(self):
        def check(metrics):
            widths = [metrics.width("Hello, world", size) for size in (8, 12, 16, 24, 32, 48, 96)]
            self.assertEqual(widths, sorted(set(widths)))

        self.each(check)

    def test_nothing_has_no_width(self):
        self.each(lambda metrics: self.assertEqual(metrics.width("", 40, bold=True, spacing=5), 0.0))

    def test_bold_text_has_a_width_too(self):
        self.each(lambda metrics: self.assertGreater(metrics.width("Bold", 30, bold=True), 0))

    def test_letter_spacing_adds_to_every_character(self):
        def check(metrics):
            plain = metrics.width("OMAWRAPPED", 34)
            self.assertAlmostEqual(metrics.width("OMAWRAPPED", 34, spacing=6) - plain, 60, places=6)

        self.each(check)

    def test_the_family_is_kept(self):
        self.assertEqual(render.Metrics("Some Family").family, "Some Family")

    def test_the_estimate_is_0_6_em_per_character(self):
        metrics = render.Metrics("monospace")
        metrics.exact = False
        self.assertAlmostEqual(metrics.width("abcde", 20), 5 * 0.6 * 20)
        self.assertAlmostEqual(metrics.width("abcde", 20, bold=True), 5 * 0.6 * 20)
        self.assertAlmostEqual(metrics.width("abcde", 20, spacing=2), 5 * 0.6 * 20 + 2 * 5)
        self.assertAlmostEqual(metrics.width("x", 100), 60)
        self.assertEqual(metrics.width("", 20), 0.0)

    def test_wide_characters_count_double_in_the_estimate(self):
        metrics = render.Metrics("monospace")
        metrics.exact = False
        self.assertAlmostEqual(metrics.width("日本", 10), metrics.width("abcd", 10))

    def test_pango_is_used_when_it_is_there(self):
        metrics = render.Metrics(system.monospace_family())
        try:
            import gi  # noqa: F401
            gi.require_version("Pango", "1.0")
            gi.require_version("PangoCairo", "1.0")
            from gi.repository import Pango, PangoCairo  # noqa: F401
        except (ImportError, ValueError):
            self.skipTest("the Pango bindings are not installed")
        self.assertTrue(metrics.exact)


class FitTests(MetricsCase):
    def test_text_that_fits_is_returned_unchanged(self):
        def check(metrics):
            self.assertEqual(metrics.fit("Short", 26, 1000), "Short")
            self.assertEqual(metrics.fit("", 26, 10), "")

        self.each(check)

    def test_text_that_fits_exactly_is_returned_unchanged(self):
        def check(metrics):
            self.assertEqual(metrics.fit("Exactly this", 26, metrics.width("Exactly this", 26)), "Exactly this")
            self.assertEqual(metrics.fit("Bold one", 26, metrics.width("Bold one", 26, True), bold=True), "Bold one")

        self.each(check)

    def test_text_that_does_not_fit_ends_in_an_ellipsis_and_fits(self):
        def check(metrics):
            text = "A very long window class that cannot possibly fit in this space"
            for limit in (60, 120, 300, 500):
                fitted = metrics.fit(text, 26, limit)
                self.assertTrue(fitted.endswith("…"), fitted)
                self.assertLessEqual(metrics.width(fitted, 26), limit + EPS, fitted)
                self.assertTrue(text.startswith(fitted[:-1]), fitted)
                self.assertLess(len(fitted), len(text))

        self.each(check)

    def test_the_ellipsis_text_is_as_long_as_it_can_be(self):
        def check(metrics):
            text = "abcdefghijklmnopqrstuvwxyz" * 3
            fitted = metrics.fit(text, 24, 300)
            longer = text[:len(fitted)] + "…"
            self.assertGreater(metrics.width(longer, 24), 300)

        self.each(check)

    def test_bold_text_is_measured_bold(self):
        def check(metrics):
            text = "A very long window class that cannot possibly fit in this space"
            fitted = metrics.fit(text, 26, 200, bold=True)
            self.assertLessEqual(metrics.width(fitted, 26, bold=True), 200 + EPS)

        self.each(check)

    def test_no_space_is_left_before_the_ellipsis(self):
        metrics = render.Metrics("monospace")
        metrics.exact = False
        # 6px per character at size 10: room for 7 characters, but "Hello W…" is 8.
        self.assertEqual(metrics.fit("Hello World", 10, 45), "Hello…")

    def test_when_not_even_the_ellipsis_fits_it_is_all_that_is_left(self):
        def check(metrics):
            self.assertEqual(metrics.fit("Anything at all", 26, 0), "…")

        self.each(check)


class ShrinkTests(MetricsCase):
    def test_the_given_size_is_kept_when_the_text_fits(self):
        def check(metrics):
            self.assertEqual(metrics.shrink("42m", 132, 640, bold=True), 132)
            exact = metrics.width("fits", 44, True)
            self.assertEqual(metrics.shrink("fits", 44, exact, bold=True), 44)

        self.each(check)

    def test_a_smaller_size_is_returned_when_the_text_does_not_fit(self):
        def check(metrics):
            size = metrics.shrink("1000h 00m", 132, 400, bold=True)
            self.assertLess(size, 132)
            self.assertGreater(size, 0)

        self.each(check)

    def test_the_smaller_size_is_smaller_the_narrower_the_room(self):
        def check(metrics):
            sizes = [metrics.shrink("1000h 00m", 132, room, bold=True) for room in (500, 400, 300, 200, 100)]
            self.assertEqual(sizes, sorted(sizes, reverse=True))

        self.each(check)

    def test_the_text_fits_at_the_returned_size(self):
        texts = ["38h 42m", "100h 00m", "8784h 00m", "1,234,567", "Wednesday", "Wed Sep 30", "x" * 30, "W" * 20]
        for kind, metrics in self.variants:
            with self.subTest(metrics=kind):
                too_wide = []
                for text in texts:
                    for size, room in ((132, 200), (132, 304), (132, 500), (132, 640), (44, 120), (44, 200), (44, 304)):
                        shrunk = metrics.shrink(text, size, room, bold=True)
                        width = metrics.width(text, shrunk, bold=True)
                        self.assertLessEqual(shrunk, size)
                        if width > room + EPS:
                            too_wide.append("%r at size %g in %g px: shrunk to %.2f, which is %g px wide" % (
                                text, size, room, shrunk, width))
                self.assertEqual(too_wide, [])

    def test_the_estimate_fits_exactly(self):
        metrics = render.Metrics("monospace")
        metrics.exact = False
        shrunk = metrics.shrink("a" * 20, 100, 600)
        self.assertAlmostEqual(shrunk, 50)
        self.assertAlmostEqual(metrics.width("a" * 20, shrunk), 600)


class HaveRendererTests(IsolatedCase):
    def test_reflects_whether_rsvg_convert_is_on_the_path(self):
        empty = self.tmp / "empty-path"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assertFalse(render.have_renderer())
        if shutil.which("rsvg-convert"):
            self.assertTrue(render.have_renderer())


class WritePngWithoutRendererTests(IsolatedCase):
    def test_a_missing_rsvg_convert_is_a_render_error_and_writes_nothing(self):
        empty = self.tmp / "empty-path"
        empty.mkdir()
        out = self.tmp / "out"
        out.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            with self.assertRaises(render.RenderError) as caught:
                render.write_png("<svg xmlns='http://www.w3.org/2000/svg' width='1' height='1'/>", out / "card.png")
        self.assertIn("rsvg-convert", str(caught.exception))
        self.assertEqual(os.listdir(out), [])


@unittest.skipUnless(shutil.which("rsvg-convert"), "rsvg-convert is not installed")
class WritePngTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        metrics = render.Metrics("monospace")
        self.svg = card.build(sample_summary(7), card.Facts("Matte Black", 3, "3.5.2", 12, 2), system.Theme(), metrics)
        self.out = self.tmp / "out"
        self.out.mkdir()

    def test_renders_a_1600_by_900_png(self):
        path = self.out / "card.png"
        render.write_png(self.svg, path)
        data = path.read_bytes()
        self.assertEqual(data[:8], PNG_SIGNATURE)
        self.assertEqual(data[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", data[16:24]), (1600, 900))

    def test_leaves_no_partial_file_behind(self):
        render.write_png(self.svg, self.out / "card.png")
        self.assertEqual(os.listdir(self.out), ["card.png"])

    def test_replaces_a_file_that_is_already_there(self):
        path = self.write(self.out / "card.png", "old")
        render.write_png(self.svg, path)
        self.assertEqual(path.read_bytes()[:8], PNG_SIGNATURE)
        self.assertEqual(os.listdir(self.out), ["card.png"])

    def test_malformed_svg_is_a_render_error_and_leaves_nothing(self):
        unfinished = "<svg xmlns='http://www.w3.org/2000/svg'>"
        for bad in ("this is not an svg", "<svg><text>", self.svg[:300], "", unfinished):
            with self.subTest(svg=bad[:30]):
                with self.assertRaises(render.RenderError):
                    render.write_png(bad, self.out / "card.png")
                self.assertEqual(os.listdir(self.out), [])

    def test_a_failed_render_leaves_an_existing_file_as_it_was(self):
        path = self.write(self.out / "card.png", "precious")
        with self.assertRaises(render.RenderError):
            render.write_png("this is not an svg", path)
        self.assertEqual(path.read_text(encoding="utf-8"), "precious")
        self.assertEqual(os.listdir(self.out), ["card.png"])

    def test_a_missing_folder_is_a_render_error(self):
        with self.assertRaises(render.RenderError):
            render.write_png(self.svg, self.out / "no-such-folder" / "card.png")
        self.assertEqual(os.listdir(self.out), [])

    @unittest.skipIf(os.geteuid() == 0, "root can write into any folder")
    def test_a_folder_that_cannot_be_written_is_a_render_error(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(locked.chmod, stat.S_IRWXU)
        with self.assertRaises(render.RenderError):
            render.write_png(self.svg, locked / "card.png")
        self.assertEqual(os.listdir(locked), [])

    def test_a_path_may_have_spaces_and_other_characters(self):
        path = self.out / "my card (1) é.png"
        render.write_png(self.svg, path)
        self.assertEqual(path.read_bytes()[:8], PNG_SIGNATURE)


if __name__ == "__main__":
    unittest.main()
