#!/usr/bin/python3
"""Writes a believable stretch of recorded days, for trying the card out.

    tests/make_sample.py TARGET [--days 30] [--end YYYY-MM-DD] [--repos]

creates TARGET/data/omawrapped/days/*.json in the format the sampler writes
and, with --repos, a few small git repositories under TARGET/repos with
commits spread over the same days. The numbers are made up but fixed: the
same arguments give the same files. Then:

    XDG_DATA_HOME=TARGET/data bin/omawrapped card --repos TARGET/repos -o card.png
"""

import argparse
import json
import os
import random
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path

# (window class, share of the focused time)
APPS = [
    ("com.mitchellh.ghostty", 0.36),
    ("chromium", 0.24),
    ("dev.zed.Zed", 0.15),
    ("slack", 0.08),
    ("obsidian", 0.06),
    ("spotify", 0.04),
    ("org.gnome.Nautilus", 0.03),
    ("signal", 0.02),
]
# How likely each hour of the day is to see work: a working day and a late evening.
HOURS = [1, 0, 0, 0, 0, 0, 0, 2, 6, 14, 18, 17, 9, 12, 16, 15, 12, 8, 4, 5, 9, 13, 11, 4]
REPOS = {"omawrapped": 0.5, "dotfiles": 0.2, "api-server": 0.3}


def day_file(day: date, rng: random.Random) -> dict:
    weekend = day.weekday() >= 5
    active = int(rng.uniform(1.0, 3.5) * 3600000) if weekend else int(rng.uniform(5.0, 9.5) * 3600000)
    weights = [w * rng.uniform(0.6, 1.4) for w in HOURS]
    hours = [int(active * w / sum(weights)) for w in weights]
    # An hour holds an hour; what does not fit is simply not there.
    hours = [min(h, 3600000) for h in hours]
    active = sum(hours)
    focused = int(active * 0.97)
    shares = [share * rng.uniform(0.7, 1.3) for _, share in APPS]
    apps = {app: int(focused * share / sum(shares)) for (app, _), share in zip(APPS, shares)}
    return {
        "version": 1,
        "date": day.isoformat(),
        "active_ms": active,
        "hours_ms": hours,
        "apps_ms": apps,
        "switches": int(active / 3600000 * rng.uniform(18, 34)),
    }


def write_days(target: Path, days: int, end: date, rng: random.Random) -> None:
    folder = target / "data" / "omawrapped" / "days"
    folder.mkdir(parents=True, exist_ok=True)
    for offset in range(days):
        day = end - timedelta(days=offset)
        (folder / (day.isoformat() + ".json")).write_text(json.dumps(day_file(day, rng)) + "\n", encoding="utf-8")


def write_repos(target: Path, days: int, end: date, rng: random.Random) -> None:
    email = "sample@example.invalid"
    for name, share in REPOS.items():
        repo = target / "repos" / name
        repo.mkdir(parents=True, exist_ok=True)
        base = ["git", "-C", str(repo), "-c", "user.name=Sample", "-c", "user.email=" + email, "-c", "commit.gpgsign=false"]
        subprocess.run(base + ["init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", email], check=True)
        for offset in reversed(range(days)):
            day = end - timedelta(days=offset)
            for _ in range(round(rng.uniform(0, 14) * share * (0.3 if day.weekday() >= 5 else 1))):
                # Never in the future: a real repository cannot hold tomorrow's commits.
                moment = min(datetime.combine(day, time(rng.randint(9, 22), rng.randint(0, 59))), datetime.now())
                stamp = moment.astimezone().isoformat()
                subprocess.run(
                    base + ["commit", "-q", "--allow-empty", "-m", "Sample commit"], check=True,
                    env={**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--repos", action="store_true", help="also create sample git repositories")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    write_days(args.target, args.days, args.end, rng)
    if args.repos:
        write_repos(args.target, args.days, args.end, rng)


if __name__ == "__main__":
    main()
