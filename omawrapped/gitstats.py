"""Counts the commits you made, in the repositories under a few folders.

Read-only: every repository is asked for its log and its configured e-mail,
nothing else. No fetch, no network, no hooks.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DIRS = ("~/projects", "~/code")
MAX_DEPTH = 4
MAX_REPOS = 500
# The walk gives up after this many folders: a tree that wide is not a
# folder of projects, and the card should not wait on it.
MAX_FOLDERS = 20000
TIMEOUT = 15
# How far before the period git is asked to look. A commit is normally
# committed at or after the moment it was authored; the week covers clocks
# that disagree, without reading the whole history of a large repository.
LOOKBACK = timedelta(days=7)

# Git is never to prompt, lock, page, verify signatures or reach a remote.
ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_PAGER": "cat",
    "LC_ALL": "C",
}


@dataclass
class GitStats:
    commits: int = 0
    # Repositories with at least one counted commit.
    repos: int = 0
    # Repositories that were looked at.
    scanned: int = 0


def find_repos(dirs) -> list:
    """Repositories at most MAX_DEPTH folders below any of dirs.

    A repository is not searched for more repositories, and hidden folders
    are skipped, which keeps the walk away from node_modules and the like.
    Linked folders are followed, and each real folder is visited once, so a
    link that leads back up cannot make the walk go round.
    """
    repos, seen = [], set()
    stack = [(Path(os.path.expanduser(str(d))), 0) for d in dirs]
    while stack and len(repos) < MAX_REPOS and len(seen) < MAX_FOLDERS:
        folder, depth = stack.pop()
        try:
            real = folder.resolve()
            if real in seen or not real.is_dir():
                continue
            seen.add(real)
            if (real / ".git").exists():
                repos.append(real)
                continue
            if depth >= MAX_DEPTH:
                continue
            with os.scandir(real) as entries:
                children = [e.path for e in entries if not e.name.startswith(".") and e.is_dir()]
        except OSError:
            continue
        stack.extend((Path(child), depth + 1) for child in sorted(children, reverse=True))
    return repos


def _git(repo, *args):
    """Output of a git command in repo, or None when it fails for any reason."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "--no-pager", *args],
            capture_output=True, text=True, errors="replace", timeout=TIMEOUT,
            stdin=subprocess.DEVNULL, env={**os.environ, **ENV},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def count_commits(dirs, start: datetime, end: datetime) -> GitStats:
    """Commits authored from start up to (not including) end, by you.

    "You" is the e-mail each repository commits with: its own setting, else
    the global one. A commit counts once however many branches or clones
    hold it; merge commits are left out. Only commits committed since
    LOOKBACK before start are looked at.
    """
    stats = GitStats()
    if not shutil.which("git"):
        return stats
    counted = set()
    for repo in find_repos(dirs):
        stats.scanned += 1
        mine = (_git(repo, "config", "--get", "user.email") or "").strip().lower()
        if not mine:
            continue
        # --since reads the commit date and only narrows the log; the author
        # date decides below.
        options = ["--no-merges", "--no-show-signature", "--since=" + (start - LOOKBACK).isoformat(),
                   "--format=%H%x09%at%x09%ae"]
        log = _git(repo, "log", "--branches", "--remotes", "HEAD", *options)
        if log is None:
            # HEAD names no commit yet (a new branch); the others still count.
            log = _git(repo, "log", "--branches", "--remotes", *options) or ""
        found = False
        for line in log.splitlines():
            parts = line.split("\t")
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            commit, authored, email = parts[0], int(parts[1]), parts[2].strip().lower()
            if email != mine or not start.timestamp() <= authored < end.timestamp():
                continue
            if commit not in counted:
                counted.add(commit)
                found = True
        if found:
            stats.repos += 1
    stats.commits = len(counted)
    return stats
