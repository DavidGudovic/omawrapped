"""Turns recorded days into the numbers on the card."""

from dataclasses import dataclass, field
from datetime import date, timedelta

PERIODS = {"week": 7, "month": 30}


@dataclass
class Period:
    start: date
    end: date

    @property
    def length(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def dates(self) -> list:
        return [self.start + timedelta(days=i) for i in range(self.length)]

    @property
    def label(self) -> str:
        return "Last %d days" % self.length if self.length != 1 else "Today"

    @property
    def span(self) -> str:
        """"Oct 3 – 9, 2026", "Sep 28 – Oct 4, 2026" or "Dec 29, 2025 – Jan 4, 2026"."""
        a, b = self.start, self.end
        if a == b:
            return "%s %d, %d" % (a.strftime("%b"), a.day, a.year)
        if a.year != b.year:
            return "%s %d, %d – %s %d, %d" % (a.strftime("%b"), a.day, a.year, b.strftime("%b"), b.day, b.year)
        if a.month != b.month:
            return "%s %d – %s %d, %d" % (a.strftime("%b"), a.day, b.strftime("%b"), b.day, b.year)
        return "%s %d – %d, %d" % (a.strftime("%b"), a.day, b.day, b.year)


def last_days(length: int, today: date) -> Period:
    """The `length` days ending today, today included."""
    return Period(today - timedelta(days=length - 1), today)


@dataclass
class Summary:
    period: Period
    total_ms: int = 0
    # Active time of every date in the period, recorded or not.
    daily_ms: list = field(default_factory=list)
    hours_ms: list = field(default_factory=lambda: [0] * 24)
    # (name, ms), most time first.
    apps: list = field(default_factory=list)
    switches: int = 0

    @property
    def active_days(self) -> int:
        return sum(1 for _, ms in self.daily_ms if ms > 0)

    @property
    def average_ms(self) -> int:
        """Per day that has any time at all; an empty day is not a short one."""
        return self.total_ms // self.active_days if self.active_days else 0

    @property
    def busiest_day(self):
        """(date, ms) of the day with the most time, the earliest on a tie; None when empty."""
        best = max(self.daily_ms, key=lambda item: item[1], default=None)
        return best if best and best[1] > 0 else None

    @property
    def peak_hour(self):
        """The hour of day (0-23) with the most time, the earliest on a tie; None when empty."""
        top = max(self.hours_ms)
        return self.hours_ms.index(top) if top > 0 else None


def summarize(days: dict, period: Period, name_of=None, exclude=()) -> Summary:
    """The summary of `days` ({date: Day}) over `period`.

    name_of maps an app id to the name it is shown under; ids that share a
    name are added up. exclude lists ids or names (any case) that are left
    out of the app list. Their time stays in the totals: it was screen time.
    """
    name_of = name_of or (lambda app: app)
    hidden = {item.casefold() for item in exclude}
    summary = Summary(period=period)
    apps = {}
    for day_date in period.dates:
        day = days.get(day_date)
        summary.daily_ms.append((day_date, day.active_ms if day else 0))
        if day is None:
            continue
        summary.total_ms += day.active_ms
        summary.switches += day.switches
        for hour in range(24):
            summary.hours_ms[hour] += day.hours_ms[hour]
        for app, ms in day.apps_ms.items():
            name = name_of(app)
            if app.casefold() in hidden or name.casefold() in hidden:
                continue
            apps[name] = apps.get(name, 0) + ms
    summary.apps = sorted(apps.items(), key=lambda item: (-item[1], item[0].casefold()))
    return summary


def duration(ms: int) -> str:
    """"38h 42m", "42m", "0m". Minutes are cut, not rounded, like the bar."""
    minutes = max(0, int(ms)) // 60000
    hours, rest = divmod(minutes, 60)
    return "%dh %02dm" % (hours, rest) if hours else "%dm" % rest


def hour_label(hour: int) -> str:
    return "%02d:00" % hour
