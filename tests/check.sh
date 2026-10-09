#!/usr/bin/sh
# tests/check.sh
# The one-command gate. Runs, in order, and stops at the first failure: the
# accounting tests (node), the command's tests (python), the manifest check,
# then the real service and the real widget in a headless Quickshell.
#
# Nothing here touches the live session, the clipboard or your recorded
# data: every part runs in a private temporary home.
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

node --test tests/tracker.test.js
/usr/bin/python3 -B -m unittest discover -s tests -p 'test_*.py'
omarchy plugin validate "$REPO"
tests/harness.sh service
tests/harness.sh widget

# The shell reloads every plugin when a file appears in a plugin folder, so
# running the code must never leave one behind.
if [ -n "$(find "$REPO" -name __pycache__ -not -path "$REPO/.git/*")" ]; then
  echo "check: bytecode was written into the repository" >&2
  exit 1
fi
echo "check: ok"
