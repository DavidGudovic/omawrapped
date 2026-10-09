"""Helpers shared by the OmaWrapped tests.

Import this module before anything else in a test module. It switches
bytecode off (no __pycache__ may appear in the repository), puts the
repository on sys.path, and provides the isolation every test depends on:
HOME, the XDG folders and OMARCHY_PATH all point into a throwaway directory,
git reads no configuration of the machine, and the CLI is run with stub
versions of the three programs that would touch the live desktop.
"""

import sys

sys.dont_write_bytecode = True

# flake8: noqa: E402
# ruff: noqa: E402
import itertools
import json
import os
import random
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
LAUNCHER = REPO / "bin" / "omawrapped"
for _folder in (str(TESTS), str(REPO)):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

import make_sample
from omawrapped import PLUGIN_ID, aggregate, store, system

# The Pango bindings warn about a deprecated GLib call when first imported. That is not what is
# being tested, so it is imported once here, quietly, and the test output stays readable.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        import gi

        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo  # noqa: F401
    except Exception:  # no bindings: render.Metrics falls back to its estimate
        pass

ME = "me@example.invalid"
OTHER = "someone.else@example.invalid"
# The last day of the fixture periods: fixed, so no test depends on the date it runs.
SAMPLE_END = date(2026, 10, 9)


# ---- Isolation ----

def isolated_env(root: Path) -> dict:
    """The variables that send every per-user lookup of the code under test into root."""
    home = root / "home"
    return {
        "HOME": str(home),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_RUNTIME_DIR": str(root / "run"),
        "XDG_DATA_DIRS": str(root / "share"),
        "XDG_PICTURES_DIR": str(root / "Pictures"),
        "OMARCHY_PATH": str(root / "omarchy"),
        # git reads no configuration but the repository's own.
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }


class IsolatedCase(unittest.TestCase):
    """A test that cannot see the real home directory.

    self.tmp is a fresh temporary directory that holds the whole fake
    machine: self.home, the data folder, the Omarchy tree and so on.
    os.environ is patched for the length of the test and restored after it.
    """

    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory(prefix="omawrapped-test-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        env = isolated_env(self.tmp)
        self.home = self.tmp / "home"
        self.data_home = self.tmp / "data"
        self.days = self.data_home / "omawrapped" / "days"
        self.config = self.home / ".config"
        self.omarchy = self.tmp / "omarchy"
        self.pictures = self.tmp / "Pictures"
        for folder in (self.home, self.tmp / "run"):
            folder.mkdir()
        (self.tmp / "gitconfig").write_text("", encoding="utf-8")

        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Nothing git-related from the machine's environment may leak in.
        for name in [name for name in os.environ if name.startswith("GIT_") and name not in env]:
            del os.environ[name]
        # If the patch did not take, stop before a test can touch real data.
        self.assertEqual(Path.home(), self.home)
        self.assertEqual(os.environ["XDG_DATA_HOME"], str(self.data_home))

    def write(self, path, text="") -> Path:
        """Writes a text file (a path below self.tmp, or absolute), making its folders."""
        path = Path(path)
        if not path.is_absolute():
            path = self.tmp / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_day(self, day: date, **fields) -> Path:
        """A valid day file in the data folder; fields as for day_json."""
        return self.write(self.days / (day.isoformat() + ".json"), json.dumps(day_json(day, **fields)))


def day_json(day: date, active_ms=0, hours_ms=None, apps_ms=None, switches=0) -> dict:
    """What the sampler writes for a day."""
    return {
        "version": 1,
        "date": day.isoformat(),
        "active_ms": active_ms,
        "hours_ms": [0] * 24 if hours_ms is None else hours_ms,
        "apps_ms": {} if apps_ms is None else apps_ms,
        "switches": switches,
    }


# ---- Sample data ----

def sample_days(end: date = SAMPLE_END, count: int = 7, seed: int = 7) -> dict:
    """{date: store.Day} for `count` days ending on `end`, from make_sample's generator."""
    rng = random.Random(seed)
    days = {}
    for offset in range(count):
        day = end - timedelta(days=offset)
        parsed = store.parse_day(json.dumps(make_sample.day_file(day, rng)), day)
        assert parsed is not None
        days[day] = parsed
    return days


def sample_summary(length: int = 7, end: date = SAMPLE_END, days=None, name_of=None, exclude=()):
    """The summary of the last `length` days ending on `end`."""
    days = sample_days(end, length) if days is None else days
    name_of = name_of or system.AppNames([]).name
    return aggregate.summarize(days, aggregate.last_days(length, end), name_of, exclude)


def clock(ms: int) -> str:
    """A duration the way the card and the CLI write it (2h 05m, 42m), worked out here again."""
    minutes = ms // 60000
    return "%dh %02dm" % divmod(minutes, 60) if minutes >= 60 else "%dm" % minutes


# ---- git ----

def git(repo, *args, env=None, check=True) -> str:
    """Runs git in repo and returns its output (empty when it fails and check is off).

    Only valid in an IsolatedCase.
    """
    isolated = "omawrapped-test-" in os.environ.get("GIT_CONFIG_GLOBAL", "")
    if os.environ.get("GIT_CONFIG_NOSYSTEM") != "1" or not isolated:
        raise RuntimeError("git helpers need the isolated environment")
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30, env={**os.environ, **(env or {})},
    )
    if done.returncode != 0:
        if check:
            raise AssertionError("git %s failed: %s" % (" ".join(args), done.stderr.strip()))
        return ""
    return done.stdout


