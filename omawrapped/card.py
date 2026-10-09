"""The card, drawn as an SVG.

A 1600x900 canvas, twice the size X shows it at. Every colour is derived
from the active Omarchy theme and every text is set in the system's
monospace font, so the card looks like the desktop it describes.

Layout, left to right: the total and how it spread over days and hours;
then the apps it went to and four headline numbers.
"""

import math
import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

from .aggregate import Summary, duration, hour_label
from .render import Metrics

WIDTH, HEIGHT = 1600, 900
MARGIN = 72
LEFT = (MARGIN, 712)
RIGHT = (800, WIDTH - MARGIN)
REPO = "github.com/DavidGudovic/omawrapped"


@dataclass
class Facts:
    """What the card says besides the recorded time."""

    theme_name: str = ""
    plugins: int = 0
    omarchy: str = ""
    # None leaves the commit tile out.
    commits: int = None
    repos: int = 0


# ---- Colour ----

def _rgb(color: str) -> tuple:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def mix(a: str, b: str, t: float) -> str:
    """a moved the fraction t of the way to b."""
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(_rgb(a), _rgb(b)))


def _luminance(color: str) -> float:
    def linear(channel):
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colours, 1 to 21."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _legible(color: str, background: str, target: float, toward: str) -> str:
    """color, moved toward `toward` just far enough to reach the contrast target."""
    for step in range(21):
        candidate = mix(color, toward, step / 20)
        if contrast(candidate, background) >= target:
            return candidate
    return toward


@dataclass
class Palette:
    background: str
    ink: str        # headline text
    secondary: str  # values and labels
    muted: str      # captions and axis labels
    mark: str       # the bars that carry the point
    quiet: str      # the bars that give it context
    panel: str      # tile fill
    rule: str       # baselines


def palette(theme) -> Palette:
    """The card's colours for a theme, each checked against its background.

    A theme may pair any colours; the contrast floors keep the text and the
    bars readable on all of them (4.5:1 for text, 4:1 for captions, 3:1 for
    the bars that matter).
    """
    bg = theme.background
    strongest = "#ffffff" if _luminance(bg) < 0.18 else "#000000"
    ink = _legible(theme.foreground, bg, 4.5, strongest)
    return Palette(
        background=bg,
        ink=ink,
        secondary=_legible(mix(ink, bg, 0.22), bg, 4.5, ink),
        muted=_legible(mix(ink, bg, 0.42), bg, 4.0, ink),
        mark=theme.accent if contrast(theme.accent, bg) >= 3.0 else ink,
        quiet=_legible(mix(ink, bg, 0.74), bg, 1.7, ink),
        panel=mix(bg, ink, 0.06),
        rule=mix(bg, ink, 0.18),
    )


# ---- Drawing ----

# What XML 1.0 cannot hold, plus the line breaks that have no place in a label.
UNPRINTABLE = re.compile("[^\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _xml(text: str) -> str:
    """text as it may stand inside an SVG element."""
    return escape(UNPRINTABLE.sub("", text))


class Canvas:
    def __init__(self, metrics: Metrics, colors: Palette):
        self.metrics = metrics
        self.colors = colors
        self.parts = []

    def rect(self, x, y, w, h, fill):
        self.parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>' % (x, y, w, h, fill))

    def text(self, x, y, text, size, fill, bold=False, anchor="start", spacing=0):
        self.parts.append(
            '<text x="%.1f" y="%.1f" font-size="%.1f" fill="%s"%s%s%s>%s</text>' % (
                x, y, size, fill,
                ' font-weight="700"' if bold else "",
                ' text-anchor="%s"' % anchor if anchor != "start" else "",
                ' letter-spacing="%g"' % spacing if spacing else "",
                _xml(text),
            )
        )

    def spans(self, x, y, parts, size, bold=False):
        """One line of text in several colours: parts is [(text, fill), ...]."""
        # Without xml:space a space at the edge of a tspan is dropped.
        self.parts.append(
            '<text x="%.1f" y="%.1f" font-size="%.1f"%s xml:space="preserve">%s</text>' % (
                x, y, size, ' font-weight="700"' if bold else "",
                "".join('<tspan fill="%s">%s</tspan>' % (fill, _xml(text)) for text, fill in parts),
            )
        )

    def column(self, x, base, w, h, fill, radius=5):
        """A bar standing on `base`: square where it stands, rounded on top."""
        r = min(radius, h, w / 2)
        top = base - h
        self.parts.append(
            '<path d="M%.1f %.1f V%.1f Q%.1f %.1f %.1f %.1f H%.1f Q%.1f %.1f %.1f %.1f V%.1f Z" fill="%s"/>' % (
                x, base, top + r, x, top, x + r, top, x + w - r, x + w, top, x + w, top + r, base, fill)
        )

    def bar(self, x, y, w, h, fill):
        """A bar growing right from x: square at x, rounded at its end."""
        r = min(h / 2, w)
        self.parts.append(
            '<path d="M%.1f %.1f H%.1f Q%.1f %.1f %.1f %.1f Q%.1f %.1f %.1f %.1f H%.1f Z" fill="%s"/>' % (
                x, y, x + w - r, x + w, y, x + w, y + h / 2, x + w, y + h, x + w - r, y + h, x, fill)
        )

    def caption(self, x, y, text):
        self.text(x, y, text, 18, self.colors.muted, spacing=3)

    def svg(self) -> str:
        family = "'%s', monospace" % self.metrics.family if self.metrics.family != "monospace" else "monospace"
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" font-family="%s">\n%s\n</svg>\n'
            % (WIDTH, HEIGHT, WIDTH, HEIGHT, family, "\n".join(self.parts))
        )


