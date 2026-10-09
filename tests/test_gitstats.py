import support  # noqa: F401  (must stay first: it disables bytecode and isolates the environment)

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from omawrapped import gitstats
from omawrapped.gitstats import GitStats, count_commits, find_repos
from support import ME, OTHER, IsolatedCase, commit, git, identity, init_repo

START = datetime(2026, 3, 10, tzinfo=timezone.utc)
END = datetime(2026, 3, 11, tzinfo=timezone.utc)
NOON = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)
LONG_AGO = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
SECOND = timedelta(seconds=1)


def fake_repo(path: Path) -> Path:
    """A folder that find_repos takes for a repository (it has a .git). Returns its resolved path."""
    (path / ".git").mkdir(parents=True)
    return path.resolve()


class FindReposTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"

    def test_finds_repositories_at_several_depths(self):
        expected = [fake_repo(self.root / "one"), fake_repo(self.root / "group" / "two"),
                    fake_repo(self.root / "a" / "b" / "three")]
        self.assertEqual(sorted(find_repos([self.root])), sorted(expected))

    def test_a_folder_that_is_itself_a_repository_is_found(self):
        root = fake_repo(self.root)
        self.assertEqual(find_repos([self.root]), [root])

    def test_a_git_file_marks_a_repository_too(self):
        # Worktrees and submodules have a .git file instead of a folder.
        folder = self.root / "worktree"
        folder.mkdir(parents=True)
        (folder / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
        self.assertEqual(find_repos([self.root]), [folder.resolve()])

    def test_does_not_descend_into_a_repository(self):
        outer = fake_repo(self.root / "outer")
        fake_repo(self.root / "outer" / "vendor" / "inner")
        self.assertEqual(find_repos([self.root]), [outer])

    def test_skips_hidden_folders(self):
        fake_repo(self.root / ".hidden" / "repo")
        fake_repo(self.root / "visible" / ".cache" / "repo")
        shown = fake_repo(self.root / "visible" / "repo")
        self.assertEqual(find_repos([self.root]), [shown])

    def test_respects_max_depth(self):
        deepest_found = fake_repo(self.root.joinpath(*["d"] * gitstats.MAX_DEPTH))
        fake_repo(self.root.joinpath(*["e"] * (gitstats.MAX_DEPTH + 1)))
        self.assertEqual(find_repos([self.root]), [deepest_found])

    def test_stops_at_max_repos(self):
        for name in "abcde":
            fake_repo(self.root / name)
        with mock.patch.object(gitstats, "MAX_REPOS", 3):
            self.assertEqual(len(find_repos([self.root])), 3)

    def test_a_missing_folder_gives_nothing(self):
        self.assertEqual(find_repos([self.tmp / "does-not-exist"]), [])
        self.assertEqual(find_repos([]), [])

    def test_a_file_in_place_of_a_folder_gives_nothing(self):
        path = self.write("root/file.txt", "x")
        self.assertEqual(find_repos([path]), [])

    def test_a_missing_folder_does_not_hide_the_others(self):
        found = fake_repo(self.root / "repo")
        self.assertEqual(find_repos([self.tmp / "nope", self.root]), [found])

    def test_tilde_is_expanded_to_home(self):
        found = fake_repo(self.home / "code" / "proj")
        self.assertEqual(find_repos(["~/code"]), [found])
        self.assertEqual(find_repos([Path("~/code")]), [found])
        self.assertEqual(find_repos(["~"]), [found])

    def test_a_repository_is_listed_once_when_the_folders_overlap(self):
        found = fake_repo(self.root / "sub" / "repo")
        self.assertEqual(find_repos([self.root, self.root / "sub"]), [found])
        self.assertEqual(find_repos([self.root / "sub", self.root]), [found])
        self.assertEqual(find_repos([self.root, self.root]), [found])
        self.assertEqual(find_repos([self.root, self.root / "sub" / ".." / "sub"]), [found])

    def test_gives_up_on_a_tree_with_too_many_folders(self):
        for name in "abcdefghij":
            fake_repo(self.root / name)
        with mock.patch.object(gitstats, "MAX_FOLDERS", 4):
            found = find_repos([self.root])
        self.assertGreater(len(found), 0)
        self.assertLessEqual(len(found), 4)

    def test_paths_may_be_strings_or_paths(self):
        found = fake_repo(self.root / "repo")
        self.assertEqual(find_repos([str(self.root)]), [found])
        self.assertEqual(find_repos([self.root]), [found])


class CountCommitsTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "repos"
        self.root.mkdir()

    def count(self, start=START, end=END, dirs=None):
        return count_commits([self.root] if dirs is None else dirs, start, end)

    def test_counts_commits_authored_inside_the_window(self):
        repo = init_repo(self.root / "a")
        commit(repo, NOON)
        commit(repo, NOON + timedelta(hours=3))
        self.assertEqual(self.count(), GitStats(commits=2, repos=1, scanned=1))

    def test_window_is_closed_at_start_and_open_at_end(self):
        repo = init_repo(self.root / "a")
        commit(repo, START - SECOND)
        commit(repo, END)
        self.assertEqual(self.count().commits, 0)
        commit(repo, START)
        commit(repo, END - SECOND)
        self.assertEqual(self.count().commits, 2)

    def test_old_and_future_commits_are_left_out(self):
        repo = init_repo(self.root / "a")
        commit(repo, LONG_AGO)
        commit(repo, NOON)
        commit(repo, datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(self.count().commits, 1)

    def test_naive_datetimes_are_local_time(self):
        repo = init_repo(self.root / "a")
        commit(repo, datetime(2026, 3, 9, 23, 59, 59))
        commit(repo, datetime(2026, 3, 10, 12))
        commit(repo, datetime(2026, 3, 11))
        self.assertEqual(self.count(datetime(2026, 3, 10), datetime(2026, 3, 11)).commits, 1)

    def test_commits_by_another_email_are_not_mine(self):
        repo = init_repo(self.root / "a")
        commit(repo, NOON, email=OTHER)
        self.assertEqual(self.count(), GitStats(commits=0, repos=0, scanned=1))
        commit(repo, NOON, email=ME)
        self.assertEqual(self.count(), GitStats(commits=1, repos=1, scanned=1))

    def test_emails_are_compared_without_regard_to_case(self):
        repo = init_repo(self.root / "a", email="Me@Example.Invalid")
        commit(repo, NOON, email="me@EXAMPLE.invalid")
        self.assertEqual(self.count().commits, 1)

    def test_the_author_date_decides_not_the_commit_date(self):
        repo = init_repo(self.root / "a")
        # Written before the window, committed (say, rebased) inside it.
        commit(repo, START - timedelta(days=3), committed=NOON)
        self.assertEqual(self.count().commits, 0)
        # Written inside the window, committed after it.
        commit(repo, NOON, committed=END + timedelta(days=2))
        self.assertEqual(self.count().commits, 1)

    def test_a_commit_authored_in_the_window_counts_even_if_its_commit_date_is_earlier(self):
        # git allows any commit date (a slow clock, GIT_COMMITTER_DATE); only the author date matters here.
        repo = init_repo(self.root / "a")
        commit(repo, NOON, committed=START - timedelta(days=1))
        self.assertEqual(self.count().commits, 1)

    def test_commits_behind_a_child_with_an_earlier_commit_date_are_still_counted(self):
        # Clock skew, or a rebase that keeps old commit dates, leaves a child "older" than its parent.
        repo = init_repo(self.root / "a")
        commit(repo, NOON)
        commit(repo, NOON + timedelta(hours=1), committed=START - timedelta(days=2))
        self.assertEqual(self.count().commits, 2)

    def test_git_is_not_asked_about_commits_made_long_before_the_window(self):
        # The price of not reading a whole history: a commit date further back than LOOKBACK is out of
        # sight, whatever its author date says. No working clock produces one.
        repo = init_repo(self.root / "a")
        commit(repo, NOON, committed=START - gitstats.LOOKBACK - timedelta(days=1))
        self.assertEqual(self.count().commits, 0)

    def test_merge_commits_are_not_counted(self):
        repo = init_repo(self.root / "a")
        commit(repo, LONG_AGO)
        git(repo, "checkout", "-q", "-b", "feature")
        commit(repo, NOON)
        git(repo, "checkout", "-q", "main")
        commit(repo, NOON + timedelta(hours=1))
        git(repo, "merge", "--no-ff", "-q", "-m", "merge feature", "feature", env=identity(NOON + timedelta(hours=2)))
        self.assertEqual(git(repo, "rev-list", "--merges", "--count", "HEAD").strip(), "1")
        self.assertEqual(self.count().commits, 2)

    def test_a_commit_on_two_branches_counts_once(self):
        repo = init_repo(self.root / "a")
        commit(repo, NOON)
        git(repo, "branch", "other")
        git(repo, "branch", "third")
        self.assertEqual(self.count().commits, 1)

    def test_the_same_repository_cloned_twice_counts_once(self):
        original = init_repo(self.root / "original")
        commit(original, NOON)
        for name in ("clone-1", "clone-2"):
            git(self.root, "clone", "-q", str(original), str(self.root / name))
            git(self.root / name, "config", "user.email", ME)
        stats = self.count()
        self.assertEqual(stats.commits, 1)
        self.assertEqual(stats.scanned, 3)
        # One commit cannot come from more than one repository.
        self.assertEqual(stats.repos, 1)

    def test_a_clone_adds_only_what_the_original_lacks(self):
        original = init_repo(self.root / "original")
        commit(original, NOON)
        clone = self.root / "clone"
        git(self.root, "clone", "-q", str(original), str(clone))
        git(clone, "config", "user.email", ME)
        commit(clone, NOON + timedelta(hours=1))
        self.assertEqual(self.count().commits, 2)

    def test_a_commit_only_on_a_branch_that_is_not_checked_out_counts(self):
        repo = init_repo(self.root / "a")
        commit(repo, LONG_AGO)
        git(repo, "checkout", "-q", "-b", "feature")
        commit(repo, NOON)
        git(repo, "checkout", "-q", "main")
        self.assertNotIn(git(repo, "rev-parse", "feature").strip(), git(repo, "rev-list", "HEAD"))
        self.assertEqual(self.count().commits, 1)

    def test_a_commit_only_on_a_remote_tracking_branch_counts(self):
        origin = init_repo(self.tmp / "origin")
        commit(origin, LONG_AGO)
        mine = self.root / "mine"
        git(self.root, "clone", "-q", str(origin), str(mine))
        git(mine, "config", "user.email", ME)
        # Somebody pushed it, and I fetched it without merging.
        commit(origin, NOON)
        git(mine, "fetch", "-q", "origin")
        self.assertEqual(self.count().commits, 1)

    def test_a_repository_with_no_commits_counts_zero(self):
        init_repo(self.root / "empty")
        self.assertEqual(self.count(), GitStats(commits=0, repos=0, scanned=1))

    def test_a_branch_without_commits_does_not_hide_the_other_branches(self):
        repo = init_repo(self.root / "a")
        commit(repo, NOON)
        git(repo, "checkout", "-q", "--orphan", "fresh")
        self.assertEqual(self.count().commits, 1)

    def test_a_repository_without_user_email_is_scanned_but_adds_nothing(self):
        repo = init_repo(self.root / "anonymous", email=None)
        commit(repo, NOON)
        self.assertEqual(git(repo, "config", "--get", "user.email", check=False), "")
        self.assertEqual(self.count(), GitStats(commits=0, repos=0, scanned=1))

    def test_an_anonymous_repository_does_not_hide_the_others(self):
        commit(init_repo(self.root / "anonymous", email=None), NOON)
        commit(init_repo(self.root / "named"), NOON)
        self.assertEqual(self.count(), GitStats(commits=1, repos=1, scanned=2))

    def test_the_global_email_is_used_when_the_repository_has_none(self):
        (self.tmp / "gitconfig").write_text("[user]\n\temail = %s\n" % ME, encoding="utf-8")
        repo = init_repo(self.root / "a", email=None)
        commit(repo, NOON, email=ME)
        commit(repo, NOON, email=OTHER)
        self.assertEqual(self.count(), GitStats(commits=1, repos=1, scanned=1))

    def test_the_repository_email_beats_the_global_one(self):
        (self.tmp / "gitconfig").write_text("[user]\n\temail = %s\n" % OTHER, encoding="utf-8")
        repo = init_repo(self.root / "a", email=ME)
        commit(repo, NOON, email=ME)
        commit(repo, NOON, email=OTHER)
        self.assertEqual(self.count().commits, 1)

    def test_scanned_and_repos_add_up_across_repositories(self):
        busy = init_repo(self.root / "busy")
        commit(busy, NOON)
        commit(busy, NOON + timedelta(hours=1))
        commit(init_repo(self.root / "quiet"), LONG_AGO)
        commit(init_repo(self.root / "group" / "deep"), NOON)
        init_repo(self.root / "empty")
        self.assertEqual(self.count(), GitStats(commits=3, repos=2, scanned=4))

    def test_several_folders_are_all_scanned(self):
        commit(init_repo(self.tmp / "one" / "a"), NOON)
        commit(init_repo(self.tmp / "two" / "b"), NOON)
        stats = self.count(dirs=[self.tmp / "one", self.tmp / "two", self.tmp / "missing"])
        self.assertEqual(stats, GitStats(commits=2, repos=2, scanned=2))

    def test_without_git_nothing_is_counted_and_nothing_fails(self):
        commit(init_repo(self.root / "a"), NOON)
        nowhere = self.tmp / "no-programs"
        nowhere.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(nowhere)}):
            self.assertEqual(self.count(), GitStats())

    def test_no_scanned_folder_gives_empty_stats(self):
        self.assertEqual(self.count(dirs=[self.tmp / "missing"]), GitStats())
        self.assertEqual(self.count(dirs=[]), GitStats())
        self.assertEqual(self.count(dirs=[self.root]), GitStats())
        self.assertEqual(GitStats().scanned, 0)

    def test_the_scan_changes_nothing_in_the_repository(self):
        repo = init_repo(self.root / "a")
        commit(repo, LONG_AGO)
        commit(repo, NOON)
        (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-q", "-m", "file", env=identity(NOON))
        (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
        (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
        git(repo, "add", "staged.txt")

        def snapshot():
            files = {}
            for folder, _, names in os.walk(repo / ".git"):
                for name in names:
                    stat = os.stat(os.path.join(folder, name))
                    files[os.path.relpath(os.path.join(folder, name), repo / ".git")] = (stat.st_size, stat.st_mtime_ns)
            return {
                "status": git(repo, "status", "--porcelain", env={"GIT_OPTIONAL_LOCKS": "0"}),
                "head": git(repo, "rev-parse", "HEAD"),
                "refs": git(repo, "for-each-ref"),
                "git_dir": files,
            }

        before = snapshot()
        self.assertIn("?? untracked.txt", before["status"])
        self.assertIn("A  staged.txt", before["status"])
        self.assertEqual(self.count().commits, 2)
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
