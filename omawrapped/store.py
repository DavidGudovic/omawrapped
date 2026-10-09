"""The day files the sampler writes, read back.

One file per local calendar day, ~/.local/share/omawrapped/days/YYYY-MM-DD.json,
written only by Service.qml (Tracker.js defines the shape). This module reads
them and deletes them; it never writes one.
"""

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

FORMAT_VERSION = 1
DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


@dataclass
class Day:
    date: date
    active_ms: int = 0
    hours_ms: list = field(default_factory=lambda: [0] * 24)
    apps_ms: dict = field(default_factory=dict)
    switches: int = 0


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "")
    root = Path(base) if base.startswith("/") else Path.home() / ".local" / "share"
    return root / "omawrapped"


def days_dir() -> Path:
    return data_dir() / "days"


def _count(value) -> int:
    """A stored counter: a finite number above zero, else 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value) or value <= 0:
        return 0
    return int(value)


def parse_day(text: str, day: date):
    """The Day in a file's text, or None when it is not a day file for that date."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    version = raw.get("version") if isinstance(raw, dict) else None
    # True equals 1 in Python; it is still not a version.
    if isinstance(version, bool) or version != FORMAT_VERSION or raw.get("date") != day.isoformat():
        return None
    hours = raw.get("hours_ms")
    hours = hours if isinstance(hours, list) else []
    apps = raw.get("apps_ms")
    apps = apps if isinstance(apps, dict) else {}
    return Day(
        date=day,
        active_ms=_count(raw.get("active_ms")),
        hours_ms=[_count(hours[h]) if h < len(hours) else 0 for h in range(24)],
        apps_ms={app: _count(ms) for app, ms in apps.items() if app and _count(ms) > 0},
        switches=_count(raw.get("switches")),
    )


def day_files() -> list:
    """(date, path) of every day file, oldest first."""
    found = []
    try:
        names = os.listdir(days_dir())
    except OSError:
        return found
    for name in names:
        match = DAY_FILE.match(name)
        if not match:
            continue
        try:
            found.append((date.fromisoformat(match.group(1)), days_dir() / name))
        except ValueError:
            continue
    return sorted(found)


def load_days(start: date, end: date) -> dict:
    """{date: Day} for the recorded days from start to end, both included."""
    days = {}
    for day, path in day_files():
        if not start <= day <= end:
            continue
        try:
            parsed = parse_day(path.read_text(encoding="utf-8"), day)
        except (OSError, UnicodeDecodeError):
            parsed = None
        if parsed is not None:
            days[day] = parsed
    return days


def reset() -> int:
    """Deletes everything OmaWrapped recorded. Returns how many day files went."""
    removed = 0
    directory = days_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    for name in names:
        # Everything in days/ goes: the day files, and whatever an
        # interrupted write of one may have left beside them.
        try:
            (directory / name).unlink()
        except OSError:
            continue
        if DAY_FILE.match(name):
            removed += 1
    for path in (directory, data_dir()):
        try:
            path.rmdir()
        except OSError:
            pass
    return removed
