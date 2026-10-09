"""Helpers shared by the OmaWrapped tests.

Import this module before anything else in a test module. It switches
bytecode off (no __pycache__ may appear in the repository), puts the
repository on sys.path, and provides the isolation every test depends on:
HOME, the XDG folders and OMARCHY_PATH all point into a throwaway directory,
git reads no configuration of the machine, and the CLI is run with stub
versions of every program that would touch the live desktop.
"""

import sys

sys.dont_write_bytecode = True

# flake8: noqa: E402
# ruff: noqa: E402
import itertools
import json
import os
import random
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

# Every program the command starts that would reach the live desktop. Each has a stand-in, always.
STUBBED = ("omarchy-shell", "omarchy-bar", "wl-copy", "xdg-open", "nautilus", "uwsm-app", "notify-send",
           "omarchy-notification-send", "omarchy-menu-select")
# What the command needs besides, and that does nothing to the desktop: these are the real programs.
TOOLS = ("rsvg-convert", "fc-match", "git")
# Where the real tools are looked for: the system's folders, never the PATH of whoever runs the tests.
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"


def real_tool(name: str):
    """Where the real program is on this machine, or None. For the tests that need it, to skip without it."""
    return shutil.which(name, path=SYSTEM_PATH)


# What every stand-in runs. /usr/bin/python3 is absolute, so a stand-in starts whatever its PATH. It logs one JSON
# list of its arguments per call (a tab or a newline in an argument stays what it was) and exits 0, unless the test
# made it fail. wl-copy also keeps what it read on its standard input. A stand-in that fails says why on stderr when
# the test gave a reason, and omarchy-bar always does.
STAND_IN = """#!/usr/bin/python3 -IBS
import json, os, sys

name, logs, state = %(name)s, %(logs)s, %(state)s
arguments = [os.fsencode(argument).decode("utf-8", "backslashreplace") for argument in sys.argv[1:]]
log = os.path.join(logs, name + ".log")
try:
    with open(log, encoding="utf-8") as handle:
        number = len(handle.readlines())
except FileNotFoundError:
    number = 0
if name == "wl-copy" and not sys.stdin.isatty():
    with open(os.path.join(logs, "%%s.%%d.stdin" %% (name, number)), "wb") as handle:
        handle.write(sys.stdin.buffer.read())
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(arguments) + "\\n")


def read(*parts):
    try:
        with open(os.path.join(state, *parts), encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


code = read("exit", name)
if code is not None:
    reason = read("reason", name)
    if reason is None and name == "omarchy-bar":
        reason = "no widget is called that"
    if reason:
        sys.stderr.write(reason if reason.endswith("\\n") else reason + "\\n")
    sys.exit(int(code))
if name == "omarchy-shell":
    # The sampler answers a canned reply to `status`, and "ok" to anything else unless it has been made deaf.
    method = arguments[1] if len(arguments) > 1 else ""
    if method == "status":
        # Replies given one to a call are used in turn, the last one for good; an empty line is no answer.
        replies = read("shell-status-replies")
        if replies is None:
            sys.stdout.write(read("shell-status-reply") or "")
        else:
            lines = replies.splitlines() or [""]
            seen = int(read("shell-status-seen") or 0)
            with open(os.path.join(state, "shell-status-seen"), "w", encoding="utf-8") as handle:
                handle.write(str(seen + 1))
            sys.stdout.write(lines[min(seen, len(lines) - 1)])
    elif read("shell-is-deaf") is None:
        print("ok")
elif name == "omarchy-bar":
    # `set <id> <key> <value> --json` reports the change as Omarchy does.
    if arguments[:1] == ["set"] and len(arguments) >= 3:
        print("Set %%s on %%s" %% (arguments[2], arguments[1]))
elif name == "omarchy-menu-select":
    # The menu answers with the label the test chose; with none, it was dismissed.
    choice = read("menu-choice")
    if not choice:
        sys.exit(1)
    sys.stdout.write(choice)
"""