def _plural(count: int, word: str) -> str:
    return "%s %s%s" % (format(count, ","), word, "" if count == 1 else "s")


def _columns(c: Canvas, values, x0, x1, top, base, peak, bar_width):
    """A row of columns over [x0, x1] standing on `base`; `peak` is the index drawn loud.

    Returns the centre x of every slot.
    """
    slot = (x1 - x0) / len(values)
    highest = max(values) or 1
    centres = []
    for index, value in enumerate(values):
        centre = x0 + slot * (index + 0.5)
        centres.append(centre)
        if value <= 0:
            continue
        # Anything recorded shows, however little.
        height = max(3, value / highest * (base - top))
        c.column(centre - bar_width / 2, base, bar_width, height, c.colors.mark if index == peak else c.colors.quiet)
    c.rect(x0, base, x1 - x0, 2, c.colors.rule)
    return centres


def _mark(c: Canvas, x, y, size):
    """The plugin's icon, as in the bar: a card with a chart in it. Drawn, so that it needs no icon font."""
    unit = size / 18
    c.parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%.1f" fill="%s"/>' % (
        x, y, size, size, 2 * unit, c.colors.mark))
    # Three bars standing on one line, cut out of the card.
    for left, height in ((4, 7), (8, 10), (12, 4)):
        c.rect(x + left * unit, y + (14 - height) * unit, 2 * unit, height * unit, c.colors.background)


def _header(c: Canvas, summary: Summary):
    _mark(c, MARGIN, 82, 26)
    c.text(MARGIN + 44, 108, "OMAWRAPPED", 34, c.colors.ink, bold=True, spacing=6)
    c.text(WIDTH - MARGIN, 106, "%s · %s" % (summary.period.label, summary.period.span), 26, c.colors.secondary, anchor="end")


def _hero(c: Canvas, summary: Summary):
    x0, x1 = LEFT
    total = duration(summary.total_ms)
    size = c.metrics.shrink(total, 132, x1 - x0, bold=True)
    # The figures carry the number; the units step back.
    c.spans(x0, 268, [(piece, c.colors.ink if piece.isdigit() else c.colors.muted)
                      for piece in re.findall(r"\d+|\D+", total)], size, bold=True)
    c.text(x0, 316, "screen time", 30, c.colors.secondary)
    if summary.period.length > 1 and summary.active_days:
        line = "avg %s per active day · %d of %d days" % (
            duration(summary.average_ms), summary.active_days, summary.period.length)
        c.text(x0, 354, c.metrics.fit(line, 22, x1 - x0), 22, c.colors.muted)


def _day_chart(c: Canvas, summary: Summary):
    x0, x1 = LEFT
    days = summary.daily_ms
    count = len(days)
    busiest = summary.busiest_day
    peak = [d for d, _ in days].index(busiest[0]) if busiest else -1
    slot = (x1 - x0) / count
    c.caption(x0, 424, "BY DAY")
    # The top 26px stay free for the label over the tallest column.
    centres = _columns(c, [ms for _, ms in days], x0, x1, 466, 576, peak, min(44, slot * 0.62))
    if busiest:
        label = duration(busiest[1])
        half = c.metrics.width(label, 20) / 2
        c.text(min(max(centres[peak], x0 + half), x1 - half), 454, label, 20, c.colors.secondary, anchor="middle")

    def tick(index, text, anchor="middle"):
        x = {"start": x0, "end": x1}.get(anchor, centres[index])
        c.text(x, 604, text, 18, c.colors.ink if index == peak else c.colors.muted, anchor=anchor)

    if count <= 7:
        for index, (day, _) in enumerate(days):
            tick(index, day.strftime("%a"))
    elif count <= 16:
        for index, (day, _) in enumerate(days):
            tick(index, str(day.day))
    else:
        # Too many days to name each: the ends, and between them one every
        # week or every few weeks, as many as fit without touching.
        def date_label(index):
            return "%s %d" % (days[index][0].strftime("%b"), days[index][0].day)

        room = c.metrics.width("Sep 30", 18) + 24
        step = 7 * max(1, math.ceil(room / slot / 7))
        tick(0, date_label(0), "start")
        tick(count - 1, date_label(count - 1), "end")
        for index in range(step, count, step):
            # A label is centred on its day; the two at the ends are not.
            if centres[index] - x0 >= 1.5 * room and x1 - centres[index] >= 1.5 * room:
                tick(index, date_label(index))


