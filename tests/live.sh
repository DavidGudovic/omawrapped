#!/usr/bin/sh
# tests/live.sh [seconds]
# Runs the real sampler against your live session (default: 300 seconds)
# without installing anything, and prints what it saw and what it recorded.
#
# It starts a second Quickshell that has no window: nothing is drawn, no
# focus is taken, no notification is sent. What it records goes to a
# throwaway directory, never to ~/.local/share/omawrapped. Use the desktop
# normally while it runs; leave it alone for half a minute to see it pause.
set -eu

SECONDS_TO_RUN=${1:-300}
case "$SECONDS_TO_RUN" in ''|*[!0-9]*) echo "usage: live.sh [seconds]" >&2; exit 2 ;; esac

REPO=$(cd "$(dirname "$0")/.." && pwd)
RUN=$(/usr/bin/mktemp -d "${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR is not set}/ow-live.XXXXXX")
case "$RUN" in /*/ow-live.??????) ;; *) echo "live: unusable run dir" >&2; exit 1 ;; esac
trap 'rm -rf -- "$RUN"' EXIT
mkdir "$RUN/root"
cp -- "$REPO/tests/live/shell.qml" "$RUN/root/shell.qml"

XDG_DATA_HOME="$RUN/data" OW_REPO="$REPO" OW_SECONDS="$SECONDS_TO_RUN" \
  QS_DISABLE_CRASH_HANDLER=1 QS_NO_RELOAD_POPUP=1 QS_DISABLE_FILE_WATCHER=1 \
  /usr/bin/quickshell -p "$RUN/root/shell.qml" > "$RUN/out.txt" 2>&1 &
PID=$!

# CPU time the whole process used, read just before it ends: an upper bound
# on what the sampler costs, since most of it is Quickshell starting up.
sleep "$((SECONDS_TO_RUN > 3 ? SECONDS_TO_RUN - 2 : 1))"
TICKS=$(awk '{ print $14 + $15 }' "/proc/$PID/stat" 2>/dev/null || echo "")
RSS=$(awk '/^VmRSS:/ { print $2 }' "/proc/$PID/status" 2>/dev/null || echo "")
wait "$PID" || true

grep -ao 'OW-LIVE .*' "$RUN/out.txt" || true
grep -a ' WARN' "$RUN/out.txt" | grep -a -e "$REPO/" >&2 || true
if [ -n "$TICKS" ]; then
  HZ=$(getconf CLK_TCK)
  echo "live: process used $(awk -v t="$TICKS" -v hz="$HZ" 'BEGIN { printf "%.2f", t / hz }')s of CPU in ${SECONDS_TO_RUN}s (startup included), ${RSS} kB resident"
fi