def init_repo(path, email=ME) -> Path:
    """A new repository on branch main. email=None leaves user.email unset everywhere."""
    if not shutil.which("git"):
        raise unittest.SkipTest("git is not installed")
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    if email is not None:
        git(path, "config", "user.email", email)
        git(path, "config", "user.name", "Tester")
    return path


def stamp(moment: datetime) -> str:
    """A moment in git's raw date format, to the second."""
    return "%d +0000" % int(moment.timestamp())


def identity(authored: datetime, email: str = ME, committed: datetime = None) -> dict:
    """The environment that makes git author and commit as `email` at fixed moments."""
    return {
        "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": stamp(authored),
        "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": stamp(committed or authored),
    }


_commits = itertools.count(1)


def commit(repo, authored: datetime, email: str = ME, committed: datetime = None, message=None) -> str:
    """An empty commit authored at a moment (committed then too, unless told otherwise). Returns its hash.

    Every commit gets its own message: two empty commits with the same author, moment and message would
    be the same commit, and git would give them one hash even in two repositories.
    """
    message = message or "work %d" % next(_commits)
    git(repo, "commit", "-q", "--allow-empty", "-m", message, env=identity(authored, email, committed))
    return git(repo, "rev-parse", "HEAD").strip()


# ---- The CLI and the programs it starts ----

STUBBED = ("omarchy-shell", "wl-copy", "xdg-open")


class Stubs:
    """Stand-ins for the programs the CLI starts, which must never reach the real ones.

    Each is a /bin/sh script that appends its arguments, one line per call, to
    its own log and exits 0. omarchy-shell can also be given a canned reply to
    `status`.
    """

    def __init__(self, root: Path):
        self.dir = root / "stubs"
        self.logs = root / "stublogs"
        self.reply = root / "shell-status-reply"
        self.dir.mkdir()
        self.logs.mkdir()
        for name in STUBBED:
            script = self.dir / name
            lines = ["#!/bin/sh", "printf '%%s\\n' \"$*\" >> %s" % shlex.quote(str(self.logs / (name + ".log")))]
            if name == "omarchy-shell":
                lines.append('[ "$2" = status ] && [ -f %s ] && cat %s' % (shlex.quote(str(self.reply)),
                                                                          shlex.quote(str(self.reply))))
            lines.append("exit 0")
            script.write_text("\n".join(lines) + "\n", encoding="utf-8")
            script.chmod(0o755)

    @property
    def path(self) -> str:
        """The PATH the CLI runs with: the stubs first, then the system's programs."""
        return "%s:/usr/bin" % self.dir

    def calls(self, name: str) -> list:
        """The arguments of every call so far to a stubbed program, one string per call."""
        try:
            return (self.logs / (name + ".log")).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def wait_for_call(self, name: str, timeout: float = 5.0) -> list:
        """calls(), once there is at least one (the program may be started detached)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.calls(name)
            if found:
                return found
            time.sleep(0.05)
        return self.calls(name)

    def reply_to_status(self, text: str) -> None:
        self.reply.write_text(text, encoding="utf-8")


def cli_env(root: Path, stubs: Stubs) -> dict:
    """The whole environment of a CLI run: nothing is inherited from the machine."""
    env = isolated_env(root)
    env.update({"PATH": stubs.path, "LANG": "C.UTF-8", "PYTHONUTF8": "1"})
    if "TZ" in os.environ:
        env["TZ"] = os.environ["TZ"]
    return env


def bytecode_dirs() -> list:
    """Any __pycache__ below the plugin's own code, where none may ever appear."""
    return sorted(str(path) for folder in (REPO / "omawrapped", REPO / "bin") for path in folder.rglob("__pycache__"))


__all__ = [
    "IsolatedCase", "LAUNCHER", "ME", "OTHER", "PLUGIN_ID", "REPO", "SAMPLE_END", "Stubs", "bytecode_dirs", "cli_env",
    "clock", "commit", "day_json", "git", "identity", "init_repo", "isolated_env", "make_sample", "sample_days",
    "sample_summary", "stamp",
]