class Stubs:
    """Stand-ins for the programs the CLI starts, which must never reach the real ones.

    All of STUBBED have one, in their own folder, and the PATH of a run is that folder and a second one that holds
    links to the harmless real tools (TOOLS) and nothing else: no program of the desktop can be found by accident,
    whatever is installed on the machine. A tool that is not installed is simulated by removing its stand-in.

    Each stand-in appends its arguments to its own log, one line per call, and exits 0. omarchy-shell can also be
    given a canned reply to `status`, or one reply after the other; it answers "ok" to anything else, as the sampler
    does. omarchy-bar reports `set` as Omarchy does and changes nothing: no test can alter the real configuration.
    """

    def __init__(self, root: Path):
        self.dir = root / "stubs"
        self.tools = root / "tools"
        self.logs = root / "stublogs"
        self.state = root / "stubstate"
        for folder in (self.dir, self.tools, self.logs, self.state, self.state / "exit", self.state / "reason"):
            folder.mkdir()
        self.reply = self.state / "shell-status-reply"
        self.replies = self.state / "shell-status-replies"
        self.deaf = self.state / "shell-is-deaf"
        self.choice = self.state / "menu-choice"
        for name in STUBBED:
            self._write_stand_in(self.dir / name)
        for name in TOOLS:
            found = real_tool(name)
            if found:
                (self.tools / name).symlink_to(found)
        for name in STUBBED:
            if shutil.which(name, path=self.path) != str(self.dir / name):
                raise RuntimeError("%s would not be the stand-in, so a test could reach the real one" % name)

    def _write_stand_in(self, script: Path) -> None:
        """The stand-in, named like the file, in place."""
        script.write_text(STAND_IN % {"name": json.dumps(script.name), "logs": json.dumps(str(self.logs)),
                                      "state": json.dumps(str(self.state))}, encoding="utf-8")
        script.chmod(0o755)

    @property
    def path(self) -> str:
        """The PATH the CLI runs with: the stand-ins first, then the harmless tools. Nothing else."""
        return "%s:%s" % (self.dir, self.tools)

    def remove(self, *names: str) -> None:
        """Makes programs "not installed": their stand-ins go, and the real ones cannot be reached."""
        for name in names:
            (self.dir / name).unlink()
            if shutil.which(name, path=self.path):
                raise RuntimeError("%s can still be found on the path of the tests" % name)

    def fail(self, name: str, code: int = 1, reason: str = None) -> None:
        """From now on the stand-in exits with `code`, after logging its call and printing `reason` on stderr.

        Without a reason it prints nothing, except omarchy-bar, which always says something; reason="" makes it
        say nothing too.
        """
        (self.state / "exit" / name).write_text(str(code), encoding="utf-8")
        if reason is not None:
            (self.state / "reason" / name).write_text(reason, encoding="utf-8")

    def replace_tool(self, name: str) -> None:
        """Puts a stand-in in place of a harmless real tool (one of TOOLS), to see whether it is ever started."""
        (self.tools / name).unlink(missing_ok=True)
        self._write_stand_in(self.tools / name)

    def choose_in_menu(self, label: str) -> None:
        """The menu stand-in answers with this label. Without it, the menu was dismissed."""
        self.choice.write_text(label + "\n", encoding="utf-8")

    def argv(self, name: str) -> list:
        """The arguments of every call so far to a stand-in, one list per call."""
        try:
            lines = (self.logs / (name + ".log")).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines]

    def calls(self, name: str) -> list:
        """The arguments of every call so far, one string per call: the arguments, a space apart."""
        return [" ".join(arguments) for arguments in self.argv(name)]

    def stdin(self, name: str, call: int = -1) -> bytes:
        """What a call to wl-copy read on its standard input (the last call, unless told which)."""
        calls = len(self.argv(name))
        return (self.logs / ("%s.%d.stdin" % (name, range(calls)[call]))).read_bytes()

    def wait_for_argv(self, name: str, timeout: float = 5.0) -> list:
        """argv(), once there is at least one call (the program may be started detached)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.argv(name)
            if found:
                return found
            time.sleep(0.05)
        return self.argv(name)

    def wait_for_call(self, name: str, timeout: float = 5.0) -> list:
        """calls(), once there is at least one."""
        self.wait_for_argv(name, timeout)
        return self.calls(name)

    def reply_to_status(self, text: str) -> None:
        """The shell answers `status` with this text, every time. It replaces any replies given in turn."""
        self.replies.unlink(missing_ok=True)
        self.reply.write_text(text, encoding="utf-8")

    def reply_to_status_in_turn(self, *texts: str) -> None:
        """The shell answers `status` with the first text, then the second, and so on; the last one for good.

        An empty text is no answer at all. Each is one line, as the sampler's JSON is. It replaces any reply given
        before, and the count of the calls starts again.
        """
        for text in texts:
            if "\n" in text:
                raise ValueError("a reply is one line")
        (self.state / "shell-status-seen").unlink(missing_ok=True)
        self.replies.write_text("".join(text + "\n" for text in texts), encoding="utf-8")

    def stop_answering(self) -> None:
        """From now on flush and discard get no answer, as from a shell that is hanging."""
        self.deaf.write_text("", encoding="utf-8")


class StubbedCase(IsolatedCase):
    """A test that calls the code under test in this process, with the stand-ins as the only desktop programs.

    self.stubs is the Stubs of this test, and PATH is theirs until the test is over.
    """

    def setUp(self):
        super().setUp()
        self.stubs = Stubs(self.tmp)
        patcher = mock.patch.dict(os.environ, {"PATH": self.stubs.path})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(shutil.which("nautilus"), str(self.stubs.dir / "nautilus"))


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
    "IsolatedCase", "LAUNCHER", "ME", "OTHER", "PLUGIN_ID", "REPO", "SAMPLE_END", "STUBBED", "Stubs", "StubbedCase",
    "bytecode_dirs", "cli_env", "clock", "commit", "day_json", "git", "identity", "init_repo", "isolated_env",
    "make_sample", "real_tool", "sample_days", "sample_summary", "stamp",
]