def _hour_chart(c: Canvas, summary: Summary):
    x0, x1 = LEFT
    peak = summary.peak_hour
    c.caption(x0, 668, "BY HOUR")
    centres = _columns(c, summary.hours_ms, x0, x1, 686, 770, -1 if peak is None else peak, 18)
    for hour in (0, 6, 12, 18):
        c.text(centres[hour], 798, "%02d" % hour, 18, c.colors.muted, anchor="middle")


def _top_apps(c: Canvas, summary: Summary):
    x0, x1 = RIGHT
    c.caption(x0, 190, "TOP APPS")
    rows = summary.apps[:5]
    if not rows:
        c.text(x0, 240, "No app time recorded in this period.", 24, c.colors.muted)
        return
    longest = rows[0][1]
    for index, (name, ms) in enumerate(rows):
        top = 214 + index * 66
        value = duration(ms)
        room = x1 - x0 - c.metrics.width(value, 24) - 28
        c.text(x0, top + 26, c.metrics.fit(name, 26, room), 26, c.colors.ink)
        c.text(x1, top + 26, value, 24, c.colors.secondary, anchor="end")
        c.bar(x0, top + 40, max(6, ms / longest * (x1 - x0)), 10, c.colors.mark)


def _tiles(c: Canvas, summary: Summary, facts: Facts):
    """Four headline numbers, each a value over its label."""
    tiles = []
    if facts.commits is not None:
        tiles.append((format(facts.commits, ","), "%s in %s" % (
            "commit" if facts.commits == 1 else "commits", _plural(facts.repos, "repo"))))
    busiest = summary.busiest_day
    if busiest:
        day = busiest[0]
        # Inside one week the weekday says which day; beyond it, the date must.
        name = day.strftime("%A") if summary.period.length <= 7 else "%s %s %d" % (
            day.strftime("%a"), day.strftime("%b"), day.day)
        tiles.append((name, "busiest day · %s" % duration(busiest[1])))
    if summary.peak_hour is not None:
        tiles.append((hour_label(summary.peak_hour), "busiest hour"))
    if summary.switches:
        tiles.append((format(summary.switches, ","), "app switch" if summary.switches == 1 else "app switches"))
    if len(tiles) < 4:
        tiles.append(("%d / %d" % (summary.active_days, summary.period.length), "days active"))

    x0, x1 = RIGHT
    gap = 24
    width = (x1 - x0 - gap) / 2
    for index, (value, label) in enumerate(tiles[:4]):
        x = x0 + (index % 2) * (width + gap)
        y = 588 + (index // 2) * 124
        c.rect(x, y, width, 100, c.colors.panel)
        room = width - 48
        c.text(x + 24, y + 48, value, c.metrics.shrink(value, 44, room, bold=True), c.colors.ink, bold=True)
        c.text(x + 24, y + 80, c.metrics.fit(label, 20, room), 20, c.colors.muted)


def _footer(c: Canvas, facts: Facts):
    parts = []
    if facts.theme_name:
        parts.append("%s theme" % facts.theme_name)
    if facts.plugins:
        parts.append(_plural(facts.plugins, "plugin"))
    if facts.omarchy:
        parts.append("Omarchy %s" % facts.omarchy)
    credit = c.metrics.width(REPO, 20)
    c.text(WIDTH - MARGIN, 850, REPO, 20, c.colors.muted, anchor="end")
    c.text(MARGIN, 850, c.metrics.fit(" · ".join(parts), 20, WIDTH - 2 * MARGIN - credit - 40), 20, c.colors.muted)


def build(summary: Summary, facts: Facts, theme, metrics: Metrics) -> str:
    """The whole card as SVG text."""
    colors = palette(theme)
    c = Canvas(metrics, colors)
    c.rect(0, 0, WIDTH, HEIGHT, colors.background)
    # A border in the accent colour, the way Hyprland frames the focused window.
    c.parts.append(
        '<rect x="2" y="2" width="%d" height="%d" fill="none" stroke="%s" stroke-width="4"/>'
        % (WIDTH - 4, HEIGHT - 4, colors.mark)
    )
    _header(c, summary)
    _hero(c, summary)
    _day_chart(c, summary)
    _hour_chart(c, summary)
    _top_apps(c, summary)
    _tiles(c, summary, facts)
    _footer(c, facts)
    return c.svg()
