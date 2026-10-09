"""The omawrapped command: card, stats, status, reset."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import PLUGIN_ID, VERSION, aggregate, card, gitstats, render, store, system


def _say(message: str) -> None:
    """Everything but the result goes to stderr, so stdout can be piped."""
    print(message, file=sys.stderr)


def _shell(method: str):
    """Calls the running sampler over the shell's IPC. None when it cannot be reached."""
    if not shutil.which("omarchy-shell"):
        return None
    env = dict(os.environ)
    env.setdefault("OMARCHY_PATH", "/usr/share/omarchy")
    try:
        done = subprocess.run(
            ["omarchy-shell", PLUGIN_ID, method],
            capture_output=True, text=True, timeout=5, env=env, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _sampler():
    """(status, ours): the running sampler's status, or None, and whether it records to the folder this command reads.

    A shell may be running with its data elsewhere while this command is
    tried out on a copy (XDG_DATA_HOME). That sampler is not ours to flush,
    and least of all to tell to forget.
    """
    try:
        status = json.loads(_shell("status") or "")
    except ValueError:
        status = None
    if not isinstance(status, dict):
        return None, False
    return status, status.get("dataDir") == str(store.data_dir())


def _period(args) -> aggregate.Period:
    return aggregate.last_days(args.days, date.today())


def _repo_dirs(args) -> list:
    if getattr(args, "repos", None):
        return [d for value in args.repos for d in system.split_list(value)]
    return system.split_list(system.settings().get("repoDirs")) or list(gitstats.DEFAULT_DIRS)


def _collect(args):
    """(summary, git stats) for the period the arguments name."""
    # The sampler holds up to a minute in memory; ask it to write that first.
    if _sampler()[1]:
        _shell("flush")
    period = _period(args)
    names = system.AppNames()
    # An app the user has told the sampler to ignore stays off the card for
    # the days recorded before that, too.
    hidden = list(args.exclude or ()) + system.split_list(system.settings().get("ignoreApps"))
    summary = aggregate.summarize(store.load_days(period.start, period.end), period, names.name,
                                  [system.app_key(app) for app in hidden])
    start = datetime.combine(period.start, time.min).astimezone()
    end = datetime.combine(period.end + timedelta(days=1), time.min).astimezone()
    return summary, gitstats.count_commits(_repo_dirs(args), start, end)


# A card needs at least a minute to say anything: times are shown in minutes.
ENOUGH_MS = 60000


def _too_little(summary: aggregate.Summary) -> str:
    """Why there is no card to draw, and what to do about it."""
    period = summary.period
    return (
        "%s was recorded for %s (%s).\n"
        "OmaWrapped counts while its widget is enabled in the bar: omarchy plugin enable %s\n"
        "Data folder: %s" % (
            "Less than a minute" if summary.total_ms else "Nothing",
            "today" if period.length == 1 else "the last %d days" % period.length,
            period.span, PLUGIN_ID, store.data_dir())
    )


# ---- card ----

def _default_output(period: aggregate.Period) -> Path:
    suffix = {7: "", 30: "-month"}.get(period.length, "-%dd" % period.length)
    return system.pictures_dir() / ("omawrapped-%s%s.png" % (period.end.isoformat(), suffix))


def _copy(path: Path, what: str) -> bool:
    if not shutil.which("wl-copy"):
        _say("wl-copy was not found, so nothing was copied.")
        return False
    # wl-copy stays behind to serve the clipboard. It must not inherit a
    # pipe of ours, or reading that pipe would wait for it forever.
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if what == "image":
            kind = "image/svg+xml" if path.suffix.lower() == ".svg" else "image/png"
            with open(path, "rb") as image:
                done = subprocess.run(["wl-copy", "--type", kind], stdin=image, timeout=10, **quiet)
        else:
            done = subprocess.run(["wl-copy", "--", str(path)], stdin=subprocess.DEVNULL, timeout=10, **quiet)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _open(path: Path) -> None:
    if not shutil.which("xdg-open"):
        _say("xdg-open was not found, so the card was not opened.")
        return
    subprocess.Popen(
        ["xdg-open", str(path)], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def cmd_card(args) -> int:
    summary, git = _collect(args)
    if summary.total_ms < ENOUGH_MS:
        _say(_too_little(summary))
        return 1
    if args.theme and not (Path(args.theme).expanduser() / "colors.toml").is_file():
        _say("%s is not a theme folder: it has no colors.toml." % args.theme)
        return 1
    theme = system.theme(Path(args.theme).expanduser() if args.theme else None)
    facts = card.Facts(
        theme_name=theme.name,
        plugins=system.plugin_count(),
        omarchy=system.omarchy_version(),
        # A card is for showing off: no commits, no commit tile.
        commits=git.commits or None,
        repos=git.repos,
    )
    svg = card.build(summary, facts, theme, render.Metrics(system.monospace_family()))
    output = Path(args.output).expanduser() if args.output else _default_output(summary.period)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.suffix.lower() == ".svg":
            output.write_text(svg, encoding="utf-8")
        else:
            render.write_png(svg, output)
    except (OSError, render.RenderError) as error:
        _say(str(error))
        return 1
    output = output.resolve()
    copied = args.copy != "none" and _copy(output, args.copy)
    _say("Saved %s%s" % (output, {"path": " (path copied)", "image": " (image copied)"}[args.copy] if copied else ""))
    print(output)
    if args.open:
        _open(output)
    return 0


# ---- stats ----

def _stats_json(summary, git) -> dict:
    busiest = summary.busiest_day
    return {
        "period": {"start": summary.period.start.isoformat(), "end": summary.period.end.isoformat(),
                   "days": summary.period.length},
        "screen_time_ms": summary.total_ms,
        "active_days": summary.active_days,
        "average_ms_per_active_day": summary.average_ms,
        "busiest_day": {"date": busiest[0].isoformat(), "ms": busiest[1]} if busiest else None,
        "busiest_hour": summary.peak_hour,
        "app_switches": summary.switches,
        "days": [{"date": day.isoformat(), "ms": ms} for day, ms in summary.daily_ms],
        "hours_ms": summary.hours_ms,
        "apps": [{"name": name, "ms": ms} for name, ms in summary.apps],
        "commits": git.commits if git.scanned else None,
        "repos_with_commits": git.repos,
        "repos_scanned": git.scanned,
        "commits_incomplete": git.incomplete,
    }


def cmd_stats(args) -> int:
    summary, git = _collect(args)
    if args.json:
        print(json.dumps(_stats_json(summary, git), indent=2))
        return 0
    period = summary.period
    print("OmaWrapped · %s · %s\n" % (period.label, period.span))
    if summary.total_ms < ENOUGH_MS:
        print(_too_little(summary))
        return 0
    busiest = summary.busiest_day
    rows = [
        ("Screen time", "%s (avg %s per active day, %d of %d days)" % (
            aggregate.duration(summary.total_ms), aggregate.duration(summary.average_ms),
            summary.active_days, period.length)),
        ("Busiest day", "%s · %s" % (busiest[0].strftime("%a %b ") + str(busiest[0].day), aggregate.duration(busiest[1]))),
        ("Busiest hour", aggregate.hour_label(summary.peak_hour) if summary.peak_hour is not None else "unknown"),
        ("App switches", format(summary.switches, ",")),
        ("Commits", "%s in %d of %d repos%s" % (
            format(git.commits, ","), git.repos, git.scanned,
            " (at least: not every repository could be read)" if git.incomplete else "")
            if git.scanned else "no repositories found in " + ", ".join(_repo_dirs(args))),
    ]
    for label, value in rows:
        print("%-13s %s" % (label, value))
    print("\nTop apps")
    for name, ms in summary.apps[:10]:
        print("  %-28.28s %9s  %3d%%" % (name, aggregate.duration(ms), round(100 * ms / summary.total_ms)))
    print("\nBy day")
    most = busiest[1]
    for day, ms in summary.daily_ms:
        print("  %s %2d  %9s  %s" % (day.strftime("%a %b"), day.day, aggregate.duration(ms), "█" * round(24 * ms / most)))
    return 0


# ---- status ----

def cmd_status(args) -> int:
    files = store.day_files()
    size = 0
    for _, path in files:
        try:
            size += path.stat().st_size
        except OSError:
            pass
    print("OmaWrapped %s" % VERSION)
    if files:
        print("Data      %s: %d day%s, %s to %s, %.1f KB" % (
            store.data_dir(), len(files), "" if len(files) == 1 else "s",
            files[0][0].isoformat(), files[-1][0].isoformat(), size / 1024))
    else:
        print("Data      %s: nothing recorded yet" % store.data_dir())
    sampler, ours = _sampler()
    if sampler is None:
        print("Sampler   not running. It runs while the widget is enabled: omarchy plugin enable %s" % PLUGIN_ID)
        ignored = system.split_list(system.settings().get("ignoreApps"))
    elif not ours:
        print("Sampler   running, but recording to %s, not the folder above" % sampler.get("dataDir"))
        ignored = system.split_list(sampler.get("ignoreApps"))
    else:
        state = "counting" if sampler.get("counting") else (
            "paused (session locked)" if sampler.get("locked") else "paused (away)")
        print("Sampler   running, %s; away after %ss without input%s; today %s" % (
            state, sampler.get("idleSeconds"),
            "" if sampler.get("countKeptAwake", True) else " (a video or call does not count)",
            aggregate.duration(sampler.get("todayMs") or 0)))
        ignored = system.split_list(sampler.get("ignoreApps"))
    print("Ignored   %s" % (", ".join(ignored) if ignored else "no apps"))
    metrics = render.Metrics(system.monospace_family())
    print("Renderer  rsvg-convert %s; text measured with %s; font %s" % (
        "found" if render.have_renderer() else "MISSING (package librsvg)",
        "Pango" if metrics.exact else "an estimate (python-gobject not available)", metrics.family))
    print("Clipboard wl-copy %s" % ("found" if shutil.which("wl-copy") else "MISSING (package wl-clipboard)"))
    dirs = _repo_dirs(args)
    repos = len(gitstats.find_repos(dirs))
    print("Git       %s; %d repositor%s under %s" % (
        "found" if shutil.which("git") else "MISSING (package git)",
        repos, "y" if repos == 1 else "ies", ", ".join(dirs)))
    return 0


# ---- reset ----

def cmd_reset(args) -> int:
    files = store.day_files()
    if not args.yes:
        if not sys.stdin.isatty():
            _say("Refusing to delete without confirmation. Run it in a terminal, or pass --yes.")
            return 1
        answer = input("Delete %d day%s of recorded activity in %s? [y/N] " % (
            len(files), "" if len(files) == 1 else "s", store.data_dir()))
        if answer.strip().lower() not in ("y", "yes"):
            _say("Nothing was deleted.")
            return 1
    # The sampler still holds the last minute and would write it back as a
    # new file. It forgets first: whatever it writes before the files go is
    # then deleted with them.
    if _sampler()[1] and _shell("discard") != "ok":
        _say("The sampler did not answer, so it may still write its last minute. Run reset again in a moment.")
    removed = store.reset()
    print("Deleted %d day file%s. Cards already saved to your Pictures folder were left alone." % (
        removed, "" if removed == 1 else "s"))
    return 0


# ---- arguments ----

def _days(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= 366:
        raise argparse.ArgumentTypeError("must be a whole number from 1 to 366")
    return value


def _add_period(parser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--week", dest="days", action="store_const", const=aggregate.PERIODS["week"],
                       help="the last 7 days, today included (default)")
    group.add_argument("--month", dest="days", action="store_const", const=aggregate.PERIODS["month"],
                       help="the last 30 days, today included")
    group.add_argument("--days", dest="days", type=_days, metavar="N", help="the last N days, today included")
    parser.set_defaults(days=aggregate.PERIODS["week"])
    parser.add_argument("--exclude", action="append", metavar="APP",
                        help="leave an app out of the app list (name or window class; repeatable)")
    parser.add_argument("--repos", action="append", metavar="DIR",
                        help="folder to look for git repositories in (repeatable; default: the widget's "
                             "setting, else ~/projects and ~/code)")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="omawrapped",
        description="A recap card of your week or month on Omarchy. Everything stays on this machine.",
    )
    root.add_argument("--version", action="version", version="omawrapped " + VERSION)
    commands = root.add_subparsers(dest="command", metavar="<command>")

    make = commands.add_parser("card", help="render the recap card as a PNG",
                               description="Render the recap card, save it to your Pictures folder and copy its path.")
    _add_period(make)
    make.add_argument("-o", "--output", metavar="FILE",
                      help="where to save it (.png, or .svg for the drawing itself; "
                           "default: ~/Pictures/omawrapped-<date>.png)")
    make.add_argument("--copy", choices=("path", "image", "none"), default="path",
                      help="what goes to the clipboard: the file's path (default), the image itself, or nothing")
    make.add_argument("--open", action="store_true", help="open the card when it is done")
    make.add_argument("--theme", metavar="DIR", help="take the colours from this theme folder instead of the active theme")
    make.set_defaults(run=cmd_card)

    stats = commands.add_parser("stats", help="print the numbers behind the card",
                                description="Print the numbers behind the card.")
    _add_period(stats)
    stats.add_argument("--json", action="store_true", help="print JSON")
    stats.set_defaults(run=cmd_stats)

    status = commands.add_parser("status", help="show what is stored, whether the sampler runs, and what is installed",
                                 description="Show what is stored, whether the sampler runs, and what is installed.")
    status.set_defaults(run=cmd_status)

    reset = commands.add_parser("reset", help="delete everything OmaWrapped has recorded",
                                description="Delete every recorded day. Saved cards are not touched.")
    reset.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    reset.set_defaults(run=cmd_reset)
    return root


def main(argv=None) -> int:
    # `omawrapped stats | head` closes the pipe early; end quietly like any
    # other command instead of printing a traceback.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    root = parser()
    args = root.parse_args(argv)
    if not args.command:
        root.print_help()
        return 0
    try:
        return args.run(args)
    except KeyboardInterrupt:
        return 130
